"""Week arithmetic for the timeline: ISO 8601 weeks, counted in Nepal Time.

A week id looks like "2026-W41". A week runs from Monday 00:00:00 to
Sunday 23:59:59 Nepal Time (NPT, a fixed UTC+5:45). Every date in the
project is bucketed with these functions, so a news story published at
20:00 UTC on a Sunday (already Monday in Kathmandu) lands in the right week.

Pure functions only: no files, no network.
"""

import re
from datetime import date, datetime, timedelta

from config import NPT

_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")


def _parse(week: str) -> tuple:
    """'2026-W41' -> (2026, 41), refusing ids that don't exist."""
    m = _WEEK_RE.fullmatch(week or "")
    if not m:
        raise ValueError(f"not a week id like 2026-W41: {week!r}")
    year, num = int(m.group(1)), int(m.group(2))
    # 28 December is always in the last ISO week of its year.
    weeks_in_year = date(year, 12, 28).isocalendar().week
    if not 1 <= num <= weeks_in_year:
        raise ValueError(f"{year} has {weeks_in_year} ISO weeks, not {num}: {week!r}")
    return year, num


def _npt_date(when) -> date:
    """The calendar date in Nepal for a date or a timezone-aware datetime."""
    if isinstance(when, datetime):
        if when.tzinfo is None:
            raise ValueError("naive datetime: say which timezone it is in "
                             "(e.g. tzinfo=timezone.utc)")
        return when.astimezone(NPT).date()
    if isinstance(when, date):
        return when          # a bare date is taken as a Nepal calendar date
    raise TypeError(f"expected a date or datetime, got {type(when).__name__}")


def week_of(when) -> str:
    """The week id that a date or aware datetime falls in (in NPT)."""
    iso = _npt_date(when).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_start_date(week: str) -> date:
    """The Monday of a week."""
    year, num = _parse(week)
    return date.fromisocalendar(year, num, 1)


def week_end_date(week: str) -> date:
    """The Sunday of a week."""
    return week_start_date(week) + timedelta(days=6)


def week_bounds(week: str) -> tuple:
    """(Monday 00:00:00 NPT, Sunday 23:59:59 NPT) as aware datetimes."""
    start = datetime.combine(week_start_date(week), datetime.min.time(), tzinfo=NPT)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


def next_week(week: str) -> str:
    return week_of(week_start_date(week) + timedelta(days=7))


def prev_week(week: str) -> str:
    return week_of(week_start_date(week) - timedelta(days=7))


def weeks_between(first: str, last: str) -> list:
    """Every week id from first to last, inclusive."""
    if week_start_date(first) > week_start_date(last):
        raise ValueError(f"{first} is after {last}")
    out = [first]
    while out[-1] != last:
        out.append(next_week(out[-1]))
    return out


def is_closed(week: str, now: datetime) -> bool:
    """True once the week's last second (Sunday 23:59:59 NPT) has passed."""
    _, end = week_bounds(week)
    if now.tzinfo is None:
        raise ValueError("naive datetime for 'now'")
    return now > end
