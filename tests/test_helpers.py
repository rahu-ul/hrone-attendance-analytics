"""Pure unit tests of the time/rule helpers. No database needed: these always run."""
from datetime import datetime, timedelta, timezone

import pytest

from app import main as m
from tests.conftest import ist_ms

IST = m.IST


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


# ---- instants ----
def test_ms_roundtrip_and_truncation():
    ms = ist_ms(2026, 7, 6, 9, 40, 0, 900)
    dt = m.ms_to_dt(ms)
    assert dt.microsecond == 0 and dt.tzinfo is not None
    assert m.dt_to_ms(dt) == ms - 900  # truncated to whole seconds


def test_dt_to_ms_accepts_naive_as_utc():
    assert m.dt_to_ms(datetime(2026, 7, 6, 4, 0)) == m.dt_to_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc))


# ---- R2 late ----
@pytest.mark.parametrize("h,mi,s,want", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0), (9, 30, 0, 0)])
def test_late_contract_examples(h, mi, s, want):
    start = ist(2026, 7, 6, 9, 30)
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), start) == want


def test_late_ignores_microseconds():
    start = ist(2026, 7, 6, 9, 30)
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900000), start) == 0  # 09:40:00.900 == 09:40:00


def test_late_uses_ist_even_for_utc_input():
    start = ist(2026, 7, 6, 22, 0)
    pin = datetime(2026, 7, 6, 17, 30, tzinfo=timezone.utc)  # 23:00 IST
    assert m.compute_late_minutes(pin, start) == 60


# ---- R4 work hours: half-up, not round() ----
def test_work_hours_half_up_not_bankers():
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    # 54 s = 0.015 h -> half-up 0.02 (Python round(54/3600, 2) gives 0.01)
    assert round(54 / 3600, 2) == 0.01
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=54)) == 0.02
    # 162 s = 0.045 h -> 0.05
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=162)) == 0.05


def test_work_hours_sample_documents():
    d = lambda s: datetime.fromisoformat(s)
    assert m.compute_work_hours(d("2026-07-06T03:58:00+00:00"), d("2026-07-06T13:05:00+00:00")) == 9.12
    assert m.compute_work_hours(d("2026-07-06T04:35:00+00:00"), d("2026-07-06T13:40:00+00:00")) == 9.08
    assert m.compute_work_hours(d("2026-07-07T04:00:00+00:00"), d("2026-07-07T07:30:00+00:00")) == 3.5
    assert m.compute_work_hours(d("2026-07-06T16:25:00+00:00"), d("2026-07-07T01:10:00+00:00")) == 8.75


def test_work_hours_exact_vs_decimal_bruteforce():
    from decimal import ROUND_HALF_UP, Decimal
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    for s in range(0, 90_000, 7):
        want = float((Decimal(s) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))
        assert m.compute_work_hours(t0, t0 + timedelta(seconds=s)) == want


def test_round_half_up_helper():
    assert m.round_half_up(2.675, 2) == 2.68  # round(2.675, 2) == 2.67
    assert m.round_half_up(0.00005, 4) == 0.0001
    assert m.round_half_up(1.005, 2) == 1.01


# ---- R5 half day: uses the ROUNDED value ----
def test_half_day_uses_rounded_hours():
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    wh = m.compute_work_hours(t0, t0 + timedelta(seconds=16182))  # 4.495 h -> rounds to 4.50
    assert wh == 4.5 and m.compute_half_day(wh) is False
    wh = m.compute_work_hours(t0, t0 + timedelta(seconds=16181))  # 4.4947 h -> 4.49
    assert wh == 4.49 and m.compute_half_day(wh) is True
    assert m.compute_half_day(None) is False


# ---- R3 overtime ----
def test_overtime_threshold_and_floor():
    end = ist(2026, 7, 6, 18, 30)
    assert m.compute_overtime(ist(2026, 7, 6, 18, 59, 59), end) == 0   # 29 min
    assert m.compute_overtime(ist(2026, 7, 6, 19, 0, 0), end) == 30    # exactly 30
    assert m.compute_overtime(ist(2026, 7, 6, 19, 10, 59), end) == 40  # floor
    assert m.compute_overtime(ist(2026, 7, 6, 18, 0), end) == 0        # before shift end


# ---- R1 attendance date & overnight windows ----
def test_attendance_date_day_shift_uses_ist_date():
    # 20:00Z on Jul 6 == 01:30 IST on Jul 7
    pin = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)
    assert m.attendance_date_for(pin, "09:30", "18:30") == "2026-07-07"


def test_attendance_date_overnight_boundaries():
    f = lambda h, mi, s: m.attendance_date_for(ist(2026, 7, 7, h, mi, s), "22:00", "06:00")
    assert f(5, 59, 59) == "2026-07-06"   # earlier than shift_end -> previous day's shift
    assert f(6, 0, 0) == "2026-07-07"     # NOT earlier than shift_end -> today
    assert f(21, 55, 0) == "2026-07-07"
    assert f(0, 0, 0) == "2026-07-06"


def test_overnight_window_and_sample7():
    start, end = m.shift_window("2026-07-06", "22:00", "06:00")
    assert start == ist(2026, 7, 6, 22, 0) and end == ist(2026, 7, 7, 6, 0)
    d = m.derive_fields("22:00", "06:00", "2026-07-06",
                        datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc), datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc))
    assert d == {"late_minutes": 0, "work_hours": 8.75, "overtime_minutes": 40, "half_day": False}


def test_overnight_early_morning_punch_in_late_vs_previous_start():
    pin = ist(2026, 7, 7, 3, 0)
    date = m.attendance_date_for(pin, "22:00", "06:00")
    assert date == "2026-07-06"
    assert m.derive_fields("22:00", "06:00", date, pin, None)["late_minutes"] == 300


def test_derive_fields_open_record_and_sample2_and_naive_input():
    pin = datetime(2026, 7, 6, 4, 35)  # naive -> treated as UTC (10:05 IST)
    assert m.derive_fields("09:30", "18:30", "2026-07-06", pin, None) == \
        {"late_minutes": 35, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    pout = datetime(2026, 7, 6, 13, 40)
    assert m.derive_fields("09:30", "18:30", "2026-07-06", pin, pout) == \
        {"late_minutes": 35, "work_hours": 9.08, "overtime_minutes": 40, "half_day": False}
