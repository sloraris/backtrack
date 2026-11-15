"""Next-run computation for daily ('04:00'), weekly ('sun 13:00') and monthly ('1 14:00') schedules."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _at(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    local = datetime.combine(d, time(h, m), tzinfo=tz)
    # Normalise through UTC: a wall time inside a spring-forward gap moves to the first valid instant.
    return local.astimezone(ZoneInfo("UTC")).astimezone(tz)


def next_run(spec: str, now: datetime, tz: ZoneInfo, kind: str) -> datetime:
    """First run strictly after `now` (an aware datetime)."""
    now = now.astimezone(tz)
    if kind == "daily":
        for i in range(0, 3):
            cand = _at(now.date() + timedelta(days=i), spec, tz)
            if cand > now:
                return cand
    elif kind == "weekly":
        day, hhmm = spec.split()
        target = DAYS.index(day)
        for i in range(0, 15):
            d = now.date() + timedelta(days=i)
            if d.weekday() == target:
                cand = _at(d, hhmm, tz)
                if cand > now:
                    return cand
    elif kind == "monthly":
        dom, hhmm = spec.split()
        y, m = now.year, now.month
        for _ in range(3):
            cand = _at(date(y, m, int(dom)), hhmm, tz)
            if cand > now:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError(f"cannot schedule {kind} {spec!r}")
