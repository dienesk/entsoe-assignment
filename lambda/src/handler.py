"""Lambda entrypoint: scrapes one (or every) configured ENTSO-E endpoint and
writes the results to S3.

Invocation shapes (both driven by EventBridge, configured in Terraform):
  {"endpoint_name": "generation_forecast_day_ahead"}   -> scrape just that endpoint
  {}                                                     -> scrape every endpoint
                                                            config found in SSM

Logging follows AWS's advanced logging controls for Python: the function is
configured with ``log_format = "JSON"`` in Terraform, so the runtime emits
each record as a JSON object carrying ``timestamp``, ``level``, ``message``,
``logger`` and ``requestId`` without any formatting code here. Two
consequences shape this module:

* **The log level is set on the function, not in code.** AWS's guidance is
  explicit that with JSON logs you configure the level through advanced
  logging controls (``application_log_level``) rather than ``setLevel()``,
  otherwise the code silently overrides the deployed setting. The only
  ``setLevel`` below is a local-development escape hatch, taken only when
  ``LOG_LEVEL`` is set -- which Terraform deliberately does not set.
* **Structured fields go in ``extra``.** Anything passed there becomes a
  top-level key in the JSON record, so ``endpoint_name``, ``row_count`` and
  friends are queryable in CloudWatch Logs Insights instead of being
  scraped back out of a message string.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3

from api_client import NoDataError, fetch_endpoint
from config import list_endpoint_names_from_ssm, load_endpoint_config_from_ssm, load_security_token
from csv_writer import upload_results
from data_processor import flatten_document

logger = logging.getLogger(__name__)

# Lambda owns the level when advanced logging controls are configured, so
# only override it when someone explicitly asks -- i.e. locally.
if os.environ.get("LOG_LEVEL"):
    logging.getLogger().setLevel(os.environ["LOG_LEVEL"])

_ssm_client = boto3.client("ssm")
_s3_client = boto3.client("s3")

# Read once per container rather than per endpoint: the token is static, and
# a SecureString read costs a KMS decrypt each time.
_security_token: str | None = None

# Flipped on the first invocation an execution environment serves, so a slow
# run can be attributed to initialization rather than to the API.
_cold_start = True


def _get_security_token() -> str:
    global _security_token
    if _security_token is None:
        _security_token = load_security_token(_ssm_client)
    return _security_token


def _run_one_endpoint(endpoint_name: str, run_date: date, bucket: str) -> dict[str, Any]:
    started = time.perf_counter()
    config = load_endpoint_config_from_ssm(endpoint_name, _ssm_client)

    documents = fetch_endpoint(config, run_date, _get_security_token())

    rows: list[dict[str, Any]] = []
    for document in documents:
        rows.extend(flatten_document(document.body, endpoint_name, document.area))

    target_date = run_date + timedelta(days=config["date_offset_days"])
    result = upload_results(
        _s3_client,
        bucket=bucket,
        s3_prefix=config["s3_prefix"],
        endpoint_name=endpoint_name,
        target_date=target_date,
        raw_documents=[(document.area, document.body) for document in documents],
        rows=rows,
    )

    summary = {
        "endpoint_name": endpoint_name,
        "target_date": target_date.isoformat(),
        "document_count": len(documents),
        "row_count": len(rows),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        **result,
    }
    # One record per endpoint carrying everything worth alarming on or
    # charting: an empty row_count on a run that "succeeded" is the failure
    # mode a message-only log would hide.
    logger.info("Scraped %s: %d row(s) -> s3://%s/%s", endpoint_name, len(rows), bucket, result["csv_key"], extra=summary)
    return summary


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _cold_start

    invocation_started = time.perf_counter()
    bucket = os.environ["OUTPUT_BUCKET"]
    run_date = date.fromisoformat(event["run_date"]) if event.get("run_date") else datetime.now(timezone.utc).date()

    endpoint_name = event.get("endpoint_name")
    endpoint_names = [endpoint_name] if endpoint_name else list_endpoint_names_from_ssm(_ssm_client)
    if not endpoint_names:
        raise RuntimeError("No endpoint configs found under SSM_CONFIG_PREFIX")

    cold_start, _cold_start = _cold_start, False
    logger.info(
        "Starting scrape of %d endpoint(s) for run_date=%s",
        len(endpoint_names),
        run_date,
        extra={
            "run_date": run_date.isoformat(),
            "endpoint_names": endpoint_names,
            "cold_start": cold_start,
            "function_version": getattr(context, "function_version", None),
        },
    )

    results = []
    errors = []
    for name in endpoint_names:
        try:
            results.append(_run_one_endpoint(name, run_date, bucket))
        except NoDataError as exc:
            # Expected often enough to deserve its own level: the request was
            # fine, the data simply isn't published yet. Alarming on ERROR
            # shouldn't page someone for a day-ahead run that started early.
            logger.warning("Endpoint %s has no data yet: %s", name, exc, extra={"endpoint_name": name})
            errors.append({"endpoint_name": name, "error": str(exc), "reason": "no_data"})
        except Exception as exc:  # noqa: BLE001 - one endpoint's failure shouldn't sink the rest
            # logger.exception gives the JSON record a stackTrace, errorType
            # and errorMessage, so the traceback stays queryable rather than
            # arriving as a wall of untagged text.
            logger.exception("Endpoint %s failed", name, extra={"endpoint_name": name})
            errors.append({"endpoint_name": name, "error": str(exc), "reason": "failed"})

    summary = {
        "run_date": run_date.isoformat(),
        "endpoint_count": len(endpoint_names),
        "succeeded_count": len(results),
        "failed_count": len(errors),
        "row_count": sum(result["row_count"] for result in results),
        "duration_ms": round((time.perf_counter() - invocation_started) * 1000),
    }

    if errors and not results:
        logger.error("All %d endpoint(s) failed", len(endpoint_names), extra=summary)
        raise RuntimeError(f"All endpoints failed: {errors}")

    logger.info(
        "Finished: %d/%d endpoint(s) succeeded, %d row(s) total",
        len(results),
        len(endpoint_names),
        summary["row_count"],
        extra=summary,
    )
    return {"run_date": run_date.isoformat(), "results": results, "errors": errors}
