"""Phase 3 analytics tests: the four /analytics/* endpoints.

Three layers:
  1. Pure tests (no database): month helper, month validation, and the SHAPE of the pipelines
     (they must use $setWindowFields/$rank, must not use $round, must not be built from Python loops).
  2. Hand-computed scenarios (need MongoDB >= 5.0 for $setWindowFields/$dateAdd): each one targets a named
     edge case and asserts exact numbers worked out by hand from openapi.yaml.
  3. A randomised cross-check (needs MongoDB): a seeded random dataset with ties, weekend records,
     employees without logs, mid-month joiners, half days, legacy documents and ghost logs is pushed through
     every endpoint, and the answers are compared with a deliberately simple, independent Python oracle
     (plain loops + Decimal). Loops are fine HERE because this is a test, not the endpoint.
"""
import calendar
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app import main
from tests.conftest import ist_ms, needs_mongo

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
EMP_KEYS = {"emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
            "total_late_minutes", "total_overtime_minutes", "attendance_pct"}
SUMMARY_KEYS = {"department", "headcount", "present_days", "avg_work_hours", "late_count",
                "total_late_minutes", "leave_count", "on_duty_count"}
BOARD_KEYS = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
TREND_KEYS = {"date", "is_working_day", "headcount", "present_count", "late_count",
              "attendance_rate", "moving_avg_7d"}


def emp(code, dept="Engineering", joined="2026-01-05", name=None, start="09:30", end="18:30"):
    return {"emp_code": code, "name": name or f"Name {code}", "email": f"{code.lower()}@example.com",
            "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)}


def log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=False, **omit):
    """One attendance document. Pass e.g. half_day=None-style removals via omit=... : any keyword
    named in `omit` with value None is REMOVED from the document (to build legacy/odd documents)."""
    d = {"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
         "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    for k, v in omit.items():
        if v is None:
            d.pop(k, None)
    return d


def seed(employees, logs):
    main.employees_col().delete_many({})
    main.attendance_col().delete_many({})
    if employees:
        main.employees_col().insert_many(employees)
    if logs:
        main.attendance_col().insert_many(logs)


def get_ok(client, url, **params):
    r = client.get(url, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def weekdays_between(first, last):
    d, n = first, 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def ist_dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=main.IST).astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 1. pure tests (no database)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("month,first,last", [
    ("2026-07", "2026-07-01", "2026-07-31"), ("2026-02", "2026-02-01", "2026-02-28"),
    ("2024-02", "2024-02-01", "2024-02-29"), ("2100-02", "2100-02-01", "2100-02-28"),
    ("2026-04", "2026-04-01", "2026-04-30"), ("2026-12", "2026-12-01", "2026-12-31"),
])
def test_month_bounds(month, first, last):
    assert main.month_bounds(month) == (first, last)


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "2026-7", "202607", "26-07", "2026-07-01", "", " 2026-07",
                                 "2026/07", "abcd-ef", "0000-05", "２０２６-07"])
def test_month_validator_rejects(bad):
    with pytest.raises(ValueError):
        main._validate_month_str(bad)


@pytest.mark.parametrize("good", ["2026-07", "1999-12", "0001-01", "9999-12"])
def test_month_validator_accepts(good):
    assert main._validate_month_str(good) == good


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


def _stage_names(pipeline):
    return [next(iter(s)) for s in pipeline]


ALL_PIPELINES = {
    "monthly": lambda: main.employee_monthly_pipeline("EMP0001", "2026-07"),
    "summary": lambda: main.department_summary_pipeline("2026-07", None),
    "leaderboard": lambda: main.late_leaderboard_pipeline("2026-07", 10, None),
    "trend": lambda: main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-31"),
}


@pytest.mark.parametrize("name", list(ALL_PIPELINES))
def test_pipelines_never_use_banker_rounding_operator(name):
    """R8 wants half-up. MongoDB's $round is round-half-even, so it must not appear anywhere."""
    assert "$round" not in set(_all_keys(ALL_PIPELINES[name]()))


def test_leaderboard_pipeline_ranks_with_window_rank_and_limits_after_ranking():
    pipe = main.late_leaderboard_pipeline("2026-07", 3, None)
    stages = _stage_names(pipe)
    assert "$setWindowFields" in stages
    window = pipe[stages.index("$setWindowFields")]["$setWindowFields"]
    assert window["output"]["rank"] == {"$rank": {}}
    assert list(window["sortBy"]) == ["total_late_minutes"]          # emp_code in sortBy would break ties
    # limit comes from `rank <= limit` AFTER ranking, never from $limit / $skip
    assert "$limit" not in stages and "$skip" not in stages
    rank_filter = pipe[stages.index("$setWindowFields") + 1]
    assert rank_filter == {"$match": {"rank": {"$lte": 3}}}
    assert pipe[-2] == {"$sort": {"total_late_minutes": -1, "_id": 1}} or pipe[-2]["$sort"] == {"total_late_minutes": -1, "_id": 1}


def test_leaderboard_department_filter_comes_before_ranking():
    stages = _stage_names(main.late_leaderboard_pipeline("2026-07", 10, "Sales"))
    dept_match = [i for i, s in enumerate(main.late_leaderboard_pipeline("2026-07", 10, "Sales"))
                  if s.get("$match", {}).get("emp.department") == "Sales"]
    assert dept_match and dept_match[0] < stages.index("$setWindowFields")


def test_trend_pipeline_uses_7_row_window_and_generates_days_in_database():
    pipe = main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-31")
    stages = _stage_names(pipe)
    window = pipe[stages.index("$setWindowFields")]["$setWindowFields"]
    assert window["sortBy"] == {"date": 1}
    (out,) = window["output"].values()
    assert out["window"] == {"documents": [-6, 0]}
    keys = set(_all_keys(pipe))
    assert "$range" in keys and "$dateAdd" in keys      # the calendar is generated inside MongoDB


def test_summary_pipeline_starts_from_employees_so_no_log_people_count():
    pipe = main.department_summary_pipeline("2026-07", None)
    assert next(iter(pipe[0])) == "$match" and pipe[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}}
    assert any("$lookup" in s for s in pipe)
    unwind = [s["$unwind"] for s in pipe if "$unwind" in s][0]
    assert unwind["preserveNullAndEmptyArrays"] is True


def test_employee_index_for_summary_is_declared():
    names = {m.document["name"] for m in main.EMPLOYEE_INDEXES}
    assert {"uq_emp_code", "department_1_emp_code_1", "joined_on_1"} <= names


# --------------------------------------------------------------------------- #
# 2a. employee monthly
# --------------------------------------------------------------------------- #
@needs_mongo
def test_monthly_full_scenario(client, clean):
    """2026-07: 23 working days (July 1 is a Wednesday; 8 weekend days)."""
    seed([emp("EMP0001")], [
        log("EMP0001", "2026-07-01", "PRESENT", wh=9.0),                       # 1.0
        log("EMP0001", "2026-07-02", "WFH", wh=3.5, half=True),                # 0.5
        log("EMP0001", "2026-07-03", "ON_DUTY", wh=8.0),                       # 1.0
        log("EMP0001", "2026-07-06", "ABSENT"),                                # 0
        log("EMP0001", "2026-07-07", "LEAVE"),                                 # leave_days 1
        log("EMP0001", "2026-07-08", "PRESENT", late=35, ot=40, wh=9.08),      # 1.0, late 35, ot 40
        log("EMP0001", "2026-07-09", "PRESENT", wh=9.0, half_day=None, history=None,   # legacy doc: fields missing
            late_minutes=None, overtime_minutes=None),                          # 1.0
        log("EMP0001", "2026-07-04", "PRESENT", late=20, ot=50, wh=9.0),       # SATURDAY: late+ot yes, present no
        log("EMP0001", "2026-06-30", "PRESENT", late=99, ot=99, wh=9.0),       # previous month
        log("EMP0001", "2026-08-01", "PRESENT", late=77, ot=77, wh=9.0),       # next month (a Saturday)
    ])
    body = get_ok(client, "/analytics/employees/EMP0001/monthly", month="2026-07")
    assert set(body) == EMP_KEYS
    assert body == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 4.5,
                    "leave_days": 1, "late_count": 2, "total_late_minutes": 55, "total_overtime_minutes": 90,
                    "attendance_pct": 19.57}                              # 4.5 / 23 * 100 = 19.5652...
    assert "_id" not in body and "id" not in body


@needs_mongo
def test_monthly_mid_month_joiner_and_half_up_tie(client, clean):
    """Joined Fri 2026-07-10 -> working days 10th, 13-17, 20-24, 27-31 = 16.
    One half day = 0.5 present -> 0.5 / 16 * 100 = 3.125 exactly -> half-up 3.13 (banker's would give 3.12)."""
    seed([emp("EMP0002", joined="2026-07-10")], [
        log("EMP0002", "2026-07-14", "WFH", wh=3.0, half=True),
        log("EMP0002", "2026-07-11", "PRESENT", late=15, wh=8.0),   # Saturday
    ])
    body = get_ok(client, "/analytics/employees/EMP0002/monthly", month="2026-07")
    assert body["working_days"] == 16
    assert body["present_days"] == 0.5
    assert body["attendance_pct"] == 3.13
    assert body["late_count"] == 1 and body["total_late_minutes"] == 15


@needs_mongo
def test_monthly_joined_on_first_of_month_and_on_weekend(client, clean):
    seed([emp("EMP0001", joined="2026-07-01"), emp("EMP0002", joined="2026-07-04"),   # Saturday
          emp("EMP0003", joined="2026-07-31"), emp("EMP0004", joined="2026-07-25")], [])  # Fri / Sat
    wd = lambda c: get_ok(client, f"/analytics/employees/{c}/monthly", month="2026-07")["working_days"]
    assert wd("EMP0001") == 23
    assert wd("EMP0002") == 20        # from Sat Jul 4: weekdays 6-10, 13-17, 20-24, 27-31 = 4 x 5 = 20
    assert wd("EMP0003") == 1         # Friday Jul 31 only
    assert wd("EMP0004") == 5         # Sat Jul 25 -> Jul 27-31


@needs_mongo
def test_monthly_joined_after_month_has_zero_working_days_and_null_pct(client, clean):
    seed([emp("EMP0003", joined="2026-08-03")], [])
    body = get_ok(client, "/analytics/employees/EMP0003/monthly", month="2026-07")
    assert body["working_days"] == 0 and body["attendance_pct"] is None
    assert body["present_days"] == 0 and body["leave_days"] == 0 and body["late_count"] == 0
    assert body["total_late_minutes"] == 0 and body["total_overtime_minutes"] == 0


@needs_mongo
def test_monthly_employee_without_any_logs_has_pct_zero_not_null(client, clean):
    seed([emp("EMP0004")], [log("EMP0001", "2026-07-01", wh=9.0)])   # someone else's log
    body = get_ok(client, "/analytics/employees/EMP0004/monthly", month="2026-07")
    assert body["working_days"] == 23 and body["present_days"] == 0 and body["attendance_pct"] == 0.0


@needs_mongo
@pytest.mark.parametrize("month,wd", [("2026-02", 20), ("2024-02", 21), ("2026-07", 23), ("2026-05", 21)])
def test_monthly_working_days_for_different_month_lengths(client, clean, month, wd):
    seed([emp("EMP0001", joined="2020-01-01")], [])
    assert get_ok(client, "/analytics/employees/EMP0001/monthly", month=month)["working_days"] == wd


@needs_mongo
def test_monthly_missing_weekday_is_not_absent_but_counts_in_working_days(client, clean):
    """Only 2 records in the month: the 21 weekdays without a record add nothing to present_days."""
    seed([emp("EMP0001")], [log("EMP0001", "2026-07-01", wh=9.0), log("EMP0001", "2026-07-02", "ABSENT")])
    body = get_ok(client, "/analytics/employees/EMP0001/monthly", month="2026-07")
    assert body["working_days"] == 23 and body["present_days"] == 1 and body["attendance_pct"] == 4.35  # 4.3478


@needs_mongo
def test_monthly_leave_on_weekend_still_counts_as_leave_record(client, clean):
    """leave_days = number of LEAVE records (contract), whatever weekday they sit on."""
    seed([emp("EMP0001")], [log("EMP0001", "2026-07-04", "LEAVE"), log("EMP0001", "2026-07-06", "LEAVE")])
    assert get_ok(client, "/analytics/employees/EMP0001/monthly", month="2026-07")["leave_days"] == 2


@needs_mongo
def test_monthly_overnight_shift_uses_the_record_date_not_the_punch_dates(client, clean):
    """Overnight 22:00-06:00. The record `date` is the day the shift STARTED (R1). Weekday-ness comes from it."""
    e = emp("EMP0005", start="22:00", end="06:00")
    cases = [  # (punch_in IST, punch_out IST)
        (ist_dt(2026, 7, 3, 21, 55), ist_dt(2026, 7, 4, 6, 40)),    # Fri night -> date Fri 07-03: present, ot 40
        (ist_dt(2026, 7, 4, 22, 20), ist_dt(2026, 7, 5, 6, 45)),    # Sat night -> date Sat 07-04: late 20, ot 45, no present
        (ist_dt(2026, 7, 6, 1, 10), ist_dt(2026, 7, 6, 6, 35)),     # Mon 01:10 belongs to Sunday's shift -> date Sun 07-05: late 190, ot 35
        (ist_dt(2026, 7, 6, 22, 5), ist_dt(2026, 7, 7, 6, 10)),     # Mon night -> date Mon 07-06: inside grace, ot 10 -> 0
    ]
    docs = []
    for pin, pout in cases:
        day = main.attendance_date_for(pin, e["shift_start"], e["shift_end"])
        derived = main.derive_fields(e["shift_start"], e["shift_end"], day, pin, pout)
        docs.append({"emp_code": "EMP0005", "date": day, "status": "PRESENT", "punch_in": pin, "punch_out": pout,
                     "history": [], **derived})
    assert [d["date"] for d in docs] == ["2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06"]
    seed([e], docs)
    body = get_ok(client, "/analytics/employees/EMP0005/monthly", month="2026-07")
    assert body["present_days"] == 2                       # Fri 07-03 and Mon 07-06 only
    assert body["late_count"] == 2 and body["total_late_minutes"] == 20 + 190
    assert body["total_overtime_minutes"] == 40 + 45 + 35  # weekend records count here


@needs_mongo
def test_monthly_unknown_employee_404(client, clean):
    seed([emp("EMP0001")], [])
    r = client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
    assert r.status_code == 404 and "detail" in r.json()


@needs_mongo
@pytest.mark.parametrize("params", [{}, {"month": "2026-13"}, {"month": "2026-7"}, {"month": "2026-07-01"}, {"month": ""},
                                    {"month": "0000-01"}])
def test_monthly_validation_422_even_for_unknown_employee(client, clean, params):
    seed([emp("EMP0001")], [])
    for code in ("EMP0001", "EMP9999"):
        r = client.get(f"/analytics/employees/{code}/monthly", params=params)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)


@needs_mongo
def test_monthly_empty_database_is_404_not_500(client, clean):
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).status_code == 404


# --------------------------------------------------------------------------- #
# 2b. department summary
# --------------------------------------------------------------------------- #
def summary_world():
    employees = [
        emp("EMP0001", "Engineering", "2026-01-05"),
        emp("EMP0002", "Engineering", "2026-07-10"),          # joined mid-month, has logs
        emp("EMP0003", "Engineering", "2026-02-01"),          # NO logs at all -> still counts
        emp("EMP0004", "Engineering", "2026-08-01"),          # joins after July -> not counted
        emp("EMP0005", "Sales", "2026-03-01"),
        emp("EMP0006", "Sales", "2026-03-01"),
        emp("EMP0007", "HR", "2026-08-15"),                   # whole department joins later -> omitted
        emp("EMP0008", "Marketing", "2026-07-31"),            # joins on the last day -> counted
    ]
    logs = [
        log("EMP0001", "2026-07-01", "PRESENT", wh=9.12),
        log("EMP0001", "2026-07-02", "WFH", wh=3.5, half=True),
        log("EMP0001", "2026-07-03", "ON_DUTY", wh=8.5),
        log("EMP0001", "2026-07-04", "PRESENT", wh=7.0),                 # Saturday: hours count, present_days not
        log("EMP0001", "2026-07-06", "LEAVE"),
        log("EMP0001", "2026-07-07", "ABSENT"),
        log("EMP0002", "2026-07-13", "PRESENT"),                         # open record: no work_hours
        log("EMP0002", "2026-07-14", "PRESENT", late=40, wh=8.75),
        log("EMP0001", "2026-06-30", "PRESENT", late=50, wh=1.0),        # other months ignored
        log("EMP0001", "2026-08-03", "PRESENT", late=60, wh=1.0),
        log("EMP0005", "2026-07-01", "PRESENT", wh=8.74),
        log("EMP0006", "2026-07-01", "PRESENT", wh=8.75),
        log("EMP0006", "2026-07-02", "ABSENT", wh=6.0),                  # absent record with an hours value: ignored
        log("GHOST01", "2026-07-01", "PRESENT", late=500, wh=9.0),       # employee does not exist -> ignored
    ]
    return employees, logs


@needs_mongo
def test_summary_world(client, clean):
    seed(*summary_world())
    body = get_ok(client, "/analytics/departments/summary", month="2026-07")
    assert body["month"] == "2026-07"
    assert [i["department"] for i in body["items"]] == ["Engineering", "Marketing", "Sales"]   # sorted; HR omitted
    assert all(set(i) == SUMMARY_KEYS for i in body["items"])
    eng, mkt, sales = body["items"]
    # headcount: EMP0001, 0002, 0003 (no logs!) = 3; EMP0004 joins in August -> excluded
    assert eng["headcount"] == 3
    assert eng["present_days"] == 4.5          # E1: 1 + 0.5 + 1 (Saturday ignored) ; E2: 1 + 1
    # hours over RECORDS: 9.12 + 3.5 + 8.5 + 7.0 + 8.75 = 36.87 / 5 = 7.374 -> 7.37
    # (an average of per-employee averages would be (7.03 + 8.75) / 2 = 7.89)
    assert eng["avg_work_hours"] == 7.37
    assert eng["late_count"] == 1 and eng["total_late_minutes"] == 40
    assert eng["leave_count"] == 1 and eng["on_duty_count"] == 1
    # Marketing: joined on the 31st, counted, no logs -> zeros and null average
    assert mkt == {"department": "Marketing", "headcount": 1, "present_days": 0, "avg_work_hours": None,
                   "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    # Sales: (8.74 + 8.75) / 2 = 8.745 -> half-up 8.75 (float rounding / banker's would give 8.74)
    assert sales["headcount"] == 2 and sales["avg_work_hours"] == 8.75 and sales["present_days"] == 2


@needs_mongo
def test_summary_department_filter(client, clean):
    seed(*summary_world())
    body = get_ok(client, "/analytics/departments/summary", month="2026-07", department="Sales")
    assert [i["department"] for i in body["items"]] == ["Sales"]
    assert get_ok(client, "/analytics/departments/summary", month="2026-07", department="sales")["items"] == []   # case-sensitive
    assert get_ok(client, "/analytics/departments/summary", month="2026-07", department="Nope")["items"] == []
    assert get_ok(client, "/analytics/departments/summary", month="2026-07", department="HR")["items"] == []     # nobody joined yet


@needs_mongo
def test_summary_other_month_counts_later_joiners(client, clean):
    seed(*summary_world())
    items = get_ok(client, "/analytics/departments/summary", month="2026-08")["items"]
    by = {i["department"]: i for i in items}
    assert by["Engineering"]["headcount"] == 4 and by["HR"]["headcount"] == 1
    assert by["Engineering"]["late_count"] == 1 and by["Engineering"]["total_late_minutes"] == 60
    assert by["Engineering"]["avg_work_hours"] == 1.0


@needs_mongo
def test_summary_empty_database_and_empty_month(client, clean):
    assert get_ok(client, "/analytics/departments/summary", month="2026-07") == {"month": "2026-07", "items": []}
    seed(*summary_world())
    items = get_ok(client, "/analytics/departments/summary", month="2025-01")["items"]   # before anybody joined
    assert items == []


@needs_mongo
def test_summary_hours_only_from_present_status_records_with_a_value(client, clean):
    seed([emp("EMP0001")], [
        log("EMP0001", "2026-07-01", "PRESENT", wh=8.0),
        log("EMP0001", "2026-07-02", "LEAVE", wh=5.0),         # not present -> ignored
        log("EMP0001", "2026-07-03", "PRESENT", wh=None),      # present but no hours yet -> ignored
        log("EMP0001", "2026-07-06", "ON_DUTY", wh=9.0),
    ])
    item = get_ok(client, "/analytics/departments/summary", month="2026-07")["items"][0]
    assert item["avg_work_hours"] == 8.5 and item["present_days"] == 3


@needs_mongo
def test_summary_validation_422(client, clean):
    for params in ({}, {"month": "2026-13"}, {"month": "July"}):
        assert client.get("/analytics/departments/summary", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# 2c. late leaderboard
# --------------------------------------------------------------------------- #
def board_world():
    """Totals: 0001=100, 0002=80, 0003=80, 0004=50, 0005=50, 0006=10, 0007=0 (never late).
    Standard competition ranks: 1, 2, 2, 4, 4, 6.  Employees are inserted in REVERSE code order."""
    codes = [f"EMP000{i}" for i in range(7, 0, -1)]
    employees = [emp(c, "Engineering" if int(c[-1]) % 2 else "Sales", name=f"Person {c[-1]}") for c in codes]
    logs = [
        log("EMP0001", "2026-07-01", late=60), log("EMP0001", "2026-07-04", late=40),   # 100 (one on a Saturday)
        log("EMP0002", "2026-07-02", late=80),
        log("EMP0003", "2026-07-03", late=30), log("EMP0003", "2026-07-06", late=50),   # 80 from two records
        log("EMP0004", "2026-07-07", late=50),
        log("EMP0005", "2026-07-08", late=20), log("EMP0005", "2026-07-09", late=30),   # 50
        log("EMP0006", "2026-07-10", late=10),
        log("EMP0007", "2026-07-13", late=0),
        log("EMP0001", "2026-06-30", late=999), log("EMP0001", "2026-08-01", late=999),  # outside the month
        log("GHOST01", "2026-07-01", late=1000),                                         # unknown employee
    ]
    return employees, logs


def rows(body):
    return [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in body["items"]]


@needs_mongo
def test_leaderboard_ranks_ties_and_skips(client, clean):
    seed(*board_world())
    body = get_ok(client, "/analytics/leaderboard/late", month="2026-07", limit=50)
    assert body["month"] == "2026-07"
    assert all(set(r) == BOARD_KEYS for r in body["items"])
    assert rows(body) == [(1, "EMP0001", 100, 2), (2, "EMP0002", 80, 1), (2, "EMP0003", 80, 2),
                          (4, "EMP0004", 50, 1), (4, "EMP0005", 50, 2), (6, "EMP0006", 10, 1)]
    # EMP0007 (0 late minutes) and the ghost are absent; names / departments come from employees
    first = body["items"][0]
    assert first["name"] == "Person 1" and first["department"] == "Engineering"


@needs_mongo
@pytest.mark.parametrize("limit,expected_ranks", [
    (1, [1]),
    (2, [1, 2, 2]),            # tie AT the cutoff: both rank-2 rows are returned -> 3 rows for limit 2
    (3, [1, 2, 2]),            # rank 3 does not exist; rank 4 > 3 is excluded
    (4, [1, 2, 2, 4, 4]),      # both rank-4 rows come back
    (5, [1, 2, 2, 4, 4]),
    (6, [1, 2, 2, 4, 4, 6]),
    (50, [1, 2, 2, 4, 4, 6]),
])
def test_leaderboard_limit_applies_after_ranking(client, clean, limit, expected_ranks):
    seed(*board_world())
    body = get_ok(client, "/analytics/leaderboard/late", month="2026-07", limit=limit)
    assert [r["rank"] for r in body["items"]] == expected_ranks


@needs_mongo
def test_leaderboard_tied_rows_are_ordered_by_emp_code(client, clean):
    seed(*board_world())
    items = get_ok(client, "/analytics/leaderboard/late", month="2026-07", limit=6)["items"]
    assert [r["emp_code"] for r in items if r["rank"] == 2] == ["EMP0002", "EMP0003"]
    assert [r["emp_code"] for r in items if r["rank"] == 4] == ["EMP0004", "EMP0005"]


@needs_mongo
def test_leaderboard_department_ranks_inside_the_department(client, clean):
    seed(*board_world())      # Engineering = odd codes 0001,0003,0005,0007 ; Sales = even 0002,0004,0006
    eng = get_ok(client, "/analytics/leaderboard/late", month="2026-07", department="Engineering")
    assert rows(eng) == [(1, "EMP0001", 100, 2), (2, "EMP0003", 80, 2), (3, "EMP0005", 50, 2)]
    sales = get_ok(client, "/analytics/leaderboard/late", month="2026-07", department="Sales", limit=2)
    assert rows(sales) == [(1, "EMP0002", 80, 1), (2, "EMP0004", 50, 1)]
    assert get_ok(client, "/analytics/leaderboard/late", month="2026-07", department="Nope")["items"] == []


@needs_mongo
def test_leaderboard_default_limit_is_10(client, clean):
    employees = [emp(f"EMP{i:04d}") for i in range(1, 14)]
    logs = [log(f"EMP{i:04d}", "2026-07-01", late=10 + i) for i in range(1, 14)]     # 13 distinct totals
    seed(employees, logs)
    body = get_ok(client, "/analytics/leaderboard/late", month="2026-07")
    assert len(body["items"]) == 10 and [r["rank"] for r in body["items"]] == list(range(1, 11))


@needs_mongo
def test_leaderboard_all_tied_and_empty_cases(client, clean):
    seed([emp(f"EMP000{i}") for i in range(1, 5)], [log(f"EMP000{i}", "2026-07-01", late=15) for i in range(1, 5)])
    body = get_ok(client, "/analytics/leaderboard/late", month="2026-07", limit=1)
    assert [r["rank"] for r in body["items"]] == [1, 1, 1, 1] and len(body["items"]) == 4   # everyone ties at rank 1
    seed([emp("EMP0001")], [])
    assert get_ok(client, "/analytics/leaderboard/late", month="2026-07")["items"] == []
    seed([], [])
    assert get_ok(client, "/analytics/leaderboard/late", month="2026-07") == {"month": "2026-07", "items": []}


@needs_mongo
@pytest.mark.parametrize("params", [{}, {"month": "2026-7"}, {"month": "2026-07", "limit": 0}, {"month": "2026-07", "limit": 51},
                                    {"month": "2026-07", "limit": "abc"}, {"month": "2026-07", "limit": -3}])
def test_leaderboard_validation_422(client, clean, params):
    assert client.get("/analytics/leaderboard/late", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# 2d. department trend
# --------------------------------------------------------------------------- #
def trend(client, dept, frm, to):
    return client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})


@needs_mongo
def test_trend_one_row_per_day_with_weekends_gaps_and_joiners(client, clean):
    """Engineering, 2026-07-01 (Wed) .. 07-14 (Tue). E1 joined in June, E2 on 07-08, E3 on 07-10."""
    seed([emp("EMP0001", joined="2026-06-01"), emp("EMP0002", joined="2026-07-08"), emp("EMP0003", joined="2026-07-10"),
          emp("EMP0099", "Sales", "2026-01-01")], [
        log("EMP0001", "2026-07-01", "PRESENT", wh=9.0),                       # rate 1.0
        # 07-02: no record at all -> working day with rate 0.0
        log("EMP0001", "2026-07-03", "WFH", late=20, wh=3.5, half=True),       # 0.5
        log("EMP0001", "2026-07-04", "PRESENT", late=10, wh=8.0),              # Saturday: counted in present_count/late_count
        # 07-05 Sunday: nothing
        log("EMP0001", "2026-07-06", "ON_DUTY", wh=8.0),                       # rate 1.0
        log("EMP0001", "2026-07-07", "ABSENT"),                                # ABSENT record: rate 0.0
        log("EMP0001", "2026-07-08", "PRESENT", wh=8.0),                       # hc 2: E1 present, E2 absent -> 0.5
        log("EMP0002", "2026-07-08", "LEAVE"),
        log("EMP0003", "2026-07-09", "PRESENT", late=45, wh=8.0),              # E3 not yet joined (joins 10th): still counted
        log("EMP0099", "2026-07-01", "PRESENT", wh=9.0),                       # other department
    ])
    resp = trend(client, "Engineering", "2026-07-01", "2026-07-14")
    assert resp.status_code == 200
    body = resp.json()
    assert body["department"] == "Engineering" and len(body["items"]) == 14
    assert [r["date"] for r in body["items"]] == [f"2026-07-{d:02d}" for d in range(1, 15)]
    assert all(set(r) == TREND_KEYS for r in body["items"])
    by = {r["date"][-2:]: r for r in body["items"]}
    assert by["04"]["is_working_day"] is False and by["05"]["is_working_day"] is False
    assert by["11"]["is_working_day"] is False and by["12"]["is_working_day"] is False
    assert by["01"]["is_working_day"] is True
    # Every row, worked out by hand: (is_working_day, headcount, present_count, late_count, attendance_rate, moving_avg_7d)
    # The moving average is the mean of the non-null rates of this row and up to 6 rows before it (weekends are rows too).
    exp = {
        "01": (True, 1, 1, 0, 1.0, 1.0),            # window: [1]
        "02": (True, 1, 0, 0, 0.0, 0.5),            # missing weekday -> rate 0.0 (not null); [1, 0]
        "03": (True, 1, 0.5, 1, 0.5, 0.5),          # [1, 0, .5]
        "04": (False, 1, 1, 1, None, 0.5),          # Saturday: present/late counted, rate null, ignored by the window
        "05": (False, 1, 0, 0, None, 0.5),          # Sunday without records
        "06": (True, 1, 1, 0, 1.0, 0.625),          # [1, 0, .5, 1]            = 2.5 / 4
        "07": (True, 1, 0, 0, 0.0, 0.5),            # [1, 0, .5, 1, 0]         = 2.5 / 5  (ABSENT record -> 0.0)
        "08": (True, 2, 1, 0, 0.5, 0.4),            # rows 02..08: [0, .5, 1, 0, .5] = 2 / 5   (E2 joined: headcount 2)
        "09": (True, 2, 1, 1, 0.5, 0.5),            # rows 03..09: [.5, 1, 0, .5, .5] = 2.5 / 5 (E3 present before joining: counted)
        "10": (True, 3, 0, 0, 0.0, 0.4),            # rows 04..10: [1, 0, .5, .5, 0]  = 2 / 5   (E3 joined: headcount 3)
        "11": (False, 3, 0, 0, None, 0.4),
        "12": (False, 3, 0, 0, None, 0.4),
        "13": (True, 3, 0, 0, 0.0, 0.2),            # rows 07..13: [0, .5, .5, 0, 0]  = 1 / 5
        "14": (True, 3, 0, 0, 0.0, 0.2),            # rows 08..14: [.5, .5, 0, 0, 0]  = 1 / 5
    }
    for day, want in exp.items():
        r = by[day]
        got = (r["is_working_day"], r["headcount"], r["present_count"], r["late_count"], r["attendance_rate"], r["moving_avg_7d"])
        assert got == want, day


@needs_mongo
def test_trend_half_up_on_rate_and_on_moving_average(client, clean):
    """16 employees. Mon 07-06: nobody present -> rate 0.0.  Tue 07-07: one present -> 1/16 = 0.0625.
    Wed 07-08: one half day -> 0.5/16 = 0.03125 -> half-up 0.0313.
    Moving average on 07-07 = (0.0 + 0.0625) / 2 = 0.03125 -> half-up 0.0313 (banker's/float rounding gives 0.0312)."""
    employees = [emp(f"EMP{i:04d}") for i in range(1, 17)]
    seed(employees, [log("EMP0001", "2026-07-07", wh=9.0), log("EMP0002", "2026-07-08", "WFH", wh=3.0, half=True)])
    items = trend(client, "Engineering", "2026-07-06", "2026-07-08").json()["items"]
    assert [r["attendance_rate"] for r in items] == [0.0, 0.0625, 0.0313]
    assert [r["moving_avg_7d"] for r in items] == [0.0, 0.0313, 0.0313]     # last: (0 + 625 + 313) / 3 = 312.67 bp -> 0.0313


@needs_mongo
def test_trend_one_third_and_two_thirds_round_correctly(client, clean):
    seed([emp("EMP0001"), emp("EMP0002"), emp("EMP0003")],
         [log("EMP0001", "2026-07-06", wh=9.0), log("EMP0001", "2026-07-07", wh=9.0), log("EMP0002", "2026-07-07", wh=9.0)])
    items = trend(client, "Engineering", "2026-07-06", "2026-07-07").json()["items"]
    assert [r["attendance_rate"] for r in items] == [0.3333, 0.6667]
    assert [r["moving_avg_7d"] for r in items] == [0.3333, 0.5]           # (3333 + 6667) / 2 = 5000 bp


@needs_mongo
def test_trend_all_days_before_anyone_joined_gives_null_rates(client, clean):
    """Department exists (so not 404) but headcount is 0 on every requested day -> null rate, null moving average."""
    seed([emp("EMP0001", joined="2026-09-01")], [])
    items = trend(client, "Engineering", "2026-07-01", "2026-07-07").json()["items"]
    assert len(items) == 7
    assert all(r["headcount"] == 0 and r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in items)
    assert [r["is_working_day"] for r in items] == [True, True, True, False, False, True, True]


@needs_mongo
def test_trend_single_day_and_weekend_only_range(client, clean):
    seed([emp("EMP0001")], [log("EMP0001", "2026-07-04", wh=8.0, late=12)])
    one = trend(client, "Engineering", "2026-07-04", "2026-07-04").json()["items"]
    assert one == [{"date": "2026-07-04", "is_working_day": False, "headcount": 1, "present_count": 1, "late_count": 1,
                    "attendance_rate": None, "moving_avg_7d": None}]
    two = trend(client, "Engineering", "2026-07-04", "2026-07-05").json()["items"]
    assert [r["moving_avg_7d"] for r in two] == [None, None]


@needs_mongo
def test_trend_range_across_month_and_year_boundaries(client, clean):
    seed([emp("EMP0001", joined="2025-01-01")], [log("EMP0001", "2025-12-31", wh=8.0), log("EMP0001", "2026-01-01", wh=8.0)])
    items = trend(client, "Engineering", "2025-12-28", "2026-01-05").json()["items"]
    assert [r["date"] for r in items] == ["2025-12-28", "2025-12-29", "2025-12-30", "2025-12-31", "2026-01-01",
                                          "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    assert [r["is_working_day"] for r in items] == [False, True, True, True, True, True, False, False, True]
    assert [r["present_count"] for r in items] == [0, 0, 0, 1, 1, 0, 0, 0, 0]
    leap = trend(client, "Engineering", "2024-02-27", "2024-03-01").json()["items"]
    assert [r["date"] for r in leap] == ["2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01"]


@needs_mongo
def test_trend_range_limits(client, clean):
    seed([emp("EMP0001")], [])
    ok = trend(client, "Engineering", "2026-07-01", "2026-09-30")          # exactly 92 calendar days
    assert ok.status_code == 200 and len(ok.json()["items"]) == 92
    assert trend(client, "Engineering", "2026-07-01", "2026-10-01").status_code == 422    # 93 days
    assert trend(client, "Engineering", "2026-07-10", "2026-07-09").status_code == 422    # to < from


@needs_mongo
@pytest.mark.parametrize("params", [{}, {"from": "2026-07-01"}, {"to": "2026-07-05"}, {"from": "2026-7-1", "to": "2026-07-05"},
                                    {"from": "2026-02-30", "to": "2026-07-05"}, {"from": "20260701", "to": "2026-07-05"},
                                    {"from": "2026-07-01", "to": "2026-02-30"}, {"from": "2026-07-01", "to": "2026-7-5"},
                                    {"from": "2026-07-01", "to": "not-a-date"}, {"from": "", "to": ""}])
def test_trend_validation_422(client, clean, params):
    seed([emp("EMP0001")], [])
    assert client.get("/analytics/departments/Engineering/trend", params=params).status_code == 422


@needs_mongo
def test_trend_unknown_department_404_and_validation_comes_first(client, clean):
    seed([emp("EMP0001")], [])
    assert trend(client, "Nope", "2026-07-01", "2026-07-05").status_code == 404
    assert trend(client, "engineering", "2026-07-01", "2026-07-05").status_code == 404      # case-sensitive
    assert trend(client, "Nope", "2026-07-05", "2026-07-01").status_code == 422             # 422 beats 404
    seed([], [])
    assert trend(client, "Engineering", "2026-07-01", "2026-07-05").status_code == 404      # empty database


@needs_mongo
def test_trend_department_with_a_space(client, clean):
    seed([emp("EMP0001", "Human Resources")], [log("EMP0001", "2026-07-01", wh=8.0)])
    body = trend(client, "Human%20Resources", "2026-07-01", "2026-07-02")
    assert body.status_code == 200 and body.json()["department"] == "Human Resources"
    assert body.json()["items"][0]["attendance_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 3. randomised cross-check against an independent brute-force oracle
# --------------------------------------------------------------------------- #
DEPTS = ["Engineering", "Sales", "HR", "Ops"]
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def build_random_world(seed_value=20261010):
    rnd = random.Random(seed_value)
    employees = []
    for i in range(1, 41):
        joined = date(2025, 12, 1) + timedelta(days=rnd.randrange(0, 290))
        employees.append(emp(f"EMP{i:04d}", rnd.choice(DEPTS), joined.isoformat()))
    logs = []
    days = [date(2026, 6, 20) + timedelta(days=k) for k in range(0, 53)]            # 06-20 .. 08-11
    for e in employees:
        if rnd.random() < 0.2:
            continue                                                                  # employee without any logs
        for d in days:
            if rnd.random() < 0.35:
                continue                                                              # missing day
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [10, 3, 2, 2, 2])[0]
            if status in PRESENT:
                late = rnd.choice([0, 0, 0, 11, 11, 25, 25, 40, 60])
                wh = None if rnd.random() < 0.1 else round(rnd.choice([3.0, 4.49, 4.5, 7.5, 8.74, 8.75, 9.0, 9.12]) + rnd.choice([0, 0.01]), 2)
                doc = log(e["emp_code"], d.isoformat(), status, late=late, ot=rnd.choice([0, 0, 30, 45]), wh=wh,
                          half=(wh is not None and wh < 4.5))
            else:
                doc = log(e["emp_code"], d.isoformat(), status)
            for k in ("half_day", "history", "late_minutes", "overtime_minutes"):   # legacy gaps
                if rnd.random() < 0.15:
                    doc.pop(k)
            logs.append(doc)
    for d in days[:5]:
        logs.append(log("GHOST01", d.isoformat(), late=700, wh=9.0))                  # ghost employee
    return employees, logs


def q2(x):
    return float(Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP))


def q4(x):
    return float(Decimal(x).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def o_present(doc):
    """Present-day value of one record on its date: 1 / 0.5 / 0 (Mon-Fri and presence status only)."""
    if doc["status"] not in PRESENT or date.fromisoformat(doc["date"]).weekday() >= 5:
        return Decimal(0)
    return Decimal("0.5") if doc.get("half_day", False) else Decimal(1)


def o_monthly(employees, logs, code, month):
    e = next(x for x in employees if x["emp_code"] == code)
    y, m = int(month[:4]), int(month[5:])
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    start = max(first, date.fromisoformat(e["joined_on"]))
    wd = weekdays_between(start, last) if start <= last else 0
    mine = [d for d in logs if d["emp_code"] == code and first.isoformat() <= d["date"] <= last.isoformat()]
    present = sum((o_present(d) for d in mine), Decimal(0))
    return {"emp_code": code, "month": month, "working_days": wd, "present_days": float(present),
            "leave_days": sum(d["status"] == "LEAVE" for d in mine),
            "late_count": sum(d.get("late_minutes", 0) > 0 for d in mine),
            "total_late_minutes": sum(d.get("late_minutes", 0) for d in mine),
            "total_overtime_minutes": sum(d.get("overtime_minutes", 0) for d in mine),
            "attendance_pct": None if wd == 0 else q2(present * 100 / wd)}


def o_summary(employees, logs, month, department=None):
    y, m = int(month[:4]), int(month[5:])
    first, last = date(y, m, 1).isoformat(), date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    out = []
    for dept in sorted({e["department"] for e in employees}):
        if department is not None and dept != department:
            continue
        members = {e["emp_code"] for e in employees if e["department"] == dept and e["joined_on"] <= last}
        if not members:
            continue
        mine = [d for d in logs if d["emp_code"] in members and first <= d["date"] <= last]
        hours = [Decimal(str(d["work_hours"])) for d in mine if d["status"] in PRESENT and d.get("work_hours") is not None]
        out.append({"department": dept, "headcount": len(members),
                    "present_days": float(sum((o_present(d) for d in mine), Decimal(0))),
                    "avg_work_hours": q2(sum(hours) / len(hours)) if hours else None,
                    "late_count": sum(d.get("late_minutes", 0) > 0 for d in mine),
                    "total_late_minutes": sum(d.get("late_minutes", 0) for d in mine),
                    "leave_count": sum(d["status"] == "LEAVE" for d in mine),
                    "on_duty_count": sum(d["status"] == "ON_DUTY" for d in mine)})
    return out


def o_board(employees, logs, month, limit, department=None):
    y, m = int(month[:4]), int(month[5:])
    first, last = date(y, m, 1).isoformat(), date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    info = {e["emp_code"]: e for e in employees}
    totals = {}
    for d in logs:
        if first <= d["date"] <= last and d["emp_code"] in info and d.get("late_minutes", 0) > 0:
            t = totals.setdefault(d["emp_code"], [0, 0])
            t[0] += d["late_minutes"]
            t[1] += 1
    if department is not None:
        totals = {c: t for c, t in totals.items() if info[c]["department"] == department}
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rows_ = []
    for code, (total, cnt) in ordered:
        rank = 1 + sum(1 for _, (t2, _c) in ordered if t2 > total)      # 1 + number of people strictly worse
        rows_.append({"rank": rank, "emp_code": code, "name": info[code]["name"], "department": info[code]["department"],
                      "total_late_minutes": total, "late_count": cnt})
    return [r for r in rows_ if r["rank"] <= limit]


def o_trend(employees, logs, dept, frm, to):
    members = [e for e in employees if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    out, rates = [], []
    d, end = date.fromisoformat(frm), date.fromisoformat(to)
    while d <= end:
        ds = d.isoformat()
        hc = sum(e["joined_on"] <= ds for e in members)
        recs = [x for x in logs if x["emp_code"] in codes and x["date"] == ds]
        present = sum((Decimal(0) if x["status"] not in PRESENT else (Decimal("0.5") if x.get("half_day", False) else Decimal(1))
                       for x in recs), Decimal(0))
        working = d.weekday() < 5
        rate = Decimal(str(q4(present / hc))) if working and hc > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        out.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": float(present),
                    "late_count": sum(x.get("late_minutes", 0) > 0 for x in recs),
                    "attendance_rate": None if rate is None else float(rate),
                    "moving_avg_7d": None if not window else q4(sum(window) / len(window))})
        d += timedelta(days=1)
    return out


@pytest.fixture()
def random_world(client, clean):
    employees, logs = build_random_world()
    seed(employees, logs)
    return employees, logs


@needs_mongo
def test_random_world_has_the_awkward_cases_it_claims_to_have(random_world):
    employees, logs = random_world
    with_logs = {d["emp_code"] for d in logs}
    assert any(e["emp_code"] not in with_logs for e in employees)                       # employees without logs
    assert any(date.fromisoformat(d["date"]).weekday() >= 5 and d["status"] in PRESENT for d in logs)   # weekend records
    assert any("half_day" not in d for d in logs) and any("late_minutes" not in d for d in logs)         # legacy gaps
    assert any(e["joined_on"] > "2026-07-01" for e in employees)                        # mid-month / later joiners


@needs_mongo
@pytest.mark.parametrize("month", ["2026-06", "2026-07", "2026-08"])
def test_random_world_monthly_matches_oracle_for_every_employee(client, random_world, month):
    employees, logs = random_world
    for e in employees:
        got = get_ok(client, f"/analytics/employees/{e['emp_code']}/monthly", month=month)
        assert got == o_monthly(employees, logs, e["emp_code"], month), e["emp_code"]


@needs_mongo
@pytest.mark.parametrize("month", ["2026-06", "2026-07", "2026-08", "2026-12"])
def test_random_world_department_summary_matches_oracle(client, random_world, month):
    employees, logs = random_world
    assert get_ok(client, "/analytics/departments/summary", month=month)["items"] == o_summary(employees, logs, month)
    for dept in DEPTS:
        assert get_ok(client, "/analytics/departments/summary", month=month, department=dept)["items"] == \
            o_summary(employees, logs, month, dept)


@needs_mongo
@pytest.mark.parametrize("month", ["2026-06", "2026-07", "2026-08"])
def test_random_world_leaderboard_matches_oracle(client, random_world, month):
    employees, logs = random_world
    for limit in (1, 2, 3, 5, 10, 50):
        assert get_ok(client, "/analytics/leaderboard/late", month=month, limit=limit)["items"] == \
            o_board(employees, logs, month, limit), limit
        for dept in DEPTS:
            assert get_ok(client, "/analytics/leaderboard/late", month=month, limit=limit, department=dept)["items"] == \
                o_board(employees, logs, month, limit, dept), (limit, dept)


@needs_mongo
@pytest.mark.parametrize("frm,to", [("2026-06-20", "2026-07-31"), ("2026-07-01", "2026-07-01"), ("2026-06-27", "2026-07-05"),
                                    ("2026-07-26", "2026-08-11"), ("2026-05-01", "2026-06-25"), ("2026-08-01", "2026-09-15")])
def test_random_world_trend_matches_oracle(client, random_world, frm, to):
    employees, logs = random_world
    for dept in DEPTS:
        got = trend(client, dept, frm, to)
        assert got.status_code == 200
        assert got.json()["items"] == o_trend(employees, logs, dept, frm, to), dept
