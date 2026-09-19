# The ENTSO-E domain

Background for working on this scraper: what the platform publishes, how its
data model is shaped, and the behaviours that will surprise you. It is
written for someone who knows software but not European electricity markets.

Everything stated as fact here was verified against the live API while
building this — report names and article references were harvested from the
API's own responses (see [How the catalogue was built](#how-the-catalogue-was-built)).
Where something is *not* verified, it says so.

---

## 1. What the platform is

ENTSO-E is the European Network of Transmission System Operators for
Electricity — the association of the companies that run the high-voltage
grids. EU transparency rules oblige those operators to publish operational
data about the electricity system: how much will be generated tomorrow, how
much was actually generated, what the demand was, what power cost.

The **Transparency Platform** is where that obligation is discharged. Each
report corresponds to an article of the regulation, which is why the API's
own error messages cite things like `[14.1.C]` or `[16.1.A]` — that is the
legal clause the data exists to satisfy, and it is the most reliable way to
identify a report.

Two consequences shape everything downstream:

- **Publication is an obligation, not a service.** Data appears when the
  responsible operator submits it. A report being empty is normal and is not
  an error (see [§6](#6-behaviours-that-will-surprise-you)).
- **Coverage is uneven.** Every operator satisfies the same regulation, but
  resolution, completeness and promptness vary by country. Code that assumes
  one country's shape will break on the next.

### Two ways in, and why this project uses the API

| | Web UI (`transparency.entsoe.eu`) | RESTful API (`web-api.tp.entsoe.eu`) |
|---|---|---|
| Intended for | humans | machines |
| Interface | single-page app, internal JSON endpoints | documented `GET`, XML |
| Auth | none | `securityToken` |
| Stability | can change without notice | versioned, documented |

This project uses the **API**. An earlier iteration scraped the UI's internal
endpoints, which worked but required reverse-engineering request payloads
from the frontend's sourcemaps and tripped a WAF rule on an unrelated
integer literal. The API removes that entire class of problem.

Get a token by registering on the Transparency Platform and emailing
transparency@entsoe.eu to request RESTful API access. It arrives as a UUID
and is passed as the `securityToken` query parameter. It is a credential:
because it rides in the URL, this codebase redacts every URL before it
reaches a log line or an exception (see `api_client._redact`).

---

## 2. The request model

One endpoint. The query string selects everything:

```
GET https://web-api.tp.entsoe.eu/api
      ?securityToken=<uuid>
      &documentType=A71          # which report
      &processType=A01           # which flavour of it
      &in_Domain=10YSK-SEPS-----K  # which area (parameter name varies!)
      &periodStart=202609162200  # UTC, yyyyMMddHHmm
      &periodEnd=202609172200
```

- **`documentType`** — the report family (`A71` generation forecast, `A65`
  load, `A44` prices).
- **`processType`** — which run of it: `A01` day-ahead, `A16` realised. The
  same `documentType` with a different `processType` is a different report,
  and an invalid pairing is rejected outright.
- **The area parameter name differs per report.** Generation uses
  `in_Domain`; load uses `outBiddingZone_Domain`; prices need both
  `in_Domain` and `out_Domain`. Getting it wrong is a clean `400`:
  `Mandatory parameter outBiddingZone_Domain is missing`. This is why this
  project puts the parameter *name* in configuration rather than in code.
- **`periodStart`/`periodEnd`** — always UTC, always `yyyyMMddHHmm`, no
  separators, no zone marker.
- **Filters** are just more parameters — `psrType=B14` narrows generation to
  one production type. Verified: `A75`/`A16` for Slovakia returned 14
  TimeSeries unfiltered and 1 with `psrType=B14`.

### One area per request

Unlike the web UI, the API takes a **single** domain per call. Scraping three
countries means three requests and three documents. This project handles it
by looping areas in `api_client.fetch_endpoint` and merging the rows into one
CSV, tagged by a `request_area` column.

---

## 3. Areas and EIC codes

Everything is addressed by **EIC** (Energy Identification Code) — a 16-character
code identifying a country, bidding zone, control area or individual
generating unit. `10YSK-SEPS-----K` is Slovakia's; `SEPS` is the Slovak TSO,
and the filler dashes pad to the fixed width.

The same country exists as several *area types*, which are not
interchangeable:

| Type | Meaning |
|---|---|
| `CTY` | Country — political borders |
| `BZN` | Bidding zone — the area within which power trades at one price |
| `CTA` | Control area — what a single TSO physically operates |
| `MBA` | Market balance area — where imbalance is settled |

For many countries these coincide and share one EIC; for others (Germany
historically, the Nordics) they emphatically do not. A report is published
against one particular type, and asking for the wrong one returns nothing
rather than an error.

**The web UI prefixes the type (`CTA|10YSK-SEPS-----K`); the API does not.**
Pass the bare EIC. This codebase's config validator rejects the prefixed
form explicitly, because it was the format an earlier version used and is an
easy thing to carry over by mistake.

The authoritative code list is Appendix A of the [API
documentation](https://documenter.getpostman.com/view/7009892/2s93JtP3F6).
A valid code does not guarantee data — check a new area returns rows before
relying on it.

---

## 4. The response model

Responses are XML **market documents** in the IEC 62325 family. Three roots
matter:

| Root element | Meaning |
|---|---|
| `GL_MarketDocument` | generation and load data |
| `Publication_MarketDocument` | market data such as prices |
| `Acknowledgement_MarketDocument` | **no data, or a rejected request** |

All data documents nest identically, which is the single fact that makes a
generic scraper possible:

```xml
<GL_MarketDocument>                     <!-- document metadata -->
  <mRID>…</mRID>                        <!-- unique document id -->
  <type>A71</type>
  <process.processType>A01</process.processType>
  <createdDateTime>…</createdDateTime>
  <time_Period.timeInterval>…</time_Period.timeInterval>

  <TimeSeries>                          <!-- one per area/unit/production type -->
    <businessType>A01</businessType>
    <inBiddingZone_Domain.mRID>10YSK-SEPS-----K</inBiddingZone_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <MktPSRType>                        <!-- present only on per-unit/per-type -->
      <psrType>B14</psrType>
      <PowerSystemResources>
        <mRID>24WG--EBOG31---D</mRID>
        <name>Bohunice TG31</name>
      </PowerSystemResources>
    </MktPSRType>

    <Period>
      <timeInterval><start>…</start><end>…</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><quantity>2197.9</quantity></Point>
      …
    </Period>
  </TimeSeries>
</GL_MarketDocument>
```

### Points carry no timestamp

A `Point` has a **1-based** `position` and a value. Its instant is:

```
timestamp = period.timeInterval.start + (position - 1) × resolution
```

Rebuilding that axis is `data_processor._period_rows`. `resolution` is an
ISO-8601 duration and genuinely varies: `PT15M` and `PT60M` in practice,
`P1D`, `P1M`, `P1Y` for longer-horizon reports. Months and years need
calendar arithmetic, not a fixed offset — hence `dates.shift_by_duration`.

### `curveType` changes what a missing position means

With **`A03`** ("variable sized block") the publisher sends a point only when
the value *changes*; the previous value holds until the next position. Every
response captured for this project was `A03`.

Taking the published points literally would leave holes in an otherwise
regular series, so this project carries values forward and flags the filled
rows in a `point_carried_forward` column — filter it out to recover exactly
what was published.

### Units

`quantity_Measure_Unit.name` is `MAW` — megawatts. It is a *power* reading at
each instant, not energy consumed over the interval. Summing a day's
quarter-hourly MW values does not give MWh; multiply by the interval length
first. Prices carry `currency_Unit.name` (`EUR`) and
`price_Measure_Unit.name` (`MWH`) instead — i.e. €/MWh.

---

## 5. Report catalogue

Harvested from the live API (see [§7](#7-how-the-catalogue-was-built)). The
"Area parameter" column is the one that report actually requires.

| documentType | processType | Data item (verbatim) | Article | Area parameter |
|---|---|---|---|---|
| `A65` | `A16` | `ACTUAL_TOTAL_LOAD_R3` | 6.1.A | `outBiddingZone_Domain` |
| `A65` | `A01` | `DAY_AHEAD_TOTAL_LOAD_FORECAST_R3` | 6.1.B | `outBiddingZone_Domain` |
| `A70` | `A33` | `YEAR_AHEAD_FORECAST_MARGIN_R3` | 8.1 | `outBiddingZone_Domain` |
| `A44` | — | `ENERGY_PRICES` | 12.1.D | `in_Domain` **and** `out_Domain` |
| `A68` | `A33` | `INSTALLED_GENERATION_CAPACITY_AGGREGATED_R3` | 14.1.A | `in_Domain` |
| `A71` | `A01` | `DAY_AHEAD_AGGREGATED_GENERATION_R3` | 14.1.C | `in_Domain` |
| `A69` | `A01` | `GENERATION_FORECAST_WIND_SOLAR` | 14.1.D | `in_Domain` |
| `A73` | `A16` | `ACTUAL_GENERATION_OUTPUT_PER_UNIT_R3` | 16.1.A | `in_Domain` |
| `A75` | `A16` | `AGGREGATED_GENERATION_PER_TYPE_R3` | 16.1.B&C | `in_Domain` |
| `A74` | `A16` | `AGGREGATED_GENERATION_PER_TYPE_R3` | 16.1.B&C | `in_Domain` |
| `A72` | `A16` | `AGGREGATE_FILLING_RATE_OF_WATER_RESERVOIRS_R3` | 16.1.D | `in_Domain` |
| `A76` | — | `UNAVAILABILITY_OF_CONSUMPTION_UNITS_AGGREGATED` | 7.1.A, 7.1.B | `biddingZone_Domain` |
| `A77` / `A80` | — | `UNAVAILABILITY_OF_PRODUCTION_AND_GENERATION_UNITS` | 15.1.A–D | `biddingZone_Domain` |

The `_R3` suffix is the platform's own revision marker on the data item
name; it is part of what the API returns, not something to send. Note that
`A74` and `A75` both resolve to the same data item, and `A77` and `A80` to
another — the mapping from document type to report is not one-to-one.

`A61`/`A01` exists but additionally requires
`Contract_MarketAgreement.Type`, so it wasn't catalogued here.

The three currently scraped are `A71`/`A01`, `A73`/`A16` and `A65`/`A16`.

### Production types (`psrType`)

Observed in Slovak per-unit data, matched against the plants named in the
same response — a useful sanity check that a code means what you think:

| Code | Units carrying it (Slovakia) | Reading |
|---|---|---|
| `B02` | Nováky TG1–TG3 | lignite / brown coal |
| `B04` | Malženice TG1, PPC Bratislava TG1 | gas (combined cycle) |
| `B05` | Vojany TG5–TG6 | hard coal |
| `B10` | Čierny Váh TG1–TG6 | pumped-storage hydro |
| `B14` | Bohunice, Mochovce (10 units) | nuclear |

The full `B01`–`B25` list is in the API documentation's appendix; only the
above were observed directly, and the plant names are the evidence for the
reading given.

---

## 6. Behaviours that will surprise you

Each of these cost real time to discover. They are handled in code, with
tests, at the places named.

**HTTP 200 does not mean data.** An unpublished period returns `200` with an
`Acknowledgement_MarketDocument`, not the report. A *rejected* request
returns the same body shape with the same `Reason` code (`999`) but a `4xx`
status. **Only the status distinguishes them** — reason text is prose and
will not parse reliably. Handled in `api_client._fetch_one`, which raises
`NoDataError` for the first and `ApiClientError` for the second, because "the
auction hasn't closed yet" and "your query is malformed" need different
responses from whoever is on call.

**Resolution varies between areas in the same report.** Slovakia publishes
day-ahead generation forecasts hourly; Czechia publishes the same report at
`PT15M` and adds a second `TimeSeries` for the out-zone. One config, two
shapes. Nothing may assume hourly data or a fixed series count.

**A day is a local calendar day, but the API speaks UTC.** The CET day
2026-07-01 is `202606302200`–`202607012200` in summer and an hour later in
winter. Getting this wrong shifts every timestamp by an hour for half the
year — the failure is silent and the data looks plausible. Isolated in
`dates.day_bounds_utc`, which is the most heavily tested function here.

**Each report has a maximum query window.** Day-ahead generation rejects
anything over `P1Y` with a `400`. Irrelevant for a daily scrape; the first
thing a backfill hits.

**Day-ahead data does not exist until the auction closes.** A run scheduled
too early legitimately returns nothing. This project schedules day-ahead at
17:00 UTC and logs a `NoDataError` at `WARNING` rather than `ERROR`, so the
routine case doesn't desensitise anyone to real errors.

**The API is rate limited.** The published limit is in the API
documentation; this project's request volume (a handful per day) is nowhere
near it, and it was not tested.

---

## 7. How the catalogue was built

Rather than transcribing a code table from memory or from a third-party
reference, the catalogue in §5 was harvested from the API itself. Requesting
any report for a far-future period returns an acknowledgement whose reason
text names the data item and its article:

```
No matching data found for Data item DAY_AHEAD_AGGREGATED_GENERATION_R3
[14.1.C] (10YSK-SEPS-----K) and interval 2090-01-01T00:00:00Z/2090-01-02T00:00:00Z.
```

The probe walked candidate `documentType`/`processType` pairs, retried each
across the four domain parameter names until one stopped complaining about a
missing mandatory parameter, and recorded which parameter the report
actually wanted. That is why the "Area parameter" column can be trusted: it
is what the API asked for, not what seemed likely.

An invalid pairing answers distinguishably:

```
The combination of [DOCUMENT_TYPE=ZZZ, PROCESS_TYPE=A01] is not valid, or the
requested data is not allowed to be fetched via this service.
```

The same technique is the fastest way to check any report before writing a
config for it.

---

## See also

- [README.md](README.md) — architecture, deployment, adding an endpoint
- [lambda-doc.MD](lambda-doc.MD) — what each module and function does
- [infrastructure.MD](infrastructure.MD) — the Terraform, parameter by parameter
- [API documentation](https://documenter.getpostman.com/view/7009892/2s93JtP3F6) — the official reference, including the full code lists
