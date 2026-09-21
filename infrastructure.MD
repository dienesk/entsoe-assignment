# Infrastructure — technical reference

Everything under `terraform/`: what each module creates, what every variable
controls, and the reasoning behind the choices that aren't obvious — chiefly
[why this uses a NAT instance instead of a NAT Gateway](#7-why-fck-nat-instead-of-a-nat-gateway).

See [lambda-doc.MD](lambda-doc.MD) for the application and
[entsoe.MD](entsoe.MD) for the data domain.

---

## Contents

1. [Layout](#1-layout)
2. [What gets created](#2-what-gets-created)
3. [Root variables](#3-root-variables)
4. [Module: network](#4-module-network)
5. [Module: storage](#5-module-storage)
6. [Module: lambda](#6-module-lambda)
7. [Why fck-nat instead of a NAT Gateway](#7-why-fck-nat-instead-of-a-nat-gateway)
8. [Module: scheduler](#8-module-scheduler)
9. [Secrets and state](#9-secrets-and-state)
10. [Outputs](#10-outputs)
11. [Operating it](#11-operating-it)

---

## 1. Layout

```
terraform/
  versions.tf     required Terraform (>= 1.9) and provider versions
  providers.tf    AWS provider, region, default tags
  variables.tf    every knob, with defaults
  main.tf         wires the four modules together
  outputs.tf      what you need after an apply
  modules/
    network/      VPC, subnets, routing, fck-nat
    storage/      S3 bucket, encryption, versioning, lifecycle
    lambda/       function, IAM, security group, log group, logging config
    scheduler/    one SSM parameter + one EventBridge rule per endpoint config
```

Split into modules by lifecycle rather than by resource type: the network
rarely changes, the scheduler changes every time an endpoint is added. Each
module takes a `name_prefix` (`<project>-<environment>`) and `tags`, so two
environments coexist in one account without collision.

`providers.tf` sets `default_tags`, so every resource is tagged
`Project`/`Environment`/`ManagedBy` without each resource repeating it.

---

## 2. What gets created

```
                         EventBridge rule (one per endpoint config)
                                   │ {"endpoint_name": "..."}
                                   ▼
   SSM Parameter Store  ──────►  Lambda function  ──────►  S3 bucket
   ├─ endpoint configs  (read)   (private subnets)         CSV + raw XML
   │  (String, one per file)           │
   └─ security token                   │ HTTPS :443
      (SecureString, NOT               ▼
       managed by Terraform)     fck-nat instance (public subnet)
                                       │
                                       ▼
                                Internet Gateway ──► web-api.tp.entsoe.eu
```

---

## 3. Root variables

### Naming and placement

| Variable | Default | What it controls |
|---|---|---|
| `aws_region` | `eu-central-1` | Region for everything. Frankfurt keeps the scraper near ENTSO-E's European infrastructure and inside the EU |
| `project` | `entsoe-scraper` | First half of `name_prefix`; prefixes every resource name and the SSM paths |
| `environment` | `dev` | Second half. Changing it gives a completely separate stack — its own VPC, bucket, parameters and schedules |

### Network

| Variable | Default | What it controls |
|---|---|---|
| `vpc_cidr` | `10.42.0.0/16` | VPC address range. Subnets are carved with `cidrsubnet(cidr, 8, n)`: `/24`s, index 0 public, 1..n private |
| `availability_zones` | `["eu-central-1a", "eu-central-1b"]` | One private subnet per AZ. **The list length is the private subnet count**; the NAT instance goes in the first AZ only |
| `fck_nat_ha_mode` | `false` | `false` = one NAT instance. `true` = autoscaling group that replaces a failed instance and re-points the route. See [§7](#7-why-fck-nat-instead-of-a-nat-gateway) |
| `fck_nat_instance_type` | `t4g.micro` | The NAT instance size. Graviton (ARM) — cheapest class that comfortably handles this traffic. Raise it only if throughput, not request count, becomes the limit |

### Lambda

| Variable | Default | What it controls |
|---|---|---|
| `lambda_timeout_seconds` | `60` | Ceiling per invocation. Sized for the worst realistic case: several areas × up to 3 attempts × a 30s HTTP timeout is bounded well under this for the endpoints shipped. Raise it before adding many areas to one config |
| `lambda_memory_mb` | `256` | Memory, and proportionally CPU. The work is IO-bound; 256 MB is comfortable for XML parsing of documents in the tens-to-hundreds of KB |
| `lambda_log_retention_days` | `30` | CloudWatch Logs retention. Log groups are **not** deleted with a function, so this is what stops logs accumulating forever |
| `lambda_application_log_level` | `INFO` | Level below which the function's own records are dropped by Lambda — before being billed or stored. Set here, not in code: see [§6](#advanced-logging-controls) |
| `lambda_system_log_level` | `WARN` | Level for the Lambda *runtime's* own records. `START`/`END`/`REPORT` lines are emitted regardless |

### Storage

| Variable | Default | What it controls |
|---|---|---|
| `s3_raw_response_expiration_days` | `90` | How long raw XML responses live. They exist so changed processing logic can reprocess without re-scraping — a reprocessing window, not an archive. The flattened CSVs are kept indefinitely |

### Secrets

| Variable | Default | What it controls |
|---|---|---|
| `security_token_parameter_name` | `null` → `/<project>/<environment>/entsoe/security-token` | Name of the SecureString parameter holding the API token. Terraform grants read access to this name and **never reads or writes the value** — see [§9](#9-secrets-and-state) |

### Scheduling

| Variable | Default | What it controls |
|---|---|---|
| `default_schedule_expression` | `cron(0 5 * * ? *)` | Applied to any endpoint config not named in `endpoint_schedules`. 05:00 UTC suits an "actuals" endpoint collecting the day that just finished |
| `endpoint_schedules` | `{generation_forecast_day_ahead = "cron(0 17 * * ? *)"}` | Per-endpoint overrides, keyed by config filename minus `.json`. Day-ahead runs at 17:00 UTC because the figures don't exist until the auction closes |

---

## 4. Module: network

One public subnet, one private subnet per AZ, and a NAT instance.

| Resource | Purpose |
|---|---|
| `aws_vpc` | DNS support and hostnames both on — required for the Lambda's ENIs to resolve the API and the AWS service endpoints |
| `aws_internet_gateway` | The public subnet's route to the internet |
| `aws_subnet.public` | Hosts the NAT instance. `map_public_ip_on_launch` so it gets a routable address |
| `aws_subnet.private[*]` | Hosts the Lambda's ENIs. No public IPs, no inbound path |
| `aws_route_table.public` | Default route → Internet Gateway |
| `aws_route_table.private` | **One table shared by all private subnets.** Its default route is written by the fck-nat module to the NAT instance's ENI |
| `module.fck_nat` | `RaJiska/terraform-aws-fck-nat`, pinned to `v1.6.1` |

`update_route_tables = true` makes the module own the private default route,
so Terraform doesn't fight it and a replaced instance re-points the route
itself.

**The shared private route table is a deliberate simplification.** One
route table means one NAT instance serving every AZ, so cross-AZ traffic
costs a little and an AZ failure that takes the NAT instance with it stops
egress entirely. A per-AZ NAT is the alternative and roughly doubles the
cost. For a scheduled batch job that retries, the trade is worth it; for a
latency-sensitive always-on workload it would not be.

---

## 5. Module: storage

One bucket, `<name_prefix>-output-<account-id>` — the account ID suffix
because bucket names are globally unique.

| Resource | Setting | Why |
|---|---|---|
| `aws_s3_bucket_public_access_block` | all four blocks on | Nothing here should ever be public |
| `aws_s3_bucket_versioning` | enabled | A re-run writes a new object; versioning means a bad deploy that overwrites good data is recoverable |
| `aws_s3_bucket_server_side_encryption_configuration` | `AES256`, bucket key on | SSE-S3 is sufficient for public market data; the bucket key reduces request costs |
| `aws_s3_bucket_lifecycle_configuration` | two rules | Below |

### Lifecycle rules

**`expire-raw-responses`** — filters on prefix `raw/` and expires after
`raw_response_expiration_days`. This is why the Lambda writes raw documents
under one fixed top-level `raw/` prefix instead of nesting `raw/` inside each
endpoint's prefix: **S3 lifecycle filters match a fixed prefix or a tag, not
a pattern**, so a per-endpoint `raw/` segment could not be selected by one
rule.

The rule also sets `noncurrent_version_expiration = 1`. Without it,
versioning defeats the rule: expiring a versioned object only writes a delete
marker, leaving the payload behind as a noncurrent version, and storage grows
without bound while appearing to be cleaned up.

**`clean-expired-delete-markers`** — removes the delete markers the first
rule leaves behind once their last noncurrent version is gone, so listings
don't fill with tombstones.

---

## 6. Module: lambda

### Packaging

`archive_file` zips `lambda/src` directly. No build step, because the
function uses only the standard library plus `boto3`, which the runtime
preinstalls. `source_code_hash` triggers redeployment when any source file
changes.

Endpoint configs are **not** bundled — they are read from SSM at invocation
time, so changing a config doesn't require repackaging the function.

### Networking

A security group allowing **egress on 443 only**, no ingress rules at all.
The function is reachable only by Lambda invoking it; nothing needs to
connect *to* it.

Running in a VPC is what makes the NAT necessary — a Lambda outside a VPC
has internet access for free. It is in the VPC so its egress goes through
infrastructure you control and can attribute, which is also what makes the
fck-nat requirement meaningful.

### IAM

`AWSLambdaVPCAccessExecutionRole` is attached for ENI management (required
for any VPC-attached function). Everything else is an inline least-privilege
policy:

| Sid | Actions | Scope |
|---|---|---|
| `WriteOwnLogGroup` | `logs:CreateLogStream`, `logs:PutLogEvents` | This function's log group only |
| `WriteScrapedData` | `s3:PutObject`, `s3:PutObjectTagging` | The output bucket only. `PutObjectTagging` is separate because the raw uploads send `x-amz-tagging`, which S3 authorises distinctly |
| `ReadEndpointConfig` | `ssm:GetParameter`, `ssm:GetParametersByPath` | The config path **and** its children. Both are listed because `GetParametersByPath` is authorised against the path queried, not only the parameters returned. Note the deliberate absence of a bare `<prefix>*` wildcard — that would also match unrelated siblings such as `<prefix>-secret` |
| `ReadSecurityToken` | `ssm:GetParameter` | Exactly the token parameter |
| `DecryptSecurityToken` | `kms:Decrypt` | The `alias/aws/ssm` key, conditioned on `kms:ViaService = ssm.<region>.amazonaws.com` so the grant only works through SSM |

A SecureString needs both the SSM read *and* the KMS decrypt; granting one
without the other fails at runtime rather than at apply.

### Advanced logging controls

```hcl
logging_config {
  log_format            = "JSON"
  application_log_level = var.application_log_level
  system_log_level      = var.system_log_level
  log_group             = aws_cloudwatch_log_group.lambda.name
}
```

`JSON` makes the runtime emit each record as a structured object with
`timestamp`, `level`, `message`, `logger` and `requestId`, and promotes
anything the code passes in `extra` to a top-level field.

It is also a prerequisite for level filtering: **Lambda cannot filter
plain-text logs by level at all.** With JSON, raising
`application_log_level` genuinely stops lower-level records being billed and
stored.

The function's environment carries **no `LOG_LEVEL`**, deliberately. AWS's
guidance is that with JSON logs the level belongs in the function
configuration, because a `setLevel()` call in code silently overrides it.
The handler only calls `setLevel` when `LOG_LEVEL` *is* present, which is the
local-development path.

The log group is created explicitly (rather than implicitly on first
invocation) so its retention is managed and `terraform destroy` removes it.

### Environment variables

| Variable | Purpose |
|---|---|
| `OUTPUT_BUCKET` | Where results go |
| `SSM_CONFIG_PREFIX` | Path under which endpoint configs are discovered |
| `SECURITY_TOKEN_PARAMETER` | Name of the token parameter |

---

## 7. Why fck-nat instead of a NAT Gateway

The task expressed a preference for [fck-nat](https://github.com/AndrewGuenther/fck-nat),
and it is the right call for this workload. The reasoning, and the honest
cost of it:

### What the two things are

A **NAT Gateway** is an AWS-managed service: you create one and AWS runs the
address translation for you, scaling bandwidth automatically and handling
failure within an AZ.

**fck-nat** is a prebuilt AMI that turns an ordinary EC2 instance into a NAT
device. Same function; you own the instance.

### The cost structure is the argument

NAT Gateway billing has **two** components: an hourly charge for every hour
the gateway exists, plus a per-GB charge for every gigabyte it processes.
Both are on top of normal data transfer costs.

A NAT instance has **one**: the EC2 hourly cost. There is no per-GB
processing charge.

Now look at the workload. This scraper runs a handful of times a day, for a
few seconds, and moves documents measured in kilobytes. The per-GB dimension
is effectively zero either way. What remains is a fixed hourly charge running
24/7 — and a NAT Gateway's hourly rate is several times that of a `t4g.micro`.
For an always-on service pushing terabytes those roles reverse and the
managed gateway earns its price; for a scheduled batch job that is idle
99.99% of the time, the gateway's fixed charge is close to pure waste.

Exact rates are region-specific and change, so check the [VPC pricing
page](https://aws.amazon.com/vpc/pricing/) rather than trusting a number
written here. The shape of the comparison — fixed hourly + per-GB versus
fixed hourly only, against a workload with negligible GB — is what decides
it, and that doesn't move.

### What you give up

This is a real trade, not a free win:

| | NAT Gateway | fck-nat instance |
|---|---|---|
| Patching | AWS | **You** — an EC2 instance you're responsible for |
| Bandwidth | scales to tens of Gbps | the instance's network limit |
| Failure handling | managed within an AZ | `ha_mode = false`: a single point of failure. `ha_mode = true`: an ASG replaces the instance and re-points the route, taking minutes |
| Blast radius | AZ-local | with one shared private route table, all AZs lose egress |

`ha_mode` defaults to `false` here because the workload is a scheduled batch
job: if egress is down for a few minutes, the run fails, logs loudly, and the
next scheduled run collects the same data. Nothing is lost that a retry
cannot recover — the API serves historical periods, so a missed day can be
backfilled by invoking with an explicit `run_date`.

**For production, flip `fck_nat_ha_mode = true` in `terraform.tfvars`.** The
cost increase is small relative to a NAT Gateway and removes the single
point of failure. If the workload ever becomes latency-sensitive or
high-volume, revisit the whole decision — the calculus genuinely inverts.

### The honest caveat

The instance is a machine in your account with a public IP that you are
responsible for keeping patched. fck-nat's AMI is minimal and purpose-built,
which limits the surface, but "no servers to patch" is a real advantage of
the managed gateway and this design forfeits it.

---

## 8. Module: scheduler

The mechanism behind "add an endpoint by configuration":

```hcl
locals {
  config_files = fileset(var.config_dir, "*.json")
  endpoint_configs = {
    for f in local.config_files :
    trimsuffix(f, ".json") => jsondecode(file("${var.config_dir}/${f}"))
  }
}
```

Every `*.json` file in `lambda/config/endpoints/` becomes three resources,
keyed by filename:

| Resource | Purpose |
|---|---|
| `aws_ssm_parameter.endpoint_config` | Type `String` (plain — configs are not secret), value is the file's JSON. This is what the Lambda reads at invocation |
| `aws_cloudwatch_event_rule.endpoint_schedule` | Schedule from `endpoint_schedules[name]`, else `default_schedule_expression` |
| `aws_cloudwatch_event_target` | Targets the function with `{"endpoint_name": "<name>"}` |
| `aws_lambda_permission.allow_eventbridge` | Lets *that specific rule* invoke the function — per-rule, not a blanket grant |

`jsondecode` at plan time means a malformed config **fails the plan**, not
the nightly run.

The glob is non-recursive (`*.json`, not `**/*.json`), so subdirectories are
not discovered.

Because the parameter's value is generated from the file, **a hand-edit in
the Parameter Store console is drift and the next apply reverts it.** That
makes console edits a legitimate way to test a query quickly, as long as the
change is folded back into the file afterwards.

---

## 9. Secrets and state

The API token is the only credential, and it is deliberately outside
Terraform's control.

Terraform never reads or writes the value: the Lambda module takes only the
parameter *name* and grants read access to it. A `data "aws_ssm_parameter"`
source would have pulled the plaintext into `terraform.tfstate`, which is
exactly what this avoids — state is a local file here, and even with a
remote backend it would mean the secret lives in two places.

Create it once:

```bash
aws ssm put-parameter \
  --name "/entsoe-scraper/dev/entsoe/security-token" \
  --type SecureString --value "<token>" --region eu-central-1
```

Consequences worth knowing:

- The parameter can be created **before or after** `apply` — the Lambda reads
  it at invocation time, so a later creation needs no redeploy.
- Rotation is `put-parameter --overwrite`. No redeploy.
- `terraform destroy` leaves it behind. Delete it with
  `aws ssm delete-parameter` if you want it gone.
- The trade-off: `apply` cannot verify it exists. A missing parameter surfaces
  at the first invocation — which is why `config.load_security_token` raises
  an error carrying the exact command to fix it.

`.gitignore` covers `*.tfstate`, `*.tfstate.*` and `terraform.tfvars`.

---

## 10. Outputs

| Output | Use |
|---|---|
| `output_bucket_name` | Where to look for results |
| `lambda_function_name` | For `aws lambda invoke` |
| `security_token_parameter_name` | The name to `put-parameter` into |
| `scraped_endpoint_names` | Which endpoints were discovered and scheduled — the quickest confirmation that a new config was picked up |
| `vpc_id`, `fck_nat_instance_public_ip` | Network debugging. The NAT IP is `null` in HA mode |

---

## 11. Operating it

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

**Adding an endpoint** — drop a JSON file in `lambda/config/endpoints/` and
apply. Confirm via `scraped_endpoint_names`.

**Changing a schedule** — edit `endpoint_schedules` in `terraform.tfvars` and
apply. Nothing redeploys; only the rule changes.

**Changing the log level** — set `lambda_application_log_level` and apply.
The function is updated in place.

**Tearing down** — `terraform destroy`. Removes the VPC, NAT instance,
bucket (and its contents, subject to versioning), function, log group and
all schedules. Leaves the token parameter.

**Cost when idle** — the NAT instance is the only thing billed by the hour.
Everything else (Lambda, EventBridge, SSM Standard parameters) costs
essentially nothing at this volume, and S3 charges for what is stored. If
you are not actively using the stack, `terraform destroy` removes the only
meaningful ongoing charge.
