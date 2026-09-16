"""Guards the task requirement that a new endpoint is addable by configuration alone.

The task asks that actual-generation-per-unit be addable "just by
changing/or adding some configuration". That endpoint is therefore NOT
shipped as a live config (an unverified request template would deploy as a
nightly job that fails every run) -- it ships as a ready-to-use example in
config/endpoints/examples/, which Terraform's discovery deliberately does
not pick up.

These tests prove the config-only path still works: the example config is
valid, the API client drives it with no code change, and the processing code
handles that endpoint's differently-shaped response. Enabling it for real is
then just `mv` into the parent directory + `terraform apply`.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from api_client import build_request_body
from config import ConfigError, validate_endpoint_config
from data_processor import flatten_response

EXAMPLES_DIR = Path(__file__).parent.parent / "config" / "endpoints" / "examples"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text())


def test_per_unit_example_config_is_valid_and_deployable():
    config = load_example("generation_actual_per_unit.json")

    # Same validation the Lambda applies to a config read from SSM, so this
    # file is ready to deploy as-is once its request_template is captured.
    validate_endpoint_config(config)

    assert config["endpoint_name"] == "generation_actual_per_unit"
    assert config["url"].endswith("/generation/actual/perUnit/loadOverview")
    # An "actuals" endpoint collects the day that just finished.
    assert config["date_offset_days"] == -1


def test_per_unit_example_config_drives_the_api_client_unchanged():
    config = load_example("generation_actual_per_unit.json")

    body = build_request_body(config, run_date=date(2026, 1, 2))

    # offset -1 from 2026-01-02 targets the CET day 2026-01-01
    assert body["dateTimeRange"]["from"] == "2025-12-31T23:00:00Z"
    assert body["dateTimeRange"]["to"] == "2026-01-01T23:00:00Z"
    assert body["timeZone"] == "CET"
    assert body["areaList"] == ["CTA|10YSK-SEPS-----K"]


def test_per_unit_response_processes_without_code_changes():
    config = load_example("generation_actual_per_unit.json")
    captured_response = json.loads((FIXTURES_DIR / "per_unit_response.json").read_text())

    rows = flatten_response(captured_response, config["endpoint_name"])

    # A dimension set day-ahead does not have, produced by the same code path.
    assert {"dim_GENERATION_UNIT", "dim_PRODUCTION_TYPE"} <= rows[0].keys()
    assert rows[0]["endpoint_name"] == "generation_actual_per_unit"


def test_areas_are_configurable_without_touching_the_template():
    # Adding a country is a top-level config edit; the request_template's
    # "{areas}" placeholder is endpoint-agnostic and never needs changing.
    config = load_example("generation_actual_per_unit.json")
    config["areas"] = ["CTA|10YCZ-CEPS-----N", "CTA|10YAT-APG------L"]

    body = build_request_body(config, run_date=date(2026, 1, 2))

    assert body["areaList"] == ["CTA|10YCZ-CEPS-----N", "CTA|10YAT-APG------L"]


def test_malformed_areas_are_rejected_at_config_load():
    config = load_example("generation_actual_per_unit.json")

    for bad in [[], "CTA|10YSK-SEPS-----K", ["10YSK-SEPS-----K"]]:
        with pytest.raises(ConfigError):
            validate_endpoint_config({**config, "areas": bad})
