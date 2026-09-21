from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dates import day_bounds_utc, format_utc_instant, parse_iso8601_duration, shift_by_duration


def test_day_bounds_utc_matches_observed_api_response():
    # The real day-ahead response we captured for the CET calendar day
    # 2026-01-01 bounded its data exactly as 2025-12-31T23:00:00Z .. 2026-01-01T23:00:00Z.
    start, end = day_bounds_utc(date(2026, 1, 1), "CET")

    assert format_utc_instant(start) == "2025-12-31T23:00:00Z"
    assert format_utc_instant(end) == "2026-01-01T23:00:00Z"


def test_day_bounds_utc_handles_dst_transition():
    # CEST (summer) is UTC+2, so the UTC offset shifts by an hour vs winter.
    start, end = day_bounds_utc(date(2026, 7, 1), "CET")

    assert format_utc_instant(start) == "2026-06-30T22:00:00Z"
    assert format_utc_instant(end) == "2026-07-01T22:00:00Z"


@pytest.mark.parametrize(
    "duration,steps,expected",
    [
        # Resolutions actually observed across the platform's endpoints.
        ("PT15M", 4, "2026-01-01T01:00:00Z"),  # intraday
        ("PT60M", 3, "2026-01-01T03:00:00Z"),  # day-ahead
        ("P1D", 2, "2026-01-03T00:00:00Z"),
        ("P1M", 2, "2026-03-01T00:00:00Z"),  # calendar months, not 30 days
        ("P1Y", 2, "2028-01-01T00:00:00Z"),  # installed capacity
    ],
)
def test_shift_by_duration_across_the_platforms_resolutions(duration, steps, expected):
    start = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))

    result = shift_by_duration(start, parse_iso8601_duration(duration), steps)

    assert format_utc_instant(result) == expected


def test_shift_by_month_clamps_to_end_of_shorter_month():
    # 31 Jan + 1 month has no 31st to land on; clamp rather than overflow.
    start = datetime(2026, 1, 31, tzinfo=ZoneInfo("UTC"))

    result = shift_by_duration(start, parse_iso8601_duration("P1M"), 1)

    assert format_utc_instant(result) == "2026-02-28T00:00:00Z"


def test_parse_iso8601_duration_rejects_unrecognized_format():
    # Better to fail loudly than silently emit wrong timestamps for every row.
    for bad in ["", "P", "PT", "1H", "P1X", "banana"]:
        with pytest.raises(ValueError):
            parse_iso8601_duration(bad)


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 1, 1, 12, 0),  # naive
        datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),  # CEST, not UTC
    ],
)
def test_format_utc_instant_rejects_non_utc(instant):
    # Appending a literal "Z" to these would silently mislabel every
    # timestamp written to the CSV.
    with pytest.raises(ValueError):
        format_utc_instant(instant)
