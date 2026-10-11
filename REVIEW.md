# REVIEW.md

Review of the starter `app/main.py` (line numbers refer to the ORIGINAL starter file).

**How these were verified.** I did not guess. I ran the starter's helpers directly and drove its endpoints with
`TestClient` against a live MongoDB-protocol server, compared each result to `openapi.yaml` / `DATA_MODEL.md` / the
problem statement, and only then listed it. The "Symptom" column is the output I actually observed. (The sandbox
database was FerretDB v1.24, not real MongoDB. None of the findings below depend on MongoDB-specific behaviour.)

**Scope.** Findings 1-28 are defects of the **starter** `app/main.py` (helpers and the five starter endpoints: `/health`,
`POST/GET /employees`, `POST /attendance/punch-in`, `GET /attendance`). They were verified in Phase 1 by running the starter, as
described above. The starter file itself is not part of the project archive any more, so I did **not** re-run them in Phase 4 and
I add no new "starter defect" without having reproduced it. Findings 29-30 are a re-review of punch-in while Phase 2 was written.
The starter contained no punch-out, PATCH, analytics or explain code, so there is nothing to review there; the sections at the end
list defects found in **my own later code** and the Phase 4 index-coverage review.

| # | Where | What is wrong | Symptom / how you'd notice it | Fix |
|---|---|---|---|---|
| 1 | L22-23 `MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))`, `os.getenv("MONGO_DB", "attendance_db")` | Connection details are hard-coded as fallbacks. The contract says never hard-code them; a missing variable silently points the app at localhost / a made-up DB. | Start with no env vars: app runs against `localhost` / `attendance_db` instead of failing visibly. | No defaults. `get_client()` reads `MONGO_URI`/`MONGO_DB` lazily; if either is missing it raises `DatabaseUnavailable`, which the API turns into 503. Real env vars still win over `.env` (see "not defects"). |
| 2 | L22 `MongoClient(...)` without `tz_aware=True`; default 30 s server-selection timeout | PyMongo returns **naive** datetimes, so every stored instant loses its zone, and later arithmetic against an aware value crashes (see #8). With Mongo down every request hangs ~30 s. | `compute_overtime(naive_punch_out, ...)` -> `TypeError: can't subtract offset-naive and offset-aware datetimes` (observed). | `MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)`. `dt_to_ms()` also treats any stray naive value as UTC. |
| 3 | `compute_late_minutes` L36-37 | Grace is applied to the **floored minutes**: `minutes > 10`. R2 says late only when *strictly more than 10:00*, so 09:40:01 must be late (10), but `int(601 s / 60) = 10` is not `> 10`. | Shift 09:30: 09:40:00 -> 0 (ok), **09:40:01 -> 0 (want 10)**, 10:15:59 -> 45 (ok). Observed. | Compare in whole **seconds** (`delta > 600`), then floor to minutes: `delta // 60`. |
| 4 | `compute_late_minutes` L35 | `punch_in.replace(hour=h, minute=m)` takes the shift start from the punch-in's *own* wall clock and calendar day. It ignores IST and the overnight rule: for a UTC-aware punch the "wall clock" is UTC, and an overnight early-morning punch is compared with *today's* 22:00 instead of yesterday's. | Shift 22:00, punch 23:00 IST (17:30Z) -> **0 (want 60)**. Shift 22:00-06:00, punch 03:00 IST next day -> **0 (want 300)**. Observed. | Shift start is built as an IST instant on the **attendance date** (`shift_window()`), and `compute_late_minutes(punch_in, shift_start_dt)` subtracts two instants. |
| 5 | `compute_work_hours` L41 | Python `round()` is banker's rounding on binary floats; R4/R8 want decimal **half-up**. Also no truncation to whole seconds. Because `half_day` (R5) depends on the rounded value, a wrong rounding can flip it. | 54 s = 0.015 h: `round` gives 0.01, half-up is **0.02**. Over 1..40000 s, **554 inputs differ**. Observed. | Integer arithmetic: `hundredths = (seconds*200 + 3600) // 7200`; inputs truncated to whole seconds first. Generic `round_half_up()` (Decimal) added for Phase 2 numbers. |
| 6 | `compute_overtime` L47 | R3 has a **30-minute minimum**; the starter counts any positive amount. | 10 min after shift end -> **10 (want 0)**; 29 min -> **29 (want 0)**. Observed. | `minutes if minutes >= 30 else 0` after the floor. |
| 7 | `compute_overtime` L46 | The shift end is placed on the record's own date. For an overnight shift the end is on the **next** day, so the difference is hugely wrong. | DATA_MODEL sample 7 (22:00-06:00, out 06:40 next day): returns **1480 (want 40)**. Observed. | `shift_window()` puts the end on `date + 1` when `shift_end <= shift_start`. |
| 8 | `compute_overtime` L46-47 | Subtracts a tz-aware IST datetime from whatever the caller passes; with the naive datetimes the DB returns this is a `TypeError` -> HTTP 500. | Observed `TypeError` (see #2). | All helpers go through `truncate_seconds()` / `ensure_utc()`, so naive and aware inputs both work. |
| 9 | `health` L72-74 | Always returns 200 and never talks to MongoDB. Contract: 200 only if Mongo answers a ping, else 503. | Stop Mongo: `/health` still 200 and the grader starts testing a dead service. | `db.command("ping")`; any `PyMongoError` / missing config -> `HTTPException(503)`. |
| 10 | `EmployeeIn` L53-60 | No validation at all (only "is a string"). Contract: `emp_code` `^EMP\d{4,6}$`, name 1-100, email pattern <=120, department 1-50, `HH:MM` shifts, `shift_start != shift_end`, real `joined_on` date. | `POST {"emp_code":"x","name":"","email":"nope","department":"","shift_start":"99:99",...}` -> **201 (want 422)**. Observed. | `EmployeeCreate` with `Field` constraints, `[0-9]` instead of `\d` (non-ASCII digits), a strict date validator and a model validator for the shift rule. |
| 11 | `create_employee` L79-83 | Check-then-insert (`find_one` then `insert_one`) and no unique index: two simultaneous requests both pass the check. Contract demands a database unique index. | 16 parallel creates of the same `emp_code` -> **10x 201, 10 documents stored**. Observed. | Unique index `uq_emp_code`; `insert_one` and catch `DuplicateKeyError` -> 409. No pre-check. |
| 12 | `create_employee` L82, L85 | `datetime.now()` is naive **server-local** time, and the response returns it as an ISO string. Contract: BSON datetime (UTC) in the DB, integer epoch **ms** in the API. | Response `created_at: "2026-10-09T16:12:47.185959"` (observed). On a non-UTC server the stored instant is also wrong. | `now_utc_millis()` (aware UTC, ms precision) stored; `employee_out()` returns `dt_to_ms(...)` as an int. |
| 13 | `list_employees` L93 | `skip = page * page_size` is off by one page: page 1 skips the first `page_size` rows. | `?page=1&page_size=3` returned EMP0003-EMP0005 instead of EMP0001-EMP0003. Observed. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees` L94 | `total` is `count_documents({})`, ignoring the department filter. Violates R10. | 3 employees in "Ops", `?department=Ops` -> `total` **8 (want 3)**. Observed. | `count_documents(query)` with the same filter as `find`. |
| 15 | `list_employees` L95 | No `.sort(...)`: order is storage order (works by luck on a tiny collection), no supporting index. Contract: ascending `emp_code`. | Order breaks as soon as documents are not inserted in code order (e.g. seeded data). Reasoning from code; confirmed absent by reading. | `.sort("emp_code", 1)`; indexes `uq_emp_code` and `(department, emp_code)` serve the sort with no in-memory step. |
| 16 | `list_employees` L89-91 | `page`/`page_size` unvalidated, and `if department:` treats `""` as "no filter". | `page=-1` -> **500** (negative skip); `page_size=0` -> returns **all** rows (limit 0 = no limit); `page_size=1000` accepted (max is 100); `department=` -> 8 rows (want 0, exact match). Observed. | `Query(ge=1)` / `Query(ge=1, le=100)`; `department is not None`. A huge `page` (10**30) is guarded so BSON int64 overflow cannot cause a 500. |
| 17 | `punch_in` L101-113 | Unknown employee: `emp` is `None`, then `emp["shift_start"]` -> `TypeError` -> **500**. Contract: 404. | `{"emp_code":"NOPE"}` -> **500**. Observed. | Explicit `if emp is None: raise HTTPException(404)`. |
| 18 | `PunchInIn.status` L66 | Any string accepted. Only `PRESENT`, `WFH`, `ON_DUTY` may be created by punch-in. | `status: "ABSENT"` -> **201 (want 422)**. Observed. | `Literal["PRESENT","WFH","ON_DUTY"]`, default `PRESENT`. |
| 19 | `PunchInIn.punched_at` L65, `punch_in` L102 | Lax `Optional[int]` accepts numeric strings and seconds-looking values; `if body.punched_at` is a **truthiness** test so `0` silently means "now". Contract: ints in ms only, within `[1e11, 4102444800000]`, else 422; explicit `null` is not allowed. | `"1783312500000"` -> 201; `1783312500` (seconds) -> 201; `0` -> 201 dated today. Observed (a float `1.5` happened to give 422). | `EpochMs = Annotated[int, Field(strict=True, ge=1e11, le=4102444800000)]`; omitted = default, explicit `null` = 422; test is `is not None`. |
| 20 | `punch_in` L102-103 | `datetime.fromtimestamp(ms/1000)` is naive **server-local** time and `.date()` takes the date in that zone, not IST. The R1 overnight rule is missing entirely. | On a UTC server, a punch at 01:30 IST on Jul 7 is given `date: 2026-07-06` (**want 2026-07-07**). Observed. Overnight punch-in 03:00 for a 22:00-06:00 shift must land on the previous day. | `ms_to_dt()` (aware UTC) + `attendance_date_for()` which converts to IST and applies the overnight rule. |
| 21 | `punch_in` L102 | Instants are not truncated to whole seconds, and the unrounded value is stored. R1: truncate before any calculation and store the truncated value. | Punch at 09:40:01.900 -> stored `2026-07-06 04:10:01.900000`. Observed. | `ms // 1000` in `ms_to_dt()`; `now_utc()` drops microseconds. |
| 22 | `punch_in` L104-118 | Check-then-insert race and no unique `(emp_code, date)` index. Contract: exactly one 201, rest 409. | 16 parallel punch-ins -> **two 201s and 2 documents** for one employee/date. Observed. | Unique index `uq_emp_code_date`; insert and catch `DuplicateKeyError` -> 409. Verified with 24 parallel requests against a real uvicorn process. |
| 23 | `punch_in` L119-121 | Response leaks an `id` (stringified `_id`) - the contract says `_id`/id never appear - and returns datetimes as ISO strings instead of epoch ms. | `{"punch_in": "2026-07-06T18:00:00", ..., "id": "6ac9..."}` observed. | `attendance_out()` builds the response explicitly: no id, instants as int ms. |
| 24 | `list_attendance` L144-147 | `list(db.attendance_logs.find(q))` loads the **whole collection** into memory, sorts and slices in Python. Contract: must stay fast at ~100k documents. | Memory/latency grows with the collection, not the page size. | Database-side `.sort().skip().limit()` backed by indexes; `count_documents` for the total. |
| 25 | `list_attendance` L145 | Sorts by `date` only. Contract: `date` desc, then `emp_code` asc. Python's sort is stable, so ties keep the (undefined) storage order, and pages are not deterministic. | Three logs on the same date inserted as EMP0003, EMP0001, EMP0002 come back in that order (**want 0001, 0002, 0003**). Observed. | `sort([("date",-1),("emp_code",1)])` (unique pair, so pagination is stable). |
| 26 | `list_attendance` L125-142 | No validation: `status` not checked against the enum, dates not checked, `date_from > date_to` not rejected, `page`/`page_size` unbounded (`page=0` slices `docs[-20:0]`). | `status=BOGUS`, `date_from=garbage`, `date_from>date_to` all -> **200 (want 422)**. Observed. | `Status` literal, strict `DateStr`, explicit from>to check raising a standard 422 body, `Query(ge=1/le=100)`. |
| 27 | `list_attendance` L148-150 | Returns stored documents as-is: legacy records without `history`/`half_day` come back without those keys, absent punch fields stay absent, instants are raw datetimes (`history[].at` and `changes.*.from/to` too), `id` leaked. | Legacy sample 9 returned keys `date, emp_code, id, late_minutes, ...` with **no `half_day`, no `history`** (observed). | `attendance_out()` defaults missing `history`->`[]`, `half_day`->`false`, late/overtime->0 and converts nested history datetimes to ms. |
| 28 | whole file | No indexes (only `_id_`) and none created at startup. | `index_information()` shows only `_id_` (observed). | Idempotent `ensure_indexes()` in the FastAPI lifespan; retried from `/health` and before writes if Mongo was down at startup. See HANDOFF.md for definitions. |

## Phase 2 re-review of the existing punch-in (and what the new endpoints needed from it)

I re-read `POST /attendance/punch-in` against the contract while writing punch-out and PATCH. The findings below were
reasoned from the code at that time; the behaviour is now covered by the test suite (see "Verification status").

| # | Where | Finding | Action |
|---|---|---|---|
| 29 | `punch_in`, `DuplicateKeyError` branch | The 409 text said "already punched in for this date". The unique `(emp_code, date)` index also fires when the existing record is a seeded `ABSENT` / `LEAVE` document, where nobody punched in. The status code is still right (a record already exists), the message was misleading. | Reworded to "a record already exists for this employee and date". Status code unchanged. |
| 30 | `ATTENDANCE_INDEXES` | Punch-out must find "the most recent record whose `punch_in` <= `punched_at`", across midnight. No existing index serves that: `(emp_code, date)` would need guessed dates, and the `date` string is not the punch time for overnight shifts. | Added `emp_code_1_punch_in_-1` (equality + range + descending sort, `limit 1`). Created idempotently at startup with the others. |

Checked in punch-in and judged correct, so not changed: truncation to whole seconds before storing, the R2 grace comparison in
seconds, the IST/overnight date rule (`attendance_date_for`), the unique-index race handling, 404 for an unknown employee, `status`
restricted to `PRESENT`/`WFH`/`ON_DUTY`, derived fields initialised as the data model says, no `history` written, and the response
built by `attendance_out` (no `_id`, instants as epoch ms). Open observations I left alone: punch-in accepts a future `punched_at`
(contract is silent), and an employee document without `shift_start`/`shift_end` would raise `KeyError` (the data model says both
always exist).

### New Phase 2 code - design points a reviewer should check

* Punch-out is one atomic `find_one_and_update` pinned to `{emp_code, date, punch_in (as read), punch_out: null}`; a second simultaneous
  request matches nothing and gets 409. It does **not** touch `late_minutes` (punch-in owns R2) and never writes `history`.
* PATCH never writes the `history` array back. It `$set`s only changed fields and `$push`es one entry, with a filter pinned to the
  state that was read (status, both punch times, history length). A concurrent edit makes the filter miss; the code re-reads and
  re-plans (max 3 attempts) and otherwise answers 409.
* Check order for punch-out: 422 body -> 404 employee -> 404 no presence record at all -> 409 already out -> 422 time window.
  A duplicate punch-out therefore gets 409 even if its timestamp would also be out of window. If the employee's only punch-ins are
  *later* than `punched_at`, the answer is 422 (`punched_at` is not after a punch-in), not 404.
* "A request that changes nothing" is judged on the final value of all seven fields (status, both times, four derived values).
  Missing legacy values are normalised first (late/overtime 0, half_day false) so a legacy record is not reported as changed.

## Defects found in my own code (not in the starter)

| # | Where | What was wrong | Symptom | Fix |
|---|---|---|---|---|
| 31 | `department_trend`, parameter `from` | The first version declared the parameter as `from_: DateStr = Query(alias="from")`. With that form the `AfterValidator` of `DateStr` was not applied, so the date check silently disappeared. | `from=2026-02-30` reached the function and the pipeline code and ended as a **500** instead of 422 (found by the Phase 3 tests: FastAPI run from source, database replaced by a stand-in). | `from_: Annotated[DateStr, Query(alias="from")]`. The explain endpoint uses the same `Annotated[...]` form. |
| 32 | `apply_regularization` (PATCH) | The correction always did `$push` onto `history`. MongoDB rejects `$push` onto a field that exists but is `null`, so a seeded record with `history: null` (not missing) would answer **500**. | A record with `"history": null`, then `PATCH` with a status change. Reproduced against a model of the update semantics; regression test `test_patch_record_whose_history_is_an_explicit_null`. | When the stored history is missing, null or empty, the update `$set`s `history: [entry]` instead (the version filter pins "history still empty", so no entry can be overwritten); otherwise `$push` as before. |

## Index coverage review (query shapes and the index expected to serve each)

The table below is what each query needs and which declared index should serve it. The IXSCAN / no-COLLSCAN check on ~100,000
records is made by `tests/test_explain_live.py`; see "Verification status" for the run result.

| Query | First stage / filter | Index expected to serve it | Gap found? |
|---|---|---|---|
| `GET /attendance`, no filter | sort `date` desc, `emp_code` asc | `date_-1_emp_code_1` (delivers the order, no SORT stage) | none |
| `GET /attendance?emp_code=` (+ date range) | equality `emp_code`, range and sort on `date` | `uq_emp_code_date` scanned backwards (second sort key dropped because it is constant) | none |
| `GET /attendance?date_from/date_to` | range on `date` + sort | `date_-1_emp_code_1` | none |
| `GET /attendance?status=` (+ range) | equality `status`, then `date`, `emp_code` | `status_1_date_-1_emp_code_1` | none |
| `employee_monthly` | `employees` `$match emp_code`; inner `$lookup` on `emp_code` + `date` range | `uq_emp_code`; `uq_emp_code_date` | none |
| `department_summary` | `employees` `joined_on <= month end` (+ `department`); inner `$lookup` as above | `joined_on_1` (or `department_1_emp_code_1` with a department); `uq_emp_code_date` | **was** a gap in Phase 2 (no index led with `joined_on`); closed in Phase 3 by `joined_on_1` |
| `late_leaderboard` | `attendance_logs` `date` range and `late_minutes > 0`, then `$lookup` employees on `emp_code` | `date_-1_emp_code_1` for the range (the `late_minutes` test is applied to the fetched documents); `uq_emp_code` for the join | none for the "no COLLSCAN" goal. It reads every record of the month once; a partial index on late records is a possible later optimisation, not added because I could not measure it |
| `department_trend` | `employees` `$match department`; inner `$lookup` on `emp_code` + `date` range | `department_1_emp_code_1`; `uq_emp_code_date` | none |

Also reviewed while adding explain:
* **Explain must not run against missing indexes.** If MongoDB was down at startup the indexes may not exist yet, and explain would then
  honestly show a collection scan that the real deployment would not have. The explain route therefore uses the same `require_indexes`
  dependency as the write endpoints (it builds them first).
* **The real endpoint and the explained query must not drift apart.** The projection and the validation rules (`date_from <= date_to`, the
  trend range rules) were moved into shared constants/functions (`ATTENDANCE_LIST_PROJECTION`, `validate_attendance_range`,
  `validate_trend_range`) that both the endpoint and explain call.


## Examined and judged NOT a defect

* `load_dotenv()` at import: its default is `override=False`, so a real environment variable wins over `.env`. This is what the contract asks for.
* `IST = timezone(timedelta(hours=5, minutes=30))`: correct. India has no DST, so a fixed offset is right.
* `doc.pop("_id", None)` after `insert_one` in `create_employee`: correct, `insert_one` adds `_id` to the dict. (The response is now built explicitly anyway.)
* Pydantic ignoring unknown JSON keys: matches the contract ("derived fields ... if a client sends one, ignore it"; `created_at` is never client-supplied).
* Plain `def` endpoints with a shared `MongoClient`: fine. FastAPI runs them in a thread pool and `MongoClient` is thread-safe. Blocking calls inside `async def` would have been the problem.
* `late = (punch_in - start)` flooring for positive values: floor and truncation agree for positive deltas; the real problem is the order of operations (#3).
* Status codes 201 on both POSTs: correct per contract.

## Verification status

* **Author's previously reported local result (full suite, not re-run by the reviewer):** 492 passed, 0 failed, 1 non-blocking Starlette deprecation warning, about
  5 min 13 s, against a local MongoDB (server version not recorded in this repository). The suite includes the live explain checks on ~100,000 records (`tests/test_explain_live.py`).
* Part A findings 1-28 were reproduced against the original starter in Phase 1 (on FerretDB, see the top of this file); the
  starter file is no longer in the repository, so they were not re-run later.
* Not tested: behaviour under a much larger dataset than the ~100,000-record explain test, and any deployment other than a
  single local `uvicorn` process.
* Interpretation choices where the contract is open: a punch-out whose `punched_at` is *before or equal to* the employee's only
  punch-in answers 422; the moving average uses the already-rounded daily rates; the 92-day limit counts both end dates.
