"""Phase 2 pure-logic tests (no database): plan_regularization, record_version_filter, record_state."""
from datetime import datetime, timedelta, timezone

import pytest

from app import main

IST, UTC = main.IST, timezone.utc
DAY_EMP = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_EMP = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


def rec(**o):
    d = {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": ist(2026, 7, 7, 10, 5),
         "punch_out": ist(2026, 7, 7, 18, 30), "work_hours": 8.42, "late_minutes": 35, "overtime_minutes": 0,
         "half_day": False, "history": []}
    d.update(o)
    return d


def test_punch_in_correction_recomputes_and_reports_only_changed_fields():
    new, changes = main.plan_regularization(DAY_EMP, rec(), {"punch_in": ist(2026, 7, 7, 9, 28)})
    assert new["late_minutes"] == 0 and new["work_hours"] == 9.03
    assert list(changes) == ["punch_in", "work_hours", "late_minutes"]  # contract key order, nothing else
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["punch_in"] == {"from": ist(2026, 7, 7, 10, 5), "to": ist(2026, 7, 7, 9, 28)}


@pytest.mark.parametrize("supplied", [
    {}, {"status": "PRESENT"}, {"punch_in": ist(2026, 7, 7, 10, 5)},
    {"status": "PRESENT", "punch_in": ist(2026, 7, 7, 10, 5), "punch_out": ist(2026, 7, 7, 18, 30)},
])
def test_unchanged_request_is_rejected(supplied):
    with pytest.raises(main.RegularizeInvalid):
        main.plan_regularization(DAY_EMP, rec(), supplied)


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("field", ["punch_in", "punch_out"])
def test_absent_leave_with_punch_time_is_invalid(status, field):
    with pytest.raises(main.RegularizeInvalid) as e:
        main.plan_regularization(DAY_EMP, rec(), {"status": status, field: ist(2026, 7, 7, 12, 0)})
    assert e.value.field == field


def test_absent_clears_everything_and_reports_it():
    new, changes = main.plan_regularization(DAY_EMP, rec(), {"status": "ABSENT"})
    assert (new["punch_in"], new["punch_out"], new["work_hours"], new["late_minutes"], new["half_day"]) == (None, None, None, 0, False)
    assert set(changes) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_presence_status_needs_a_punch_in():
    absent = rec(status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0)
    with pytest.raises(main.RegularizeInvalid):
        main.plan_regularization(DAY_EMP, absent, {"status": "PRESENT"})
    new, _ = main.plan_regularization(DAY_EMP, absent, {"status": "PRESENT", "punch_in": ist(2026, 7, 7, 9, 50)})
    assert new["late_minutes"] == 20 and new["work_hours"] is None


def test_punch_in_must_stay_on_attendance_date_including_overnight_rule():
    with pytest.raises(main.RegularizeInvalid):
        main.plan_regularization(DAY_EMP, rec(), {"punch_in": ist(2026, 7, 8, 9, 0)})
    night = rec(emp_code="EMP0005", date="2026-07-06", punch_in=ist(2026, 7, 6, 21, 55), punch_out=ist(2026, 7, 7, 6, 40))
    new, _ = main.plan_regularization(NIGHT_EMP, night, {"punch_in": ist(2026, 7, 7, 3, 0)})  # 03:00 < 06:00 -> still 07-06
    assert new["late_minutes"] == 300
    with pytest.raises(main.RegularizeInvalid):
        main.plan_regularization(NIGHT_EMP, night, {"punch_in": ist(2026, 7, 7, 6, 0)})  # 06:00 belongs to 07-07


def test_punch_out_window_after_in_and_at_most_24h():
    base = rec()
    pin = base["punch_in"]
    for bad in (pin, pin - timedelta(seconds=1), pin + timedelta(hours=24, seconds=1)):
        with pytest.raises(main.RegularizeInvalid):
            main.plan_regularization(DAY_EMP, base, {"punch_out": bad})
    new, _ = main.plan_regularization(DAY_EMP, base, {"punch_out": pin + timedelta(hours=24)})
    assert new["work_hours"] == 24.0


def test_new_punch_in_after_stored_punch_out_is_invalid():
    with pytest.raises(main.RegularizeInvalid):
        main.plan_regularization(DAY_EMP, rec(), {"punch_in": ist(2026, 7, 7, 18, 45)})


def test_legacy_record_defaults():
    legacy = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT", "punch_in": ist(2026, 7, 14, 9, 32),
              "punch_out": ist(2026, 7, 14, 18, 33), "work_hours": 9.02, "late_minutes": 0}
    st = main.record_state(legacy)
    assert st["overtime_minutes"] == 0 and st["half_day"] is False
    _, changes = main.plan_regularization(DAY_EMP, legacy, {"status": "WFH"})
    assert list(changes) == ["status"]  # missing half_day/overtime are NOT reported as changes


def test_version_filter_shapes():
    f = main.record_version_filter({"emp_code": "E", "date": "d", "status": "PRESENT", "punch_in": None})
    assert f["punch_in"] is None and f["punch_out"] is None and "$or" in f and "history" not in f
    f = main.record_version_filter({"emp_code": "E", "date": "d", "status": "PRESENT", "history": [{}, {}]})
    assert f["history"] == {"$size": 2} and "$or" not in f
