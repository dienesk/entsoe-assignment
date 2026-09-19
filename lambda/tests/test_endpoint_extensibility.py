"""Guards the task requirement that a new endpoint is addable by configuration alone.

The task asks that actual-generation-per-unit be addable "just by
changing/or adding some configuration". That endpoint is therefore NOT
shipped as a live config (every live config becomes a scheduled job that
costs requests and storage) -- it ships as a ready-to-use example in
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

from api_client import build_query_params
from config import ConfigError, validate_endpoint_config
from data_processor import flatten_document

EXAMPLES_DIR = Path(__file__).parent.parent / "config" / "endpoints" / "examples"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text())


def test_per_unit_example_config_is_valid_and_deployable():
    config = load_example("generation_actual_per_unit.json")

    # Same validation the Lambda applies to a config read from SSM, so this
    # file is ready to deploy as-is.
    validate_endpoint_config(config)

    assert config["endpoint_name"] == "generation_actual_per_unit"
    # An "actuals" endpoint collects the day that just finished.
    assert config["date_offset_days"] == -1


def test_per_unit_example_config_drives_the_api_client_unchanged():
    config = load_example("generation_actual_per_unit.json")

    params = build_query_params(config, run_date=date(2026, 1, 2), area="10YSK-SEPS-----K")

    # A different report is a different documentType/processType pair and
    # nothing else -- no new code path.
    assert params["documentType"] == "A73"
    assert params["processType"] == "A16"
    assert params["in_Domain"] == "10YSK-SEPS-----K"
    # offset -1 from 2026-01-02 targets the CET day 2026-01-01
    assert params["periodStart"] == "202512312300"
    assert params["periodEnd"] == "202601012300"


def test_per_unit_response_processes_without_code_changes():
    config = load_example("generation_actual_per_unit.json")
    captured = (FIXTURES_DIR / "per_unit_response.xml").read_bytes()

    rows = flatten_document(captured, config["endpoint_name"])

    # Columns day-ahead does not have, produced by the same code path.
    assert {"ts_MktPSRType.psrType", "ts_MktPSRType.PowerSystemResources.name"} <= rows[0].keys()
    assert rows[0]["endpoint_name"] == "generation_actual_per_unit"


def test_areas_are_configurable_without_touching_the_query_template():
    # Adding a country is a top-level config edit; the template's "{area}"
    # placeholder is endpoint-agnostic and never needs changing.
    config = load_example("generation_actual_per_unit.json")

    for area in ["10YCZ-CEPS-----N", "10YAT-APG------L"]:
        assert build_query_params(config, date(2026, 1, 2), area)["in_Domain"] == area


def test_malformed_areas_are_rejected_at_config_load():
    config = load_example("generation_actual_per_unit.json")

    for bad in [[], "10YSK-SEPS-----K", [""], ["CTA|10YSK-SEPS-----K"]]:
        with pytest.raises(ConfigError):
            validate_endpoint_config({**config, "areas": bad})


def test_a_config_may_not_carry_the_security_token():
    # Endpoint configs are plain-text SSM parameters generated from files in
    # this repo, so a token placed here would be committed and unencrypted.
    config = load_example("generation_actual_per_unit.json")
    config["query_template"] = {**config["query_template"], "securityToken": "leaked"}

    with pytest.raises(ConfigError, match="securityToken"):
        validate_endpoint_config(config)


def test_the_live_config_set_is_exactly_the_endpoint_the_task_asked_for():
    live = sorted(p.stem for p in (EXAMPLES_DIR.parent).glob("*.json"))

    assert live == ["generation_forecast_day_ahead"]
