"""Turns flattened rows into CSV and uploads results (CSV + raw JSON) to S3."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone
from typing import Any


def rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    """Serialize rows to CSV bytes.

    The column set is the *union* of keys seen across all rows (not a fixed
    schema) so an endpoint that adds or removes a dimension/metric changes
    the output columns automatically. Columns are sorted for a stable,
    diff-friendly header order across runs.
    """
    if not rows:
        return b""

    fieldnames: list[str] = sorted({key for row in rows for key in row.keys()})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


RAW_PREFIX = "raw/"


def build_s3_key(s3_prefix: str, endpoint_name: str, target_date: date, run_timestamp: datetime, extension: str) -> str:
    """Build the S3 key for one output file.

    Raw JSON goes under a single top-level ``raw/`` prefix rather than a
    ``raw/`` segment nested inside each endpoint's prefix, because S3
    lifecycle rules can only filter on a fixed key prefix (or tag) -- see
    the expire-raw-json rule in terraform/modules/storage.
    """
    ts = run_timestamp.strftime("%Y%m%dT%H%M%SZ")
    root = RAW_PREFIX if extension == "json" else ""
    return f"{root}{s3_prefix.strip('/')}/date={target_date.isoformat()}/{endpoint_name}_{ts}.{extension}"


def upload_results(
    s3_client,
    bucket: str,
    s3_prefix: str,
    endpoint_name: str,
    target_date: date,
    raw_payload: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    """Upload both the flattened CSV and the raw JSON response to S3. Returns the two keys written."""
    run_timestamp = datetime.now(timezone.utc)
    csv_key = build_s3_key(s3_prefix, endpoint_name, target_date, run_timestamp, "csv")
    raw_key = build_s3_key(s3_prefix, endpoint_name, target_date, run_timestamp, "json")

    s3_client.put_object(
        Bucket=bucket,
        Key=csv_key,
        Body=rows_to_csv(rows),
        ContentType="text/csv",
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=raw_key,
        Body=json.dumps(raw_payload).encode("utf-8"),
        ContentType="application/json",
        # Matched by the bucket's lifecycle rule to expire raw responses
        # after N days, independent of the flattened CSVs (kept indefinitely).
        Tagging="data-class=raw",
    )
    return {"csv_key": csv_key, "raw_key": raw_key}
