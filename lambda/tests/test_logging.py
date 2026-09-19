"""Pins the logging contract the function is deployed with.

Terraform configures the function with AWS advanced logging controls
(`log_format = "JSON"`, `application_log_level`), which means two things this
code has to hold up its end of:

1. Structured fields are passed via `extra`, becoming top-level keys in the
   JSON record. That is worth testing because `extra` is unforgiving: a key
   colliding with a reserved `LogRecord` attribute (`message`, `module`,
   `args`, ...) raises at the call site, and only when that line is reached --
   exactly the kind of bug that surfaces at 05:00 in a scheduled run.
2. The level is owned by the deployed configuration, not by a `setLevel()`
   call, so the handler must not override it unless asked locally.
"""

import importlib
import json
import logging
import sys
import urllib.error
from datetime import date
from pathlib import Path

import pytest

import api_client
import handler
from test_handler import _FakeResponse, _FakeSsm, wired  # noqa: F401 - `wired` is a fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TOKEN = "00000000-1111-2222-3333-444444444444"

# Attributes logging puts on every record itself; an `extra` key matching one
# raises KeyError. Kept explicit so the reason a test fails is obvious.
RESERVED_RECORD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName",
}


# Set onto the record by a formatter rather than by the caller, so they are
# not `extra` keys even though they aren't on a freshly built record.
_FORMATTER_ADDED_ATTRS = {"message", "asctime"}


def _extras(record: logging.LogRecord) -> dict:
    """The keys this code added, i.e. what Lambda would render as JSON fields."""
    baseline = vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in baseline and key not in _FORMATTER_ADDED_ATTRS
    }


def test_a_successful_run_logs_queryable_fields_not_just_prose(wired, caplog):
    with caplog.at_level(logging.INFO):
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    by_message = {record.getMessage().split(":")[0]: record for record in caplog.records}

    scraped = next(record for record in caplog.records if record.getMessage().startswith("Scraped"))
    extras = _extras(scraped)
    assert extras["endpoint_name"] == "generation_forecast_day_ahead"
    assert extras["row_count"] == 6
    assert extras["document_count"] == 2
    assert extras["target_date"] == "2026-09-17"
    assert extras["csv_key"].endswith(".csv")
    assert isinstance(extras["duration_ms"], int)

    finished = next(record for record in caplog.records if record.getMessage().startswith("Finished"))
    assert _extras(finished)["succeeded_count"] == 1
    assert _extras(finished)["failed_count"] == 0
    assert by_message  # guards against the loop above silently matching nothing


def test_the_first_invocation_is_marked_as_a_cold_start(wired, caplog, monkeypatch):
    monkeypatch.setattr(handler, "_cold_start", True)

    with caplog.at_level(logging.INFO):
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)
        first = next(r for r in caplog.records if r.getMessage().startswith("Starting"))
        assert _extras(first)["cold_start"] is True

        caplog.clear()
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)
        second = next(r for r in caplog.records if r.getMessage().startswith("Starting"))
        assert _extras(second)["cold_start"] is False


def test_no_data_logs_a_warning_rather_than_an_error(wired, caplog, monkeypatch):
    # Day-ahead data appears only once the auction closes, so an early run is
    # routine. Logging it at ERROR would train whoever owns the alarm to
    # ignore ERROR.
    body = (FIXTURES_DIR / "acknowledgement_no_data.xml").read_bytes()
    monkeypatch.setattr(api_client.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(body))

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="All endpoints failed"):
            handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    levels = {record.levelno for record in caplog.records if "no data" in record.getMessage().lower()}
    assert levels == {logging.WARNING}


def test_a_real_failure_logs_at_error_with_a_traceback(wired, caplog, monkeypatch):
    # exc_info is what gives the JSON record its stackTrace/errorType keys.
    monkeypatch.setattr(handler, "flatten_document", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    failed = next(record for record in caplog.records if record.getMessage().startswith("Endpoint"))
    assert failed.levelno == logging.ERROR
    assert failed.exc_info is not None
    assert _extras(failed)["endpoint_name"] == "generation_forecast_day_ahead"


def test_request_logs_carry_the_window_that_was_actually_requested(wired, caplog):
    with caplog.at_level(logging.INFO, logger="api_client"):
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    request_log = next(record for record in caplog.records if record.getMessage().startswith("[generation"))
    extras = _extras(request_log)
    assert extras["period_start"] == "202609162200"  # CEST: the CET day starts at 22:00Z
    assert extras["period_end"] == "202609172200"
    assert extras["area"] in {"10YSK-SEPS-----K", "10YCZ-CEPS-----N"}


def test_a_retry_records_the_status_and_attempt(caplog, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, _EmptyBody())
        return _FakeResponse((FIXTURES_DIR / "day_ahead_response.xml").read_bytes())

    monkeypatch.setattr(api_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: None)
    config = json.loads((Path(__file__).parent.parent / "config" / "endpoints" / "generation_forecast_day_ahead.json").read_text())

    with caplog.at_level(logging.INFO, logger="api_client"):
        api_client.fetch_endpoint(config, date(2026, 9, 16), TOKEN)

    retry = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert _extras(retry)["status"] == 503
    assert _extras(retry)["attempt"] == 1
    # and the eventual success records how long the call took
    success = next(record for record in caplog.records if "byte(s) in" in record.getMessage())
    assert _extras(success)["response_bytes"] > 0


def test_no_extra_key_collides_with_a_reserved_record_attribute(wired, caplog, monkeypatch):
    # logging.Logger.makeRecord raises KeyError("Attempt to overwrite ...")
    # when an `extra` key shadows a record attribute, at the moment that line
    # runs. Driving both the success and the failure path here means every
    # log call in the module is executed at least once, so the test fails on
    # a collision even before reaching the assertion below.
    monkeypatch.setattr(handler, "_cold_start", True)

    with caplog.at_level(logging.DEBUG):
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)
        monkeypatch.setattr(handler, "flatten_document", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    assert caplog.records
    for record in caplog.records:
        assert not RESERVED_RECORD_ATTRS & set(_extras(record)), record.getMessage()


def test_the_security_token_never_reaches_a_log_record(wired, caplog):
    with caplog.at_level(logging.DEBUG):
        handler.lambda_handler({"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-16"}, None)

    # Both the message and the structured fields, since extras are rendered too.
    assert "test-token" not in caplog.text
    assert "test-token" not in json.dumps([_extras(record) for record in caplog.records], default=str)


def test_the_handler_leaves_the_log_level_to_lambda_unless_told_otherwise(monkeypatch):
    # With JSON logs, AWS's guidance is to set the level through advanced
    # logging controls; a setLevel() in code would silently win over the
    # deployed application_log_level. Terraform sets no LOG_LEVEL for exactly
    # this reason, so import-time must be a no-op without it.
    root = logging.getLogger()
    original_level = root.level
    try:
        root.setLevel(logging.ERROR)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        importlib.reload(sys.modules["handler"])
        assert root.level == logging.ERROR, "the handler overrode a level Lambda had set"

        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        importlib.reload(sys.modules["handler"])
        assert root.level == logging.DEBUG, "the local escape hatch stopped working"
    finally:
        root.setLevel(original_level)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        importlib.reload(sys.modules["handler"])


class _EmptyBody:
    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass
