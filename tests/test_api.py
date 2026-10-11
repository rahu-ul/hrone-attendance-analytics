"""API tests for the Phase 1 endpoints. Need a reachable MongoDB; skipped otherwise."""
import pytest

from app import main
from tests.conftest import ist_ms, make_emp, needs_mongo

pytestmark = needs_mongo


# ---------------- /health ----------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_503_when_mongo_down(monkeypatch):
    from fastapi.testclient import TestClient
    from pymongo.errors import ServerSelectionTimeoutError

    class Boom:
        def command(self, *a, **k):
            raise ServerSelectionTimeoutError("down")

    monkeypatch.setattr(main, "get_db", lambda: Boom())
    r = TestClient(main.app).get("/health")  # no lifespan: nothing else touches Mongo
    assert r.status_code == 503 and "detail" in r.json()


def test_health_503_when_not_configured(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_client", None)
    monkeypatch.delenv("MONGO_URI")
    assert TestClient(main.app).get("/health").status_code == 503


def test_endpoint_503_not_500_when_mongo_down(monkeypatch):
    from fastapi.testclient import TestClient
    from pymongo.errors import AutoReconnect

    class Col:
        def count_documents(self, *a, **k):
            raise AutoReconnect("gone")

    monkeypatch.setattr(main, "employees_col", lambda: Col())
    assert TestClient(main.app).get("/employees").status_code == 503


# ---------------- POST /employees ----------------
def test_create_employee_ok_defaults_and_shape(client, clean):
    r = make_emp(client)
    assert r.status_code == 201
    b = r.json()
    assert set(b) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert b["shift_start"] == "09:30" and b["shift_end"] == "18:30"
    assert isinstance(b["created_at"], int) and b["created_at"] > main.EPOCH_MS_MIN
    stored = main.employees_col().find_one({"emp_code": "EMP0001"})
    assert stored["created_at"].tzinfo is not None           # stored as aware BSON datetime
    assert main.dt_to_ms(stored["created_at"]) == b["created_at"]


def test_create_employee_ignores_client_created_at_and_extras(client, clean):
    r = make_emp(client, created_at=5, _id="x", bogus=1)
    assert r.status_code == 201 and r.json()["created_at"] != 5
    assert "_id" not in r.json() and "bogus" not in r.json()


def test_create_employee_duplicate_409(client, clean):
    assert make_emp(client).status_code == 201
    assert make_emp(client, name="Other").status_code == 409


@pytest.mark.parametrize("override", [
    {"emp_code": "EMP001"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"}, {"emp_code": "EMP0001\n"},
    {"emp_code": "EMP٠٠٠١"},  # non-ASCII digits
    {"name": ""}, {"name": "x" * 101}, {"email": "nope"}, {"email": "a@b"}, {"email": "a b@c.d"},
    {"department": ""}, {"department": "d" * 51},
    {"shift_start": "24:00"}, {"shift_start": "9:30"}, {"shift_end": "18:60"},
    {"shift_start": "09:30", "shift_end": "09:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"joined_on": "2026-1-5"}, {"joined_on": 20260105},
    {"name": 123},
])
def test_create_employee_422(client, clean, override):
    assert make_emp(client, **override).status_code == 422


def test_create_employee_missing_fields_and_non_object(client, clean):
    assert client.post("/employees", json={"emp_code": "EMP0001"}).status_code == 422
    assert client.post("/employees", json=[1, 2]).status_code == 422
    assert client.post("/employees", content="not json", headers={"content-type": "application/json"}).status_code == 422


def test_create_employee_overnight_allowed(client, clean):
    assert make_emp(client, shift_start="22:00", shift_end="06:00").status_code == 201


# ---------------- GET /employees ----------------
def _seed_employees(client):
    for i in range(1, 8):
        make_emp(client, f"EMP{i:04d}", department="Ops" if i % 2 == 0 else "Engineering")


def test_list_employees_sorted_paginated_and_filtered_total(client, clean):
    _seed_employees(client)
    r = client.get("/employees", params={"page": 1, "page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003"]
    assert (r["total"], r["page"], r["page_size"]) == (7, 1, 3)
    r = client.get("/employees", params={"page": 3, "page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0007"]
    r = client.get("/employees", params={"department": "Ops"}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0002", "EMP0004", "EMP0006"]
    r = client.get("/employees", params={"department": "Ops", "page_size": 2, "page": 2}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0006"]


def test_list_employees_department_exact_case_sensitive(client, clean):
    _seed_employees(client)
    assert client.get("/employees", params={"department": "ops"}).json()["total"] == 0
    assert client.get("/employees", params={"department": ""}).json()["total"] == 0


def test_list_employees_defaults_and_beyond_last_page(client, clean):
    _seed_employees(client)
    r = client.get("/employees").json()
    assert r["page"] == 1 and r["page_size"] == 20 and len(r["items"]) == 7
    r = client.get("/employees", params={"page": 99}).json()
    assert r["items"] == [] and r["total"] == 7
    assert client.get("/employees", params={"page": 10**30}).status_code == 200  # no 500 on huge skip


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}, {"page_size": 1.5}])
def test_list_employees_422(client, clean, params):
    assert client.get("/employees", params=params).status_code == 422


def test_list_employees_page_size_100_ok(client, clean):
    assert client.get("/employees", params={"page_size": 100}).status_code == 200


# ---------------- POST /attendance/punch-in ----------------
def punch(client, code="EMP0001", **kw):
    return client.post("/attendance/punch-in", json={"emp_code": code, **kw})


def test_punch_in_basic_late_and_defaults(client, clean):
    make_emp(client)
    r = punch(client, punched_at=ist_ms(2026, 7, 6, 10, 15, 59))
    assert r.status_code == 201
    b = r.json()
    assert b == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                 "punch_in": ist_ms(2026, 7, 6, 10, 15, 59), "punch_out": None, "work_hours": None,
                 "late_minutes": 45, "overtime_minutes": 0, "half_day": False, "history": []}
    stored = main.attendance_col().find_one({"emp_code": "EMP0001"})
    assert stored["punch_in"].tzinfo is not None and stored["history"] == [] and stored["punch_out"] is None


@pytest.mark.parametrize("h,mi,s,want", [(9, 40, 0, 0), (9, 40, 1, 10), (9, 29, 0, 0), (10, 15, 59, 45)])
def test_punch_in_late_rule(client, clean, h, mi, s, want):
    make_emp(client)
    assert punch(client, punched_at=ist_ms(2026, 7, 6, h, mi, s)).json()["late_minutes"] == want


def test_punch_in_truncates_to_whole_seconds_stored_and_returned(client, clean):
    make_emp(client)
    ms = ist_ms(2026, 7, 6, 9, 40, 0, 900)  # 09:40:00.900
    b = punch(client, punched_at=ms).json()
    assert b["punch_in"] == ms - 900 and b["late_minutes"] == 0
    assert main.attendance_col().find_one({})["punch_in"].microsecond == 0
    # one second later (09:40:01.100) IS late
    make_emp(client, "EMP0002")
    assert punch(client, "EMP0002", punched_at=ist_ms(2026, 7, 6, 9, 40, 1, 100)).json()["late_minutes"] == 10


def test_punch_in_date_is_ist_date(client, clean):
    make_emp(client)
    # 01:30 IST on Jul 7 == 20:00Z on Jul 6
    assert punch(client, punched_at=ist_ms(2026, 7, 7, 1, 30)).json()["date"] == "2026-07-07"


def test_punch_in_overnight_rule(client, clean):
    make_emp(client, shift_start="22:00", shift_end="06:00")
    b = punch(client, punched_at=ist_ms(2026, 7, 7, 3, 0)).json()   # before shift_end -> previous day
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 300
    make_emp(client, "EMP0002", shift_start="22:00", shift_end="06:00")
    b = punch(client, "EMP0002", punched_at=ist_ms(2026, 7, 6, 21, 55)).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 0
    # same attendance date as the 21:55 punch -> conflict
    assert punch(client, "EMP0002", punched_at=ist_ms(2026, 7, 7, 2, 0)).status_code == 409


def test_punch_in_status_variants(client, clean):
    make_emp(client)
    for i, st in enumerate(["WFH", "ON_DUTY", "PRESENT"]):
        assert punch(client, status=st, punched_at=ist_ms(2026, 7, 6 + i, 9, 0)).json()["status"] == st


@pytest.mark.parametrize("st", ["ABSENT", "LEAVE", "present", "BOGUS", "", None, 1])
def test_punch_in_bad_status_422(client, clean, st):
    make_emp(client)
    assert punch(client, status=st, punched_at=ist_ms(2026, 7, 6, 9, 0)).status_code == 422


def test_punch_in_unknown_employee_404(client, clean):
    assert punch(client, "EMP9999").status_code == 404


def test_punch_in_duplicate_409_and_other_employee_ok(client, clean):
    make_emp(client); make_emp(client, "EMP0002")
    t = ist_ms(2026, 7, 6, 9, 0)
    assert punch(client, punched_at=t).status_code == 201
    assert punch(client, punched_at=t + 60000).status_code == 409   # same date
    assert punch(client, "EMP0002", punched_at=t).status_code == 201
    assert punch(client, punched_at=ist_ms(2026, 7, 7, 9, 0)).status_code == 201
    assert main.attendance_col().count_documents({"emp_code": "EMP0001"}) == 2


@pytest.mark.parametrize("bad", [1783312500, 99_999_999_999, 4_102_444_800_001, 1.5, 1783312500000.0, "1783312500000",
                                 True, None, 0, -5, [1], {}])
def test_punch_in_bad_punched_at_422(client, clean, bad):
    make_emp(client)
    assert punch(client, punched_at=bad).status_code == 422


def test_punch_in_punched_at_boundaries_ok(client, clean):
    make_emp(client)
    assert punch(client, punched_at=main.EPOCH_MS_MIN).status_code == 201
    assert punch(client, punched_at=main.EPOCH_MS_MAX).status_code == 201


def test_punch_in_default_now_and_ignores_derived_fields(client, clean):
    make_emp(client)
    r = punch(client, late_minutes=999, work_hours=9, half_day=True, history=[1])
    assert r.status_code == 201
    b = r.json()
    assert b["late_minutes"] >= 0 and b["late_minutes"] != 999 and b["work_hours"] is None and b["half_day"] is False
    assert b["history"] == [] and b["punch_in"] % 1000 == 0  # whole seconds


def test_punch_in_response_has_no_id(client, clean):
    make_emp(client)
    b = punch(client, punched_at=ist_ms(2026, 7, 6, 9, 0)).json()
    assert "id" not in b and "_id" not in b


def test_punch_in_missing_emp_code_422(client, clean):
    assert client.post("/attendance/punch-in", json={}).status_code == 422


# ---------------- GET /attendance ----------------
def _insert_logs(rows):
    from datetime import datetime, timezone
    docs = []
    for code, date, status in rows:
        d = {"emp_code": code, "date": date, "status": status, "late_minutes": 0, "overtime_minutes": 0,
             "work_hours": None, "half_day": False, "history": [], "punch_out": None,
             "punch_in": datetime.fromisoformat(date + "T04:00:00+00:00") if status in main.PRESENCE_STATUSES else None}
        docs.append(d)
    main.attendance_col().insert_many(docs)


def test_attendance_sort_date_desc_then_emp_code_asc(client, clean):
    _insert_logs([("EMP0003", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "PRESENT"),
                  ("EMP0002", "2026-07-07", "WFH"), ("EMP0002", "2026-07-06", "LEAVE")])
    items = client.get("/attendance").json()["items"]
    assert [(i["date"], i["emp_code"]) for i in items] == [
        ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"), ("2026-07-06", "EMP0003")]


def test_attendance_filters_total_and_pagination(client, clean):
    rows = [(f"EMP{i:04d}", f"2026-07-{d:02d}", "PRESENT" if (i + d) % 3 else "LEAVE")
            for i in range(1, 6) for d in range(1, 11)]
    _insert_logs(rows)
    r = client.get("/attendance", params={"emp_code": "EMP0002"}).json()
    assert r["total"] == 10 and [i["date"] for i in r["items"]] == sorted((i["date"] for i in r["items"]), reverse=True)
    r = client.get("/attendance", params={"date_from": "2026-07-03", "date_to": "2026-07-05"}).json()
    assert r["total"] == 15 and {i["date"] for i in r["items"]} <= {"2026-07-03", "2026-07-04", "2026-07-05"}
    r = client.get("/attendance", params={"date_from": "2026-07-10"}).json()      # inclusive lower bound
    assert r["total"] == 5 and {i["date"] for i in r["items"]} == {"2026-07-10"}
    r = client.get("/attendance", params={"date_to": "2026-07-01"}).json()        # inclusive upper bound
    assert r["total"] == 5
    r = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert r["total"] == sum(1 for x in rows if x[2] == "LEAVE") and all(i["status"] == "LEAVE" for i in r["items"])
    r = client.get("/attendance", params={"emp_code": "EMP0003", "status": "LEAVE", "date_from": "2026-07-02", "date_to": "2026-07-09"}).json()
    want = [x for x in rows if x[0] == "EMP0003" and x[2] == "LEAVE" and "2026-07-02" <= x[1] <= "2026-07-09"]
    assert r["total"] == len(want) and len(r["items"]) == len(want)
    # pagination walk covers everything exactly once, in order
    seen, page = [], 1
    while True:
        r = client.get("/attendance", params={"page": page, "page_size": 7}).json()
        assert r["total"] == 50
        if not r["items"]:
            break
        seen += [(i["date"], i["emp_code"]) for i in r["items"]]
        page += 1
    assert len(seen) == 50 == len(set(seen))
    assert seen == sorted(seen, key=lambda t: (-int(t[0].replace("-", "")), t[1]))


def test_attendance_unknown_emp_empty_not_error(client, clean):
    r = client.get("/attendance", params={"emp_code": "NOPE"})
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0


def test_attendance_same_day_range(client, clean):
    _insert_logs([("EMP0001", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-07", "PRESENT")])
    r = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06"}).json()
    assert r["total"] == 1


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "garbage"}, {"date_to": "2026-13-01"},
    {"date_from": "20260706"}, {"date_from": "2026-02-30"}, {"status": "BOGUS"}, {"status": "present"},
    {"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": -3}])
def test_attendance_422(client, clean, params):
    r = client.get("/attendance", params=params)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_attendance_legacy_and_history_serialization(client, clean):
    from datetime import datetime, timezone
    u = lambda s: datetime.fromisoformat(s)
    main.attendance_col().insert_many([
        {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",           # legacy: no history, no half_day
         "punch_in": u("2026-07-14T04:02:00+00:00"), "punch_out": u("2026-07-14T13:01:00+00:00"),
         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": u("2026-07-07T03:58:00+00:00"),
         "punch_out": u("2026-07-07T13:00:00+00:00"), "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0,
         "half_day": False, "history": [{"at": u("2026-07-08T05:00:00+00:00"), "by": "hr.admin", "reason": "biometric glitch",
          "changes": {"punch_in": {"from": u("2026-07-07T04:35:00+00:00"), "to": u("2026-07-07T03:58:00+00:00")},
                      "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}}]},
        {"emp_code": "EMP0004", "date": "2026-07-08", "status": "ABSENT", "punch_in": None, "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0005", "date": "2026-07-09", "status": "LEAVE"},           # punch fields missing entirely
    ])
    items = {i["emp_code"]: i for i in client.get("/attendance").json()["items"]}
    legacy = items["EMP0002"]
    assert legacy["history"] == [] and legacy["half_day"] is False
    assert legacy["punch_in"] == main.dt_to_ms(__import__("datetime").datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc))
    h = items["EMP0001"]["history"][0]
    assert h["at"] == 1783486800000 and h["by"] == "hr.admin"
    assert h["changes"]["punch_in"] == {"from": 1783398900000, "to": 1783396680000}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert items["EMP0004"]["punch_in"] is None and items["EMP0004"]["work_hours"] is None
    missing = items["EMP0005"]
    assert missing["punch_in"] is None and missing["late_minutes"] == 0 and missing["history"] == []
    assert all(set(i) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
                          "overtime_minutes", "half_day", "history"} for i in items.values())


def test_indexes_exist_with_expected_keys(client):
    e = main.employees_col().index_information()
    a = main.attendance_col().index_information()
    assert e["uq_emp_code"]["unique"] and e["uq_emp_code"]["key"] == [("emp_code", 1)]
    assert e["department_1_emp_code_1"]["key"] == [("department", 1), ("emp_code", 1)]
    assert a["uq_emp_code_date"]["unique"] and a["uq_emp_code_date"]["key"] == [("emp_code", 1), ("date", 1)]
    assert a["date_-1_emp_code_1"]["key"] == [("date", -1), ("emp_code", 1)]
    assert a["status_1_date_-1_emp_code_1"]["key"] == [("status", 1), ("date", -1), ("emp_code", 1)]


def test_ensure_indexes_is_idempotent(client):
    """Real MongoDB: re-running createIndexes with identical specs is a no-op.
    The FerretDB v1.24 stand-in used in the sandbox PANICS on that call (server bug, connection
    closed), so in that case this test is skipped instead of reported as a pass."""
    from pymongo.errors import ConnectionFailure
    try:
        main.ensure_indexes()
        main.ensure_indexes()
    except ConnectionFailure:
        pytest.skip("server dropped the connection on repeat createIndexes (FerretDB v1.24 bug); verify on real MongoDB")
    assert set(main.attendance_col().index_information()) >= {"uq_emp_code_date", "date_-1_emp_code_1"}


