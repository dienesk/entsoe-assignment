# entsoe-assignment

Scheduled AWS Lambda scraper for the [ENTSO-E Transparency
Platform](https://transparency.entsoe.eu/), landing data in S3 as CSV.
Infrastructure is defined entirely in Terraform, with the Lambda's internet
egress provided by [fck-nat](https://github.com/AndrewGuenther/fck-nat)
instead of a managed NAT Gateway.

Data comes from the platform's official [RESTful
API](https://documenter.getpostman.com/view/7009892/2s93JtP3F6)
(`web-api.tp.entsoe.eu`), which needs a security token — see
[Before the first apply](#before-the-first-apply).

## Architecture

```
EventBridge (1 schedule per endpoint config)
        │  invokes with {"endpoint_name": "..."}
        ▼
   Lambda function  ───────────────►  SSM Parameter Store
   (private subnet)   reads its           ├─ one plain JSON config per endpoint
        │              config +           └─ the API token (SecureString,
        │              token                   created outside Terraform)
        │ GET via fck-nat NAT instance (public subnet), one request per area
        ▼
 web-api.tp.entsoe.eu
        │  XML market document
        ▼
     data_processor   ──► CSV + raw XML  ──►  S3 bucket
```

- **Lambda** (`lambda/src/`) — Python 3.13, standard library + `boto3` only
  (no dependency packaging/build step). Runs in private subnets; its only
  route to the internet is through the fck-nat instance.
- **fck-nat** (`terraform/modules/network`) — a single NAT *instance* (not a
  managed NAT Gateway) using the community
  [`terraform-aws-fck-nat`](https://github.com/RaJiska/terraform-aws-fck-nat)
  module, per the task's stated preference. `ha_mode = false` by default to
  keep this cheap; flip it on in `terraform.tfvars` for production use.
- **S3** (`terraform/modules/storage`) — private, versioned, encrypted
  bucket. Flattened CSVs are kept indefinitely; raw XML responses (kept for
  reprocessing) expire after `s3_raw_response_expiration_days` (default 90).
- **EventBridge + SSM** (`terraform/modules/scheduler`) — every file in
  [`lambda/config/endpoints/`](lambda/config/endpoints) becomes one SSM
  parameter (the config the Lambda reads at invocation time) and one
  scheduled EventBridge rule that invokes the Lambda for just that endpoint.

## Before the first apply

The API security token is the one thing Terraform does not create. It is
written to a SecureString SSM parameter out of band, so the credential never
enters `terraform.tfstate` — Terraform only grants the Lambda permission to
read that parameter name, and a Terraform `data` source would have pulled the
plaintext into state, which is exactly what this avoids.

Get a token by [requesting API access from
ENTSO-E](https://transparency.entsoe.eu/) (register, then email
transparency@entsoe.eu asking for RESTful API access), then:

```bash
aws ssm put-parameter \
  --name "/entsoe-scraper/dev/entsoe/security-token" \
  --type SecureString \
  --value "<your-token>" \
  --region eu-central-1
```

Or, in the console: **Systems Manager → Parameter Store → Create
parameter**, name `/entsoe-scraper/dev/entsoe/security-token`, tier Standard,
type **SecureString**, KMS key source **My current account** with the default
`alias/aws/ssm` key — that key is what the Lambda's `kms:Decrypt` grant is
scoped to, so a parameter encrypted under a customer-managed key won't be
readable without widening the policy in `terraform/modules/lambda/main.tf`.

The name is `/<project>/<environment>/entsoe/security-token` by default, and
is reported by the `security_token_parameter_name` output; override it with
the `security_token_parameter_name` variable.

The parameter can be created before or after `terraform apply` — the Lambda
reads it at invocation time, so creating it later needs no redeploy and no
re-apply, just another invoke. Rotating the token is the same command with
`--overwrite`.

## Why a "generic" scraper, and what that means concretely

The task asks for day-ahead generation forecast to be scraped, and for
adding the actual-generation-per-unit endpoint later to be a matter of
configuration rather than code. Three endpoints now ship live, each one a
single JSON file and no application code:

| Config | Report | Verified |
|---|---|---|
| `generation_forecast_day_ahead.json` | Day-ahead aggregated generation, `A71`/`A01` (article 14.1.C) | 24 hourly points, SK |
| `generation_actual_per_unit.json` | Actual generation per unit, `A73`/`A16` (article 16.1.A) | 29 generation units, SK |
| `load_actual_total.json` | Actual total load, `A65`/`A16` (article 6.1.A) | 96 quarter-hourly points, SK |

That works because they are the same kind of document —
`GL_MarketDocument` — differing only in their `documentType`/`processType`
selectors and in which fields each `TimeSeries` carries. Two design
decisions follow:

1. **[`lambda/src/data_processor.py`](lambda/src/data_processor.py) doesn't
   hard-code column names.** It walks the document and emits a column per
   leaf element it finds: document-level fields become `doc_*`, per-series
   fields (including nested ones like `MktPSRType/PowerSystemResources/name`)
   become `ts_*`, and a point's non-`position` children become the metric
   columns — `quantity` on a generation document, `price.amount` on a price
   one. If ENTSO-E adds or renames a field, the CSV's columns change
   accordingly instead of the scrape breaking. The raw XML is always kept
   alongside the CSV specifically so a materially different future shape can
   be reprocessed without re-scraping.
2. **Adding a new endpoint is a configuration change, not a code change.**
   Drop a new JSON file in `lambda/config/endpoints/`, run `terraform
   apply`. See "Adding a new endpoint" below.

### Checking both claims for yourself

The two claims above — adapts to response-structure changes, extensible by
configuration — are each pinned by a test file, so they can be checked
without deploying anything:

```bash
cd lambda && pytest tests/test_response_structure_changes.py tests/test_endpoint_extensibility.py -v
```

`test_response_structure_changes.py` takes real captured responses and
mutates them the way ENTSO-E plausibly could — renaming a metric, adding one,
removing a block, re-nesting a field, changing the resolution, adding a
series with different fields — and asserts the CSV's columns follow instead
of the run breaking or rows misaligning. Its strongest case uses a real
`A44` day-ahead price response: a `Publication_MarketDocument` rather than a
`GL_MarketDocument`, with a different namespace, a different document-level
interval element and a `price.amount` metric where generation reports carry
`quantity`. It flattens correctly with no code written for it.

That file also marks the limit of the claim: a response that has stopped
being a TimeSeries/Period/Point document is a genuine breaking change and
raises `DocumentError`, because a scrape that silently writes an empty CSV
every night is the worse failure.

`test_endpoint_extensibility.py` drives the shipped per-unit example config
through config validation, the API client and the processing code, asserting
no application code is touched on the way. To prove it end-to-end against
AWS, enable that endpoint for real (below) and invoke the function.

### What I verified, and the five things worth knowing

Both endpoints were exercised against the live API before any of this was
written: day-ahead (`A71`/`A01`) returns hourly generation forecasts,
per-unit (`A73`/`A16`) returns 29 generation units for the Slovak zone. The
trimmed fixtures in `lambda/tests/fixtures/` are those real responses.

- **HTTP 200 does not mean data.** When a report exists but has nothing
  published for the requested period, the API answers `200` with an
  `Acknowledgement_MarketDocument` instead of the expected
  `GL_MarketDocument`. Rejected requests use the *same* acknowledgement body
  and the same `Reason` code (`999`) but a `4xx` status, so the status — not
  the reason text — is the only reliable discriminator. The client branches
  on it and raises a distinct `NoDataError`, because "the auction hasn't
  closed yet" and "your query is wrong" need different responses from
  whoever is on call.
- **One area per request.** Unlike the web UI, the API takes a single domain
  EIC per call, so an endpoint config listing three areas issues three
  requests and stores three raw documents. They merge into one CSV,
  distinguished by the `request_area` column.
- **Resolution varies by area, in the same report.** Slovakia publishes
  day-ahead forecasts at `PT60M`; Czechia publishes the same report at
  `PT15M`, and adds a second `TimeSeries` for the out-zone. Nothing in the
  processing assumes hourly data or a fixed series count — the time axis is
  rebuilt per period from that period's own `resolution`.
- **`curveType` `A03` means gaps are meaningful.** A "variable sized block"
  series publishes a point only when the value *changes*; the previous value
  holds until the next position. Emitting only published points would leave
  holes in an otherwise regular series, so gaps are carried forward and
  flagged in the `point_carried_forward` column — filter it out to get back
  to exactly what was published.
- **Each report has a maximum query period.** Day-ahead generation rejects
  anything over `P1Y` with a `400`. Irrelevant for a daily scrape, but it is
  what a backfill will hit first.

Points carry a 1-based `position` and no timestamp, so each row's
`timestamp_utc` is `period start + (position - 1) * resolution`.
`periodStart`/`periodEnd` are UTC in `yyyyMMddHHmm` form, while a report's
"day" is a *local* calendar day — so the CET day 2026-07-01 is
`202606302200`..`202607012200` in summer and an hour later in winter.
[`lambda/src/dates.py`](lambda/src/dates.py) owns that conversion and is the
most heavily tested module here for that reason.

If a request is ever wrong, the Lambda's CloudWatch Logs carry the API's own
`Reason` code and text verbatim (see
[`api_client.py`](lambda/src/api_client.py)) — it's designed to fail loudly
and specifically rather than silently write an empty CSV. Request URLs are
logged with the token redacted, since it travels as a query parameter.

## Logging

The function uses [AWS advanced logging
controls](https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html):
Terraform sets `log_format = "JSON"`, so the runtime emits every record from
the standard `logging` module as a structured object carrying `timestamp`,
`level`, `message`, `logger` and `requestId` — no formatting code, no
`print`, and no logging dependency in the deployment package.

Context travels in `extra`, which Lambda promotes to top-level JSON keys, so
the facts you would actually query on are fields rather than prose:

```json
{"timestamp":"2026-09-16T17:00:04.112Z","level":"INFO","logger":"handler",
 "requestId":"3bcf5fb6-4870-4e09-a471-37a1c6882d30",
 "message":"Scraped generation_forecast_day_ahead: 24 row(s) -> s3://...",
 "endpoint_name":"generation_forecast_day_ahead","target_date":"2026-09-17",
 "document_count":1,"row_count":24,"duration_ms":412,"csv_key":"...","raw_keys":["..."]}
```

Which makes the questions worth asking one-liners in CloudWatch Logs
Insights — for example, spotting a run that "succeeded" while writing
nothing:

```
fields @timestamp, endpoint_name, row_count, duration_ms
| filter ispresent(row_count) and row_count = 0
| sort @timestamp desc
```

Three deliberate choices behind that:

- **The log level lives on the function, not in code.** AWS's guidance is
  that with JSON logs you set the level through `application_log_level`
  rather than `setLevel()`, since code would silently override whatever is
  deployed — so `lambda_application_log_level` (default `INFO`) is a
  Terraform variable, the function's environment carries no `LOG_LEVEL`, and
  the handler only calls `setLevel` when `LOG_LEVEL` *is* set, which is the
  local-development path. Level filtering is also the reason JSON format
  matters: Lambda cannot filter plain-text logs by level at all, so raising
  the level to `WARN` in a chattier environment genuinely stops those records
  being billed and stored. `lambda_system_log_level` (default `WARN`) covers
  the runtime's own records.
- **"No data yet" logs at `WARNING`, real failures at `ERROR`.** Day-ahead
  figures only exist once the auction closes, so an early run is routine;
  logging it as `ERROR` would train whoever owns the alarm to ignore `ERROR`.
- **Failures use `logger.exception`,** which gives the JSON record
  `stackTrace`, `errorType` and `errorMessage` keys, so a traceback stays
  queryable instead of arriving as untagged text.

The security token travels as a query parameter, so every URL is redacted
(`securityToken=***`) before it reaches a log line, an exception message or a
structured field. `lambda/tests/test_logging.py` pins that, along with the
level-ownership rule and the `extra` keys each path emits.

## Documentation

| Document | Covers |
|---|---|
| [entsoe.MD](entsoe.MD) | The data domain: what the platform publishes, EIC codes and area types, the market-document model, the report catalogue, and the behaviours that will surprise you |
| [lambda-doc.MD](lambda-doc.MD) | The application: every module, class and function, the config schema, the error taxonomy |
| [infrastructure.MD](infrastructure.MD) | The Terraform: every variable, each module's resources, secrets handling, and why this uses fck-nat rather than a NAT Gateway |
| [patterns.MD](patterns.MD) | Inventory of the language constructs, design patterns and conventions used — including what is deliberately absent |

## Repository layout

```
lambda/
  src/            # Lambda source (packaged as-is, no build step)
  config/endpoints/   # one JSON file per scraped endpoint (source of truth)
  tests/          # pytest, using real captured response fixtures
terraform/
  main.tf, variables.tf, outputs.tf, providers.tf, versions.tf
  modules/network/    # VPC, subnets, fck-nat
  modules/storage/    # S3 bucket
  modules/lambda/     # function, IAM, security group, log group
  modules/scheduler/  # SSM params + EventBridge rules, one per endpoint config
```

## Deploying

Prerequisites: Terraform >= 1.9, an AWS account/credentials with permission
to create the resources above, Python 3.13 (for running tests locally), and
the security token parameter from [Before the first
apply](#before-the-first-apply).

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit as needed
terraform init
terraform plan
terraform apply
```

### Testing the endpoints in the AWS console

Open the function (`entsoe-scraper-dev-scraper`) → **Test** tab → paste one
of the events below → **Test**. Each event scrapes exactly one endpoint.

`run_date` is the **run** date, not the day fetched: each config's
`date_offset_days` is applied on top. Omit it and it defaults to today (UTC),
which is what the schedule does — but for a manual test, pinning a date you
know has data separates "my change is broken" from "this data isn't
published yet".

#### 1. Day-ahead generation forecast

```json
{ "endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-19" }
```

Offset `+1`, so this fetches **2026-09-20**. Expect **24 rows** for Slovakia
— one hourly point per CET day.

Leaving `run_date` out fetches *tomorrow*, which only exists once the
day-ahead auction has closed. Verified: on the morning of 2026-09-20 a run
for 2026-09-21 returned `NoDataError`, while 2026-09-20 returned a full 24
points. This is exactly why the shipped schedule is 17:00 UTC.

#### 2. Actual generation per unit

```json
{ "endpoint_name": "generation_actual_per_unit", "run_date": "2026-09-19" }
```

Offset `-1`, so this fetches **2026-09-18**. Expect **696 rows** — 24 hourly
points for each Slovak generation unit, with the unit names arriving as
`ts_MktPSRType.PowerSystemResources.name` columns.

**This report lags by about two days.** Verified on 2026-09-20: the previous
day (2026-09-19) returned `NoDataError`, while 2026-09-18 and earlier
returned complete data. For a manual test, target three days back and you
will not be chasing a phantom.

#### 3. Actual total load

```json
{ "endpoint_name": "load_actual_total", "run_date": "2026-09-19" }
```

Offset `-1`, so this fetches **2026-09-18**. Expect **96 rows** — Slovak load
is published **quarter-hourly**, not hourly, which is a good reminder that
resolution is a property of the report and the area, never an assumption.

The most recent day may be partial: on 2026-09-20, the previous day returned
92 of 96 points while older days returned all 96.

#### All three at once

```json
{}
```

An empty event scrapes every endpoint configured in SSM — what a scheduled
rule does when no endpoint is named. Useful as one check that all three are
deployed and the token works.

Expect a **partial** result rather than a clean pass: with default dates,
day-ahead may be too early and per-unit too recent. That is the correct
behaviour — one endpoint's failure does not sink the others, so you get a
`200` with entries in `errors`. Only an all-endpoints failure raises.

### Reading the result

A successful endpoint reports the keys it wrote:

```json
{
  "run_date": "2026-09-19",
  "results": [
    {
      "endpoint_name": "generation_forecast_day_ahead",
      "row_count": 24,
      "target_date": "2026-09-20",
      "document_count": 1,
      "duration_ms": 412,
      "csv_key": "generation/forecast/day-ahead/date=2026-09-20/generation_forecast_day_ahead_20260919T170014Z.csv",
      "raw_keys": ["raw/generation/forecast/day-ahead/date=2026-09-20/generation_forecast_day_ahead_10YSK-SEPS-----K_20260919T170014Z.xml"]
    }
  ],
  "errors": []
}
```

`row_count: 0` on a run that otherwise succeeded is the case worth watching —
it means a document arrived with no points in it.

Then check **CloudWatch → Logs Insights** on the function's log group. Logs
are JSON, so the fields are queryable directly:

```
fields @timestamp, endpoint_name, area, period_start, period_end, row_count, duration_ms
| sort @timestamp desc | limit 20
```

`period_start`/`period_end` show the window actually requested, which
answers most "why is this empty" questions. Finally, the CSV lands in the
output bucket under each endpoint's `s3_prefix`
(`generation/forecast/day-ahead/`, `generation/actual/per-unit/`,
`load/actual/total/`).

### The two errors you are most likely to see

| Error | Meaning |
|---|---|
| `NoDataError: ... 999: No matching data found ...` | Request was well-formed; the data isn't published for that period. Not a bug — adjust `run_date` as described above. Logged at `WARNING`, not `ERROR` |
| `ConfigError: No config for endpoint '...'` | The config file exists locally but hasn't been applied. The message lists what *is* deployed. Run `terraform apply` |

### From the CLI instead

```bash
aws lambda invoke --function-name "$(terraform -chdir=terraform output -raw lambda_function_name)" \
  --payload '{"endpoint_name": "generation_forecast_day_ahead", "run_date": "2026-09-19"}' \
  --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
```

To tear everything down: `terraform destroy` (from `terraform/`). The token
parameter survives, since Terraform doesn't manage it; delete it with
`aws ssm delete-parameter` if you want it gone.

## Adding a new endpoint

Any `*.json` file directly inside `lambda/config/endpoints/` is a live
endpoint: Terraform discovers it and creates its SSM parameter, EventBridge
rule and invoke permission automatically. Adding one is a file plus an
apply — no Python, no Terraform:

```bash
cp lambda/config/endpoints/load_actual_total.json \
   lambda/config/endpoints/load_forecast_day_ahead.json
# edit documentType/processType, s3_prefix, date_offset_days
terraform -chdir=terraform apply
```

Optionally add a schedule to `endpoint_schedules` in `terraform.tfvars`;
otherwise it inherits `default_schedule_expression` (05:00 UTC).

`lambda/tests/test_endpoint_extensibility.py` pins this path. Its strongest
case builds a config for a report that exists in no file in this repo —
day-ahead prices, a different document family entirely — and processes a
real response from it. If adding an endpoint needed a code change, that test
could not pass.

See [entsoe.MD](entsoe.MD) for the report catalogue (document types, process
types, and which parameter each report scopes its area with), all harvested
from the live API.

### Choosing countries / areas

`areas` is a top-level config field holding bare EIC codes, so widening
coverage is a one-line edit — no template or code change:

```json
"areas": ["10YSK-SEPS-----K", "10YCZ-CEPS-----N"]
```

Each area gets its own request, and its rows are distinguished by the
`request_area` column in the CSV.

The default `10YSK-SEPS-----K` (Slovakia / SEPS) comes from the assignment's
own URL — its `appState` parameter decodes to
`{"sa":["CTA|10YSK-SEPS-----K"],"st":"CTA",...}`, where `sa` is the selected
area list. Note the API wants the bare EIC, *not* the `CTA|` prefix the web
UI uses; the config validator rejects the prefixed form with a message
saying so.

The authoritative EIC code list is Appendix A of the [API
documentation](https://documenter.getpostman.com/view/7009892/2s93JtP3F6).
One caveat: a valid code doesn't guarantee data for a given report — check a
new area returns rows before relying on it.

### Any other endpoint

1. Find the report's `documentType` and `processType` in the [API
   documentation](https://documenter.getpostman.com/view/7009892/2s93JtP3F6),
   along with which parameter carries the area (`in_Domain`,
   `outBiddingZone_Domain`, `biddingZone_Domain`, ...).
2. Create `lambda/config/endpoints/<new_endpoint_name>.json` following the
   schema of `generation_forecast_day_ahead.json` (`endpoint_name`, `url`,
   `timezone`, `date_offset_days`, `query_template`, `s3_prefix`, and
   optionally `areas`). Put the selectors in `query_template` and use
   `{area}` for whichever parameter carries the area —
   `periodStart`/`periodEnd` are added automatically. An "actuals"-style
   endpoint wants a negative `date_offset_days` so each run collects the day
   that just finished.
3. Optionally add a schedule override to `endpoint_schedules` in
   `terraform.tfvars`.
4. `terraform apply`.

No Python or Terraform code changes are needed as long as the report returns
a `TimeSeries`/`Period`/`Point` document, which holds for the whole
`*_MarketDocument` family. The processing derives its columns from the
document, so a report with different fields produces a CSV with different
columns rather than an error.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and
pull request. No AWS credentials are needed or used — three jobs, all
offline:

| Job | Checks |
|---|---|
| **Unit tests** | `pytest` on Python 3.13, the same version the function is deployed on |
| **Deployment package** | Byte-compiles `lambda/src`, imports the handler against the **real** `boto3`, and zips the package the way Terraform's `archive_file` does |
| **Terraform** | `fmt -check -recursive`, `init -backend=false`, `validate` |

The middle job earns its place: the test suite stubs `boto3` (the Lambda
runtime provides it, this repo doesn't vendor it), so nothing else would
notice a real import breaking. `-backend=false` means the Terraform job
validates configuration without touching state or AWS.

## Local development

See [`lambda/README.md`](lambda/README.md).
