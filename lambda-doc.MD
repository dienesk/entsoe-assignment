# Lambda application — technical reference

Every module, class and function in `lambda/src`, what it does, and why it
is shaped that way. For the electricity-market vocabulary used throughout
(`documentType`, `curveType`, EIC, `psrType`), see [entsoe.MD](entsoe.MD).
For the infrastructure that runs it, see
[infrastructure.MD](infrastructure.MD).

---

## Contents

1. [Shape of the application](#1-shape-of-the-application)
2. [Request lifecycle](#2-request-lifecycle)
3. [`handler.py`](#3-handlerpy--entrypoint-and-orchestration)
4. [`config.py`](#4-configpy--configuration-and-credentials)
5. [`api_client.py`](#5-api_clientpy--http-and-the-apis-failure-modes)
6. [`data_processor.py`](#6-data_processorpy--xml--rows)
7. [`csv_writer.py`](#7-csv_writerpy--serialisation-and-s3)
8. [`dates.py`](#8-datespy--the-time-arithmetic)
9. [Endpoint config schema](#9-endpoint-config-schema)
10. [Error taxonomy](#10-error-taxonomy)
11. [Tests](#11-tests)

---

## 1. Shape of the application

```
lambda/
  src/
    handler.py          entrypoint, orchestration, invocation-level logging
    config.py           loads endpoint configs + the API token, validates them
    api_client.py       builds and issues requests; owns the API's failure modes
    data_processor.py   XML market document -> flat row dicts
    csv_writer.py       rows -> CSV bytes; uploads CSV + raw XML to S3
    dates.py            calendar-day <-> UTC-instant conversion, ISO-8601 durations
  config/endpoints/     one JSON file per scraped endpoint (the source of truth)
  tests/                pytest; fixtures are real captured API responses
```

**No third-party dependencies.** Standard library plus `boto3`, which the
Lambda Python runtime preinstalls. There is no build step: Terraform zips
`src/` as-is. `requirements.txt` carries `pytest` for local development only.

**Flat imports.** Modules import each other by bare name (`from dates import
...`) because the deployment package puts `src/` at the root of `sys.path`.
`tests/conftest.py` reproduces that layout so tests and Lambda agree.

Two rules explain most of the design:

- **Nothing hard-codes a column name.** Output columns are derived from the
  document that arrives, so a renamed or added field changes the CSV instead
  of breaking the run.
- **Nothing hard-codes an endpoint.** Every report-specific fact lives in a
  JSON config; no module branches on which endpoint it is processing.

---

## 2. Request lifecycle

```
EventBridge  {"endpoint_name": "..."}
     │
     ▼
lambda_handler
     ├─ load_security_token      (SSM SecureString, cached per container)
     ├─ load_endpoint_config_from_ssm   (per endpoint, per invocation)
     │
     ├─ fetch_endpoint           → one GET per configured area
     │      build_query_params   → selectors + computed period
     │      _fetch_one           → retry/5xx, fail-fast/4xx, NoDataError/ack
     │
     ├─ flatten_document         → one call per document, rows concatenated
     │
     └─ upload_results           → 1 CSV (all areas) + 1 raw XML per area
```

---

## 3. `handler.py` — entrypoint and orchestration

Owns the invocation: what to scrape, in what order, what happens when one
part fails, and the structured logging around it.

### Module state

| Name | Purpose |
|---|---|
| `_ssm_client`, `_s3_client` | boto3 clients created at import so they are reused across warm invocations |
| `_security_token` | token cache; a SecureString read costs a KMS decrypt, and the token is static |
| `_cold_start` | `True` until the first invocation an execution environment serves, so a slow run can be attributed to initialisation |

At import it calls `setLevel` **only if `LOG_LEVEL` is set**. In AWS it is
not: the level is owned by Lambda's advanced logging controls, and code that
set it would silently override the deployed `application_log_level`. The env
var is the local-development escape hatch.

### `lambda_handler(event, context) -> dict`

The entrypoint. Accepts two optional event keys:

| Key | Effect |
|---|---|
| `endpoint_name` | scrape just that endpoint; omitted means every endpoint configured in SSM |
| `run_date` | ISO date; defaults to today (UTC). The *run* date — `date_offset_days` is applied on top to get the day actually fetched |

Runs each endpoint in turn and collects outcomes rather than failing at the
first problem, so one broken report does not cost you the others. Raises
`RuntimeError` only when **every** endpoint failed — a partial failure
returns normally with entries in `errors`, because something was written and
the invocation should not be retried wholesale.

Failures are sorted into two buckets, which matters operationally:

- `NoDataError` → logged at **`WARNING`**, `reason: "no_data"`. Routine: the
  data isn't published yet.
- anything else → logged at **`ERROR`** via `logger.exception`, giving the
  JSON record `stackTrace`, `errorType` and `errorMessage`.

Emits three log records per invocation: a start record
(`run_date`, `endpoint_names`, `cold_start`), one per endpoint (see below),
and a summary (`succeeded_count`, `failed_count`, `row_count`,
`duration_ms`).

**Returns**

```python
{"run_date": "2026-09-16",
 "results": [{"endpoint_name": ..., "row_count": 24, "csv_key": ..., "raw_keys": [...],
              "target_date": ..., "document_count": 1, "duration_ms": 412}],
 "errors":  [{"endpoint_name": ..., "error": "...", "reason": "no_data" | "failed"}]}
```

### `_run_one_endpoint(endpoint_name, run_date, bucket) -> dict`

One endpoint end to end: load config → fetch every area → flatten each
document → upload. Returns the summary dict above, which is also logged via
`extra` so `row_count` and `duration_ms` are queryable fields rather than
prose. A run that "succeeds" with `row_count: 0` is the failure mode a
message-only log would hide.

### `_get_security_token() -> str`

Reads the token once per container and memoises it. Only caches on success,
so a failed read isn't sticky — fix the parameter and the next invocation
picks it up with no redeploy.

---

## 4. `config.py` — configuration and credentials

Configs live in SSM (written by Terraform from the JSON files) in Lambda,
and are read from disk locally, so tests need no AWS access.

**The token is deliberately not part of any endpoint config.** Configs are
plain-text SSM parameters generated from files in this repository; the token
lives in its own SecureString parameter created outside Terraform.

### `validate_endpoint_config(config) -> dict`

Checks required fields (`endpoint_name`, `url`, `timezone`,
`date_offset_days`, `query_template`, `s3_prefix`), that `query_template` is
a non-empty object, and that it does **not** contain `securityToken` — which
would put a credential in a plain-text parameter and in git. Returns the
config so it can wrap a load call.

Validation exists because the alternative failure is worse: a malformed
config deploys as a scheduled job that fails every night at 05:00, and the
API's rejection (`HTTP 400`, `Reason 999`) doesn't say which field was wrong.

### `_validate_areas(config) -> None`

`areas` is optional — not every report is area-scoped — but if present must
be a non-empty list of non-empty strings. Rejects the `CTA|10YSK-SEPS-----K`
form explicitly with a message naming the bare-EIC alternative: that is the
web UI's format and what an earlier version of this scraper used, so it is
an easy thing to carry over.

### `load_endpoint_config_from_ssm(endpoint_name, ssm_client, prefix=None) -> dict`

Reads `<prefix>/<endpoint_name>` and validates it. Catches
`ParameterNotFound` — which boto3 raises with an *empty* message — and
re-raises a `ConfigError` naming the endpoint, the parameter, and what is
actually deployed. Without this the CloudWatch entry identifies nothing.

### `_describe_available(ssm_client, prefix) -> str`

Best-effort list of deployed endpoints for that error message. Swallows any
exception: a failing diagnostic must not replace the real error.

### `list_endpoint_names_from_ssm(ssm_client, prefix=None) -> list[str]`

Paginated, **non-recursive** listing under the prefix — this is what an
empty `{}` event scrapes.

### `load_security_token(ssm_client, parameter_name=None) -> str`

Reads the SecureString with `WithDecryption=True`. A missing parameter means
the one manual deployment step was skipped, so the error carries the exact
`aws ssm put-parameter` command rather than a bare `ParameterNotFound`.

### `load_local_endpoint_config` / `list_local_endpoint_names`

Disk equivalents for tests and local runs. `load_local_endpoint_config` is
the mirror of the SSM read, so a file that loads here is a parameter that
loads there.

---

## 5. `api_client.py` — HTTP and the API's failure modes

Uses `urllib.request` rather than `requests` to keep the package
dependency-free.

### `FetchedDocument`

Frozen dataclass of `area: str | None` and `body: bytes`. The body stays
**bytes** all the way to S3 so the archived document is byte-for-byte what
the API returned — re-encoding would make it a paraphrase.

### `build_query_params(config, run_date, area=None) -> dict[str, str]`

Builds the query string minus the token. `query_template` values are
formatted against `{area}`, `{period_start}` and `{period_end}`, then
`periodStart`/`periodEnd` are injected unless the template set them itself.

`{area}` lives in the template because **which parameter carries the area
differs per report** — `in_Domain` for generation, `outBiddingZone_Domain`
for load. Putting the parameter *name* in configuration is what makes whole
report families reachable without touching this module.

### `fetch_endpoint(config, run_date, security_token) -> list[FetchedDocument]`

One request per configured area (or a single request if the report has no
`areas`), because the API takes one domain per call. Logs each request with
`period_start`/`period_end` as structured fields — most "why is this empty"
questions are questions about the window actually requested.

### `_fetch_one(url, endpoint_name, area) -> bytes`

The retry and failure policy:

| Outcome | Behaviour | Why |
|---|---|---|
| network error | retry, linear backoff, 3 attempts | transient |
| `5xx` | retry | transient |
| `4xx` | **fail immediately** | a malformed request fails identically every time; the reason text is more useful now than three attempts later |
| `200` + `Acknowledgement_MarketDocument` | raise `NoDataError` | well-formed request, nothing published |
| `200` + report | return body | |

The acknowledgement check is the critical one: both "no data" and "rejected"
arrive with `Reason` code `999`, and **only the HTTP status separates them**,
so this branches on status rather than parsing prose.

### `_redact(text) -> str`

Replaces `securityToken=…` with `securityToken=***`. The token is a
credential travelling in a URL, so every URL that reaches a log line or an
exception passes through here. Pinned by tests that assert the token appears
in neither messages nor structured fields.

### `_reason(body) -> str`

Extracts `Reason/code` and `Reason/text` from an acknowledgement so the
API's own explanation survives into the exception, falling back to a
truncated body when the pattern doesn't match.

---

## 6. `data_processor.py` — XML → rows

Turns a market document into flat row dicts. Four jobs: rebuild the time
axis, fill variable-sized blocks, denormalise metadata, and derive column
names from the document.

### Column derivation

| Source | Prefix | Example |
|---|---|---|
| document-level leaf elements | `doc_` | `doc_type`, `doc_mRID`, `doc_createdDateTime` |
| `TimeSeries` leaf elements, dotted when nested | `ts_` | `ts_curveType`, `ts_MktPSRType.PowerSystemResources.name` |
| `Point` children other than `position` | *(none)* | `quantity`, `price.amount` |
| added by this module | — | `endpoint_name`, `request_area`, `timestamp_utc`, `point_position`, `point_carried_forward` |

The metric column is named after the element inside `Point`, which is why a
generation document yields `quantity` and a price document yields
`price.amount` with no code aware of either.

`sender_MarketParticipant` and `receiver_MarketParticipant` are dropped —
they are ENTSO-E itself on every document of every type. `doc_mRID` and
`doc_createdDateTime` survive, and are what actually tie a row back to the
response it came from.

### `flatten_document(document, endpoint_name, area=None) -> list[dict]`

Entry point. Parses, collects `doc_` columns once, then walks each
`TimeSeries` and each `Period` within it. A document with no `TimeSeries`
yields no rows — a real, if empty, answer. Malformed XML raises
`DocumentError`, because silently writing an empty CSV every night is the
worse failure.

### `_period_rows(period, curve_type, base_row) -> list[dict]`

One row per (period, position). Timestamp is
`start + (position - 1) × resolution` — points carry no timestamp of their
own.

Handles `curveType` **`A03`**, where the publisher sends a point only when
the value changes and the previous value holds until the next position.
Gaps are carried forward and flagged in `point_carried_forward`, so filtering
that column recovers exactly what was published. For other curve types only
published positions are emitted.

### `_slot_count(start, end, resolution) -> int`

How many whole resolution steps fit the period. **Stepped rather than
divided**, because `P1M`/`P1Y` have no fixed length. Guarded against a
zero-length resolution (which would never advance) and capped at
`_MAX_POINTS_PER_PERIOD`.

### `_leaf_values(element, exclude) -> dict[str, str]`

Depth-first walk producing `{dotted.path: text}` for every leaf, skipping the
container tags handled separately (`TimeSeries`, `Period`). Repeated sibling
tags collapse onto one column — acceptable for the metadata blocks this is
used on, where each tag appears at most once.

### `_points_by_position(period)` / `_local_name(tag)`

Index a period's points by position; strip the XML namespace
(`{urn:iec62325…}TimeSeries` → `TimeSeries`) so documents from different
namespaces are handled identically. That is what lets a
`Publication_MarketDocument` flow through code written against
`GL_MarketDocument`.

---

## 7. `csv_writer.py` — serialisation and S3

### `rows_to_csv(rows) -> bytes`

Columns are the **union** of keys across all rows, sorted for a stable,
diff-friendly header. The union matters: areas and report types differ in
which fields they publish, and a row missing a column another row has must
not misalign the file. Empty input yields `b""` rather than a bare header.

### `build_s3_key(s3_prefix, endpoint_name, target_date, run_timestamp, extension, area=None) -> str`

```
<s3_prefix>/date=<target_date>/<endpoint_name>_<run_ts>.csv            # kept
raw/<s3_prefix>/date=<target_date>/<endpoint_name>_<area>_<run_ts>.xml # expires
```

Raw documents go under a single top-level `raw/` prefix rather than a `raw/`
segment nested per endpoint, because **S3 lifecycle rules can only filter on
a fixed prefix or a tag** — the expiry rule could not otherwise select them.
The area appears in raw keys because the API returns one document per area;
the CSV merges them and carries no area in its key. Area values are sanitised
so a stray config value cannot invent path segments.

### `upload_results(...) -> dict`

Writes one CSV plus one raw XML per area. Raw objects are tagged
`data-class=raw`, matching the bucket's lifecycle rule; the CSV is untagged
and kept indefinitely. Returns `{"csv_key": ..., "raw_keys": [...]}`.

---

## 8. `dates.py` — the time arithmetic

The genuinely tricky part, isolated and heavily tested. A mistake here
shifts every timestamp by an hour for half the year, and the output still
looks plausible.

### `day_bounds_utc(local_day, tz_name) -> tuple[datetime, datetime]`

UTC `[start, end)` instants for one **local** calendar day, via stdlib
`zoneinfo` so DST is handled by the tz database rather than by arithmetic.
The CET day 2026-01-01 is `2025-12-31T23:00Z`–`2026-01-01T23:00Z`; the CET
day 2026-07-01 starts an hour earlier in UTC.

### `format_api_period_bound(instant) -> str`

The API's `periodStart`/`periodEnd` spelling: `yyyyMMddHHmm`, UTC, no
separators, no zone marker.

### `format_utc_instant(instant) -> str`

ISO-8601 with a literal `Z`, for the `timestamp_utc` column.

### `_require_utc(instant) -> datetime`

Rejects naive or non-UTC input. Both formatters state UTC implicitly — one
with a `Z`, one by omitting the zone — so formatting a CET instant would
silently mislabel it.

### `parse_iso8601_duration(duration) -> dict[str, int]`

Parses `PT15M`, `PT60M`, `P1D`, `P1M`, `P1Y`. Note `M` means *months* before
the `T` and *minutes* after it. Raises on anything unrecognised rather than
guessing, since a wrong resolution corrupts every timestamp in the output.

### `shift_by_duration(instant, duration, multiplier) -> datetime`

Advances by whole steps. Years and months use calendar arithmetic (a `P1Y`
series must land on the same calendar date, not 365 days later), clamping to
month end where the day doesn't exist — 31 Jan + 1 month → 28 Feb.

---

## 9. Endpoint config schema

One JSON file per endpoint in `lambda/config/endpoints/`. Terraform turns
each into an SSM parameter and an EventBridge schedule.

| Field | Required | Meaning |
|---|---|---|
| `endpoint_name` | ✅ | Must equal the filename minus `.json` — Terraform keys the parameter and rule off the filename, the Lambda reports itself by this |
| `url` | ✅ | `https://web-api.tp.entsoe.eu/api` |
| `timezone` | ✅ | IANA/abbrev zone defining the report's calendar day, e.g. `CET` |
| `date_offset_days` | ✅ | Days from run date to target day. `+1` day-ahead, `-1` actuals |
| `query_template` | ✅ | Report selectors sent verbatim. `{area}` substituted; `periodStart`/`periodEnd` added automatically; `securityToken` rejected |
| `s3_prefix` | ✅ | Output prefix; must be unique across endpoints |
| `areas` | — | Bare EIC codes, one request each. Omit for reports with no area |
| `description`, `_*_note` | — | Documentation; ignored by the loader |

Keys beginning `_` are conventional inline notes and are simply carried
through.

---

## 10. Error taxonomy

| Exception | Raised by | Means | Operator action |
|---|---|---|---|
| `ConfigError` | `config.py` | Missing/invalid config, missing token parameter | Message names the parameter and the fix |
| `NoDataError` | `api_client.py` | `200` + acknowledgement: nothing published yet | Usually none; retry later or with an earlier `run_date` |
| `ApiClientError` | `api_client.py` | `4xx`, or retries exhausted | Check `documentType`/`processType`/area parameter |
| `DocumentError` | `data_processor.py` | Response isn't a parseable market document | Genuine breaking change upstream |
| `RuntimeError` | `handler.py` | Every endpoint failed | See the per-endpoint errors in the message |

`NoDataError` subclasses `ApiClientError`, so `except ApiClientError` still
catches both — the split exists to let the handler log them at different
levels.

---

## 11. Tests

73 tests, no network, no AWS. Fixtures in `tests/fixtures/` are real captured
API responses, trimmed; `variable_block_response.xml` is the one synthetic
file, because every captured response happened to be gap-free.

| File | Covers |
|---|---|
| `test_dates.py` | DST boundaries, duration parsing, calendar arithmetic |
| `test_api_client.py` | Query building, retry policy, both failure shapes, token redaction |
| `test_data_processor.py` | Flattening real day-ahead and per-unit responses, carry-forward, malformed input |
| `test_response_structure_changes.py` | Renamed/added/removed/re-nested fields, changed resolution, a different document family — the "adapts to schema change" claim |
| `test_endpoint_extensibility.py` | Config-only extensibility, including a report that exists in no file |
| `test_csv_writer.py` | Key layout, column union, key sanitisation |
| `test_handler.py` | Orchestration wiring, token caching, failure isolation |
| `test_logging.py` | Structured `extra` fields, level ownership, reserved-attribute collisions |

```bash
cd lambda && pytest -q
```

`conftest.py` puts `src/` on `sys.path` and stubs `boto3`, which the runtime
provides but this repo does not vendor.
