"""API tests for POST /attendance/punch-out and PATCH /attendance/{emp_code}/{date}.
Need a reachable MongoDB (TEST_MONGO_URI); skipped otherwise. Concurrency tests live in test_live_server.py."""
from datetime import timedelta

import pytest

from app import main
from tests.conftest import ist_ms, make_emp, needs_mongo

pytestmark = needs_mongo
H = 3600_000
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def pin(client, code="EMP0001", at=None, **kw):
    body = {"emp_code": code, **kw}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-in", json=body)


def pout(client, code="EMP0001", at=None, **kw):
    body = {"emp_code": code, **kw}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-out", json=body)


def patch(client, code, date, reason="biometric glitch", by="hr.admin", **kw):
    return client.patch(f"/attendance/{code}/{date}", json={"reason": reason, "regularized_by": by, **kw})


KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}


# ======================= punch-out =======================
def test_punch_out_normal_day(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 28))
    r = pout(client, at=ist_ms(2026, 7, 6, 18, 35))
    assert r.status_code == 200
    b = r.json()
    assert set(b) == KEYS and "id" not in b and "_id" not in b
    assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["late_minutes"], b["history"]) == (9.12, 0, False, 0, [])
    assert b["punch_out"] == ist_ms(2026, 7, 6, 18, 35)
    stored = main.attendance_col().find_one({"emp_code": "EMP0001"})
    assert stored["punch_out"].tzinfo is not None and stored["history"] == []


def test_punch_out_overnight_across_midnight(client, clean):
    make_emp(client, "EMP0005", **NIGHT)
    assert pin(client, "EMP0005", ist_ms(2026, 7, 6, 21, 55)).json()["date"] == "2026-07-06"
    b = pout(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40)).json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"], b["half_day"]) == ("2026-07-06", 8.75, 40, False)


def test_punch_out_overnight_early_morning_punch_in_belongs_to_previous_day(client, clean):
    make_emp(client, "EMP0005", **NIGHT)
    assert pin(client, "EMP0005", ist_ms(2026, 7, 7, 3, 0)).json()["date"] == "2026-07-06"
    b = pout(client, "EMP0005", ist_ms(2026, 7, 7, 8, 0)).json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"]) == ("2026-07-06", 5.0, 120)


@pytest.mark.parametrize("secs,wh,half", [
    (12600, 3.5, True), (16164, 4.49, True), (16181, 4.49, True), (16182, 4.5, False), (16200, 4.5, False), (54, 0.02, True),
])
def test_punch_out_half_day_uses_rounded_hours(client, clean, secs, wh, half):
    make_emp(client)
    t = ist_ms(2026, 7, 6, 9, 30)
    pin(client, at=t)
    b = pout(client, at=t + secs * 1000).json()
    assert (b["work_hours"], b["half_day"]) == (wh, half)


@pytest.mark.parametrize("extra_s,want", [(0, 0), (29 * 60, 0), (29 * 60 + 59, 0), (30 * 60, 30), (30 * 60 + 59, 30), (31 * 60, 31)])
def test_punch_out_overtime_threshold(client, clean, extra_s, want):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 30))
    b = pout(client, at=ist_ms(2026, 7, 6, 18, 30) + extra_s * 1000).json()
    assert b["overtime_minutes"] == want


def test_punch_out_truncates_to_whole_seconds(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 0) + 900)
    b = pout(client, at=ist_ms(2026, 7, 6, 18, 0) + 999).json()
    assert b["punch_in"] % 1000 == 0 and b["punch_out"] % 1000 == 0
    assert b["punch_out"] == ist_ms(2026, 7, 6, 18, 0)


def test_punch_out_keeps_late_minutes_status_and_writes_no_history(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 10, 5), status="WFH")
    b = pout(client, at=ist_ms(2026, 7, 6, 19, 10)).json()
    assert (b["late_minutes"], b["status"], b["overtime_minutes"], b["work_hours"], b["history"]) == (35, "WFH", 40, 9.08, [])


def test_punch_out_duplicate_409_and_record_unchanged(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 0))
    assert pout(client, at=ist_ms(2026, 7, 6, 18, 0)).status_code == 200
    before = main.attendance_col().find_one({"emp_code": "EMP0001"})
    assert pout(client, at=ist_ms(2026, 7, 6, 19, 0)).status_code == 409
    assert main.attendance_col().find_one({"emp_code": "EMP0001"}) == before


def test_punch_out_unknown_employee_404(client, clean):
    assert pout(client, "EMP9999", ist_ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_no_attendance_record_404(client, clean):
    make_emp(client)
    assert pout(client, at=ist_ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_before_punch_in_is_422(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 0))
    assert pout(client, at=ist_ms(2026, 7, 6, 8, 0)).status_code == 422


def test_punch_out_absent_and_leave_records_are_not_matched(client, clean):
    make_emp(client)
    main.attendance_col().insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT", "punch_in": None, "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE"},  # punch fields missing entirely
    ])
    assert pout(client, at=ist_ms(2026, 7, 8, 18, 0)).status_code == 404


@pytest.mark.parametrize("offset_ms,want", [(0, 422), (-1000, 422), (1000, 200), (24 * H, 200), (24 * H + 1000, 422)])
def test_punch_out_time_window(client, clean, offset_ms, want):
    make_emp(client)
    t = ist_ms(2026, 7, 6, 9, 0)
    pin(client, at=t)
    r = pout(client, at=t + offset_ms)
    assert r.status_code == want
    if want == 422:
        assert isinstance(r.json()["detail"], list)
        assert main.attendance_col().find_one({"emp_code": "EMP0001"})["punch_out"] is None


@pytest.mark.parametrize("bad", [1783312500, 99_999_999_999, 4_102_444_800_001, 1.5, 1783312500000.0, "1783312500000", None, True, [], {}])
def test_punch_out_bad_punched_at_422(client, clean, bad):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 0))
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad})
    assert r.status_code == 422
    assert main.attendance_col().find_one({"emp_code": "EMP0001"})["punch_out"] is None


def test_punch_out_missing_emp_code_or_bad_body_422(client, clean):
    assert client.post("/attendance/punch-out", json={}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": 5}).status_code == 422
    assert client.post("/attendance/punch-out", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_punch_out_defaults_to_now_and_ignores_client_derived_fields(client, clean, monkeypatch):
    make_emp(client)
    # Keep this default-time behavior test independent of the wall clock: overtime is based
    # on the shift end, not on elapsed work time.
    fixed_now = main.ms_to_dt(ist_ms(2026, 7, 6, 10, 0))
    monkeypatch.setattr(main, "now_utc", lambda: fixed_now)
    now_ms = main.dt_to_ms(fixed_now)
    pin(client, at=now_ms - 3600_000)
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "work_hours": 99, "half_day": True, "overtime_minutes": 500})
    assert r.status_code == 200
    b = r.json()
    assert 0.99 <= b["work_hours"] <= 1.02 and b["half_day"] is True and b["overtime_minutes"] == 0  # 1 h worked -> real half day


def test_punch_out_picks_the_most_recent_record(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 6, 9, 0))
    pin(client, at=ist_ms(2026, 7, 7, 9, 0))
    assert pout(client, at=ist_ms(2026, 7, 7, 18, 0)).status_code == 200
    assert pout(client, at=ist_ms(2026, 7, 7, 19, 0)).status_code == 409                      # latest is closed -> 409
    assert main.attendance_col().find_one({"emp_code": "EMP0001", "date": "2026-07-06"})["punch_out"] is None
    assert pout(client, at=ist_ms(2026, 7, 6, 18, 0)).json()["date"] == "2026-07-06"           # earlier instant -> earlier record


def test_punch_out_legacy_open_record_without_extra_fields(client, clean):
    make_emp(client)
    main.attendance_col().insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                      "punch_in": main.ms_to_dt(ist_ms(2026, 7, 14, 9, 32)), "late_minutes": 0})
    b = pout(client, at=ist_ms(2026, 7, 14, 18, 33)).json()
    assert b["work_hours"] == 9.02 and b["history"] == [] and b["half_day"] is False


# ======================= PATCH =======================
def _punched_day(client, code="EMP0001", day=(2026, 7, 7), pin_at=(10, 5), pout_at=(18, 30)):
    y, mo, d = day
    pin(client, code, ist_ms(y, mo, d, *pin_at))
    pout(client, code, ist_ms(y, mo, d, *pout_at))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def test_patch_punch_in_correction_matches_data_model_sample_8(client, clean):
    make_emp(client)
    date = _punched_day(client)
    r = patch(client, "EMP0001", date, punch_in=ist_ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert set(b) == KEYS
    assert (b["late_minutes"], b["work_hours"], b["punch_in"]) == (0, 9.03, ist_ms(2026, 7, 7, 9, 28))
    assert len(b["history"]) == 1
    e = b["history"][0]
    assert set(e) == {"at", "by", "reason", "changes"} and e["by"] == "hr.admin" and e["reason"] == "biometric glitch"
    assert isinstance(e["at"], int) and e["at"] > main.EPOCH_MS_MIN
    assert set(e["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert e["changes"]["punch_in"] == {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)}
    assert e["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert e["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    stored = main.attendance_col().find_one({"emp_code": "EMP0001"})["history"][0]       # BSON dates inside history
    assert stored["at"].tzinfo is not None and stored["changes"]["punch_in"]["from"].tzinfo is not None


def test_patch_keeps_old_history_and_appends_exactly_one(client, clean):
    make_emp(client)
    date = _punched_day(client)
    r1 = patch(client, "EMP0001", date, reason="first fix", punch_in=ist_ms(2026, 7, 7, 9, 28)).json()
    r2 = patch(client, "EMP0001", date, reason="second fix", by="boss", punch_out=ist_ms(2026, 7, 7, 19, 0)).json()
    assert len(r2["history"]) == 2 and r2["history"][0] == r1["history"][0]
    assert r2["history"][1]["by"] == "boss" and set(r2["history"][1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    assert r2["overtime_minutes"] == 30


@pytest.mark.parametrize("kw", [
    {}, {"status": "PRESENT"}, {"punch_in": ist_ms(2026, 7, 7, 10, 5)}, {"punch_in": ist_ms(2026, 7, 7, 10, 5) + 900},
    {"status": "PRESENT", "punch_in": ist_ms(2026, 7, 7, 10, 5), "punch_out": ist_ms(2026, 7, 7, 18, 30)},
])
def test_patch_unchanged_is_422_and_writes_nothing(client, clean, kw):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP0001", date, **kw).status_code == 422
    assert main.attendance_col().find_one({"emp_code": "EMP0001"})["history"] == []


def test_patch_absent_and_leave_clear_times(client, clean):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP0001", date, status="ABSENT", punch_in=ist_ms(2026, 7, 7, 9, 0)).status_code == 422
    assert patch(client, "EMP0001", date, status="LEAVE", punch_out=ist_ms(2026, 7, 7, 19, 0)).status_code == 422
    assert main.attendance_col().find_one({"emp_code": "EMP0001"})["history"] == []
    b = patch(client, "EMP0001", date, status="LEAVE").json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == \
           ("LEAVE", None, None, None, 0, 0, False)
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_patch_absent_record_cannot_get_punch_time_without_presence_status(client, clean):
    make_emp(client)
    main.attendance_col().insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT", "punch_in": None,
                                      "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                                      "half_day": False, "history": []})
    assert patch(client, "EMP0001", "2026-07-08", punch_in=ist_ms(2026, 7, 8, 9, 0)).status_code == 422
    assert patch(client, "EMP0001", "2026-07-08", status="PRESENT").status_code == 422          # needs a punch_in
    b = patch(client, "EMP0001", "2026-07-08", status="PRESENT", punch_in=ist_ms(2026, 7, 8, 9, 50)).json()
    assert (b["status"], b["late_minutes"], b["work_hours"], b["punch_out"]) == ("PRESENT", 20, None, None)


def test_patch_punch_in_must_stay_on_attendance_date(client, clean):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP0001", date, punch_in=ist_ms(2026, 7, 8, 9, 0)).status_code == 422
    assert patch(client, "EMP0001", date, punch_in=ist_ms(2026, 7, 6, 23, 59)).status_code == 422


def test_patch_overnight_record_may_cross_midnight(client, clean):
    make_emp(client, "EMP0005", **NIGHT)
    pin(client, "EMP0005", ist_ms(2026, 7, 6, 21, 55))
    pout(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40))
    b = patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 3, 0), punch_out=ist_ms(2026, 7, 7, 8, 0)).json()
    assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"]) == (300, 5.0, 120)
    assert patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 6, 0)).status_code == 422


@pytest.mark.parametrize("out", [ist_ms(2026, 7, 7, 10, 5), ist_ms(2026, 7, 7, 9, 0), ist_ms(2026, 7, 8, 10, 5) + 1000])
def test_patch_punch_out_must_be_after_in_and_within_24h(client, clean, out):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP0001", date, punch_out=out).status_code == 422


def test_patch_exactly_24h_ok(client, clean):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP0001", date, punch_out=ist_ms(2026, 7, 8, 10, 5)).status_code == 200


def test_patch_sets_punch_out_on_open_record(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 7, 9, 30))
    b = patch(client, "EMP0001", "2026-07-07", punch_out=ist_ms(2026, 7, 7, 13, 0)).json()
    assert (b["work_hours"], b["half_day"], b["overtime_minutes"]) == (3.5, True, 0)
    assert set(b["history"][0]["changes"]) == {"punch_out", "work_hours", "half_day"}


def test_patch_404s(client, clean):
    make_emp(client)
    date = _punched_day(client)
    assert patch(client, "EMP9999", date, status="WFH").status_code == 404
    assert patch(client, "EMP0001", "2026-07-09", status="WFH").status_code == 404


@pytest.mark.parametrize("body", [
    {"reason": "abcd", "regularized_by": "x", "status": "WFH"},                  # reason too short
    {"reason": "a" * 201, "regularized_by": "x", "status": "WFH"},
    {"reason": "valid reason", "regularized_by": "", "status": "WFH"},
    {"reason": "valid reason", "regularized_by": "x" * 51, "status": "WFH"},
    {"regularized_by": "x", "status": "WFH"},                                    # reason missing
    {"reason": "valid reason", "status": "WFH"},                                 # regularized_by missing
    {"reason": "valid reason", "regularized_by": "x", "status": "NOPE"},
    {"reason": "valid reason", "regularized_by": "x", "punch_in": 1783312500},  # seconds
    {"reason": "valid reason", "regularized_by": "x", "punch_in": 1.5},
    {"reason": "valid reason", "regularized_by": "x", "punch_in": "1783312500000"},
    {"reason": "valid reason", "regularized_by": "x", "punch_in": None},
    {"reason": "valid reason", "regularized_by": "x", "punch_out": None},
    {"reason": "valid reason", "regularized_by": "x", "status": None},
])
def test_patch_body_validation_422(client, clean, body):
    make_emp(client)
    date = _punched_day(client)
    assert client.patch(f"/attendance/EMP0001/{date}", json=body).status_code == 422
    assert main.attendance_col().find_one({"emp_code": "EMP0001"})["history"] == []


@pytest.mark.parametrize("bad_date", ["2026-13-01", "2026-02-30", "20260707", "2026-7-7", "garbage"])
def test_patch_bad_date_in_path_422(client, clean, bad_date):
    make_emp(client)
    assert patch(client, "EMP0001", bad_date, status="WFH").status_code == 422


def test_patch_ignores_derived_fields_in_body(client, clean):
    make_emp(client)
    date = _punched_day(client)
    r = client.patch(f"/attendance/EMP0001/{date}", json={
        "reason": "valid reason", "regularized_by": "hr", "status": "WFH", "work_hours": 1, "late_minutes": 99,
        "overtime_minutes": 99, "half_day": True, "history": [], "date": "2000-01-01", "emp_code": "EMP0002"})
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["half_day"], b["date"], b["emp_code"]) == (8.42, 35, False, date, "EMP0001")
    assert set(b["history"][0]["changes"]) == {"status"} and len(b["history"]) == 1


def test_patch_legacy_record_without_history_or_half_day(client, clean):
    make_emp(client)
    main.attendance_col().insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                      "punch_in": main.ms_to_dt(ist_ms(2026, 7, 14, 9, 32)),
                                      "punch_out": main.ms_to_dt(ist_ms(2026, 7, 14, 18, 33)),
                                      "work_hours": 9.02, "late_minutes": 0, "overtime_minutes": 0})
    b = patch(client, "EMP0001", "2026-07-14", status="WFH").json()
    assert len(b["history"]) == 1 and list(b["history"][0]["changes"]) == ["status"] and b["half_day"] is False


def test_patch_record_whose_history_is_an_explicit_null(client, clean):
    """Regression: $push onto a null field is a MongoDB error (-> 500). history: null must behave like []."""
    make_emp(client)
    main.attendance_col().insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                      "punch_in": main.ms_to_dt(ist_ms(2026, 7, 14, 9, 32)),
                                      "punch_out": main.ms_to_dt(ist_ms(2026, 7, 14, 18, 33)),
                                      "work_hours": 9.02, "late_minutes": 0, "overtime_minutes": 0, "history": None})
    r = patch(client, "EMP0001", "2026-07-14", status="WFH")
    assert r.status_code == 200 and len(r.json()["history"]) == 1
    r = patch(client, "EMP0001", "2026-07-14", status="ON_DUTY")  # now a real array: appended, not replaced
    assert r.status_code == 200 and [h["changes"]["status"]["to"] for h in r.json()["history"]] == ["WFH", "ON_DUTY"]


def test_punch_in_and_out_never_write_history_but_patch_does(client, clean):
    make_emp(client)
    pin(client, at=ist_ms(2026, 7, 7, 9, 0))
    pout(client, at=ist_ms(2026, 7, 7, 18, 0))
    assert main.attendance_col().find_one({"emp_code": "EMP0001"})["history"] == []
    patch(client, "EMP0001", "2026-07-07", status="WFH")
    assert len(main.attendance_col().find_one({"emp_code": "EMP0001"})["history"]) == 1


def test_patch_then_get_attendance_round_trip(client, clean):
    make_emp(client)
    date = _punched_day(client)
    patch(client, "EMP0001", date, punch_in=ist_ms(2026, 7, 7, 9, 28))
    item = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert item["history"][0]["changes"]["punch_in"]["to"] == ist_ms(2026, 7, 7, 9, 28)


def test_new_punch_out_index_exists(client):
    a = main.attendance_col().index_information()
    assert a["emp_code_1_punch_in_-1"]["key"] == [("emp_code", 1), ("punch_in", -1)]
