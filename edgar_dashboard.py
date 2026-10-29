#!/usr/bin/env python3
"""SEC EDGAR 10-K financial dashboard.

Downloads the last N annual filings (every one by default; 10-K, or 20-F /
40-F for foreign filers, IFRS included) for a target ticker straight from SEC
EDGAR, writes the data as CSV into ``financial_graphs/<TICKER>/`` in the
filer's reporting currency, checks every total against its calculation
linkbase (``calc_check.csv``) and writes the chart specs (``charts.json``).
Filings an earlier run already read come from the filing cache
(``filing_cache.py``) instead of SEC.

Usage:
    python edgar_dashboard.py
    python edgar_dashboard.py --ticker NFLX --filings 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time

import numpy as np
import pandas as pd

from edgar import Company, set_identity

import calc_check
import charts
import extract
import filing_cache

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# The SEC requires a descriptive User-Agent identifying who is making requests.
# Override it in production via the EDGAR_IDENTITY env var (see docker-compose).
set_identity(os.environ.get("EDGAR_IDENTITY", "Data Analyst analyst@example.com"))

TICKER = ""                 # set from --ticker
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
NUM_FILINGS = "max"         # every annual filing; or a number
BASE_OUTPUT_DIR = "financial_graphs"

# A cached filing is reused only while the code that extracted it is
# unchanged: extract.py, xbrl_extract.py, calc_check.py (filing_cache.KINDS).
CACHE_KIND = "annual"
CACHE_VERSION = filing_cache.version(CACHE_KIND)
# Shared by every run (the filings are public); --cache-dir or this variable
# moves it, and the default sits inside the output directory.
CACHE_ENV = "STOCKY_FILING_CACHE"
CACHE_DIRNAME = ".filing_cache"

# Resolved at runtime in main(): each ticker gets its own sub-directory so that
# running several companies does not overwrite previous results.
OUTPUT_DIR = BASE_OUTPUT_DIR
DATA_CSV = os.path.join(BASE_OUTPUT_DIR, "financial_data.csv")

# Money stays in the filer's reporting currency (USD for most 10-Ks; EUR, TWD,
# JPY... for foreign filers' 20-F/40-F), converted to millions for readability.
USD_TO_MILLIONS = 1_000_000

# Chart-friendly display labels.
LABELS = {
    "Revenues": "Revenue",
    "NetIncomeLoss": "Net Income",
    "GrossProfit": "Gross Profit",
    "CostOfRevenue": "Cost of Revenue",
    "SellingGeneralAndAdministrativeExpense": "SG&A",
    "ResearchAndDevelopmentExpense": "R&D",
    "GoodwillImpairmentLoss": "Goodwill Impairment",
    "OperatingIncomeLoss": "Operating Income",
    "OperatingCashFlow": "Operating Cash Flow",
    "CapEx": "Capital Expenditure",
    "AssetsCurrent": "Current Assets",
    "LiabilitiesCurrent": "Current Liabilities",
    "LongTermDebt": "Long-Term Debt",
    "StockholdersEquity": "Stockholders' Equity",
}


# --------------------------------------------------------------------------- #
# Data extraction
# --------------------------------------------------------------------------- #

def _report_date(filing) -> str:
    """The filing's period end as ``YYYY-MM-DD``.

    Comes from the submissions index (``report_date``), which needs no
    download; ``period_of_report`` fetches and parses the whole submission, so
    it is only the fallback when the index has no date.
    """
    report_date = getattr(filing, "report_date", None)
    if report_date:
        return str(report_date)
    return str(filing.period_of_report or "")


def build_dataset(ticker: str, n_filings: int | None,
                  cache_dir: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch the last ``n_filings`` annual filings (``None``: all) and assemble the datasets.

    Each filing's extraction is kept in ``cache_dir`` (see ``filing_cache``),
    so a later run parses only the filings it has not seen; ``None`` disables it.

    Supports 10-K (US domestic), 20-F and 40-F (foreign private issuers) so
    tickers such as Celestica (CLS) get their full available history.

    Returns ``(ratios, statements, facts, checks)``:
    - ``ratios`` — the wide per-year summary used by the ratio charts, with the
      reporting ``currency`` and, for ADS listings, ``ads_ratio``.
    - ``statements`` — long-form, every dimension-free primary-statement line.
    - ``facts`` — long-form, every fact for the period, dimensions included.
    - ``checks`` — the calculation-linkbase check, one row per total.
    """
    print(f"[*] Looking up {ticker} on SEC EDGAR")
    company = Company(ticker)
    print(f"[+] Company: {company.name} (CIK {company.cik})")
    if cache_dir:
        print(f"[*] Filing cache: {filing_cache.explain(cache_dir, CACHE_KIND)}")
    print("[*] Listing annual filings (10-K, 20-F, 40-F)")

    # Pull annual filings for US (10-K) and foreign (20-F / 40-F) filers, then
    # merge, deduplicate by accession and keep the most recent ``n_filings``.
    annual_forms = ["10-K", "20-F", "40-F"]
    all_filings: list = []
    seen_accessions: set[str] = set()
    for form in annual_forms:
        try:
            batch = company.get_filings(form=form, amendments=False)
            for filing in batch:
                if filing.form != form:
                    continue
                accession = getattr(filing, "accession_no", None)
                if not accession or accession in seen_accessions:
                    continue
                seen_accessions.add(accession)
                all_filings.append(filing)
        except Exception as exc:  # noqa: BLE001 - a missing form should not abort the others
            print(f"    [!] {form}: filing list failed ({exc.__class__.__name__}: {exc}); continuing")
            continue

    period_filings: list[tuple[str, object]] = []
    for filing in all_filings:
        try:
            period = _report_date(filing)
        except Exception as exc:  # noqa: BLE001 - skip filings with unreadable periods
            print(f"    [!] unreadable period ({exc.__class__.__name__}: {exc}); skipping filing")
            continue
        period_filings.append((period, filing))
    period_filings.sort(key=lambda item: item[0], reverse=True)
    filings = [filing for _, filing in period_filings[:n_filings]]  # [:None] keeps them all
    forms = ", ".join(f"{sum(f.form == form for f in all_filings)} {form}" for form in annual_forms
                      if any(f.form == form for f in all_filings))
    reading = "reading all of them" if n_filings is None else f"reading the newest {len(filings)}"
    print(f"[+] Found {len(all_filings)} annual filings ({forms or 'none'}); {reading}")

    records: list[dict] = []
    statement_records: list[dict] = []
    fact_records: list[dict] = []
    check_records: list[dict] = []
    seen_periods: set[str] = set()
    # From the cache / parsed because new to the cache / parsed because only
    # an older code version had it.
    hits = new = stale = 0

    for position, filing in enumerate(filings, 1):
        if n_filings is not None and len(records) >= n_filings:
            break
        accession = None

        # Everything is guarded per filing: edgartools can raise on individual
        # malformed filings (e.g. missing filing dates) and one bad filing
        # should not abort the whole run.
        try:
            period_of_report = _report_date(filing)
            if not period_of_report:
                continue

            # De-duplicate by the exact period end, NOT by calendar year.
            # Companies on a 52/53-week calendar can have two fiscal years whose
            # period ends fall in the same calendar year (e.g. a year ending
            # 2024-01-01 and one ending 2024-12-30); keying on the year silently
            # dropped one of them.
            period_key = str(period_of_report)
            if period_key in seen_periods:
                continue

            accession = filing.accession_no
            started = time.monotonic()
            # A filing never changes: what an earlier run extracted from it is
            # reused, so only filings new to the cache are downloaded.
            extracted = filing_cache.load(cache_dir, CACHE_KIND, CACHE_VERSION, company.cik, accession)
            cached = extracted is not None
            if cached:
                hits += 1
                print(f"[*] Filing {position}/{len(filings)}: {filing.form} for the year ended {period_key}, "
                      f"from the cache ({accession})")
            else:
                older = filing_cache.cached_elsewhere(cache_dir, CACHE_KIND, CACHE_VERSION, company.cik, accession)
                why = ", cached only by an older code version" if older else ""
                print(f"[*] Filing {position}/{len(filings)}: {filing.form} for the year ended {period_key}, "
                      f"downloading and parsing its XBRL ({accession}{why})")
                extracted = extract.extract_filing(filing, period_key)
                filing_cache.store(cache_dir, CACHE_KIND, CACHE_VERSION, company.cik, accession, extracted)
                if older:
                    stale += 1
                else:
                    new += 1
            if not extracted.get("xbrl"):
                print(f"    [!] {period_key} ({accession}): no XBRL; skipping")
                continue
            row = extracted["row"]
            statement_rows = extracted["statements"]
            filing_facts = extracted["facts"]
            filing_checks = extracted["checks"]
            fiscal_year = row["fiscal_year"]
        except Exception as exc:  # noqa: BLE001 - skip unparseable filings
            ident = accession or getattr(filing, "accession_no", "?")
            print(f"    [!] {ident}: unreadable ({exc.__class__.__name__}: {exc}); skipping")
            continue

        records.append(row)
        statement_records.extend(statement_rows)
        fact_records.extend(filing_facts)
        check_records.extend(filing_checks)
        seen_periods.add(period_key)
        money = "" if row["currency"] == "USD" else f", {row['currency']}"
        adds_up = sum(check["status"] == calc_check.STATUS_OK for check in filing_checks)
        took = "from the cache" if cached else f"in {time.monotonic() - started:.0f} s"
        print(f"    [+] FY{fiscal_year} (period end {period_key}): extracted {accession} ({filing.form}{money}) "
              f"{took}; {len(statement_rows)} statement lines, "
              f"{adds_up}/{len(filing_checks)} linkbase totals add up")

    if cache_dir:
        print(f"[+] Filing cache {CACHE_VERSION}: {hits} from the cache, {new} new filings parsed, "
              f"{stale} parsed again (cached only by an older code version)")

    if not records:
        raise RuntimeError(
            f"No parseable annual filings found for {ticker!r}. It may be a "
            "recent registrant or outside the US-GAAP / IFRS SEC universe."
        )

    df = pd.DataFrame(records).sort_values("period_of_report").reset_index(drop=True)
    period_dates = pd.to_datetime(df["period_of_report"])

    # Number the fiscal years by walking backwards from the most recent filing's
    # filer-asserted fiscal year (DEI DocumentFiscalYearFocus), stepping one year
    # per ~annual period. This keeps the axis contiguous and correct for 52/53-
    # week calendars and fiscal-year-end changes, where neither the calendar year
    # of the period end nor the DEI tag alone is reliable (e.g. TTM Technologies'
    # DEI focus skips 2022 and its period years skip 2021).
    years = [None] * len(df)
    # Anchor value: the raw DEI focus (or period-year fallback) of the newest row.
    years[-1] = int(df.iloc[-1]["fiscal_year"])
    for i in range(len(df) - 2, -1, -1):
        gap_days = (period_dates.iloc[i + 1] - period_dates.iloc[i]).days
        step = max(1, round(gap_days / 365.25))
        years[i] = years[i + 1] - step
    df["fiscal_year"] = years

    year_by_period = dict(zip(df["period_of_report"], df["fiscal_year"]))
    statements = pd.DataFrame(statement_records)
    if not statements.empty:
        statements["fiscal_year"] = statements["period_of_report"].map(year_by_period)
    facts = pd.DataFrame(fact_records)
    if not facts.empty:
        facts["fiscal_year"] = facts["period_of_report"].map(year_by_period)
    checks = pd.DataFrame(check_records)
    if not checks.empty:
        checks.insert(0, "fiscal_year", checks["period_of_report"].map(year_by_period))

    return df, statements, facts, checks


# --------------------------------------------------------------------------- #
# Cleaning + derived metrics
# --------------------------------------------------------------------------- #

def clean_and_derive(df: pd.DataFrame) -> pd.DataFrame:
    """Convert to millions, derive missing/derived metrics, sort by year."""
    value_cols = list(extract.METRIC_CONCEPTS.keys())

    # Convert USD -> millions and force numeric dtypes.
    for col in value_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce") / USD_TO_MILLIONS

    # Netflix only tags GrossProfit quarterly in its notes for the earlier
    # filings, so fall back to the standard definition Revenue - Cost of
    # Revenue, which is consistent across the whole window.
    derived_gross = df["Revenues"] - df["CostOfRevenue"]
    missing_gross = df["GrossProfit"].isna()
    df["GrossProfit"] = df["GrossProfit"].fillna(derived_gross)
    # Only rows that were actually filled count as derived. Comparing the
    # finished values instead would also flag filers whose tagged GrossProfit
    # equals Revenue - CostOfRevenue (Apple, Nvidia), which is the normal case
    # whenever cost of revenue is complete.
    df["gross_profit_derived"] = missing_gross & df["GrossProfit"].notna()

    # Some filers (e.g. Nike) do not tag OperatingIncomeLoss. Derive it from
    # gross profit less SG&A and R&D when those components are available.
    sga = df["SellingGeneralAndAdministrativeExpense"].fillna(0)
    rnd = df["ResearchAndDevelopmentExpense"].fillna(0)
    has_op_components = (
        df["SellingGeneralAndAdministrativeExpense"].notna()
        | df["ResearchAndDevelopmentExpense"].notna()
    )
    derived_operating = df["GrossProfit"] - sga - rnd
    df.loc[has_op_components, "OperatingIncomeLoss"] = df.loc[
        has_op_components, "OperatingIncomeLoss"
    ].fillna(derived_operating)

    # Derived health metrics (guard every denominator against zero).
    df["GrossMargin"] = _safe_div(df["GrossProfit"], df["Revenues"])
    df["OperatingMargin"] = _safe_div(df["OperatingIncomeLoss"], df["Revenues"])
    df["NetMargin"] = _safe_div(df["NetIncomeLoss"], df["Revenues"])
    df["FreeCashFlow"] = df["OperatingCashFlow"] - df["CapEx"]
    df["CurrentRatio"] = _safe_div(df["AssetsCurrent"], df["LiabilitiesCurrent"])
    df["DebtToEquity"] = _safe_div(df["LongTermDebt"], df["StockholdersEquity"])

    sort_col = "period_of_report" if "period_of_report" in df.columns else "fiscal_year"
    df = df.sort_values(sort_col).reset_index(drop=True)
    return df


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    result = numerator / denominator.replace(0, np.nan)
    return result.replace([np.inf, -np.inf], np.nan)


# Note schedules we surface as their own tables/charts. Matched against the
# concept name and the dimension, so they adapt to whatever a filer tags.
NOTE_SCHEDULES: dict[str, str] = {
    "debt": r"Debt|Borrow|CommercialPaper|NotesPayable|LineOfCredit|Lease",
    "segments": r"Segment|StatementBusinessSegmentsAxis|StatementGeographicalAxis",
    "eps": r"EarningsPerShare",
    "taxes": r"IncomeTax|EffectiveIncomeTaxRate|DeferredTax|UnrecognizedTaxBenefits",
    "shares": r"CommonStockShares|WeightedAverageNumberOfShares|StockRepurchase|TreasuryStock",
}


def extract_note_schedules(facts: list[dict]) -> list[dict]:
    """Tag the fact rows with the key schedule they belong to (``notes.csv``)."""
    rows: list[dict] = []
    for fact in facts:
        haystack = f"{fact['concept']} {fact.get('dimension') or ''} {fact.get('member') or ''}"
        for schedule, pattern in NOTE_SCHEDULES.items():
            if re.search(pattern, haystack, re.IGNORECASE):
                rows.append({**fact, "schedule": schedule})
                break
    return rows


def check_summary(rows: list[dict] | pd.DataFrame) -> dict:
    """Counts of the calculation check for the run log and the page."""
    frame = pd.DataFrame(rows)
    out = {"filings": frame["period_of_report"].nunique() if not frame.empty else 0}
    for kind in ("statement", "note"):
        part = frame[frame["kind"] == kind] if not frame.empty else frame
        out[kind] = {
            "totals": len(part),
            "ok": int((part["status"] == calc_check.STATUS_OK).sum()) if len(part) else 0,
            "mismatch": int((part["status"] == calc_check.STATUS_MISMATCH).sum()) if len(part) else 0,
            "incomplete": int((part["status"] == calc_check.STATUS_INCOMPLETE).sum()) if len(part) else 0,
        }
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _write_checks(checks: pd.DataFrame) -> None:
    """``calc_check.csv`` and a one-line verdict in the log."""
    path = os.path.join(OUTPUT_DIR, "calc_check.csv")
    if checks.empty:
        if os.path.exists(path):
            os.remove(path)
        print("[!] Calculation check: no calculation linkbase in these filings")
        return
    checks.to_csv(path, index=False)
    counts = check_summary(checks)
    statements, notes = counts["statement"], counts["note"]
    print(f"[+] Calculation check ({counts['filings']} filings, calc_check.csv): statements "
          f"{statements['ok']}/{statements['totals']} totals add up, {statements['mismatch']} do not, "
          f"{statements['incomplete']} have untagged parts; notes {notes['ok']}/{notes['totals']}")


def print_summary(df: pd.DataFrame) -> None:
    display_cols = [
        "fiscal_year", "Revenues", "NetIncomeLoss", "GrossMargin",
        "OperatingMargin", "NetMargin", "OperatingCashFlow", "FreeCashFlow",
        "CurrentRatio", "DebtToEquity",
    ]
    summary = df[display_cols].copy()
    summary["GrossMargin"] = (summary["GrossMargin"] * 100).round(1)
    summary["OperatingMargin"] = (summary["OperatingMargin"] * 100).round(1)
    summary["NetMargin"] = (summary["NetMargin"] * 100).round(1)
    for col in ("Revenues", "NetIncomeLoss", "OperatingCashFlow", "FreeCashFlow"):
        summary[col] = summary[col].round(0)
    summary["CurrentRatio"] = summary["CurrentRatio"].round(2)
    summary["DebtToEquity"] = summary["DebtToEquity"].round(2)

    print("\n" + "=" * 100)
    currency = str(df["currency"].iloc[-1]) if "currency" in df.columns and len(df) else "USD"
    print(f"{TICKER} financial summary ({currency} millions; margins in %)")
    print("=" * 100)
    print(summary.to_string(index=False))
    print("=" * 100 + "\n")


def _start_insiders(ticker: str, output_dir: str,
                    cache_dir: str | None = None) -> tuple[threading.Thread, dict]:
    """Start writing ``insider.json`` in the background; ``_report_insiders`` collects it.

    A daemon thread, so a failed 10-K step still ends the run at once.
    """
    result: dict = {}

    def work() -> None:
        try:
            import insider

            result["path"] = insider.write_insiders(ticker, output_dir, cache_dir=cache_dir)
        except Exception as exc:  # noqa: BLE001 - optional artifact must never abort
            result["error"] = exc

    print("[*] Insider trades: reading Form 4 filings in the background")
    thread = threading.Thread(target=work, name="insiders", daemon=True)
    thread.start()
    return thread, result


def _report_insiders(started: tuple[threading.Thread, dict]) -> None:
    """Wait for the background insider step and print its outcome."""
    thread, result = started
    if thread.is_alive():
        print("[*] Waiting for the insider trades (Form 4 filings) to finish")
    thread.join()
    if "error" in result:
        exc = result["error"]
        print(f"[!] insider data skipped: {type(exc).__name__}: {exc}")
        return
    insider_path = result.get("path")
    if not insider_path:
        return
    trades = None
    try:
        with open(insider_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        trades = len(payload) if isinstance(payload, list) else len(payload.get("transactions", []))
    except Exception:  # noqa: BLE001 - count is cosmetic only
        trades = None
    if trades is None:
        print(f"[+] Wrote insider transactions ({insider_path})")
    else:
        print(f"[+] Wrote insider transactions ({trades} trades)")


class _Stamped:
    """A stream that starts every line with the time, so the log shows where a run spends it."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._at_line_start = True

    def write(self, text: str) -> int:
        with self._lock:
            out = []
            for piece in text.splitlines(keepends=True):
                if self._at_line_start and piece.strip():
                    out.append(time.strftime("%H:%M:%S "))
                out.append(piece)
                self._at_line_start = piece.endswith("\n")
            self._stream.write("".join(out))
            self._stream.flush()
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _filings_arg(value: str) -> int | str:
    """``--filings``: a positive number, or ``max`` for every annual filing."""
    if value.strip().lower() == "max":
        return "max"
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number or 'max'") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> None:
    global OUTPUT_DIR, DATA_CSV, TICKER
    sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument(
        "--filings", type=_filings_arg, default=NUM_FILINGS,
        help="Number of 10-K filings to use, or 'max' for all of them (default: %(default)s)",
    )
    parser.add_argument(
        "--no-charts", action="store_true",
        help="Write the CSV/valuation data only; skip chart rendering (screener mode)",
    )
    parser.add_argument(
        "--output-dir", default=BASE_OUTPUT_DIR,
        help="Parent directory for the per-ticker results (default: %(default)s)",
    )
    parser.add_argument(
        "--cache-dir", default=os.environ.get(CACHE_ENV),
        help=f"Where each filing's extraction is kept (default: ${CACHE_ENV}, else "
             f"<output-dir>/{CACHE_DIRNAME})",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Parse every filing again, ignoring and not updating the filing cache",
    )
    args = parser.parse_args()
    n_filings = None if args.filings == "max" else args.filings
    cache_dir = None if args.no_cache else (args.cache_dir or os.path.join(args.output_dir, CACHE_DIRNAME))

    if not TICKER_RE.match(args.ticker.upper()):
        parser.error("invalid ticker")

    # Point the module globals at a per-ticker output directory.
    TICKER = args.ticker.upper()
    OUTPUT_DIR = os.path.join(args.output_dir, TICKER)
    DATA_CSV = os.path.join(OUTPUT_DIR, "financial_data.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Insider trades need nothing from the 10-Ks, so their Form 4 downloads
    # run while the 10-Ks are fetched instead of after them.
    insiders = _start_insiders(TICKER, OUTPUT_DIR, cache_dir)

    which = "every annual filing" if n_filings is None else f"the last {n_filings} annual filings"
    print(f"[*] Extracting {TICKER} data from {which}")
    raw, statements, facts, checks = build_dataset(TICKER, n_filings, cache_dir)
    df = clean_and_derive(raw)

    df.to_csv(DATA_CSV, index=False)
    print(f"[+] Wrote ratio data to {os.path.abspath(DATA_CSV)}")

    # XBRL-driven artifacts: everything the filer stated, not a curated list.
    if not statements.empty:
        statements.to_csv(os.path.join(OUTPUT_DIR, "statements.csv"), index=False)
        print(f"[+] Wrote {len(statements)} statement lines to statements.csv")
    if not facts.empty:
        facts.to_csv(os.path.join(OUTPUT_DIR, "facts.csv"), index=False)
        notes = pd.DataFrame(extract_note_schedules(facts.to_dict("records")))
        if not notes.empty:
            notes.to_csv(os.path.join(OUTPUT_DIR, "notes.csv"), index=False)
        print(f"[+] Wrote {len(facts)} facts and {len(notes)} note rows")

    _write_checks(checks)
    print_summary(df)

    # Optional valuation artifacts: price history vs. reported fundamentals.
    # Entirely best-effort so a data source outage cannot fail the run.
    try:
        import valuation

        print("[*] Valuation: fetching the monthly price history (Yahoo Finance)")
        history = valuation.fetch_price_history(TICKER)
        # Foreign filers: the quote is in USD per ADS, the filings in their own currency.
        scale, price_currency, note = valuation.market_scale(TICKER, df, facts, statements)
        if note:
            print(f"[*] Valuation: {note}")
        evaluation = valuation.build_evaluation(TICKER, df, statements, facts, history,
                                                price_scale=scale, price_currency=price_currency)
        if evaluation.empty:
            print("[!] valuation skipped: no price history")
        else:
            valuation.write_evaluation(TICKER, evaluation, OUTPUT_DIR)
            print(f"[+] Wrote valuation data ({len(evaluation)} years)")
    except Exception as exc:  # noqa: BLE001 - optional artifact must never abort
        print(f"[!] valuation skipped: {type(exc).__name__}: {exc}")

    # Insider trades were fetched alongside the 10-Ks; report what came back.
    _report_insiders(insiders)

    if args.no_charts:
        print("[*] Charts skipped (screener mode)")
        print(f"\n[+] Done. CSV data written to '{os.path.abspath(OUTPUT_DIR)}/'.")
        return

    # charts.json only: the page draws it, and PNGs are drawn when downloaded.
    print("[*] Building charts")
    rendered = charts.render_all(TICKER, OUTPUT_DIR, df, statements, facts, log=print)
    print(f"    [+] {len(rendered)} charts written to charts.json")

    print(f"\n[+] Done. {len(rendered)} charts and CSV data written to '{os.path.abspath(OUTPUT_DIR)}/'.")


if __name__ == "__main__":
    main()
