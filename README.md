# SEC EDGAR 10-K Financial Health Dashboard

Connect to SEC EDGAR, download the last ten 10-K filings for a company,
extract a unified set of XBRL financial facts, derive health metrics, and
render nine charts into `financial_graphs/`. The run form accepts 3, 5, 10 or
20 filings.

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
│   ├── 07_income_statement.png
│   ├── 08_balance_sheet.png
│   ├── 09_debt_overview.png
│   ├── 10_cash_flow.png
│   ├── 11_segments.png
│   ├── financial_data.csv   # per-year ratio inputs/outputs
│   ├── statements.csv       # every dimension-free primary-statement line
│   ├── facts.csv            # every fact for each period, dimensions included
│   └── notes.csv            # key schedules: debt, segments, EPS, taxes, shares
├── AAPL/   # same charts + CSVs
└── MSFT/   # same charts + CSVs
```

`statements.csv` / `facts.csv` / `notes.csv` are the data-first, XBRL-driven
artifacts: no metric is hard-coded, so a new filer needs no code change. The
charts are views over that data (`07`–`11` are generated from whatever the
statement contains; `01`–`06` are ratios over a stable set of standard
concepts).

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
| `GET` | `/api/identity` | Current SEC identity (name + email) |
| `POST` | `/api/identity` | `{"identity":"Jane Doe jane@example.com"}` → save it |
| `GET` | `/api/recent` | Tickers with results, newest first |
| `GET` | `/api/files/<ticker>` | Chart list for a ticker |
| `GET` | `/files/<ticker>/<name>` | View a chart inline |
| `GET` | `/download/<ticker>` | Whole folder as a `.zip` |
| `GET` | `/download/<ticker>/<name>` | One file |
| `GET` | `/healthz` | Health probe |

## Docker

A single service that publishes one HTTP port:

> **Windows users:** see **[WINDOWS.md](WINDOWS.md)** for a from-scratch guide
> (Docker Desktop + WSL2) — no Python or Git required. After Docker is installed,
> double-click `run-windows.cmd` and it creates `.env`, builds, starts, and opens
> the browser.

```bash
docker compose up -d --build     # http://localhost:${HTTP_PORT:-8000}
```

### Dockge

1. Put this project (or at least `docker-compose.yml` + `.env`) in a stack
   directory Dockge manages.
2. `cp .env.example .env` and edit it:
   - `EDGAR_IDENTITY` — your name/email (the SEC requires a real contact).
   - `HTTP_PORT` — the host port to publish (default 8000); or just edit the
     `ports:` line in the compose file directly.
3. Deploy in Dockge. If the compose file is not next to the Dockerfile, point
   the build at the repo:

   ```yaml
   services:
     dashboard:
       build:
         context: /path/to/10kanal-yzer
         dockerfile: Dockerfile
   ```

Then attach your own nginx (below). No certificate or reverse-proxy containers
are bundled on purpose — you handle that side.

### Your own nginx

`nginx/dashboard.conf` is a ready server block. Copy it, set `server_name`, make
`proxy_pass` match your published port, and reload:

```bash
sudo cp nginx/dashboard.conf /etc/nginx/conf.d/dashboard.conf
sudo nano /etc/nginx/conf.d/dashboard.conf     # server_name + proxy_pass port
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dash.example.com       # optional TLS
```

Want a login? Create a password file and uncomment the `auth_basic` lines:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd you
```

### Notes

- `./financial_graphs` is bind-mounted so results also land on the host; the
  container runs as root, so those files may be root-owned.
- `edgar_cache` / `edgar_data` are named volumes that persist edgartools' raw
  SEC downloads, making repeat runs much faster.
- The app listens on `0.0.0.0:8000` inside the container (override with `PORT`).
  It has **no authentication** — keep it behind nginx or a private network.
- Runs are serialized (one at a time); ~1–3 minutes per ticker the first time.

### Rebuilding / clean redeploy (Dockge)

Dockge's **Update** button only pulls images, so it does nothing for a stack
that uses `build:` (see Dockge discussion #267). This stack builds its image
locally, so rebuild it from the stack's **Web Terminal** (or over SSH in the
stack directory):

```bash
docker compose down                 # remove the container
docker image rm edgar-dashboard     # delete the built image
docker compose build --no-cache     # rebuild from scratch
docker compose up -d                # start it again
```

- Code-only changes usually just need `docker compose up -d --build`.
- `docker builder prune` clears leftover build cache if you want the space back.
- Add `-v` to `docker compose down` to also drop the `edgar_cache` /
  `edgar_data` volumes (SEC download cache). `./financial_graphs` on the host is
  untouched either way.
- If you removed `image:` from the compose file, check the generated name with
  `docker compose images`.

## Metrics extracted per fiscal year

| Metric | XBRL concept(s) | Statement |
| --- | --- | --- |
| Revenue | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet` | Income |
| Cost of Revenue | `CostOfRevenue`, `CostOfGoodsAndServicesSold`, `CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization`, `CostOfGoodsSold`, `CostOfServices` | Income |
| Net Income | `NetIncomeLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic`, `ProfitLoss` | Income |
| Gross Profit | `GrossProfit` (fallback: `Revenues - CostOfRevenue`) | Income |
| SG&A | `SellingGeneralAndAdministrativeExpense`, `SellingGeneralAndAdministrative`, `SellingAndMarketingExpense`, `GeneralAndAdministrativeExpense` | Income |
| R&D | `ResearchAndDevelopmentExpense`, `ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost` | Income |
| Goodwill Impairment | `GoodwillImpairmentLoss`, `GoodwillAndIntangibleAssetImpairment`, `AssetImpairmentCharges` | Income |
| Operating Income | `OperatingIncomeLoss` | Income |
| Operating Cash Flow | `NetCashProvidedByUsedInOperatingActivities`, `...ContinuingOperations` | Cash Flow |
| CapEx | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets` | Cash Flow |
| Current Assets | `AssetsCurrent` | Balance Sheet |
| Current Liabilities | `LiabilitiesCurrent` | Balance Sheet |
| Long-Term Debt | `LongTermDebt`, `LongTermDebtNoncurrent`, `LongTermDebtAndCapitalLeaseObligations` | Balance Sheet |
| Stockholders' Equity | `StockholdersEquity`, `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | Balance Sheet |

Derived in-script: **Gross Margin**, **Operating Margin**, **Net Margin**
(`NetIncomeLoss / Revenues`), **Free Cash Flow** (`OperatingCashFlow - CapEx`),
**Current Ratio** (`AssetsCurrent / LiabilitiesCurrent`), **Debt-to-Equity**
(`LongTermDebt / StockholdersEquity`). All monetary values are in USD millions.

### Balance-sheet and debt extraction

The income and ratio charts use the curated concepts in the table above. The
**balance sheet** (assets / liabilities & equity) and **debt** charts do not
hard-code lines: `extract_balance_lines()` reads the filer's own presentation
linkbase through edgartools' standardized `balance_sheet()` statement. The
library maps each tagged line onto a `standard_concept`, absorbing the
filer-specific and extension tags that vary company to company, and we keep one
row per dimension-free line. The charts stack every line the filer states
(totals are skipped when stacking because they are another line's parent) and
flag debt-like and cash-like lines by concept/label text. So a new company works
without adding tags by hand; only the curated income/ratio series need
maintenance.

## Notes on the data

- `amendments=False` excludes `10-K/A` amendments so each fiscal year appears once.
- Duration facts are restricted to periods of **≥ 300 days**. Without this, the
  quarterly facts tagged in the notes (which share a `period_end` with the
  annual figure) can be selected instead of the full-year value.
- After a merger, a filer often tags the consolidated statements under a
  successor/predecessor reporting basis (`us-gaap:StatementScenarioAxis`). There
  is then no dimension-free annual fact, so the extractor falls back to a
  scenario-only fact as the consolidated value (KHC's FY2015 successor year).
  That axis marks a period's basis, not a line-item breakdown, so it is safe.
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
- Each metric tries several XBRL concepts in priority order, because filers
  change tags over time. A company can stop tagging `NetIncomeLoss` from a given
  year (Estée Lauder moved to `NetIncomeLossAvailableToCommonStockholdersBasic`),
  tag only the total-equity variant once it has non-controlling interests, or
  use `LongTermDebtAndCapitalLeaseObligations` instead of `LongTermDebt`.
- When a filing contains the same fact twice (a rounded copy and a precise one),
  the value with the greatest decimal precision is used.
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

It exits non-zero if any value differs. It now covers the income-statement
lines (revenue, cost of revenue, gross profit, SG&A, R&D, goodwill impairment,
operating income, net income) as well as the balance lines used by the ratio
charts. Derived gross profit is checked internally as `Revenue - CostOfRevenue`.
Run it after generating a ticker to confirm the charts rest on verified numbers.
All checks pass exactly (e.g. KHC 125/125, the default sample 284/284).

> The fallback for successor/predecessor reporting bases (§ Notes) produces
> values that SEC's `companyconcept` API does not expose, so those specific
> year-values are skipped rather than compared. Everything the API does expose
> is matched exactly.
