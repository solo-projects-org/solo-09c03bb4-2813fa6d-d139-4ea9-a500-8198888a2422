"""Test-swipe audit of a monthly summary.

Before the monthly report goes to the statistics group, every non-empty
monthly register must be explainable without the month's registered
test-swipe tickets.

Definitions
-----------
* A *source* of a monthly bucket is a daily register whose folded value at
  the monthly precision equals the monthly register's current value — the
  daily buckets that "reach" the monthly maximum.
* A source *hits* a test-swipe ticket when the ticket hashes to the same
  bucket **and** the same rho at the daily sketch's own (original)
  precision.  Such a register value may be fully explained by the test
  swipe; a register that hits no test ticket must come from real traffic.
* A monthly bucket passes when at least one of its sources hits no test
  ticket, or when the current revision of that source's daily sketch was
  confirmed as rebuilt without the listed test tickets.  Otherwise the
  bucket fails and every source of it must be re-reported.

The audit is a pure function of one committed snapshot (monthly summary,
current daily sketches, registered test list); it takes no lock itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import hll
from .store import DailySketch, MonthlySummary


@dataclass(frozen=True)
class AuditSource:
    """One daily register that reaches a monthly bucket's current value."""

    venue_id: str
    day: str
    revision: int
    p: int
    day_bucket: int
    day_value: int
    matched_ticket: str | None  # the listed test ticket hit, if any
    confirmed: bool  # current revision confirmed rebuilt without test swipes


@dataclass(frozen=True)
class BucketFailure:
    bucket: int
    month_value: int
    sources: tuple[AuditSource, ...]


@dataclass(frozen=True)
class AuditResult:
    month: str
    status: str  # "passed" | "failed"
    buckets_nonempty: int
    buckets_failed: int
    failures: tuple[BucketFailure, ...]


def fold_contribution(
    source_p: int, index: int, value: int, target_p: int
) -> tuple[int, int]:
    """Map one source register to ``(target bucket, folded value)``.

    Mirrors the fold inside :func:`app.hll.merge_sketches` so a source is
    attributed exactly when the merge would have taken its value.
    """
    hll.validate_precision(source_p)
    hll.validate_precision(target_p)
    if source_p < target_p:
        raise hll.SketchError(
            f"source precision {source_p} below target precision {target_p}"
        )
    shift = source_p - target_p
    target = index >> shift
    if value == 0:
        return target, 0
    offset = index & ((1 << shift) - 1)
    if offset == 0:
        return target, value + shift
    return target, shift - offset.bit_length() + 1


def _hit_tables(
    tickets: list[str], precisions: set[int]
) -> dict[int, dict[tuple[int, int], str]]:
    """For each precision: map ``(bucket, rho)`` to the first listed ticket."""
    hashed = sorted((ticket, hll.hash_ticket(ticket)) for ticket in tickets)
    tables: dict[int, dict[tuple[int, int], str]] = {}
    for p in precisions:
        table: dict[tuple[int, int], str] = {}
        for ticket, digest in hashed:
            split = hll.bucket_and_rho(digest, p)
            table.setdefault((split.bucket, split.rho), ticket)
        tables[p] = table
    return tables


def audit_month(
    summary: MonthlySummary,
    dailies: list[DailySketch],
    tickets: list[str],
) -> AuditResult:
    """Check every non-empty monthly register against the test-swipe list."""
    hits = _hit_tables(tickets, {sketch.p for sketch in dailies})

    sources: dict[int, list[AuditSource]] = {}
    for sketch in dailies:
        table = hits[sketch.p]
        for index, value in enumerate(sketch.registers):
            if value == 0:
                continue
            bucket, contribution = fold_contribution(sketch.p, index, value, summary.p)
            if contribution != summary.registers[bucket]:
                continue
            sources.setdefault(bucket, []).append(
                AuditSource(
                    venue_id=sketch.venue_id,
                    day=sketch.day,
                    revision=sketch.revision,
                    p=sketch.p,
                    day_bucket=index,
                    day_value=value,
                    matched_ticket=table.get((index, value)),
                    confirmed=sketch.test_list_confirmed,
                )
            )

    failures: list[BucketFailure] = []
    nonempty = 0
    for bucket, month_value in enumerate(summary.registers):
        if month_value == 0:
            continue
        nonempty += 1
        bucket_sources = sources.get(bucket, [])
        if any(
            source.matched_ticket is None or source.confirmed
            for source in bucket_sources
        ):
            continue
        failures.append(
            BucketFailure(
                bucket=bucket,
                month_value=month_value,
                sources=tuple(bucket_sources),
            )
        )

    return AuditResult(
        month=summary.month,
        status="failed" if failures else "passed",
        buckets_nonempty=nonempty,
        buckets_failed=len(failures),
        failures=tuple(failures),
    )
