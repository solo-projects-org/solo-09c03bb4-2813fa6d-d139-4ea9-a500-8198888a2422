#!/usr/bin/env python3
"""End-to-end evidence run against a live service (standard library only).

Covers the acceptance points that need a real process:

1. monthly registers equal a fresh table built from the union of ticket ids at
   the month's lowest precision (cross-venue dedup);
2. a correction can raise the month precision and shrink registers;
3. concurrent backfills from many threads never drop a current sketch;
4. the test-swipe audit blocks a test-explained bucket until the venue
   re-reports with a confirmed rebuild, then releases the monthly report;
5. a monthly maximum tied between two venues is re-attributed as the sources
   change revision, and passes only once every tied source is confirmed.

Run with an empty database::

    MUSEUM_DB_PATH=/tmp/evidence.db uvicorn app.main:app --port 8001 &
    python scripts/verify.py http://127.0.0.1:8001
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def hash_ticket(ticket: str) -> int:
    return int.from_bytes(hashlib.sha256(ticket.encode()).digest()[:8], "big")


def sketch(tickets, p: int) -> list[int]:
    registers = [0] * (1 << p)
    bits = 64 - p
    for ticket in tickets:
        hashed = hash_ticket(ticket)
        bucket = hashed >> bits
        remainder = hashed & ((1 << bits) - 1)
        rho = bits + 1 if remainder == 0 else bits - remainder.bit_length() + 1
        registers[bucket] = max(registers[bucket], rho)
    return registers


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def submit(venue, day, revision, p, registers, confirmed=None):
    body = {
        "venue_id": venue,
        "day": day,
        "revision": revision,
        "p": p,
        "registers": registers,
    }
    if confirmed is not None:
        body["test_list_confirmed"] = confirmed
    return call("POST", "/api/v1/daily-sketches", body)


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}"
        )
        if not ok:
            failures.append(name)

    # 1. cross-venue dedup, p=10 vs p=7 => summary at p=7, exact rebuild.
    shared = [f"S-{i:05d}" for i in range(300)]
    a_only = [f"A-{i:05d}" for i in range(120)]
    b_only = [f"B-{i:05d}" for i in range(80)]
    submit("venue-a", "2026-05-03", 1, 10, sketch(shared + a_only, 10))
    submit("venue-b", "2026-05-04", 1, 7, sketch(shared + b_only, 7))
    status, report = call("GET", "/api/v1/reports/monthly/2026-05")
    expected = sketch(shared + a_only + b_only, 7)
    check(
        "month merged at lowest precision equals fresh rebuild",
        status == 200 and report["precision"] == 7 and report["registers"] == expected,
        f"p={report.get('precision')} estimate={report.get('estimate'):.1f} truth=500",
    )
    check(
        "monthly estimate within 15% of 500 unique tickets",
        abs(report["estimate"] - 500) <= 75,
        f"estimate={report['estimate']:.1f}",
    )

    # 2. correction raises precision and can shrink registers.
    submit("venue-c", "2026-06-01", 1, 5, sketch([f"C-{i}" for i in range(400)], 5))
    before = call("GET", "/api/v1/reports/monthly/2026-06")[1]
    submit("venue-c", "2026-06-01", 2, 12, sketch(["C-single"], 12))
    after = call("GET", "/api/v1/reports/monthly/2026-06")[1]
    check(
        "correction raises month precision 5 -> 12",
        before["precision"] == 5 and after["precision"] == 12,
        f"{before['precision']} -> {after['precision']}",
    )
    check(
        "correction shrinks registers",
        sum(after["registers"])
        < sum(sketch([f"C-{i}" for i in range(400)], 5)) * 1024 // 32,
        f"sum={sum(after['registers'])} len={len(after['registers'])}",
    )

    # 3. concurrent backfill, 48 distinct venues across 12 days.
    def worker(i: int):
        return submit(
            f"race-{i}",
            f"2026-07-{(i % 12) + 1:02d}",
            1,
            8,
            sketch([f"R-{i}-{j}" for j in range(20)], 8),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(worker, range(48)))
    codes = sorted({code for code, _ in results})
    report = call("GET", "/api/v1/reports/monthly/2026-07")[1]
    check(
        "48 concurrent backfills all accepted",
        codes == [200],
        f"codes={codes}",
    )
    check(
        "concurrent summary contains every current venue and day",
        report["venue_count"] == 48 and report["day_count"] == 12,
        f"venues={report['venue_count']} days={report['day_count']}",
    )
    expected_race = sketch([f"R-{i}-{j}" for i in range(48) for j in range(20)], 8)
    check(
        "concurrent summary registers equal fresh rebuild",
        report["registers"] == expected_race,
    )

    # 4. test-swipe audit: register the month's list, fail on a test-explained
    # bucket, re-report with a confirmed rebuild, then the report is released.
    TEST = "TEST-SWIPE-0001"
    status, body = call("PUT", "/api/v1/test-tickets/2026-08", {"tickets": [TEST]})
    check(
        "test-swipe list registered for 2026-08",
        status == 200 and body["status"] == "created" and body["ticket_count"] == 1,
        f"{status} {body}",
    )
    status, body = call("PUT", "/api/v1/test-tickets/2026-08", {"tickets": [TEST]})
    check("re-registering the same list is idempotent", body["status"] == "unchanged")
    status, body = call(
        "PUT", "/api/v1/test-tickets/2026-08", {"tickets": [TEST, "OTHER"]}
    )
    check("a different list for the same month conflicts", status == 409)
    status, body = call("GET", "/api/v1/test-tickets/2026-08")
    check("test-swipe list read back", status == 200 and body["tickets"] == [TEST])

    real_t1 = [f"T1-REAL-{i:03d}" for i in range(30)]
    submit("venue-t1", "2026-08-03", 1, 8, sketch([TEST] + real_t1, 8))
    status, audit = call("GET", "/api/v1/reports/monthly/2026-08/audit")
    entries = [e for f in audit.get("failures", []) for e in f["re_report"]]
    check(
        "audit fails and names venue/day/current revision to re-report",
        status == 200
        and audit["status"] == "failed"
        and audit["report"] is None
        and entries
        and all(
            e["venue_id"] == "venue-t1"
            and e["day"] == "2026-08-03"
            and e["revision"] == 1
            and e["matched_ticket"] == TEST
            for e in entries
        ),
        f"failures={audit.get('failures')}",
    )
    status, body = submit(
        "venue-t1", "2026-08-03", 2, 8, sketch(real_t1, 8), confirmed=True
    )
    check(
        "confirmed rebuild accepted and stored with the new revision",
        status == 200
        and body["status"] == "replaced"
        and body["test_list_confirmed"] is True,
        f"{status} {body}",
    )
    status, audit = call("GET", "/api/v1/reports/monthly/2026-08/audit")
    check(
        "audit passes after the confirmed rebuild and returns the report",
        status == 200
        and audit["status"] == "passed"
        and audit["report"] is not None
        and abs(audit["report"]["estimate"] - 30) <= 6,
        f"estimate={audit.get('report', {}).get('estimate')}",
    )

    # 5. tied maximum: two venues explain the same monthly bucket with the same
    # test swipe; the source set is re-attributed as revisions change.
    test_bucket = hash_ticket(TEST) >> (64 - 8)
    real_x = [f"REALX-{i}" for i in range(3)]
    real_y = [f"REALY-{i}" for i in range(3)]
    check(  # fixture sanity: the rebuilds must not re-enter the tied bucket
        "local fixtures keep the rebuilt sketches out of the test bucket",
        sketch(real_x, 8)[test_bucket] == 0 and sketch(real_y, 8)[test_bucket] == 0,
    )
    call("PUT", "/api/v1/test-tickets/2026-09", {"tickets": [TEST]})
    submit("venue-x", "2026-09-01", 1, 8, sketch([TEST], 8))
    submit("venue-y", "2026-09-02", 1, 8, sketch([TEST], 8))
    _, audit = call("GET", "/api/v1/reports/monthly/2026-09/audit")
    check(
        "tied maximum lists both venues on the same bucket",
        audit["status"] == "failed"
        and len(audit["failures"]) == 1
        and {s["venue_id"] for s in audit["failures"][0]["re_report"]}
        == {"venue-x", "venue-y"},
        f"failures={audit.get('failures')}",
    )
    submit("venue-x", "2026-09-01", 2, 8, sketch(real_x, 8), confirmed=True)
    _, audit = call("GET", "/api/v1/reports/monthly/2026-09/audit")
    remaining = [
        (s["venue_id"], s["revision"])
        for f in audit["failures"]
        for s in f["re_report"]
    ]
    check(
        "after venue-x changes revision, only venue-y remains on the bucket",
        audit["status"] == "failed" and remaining == [("venue-y", 1)],
        f"remaining={remaining}",
    )
    submit("venue-y", "2026-09-02", 2, 8, sketch(real_y, 8), confirmed=True)
    _, audit = call("GET", "/api/v1/reports/monthly/2026-09/audit")
    check(
        "all tied sources confirmed: audit passes and returns the report",
        audit["status"] == "passed"
        and audit["report"] is not None
        and abs(audit["report"]["estimate"] - 6) <= 2,
        f"estimate={audit.get('report', {}).get('estimate')}",
    )

    print()
    print(
        f"{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
