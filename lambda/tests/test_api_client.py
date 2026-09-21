"""Tests for request building and the API's two failure shapes."""

import urllib.error
from datetime import date
from pathlib import Path

import pytest

import api_client
from api_client import ApiClientError, NoDataError, build_query_params, fetch_endpoint
from config import load_local_endpoint_config

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TOKEN = "00000000-1111-2222-3333-444444444444"


def test_query_params_carry_the_report_selectors_and_the_computed_period():
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    params = build_query_params(config, run_date=date(2025, 12, 31), area="10YSK-SEPS-----K")

    assert params["documentType"] == "A71"
    assert params["processType"] == "A01"
    # {area} lands in whichever parameter the config says carries it
    assert params["in_Domain"] == "10YSK-SEPS-----K"
    # date_offset_days is +1, so run_date 2025-12-31 targets the CET day 2026-01-01
    assert params["periodStart"] == "202512312300"
    assert params["periodEnd"] == "202601012300"


def test_query_params_never_carry_the_security_token():
    # The token is added only when the URL is assembled, so it cannot leak
    # into a config file or an SSM parameter by accident.
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    assert "securityToken" not in build_query_params(config, date(2025, 12, 31), "10YSK-SEPS-----K")


def test_period_bounds_follow_the_configured_timezone_across_dst():
    config = load_local_endpoint_config("generation_forecast_day_ahead")

    params = build_query_params(config, run_date=date(2026, 6, 30), area="10YSK-SEPS-----K")

    # CEST is UTC+2, so the CET day 2026-07-01 starts an hour earlier in UTC
    assert params["periodStart"] == "202606302200"
    assert params["periodEnd"] == "202607012200"


def test_negative_offset_collects_the_day_that_just_finished():
    config = {
        "endpoint_name": "some_actuals_endpoint",
        "timezone": "CET",
        "date_offset_days": -1,
        "query_template": {"documentType": "A73"},
    }

    params = build_query_params(config, run_date=date(2026, 1, 2))

    assert params["periodStart"] == "202512312300"
    assert params["periodEnd"] == "202601012300"


def test_a_template_may_set_its_own_period():
    # Injected only when the template didn't already say something, so an
    # endpoint with unusual period handling stays configurable.
    config = {
        "endpoint_name": "explicit_period",
        "timezone": "CET",
        "date_offset_days": 0,
        "query_template": {"documentType": "A71", "periodStart": "202601010000"},
    }

    params = build_query_params(config, run_date=date(2026, 1, 1))

    assert params["periodStart"] == "202601010000"
    assert params["periodEnd"] == "202601012300"  # still computed


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


def _config() -> dict:
    return load_local_endpoint_config("generation_forecast_day_ahead")


def test_fetch_returns_one_document_per_configured_area(monkeypatch):
    config = {**_config(), "areas": ["10YSK-SEPS-----K", "10YCZ-CEPS-----N"]}
    body = (FIXTURES_DIR / "day_ahead_response.xml").read_bytes()
    requested = []

    def fake_urlopen(request, timeout=None):
        requested.append(request.full_url)
        return _FakeResponse(body)

    monkeypatch.setattr(api_client.urllib.request, "urlopen", fake_urlopen)

    documents = fetch_endpoint(config, date(2025, 12, 31), TOKEN)

    # The API takes one domain per call, so two areas means two requests.
    assert [d.area for d in documents] == ["10YSK-SEPS-----K", "10YCZ-CEPS-----N"]
    assert all(TOKEN in url for url in requested)
    assert "in_Domain=10YCZ-CEPS-----N" in requested[1]


def test_acknowledgement_on_a_200_is_reported_as_missing_data(monkeypatch):
    # The API answers 200 with an acknowledgement when the report exists but
    # has nothing published for the period -- a 200 alone is not success.
    body = (FIXTURES_DIR / "acknowledgement_no_data.xml").read_bytes()
    monkeypatch.setattr(api_client.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(body))

    with pytest.raises(NoDataError) as excinfo:
        fetch_endpoint(_config(), date(2025, 12, 31), TOKEN)

    # The API's own reason survives into the message, rather than a bare status.
    assert "999" in str(excinfo.value)
    assert "No matching data found" in str(excinfo.value)


def test_a_4xx_fails_immediately_without_retrying(monkeypatch):
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, _ReasonBody(b"<Reason><code>999</code><text>nope</text></Reason>")
        )

    monkeypatch.setattr(api_client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ApiClientError) as excinfo:
        fetch_endpoint(_config(), date(2025, 12, 31), TOKEN)

    assert len(attempts) == 1  # a rejected request fails the same way every time
    assert "999: nope" in str(excinfo.value)


def test_a_5xx_is_retried_then_succeeds(monkeypatch):
    body = (FIXTURES_DIR / "day_ahead_response.xml").read_bytes()
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, _ReasonBody(b""))
        return _FakeResponse(body)

    monkeypatch.setattr(api_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: None)

    documents = fetch_endpoint(_config(), date(2025, 12, 31), TOKEN)

    assert len(calls) == 2
    assert documents[0].body == body


def test_the_security_token_is_redacted_from_logs(monkeypatch, caplog):
    body = (FIXTURES_DIR / "day_ahead_response.xml").read_bytes()
    monkeypatch.setattr(api_client.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(body))

    with caplog.at_level("INFO", logger="api_client"):
        fetch_endpoint(_config(), date(2025, 12, 31), TOKEN)

    # The token rides in the query string, so every URL that reaches a log
    # line or an exception has to be scrubbed first.
    assert caplog.text, "expected the request to be logged"
    assert TOKEN not in caplog.text
    assert "securityToken=***" in caplog.text


def test_the_security_token_is_redacted_from_failure_messages(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(f"connection refused for {request.full_url}")

    monkeypatch.setattr(api_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: None)

    with pytest.raises(ApiClientError) as excinfo:
        fetch_endpoint(_config(), date(2025, 12, 31), TOKEN)

    assert TOKEN not in str(excinfo.value)


class _ReasonBody:
    """Minimal stand-in for the file object HTTPError carries as its body."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        # HTTPError takes ownership of the body and closes it on teardown.
        pass
