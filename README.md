# entsoe-assignment

Scheduled AWS Lambda scraper for the [ENTSO-E Transparency
Platform](https://transparency.entsoe.eu/), landing data in S3 as CSV.
Infrastructure is defined entirely in Terraform, with the Lambda's internet
egress provided by [fck-nat](https://github.com/AndrewGuenther/fck-nat)
instead of a managed NAT Gateway.

## Architecture

```
EventBridge (1 schedule per endpoint config)
        │  invokes with {"endpoint_name": "..."}
        ▼
   Lambda function  ───────────────►  SSM Parameter Store
   (private subnet)   reads its           (one JSON config
        │              endpoint config     per endpoint)
        │ POST via fck-nat NAT instance (public subnet)
        ▼
 iop-transparency.entsoe.eu
        │  JSON response
        ▼
     data_processor   ──► CSV + raw JSON  ──►  S3 bucket
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
  bucket. Flattened CSVs are kept indefinitely; raw JSON responses (kept for
  reprocessing) expire after `s3_raw_json_expiration_days` (default 90).
- **EventBridge + SSM** (`terraform/modules/scheduler`) — every file in
  [`lambda/config/endpoints/`](lambda/config/endpoints) becomes one SSM
  parameter (the config the Lambda reads at invocation time) and one
  scheduled EventBridge rule that invokes the Lambda for just that endpoint.

## Why a "generic" scraper, and what that means concretely

The task asks for day-ahead generation forecast to be scraped, and for
adding the actual-generation-per-unit endpoint later to be a matter of
configuration rather than code. So day-ahead is the only *live* config;
per-unit ships pre-written and verified under
`lambda/config/endpoints/examples/`, and is enabled by moving one file (see
"Adding a new endpoint"). It isn't live by default simply because the task
only asked for day-ahead to be scraped, and every live config file becomes a
scheduled job that costs requests and storage.

That split works because the two are different reports on the same platform
returning the same JSON envelope shape
(`instanceList[].businessDimensionMap` + `curveData.periodList[].pointMap`),
confirmed by inspecting real traffic from both source pages — the captured
per-unit response is kept as a test fixture precisely so the processing code
is proven against a second, differently-shaped report. Two design decisions
follow from that:

1. **[`lambda/src/data_processor.py`](lambda/src/data_processor.py)** doesn't hard-code
   column names. It derives dimension columns from whatever keys are in
   `businessDimensionMap`, and metric columns from whatever keys are in
   `pointAttributeVariabilityMap`, so if ENTSO-E adds/renames a dimension or
   metric, the CSV's columns change accordingly instead of the scrape
   breaking. Point values are looked for in order (`value`, then `alt`, then
   a raw JSON dump as a last resort) so an unrecognized point shape degrades
   gracefully. The raw JSON response is always kept alongside the CSV
   specifically so a materially different future shape can be reprocessed
   without re-scraping.
2. **Adding a new endpoint is a configuration change, not a code change.**
   Drop a new JSON file in `lambda/config/endpoints/`, run `terraform
   apply`. See "Adding a new endpoint" below.

### What I verified before building this, and one important caveat

The URLs given in the task (e.g.
`.../generation/forecast/dayAhead?appState=...`) are not REST GET endpoints
— they're deep links into a single-page app. Using a browser, I confirmed
the page actually issues a `POST` to a companion endpoint:

| Task URL | Real request |
|---|---|
| `.../generation/forecast/dayAhead` | `POST https://iop-transparency.entsoe.eu/generation/forecast/dayAhead/load` |
| `.../generation/actual/perUnit` | `POST https://iop-transparency.entsoe.eu/generation/actual/perUnit/loadOverview` |

There is no published schema for these endpoints, so the request body was
recovered from the platform's own frontend: its webpack bundle ships
**inline sourcemaps**, so the original source can be read directly. The
authoritative builder is `getDtoInByOptions()` in
`core/contexts/data-view-data-context-provider.js`, which assembles every
data view's request identically:

```json
{
  "dateTimeRange": { "from": "2025-12-31T23:00:00Z", "to": "2026-01-01T23:00:00Z" },
  "areaList":      ["CTA|10YSK-SEPS-----K"],
  "timeZone":      "CET",
  "sorterList":    [],
  "intervalPageInfo": { "itemIndex": 0, "pageSize": 100 }
}
```

Both endpoints were then verified live: day-ahead returns `200` with 24
hourly points, per-unit returns `200` with 23 generation units. Because
*every* data view uses this one builder, the same template shape works for
any report on the platform — which is what makes the config-only
extensibility real rather than aspirational.

Three findings worth knowing before you add an endpoint:

- **`areaList` is a list** of `"<AREA_TYPE>|<EIC>"` strings, not the
  `businessDimensionMap` object that appears in the *response*. Request and
  response shapes differ; don't infer one from the other.
- **The WAF blocks `2147483647`.** The platform sits behind a Microsoft
  Azure Application Gateway that rejects that exact literal (`Integer.MAX_VALUE`)
  anywhere in the body with a `403 Forbidden` HTML page, as an
  integer-overflow attack signature. `2147483646` passes. Keep `pageSize`
  well below it — this costs hours to diagnose, because the 403 arrives
  before the application and looks nothing like an application error. There
  is a regression test pinning this.
- **Per-unit posts to `/loadOverview`**, not `/load`. Most report endpoints
  use `/load`; check the frontend's `calls` module for the exact command URI.

No authentication is required: an unauthenticated `GET` gets a clean
`405 invalidInvocationMethod` from the app layer, not a 401/403.

If a template is ever wrong, the Lambda's CloudWatch Logs show either the
HTTP error or the API's `uuAppErrorMap` verbatim (see
[`api_client.py`](lambda/src/api_client.py)) — it's designed to fail
loudly and specifically rather than silently write an empty CSV.

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
to create the resources above, Python 3.13 (for running tests locally).

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit as needed
terraform init
terraform plan
terraform apply
```

No pre-deploy steps needed: the shipped day-ahead config is verified against
the live endpoint and returns real data as-is.

Smoke test after apply:
```bash
aws lambda invoke --function-name "$(terraform -chdir=terraform output -raw lambda_function_name)" \
  --payload '{"endpoint_name": "generation_forecast_day_ahead"}' --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json
```
Then check CloudWatch Logs for the function, and
`s3://$(terraform -chdir=terraform output -raw output_bucket_name)/` for the
CSV.

To tear everything down: `terraform destroy` (from `terraform/`).

## Adding a new endpoint

Any `*.json` file directly inside `lambda/config/endpoints/` is a live
endpoint: Terraform discovers it and creates its SSM parameter, EventBridge
rule and invoke permission automatically. Files in
`lambda/config/endpoints/examples/` are deliberately **not** discovered
(the discovery glob is non-recursive), so an example can sit there ready to
use without deploying a job.

### Turning on actual generation per unit

This is the endpoint the task asks to be addable by configuration, and it
ships pre-written as an example — enabling it is a file move plus an apply,
with no code change:

```bash
mv lambda/config/endpoints/examples/generation_actual_per_unit.json \
   lambda/config/endpoints/
terraform -chdir=terraform apply
```

Its `request_template` is already verified against the live endpoint (200,
23 generation units), so no payload capture is needed. Optionally add a
schedule for it to `endpoint_schedules` in `terraform.tfvars`; otherwise it
inherits `default_schedule_expression`.

`lambda/tests/test_endpoint_extensibility.py` pins this path: it checks the
example config validates, drives the API client, and processes that
endpoint's captured response — all without touching application code.

### Any other endpoint

1. Find the endpoint's command URI in the frontend's `calls` module (e.g.
   `generation/forecast/windAndSolar/solar/load`) — or read it off the
   Network tab. The request body shape is the same for all of them.
2. Create `lambda/config/endpoints/<new_endpoint_name>.json` following the
   schema of `generation_forecast_day_ahead.json` (`endpoint_name`,
   `method`, `url`, `timezone`, `date_offset_days`, `request_template`,
   `s3_prefix`). An "actuals"-style endpoint wants a negative
   `date_offset_days` so each run collects the day that just finished.
3. Optionally add a schedule override to `endpoint_schedules` in
   `terraform.tfvars`.
4. `terraform apply`.

No Python or Terraform code changes are needed as long as the new endpoint
returns the same `instanceList`/`curveData`/`pointMap` envelope — which
holds for every report on this platform built on the same uuApp
business-report component. `lambda/tests/` includes a captured per-unit
response as a fixture, so the processing code is already proven against
that endpoint's differently-shaped payload.

## Local development

See [`lambda/README.md`](lambda/README.md).
