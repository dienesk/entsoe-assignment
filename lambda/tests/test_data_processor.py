"""Tests for the document-to-rows processing, run against real captured API responses.

day_ahead_response.xml and per_unit_response.xml are trimmed copies of
actual ENTSO-E API responses for the Slovak bidding zone (SK day-ahead
forecast for 2026-09-17, per-unit actuals for 2026-09-15), cut down to a few
points so the expected values can be written out by hand.
variable_block_response.xml is synthetic: every captured response happened
to publish a point at every position, so it is the only way to exercise the
curveType A03 carry-forward path.
"""

from pathlib import Path

import pytest

from data_processor import DocumentError, flatten_document

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_day_ahead_real_response_flattens_one_row_per_point():
    rows = flatten_document(load_fixture("day_ahead_response.xml"), "generation_forecast_day_ahead", "10YSK-SEPS-----K")

    assert len(rows) == 3  # 3 points in the trimmed fixture
    first = rows[0]
    assert first["endpoint_name"] == "generation_forecast_day_ahead"
    assert first["request_area"] == "10YSK-SEPS-----K"
    assert first["timestamp_utc"] == "2026-09-17T00:00:00Z"
    assert first["quantity"] == "2197.9"
    # document-level provenance, denormalized onto every row
    assert first["doc_type"] == "A71"
    assert first["doc_mRID"] == "263d0351ffeb4aeea98562c9764b9a04"
    # TimeSeries metadata, named after the elements it came from
    assert first["ts_inBiddingZone_Domain.mRID"] == "10YSK-SEPS-----K"
    assert first["ts_quantity_Measure_Unit.name"] == "MAW"


def test_constant_platform_identifiers_are_dropped():
    # sender/receiver are ENTSO-E itself on every document of every type;
    # doc_mRID is what actually identifies the response a row came from.
    rows = flatten_document(load_fixture("day_ahead_response.xml"), "generation_forecast_day_ahead")

    assert not [key for key in rows[0] if "MarketParticipant" in key]


def test_timestamps_are_reconstructed_from_position_and_resolution():
    # Points carry a 1-based position and no timestamp; the time axis has to
    # be rebuilt from the period start and the resolution.
    rows = flatten_document(load_fixture("day_ahead_response.xml"), "generation_forecast_day_ahead")

    assert [row["timestamp_utc"] for row in rows] == [
        "2026-09-17T00:00:00Z",
        "2026-09-17T01:00:00Z",
        "2026-09-17T02:00:00Z",
    ]
    assert [row["point_position"] for row in rows] == [1, 2, 3]


def test_per_unit_response_adds_generation_unit_columns_with_no_code_change():
    # Per-unit is not a live config (only day-ahead is), but its captured
    # response is kept here as evidence that the processing generalizes to a
    # second, differently-shaped report -- which is what makes it addable by
    # configuration alone.
    rows = flatten_document(load_fixture("per_unit_response.xml"), "generation_actual_per_unit")

    assert len(rows) == 4  # 2 units x 2 points
    units = {row["ts_MktPSRType.PowerSystemResources.name"] for row in rows}
    assert units == {"Bohunice TG31", "Bohunice TG32"}
    # dimensions day-ahead does not have, produced by the same code path
    assert rows[0]["ts_MktPSRType.psrType"] == "B14"
    assert rows[0]["ts_registeredResource.mRID"] == "24WV--EBO------8"
    assert rows[0]["doc_type"] == "A73"


def test_variable_sized_blocks_carry_the_previous_value_forward():
    # curveType A03 publishes a point only when the value changes, so
    # positions 2 and 4 are absent and inherit 1 and 3.
    rows = flatten_document(load_fixture("variable_block_response.xml"), "generation_forecast_day_ahead")

    assert [(row["point_position"], row["quantity"]) for row in rows] == [
        (1, "2197.9"),
        (2, "2197.9"),
        (3, "2204.5"),
        (4, "2204.5"),
    ]
    # Filled values are flagged, so an analyst can always get back to exactly
    # what was published.
    assert [row["point_carried_forward"] for row in rows] == [False, True, False, True]


def test_a_document_with_no_time_series_yields_no_rows():
    empty = b'<?xml version="1.0"?><GL_MarketDocument><mRID>x</mRID></GL_MarketDocument>'

    assert flatten_document(empty, "whatever") == []


def test_malformed_xml_fails_loudly():
    # Better a named error naming the endpoint than an empty CSV.
    with pytest.raises(DocumentError):
        flatten_document(b"<GL_MarketDocument><unclosed>", "generation_forecast_day_ahead")


@pytest.mark.parametrize(
    "start_instant",
    ["2026-09-17T00:00Z", "2026-09-17T00:00:00Z", "2026-09-17T00:00:00.000Z", "2026-09-17T00:00+00:00"],
)
def test_period_start_accepts_iso8601_spelling_variants(start_instant):
    # The API currently omits seconds; a serializer change that starts
    # emitting them (or a numeric offset) must not take the scrape down.
    document = load_fixture("day_ahead_response.xml").decode()
    document = document.replace("<start>2026-09-17T00:00Z</start>", f"<start>{start_instant}</start>")

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert rows[0]["timestamp_utc"] == "2026-09-17T00:00:00Z"


@pytest.mark.parametrize("fixture_name", ["day_ahead_response.xml", "per_unit_response.xml"])
def test_unknown_extra_elements_become_columns_rather_than_breaking(fixture_name):
    # The point of deriving columns from the document: a field ENTSO-E adds
    # later shows up in the CSV instead of being dropped or crashing the run.
    document = load_fixture(fixture_name).decode()
    document = document.replace("<curveType>", "<someBrandNewField>surprise</someBrandNewField><curveType>", 1)

    rows = flatten_document(document.encode(), "x")

    assert rows[0]["ts_someBrandNewField"] == "surprise"
    assert len(rows) > 0
