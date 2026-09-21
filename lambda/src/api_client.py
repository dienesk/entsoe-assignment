"""HTTP client for the ENTSO-E Transparency Platform's RESTful API.

The API is a single GET endpoint (``https://web-api.tp.entsoe.eu/api``)
whose query string selects the report: a ``documentType``/``processType``
pair, an area, and a UTC period. Each endpoint config supplies the selecting
parameters as a ``query_template``; this client adds the period bounds and
the security token at request time.

Two things worth knowing before changing anything here:

* **One area per request.** Unlike the platform's web UI, the API takes a
  single domain EIC per call, so an endpoint config listing three areas
  produces three requests and three documents. They are flattened into one
  CSV, distinguished by the ``request_area`` column.
* **HTTP 200 does not mean data.** When a report exists but has nothing for
  the requested period, the API answers ``200`` with an
  ``Acknowledgement_MarketDocument`` instead of the expected
  ``*_MarketDocument``. Rejected requests use the same acknowledgement body
  but a ``4xx`` status. That status is the only reliable discriminator --
  both carry ``Reason/code`` 999 -- so it, not the reason text, is what
  this module branches on.

The security token is a credential: it is passed as a query parameter, which
means it would otherwise end up in log lines and exception messages. Every
URL that leaves this module via a log or an exception goes through
``_redact`` first.

Log records here carry their context in ``extra`` rather than only in the
message, because the function runs with AWS's JSON log format: those keys
become top-level JSON fields, so a retry storm can be counted by
``endpoint_name`` and ``status`` in CloudWatch Logs Insights instead of
parsed back out of prose.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dates import day_bounds_utc, format_api_period_bound

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2
_TIMEOUT_SECONDS = 30

_TOKEN_PARAM_PATTERN = re.compile(r"(securityToken=)[^&\s]*")
_ACKNOWLEDGEMENT_ROOT = b"Acknowledgement_MarketDocument"
_REASON_PATTERN = re.compile(rb"<Reason>.*?<code>(.*?)</code>.*?<text>(.*?)</text>.*?</Reason>", re.DOTALL)


class ApiClientError(RuntimeError):
    """Raised when the API rejects a request or answers with an unusable body."""


class NoDataError(ApiClientError):
    """Raised when the API answers 200 with an acknowledgement instead of a report.

    Its own class because it means something operationally different from a
    rejected request: the request was well-formed but the data isn't
    published (yet). Day-ahead figures appear only once the auction closes,
    so a run scheduled too early lands here.
    """


@dataclass(frozen=True)
class FetchedDocument:
    """One area's raw XML response, kept as bytes so it is stored byte-for-byte."""

    area: str | None
    body: bytes


def _redact(text: str) -> str:
    return _TOKEN_PARAM_PATTERN.sub(r"\1***", text)


def build_query_params(config: dict[str, Any], run_date: date, area: str | None = None) -> dict[str, str]:
    """Build the query parameters for one request, minus the security token.

    ``query_template`` holds the report selectors verbatim (``documentType``,
    ``processType``, and whichever parameter carries the area for this report
    -- ``in_Domain``, ``outBiddingZone_Domain``, ...). ``{area}`` in any
    value is replaced with the area being fetched, which is why adding a
    report that keys its area differently needs no code change.

    ``periodStart``/``periodEnd`` are computed from the config's timezone and
    ``date_offset_days`` and injected unless the template set them itself.
    """
    day = run_date + timedelta(days=config["date_offset_days"])
    start_utc, end_utc = day_bounds_utc(day, config["timezone"])
    substitutions = {
        "area": area or "",
        "period_start": format_api_period_bound(start_utc),
        "period_end": format_api_period_bound(end_utc),
    }

    params = {key: str(value).format(**substitutions) for key, value in config["query_template"].items()}
    params.setdefault("periodStart", substitutions["period_start"])
    params.setdefault("periodEnd", substitutions["period_end"])
    return params


def _reason(body: bytes) -> str:
    match = _REASON_PATTERN.search(body)
    if not match:
        return body[:2000].decode("utf-8", errors="replace")
    code, text = (part.decode("utf-8", errors="replace").strip() for part in match.groups())
    return f"{code}: {text}"


def _fetch_one(url: str, endpoint_name: str, area: str | None) -> bytes:
    """GET one document, retrying transient failures.

    Network errors and 5xx are retried; 4xx is not, because a rejected
    request (bad document type, period too long, expired token) fails
    identically every time and the reason text is more useful now than three
    attempts later.
    """
    context = {"endpoint_name": endpoint_name, "area": area}
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/xml"})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
            if status < 500:
                raise ApiClientError(
                    f"[{endpoint_name}] area={area}: request rejected with HTTP {status} -- {_reason(body)}"
                ) from exc
            last_error = exc
            logger.warning(
                "[%s] HTTP %s on attempt %d/%d, retrying",
                endpoint_name,
                status,
                attempt,
                _MAX_ATTEMPTS,
                extra={**context, "status": status, "attempt": attempt, "max_attempts": _MAX_ATTEMPTS},
            )
        except urllib.error.URLError as exc:
            last_error = exc
            logger.warning(
                "[%s] network error on attempt %d/%d: %s",
                endpoint_name,
                attempt,
                _MAX_ATTEMPTS,
                _redact(str(exc)),
                extra={**context, "attempt": attempt, "max_attempts": _MAX_ATTEMPTS},
            )
        else:
            duration_ms = round((time.perf_counter() - started) * 1000)
            if _ACKNOWLEDGEMENT_ROOT in body[:500]:
                raise NoDataError(f"[{endpoint_name}] area={area}: no data published -- {_reason(body)}")
            logger.info(
                "[%s] area=%s: %d byte(s) in %d ms",
                endpoint_name,
                area,
                len(body),
                duration_ms,
                extra={**context, "status": status, "attempt": attempt, "response_bytes": len(body), "duration_ms": duration_ms},
            )
            return body

        time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    raise ApiClientError(
        f"[{endpoint_name}] area={area}: request failed after {_MAX_ATTEMPTS} attempts: {_redact(str(last_error))}"
    )


def fetch_endpoint(config: dict[str, Any], run_date: date, security_token: str) -> list[FetchedDocument]:
    """Fetch one document per configured area (or a single one if the report takes no area)."""
    endpoint_name = config["endpoint_name"]
    areas: list[str | None] = list(config.get("areas") or [None])

    documents: list[FetchedDocument] = []
    for area in areas:
        params = build_query_params(config, run_date, area)
        url = f"{config['url']}?{urllib.parse.urlencode({**params, 'securityToken': security_token})}"
        logger.info(
            "[%s] GET %s",
            endpoint_name,
            _redact(url),
            # periodStart/periodEnd are the fields worth having as their own
            # keys: nearly every "why is this empty" question is a question
            # about the window that was actually requested.
            extra={
                "endpoint_name": endpoint_name,
                "area": area,
                "period_start": params.get("periodStart"),
                "period_end": params.get("periodEnd"),
            },
        )
        documents.append(FetchedDocument(area=area, body=_fetch_one(url, endpoint_name, area)))
    return documents
