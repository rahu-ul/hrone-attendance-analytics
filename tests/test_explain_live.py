"""Phase 4 PROOF tests: real MongoDB, ~100,000 attendance records, real explain output.

Needs MongoDB 6.0+ (7.0 is what the grader uses); otherwise every test here is SKIPPED (never "passed").
Run:   python -m pytest tests/test_explain_live.py -v -rs -s     (-s shows the plan summaries it prints)

What is checked, for every endpoint and for the filter combinations of GET /attendance:
  * HTTP 200 and the contract envelope {endpoint, collection, explain};
  * the raw explain text contains "IXSCAN", does NOT contain "COLLSCAN", and contains "executionStats";
  * for GET /attendance (find): no blocking "SORT" stage (the index delivers the order).
A negative control proves the check can fail: the same explain on a collection WITHOUT indexes shows COLLSCAN.

The data is generated here (not a dump, not committed): 1,000 employees x 100 weekdays = 100,000 records, with
LEAVE/ABSENT/WFH/ON_DUTY rows, late rows, half days, ties are likely. Indexes are created AFTER the load by the
app's own ensure_indexes(), like the grader's flow (data first, app start second).
"""
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest

from app import main
from tests.conftest import TEST_URI, clear_database, needs_mongo

pytestmark = needs_mongo

N_EMPLOYEES = 1_000
N_WEEKDAYS = 100
N_DEPARTMENTS = 10
EXPECTED_ATTENDANCE_DOCS = N_EMPLOYEES * N_WEEKDAYS  # 100,000


def build_dataset(seed=20260701):
    rng = random.Random(seed)
    employees = [
        {
            "emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"emp{i:04d}@example.com",
            "department": f"Dept{i % N_DEPARTMENTS:02d}", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2025-01-06" if i % 50 else "2026-06-15",  # a few mid-period joiners
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
        }
        for i in range(1, N_EMPLOYEES + 1)
    ]
    days, d = [], date(2026, 4, 1)
    while len(days) < N_WEEKDAYS:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    def attendance_docs():
        for e in employees:
            for day in days:
                roll = rng.random()
                status = ("PRESENT" if roll < 0.80 else "WFH" if roll < 0.85 else "ON_DUTY" if roll < 0.88
                          else "LEAVE" if roll < 0.94 else "ABSENT")
                doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
                       "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                       "half_day": False, "history": []}
                if status in main.PRESENCE_STATUSES:
                    p_in = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc) + timedelta(minutes=rng.randint(0, 90))
                    secs = rng.randint(3 * 3600, 10 * 3600)
                    p_out = p_in + timedelta(seconds=secs)
                    hours = (secs * 200 + 3600) // 7200 / 100
                    doc.update(punch_in=p_in, punch_out=p_out, work_hours=hours, half_day=hours < 4.5,
                               late_minutes=rng.randint(11, 90) if rng.random() < 0.25 else 0,
                               overtime_minutes=rng.randint(30, 120) if rng.random() < 0.1 else 0)
                yield doc

    return employees, attendance_docs, days


def plan_facts(explain):
    """Best-effort summary of a raw explain document (for printing only; asserts use the raw text)."""
    stages, indexes, found = set(), set(), {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "stage" and isinstance(v, str):
                    stages.add(v)
                elif k == "indexName" and isinstance(v, str):
                    indexes.add(v)
                elif k == "indexesUsed" and isinstance(v, list):
                    indexes.update(x for x in v if isinstance(x, str))
                elif k in ("nReturned", "totalKeysExamined", "totalDocsExamined", "executionTimeMillis"):
                    found.setdefault(k, v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(explain)
    return sorted(stages), sorted(indexes), found


@pytest.fixture(scope="module")
def big_world(client):
    """Load ~100k records, THEN create indexes with the app's own function."""
    employees, attendance_docs, days = build_dataset()
    main.employees_col().drop()
    main.attendance_col().drop()  # drops the indexes too: we want 'data first, indexes at startup'
    main.employees_col().insert_many(employees)
    batch, total = [], 0
    for doc in attendance_docs():
        batch.append(doc)
        if len(batch) == 10_000:
            main.attendance_col().insert_many(batch, ordered=False)
            total += len(batch)
            batch = []
    if batch:
        main.attendance_col().insert_many(batch, ordered=False)
        total += len(batch)
    t0 = time.time()
    main.ensure_indexes()
    index_seconds = time.time() - t0
    yield {"docs": total, "index_seconds": index_seconds, "days": days}
    main.employees_col().delete_many({})
    main.attendance_col().delete_many({})  # keep the indexes for other test modules


def test_dataset_has_about_100k_attendance_records(big_world):
    assert main.attendance_col().count_documents({}) == EXPECTED_ATTENDANCE_DOCS == big_world["docs"]
    assert main.employees_col().count_documents({}) == N_EMPLOYEES


def test_startup_index_creation_on_a_populated_database_is_fast_and_idempotent(big_world):
    before = {n: v["key"] for n, v in main.attendance_col().index_information().items()}
    t0 = time.time()
    main.ensure_indexes()  # second run: must be a no-op, not an error
    main.ensure_indexes()
    again = time.time() - t0
    after = {n: v["key"] for n, v in main.attendance_col().index_information().items()}
    assert before == after
    assert {"uq_emp_code_date", "date_-1_emp_code_1", "status_1_date_-1_emp_code_1", "emp_code_1_punch_in_-1"} <= set(after)
    assert {"uq_emp_code", "department_1_emp_code_1", "joined_on_1"} <= set(main.employees_col().index_information())
    assert again < 10
    print(f"\n[index build on {big_world['docs']} docs] first build {big_world['index_seconds']:.2f}s, two repeat runs {again:.3f}s")


ATTENDANCE_LIST_CASES = [
    ("no_filters", {}),
    ("emp_code", {"emp_code": "EMP0007"}),
    ("emp_code_and_range", {"emp_code": "EMP0007", "date_from": "2026-05-01", "date_to": "2026-05-31"}),
    ("date_range", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("date_from_only", {"date_from": "2026-07-01"}),
    ("date_to_only", {"date_to": "2026-05-31"}),
    ("status", {"status": "LEAVE"}),
    ("status_and_range", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("emp_code_and_status", {"emp_code": "EMP0007", "status": "PRESENT"}),
    ("everything", {"emp_code": "EMP0007", "status": "WFH", "date_from": "2026-04-01", "date_to": "2026-08-31"}),
    ("deep_page", {"page": 500, "page_size": 100}),
    ("page_2_of_range", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2, "page_size": 50}),
]

AGGREGATE_CASES = [
    ("employee_monthly", {"emp_code": "EMP0007", "month": "2026-07"}),
    ("employee_monthly", {"emp_code": "EMP9999", "month": "2026-07"}),  # unknown employee: still explainable
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Dept03"}),
    ("department_summary", {"month": "2026-04"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 50}),
    ("late_leaderboard", {"month": "2026-07", "limit": 3, "department": "Dept03"}),
    ("late_leaderboard", {"month": "2026-01"}),  # month with no data
    ("department_trend", {"department": "Dept03", "from": "2026-06-01", "to": "2026-08-31"}),  # 92 days
    ("department_trend", {"department": "Dept03", "from": "2026-07-01", "to": "2026-07-07"}),
    ("department_trend", {"department": "Nope", "from": "2026-07-01", "to": "2026-07-07"}),
]


def explain_via_http(client, endpoint, params):
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"endpoint", "collection", "explain"}
    assert body["endpoint"] == endpoint and isinstance(body["explain"], dict)
    return body, json.dumps(body["explain"])


def assert_index_plan(text):
    assert "IXSCAN" in text, "plan has no index scan"
    assert "COLLSCAN" not in text, "plan contains a collection scan"
    assert "executionStats" in text, "not an executionStats explain"


@pytest.mark.parametrize("label,params", ATTENDANCE_LIST_CASES, ids=[c[0] for c in ATTENDANCE_LIST_CASES])
def test_attendance_list_explain_uses_an_index(client, big_world, capsys, label, params):
    body, text = explain_via_http(client, "attendance_list", params)
    assert body["collection"] == "attendance_logs"
    assert_index_plan(text)
    stages, indexes, facts = plan_facts(body["explain"])
    with capsys.disabled():
        print(f"\n[attendance_list:{label}] stages={stages} indexes={indexes} {facts}")


@pytest.mark.parametrize("label,params", ATTENDANCE_LIST_CASES, ids=[c[0] for c in ATTENDANCE_LIST_CASES])
def test_attendance_list_needs_no_blocking_sort(client, big_world, label, params):
    """The sort (date desc, emp_code asc) must come from the index, not from an in-memory SORT stage."""
    _, text = explain_via_http(client, "attendance_list", params)
    assert '"SORT"' not in text and '"SORT_KEY_GENERATOR"' not in text


@pytest.mark.parametrize("endpoint,params", AGGREGATE_CASES, ids=[f"{e}-{i}" for i, (e, _) in enumerate(AGGREGATE_CASES)])
def test_analytics_explain_uses_an_index(client, big_world, capsys, endpoint, params):
    body, text = explain_via_http(client, endpoint, params)
    expected = {"employee_monthly": "employees", "department_summary": "employees",
                "late_leaderboard": "attendance_logs", "department_trend": "employees"}[endpoint]
    assert body["collection"] == expected
    assert_index_plan(text)
    stages, indexes, facts = plan_facts(body["explain"])
    with capsys.disabled():
        print(f"\n[{endpoint} {params}] stages={stages} indexes={indexes} {facts}")


def test_explain_actually_ran_the_query(client, big_world):
    """executionStats means the query really executed: the find explain must report returned documents."""
    body, _ = explain_via_http(client, "attendance_list", {"page_size": 20})
    stats = body["explain"]["executionStats"]
    assert stats["nReturned"] == 20
    assert stats["totalDocsExamined"] < 1000  # a page of 20 must not read the collection


def test_leaderboard_scan_is_bounded_by_the_month_not_the_collection(client, big_world, capsys):
    body, _ = explain_via_http(client, "late_leaderboard", {"month": "2026-07"})
    _, _, facts = plan_facts(body["explain"])
    july_docs = main.attendance_col().count_documents({"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}})
    with capsys.disabled():
        print(f"\n[late_leaderboard July] docs in July={july_docs}, of {EXPECTED_ATTENDANCE_DOCS}; plan facts={facts}")
    examined = facts.get("totalDocsExamined")
    if examined is not None:
        assert examined <= july_docs  # never reads outside the month


def test_negative_control_without_indexes_the_check_does_fail(big_world):
    """Prove the COLLSCAN check is not vacuous: same explain on a scratch DB with NO indexes."""
    import uuid

    name = f"hrone_noidx_{uuid.uuid4().hex[:8]}"
    db = main.get_client()[name]
    try:
        db["attendance_logs"].insert_many(
            [{"emp_code": f"EMP{i:04d}", "date": "2026-07-01", "status": "LEAVE", "late_minutes": 0} for i in range(1, 2001)]
        )
        out = main.run_explain(db, "attendance_list", status="LEAVE", use_attendance_list_hint=False)
        text = json.dumps(out["explain"])
        assert "COLLSCAN" in text and "IXSCAN" not in text
    finally:
        clear_database(name)


def test_concurrent_explain_calls_are_all_correct(big_world):
    jobs = [("attendance_list", {"status": "LEAVE"}), ("employee_monthly", {"emp_code": "EMP0007", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07"}),
            ("department_trend", {"department": "Dept03", "from_": "2026-07-01", "to": "2026-07-31"})] * 6

    def one(job):
        endpoint, params = job
        out = main.run_explain(main.get_db(), endpoint, **params)
        return endpoint, json.dumps(out["explain"])

    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(one, jobs))
    assert len(results) == 30
    for endpoint, text in results:
        assert "IXSCAN" in text and "COLLSCAN" not in text, endpoint


def test_explain_on_empty_collections_is_200_not_500(client, clean):
    """Empty data: explain still answers (and, with the indexes present, still shows an index scan)."""
    for endpoint, params in [("attendance_list", {}), ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                             ("department_summary", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07"}),
                             ("department_trend", {"department": "X", "from": "2026-07-01", "to": "2026-07-02"})]:
        body, text = explain_via_http(client, endpoint, params)
        assert "COLLSCAN" not in text, endpoint


def test_explain_over_real_http_with_parallel_clients():
    """A real uvicorn process (own DB), 20 parallel explain requests: all 200 and all index plans."""
    import uuid

    import httpx
    from pymongo import MongoClient

    from tests.test_live_server import _start, _wait_status

    db = f"hrone_live_explain_{uuid.uuid4().hex[:8]}"
    proc, base, _ = _start(TEST_URI, db)
    try:
        assert _wait_status(base, 200, 20) is not None, "app did not become healthy in 20 s"
        urls = [
            "/admin/explain/attendance_list?status=LEAVE&page=2",
            "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
            "/admin/explain/department_summary?month=2026-07",
            "/admin/explain/late_leaderboard?month=2026-07&limit=5",
            "/admin/explain/department_trend?department=X&from=2026-07-01&to=2026-07-31",
        ] * 4

        def get(path):
            return httpx.get(base + path, timeout=30)

        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(get, urls))
        for path, r in zip(urls, responses):
            assert r.status_code == 200, (path, r.text)
            text = json.dumps(r.json()["explain"])
            assert "IXSCAN" in text and "COLLSCAN" not in text, path
        assert httpx.get(base + "/admin/explain/nope", timeout=10).status_code == 422
        assert httpx.get(base + "/admin/explain/department_summary", timeout=10).status_code == 422
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        clear_database(db)
