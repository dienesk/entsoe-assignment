"""Turns flattened rows into CSV and uploads results (CSV + raw XML) to S3."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone
from typing import Any

# S3 keys accept far more than this, but restricting the area segment to the
# shape of an EIC code keeps keys predictable and stops a stray config value
# from inventing new path segments.
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    """Serialize rows to CSV bytes.

    The column set is the *union* of keys seen across all rows (not a fixed
    schema) so an endpoint that adds or removes a field changes the output
    columns automatically. Columns are sorted for a stable, diff-friendly
    header order across runs.
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


def build_s3_key(
    s3_prefix: str,
    endpoint_name: str,
    target_date: date,
    run_timestamp: datetime,
    extension: str,
    area: str | None = None,
) -> str:
    """Build the S3 key for one output file.

    Raw XML goes under a single top-level ``raw/`` prefix rather than a
    ``raw/`` segment nested inside each endpoint's prefix, because S3
    lifecycle rules can only filter on a fixed key prefix (or tag) -- see
    the expire-raw-xml rule in terraform/modules/storage.

    Raw keys also carry the area, because the API returns one document per
    area and all of them belong to the same run; the CSV that merges them
    carries no area in its key.
    """
    ts = run_timestamp.strftime("%Y%m%dT%H%M%SZ")
    root = RAW_PREFIX if extension == "xml" else ""
    suffix = f"_{_UNSAFE_KEY_CHARS.sub('-', area)}" if area else ""
    return f"{root}{s3_prefix.strip('/')}/date={target_date.isoformat()}/{endpoint_name}{suffix}_{ts}.{extension}"


def upload_results(
    s3_client,
    bucket: str,
    s3_prefix: str,
    endpoint_name: str,
    target_date: date,
    raw_documents: list[tuple[str | None, bytes]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upload the flattened CSV plus each area's raw XML. Returns the keys written."""
    run_timestamp = datetime.now(timezone.utc)
    csv_key = build_s3_key(s3_prefix, endpoint_name, target_date, run_timestamp, "csv")

    s3_client.put_object(
        Bucket=bucket,
        Key=csv_key,
        Body=rows_to_csv(rows),
        ContentType="text/csv",
    )

    raw_keys = []
    for area, body in raw_documents:
        raw_key = build_s3_key(s3_prefix, endpoint_name, target_date, run_timestamp, "xml", area)
        s3_client.put_object(
            Bucket=bucket,
            Key=raw_key,
            Body=body,
            ContentType="application/xml",
            # Matched by the bucket's lifecycle rule to expire raw responses
            # after N days, independent of the flattened CSVs (kept indefinitely).
            Tagging="data-class=raw",
        )
        raw_keys.append(raw_key)

    return {"csv_key": csv_key, "raw_keys": raw_keys}
