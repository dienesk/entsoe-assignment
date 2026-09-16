from datetime import date, datetime, timezone

from csv_writer import build_s3_key


RUN_TS = datetime(2026, 1, 2, 5, 30, 0, tzinfo=timezone.utc)


def test_csv_key_lands_under_the_endpoint_prefix():
    key = build_s3_key("generation/forecast/day-ahead", "day_ahead", date(2026, 1, 1), RUN_TS, "csv")

    assert key == "generation/forecast/day-ahead/date=2026-01-01/day_ahead_20260102T053000Z.csv"


def test_raw_json_key_lands_under_the_single_raw_prefix():
    # The bucket's expire-raw-json lifecycle rule filters on this fixed
    # top-level prefix, so raw payloads must not be nested per-endpoint.
    key = build_s3_key("generation/forecast/day-ahead", "day_ahead", date(2026, 1, 1), RUN_TS, "json")

    assert key.startswith("raw/")
    assert key == "raw/generation/forecast/day-ahead/date=2026-01-01/day_ahead_20260102T053000Z.json"
