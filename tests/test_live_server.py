"""Tests against a REAL uvicorn process with parallel HTTP clients (needs MongoDB; else skipped)."""
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.conftest import ROOT, TEST_URI, clear_database, ist_ms, needs_mongo


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(uri, db):
    port = _free_port()
    env = {**os.environ, "MONGO_URI": uri, "MONGO_DB": db}
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}", t0


def _wait_status(base, want, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(base + "/health", timeout=8)
            if r.status_code == want:
                return r
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return None


@pytest.fixture(scope="module")
def live():
    from pymongo import MongoClient
    db = f"hrone_live_{uuid.uuid4().hex[:8]}"
    proc, base, t0 = _start(TEST_URI, db)
    r = _wait_status(base, 200, 20)
    ready_in = time.time() - t0
    try:
        assert r is not None, "app did not answer /health 200 within 20 s"
        yield {"base": base, "ready_in": ready_in, "db": db}
    finally:
        proc.terminate(); proc.wait(timeout=10)
        clear_database(db)


def _fire(n, fn):
    """Run fn(i) in n threads released at the same instant."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(run, range(n)))


@needs_mongo
def test_ready_quickly_and_indexes_created_at_startup(live):
    from pymongo import MongoClient
    assert live["ready_in"] < 20
    info = MongoClient(TEST_URI)[live["db"]]["attendance_logs"].index_information()
    assert "uq_emp_code_date" in info


@needs_mongo
def test_concurrent_employee_create_one_201_rest_409(live):
    body = {"emp_code": "EMP7001", "name": "Race", "email": "r@example.com", "department": "Eng", "joined_on": "2026-01-05"}
    codes = _fire(24, lambda i: httpx.post(live["base"] + "/employees", json=body, timeout=20).status_code)
    assert sorted(codes) == [201] + [409] * 23
    assert httpx.get(live["base"] + "/employees", params={"department": "Eng"}).json()["total"] >= 1


@needs_mongo
def test_concurrent_punch_in_same_date_one_201_rest_409(live):
    httpx.post(live["base"] + "/employees", json={"emp_code": "EMP7002", "name": "R", "email": "r@example.com",
                                                   "department": "Ops", "joined_on": "2026-01-05"})
    t = ist_ms(2026, 7, 8, 9, 0)
    # identical instants AND slightly different instants on the same attendance date
    codes = _fire(24, lambda i: httpx.post(live["base"] + "/attendance/punch-in",
                                           json={"emp_code": "EMP7002", "punched_at": t + (i % 3) * 1000}, timeout=20).status_code)
    assert sorted(codes) == [201] + [409] * 23
    r = httpx.get(live["base"] + "/attendance", params={"emp_code": "EMP7002"}).json()
    assert r["total"] == 1


@needs_mongo
def test_concurrent_punch_in_different_employees_all_succeed(live):
    for i in range(10):
        httpx.post(live["base"] + "/employees", json={"emp_code": f"EMP71{i:02d}", "name": "R", "email": "r@example.com",
                                                       "department": "Ops", "joined_on": "2026-01-05"})
    codes = _fire(10, lambda i: httpx.post(live["base"] + "/attendance/punch-in",
                                           json={"emp_code": f"EMP71{i:02d}", "punched_at": ist_ms(2026, 7, 9, 9, 0)}, timeout=20).status_code)
    assert codes == [201] * 10


@needs_mongo
def test_503_when_mongo_unreachable_and_app_still_starts():
    proc, base, _ = _start("mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=500", "x")
    try:
        r = _wait_status(base, 503, 25)
        assert r is not None, "expected /health to answer 503 (not crash, not 200)"
        assert httpx.get(base + "/employees", timeout=20).status_code == 503
    finally:
        proc.terminate(); proc.wait(timeout=10)


# ----------------------------- Phase 2: concurrency on attendance operations -----------------------------
def _mk_emp(base, code, **kw):
    body = {"emp_code": code, "name": "R", "email": "r@example.com", "department": "Ops", "joined_on": "2026-01-05", **kw}
    assert httpx.post(base + "/employees", json=body, timeout=20).status_code == 201


@needs_mongo
def test_concurrent_punch_out_exactly_one_200_rest_409(live):
    _mk_emp(live["base"], "EMP7201")
    t = ist_ms(2026, 7, 10, 9, 0)
    assert httpx.post(live["base"] + "/attendance/punch-in", json={"emp_code": "EMP7201", "punched_at": t}).status_code == 201
    # identical instants and different instants: still exactly one winner
    codes = _fire(24, lambda i: httpx.post(live["base"] + "/attendance/punch-out",
                                           json={"emp_code": "EMP7201", "punched_at": t + 9 * 3600_000 + (i % 4) * 1000},
                                           timeout=20).status_code)
    assert sorted(codes) == [200] + [409] * 23
    rec = httpx.get(live["base"] + "/attendance", params={"emp_code": "EMP7201"}).json()["items"][0]
    assert rec["punch_out"] is not None and rec["history"] == []


@needs_mongo
def test_concurrent_punch_out_for_different_employees_all_succeed(live):
    for i in range(10):
        # EMP7201 belongs to the preceding test in this module-scoped live DB;
        # start this independent range at 10 so every setup record is unique.
        emp_code = f"EMP72{i + 10:02d}"
        _mk_emp(live["base"], emp_code)
        httpx.post(live["base"] + "/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ist_ms(2026, 7, 11, 9, 0)})
    codes = _fire(10, lambda i: httpx.post(live["base"] + "/attendance/punch-out",
                                           json={"emp_code": f"EMP72{i + 10:02d}", "punched_at": ist_ms(2026, 7, 11, 18, 0)}, timeout=20).status_code)
    assert codes == [200] * 10


@needs_mongo
def test_concurrent_corrections_never_lose_history(live):
    _mk_emp(live["base"], "EMP7301")
    base = ist_ms(2026, 7, 12, 9, 30)
    httpx.post(live["base"] + "/attendance/punch-in", json={"emp_code": "EMP7301", "punched_at": base})
    n = 20
    # every request moves punch_in to a different instant, so every one of them is a real change
    codes = _fire(n, lambda i: httpx.patch(live["base"] + "/attendance/EMP7301/2026-07-12",
                                           json={"reason": f"fix number {i}", "regularized_by": f"user{i}",
                                                 "punch_in": base - (i + 1) * 60_000}, timeout=30).status_code)
    assert set(codes) <= {200, 409}, codes
    assert codes.count(200) >= 1
    rec = httpx.get(live["base"] + "/attendance", params={"emp_code": "EMP7301"}).json()["items"][0]
    hist = rec["history"]
    assert len(hist) == codes.count(200)                                   # one entry per success, none lost
    assert len({h["by"] for h in hist}) == len(hist)                       # no entry duplicated
    for a, b in zip(hist, hist[1:]):                                       # entries form an unbroken chain
        assert b["changes"]["punch_in"]["from"] == a["changes"]["punch_in"]["to"]
    assert rec["punch_in"] == hist[-1]["changes"]["punch_in"]["to"]


@needs_mongo
def test_concurrent_identical_corrections_one_wins_no_5xx(live):
    _mk_emp(live["base"], "EMP7302")
    base = ist_ms(2026, 7, 13, 9, 30)
    httpx.post(live["base"] + "/attendance/punch-in", json={"emp_code": "EMP7302", "punched_at": base})
    codes = _fire(16, lambda i: httpx.patch(live["base"] + "/attendance/EMP7302/2026-07-13",
                                            json={"reason": "same fix", "regularized_by": "hr", "punch_in": base - 600_000},
                                            timeout=30).status_code)
    assert codes.count(200) == 1 and set(codes) <= {200, 409, 422}, codes
    rec = httpx.get(live["base"] + "/attendance", params={"emp_code": "EMP7302"}).json()["items"][0]
    assert len(rec["history"]) == 1


@needs_mongo
def test_punch_out_racing_a_correction_never_writes_stale_numbers(live):
    _mk_emp(live["base"], "EMP7303")
    base = ist_ms(2026, 7, 14, 10, 0)
    httpx.post(live["base"] + "/attendance/punch-in", json={"emp_code": "EMP7303", "punched_at": base})
    out_at = ist_ms(2026, 7, 14, 18, 0)

    def go(i):
        if i % 2 == 0:
            return ("out", httpx.post(live["base"] + "/attendance/punch-out", json={"emp_code": "EMP7303", "punched_at": out_at}, timeout=30).status_code)
        return ("fix", httpx.patch(live["base"] + "/attendance/EMP7303/2026-07-14",
                                   json={"reason": "earlier start", "regularized_by": "hr", "punch_in": ist_ms(2026, 7, 14, 9, 0)}, timeout=30).status_code)

    res = _fire(8, go)
    assert all(code < 500 for _, code in res), res
    rec = httpx.get(live["base"] + "/attendance", params={"emp_code": "EMP7303"}).json()["items"][0]
    # whatever the interleaving, stored numbers must agree with the stored times (no stale derived fields)
    assert rec["punch_out"] == out_at
    assert rec["work_hours"] == round((rec["punch_out"] - rec["punch_in"]) / 3_600_000, 2)
