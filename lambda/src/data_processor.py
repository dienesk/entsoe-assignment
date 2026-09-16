"""HTTP client for the ENTSO-E Transparency Platform's internal report API.

The platform's UI (https://iop-transparency.entsoe.eu/...) is a single-page
app: the URLs given in the task are deep links that make the browser POST a
JSON body to a companion ``/load`` (or ``/loadOverview``) endpoint and render
the JSON response as a table/chart. There's no documented public REST API
for this, so each endpoint config carries a ``request_template`` — the exact
JSON body captured once from the browser's DevTools Network tab (see the
root README's "Adding a new endpoint" section) — with a few placeholders
this client fills in at runtime:

  {datetime_from} / {datetime_to}   UTC instants bounding the target day,
                                     e.g. "2025-12-31T23:00:00Z"
  {timezone}                        the endpoint's configured timezone code

We confirmed (via direct probing) that these endpoints do not require
authentication: an unauthenticated GET returns a clean
``uu-app-server/invalidInvocationMethod`` error from the application layer
rather than a 401/403.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

from dates import day_bounds_utc, format_utc_instant

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2
_TIMEOUT_SECONDS = 30


class DataProcessorError(RuntimeError):
    """Raised when the API returns a non-2xx status or a populated uuAppErrorMap."""


def _fill_template(node: Any, substitutions: dict[str, str]) -> Any:
    """Recursively substitute ``{placeholder}`` tokens in string leaves of a JSON tree."""
    if isinstance(node, str):
        return node.format(**substitutions)
    if isinstance(node, dict):
        return {key: _fill_template(value, substitutions) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill_template(item, substitutions) for item in node]
    return node


def build_request_body(config: dict[str, Any], run_date: date) -> dict[str, Any]:
    day = run_date + timedelta(days=config["date_offset_days"])
    start_utc, end_utc = day_bounds_utc(day, config["timezone"])
    substitutions = {
        "datetime_from": format_utc_instant(start_utc),
        "datetime_to": format_utc_instant(end_utc),
        "timezone": config["timezone"],
    }
    return _fill_template(config["request_template"], substitutions)


def fetch_endpoint(config: dict[str, Any], run_date: date) -> dict[str, Any]:
    """POST the templated request body and return the parsed JSON response.

    Retries a small, fixed number of times on network errors and 5xx
    responses (transient); anything else (4xx, or a 2xx carrying a non-empty
    ``uuAppErrorMap``, which is how this API reports validation errors even
    with a 200 status) fails fast since retrying won't help.
    """
    body = build_request_body(config, run_date)
    encoded_body = json.dumps(body).encode("utf-8")
    endpoint_name = config["endpoint_name"]

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            config["url"],
            data=encoded_body,
            method=config.get("method", "POST"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw_body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw_body = exc.read()
            if status < 500:
                raise DataProcessorError(
                    f"[{endpoint_name}] request rejected with HTTP {status}: {raw_body[:2000]!r}"
                ) from exc
            last_error = exc
            logger.warning("[%s] HTTP %s on attempt %d/%d, retrying", endpoint_name, status, attempt, _MAX_ATTEMPTS)
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue
        except urllib.error.URLError as exc:
            last_error = exc
            logger.warning("[%s] network error on attempt %d/%d: %s", endpoint_name, attempt, _MAX_ATTEMPTS, exc)
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        payload = json.loads(raw_body)
        error_map = payload.get("uuAppErrorMap") or {}
        if error_map:
            raise DataProcessorError(f"[{endpoint_name}] API returned uuAppErrorMap: {json.dumps(error_map)[:2000]}")
        return payload

    raise DataProcessorError(f"[{endpoint_name}] request failed after {_MAX_ATTEMPTS} attempts: {last_error}")
