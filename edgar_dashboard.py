#!/usr/bin/env python3
"""SEC EDGAR 10-K financial dashboard.

Downloads the last N 10-K filings for a target ticker straight from SEC EDGAR,
writes the data as CSV into ``financial_graphs/<TICKER>/`` and renders a fixed
set of charts from that data.

Usage:
    python edgar_dashboard.py
    python edgar_dashboard.py --ticker NFLX --filings 10
"""

from __future__ import annotations

import argparse
import os
from typing import Iterable

import numpy as np
import pandas as pd

from edgar import Company, set_identity

import charts
import xbrl_extract

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# The SEC requires a descriptive User-Agent identifying who is making requests.
# Override it in production via the EDGAR_IDENTITY env var (see docker-compose).
set_identity(os.environ.get("EDGAR_IDENTITY", "Data Analyst analyst@example.com"))

TICKER = "NFLX"
NUM_FILINGS = 10
BASE_OUTPUT_DIR = "financial_graphs"

# Resolved at runtime in main(): each ticker gets its own sub-directory so that
# running several companies does not overwrite previous results.
OUTPUT_DIR = BASE_OUTPUT_DIR
DATA_CSV = os.path.join(BASE_OUTPUT_DIR, "financial_data.csv")

# Money is reported in USD; we convert everything to millions for readability.
USD_TO_MILLIONS = 1_000_000

# Minimum span (in days) for a "duration" fact to count as the annual figure.
# This reliably filters out the quarterly facts that appear in the notes and
# the quarterly columns of comparative disclosures.
ANNUAL_MIN_DAYS = 300

# A selected fact's period end must sit this close to the filing's report date.
# Without it, a concept the filer did not tag in the current year falls back to
# the prior-year comparative in the same filing (QCOM FY2020 goodwill
# impairment was FY2019's 146M, not FY2020's 0).
PERIOD_MATCH_DAYS = 20

# Post-merger filers tag the consolidated statements under a successor/
# predecessor reporting basis using this axis. It marks a period's basis, not
# a line-item breakdown, so a scenario-only fact is a valid consolidated value
# and is used as a fallback when no dimension-free fact exists.
SCENARIO_AXIS = "us-gaap:StatementScenarioAxis"

# XBRL concept resolution order per metric. The first concept that yields an
# annual fact for the fiscal year wins. Netflix reports plain `Revenues`,
# `CostOfRevenue` and `LongTermDebtNoncurrent`, so those fallbacks matter.
METRIC_CONCEPTS: dict[str, tuple[list[str], str]] = {
    "Revenues": (
        [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            # KHC and others tag the "including assessed tax" variant (post-
            # ASC 606); omitting it silently drops revenue for those years.
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            # Older filings (pre-ASC 606) often split goods/services or use this.
            "SalesRevenueGoodsNet",
        ],
        "duration",
    ),
    "NetIncomeLoss": (
        [
            "NetIncomeLoss",
            # Used by companies with non-controlling interests / preferred
            # stock that stop tagging plain NetIncomeLoss (e.g. Estee Lauder
            # from FY2021 on).
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            # Last resort: total net income including non-controlling interests.
            "ProfitLoss",
        ],
        "duration",
    ),
    "GrossProfit": (["GrossProfit"], "duration"),
    "CostOfRevenue": (
        [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
            "CostOfGoodsSold",
            "CostOfServices",
        ],
        "duration",
    ),
    "SellingGeneralAndAdministrativeExpense": (
        [
            "SellingGeneralAndAdministrativeExpense",
            "SellingGeneralAndAdministrative",
            # Some filers split the two halves instead.
            "SellingAndMarketingExpense",
            "GeneralAndAdministrativeExpense",
        ],
        "duration",
    ),
    "ResearchAndDevelopmentExpense": (
        [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        ],
        "duration",
    ),
    "GoodwillImpairmentLoss": (
        [
            "GoodwillImpairmentLoss",
            # Combined goodwill + intangibles impairment, or the broader
            # catch-all some filers use for the annual writedown.
            "GoodwillAndIntangibleAssetImpairment",
            "AssetImpairmentCharges",
        ],
        "duration",
    ),
    "OperatingIncomeLoss": (["OperatingIncomeLoss"], "duration"),
    "OperatingCashFlow": (
        [
            "NetCashProvidedByUsedInOperatingActivities",
            # Older filings (pre-~2017) use the "continuing operations" variant.
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
        "duration",
    ),
    "CapEx": (
        [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ],
        "duration",
    ),
    "AssetsCurrent": (["AssetsCurrent"], "instant"),
    "LiabilitiesCurrent": (["LiabilitiesCurrent"], "instant"),
    "LongTermDebt": (
        [
            "LongTermDebt",
            "LongTermDebtNoncurrent",
            # Common balance-sheet line for companies that fold leases in,
            # and the tag Estee Lauder actually uses (no LongTermDebt tag).
            "LongTermDebtAndCapitalLeaseObligations",
        ],
        "instant",
    ),
    "StockholdersEquity": (
        [
            "StockholdersEquity",
            # Many filers only tag the total-equity variant (e.g. Estee
            # Lauder from FY2023 on, once it has non-controlling interests).
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        "instant",
    ),
}

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

def extract_metric(
    facts: pd.DataFrame,
    candidates: Iterable[str],
    period_of_report: str,
    period_type: str,
) -> float:
    """Return the value for the first matching concept, else nan.

    ``facts`` is the dataframe produced by ``xbrl.facts.to_dataframe()``.

    The governing period is located by matching each fact's end date against
    the filing's ``period_of_report`` date. We deliberately do *not* use the
    dataframe's ``fiscal_year`` column: for companies with non-calendar fiscal
    years it mislabels prior-year comparatives (e.g. Apple's FY2017 annual
    revenue is tagged ``fiscal_year=2018``), which silently duplicates years.

    Only dimension-free facts are considered. For duration concepts the fact
    must span an entire year (>= 300 days) so quarterly facts are ignored; the
    remaining candidate closest to the report date wins. For instant concepts
    the value on the balance-sheet date closest to the report date is used.
    When a concept has no dimension-free fact, a scenario-only fact (successor/
    predecessor reporting basis) is accepted as the consolidated value.
    """
    target = pd.Timestamp(period_of_report)
    is_dimensional = facts["is_dimensioned"].astype(bool)
    dimension = facts["dimension"] if "dimension" in facts.columns else pd.Series(pd.NA, index=facts.index)
    scenario_only = dimension == SCENARIO_AXIS
    concept_name = facts["concept"].str.split(":").str[-1]

    def pick(mask: pd.Series):
        sub = facts[mask & (concept_name == concept)].copy()
        if sub.empty:
            return np.nan
        sub["period_start"] = pd.to_datetime(sub["period_start"], errors="coerce")
        # Instant (balance-sheet) facts carry their date in ``period_instant``
        # while duration facts use ``period_end``; merge the two.
        sub["period_end"] = pd.to_datetime(sub["period_end"], errors="coerce")
        sub["period_end"] = sub["period_end"].fillna(
            pd.to_datetime(sub["period_instant"], errors="coerce")
        )
        sub = sub.dropna(subset=["period_end"])
        if period_type == "duration":
            span = (sub["period_end"] - sub["period_start"]).dt.days
            sub = sub[span >= ANNUAL_MIN_DAYS]
        if sub.empty:
            return np.nan
        # Closest period end to the filing's report date is the current year.
        sub["_dist"] = (sub["period_end"] - target).abs()
        sub = sub[sub["_dist"] <= pd.Timedelta(days=PERIOD_MATCH_DAYS)]
        if sub.empty:
            return np.nan
        # A filing often carries the same fact twice: a rounded copy
        # (decimals=-5) and a precise one (decimals=-3). Prefer the precise one.
        if "decimals" in sub.columns:
            sub["_decimals"] = pd.to_numeric(sub["decimals"], errors="coerce").fillna(-99)
        else:
            sub["_decimals"] = -99
        sub = sub.sort_values(["_dist", "_decimals"], ascending=[True, False])
        return pd.to_numeric(sub.iloc[0]["numeric_value"], errors="coerce")

    for concept in candidates:
        # Prefer dimension-free facts; fall back to a scenario-only fact (a
        # successor/predecessor reporting basis) when the filing has none.
        for mask in ((~is_dimensional), scenario_only):
            value = pick(mask)
            if pd.notna(value):
                return float(value)

    return np.nan


def build_dataset(ticker: str, n_filings: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch the last ``n_filings`` 10-Ks and assemble the datasets.

    Returns ``(ratios, statements, facts)``:
    - ``ratios`` — the wide per-year summary used by the ratio charts.
    - ``statements`` — long-form, every dimension-free primary-statement line.
    - ``facts`` — long-form, every fact for the period, dimensions included.
    """
    company = Company(ticker)
    print(f"[+] Company: {company.name} (CIK {company.cik})")

    # ``amendments=False`` keeps out the 10-K/A duplicates; we also guard with an
    # exact form match. A few extra filings are pulled so a filing without
    # parseable XBRL does not leave us short of ten fiscal years.
    filings = company.get_filings(form="10-K", amendments=False)
    filings = [f for f in filings if f.form == "10-K"]

    records: list[dict] = []
    statement_records: list[dict] = []
    fact_records: list[dict] = []
    seen_periods: set[str] = set()

    for filing in filings:
        if len(records) >= n_filings:
            break

        # Everything is guarded per filing: edgartools can raise on individual
        # malformed filings (e.g. missing filing dates) and one bad filing
        # should not abort the whole run.
        try:
            period_of_report = filing.period_of_report
            if period_of_report is None:
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
            xbrl = filing.xbrl()
            if xbrl is None:
                print(f"    [!] {period_key} ({accession}): no XBRL; skipping")
                continue

            facts = xbrl.facts.to_dataframe()

            # The filer's own fiscal-year label (authoritative, unlike the
            # calendar year of the period end).
            fy_facts = facts.loc[facts["concept"] == "dei:DocumentFiscalYearFocus", "value"]
            if len(fy_facts):
                fiscal_year = int(float(fy_facts.iloc[0]))
            else:
                fiscal_year = int(period_key[:4])

            row: dict = {"period_of_report": period_key, "fiscal_year": fiscal_year}
            for metric, (concepts, period_type) in METRIC_CONCEPTS.items():
                row[metric] = extract_metric(facts, concepts, period_of_report, period_type)
            statement_records.extend(xbrl_extract.extract_statements(xbrl, period_key))
            fact_records.extend(xbrl_extract.extract_facts(xbrl, period_key))
        except Exception as exc:  # noqa: BLE001 - skip unparseable filings
            ident = locals().get("accession") or getattr(filing, "accession_no", "?")
            print(f"    [!] {ident}: unreadable ({exc.__class__.__name__}: {exc}); skipping")
            continue

        records.append(row)
        seen_periods.add(period_key)
        print(f"    [+] FY{fiscal_year} (period end {period_key}): extracted {accession}")

    if not records:
        raise RuntimeError(
            f"No parseable 10-K filings found for {ticker!r}. It may be a "
            "foreign private issuer (e.g. files 20-F), a recent registrant, or "
            "otherwise outside the US-GAAP 10-K universe."
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

    return df, statements, facts


# --------------------------------------------------------------------------- #
# Cleaning + derived metrics
# --------------------------------------------------------------------------- #

def clean_and_derive(df: pd.DataFrame) -> pd.DataFrame:
    """Convert to millions, derive missing/derived metrics, sort by year."""
    value_cols = list(METRIC_CONCEPTS.keys())

    # Convert USD -> millions and force numeric dtypes.
    for col in value_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce") / USD_TO_MILLIONS

    # Netflix only tags GrossProfit quarterly in its notes for the earlier
    # filings, so fall back to the standard definition Revenue - Cost of
    # Revenue, which is consistent across the whole window.
    derived_gross = df["Revenues"] - df["CostOfRevenue"]
    df["GrossProfit"] = df["GrossProfit"].fillna(derived_gross)
    df["gross_profit_derived"] = df["GrossProfit"].eq(derived_gross)

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


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

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
    print(f"{TICKER} financial summary (USD millions; margins in %)")
    print("=" * 100)
    print(summary.to_string(index=False))
    print("=" * 100 + "\n")


def main() -> None:
    global OUTPUT_DIR, DATA_CSV, TICKER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=TICKER, help="Ticker symbol (default: %(default)s)")
    parser.add_argument(
        "--filings", type=int, default=NUM_FILINGS,
        help="Number of 10-K filings to use (default: %(default)s)",
    )
    args = parser.parse_args()

    # Point the module globals at a per-ticker output directory.
    TICKER = args.ticker.upper()
    OUTPUT_DIR = os.path.join(BASE_OUTPUT_DIR, TICKER)
    DATA_CSV = os.path.join(OUTPUT_DIR, "financial_data.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[*] Extracting {args.ticker} data from the last {args.filings} 10-K filings")
    raw, statements, facts = build_dataset(args.ticker, args.filings)
    df = clean_and_derive(raw)

    df.to_csv(DATA_CSV, index=False)
    print(f"[+] Wrote ratio data to {DATA_CSV}")

    # XBRL-driven artifacts: everything the filer stated, not a curated list.
    if not statements.empty:
        statements.to_csv(os.path.join(OUTPUT_DIR, "statements.csv"), index=False)
        print(f"[+] Wrote {len(statements)} statement lines to statements.csv")
    if not facts.empty:
        facts.to_csv(os.path.join(OUTPUT_DIR, "facts.csv"), index=False)
        notes = pd.DataFrame(xbrl_extract.extract_note_schedules(facts.to_dict("records")))
        if not notes.empty:
            notes.to_csv(os.path.join(OUTPUT_DIR, "notes.csv"), index=False)
        print(f"[+] Wrote {len(facts)} facts and {len(notes)} note rows")

    print_summary(df)

    print("[*] Rendering charts")
    rendered = charts.render_all(TICKER, OUTPUT_DIR, df, statements, facts)
    print(f"    [+] {len(rendered)} charts written")

    print(f"\n[+] Done. {len(rendered)} charts and CSV data written to '{OUTPUT_DIR}/'.")


if __name__ == "__main__":
    main()
