"""
Parse a clinic's free-text weekly operating hours into numbers, and sum the
scheduled open hours that fall inside a date range.

``Users.clinic_location_details`` stores one ``hours_<weekday>`` STRING per day,
hand-entered and therefore irregular: ``"9:00 AM - 5:00 PM"``, ``"9am-5pm"``,
``"9 - 5"``, ``"Closed"``, ``"By appointment"``, or a split day like
``"9:00 AM - 12:00 PM, 1:00 PM - 5:00 PM"``. This module is intentionally
dependency-free (pure stdlib) so it is trivially unit-testable without a DB or
BigQuery — the API layer feeds it the seven raw strings.

Used by the Intelligence Overview "Revenue per clinic hour" KPI: revenue ÷
scheduled open hours in the selected window.
"""
from __future__ import annotations

import datetime as _dt
import re

# Weekday order matching Python's date.weekday() (0 = Monday … 6 = Sunday).
WEEKDAY_ATTRS = (
    "hours_monday", "hours_tuesday", "hours_wednesday", "hours_thursday",
    "hours_friday", "hours_saturday", "hours_sunday",
)

_CLOSED_TOKENS = ("closed", "by appointment", "appointment only", "n/a", "")

# One clock time: "9", "9:30", "9 am", "9:30am", "9:30 AM", "17:00".
_TIME_RE = re.compile(
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?",
    re.IGNORECASE,
)


def _parse_time(token: str) -> float | None:
    """A single clock time → hours-since-midnight as a float, or None."""
    m = _TIME_RE.fullmatch(token.strip())
    if not m:
        return None
    h = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 24) or not (0 <= minute < 60):
        return None
    return h + minute / 60.0


def parse_day_intervals(raw: str | None) -> list[tuple[float, float]]:
    """Open intervals for a single day as ``(open_hour, close_hour)`` floats
    (hours since midnight). Empty for closed / unparseable / empty input.
    Handles multiple ranges separated by commas/semicolons (split lunch)."""
    if not raw:
        return []
    text = raw.strip().lower()
    if text in _CLOSED_TOKENS:
        return []

    intervals: list[tuple[float, float]] = []
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Range separator is an en/em dash, hyphen, or the word "to".
        parts = re.split(r"\s*(?:-|–|—|to)\s*", chunk)
        if len(parts) != 2:
            continue
        open_h = _parse_time(parts[0])
        close_h = _parse_time(parts[1])
        if open_h is None or close_h is None:
            continue
        # Clinic hours are daytime: a close at or before the open (common when
        # am/pm is omitted, e.g. "9 - 5" or "8 to 4") means the close is PM.
        if close_h <= open_h:
            close_h += 12
        if close_h > open_h:
            intervals.append((open_h, close_h))
    return intervals


def parse_day_hours(raw: str | None) -> float:
    """Total open hours for a single day's free-text string (0.0 if closed)."""
    return sum(close - open_ for open_, close in parse_day_intervals(raw))


def _raw_for(location, attr):
    return location.get(attr) if isinstance(location, dict) else getattr(location, attr, None)


def weekly_hours(location) -> dict[int, float]:
    """Map weekday index (0=Mon … 6=Sun) → open hours, from a location object
    or dict exposing the seven ``hours_<weekday>`` attributes/keys."""
    return {idx: parse_day_hours(_raw_for(location, attr))
            for idx, attr in enumerate(WEEKDAY_ATTRS)}


def weekly_intervals(location) -> dict[int, list[tuple[float, float]]]:
    """Map weekday index (0=Mon … 6=Sun) → list of open ``(open, close)`` hour
    intervals."""
    return {idx: parse_day_intervals(_raw_for(location, attr))
            for idx, attr in enumerate(WEEKDAY_ATTRS)}


def is_open_at(location, weekday: int, hour_of_day: float) -> bool:
    """Whether the clinic is open at ``hour_of_day`` (0–24 float) on ``weekday``
    (0=Mon). Used to flag missed calls that landed during business hours."""
    return any(open_ <= hour_of_day < close
               for open_, close in weekly_intervals(location).get(weekday, []))


def open_hours_in_window(
    location,
    start_date: str | _dt.date,
    end_date_inclusive: str | _dt.date,
) -> float:
    """Sum scheduled open hours for every calendar day in ``[start, end]``
    (both inclusive), based on each day's weekday template."""
    weekly = weekly_hours(location)
    start = _dt.date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end = _dt.date.fromisoformat(end_date_inclusive) if isinstance(end_date_inclusive, str) else end_date_inclusive
    if end < start:
        return 0.0
    total = 0.0
    d = start
    while d <= end:
        total += weekly.get(d.weekday(), 0.0)
        d += _dt.timedelta(days=1)
    return total
