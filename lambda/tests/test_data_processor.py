"""Tests for the response-to-rows processing, run against real captured API responses.

day_ahead_response.json and per_unit_response.json are trimmed copies of
actual responses captured from the ENTSO-E Transparency Platform while
researching this task (both endpoints happened to have no data yet for the
sampled date, hence the "N/A"/"n/e" placeholders). populated_response.json
is a synthetic, schema-conformant fixture used to exercise the "value"
extraction path, since no captured sample had real numeric data.
"""

import json
from pathlib import Path

import pytest

from data_processor import flatten_response

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def test_day_ahead_real_response_flattens_one_row_per_point():
    payload = load_fixture("day_ahead_response.json")

    rows = flatten_response(payload, "generation_forecast_day_ahead")

    assert len(rows) == 3  # 3 points in the trimmed fixture's pointMap
    first = rows[0]
    assert first["endpoint_name"] == "generation_forecast_day_ahead"
    assert first["dim_AREA"] == "CTA|10YSK-SEPS-----K"
    assert first["timestamp_utc"] == "2025-12-31T23:00:00Z"
    # metric columns come from pointAttributeVariabilityMap, in order
    assert set(["SCHEDULED_CONSUMPTION", "GENERATION_FORECAST", "ACTUAL_GENERATION"]) <= first.keys()
    assert first["GENERATION_FORECAST"] == "N/A"  # falls back to the "alt" key
    # instanceAttributeMap surfaces as attr_ columns, kept as raw strings
    assert first["attr_PRODUCTION_TYPE_LIST"].startswith("[\"B19\"")


def test_timestamps_increment_by_the_resolution():
    payload = load_fixture("day_ahead_response.json")

    rows = flatten_response(payload, "generation_forecast_day_ahead")

    assert [row["timestamp_utc"] for row in rows] == [
        "2025-12-31T23:00:00Z",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
    ]


def test_per_unit_response_has_one_row_per_generation_unit():
    # Per-unit does not ship as a configured endpoint (only day-ahead does),
    # but its captured response is kept here as the evidence that the
    # processing generalizes to a second, differently-shaped report --
    # which is what makes it addable by configuration alone.
    payload = load_fixture("per_unit_response.json")

    rows = flatten_response(payload, "generation_actual_per_unit")

    assert len(rows) == 2
    units = {row["dim_GENERATION_UNIT"] for row in rows}
    assert units == {"24WG--EMOG31---P", "24WG--ENOG03---O"}
    # a *different* dimension set than day-ahead -- same processor handles both
    assert "dim_PRODUCTION_TYPE" in rows[0]
    assert "ACTUAL_GENERATION_OUTPUT" in rows[0]


def test_populated_points_prefer_value_over_alt():
    payload = load_fixture("populated_response.json")

    rows = flatten_response(payload, "generation_forecast_day_ahead")

    assert rows[0]["GENERATION_FORECAST"] == 1234.5
    assert rows[0]["SCHEDULED_CONSUMPTION"] == 987.0
    # second point mixes a real value and a still-missing one in the same row
    assert rows[1]["GENERATION_FORECAST"] == 1300.0
    assert rows[1]["SCHEDULED_CONSUMPTION"] == "N/A"


def test_missing_instance_list_yields_no_rows():
    assert flatten_response({}, "whatever") == []


@pytest.mark.parametrize(
    "from_instant",
    ["2025-12-31T23:00:00Z", "2025-12-31T23:00:00.000Z", "2025-12-31T23:00:00+00:00"],
)
def test_period_start_accepts_iso8601_spelling_variants(from_instant):
    # The platform is an undocumented internal API; a serializer change that
    # starts emitting fractional seconds or a numeric offset must not take
    # the whole scrape down.
    payload = load_fixture("day_ahead_response.json")
    payload["instanceList"][0]["curveData"]["periodList"][0]["timeInterval"]["from"] = from_instant

    rows = flatten_response(payload, "generation_forecast_day_ahead")

    assert rows[0]["timestamp_utc"] == "2025-12-31T23:00:00Z"


@pytest.mark.parametrize("fixture_name", ["day_ahead_response.json", "per_unit_response.json"])
def test_unknown_extra_keys_do_not_break_flattening(fixture_name):
    payload = load_fixture(fixture_name)
    payload["someBrandNewTopLevelField"] = {"nested": "value"}
    payload["instanceList"][0]["someBrandNewInstanceField"] = "ignored"

    rows = flatten_response(payload, "x")

    assert len(rows) > 0
