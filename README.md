# SEC EDGAR 10-K Financial Health Dashboard

Connect to SEC EDGAR, download the last ten 10-K filings for a company,
extract a unified set of XBRL financial facts, derive health metrics, and
render six charts into `financial_graphs/`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`edgar_dashboard.py` sets the SEC-required identity at import time:

```python
from edgar import set_identity
set_identity("Data Analyst analyst@example.com")   # replace with your name/email
```

> The SEC asks that you use a real contact email in the User-Agent so they can
> reach you if your requests cause problems. Update the placeholder before
> running at any volume.

## Run

```bash
python edgar_dashboard.py                 # defaults to NFLX
python edgar_dashboard.py --ticker AAPL --filings 10
```

## Output

Each ticker writes to its own sub-directory, so multiple runs never overwrite
one another:

```
financial_graphs/
├── NFLX/
│   ├── 01_revenue_vs_net_income.png
│   ├── 02_margin_trends.png
│   ├── 03_net_income_vs_operating_cash_flow.png
│   ├── 04_free_cash_flow_trajectory.png
│   ├── 05_current_ratio.png
│   ├── 06_debt_to_equity_trend.png
│   └── financial_data.csv
├── AAPL/   # same six charts + CSV
└── MSFT/   # same six charts + CSV
```

```bash
python edgar_dashboard.py --ticker NFLX
python edgar_dashboard.py --ticker AAPL
python edgar_dashboard.py --ticker MSFT
```

## Web UI (minimal)

A small local web front-end wraps the pipeline: type a ticker, watch the run,
then view or download the charts. Runs are queued and executed one at a time
(SEC is rate-limited); results persist in `financial_graphs/`, so they are
available later from the **Recent** tab. No accounts, no database.

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

Routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The UI (Run / Recent tabs) |
| `POST` | `/api/run` | `{"ticker":"NFLX","filings":10}` → queues a run |
| `GET` | `/api/status/<ticker>` | queued / running / done / error + log tail |
| `GET` | `/api/recent` | Tickers with results, newest first |
| `GET` | `/api/files/<ticker>` | Chart list for a ticker |
| `GET` | `/files/<ticker>/<name>` | View a chart inline |
| `GET` | `/download/<ticker>` | Whole folder as a `.zip` |
| `GET` | `/download/<ticker>/<name>` | One file |
| `GET` | `/healthz` | Health probe |

## Docker

```bash
docker compose up --build      # then open http://localhost:8000
```

or without Compose:

```bash
docker build -t edgar-dashboard .
docker run --rm -p 8000:8000 \
  -v "$PWD/financial_graphs:/app/financial_graphs" \
  -v edgar_cache:/root/.edgar_cache edgar-dashboard
```

Notes:

- `./financial_graphs` is bind-mounted, so results are also on the host after a
  run. The container runs as root, so those files may be root-owned; delete them
  with `sudo` or adjust the mount if that bothers you.
- `edgar_cache` / `edgar_data` are named volumes that persist edgartools' raw
  SEC downloads, making repeat runs much faster.
- The server binds `0.0.0.0:8000` inside the container; only publish the port
  to your own machine (it has no authentication).

## Metrics extracted per fiscal year

| Metric | XBRL concept(s) | Statement |
| --- | --- | --- |
| Revenue | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet` | Income |
| Cost of Revenue | `CostOfRevenue`, `CostOfGoodsAndServicesSold`, `CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization`, `CostOfGoodsSold`, `CostOfServices` | Income |
| Net Income | `NetIncomeLoss` | Income |
| Gross Profit | `GrossProfit` (fallback: `Revenues - CostOfRevenue`) | Income |
| Operating Income | `OperatingIncomeLoss` | Income |
| Operating Cash Flow | `NetCashProvidedByUsedInOperatingActivities`, `...ContinuingOperations` | Cash Flow |
| CapEx | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets` | Cash Flow |
| Current Assets | `AssetsCurrent` | Balance Sheet |
| Current Liabilities | `LiabilitiesCurrent` | Balance Sheet |
| Long-Term Debt | `LongTermDebt`, `LongTermDebtNoncurrent` | Balance Sheet |
| Stockholders' Equity | `StockholdersEquity` | Balance Sheet |

Derived in-script: **Gross Margin**, **Operating Margin**,
**Free Cash Flow** (`OperatingCashFlow - CapEx`), **Current Ratio**
(`AssetsCurrent / LiabilitiesCurrent`), **Debt-to-Equity**
(`LongTermDebt / StockholdersEquity`). All monetary values are in USD millions.

## Notes on the data

- `amendments=False` excludes `10-K/A` amendments so each fiscal year appears once.
- Duration facts are restricted to periods of **≥ 300 days**. Without this, the
  quarterly facts tagged in the notes (which share a `period_end` with the
  annual figure) can be selected instead of the full-year value.
- The current year is located by matching each fact's end date to the filing's
  `period_of_report`, **not** by the dataframe's `fiscal_year` column. For
  non-calendar fiscal years that column mislabels prior-year comparatives
  (Apple's FY2017 annual revenue is tagged `fiscal_year=2018`), which would
  silently duplicate years.
- Filings are de-duplicated by the exact `period_of_report` date, not by
  calendar year. Companies on a 52/53-week calendar can have two fiscal periods
  ending in the same calendar year (TTM Technologies has periods ending
  2024-01-01 and 2024-12-30); year-keyed de-duplication silently dropped one.
- Fiscal-year labels are numbered backwards from the newest filing's
  `dei:DocumentFiscalYearFocus`, stepping one year per ~annual period. That
  keeps the axis contiguous even when the DEI tag or the period's calendar year
  is individually unreliable (TTM's DEI focus skips 2022 and its period years
  skip 2021). The chosen year is stored in `fiscal_year`.
- `GrossProfit` is used when a filer tags it annually; otherwise it is derived
  as `Revenues - CostOfRevenue`. Netflix only tags `GrossProfit` quarterly, and
  biotech filers like Aquestive often have no cost of revenue at all, so some
  gross-margin points are legitimately absent. The CSV flags derived rows via
  `gross_profit_derived`.
- Unparseable or missing filings are skipped with a warning rather than
  aborting the run, so one bad document cannot sink a whole dashboard.
- This workflow targets US domestic **10-K** filers (US-GAAP, XBRL). Foreign
  private issuers that report under IFRS / file 20-F, and companies with no
  EDGAR annual filings at all (e.g. Volkswagen), are out of scope.

## Verification

`verify_facts.py` re-fetches the extracted facts from SEC's own XBRL API
(`data.sec.gov/api/xbrl/companyconcept/...`) and compares them to the dashboard
year by year, matching each year to the exact accession it came from:

```bash
python verify_facts.py                 # default sample
python verify_facts.py NFLX AAPL MSFT
```

It exits non-zero if any value differs. All checks pass exactly (170/170
year-values) for the tested set.
