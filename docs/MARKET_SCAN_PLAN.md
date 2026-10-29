# Market scan — plan of changes

What has to change in Stocky to scan every company that files XBRL once, keep
it current as new filings arrive, and screen the whole market with a few
strategies. Companion to [DATA_DICTIONARY.md](DATA_DICTIONARY.md), which lists
every field.

Status: **agreed plan, not built** (2026-10-29) except section 2.

---

## 1. Goal and rules

**Goal.** One big run: download and extract every annual report (and the
current year's 10-Qs) of every XBRL filer, store every fact, compute the
curated data, then keep it current by grabbing each new 10-K and 10-Q as soon
as it is filed. Screen the market with the strategies in section 4.

**Rules.**

1. **Nothing may force regenerating everything.** A change may only re-read
   the filings it really affects. (Also in `README.md`, the tests and the AI
   memory.)
2. **SEC is the only source of statement data**; Yahoo is the only source of
   prices. No insiders, news or other external data in the global run.
3. **The dump is complete, the curated data is minimal.** Everything a filing
   says (except long text) is stored once; the curated database holds only
   what the strategies use and is rebuilt from the dump.
4. **One bad company never stops the run**, and every failure is recorded
   with its reason.

---

## 2. Already done (1.12.0 and 1.13.0, uncommitted)

| Change | Where |
| --- | --- |
| `--filings max` (every annual filing), default in CLI, API and UI | `edgar_dashboard.py`, `app.py`, `templates/index.html` |
| Per-filing cache by accession, atomic writes, shared by all users (`data/filing_cache/`) | `filing_cache.py` |
| Form 4 cache per company | `insider.py` |
| Stage 1 / stage 2 split: filing reading in `extract.py`, `xbrl_extract.py`, `calc_check.py`; everything computed in `edgar_dashboard.py`, `valuation.py`, `charts.py` | |
| Versioned cache folders, old versions kept; `filing_cache.py status / prune / delete` | `filing_cache.py` |
| Run log says why filings are parsed (new / stale); `/healthz` shows cache versions | |
| Guard tests: stage-2 modules never hashed, stage-1 files import only each other | `tests/test_filing_cache.py` |

**First action: commit this**, so the refactor below starts from a clean point.

---

## 3. Decisions

| Topic | Decision |
| --- | --- |
| Annual reports | every 10-K, 10-KT, 20-F, 40-F back to the first with XBRL; amendments skipped |
| Quarterly reports | only 10-Qs whose period ends after the latest annual report (0–3); more only if the 10-K is overdue, which is flagged |
| What is stored | all facts, **all periods**, all dimensions; short text ≤ ~500 characters; presentation and calculation trees; labels; edgartools' rendered statements |
| Not stored | long text, HTML, exhibits, images |
| Statements | option 1: keep edgartools' rendering in the dump; our own rendering from the trees is possible later without SEC |
| Prices | one Yahoo chart request per ticker: monthly bars `range=max`, volume, dividends, splits, meta; FX pairs for foreign filers |
| Not in the global run | insiders, news, analyst estimates, short interest (shortlist only, later) |
| Financials | banks, brokers, insurers, REITs extracted and flagged, excluded from screens |
| Storage | dump: gzipped JSON, one file per filing; curated: one SQLite file (`market.db`), rebuilt from the dump |
| Raw downloads | deleted after each company is dumped |
| Rebuild-all command | not now (a stage-2 fix reaches a company on its next run) |
| Strategies | turnaround, Piotroski on cheap, compounders, Magic Formula, shareholder yield, risk screens |

---

## 4. Strategies (detail in DATA_DICTIONARY.md §7)

| Strategy | Idea |
| --- | --- |
| **Turnaround** (main) | fell (price or margins far below normal), not collapsing (cash flow, solvency, debt maturities covered, no heavy dilution), turning (F-score, margins, revenue trend improving); ranked by EV / normalised EBIT; sector vs company problem from SIC peers |
| **Piotroski on cheap** | F-score ≥ 7 among the cheapest 20% by price to book |
| **Compounders** | ROIC > 15% for 10 years, stable margins, FCF ≈ earnings |
| **Magic Formula** | return on capital + earnings yield (exists) |
| **Shareholder yield** | dividends + net buybacks + debt paydown vs market cap |
| **Risk screens** | Altman Z, Beneish M, accruals, dilution, dividend cuts, overdue 10-K, restatements, auditor change |

---

## 5. Changes to make

Each item: what, why, and how we know it is done.

### 5.1 Dump format, schema v1 (stage 1)

- **What.** Replace today's cache entry (ratio row + statement lines +
  current-year facts + checks) with a complete dump in our own format: all
  facts of all periods with all dimension pairs, short text ≤ 500 characters,
  presentation tree, calculation tree, labels, edgartools' statements, DEI,
  filing metadata. Same for 10-Qs. Treat 10-KT as annual.
- **Why.** Every later need (a new tag, a metric, a chart, a strategy) comes
  from the dump without downloading. Our own format, not edgartools'
  dataframe, so a library upgrade cannot change what is stored.
- **Done when.** NFLX, JPM, TSM, KO and the 11 crosscheck tickers dump
  without error; sizes match the estimate (~100 KB–1.5 MB per filing gz); a
  dump read back holds every fact edgartools returns (counts compared).

### 5.2 Versioning: schema + reader stamp (replaces the hash folders)

- **What.** Cache folder named by a manual **schema version** (changes only
  when the stored format changes). Each entry stamped with the **reader**
  (hash of stage-1 code + edgartools version). A guard test fails whenever
  stage-1 code changes until the change is classified: compatible (record
  it), format change (bump schema), or bug (invalidate only the affected
  entries: `filing_cache.py invalidate --reader X` / `--form` / `--before`).
- **Why.** A new XBRL format or an edgartools upgrade then needs a new reader
  for new filings only; old filings stay valid. Only a deliberate format
  change can force a full re-read.
- **Done when.** Upgrading edgartools or editing `xbrl_extract.py` leaves
  existing entries valid after classification; `invalidate` re-reads only the
  selected entries; tests cover all three classifications.

### 5.3 Metric selection moves to stage 2

- **What.** `METRIC_CONCEPTS`, `COMPONENT_METRICS`, `extract_metric`, the
  CapEx / cost-of-revenue / debt fallbacks, reporting currency, ADS ratio and
  the calculation check run on the dump instead of the live XBRL. Stage 1
  becomes "copy what the filing says".
- **Why.** Fixing a missing tag (the most common fix after a scan) becomes
  free: no SEC requests, no re-parse.
- **Done when.** For the test tickers, `financial_data.csv`,
  `statements.csv`, `calc_check.csv`, `evaluation.csv` and `charts.json` are
  byte-identical to today's (or every difference is explained and accepted).

### 5.4 10-Q support and periods

- **What.** Select the 10-Qs after the latest annual report. Build quarterly
  and TTM rows: Q4 = FY − 9-month YTD; quarterly cash flow from YTD
  differences; prior-year quarters from each 10-Q's comparatives;
  TTM = FY + current YTD − prior-year YTD. Flag overdue 10-Ks.
- **Why.** Turnarounds show in quarters long before the next 10-K.
- **Done when.** For a December and a non-December fiscal year company,
  quarterly revenue, operating income and operating cash flow add up to the
  fiscal year; TTM matches a hand calculation.

### 5.5 Form 4 dump (app only)

- **What.** Cache every Form 4 transaction (all codes, not only P and S) and
  filter in stage 2.
- **Why.** Showing option exercises, grants or gifts later must not mean
  re-reading every Form 4. Not part of the global run.
- **Done when.** The Insiders tab shows the same trades as today.

### 5.6 Prices

- **What.** One chart request per ticker (`interval=1mo&range=max&events=div,split`)
  replacing today's two; store monthly OHLC, adjusted close, volume,
  dividends, splits and the meta block. SEC → Yahoo symbol mapping
  (`BRK.B` → `BRK-B`, foreign listings); unmapped tickers go to the status
  report. Polite pacing (1–2 requests per second). FX pairs once per
  currency.
- **Why.** Price is the one external input every strategy needs; the same
  request also gives liquidity, dividends, splits and the instrument type.
- **Done when.** About 6,500 requests in 1–2 hours; prices stored for ≥ 95%
  of the universe; the rest listed with a reason.

### 5.7 Curated database `market.db`

- **What.** One SQLite file with the tables of DATA_DICTIONARY.md:
  `companies`, `filings`, `fundamentals`, `prices_monthly`, `dividends`,
  `splits`, `quote`, `fx_monthly`, `signals`, `status`. Built from the dump
  and prices; each company is written in one transaction. Delete it and it is
  rebuilt without SEC.
- **Why.** A market-wide screen is one query instead of opening ~6,500
  folders; updates are all-or-nothing; corruption costs a rebuild, not data.
  CSV stays as an export.
- **Done when.** The screens in §4 run as SQL in seconds; deleting and
  rebuilding `market.db` from the dump gives the same rows.

### 5.8 New metrics and scores

- **What.** Everything marked *new* in DATA_DICTIONARY.md §2–6: interest,
  debt detail and maturities, leases, cash and investments, dividends,
  buybacks, issuance, SBC, D&A, receivables, inventory, PP&E; derived ratios
  (net debt / EBITDA, interest coverage, refinancing cover, ROIC, FCF after
  SBC…); history statistics; Z'', Beneish M, data-quality score; SIC peer
  medians and percentiles.
- **Why.** Each is used by at least one strategy (the dictionary says which).
- **Done when.** Each metric has a test on a known company, and its tags are
  confirmed in the pilot (the *verify* marks).

### 5.9 Strategy screens

- **What.** The rules of DATA_DICTIONARY.md §7 as queries producing the
  `signals` table: pass / fail per condition, a rank per strategy, risk
  flags; financials, shells, non-equities and illiquid stocks excluded.
- **Why.** "What is the market doing" as lists you can open one by one.
- **Done when.** Each screen returns a plausible list on the pilot set, and
  every listed company's numbers can be traced to its filings.

### 5.10 Universe and runner

- **What.** Universe: SEC `company_tickers.json` (plus exchange data), minus
  shells and blank checks (`EntityShellCompany`, SIC 6770), funds and ETFs
  (Yahoo `instrument_type`), companies without revenue. A batch runner with
  3–4 workers sharing one global SEC limit (≤ 8 requests per second),
  resumable (skips companies already done), writing a `status` row per
  company. Raw downloads deleted after each company. A failure report
  grouped by cause.
- **Why.** The scan takes about one to two days; it must survive restarts and
  say exactly what failed.
- **Done when.** A pilot run can be killed and resumed without repeating
  work; the failure report groups errors by cause.

### 5.11 Update loop

- **What.** Once a day, read SEC's daily index (`daily-index/form.YYYYMMDD.idx`),
  queue every new 10-K, 10-KT, 20-F, 40-F and 10-Q of a known company, dump it,
  recompute that company in `market.db`. Prices refreshed on their own
  schedule (weekly or monthly).
- **Why.** Keeps the market current with a handful of filings a day (up to
  ~1,000 at the February–March peak).
- **Done when.** A filing published yesterday is in `market.db` today.

### 5.12 Housekeeping

- Pin the edgartools version actually tested (5.62.0 locally, 5.58.0 in
  `requirements.txt`).
- `PLAN.md` and `README.md` updated for the new layout and the market scan.
- Backups: `sqlite3 market.db ".backup …"` and `stocky.db` nightly; `rsync`
  of the dump (files never change, so only new ones copy).

---

## 6. Order of work

1. Commit 1.12 + 1.13.
2. Review DATA_DICTIONARY.md (thresholds, floors, peer groups, ROIC).
3. 5.1 dump format, 5.2 versioning — tested on NFLX, JPM, TSM, KO.
4. 5.3 metric selection to stage 2 — output identical to today.
5. 5.4 10-Qs and periods; 5.5 Form 4 dump.
6. 5.6 prices; 5.7 `market.db`; 5.8 metrics; 5.9 screens.
7. 5.10 runner — **pilot of 50–100 companies**: measure requests, time,
   size, failures; tune thresholds and confirm *verify* tags.
8. Full market scan.
9. 5.11 update loop.

---

## 7. Estimates (to confirm in the pilot)

| Item | Estimate |
| --- | --- |
| Companies | ~6,500 filers (fewer after filters) |
| Filings | ~100,000 annual + ~15,000 quarterly |
| Dump size | ~15–30 GB gzipped (all periods, no long text) |
| `market.db` | ~200–400 MB (fundamentals ~150k rows, prices ~2M rows) |
| SEC time | ~1–2 days with 3–4 workers (parsing ~2.5 s per filing dominates) |
| Yahoo time | ~1–2 hours (6,500 requests, paced) |
| Daily updates | minutes on most days, about an hour at the March peak |

---

## 8. Later (not before the scan)

- Financials: their own metrics (net interest margin, combined ratio, FFO…)
  from the dump, no re-download.
- Shortlist enrichment: analyst estimates and short interest for the top
  ~200 candidates.
- Delisted companies, for testing strategies without survivorship bias.
- Rebuild-all command (reads only the dump).
- Our own statement rendering from the stored trees (option 2).
- Atomic ticker folders for the app's per-user results.
- How the app shows a company from `market.db` instead of its per-user CSVs.
