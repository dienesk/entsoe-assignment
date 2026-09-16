"""The two pieces of date handling that are genuinely tricky here.

Everything else the app needs (parsing an instant, adding a day offset) is a
one-liner on top of the standard library and lives at its call site.

The ENTSO-E Transparency Platform expresses each report's time window as a
UTC instant pair (e.g. ``2025-12-31T23:00:00Z`` .. ``2026-01-01T23:00:00Z``
for the CET calendar day 2026-01-01), so converting a local calendar day to
those bounds — correctly across DST — is the interesting part.
"""

from __future__ import annotations

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


def parse_iso8601_duration_minutes(duration: str) -> int:
    """Parse the small subset of ISO-8601 durations this API emits (e.g. PT60M, PT30M, PT15M).

    Deliberately narrow: raises ValueError on anything else rather than
    guessing, since a silently-wrong resolution would corrupt every
    timestamp in the output.
    """
    if not duration.startswith("PT") or not duration.endswith("M"):
        raise ValueError(f"Unsupported resolution format: {duration!r}")
    return int(duration[2:-1])
