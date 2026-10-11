"""Phase 4 tests for GET /admin/explain/{endpoint} that need NO MongoDB.

What these tests prove: we send MongoDB the right command (same filter / sort / skip / limit / pipeline as
the real endpoint, verbosity executionStats), we validate input the way openapi.yaml says, and we wrap
MongoDB's reply in {endpoint, collection, explain} without changing it.

What they do NOT prove: anything about real query plans. A "recording" database object is used here only
to see which command we send; its canned reply is meaningless. Plans are checked in test_explain_live.py,
which needs a real MongoDB and ~100k documents.

Layers: (1) pure tests of build_explain_request / explain_json_safe / the index declarations (no HTTP);
(2) HTTP tests through TestClient with the recording database (they need FastAPI installed, not MongoDB).
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.exceptions import RequestValidationError

from app import main

ALL_ENDPOINTS = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def error_locs(exc_info):
    return [tuple(e["loc"]) for e in exc_info.value.errors()]


# --------------------------------------------------------------------------- #
# 1. pure tests
# --------------------------------------------------------------------------- #
def test_the_source_never_hard_codes_an_index_scan_claim():
    """The explain output must come from MongoDB. The literal plan names must not appear in the app."""
    source = open(main.__file__, encoding="utf-8").read()
    assert "IXSCAN" not in source
    assert "COLLSCAN" not in source


def test_attendance_list_default_command():
    collection, cmd = main.build_explain_request("attendance_list")
    assert collection == "attendance_logs"
    assert list(cmd) == ["explain", "verbosity"]  # "explain" must be the FIRST key (it names the command)
    assert cmd["verbosity"] == "executionStats"
    find = cmd["explain"]
    assert find["find"] == "attendance_logs"
    assert find["filter"] == {}
    assert list(find["sort"].items()) == [("date", -1), ("emp_code", 1)]
    assert find["projection"] == {"_id": 0}
    assert find["limit"] == 20
    assert "skip" not in find


@pytest.mark.parametrize(
    "page,page_size,skip,limit",
    [(1, 20, None, 20), (2, 20, 20, 20), (3, 5, 10, 5), (10, 100, 900, 100), (1, 1, None, 1)],
)
def test_attendance_list_pagination_is_part_of_the_explained_query(page, page_size, skip, limit):
    _, cmd = main.build_explain_request("attendance_list", page=page, page_size=page_size)
    find = cmd["explain"]
    assert find.get("skip") == skip
    assert find["limit"] == limit


def test_attendance_list_absurd_page_is_capped_not_rejected():
    _, cmd = main.build_explain_request("attendance_list", page=10**30, page_size=100)
    assert cmd["explain"]["skip"] == main.MAX_SKIP  # fits BSON int64, so MongoDB will not refuse it


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"emp_code": "EMP0001"},
        {"date_from": "2026-07-01"},
        {"date_to": "2026-07-31"},
        {"date_from": "2026-07-01", "date_to": "2026-07-31"},
        {"status": "LEAVE"},
        {"emp_code": "EMP0001", "status": "PRESENT"},
        {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "WFH"},
        {"emp_code": ""},  # real endpoint: empty string is an exact filter that matches nothing
    ],
)
def test_attendance_list_uses_exactly_the_real_endpoints_filter_and_sort(params):
    query, sort = main.attendance_list_plan(
        params.get("emp_code"), params.get("date_from"), params.get("date_to"), params.get("status")
    )
    _, cmd = main.build_explain_request("attendance_list", **params)
    assert cmd["explain"]["filter"] == query
    assert list(cmd["explain"]["sort"].items()) == sort


def test_attendance_list_with_fixed_emp_code_sorts_by_date_only():
    _, cmd = main.build_explain_request("attendance_list", emp_code="EMP0001")
    assert list(cmd["explain"]["sort"].items()) == [("date", -1)]


@pytest.mark.parametrize(
    "params,hint",
    [
        ({}, "date_-1_emp_code_1"),
        ({"status": "LEAVE"}, "status_1_date_-1_emp_code_1"),
        ({"emp_code": "EMP0001"}, "uq_emp_code_date"),
        ({"emp_code": "EMP0001", "status": "PRESENT"}, "emp_code_1_status_1_date_-1"),
    ],
)
def test_attendance_list_explain_uses_the_ordered_index_for_each_filter_shape(params, hint):
    _, cmd = main.build_explain_request("attendance_list", **params)
    assert cmd["explain"]["hint"] == hint


def test_attendance_list_date_range_is_inclusive():
    _, cmd = main.build_explain_request("attendance_list", date_from="2026-07-01", date_to="2026-07-31")
    assert cmd["explain"]["filter"] == {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}


def test_attendance_list_from_after_to_is_422():
    with pytest.raises(RequestValidationError) as e:
        main.build_explain_request("attendance_list", date_from="2026-07-31", date_to="2026-07-01")
    assert error_locs(e) == [("query", "date_from")]


def test_attendance_list_same_day_range_is_fine():
    main.build_explain_request("attendance_list", date_from="2026-07-01", date_to="2026-07-01")


def test_employee_monthly_uses_the_real_pipeline():
    collection, cmd = main.build_explain_request("employee_monthly", emp_code="EMP0001", month="2026-07")
    assert collection == "employees"
    agg = cmd["explain"]
    assert agg["aggregate"] == "employees" and agg["cursor"] == {}
    assert agg["pipeline"] == main.employee_monthly_pipeline("EMP0001", "2026-07")
    assert cmd["verbosity"] == "executionStats"


@pytest.mark.parametrize("department", [None, "Engineering"])
def test_department_summary_uses_the_real_pipeline(department):
    collection, cmd = main.build_explain_request("department_summary", month="2026-07", department=department)
    assert collection == "employees"
    assert cmd["explain"]["pipeline"] == main.department_summary_pipeline("2026-07", department)


@pytest.mark.parametrize("limit,department", [(10, None), (3, None), (50, "Engineering")])
def test_late_leaderboard_uses_the_real_pipeline_with_limit_and_department(limit, department):
    collection, cmd = main.build_explain_request("late_leaderboard", month="2026-07", limit=limit, department=department)
    assert collection == "attendance_logs"
    assert cmd["explain"]["aggregate"] == "attendance_logs"
    assert cmd["explain"]["pipeline"] == main.late_leaderboard_pipeline("2026-07", limit, department)


def test_late_leaderboard_default_limit_is_10():
    _, cmd = main.build_explain_request("late_leaderboard", month="2026-07")
    assert {"$match": {"rank": {"$lte": 10}}} in cmd["explain"]["pipeline"]


def test_department_trend_uses_the_real_pipeline():
    collection, cmd = main.build_explain_request(
        "department_trend", department="Engineering", from_="2026-07-01", to="2026-07-31"
    )
    assert collection == "employees"
    assert cmd["explain"]["pipeline"] == main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-31")


def test_parameters_an_endpoint_does_not_use_are_ignored():
    base = main.build_explain_request("employee_monthly", emp_code="EMP0001", month="2026-07")
    noisy = main.build_explain_request(
        "employee_monthly", emp_code="EMP0001", month="2026-07",
        department="Ops", limit=5, status="LEAVE", page=9, page_size=7, date_from="2026-01-01",
    )
    assert base == noisy


@pytest.mark.parametrize(
    "endpoint,params,missing",
    [
        ("employee_monthly", {"month": "2026-07"}, "emp_code"),
        ("employee_monthly", {"emp_code": "EMP0001"}, "month"),
        ("employee_monthly", {}, "emp_code"),
        ("department_summary", {}, "month"),
        ("department_summary", {"department": "Engineering"}, "month"),
        ("late_leaderboard", {}, "month"),
        ("late_leaderboard", {"limit": 5}, "month"),
        ("department_trend", {"from_": "2026-07-01", "to": "2026-07-31"}, "department"),
        ("department_trend", {"department": "Engineering", "to": "2026-07-31"}, "from"),
        ("department_trend", {"department": "Engineering", "from_": "2026-07-01"}, "to"),
        ("employee_monthly", {"emp_code": "", "month": "2026-07"}, "emp_code"),
        ("department_trend", {"department": "", "from_": "2026-07-01", "to": "2026-07-31"}, "department"),
    ],
)
def test_missing_or_empty_required_parameter_is_422(endpoint, params, missing):
    with pytest.raises(RequestValidationError) as e:
        main.build_explain_request(endpoint, **params)
    assert error_locs(e)[0] == ("query", missing)


@pytest.mark.parametrize(
    "from_,to",
    [("2026-07-31", "2026-07-01"), ("2026-01-01", "2026-04-04")],  # to < from ; 94 days
)
def test_trend_range_rules_apply_to_explain(from_, to):
    with pytest.raises(RequestValidationError) as e:
        main.build_explain_request("department_trend", department="Engineering", from_=from_, to=to)
    assert error_locs(e) == [("query", "to")]


def test_trend_range_of_exactly_92_days_is_allowed_and_93_is_not():
    main.build_explain_request("department_trend", department="E", from_="2026-07-01", to="2026-09-30")  # 92 days
    with pytest.raises(RequestValidationError):
        main.build_explain_request("department_trend", department="E", from_="2026-07-01", to="2026-10-01")  # 93


@pytest.mark.parametrize("bad", ["", "Attendance_List", "attendance-list", "analytics", "employees", "health", "drop"])
def test_unknown_endpoint_identifier_is_422_for_direct_callers(bad):
    with pytest.raises(RequestValidationError) as e:
        main.build_explain_request(bad)
    assert error_locs(e) == [("path", "endpoint")]


def test_the_five_identifiers_are_exactly_the_contracts_enum():
    assert list(main.ExplainEndpoint.__args__) == ALL_ENDPOINTS


class RecordingDb:
    """Stand-in for a database object. Remembers every command; the reply is canned and MEANINGLESS as an
    explain result. It is here only to test what we send and how we wrap the answer."""

    def __init__(self, reply=None):
        self.commands = []
        self.reply = reply if reply is not None else {"ok": 1.0, "canned": True}

    def command(self, cmd):
        self.commands.append(cmd)
        return self.reply


def test_run_explain_returns_raw_reply_unchanged_inside_the_envelope():
    reply = {"ok": 1.0, "queryPlanner": {"namespace": "x.attendance_logs"}, "executionStats": {"nReturned": 3}}
    db = RecordingDb(reply)
    out = main.run_explain(db, "attendance_list", status="LEAVE")
    assert set(out) == {"endpoint", "collection", "explain"}
    assert out["endpoint"] == "attendance_list"
    assert out["collection"] == "attendance_logs"
    assert out["explain"] == reply
    assert len(db.commands) == 1 and db.commands[0]["verbosity"] == "executionStats"


def test_run_explain_validates_before_touching_the_database():
    db = RecordingDb()
    with pytest.raises(RequestValidationError):
        main.run_explain(db, "department_summary")
    assert db.commands == []


def test_explain_json_safe_handles_bson_types():
    from bson import Int64, ObjectId, Timestamp

    raw = {
        "ok": 1.0,
        "id": ObjectId("65f0000000000000000000aa"),
        "when": datetime(2026, 7, 1, 4, 30, tzinfo=timezone.utc),
        "big": Int64(2**40),
        "ts": Timestamp(1, 2),
        "nested": {"list": [1, {"x": datetime(2026, 1, 1, tzinfo=timezone.utc)}]},
    }
    out = main.explain_json_safe(raw)
    json.dumps(out, allow_nan=False)  # plain JSON, no BSON objects left
    assert out["id"] == {"$oid": "65f0000000000000000000aa"}
    assert "$date" in out["when"]
    assert out["big"] in (2**40, {"$numberLong": str(2**40)})
    assert out["ok"] == 1.0


# ---- index declarations: every endpoint's first stage has an index to start from ---- #
def _keys(index_model):
    return list(index_model.document["key"].items())


def _has_index_with_prefix(indexes, prefix):
    return any(_keys(ix)[: len(prefix)] == prefix for ix in indexes)


def test_index_names_are_unique_and_explicit():
    for group in (main.EMPLOYEE_INDEXES, main.ATTENDANCE_INDEXES):
        names = [ix.document["name"] for ix in group]
        assert len(names) == len(set(names))  # explicit names -> same spec on restart is a no-op


@pytest.mark.parametrize(
    "indexes,prefix,why",
    [
        (main.EMPLOYEE_INDEXES, [("emp_code", 1)], "employee_monthly first stage; leaderboard employee join"),
        (main.EMPLOYEE_INDEXES, [("department", 1)], "department_trend / department_summary department filter"),
        (main.EMPLOYEE_INDEXES, [("joined_on", 1)], "department_summary without a department"),
        (main.ATTENDANCE_INDEXES, [("emp_code", 1), ("date", 1)], "every per-employee $lookup, GET /attendance?emp_code="),
        (main.ATTENDANCE_INDEXES, [("date", -1), ("emp_code", 1)], "late_leaderboard month range; GET /attendance sort"),
        (main.ATTENDANCE_INDEXES, [("status", 1), ("date", -1), ("emp_code", 1)], "GET /attendance?status="),
    ],
)
def test_needed_index_is_declared(indexes, prefix, why):
    assert _has_index_with_prefix(indexes, prefix), why


def test_unique_indexes_that_guard_races_are_unique():
    by_name = {ix.document["name"]: ix.document for ix in main.EMPLOYEE_INDEXES + main.ATTENDANCE_INDEXES}
    assert by_name["uq_emp_code"].get("unique") is True
    assert by_name["uq_emp_code_date"].get("unique") is True


# ---- analytics pipelines tolerate missing fields and empty data (shape checks, no database) ---- #
def _dump(pipeline):
    return json.dumps(pipeline, default=str)


def test_group_stage_treats_missing_stored_values_as_zero_or_false():
    text = _dump(main._month_log_totals_group())
    assert '"$ifNull": ["$late_minutes", 0]' in text
    assert '"$ifNull": ["$overtime_minutes", 0]' in text
    assert '"$isNumber": "$work_hours"' in text  # missing/null work_hours never enters the average
    assert '"$eq": ["$half_day", true]' in text  # missing half_day is "not a half day"


def test_pipelines_do_not_depend_on_data_being_present():
    # No $limit/$skip/$sample/$out/$merge, nothing that would change the answer or write data.
    for pipeline in (
        main.employee_monthly_pipeline("EMP0001", "2026-07"),
        main.department_summary_pipeline("2026-07", None),
        main.late_leaderboard_pipeline("2026-07", 10, None),
        main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-31"),
    ):
        stages = {k for st in pipeline for k in st}
        assert not stages & {"$out", "$merge", "$limit", "$skip", "$sample"}


# --------------------------------------------------------------------------- #
# 2. HTTP tests (FastAPI needed, MongoDB not needed: the database object is replaced by RecordingDb)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def recording_db(monkeypatch):
    db = RecordingDb()
    monkeypatch.setattr(main, "get_db", lambda: db)
    main.app.dependency_overrides[main.require_indexes] = lambda: None  # do not need MongoDB for index creation
    yield db
    main.app.dependency_overrides.pop(main.require_indexes, None)


EXPLAIN_CASES = [
    ("attendance_list", {}, "attendance_logs"),
    ("attendance_list", {"status": "LEAVE", "page": 2, "page_size": 5}, "attendance_logs"),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "employees"),
    ("department_summary", {"month": "2026-07"}, "employees"),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}, "employees"),
    ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
    ("late_leaderboard", {"month": "2026-07", "limit": 3, "department": "Engineering"}, "attendance_logs"),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}, "employees"),
]


@pytest.mark.parametrize("endpoint,params,collection", EXPLAIN_CASES)
def test_http_response_matches_the_contract_shape(client, recording_db, endpoint, params, collection):
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"endpoint", "collection", "explain"}
    assert body["endpoint"] == endpoint
    assert body["collection"] == collection
    assert isinstance(body["explain"], dict)
    assert body["explain"] == {"ok": 1.0, "canned": True}  # MongoDB's reply, passed through untouched
    cmd = recording_db.commands[-1]
    assert list(cmd)[0] == "explain" and cmd["verbosity"] == "executionStats"


def test_http_pagination_reaches_the_explained_find(client, recording_db):
    r = client.get("/admin/explain/attendance_list", params={"page": 3, "page_size": 5, "emp_code": "EMP0001"})
    assert r.status_code == 200
    find = recording_db.commands[-1]["explain"]
    assert find["skip"] == 10 and find["limit"] == 5
    assert find["filter"] == {"emp_code": "EMP0001"}
    assert list(find["sort"].items()) == [("date", -1)]


def test_http_trend_uses_the_from_query_name(client, recording_db):
    r = client.get("/admin/explain/department_trend",
                   params={"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"})
    assert r.status_code == 200
    pipeline = recording_db.commands[-1]["explain"]["pipeline"]
    assert pipeline == main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")


def test_http_explain_does_not_404_for_unknown_employee_or_department(client, recording_db):
    assert client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP9999", "month": "2026-07"}).status_code == 200
    assert client.get("/admin/explain/department_trend",
                      params={"department": "Nope", "from": "2026-07-01", "to": "2026-07-02"}).status_code == 200


@pytest.mark.parametrize("bad", ["nope", "Attendance_List", "attendance-list", "employees", "attendance_list%20", "0"])
def test_http_unknown_endpoint_is_422(client, recording_db, bad):
    r = client.get(f"/admin/explain/{bad}")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert recording_db.commands == []


def test_http_post_is_not_allowed(client, recording_db):
    assert client.post("/admin/explain/attendance_list").status_code == 405


INVALID_REQUESTS = [
    ("employee_monthly", {"month": "2026-07"}),                                   # missing emp_code
    ("employee_monthly", {"emp_code": "EMP0001"}),                                # missing month
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-13"}),            # bad month
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-7"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "0000-01"}),
    ("department_summary", {}),
    ("department_summary", {"month": "july"}),
    ("late_leaderboard", {}),
    ("late_leaderboard", {"month": "2026-07", "limit": 0}),
    ("late_leaderboard", {"month": "2026-07", "limit": 51}),
    ("late_leaderboard", {"month": "2026-07", "limit": "abc"}),
    ("department_trend", {"from": "2026-07-01", "to": "2026-07-31"}),             # missing department
    ("department_trend", {"department": "E", "to": "2026-07-31"}),                # missing from
    ("department_trend", {"department": "E", "from": "2026-07-01"}),              # missing to
    ("department_trend", {"department": "E", "from": "2026-07-31", "to": "2026-07-01"}),
    ("department_trend", {"department": "E", "from": "2026-01-01", "to": "2026-04-04"}),
    ("department_trend", {"department": "E", "from": "2026-02-30", "to": "2026-03-01"}),
    ("attendance_list", {"status": "BOGUS"}),
    ("attendance_list", {"page": 0}),
    ("attendance_list", {"page": -1}),
    ("attendance_list", {"page_size": 0}),
    ("attendance_list", {"page_size": 101}),
    ("attendance_list", {"date_from": "garbage"}),
    ("attendance_list", {"date_to": "2026-02-30"}),
    ("attendance_list", {"date_from": "2026-07-31", "date_to": "2026-07-01"}),
    # declared parameters are type-checked even when this endpoint does not use them
    ("attendance_list", {"month": "2026-13"}),
    ("attendance_list", {"limit": 99}),
]


@pytest.mark.parametrize("endpoint,params", INVALID_REQUESTS)
def test_http_invalid_requests_are_422_and_never_reach_the_database(client, recording_db, endpoint, params):
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], list) and r.json()["detail"]
    assert recording_db.commands == []


def test_http_unknown_extra_parameter_is_ignored(client, recording_db):
    r = client.get("/admin/explain/department_summary", params={"month": "2026-07", "colour": "red"})
    assert r.status_code == 200


def test_http_explain_reply_with_bson_types_is_still_valid_json(client, recording_db):
    from bson import ObjectId

    recording_db.reply = {"ok": 1.0, "oid": ObjectId(), "when": datetime(2026, 7, 1, tzinfo=timezone.utc)}
    r = client.get("/admin/explain/attendance_list")
    assert r.status_code == 200
    assert "$oid" in r.json()["explain"]["oid"] and "$date" in r.json()["explain"]["when"]
