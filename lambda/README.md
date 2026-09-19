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

Tests run entirely offline. `test_data_processor.py` and `test_dates.py`
exercise the document-processing and date-math logic against fixtures in
`tests/fixtures/`, which are (lightly trimmed) real ENTSO-E API responses
captured while building this — see the note at the top of
`test_data_processor.py` for exactly which fixtures are real vs. synthetic.
`test_api_client.py` checks query building, the API's two failure shapes and
token redaction with `urlopen` monkeypatched, so no network call is made.

## Module overview

| Module | Responsibility |
|---|---|
| `handler.py` | Lambda entrypoint; loops over the endpoint(s) named in the event (or every configured endpoint) |
| `config.py` | Loads an endpoint's JSON config, from SSM Parameter Store in Lambda or from `config/endpoints/*.json` for local tests; also reads the API security token from its SecureString parameter |
| `dates.py` | The two genuinely tricky bits of date handling: local calendar day + timezone → UTC instant bounds via stdlib `zoneinfo` (DST-correct — see `test_day_bounds_utc_handles_dst_transition`), and ISO-8601 duration parsing. Parsing instants and applying day offsets are stdlib one-liners done at their call sites |
| `api_client.py` | Builds each request's query string from the endpoint's `query_template`, GETs one document per area, retries on 5xx, fails fast on 4xx, and raises `NoDataError` for a 200 acknowledgement. Redacts the security token from every log line and error message |
| `data_processor.py` | XML document → row-dicts: rebuilds the time axis from `position` + `resolution`, carries `curveType` A03 blocks forward, denormalizes document/series metadata, derives every column from the document (see the root README for the design rationale) |
| `csv_writer.py` | Row-dicts → CSV bytes; uploads the merged CSV plus each area's raw XML to S3 |

Output layout in the bucket — raw responses sit under one fixed top-level
prefix so the bucket's `expire-raw-responses` lifecycle rule can select them,
since S3 lifecycle filters only match a fixed prefix (or a tag). One CSV per
run merges every area; the raw documents stay one-per-area, as fetched:

```
<s3_prefix>/date=<target_date>/<endpoint_name>_<run_ts>.csv            # kept
raw/<s3_prefix>/date=<target_date>/<endpoint_name>_<area>_<run_ts>.xml # expires
```

## Invoking locally without deploying

`handler.lambda_handler` reads config and the token from SSM, so a full
local invocation needs real parameters (e.g. `AWS_PROFILE=...` after
`terraform apply`). To drive the real API without any AWS access, wire the
pieces up directly — `load_local_endpoint_config` reads the same JSON file
Terraform would publish to SSM:

```python
import sys; sys.path.insert(0, "src")
from datetime import date
from api_client import fetch_endpoint
from config import load_local_endpoint_config
from csv_writer import rows_to_csv
from data_processor import flatten_document

config = load_local_endpoint_config("generation_forecast_day_ahead")
documents = fetch_endpoint(config, date.today(), "<your-token>")
rows = [row for d in documents for row in flatten_document(d.body, config["endpoint_name"], d.area)]
print(rows_to_csv(rows).decode())
```

The unit tests exercise the same code paths against local fixtures without
needing a token at all.
