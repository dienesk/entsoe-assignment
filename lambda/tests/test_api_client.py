import json
from datetime import date

from api_client import build_request_body
from config import load_local_endpoint_config


def test_build_request_body_substitutes_date_placeholders():
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    body = build_request_body(config, run_date=date(2025, 12, 31))

    # date_offset_days is +1, so run_date 2025-12-31 targets the CET day 2026-01-01
    assert body["dateTimeRange"]["from"] == "2025-12-31T23:00:00Z"
    assert body["dateTimeRange"]["to"] == "2026-01-01T23:00:00Z"
    assert body["timeZone"] == "CET"
    # non-placeholder fields pass through untouched; areaList is a list of
    # "<AREA_TYPE>|<EIC>" strings, which is what the platform's own client sends
    assert body["areaList"] == ["CTA|10YSK-SEPS-----K"]
    assert body["sorterList"] == []


def test_areas_come_from_the_top_level_config_field_as_a_real_list():
    config = load_local_endpoint_config("generation_forecast_day_ahead")
    config["areas"] = ["CTA|10YSK-SEPS-----K", "CTA|10YCZ-CEPS-----N"]

    body = build_request_body(config, run_date=date(2025, 12, 31))

    # A whole-value placeholder keeps its type: a JSON list, not "['CTA|...']"
    assert body["areaList"] == ["CTA|10YSK-SEPS-----K", "CTA|10YCZ-CEPS-----N"]


def test_endpoint_without_areas_still_builds():
    # Not every endpoint takes an area list; omitting both the field and the
    # placeholder must not break templating.
    config = {
        "endpoint_name": "no_areas",
        "timezone": "CET",
        "date_offset_days": 0,
        "request_template": {"dateTimeRange": {"from": "{datetime_from}", "to": "{datetime_to}"}},
    }

    body = build_request_body(config, run_date=date(2026, 1, 1))

    assert body["dateTimeRange"]["from"] == "2025-12-31T23:00:00Z"


def test_request_body_avoids_the_waf_blocked_max_int():
    # The platform sits behind an Azure Application Gateway WAF that rejects
    # the literal 2147483647 (Integer.MAX_VALUE) anywhere in the body as an
    # integer-overflow signature, returning 403 before the app ever sees it.
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    body = build_request_body(config, run_date=date(2025, 12, 31))

    assert "2147483647" not in json.dumps(body)


def test_build_request_body_applies_negative_offset():
    # Only the day-ahead endpoint ships as a config (offset +1), but an
    # "actuals"-style endpoint added later will use a negative offset to
    # collect the day that just finished, so keep that path covered.
    config = {
        "endpoint_name": "some_actuals_endpoint",
        "timezone": "CET",
        "date_offset_days": -1,
        "request_template": {"timeInterval": {"from": "{datetime_from}", "to": "{datetime_to}"}},
    }

    body = build_request_body(config, run_date=date(2026, 1, 2))

    # run_date 2026-01-02 with offset -1 targets the CET day 2026-01-01
    assert body["timeInterval"]["from"] == "2025-12-31T23:00:00Z"
    assert body["timeInterval"]["to"] == "2026-01-01T23:00:00Z"
