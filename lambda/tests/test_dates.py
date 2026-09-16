from datetime import date

from dates import day_bounds_utc, format_utc_instant, parse_iso8601_duration_minutes, target_date


def test_target_date_applies_offset():
    assert target_date(date(2026, 1, 1), offset_days=1) == date(2026, 1, 2)
    assert target_date(date(2026, 1, 1), offset_days=-1) == date(2025, 12, 31)
    assert target_date(date(2026, 1, 1), offset_days=0) == date(2026, 1, 1)


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


def test_parse_iso8601_duration_minutes():
    assert parse_iso8601_duration_minutes("PT60M") == 60
    assert parse_iso8601_duration_minutes("PT30M") == 30
    assert parse_iso8601_duration_minutes("PT15M") == 15


def test_parse_iso8601_duration_minutes_rejects_unsupported_format():
    import pytest

    with pytest.raises(ValueError):
        parse_iso8601_duration_minutes("P1D")
