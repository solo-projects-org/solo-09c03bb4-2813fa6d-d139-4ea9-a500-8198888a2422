# Museum Combined-Ticket Deduplication Service

A FastAPI + SQLite service that receives one HyperLogLog (HLL) daily sketch per
venue and answers "how many distinct combined tickets were used this month?".
A ticket scanned at several venues is counted once, because every venue uses the
same 64-bit hash and the monthly table is the merge of the current daily tables.

## Data contract

| Field | Meaning |
| --- | --- |
| `venue_id` | Venue code, `[A-Za-z0-9._-]{1,64}` |
| `day` | UTC date, `YYYY-MM-DD` |
| `revision` | Monotonic integer per `(venue_id, day)`; higher replaces the whole record |
| `p` | HLL precision, `4 <= p <= 12` |
| `registers` | Exactly `2**p` integers in `[0, 64-p+1]`; `0` = empty bucket |
| `test_list_confirmed` | Optional boolean; confirms this revision was rebuilt without the month's listed test swipes (stored only with a newly written revision) |

The shared hash is documented live at `GET /api/v1/hash-spec`:

* hash = first 8 bytes of SHA-256 over the UTF-8 ticket id, big-endian (64 bits);
* bucket = the high `p` bits;
* `rho` = 1 + leading zero count of the remaining `64-p` bits, or `64-p+1` when
  those bits are all zero;
* a register keeps the maximum `rho` seen for its bucket.

Merging several sketches always targets the **lowest** precision in the set.
The fold is exact: the resulting table is byte-identical to hashing the union
of the ticket ids and rebuilding at that precision
(`app/hll.py:merge_sketches`, property-tested against rebuild in
`tests/test_hll.py`).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/daily-sketches` | Create / replace / ignore a daily sketch |
| `GET` | `/api/v1/daily-sketches/{venue_id}/{day}` | Read current sketch |
| `GET` | `/api/v1/reports/monthly/{month}` | Summary: estimate, precision, registers |
| `GET` | `/api/v1/reports/monthly/{month}/audit` | Audit the month against the test-swipe list |
| `PUT` | `/api/v1/test-tickets/{month}` | Register the month's fixed test-swipe list |
| `GET` | `/api/v1/test-tickets/{month}` | Read the registered test-swipe list |
| `GET` | `/api/v1/hash-spec` | Shared hash and rho rules, with a sample |
| `GET` | `/healthz` | Liveness + database path |

`POST` responses report `status`:

* `created` — first submission for that venue/day;
* `replaced` — higher revision replaced the whole record;
* `ignored` — lower revision; the current record is untouched (HTTP 200);
* `unchanged` — same revision and same content (idempotent retry, HTTP 200);
* HTTP `409 revision_conflict` — same revision, different content.

Same-content comparison covers both `p` and the registers, so re-sending the
same registers at a different precision is a conflict, not an update.

## Test-swipe audit

Before a monthly report goes to the statistics group, the venue-days that
still include **test-swipe tickets** must be found and re-reported without
them.

1. **Register the month's list** — `PUT /api/v1/test-tickets/{month}` with
   `{"tickets": [...]}`. The list is fixed per month: re-registering the
   identical list is idempotent, a different one answers
   `409 test_list_conflict`. Each ticket is hashed once and mapped to
   `(bucket, rho)` at every daily precision present in the month.
2. **Audit** — `GET /api/v1/reports/monthly/{month}/audit` checks every
   non-empty monthly register. For each it finds the *sources*: the daily
   registers whose folded value equals the monthly register's current value.
   A source is suspicious when its `(bucket, value)` at its own (original)
   precision matches a listed test ticket. A bucket **passes** when at least
   one source hits no test ticket, or when that source's current revision was
   confirmed as rebuilt. Otherwise the bucket fails and the response lists,
   per monthly bucket, the `venue_id`, `day`, and current `revision` to
   re-report (plus the matched test ticket). The monthly `report` is included
   in the audit response **only when every bucket passes**.
3. **Re-report** — the venue rebuilds its day without the listed test swipes
   and resubmits through the usual `POST /api/v1/daily-sketches` with a higher
   `revision` and `"test_list_confirmed": true`. The confirmation is stored
   only together with a successfully written new revision and is valid for
   that revision only: a later revision starts unconfirmed again, and an
   idempotent (`unchanged`) or stale (`ignored`) submission stores nothing.

The audit reads the summary, the current daily sketches, and the registered
list inside a single deferred read transaction: the verdict and the released
report always come from the same committed snapshot, and no write lock is
taken. The list and the confirmations live in SQLite and survive restarts.

## Storage and consistency

SQLite lives at `MUSEUM_DB_PATH` (default `/data/museum.db`), WAL mode,
`synchronous=FULL`, `busy_timeout=30s`.

* Every write runs in `BEGIN IMMEDIATE`: the daily row and the recomputed
  monthly summary commit together or not at all. A crash, exception, or
  `kill -9` before commit leaves the previous summary readable.
* Each write re-reads **all current** daily sketches of the month after taking
  the write lock and recomputes the month from scratch. Concurrent backfills
  therefore serialise and the last commit always includes every other current
  sketch. This also means a correction that raises the minimum precision or
  lowers register values is reflected — the summary is never an append-only
  max that could never shrink.
* The monthly report reads only the `monthly_summary` row; it never merges on
  demand.
* The test-swipe list lives in `test_ticket_list`; the per-revision
  confirmation flag lives in `daily_sketch.test_list_confirmed` (added to
  existing databases by a start-up migration). Both persist across restarts.

## Run with Docker Compose

```bash
docker compose up --build -d
docker compose ps                 # wait for (healthy)
open http://localhost:8000/docs   # Swagger UI
```

Data persists in the named volume `museum-data`. The image runs as UID/GID
`10001` and ships `/data` owned by that UID, so Docker copies that ownership
into a fresh named volume and the server can write there. If you switch to a
bind mount, make it writable for UID `10001`. To reset:

```bash
docker compose down -v
```

Smoke-test from Swagger:

1. `POST /api/v1/daily-sketches` with the prefilled example (`p=4`). Execute.
2. `GET /api/v1/reports/monthly/2026-05` — expect `precision: 4`,
   `registers` with 16 entries.
3. Submit a second venue for the same month and query again; `venue_count`
   becomes 2 while a ticket present in both sketches is still counted once.

Audit demo from Swagger (the register arrays below are prefilled sketches for
`TEST-SWIPE-0001` and one real ticket `GUEST-0001` at `p=4`):

1. `PUT /api/v1/test-tickets/2026-10` with `{"tickets": ["TEST-SWIPE-0001"]}`
   → `status: created`.
2. `POST /api/v1/daily-sketches` with the test swipe included:
   ```json
   {
     "venue_id": "museum-a",
     "day": "2026-10-03",
     "revision": 1,
     "p": 4,
     "registers": [0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0]
   }
   ```
3. `GET /api/v1/reports/monthly/2026-10/audit` → `status: failed`, `report`
   is null, and bucket `12` lists `museum-a / 2026-10-03 / revision 1` with
   `matched_ticket: TEST-SWIPE-0001`. (Bucket `5`, the real ticket, passes on
   its own.)
4. Re-report the rebuilt day with the confirmation flag:
   ```json
   {
     "venue_id": "museum-a",
     "day": "2026-10-03",
     "revision": 2,
     "p": 4,
     "registers": [0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
     "test_list_confirmed": true
   }
   ```
   → `status: replaced`, `test_list_confirmed: true`.
5. `GET /api/v1/reports/monthly/2026-10/audit` again → `status: passed` and
   the monthly `report` is included (`estimate ≈ 1.03`, one real ticket).

Or run the scripts:

```bash
python3 scripts/demo.py http://localhost:8000
python3 scripts/verify.py http://localhost:8000   # needs an empty month range
```

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
MUSEUM_DB_PATH=/tmp/museum.db uvicorn app.main:app --reload
python -m pytest -q
```

## Verification

`python -m pytest -q` → 60 tests pass. `scripts/verify.py` against a live server
produced:

| Check | Result |
| --- | --- |
| Month at lowest precision equals fresh rebuild of the ticket union | exact match (p=7, ~471 vs 500 truth) |
| Correction raises month precision 5 → 12 | pass |
| Correction shrinks registers | pass |
| 48 concurrent venue backfills across 12 days | all HTTP 200 |
| Concurrent summary has all 48 venues / 12 days, equal to rebuild | pass |
| Test-swipe list registered / idempotent / conflicting list → 409 | pass |
| Audit fails on a test-explained bucket, naming venue/day/revision | pass |
| Confirmed rebuild stored with the new revision; audit releases report | pass |
| Tied monthly maximum: both venues listed; after one re-reports, only the other remains; both confirmed → passed | pass |
| Summary, list, and confirmations still readable after `kill -9` + restart | pass |

`ruff check` and `ruff format --check` are clean.

## Known limitations

* Only one process may own the SQLite file; run a single replica (the default
  Compose setup). Horizontal scaling would need a shared database.
* Image build and `docker compose up` were **not executed in this
  environment** — it has no Docker daemon and cannot run containers. What was
  verified instead: the exact `requirements.txt` / `requirements-dev.txt`
  install into a clean Python 3.11 environment, the app boots and serves the
  full demo and audit flow from it (including the Swagger UI at `/docs`), and
  the healthcheck command returns 0 against the live server.
* The audit verdict is conservative by design: a daily register whose
  `(bucket, value)` coincides with a listed test ticket is treated as
  test-explained even when a real ticket produced the same value — HLL
  registers cannot distinguish the two. Such a day passes only via a clean
  co-source or a confirmed rebuild.
* Each ingest recomputes the whole month inside the write transaction. At 8
  venues × 31 days the rebuild is ~0.04 s; the cost grows linearly with the
  number of stored venue-days. The audit is a read and takes no write lock.
* Registers are stored as one blob of `2**p` bytes; a `p=12` row is 4 KiB.
* The estimator is the classic HLL estimator: ~1.04/√m relative error
  (measured std. dev. ≈ 1.0% at p=12, 6.5% at p=8 for 400 tickets).
* No authentication — put it behind a trusted network or a reverse proxy.
* `GET /api/v1/daily-sketches/{venue_id}/{day}` is a convenience read; venues
  are assumed to retain their own raw ticket ids.

## Layout

```
app/hll.py       HLL hashing, validation, exact merge, estimator
app/store.py     SQLite schema, transactional ingest, monthly recompute,
                 test-swipe list, audit snapshot
app/audit.py     test-swipe audit: source attribution and bucket verdicts
app/main.py      FastAPI models/routes
tests/           unit, storage-consistency, audit, and HTTP tests
scripts/         stdlib-only demo and acceptance-check clients
Dockerfile / docker-compose.yml
```
