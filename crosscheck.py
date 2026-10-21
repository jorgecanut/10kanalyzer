#!/usr/bin/env python3
"""Cross-check the dashboard's numbers against an independent implementation.

``edgar_dashboard.py`` extracts facts from the filing's raw XBRL with its own
concept-priority rules. ``edgar-analytics`` reads the *same* filings through
``edgartools`` high-level statement API, with its own synonyms, sign handling
and fallbacks. Two independent code paths over one source: a disagreement is a
bug in one of them.

This script compares the two year by year and reports, per metric:

- ``OK``    both sides present and equal within tolerance
- ``ALT``   both present but under a different concept in the same filing;
            ``note`` names the concept (total revenue vs net sales)
- ``INFO``  known definitional gap, see ADVISORY (CapEx fallback estimates)
- ``DIFF``  both present, values disagree with no explanation in the filing
- ``MINE``  dashboard is missing a value the other side found (a real gap)
- ``THEIRS``the other side is missing a value the dashboard has
- ``N/A``   neither side has it (bank without current assets, REIT without
            gross profit) -- expected, not a failure

Usage:
    python crosscheck.py                      # default sample
    python crosscheck.py AAPL JPM O           # explicit tickers
    python crosscheck.py AAPL --refresh       # re-run the extractor first

Exit code is 1 when any ``DIFF`` or ``MINE`` row exists, so it can gate CI.

Outputs:
    reports/crosscheck/<TICKER>.csv            one row per metric/period
    reports/crosscheck/<TICKER>_theirs.json    raw edgar-analytics payload
    reports/crosscheck/summary.csv             one row per ticker
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

# The SEC rejects anonymous requests; keep the same contract as the dashboard.
IDENTITY = os.environ.get("EDGAR_IDENTITY", "Data Analyst analyst@example.com")

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

BASE_OUTPUT_DIR = Path("financial_graphs")
REPORT_DIR = Path("reports/crosscheck")
EXTRACTOR = Path("edgar_dashboard.py")

# Default sample: one company per discipline, chosen to stress the statement
# shapes the extractor has to survive (see PLAN.md, "Test matrix").
DEFAULT_TICKERS = ["AAPL", "NVDA", "WMT", "JNJ", "JPM", "MET", "XOM", "CAT", "DUK", "O"]

# 10kanalyzer CSV column -> (edgar-analytics multi-year key, relative tolerance).
# Tolerance is 0 for facts both sides should read identically; the balance-sheet
# aggregates get slack because a wider definition (non-controlling interests,
# finance leases) is defensible, not a bug.
CHECKS: dict[str, tuple[str, float]] = {
    "Revenues": ("Revenue", 0.0),
    "CostOfRevenue": ("Cost of Revenue", 0.0),
    "GrossProfit": ("Gross Profit", 0.0),
    "OperatingIncomeLoss": ("Operating Income", 0.0),
    "NetIncomeLoss": ("Net Income", 0.0),
    "OperatingCashFlow": ("Cash from Operations", 0.0),
    "CapEx": ("CapEx", 0.0),
    "AssetsCurrent": ("Current Assets", 0.0),
    "LiabilitiesCurrent": ("Current Liabilities", 0.0),
    "StockholdersEquity": ("Total Equity", 0.002),
}

# Metrics where a one-sided difference is a known definitional gap, not a bug.
# edgar-analytics derives CapEx from net investing outflow when the filer never
# tags a PP&E purchase (banks, insurers, REITs), so its number is not capex.
ADVISORY = {"CapEx"}

USD_TO_MILLIONS = 1_000_000


def load_mine(ticker: str) -> pd.DataFrame | None:
    """The dashboard's derived table (USD millions)."""
    path = BASE_OUTPUT_DIR / ticker / "financial_data.csv"
    if not path.exists():
        print(f"  [!] no dashboard output at {path} (run with --refresh)")
        return None
    return pd.read_csv(path)


def load_theirs(ticker: str, years: int) -> tuple[dict, dict]:
    """edgar-analytics multi-year values, in raw USD, keyed by period end."""
    from edgar import set_identity

    set_identity(IDENTITY)

    import edgar_analytics as ea

    result = ea.analyze(
        ticker,
        n_years=years,
        n_quarters=1,
        disable_forecast=True,
        identity=IDENTITY,
    )
    multiyear = result.main.multiyear
    annual = multiyear.annual_data or {}
    payload = {
        "ticker": ticker,
        "annual_data": annual,
        "cagr": multiyear.cagr,
        "filing_info": vars(result.main.annual_snapshot.filing_info)
        if result.main.annual_snapshot.filing_info
        else None,
        "scores": result.main.annual_snapshot.metrics.scores.to_dict()
        if result.main.annual_snapshot.metrics.scores
        else None,
        "alerts": list(result.main.annual_snapshot.metrics.alerts or ()),
    }
    return annual, payload


def close(mine_usd: float, theirs_usd: float, rel_tol: float) -> bool:
    if theirs_usd == 0:
        return abs(mine_usd) <= 1.0
    return abs(mine_usd - theirs_usd) <= max(1.0, abs(theirs_usd) * rel_tol)


def load_alternatives(ticker: str) -> dict[str, list[tuple[str, float]]]:
    """Every dimension-free statement line per period, as (concept, raw USD).

    Used to explain a difference instead of reporting it as a bug: "total
    revenues" and "net sales" are both correct and both in the filing.
    """
    path = BASE_OUTPUT_DIR / ticker / "statements.csv"
    if not path.exists():
        return {}
    statements = pd.read_csv(path)
    out: dict[str, list[tuple[str, float]]] = {}
    for _, row in statements.iterrows():
        value = pd.to_numeric(row.get("value"), errors="coerce")
        if pd.isna(value):
            continue
        period = str(row["period_of_report"])
        concept = str(row["concept"]).split(":")[-1].split("_")[-1]
        out.setdefault(period, []).append((concept, float(value)))
    return out


def explain(alternatives: dict[str, list[tuple[str, float]]], period: str, target: float) -> str | None:
    """Name a statement line whose value equals ``target``, if one exists."""
    for concept, value in alternatives.get(period, []):
        if abs(value - target) <= 1.0:
            return concept
    return None


def compare(
    ticker: str,
    mine: pd.DataFrame,
    theirs: dict,
    alternatives: dict[str, list[tuple[str, float]]] | None = None,
) -> list[dict]:
    """One row per (metric, period). Every cell is classified, never guessed."""
    rows: list[dict] = []
    for column, (key, rel_tol) in CHECKS.items():
        if column not in mine.columns:
            rows.append(
                {"ticker": ticker, "metric": column, "period": "-", "mine": None,
                 "theirs": None, "delta": None, "status": "MINE"}
            )
            continue
        theirs_series = {str(k)[:10]: v for k, v in (theirs.get(key) or {}).items()}
        # Periods the dashboard claims to cover; theirs may hold more (its own
        # lookback), which is fine and not reported.
        for _, record in mine.iterrows():
            period = str(record["period_of_report"])[:10]
            raw = record[column]
            mine_usd = None if pd.isna(raw) else float(raw) * USD_TO_MILLIONS
            theirs_usd = theirs_series.get(period)

            if mine_usd is None and theirs_usd is None:
                status = "N/A"
            elif mine_usd is None:
                status = "INFO" if column in ADVISORY else "MINE"
            elif theirs_usd is None:
                status = "THEIRS"
            elif close(mine_usd, float(theirs_usd), rel_tol):
                status = "OK"
            elif column in ADVISORY:
                status = "INFO"
            else:
                status = "DIFF"

            note = None
            if status in ("DIFF", "INFO") and theirs_usd is not None and alternatives:
                noted = explain(alternatives, period, float(theirs_usd))
                if noted:
                    status, note = "ALT", noted

            rows.append(
                {
                    "ticker": ticker,
                    "metric": column,
                    "period": period,
                    "mine": mine_usd,
                    "theirs": None if theirs_usd is None else float(theirs_usd),
                    "delta": None
                    if (mine_usd is None or theirs_usd is None)
                    else mine_usd - float(theirs_usd),
                    "status": status,
                    "note": note,
                }
            )
    return rows


def refresh(ticker: str, filings: int) -> bool:
    """Regenerate the dashboard artifacts for one ticker."""
    print(f"  [*] extracting {ticker} ({filings} filings)")
    completed = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--ticker", ticker, "--filings", str(filings)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
        print(f"  [!] extractor failed for {ticker}: {' | '.join(tail)}")
        return False
    return True


def print_block(ticker: str, rows: list[dict], verbose: bool = False) -> dict:
    """Console table for one ticker, plus its counters."""
    statuses = ("OK", "ALT", "INFO", "DIFF", "MINE", "THEIRS", "N/A")
    counts = dict.fromkeys(statuses, 0)
    for row in rows:
        counts[row["status"]] += 1

    print(f"\n{ticker}  ({len(rows)} metric-years)")
    for row in rows:
        if row["status"] in ("OK", "N/A") or (row["status"] == "INFO" and not verbose):
            continue
        mine = "-" if row["mine"] is None else f"{row['mine']:,.0f}"
        theirs = "-" if row["theirs"] is None else f"{row['theirs']:,.0f}"
        note = f"  ({row['note']})" if row.get("note") else ""
        print(
            f"  [{row['status']:<6}] {row['metric']:<26} {row['period']}  "
            f"mine={mine:<16} theirs={theirs}{note}"
        )
    print(
        f"  -> {counts['OK']} ok, {counts['ALT']} alternative-concept, "
        f"{counts['INFO']} informational, {counts['DIFF']} diff, "
        f"{counts['MINE']} mine-missing, {counts['THEIRS']} theirs-missing, "
        f"{counts['N/A']} not-applicable"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=None, help="tickers to check")
    parser.add_argument("--years", type=int, default=5, help="years to pull (default 5)")
    parser.add_argument("--filings", type=int, default=5, help="filings for --refresh")
    parser.add_argument("--refresh", action="store_true", help="re-run the extractor first")
    parser.add_argument("--out", default=str(REPORT_DIR), help="report directory")
    parser.add_argument("--verbose", action="store_true", help="also print informational rows")
    args = parser.parse_args()

    tickers = [t.upper() for t in (args.tickers or DEFAULT_TICKERS)]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    failures = 0

    for ticker in tickers:
        if not TICKER_RE.match(ticker):
            print(f"  [!] invalid ticker: {ticker}")
            failures += 1
            continue

        if args.refresh and not refresh(ticker, args.filings):
            failures += 1
            continue

        mine = load_mine(ticker)
        if mine is None:
            failures += 1
            continue

        try:
            annual, payload = load_theirs(ticker, args.years)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            print(f"\n{ticker}: edgar-analytics failed ({exc.__class__.__name__}: {exc})")
            failures += 1
            continue

        (out_dir / f"{ticker}_theirs.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

        rows = compare(ticker, mine, annual, load_alternatives(ticker))
        pd.DataFrame(rows).to_csv(out_dir / f"{ticker}.csv", index=False)
        counts = print_block(ticker, rows, verbose=args.verbose)
        summary.append({"ticker": ticker, **counts, "years": len(mine)})
        if counts["DIFF"] or counts["MINE"]:
            failures += 1

    if summary:
        pd.DataFrame(summary).to_csv(out_dir / "summary.csv", index=False)
        keys = ("OK", "ALT", "INFO", "DIFF", "MINE", "THEIRS", "N/A")
        total = {k: sum(s[k] for s in summary) for k in keys}
        print(
            f"\nTOTAL: {total['OK']} ok, {total['ALT']} alternative-concept, "
            f"{total['INFO']} informational, {total['DIFF']} diff, "
            f"{total['MINE']} mine-missing, {total['THEIRS']} theirs-missing, "
            f"{total['N/A']} not-applicable"
        )
        print(f"Reports written to {out_dir}/")
        if total["DIFF"]:
            print("Investigate DIFF rows: two independent readers disagree on a fact.")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
