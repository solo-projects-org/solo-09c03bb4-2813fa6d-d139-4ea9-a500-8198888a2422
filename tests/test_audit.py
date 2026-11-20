"""Tests for the test-swipe list, the monthly audit, and confirmations."""

import random
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import hll
from app.audit import audit_month, fold_contribution
from app.main import create_app
from app.store import Storage
from app.store import TestListConflictError as ListConflictError

MONTH = "2026-09"
TEST_TICKET = "TEST-SWIPE-0001"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"))
    with TestClient(app) as test_client:
        yield test_client


def sketch(tickets, p=8):
    return hll.sketch_for_tickets(tickets, p)


def split(ticket, p):
    return hll.bucket_and_rho(hll.hash_ticket(ticket), p)


def submit(client, venue, day, revision, registers, p=8, confirmed=None):
    body = {
        "venue_id": venue,
        "day": day,
        "revision": revision,
        "p": p,
        "registers": registers,
    }
    if confirmed is not None:
        body["test_list_confirmed"] = confirmed
    return client.post("/api/v1/daily-sketches", json=body)


def register(client, tickets, month=MONTH):
    return client.put(f"/api/v1/test-tickets/{month}", json={"tickets": tickets})


def audit(client, month=MONTH):
    return client.get(f"/api/v1/reports/monthly/{month}/audit")


def real_ticket_avoiding(test_tickets, p, prefix="REAL"):
    """A ticket whose (bucket, rho) at precision p hits no test ticket."""
    hits = set()
    for ticket in test_tickets:
        found = split(ticket, p)
        hits.add((found.bucket, found.rho))
    for i in range(100_000):
        candidate = f"{prefix}-{i}"
        found = split(candidate, p)
        if (found.bucket, found.rho) not in hits:
            return candidate
    raise AssertionError("no collision-free ticket found")  # pragma: no cover


# -- list registration -------------------------------------------------------


def test_register_and_read_back(client):
    response = register(client, ["T-2", "T-1", "T-2"])
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["ticket_count"] == 2

    stored = client.get(f"/api/v1/test-tickets/{MONTH}")
    assert stored.status_code == 200
    assert stored.json()["tickets"] == ["T-1", "T-2"]  # sorted, deduplicated

    again = register(client, ["T-1", "T-2"])
    assert again.status_code == 200
    assert again.json()["status"] == "unchanged"


def test_register_conflict_on_different_list(client):
    assert register(client, ["T-1"]).status_code == 200
    conflict = register(client, ["T-1", "T-2"])
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "test_list_conflict"
    # The original list is untouched.
    assert client.get(f"/api/v1/test-tickets/{MONTH}").json()["tickets"] == ["T-1"]


def test_register_validation(client):
    assert register(client, []).status_code == 422
    assert register(client, [""]).status_code == 422
    assert register(client, ["x" * 129]).status_code == 422
    assert register(client, ["T-1"], month="2026-13").status_code == 422
    assert register(client, ["T-1"], month="2026-9").status_code == 422
    assert client.get("/api/v1/test-tickets/2026-08").status_code == 404


def test_list_survives_reopen(tmp_path):
    path = str(tmp_path / "museum.db")
    first = Storage(path)
    first.register_test_list("2026-05", ["T-2", "T-1"])
    reopened = Storage(path)
    stored = reopened.get_test_list("2026-05")
    assert stored.tickets == ["T-1", "T-2"]
    with pytest.raises(ListConflictError):
        reopened.register_test_list("2026-05", ["T-3"])


# -- confirmation storage semantics ------------------------------------------


def test_confirmation_stored_only_with_new_revision(client):
    register(client, [TEST_TICKET])
    # created: flag stored
    response = submit(
        client, "museum-a", f"{MONTH}-03", 1, sketch([TEST_TICKET]), confirmed=True
    )
    assert response.json()["status"] == "created"
    assert response.json()["test_list_confirmed"] is True

    # unchanged retry carrying the flag writes nothing new; stored flag is
    # reported back, not updated.
    newer = sketch([TEST_TICKET, "REAL-1"], 8)
    assert (
        submit(client, "museum-a", f"{MONTH}-03", 2, newer).json()[
            "test_list_confirmed"
        ]
        is False
    )
    retry = submit(client, "museum-a", f"{MONTH}-03", 2, newer, confirmed=True)
    assert retry.json()["status"] == "unchanged"
    assert retry.json()["test_list_confirmed"] is False
    assert (
        client.get(f"/api/v1/daily-sketches/museum-a/{MONTH}-03").json()[
            "test_list_confirmed"
        ]
        is False
    )

    # ignored stale revision does not store the flag either
    stale = submit(
        client, "museum-a", f"{MONTH}-03", 1, sketch([TEST_TICKET]), confirmed=True
    )
    assert stale.json()["status"] == "ignored"
    assert stale.json()["test_list_confirmed"] is False

    # replaced: the flag is stored with the new revision
    replaced = submit(client, "museum-a", f"{MONTH}-03", 3, newer, confirmed=True)
    assert replaced.json()["status"] == "replaced"
    assert replaced.json()["test_list_confirmed"] is True
    assert (
        client.get(f"/api/v1/daily-sketches/museum-a/{MONTH}-03").json()[
            "test_list_confirmed"
        ]
        is True
    )


def test_confirmation_flag_must_be_a_boolean(client):
    register(client, [TEST_TICKET])
    body = {
        "venue_id": "museum-a",
        "day": f"{MONTH}-03",
        "revision": 1,
        "p": 8,
        "registers": sketch([TEST_TICKET]),
        "test_list_confirmed": "yes",
    }
    assert client.post("/api/v1/daily-sketches", json=body).status_code == 422


def test_confirmation_survives_reopen(tmp_path):
    path = str(tmp_path / "museum.db")
    first = Storage(path)
    first.ingest("v1", "2026-05-01", 1, 8, sketch(["a"], 8), test_list_confirmed=True)
    first.ingest("v2", "2026-05-02", 1, 8, sketch(["b"], 8))
    reopened = Storage(path)
    assert reopened.get_daily("v1", "2026-05-01").test_list_confirmed is True
    assert reopened.get_daily("v2", "2026-05-02").test_list_confirmed is False


def test_existing_database_is_migrated(tmp_path):
    path = str(tmp_path / "museum.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE daily_sketch ("
        "venue_id TEXT NOT NULL, day TEXT NOT NULL, revision INTEGER NOT NULL, "
        "p INTEGER NOT NULL, registers BLOB NOT NULL, content_hash TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (venue_id, day))"
    )
    conn.execute(
        "INSERT INTO daily_sketch VALUES ('v1', '2026-01-01', 1, 4, ?, 'h', 'now')",
        (bytes(sketch(["a"], 4)),),
    )
    conn.commit()
    conn.close()

    storage = Storage(path)  # must add the missing column
    stored = storage.get_daily("v1", "2026-01-01")
    assert stored.test_list_confirmed is False
    outcome = storage.ingest("v1", "2026-01-01", 2, 4, sketch(["b"], 4), True)
    assert outcome.test_list_confirmed is True


# -- audit behaviour ---------------------------------------------------------


def test_audit_404_without_summary_or_list(client):
    assert audit(client, "2026-08").status_code == 404  # nothing at all
    register(client, [TEST_TICKET])
    assert audit(client).status_code == 404  # list but no summary
    submit(client, "museum-a", f"{MONTH}-03", 1, sketch(["REAL-0"]))
    assert audit(client, "2026-10").status_code == 404  # summary but no list


def test_audit_passes_when_no_source_hits_a_test_ticket(client):
    register(client, [TEST_TICKET])
    real = real_ticket_avoiding([TEST_TICKET], 8)
    submit(client, "museum-a", f"{MONTH}-03", 1, sketch([real, "REAL-x"]))
    response = audit(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["failures"] == []
    assert body["buckets_failed"] == 0
    # The report is released and matches the plain monthly endpoint.
    assert body["report"] is not None
    assert body["report"] == client.get(f"/api/v1/reports/monthly/{MONTH}").json()


def test_audit_flags_test_explained_bucket(client):
    register(client, [TEST_TICKET])
    submit(client, "museum-a", f"{MONTH}-03", 7, sketch([TEST_TICKET]))
    body = audit(client).json()
    assert body["status"] == "failed"
    assert body["report"] is None  # no report while any bucket fails
    assert body["buckets_nonempty"] == 1
    assert body["buckets_failed"] == 1

    expected = split(TEST_TICKET, 8)
    (failure,) = body["failures"]
    assert failure["bucket"] == expected.bucket
    assert failure["month_value"] == expected.rho
    (entry,) = failure["re_report"]
    assert entry["venue_id"] == "museum-a"
    assert entry["day"] == f"{MONTH}-03"
    assert entry["revision"] == 7  # the current revision to re-report
    assert entry["p"] == 8
    assert entry["day_bucket"] == expected.bucket
    assert entry["day_value"] == expected.rho
    assert entry["matched_ticket"] == TEST_TICKET


def test_audit_uses_original_precision_for_hits(client):
    register(client, [TEST_TICKET])
    # A p=10 daily sketch; the month drops to p=8 because of another venue.
    submit(client, "museum-a", f"{MONTH}-03", 1, sketch([TEST_TICKET], 10), p=10)
    real = real_ticket_avoiding([TEST_TICKET], 8)
    submit(client, "museum-b", f"{MONTH}-04", 1, sketch([real], 8), p=8)

    body = audit(client).json()
    assert body["status"] == "failed"
    expected10 = split(TEST_TICKET, 10)
    (failure,) = body["failures"]
    assert failure["bucket"] == expected10.bucket >> 2  # folded to month p=8
    (entry,) = failure["re_report"]
    assert entry["p"] == 10
    assert entry["day_bucket"] == expected10.bucket  # original-precision bucket
    assert entry["day_value"] == expected10.rho  # original-precision rho
    assert entry["matched_ticket"] == TEST_TICKET

    # Confirmed re-report of the same content clears the failure.
    submit(
        client,
        "museum-a",
        f"{MONTH}-03",
        2,
        sketch([TEST_TICKET], 10),
        p=10,
        confirmed=True,
    )
    assert audit(client).json()["status"] == "passed"


def test_clean_cosource_lets_bucket_pass(client):
    """A tied monthly maximum passes if one source hits no test ticket."""
    # Pick a test ticket whose rho at p=8 is reachable by an offset fold.
    for i in range(1000):
        ticket = f"TEST-CANDIDATE-{i}"
        if split(ticket, 8).rho <= 2:
            break
    else:  # pragma: no cover
        raise AssertionError("no suitable test ticket found")
    register(client, [ticket])
    at_8 = split(ticket, 8)
    at_10 = split(ticket, 10)

    # museum-a explains the monthly bucket with the test ticket itself.
    submit(client, "museum-a", f"{MONTH}-03", 1, sketch([ticket], 8), p=8)

    # museum-b (p=10) reaches the same folded value from a register that hits
    # no listed ticket at its own precision.
    shift = 2
    for offset in (1, 2, 3):
        if shift - offset.bit_length() + 1 != at_8.rho:
            continue
        bucket10 = (at_8.bucket << shift) | offset
        for value in (1, 2, 3):
            if (bucket10, value) != (at_10.bucket, at_10.rho):
                break
        else:  # pragma: no cover
            continue
        break
    registers = [0] * (1 << 10)
    registers[bucket10] = value
    submit(client, "museum-b", f"{MONTH}-04", 1, registers, p=10)

    body = audit(client).json()
    assert body["status"] == "passed"
    assert body["report"] is not None


def test_tied_maximum_sources_change_revision(client):
    """Two venues tie at the monthly max; re-reporting them one by one."""
    register(client, [TEST_TICKET])
    day_x, day_y = f"{MONTH}-01", f"{MONTH}-02"
    submit(client, "venue-x", day_x, 1, sketch([TEST_TICKET]))
    submit(client, "venue-y", day_y, 1, sketch([TEST_TICKET]))

    body = audit(client).json()
    assert body["status"] == "failed"
    (failure,) = body["failures"]
    assert {s["venue_id"] for s in failure["re_report"]} == {"venue-x", "venue-y"}

    # venue-x re-reports rebuilt without the test swipe, confirmed; the tied
    # bucket's remaining source is venue-y alone.
    real_x = real_ticket_avoiding([TEST_TICKET], 8, prefix="REALX")
    submit(client, "venue-x", day_x, 2, sketch([real_x]), confirmed=True)
    body = audit(client).json()
    assert body["status"] == "failed"
    (failure,) = body["failures"]
    assert [(s["venue_id"], s["revision"]) for s in failure["re_report"]] == [
        ("venue-y", 1)
    ]

    # venue-y follows; every source is now confirmed, the report is released.
    real_y = real_ticket_avoiding([TEST_TICKET], 8, prefix="REALY")
    submit(client, "venue-y", day_y, 2, sketch([real_y]), confirmed=True)
    body = audit(client).json()
    assert body["status"] == "passed"
    assert body["report"] is not None
    assert body["report"]["estimate"] == pytest.approx(2.0, abs=1.0)


def test_confirmation_is_bound_to_its_revision(client):
    register(client, [TEST_TICKET])
    day = f"{MONTH}-03"
    submit(client, "museum-a", day, 1, sketch([TEST_TICKET]))
    assert audit(client).json()["status"] == "failed"

    # Revision 2 is confirmed rebuilt: the audit passes.
    submit(client, "museum-a", day, 2, sketch([TEST_TICKET]), confirmed=True)
    assert audit(client).json()["status"] == "passed"

    # Revision 3 (a new version of the same source) is not confirmed: the
    # confirmation of revision 2 does not carry over.
    submit(client, "museum-a", day, 3, sketch([TEST_TICKET, "REAL-1"]))
    body = audit(client).json()
    assert body["status"] == "failed"
    (failure,) = body["failures"]
    assert failure["re_report"][0]["revision"] == 3


def test_audit_passes_vacuously_when_all_registers_zero(client):
    register(client, [TEST_TICKET])
    submit(client, "museum-a", f"{MONTH}-03", 1, [0] * 256)
    body = audit(client).json()
    assert body["status"] == "passed"
    assert body["buckets_nonempty"] == 0
    assert body["report"]["estimate"] == 0.0


def test_audit_runs_concurrently_with_writes(client):
    """Smoke test: audits never error out while other venues backfill."""
    register(client, [TEST_TICKET])
    submit(client, "venue-0", f"{MONTH}-01", 1, sketch(["seed"]))  # summary exists
    errors = []

    def worker(n: int) -> None:
        try:
            for k in range(4):
                submit(
                    client,
                    f"venue-{n}",
                    f"{MONTH}-{n + 1:02d}",
                    k + 1,
                    sketch([f"t{n}-{k}"]),
                )
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(10):
        response = audit(client)
        assert response.status_code == 200
        assert response.json()["status"] in ("passed", "failed")
    for thread in threads:
        thread.join()
    assert errors == []


def test_openapi_lists_audit_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/test-tickets/{month}" in paths
    assert "/api/v1/reports/monthly/{month}/audit" in paths


# -- pure audit/fold logic ----------------------------------------------------


def test_fold_contribution_reproduces_merge():
    rng = random.Random(21)
    for _ in range(300):
        source_p = rng.randint(4, 12)
        target_p = rng.randint(4, source_p)
        hashes = [rng.getrandbits(64) for _ in range(rng.randint(0, 200))]
        source = hll.build_sketch(hashes, source_p)
        merged = hll.merge_sketches(
            [(source_p, source), (target_p, [0] * (1 << target_p))]
        )
        contributions: dict[int, int] = {}
        for index, value in enumerate(source):
            if value == 0:
                continue
            bucket, folded = fold_contribution(source_p, index, value, target_p)
            contributions[bucket] = max(contributions.get(bucket, 0), folded)
        for bucket, value in enumerate(merged):
            assert contributions.get(bucket, 0) == value


def test_audit_month_empty_source_is_a_failure():
    # Defensive: a non-empty monthly bucket with no daily source cannot pass.
    from app.store import DailySketch, MonthlySummary

    summary = MonthlySummary("2026-09", 4, [0, 2] + [0] * 14, 1, 1, "now")
    result = audit_month(summary, [], ["TEST-1"])
    assert result.status == "failed"
    assert result.failures[0].bucket == 1
    assert result.failures[0].sources == ()

    # ...but any clean source lets it pass.
    sketch = DailySketch("v1", "2026-09-01", 1, 4, [0, 2] + [0] * 14, "h", "now", False)
    assert audit_month(summary, [sketch], ["TEST-1"]).status == "passed"
