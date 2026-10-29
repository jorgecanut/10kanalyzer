# Data dictionary — global market scan

Every piece of data the global run stores, what it means, where it comes from
and which strategy uses it. The rule for this list: **everything needed, and
only what is needed** in the curated layer; the raw dump stays complete so a
new need never means downloading again.

Status: **draft for review** (2026-10-29). Nothing below is built yet unless
marked *exists*. Tags marked *verify* are candidates to confirm in the pilot.

---

## 0. Conventions

| Topic | Rule |
| --- | --- |
| Source of statement data | SEC filings only (XBRL). No vendor numbers, no estimates, no filled zeros: a missing value stays empty. |
| Source of market data | Yahoo chart API only (one request per ticker), plus FX pairs for foreign filers. |
| Traceability | Every curated value points to its accession(s) and XBRL concept, or to the formula that derived it. |
| Money | The filer's reporting currency, in units (not millions). Ratios are plain numbers. Conversion to USD only where a price is combined with filing data. |
| Periods | `FY` (fiscal year), `Q1`–`Q3` (from 10-Qs), `Q4` (derived: FY − 9-month YTD), `TTM` (latest FY + current YTD − prior-year YTD). |
| Flows vs. balances | Flows (revenue, cash flow) cover a period; balances (assets, debt) are at the period end. |
| Quality flag per value | `reported` (a tagged fact), `statement` (the filer's statement line), `derived` (computed from other values), `missing`. |
| Financials | Banks, brokers, insurers, REITs (SIC 6000–6411, 6798) are extracted like everyone else, flagged `financial`, and left out of the screens. |
| Stages | Everything in sections 2–7 is **stage 2**: computed from the dump, free to change without downloading anything. |

Strategy codes used in the tables:

| Code | Strategy |
| --- | --- |
| **T** | Turnaround: fallen, not collapsing, turning |
| **P** | Piotroski F-score on the cheapest stocks by price to book |
| **C** | Quality compounders |
| **M** | Magic Formula |
| **S** | Shareholder yield |
| **R** | Risk screens (applied to every strategy) |
| **A** | App only: shown when one company is opened, not used by a screen |

---

## 1. Raw dump (stage 1) — stored per filing, never curated

Stored once per filing as gzipped JSON, in our own fixed format (schema v1),
so nothing stage 2 does later needs SEC again.

| Part | Contents | Notes |
| --- | --- | --- |
| Filing | accession, CIK, form (10-K, 10-KT, 20-F, 40-F, 10-Q), filed date, period end, fiscal year and period (DEI) | 10-K/A and 10-Q/A skipped |
| Facts | every fact, **all periods**: concept, value, unit, decimals, start / end / instant, **all dimensions** (axis → member pairs), context id | numbers and text up to ~500 characters |
| Short text | DEI and other short text facts (names, flags, dates, durations, enumerations) | longer text (notes, narratives) is **not** stored |
| Presentation tree | statement roles, line order, parent, level, preferred label | to rebuild statements in our own code later |
| Calculation tree | total → parts with weights, per role | the calculation check runs on this |
| Labels | standard, terse, total and negated labels per concept | |
| Statements | edgartools' rendered statements (option 1) | convenience copy; replaceable later from the trees |
| Reader stamp | hash of the stage-1 code and edgartools version that wrote it | for targeted re-reads if a reader bug is found |

Which filings: every annual report (10-K, 10-KT, 20-F, 40-F) back to the
first one with XBRL, plus the 10-Qs whose period ends after the latest annual
report (0–3; more only if the 10-K is overdue, which is flagged).

---

## 2. `companies` — one row per company

| Field | Definition | Source | Used by | Status |
| --- | --- | --- | --- | --- |
| `cik` | SEC company id (key) | SEC | all | exists |
| `ticker` | primary ticker | SEC `company_tickers.json` | all | exists |
| `yahoo_symbol` | the symbol Yahoo uses (`BRK.B` → `BRK-B`) | mapping rule | prices | new |
| `name` | registrant name | `dei:EntityRegistrantName` | A | new |
| `sic` | 4-digit industry code | SEC submissions | T, sectors, financial flag | new |
| `sic_description` | industry name | SEC submissions | A | new |
| `sector` | SIC division (2-digit group) for peer comparisons | derived from `sic` | T | new |
| `financial` | bank / broker / insurer / REIT | `sic` in 6000–6411 or 6798 | excludes from screens | new |
| `shell` | shell company | `dei:EntityShellCompany`, SIC 6770 | excludes from universe | new |
| `exchange` | listing exchange | `dei:SecurityExchangeName`, Yahoo meta | universe filter | new |
| `instrument_type` | EQUITY / ETF / FUND… | Yahoo meta | excludes non-equities | new |
| `filer_category` | large accelerated … smaller reporting | `dei:EntityFilerCategory` | R (size), A | new |
| `incorporation` | state / country | `dei:EntityIncorporationStateCountryCode` | A | new |
| `fiscal_year_end` | month-day | `dei:CurrentFiscalYearEndDate` | periods | new |
| `accounting_standard` | US GAAP / IFRS | `dei:DocumentAccountingStandard` (20-F), else US GAAP | A | new |
| `currency` | reporting currency | facts' units | all money | exists |
| `ads_ratio` | ordinary shares per ADS (foreign filers) | `dei:Security12bTitle` text, EPS ratio | per-share values | exists |
| `auditor_id` | PCAOB firm id (2021+) | `dei:AuditorFirmId` | R (auditor change), A | new |
| `first_xbrl_filing` | date of the first annual report with XBRL | dump | A | new |
| `listed_since` | first trade date | Yahoo `firstTradeDate` | A | new |

---

## 3. `filings` — one row per stored filing

| Field | Definition | Source | Used by | Status |
| --- | --- | --- | --- | --- |
| `accession` | key | SEC | traceability | exists |
| `cik` | company | SEC | | exists |
| `form` | 10-K, 10-KT, 20-F, 40-F, 10-Q | SEC | periods | exists |
| `filed` | filing date | SEC | update loop, R (late filer) | new |
| `period_end` | period the filing covers | SEC index | periods | exists |
| `fiscal_year`, `fiscal_period` | FY / Q1 / Q2 / Q3 | DEI | periods | exists (FY only) |
| `error_correction` | restatement flag (2023+) | `dei:DocumentFinStmtErrorCorrectionFlag` | R | new |
| `icfr_attested` | auditor attested internal controls | `dei:IcfrAuditorAttestationFlag` | R, A | new |
| `checks_ok`, `checks_mismatch`, `checks_incomplete` | calculation check counts | calculation tree | data quality | exists (detail only) |

---

## 4. `fundamentals` — one row per company and period (FY, Q1–Q4, TTM)

### 4.1 Income statement (flows)

| Field | Definition | Tags (first found wins) | Used by | Status |
| --- | --- | --- | --- | --- |
| `revenue` | total revenue | Revenues, RevenueFromContractWithCustomerExcludingAssessedTax, …IncludingAssessedTax, SalesRevenueNet, SalesRevenueGoodsNet; IFRS RevenueFromContractsWithCustomers, Revenue | all | exists |
| `cost_of_revenue` | cost of goods / services | CostOfRevenue, CostOfGoodsAndServicesSold, CostOfGoodsSold, CostOfServices; IFRS CostOfSales; statement lines | margins | exists |
| `gross_profit` | revenue − cost of revenue | GrossProfit, else derived | P, margins | exists |
| `sga` | selling, general and administrative | SellingGeneralAndAdministrativeExpense, else selling + G&A components | R (Beneish) | exists |
| `rnd` | research and development | ResearchAndDevelopmentExpense (+ variants) | C, A | exists |
| `operating_income` | operating income (EBIT proxy) | OperatingIncomeLoss; IFRS ProfitLossFromOperatingActivities; else gross − SG&A − R&D | all | exists |
| `interest_expense` | interest on debt | InterestExpense, InterestExpenseNonoperating, InterestExpenseDebt; IFRS FinanceCosts — *verify* | T, R (coverage) | new |
| `pretax_income` | income before tax | IncomeLossFromContinuingOperationsBeforeIncomeTaxes… ; IFRS ProfitLossBeforeTax — *verify* | C (tax rate), EBIT fallback | new |
| `income_tax` | income tax expense | IncomeTaxExpenseBenefit; IFRS IncomeTaxExpenseContinuingOperations — *verify* | C (NOPAT) | new |
| `net_income` | net income to common holders | NetIncomeLoss, …AvailableToCommonStockholdersBasic; IFRS ProfitLossAttributableToOwnersOfParent; ProfitLoss | all | exists |
| `eps_diluted` | diluted EPS | EarningsPerShareDiluted; IFRS DilutedEarningsLossPerShare | A, ADS ratio | exists (ADS only) |
| `goodwill_impairment` | goodwill / asset write-offs | GoodwillImpairmentLoss, GoodwillAndIntangibleAssetImpairment, AssetImpairmentCharges; IFRS ImpairmentLossRecognisedInProfitOrLossGoodwill | T (the fall) | exists |
| `restructuring` | restructuring charges | RestructuringCharges — *verify* | T (the fall) | new |
| `depreciation_amortization` | D&A | DepreciationDepletionAndAmortization, DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet; IFRS DepreciationAndAmortisationExpense, AdjustmentsForDepreciationAndAmortisationExpense | EBITDA, R (Beneish), C | exists (in valuation) |
| `sbc` | stock-based compensation | ShareBasedCompensation; IFRS AdjustmentsForSharebasedPayments — *verify* | C (FCF after SBC) | new |

### 4.2 Balance sheet (at period end)

| Field | Definition | Tags | Used by | Status |
| --- | --- | --- | --- | --- |
| `cash` | cash and equivalents | CashAndCashEquivalentsAtCarryingValue, CashCashEquivalentsRestrictedCash…; IFRS CashAndCashEquivalents | net debt, T | exists (in valuation) |
| `short_term_investments` | marketable securities, current | ShortTermInvestments, MarketableSecuritiesCurrent, AvailableForSaleSecuritiesDebtSecuritiesCurrent — *verify* | net debt | new |
| `receivables` | trade receivables | AccountsReceivableNetCurrent, ReceivablesNetCurrent; IFRS TradeAndOtherCurrentReceivables — *verify* | R (Beneish), T | new |
| `inventory` | inventories | InventoryNet; IFRS Inventories | T (normalising), R | new |
| `current_assets` | total current assets | AssetsCurrent; IFRS CurrentAssets | P, R (Z) | exists |
| `ppe_net` | property, plant and equipment | PropertyPlantAndEquipmentNet; IFRS PropertyPlantAndEquipment | M (ROC), R (Beneish) | new |
| `goodwill` | goodwill | Goodwill | R (asset quality), A | new |
| `intangibles` | other intangibles | IntangibleAssetsNetExcludingGoodwill; IFRS IntangibleAssetsOtherThanGoodwill — *verify* | A | new |
| `total_assets` | total assets | Assets; statement total | P, R, M | exists (in valuation) |
| `current_liabilities` | total current liabilities | LiabilitiesCurrent; IFRS CurrentLiabilities | P, M, R | exists |
| `debt_current` | debt due within a year | LongTermDebtCurrent, DebtCurrent, ShortTermBorrowings, CommercialPaper; IFRS CurrentPortionOfNoncurrentBorrowings, CurrentBorrowings — *verify* | T (refinancing), net debt | new |
| `debt_noncurrent` | long-term debt | LongTermDebt(Noncurrent), …AndCapitalLeaseObligations; IFRS NoncurrentBorrowings, components | all | exists |
| `lease_liabilities` | operating + finance lease liabilities | OperatingLeaseLiability(+Current/Noncurrent), FinanceLeaseLiability; IFRS LeaseLiabilities — *verify* | total debt | new |
| `debt_due_12m` | principal repaid within 12 months | LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths — *verify* | T (not collapsing) | new |
| `debt_due_2_5y` | principal due in years 2–5 (sum) | …InYearTwo … InYearFive | T, A | new |
| `total_liabilities` | total liabilities | Liabilities; else current + noncurrent, else liabilities-and-equity − equity | R (Z), C | exists (in valuation) |
| `retained_earnings` | retained earnings / accumulated deficit | RetainedEarningsAccumulatedDeficit | R (Z) | exists (in valuation) |
| `equity` | equity to common holders | StockholdersEquity (+ variants); IFRS EquityAttributableToOwnersOfParent | P, all | exists |
| `shares_outstanding` | shares at the cover date | `dei:EntityCommonStockSharesOutstanding` (sum of classes) | market cap | exists (in valuation) |
| `shares_diluted` | weighted diluted shares | WeightedAverageNumberOfDilutedSharesOutstanding; IFRS equivalent — *verify* | P (dilution), per share | new |

### 4.3 Cash flow (flows; quarters derived from YTD differences)

| Field | Definition | Tags | Used by | Status |
| --- | --- | --- | --- | --- |
| `operating_cash_flow` | cash from operations | NetCashProvidedByUsedInOperatingActivities (+ continuing); IFRS CashFlowsFromUsedInOperatingActivities | all | exists |
| `capex` | purchases of PP&E | PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets; IFRS PurchaseOfPropertyPlantAndEquipment…; statement line | FCF | exists |
| `acquisitions` | businesses bought, net of cash | PaymentsToAcquireBusinessesNetOfCashAcquired — *verify* | C (capital allocation), A | new |
| `dividends_paid` | cash dividends | PaymentsOfDividends, PaymentsOfDividendsCommonStock; IFRS DividendsPaid… — *verify* | S, R (cuts) | new |
| `buybacks` | shares repurchased | PaymentsForRepurchaseOfCommonStock; IFRS PaymentsToAcquireOrRedeemEntitysShares — *verify* | S | new |
| `shares_issued` | shares issued for cash | ProceedsFromIssuanceOfCommonStock — *verify* | S (net buybacks), R (dilution) | new |
| `debt_issued` | debt raised | ProceedsFromIssuanceOfLongTermDebt — *verify* | S (debt paydown) | new |
| `debt_repaid` | debt repaid | RepaymentsOfLongTermDebt — *verify* | S (debt paydown) | new |

### 4.4 Derived per period (computed, no new tags)

| Field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `gross_margin` | gross profit / revenue | P, T, C | exists |
| `operating_margin` | operating income / revenue | T, C | exists |
| `net_margin` | net income / revenue | A | exists |
| `ebitda` | operating income + D&A | T, R | exists (in valuation) |
| `fcf` | operating cash flow − capex | all | exists |
| `fcf_after_sbc` | FCF − stock-based compensation | C | new |
| `fcf_margin` | FCF / revenue | C | new |
| `total_debt` | debt current + noncurrent + leases | T, R | new |
| `net_debt` | total debt − cash − short-term investments | T, R | new |
| `net_debt_to_ebitda` | net debt / EBITDA (replaces debt/equity) | T, R | new |
| `interest_coverage` | operating income / interest expense | T, R | new |
| `refinancing_cover` | (cash + TTM FCF) / debt due in 12 months | T (not collapsing) | new |
| `current_ratio` | current assets / current liabilities | P, R | exists |
| `roe` | net income / average equity | C, A | exists (chart only) |
| `roa` | net income / average total assets | P, A | exists (chart only) |
| `roic` | operating income × (1 − tax rate) / (total debt + equity − cash) | C | exists (chart only), to formalise |
| `return_on_capital` | Greenblatt: operating income / (net working capital + PP&E) | M | exists (proxy), to use PP&E |
| `asset_turnover` | revenue / total assets | P | exists (inside F-score) |
| `cash_conversion` | operating cash flow / net income | C, R | new |
| `accruals_ratio` | (net income − operating cash flow) / total assets | R | exists |
| `capex_to_da` | capex / D&A (under-investing when < 1 for years) | T, C | new |
| `share_change` | year-over-year change in diluted shares | P, R (dilution), S | exists (inside F-score) |
| `revenue_growth` | year over year (quarters: vs same quarter last year) | T, C | new |
| `goodwill_to_assets` | goodwill / total assets | R | new |

### 4.5 History statistics (per company, over its fiscal years)

| Field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `revenue_cagr_3y`, `_5y`, `_10y` | compound annual revenue growth | C, T | new |
| `revenue_peak`, `revenue_vs_peak` | highest FY revenue; latest / peak | T (the fall) | new |
| `op_margin_median_10y`, `op_margin_min_10y` | operating margin median / minimum over up to 10 FYs | T (normal earnings), C (stability) | new |
| `op_margin_vs_median` | latest TTM margin − 10y median | T | new |
| `roic_median_10y`, `roic_min_10y` | ROIC median / minimum | C | new |
| `years_fcf_positive` | count of FYs with positive FCF (of the last 10) | C, T | new |
| `years_revenue_up` | count of FYs with revenue growth (of the last 10) | C | new |
| `margin_trend_quarters` | TTM operating margin now vs 1 and 2 quarters ago | T (turning) | new |
| `revenue_decline_slowing` | last quarter's YoY growth > previous quarter's | T (turning) | new |
| `years_of_history` | FYs with data | data quality | new |

---

## 5. Market data (Yahoo, one request per ticker)

### 5.1 `prices_monthly` — one row per ticker and month

| Field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `month` | month (key) | | exists (close only) |
| `open`, `high`, `low`, `close` | monthly bar | T (drawdown uses `high`) | close exists |
| `adj_close` | adjusted for splits and dividends | total return, volatility | new |
| `volume` | shares traded | liquidity | new |

### 5.2 `dividends`, `splits`, `quote`

| Table / field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `dividends(date, amount)` | every cash dividend per share | S, R (cuts) | new |
| `splits(date, ratio)` | stock splits | share-count checks | new |
| `quote.price`, `quote.time` | latest price | valuation | exists (second request; merged) |
| `quote.currency`, `exchange`, `instrument_type`, `first_trade_date` | meta block | universe, FX, `listed_since` | partly |
| `fx_monthly(pair, month, close)` | reporting currency → price currency | foreign filers | exists |

### 5.3 Derived from prices (computed at the latest month and per FY end)

| Field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `market_cap` | price × shares outstanding (ADS-adjusted) | all | exists |
| `enterprise_value` | market cap + total debt − cash − short-term investments | all | exists (LTD only), to use total debt |
| `pe`, `pb`, `ps`, `pfcf` | price ratios | P (P/B), T | exists (P/S new) |
| `ev_ebit`, `ev_ebitda` | EV multiples | T, M | exists |
| `earnings_yield` | operating income / EV | M | exists |
| `fcf_yield` | FCF / EV | C, S | exists |
| `dividend_yield` | TTM dividends / price | S | new |
| `shareholder_yield` | (dividends + buybacks − issuance + net debt paydown) / market cap | S | new |
| `pe_median_hist`, `pe_percentile` | P/E against its own history | T | exists |
| `ev_normalized_ebit` | EV / (10y median operating margin × TTM revenue) | **T (key metric)** | new |
| `drawdown_5y` | latest close / highest monthly high of 5 years − 1 | T (the fall) | new |
| `momentum_12_1` | return from 12 months ago to 1 month ago | T (turning), A | new |
| `above_10m_average` | close > 10-month average close | T (turning) | new |
| `volatility_3y` | standard deviation of monthly returns, 3 years | R | new |
| `dollar_volume_12m` | average monthly close × volume, 12 months (USD) | **universe: liquidity filter** | new |
| `total_return_1y`, `_3y`, `_5y` | from adjusted closes | A | new |

---

## 6. Scores and sector context

| Field | Definition | Used by | Status |
| --- | --- | --- | --- |
| `f_score` | Piotroski F-score 0–9 (9 tests, at least 6 required) | P, T (turning) | exists |
| `f_score_change` | F-score now − one year ago | T (turning) | new |
| `z_score` | Altman Z (manufacturer form) | R, T | exists |
| `z_score_nonmanufacturing` | Altman Z'' for non-manufacturers (no revenue/assets term) | R | new |
| `beneish_m` | Beneish M-score (8 ratios: receivables, gross margin, asset quality, growth, depreciation, SG&A, leverage, accruals) | R | new |
| `data_quality` | share of key fields `reported` vs `derived` / `missing`, plus check mismatches | trust in every screen | new |
| `peer_count` | companies in the same SIC group | sector context | new |
| `peer_median_*` | sector medians of operating margin, revenue growth, EV/EBIT, drawdown | T (sector or company problem) | new |
| `peer_percentile_*` | the company's rank within its sector for the same fields | T, A | new |

---

## 7. `signals` — one row per company, strategy and date

Thresholds are **first drafts**, to be tuned after the pilot.

| Strategy | Passes when | Output |
| --- | --- | --- |
| **T** Turnaround | **fell**: drawdown_5y ≤ −40% or revenue_vs_peak < 0.85 or op_margin_vs_median < 0, **and not collapsing**: TTM OCF > 0, Z > 1.8 (or Z'' > 1.1), interest coverage > 3, refinancing_cover > 1, share_change < 5%, **and turning**: f_score_change > 0 or margin_trend_quarters up or revenue_decline_slowing; ranked by `ev_normalized_ebit` | pass / fail per condition, rank |
| **P** Piotroski on cheap | P/B in the cheapest 20% of the universe and F-score ≥ 7 | rank by F-score, then P/B |
| **C** Compounders | roic_median_10y > 15%, roic_min_10y > 8%, years_fcf_positive ≥ 9, cash_conversion > 0.8, revenue_cagr_10y > 0 | rank by ROIC, then FCF yield |
| **M** Magic Formula | rank by return_on_capital + rank by earnings_yield (existing logic) | combined rank |
| **S** Shareholder yield | shareholder_yield in the top 10%, FCF covers it | rank |
| **R** Risk flags | Z < 1.8, Beneish M > −1.78, accruals in the worst 10%, share_change > 5%, dividend cut, overdue 10-K, error-correction flag, auditor change | flags (excluded or shown) |

Universe for every screen: not `financial`, not `shell`, `instrument_type` =
EQUITY, `dollar_volume_12m` above a floor (e.g. 1 M USD/month), data_quality
above a floor.

---

## 8. `status` — one row per company

| Field | Definition |
| --- | --- |
| `cik`, `last_run`, `result` | ok / failed / partial |
| `error` | reason when not ok |
| `annual_found`, `annual_with_xbrl`, `quarterly_stored` | filing counts |
| `overdue_10k` | 10-Qs after the last 10-K exceed 3, or the 10-K is past its deadline |
| `ticker_mapped`, `prices_ok` | Yahoo lookup worked |
| `schema_version`, `reader` | which dump format and reader wrote the filings |

---

## 9. Deliberately not stored in the global run

| Data | Why not | Where it lives instead |
| --- | --- | --- |
| Insider trades (Form 4) | about 1M requests; individual analysis only | app, per company |
| News | individual analysis only | app, per company |
| Analyst estimates, price targets, short interest | second fragile Yahoo request per ticker | later, shortlist only (~200) |
| Long text (notes, narratives), HTML, exhibits, images | size; no planned use | not stored (needing it later means a re-read) |
| `statements.csv`, `facts.csv`, `notes.csv`, calculation-check detail | only needed when a company is opened | the dump |
| `charts.json`, PNGs | generated in under a second on open | on demand |
| Financials-specific metrics (FFO, net interest margin, combined ratio…) | financials excluded for now | later, from the dump (no re-download) |

---

## 10. Open points to review

1. Thresholds in section 7.
2. Liquidity floor and data-quality floor for the universe.
3. Peer groups: SIC 2-digit (broad) or 3-digit (narrow, fewer peers)?
4. ROIC definition (section 4.4): include goodwill in invested capital or not?
5. Which *verify* tags hold up in the pilot, and which need fallbacks.
