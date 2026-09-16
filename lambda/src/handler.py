"""Lambda entrypoint: scrapes one (or every) configured ENTSO-E endpoint and
writes the results to S3.

Invocation shapes (both driven by EventBridge, configured in Terraform):
  {"endpoint_name": "generation_forecast_day_ahead"}   -> scrape just that endpoint
  {}                                                     -> scrape every endpoint
                                                            config found in SSM
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import boto3

from config import list_endpoint_names_from_ssm, load_endpoint_config_from_ssm
from csv_writer import upload_results
from dates import target_date as compute_target_date
from entsoe_client import fetch_endpoint
from flattener import flatten_response

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_ssm_client = boto3.client("ssm")
_s3_client = boto3.client("s3")


def _run_one_endpoint(endpoint_name: str, run_date: date, bucket: str) -> dict[str, Any]:
    config = load_endpoint_config_from_ssm(endpoint_name, _ssm_client)
    logger.info("Fetching endpoint %s for run_date=%s", endpoint_name, run_date)

    payload = fetch_endpoint(config, run_date)
    rows = flatten_response(payload, endpoint_name)
    logger.info("Flattened %s -> %d row(s)", endpoint_name, len(rows))

    result = upload_results(
        _s3_client,
        bucket=bucket,
        s3_prefix=config["s3_prefix"],
        endpoint_name=endpoint_name,
        target_date=compute_target_date(run_date, config["date_offset_days"]),
        raw_payload=payload,
        rows=rows,
    )
    logger.info("Uploaded %s -> s3://%s/%s", endpoint_name, bucket, result["csv_key"])
    return {"endpoint_name": endpoint_name, "row_count": len(rows), **result}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    bucket = os.environ["OUTPUT_BUCKET"]
    run_date = date.fromisoformat(event["run_date"]) if event.get("run_date") else datetime.now(timezone.utc).date()

    endpoint_name = event.get("endpoint_name")
    endpoint_names = [endpoint_name] if endpoint_name else list_endpoint_names_from_ssm(_ssm_client)
    if not endpoint_names:
        raise RuntimeError("No endpoint configs found under SSM_CONFIG_PREFIX")

    results = []
    errors = []
    for name in endpoint_names:
        try:
            results.append(_run_one_endpoint(name, run_date, bucket))
        except Exception as exc:  # noqa: BLE001 - one endpoint's failure shouldn't sink the rest
            logger.exception("Endpoint %s failed", name)
            errors.append({"endpoint_name": name, "error": str(exc)})

    if errors and not results:
        raise RuntimeError(f"All endpoints failed: {errors}")

    return {"run_date": run_date.isoformat(), "results": results, "errors": errors}
