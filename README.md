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
   generic flattener  ──► CSV + raw JSON  ──►  S3 bucket
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

The two endpoints named in the task (day-ahead generation forecast, actual
generation per unit) are two different reports on the same platform, and
both return the same JSON envelope shape (`instanceList[].businessDimensionMap`
+ `curveData.periodList[].pointMap`, confirmed by inspecting real traffic
from both source pages — see "What I verified" below). Two design decisions
follow directly from that:

1. **[`lambda/src/flattener.py`](lambda/src/flattener.py)** doesn't hard-code
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

I captured and verified the **response** shape from both (see
`lambda/tests/fixtures/*.json` — real, only lightly trimmed). I could not
recover the exact **request** body the browser sends — it's not exposed by
response inspection, and a couple of attempts to reconstruct it by informed
guesswork returned a generic `500 internalServerError` rather than a useful
validation error (I did confirm no authentication is required: an
unauthenticated `GET` gets a clean `405 invalidInvocationMethod` from the
app layer, not a 401/403). So `request_template` in each shipped config file
is a **best-effort placeholder**, clearly flagged with a `_request_template_note`
field in the JSON — **you must capture the real payload before first
deploy**:

1. Open the source page (e.g. the day-ahead URL from the task) with your
   browser's DevTools Network tab open.
2. Let the page load; find the `POST` request to the `/load` (or
   `/loadOverview`) endpoint.
3. Copy its request payload.
4. Paste it into `request_template` in the matching config file, replacing
   the concrete date values the browser sent with the literal strings
   `{datetime_from}` and `{datetime_to}` (UTC instants, e.g.
   `2025-12-31T23:00:00Z`) and `{timezone}` where the timezone code appears.

If the template is wrong, the Lambda's CloudWatch Logs will show either an
HTTP error or the API's `uuAppErrorMap` verbatim (see
[`data_processor.py`](lambda/src/data_processor.py)) — it's designed to fail
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

First-time setup: before `apply`, follow "What I verified... one important
caveat" above to replace the placeholder `request_template` in
`lambda/config/endpoints/*.json` with a real captured payload for each
endpoint you want working data from.

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

1. Capture the endpoint's request payload from DevTools (see above).
2. Create `lambda/config/endpoints/<new_endpoint_name>.json` following the
   schema of the existing two files (`endpoint_name`, `method`, `url`,
   `timezone`, `date_offset_days`, `request_template`, `s3_prefix`).
3. Optionally add a schedule override for it to `endpoint_schedules` in
   `terraform.tfvars` (otherwise it gets `default_schedule_expression`).
4. `terraform apply`.

No Python or Terraform code changes are needed as long as the new endpoint
returns the same `instanceList`/`curveData`/`pointMap` envelope as the two
shipped endpoints — which holds for every report on this platform that uses
the same underlying uuApp business-report component.

## Local development

See [`lambda/README.md`](lambda/README.md).
