from datetime import date

from config import load_local_endpoint_config
from entsoe_client import build_request_body


def test_build_request_body_substitutes_date_placeholders():
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    body = build_request_body(config, run_date=date(2025, 12, 31))

    # date_offset_days is +1, so run_date 2025-12-31 targets 2026-01-01
    assert body["intervalCriteria"]["timeInterval"]["from"] == "2025-12-31T23:00:00Z"
    assert body["intervalCriteria"]["timeInterval"]["to"] == "2026-01-01T23:00:00Z"
    assert body["timeZone"] == "CET"
    # non-placeholder fields pass through untouched
    assert body["dataViewCode"] == "GUI_TOTAL_GENERATION_FORECAST"
    assert body["businessDimensionMap"]["AREA"] == "CTA|10YSK-SEPS-----K"


def test_build_request_body_applies_negative_offset():
    config = load_local_endpoint_config("generation_actual_per_unit")

    body = build_request_body(config, run_date=date(2026, 1, 2))

    # date_offset_days is -1, so run_date 2026-01-02 targets 2026-01-01
    assert body["intervalCriteria"]["timeInterval"]["from"] == "2025-12-31T23:00:00Z"
    assert body["intervalCriteria"]["timeInterval"]["to"] == "2026-01-01T23:00:00Z"
