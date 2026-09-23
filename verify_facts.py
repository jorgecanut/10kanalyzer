#!/usr/bin/env python3
"""Independently verify a dashboard's extracted facts against SEC's XBRL API.

The dashboard (``edgar_dashboard.py``) reads facts by parsing a filing's raw
XBRL with edgartools. This script re-fetches the *same* facts from SEC's own
endpoint (``https://data.sec.gov/api/xbrl/companyconcept/...``) -- a different
source and code path -- and matches each fiscal year to the exact 10-K
accession it was extracted from, so restatements in later filings do not create
false mismatches.

Usage:
    python verify_facts.py                       # verifies the default set
    python verify_facts.py NFLX AAPL MSFT
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

import pandas as pd

from edgar import Company, set_identity

set_identity("Data Analyst analyst@example.com")

HEADERS = {"User-Agent": "Data Analyst analyst@example.com", "Accept-Encoding": "identity"}

DURATION_TAGS = {
    "Revenues": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "NetIncomeLoss": ["NetIncomeLoss"],
    "OperatingIncomeLoss": ["OperatingIncomeLoss"],
}
INSTANT_TAGS = {
    "AssetsCurrent": ["AssetsCurrent"],
    "LiabilitiesCurrent": ["LiabilitiesCurrent"],
    "StockholdersEquity": ["StockholdersEquity"],
}

DEFAULT_TICKERS = ["CAPNR", "TTMI", "FNKO", "AQST"]


def fetch_concept(cik: int, tag: str):
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def build_map(data, duration: bool) -> dict:
    """(accession, end_date) -> value from annual 10-K facts."""
    out = {}
    for unit, entries in (data or {}).get("units", {}).items():
        if unit != "USD":
            continue
        for e in entries:
            if not str(e.get("form", "")).startswith("10-K"):
                continue
            end = e.get("end")
            if not end:
                continue
            if duration:
                start = e.get("start")
                if not start:
                    continue
                span = (pd.Timestamp(end) - pd.Timestamp(start)).days
                if not (300 <= span <= 400):
                    continue
            out[(e.get("accn"), end)] = float(e["val"])
    return out


def verify_ticker(ticker: str) -> tuple[int, int]:
    company = Company(ticker)
    cik = int(company.cik)
    filings = [f for f in company.get_filings(form="10-K", amendments=False) if f.form == "10-K"]
    period2acc = {
        str(f.period_of_report): f.accession_no
        for f in filings
        if f.period_of_report is not None
    }

    csv_path = f"financial_graphs/{ticker}/financial_data.csv"
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        print(f"\n{ticker}: no dashboard output at {csv_path}; skipping.")
        return 0, 0

    print(f"\n{'='*80}\n{ticker} - {company.name} (CIK {cik})  |  {len(df)} fiscal years\n{'='*80}")
    total_ok = total_bad = 0

    for metric, tags in {**DURATION_TAGS, **INSTANT_TAGS}.items():
        duration = metric in DURATION_TAGS
        # Candidate tags in priority order; a higher-priority tag must not be
        # overwritten by a lower-priority one (e.g. MSFT tags total revenue as
        # SalesRevenueNet but also tags a smaller SalesRevenueGoodsNet for the
        # same period in the same filing).
        sec_map = {}
        for tag in tags:
            for key, value in build_map(fetch_concept(cik, tag), duration).items():
                sec_map.setdefault(key, value)
            time.sleep(0.15)
        if not sec_map:
            print(f"  [SKIP] {metric:<19} SEC API has no annual 10-K facts")
            continue

        ok = bad = 0
        for _, row in df.iterrows():
            period = str(row["period_of_report"])
            mine = row[metric] * 1_000_000  # dashboard is USD millions
            key = (period2acc.get(period), period)
            if key not in sec_map:
                continue
            sec = sec_map[key]
            if pd.isna(mine):
                print(f"    [MISS] {metric} FY{row['fiscal_year']}: dashboard NaN, SEC={sec:,.0f}")
                bad += 1
            elif abs(mine - sec) <= max(abs(sec) * 1e-6, 1.0):
                ok += 1
            else:
                print(f"    [DIFF] {metric} FY{row['fiscal_year']}: dashboard={mine:,.0f} SEC={sec:,.0f}")
                bad += 1
        print(f"  [{'PASS' if bad == 0 and ok else 'FAIL'}] {metric:<19} {ok} match / {bad} problem")
        total_ok += ok
        total_bad += bad

    return total_ok, total_bad


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    total_ok = total_bad = 0
    for ticker in tickers:
        ok, bad = verify_ticker(ticker.upper())
        total_ok += ok
        total_bad += bad

    print(f"\n{'='*80}")
    print(f"TOTAL: {total_ok} year-values matched exactly, {total_bad} problems")
    print("=" * 80)
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
