"""Guards the task requirement that a new endpoint is addable by configuration alone.

The task asks that actual-generation-per-unit be addable "just by
changing/or adding some configuration". These tests state that as an
executable claim rather than a promise in a README: a config file is driven
through validation, request building and response processing without any
application code being touched or extended.

The strongest case here is `test_an_endpoint_this_repo_has_never_seen_works`,
which builds a config for a report that ships in no file at all and processes
a real response from it. If adding an endpoint needed a code change, that
test could not pass.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from api_client import build_query_params
from config import ConfigError, load_local_endpoint_config, validate_endpoint_config
from data_processor import flatten_document

CONFIG_DIR = Path(__file__).parent.parent / "config" / "endpoints"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / f"{name}.json").read_text())


def test_every_live_config_is_valid():
    # Each file here becomes a scheduled job on the next apply, so a config
    # that fails validation deploys as a job that fails every night. Cheaper
    # to catch at commit time than in CloudWatch at 05:00.
    live = sorted(CONFIG_DIR.glob("*.json"))

    assert live, "expected at least the endpoint the task asked for"
    for path in live:
        config = validate_endpoint_config(json.loads(path.read_text()))
        # Terraform keys the SSM parameter and the EventBridge rule off the
        # filename, while the Lambda reports itself by endpoint_name; if they
        # disagree the logs name something the schedule doesn't.
        assert config["endpoint_name"] == path.stem
        # Two endpoints writing to one prefix would interleave in S3.
        assert config["s3_prefix"].strip("/")

    prefixes = [json.loads(path.read_text())["s3_prefix"] for path in live]
    assert len(set(prefixes)) == len(prefixes), "two endpoints share an s3_prefix"


def test_the_endpoint_the_task_asked_for_is_live():
    assert (CONFIG_DIR / "generation_forecast_day_ahead.json").exists()


def test_per_unit_is_a_config_change_not_a_code_change():
    config = load_config("generation_actual_per_unit")

    params = build_query_params(config, run_date=date(2026, 1, 2), area="10YSK-SEPS-----K")

    # A different report is a different documentType/processType pair and
    # nothing else -- no new code path, no new module, no branch on name.
    assert params["documentType"] == "A73"
    assert params["processType"] == "A16"
    assert params["in_Domain"] == "10YSK-SEPS-----K"
    # An "actuals" endpoint collects the day that just finished: offset -1
    # from 2026-01-02 targets the CET day 2026-01-01.
    assert config["date_offset_days"] == -1
    assert params["periodStart"] == "202512312300"
    assert params["periodEnd"] == "202601012300"


def test_per_unit_response_processes_without_code_changes():
    config = load_config("generation_actual_per_unit")
    captured = (FIXTURES_DIR / "per_unit_response.xml").read_bytes()

    rows = flatten_document(captured, config["endpoint_name"])

    # Columns day-ahead does not have, produced by the same code path.
    assert {"ts_MktPSRType.psrType", "ts_MktPSRType.PowerSystemResources.name"} <= rows[0].keys()
    assert rows[0]["endpoint_name"] == "generation_actual_per_unit"


def test_a_report_that_scopes_its_area_differently_needs_no_code_change():
    # Generation reports scope by in_Domain; load reports by
    # outBiddingZone_Domain, and sending the wrong one is a hard 400. That
    # the parameter name itself is configuration is what makes whole
    # families of reports reachable without touching the client.
    config = load_config("load_actual_total")

    params = build_query_params(config, run_date=date(2026, 1, 2), area="10YSK-SEPS-----K")

    assert params["outBiddingZone_Domain"] == "10YSK-SEPS-----K"
    assert "in_Domain" not in params


def test_an_endpoint_this_repo_has_never_seen_works():
    # Day-ahead prices: a config that exists in no file, for a document
    # family with its own namespace, its own document-level interval element
    # and a price.amount metric instead of quantity. Nothing was written for
    # it; it is processed by exactly the code the other endpoints use.
    config = validate_endpoint_config(
        {
            "endpoint_name": "day_ahead_prices",
            "url": "https://web-api.tp.entsoe.eu/api",
            "timezone": "CET",
            "date_offset_days": 1,
            "s3_prefix": "prices/day-ahead",
            "areas": ["10YSK-SEPS-----K"],
            "query_template": {"documentType": "A44", "in_Domain": "{area}", "out_Domain": "{area}"},
        }
    )

    params = build_query_params(config, run_date=date(2026, 9, 15), area="10YSK-SEPS-----K")
    assert params == {
        "documentType": "A44",
        "in_Domain": "10YSK-SEPS-----K",
        "out_Domain": "10YSK-SEPS-----K",
        "periodStart": "202609152200",
        "periodEnd": "202609162200",
    }

    rows = flatten_document((FIXTURES_DIR / "day_ahead_prices_response.xml").read_bytes(), "day_ahead_prices")
    assert rows[0]["price.amount"] == "178.7"
    assert rows[0]["ts_currency_Unit.name"] == "EUR"


def test_areas_are_configurable_without_touching_the_query_template():
    # Adding a country is a top-level config edit; the template's "{area}"
    # placeholder is endpoint-agnostic and never needs changing.
    config = load_config("generation_actual_per_unit")

    for area in ["10YCZ-CEPS-----N", "10YAT-APG------L"]:
        assert build_query_params(config, date(2026, 1, 2), area)["in_Domain"] == area


def test_malformed_areas_are_rejected_at_config_load():
    config = load_config("generation_actual_per_unit")

    for bad in [[], "10YSK-SEPS-----K", [""], ["CTA|10YSK-SEPS-----K"]]:
        with pytest.raises(ConfigError):
            validate_endpoint_config({**config, "areas": bad})


def test_a_config_may_not_carry_the_security_token():
    # Endpoint configs are plain-text SSM parameters generated from files in
    # this repo, so a token placed here would be committed and unencrypted.
    config = load_config("generation_actual_per_unit")
    config["query_template"] = {**config["query_template"], "securityToken": "leaked"}

    with pytest.raises(ConfigError, match="securityToken"):
        validate_endpoint_config(config)


def test_configs_load_by_name_the_same_way_the_lambda_loads_them_from_ssm():
    # load_local_endpoint_config is the local mirror of the SSM read, so a
    # file that loads here is a parameter that loads there.
    for path in CONFIG_DIR.glob("*.json"):
        assert load_local_endpoint_config(path.stem)["endpoint_name"] == path.stem
