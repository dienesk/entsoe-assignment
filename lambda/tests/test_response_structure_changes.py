"""Pins the requirement that the scraper adapts to changes in response structure.

The task asks for processing that survives ENTSO-E changing what a response
looks like. "Survives" is doing a lot of work in that sentence, so these
tests state exactly what is and isn't claimed.

What holds: the output *columns* are derived from the document, so a renamed,
added, removed or re-nested field changes the CSV's columns instead of
raising, dropping data silently, or misaligning rows. A report with a
different metric, a different resolution, a different number of series -- or
from an entirely different document family -- needs no code change.

What doesn't: a response that stops being a TimeSeries/Period/Point document
altogether is a genuine breaking change, and fails loudly rather than
producing a plausible-looking empty CSV. That case is the last test here.

Each test mutates a *real* captured response in a way ENTSO-E could
plausibly ship, rather than asserting against a hand-built document that
already assumes the answer.
"""

import re
from pathlib import Path

import pytest

from csv_writer import rows_to_csv
from data_processor import DocumentError, flatten_document

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def day_ahead() -> str:
    return (FIXTURES_DIR / "day_ahead_response.xml").read_text()


def test_a_renamed_metric_renames_the_column_instead_of_emptying_it():
    # The metric column is named after the element inside <Point>, so this is
    # the difference between a renamed column and 24 rows of nulls.
    document = day_ahead().replace("quantity>", "quantity_v2>")

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert rows[0]["quantity_v2"] == "2197.9"
    assert "quantity" not in rows[0]


def test_an_added_metric_becomes_an_extra_column_alongside_the_existing_one():
    document = day_ahead().replace(
        "<quantity>2197.9</quantity>",
        "<quantity>2197.9</quantity><secondaryQuantity>42.0</secondaryQuantity>",
    )

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert rows[0]["quantity"] == "2197.9"
    assert rows[0]["secondaryQuantity"] == "42.0"
    # Only the first point gained the field; the CSV still has to line up.
    assert "secondaryQuantity" not in rows[1]
    header, first, second = rows_to_csv(rows).decode().splitlines()[:3]
    assert header.split(",").index("secondaryQuantity") == first.split(",").index("42.0")
    assert second.count(",") == first.count(",")


def test_a_removed_field_drops_its_column_and_leaves_the_rest_intact():
    # Per-unit's MktPSRType block is the richest thing in any response here;
    # losing it must not take the surrounding columns with it.
    document = (FIXTURES_DIR / "per_unit_response.xml").read_text()
    without_psr = re.sub(r"\s*<MktPSRType>.*?</MktPSRType>", "", document, flags=re.DOTALL)

    rows = flatten_document(without_psr.encode(), "generation_actual_per_unit")

    assert not [key for key in rows[0] if "MktPSRType" in key]
    assert rows[0]["ts_registeredResource.mRID"] == "24WV--EBO------8"
    assert rows[0]["quantity"] == "246.603"


def test_a_field_that_gains_nesting_becomes_a_dotted_column():
    # Re-nesting is the change most likely to silently drop a value, since a
    # naive reader looks for the old flat tag and finds nothing.
    document = day_ahead().replace(
        "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>",
        "<quantity_Measure_Unit><name>MAW</name></quantity_Measure_Unit>",
    )

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert rows[0]["ts_quantity_Measure_Unit.name"] == "MAW"


def test_a_changed_resolution_rebuilds_the_time_axis_to_match():
    # Not hypothetical: Slovakia publishes this report hourly and Czechia
    # publishes it at PT15M, so the two arrive from the same config.
    document = day_ahead().replace("<resolution>PT60M</resolution>", "<resolution>PT15M</resolution>")

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert [row["timestamp_utc"] for row in rows[:3]] == [
        "2026-09-17T00:00:00Z",
        "2026-09-17T00:15:00Z",
        "2026-09-17T00:30:00Z",
    ]
    # The same 3-hour period now holds 12 quarter-hour slots rather than 3
    # hourly ones, and this is a curveType A03 series, so the 3 published
    # points fill positions 1-3 and the last one is held across 4-12. Both
    # the slot count and the carry rule come from the document.
    assert len(rows) == 12
    assert [row["point_carried_forward"] for row in rows] == [False] * 3 + [True] * 9


def test_an_extra_time_series_with_different_fields_merges_without_misaligning():
    # Also real: Czechia returns a second series for the out-zone, carrying a
    # field the first one doesn't have.
    document = day_ahead()
    series = document[document.index("<TimeSeries>") : document.index("</TimeSeries>") + len("</TimeSeries>")]
    extra = series.replace(
        "<curveType>", "<outBiddingZone_Domain.mRID>10YCZ-CEPS-----N</outBiddingZone_Domain.mRID><curveType>"
    )
    document = document.replace(series, series + extra)

    rows = flatten_document(document.encode(), "generation_forecast_day_ahead")

    assert len(rows) == 6  # two series x three points
    assert "ts_outBiddingZone_Domain.mRID" not in rows[0]
    assert rows[3]["ts_outBiddingZone_Domain.mRID"] == "10YCZ-CEPS-----N"
    # The union column exists and stays empty for the series that lacks it.
    lines = rows_to_csv(rows).decode().splitlines()
    column = lines[0].split(",").index("ts_outBiddingZone_Domain.mRID")
    assert lines[1].split(",")[column] == ""
    assert lines[4].split(",")[column] == "10YCZ-CEPS-----N"


def test_an_entirely_different_document_family_needs_no_code_change():
    # day_ahead_prices_response.xml is a real A44 response: a
    # Publication_MarketDocument, not a GL_MarketDocument -- different
    # namespace, different document-level interval element, and a
    # <price.amount> metric where generation reports carry <quantity>.
    # Nothing in the processing was changed to accommodate it.
    document = (FIXTURES_DIR / "day_ahead_prices_response.xml").read_bytes()

    rows = flatten_document(document, "day_ahead_prices", "10YSK-SEPS-----K")

    assert rows[0]["price.amount"] == "178.7"
    assert rows[0]["doc_type"] == "A44"
    assert rows[0]["ts_currency_Unit.name"] == "EUR"
    # The document-level interval is <period.timeInterval> here and
    # <time_Period.timeInterval> on generation documents; both survive.
    assert rows[0]["doc_period.timeInterval.start"] == "2026-09-15T22:00Z"
    assert rows[0]["timestamp_utc"] == "2026-09-15T22:00:00Z"
    assert "quantity" not in rows[0]


def test_a_genuinely_unrecognizable_response_fails_loudly():
    # The limit of the claim: adapting to new *fields* is automatic, but a
    # response that is no longer a market document at all is a breaking
    # change, and has to look like one. Silently writing an empty CSV every
    # night is the failure mode worth avoiding.
    with pytest.raises(DocumentError):
        flatten_document(b"<html><body>502 Bad Gateway</body></html><<<", "generation_forecast_day_ahead")

    # A well-formed document carrying no series is not an error -- it is a
    # real, if empty, answer -- so it yields no rows rather than raising.
    assert flatten_document(b"<GL_MarketDocument><mRID>x</mRID></GL_MarketDocument>", "x") == []
