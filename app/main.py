"""
Employee Attendance & Analytics API  (FastAPI + PyMongo + MongoDB 6.0+)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment
      variables always win, because load_dotenv() does not override them)

PHASE 1 (foundation): config/connection, /health, POST+GET /employees, POST /attendance/punch-in,
GET /attendance, indexes, and the reusable time/rule helpers.
PHASE 2 (attendance operations): POST /attendance/punch-out and PATCH /attendance/{emp_code}/{date}.
PHASE 3 (analytics): the four /analytics/* endpoints, each one a MongoDB aggregation pipeline.
PHASE 4 (proof): GET /admin/explain/{endpoint} - runs MongoDB explain("executionStats") on the real query /
pipeline of the named endpoint and returns MongoDB's raw answer.

Conventions used in this file
  * Instants cross the API boundary as integer epoch MILLISECONDS and live in MongoDB as
    BSON datetimes (UTC). The client is created with tz_aware=True so PyMongo hands back
    timezone-aware datetimes instead of naive ones.
  * Attendance instants are truncated to whole seconds before any calculation or storage (R1).
  * Dates are "YYYY-MM-DD" strings (IST calendar dates), shift times are "HH:MM" strings (IST).
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): variables already in the environment win

logger = logging.getLogger("attendance")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Contract limits for an epoch-millisecond instant (openapi.yaml -> EpochMillis).
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

LATE_GRACE_SECONDS = 10 * 60      # R2: late only if STRICTLY more than 10:00 after shift start
OVERTIME_MIN_MINUTES = 30         # R3: overtime counts only if it is at least 30 whole minutes
HALF_DAY_THRESHOLD_HOURS = 4.50   # R5: half day if the ROUNDED work_hours < 4.50
MAX_SKIP = 2**62                  # keeps a silly ?page=10**30 from overflowing BSON int64 (-> 500)
MAX_SHIFT_DURATION = timedelta(hours=24)  # punch_out may be at most 24 h after punch_in (punch-out, PATCH)
WRITE_ATTEMPTS = 3                # optimistic-concurrency retries for punch-out / PATCH before giving up with 409

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# MongoDB configuration, connection and indexes
# --------------------------------------------------------------------------- #
class DatabaseUnavailable(Exception):
    """MONGO_URI / MONGO_DB missing. Surfaces to the client as 503."""


_client: Optional[MongoClient] = None
_client_lock = threading.Lock()
_indexes_ready = False


def get_client() -> MongoClient:
    """One shared, thread-safe MongoClient, created lazily from the environment.

    Nothing is hard-coded: if MONGO_URI or MONGO_DB is missing we raise
    DatabaseUnavailable (-> 503) instead of silently falling back to localhost.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                uri = os.environ.get("MONGO_URI")
                if not uri or not os.environ.get("MONGO_DB"):
                    raise DatabaseUnavailable("MONGO_URI and MONGO_DB must be set")
                _client = MongoClient(
                    uri,
                    tz_aware=True,                 # datetimes come back UTC-aware, never naive
                    serverSelectionTimeoutMS=3000,  # fail fast so /health can answer 503
                    connectTimeoutMS=3000,
                )
    return _client


def get_db():
    get_client()  # raises DatabaseUnavailable when not configured
    return _client[os.environ["MONGO_DB"]]


def employees_col():
    return get_db()["employees"]


def attendance_col():
    return get_db()["attendance_logs"]


EMPLOYEE_INDEXES = [
    # Uniqueness of emp_code is enforced HERE, by the database (concurrent creates -> one 201).
    # Also serves: emp_code lookups and the unfiltered GET /employees sort.
    IndexModel([("emp_code", 1)], name="uq_emp_code", unique=True),
    # GET /employees?department=X sorted by emp_code: equality prefix + sort key, no in-memory sort.
    IndexModel([("department", 1), ("emp_code", 1)], name="department_1_emp_code_1"),
    # Phase 3: department summary starts with "employees whose joined_on <= end of month". Without a
    # department filter that predicate has no other index -> this one avoids a collection scan.
    IndexModel([("joined_on", 1)], name="joined_on_1"),
]

ATTENDANCE_INDEXES = [
    # "At most one record per (emp_code, date)" is enforced HERE (race-safe punch-in).
    # Also serves: record lookup by natural key, GET /attendance?emp_code=... (with date range).
    IndexModel([("emp_code", 1), ("date", 1)], name="uq_emp_code_date", unique=True),
    # GET /attendance without emp_code: sort is date DESC, emp_code ASC, date range on `date`.
    IndexModel([("date", -1), ("emp_code", 1)], name="date_-1_emp_code_1"),
    # GET /attendance?status=...: equality on status, then the same sort/range, no collection scan.
    IndexModel([("status", 1), ("date", -1), ("emp_code", 1)], name="status_1_date_-1_emp_code_1"),
    # GET /attendance?emp_code=...&status=...: both filters are equality predicates, followed by
    # the required date ordering.  This keeps Atlas from choosing an emp_code-only index and
    # adding a blocking SORT for this combined filter shape.
    IndexModel([("emp_code", 1), ("status", 1), ("date", -1)], name="emp_code_1_status_1_date_-1"),
    # POST /attendance/punch-out: "this employee's most recent record with punch_in <= punched_at".
    # Equality on emp_code, range on punch_in, sorted by punch_in DESC, limit 1 -> one index seek,
    # no in-memory sort, and it works across midnight because it does not depend on the `date` string.
    IndexModel([("emp_code", 1), ("punch_in", -1)], name="emp_code_1_punch_in_-1"),
]


def ensure_indexes() -> None:
    """Create every required index. Idempotent: same name + same spec is a no-op."""
    global _indexes_ready
    employees_col().create_indexes(EMPLOYEE_INDEXES)
    attendance_col().create_indexes(ATTENDANCE_INDEXES)
    _indexes_ready = True


def require_indexes() -> None:
    """Dependency for write endpoints: never write before the unique indexes exist.

    Normally startup already built them. This only matters if MongoDB was down at startup.
    """
    if not _indexes_ready:
        ensure_indexes()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(ensure_indexes)  # PyMongo is blocking, keep it off the event loop
    except Exception:  # the app must still start so /health can answer 503 while Mongo is down
        logger.exception("could not create indexes at startup; will retry on /health and writes")
    yield
    global _client
    if _client is not None:
        _client.close()
        _client = None


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _mongo_connection_failure(_: Request, exc: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.exception_handler(DatabaseUnavailable)
async def _mongo_not_configured(_: Request, exc: DatabaseUnavailable):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def raise_validation_error(loc: Tuple[str, ...], msg: str, value: Any = None):
    """Raise a 422 with FastAPI's standard {"detail": [ ... ]} body for checks done in code."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


# --------------------------------------------------------------------------- #
# Time helpers (R1: IST + whole seconds; epoch ms at the API boundary)
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    """Make a datetime timezone-aware. A naive value is taken to be UTC (what MongoDB stores)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def truncate_seconds(dt: datetime) -> datetime:
    """Drop microseconds: 09:40:00.900 counts as 09:40:00 (R1)."""
    return ensure_utc(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (floor division)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """UTC datetime -> epoch milliseconds, using integer arithmetic only (no float error)."""
    delta = ensure_utc(dt) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def now_utc() -> datetime:
    """Current time as a UTC-aware datetime, whole seconds (used as the default punched_at)."""
    return datetime.now(UTC).replace(microsecond=0)


def now_utc_millis() -> datetime:
    """Current time with millisecond precision (BSON's precision), used for created_at."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def parse_hhmm(value: str) -> Tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next calendar day."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance date is the IST calendar date of the punch-in.

    Overnight shifts: a punch-in whose IST time is EARLIER than shift_end belongs to the shift
    that started the previous day (e.g. 22:00-06:00, punch-in 03:00 -> yesterday's record).
    """
    ist = truncate_seconds(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """(shift start instant, shift end instant) for the attendance date, both IST-aware.

    For an overnight shift the end falls on the NEXT calendar day.
    """
    d = date_cls.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    end_day = d + timedelta(days=1) if is_overnight(shift_start, shift_end) else d
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return start, end


# --------------------------------------------------------------------------- #
# Business-rule helpers (R2 - R5). Pure functions, no database access.
# --------------------------------------------------------------------------- #
def round_half_up(value: Any, places: int = 2) -> float:
    """Decimal half-up rounding (R8), NOT Python's round() (banker's rounding on binary floats).

    Goes through Decimal(str(value)) so 2.675 is treated as the decimal 2.675 -> 2.68.
    """
    from decimal import ROUND_HALF_UP, Decimal

    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes
    (floor) since SHIFT START (not since the end of the grace period).

    Shift 09:30: 09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45.
    The comparison is done in SECONDS; flooring to minutes first would turn 09:40:01
    into "10 minutes" and wrongly call it not late.
    """
    delta = int((truncate_seconds(punch_in) - truncate_seconds(shift_start_dt)).total_seconds())
    return delta // 60 if delta > LATE_GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, rounded to 2 decimals HALF-UP.

    Integer maths only: hundredths = floor(seconds * 100 / 3600 + 1/2). No float rounding.
    """
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    if seconds < 0:
        raise ValueError("punch_out is before punch_in")
    hundredths = (seconds * 200 + 3600) // 7200
    return hundredths / 100


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes (floor) from shift end to punch-out, counted only when >= 30."""
    delta = int((truncate_seconds(punch_out) - truncate_seconds(shift_end_dt)).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half day when the ROUNDED work_hours < 4.50."""
    return work_hours is not None and work_hours < HALF_DAY_THRESHOLD_HOURS


def derive_fields(
    shift_start: str,
    shift_end: str,
    date_str: str,
    punch_in: datetime,
    punch_out: Optional[datetime],
) -> Dict[str, Any]:
    """All four derived fields for one record (R2-R5). Reused by punch-in, and in Phase 2 by
    punch-out and the PATCH regularize endpoint so the rules live in exactly one place."""
    start_dt, end_dt = shift_window(date_str, shift_start, shift_end)
    late = compute_late_minutes(punch_in, start_dt)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_out, end_dt),
        "half_day": compute_half_day(work_hours),
    }


# --------------------------------------------------------------------------- #
# Request models / validation (openapi.yaml schemas)
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _validate_date_str(value: str) -> str:
    """Strict YYYY-MM-DD that is a real calendar date. (date.fromisoformat alone would also
    accept forms like '20260706', which the contract does not allow.)"""
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a valid calendar date")
    return value


DateStr = Annotated[str, AfterValidator(_validate_date_str)]

_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _validate_month_str(value: str) -> str:
    """Strict YYYY-MM (openapi.yaml -> Month). Year 0000 matches the pattern but is not a real
    calendar year, so it is rejected too (422) instead of crashing later."""
    if not _MONTH_RE.fullmatch(value):
        raise ValueError("must be a month in YYYY-MM format")
    if int(value[:4]) < 1:
        raise ValueError("year must be 0001 or later")
    return value


MonthStr = Annotated[str, AfterValidator(_validate_month_str)]
# [0-9] instead of \d: in the validator's regex engine \d also matches non-ASCII digits.
EmpCode = Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict=True: only a real JSON integer is accepted (no floats like 1.5, no "123" strings, no bool).
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


class EmployeeCreate(BaseModel):
    emp_code: EmpCode
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_times_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    # Annotated type + None default: an OMITTED field means "now", but an explicit null is
    # rejected (the contract does not make punched_at nullable). Pydantic does not validate defaults.
    punched_at: EpochMs = None  # type: ignore[assignment]
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: EpochMs = None  # type: ignore[assignment]  # omitted -> now; explicit null -> 422


class RegularizeRequest(BaseModel):
    """Only these fields can be corrected. Derived fields (work_hours, ...) sent by a client are
    ignored because pydantic drops unknown keys. An OMITTED field means "leave unchanged"; an
    explicit null is rejected (none of these fields is nullable in the contract)."""

    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Response serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(value: Any) -> Any:
    """BSON datetimes -> epoch ms, recursively (needed for history[].changes.*.from/to)."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: _api_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_api_value(v) for v in value]
    return value


def employee_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created) if created is not None else None,
    }


def attendance_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy/seeded documents may omit fields: missing history -> [], half_day -> False,
    late/overtime -> 0 (DATA_MODEL.md). _id is never returned."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    history = []
    for h in doc.get("history") or []:
        at = h.get("at")
        history.append(
            {
                "at": dt_to_ms(at) if isinstance(at, datetime) else at,
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": _api_value(h.get("changes") or {}),
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pin) if pin is not None else None,
        "punch_out": dt_to_ms(pout) if pout is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only when MongoDB answers a ping; otherwise 503."""
    try:
        get_db().command("ping")
    except (PyMongoError, DatabaseUnavailable):
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:  # Mongo was down at startup: build the indexes now (best effort)
        try:
            ensure_indexes()
        except Exception:
            logger.exception("index creation failed during health check")
    return {"status": "ok"}


@app.post("/employees", status_code=201, dependencies=[Depends(require_indexes)])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc_millis()
    try:
        # No "find first, then insert": that is a race. The unique index decides the winner.
        employees_col().insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    query: Dict[str, Any] = {} if department is None else {"department": department}  # exact, case-sensitive
    col = employees_col()
    total = col.count_documents(query)  # the FILTERED total (R10)
    skip = (page - 1) * page_size
    items = []
    if skip <= MAX_SKIP:
        cursor = col.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, dependencies=[Depends(require_indexes)])
def punch_in(body: PunchInRequest):
    emp = employees_col().find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # Whole seconds, UTC-aware. This truncated value is what is stored and returned (R1).
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])  # R1, overnight aware
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp["shift_start"], emp["shift_end"], date_str, ts, None),  # R2
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one concurrent insert win.
        attendance_col().insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "a record already exists for this employee and date")
    return attendance_out(doc)


ATTENDANCE_LIST_PROJECTION = {"_id": 0}  # GET /attendance never returns _id; explain must use the same projection


def validate_attendance_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    """GET /attendance (and its explain): date_from must not be after date_to -> 422."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise_validation_error(("query", "date_from"), "date_from must not be after date_to", date_from)


def attendance_list_plan(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> Tuple[Dict[str, Any], list]:
    """The filter and sort of GET /attendance, in one place so the explain endpoint can
    build exactly the same query."""
    query: Dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: Dict[str, str] = {}
        if date_from is not None:
            rng["$gte"] = date_from  # inclusive
        if date_to is not None:
            rng["$lte"] = date_to    # inclusive
        query["date"] = rng
    if status is not None:
        query["status"] = status
    # Required order: date DESC, then emp_code ASC. With a single fixed emp_code the second key
    # is constant, so we drop it and let the (emp_code, date) index serve a plain backward scan.
    sort = [("date", -1)] if emp_code is not None else [("date", -1), ("emp_code", 1)]
    return query, sort


def attendance_list_index_hint(emp_code: Optional[str], status: Optional[str]) -> str:
    """Return the index that supplies both the selected filter shape and list ordering."""
    if emp_code is not None and status is not None:
        return "emp_code_1_status_1_date_-1"
    if emp_code is not None:
        return "uq_emp_code_date"
    if status is not None:
        return "status_1_date_-1_emp_code_1"
    return "date_-1_emp_code_1"


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    validate_attendance_range(date_from, date_to)
    query, sort = attendance_list_plan(emp_code, date_from, date_to, status)
    hint = attendance_list_index_hint(emp_code, status)
    col = attendance_col()
    total = col.count_documents(query)  # filtered total (R10), computed by the database
    skip = (page - 1) * page_size
    items = []
    if skip <= MAX_SKIP:
        # The database sorts (using an index) and slices; we never load the whole collection.
        cursor = col.find(query, ATTENDANCE_LIST_PROJECTION).hint(hint).sort(sort).skip(skip).limit(page_size)
        items = [attendance_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-out (R1, R3, R4, R5)
# --------------------------------------------------------------------------- #
def apply_punch_out(emp_code: str, punched_at_ms: Optional[int]) -> Dict[str, Any]:
    """Close the employee's most recent record whose punch_in <= punched_at.

    Order of checks (each maps to one status code of the contract):
      404 unknown employee -> 404 no punch-in exists -> 409 that record is already punched out
      -> 422 punched_at <= punch_in or more than 24 h after it.
    The write is ONE atomic find_one_and_update whose filter says "this exact record, still open,
    punch_in unchanged". Two simultaneous punch-outs therefore cannot both match: the database
    lets the first one in, and the second one matches nothing and is answered with 409.
    """
    emp = employees_col().find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(punched_at_ms) if punched_at_ms is not None else now_utc()  # whole seconds (R1)
    col = attendance_col()
    for _ in range(WRITE_ATTEMPTS):
        # Most recent presence record with punch_in <= ts. A null/missing punch_in (ABSENT, LEAVE)
        # never satisfies "$lte" (comparisons are type-bracketed), so those records are skipped.
        record = col.find_one(
            {"emp_code": emp_code, "punch_in": {"$lte": ts}, "status": {"$in": list(PRESENCE_STATUSES)}},
            sort=[("punch_in", -1)],
        )
        if record is None:
            # The contract distinguishes an employee with no punch-in (404) from an invalid
            # punch-out for an existing one (422).  The <= lookup above cannot see a punch-in
            # one second in the future, so check that case explicitly before returning 404.
            future_record = col.find_one(
                {"emp_code": emp_code, "punch_in": {"$gt": ts}, "status": {"$in": list(PRESENCE_STATUSES)}},
                sort=[("punch_in", 1)],
            )
            if future_record is not None:
                raise_validation_error(("body", "punched_at"), "punched_at must be after punch_in", punched_at_ms)
            raise HTTPException(404, "no punch-in found for this employee")
        if record.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")

        punch_in_dt = ensure_utc(record["punch_in"])
        if ts <= punch_in_dt:
            raise_validation_error(("body", "punched_at"), "punched_at must be after punch_in", punched_at_ms)
        if ts - punch_in_dt > MAX_SHIFT_DURATION:
            raise_validation_error(("body", "punched_at"), "punched_at must be within 24 hours of punch_in", punched_at_ms)

        # The record's own `date` fixes the shift window (overnight shifts end on date + 1).
        derived = derive_fields(emp["shift_start"], emp["shift_end"], record["date"], punch_in_dt, ts)
        updated = col.find_one_and_update(
            {
                "emp_code": emp_code,
                "date": record["date"],
                "punch_in": record["punch_in"],  # derived values below were computed from THIS punch_in
                "punch_out": None,               # null or missing: still open. This is the race guard.
                "status": {"$in": list(PRESENCE_STATUSES)},
            },
            {  # late_minutes is deliberately not touched: it belongs to punch-in (R2). No history (PATCH only).
                "$set": {
                    "punch_out": ts,
                    "work_hours": derived["work_hours"],
                    "overtime_minutes": derived["overtime_minutes"],
                    "half_day": derived["half_day"],
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return attendance_out(updated)
        # Matched nothing: another request changed this record between our read and our write.
        # Loop: if it was a punch-out, the next read answers 409; if it was a correction, retry on fresh data.
    raise HTTPException(409, "record was modified concurrently, please retry")


@app.post("/attendance/punch-out", dependencies=[Depends(require_indexes)])
def punch_out(body: PunchOutRequest):
    return apply_punch_out(body.emp_code, body.punched_at)


# --------------------------------------------------------------------------- #
# Manual correction (PATCH) - R1-R5 recomputed, one history entry per success
# --------------------------------------------------------------------------- #
CORRECTABLE_FIELDS = (  # key order of history[].changes
    "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day",
)


class RegularizeInvalid(Exception):
    """A correction that breaks a contract rule (-> 422). `field` is the request field to blame."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def record_state(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The seven correctable fields of a stored record, with legacy gaps filled the way
    DATA_MODEL.md says (missing late/overtime -> 0, missing half_day -> false, missing punch -> null)."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    return {
        "status": doc["status"],
        "punch_in": ensure_utc(pin) if pin is not None else None,
        "punch_out": ensure_utc(pout) if pout is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def plan_regularization(
    emp: Dict[str, Any], record: Dict[str, Any], supplied: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Pure function (no database): apply a correction to a record's state.

    `supplied` holds only the fields the client sent: status (str), punch_in / punch_out
    (UTC datetimes already truncated to whole seconds). Returns (new_state, changes) where
    `changes` = {field: {"from": old, "to": new}} for the fields whose value REALLY changed.
    Raises RegularizeInvalid for every rule of the contract that is broken.
    """
    old = record_state(record)
    status = supplied.get("status", old["status"])

    if status in ("ABSENT", "LEAVE"):
        # These statuses clear both punch times, so supplying one is a contradiction.
        for field in ("punch_in", "punch_out"):
            if field in supplied:
                raise RegularizeInvalid(field, f"{field} cannot be supplied when status is {status}")
        new = {
            "status": status, "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        }
    else:
        p_in = supplied.get("punch_in", old["punch_in"])
        p_out = supplied.get("punch_out", old["punch_out"])
        if p_in is None:
            raise RegularizeInvalid("punch_in", f"punch_in is required for status {status}")
        if "punch_in" in supplied and attendance_date_for(p_in, emp["shift_start"], emp["shift_end"]) != record["date"]:
            raise RegularizeInvalid("punch_in", f"punch_in must fall on the attendance date {record['date']} (R1)")
        if p_out is not None:
            if p_out <= p_in:
                raise RegularizeInvalid("punch_out", "punch_out must be after punch_in")
            if p_out - p_in > MAX_SHIFT_DURATION:
                raise RegularizeInvalid("punch_out", "punch_out must be within 24 hours of punch_in")
        new = {"status": status, "punch_in": p_in, "punch_out": p_out,
               **derive_fields(emp["shift_start"], emp["shift_end"], record["date"], p_in, p_out)}

    changes = {f: {"from": old[f], "to": new[f]} for f in CORRECTABLE_FIELDS if old[f] != new[f]}
    if not changes:
        raise RegularizeInvalid("body", "the request does not change anything")
    return new, changes


def record_version_filter(record: Dict[str, Any]) -> Dict[str, Any]:
    """Filter that matches the record ONLY IF it is still exactly as we read it.

    Pins every field a correction depends on (status, both punch times) plus the length of
    `history`. Any other writer (a punch-out, or another correction) changes at least one of
    those, so our write then matches nothing instead of silently overwriting its work.
    `{"punch_in": None}` matches null AND missing, as DATA_MODEL.md requires for legacy records.
    """
    flt: Dict[str, Any] = {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    n = len(record.get("history") or [])
    if n:
        flt["history"] = {"$size": n}
    else:  # legacy documents have no history field at all
        flt["$or"] = [{"history": None}, {"history": {"$size": 0}}]
    return flt


def apply_regularization(emp_code: str, date_str: str, supplied: Dict[str, Any], reason: str, by: str) -> Dict[str, Any]:
    """Optimistic concurrency: read -> plan -> one atomic update pinned to what we read.

    The update $sets only the changed fields and $pushes ONE history entry. We never write the
    whole history array back, so an entry added by someone else cannot be overwritten. If the
    pinned filter matches nothing, somebody else changed the record first: we re-read and plan
    again on the fresh state (up to WRITE_ATTEMPTS times), then answer 409.
    """
    emp = employees_col().find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    col = attendance_col()
    for attempt in range(WRITE_ATTEMPTS):
        record = col.find_one({"emp_code": emp_code, "date": date_str})
        if record is None:
            raise HTTPException(404, "no attendance record for that employee and date")
        try:
            new, changes = plan_regularization(emp, record, supplied)
        except RegularizeInvalid as exc:
            if attempt == 0:  # judged against the state the client could see: a plain 422
                raise_validation_error(("body", exc.field), exc.message)
            # A retry is only needed because someone else edited the record meanwhile, so a
            # rule that now fails (or "changes nothing") is a conflict with that edit, not a bad request.
            raise HTTPException(409, "record was modified concurrently, please retry")
        entry = {"at": now_utc_millis(), "by": by, "reason": reason, "changes": changes}
        fields = {f: new[f] for f in changes}
        if record.get("history"):
            # Existing entries: append exactly one, never write the array back.
            update = {"$set": fields, "$push": {"history": entry}}
        else:
            # history is missing, null or []. record_version_filter() pins exactly that state, so writing a
            # one-element array cannot overwrite anybody's entry. ($push onto a null field is a server error -> 500.)
            update = {"$set": {**fields, "history": [entry]}}
        updated = col.find_one_and_update(
            record_version_filter(record),
            update,
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return attendance_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


@app.patch("/attendance/{emp_code}/{date}", dependencies=[Depends(require_indexes)])
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeRequest):
    supplied: Dict[str, Any] = {}
    if body.status is not None:
        supplied["status"] = body.status
    if body.punch_in is not None:
        supplied["punch_in"] = ms_to_dt(body.punch_in)    # whole seconds (R1)
    if body.punch_out is not None:
        supplied["punch_out"] = ms_to_dt(body.punch_out)
    return apply_regularization(emp_code, date, supplied, body.reason, body.regularized_by)


# --------------------------------------------------------------------------- #
# Analytics (Phase 3). EVERY number below is computed by a MongoDB aggregation pipeline.
# Python only (1) validates the request, (2) builds the pipeline, (3) runs it, (4) returns the rows.
#
# Rules shared by all four pipelines
#   * Stored derived values are used as they are (late_minutes, overtime_minutes, half_day,
#     work_hours); nothing is recomputed from punch times. A missing field counts as 0 / false.
#   * "Present" = status PRESENT, WFH or ON_DUTY (R6).
#   * Mon-Fri is decided from the record's `date` string with $dayOfWeek (1 = Sunday ... 7 = Saturday,
#     so Mon-Fri = 2..6). Dates are plain IST calendar strings, so no time-zone maths is needed.
#   * Rounding (R8) is exact half-up and is done with INTEGER arithmetic inside the pipeline
#     (see _half_up_scaled). MongoDB's own $round is banker's rounding, which R8 forbids.
#   * Half days are counted in "half units": a full present day = 2, a half day = 1. Dividing by 2 at
#     the very end avoids 0.5 floating-point sums.
# --------------------------------------------------------------------------- #
EMPLOYEES_COLLECTION = "employees"
ATTENDANCE_COLLECTION = "attendance_logs"
WEEKDAY_NUMBERS = [2, 3, 4, 5, 6]  # $dayOfWeek values for Monday..Friday
MAX_TREND_DAYS = 92                # trend range limit, counted in calendar days, both ends included


def month_bounds(month: str) -> Tuple[str, str]:
    """('2026-02-01', '2026-02-28') for '2026-02'. Dates are ISO strings, so comparing them as
    strings gives the same order as comparing real dates (and lets an index do the range scan)."""
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def _date_from(date_expr: Any) -> Dict[str, Any]:
    """Expression: 'YYYY-MM-DD' string -> date (midnight UTC; only used to ask 'which weekday?')."""
    return {"$dateFromString": {"dateString": date_expr, "onError": None}}


def _is_weekday(date_expr: Any) -> Dict[str, Any]:
    return {"$in": [{"$dayOfWeek": _date_from(date_expr)}, WEEKDAY_NUMBERS]}


def _half_up_scaled(numerator: Any, denominator: Any, places: int) -> Dict[str, Any]:
    """Expression for numerator / denominator rounded HALF-UP to `places` decimals (R8), returned as
    a whole number of 10**-places units (e.g. 0.8333 with places=4 -> 8333).

    Both inputs must be non-negative integers. The trick: x rounded half-up = floor(x + 1/2).
    With x = numerator * 10**places / denominator that becomes
        floor((2 * numerator * 10**places + denominator) / (2 * denominator))
    Only whole numbers go in, so there is no binary-float error that could turn 0.125 into 0.12.
    """
    scale = 10 ** places
    top = {"$add": [{"$multiply": [numerator, 2 * scale]}, denominator]}
    return {"$floor": {"$divide": [top, {"$multiply": [denominator, 2]}]}}


def _month_log_totals_group() -> Dict[str, Any]:
    """$group that boils ONE employee's logs for the month down to a single totals document."""
    present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    late = {"$ifNull": ["$late_minutes", 0]}
    has_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}
    present_half_units = {
        "$cond": [
            {"$and": [present, _is_weekday("$date")]},        # present AND Mon-Fri ...
            {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},  # ... half day = 1, full day = 2
            0,                                                # weekend / ABSENT / LEAVE -> 0
        ]
    }
    return {
        "$group": {
            "_id": None,
            "present_half_units": {"$sum": present_half_units},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},                           # weekends included
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            # work_hours always has 2 decimals, so x100 gives whole numbers that add up exactly.
            "hours_hundredths": {
                "$sum": {"$cond": [has_hours, {"$floor": {"$add": [{"$multiply": ["$work_hours", 100]}, 0.5]}}, 0]}
            },
            "hours_records": {"$sum": {"$cond": [has_hours, 1, 0]}},
        }
    }


def _lookup_month_totals(first: str, last: str) -> list:
    """For every employee document flowing by: attach that employee's month totals as field `t`
    (missing when the employee has no logs). Uses the unique (emp_code, date) index: one bounded
    index range per employee. Employees with no logs are KEPT (they still count for headcount)."""
    return [
        {
            "$lookup": {
                "from": ATTENDANCE_COLLECTION,
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, _month_log_totals_group()],
                "as": "t",
            }
        },
        {"$unwind": {"path": "$t", "preserveNullAndEmptyArrays": True}},
    ]


# ------------------------------- 1. employee monthly ------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """Runs on `employees`. Plain English:
    1. find the employee (unique index);  2. attach their month totals from attendance_logs;
    3. count Mon-Fri days from max(first of month, joined_on) to the end of the month;
    4. shape the answer; attendance_pct is rounded half-up with whole-number maths."""
    first, last = month_bounds(month)
    working_days = {
        "$size": {
            "$filter": {
                # day offsets 0..n-1 from the first counted day; keep the ones that fall on Mon-Fri
                "input": {"$range": [0, {"$toInt": "$days_counted"}]},
                "as": "i",
                "cond": {
                    "$in": [
                        {"$dayOfWeek": {"$dateAdd": {"startDate": _date_from("$count_from"), "unit": "day", "amount": "$$i"}}},
                        WEEKDAY_NUMBERS,
                    ]
                },
            }
        }
    }
    return [
        {"$match": {"emp_code": emp_code}},
        *_lookup_month_totals(first, last),
        # first day that counts = the later of "1st of month" and joined_on (mid-month joiners)
        {"$addFields": {"count_from": {"$max": [first, "$joined_on"]}}},
        {
            "$addFields": {
                "days_counted": {
                    "$cond": [
                        {"$gt": ["$count_from", last]},  # joined after this month -> no days
                        0,
                        {"$add": [{"$dateDiff": {"startDate": _date_from("$count_from"), "endDate": _date_from(last), "unit": "day"}}, 1]},
                    ]
                }
            }
        },
        {"$addFields": {"working_days": working_days, "present_half_units": {"$ifNull": ["$t.present_half_units", 0]}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": {"$divide": ["$present_half_units", 2]},
                "leave_days": {"$ifNull": ["$t.leave_count", 0]},
                "late_count": {"$ifNull": ["$t.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$t.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$t.total_overtime_minutes", 0]},
                # present_days / working_days * 100  ==  half_units * 50 / working_days
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        {"$divide": [_half_up_scaled({"$multiply": ["$present_half_units", 50]}, "$working_days", 2), 100]},
                    ]
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    rows = list(employees_col().aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


# ------------------------------- 2. department summary ------------------------------- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` (so people with NO logs are still there). Plain English:
    1. keep employees who joined on/before the last day of the month (R9) [and the department];
    2. attach each one's month totals; 3. group by department, adding the totals up;
    4. average work hours = total hours / number of RECORDS with hours (not an average of averages);
    5. sort by department."""
    first, last = month_bounds(month)
    match: Dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        *_lookup_month_totals(first, last),
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one per employee, logs or not
                "present_half_units": {"$sum": "$t.present_half_units"},
                "late_count": {"$sum": "$t.late_count"},
                "total_late_minutes": {"$sum": "$t.total_late_minutes"},
                "leave_count": {"$sum": "$t.leave_count"},
                "on_duty_count": {"$sum": "$t.on_duty_count"},
                "hours_hundredths": {"$sum": "$t.hours_hundredths"},
                "hours_records": {"$sum": "$t.hours_records"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$divide": ["$present_half_units", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_records", 0]},
                        {"$divide": [_half_up_scaled("$hours_hundredths", "$hours_records", 0), 100]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = list(employees_col().aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


# ------------------------------- 3. late leaderboard ------------------------------- #
def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    """Runs on `attendance_logs`. Plain English:
    1. take the month's late records (date-range index);  2. add up late minutes per employee;
    3. join the employee (drops logs of unknown employees) and apply the department filter;
    4. $setWindowFields + $rank gives standard competition ranking (1, 2, 2, 4) from the totals;
    5. keep rank <= limit (so ties at the cutoff all stay), then order by total desc, emp_code asc."""
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": EMPLOYEES_COLLECTION, "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # no employee document -> the row disappears
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # BEFORE ranking: rank inside the department
    pipeline += [
        # sortBy has only the total, so equal totals get the SAME rank and the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    items = list(attendance_col().aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


# ------------------------------- 4. department trend ------------------------------- #
def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    """Runs on `employees`. Plain English:
    1. take the department's employees; $facet splits the stream into two small answers:
         roster = how many people joined on each date (to get headcount for any day),
         daily  = per date: half-units present and late count (from each employee's logs in range);
    2. an empty roster means the department does not exist (the endpoint turns that into 404);
    3. $range builds one slot per calendar day (this is the gap filling: weekends and days with no
       logs get a row too) and each slot picks up its headcount and its daily numbers (0 if none);
    4. rate = present / headcount on working days only, kept as whole basis points (1 = 0.0001);
    5. $setWindowFields averages the basis points of the current row and the 6 rows before it
       ({documents: [-6, 0]}); the first rows simply have a shorter window; null rates are ignored."""
    n_days = (date_cls.fromisoformat(date_to) - date_cls.fromisoformat(date_from)).days + 1
    present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    day_slot = {
        "$let": {
            "vars": {
                "d": {
                    "$dateToString": {
                        "date": {"$dateAdd": {"startDate": _date_from(date_from), "unit": "day", "amount": "$$i"}},
                        "format": "%Y-%m-%d",
                    }
                }
            },
            "in": {
                "$let": {
                    "vars": {"hit": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$d"]}}}, 0]}},
                    "in": {
                        "date": "$$d",
                        "is_working_day": _is_weekday("$$d"),
                        "headcount": {
                            "$sum": {
                                "$map": {
                                    "input": {"$filter": {"input": "$roster", "as": "r", "cond": {"$lte": ["$$r._id", "$$d"]}}},
                                    "as": "r",
                                    "in": "$$r.n",
                                }
                            }
                        },
                        "present_half_units": {"$ifNull": ["$$hit.present_half_units", 0]},
                        "late_count": {"$ifNull": ["$$hit.late_count", 0]},
                    },
                }
            },
        }
    }
    return [
        {"$match": {"department": department}},
        {
            "$facet": {
                "roster": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
                "daily": [
                    {
                        "$lookup": {
                            "from": ATTENDANCE_COLLECTION,
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": 1,
                                        "present_half_units": {"$cond": [present, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]},
                                        "is_late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
                                    }
                                },
                            ],
                            "as": "logs",
                        }
                    },
                    {"$unwind": "$logs"},
                    {"$group": {"_id": "$logs.date", "present_half_units": {"$sum": "$logs.present_half_units"}, "late_count": {"$sum": "$logs.is_late"}}},
                ],
            }
        },
        {"$match": {"$expr": {"$gt": [{"$size": "$roster"}, 0]}}},
        {"$project": {"days": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": day_slot}}}},
        {"$unwind": "$days"},
        {"$replaceRoot": {"newRoot": "$days"}},
        {
            "$addFields": {
                "rate_bp": {  # attendance rate in basis points, half-up; null unless working day with headcount > 0
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _half_up_scaled("$present_half_units", {"$multiply": ["$headcount", 2]}, 4),
                        None,
                    ]
                }
            }
        },
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"avg_bp": {"$avg": "$rate_bp", "window": {"documents": [-6, 0]}}}}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_half_units", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$isNumber": "$rate_bp"}, {"$divide": ["$rate_bp", 10000]}, None]},
                # the window average of whole basis points is exact; floor(x + 1/2) rounds it half-up
                "moving_avg_7d": {
                    "$cond": [{"$isNumber": "$avg_bp"}, {"$divide": [{"$floor": {"$add": ["$avg_bp", 0.5]}}, 10000]}, None]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(date_from: str, date_to: str) -> None:
    """Trend (and its explain): to >= from, and at most MAX_TREND_DAYS calendar days -> else 422."""
    if date_to < date_from:
        raise_validation_error(("query", "to"), "to must not be before from", date_to)
    if (date_cls.fromisoformat(date_to) - date_cls.fromisoformat(date_from)).days + 1 > MAX_TREND_DAYS:
        raise_validation_error(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", date_to)


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    validate_trend_range(from_, to)
    items = list(employees_col().aggregate(department_trend_pipeline(department, from_, to)))
    if not items:  # the pipeline returns nothing only when the department has no employees at all
        raise HTTPException(404, "department not found")
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: GET /admin/explain/{endpoint}   (Phase 4)
#
# Idea: ask MongoDB HOW it would run the main query of an endpoint, and hand back its raw answer.
#   * Nothing here is invented or hard-coded. The query / pipeline is built by the SAME functions the
#     real endpoint uses (attendance_list_plan, employee_monthly_pipeline, department_summary_pipeline,
#     late_leaderboard_pipeline, department_trend_pipeline) and MongoDB itself produces the explain output.
#   * verbosity is "executionStats": the query is really executed and MongoDB reports the plan that won,
#     how many index keys / documents it looked at, and how long it took.
#   * Parameters follow openapi.yaml: every declared parameter is type-checked (bad month, limit 0,
#     status BOGUS ... -> 422); the parameters an endpoint REQUIRES are checked per endpoint (below).
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require_query_param(name: str, value: Optional[str]) -> str:
    """A parameter that the explained endpoint needs: missing or empty -> the usual 422 body."""
    if value is None:
        raise_validation_error(("query", name), "field required")
    if value == "":
        raise_validation_error(("query", name), f"{name} must not be empty", value)
    return value


def build_explain_request(
    endpoint: str,
    *,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    use_attendance_list_hint: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Pure function (no database): returns (collection name, MongoDB `explain` command document).

    Required parameters per endpoint (same as the real endpoint) - a missing one raises 422:
      attendance_list    none (emp_code, date_from, date_to, status, page, page_size are optional)
      employee_monthly   emp_code, month
      department_summary month                         (department optional)
      late_leaderboard   month                         (limit default 10, department optional)
      department_trend   department, from, to          (same 'to >= from' and 92-day rules)
    Parameters that the endpoint does not use are accepted and ignored.
    """
    if endpoint == "attendance_list":
        validate_attendance_range(date_from, date_to)
        query, sort = attendance_list_plan(emp_code, date_from, date_to, status)
        # The real endpoint skips (page-1)*page_size and returns [] when that is absurdly large. For explain
        # we cap the number so MongoDB does not reject it (int64 overflow); the plan shape is the same.
        skip = min((page - 1) * page_size, MAX_SKIP)
        find: Dict[str, Any] = {
            "find": ATTENDANCE_COLLECTION,
            "filter": query,
            "sort": dict(sort),  # dict keeps insertion order: date first, then emp_code
            "projection": ATTENDANCE_LIST_PROJECTION,
            "limit": page_size,
        }
        # Production requests and the admin explain route retain their explicit
        # index selection. The switch is only for the index-free control database.
        if use_attendance_list_hint:
            find["hint"] = attendance_list_index_hint(emp_code, status)
        if skip:
            find["skip"] = skip
        return ATTENDANCE_COLLECTION, {"explain": find, "verbosity": "executionStats"}

    if endpoint == "employee_monthly":
        code = require_query_param("emp_code", emp_code)
        mon = require_query_param("month", month)
        collection, pipeline = EMPLOYEES_COLLECTION, employee_monthly_pipeline(code, mon)
    elif endpoint == "department_summary":
        mon = require_query_param("month", month)
        collection, pipeline = EMPLOYEES_COLLECTION, department_summary_pipeline(mon, department)
    elif endpoint == "late_leaderboard":
        mon = require_query_param("month", month)
        collection, pipeline = ATTENDANCE_COLLECTION, late_leaderboard_pipeline(mon, limit, department)
    elif endpoint == "department_trend":
        dep = require_query_param("department", department)
        start = require_query_param("from", from_)
        end = require_query_param("to", to)
        validate_trend_range(start, end)
        collection, pipeline = EMPLOYEES_COLLECTION, department_trend_pipeline(dep, start, end)
    else:  # the route only lets the five known names through; this guards direct callers
        raise_validation_error(("path", "endpoint"), f"unsupported endpoint '{endpoint}'", endpoint)

    command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    return collection, command


def explain_json_safe(raw: Dict[str, Any]) -> Dict[str, Any]:
    """MongoDB's reply as plain JSON. It can hold BSON types (ObjectId, datetime, Int64, Timestamp);
    bson.json_util writes them in 'relaxed' extended JSON, so nothing is dropped or invented."""
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


def run_explain(db: Any, endpoint: str, **params: Any) -> Dict[str, Any]:
    """Build the command, let MongoDB explain it, return {endpoint, collection, explain}."""
    collection, command = build_explain_request(endpoint, **params)
    raw = db.command(command)  # raw explain output exactly as MongoDB produced it
    return {"endpoint": endpoint, "collection": collection, "explain": explain_json_safe(raw)}


@app.get("/admin/explain/{endpoint}", dependencies=[Depends(require_indexes)])
def explain_endpoint(
    endpoint: ExplainEndpoint,  # anything else -> 422 (enum in the contract)
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    # require_indexes (above) makes sure the startup indexes exist first, so the plan we show is the real one.
    result = run_explain(
        get_db(), endpoint,
        emp_code=emp_code, month=month, department=department, limit=limit, date_from=date_from,
        date_to=date_to, status=status, from_=from_, to=to, page=page, page_size=page_size,
    )
    return JSONResponse(content=result)
