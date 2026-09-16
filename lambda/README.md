# Lambda scraper — local development

## Setup

```bash
cd lambda
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running the tests

```bash
pytest tests/ -v
```

Tests run entirely offline: `test_flattener.py` and `test_dates.py` exercise
the flattening and date-math logic against fixtures in `tests/fixtures/`,
which are (lightly trimmed) real responses captured from the ENTSO-E
Transparency Platform while researching this task — see the note at the top
of `test_flattener.py` for exactly which fixtures are real vs. synthetic.
`test_entsoe_client.py` checks request-template placeholder substitution
without making any network call.

## Module overview

| Module | Responsibility |
|---|---|
| `handler.py` | Lambda entrypoint; loops over the endpoint(s) named in the event (or every configured endpoint) |
| `config.py` | Loads an endpoint's JSON config, from SSM Parameter Store in Lambda or from `config/endpoints/*.json` for local tests |
| `dates.py` | Converts a local calendar day + timezone into the UTC instant bounds the API expects, using stdlib `zoneinfo` (handles DST correctly — see `test_day_bounds_utc_handles_dst_transition`) |
| `entsoe_client.py` | Fills in an endpoint's `request_template` placeholders and POSTs it, with retry-on-5xx and fail-fast on 4xx/`uuAppErrorMap` |
| `flattener.py` | Generic response → row-dict flattening (see the root README for the design rationale) |
| `csv_writer.py` | Row-dicts → CSV bytes; uploads CSV + raw JSON to S3 |

## Invoking locally without deploying

`handler.lambda_handler` reads endpoint config from SSM
(`config.load_endpoint_config_from_ssm`), so a full local invocation needs
either real SSM parameters (e.g. via `AWS_PROFILE=... python -c "..."` after
`terraform apply`) or a small monkeypatch in a scratch script substituting
`config.load_local_endpoint_config`. The unit tests exercise the same code
paths against local fixtures without needing either.
