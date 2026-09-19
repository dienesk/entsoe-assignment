"""End-to-end wiring test for the Lambda entrypoint, with AWS and the network faked.

Covers the seams the per-module tests can't see: that the token is read from
its own parameter (once per container, not per endpoint), that one document
per area becomes one merged CSV plus one raw object each, and that a failing
endpoint doesn't take the others down.
"""

import json
from pathlib import Path

import pytest

import api_client
import handler

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "endpoints" / "generation_forecast_day_ahead.json"


class _FakeSsm:
    """Serves the endpoint config and the token from the two parameter paths."""

    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, config: dict):
        self._config = config
        self.token_reads = 0

    def get_parameter(self, Name, WithDecryption=False):
        if Name.endswith("/security-token"):
            assert WithDecryption, "a SecureString has to be read with decryption"
            self.token_reads += 1
            return {"Parameter": {"Value": "test-token"}}
        name = Name.rsplit("/", 1)[-1]
        return {"Parameter": {"Value": json.dumps({**self._config, "endpoint_name": name})}}

    def get_paginator(self, operation_name):
        # Stands in for the discovery an empty event triggers: two endpoints
        # configured under the prefix.
        assert operation_name == "get_parameters_by_path"
        return _FakePaginator(["/test/entsoe/endpoints/endpoint_a", "/test/entsoe/endpoints/endpoint_b"])


class _FakePaginator:
    def __init__(self, parameter_names: list[str]):
        self._parameter_names = parameter_names

    def paginate(self, Path, Recursive=False):
        return [{"Parameters": [{"Name": name} for name in self._parameter_names]}]


class _FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, Tagging=None):
        self.objects[Key] = {"body": Body, "content_type": ContentType, "tagging": Tagging}


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def wired(monkeypatch):
    config = {**json.loads(CONFIG_PATH.read_text()), "areas": ["10YSK-SEPS-----K", "10YCZ-CEPS-----N"]}
    ssm, s3 = _FakeSsm(config), _FakeS3()

    monkeypatch.setattr(handler, "_ssm_client", ssm)
    monkeypatch.setattr(handler, "_s3_client", s3)
    monkeypatch.setattr(handler, "_security_token", None)  # the cache is module state
    monkeypatch.setenv("OUTPUT_BUCKET", "test-bucket")
    monkeypatch.setenv("SSM_CONFIG_PREFIX", "/test/entsoe/endpoints")
    monkeypatch.setenv("SECURITY_TOKEN_PARAMETER", "/test/entsoe/security-token")

    body = (FIXTURES_DIR / "day_ahead_response.xml").read_bytes()
    monkeypatch.setattr(api_client.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(body))
    return ssm, s3


def test_one_run_writes_one_csv_and_one_raw_object_per_area(wired):
    _, s3 = wired

    result = handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    assert result["errors"] == []
    assert result["results"][0]["row_count"] == 6  # 3 points x 2 areas

    csv_keys = [key for key in s3.objects if key.endswith(".csv")]
    raw_keys = sorted(key for key in s3.objects if key.endswith(".xml"))
    assert len(csv_keys) == 1
    assert len(raw_keys) == 2
    # date_offset_days is +1, so a 2026-09-16 run lands under the day it targets
    assert "date=2026-09-17" in csv_keys[0]
    assert "10YCZ-CEPS-----N" in raw_keys[0] and "10YSK-SEPS-----K" in raw_keys[1]
    # only the raw objects are tagged, so the lifecycle rule can't reach the CSVs
    assert s3.objects[raw_keys[0]]["tagging"] == "data-class=raw"
    assert s3.objects[csv_keys[0]]["tagging"] is None


def test_both_areas_land_in_the_same_csv_tagged_by_request_area(wired):
    _, s3 = wired

    handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    csv_body = next(v["body"] for k, v in s3.objects.items() if k.endswith(".csv")).decode()
    assert csv_body.count("10YSK-SEPS-----K,2026-09-17T00:00:00Z") == 1
    assert csv_body.count("10YCZ-CEPS-----N,2026-09-17T00:00:00Z") == 1


def test_the_token_is_read_once_per_container_not_once_per_request(wired):
    ssm, _ = wired

    handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)
    handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    # Two invocations, two areas each: still one KMS-backed read.
    assert ssm.token_reads == 1


def test_a_failing_endpoint_does_not_sink_the_others(wired, monkeypatch):
    # An empty event scrapes every configured endpoint; one bad report should
    # cost you that report, not the whole nightly run.
    real_flatten = handler.flatten_document

    def flaky_flatten(document, endpoint_name, area=None):
        if endpoint_name == "endpoint_a":
            raise RuntimeError("boom")
        return real_flatten(document, endpoint_name, area)

    monkeypatch.setattr(handler, "flatten_document", flaky_flatten)

    result = handler.lambda_handler({"run_date": "2026-09-16"}, None)

    assert [error["endpoint_name"] for error in result["errors"]] == ["endpoint_a"]
    assert [success["endpoint_name"] for success in result["results"]] == ["endpoint_b"]
    assert "boom" in result["errors"][0]["error"]


def test_all_endpoints_failing_raises(wired, monkeypatch):
    # Nothing was written anywhere, so the invocation has to fail visibly
    # rather than return a success-shaped result full of errors.
    monkeypatch.setattr(handler, "flatten_document", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="All endpoints failed"):
        handler.lambda_handler({"run_date": "2026-09-16"}, None)


def test_an_unknown_endpoint_says_which_parameter_and_what_is_deployed(wired, monkeypatch):
    # boto3 raises ParameterNotFound with an empty message, so without this
    # the CloudWatch entry names neither the endpoint nor the parameter --
    # which is exactly what you hit invoking an endpoint still sitting in
    # config/endpoints/examples/.
    ssm, _ = wired

    def not_found(Name, WithDecryption=False):
        if Name.endswith("/security-token"):
            return {"Parameter": {"Value": "test-token"}}
        raise _FakeSsm.exceptions.ParameterNotFound()

    monkeypatch.setattr(ssm, "get_parameter", not_found)

    # It is the only endpoint in this invocation, so its failure fails the run.
    with pytest.raises(RuntimeError, match="All endpoints failed") as excinfo:
        handler.lambda_handler({"endpoint_name": "generation_actual_per_unit", "run_date": "2026-09-16"}, None)

    error = str(excinfo.value)
    assert "generation_actual_per_unit" in error
    assert "/test/entsoe/endpoints/generation_actual_per_unit" in error
    assert "endpoint_a, endpoint_b" in error  # what is actually deployed
    assert "examples/" in error
