"""Charts for the SEC EDGAR 10-K dashboard.

All charts read from the CSVs written by ``edgar_dashboard.py``:
``financial_data.csv`` for ratio/metric inputs, ``statements.csv`` for
statement lines, and ``facts.csv`` for items such as shares outstanding.

A run writes ``charts.json``: one spec per chart (years, series, colours,
formats, axes) and no images. The web app draws the specs with ECharts;
``render_pngs`` draws the same specs with matplotlib when PNGs are
downloaded, so the page and the images never disagree.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import threading
from typing import Iterable

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
from matplotlib import font_manager
from matplotlib.container import BarContainer
from matplotlib.patches import FancyBboxPatch
import matplotlib.ticker as mticker
from matplotlib.transforms import blended_transform_factory
import numpy as np
import pandas as pd
import seaborn as sns

USD_TO_MILLIONS = 1_000_000
CHART_WINDOW = 5

# --------------------------------------------------------------------------- #
# Font: the web app's own monospace, bundled in static/fonts so the charts match
# the page on every machine (Docker has no fonts installed).
# --------------------------------------------------------------------------- #
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
FONT_FAMILY = "Ubuntu Mono derivative Stocky"
for _font_file in ("UbuntuMono-Regular.ttf", "UbuntuMono-Bold.ttf"):
    _font_path = os.path.join(FONT_DIR, _font_file)
    if os.path.isfile(_font_path):
        font_manager.fontManager.addfont(_font_path)

# --------------------------------------------------------------------------- #
# Themes: a download picks light or dark. The colours are the app's own
# surface, text and line tokens. Sleek by default: no frame, no ticks, no
# vertical grid; only faint horizontal lines.
# --------------------------------------------------------------------------- #
LIGHT_INK = {"ink": "#8d919a", "label": "#1b1c1f", "muted": "#646770", "halo": "#ffffff", "strong": "#1b1c1f"}
DARK_INK = {"ink": "#8d919a", "label": "#ffffff", "muted": "#cfd2d8", "halo": "#2c2d31", "strong": "#ffffff"}


def _theme_rc(ink: dict, grid: str) -> dict:
    surface, text, muted = ink["halo"], ink["strong"], ink["muted"]
    return {
        "font.family": [FONT_FAMILY, "DejaVu Sans Mono", "monospace"],
        "font.size": 10,
        "figure.autolayout": False,
        "figure.facecolor": surface,
        "axes.facecolor": surface,
        "savefig.facecolor": surface,
        "savefig.edgecolor": surface,
        "patch.edgecolor": surface,
        "axes.spines.left": False,
        "axes.spines.right": False,
        "axes.spines.top": False,
        "axes.spines.bottom": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "axes.axisbelow": True,
        "grid.color": grid,
        "grid.linewidth": 0.8,
        "grid.linestyle": "-",
        "axes.labelcolor": muted,
        "axes.titlelocation": "left",
        "axes.titlecolor": text,
        "text.color": text,
        "xtick.color": muted,
        "ytick.color": muted,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.major.pad": 6,
        "ytick.major.pad": 6,
        "legend.frameon": False,
        "lines.linewidth": 2,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    }


LIGHT_RC = _theme_rc(LIGHT_INK, grid="#e7e8ec")
DARK_RC = _theme_rc(DARK_INK, grid="#3a3c41")
plt.rcParams.update(LIGHT_RC)
_INK = LIGHT_INK


def _c(role: str) -> str:
    """Theme colour for non-data ink: zero lines, labels, halos."""
    return _INK[role]

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
    "Net Income": "#e07b39",   # orange: the old pink sat next to Operating Income's
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
def _compact(value: float, prefix: str = "", decimals: int = 2, strip: bool = False) -> str:
    """A millions value as $29,70B / −$765,62M (decimal comma, true minus)."""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        number, unit = magnitude / 1_000_000, "T"
    elif magnitude >= 1000:
        number, unit = magnitude / 1000, "B"
    else:
        number, unit = magnitude, "M"
    text = f"{number:.{decimals}f}"
    if strip and "." in text:
        text = text.rstrip("0").rstrip(".")
    sign = "\u2212" if value < 0 else ""
    return f"{sign}{prefix}{text.replace('.', ',')}{unit}"


def _fmt_mm(value: float) -> str:
    """Abbreviate a USD-millions value as $XX,XXB."""
    return _compact(value, "$")


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}".replace(".", ",").replace("-", "\u2212") + "%"


def _fmt_shares(value: float) -> str:
    """Shares are passed in millions of shares."""
    return _compact(value)


def _axis_money(value: float, _pos=None) -> str:
    """Y-axis ticks for USD millions: $10B, $2,5B, 0."""
    return "0" if value == 0 else _compact(value, "$", decimals=1, strip=True)


def _axis_shares(value: float, _pos=None) -> str:
    # Two decimals: share counts move little, and one decimal repeated "4,3B".
    return "0" if value == 0 else _compact(value, decimals=2, strip=True)


def _axis_pct(value: float, _pos=None) -> str:
    return f"{value * 100:.0f}%".replace("-", "\u2212")


def _span(years: list[int]) -> str:
    return f"{years[0]}\u2013{years[-1]}" if years else ""


def _base_color(label: str) -> str:
    """The metric's colour for a spec; ``strong`` is the theme's text colour."""
    if label in COLORS:
        # Near-black series would vanish on the dark background.
        return "strong" if COLORS[label] == "#111827" else COLORS[label]
    idx = int(hashlib.md5(label.encode()).hexdigest(), 16) % len(FALLBACK_PALETTE)
    return FALLBACK_PALETTE[idx]


def _ink_color(color: str) -> str:
    """A spec colour for the theme being drawn."""
    return _c("strong") if color == "strong" else color


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
    statements["fiscal_year"] = pd.to_numeric(statements["fiscal_year"], errors="coerce").astype("Int64")
    statements = statements.dropna(subset=["fiscal_year"])
    concepts = list(concepts)
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
    priority = {concept: i for i, concept in enumerate(concepts)}
    sub = sub.assign(
        _priority=sub["standard_concept"].map(priority).fillna(len(concepts))
    ).sort_values(["fiscal_year", "_priority"])
    s = sub.groupby("fiscal_year")["value"].first() / USD_TO_MILLIONS
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
    facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce").astype("Int64")
    facts = facts.dropna(subset=["fiscal_year"])
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
    """Shares outstanding per year: the first of ``concepts`` (in order) with a value.

    The concepts are alternatives (balance-sheet count, cover-page count,
    weighted average), never added together. A concept reported only per
    share class (a class-of-stock dimension) is the sum of its classes.
    """
    result = pd.Series(np.nan, index=years, dtype=float)
    if facts.empty:
        return result
    facts = facts.copy()
    facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce").astype("Int64")
    facts["numeric"] = pd.to_numeric(facts["value"], errors="coerce")
    facts = facts.dropna(subset=["fiscal_year", "numeric"])
    concept_name = facts["concept"].str.split(":").str[-1]
    dimensioned = facts["is_dimensioned"].astype(bool)
    dimension = facts["dimension"] if "dimension" in facts.columns else pd.Series(None, index=facts.index)
    by_class = dimensioned & dimension.astype(str).str.contains("ClassOfStock", na=False)
    class_key = ["fiscal_year", "member"] if "member" in facts.columns else ["fiscal_year", "numeric"]
    for concept in concepts:
        of_concept = concept_name == concept
        plain = facts[of_concept & ~dimensioned].groupby("fiscal_year")["numeric"].first()
        classes = (facts[of_concept & by_class].drop_duplicates(class_key)
                   .groupby("fiscal_year")["numeric"].sum())
        found = plain.combine_first(classes) / USD_TO_MILLIONS
        found.index = found.index.astype(int)
        result = result.fillna(found.reindex(years))
    return result


def _year_windows(years: list[int], window: int = CHART_WINDOW) -> Iterable[list[int]]:
    """Consecutive runs of ``window`` years; ``window`` 0 is every year at once."""
    years = sorted(years)
    if not window:
        if years:
            yield years
        return
    for i in range(0, len(years), window):
        yield years[i : i + window]


# --------------------------------------------------------------------------- #
# Specs: each chart as plain data. A chart has one or more panels (side by
# side); a panel has its years, series and axes. ``{span}`` in a subtitle is
# replaced by the years on show ("2019–2023"), so a window or a zoomed web
# chart names its own range.
# --------------------------------------------------------------------------- #
SPEC_FILE = "charts.json"
SPEC_VERSION = 1
# Downloaded PNGs are kept here per theme and window size until the next run.
PNG_CACHE_DIR = ".png_cache"


def _values(series: pd.Series) -> list[float | None]:
    return [float(v) if pd.notna(v) and np.isfinite(v) else None for v in series.to_numpy(dtype=float)]


def _series(
    label: str,
    series: pd.Series,
    source: str = "reported",
    *,
    kind: str = "bar",
    fmt: str = "mm",
    axis: int = 0,
    concepts: Iterable[str] = (),
    color: str | None = None,
    mark_derived: bool = True,
    quality: bool = True,
) -> dict:
    """One series of a panel. ``concepts`` are the tags it is read from (for search)."""
    if quality and not series.dropna().empty:
        _record_quality(label, series, source)
    return {
        "label": label,
        "kind": kind,
        "format": fmt,
        "axis": axis,
        "derived": mark_derived and source == "derived",
        "color": color or _base_color(label),
        "concepts": list(concepts),
        "values": _values(series),
    }


def _panel(
    title: str,
    subtitle: str | None,
    years: list[int],
    series: list[dict],
    *,
    axes: list[dict] | None = None,
    margin: float = 0.14,
    note: str | None = None,
    reference: dict | None = None,
    empty: str = "No data for this window",
    group_width: float = 0.8,
) -> dict:
    return {
        "title": title,
        "subtitle": subtitle,
        "years": [int(y) for y in years],
        "series": series,
        "axes": axes or [{"format": "money"}],
        "margin": margin,
        "note": note,
        "reference": reference,
        "empty": empty,
        "group_width": group_width,
    }


def _chart(chart_id: str, name: str, *panels: dict) -> dict:
    return {"id": chart_id, "name": name, "panels": list(panels)}


def _bar_chart(
    chart_id: str,
    name: str,
    ticker: str,
    metrics: list[tuple[str, pd.Series, str, list[str]]],
    years: list[int],
    note: str | None = None,
) -> dict:
    """Grouped bars in USD; metrics without a single value are left out."""
    series = [
        _series(label, values, source, concepts=concepts)
        for label, values, source, concepts in metrics
        if not values.dropna().empty
    ]
    return _chart(chart_id, name, _panel(
        f"{ticker} · {name}", "Fiscal years {span} · USD", years, series, note=note,
    ))


def _line_chart(
    chart_id: str,
    name: str,
    ticker: str,
    subtitle: str,
    metrics: list[tuple[str, pd.Series, list[str]]],
    years: list[int],
) -> dict:
    """Ratios as lines; every metric is derived, so none is starred."""
    for label, values, _concepts in metrics:
        _record_quality(label, values, "derived")
    series = [
        _series(label, values, "derived", kind="line", fmt="pct", concepts=concepts,
                mark_derived=False, quality=False)
        for label, values, concepts in metrics
    ]
    return _chart(chart_id, name, _panel(
        f"{ticker} · {name}", "Fiscal years {span} · " + subtitle, years, series,
        axes=[{"format": "pct"}], margin=0.18,
    ))


# --------------------------------------------------------------------------- #
# Chart 1: Income statement (grouped bars).
# --------------------------------------------------------------------------- #
def chart_income_statement(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> dict:
    impairments = [
        "AssetImpairmentCharges",
        "ImpairmentOfIntangibleAssetsExcludingGoodwill",
        "ImpairmentOfLongLivedAssetsHeldAndUsed",
        "ImpairmentOfLongLivedAssets",
    ]
    metrics = [
        ("Revenue", _df_series(df, "Revenues", years), "reported", ["Revenues"]),
        ("Cost of Revenue", _df_series(df, "CostOfRevenue", years), "reported", ["CostOfRevenue"]),
        ("Gross Profit", _df_series(df, "GrossProfit", years), "derived", ["GrossProfit"]),
        ("SG&A", _df_series(df, "SellingGeneralAndAdministrativeExpense", years), "reported",
         ["SellingGeneralAndAdministrativeExpense"]),
        ("R&D", _df_series(df, "ResearchAndDevelopmentExpense", years), "reported",
         ["ResearchAndDevelopmentExpense"]),
        ("Operating Income", _df_series(df, "OperatingIncomeLoss", years), "reported", ["OperatingIncomeLoss"]),
        ("Interest Expense", _stmt_series(statements, "income", ["InterestExpense"], years), "reported",
         ["InterestExpense"]),
        ("Goodwill Impairment", _stmt_series(statements, "income", ["GoodwillImpairmentLoss"], years), "reported",
         ["GoodwillImpairmentLoss"]),
        ("Asset Writedown", _stmt_series(statements, "income", impairments, years), "derived", impairments),
        ("Net Income", _df_series(df, "NetIncomeLoss", years), "reported", ["NetIncomeLoss"]),
    ]
    return _bar_chart("01_income_statement", "Income statement", ticker, metrics, years)


# --------------------------------------------------------------------------- #
# Chart 2: Margins (lines).
# --------------------------------------------------------------------------- #
def chart_margins(ticker: str, df: pd.DataFrame, years: list[int]) -> dict:
    metrics = [
        ("Gross Margin", _df_series(df, "GrossMargin", years), ["GrossProfit", "Revenues"]),
        ("Operating Margin", _df_series(df, "OperatingMargin", years), ["OperatingIncomeLoss", "Revenues"]),
        ("Net Margin", _df_series(df, "NetMargin", years), ["NetIncomeLoss", "Revenues"]),
    ]
    return _line_chart("02_margins", "Margins", ticker, "gross, operating and net", metrics, years)


# --------------------------------------------------------------------------- #
# Chart 3: Balance sheet (grouped bars; shares on the right axis).
# --------------------------------------------------------------------------- #
def chart_balance(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
    years: list[int],
) -> dict:
    ppe = ["NetPropertyPlantAndEquipment", "PlantPropertyEquipmentNet", "PropertyPlantAndEquipmentNet"]
    intangibles = ["IntangibleAssetsNetExcludingGoodwill", "IntangibleAssetsNet", "IntangibleAssets"]
    inventory = ["InventoryNet", "Inventory", "Inventories"]
    retained = ["RetainedEarningsAccumulatedDeficit", "RetainedEarnings"]
    usd_metrics = [
        ("Total Assets", _stmt_series(statements, "balance", ["Assets"], years), "reported", ["Assets"]),
        ("Net PP&E", _stmt_series(statements, "balance", ppe, years), "reported", ppe),
        ("Goodwill", _stmt_series(statements, "balance", ["Goodwill"], years), "reported", ["Goodwill"]),
        ("Other Intangibles", _stmt_series(statements, "balance", intangibles, years), "reported", intangibles),
        ("Inventory", _stmt_series(statements, "balance", inventory, years), "reported", inventory),
        ("Total Current Liabilities", _df_series(df, "LiabilitiesCurrent", years), "reported",
         ["LiabilitiesCurrent"]),
        ("Total Liabilities", _stmt_series(statements, "balance", ["Liabilities"], years), "reported",
         ["Liabilities"]),
        ("Total Equity", _df_series(df, "StockholdersEquity", years), "reported", ["StockholdersEquity"]),
        ("Retained Earnings", _stmt_series(statements, "balance", retained, years), "reported", retained),
    ]
    share_concepts = [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ]
    shares = _shares_series(facts, share_concepts, years)
    chart = _bar_chart("03_balance", "Balance sheet", ticker, usd_metrics, years)
    panel = chart["panels"][0]
    panel["subtitle"] = "Fiscal years {span} · USD, shares on the right axis"
    panel["margin"] = 0.16
    if not shares.dropna().empty:
        _record_quality("Total Shares Outstanding", shares, "reported")
        panel["series"].append(_series(
            "Shares outstanding", shares, kind="line", fmt="shares", axis=1, concepts=share_concepts,
            color=_base_color("Total Shares Outstanding"), quality=False,
        ))
        # The second axis keeps its numbers only: no grid, so its lines never
        # cross the main axis's.
        panel["axes"].append({"format": "shares", "margin": 0.20})
    return chart


# --------------------------------------------------------------------------- #
# Chart 4: Debt (grouped bars).
# --------------------------------------------------------------------------- #
def chart_debt(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> dict:
    lt_debt = _df_series(df, "LongTermDebt", years)
    # IFRS / foreign filers may not tag a plain LongTermDebt concept for all
    # years, so fill missing years from the balance-sheet statement.
    lt_concepts = ["LongTermDebt", "LongTermDebtNoncurrent", "Borrowings"]
    stmt_lt_debt = _stmt_series(
        statements,
        "balance",
        lt_concepts,
        years,
        include_labels=[
            "long-term portion of borrowings",
            "long-term borrowings",
            "non-current borrowings",
            "long-term debt",
        ],
    )
    lt_debt = lt_debt.combine_first(stmt_lt_debt)
    current_concepts = [
        "LongTermDebtCurrent",
        "FinanceLeaseLiabilityCurrent",
        "CapitalLeaseObligationsCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ]
    current_debt = _stmt_series(
        statements,
        "balance",
        current_concepts,
        years,
        include_labels=[
            "current portion of borrowings",
            "current maturities of debt",
            "current portion of capital lease",
        ],
    )
    lease_concepts = [
        "FinanceLeaseLiabilityNoncurrent",
        "CapitalLeaseObligationsNoncurrent",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    ]
    capital_leases = _stmt_series(
        statements,
        "balance",
        lease_concepts,
        years,
        include_labels=["finance lease liability", "capital lease"],
    )
    cash_concepts = [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndCashEquivalentsAtFairValue",
        "CashAndCashEquivalents",
        "CashAndMarketableSecurities",
    ]
    cash = _stmt_series(statements, "balance", cash_concepts, years)
    total_debt = lt_debt.fillna(0) + current_debt.fillna(0) + capital_leases.fillna(0)
    total_debt = total_debt.replace(0, np.nan)
    net_debt = total_debt - cash

    # A debt-free filer (TAL Education, for one) would otherwise render a cash
    # bar with the debt series silently absent, which reads as missing data.
    note = None
    if all(series.dropna().empty for series in (lt_debt, current_debt, capital_leases)):
        note = "No borrowings or finance leases reported in the balance sheet"

    debt_concepts = lt_concepts + current_concepts + lease_concepts
    metrics = [
        ("Long-Term Debt", lt_debt, "reported", lt_concepts),
        ("Current Portion Debt/Leases", current_debt, "reported", current_concepts),
        ("Capital Leases", capital_leases, "reported", lease_concepts),
        ("Total Debt", total_debt, "derived", debt_concepts),
        ("Cash & Equivalents", cash, "reported", cash_concepts),
        ("Net Debt", net_debt, "derived", debt_concepts + cash_concepts),
    ]
    return _bar_chart("04_debt", "Debt and cash", ticker, metrics, years, note=note)


# --------------------------------------------------------------------------- #
# Chart 5: Cash flow operations (grouped bars).
# --------------------------------------------------------------------------- #
def chart_cashflow_operations(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> dict:
    da_concepts = [
        "DepreciationAndAmortization",
        "Depreciation",
        "AmortizationOfIntangibleAssets",
        "DepreciationExpense",
        "AmortizationOfIntangibles",
    ]
    da = _stmt_series(
        statements,
        "cashflow",
        da_concepts,
        years,
        include_labels=["depreciation and amortization"],
    )
    nwc = (_df_series(df, "AssetsCurrent", years) - _df_series(df, "LiabilitiesCurrent", years)).diff()

    metrics = [
        ("Revenue", _df_series(df, "Revenues", years), "reported", ["Revenues"]),
        ("D&A", da, "reported", da_concepts),
        ("Capital Expenditure", _df_series(df, "CapEx", years), "reported", ["CapEx"]),
        ("Operating Cash Flow", _df_series(df, "OperatingCashFlow", years), "reported", ["OperatingCashFlow"]),
        ("Change in NWC", nwc, "derived", ["AssetsCurrent", "LiabilitiesCurrent"]),
        ("Free Cash Flow", _df_series(df, "FreeCashFlow", years), "derived", ["OperatingCashFlow", "CapEx"]),
    ]
    return _bar_chart("05_cashflow_operations", "Operating cash flow", ticker, metrics, years)


# --------------------------------------------------------------------------- #
# Chart 6: Cash flow financing (grouped bars).
# --------------------------------------------------------------------------- #
def chart_cashflow_financing(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> dict:
    issuance_concepts = [
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromIssuanceOfCommonShares",
        "ProceedsFromStockOptionsExercised",
    ]
    issuance = _stmt_series(
        statements,
        "cashflow",
        issuance_concepts,
        years,
        include_labels=["issuance of common stock", "exercise of stock options"],
        exclude_labels=["debt"],
        normalize_cashflow=True,
    )
    repurchase_concepts = [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsToAcquireTreasuryStock",
        "PaymentsForRepurchaseOfCommonShares",
    ]
    repurchase = _stmt_series(
        statements,
        "cashflow",
        repurchase_concepts,
        years,
        include_labels=["repurchase of common stock", "repurchases of common stock"],
        normalize_cashflow=True,
    )
    dividend_concepts = [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
    ]
    dividends = _stmt_series(
        statements,
        "cashflow",
        dividend_concepts,
        years,
        include_labels=["dividends paid", "payments for dividends"],
        exclude_labels=["preferred"],
        normalize_cashflow=True,
    )
    sbc_concepts = ["ShareBasedCompensation", "NoncashShareBasedCompensation", "StockBasedCompensationExpense"]
    sbc = _stmt_series(
        statements,
        "cashflow",
        sbc_concepts,
        years,
        include_labels=["equity award compensation", "stock-based compensation", "share-based compensation"],
    )

    metrics = [
        ("Stock Issuance", issuance, "reported", issuance_concepts),
        ("Stock Repurchase", repurchase, "reported", repurchase_concepts),
        ("Dividends Paid", dividends, "reported", dividend_concepts),
        ("Stock-Based Compensation", sbc, "reported", sbc_concepts),
    ]
    return _bar_chart("06_cashflow_financing", "Financing cash flow", ticker, metrics, years)


# --------------------------------------------------------------------------- #
# Chart 7: Returns (lines).
# --------------------------------------------------------------------------- #
def chart_returns(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    years: list[int],
) -> dict:
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
        ("ROE", roe, ["NetIncomeLoss", "StockholdersEquity"]),
        ("ROA", roa, ["NetIncomeLoss", "Assets"]),
        ("ROIC", roic, ["OperatingIncomeLoss", "StockholdersEquity", "LongTermDebt"]),
    ]
    return _line_chart("07_returns", "Returns", ticker, "ROE, ROA and ROIC", metrics, years)


# --------------------------------------------------------------------------- #
# Chart 8: Valuation & quality (P/E history + F-score / Z-score).
# --------------------------------------------------------------------------- #
def _valuation_column(frame: pd.DataFrame, col: str) -> pd.Series:
    if col not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return pd.to_numeric(frame[col], errors="coerce")


def chart_valuation(ticker: str, output_dir: str) -> dict | None:
    path = os.path.join(output_dir, "evaluation.csv")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    try:
        data = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    if data.empty or "fiscal_year" not in data.columns:
        return None

    numeric_cols = [
        "price", "shares", "market_cap", "pe", "pb", "pfcf", "ev_ebit",
        "ev_ebitda", "earnings_yield", "fcf_yield", "pe_median_hist",
        "pe_percentile", "f_score", "z_score", "accruals_ratio",
    ]
    for col in numeric_cols:
        data[col] = _valuation_column(data, col)
    data["fiscal_year"] = pd.to_numeric(data["fiscal_year"], errors="coerce")
    data = data.dropna(subset=["fiscal_year"]).copy()
    if data.empty:
        return None
    data["fiscal_year"] = data["fiscal_year"].astype(int)
    data = data.sort_values("fiscal_year").reset_index(drop=True)
    years = data["fiscal_year"].tolist()

    # Left: P/E per fiscal year with the historical median as reference.
    pe_series = []
    if data["pe"].notna().any():
        pe_series.append(_series("P/E", data["pe"], fmt="x", color=COLORS["Revenue"],
                                 concepts=["pe"], quality=False))
    reference = None
    medians = data["pe_median_hist"].dropna()
    if not medians.empty:
        median_pe = float(medians.iloc[-1])
        reference = {
            "value": median_pe,
            "label": f"median {median_pe:.1f}x".replace(".", ","),
            "legend": "Historical median",
            "color": COLORS["Interest Expense"],
        }
    pe_subtitle = "with the historical median"
    percentiles = data["pe_percentile"].dropna()
    if not percentiles.empty:
        pct_value = float(percentiles.iloc[-1])
        if abs(pct_value) <= 1.0:
            pct_value *= 100
        pct_year = int(data.loc[percentiles.index[-1], "fiscal_year"])
        pe_subtitle = f"FY{pct_year} at the {pct_value:.0f}th percentile · " + pe_subtitle
    pe_panel = _panel(
        f"{ticker} · P/E by fiscal year", pe_subtitle, years, pe_series,
        axes=[{"format": "x"}], margin=0.18, reference=reference, empty="No P/E data", group_width=0.6,
    )

    # Right: F-score on the left axis, Z-score on a twin right axis. The bars
    # are offset so both series stay readable at every year.
    scores = [
        _series("F-score (left)", data["f_score"], fmt="int", color=COLORS["Gross Margin"],
                concepts=["f_score"], quality=False),
        _series("Z-score (right)", data["z_score"], fmt="dec1", axis=1, color=COLORS["ROA"],
                concepts=["z_score"], quality=False),
    ]
    quality_panel = _panel(
        "F-score and Z-score", "quality (0–9) and distress risk", years,
        [s for s in scores if any(v is not None for v in s["values"])],
        axes=[
            {"format": "plain", "min": 0, "max": 9.6, "interval": 3, "name": "F-score (0–9)"},
            # The Z-score axis keeps its numbers only: no grid, so its lines
            # never cross the F-score's.
            {"format": "plain", "name": "Z-score", "margin": 0.18},
        ],
        margin=0.18, empty="No quality data", group_width=0.72,
    )
    return _chart("08_valuation", "Valuation", pe_panel, quality_panel)


# --------------------------------------------------------------------------- #
# Matplotlib: draw a spec, one PNG per window of years.
# --------------------------------------------------------------------------- #
def _header(
    ax: plt.Axes,
    title: str,
    subtitle: str | None,
    handles: list | None = None,
    labels: list[str] | None = None,
    title_size: float = 16,
) -> None:
    """Left-aligned title, muted subtitle, then the legend in rows above the plot."""
    legend_h = 0.0
    if handles:
        # Columns that fit the axes width; Ubuntu Mono is ~0.5 em per character.
        width_pt = ax.get_position().width * ax.figure.get_figwidth() * 72
        item_pt = max(len(label) for label in labels) * 0.5 * 10 + 34
        columns = max(1, min(len(labels), int(width_pt // item_pt)))
        rows = -(-len(labels) // columns)
        ax.legend(
            handles, labels, loc="lower left", bbox_to_anchor=(0, 1.0), borderaxespad=0.6,
            ncol=columns, fontsize=10, handlelength=1.0, handleheight=0.8, handletextpad=0.5,
            columnspacing=1.6, labelcolor=_c("label"),
        )
        legend_h = rows * 17 + 10
    y = legend_h + 10
    if subtitle:
        ax.annotate(subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, y),
                    textcoords="offset points", ha="left", va="bottom", fontsize=11, color=_c("muted"))
        y += 18
    ax.annotate(title, xy=(0, 1), xycoords="axes fraction", xytext=(0, y),
                textcoords="offset points", ha="left", va="bottom", fontsize=title_size,
                fontweight="bold", color=_c("strong"))


BAR_RADIUS_PX = 4


def _round_bars(fig: plt.Figure) -> None:
    """Swap every bar for one with slightly rounded corners.

    The radius is set in pixels and corrected for each axes' aspect, so the
    corners stay circular whatever the scales; short bars get a smaller one.
    Runs once the layout and limits are final.
    """
    fig.canvas.draw()
    for ax in fig.axes:
        box = ax.get_window_extent()
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        if not box.width or not box.height or x1 == x0 or y1 == y0:
            continue
        x_per_px = abs(x1 - x0) / box.width
        y_per_px = abs(y1 - y0) / box.height
        for container in ax.containers:
            if not isinstance(container, BarContainer):
                continue
            for rect in container.patches:
                x, y, w, h = rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()
                if not all(np.isfinite([x, y, w, h])) or w == 0 or h == 0:
                    continue
                radius_px = min(BAR_RADIUS_PX, w / x_per_px / 2, abs(h) / y_per_px / 2)
                rounded = FancyBboxPatch(
                    (x, min(y, y + h)), w, abs(h),
                    boxstyle=f"round,pad=0,rounding_size={radius_px * x_per_px}",
                    mutation_aspect=y_per_px / x_per_px,
                    facecolor=rect.get_facecolor(), edgecolor=rect.get_edgecolor(),
                    linewidth=rect.get_linewidth(), zorder=rect.get_zorder(),
                    alpha=rect.get_alpha(), label="_nolegend_",
                )
                rect.set_visible(False)
                ax.add_patch(rounded)


_LABEL_FORMATS = {
    "mm": _fmt_mm,
    "shares": _fmt_shares,
    "pct": _fmt_pct,
    "x": lambda v: f"{v:.1f}x".replace(".", ","),
    "int": lambda v: f"{v:.0f}",
    "dec1": lambda v: f"{v:.1f}".replace(".", ",").replace("-", "−"),
}
_AXIS_FORMATS = {
    "money": _axis_money,
    "shares": _axis_shares,
    "pct": _axis_pct,
    "x": lambda v, _pos=None: f"{v:.0f}x",
}

# pyplot and the theme globals are shared, so one render at a time; the
# quality rows likewise, so one spec build at a time.
_RENDER_LOCK = threading.Lock()
_SPEC_LOCK = threading.Lock()


def _save(fig: plt.Figure, path: str) -> str:
    fig.tight_layout()
    _round_bars(fig)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _style_axes(ax: plt.Axes, years: list[int], yfmt=_axis_money) -> None:
    ax.set_xticks(range(len(years)))
    ax.set_xticklabels([str(y) for y in years], fontsize=10)
    ax.tick_params(axis="y", labelsize=10)
    ax.grid(axis="x", visible=False)
    if yfmt is not None:
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(yfmt))
    ax.yaxis.set_major_locator(mticker.MaxNLocator(5))
    ax.axhline(0, color=_c("ink"), linewidth=1)


def _apply_axis(ax: plt.Axes, spec: dict, margin: float) -> None:
    """Fixed limits, tick step and name from an axis spec; else the margin."""
    if "min" in spec or "max" in spec:
        ax.set_ylim(spec.get("min"), spec.get("max"))
    else:
        ax.margins(y=margin)
    if spec.get("interval"):
        ax.yaxis.set_major_locator(mticker.MultipleLocator(spec["interval"]))
    if spec.get("name"):
        ax.set_ylabel(spec["name"], fontsize=10)


def _bar_style() -> dict:
    """A thin surface-coloured gap between touching bars instead of a border."""
    return {"edgecolor": _c("halo"), "linewidth": 1.2}


def _line_style() -> dict:
    return {"linewidth": 2, "marker": "o", "markersize": 6,
            "markeredgecolor": _c("halo"), "markeredgewidth": 1.5}


def _collect_label(
    candidates: list[tuple[float, float, str, int]],
    x: float,
    y: float,
    value: float,
    fmt: str = "mm",
) -> None:
    """Queue a value label; placement happens once the axes limits are final."""
    if pd.isna(value):
        return
    candidates.append((x, y, _LABEL_FORMATS[fmt](value), 1 if value >= 0 else -1))


def _declutter(
    ax: plt.Axes,
    candidates: list[tuple[float, float, str, int]],
    fontsize: float = 7,
    color: str | None = None,
    draw_ax: plt.Axes | None = None,
    draw_transform=None,
) -> None:
    """Draw every label, pushing each away from the ones already placed.

    Each label is pushed away from its anchor in ``prefer`` direction first,
    then to the opposite side, then further out, until it clears every label
    already placed. Every label is drawn: when no clear slot exists the label
    still goes out at its first natural offset, so no column is left unlabelled.
    """
    if not candidates:
        return
    color = color or _c("label")
    y0, y1 = ax.get_ylim()
    x0, x1 = ax.get_xlim()
    y_range = (y1 - y0) or 1.0
    x_range = (x1 - x0) or 1.0
    # Measured for a 7 pt monospace label on the standard 11-inch figure.
    y_half = 0.017 * y_range * fontsize / 7
    placed: list[tuple[float, float, float]] = []
    # ``draw_ax`` may differ from ``ax`` so labels can be painted on a
    # foreground axes while their positions are measured on a twin axis.
    target = draw_ax or ax
    trans = draw_transform or ax.transData

    for x, y, text, prefer in candidates:
        width = 0.0060 * x_range * max(len(text), 3) * fontsize / 7
        centre = None
        # Step 0 is deliberately skipped: a label centred on its own anchor sits
        # on the marker or the bar edge. Always leave at least one step of room.
        for step in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6):
            dy = prefer * step * 2.1 * y_half
            trial = y + dy
            if not (y0 <= trial <= y1):
                continue
            clear = all(
                abs(x - px) >= (width + pw) / 2 + 0.006 * x_range
                or abs(trial - py) >= 2.2 * y_half
                for px, py, pw in placed
            )
            if clear:
                centre = trial
                break
        if centre is None:
            centre = float(np.clip(y + prefer * 2.1 * y_half, y0, y1))
        placed.append((x, centre, width))
        target.text(
            x, centre, text, ha="center", va="center",
            fontsize=fontsize, color=color, transform=trans,
            # A white halo keeps the number readable when it lands on
            # a bar or a line; it does not hide the data underneath.
            path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
        )


def _window_values(panel: dict, series: dict, years: list[int]) -> np.ndarray:
    lookup = dict(zip(panel["years"], series["values"]))
    return np.array([np.nan if lookup.get(y) is None else lookup[y] for y in years], dtype=float)


def _active_bars(panel: dict, years: list[int]) -> int:
    return sum(
        1 for s in panel["series"]
        if s["kind"] == "bar" and np.isfinite(_window_values(panel, s, years)).any()
    )


def _draw_panel(fig: plt.Figure, ax: plt.Axes, panel: dict, window: list[int]) -> None:
    shown = [y for y in window if y in panel["years"]]
    positions = np.arange(len(shown))
    series = panel["series"]
    values = [_window_values(panel, s, shown) for s in series]
    has = [bool(np.isfinite(v).any()) for v in values]
    axes = panel["axes"]

    twin = None
    if len(axes) > 1 and any(s["axis"] == 1 and has[i] for i, s in enumerate(series)):
        twin = ax.twinx()
    # A line on the twin axis is pushed behind the bars so it never crosses
    # over the columns; its markers and labels are painted on the primary axes
    # (blended transform) so they stay in front.
    behind = twin is not None and all(s["kind"] == "line" for s in series if s["axis"] == 1)
    if behind:
        twin.set_zorder(ax.get_zorder() - 1)
        ax.patch.set_visible(False)
    blended = blended_transform_factory(ax.transData, twin.transData) if behind else None

    bars = [i for i, s in enumerate(series) if s["kind"] == "bar" and has[i]]
    width = panel["group_width"] / max(len(bars), 1)
    candidates: list[tuple[float, float, str, int]] = []
    twin_candidates: list[tuple[float, float, str, int]] = []
    twin_color = None
    for i, s in enumerate(series):
        on_twin = s["axis"] == 1 and twin is not None
        target = twin if on_twin else ax
        queue = twin_candidates if on_twin else candidates
        color = _ink_color(s["color"])
        if s["kind"] == "bar":
            if not has[i]:
                continue
            offset = (bars.index(i) - (len(bars) - 1) / 2) * width
            drawn = target.bar(positions + offset, values[i], width, label=s["label"], color=color, **_bar_style())
            for bar, v in zip(drawn, values[i]):
                _collect_label(queue, bar.get_x() + bar.get_width() / 2, bar.get_height(), v, s["format"])
            continue
        if on_twin and behind:
            if not has[i]:
                continue
            twin.plot(positions, values[i], color=color, linewidth=2, marker="o", markersize=6, label=s["label"])
            ax.plot(positions, values[i], linestyle="none", marker="o", markersize=6,
                    markeredgecolor=_c("halo"), markeredgewidth=1.5, color=color, transform=blended)
            twin_color = color
        else:
            target.plot(positions, values[i], label=s["label"], color=color, **_line_style())
        for x, v in enumerate(values[i]):
            _collect_label(queue, x, v, v, s["format"])

    reference = panel.get("reference")
    if reference:
        ax.axhline(reference["value"], color=_ink_color(reference["color"]), linestyle="--", linewidth=1.5,
                   label=reference["legend"])
        ax.text(
            positions[-1] if len(positions) else 0, reference["value"], reference["label"],
            ha="right", va="bottom", fontsize=9, color=_c("label"),
            path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
        )

    _style_axes(ax, shown, _AXIS_FORMATS.get(axes[0].get("format")))
    _apply_axis(ax, axes[0], panel["margin"])
    if twin is not None:
        twin.grid(False)
        twin.tick_params(axis="y", labelsize=10)
        twin_format = _AXIS_FORMATS.get(axes[1].get("format"))
        if twin_format:
            twin.yaxis.set_major_formatter(mticker.FuncFormatter(twin_format))
        _apply_axis(twin, axes[1], axes[1].get("margin", 0.20))
        if behind:
            _declutter(twin, twin_candidates, color=twin_color, draw_ax=ax, draw_transform=blended)
        else:
            _declutter(twin, twin_candidates)
    _declutter(ax, candidates)

    if not any(has):
        ax.text(0.5, 0.5, panel["empty"], transform=ax.transAxes, ha="center", va="center", color=_c("muted"))
    if panel.get("note"):
        fig.text(0.5, 0.015, panel["note"], ha="center", va="bottom", fontsize=10, color=_c("muted"))

    handles, labels = ax.get_legend_handles_labels()
    if twin is not None:
        more_handles, more_labels = twin.get_legend_handles_labels()
        handles, labels = handles + more_handles, labels + more_labels
    derived = {s["label"] for s in series if s["derived"]}
    labels = [f"{label} *" if label in derived else label for label in labels]
    subtitle = panel["subtitle"].replace("{span}", _span(shown)) if panel["subtitle"] else None
    _header(ax, panel["title"], subtitle, handles, labels)


def _draw_chart(chart: dict, window: list[int], path: str) -> str:
    panels = chart["panels"]
    count = len(panels)
    height = max(6.5, 5.2 + 0.42 * _active_bars(panels[0], window)) if count == 1 else 6.5
    # Wider than five years ("all years in one image") widens the figure, and
    # panels then stack instead of sitting side by side.
    scale = max(1.0, len(window) / CHART_WINDOW)
    if count > 1 and scale > 1:
        fig, axes = plt.subplots(count, 1, figsize=(11 * scale, height * count), squeeze=False)
        fig.subplots_adjust(hspace=0.45)
        axes = axes[:, 0]
    else:
        fig, axes = plt.subplots(1, count, figsize=((11 if count == 1 else 14) * scale, height), squeeze=False)
        if count > 1:
            fig.subplots_adjust(wspace=0.28)
        axes = axes[0]
    for ax, panel in zip(axes, panels):
        _draw_panel(fig, ax, panel, window)
    return _save(fig, path)


def png_name(chart_id: str, window: list[int]) -> str:
    return f"{chart_id}_{window[0]}_{window[-1]}.png"


def png_names(spec: dict, ids: Iterable[str] | None = None, years_per_image: int = CHART_WINDOW) -> list[str]:
    """The file names ``render_pngs`` writes for these charts, in order."""
    wanted = None if ids is None else set(ids)
    names = []
    for chart in spec.get("charts", []):
        if wanted is not None and chart["id"] not in wanted:
            continue
        years = sorted({y for panel in chart["panels"] for y in panel["years"]})
        names.extend(png_name(chart["id"], window) for window in _year_windows(years, years_per_image))
    return names


def render_pngs(
    spec: dict,
    output_dir: str,
    ids: Iterable[str] | None = None,
    years_per_image: int = CHART_WINDOW,
    dark: bool = False,
    skip_existing: bool = False,
) -> list[str]:
    """Draw the specs as PNGs, one per chart and window of ``years_per_image``.

    ``years_per_image`` 0 puts every year in one (wider) image. Returns the
    paths in chart order; ``skip_existing`` keeps images already drawn.
    """
    global _INK
    os.makedirs(output_dir, exist_ok=True)
    wanted = None if ids is None else set(ids)
    paths: list[str] = []
    with _RENDER_LOCK:
        _INK = DARK_INK if dark else LIGHT_INK
        try:
            with plt.rc_context(DARK_RC if dark else LIGHT_RC):
                for chart in spec.get("charts", []):
                    if wanted is not None and chart["id"] not in wanted:
                        continue
                    years = sorted({y for panel in chart["panels"] for y in panel["years"]})
                    for window in _year_windows(years, years_per_image):
                        path = os.path.join(output_dir, png_name(chart["id"], window))
                        if not (skip_existing and os.path.isfile(path)):
                            try:
                                _draw_chart(chart, window, path)
                            finally:
                                plt.close("all")
                        paths.append(path)
        finally:
            _INK = LIGHT_INK
    return paths


# --------------------------------------------------------------------------- #
# Public entry points.
# --------------------------------------------------------------------------- #
def build_spec(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
) -> dict:
    """Every chart's spec; fills the data-quality rows as a side effect."""
    _QUALITY_ROWS.clear()
    years = sorted(df["fiscal_year"].dropna().astype(int).unique().tolist())
    charts: list[dict] = []
    if years:
        charts = [
            chart_income_statement(ticker, df, statements, years),
            chart_margins(ticker, df, years),
            chart_balance(ticker, df, statements, facts, years),
            chart_debt(ticker, df, statements, years),
            chart_cashflow_operations(ticker, df, statements, years),
            chart_cashflow_financing(ticker, df, statements, years),
            chart_returns(ticker, df, statements, years),
        ]
        valuation = chart_valuation(ticker, output_dir)
        if valuation:
            charts.append(valuation)
    return {"version": SPEC_VERSION, "ticker": ticker, "years": years, "charts": charts}


def clear_images(output_dir: str) -> None:
    """Drop PNGs from earlier runs: the charts they show are out of date."""
    for path in glob.glob(os.path.join(output_dir, "*.png")):
        os.remove(path)
    for folder in ("dark", PNG_CACHE_DIR):
        shutil.rmtree(os.path.join(output_dir, folder), ignore_errors=True)


def render_all(
    ticker: str,
    output_dir: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
) -> list[str]:
    """Write ``charts.json`` and ``data_quality.csv``; returns the chart ids."""
    with _SPEC_LOCK:
        spec = build_spec(ticker, output_dir, df, statements, facts)
        if not spec["charts"]:
            return []
        clear_images(output_dir)
        # Written aside and renamed, so a reader never sees half a file.
        path = os.path.join(output_dir, SPEC_FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(path + ".tmp", path)
        _write_quality_csv(output_dir)
        _QUALITY_ROWS.clear()
    return [chart["id"] for chart in spec["charts"]]


def _read_csv(path: str) -> pd.DataFrame:
    try:
        return pd.read_csv(path, low_memory=False)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def render_from_dir(ticker: str, output_dir: str) -> list[str]:
    """``render_all`` from a run's CSVs: upgrades a run made before charts.json."""
    df = _read_csv(os.path.join(output_dir, "financial_data.csv"))
    if df.empty or "fiscal_year" not in df.columns:
        return []
    statements = _read_csv(os.path.join(output_dir, "statements.csv"))
    facts = _read_csv(os.path.join(output_dir, "facts.csv"))
    return render_all(ticker, output_dir, df, statements, facts)
