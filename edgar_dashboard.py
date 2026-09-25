#!/usr/bin/env python3
"""SEC EDGAR 10-K financial health dashboard.

Downloads the last ten 10-K filings for a target ticker straight from SEC
EDGAR, extracts a fixed set of XBRL facts (income statement, cash flow and
balance sheet), derives a handful of health metrics and renders six charts
into ``financial_graphs/``.

Usage:
    python edgar_dashboard.py
    python edgar_dashboard.py --ticker NFLX --filings 10
"""

from __future__ import annotations

import argparse
import os
from typing import Iterable

import matplotlib

# Headless backend so the script runs on servers / CI without a display.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

from edgar import Company, set_identity

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

# XBRL concept resolution order per metric. The first concept that yields an
# annual fact for the fiscal year wins. Netflix reports plain `Revenues`,
# `CostOfRevenue` and `LongTermDebtNoncurrent`, so those fallbacks matter.
METRIC_CONCEPTS: dict[str, tuple[list[str], str]] = {
    "Revenues": (
        [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            # Older filings (pre-ASC 606) often split goods/services or use this.
            "SalesRevenueGoodsNet",
        ],
        "duration",
    ),
    "NetIncomeLoss": (["NetIncomeLoss"], "duration"),
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
    "LongTermDebt": (["LongTermDebt", "LongTermDebtNoncurrent"], "instant"),
    "StockholdersEquity": (["StockholdersEquity"], "instant"),
}

# Chart-friendly display labels.
LABELS = {
    "Revenues": "Revenue",
    "NetIncomeLoss": "Net Income",
    "GrossProfit": "Gross Profit",
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
    """
    target = pd.Timestamp(period_of_report)
    is_dimensional = facts["is_dimensioned"].astype(bool)

    for concept in candidates:
        concept_name = facts["concept"].str.split(":").str[-1]
        sub = facts[(~is_dimensional) & (concept_name == concept)].copy()
        if sub.empty:
            continue

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
                continue

        # Closest period end to the filing's report date is the current year.
        sub = sub.assign(_dist=(sub["period_end"] - target).abs())
        sub = sub.sort_values(["_dist", "period_end"])

        value = pd.to_numeric(sub.iloc[0]["numeric_value"], errors="coerce")
        if pd.notna(value):
            return float(value)

    return np.nan


def build_dataset(ticker: str, n_filings: int) -> pd.DataFrame:
    """Fetch the last ``n_filings`` 10-Ks and assemble a tidy dataframe."""
    company = Company(ticker)
    print(f"[+] Company: {company.name} (CIK {company.cik})")

    # ``amendments=False`` keeps out the 10-K/A duplicates; we also guard with an
    # exact form match. A few extra filings are pulled so a filing without
    # parseable XBRL does not leave us short of ten fiscal years.
    filings = company.get_filings(form="10-K", amendments=False)
    filings = [f for f in filings if f.form == "10-K"]

    records: list[dict] = []
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

    return df


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
# Chart helpers
# --------------------------------------------------------------------------- #

def _new_figure(title: str, subtitle: str | None = None) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(11, 6.5))
    heading = title if subtitle is None else f"{title}\n{subtitle}"
    ax.set_title(heading, fontsize=15, fontweight="bold", pad=16)
    fig.tight_layout()
    return fig, ax


def _x_positions(df: pd.DataFrame) -> np.ndarray:
    """Evenly spaced x positions shared by bars and overlay lines."""
    return np.arange(len(df))


def _style_axes(ax: plt.Axes, df: pd.DataFrame) -> None:
    positions = _x_positions(df)
    labels = [str(y) for y in df["fiscal_year"]]
    ax.set_xlabel("Fiscal Year", fontsize=11)
    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    # Long/disambiguated labels (e.g. "2024 (Jan)") would overlap when upright.
    if any(len(label) > 4 for label in labels):
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)
    ax.set_xlim(positions[0] - 0.6, positions[-1] + 0.6)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)


def _save(fig: plt.Figure, filename: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    [+] saved {path}")
    return path


def chart_revenue_vs_net_income(df: pd.DataFrame) -> str:
    """Grouped bar chart: revenue vs net income over ten years."""
    fig, ax = _new_figure(
        f"{TICKER} - Revenue vs. Net Income",
        "Last ten fiscal years (USD millions)",
    )
    melted = df.melt(
        id_vars="fiscal_year",
        value_vars=["Revenues", "NetIncomeLoss"],
        var_name="Metric",
        value_name="USD millions",
    )
    melted["Metric"] = melted["Metric"].map(LABELS)
    sns.barplot(
        data=melted,
        x="fiscal_year",
        y="USD millions",
        hue="Metric",
        palette="deep",
        ax=ax,
    )
    ax.set_ylabel("USD millions", fontsize=11)
    ax.legend(title="", frameon=False)
    _style_axes(ax, df)
    return _save(fig, "01_revenue_vs_net_income.png")


def chart_margin_trends(df: pd.DataFrame) -> str:
    """Line chart: gross margin and operating margin over ten years."""
    fig, ax = _new_figure(
        f"{TICKER} - Margin Trends",
        "Gross and operating margin by fiscal year",
    )
    positions = _x_positions(df)
    sns.lineplot(
        x=positions, y=df["GrossMargin"],
        marker="o", linewidth=2.5, label="Gross Margin", ax=ax,
    )
    sns.lineplot(
        x=positions, y=df["OperatingMargin"],
        marker="s", linewidth=2.5, label="Operating Margin", ax=ax,
    )
    ax.set_ylabel("Margin", fontsize=11)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax.legend(frameon=False)
    _style_axes(ax, df)
    return _save(fig, "02_margin_trends.png")


def chart_net_income_vs_operating_cash_flow(df: pd.DataFrame) -> str:
    """Grouped bar chart: net income vs operating cash flow (earnings quality)."""
    fig, ax = _new_figure(
        f"{TICKER} - Net Income vs. Operating Cash Flow",
        "Gap between reported earnings and cash generation (USD millions)",
    )
    melted = df.melt(
        id_vars="fiscal_year",
        value_vars=["NetIncomeLoss", "OperatingCashFlow"],
        var_name="Metric",
        value_name="USD millions",
    )
    melted["Metric"] = melted["Metric"].map(LABELS)
    sns.barplot(
        data=melted,
        x="fiscal_year",
        y="USD millions",
        hue="Metric",
        palette="muted",
        ax=ax,
    )
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.legend(title="", frameon=False)
    _style_axes(ax, df)
    return _save(fig, "03_net_income_vs_operating_cash_flow.png")


def chart_free_cash_flow(df: pd.DataFrame) -> str:
    """Area chart: free cash flow trajectory."""
    fig, ax = _new_figure(
        f"{TICKER} - Free Cash Flow Trajectory",
        "Operating cash flow minus capital expenditure (USD millions)",
    )
    positions = _x_positions(df)
    fcf = df["FreeCashFlow"]
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in fcf]
    ax.fill_between(positions, fcf, 0, color="#4c72b0", alpha=0.35, label="Free Cash Flow")
    ax.plot(positions, fcf, color="#1f3b73", linewidth=2.5, marker="o")
    ax.scatter(positions, fcf, c=colors, s=70, zorder=5)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.legend(frameon=False)
    _style_axes(ax, df)
    return _save(fig, "04_free_cash_flow_trajectory.png")


def chart_current_ratio(df: pd.DataFrame) -> str:
    """Grouped bar chart: current assets vs current liabilities + ratio line."""
    fig, ax = _new_figure(
        f"{TICKER} - Liquidity: Current Ratio",
        "Current assets vs. current liabilities (USD millions)",
    )
    melted = df.melt(
        id_vars="fiscal_year",
        value_vars=["AssetsCurrent", "LiabilitiesCurrent"],
        var_name="Metric",
        value_name="USD millions",
    )
    melted["Metric"] = melted["Metric"].map(LABELS)
    sns.barplot(
        data=melted,
        x="fiscal_year",
        y="USD millions",
        hue="Metric",
        palette="crest",
        ax=ax,
    )
    ax.set_ylabel("USD millions", fontsize=11)
    ax.set_ylim(0, ax.get_ylim()[1] * 1.18)
    ax.legend(title="", frameon=False, loc="upper left", ncols=2)

    ratio_ax = ax.twinx()
    ratio_ax.plot(
        _x_positions(df), df["CurrentRatio"],
        color="#c44e52", marker="D", linewidth=2, label="Current Ratio (x)",
    )
    ratio_ax.set_ylabel("Current Ratio (x)", fontsize=11, color="#c44e52")
    ratio_ax.tick_params(axis="y", colors="#c44e52")
    ratio_ax.legend(frameon=False, loc="upper right")

    _style_axes(ax, df)
    return _save(fig, "05_current_ratio.png")


def chart_debt_to_equity(df: pd.DataFrame) -> str:
    """Stacked bar chart: long-term debt vs equity + debt-to-equity line."""
    fig, ax = _new_figure(
        f"{TICKER} - Debt vs. Equity",
        "Stacked capital structure with debt-to-equity ratio",
    )
    positions = _x_positions(df)
    debt = df["LongTermDebt"].fillna(0).to_numpy()
    equity = df["StockholdersEquity"].fillna(0).to_numpy()

    ax.bar(positions, equity, label="Stockholders' Equity", color="#55a868", width=0.6)
    ax.bar(positions, debt, bottom=equity, label="Long-Term Debt", color="#c44e52", width=0.6)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.set_ylim(0, ax.get_ylim()[1] * 1.18)
    ax.legend(frameon=False, loc="upper left")

    ratio_ax = ax.twinx()
    ratio_ax.plot(
        positions, df["DebtToEquity"], color="#8172b3",
        marker="o", linewidth=2, label="Debt / Equity (x)",
    )
    ratio_ax.set_ylabel("Debt / Equity (x)", fontsize=11, color="#8172b3")
    ratio_ax.tick_params(axis="y", colors="#8172b3")
    ratio_ax.legend(frameon=False, loc="upper right")

    _style_axes(ax, df)
    return _save(fig, "06_debt_to_equity_trend.png")


CHARTS = (
    chart_revenue_vs_net_income,
    chart_margin_trends,
    chart_net_income_vs_operating_cash_flow,
    chart_free_cash_flow,
    chart_current_ratio,
    chart_debt_to_equity,
)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def print_summary(df: pd.DataFrame) -> None:
    display_cols = [
        "fiscal_year", "Revenues", "NetIncomeLoss", "GrossMargin",
        "OperatingMargin", "OperatingCashFlow", "FreeCashFlow",
        "CurrentRatio", "DebtToEquity",
    ]
    summary = df[display_cols].copy()
    summary["GrossMargin"] = (summary["GrossMargin"] * 100).round(1)
    summary["OperatingMargin"] = (summary["OperatingMargin"] * 100).round(1)
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

    sns.set_theme(style="whitegrid", context="talk")
    plt.rcParams["figure.autolayout"] = False

    print(f"[*] Building {args.ticker} dashboard from the last {args.filings} 10-K filings")
    raw = build_dataset(args.ticker, args.filings)
    df = clean_and_derive(raw)

    df.to_csv(DATA_CSV, index=False)
    print(f"[+] Wrote raw data to {DATA_CSV}")

    print_summary(df)

    print("[*] Rendering charts")
    for chart in CHARTS:
        chart(df)

    print(f"\n[+] Done. {len(CHARTS)} charts written to '{OUTPUT_DIR}/'.")


if __name__ == "__main__":
    main()
