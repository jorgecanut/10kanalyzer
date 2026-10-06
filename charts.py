"""Chart rendering for the SEC EDGAR 10-K dashboard.

All charts read from the CSVs written by ``edgar_dashboard.py``:
``financial_data.csv`` for ratio/metric inputs, ``statements.csv`` for
statement lines, and ``facts.csv`` for items such as shares outstanding.
"""

from __future__ import annotations

import os
from typing import Iterable

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

USD_TO_MILLIONS = 1_000_000
CHART_WINDOW = 5

sns.set_theme(style="whitegrid", context="talk")
plt.rcParams["figure.autolayout"] = False

# --------------------------------------------------------------------------- #
# Color palette: one color per metric across every chart.
# --------------------------------------------------------------------------- #
COLORS: dict[str, str] = {
    # Income statement
    "Revenue": "#4c72b0",
    "Cost of Revenue": "#c44e52",
    "Gross Profit": "#55a868",
    "SG&A": "#8172b2",
    "R&D": "#937860",
    "Operating Income": "#da8bc3",
    "Interest Expense": "#ccb974",
    "Goodwill Impairment": "#64b5cd",
    "Asset Writedown": "#8c8c8c",
    "Net Income": "#e377c2",
    # Balance sheet
    "Total Assets": "#4c72b0",
    "Net PP&E": "#dd8452",
    "Goodwill": "#55a868",
    "Other Intangibles": "#c44e52",
    "Inventory": "#937860",
    "Total Current Liabilities": "#8172b2",
    "Total Liabilities": "#da8bc3",
    "Total Equity": "#2ca02c",
    "Retained Earnings": "#ccb974",
    "Total Shares Outstanding": "#111827",
    # Debt
    "Long-Term Debt": "#4c72b0",
    "Current Portion Leases": "#c44e52",
    "Capital Leases": "#55a868",
    "Total Debt": "#111827",
    "Cash & Equivalents": "#2ca02c",
    "Net Debt": "#d62728",
    # Cash flow operations
    "D&A": "#8172b2",
    "Capital Expenditure": "#dd8452",
    "Operating Cash Flow": "#2ca02c",
    "Change in NWC": "#937860",
    "Free Cash Flow": "#111827",
    # Cash flow financing
    "Stock Issuance": "#55a868",
    "Stock Repurchase": "#c44e52",
    "Dividends Paid": "#da8bc3",
    "Stock-Based Compensation": "#ccb974",
    # Margins / returns
    "Gross Margin": "#4c72b0",
    "Operating Margin": "#dd8452",
    "Net Margin": "#55a868",
    "ROE": "#4c72b0",
    "ROA": "#dd8452",
    "ROIC": "#55a868",
}

FALLBACK_PALETTE = sns.color_palette("husl", 24).as_hex()


# --------------------------------------------------------------------------- #
# Quality tracking: one row per metric x year.
# --------------------------------------------------------------------------- #
_QUALITY_ROWS: list[dict] = []


def _record_quality(metric: str, series: pd.Series, source: str) -> None:
    for year, value in series.items():
        _QUALITY_ROWS.append(
            {
                "metric": metric,
                "fiscal_year": int(year),
                "value": float(value) if pd.notna(value) else np.nan,
                "source": "missing" if pd.isna(value) else source,
            }
        )


def _write_quality_csv(output_dir: str) -> None:
    if not _QUALITY_ROWS:
        return
    pd.DataFrame(_QUALITY_ROWS).to_csv(
        os.path.join(output_dir, "data_quality.csv"), index=False
    )


# --------------------------------------------------------------------------- #
# Formatting helpers.
# --------------------------------------------------------------------------- #
def _fmt_mm(value: float) -> str:
    """Abbreviate a USD-millions value as XX,XX + unit."""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.2f}".replace(".", ",") + "T"
    if magnitude >= 1000:
        return f"{value / 1000:.2f}".replace(".", ",") + "B"
    return f"{value:.2f}".replace(".", ",") + "MM"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}".replace(".", ",") + "%"


def _fmt_shares(value: float) -> str:
    """Shares are passed in millions of shares."""
    return _fmt_mm(value).replace("MM", "M").replace("B", "B").replace("T", "T")


def _color_for(label: str) -> str:
    if label in COLORS:
        return COLORS[label]
    idx = abs(hash(label)) % len(FALLBACK_PALETTE)
    return FALLBACK_PALETTE[idx]


# --------------------------------------------------------------------------- #
# Data helpers.
# --------------------------------------------------------------------------- #
def _df_series(df: pd.DataFrame, col: str, years: list[int]) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=years)
    return df.set_index("fiscal_year")[col].reindex(years)


def _stmt_series(
    statements: pd.DataFrame,
    statement_type: str,
    concepts: Iterable[str],
    years: list[int],
    include_labels: Iterable[str] | None = None,
    exclude_labels: Iterable[str] | None = None,
    normalize_cashflow: bool = False,
) -> pd.Series:
    if statements.empty:
        return pd.Series(np.nan, index=years)
    statements = statements.copy()
    statements["value"] = pd.to_numeric(statements["value"], errors="coerce")
    statements["fiscal_year"] = pd.to_numeric(statements["fiscal_year"], errors="coerce").astype(int)
    std_mask = statements["standard_concept"].fillna("").isin(concepts)
    label_mask = pd.Series(False, index=statements.index)
    if include_labels:
        labels = statements["label"].fillna("").str.lower()
        for pat in include_labels:
            label_mask |= labels.str.contains(pat.lower(), regex=False)
        if exclude_labels:
            excl_mask = pd.Series(False, index=statements.index)
            for pat in exclude_labels:
                excl_mask |= labels.str.contains(pat.lower(), regex=False)
            label_mask &= ~excl_mask
    mask = (statements["statement"] == statement_type) & (std_mask | label_mask)
    sub = statements[mask]
    if sub.empty:
        return pd.Series(np.nan, index=years)
    if normalize_cashflow:
        labels = sub["label"].fillna("").str.lower()
        outflow = labels.str.contains(
            r"payment|repurchase|dividend|buyback|acquire treasury|redemption", regex=True
        )
        inflow = labels.str.contains(r"proceeds|issuance", regex=True)
        sub.loc[outflow, "value"] = -sub.loc[outflow, "value"].abs()
        sub.loc[inflow, "value"] = sub.loc[inflow, "value"].abs()
    s = sub.groupby("fiscal_year")["value"].sum() / USD_TO_MILLIONS
    return s.reindex(years)


def _fact_series(
    facts: pd.DataFrame,
    concepts: Iterable[str],
    years: list[int],
    agg: str = "sum",
) -> pd.Series:
    if facts.empty:
        return pd.Series(np.nan, index=years)
    facts = facts.copy()
    facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce").astype(int)
    concept_name = facts["concept"].str.split(":").str[-1]
    mask = concept_name.isin(concepts) & (~facts["is_dimensioned"].astype(bool))
    sub = facts[mask]
    if sub.empty:
        return pd.Series(np.nan, index=years)
    sub = sub.copy()
    sub["numeric"] = pd.to_numeric(sub["value"], errors="coerce")
    sub = sub.dropna(subset=["numeric"])
    if agg == "max":
        s = sub.groupby("fiscal_year")["numeric"].max()
    elif agg == "first":
        s = sub.groupby("fiscal_year")["numeric"].first()
    else:
        s = sub.groupby("fiscal_year")["numeric"].sum()
    s = s / USD_TO_MILLIONS
    return s.reindex(years)


def _shares_series(facts: pd.DataFrame, concepts: Iterable[str], years: list[int]) -> pd.Series:
    """Total shares outstanding: dedupe by concept, then sum across classes."""
    if facts.empty:
        return pd.Series(np.nan, index=years)
    facts = facts.copy()
    facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce").astype(int)
    concept_name = facts["concept"].str.split(":").str[-1]
    mask = concept_name.isin(concepts) & (~facts["is_dimensioned"].astype(bool))
    sub = facts[mask].copy()
    if sub.empty:
        return pd.Series(np.nan, index=years)
    sub["numeric"] = pd.to_numeric(sub["value"], errors="coerce")
    sub = sub.dropna(subset=["numeric"])
    # One value per concept per year, then add across share classes.
    s = sub.groupby(["fiscal_year", "concept"])["numeric"].first().groupby("fiscal_year").sum()
    s = s / USD_TO_MILLIONS
    return s.reindex(years)


def _year_windows(years: list[int], window: int = CHART_WINDOW) -> Iterable[list[int]]:
    years = sorted(years)
    for i in range(0, len(years), window):
        yield years[i : i + window]


def _window_suffix(window_years: list[int]) -> str:
    return f"{min(window_years)}_{max(window_years)}"


# --------------------------------------------------------------------------- #
# Matplotlib helpers.
# --------------------------------------------------------------------------- #
def _new_figure(title: str, subtitle: str | None = None) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(11, 6.5))
    full_title = title
    if subtitle:
        full_title += f"\n{subtitle}"
    ax.set_title(full_title, fontsize=14, fontweight="bold", pad=16)
    return fig, ax


def _save(fig: plt.Figure, path: str) -> str:
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _style_axes(ax: plt.Axes, years: list[int]) -> None:
    ax.set_xticks(range(len(years)))
    ax.set_xticklabels([str(y) for y in years], rotation=45, ha="right", fontsize=9)
    ax.tick_params(axis="y", labelsize=9)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    ax.axhline(0, color="black", linewidth=0.8)


def _label_bar(ax: plt.Axes, x: float, y: float, value: float, fmt: str = "mm") -> None:
    if pd.isna(value):
        return
    text = _fmt_mm(value) if fmt == "mm" else _fmt_pct(value)
    offset = 3 if value >= 0 else -3
    va = "bottom" if value >= 0 else "top"
    ax.annotate(
        text,
        (x, y),
        textcoords="offset points",
        xytext=(0, offset),
        ha="center",
        va=va,
        fontsize=5,
        rotation=0,
    )


def _legend(ax: plt.Axes, derived_labels: set[str]) -> None:
    handles, labels = ax.get_legend_handles_labels()
    if derived_labels:
        labels = [f"{lab} *" if lab in derived_labels else lab for lab in labels]
    ax.legend(handles, labels, loc="upper left", fontsize=8, framealpha=0.9)


# --------------------------------------------------------------------------- #
# Chart 1: Income statement (grouped bars, 5-year windows).
# --------------------------------------------------------------------------- #
def chart_income_statement(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> list[str]:
    metrics = [
        ("Revenue", _df_series(df, "Revenues", years), "reported"),
        ("Cost of Revenue", _df_series(df, "CostOfRevenue", years), "reported"),
        ("Gross Profit", _df_series(df, "GrossProfit", years), "derived"),
        ("SG&A", _df_series(df, "SellingGeneralAndAdministrativeExpense", years), "reported"),
        ("R&D", _df_series(df, "ResearchAndDevelopmentExpense", years), "reported"),
        ("Operating Income", _df_series(df, "OperatingIncomeLoss", years), "reported"),
        (
            "Interest Expense",
            _stmt_series(statements, "income", ["InterestExpense"], years),
            "reported",
        ),
        (
            "Goodwill Impairment",
            _stmt_series(statements, "income", ["GoodwillImpairmentLoss"], years),
            "reported",
        ),
        (
            "Asset Writedown",
            _stmt_series(
                statements,
                "income",
                [
                    "AssetImpairmentCharges",
                    "ImpairmentOfIntangibleAssetsExcludingGoodwill",
                    "ImpairmentOfLongLivedAssetsHeldAndUsed",
                    "ImpairmentOfLongLivedAssets",
                ],
                years,
            ),
            "derived",
        ),
        ("Net Income", _df_series(df, "NetIncomeLoss", years), "reported"),
    ]
    return _grouped_bar_windows(
        ticker, output_dir, "01_income_statement", metrics, years, ylabel="USD millions"
    )


# --------------------------------------------------------------------------- #
# Chart 2: Margins (lines, full period).
# --------------------------------------------------------------------------- #
def chart_margins(
    ticker: str, output_dir: str, df: pd.DataFrame, years: list[int]
) -> list[str]:
    metrics = [
        ("Gross Margin", _df_series(df, "GrossMargin", years), "derived"),
        ("Operating Margin", _df_series(df, "OperatingMargin", years), "derived"),
        ("Net Margin", _df_series(df, "NetMargin", years), "derived"),
    ]
    for label, series, source in metrics:
        _record_quality(label, series, source)
    fig, ax = _new_figure(f"{ticker} - Margins", "Gross / Operating / Net margin")
    for label, series, _source in metrics:
        ax.plot(range(len(years)), series.to_numpy(), marker="o", label=label, color=_color_for(label))
        for x, v in enumerate(series.to_numpy()):
            if pd.notna(v):
                # Net margin label below the point so it does not overlap gross.
                below = label == "Net Margin"
                ax.annotate(
                    _fmt_pct(v),
                    (x, v),
                    textcoords="offset points",
                    xytext=(0, -6 if below else 6),
                    ha="center",
                    va="top" if below else "bottom",
                    fontsize=6,
                )
    _style_axes(ax, years)
    ax.set_ylabel("Margin (%)", fontsize=10)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax.legend(loc="best", fontsize=8)
    path = _save(fig, os.path.join(output_dir, "02_margins_full.png"))
    return [path]


# --------------------------------------------------------------------------- #
# Chart 3: Balance sheet (grouped bars, 5-year windows; shares on right axis).
# --------------------------------------------------------------------------- #
def chart_balance(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
    years: list[int],
) -> list[str]:
    usd_metrics = [
        ("Total Assets", _stmt_series(statements, "balance", ["Assets"], years), "reported"),
        (
            "Net PP&E",
            _stmt_series(
                statements,
                "balance",
                ["NetPropertyPlantAndEquipment", "PlantPropertyEquipmentNet", "PropertyPlantAndEquipmentNet"],
                years,
            ),
            "reported",
        ),
        ("Goodwill", _stmt_series(statements, "balance", ["Goodwill"], years), "reported"),
        (
            "Other Intangibles",
            _stmt_series(
                statements,
                "balance",
                ["IntangibleAssetsNetExcludingGoodwill", "IntangibleAssetsNet", "IntangibleAssets"],
                years,
            ),
            "reported",
        ),
        (
            "Inventory",
            _stmt_series(statements, "balance", ["InventoryNet", "Inventory", "Inventories"], years),
            "reported",
        ),
        ("Total Current Liabilities", _df_series(df, "LiabilitiesCurrent", years), "reported"),
        ("Total Liabilities", _stmt_series(statements, "balance", ["Liabilities"], years), "reported"),
        ("Total Equity", _df_series(df, "StockholdersEquity", years), "reported"),
        (
            "Retained Earnings",
            _stmt_series(
                statements,
                "balance",
                ["RetainedEarningsAccumulatedDeficit", "RetainedEarnings"],
                years,
            ),
            "reported",
        ),
    ]
    shares = _shares_series(
        facts,
        [
            "EntityCommonStockSharesOutstanding",
            "CommonStockSharesOutstanding",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
        ],
        years,
    )
    _record_quality("Total Shares Outstanding", shares, "reported")

    paths: list[str] = []
    for window in _year_windows(years):
        suffix = _window_suffix(window)
        fig, ax = _new_figure(
            f"{ticker} - Balance Sheet", f"Fiscal years {suffix}"
        )
        positions = np.arange(len(window))
        width = 0.8 / len(usd_metrics)
        derived_labels: set[str] = set()

        for i, (label, series_full, source) in enumerate(usd_metrics):
            series = series_full.reindex(window)
            if series.dropna().empty:
                continue
            _record_quality(label, series, source)
            if source == "derived":
                derived_labels.add(label)
            offset = (i - (len(usd_metrics) - 1) / 2) * width
            bars = ax.bar(
                positions + offset,
                series.to_numpy(),
                width,
                label=label,
                color=_color_for(label),
            )
            for bar, v in zip(bars, series.to_numpy()):
                _label_bar(ax, bar.get_x() + bar.get_width() / 2, bar.get_height(), v)

        # Shares on right axis.
        if not shares.reindex(window).dropna().empty:
            ax2 = ax.twinx()
            ax2.plot(
                positions,
                shares.reindex(window).to_numpy(),
                color=_color_for("Total Shares Outstanding"),
                marker="s",
                linewidth=2,
                label="Total Shares Outstanding",
            )
            for x, v in enumerate(shares.reindex(window).to_numpy()):
                if pd.notna(v):
                    ax2.annotate(
                        _fmt_shares(v),
                        (x, v),
                        textcoords="offset points",
                        xytext=(0, 6),
                        ha="center",
                        va="bottom",
                        fontsize=5,
                        color=_color_for("Total Shares Outstanding"),
                    )
            ax2.set_ylabel("Shares outstanding (millions)", fontsize=10)
            ax2.tick_params(axis="y", labelsize=8)

        _style_axes(ax, window)
        ax.set_ylabel("USD millions", fontsize=10)
        _legend(ax, derived_labels)
        paths.append(_save(fig, os.path.join(output_dir, f"03_balance_{suffix}.png")))
    return paths


# --------------------------------------------------------------------------- #
# Chart 4: Debt (grouped bars, 5-year windows).
# --------------------------------------------------------------------------- #
def chart_debt(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> list[str]:
    lt_debt = _df_series(df, "LongTermDebt", years)
    # IFRS / foreign filers may not tag a plain LongTermDebt concept for all
    # years, so fill missing years from the balance-sheet statement.
    stmt_lt_debt = _stmt_series(
        statements,
        "balance",
        ["LongTermDebt", "LongTermDebtNoncurrent", "Borrowings"],
        years,
        include_labels=[
            "long-term portion of borrowings",
            "long-term borrowings",
            "non-current borrowings",
            "long-term debt",
        ],
    )
    lt_debt = lt_debt.combine_first(stmt_lt_debt)
    current_debt = _stmt_series(
        statements,
        "balance",
        [
            "LongTermDebtCurrent",
            "FinanceLeaseLiabilityCurrent",
            "CapitalLeaseObligationsCurrent",
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
        ],
        years,
        include_labels=[
            "current portion of borrowings",
            "current maturities of debt",
            "current portion of capital lease",
        ],
    )
    capital_leases = _stmt_series(
        statements,
        "balance",
        [
            "FinanceLeaseLiabilityNoncurrent",
            "CapitalLeaseObligationsNoncurrent",
            "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        ],
        years,
        include_labels=["finance lease liability", "capital lease"],
    )
    cash = _stmt_series(
        statements,
        "balance",
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashAndCashEquivalentsAtFairValue",
            "CashAndCashEquivalents",
            "CashAndMarketableSecurities",
        ],
        years,
    )
    total_debt = lt_debt.fillna(0) + current_debt.fillna(0) + capital_leases.fillna(0)
    total_debt = total_debt.replace(0, np.nan)
    net_debt = total_debt - cash

    metrics = [
        ("Long-Term Debt", lt_debt, "reported"),
        ("Current Portion Debt/Leases", current_debt, "reported"),
        ("Capital Leases", capital_leases, "reported"),
        ("Total Debt", total_debt, "derived"),
        ("Cash & Equivalents", cash, "reported"),
        ("Net Debt", net_debt, "derived"),
    ]
    return _grouped_bar_windows(
        ticker, output_dir, "04_debt", metrics, years, ylabel="USD millions"
    )


# --------------------------------------------------------------------------- #
# Chart 5: Cash flow operations (grouped bars, 5-year windows).
# --------------------------------------------------------------------------- #
def chart_cashflow_operations(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> list[str]:
    da = _stmt_series(
        statements,
        "cashflow",
        [
            "DepreciationAndAmortization",
            "Depreciation",
            "AmortizationOfIntangibleAssets",
            "DepreciationExpense",
            "AmortizationOfIntangibles",
        ],
        years,
        include_labels=["depreciation and amortization"],
    )
    nwc = (_df_series(df, "AssetsCurrent", years) - _df_series(df, "LiabilitiesCurrent", years)).diff()

    metrics = [
        ("Revenue", _df_series(df, "Revenues", years), "reported"),
        ("D&A", da, "reported"),
        ("Capital Expenditure", _df_series(df, "CapEx", years), "reported"),
        ("Operating Cash Flow", _df_series(df, "OperatingCashFlow", years), "reported"),
        ("Change in NWC", nwc, "derived"),
        ("Free Cash Flow", _df_series(df, "FreeCashFlow", years), "derived"),
    ]
    return _grouped_bar_windows(
        ticker, output_dir, "05_cashflow_operations", metrics, years, ylabel="USD millions"
    )


# --------------------------------------------------------------------------- #
# Chart 6: Cash flow financing (grouped bars, 5-year windows).
# --------------------------------------------------------------------------- #
def chart_cashflow_financing(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> list[str]:
    issuance = _stmt_series(
        statements,
        "cashflow",
        [
            "ProceedsFromIssuanceOfCommonStock",
            "ProceedsFromIssuanceOfCommonShares",
            "ProceedsFromStockOptionsExercised",
        ],
        years,
        include_labels=["issuance of common stock", "exercise of stock options"],
        exclude_labels=["debt"],
        normalize_cashflow=True,
    )
    repurchase = _stmt_series(
        statements,
        "cashflow",
        [
            "PaymentsForRepurchaseOfCommonStock",
            "PaymentsToAcquireTreasuryStock",
            "PaymentsForRepurchaseOfCommonShares",
        ],
        years,
        include_labels=["repurchase of common stock", "repurchases of common stock"],
        normalize_cashflow=True,
    )
    dividends = _stmt_series(
        statements,
        "cashflow",
        [
            "PaymentsOfDividends",
            "PaymentsOfDividendsCommonStock",
        ],
        years,
        include_labels=["dividends paid", "payments for dividends"],
        exclude_labels=["preferred"],
        normalize_cashflow=True,
    )
    sbc = _stmt_series(
        statements,
        "cashflow",
        ["ShareBasedCompensation", "NoncashShareBasedCompensation", "StockBasedCompensationExpense"],
        years,
        include_labels=["equity award compensation", "stock-based compensation", "share-based compensation"],
    )

    metrics = [
        ("Stock Issuance", issuance, "reported"),
        ("Stock Repurchase", repurchase, "reported"),
        ("Dividends Paid", dividends, "reported"),
        ("Stock-Based Compensation", sbc, "reported"),
    ]
    return _grouped_bar_windows(
        ticker, output_dir, "06_cashflow_financing", metrics, years, ylabel="USD millions"
    )


# --------------------------------------------------------------------------- #
# Chart 7: Returns (lines, full period).
# --------------------------------------------------------------------------- #
def chart_returns(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> list[str]:
    net_income = _df_series(df, "NetIncomeLoss", years)
    equity = _df_series(df, "StockholdersEquity", years)
    assets = _stmt_series(statements, "balance", ["Assets"], years)
    operating_income = _df_series(df, "OperatingIncomeLoss", years)
    lt_debt = _df_series(df, "LongTermDebt", years)

    roe = net_income / equity.replace(0, np.nan)
    roa = net_income / assets.replace(0, np.nan)
    invested_capital = equity.fillna(0) + lt_debt.fillna(0)
    roic = operating_income / invested_capital.replace(0, np.nan)

    metrics = [
        ("ROE", roe, "derived"),
        ("ROA", roa, "derived"),
        ("ROIC", roic, "derived"),
    ]
    for label, series, source in metrics:
        _record_quality(label, series, source)
    fig, ax = _new_figure(f"{ticker} - Returns", "ROE / ROA / ROIC")
    for label, series, _source in metrics:
        ax.plot(range(len(years)), series.to_numpy(), marker="o", label=label, color=_color_for(label))
        for x, v in enumerate(series.to_numpy()):
            if pd.notna(v):
                ax.annotate(
                    _fmt_pct(v),
                    (x, v),
                    textcoords="offset points",
                    xytext=(0, 6),
                    ha="center",
                    va="bottom",
                    fontsize=6,
                )
    _style_axes(ax, years)
    ax.set_ylabel("Return (%)", fontsize=10)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax.axhline(0, color="black", linewidth=0.8)
    ax.legend(loc="best", fontsize=8)
    path = _save(fig, os.path.join(output_dir, "07_returns_full.png"))
    return [path]


# --------------------------------------------------------------------------- #
# Generic grouped-bar renderer with 5-year windows.
# --------------------------------------------------------------------------- #
def _grouped_bar_windows(
    ticker: str,
    output_dir: str,
    filename_prefix: str,
    metrics: list[tuple[str, pd.Series, str]],
    years: list[int],
    ylabel: str,
) -> list[str]:
    paths: list[str] = []
    for window in _year_windows(years):
        suffix = _window_suffix(window)
        fig, ax = _new_figure(f"{ticker} - {filename_prefix.replace('_', ' ').title()}", f"Fiscal years {suffix}")
        positions = np.arange(len(window))
        # Only use metrics that have at least one value in this window.
        active = [
            (label, series_full.reindex(window), source)
            for label, series_full, source in metrics
            if not series_full.reindex(window).dropna().empty
        ]
        width = 0.8 / max(len(active), 1)
        derived_labels: set[str] = set()

        for i, (label, series, source) in enumerate(active):
            _record_quality(label, series, source)
            if source == "derived":
                derived_labels.add(label)
            offset = (i - (len(active) - 1) / 2) * width
            bars = ax.bar(
                positions + offset,
                series.to_numpy(),
                width,
                label=label,
                color=_color_for(label),
            )
            for bar, v in zip(bars, series.to_numpy()):
                _label_bar(ax, bar.get_x() + bar.get_width() / 2, bar.get_height(), v)

        _style_axes(ax, window)
        ax.set_ylabel(ylabel, fontsize=10)
        if active:
            _legend(ax, derived_labels)
        else:
            ax.text(
                0.5,
                0.5,
                "No data for this window",
                transform=ax.transAxes,
                ha="center",
                va="center",
                color="#6b7280",
            )
        paths.append(
            _save(fig, os.path.join(output_dir, f"{filename_prefix}_{suffix}.png"))
        )
    return paths


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #
def render_all(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
) -> list[str]:
    _QUALITY_ROWS.clear()
    years = sorted(df["fiscal_year"].dropna().astype(int).unique().tolist())
    if not years:
        return []

    paths: list[str] = []
    paths.extend(chart_income_statement(ticker, output_dir, df, statements, years))
    paths.extend(chart_margins(ticker, output_dir, df, years))
    paths.extend(chart_balance(ticker, output_dir, df, statements, facts, years))
    paths.extend(chart_debt(ticker, output_dir, df, statements, years))
    paths.extend(chart_cashflow_operations(ticker, output_dir, df, statements, years))
    paths.extend(chart_cashflow_financing(ticker, output_dir, df, statements, years))
    paths.extend(chart_returns(ticker, output_dir, df, statements, years))

    _write_quality_csv(output_dir)
    return paths
