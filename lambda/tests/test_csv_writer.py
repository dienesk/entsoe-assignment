from datetime import date, datetime, timezone

from csv_writer import build_s3_key, rows_to_csv


RUN_TS = datetime(2026, 1, 2, 5, 30, 0, tzinfo=timezone.utc)


def test_csv_key_lands_under_the_endpoint_prefix():
    key = build_s3_key("generation/forecast/day-ahead", "day_ahead", date(2026, 1, 1), RUN_TS, "csv")

    assert key == "generation/forecast/day-ahead/date=2026-01-01/day_ahead_20260102T053000Z.csv"


def test_raw_xml_key_lands_under_the_single_raw_prefix_and_names_its_area():
    # The bucket's expire-raw lifecycle rule filters on this fixed top-level
    # prefix, so raw payloads must not be nested per-endpoint. The area is in
    # the key because one run fetches one document per area.
    key = build_s3_key("generation/forecast/day-ahead", "day_ahead", date(2026, 1, 1), RUN_TS, "xml", "10YSK-SEPS-----K")

    assert key.startswith("raw/")
    assert key == "raw/generation/forecast/day-ahead/date=2026-01-01/day_ahead_10YSK-SEPS-----K_20260102T053000Z.xml"


def test_an_odd_area_value_cannot_invent_key_segments():
    # A stray config value must not add path segments the lifecycle rule and
    # the prefix layout don't expect.
    key = build_s3_key("p", "e", date(2026, 1, 1), RUN_TS, "xml", "10Y/../SK 1")

    assert "/" not in key.removeprefix("raw/p/date=2026-01-01/")
    assert key == "raw/p/date=2026-01-01/e_10Y-..-SK-1_20260102T053000Z.xml"


def test_csv_columns_are_the_union_across_rows():
    # Areas and report types differ in which fields they publish, so a row
    # missing a column another row has must not drop it or misalign the file.
    rows = [
        {"timestamp_utc": "2026-01-01T00:00:00Z", "quantity": "1"},
        {"timestamp_utc": "2026-01-01T01:00:00Z", "quantity": "2", "ts_outBiddingZone_Domain.mRID": "10YCZ"},
    ]

    lines = rows_to_csv(rows).decode().splitlines()

    assert lines[0] == "quantity,timestamp_utc,ts_outBiddingZone_Domain.mRID"
    assert lines[1] == "1,2026-01-01T00:00:00Z,"


def test_no_rows_writes_an_empty_body_rather_than_a_bare_header():
    assert rows_to_csv([]) == b""
