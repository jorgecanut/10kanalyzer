# Today's valuation (TTM) and the quarters since the last 10-K

Date: 2026-11-18. Status: approved by the owner ("revise the plan with my rules, and go on").

## Why

Checking TGT against Yahoo showed that every figure read from the 10-K was
right (revenue 104,780, operating income 5,117, net income 3,705, all equal to
Yahoo's annual page), but the Home card looked wrong: its P/E, EV/EBIT and
yields priced the company at the fiscal year end (31 Jan 2026, 129.75) on
fiscal-year earnings, while Yahoo prices it today (167.77) on the last twelve
months. A buy decision needs today's price over the latest earnings, and a
turnaround shows in the quarters long before the next 10-K. This is item 5.4 of
`docs/MARKET_SCAN_PLAN.md`, limited to the 10-Qs after the latest annual report.

## Goals

1. **Home card on today's data.** Every number with a price in it uses today's
   price and today's share count over trailing-twelve-month (TTM) fundamentals:
   P/E, EV/EBIT, earnings yield, FCF yield, P/E pct (today's P/E against the
   past fiscal-year P/Es), Magic rank, and the market-value part of the
   Z-score. Debt, cash and the balance-sheet parts come from the latest 10-Q.
   The card says what it used: "price 18 Nov 2026 · earnings TTM to Q2 FY2026".
2. **Charts: the annual bars, then the 0–3 quarters since the last 10-K**:
   `FY2023 FY2024 FY2025 | Q1'26 Q2'26 (Q3'26)`. Owner's choice C:
   - flow values (revenue, profits, cash flows, margins and ratios on them): the
     bar is the **TTM ending at that quarter**; the tooltip also gives the
     quarter's own three-month value;
   - balance-sheet values (cash, debt, equity, shares): the value at the
     quarter end, as is;
   - valuation charts: the quarter slot is priced at that quarter end (the same
     rule as the annual points: price at the period end), on that slot's TTM;
   - quarter slots are drawn lighter and labelled "TTM Q2'26", never mistaken
     for a 10-K year.
3. **Two bugs found on the way**, fixed in the same change:
   - "Magic rank #3 of 2": the field is a score (earnings-yield rank +
     return-on-capital rank), shown as a position. Show the position among the
     ranked tickers, with the score in the tooltip.
   - P/E used the year-end share count (452.8 M) where Yahoo and the standard
     use diluted EPS (weighted diluted shares, ~455.7 M). Use price / diluted
     EPS when the filing reports it (annual and TTM); otherwise market cap / net
     income, reported as an issue (`valuation_assumed`).

## Non-goals

- No quarterly history: only the 10-Qs whose period ends after the latest
  annual report (owner's choice), 0–3 normally, more only when the 10-K is
  overdue (all are shown and `overdue_10k` is reported).
- The Market scan keeps dumping its 10-Qs but does not analyse them yet; the
  README keeps saying so.
- F-score stays on the latest fiscal year (it compares two full years); the
  card labels it "FY2025".
- No live price refresh in the browser: "today" is the run's time, shown on
  the card.
- 20-F / 40-F filers have no 10-Qs: TTM is the latest fiscal year, labelled
  so, priced today.

## Anti-goals (a failure even if it works)

- A quarter value that is missing, from a different tag than the year, or
  inconsistent, **shown as if fine or silently dropped**. Every gap is an issue,
  counted, and listed under the charts drawn from it (CLAUDE.md "Hiding is not
  fixing").
- A change to `dump.py`, or anything that forces re-reading every filing.
- A rule for one company (ticker, CIK or name in the code; per-company tables;
  an option for one case).

## Constraints (the project's rules)

- **Stage 1 is only `dump.py`.** 10-Qs are dumped by the existing reader, as
  the scan already does: no edit to `dump.py`, no new reader in
  `readers.json`. Listing the 10-Qs is the only new SEC traffic: one listing
  plus 0–3 downloads per run, then read from the dump forever.
- **Everything that picks or computes is stage 2**, on the dump.
- **Report everything.** New issue kinds (below) are written to
  `data_issues.csv`, shown in the Market tab's "Other" column if a scan ever
  produces them, and attached under the charts (`charts.attach_warnings`). A
  claim about what a 10-Q contains is checked against its stored dump, never
  against our extraction (`data_issues.metric_evidence`).
- **Generic only.** Fixes for odd filers are general rules, tested.
- **Release.** Minor version bump (1.22.0: a visible feature), tests, README.md
  and README.local.md updated together, `docs/DATA_DICTIONARY.md` updated,
  commit, tag `v1.22.0`, push with tags. No personal data in the commit
  (`git diff --cached` checked).

## Approaches

1. **Same rules as the annual reports (chosen).** `extract.extract_filing`
   already decides everything through `period_key` and `min_days`
   (`annual_min_days` even accepts a cover-page span shorter than a year). A
   10-Q is read twice by the same code: at its own period end with its
   year-to-date (YTD) span, and at the same date a year earlier (its
   comparative columns). Every fallback built for odd 10-K filers then works
   for quarters too. Cost: the statement-line fallbacks must take the YTD
   column, not the three-month one, from a 10-Q statement.
2. Reuse the tag the annual row picked and read only that tag from the 10-Q.
   Simpler and consistent, but every value the annual run took from a
   statement line gets no quarter. Kept as a **check**, not the method.
3. Read edgartools' rendered quarterly statements only. Dropped: bypasses the
   tag logic.

## Design

### Reading (stage 1 traffic, no stage-1 code change)

- `edgar_dashboard.build_dataset` also lists the 10-Qs whose period ends after
  the latest annual report, the way `scan.list_filings` does (move that
  listing into one shared function both call). Each is obtained through
  `obtain()` (dump first, download only if new). A 10-Q that cannot be listed,
  downloaded or read is an issue (`quarter_unread`), never a failed run.
- The run's `--filings N` limits annual reports only; the quarters after the
  latest one are always read.

### Quarter rows (stage 2, a new module `quarters.py`)

For each 10-Q, newest last, with `period_end` from `filing_period()`:

- `ytd` = `extract_filing` at `period_end`, durations spanning the YTD
  (`min_days` from the cover page's period start; Q1's YTD is three months).
- `ytd_prior` = the same extraction at the comparative date one year earlier
  (the 10-Q's own prior-year columns), same span.
- `quarter` (tooltip value) = `ytd` minus the previous 10-Q's `ytd` of the same
  fiscal year; Q1 = its `ytd`. Cash-flow lines are YTD only in 10-Qs, so this
  is the only way to get them.
- `ttm` = latest FY + `ytd` − `ytd_prior`, per flow metric.
- Instants (balance sheet, shares) = `ytd`'s value at `period_end`.
- Labels from `dei:DocumentFiscalPeriodFocus` / `DocumentFiscalYearFocus`,
  checked against the dates (a Q label that disagrees with the gap from the
  fiscal year end is reported, and the date-based one used).
- Derived metrics (margins, FCF, current ratio, debt/equity) are recomputed on
  the TTM and instant values with `clean_and_derive`'s formulas.

Written to `quarters.csv` (one row per quarter: period end, label, fiscal
year, quarter, and per metric `ytd`, `ytd_prior`, `quarter`, `ttm` or the
instant value, plus the source concept of each).

### Checks, each an issue when it fails

| Kind | When |
| --- | --- |
| `quarter_unread` | a 10-Q could not be listed, downloaded or read |
| `quarter_metric_missing` | the latest FY has the metric but the 10-Q's `ytd` or `ytd_prior` does not; the issue says what the stored 10-Q holds for it (`metric_evidence`) |
| `quarter_concept_differs` | the 10-Q's value comes from another tag (or a statement line) than the fiscal year's: TTM may mix two measures |
| `quarter_restated` | the 10-Q's prior-year comparative differs from what the 10-K reported for that period, or its prior year-end balance differs from the 10-K's |
| `quarter_label_mismatch` | the filer's fiscal-period label disagrees with the dates |

The 10-Qs' own calculation checks (`calc_check`) run as for 10-Ks and are
reported the same way (`check_mismatch`, with the period). A TTM slot whose
metric is missing is drawn empty and the chart lists the issue.

### Valuation

- `valuation.build_evaluation` adds one row per quarter slot (price at the
  quarter end, the slot's TTM and instant values) after the fiscal years; the
  valuation charts draw them as quarter slots.
- A new **today** record (`valuation_today.json`): Yahoo's latest price and its
  time (chart meta `regularMarketPrice`, `regularMarketTime`), shares from the
  latest dump's cover page (`dei:EntityCommonStockSharesOutstanding` and its
  date), the latest slot's TTM, the latest balance sheet. Same formulas as the
  annual rows. P/E pct compares today's P/E with the fiscal-year P/Es only.
- P/E = price / diluted EPS (TTM diluted EPS = FY + YTD − prior YTD) when the
  filings report diluted EPS; otherwise market cap / net income with a
  `valuation_assumed` issue. Same rule for the annual rows.
- Return on capital uses the TTM EBIT and the latest balance sheet (the Magic
  rank combines it with earnings yield, so both must cover the same period).

### Home card

Reads `valuation_today.json` (falls back to the latest fiscal year, labelled,
for runs made before this version). Shows the price date and the earnings
period under the numbers; F-score labelled with its fiscal year. Magic rank
shows the position (`#1 of 2`) with the score in the tooltip.

### Charts

Each panel gets its quarter slots after the years (an explicit slot list in
`charts.json`, not fake years), styled lighter, labelled "TTM Q2'26" for flows
and "Q2'26" for balances; the tooltip gives the three-month value and the
period end. The years slider keeps working on the annual part and always shows
the slots. PNG downloads draw them the same way. Warnings under a chart include
the quarter issues of its concepts.

## Verification

- Unit tests, on synthetic dumps: TTM = FY + YTD − prior YTD; quarter = YTD
  difference; Q1; cash-flow YTD-only lines; instants; a missing metric, a tag
  change and a restatement each produce their issue; a 52/53-week year; a
  non-December year end; a 20-F with no 10-Q.
- Existing tests unchanged in result except where the P/E rule changes, each
  difference listed with before and after.
- Live check on TGT (non-December year): TTM revenue ≈ Yahoo's 107.7 B, TTM
  diluted EPS ≈ 9.63, P/E at today's price ≈ Yahoo's; and on one
  December-year company. Differences explained, not tuned away.
- A run before and after on TGT: list which numbers changed and why.

## Left to the builder

- The exact `charts.json` shape for slots and how the page/PNG code draws them.
- Moving the 10-Q listing into a shared function.
- How `extract_metric` reports the concept it used (needed for
  `quarter_concept_differs`).
- 10-Q statement column headers: verify on a real dump in the first step, and
  make `xbrl_extract._period_column` pick the YTD column.
