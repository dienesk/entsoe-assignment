"""The two pieces of date handling that are genuinely tricky here.

Everything else the app needs (parsing an instant, adding a day offset) is a
one-liner on top of the standard library and lives at its call site.

The ENTSO-E Transparency Platform expresses each report's time window as a
UTC instant pair (e.g. ``2025-12-31T23:00:00Z`` .. ``2026-01-01T23:00:00Z``
for the CET calendar day 2026-01-01), so converting a local calendar day to
those bounds — correctly across DST — is the interesting part.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")


def day_bounds_utc(local_day: date, tz_name: str) -> tuple[datetime, datetime]:
    """UTC [start, end) instants for one local calendar day.

    Example: ``day_bounds_utc(date(2026, 1, 1), "CET")`` ->
    ``(2025-12-31T23:00:00Z, 2026-01-01T23:00:00Z)``.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def format_utc_instant(instant: datetime) -> str:
    """Format an aware UTC datetime the way the platform's API expresses instants.

    Rejects naive or non-UTC input rather than appending a ``Z`` to something
    that isn't UTC, which would silently mislabel every timestamp downstream.
    """
    if instant.utcoffset() != timedelta(0):
        raise ValueError(f"Expected an aware UTC datetime, got {instant!r}")
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


# Note the two different meanings of "M": months before the T separator,
# minutes after it. Endpoints on this platform range from PT15M (intraday)
# to P1Y (installed capacity), so the parser has to span both.
_DURATION_PATTERN = re.compile(
    r"^P(?!$)(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<days>\d+)D)?"
    r"(?:T(?!$)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration(duration: str) -> dict[str, int]:
    """Parse an ISO-8601 duration into its components.

    Raises ValueError on anything unrecognized rather than guessing, since a
    silently-wrong resolution would corrupt every timestamp in the output.
    """
    match = _DURATION_PATTERN.match(duration)
    if not match:
        raise ValueError(f"Unsupported resolution format: {duration!r}")
    return {unit: int(value or 0) for unit, value in match.groupdict().items()}


def shift_by_duration(instant: datetime, duration: dict[str, int], multiplier: int) -> datetime:
    """Advance ``instant`` by ``multiplier`` whole ``duration`` steps.

    Years and months need calendar arithmetic rather than a fixed timedelta,
    because their length varies -- a P1Y series must land on the same
    calendar date each year, not 365 days later.
    """
    months = (duration["years"] * 12 + duration["months"]) * multiplier
    if months:
        zero_based = instant.month - 1 + months
        year = instant.year + zero_based // 12
        month = zero_based % 12 + 1
        day = min(instant.day, monthrange(year, month)[1])
        instant = instant.replace(year=year, month=month, day=day)

    return instant + multiplier * timedelta(
        days=duration["days"],
        hours=duration["hours"],
        minutes=duration["minutes"],
        seconds=duration["seconds"],
    )
