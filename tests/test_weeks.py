"""Week arithmetic: ISO weeks counted in Nepal Time (UTC+5:45)."""

from datetime import date, datetime, timedelta, timezone

import pytest

import weeks
from config import NPT

UTC = timezone.utc


def test_event_day_is_week_35():
    assert weeks.week_of(date(2026, 8, 26)) == "2026-W35"


def test_sunday_2359_npt_is_1814_utc_and_still_the_same_week():
    # Sun 11 Oct 23:59 NPT is 18:14 UTC; one minute later Monday has begun.
    assert weeks.week_of(datetime(2026, 10, 11, 18, 14, tzinfo=UTC)) == "2026-W41"
    assert weeks.week_of(datetime(2026, 10, 11, 18, 15, tzinfo=UTC)) == "2026-W42"


def test_utc_date_vs_npt_date():
    # 20:00 UTC on Sunday is already Monday 01:45 in Nepal.
    assert weeks.week_of(datetime(2026, 9, 6, 20, 0, tzinfo=UTC)) == "2026-W37"


def test_week_53_and_the_year_boundary():
    assert weeks.week_of(date(2026, 12, 31)) == "2026-W53"
    assert weeks.week_of(date(2027, 1, 3)) == "2026-W53"
    assert weeks.week_of(date(2027, 1, 4)) == "2027-W01"


def test_week_bounds_are_monday_to_sunday_in_npt():
    start, end = weeks.week_bounds("2026-W53")
    assert start == datetime(2026, 12, 28, 0, 0, 0, tzinfo=NPT)
    assert end == datetime(2027, 1, 3, 23, 59, 59, tzinfo=NPT)
    assert weeks.week_end_date("2026-W35") == date(2026, 8, 30)
    assert weeks.week_start_date("2026-W35") == date(2026, 8, 24)


def test_weeks_between_crosses_the_year():
    assert weeks.weeks_between("2026-W52", "2027-W02") == [
        "2026-W52", "2026-W53", "2027-W01", "2027-W02"]
    assert weeks.next_week("2026-W53") == "2027-W01"
    assert weeks.prev_week("2027-W01") == "2026-W53"


def test_is_closed_only_after_sunday_midnight_npt():
    sunday_late = datetime(2026, 10, 11, 23, 59, 59, tzinfo=NPT)
    monday = datetime(2026, 10, 12, 0, 0, 0, tzinfo=NPT)
    assert not weeks.is_closed("2026-W41", now=sunday_late)
    assert weeks.is_closed("2026-W41", now=monday)


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        weeks.week_of(datetime(2026, 9, 1, 12, 0))


@pytest.mark.parametrize("bad", ["2026-W54", "2025-W53", "2026-35", "2026-W5", ""])
def test_invalid_week_ids_are_refused(bad):
    with pytest.raises(ValueError):
        weeks.week_bounds(bad)
