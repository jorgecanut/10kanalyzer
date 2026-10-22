"""Chart rendering for the SEC EDGAR 10-K dashboard.

All charts read from the CSVs written by ``edgar_dashboard.py``:
``financial_data.csv`` for ratio/metric inputs, ``statements.csv`` for
statement lines, and ``facts.csv`` for items such as shares outstanding.
"""

from __future__ import annotations

import hashlib
import os
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
# Themes: every chart is drawn twice, light into the ticker folder and dark into
# its ``dark/`` sub-folder, so the web app can match the page theme. The
# colours are the app's own surface, text and line tokens. Sleek by default:
# no frame, no ticks, no vertical grid; only faint horizontal lines.
# --------------------------------------------------------------------------- #
DARK_SUBDIR = "dark"
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
_DARK = False


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
    return "0" if value == 0 else _compact(value, decimals=1, strip=True)


def _axis_pct(value: float, _pos=None) -> str:
    return f"{value * 100:.0f}%".replace("-", "\u2212")


def _span(years: list[int]) -> str:
    return f"{years[0]}\u2013{years[-1]}" if years else ""


def _color_for(label: str) -> str:
    if label in COLORS:
        # Near-black series would vanish on the dark background.
        return _c("strong") if COLORS[label] == "#111827" else COLORS[label]
    idx = int(hashlib.md5(label.encode()).hexdigest(), 16) % len(FALLBACK_PALETTE)
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
    """Total shares outstanding: dedupe by concept, then sum across classes."""
    if facts.empty:
        return pd.Series(np.nan, index=years)
    facts = facts.copy()
    facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce").astype("Int64")
    facts = facts.dropna(subset=["fiscal_year"])
    concept_name = facts["concept"].str.split(":").str[-1]
    mask = concept_name.isin(concepts) & (~facts["is_dimensioned"].astype(bool))
    sub = facts[mask].copy()
    if sub.empty:
        return pd.Series(np.nan, index=years)
    sub["numeric"] = pd.to_numeric(sub["value"], errors="coerce")
    sub = sub.dropna(subset=["numeric"])
    is_dei = sub["concept"].str.startswith("dei:")
    years_with_gaap = sub.loc[~is_dei, "fiscal_year"].unique()
    sub = sub[~is_dei | ~sub["fiscal_year"].isin(years_with_gaap)]
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
    """A figure whose title, subtitle and legend are drawn by ``_legend``."""
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax._stocky_header = (title, subtitle)
    return fig, ax


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


def _save(fig: plt.Figure, path: str) -> str:
    if _DARK:
        folder = os.path.join(os.path.dirname(path), DARK_SUBDIR)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, os.path.basename(path))
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
    if fmt == "mm":
        text = _fmt_mm(value)
    elif fmt == "shares":
        text = _fmt_shares(value)
    else:
        text = _fmt_pct(value)
    candidates.append((x, y, text, 1 if value >= 0 else -1))


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


def _legend(ax: plt.Axes, derived_labels: set[str], extra: list | None = None) -> None:
    """Title, subtitle and legend (``extra`` adds handles from a twin axis)."""
    handles, labels = ax.get_legend_handles_labels()
    for other in extra or []:
        more_handles, more_labels = other.get_legend_handles_labels()
        handles, labels = handles + more_handles, labels + more_labels
    if derived_labels:
        labels = [f"{lab} *" if lab in derived_labels else lab for lab in labels]
    title, subtitle = getattr(ax, "_stocky_header", ("", None))
    _header(ax, title, subtitle, handles, labels)


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
    return _grouped_bar_windows(ticker, output_dir, "01_income_statement", "Income statement", metrics, years)


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
    fig, ax = _new_figure(f"{ticker} \u00b7 Margins", f"Fiscal years {_span(years)} \u00b7 gross, operating and net")
    candidates: list[tuple[float, float, str, int]] = []
    for label, series, _source in metrics:
        ax.plot(range(len(years)), series.to_numpy(), label=label, color=_color_for(label), **_line_style())
        for x, v in enumerate(series.to_numpy()):
            _collect_label(candidates, x, v, v, "pct")
    _style_axes(ax, years, _axis_pct)
    ax.margins(y=0.18)
    _declutter(ax, candidates)
    _legend(ax, set())
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
            f"{ticker} \u00b7 Balance sheet", f"Fiscal years {_span(window)} \u00b7 USD, shares on the right axis"
        )
        positions = np.arange(len(window))
        fig.set_size_inches(11, max(6.5, 5.2 + 0.42 * len(usd_metrics)))
        width = 0.8 / len(usd_metrics)
        derived_labels: set[str] = set()
        candidates: list[tuple[float, float, str, int]] = []

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
                **_bar_style(),
            )
            for bar, v in zip(bars, series.to_numpy()):
                _collect_label(
                    candidates, bar.get_x() + bar.get_width() / 2, bar.get_height(), v
                )

        # Shares on right axis. The twin axis is pushed behind the bars so its
        # line never crosses over the columns. Markers and labels are painted
        # on the primary axes (blended transform) so they stay in front.
        if not shares.reindex(window).dropna().empty:
            ax2 = ax.twinx()
            ax2.set_zorder(ax.get_zorder() - 1)
            ax.patch.set_visible(False)
            share_color = _color_for("Total Shares Outstanding")
            share_values = shares.reindex(window).to_numpy()
            share_trans = blended_transform_factory(ax.transData, ax2.transData)
            ax2.plot(
                positions,
                share_values,
                color=share_color,
                linewidth=2,
                marker="o",
                markersize=6,
                label="Shares outstanding",
            )
            ax.plot(
                positions,
                share_values,
                linestyle="none",
                marker="o",
                markersize=6,
                markeredgecolor=_c("halo"),
                markeredgewidth=1.5,
                color=share_color,
                transform=share_trans,
            )
            share_candidates: list[tuple[float, float, str, int]] = []
            for x, v in enumerate(share_values):
                _collect_label(share_candidates, x, v, v, "shares")
            # The second axis keeps its numbers only: no grid, so its lines
            # never cross the main axis's.
            ax2.grid(False)
            ax2.tick_params(axis="y", labelsize=10)
            ax2.yaxis.set_major_formatter(mticker.FuncFormatter(_axis_shares))
            ax2.margins(y=0.20)
            _declutter(
                ax2,
                share_candidates,
                color=share_color,
                draw_ax=ax,
                draw_transform=share_trans,
            )

        _style_axes(ax, window)
        ax.margins(y=0.16)
        _declutter(ax, candidates)
        _legend(ax, derived_labels, extra=[a for a in fig.axes if a is not ax])
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

    # A debt-free filer (TAL Education, for one) would otherwise render a cash
    # bar with the debt series silently absent, which reads as missing data.
    note = None
    if all(series.dropna().empty for series in (lt_debt, current_debt, capital_leases)):
        note = "No borrowings or finance leases reported in the balance sheet"

    metrics = [
        ("Long-Term Debt", lt_debt, "reported"),
        ("Current Portion Debt/Leases", current_debt, "reported"),
        ("Capital Leases", capital_leases, "reported"),
        ("Total Debt", total_debt, "derived"),
        ("Cash & Equivalents", cash, "reported"),
        ("Net Debt", net_debt, "derived"),
    ]
    return _grouped_bar_windows(ticker, output_dir, "04_debt", "Debt and cash", metrics, years, note=note)


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
        ticker, output_dir, "05_cashflow_operations", "Operating cash flow", metrics, years
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
        ticker, output_dir, "06_cashflow_financing", "Financing cash flow", metrics, years
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
    fig, ax = _new_figure(f"{ticker} \u00b7 Returns", f"Fiscal years {_span(years)} \u00b7 ROE, ROA and ROIC")
    candidates: list[tuple[float, float, str, int]] = []
    for label, series, _source in metrics:
        ax.plot(range(len(years)), series.to_numpy(), label=label, color=_color_for(label), **_line_style())
        for x, v in enumerate(series.to_numpy()):
            _collect_label(candidates, x, v, v, "pct")
    _style_axes(ax, years, _axis_pct)
    ax.margins(y=0.18)
    _declutter(ax, candidates)
    _legend(ax, set())
    path = _save(fig, os.path.join(output_dir, "07_returns_full.png"))
    return [path]


# --------------------------------------------------------------------------- #
# Chart 8: Valuation & quality (P/E history + F-score / Z-score).
# --------------------------------------------------------------------------- #
def _valuation_column(frame: pd.DataFrame, col: str) -> pd.Series:
    if col not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return pd.to_numeric(frame[col], errors="coerce")


def chart_valuation(ticker: str, output_dir: str, years: list[int]) -> list[str]:
    path = os.path.join(output_dir, "evaluation.csv")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return []
    try:
        data = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return []
    if data.empty or "fiscal_year" not in data.columns:
        return []

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
        return []
    data["fiscal_year"] = data["fiscal_year"].astype(int)
    data = data.sort_values("fiscal_year").reset_index(drop=True)

    pe = data["pe"]
    f_score = data["f_score"]
    z_score = data["z_score"]
    positions = np.arange(len(data))
    labels = [str(year) for year in data["fiscal_year"]]

    fig, (ax_pe, ax_q) = plt.subplots(1, 2, figsize=(14, 6.5))
    fig.subplots_adjust(wspace=0.28)

    # Left: P/E per fiscal year with the historical median as reference.
    pe_color = COLORS["Revenue"]
    pe_mask = pe.notna()
    if pe_mask.any():
        ax_pe.bar(
            positions[pe_mask.to_numpy()],
            pe[pe_mask].to_numpy(),
            width=0.6,
            color=pe_color,
            label="P/E",
            **_bar_style(),
        )
        for x, value in zip(positions[pe_mask.to_numpy()], pe[pe_mask].to_numpy()):
            ax_pe.text(
                x, value, f" {value:.1f}x".replace(".", ","), ha="center", va="bottom", fontsize=8,
                color=_c("label"),
                path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
            )
    else:
        ax_pe.text(
            0.5, 0.5, "No P/E data", transform=ax_pe.transAxes,
            ha="center", va="center", color=_c("muted"),
        )

    medians = data["pe_median_hist"].dropna()
    if not medians.empty:
        median_pe = float(medians.iloc[-1])
        ax_pe.axhline(
            median_pe, color=COLORS["Interest Expense"], linestyle="--", linewidth=1.5,
            label="Historical median",
        )
        ax_pe.text(
            positions[-1] if len(positions) else 0,
            median_pe,
            f"median {median_pe:.1f}x".replace(".", ","),
            ha="right",
            va="bottom",
            fontsize=9,
            color=_c("label"),
            path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
        )

    pe_subtitle = "with the historical median"
    percentiles = data["pe_percentile"].dropna()
    if not percentiles.empty:
        pct_value = float(percentiles.iloc[-1])
        if abs(pct_value) <= 1.0:
            pct_value *= 100
        pct_year = int(data.loc[percentiles.index[-1], "fiscal_year"])
        pe_subtitle = f"FY{pct_year} at the {pct_value:.0f}th percentile \u00b7 " + pe_subtitle

    _style_axes(ax_pe, labels, lambda v, _pos=None: f"{v:.0f}x")
    ax_pe.margins(y=0.18)
    # Its own legend row too, so both panel titles line up.
    _header(ax_pe, f"{ticker} \u00b7 P/E by fiscal year", pe_subtitle, *ax_pe.get_legend_handles_labels())

    # Right: F-score on the left axis, Z-score on a twin right axis. The bars
    # are offset so both series stay readable at every year.
    ax_f = ax_q
    ax_z = ax_f.twinx()
    f_color = COLORS["Gross Margin"]
    z_color = COLORS["ROA"]
    width = 0.36
    f_bars = ax_f.bar(
        positions - width / 2,
        f_score.to_numpy(),
        width,
        color=f_color,
        label="F-score (left)",
        **_bar_style(),
    )
    z_bars = ax_z.bar(
        positions + width / 2,
        z_score.to_numpy(),
        width,
        color=z_color,
        label="Z-score (right)",
        **_bar_style(),
    )
    for bar, value in zip(f_bars, f_score.to_numpy()):
        if pd.notna(value):
            ax_f.text(
                bar.get_x() + bar.get_width() / 2, value, f"{value:.0f}",
                ha="center", va="bottom", fontsize=8, color=_c("label"),
                path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
            )
    for bar, value in zip(z_bars, z_score.to_numpy()):
        if pd.notna(value):
            ax_z.text(
                bar.get_x() + bar.get_width() / 2, value, f"{value:.1f}".replace(".", ","),
                ha="center", va="bottom", fontsize=8, color=_c("label"),
                path_effects=[path_effects.withStroke(linewidth=2.5, foreground=_c("halo"))],
            )

    if f_score.dropna().empty and z_score.dropna().empty:
        ax_f.text(
            0.5, 0.5, "No quality data", transform=ax_f.transAxes,
            ha="center", va="center", color=_c("muted"),
        )

    _style_axes(ax_f, labels, None)
    ax_f.set_ylabel("F-score (0\u20139)", fontsize=10)
    ax_f.set_ylim(0, 9.6)
    ax_f.yaxis.set_major_locator(mticker.MultipleLocator(3))
    # The Z-score axis keeps its numbers only: no grid, so its lines never
    # cross the F-score's.
    ax_z.grid(False)
    ax_z.set_ylabel("Z-score", fontsize=10)
    ax_z.tick_params(axis="y", labelsize=10)
    ax_z.margins(y=0.18)
    _header(ax_f, "F-score and Z-score", "quality (0\u20139) and distress risk",
            [f_bars, z_bars], ["F-score (left)", "Z-score (right)"])

    return [_save(fig, os.path.join(output_dir, "08_valuation.png"))]


# --------------------------------------------------------------------------- #
# Generic grouped-bar renderer with 5-year windows.
# --------------------------------------------------------------------------- #
def _grouped_bar_windows(
    ticker: str,
    output_dir: str,
    filename_prefix: str,
    title: str,
    metrics: list[tuple[str, pd.Series, str]],
    years: list[int],
    note: str | None = None,
) -> list[str]:
    paths: list[str] = []
    for window in _year_windows(years):
        suffix = _window_suffix(window)
        fig, ax = _new_figure(f"{ticker} \u00b7 {title}", f"Fiscal years {_span(window)} \u00b7 USD")
        positions = np.arange(len(window))
        # Only use metrics that have at least one value in this window.
        active = [
            (label, series_full.reindex(window), source)
            for label, series_full, source in metrics
            if not series_full.reindex(window).dropna().empty
        ]
        fig.set_size_inches(11, max(6.5, 5.2 + 0.42 * len(active)))
        width = 0.8 / max(len(active), 1)
        derived_labels: set[str] = set()
        candidates: list[tuple[float, float, str, int]] = []

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
                **_bar_style(),
            )
            for bar, v in zip(bars, series.to_numpy()):
                _collect_label(
                    candidates, bar.get_x() + bar.get_width() / 2, bar.get_height(), v
                )

        _style_axes(ax, window)
        ax.margins(y=0.14)
        _declutter(ax, candidates)
        _legend(ax, derived_labels)
        if not active:
            ax.text(
                0.5,
                0.5,
                "No data for this window",
                transform=ax.transAxes,
                ha="center",
                va="center",
                color=_c("muted"),
            )
        if note:
            fig.text(0.5, 0.015, note, ha="center", va="bottom", fontsize=10, color=_c("muted"))
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

    global _INK, _DARK
    paths: list[str] = []
    chart_calls = [
        lambda: chart_income_statement(ticker, output_dir, df, statements, years),
        lambda: chart_margins(ticker, output_dir, df, years),
        lambda: chart_balance(ticker, output_dir, df, statements, facts, years),
        lambda: chart_debt(ticker, output_dir, df, statements, years),
        lambda: chart_cashflow_operations(ticker, output_dir, df, statements, years),
        lambda: chart_cashflow_financing(ticker, output_dir, df, statements, years),
        lambda: chart_returns(ticker, output_dir, df, statements, years),
        lambda: chart_valuation(ticker, output_dir, years),
    ]
    for call in chart_calls:
        try:
            paths.extend(call())
        finally:
            plt.close("all")
    _write_quality_csv(output_dir)

    # Dark copies: same charts, same names, under dark/. A failure here never
    # costs the light charts already written.
    _INK, _DARK = DARK_INK, True
    try:
        with plt.rc_context(DARK_RC):
            for call in chart_calls:
                try:
                    call()
                except Exception as exc:  # noqa: BLE001 - the light set is what counts
                    print(f"    [!] dark chart skipped: {exc}")
                finally:
                    plt.close("all")
    finally:
        _INK, _DARK = LIGHT_INK, False
        _QUALITY_ROWS.clear()
    return paths
