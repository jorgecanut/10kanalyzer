# Today's valuation (TTM) and quarter slots — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Home card values the company at today's price on trailing-twelve-month (TTM) fundamentals, and every chart shows the 0–3 quarters since the last 10-K after its fiscal years.

**Architecture:** Runs also dump the 10-Qs after the latest annual report (existing reader, no `dump.py` change). A new stage-2 module `quarters.py` reads each 10-Q twice with the existing `extract.extract_filing` (year-to-date at its period end, and the prior-year comparative) and builds TTM, three-month and instant values plus their issues. Valuation adds quarter rows and a "today" record; charts append quarter slots; the Home card reads the today record.

**Tech Stack:** Python 3 (pandas, numpy), Flask (`app.py`), vanilla JS + ECharts (`static/app.js`), matplotlib PNGs (`charts.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-ttm-quarters-design.md`

## Global Constraints

- No edit to `dump.py`; no new entry in `readers.json`; `tests/test_filing_cache.py` must stay green unchanged.
- Everything that picks or computes is stage 2 and runs on the dump.
- No ticker, CIK or company name in code or tests' logic (a comment may name one as an example).
- Every gap is an issue in `data_issues.csv` with a title in `data_issues.TITLES` and in `static/app.js` issue titles; nothing is dropped from a count.
- New issue kinds: `quarter_unread`, `quarter_metric_missing`, `quarter_concept_differs`, `quarter_restated`, `quarter_label_mismatch`.
- `--filings N` limits annual reports only; the quarters after the latest one are always read.
- Version `1.22.0`; README.md and README.local.md changed together; `docs/DATA_DICTIONARY.md` documents `quarters.csv`, `valuation_today.json`, the new evaluation rows and the new issue kinds.
- Before committing: `git diff --cached` has no personal data (names, emails, hosts, home paths, anything from `data/`).

## Review Focus

1. A 10-Q whose statement has both a three-month and a year-to-date column: the YTD column must be read, never the three-month one (Task 2 test `test_10q_statement_takes_ytd_column`).
2. Runs saved before this version (no `quarters.csv`, no `valuation_today.json`): charts and Home card render as before, labelled "FY" (Task 6 `test_spec_without_quarters_unchanged`, Task 7 `test_home_falls_back_to_latest_fy`).
3. A company with no 10-Q since its 10-K (just filed, or a 20-F filer): no slots, TTM = latest FY, card says "earnings FY2025" (Task 3 `test_no_quarters_ttm_is_fy`).
4. Yahoo unreachable for today's quote: card falls back to the latest month-end close with its date, and a `valuation_skipped`-style issue says so (Task 5 `test_today_without_quote`).
5. 52/53-week fiscal years, where the prior-year comparative ends up to 7 days off one calendar year (Task 3 `test_53_week_prior_period`).

---

### Task 1: List and dump the 10-Qs after the latest annual report in every run

**Files:**
- Modify: `edgar_dashboard.py` (`build_dataset`, new `list_quarterly`)
- Modify: `scan.py:277-303` (`list_filings` calls `list_quarterly`)
- Test: `tests/test_dashboard.py`, `tests/test_scan.py`

**Interfaces:**
- Produces: `edgar_dashboard.list_quarterly(company, after: str, log=print) -> list` — 10-Q filings (amendments skipped) whose `_report_date` is later than `after`, oldest first.
- Produces: `build_dataset(...)` returns a 5th item `quarterly: list[dict]` = the dumps (`entry` dicts) of those 10-Qs, oldest first; failures go to a new keyword `quarter_failures: list | None` as `{"accession", "period", "error"}`.

- [ ] **Step 1: Failing tests** — `test_list_quarterly_after_latest` (fake company with 10-Qs dated before/after `after`, newest first as edgartools returns them; expect only the later ones, oldest first, and the listing stops at the first older one); `test_build_dataset_quarter_failure_is_recorded` (an `obtain` that raises for one 10-Q: run continues, failure recorded); in `test_scan.py`, `test_scan_uses_shared_quarter_listing` (scan's recorded quarterly filings unchanged for the existing fixture).
- [ ] **Step 2: Run** `pytest tests/test_dashboard.py tests/test_scan.py -q` — new tests FAIL.
- [ ] **Step 3: Implement** `list_quarterly` (moved from `scan.list_filings`), call it from `build_dataset` after `list_annual` with `after = _report_date(latest annual)`; obtain each with `obtain()`; log `[*] Quarter k/n: 10-Q for the period ended …`. Update every caller of `build_dataset` (`_run`) to unpack 5 values.
- [ ] **Step 4: Run** the full suite `pytest -q` — PASS.
- [ ] **Step 5: Manual check on a real 10-Q dump.** Needs SEC access with the owner's identity (ask first). `.venv/bin/python -c` load TGT's newest 10-Q dump through `filing_view.FilingView`, print the income statement and cash-flow statement column headers and `dei:DocumentFiscalPeriodFocus`. Record the header format (expected like `2026-08-01 (Q2)` / `2026-08-01 (YTD)` — confirm) in the Task 2 test fixture.
- [ ] **Step 6: Commit** `feat: runs read the 10-Qs after the latest annual report`.

### Task 2: Extraction reads a 10-Q at a chosen span and says which tag it used

**Files:**
- Modify: `extract.py` (`extract_metric`, `_component_metric`, `extract_filing`)
- Modify: `xbrl_extract.py:44-57` (`_period_column`)
- Test: `tests/test_extraction.py`

**Interfaces:**
- Produces: `extract.extract_filing(filing, period_key: str, min_days: int | None = None, max_days: int | None = None) -> dict` — unchanged for annual calls (`None` → `annual_min_days`); the result gains `"sources": {metric: str}` where the string is the concept name (`"Revenues"`), `"statement:<label>"` for a statement-line value, or `"derived"`.
- Produces: `extract.extract_metric(..., max_days: int | None = None, sources: dict | None = None, metric: str | None = None)` — durations must satisfy `min_days <= span <= max_days` when `max_days` is set; writes the chosen concept into `sources[metric]`.
- Produces: `xbrl_extract._period_column(data, period_of_report, span_days: int | None = None)` — when several columns start with the date, picks the one whose label/period length matches `span_days` (YTD), never the three-month one when YTD is longer.

- [ ] **Step 1: Failing tests** — `test_extract_metric_respects_max_days` (facts with 91-day and 182-day spans ending the same date; `min_days=170, max_days=195` returns the 182-day value); `test_extract_filing_reports_sources` (annual fixture: `sources["Revenues"] == "Revenues"`; a value from a statement line → `"statement:…"`); `test_10q_statement_takes_ytd_column` (statement frame with columns from Task 1 Step 5, both for the same date; YTD chosen); `test_annual_results_unchanged` (existing annual fixtures produce identical rows).
- [ ] **Step 2: Run** `pytest tests/test_extraction.py -q` — new tests FAIL.
- [ ] **Step 3: Implement** the parameters; thread `min_days/max_days` into `calc_check.period_values` / `convenience_values` calls the same way.
- [ ] **Step 4: Run** `pytest -q` — PASS, no change in existing results.
- [ ] **Step 5: Commit** `feat: extraction reads a chosen period span and records each metric's source`.

### Task 3: `quarters.py` — YTD, prior YTD, three-month, TTM, instants and their issues

**Files:**
- Create: `quarters.py`
- Test: `tests/test_quarters.py`

**Interfaces:**
- Consumes: `extract.extract_filing(..., min_days, max_days)` and `"sources"` (Task 2); `edgar_dashboard.filing_period`; `edgar_dashboard.clean_and_derive` formulas.
- Produces: `quarters.build(annual: pd.DataFrame, annual_sources: dict[str, str], entries: list[dict]) -> tuple[pd.DataFrame, list[dict]]`. `annual` is `financial_data.csv`'s frame (latest FY row used); `entries` the 10-Q dumps oldest first. Returns `(quarters_df, issues)`.
- `quarters_df` columns: `slot` (`"Q2'26"`: quarter + two-digit fiscal year), `label` (`"TTM Q2'26"`), `period_end`, `fiscal_year` (int, the fiscal year the quarter belongs to), `quarter` (1–3), `accession`; then for every metric in `extract.METRIC_CONCEPTS`: `<metric>` (TTM for durations, the period-end value for instants), `<metric>__q` (three-month value, durations only), `<metric>__ytd`, `<metric>__ytd_prior`, `<metric>__source`; then the derived columns of `clean_and_derive` computed on the TTM/instant values.
- Duration span for a 10-Q: from the cover page (`dei` period start → `period_end`), `min_days = span - 20`, `max_days = span + 20`; Q1 ≈ 91 days.
- Prior-year comparative date: the 10-Q's duration facts of the same span whose end is 358–372 days before `period_end` (52/53-week years); none found → `quarter_metric_missing` for each metric.
- `TTM = FY + ytd - ytd_prior`; `q = ytd - previous 10-Q's ytd` when the previous 10-Q is of the same fiscal year, else `ytd` for Q1, else blank with `quarter_metric_missing` detail "three-month value needs the previous 10-Q".
- Issue dicts use `data_issues._issue(kind, severity, fiscal_year=<slot>, period=period_end, form="10-Q", accession=…, concept=metric, detail=…)`; kinds and severities: `quarter_unread` warning, `quarter_metric_missing` warning (detail from `data_issues.metric_evidence` on the 10-Q), `quarter_concept_differs` warning (both sources named), `quarter_restated` info (both values), `quarter_label_mismatch` info.
- Restatement test: the 10-Q's instant facts at the latest FY end vs `annual`'s values for `AssetsCurrent, LiabilitiesCurrent, LongTermDebt, StockholdersEquity`; differ by more than 0.5% → `quarter_restated`.

- [ ] **Step 1: Failing tests** in `tests/test_quarters.py`, each on synthetic dumps built with the existing fixture helpers in `tests/test_extraction.py` (reuse, don't copy): `test_ttm_is_fy_plus_ytd_minus_prior` (FY rev 100, YTD 60, prior 55 → TTM 105); `test_q1_three_month_is_ytd`; `test_q2_three_month_is_ytd_difference`; `test_cash_flow_ytd_only` (cash-flow facts have only YTD spans; TTM and `__q` still computed); `test_instant_is_period_end_value`; `test_missing_metric_issue_names_evidence`; `test_concept_differs_issue`; `test_restated_issue`; `test_label_mismatch_issue`; `test_53_week_prior_period` (prior comparative 371 days earlier is matched); `test_non_december_year` (fiscal year ending 31 Jan: slot `Q2'26` for period ending 2026-08-01, `fiscal_year` 2026); `test_no_quarters_ttm_is_fy` (`entries=[]` → empty frame, no issues); `test_unreadable_entry_is_reported` (dump with `xbrl: False` → `quarter_unread`, other quarters still built).
- [ ] **Step 2: Run** `pytest tests/test_quarters.py -q` — FAIL (no module).
- [ ] **Step 3: Implement** `quarters.build` and private helpers.
- [ ] **Step 4: Run** `pytest -q` — PASS.
- [ ] **Step 5: Commit** `feat: quarters.py builds TTM, three-month and quarter-end values with their issues`.

### Task 4: Wire quarters into the run and the issue lists

**Files:**
- Modify: `edgar_dashboard.py` (`_run`, `_write_issues`)
- Modify: `data_issues.py` (`TITLES`, `chart_warnings` accepts slot ids)
- Modify: `static/app.js:3316` (titles for the five kinds)
- Test: `tests/test_dashboard.py`, `tests/test_data_issues.py`

**Interfaces:**
- Consumes: `build_dataset` 5th value (Task 1), `quarters.build` (Task 3).
- Produces: `<run>/quarters.csv` (written only when non-empty); quarter issues appended to `data_issues.csv`; `data_issues.chart_warnings(concepts, years: list[int | str], issues)` matches an issue's `fiscal_year` against slot strings as well as ints.

- [ ] **Step 1: Failing tests** — `test_run_writes_quarters_csv` (pipeline on fixtures with one 10-Q: file exists, one row, slot correct); `test_quarter_issues_in_data_issues_csv`; `test_chart_warnings_match_slots`; `test_titles_cover_quarter_kinds` (every new kind in `TITLES`, and the string appears in `static/app.js`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**; the 10-Qs' own calculation checks (`extract_filing(...)["checks"]`) join `calc_check.csv` and the issues as `check_mismatch` with the 10-Q's period and form; more than 3 10-Qs after the 10-K adds `overdue_10k` (the scan's kind); the run logs `[+] Quarters: Q1'26, Q2'26 (TTM to 2026-08-01)` or `[*] Quarters: none since the 10-K`.
- [ ] **Step 4: Run** `pytest -q` — PASS.
- [ ] **Step 5: Commit** `feat: runs write quarters.csv and report quarter issues`.

### Task 5: Valuation — diluted-EPS P/E, quarter rows, today's record

**Files:**
- Modify: `valuation.py` (`build_evaluation`, new `fetch_quote`, new `build_today`, `write_evaluation`)
- Modify: `edgar_dashboard.py` (`_run` valuation block)
- Test: `tests/test_valuation.py`

**Interfaces:**
- Consumes: `quarters_df` (Task 3).
- Produces: `valuation.fetch_quote(ticker) -> dict | None` = `{"price": float, "time": "YYYY-MM-DDTHH:MM:SSZ", "currency": str}` from the chart API meta (`regularMarketPrice`, `regularMarketTime`, `currency`); None when unreachable.
- Produces: `build_evaluation(..., quarters: pd.DataFrame | None = None)` — after the fiscal-year rows, one row per slot with `fiscal_year` = slot id, `period_end`, price = `_close_near` the quarter end, TTM/instant inputs. Also a new column `eps_diluted` and `pe_basis` (`"diluted_eps"` or `"net_income"`).
- P/E rule: `price / eps_diluted` when `EarningsPerShareDiluted` is read for that period (annual: the FY fact; slot: FY + YTD − prior YTD of that tag); else `market_cap / net_income` and a `valuation_assumed` issue with concept `EarningsPerShareDiluted`.
- Produces: `valuation.build_today(evaluation: pd.DataFrame, quote: dict | None, shares: tuple[float, str] | None, issues: list) -> dict` written to `<run>/valuation_today.json`: `{"price", "price_time", "price_source": "quote" | "month_close", "shares", "shares_date", "earnings_period": "TTM Q2'26" | "FY2025", "pe", "ev_ebit", "earnings_yield", "return_on_capital", "fcf_yield", "pe_percentile", "z_score", "f_score", "f_score_year"}` — fundamentals from the last evaluation row (latest slot, else latest FY); `pe_percentile` against fiscal-year rows only; `f_score` from the latest FY row.
- Shares: latest 10-Q dump's (else 10-K's) `dei:EntityCommonStockSharesOutstanding` and its date.

- [ ] **Step 1: Failing tests** — `test_pe_uses_diluted_eps` (FY with EPS 8.13, price 129.75 → 15.96; `pe_basis == "diluted_eps"`); `test_pe_falls_back_and_reports` (no EPS → net-income P/E and a `valuation_assumed` issue); `test_quarter_rows_after_years` (slot rows priced at the quarter-end close, TTM inputs); `test_today_uses_quote_and_ttm` (quote 167.77, TTM EPS 9.63 → P/E 17.42); `test_today_without_quote` (falls back to the latest month close, `price_source == "month_close"`, issue recorded); `test_pe_percentile_today_vs_fiscal_years_only`; `test_today_no_quarters_uses_fy` (`earnings_period == "FY2025"`). Existing `test_valuation.py` expectations that change because of the P/E rule: update each and list old → new in the commit message.
- [ ] **Step 2: Run** `pytest tests/test_valuation.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q` — PASS.
- [ ] **Step 5: Commit** `feat: valuation on diluted EPS, quarter rows and today's record`.

### Task 6: Charts — quarter slots after the years (page and PNG)

**Files:**
- Modify: `charts.py` (`build_spec`, `render_all`, `render_from_dir`, `_panel`, `_series`, chart functions, `_draw_panel`, `chart_valuation`)
- Modify: `static/app.js` (panel drawing ~1572–2010, tooltip, slider), `static/app.css` (slot style)
- Test: `tests/test_charts.py`

**Interfaces:**
- Consumes: `quarters_df` (Task 3), evaluation slot rows (Task 5).
- Produces spec shape: each panel keeps `years: [int]` and gains `quarters: [{"id": "Q2'26", "label": "TTM Q2'26" | "Q2'26", "period_end": "YYYY-MM-DD"}]`; each series' `values` has `len(years) + len(quarters)` entries; each series gains `quarter_values: [float|None]` (three-month values; `None` for instants). `SPEC_VERSION` + 1. Label is `"TTM …"` when the series is a flow, plain for instants (a panel mixing both uses `"TTM …"` and the tooltip states it per series).
- Chart functions keep their signatures with `years` widened to `list[int | str]`; `build_spec` passes `years + slot ids`, and `df`/`statements` with the slot rows appended (`fiscal_year` = slot id). Statement-line series for slots use TTM over the 10-Q statement lines matched by concept; when unavailable the slot is `None` and Task 3's issue covers it.

- [ ] **Step 1: Failing tests** — `test_spec_has_quarter_slots` (one slot: `quarters[0]["id"] == "Q2'26"`, values length years+1, `quarter_values` set for revenue, `None` for cash); `test_spec_without_quarters_unchanged` (no `quarters.csv` → spec equal to before except `version` and empty `quarters`); `test_png_draws_slots` (render a PNG with one slot; no exception, slot tick label `TTM Q2'26` present on the axis); `test_valuation_chart_slots`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** Python side; then JS: x-axis = years + quarter labels, slot bars at 45% opacity with a dashed separator before the first slot, tooltip shows TTM value, three-month value and period end; the years slider moves over the annual part and always keeps the slots visible; `yearSpan` text adds ` + Q2'26`.
- [ ] **Step 4: Run** `pytest -q` — PASS. Start `app.py`, open a run with slots, check every chart in light and dark, and the Charts tab spacing against the other tabs (memory: tabs share one spacing system).
- [ ] **Step 5: Commit** `feat: charts show the quarters since the last 10-K`.

### Task 7: Home card on today's record; Magic rank shows a position

**Files:**
- Modify: `app.py` (`_screener_items`, `_greenblatt_ranks`)
- Modify: `static/app.js:1070-1100` (Home stats) and `:42` (tooltip text)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `valuation_today.json` (Task 5).
- Produces: screener items read `valuation_today.json` when present (fields `pe, ev_ebit, earnings_yield, return_on_capital, fcf_yield, pe_percentile, z_score, f_score`, plus `price, price_time, earnings_period, f_score_year`), else the last fiscal-year row of `evaluation.csv` with `earnings_period = "FY<year>"`; `greenblatt_score` = the sum of ranks; `greenblatt_rank` = 1-based position by score (ties share the position).

- [ ] **Step 1: Failing tests** — `test_screener_uses_today_record`; `test_home_falls_back_to_latest_fy` (no json → FY row, slot rows of evaluation.csv ignored); `test_magic_rank_is_position` (two tickers: ranks 1 and 2, scores 3 and 3 → both position 1; scores 2 and 4 → 1 and 2).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**; card shows under the numbers: `price 167.77 · 18 Nov 2026 15:10 · earnings TTM Q2'26`, F-score label `F-score (FY2025)`, Magic `#1 of 2` with tooltip `score 3: earnings-yield rank + return-on-capital rank`.
- [ ] **Step 4: Run** `pytest -q` — PASS; check the card in the browser at phone width and desktop.
- [ ] **Step 5: Commit** `feat: Home card on today's price and TTM; Magic rank is a position`.

### Task 8: Live check, docs, release

**Files:**
- Modify: `README.md`, `README.local.md` (same text: run reads the 10-Qs after the latest annual report; quarter slots; today's card; new issue kinds; Market scan still does not analyse 10-Qs), `docs/DATA_DICTIONARY.md`, `PLAN.md` §7 backlog (b) done, `VERSION` → `1.22.0`

- [ ] **Step 1: Live TGT run** (owner's identity, asked first) before and after: list every number on the Home card and each chart's last FY value, old → new, with the reason. Compare with Yahoo: TTM revenue vs 107.7 B, TTM diluted EPS vs 9.63, P/E vs Yahoo's at the same price. Differences explained, not tuned.
- [ ] **Step 2: One December-year company** (owner picks or a well-known one) the same way.
- [ ] **Step 3: Docs and VERSION.** Run `pytest -q` — PASS.
- [ ] **Step 4: Commit, tag, push.** `git diff --cached` checked for personal data; `git commit -m "feat: v1.22.0; today's valuation on TTM, quarters since the last 10-K in the charts"`; `git tag v1.22.0`; `git push origin main --tags`.
