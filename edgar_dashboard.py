#!/usr/bin/env python3
"""SEC EDGAR 10-K financial health dashboard.

Downloads the last ten 10-K filings for a target ticker straight from SEC
EDGAR, extracts a fixed set of XBRL facts (income statement, cash flow and
balance sheet), derives a handful of health metrics and renders the charts plus
a full XBRL-driven data dump into ``financial_graphs/``.

Usage:
    python edgar_dashboard.py
    python edgar_dashboard.py --ticker NFLX --filings 10
"""

from __future__ import annotations

import argparse
import os
import re
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

# Every dimension-free primary-statement line, long form (one row per period x
# line), produced by xbrl_extract. Populated by build_dataset() so the charts
# can show whatever each filer states without a hard-coded metric list.
STATEMENTS: pd.DataFrame = pd.DataFrame()
# Every fact for each reporting period, dimensions included.
FACTS: pd.DataFrame = pd.DataFrame()

# Debt-like and cash-like balance lines, matched on concept/label text.
DEBT_RE = re.compile(
    r"debt|borrow|notes? payable|commercial paper|line of credit|credit facility"
    r"|finance lease|capital lease|operating lease",
    re.IGNORECASE,
)
CASH_RE = re.compile(r"cash", re.IGNORECASE)
# Per-share and share-count amounts are not USD, so keep them out of USD charts.
PER_SHARE_RE = re.compile(
    r"per share|earnings per|in shares|number of shares|weighted.average.{0,20}shares",
    re.IGNORECASE,
)

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
        f"Last {len(df)} fiscal years (USD millions)",
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
    """Line chart: gross, operating and net margin over the window."""
    fig, ax = _new_figure(
        f"{TICKER} - Margin Trends",
        "Gross, operating and net margin by fiscal year",
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
    sns.lineplot(
        x=positions, y=df["NetMargin"],
        marker="^", linewidth=2.5, label="Net Margin", ax=ax,
    )
    ax.axhline(0, color="black", linewidth=0.8)
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


def _pretty(name: str) -> str:
    """Turn a camelCase standardized concept into spaced words for a legend."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name)).strip()


def _row_label(row) -> str:
    """Prefer the standardized concept name; fall back to the filing's label."""
    standard = row.get("standard_concept")
    base = standard if isinstance(standard, str) and standard else row.get("label", "")
    return _pretty(base)[:42]


def _snapshot_label(row) -> str:
    """Filer's own wording preferred, so repeated standard names stay distinct."""
    label, standard = row.get("label"), row.get("standard_concept")
    base = label if isinstance(label, str) and label else (standard if isinstance(standard, str) else "")
    return _pretty(base)[:46]


def _statement_snapshot(statements: pd.DataFrame, statement: str, filename: str, title: str) -> str:
    """Horizontal bars of every line the filer reported, latest fiscal year.

    The statement CSV holds all years; this view trades history for
    completeness, so a filer's full line-item set is visible at a glance.
    """
    rows = statements[statements["statement"] == statement].copy() if statements is not None and not statements.empty else pd.DataFrame()
    years = rows["fiscal_year"].dropna()
    if rows.empty or years.empty:
        latest, rows = None, pd.DataFrame()
    else:
        latest = years.max()
        rows = rows[rows["fiscal_year"] == latest].copy()
        text = rows["label"].fillna("").astype(str) + " " + rows["standard_concept"].fillna("").astype(str)
        rows = rows[~text.str.contains(PER_SHARE_RE, na=False)]
        rows["value_m"] = pd.to_numeric(rows["value"], errors="coerce") / USD_TO_MILLIONS
        rows = rows[rows["value_m"].notna()]

    fig, ax = plt.subplots(figsize=(12, max(5.0, 0.42 * max(len(rows), 1) + 2)))
    if rows.empty:
        ax.text(0.5, 0.5, "No statement data", ha="center", va="center",
                color="#6b7280", transform=ax.transAxes)
        ax.set_yticks([])
    else:
        rows = rows.iloc[::-1].reset_index(drop=True)  # top-down reading order
        colors = ["#4c72b0" if v >= 0 else "#c44e52" for v in rows["value_m"]]
        ax.barh(range(len(rows)), rows["value_m"], color=colors)
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([_snapshot_label(r) for _, r in rows.iterrows()], fontsize=8)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.grid(axis="x", alpha=0.3)
        ax.set_axisbelow(True)
    ax.set_xlabel("USD millions", fontsize=11)
    ax.set_title(f"{TICKER} - {title}\nFY{latest} as reported (USD millions)",
                 fontsize=15, fontweight="bold", pad=16)
    return _save(fig, filename)


def _leaf_matrix(statements: pd.DataFrame, statement: str, section_prefix: str | None = None,
                 debt_only: bool = False, cash_only: bool = False):
    """Pivot dimension-free statement lines into (matrix[fiscal_year x concept], labels).

    Totals (any concept that is another row's parent) are dropped so stacked
    charts do not double count. Values are converted to USD millions.
    """
    rows = statements[statements["value"].notna()].copy() if statements is not None and not statements.empty else pd.DataFrame()
    if rows.empty or "statement" not in statements.columns:
        return pd.DataFrame(), {}
    rows = rows[rows["statement"] == statement]
    if section_prefix is not None:
        rows = rows[rows["section"].astype(str).str.upper().str.startswith(section_prefix.upper())]
    if debt_only or cash_only:
        text = (rows["concept"].fillna("").astype(str) + " " + rows["label"].fillna("").astype(str)
                + " " + rows["standard_concept"].fillna("").astype(str))
        pattern = DEBT_RE if debt_only else CASH_RE
        rows = rows[text.str.contains(pattern, na=False)]
    if rows.empty:
        return pd.DataFrame(), {}

    parents = set(statements["parent_concept"].dropna())
    rows = rows[~rows["concept"].isin(parents)]
    if rows.empty:
        return pd.DataFrame(), {}

    labels: dict[str, str] = {}
    for concept, group in rows.groupby("concept"):
        labels[concept] = _row_label(group.iloc[0])

    rows = rows.assign(value_m=pd.to_numeric(rows["value"], errors="coerce") / USD_TO_MILLIONS)
    matrix = rows.pivot_table(
        index="fiscal_year", columns="concept", values="value_m", aggfunc="sum"
    ).fillna(0.0)
    # Order items so the largest sit at the bottom of each stack.
    order = matrix.abs().mean().sort_values(ascending=False).index
    return matrix[order], labels


def _colors(count: int):
    return sns.color_palette("husl", max(count, 1))


def chart_income_statement(df: pd.DataFrame, statements: pd.DataFrame | None = None) -> str:
    statements = STATEMENTS if statements is None else statements
    return _statement_snapshot(statements, "income", "07_income_statement.png", "Income Statement")


def chart_cash_flow(df: pd.DataFrame, statements: pd.DataFrame | None = None) -> str:
    statements = STATEMENTS if statements is None else statements
    return _statement_snapshot(statements, "cashflow", "10_cash_flow.png", "Cash Flow Statement")


def chart_balance_sheet(df: pd.DataFrame, statements: pd.DataFrame | None = None) -> str:
    """Two stacked panels: assets, then liabilities + equity, for each year."""
    statements = STATEMENTS if statements is None else statements
    assets, asset_labels = _leaf_matrix(statements, "balance", section_prefix="ASSET")
    liab_equity, le_labels = _leaf_matrix(statements, "balance", section_prefix="LIABILIT")

    fig, (ax_assets, ax_le) = plt.subplots(1, 2, figsize=(16, 8), sharey=True)
    years = list(df["fiscal_year"])
    positions = _x_positions(df)

    for ax, matrix, labels, title in (
        (ax_assets, assets, asset_labels, "Assets"),
        (ax_le, liab_equity, le_labels, "Liabilities & Equity"),
    ):
        if matrix.empty:
            ax.text(0.5, 0.5, "No balance-sheet data", ha="center", va="center", color="#6b7280")
        else:
            palette = _colors(len(matrix.columns))
            bottom = np.zeros(len(years))
            for color, concept in zip(palette, matrix.columns):
                values = matrix[concept].reindex(years).fillna(0.0).to_numpy()
                ax.bar(positions, values, bottom=bottom, width=0.75,
                       color=color, label=labels.get(concept, concept))
                bottom += values
        ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
        ax.set_xlabel("Fiscal Year", fontsize=11)
        ax.set_xticks(positions)
        ax.set_xticklabels([str(y) for y in years], rotation=45, ha="right")
        ax.axhline(0, color="black", linewidth=0.8)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    ax_assets.set_ylabel("USD millions", fontsize=11)

    handles, legend_labels = [], []
    for ax in (ax_assets, ax_le):
        for handle, label in zip(*ax.get_legend_handles_labels()):
            if label not in legend_labels:
                handles.append(handle)
                legend_labels.append(label)
    if handles:
        fig.legend(handles, legend_labels, loc="lower center", ncols=6,
                   fontsize=7, frameon=False, bbox_to_anchor=(0.5, 0.0))
        fig.tight_layout(rect=(0, 0.16, 1, 1))
    return _save(fig, "08_balance_sheet.png")


def chart_debt(df: pd.DataFrame, statements: pd.DataFrame | None = None) -> str:
    """Stacked debt components with cash and net-debt lines by fiscal year."""
    statements = STATEMENTS if statements is None else statements
    debt, debt_labels = _leaf_matrix(statements, "balance", debt_only=True)
    cash_rows, _ = _leaf_matrix(statements, "balance", cash_only=True)

    fig, ax = plt.subplots(figsize=(13, 7))
    years = list(df["fiscal_year"])
    positions = _x_positions(df)

    if debt.empty:
        ax.text(0.5, 0.5, "No debt lines found", ha="center", va="center")
        total = pd.Series(dtype=float)
    else:
        palette = _colors(len(debt.columns))
        bottom = np.zeros(len(years))
        for color, concept in zip(palette, debt.columns):
            values = debt[concept].reindex(years).fillna(0.0).to_numpy()
            ax.bar(positions, values, bottom=bottom, width=0.7,
                   color=color, label=debt_labels.get(concept, concept))
            bottom += values
        total = debt.reindex(years).sum(axis=1)

    cash = cash_rows.reindex(years).sum(axis=1).fillna(0.0) if not cash_rows.empty else pd.Series(0.0, index=years)

    if not total.empty:
        net = total - cash
        ax.plot(positions, total.to_numpy(), color="#111827", marker="o",
                linewidth=2, label="Total debt")
        ax.plot(positions, net.to_numpy(), color="#d62728", marker="s",
                linewidth=2, linestyle="--", label="Net debt (debt − cash)")
    if cash.abs().sum() > 0:
        ax.plot(positions, cash.to_numpy(), color="#2ca02c", marker="^",
                linewidth=2, label="Cash & equivalents")

    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(f"{TICKER} - Debt Overview",
                 fontsize=15, fontweight="bold", pad=16)
    ax.set_xlabel("Fiscal Year", fontsize=11)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.set_xticks(positions)
    ax.set_xticklabels([str(y) for y in years])
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, ncols=4, fontsize=8,
              loc="upper center", bbox_to_anchor=(0.5, -0.12))
    return _save(fig, "09_debt_overview.png")


def chart_segments(df: pd.DataFrame, facts: pd.DataFrame | None = None) -> str:
    """Revenue by business segment over time, if the filer reports segments."""
    facts = FACTS if facts is None else facts
    fig, ax = plt.subplots(figsize=(13, 7))
    years = list(df["fiscal_year"])
    positions = _x_positions(df)

    seg = pd.DataFrame()
    if facts is not None and not facts.empty:
        dimensional = facts["dimension"].astype(str).str.contains("BusinessSegmentsAxis", na=False)
        revenue = facts["concept"].astype(str).str.contains("Revenue|Sales", case=False, na=False)
        seg = facts[dimensional & revenue & facts["value"].notna()].copy()
        seg["value_m"] = pd.to_numeric(seg["value"], errors="coerce") / USD_TO_MILLIONS
        seg["segment"] = seg["member"].fillna(seg["dimension_label"]).astype(str).str.split(":").str[-1]
        seg["segment"] = seg["segment"].str.replace(r"(Segment)?Member$", "", regex=True)

    if seg.empty:
        ax.text(0.5, 0.5, "No segment revenue reported", ha="center", va="center",
                color="#6b7280", transform=ax.transAxes)
        ax.set_yticks([])
    else:
        matrix = seg.pivot_table(index="fiscal_year", columns="segment", values="value_m", aggfunc="sum").fillna(0.0)
        order = matrix.abs().mean().sort_values(ascending=False).index
        matrix = matrix[order]
        palette = _colors(len(matrix.columns))
        bottom = np.zeros(len(years))
        for color, segment in zip(palette, matrix.columns):
            values = matrix[segment].reindex(years).fillna(0.0).to_numpy()
            ax.bar(positions, values, bottom=bottom, width=0.75,
                   color=color, label=_pretty(segment)[:30])
            bottom += values

    ax.set_title(f"{TICKER} - Revenue by Segment",
                 fontsize=15, fontweight="bold", pad=16)
    ax.set_xlabel("Fiscal Year", fontsize=11)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.set_xticks(positions)
    ax.set_xticklabels([str(y) for y in years], rotation=45, ha="right")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    if not seg.empty:
        ax.legend(frameon=False, ncols=4, fontsize=8,
                  loc="upper center", bbox_to_anchor=(0.5, -0.12))
    return _save(fig, "11_segments.png")


def _standard_series(statements: pd.DataFrame, statement: str, standard: str, years: list):
    """Per-year value for a standard concept, taking the total when several
    lines share the name (e.g. a subtotal and the real total both map to
    NetCashFromOperatingActivities)."""
    if statements is None or statements.empty:
        return pd.Series(0.0, index=years)
    rows = statements[(statements["statement"] == statement) & (statements["standard_concept"] == standard)].copy()
    if rows.empty:
        return pd.Series(0.0, index=years)
    rows["v"] = pd.to_numeric(rows["value"], errors="coerce") / USD_TO_MILLIONS
    rows = rows[rows["v"].notna()]
    if rows.empty:
        return pd.Series(0.0, index=years)
    # Largest magnitude per year is the consolidated total, not a breakdown line.
    picked = rows.loc[rows.groupby("fiscal_year")["v"].apply(lambda s: s.abs().idxmax())]
    return picked.set_index("fiscal_year")["v"].reindex(years).fillna(0.0)


def chart_cash_flow_trend(df: pd.DataFrame, statements: pd.DataFrame | None = None) -> str:
    """Grouped bars: operating, investing and financing cash flow by year."""
    statements = STATEMENTS if statements is None else statements
    years = list(df["fiscal_year"])
    positions = _x_positions(df)
    series = [
        ("Operating cash flow", "NetCashFromOperatingActivities", "#2ca02c"),
        ("Investing cash flow", "NetCashFromInvestingActivities", "#4c72b0"),
        ("Financing cash flow", "NetCashFromFinancingActivities", "#c44e52"),
        ("Net change in cash", "NetChangeInCash", "#8172b3"),
    ]

    fig, ax = plt.subplots(figsize=(13, 7))
    count = len(series)
    width = 0.82 / count
    for i, (label, standard, color) in enumerate(series):
        values = _standard_series(statements, "cashflow", standard, years).to_numpy()
        ax.bar(positions + (i - (count - 1) / 2) * width, values, width=width,
               label=label, color=color)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(f"{TICKER} - Cash Flow Trend",
                 fontsize=15, fontweight="bold", pad=16)
    ax.set_ylabel("USD millions", fontsize=11)
    ax.legend(frameon=False, ncols=4, fontsize=9,
              loc="upper center", bbox_to_anchor=(0.5, -0.12))
    _style_axes(ax, df)
    return _save(fig, "12_cash_flow_trend.png")


CHARTS = (
    chart_revenue_vs_net_income,
    chart_margin_trends,
    chart_net_income_vs_operating_cash_flow,
    chart_free_cash_flow,
    chart_current_ratio,
    chart_debt_to_equity,
    chart_income_statement,
    chart_balance_sheet,
    chart_debt,
    chart_cash_flow,
    chart_segments,
    chart_cash_flow_trend,
)


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
    global OUTPUT_DIR, DATA_CSV, TICKER, STATEMENTS, FACTS

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
    raw, statements, facts = build_dataset(args.ticker, args.filings)
    STATEMENTS, FACTS = statements, facts
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
    for chart in CHARTS:
        chart(df)

    print(f"\n[+] Done. {len(CHARTS)} charts written to '{OUTPUT_DIR}/'.")


if __name__ == "__main__":
    main()
