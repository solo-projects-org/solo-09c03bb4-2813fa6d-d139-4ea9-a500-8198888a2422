"""FastAPI application: venue daily-sketch ingestion and monthly reports."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import audit, hll
from .store import (
    ConflictError,
    MonthlySummary,
    Storage,
    TestListConflictError,
)

DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
VENUE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_REVISION = 2**31 - 1


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


StorageDep = Annotated[Storage, Depends(get_storage)]


class SketchSubmission(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "venue_id": "museum-a",
                "day": "2026-05-03",
                "revision": 1,
                "p": 4,
                "registers": [0, 3, 0, 5, 1, 0, 0, 2, 0, 4, 0, 0, 1, 0, 0, 0],
                "test_list_confirmed": False,
            }
        },
    )

    venue_id: Annotated[str, Field(min_length=1, max_length=64)]
    day: Annotated[str, Field(description="UTC date, YYYY-MM-DD")]
    revision: Annotated[int, Field(strict=True, ge=0, le=MAX_REVISION)]
    p: Annotated[int, Field(strict=True, ge=4, le=12)]
    registers: Annotated[
        list[Annotated[int, Field(strict=True)]],
        Field(description="exactly 2**p registers"),
    ]
    test_list_confirmed: Annotated[
        bool,
        Field(
            strict=True,
            description=(
                "Venue confirms this revision was rebuilt without the month's "
                "registered test-swipe tickets. Stored only when a new "
                "revision is written (created/replaced); valid for that "
                "revision only."
            ),
        ),
    ] = False

    @field_validator("venue_id")
    @classmethod
    def _venue(cls, value: str) -> str:
        if not VENUE_PATTERN.match(value):
            raise ValueError("venue_id must match [A-Za-z0-9._-]{1,64}")
        return value

    @field_validator("day")
    @classmethod
    def _day(cls, value: str) -> str:
        if not DAY_PATTERN.match(value):
            raise ValueError("day must be a UTC date formatted YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("day is not a valid calendar date") from exc
        return value


class SketchResponse(BaseModel):
    venue_id: str
    day: str
    revision: int
    p: int
    registers: list[int]
    updated_at: str
    test_list_confirmed: bool


class IngestResponse(BaseModel):
    status: str
    venue_id: str
    day: str
    revision: int
    current_revision: int
    month: str
    test_list_confirmed: bool


class MonthlyReport(BaseModel):
    month: str
    estimate: float
    precision: int
    registers: list[int]
    day_count: int
    venue_count: int
    updated_at: str


class TestListSubmission(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"tickets": ["TEST-SWIPE-0001", "TEST-SWIPE-0002"]}
        },
    )

    tickets: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]],
        Field(
            min_length=1,
            max_length=10_000,
            description=(
                "Test-swipe ticket ids for the month. Duplicates are removed "
                "and the list is stored sorted."
            ),
        ),
    ]


class TestListResponse(BaseModel):
    month: str
    status: str  # created | unchanged
    ticket_count: int
    updated_at: str


class TestListView(BaseModel):
    month: str
    tickets: list[str]
    ticket_count: int
    updated_at: str


class AuditSourceView(BaseModel):
    """One venue/day whose daily register reaches the monthly bucket value."""

    venue_id: str
    day: str
    revision: int
    p: int
    day_bucket: int
    day_value: int
    matched_ticket: str | None


class BucketFailureView(BaseModel):
    bucket: int
    month_value: int
    re_report: list[AuditSourceView]


class MonthlyAuditResponse(BaseModel):
    month: str
    status: str  # passed | failed
    list_ticket_count: int
    list_updated_at: str
    buckets_nonempty: int
    buckets_failed: int
    failures: list[BucketFailureView]
    report: MonthlyReport | None = Field(
        description="The monthly report, present only when every bucket passes"
    )


class HealthResponse(BaseModel):
    status: str
    database: str


class HashSpecResponse(BaseModel):
    algorithm: str
    digest_bits: int
    bits_used: str
    bucket_rule: str
    rho_rule: str
    empty_register: int
    sample_ticket: str
    sample_hash: int
    sample_bucket_p8: int
    sample_rho_p8: int


HASH_SPEC = {
    "algorithm": "SHA-256, first 8 bytes, big-endian unsigned integer",
    "digest_bits": 64,
    "bits_used": "all 64 bits",
    "bucket_rule": "high p bits of the 64-bit hash",
    "rho_rule": (
        "1 + leading zero count of the remaining 64-p bits; "
        "64-p+1 when those bits are all zero"
    ),
    "empty_register": 0,
    "sample_ticket": "TICKET-00001",
}


def _report(summary: MonthlySummary) -> MonthlyReport:
    return MonthlyReport(
        month=summary.month,
        estimate=hll.estimate(summary.registers, summary.p),
        precision=summary.p,
        registers=summary.registers,
        day_count=summary.day_count,
        venue_count=summary.venue_count,
        updated_at=summary.updated_at,
    )


def create_app(db_path: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.storage = Storage(db_path) if db_path is not None else Storage()
        yield

    app = FastAPI(
        title="Museum Combined-Ticket Dedup Service",
        version="1.1.0",
        description=(
            "Venues submit daily HyperLogLog sketches of combined-ticket ids. "
            "Identical tickets scanned at several venues are counted once. "
            "Monthly reports are served from a persisted summary that is "
            "rebuilt transactionally from the current daily sketches. "
            "Before a report goes to the statistics group, it can be audited "
            "against the month's registered test-swipe ticket list: buckets "
            "explainable only by test swipes must be re-reported by the "
            "venue with a confirmed rebuild."
        ),
        lifespan=lifespan,
    )

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    def healthz(storage: StorageDep) -> HealthResponse:
        return HealthResponse(status="ok", database=storage.path)

    @app.get(
        "/api/v1/hash-spec",
        response_model=HashSpecResponse,
        tags=["ops"],
        summary="The single 64-bit hash and bucket/rho rules venues must mirror",
    )
    def hash_spec() -> HashSpecResponse:
        hashed = hll.hash_ticket(HASH_SPEC["sample_ticket"])
        split = hll.bucket_and_rho(hashed, 8)
        return HashSpecResponse(
            **HASH_SPEC,
            sample_hash=hashed,
            sample_bucket_p8=split.bucket,
            sample_rho_p8=split.rho,
        )

    @app.post(
        "/api/v1/daily-sketches",
        response_model=IngestResponse,
        status_code=200,
        tags=["sketches"],
        summary="Submit (or correct) one venue's daily sketch",
        responses={
            409: {"description": "Same revision already stored with other content"},
            422: {"description": "Malformed sketch"},
        },
    )
    def submit(submission: SketchSubmission, storage: StorageDep) -> IngestResponse:
        try:
            clean = hll.validate_registers(submission.p, submission.registers)
        except hll.SketchError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_sketch", "message": str(exc)},
            ) from exc
        try:
            outcome = storage.ingest(
                venue_id=submission.venue_id,
                day=submission.day,
                revision=submission.revision,
                p=submission.p,
                registers=clean,
                test_list_confirmed=submission.test_list_confirmed,
            )
        except ConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "message": str(exc),
                    "venue_id": exc.venue_id,
                    "day": exc.day,
                    "revision": exc.revision,
                    "current_revision": exc.current_revision,
                },
            ) from exc
        return IngestResponse(
            status=outcome.status,
            venue_id=outcome.venue_id,
            day=outcome.day,
            revision=outcome.revision,
            current_revision=outcome.current_revision,
            month=outcome.month,
            test_list_confirmed=outcome.test_list_confirmed,
        )

    @app.get(
        "/api/v1/daily-sketches/{venue_id}/{day}",
        response_model=SketchResponse,
        tags=["sketches"],
        summary="Read the current sketch for one venue/day",
    )
    def read_daily(venue_id: str, day: str, storage: StorageDep) -> SketchResponse:
        _validate_path_day(day)
        stored = storage.get_daily(venue_id, day)
        if stored is None:
            raise HTTPException(status_code=404, detail="no sketch for that venue/day")
        return SketchResponse(
            venue_id=stored.venue_id,
            day=stored.day,
            revision=stored.revision,
            p=stored.p,
            registers=stored.registers,
            updated_at=stored.updated_at,
            test_list_confirmed=stored.test_list_confirmed,
        )

    @app.get(
        "/api/v1/reports/monthly/{month}",
        response_model=MonthlyReport,
        tags=["reports"],
        summary="Monthly deduplicated ticket estimate from the stored summary",
    )
    def monthly(month: str, storage: StorageDep) -> MonthlyReport:
        _validate_month(month)
        summary = storage.get_monthly(month)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no summary for {month}")
        return _report(summary)

    @app.put(
        "/api/v1/test-tickets/{month}",
        response_model=TestListResponse,
        tags=["test-tickets"],
        summary="Register the month's fixed test-swipe ticket list",
        responses={
            409: {"description": "A different list is already registered"},
            422: {"description": "Malformed month or ticket list"},
        },
    )
    def register_test_list(
        month: str, submission: TestListSubmission, storage: StorageDep
    ) -> TestListResponse:
        _validate_month(month)
        try:
            outcome = storage.register_test_list(month, submission.tickets)
        except TestListConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "test_list_conflict", "message": str(exc)},
            ) from exc
        return TestListResponse(
            month=outcome.month,
            status=outcome.status,
            ticket_count=outcome.ticket_count,
            updated_at=outcome.updated_at,
        )

    @app.get(
        "/api/v1/test-tickets/{month}",
        response_model=TestListView,
        tags=["test-tickets"],
        summary="Read the registered test-swipe ticket list",
    )
    def read_test_list(month: str, storage: StorageDep) -> TestListView:
        _validate_month(month)
        stored = storage.get_test_list(month)
        if stored is None:
            raise HTTPException(
                status_code=404, detail=f"no test-swipe list for {month}"
            )
        return TestListView(
            month=stored.month,
            tickets=stored.tickets,
            ticket_count=len(stored.tickets),
            updated_at=stored.updated_at,
        )

    @app.get(
        "/api/v1/reports/monthly/{month}/audit",
        response_model=MonthlyAuditResponse,
        tags=["reports"],
        summary=(
            "Audit the monthly summary against the registered test-swipe "
            "list; the report is returned only when every bucket passes"
        ),
        responses={
            404: {"description": "No monthly summary or no registered list"},
        },
    )
    def audit_monthly(month: str, storage: StorageDep) -> MonthlyAuditResponse:
        _validate_month(month)
        snapshot = storage.get_audit_snapshot(month)
        if snapshot.summary is None:
            raise HTTPException(status_code=404, detail=f"no summary for {month}")
        if snapshot.test_list is None:
            raise HTTPException(
                status_code=404,
                detail=f"no test-swipe list registered for {month}",
            )
        result = audit.audit_month(
            snapshot.summary, snapshot.dailies, snapshot.test_list.tickets
        )
        return MonthlyAuditResponse(
            month=result.month,
            status=result.status,
            list_ticket_count=len(snapshot.test_list.tickets),
            list_updated_at=snapshot.test_list.updated_at,
            buckets_nonempty=result.buckets_nonempty,
            buckets_failed=result.buckets_failed,
            failures=[
                BucketFailureView(
                    bucket=failure.bucket,
                    month_value=failure.month_value,
                    re_report=[
                        AuditSourceView(
                            venue_id=source.venue_id,
                            day=source.day,
                            revision=source.revision,
                            p=source.p,
                            day_bucket=source.day_bucket,
                            day_value=source.day_value,
                            matched_ticket=source.matched_ticket,
                        )
                        for source in failure.sources
                    ],
                )
                for failure in result.failures
            ],
            report=_report(snapshot.summary) if result.status == "passed" else None,
        )

    return app


def _validate_path_day(day: str) -> None:
    if not DAY_PATTERN.match(day):
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="day is not a valid date") from exc


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    _, mon = month.split("-")
    if not 1 <= int(mon) <= 12:
        raise HTTPException(status_code=422, detail="month is not a valid month")


app = create_app()
