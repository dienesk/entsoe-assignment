"""Timezone-aware date math shared by every endpoint config.

The ENTSO-E Transparency Platform expresses each report's time window as a
UTC instant pair (e.g. ``2025-12-31T23:00:00Z`` .. ``2026-01-01T23:00:00Z``
for the CET calendar day 2026-01-01). This module turns a *local calendar
day* plus an offset into those UTC bounds, using only the standard library.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def target_date(run_date: date, offset_days: int) -> date:
    """The local calendar day a scrape run should fetch.

    ``offset_days`` is endpoint-specific: +1 for a day-ahead forecast run
    today for tomorrow, -1 for an "actuals" endpoint gathering yesterday's
    finalized data, 0 to fetch the current day, etc.
    """
    return run_date + timedelta(days=offset_days)


def day_bounds_utc(local_day: date, tz_name: str) -> tuple[datetime, datetime]:
    """UTC [start, end) instants for one local calendar day.

    Example: ``day_bounds_utc(date(2026, 1, 1), "CET")`` ->
    ``(2025-12-31T23:00:00Z, 2026-01-01T23:00:00Z)``.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def format_utc_instant(instant: datetime) -> str:
    """Format a UTC datetime the way the platform's API responses do."""
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_instant(text: str) -> datetime:
    """Parse an API instant such as ``2025-12-31T23:00:00Z``."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))


def parse_iso8601_duration_minutes(duration: str) -> int:
    """Parse the small subset of ISO-8601 durations this API emits (e.g. PT60M, PT30M, PT15M).

    Deliberately narrow: raises ValueError on anything else rather than
    guessing, since a silently-wrong resolution would corrupt every
    timestamp in the output.
    """
    if not duration.startswith("PT") or not duration.endswith("M"):
        raise ValueError(f"Unsupported resolution format: {duration!r}")
    return int(duration[2:-1])
