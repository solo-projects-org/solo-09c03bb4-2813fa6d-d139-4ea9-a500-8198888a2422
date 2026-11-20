"""SQLite persistence for daily venue sketches and monthly summaries.

Invariants
----------
* A daily sketch and the recomputed monthly summary are written in one
  ``BEGIN IMMEDIATE`` transaction.  Either both become visible or neither does.
* Every write transaction re-reads all current daily sketches of the affected
  month before merging.  SQLite serialises writers, so concurrent backfills
  cannot commit a summary that misses another writer's current sketch.
* A correction always recomputes the month from scratch, so raising the
  minimum precision or lowering register values is reflected instead of being
  frozen into an incrementally merged summary.
* A ``test_list_confirmed`` flag is stored only together with a newly written
  revision (created or replaced).  It certifies that this revision was rebuilt
  without the month's registered test-swipe tickets, and it never carries over
  to a later revision.
* The monthly audit reads the summary, the current daily sketches, and the
  registered test list inside one deferred read transaction, so its verdict
  and the report it may release come from the same committed snapshot while
  holding no write lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from . import hll

DEFAULT_DB_PATH = os.environ.get("MUSEUM_DB_PATH", "/data/museum.db")
BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class DailySketch:
    venue_id: str
    day: str
    revision: int
    p: int
    registers: list[int]
    content_hash: str
    updated_at: str
    test_list_confirmed: bool


@dataclass(frozen=True)
class MonthlySummary:
    month: str
    p: int
    registers: list[int]
    day_count: int
    venue_count: int
    updated_at: str


@dataclass(frozen=True)
class IngestOutcome:
    status: str  # created | replaced | unchanged | ignored
    venue_id: str
    day: str
    revision: int
    current_revision: int
    month: str
    summary: MonthlySummary | None
    test_list_confirmed: bool  # the flag as stored for the current revision


@dataclass(frozen=True)
class TestList:
    month: str
    tickets: list[str]
    updated_at: str


@dataclass(frozen=True)
class TestListOutcome:
    status: str  # created | unchanged
    month: str
    ticket_count: int
    updated_at: str


@dataclass(frozen=True)
class AuditSnapshot:
    """One committed view of everything the monthly audit needs."""

    summary: MonthlySummary | None
    dailies: list[DailySketch]
    test_list: TestList | None


class ConflictError(Exception):
    """Same revision was already stored with different content."""

    def __init__(self, venue_id: str, day: str, revision: int, current_revision: int):
        super().__init__(
            f"{venue_id}/{day} revision {revision} conflicts with stored content"
        )
        self.venue_id = venue_id
        self.day = day
        self.revision = revision
        self.current_revision = current_revision


class TestListConflictError(Exception):
    """A different test-swipe list is already registered for the month."""

    def __init__(self, month: str):
        super().__init__(
            f"a different test-swipe list is already registered for {month}; "
            "the monthly list is fixed once registered"
        )
        self.month = month


def month_of(day: str) -> str:
    return day[:7]


def next_month(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    if mon == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{mon + 1:02d}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def content_fingerprint(p: int, registers: Sequence[int]) -> str:
    """Canonical fingerprint of a sketch payload (precision + registers)."""
    payload = bytes([p]) + bytes(registers)
    return hashlib.sha256(payload).hexdigest()


class Storage:
    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.initialize()

    # -- connections -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        # journal_mode is persisted in the database file, so it is set once
        # here instead of on every connection (which would need a write lock).
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_sketch (
                    venue_id             TEXT    NOT NULL,
                    day                  TEXT    NOT NULL,
                    revision             INTEGER NOT NULL,
                    p                    INTEGER NOT NULL,
                    registers            BLOB    NOT NULL,
                    content_hash         TEXT    NOT NULL,
                    updated_at           TEXT    NOT NULL,
                    test_list_confirmed  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (venue_id, day)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_sketch_month
                    ON daily_sketch (day);
                CREATE TABLE IF NOT EXISTS monthly_summary (
                    month        TEXT    PRIMARY KEY,
                    p            INTEGER NOT NULL,
                    registers    BLOB    NOT NULL,
                    day_count    INTEGER NOT NULL,
                    venue_count  INTEGER NOT NULL,
                    updated_at   TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_ticket_list (
                    month        TEXT    PRIMARY KEY,
                    tickets_json TEXT    NOT NULL,
                    ticket_count INTEGER NOT NULL,
                    updated_at   TEXT    NOT NULL
                );
                """
            )
            # Databases created before the confirmation flag existed get it
            # here; CREATE TABLE IF NOT EXISTS cannot add columns.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(daily_sketch)")
            }
            if "test_list_confirmed" not in columns:
                conn.execute(
                    "ALTER TABLE daily_sketch "
                    "ADD COLUMN test_list_confirmed INTEGER NOT NULL DEFAULT 0"
                )
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            # Take the write lock before reading, so the recompute below sees
            # every sketch that was current when this writer started.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """One consistent snapshot for several reads, without the write lock.

        In WAL mode a deferred read transaction never blocks writers; every
        statement below sees the same committed state.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()  # read-only: just release the snapshot
        finally:
            conn.close()

    # -- ingest ----------------------------------------------------------
    def ingest(
        self,
        venue_id: str,
        day: str,
        revision: int,
        p: int,
        registers: Sequence[int],
        test_list_confirmed: bool = False,
    ) -> IngestOutcome:
        hll.validate_precision(p)
        clean = hll.validate_registers(p, registers)
        fingerprint = content_fingerprint(p, clean)
        month = month_of(day)

        with self._write_transaction() as conn:
            existing = conn.execute(
                "SELECT revision, content_hash, test_list_confirmed "
                "FROM daily_sketch WHERE venue_id = ? AND day = ?",
                (venue_id, day),
            ).fetchone()

            if existing is None:
                status = "created"
                stored_confirmation = test_list_confirmed
                conn.execute(
                    "INSERT INTO daily_sketch "
                    "(venue_id, day, revision, p, registers, content_hash, "
                    "updated_at, test_list_confirmed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        venue_id,
                        day,
                        revision,
                        p,
                        bytes(clean),
                        fingerprint,
                        utc_now(),
                        int(test_list_confirmed),
                    ),
                )
            elif revision > existing["revision"]:
                status = "replaced"
                stored_confirmation = test_list_confirmed
                conn.execute(
                    "UPDATE daily_sketch SET revision = ?, p = ?, registers = ?, "
                    "content_hash = ?, updated_at = ?, test_list_confirmed = ? "
                    "WHERE venue_id = ? AND day = ?",
                    (
                        revision,
                        p,
                        bytes(clean),
                        fingerprint,
                        utc_now(),
                        int(test_list_confirmed),
                        venue_id,
                        day,
                    ),
                )
            elif revision == existing["revision"]:
                if existing["content_hash"] == fingerprint:
                    return IngestOutcome(
                        status="unchanged",
                        venue_id=venue_id,
                        day=day,
                        revision=revision,
                        current_revision=existing["revision"],
                        month=month,
                        summary=self._read_summary(conn, month),
                        # Nothing was written, so a confirmation carried by an
                        # idempotent retry is not stored; the stored flag for
                        # the current revision is reported back unchanged.
                        test_list_confirmed=bool(existing["test_list_confirmed"]),
                    )
                raise ConflictError(venue_id, day, revision, existing["revision"])
            else:
                return IngestOutcome(
                    status="ignored",
                    venue_id=venue_id,
                    day=day,
                    revision=revision,
                    current_revision=existing["revision"],
                    month=month,
                    summary=self._read_summary(conn, month),
                    test_list_confirmed=bool(existing["test_list_confirmed"]),
                )

            summary = self._recompute_month(conn, month)
            return IngestOutcome(
                status=status,
                venue_id=venue_id,
                day=day,
                revision=revision,
                current_revision=revision,
                month=month,
                summary=summary,
                test_list_confirmed=stored_confirmation,
            )

    def _recompute_month(self, conn: sqlite3.Connection, month: str) -> MonthlySummary:
        start, end = f"{month}-01", f"{next_month(month)}-01"
        rows = conn.execute(
            "SELECT venue_id, day, p, registers FROM daily_sketch "
            "WHERE day >= ? AND day < ? ORDER BY day, venue_id",
            (start, end),
        ).fetchall()
        if not rows:
            raise RuntimeError(f"month {month} has no current daily sketches")

        sketches: list[tuple[int, list[int]]] = []
        days: set[str] = set()
        venues: set[str] = set()
        for row in rows:
            p = int(row["p"])
            registers = list(row["registers"])
            # Re-validate what we read: a corrupted row must not silently
            # poison the monthly summary.
            hll.validate_registers(p, registers)
            sketches.append((p, registers))
            venues.add(row["venue_id"])
            days.add(row["day"])

        merged = hll.merge_sketches(sketches)
        target_p = min(p for p, _ in sketches)
        now = utc_now()
        conn.execute(
            "INSERT INTO monthly_summary "
            "(month, p, registers, day_count, venue_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(month) DO UPDATE SET "
            "p = excluded.p, registers = excluded.registers, "
            "day_count = excluded.day_count, venue_count = excluded.venue_count, "
            "updated_at = excluded.updated_at",
            (month, target_p, bytes(merged), len(days), len(venues), now),
        )
        return MonthlySummary(
            month=month,
            p=target_p,
            registers=merged,
            day_count=len(days),
            venue_count=len(venues),
            updated_at=now,
        )

    # -- reads -----------------------------------------------------------
    @staticmethod
    def _row_to_daily(row: sqlite3.Row) -> DailySketch:
        p = int(row["p"])
        registers = list(row["registers"])
        hll.validate_registers(p, registers)
        return DailySketch(
            venue_id=row["venue_id"],
            day=row["day"],
            revision=int(row["revision"]),
            p=p,
            registers=registers,
            content_hash=row["content_hash"],
            updated_at=row["updated_at"],
            test_list_confirmed=bool(row["test_list_confirmed"]),
        )

    _DAILY_COLUMNS = (
        "venue_id, day, revision, p, registers, content_hash, updated_at, "
        "test_list_confirmed"
    )

    @staticmethod
    def _read_summary(conn: sqlite3.Connection, month: str) -> MonthlySummary | None:
        row = conn.execute(
            "SELECT month, p, registers, day_count, venue_count, updated_at "
            "FROM monthly_summary WHERE month = ?",
            (month,),
        ).fetchone()
        if row is None:
            return None
        p = int(row["p"])
        registers = list(row["registers"])
        hll.validate_registers(p, registers)
        return MonthlySummary(
            month=row["month"],
            p=p,
            registers=registers,
            day_count=int(row["day_count"]),
            venue_count=int(row["venue_count"]),
            updated_at=row["updated_at"],
        )

    def get_monthly(self, month: str) -> MonthlySummary | None:
        conn = self._connect()
        try:
            return self._read_summary(conn, month)
        finally:
            conn.close()

    def get_daily(self, venue_id: str, day: str) -> DailySketch | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._DAILY_COLUMNS} FROM daily_sketch "
                "WHERE venue_id = ? AND day = ?",
                (venue_id, day),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_daily(row)
        finally:
            conn.close()

    # -- test-swipe list ---------------------------------------------------
    @staticmethod
    def _canonical_tickets(tickets: Sequence[str]) -> list[str]:
        """The stored form of a list: duplicates removed, sorted."""
        return sorted(set(tickets))

    @staticmethod
    def _row_to_test_list(row: sqlite3.Row) -> TestList:
        tickets = json.loads(row["tickets_json"])
        if not isinstance(tickets, list) or not all(
            isinstance(ticket, str) for ticket in tickets
        ):
            raise RuntimeError(f"corrupt test-swipe list for {row['month']}")
        return TestList(
            month=row["month"], tickets=tickets, updated_at=row["updated_at"]
        )

    def register_test_list(self, month: str, tickets: Sequence[str]) -> TestListOutcome:
        """Register the month's fixed test-swipe list.

        The list is fixed once registered: re-registering the identical list
        is idempotent, a different one raises :class:`TestListConflictError`.
        """
        canonical = self._canonical_tickets(tickets)
        payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT tickets_json, updated_at FROM test_ticket_list WHERE month = ?",
                (month,),
            ).fetchone()
            if row is None:
                now = utc_now()
                conn.execute(
                    "INSERT INTO test_ticket_list "
                    "(month, tickets_json, ticket_count, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (month, payload, len(canonical), now),
                )
                return TestListOutcome(
                    status="created",
                    month=month,
                    ticket_count=len(canonical),
                    updated_at=now,
                )
            if row["tickets_json"] == payload:
                return TestListOutcome(
                    status="unchanged",
                    month=month,
                    ticket_count=len(canonical),
                    updated_at=row["updated_at"],
                )
            raise TestListConflictError(month)

    def get_test_list(self, month: str) -> TestList | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT month, tickets_json, updated_at FROM test_ticket_list "
                "WHERE month = ?",
                (month,),
            ).fetchone()
            return self._row_to_test_list(row) if row is not None else None
        finally:
            conn.close()

    # -- audit ---------------------------------------------------------------
    def get_audit_snapshot(self, month: str) -> AuditSnapshot:
        """Read summary, current dailies, and test list from one snapshot.

        Everything is read inside a single deferred read transaction, so the
        audit verdict and the monthly report it may release are computed from
        the same committed data.  No write lock is taken.
        """
        start, end = f"{month}-01", f"{next_month(month)}-01"
        with self._read_transaction() as conn:
            summary = self._read_summary(conn, month)
            rows = conn.execute(
                f"SELECT {self._DAILY_COLUMNS} FROM daily_sketch "
                "WHERE day >= ? AND day < ? ORDER BY day, venue_id",
                (start, end),
            ).fetchall()
            dailies = [self._row_to_daily(row) for row in rows]
            row = conn.execute(
                "SELECT month, tickets_json, updated_at FROM test_ticket_list "
                "WHERE month = ?",
                (month,),
            ).fetchone()
            test_list = self._row_to_test_list(row) if row is not None else None
        return AuditSnapshot(summary=summary, dailies=dailies, test_list=test_list)
