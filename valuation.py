#!/usr/bin/env python3
"""Per-fiscal-year valuation ratios, quality scores and history percentiles.

Consumes the artifacts the extraction pipeline already writes
(``financial_data.csv``, ``statements.csv``, ``facts.csv``) plus a monthly,
split-adjusted price history from Yahoo Finance, and writes
``evaluation.csv`` next to them.

Money is in millions of the filer's reporting currency. Share counts are
normalised for stock splits so that they line up with Yahoo's split-adjusted
closes; without that, a 10:1 split would multiply the apparent market cap of
every earlier year.

Foreign filers trade in New York as American Depositary Shares priced in USD
while they report in EUR, TWD, JPY... ``market_scale`` turns each year's
quoted price into a price per ordinary share in the reporting currency (FX
rate at the fiscal year end, divided by the ordinary shares per ADS), so
market cap, P/E and the rest compare like with like.

Usage:
    python valuation.py NFLX
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from http.cookiejar import CookieJar
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

USD_TO_MILLIONS = 1_000_000
PRICE_MATCH_DAYS = 10
MIN_F_SCORE_TESTS = 6
PRICE_RANGE_YEARS = 10
PRICE_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Stocky/1.0"
# Yahoo's quote API (cookie + crumb) wants a full browser User-Agent; the same as app.py's.
YAHOO_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) Stocky/1.0"
BASE_OUTPUT_DIR = "financial_graphs"
OUTPUT_COLUMNS = [
    "fiscal_year", "price", "shares", "market_cap", "pe", "pb", "pfcf",
    "ev_ebit", "ev_ebitda", "earnings_yield", "return_on_capital", "fcf_yield",
    "pe_median_hist", "pe_percentile", "f_score", "z_score", "accruals_ratio",
    # Money columns are in ``currency``; ``price`` is as quoted, in
    # ``price_currency``, per traded share (an ADS for foreign filers).
    "currency", "price_currency",
]
# Ordinary shares per ADS seen in practice; an inferred ratio snaps to one.
NICE_RATIOS = (0.01, 0.02, 0.025, 0.04, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10,
               12, 15, 20, 25, 30, 40, 50, 100)
# The usual ratios only, for the rough Yahoo estimate (Toyota's 11.5 is 10, not 12).
COMMON_RATIOS = (0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 5, 10, 20, 25, 50, 100)

# Candidate standard concepts / labels for the balance-sheet inputs that the
# ratio dataframe does not carry (total assets and liabilities, retained
# earnings, cash). The extractor's standard_concept vocabulary differs per
# filer, so each metric lists every spelling seen in practice.
TOTAL_ASSETS = (["Assets", "AssetsTotal", "TotalAssets"], ["total assets"])
TOTAL_LIABILITIES = (
    ["Liabilities", "LiabilitiesTotal", "TotalLiabilities"],
    ["total liabilities"],
)
# For deriving total liabilities when a filer reports no total line.
NONCURRENT_LIABILITIES = (
    ["NonCurrentLiabilitiesTotal", "LiabilitiesNoncurrent", "NoncurrentLiabilities"],
    ["total noncurrent liabilities", "total non-current liabilities"],
)
LIABILITIES_AND_EQUITY = (
    ["LiabilitiesAndEquity", "LiabilitiesAndStockholdersEquity", "EquityAndLiabilities"],
    ["total liabilities and equity", "total liabilities and stockholders' equity",
     "total liabilities and shareholders' equity"],
)
RETAINED_EARNINGS = (
    ["RetainedEarnings", "RetainedEarningsAccumulatedDeficit", "AccumulatedDeficit"],
    ["retained earnings", "accumulated deficit"],
)
CASH = (
    [
        "CashAndCashEquivalents", "CashAndCashEquivalentsAtCarryingValue",
        "CashAndMarketableSecurities",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashCashEquivalentsAndShortTermInvestments",
    ],
    ["cash and cash equivalents", "cash and cash equivalents and restricted cash"],
)
EQUITY = (
    [
        "AllEquityBalance", "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity", "CommonEquity", "EquityAttributableToOwnersOfParent",
    ],
    ["total stockholders' equity", "total shareholders' equity", "total equity"],
)
LONG_TERM_DEBT = (
    ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
     # IFRS spellings of borrowings due after a year.
     "NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings", "NoncurrentBorrowings", "BondsIssued"],
    ["long-term debt", "long-term borrowings", "non-current borrowings", "bonds payable"],
)
CURRENT_ASSETS = (
    ["CurrentAssetsTotal", "AssetsCurrent", "CurrentAssets"],
    ["total current assets"],
)
CURRENT_LIABILITIES = (
    ["CurrentLiabilitiesTotal", "LiabilitiesCurrent", "CurrentLiabilities"],
    ["total current liabilities"],
)
DEPRECIATION = (
    ["DepreciationExpense", "DepreciationDepletionAndAmortization",
     "DepreciationAmortizationAndAccretionNet",
     "DepreciationAndAmortisationExpense", "AdjustmentsForDepreciationAndAmortisationExpense"],
    ["depreciation and amortization"],
)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #

def _chart(ticker: str, query: str) -> dict:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(ticker)}?{query}"
    )
    request = Request(url, headers={
        "User-Agent": PRICE_USER_AGENT,
        "Accept": "application/json",
    })
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_price_history(ticker: str, years: int = 10) -> list[dict]:
    """Monthly split-adjusted closes from Yahoo's v8 chart API.

    Returns ``[{"date": "YYYY-MM-DD", "close": float}]`` ascending by date, or
    an empty list when the request fails for any reason.
    """
    try:
        span = max(1, int(years))
        payload = _chart(ticker, f"interval=1mo&range={span}y")
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError("empty chart result")
        series = result[0]
        stamps = series.get("timestamp") or []
        quotes = ((series.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        bars: dict[str, float] = {}
        for stamp, close in zip(stamps, closes):
            if close is None:
                continue
            value = float(close)
            if not np.isfinite(value):
                continue
            day = dt.datetime.fromtimestamp(int(stamp), dt.UTC).date()
            bars[day.isoformat()] = value
        return [{"date": day, "close": bars[day]} for day in sorted(bars)]
    except Exception:  # noqa: BLE001 - market data is best effort
        print(f"[!] Price history unavailable for {ticker}")
        return []


def fetch_quote(ticker: str) -> dict | None:
    """Latest price and currency from the chart meta block, or ``None``."""
    try:
        payload = _chart(ticker, "interval=1d&range=1d")
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            quotes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            closes = [c for c in (quotes.get("close") or []) if c is not None]
            price = closes[-1] if closes else None
        if price is None:
            return None
        return {
            "price": float(price),
            "currency": str(meta.get("currency") or "USD"),
        }
    except Exception:  # noqa: BLE001 - best effort
        return None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame is None or column not in frame.columns:
        return pd.Series(np.nan, index=frame.index if frame is not None else None)
    return pd.to_numeric(frame[column], errors="coerce")


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _finite(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return np.nan
    return result if np.isfinite(result) else np.nan


def _safe_div(numerator, denominator, positive_only: bool = False) -> float:
    if not np.isfinite(numerator) or not np.isfinite(denominator):
        return np.nan
    if denominator == 0 or (positive_only and denominator <= 0):
        return np.nan
    return numerator / denominator


def _statement_metric(
    statements: pd.DataFrame,
    years: list[int],
    concepts: list[str],
    labels: list[str],
    fuzzy: bool = True,
) -> pd.Series:
    """Look a balance-sheet line up per fiscal year, tolerant to tag drift.

    Exact standard_concept matches win, then exact label matches, then (when
    fuzzy) a case-insensitive substring match. Values are converted to USD
    millions. Totals pass fuzzy=False: a substring such as "liabilities" also
    matches every single liability line.
    """
    out = pd.Series(np.nan, index=years, dtype="float64")
    if statements is None or statements.empty or "value" not in statements.columns:
        return out
    frame = statements.copy()
    if "fiscal_year" not in frame.columns:
        return out
    frame["_fy"] = pd.to_numeric(frame["fiscal_year"], errors="coerce")
    frame = frame[frame["_fy"].isin(years)]
    frame["_value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame[frame["_value"].notna()]
    if frame.empty:
        return out
    if "standard_concept" in frame.columns:
        frame["_std"] = frame["standard_concept"].map(_norm)
    else:
        frame["_std"] = ""
    # The tag itself (``ifrs-full_Borrowings`` -> ``borrowings``): IFRS lines
    # often carry no standard_concept.
    if "concept" in frame.columns:
        frame["_tag"] = frame["concept"].astype(str).str.split(r"[:_]", n=1, regex=True).str[-1].map(_norm)
    else:
        frame["_tag"] = ""
    if "label" in frame.columns:
        frame["_label"] = frame["label"].map(_norm)
    else:
        frame["_label"] = ""

    concept_keys = [_norm(name) for name in concepts]
    label_keys = [_norm(name) for name in labels]

    for year in years:
        rows = frame[frame["_fy"] == year]
        if rows.empty:
            continue
        value = np.nan
        # 1. Exact standard_concept (or tag) match, in candidate order.
        for key in concept_keys:
            hit = rows[(rows["_std"] == key) | (rows["_tag"] == key)]
            if not hit.empty:
                value = hit.iloc[0]["_value"]
                break
        # 2. Exact label match (e.g. Netflix tags cash as CashAndMarketableSecurities).
        if pd.isna(value):
            for key in label_keys:
                hit = rows[rows["_label"] == key]
                if not hit.empty:
                    value = hit.iloc[0]["_value"]
                    break
        # 3. Substring fallback on either field.
        if fuzzy and pd.isna(value):
            for key in concept_keys + label_keys:
                if len(key) < 6:
                    continue
                hit = rows[
                    rows["_std"].str.contains(key, regex=False, na=False)
                    | rows["_label"].str.contains(key, regex=False, na=False)
                ]
                if not hit.empty:
                    value = hit.iloc[0]["_value"]
                    break
        out[year] = _finite(value) / USD_TO_MILLIONS
    return out


def _total_liabilities(statements: pd.DataFrame, years: list[int]) -> pd.Series:
    """Total liabilities per fiscal year (millions), derived when not reported.

    Many filers print no "Total liabilities" line. Then: current plus
    noncurrent totals, else total liabilities and equity (or total assets)
    minus total equity. Equity may be negative (a deficit), which is right.
    """
    reported = _statement_metric(statements, years, *TOTAL_LIABILITIES, fuzzy=False)
    current = _statement_metric(statements, years, *CURRENT_LIABILITIES, fuzzy=False)
    noncurrent = _statement_metric(statements, years, *NONCURRENT_LIABILITIES, fuzzy=False)
    both_sides = _statement_metric(statements, years, *LIABILITIES_AND_EQUITY, fuzzy=False)
    assets = _statement_metric(statements, years, *TOTAL_ASSETS, fuzzy=False)
    equity = _statement_metric(statements, years, *EQUITY, fuzzy=False)
    total = reported.where(reported.notna(), current + noncurrent)
    total = total.where(total.notna(), both_sides - equity)
    return total.where(total.notna(), assets - equity)


def _fact_metric(facts: pd.DataFrame, years: list[int], concepts: list[str],
                 currency: str | None = None) -> pd.Series:
    """Fallback lookup of a single fact concept per fiscal year (millions).

    Facts in another currency (a foreign filer's USD convenience copy) are skipped.
    """
    out = pd.Series(np.nan, index=years, dtype="float64")
    if facts is None or facts.empty or "concept" not in facts.columns:
        return out
    frame = facts.copy()
    if "fiscal_year" not in frame.columns or "value" not in frame.columns:
        return out
    frame["_fy"] = pd.to_numeric(frame["fiscal_year"], errors="coerce")
    frame = frame[frame["_fy"].isin(years)]
    frame["_value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame[frame["_value"].notna()]
    if currency and "currency" in frame.columns:
        frame = frame[frame["currency"].isna() | (frame["currency"].astype(str).str.upper() == currency)]
    if frame.empty:
        return out
    names = frame["concept"].astype(str).str.split(":").str[-1]
    if "is_dimensioned" in frame.columns:
        frame = frame[~frame["is_dimensioned"].astype(bool)]
        names = names.loc[frame.index]
    for year in years:
        rows = frame[frame["_fy"] == year]
        year_names = names.loc[rows.index]
        for concept in concepts:
            hit = rows[year_names == concept]
            if not hit.empty:
                out[year] = _finite(hit.iloc[0]["_value"]) / USD_TO_MILLIONS
                break
    return out


def _period_dates(data: pd.DataFrame, statements: pd.DataFrame) -> dict[int, pd.Timestamp]:
    """Fiscal year -> period end date, preferring the statements' report dates."""
    mapping: dict[int, pd.Timestamp] = {}
    if statements is not None and not statements.empty:
        if {"fiscal_year", "period_of_report"} <= set(statements.columns):
            frame = statements[["fiscal_year", "period_of_report"]].copy()
            frame["_fy"] = pd.to_numeric(frame["fiscal_year"], errors="coerce")
            frame["_date"] = pd.to_datetime(frame["period_of_report"], errors="coerce")
            frame = frame.dropna(subset=["_fy", "_date"])
            for year, group in frame.groupby("_fy"):
                mapping[int(year)] = group["_date"].max()
    if "period_of_report" in data.columns:
        dates = pd.to_datetime(data["period_of_report"], errors="coerce")
        for year, date in zip(data["fiscal_year"], dates):
            if pd.notna(date) and int(year) not in mapping:
                mapping[int(year)] = date
    for year in data["fiscal_year"]:
        mapping.setdefault(int(year), pd.Timestamp(f"{int(year)}-12-31"))
    return mapping


def _close_near(price_history: list[dict], target: pd.Timestamp) -> float:
    """Close closest to ``target`` within 10 days.

    Monthly bars carry a first-of-month timestamp but represent the whole
    month, so both the raw timestamp and the month end are considered.
    """
    if not price_history or target is None or pd.isna(target):
        return np.nan
    best = np.nan
    best_gap = None
    for bar in price_history:
        day = pd.to_datetime(bar.get("date"), errors="coerce")
        close = _finite(bar.get("close"))
        if pd.isna(day) or pd.isna(close):
            continue
        for candidate in {day, day + pd.offsets.MonthEnd(0)}:
            gap = abs((candidate - target).days)
            if gap <= PRICE_MATCH_DAYS and (best_gap is None or gap < best_gap):
                best, best_gap = close, gap
    return best


def _split_factor(ratio: float) -> float:
    """Stock-split multiplier for a raw share-count ratio, else 1.0."""
    if not np.isfinite(ratio) or ratio <= 0:
        return 1.0
    if ratio >= 1.5:
        rounded = round(ratio)
        if rounded >= 2 and abs(ratio - rounded) / rounded <= 0.10:
            return float(rounded)
        return ratio
    if ratio <= 1 / 1.5:
        inverse = 1 / ratio
        rounded = round(inverse)
        if rounded >= 2 and abs(inverse - rounded) / rounded <= 0.10:
            return 1 / rounded
        return ratio
    return 1.0


def _latest_value(frame: pd.DataFrame, names: pd.Series, concept: str) -> tuple[float, object]:
    """Newest dimension-free positive value of ``concept`` and its date (NaN, NaT if none)."""
    rows = frame[names == concept].copy()
    if rows.empty:
        return np.nan, pd.NaT
    if "is_dimensioned" in rows.columns:
        rows = rows[~rows["is_dimensioned"].astype(bool)]
    rows["_value"] = pd.to_numeric(rows["value"], errors="coerce")
    rows = rows[rows["_value"] > 0]
    if rows.empty:
        return np.nan, pd.NaT
    if "period_instant" in rows.columns:
        instant = pd.to_datetime(rows["period_instant"], errors="coerce")
    else:
        instant = pd.Series(pd.NaT, index=rows.index)
    if "period_end" in rows.columns:
        end = pd.to_datetime(rows["period_end"], errors="coerce")
    else:
        end = pd.Series(pd.NaT, index=rows.index)
    rows["_date"] = instant.fillna(end)
    rows = rows.sort_values("_date", ascending=False, na_position="last")
    return _finite(rows.iloc[0]["_value"]), rows.iloc[0]["_date"]


def _extract_shares(facts: pd.DataFrame, year: int) -> float:
    """Shares outstanding for one fiscal year, in shares.

    In order: the reported outstanding count; issued minus treasury shares at
    the same date (filers such as Pfizer report only those two); the weighted
    average diluted, then basic, count for the year.
    """
    if facts is None or facts.empty:
        return np.nan
    if not {"concept", "value"} <= set(facts.columns):
        return np.nan
    frame = facts
    if "fiscal_year" in frame.columns:
        frame = frame[pd.to_numeric(frame["fiscal_year"], errors="coerce") == year]
    if frame.empty:
        return np.nan
    names = frame["concept"].astype(str).str.split(":").str[-1]
    for concept in ("CommonStockSharesOutstanding",
                    "EntityCommonStockSharesOutstanding",
                    # IFRS filers.
                    "NumberOfSharesOutstanding"):
        value, _ = _latest_value(frame, names, concept)
        if np.isfinite(value):
            return value

    issued, issued_date = _latest_value(frame, names, "CommonStockSharesIssued")
    if np.isfinite(issued):
        # The treasury concept was renamed in newer taxonomies; take either.
        for concept in ("TreasuryStockCommonShares", "TreasuryStockShares"):
            treasury, treasury_date = _latest_value(frame, names, concept)
            if np.isfinite(treasury) and treasury_date == issued_date and treasury < issued:
                return issued - treasury

    for concept in ("WeightedAverageNumberOfDilutedSharesOutstanding",
                    "WeightedAverageNumberOfSharesOutstandingBasic",
                    # IFRS filers.
                    "AdjustedWeightedAverageShares",
                    "WeightedAverageShares"):
        value, _ = _latest_value(frame, names, concept)
        if np.isfinite(value):
            return value
    return np.nan


def _normalise_splits(raw: dict[int, float]) -> dict[int, float]:
    """Scale historical share counts up to the newest split basis.

    Yahoo prices are split-adjusted, so share counts before a split must be
    multiplied by every later split factor or market cap collapses.
    """
    adjusted = dict(raw)
    years = sorted(year for year, value in raw.items() if np.isfinite(value))
    cumulative = 1.0
    for index in range(len(years) - 1, -1, -1):
        year = years[index]
        adjusted[year] = raw[year] * cumulative
        if index == 0:
            break
        older, newer = years[index - 1], year
        if raw[older] > 0:
            cumulative *= _split_factor(raw[newer] / raw[older])
    return adjusted


def _f_score(
    year: int,
    metrics: dict[int, dict],
    shares: dict[int, float],
) -> float:
    """Piotroski F-score from current vs prior year, NaN under 6 tests."""
    current = metrics[year]
    prior = metrics.get(current["prior_year"])
    if prior is None or prior is current:
        return np.nan

    tests: list[bool] = []
    ni = current["net_income"]
    ocf = current["operating_cash_flow"]
    if np.isfinite(ni):
        tests.append(ni > 0)
    if np.isfinite(ocf):
        tests.append(ocf > 0)
    roa = _safe_div(ni, current["total_assets"], positive_only=True)
    roa_prior = _safe_div(prior["net_income"], prior["total_assets"], positive_only=True)
    if np.isfinite(roa) and np.isfinite(roa_prior):
        tests.append(roa > roa_prior)
    if np.isfinite(ni) and np.isfinite(ocf):
        tests.append(ocf > ni)
    leverage = _safe_div(current["long_term_debt"], current["total_assets"], positive_only=True)
    leverage_prior = _safe_div(prior["long_term_debt"], prior["total_assets"], positive_only=True)
    if np.isfinite(leverage) and np.isfinite(leverage_prior):
        tests.append(leverage < leverage_prior)
    if np.isfinite(current["current_ratio"]) and np.isfinite(prior["current_ratio"]):
        tests.append(current["current_ratio"] > prior["current_ratio"])
    current_shares = shares.get(year, np.nan)
    prior_shares = shares.get(metrics[year]["prior_year"], np.nan)
    if np.isfinite(current_shares) and np.isfinite(prior_shares) and prior_shares > 0:
        tests.append(current_shares <= prior_shares * 1.02)
    if np.isfinite(current["gross_margin"]) and np.isfinite(prior["gross_margin"]):
        tests.append(current["gross_margin"] > prior["gross_margin"])
    turnover = _safe_div(current["revenue"], current["total_assets"], positive_only=True)
    turnover_prior = _safe_div(prior["revenue"], prior["total_assets"], positive_only=True)
    if np.isfinite(turnover) and np.isfinite(turnover_prior):
        tests.append(turnover > turnover_prior)

    if len(tests) < MIN_F_SCORE_TESTS:
        return np.nan
    return float(int(round(sum(1 for passed in tests if passed))))


# --------------------------------------------------------------------------- #
# Evaluation build
# --------------------------------------------------------------------------- #

def _reporting_currency(data: pd.DataFrame) -> str:
    """The latest filing's reporting currency (ratio data from the pipeline)."""
    if "currency" in data.columns:
        values = data["currency"].dropna().astype(str)
        if len(values):
            return values.iloc[-1].upper()
    return "USD"


def snap_ratio(value: float | None, tolerance: float = 0.03, ratios: tuple = NICE_RATIOS) -> float | None:
    """The ADS ratio ``value`` is close to (within ``tolerance``), else None.

    Tight by default: the filing's own figures give the ratio almost exactly.
    The Yahoo estimate (trailing EPS against the last annual EPS) passes more.
    """
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    best = min(ratios, key=lambda nice: abs(np.log(value / nice)))
    return float(best) if abs(value / best - 1) <= tolerance else None


def fetch_fx_history(base: str, quote_currency: str, years: int = PRICE_RANGE_YEARS) -> list[dict]:
    """Monthly ``quote_currency`` per one ``base`` (EUR -> USD: about 1.1), as price bars."""
    if base == quote_currency:
        return []
    return fetch_price_history(f"{base}{quote_currency}=X", years)


def _yahoo_summary(ticker: str, modules: str) -> dict:
    """Yahoo's quoteSummary modules (needs the cookie and crumb its site sets)."""
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    # A browser User-Agent and no Accept header: the crumb endpoint answers 406 otherwise.
    headers = {"User-Agent": YAHOO_BROWSER_UA}
    try:
        opener.open(Request("https://fc.yahoo.com", headers=headers), timeout=10).close()
    except Exception:  # noqa: BLE001, S110 - the 404 still sets the cookie
        pass
    with opener.open(Request("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=headers),
                     timeout=10) as response:
        crumb = response.read().decode("utf-8", "replace").strip()
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker)}?"
           + urlencode({"modules": modules, "crumb": crumb}))
    with opener.open(Request(url, headers=headers), timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return ((payload.get("quoteSummary") or {}).get("result") or [{}])[0] or {}


def _latest_eps(facts: pd.DataFrame, currency: str) -> float:
    """The newest annual basic EPS per ordinary share, in ``currency``."""
    if facts is None or facts.empty or "concept" not in facts.columns:
        return np.nan
    names = facts["concept"].astype(str).str.split(":").str[-1]
    frame = facts[names.isin(["BasicEarningsLossPerShare", "EarningsPerShareBasic"])]
    if "is_dimensioned" in frame.columns:
        frame = frame[~frame["is_dimensioned"].astype(bool)]
    if "currency" in frame.columns:
        frame = frame[frame["currency"].isna() | (frame["currency"].astype(str).str.upper() == currency)]
    frame = frame.assign(_value=pd.to_numeric(frame["value"], errors="coerce"),
                         _fy=pd.to_numeric(frame.get("fiscal_year"), errors="coerce")).dropna(subset=["_value", "_fy"])
    if frame.empty:
        return np.nan
    return float(frame.sort_values("_fy").iloc[-1]["_value"])


def market_scale(
    ticker: str,
    data: pd.DataFrame,
    facts: pd.DataFrame,
    statements: pd.DataFrame,
) -> tuple[dict[int, float] | None, str, str]:
    """Per fiscal year, quoted price -> price per ordinary share in the reporting currency.

    Returns ``(scale, price_currency, note)``. ``scale`` is ``None`` when the
    quote needs nothing (USD reporting, no ADSs). The ADS ratio comes from the
    pipeline (cover page or EPS per ADS), else from Yahoo's trailing EPS per
    traded share against the latest EPS per ordinary share; when it cannot be
    found the scale is empty, so market-based ratios stay blank, not wrong.
    """
    currency = _reporting_currency(data)
    print("[*] Valuation: reading the quote currency (Yahoo Finance)")
    quote_info = fetch_quote(ticker) or {}
    price_currency = str(quote_info.get("currency") or "USD").upper()
    is_adr = "ads" in data.columns and data["ads"].astype(str).str.lower().isin(["true", "1"]).any()
    ratio, source = np.nan, ""
    if "ads_ratio" in data.columns:
        known = pd.to_numeric(data["ads_ratio"], errors="coerce").dropna()
        if len(known):
            ratio = float(known.iloc[-1])
            sources = data.get("ads_ratio_source")
            source = str(sources.dropna().iloc[-1]) if sources is not None and sources.notna().any() else "filing"
    if currency == price_currency and not is_adr:
        return None, price_currency, ""

    if currency != price_currency:
        print(f"[*] Valuation: fetching {currency}/{price_currency} exchange rates (Yahoo Finance)")
    fx_history = fetch_fx_history(currency, price_currency)
    if currency != price_currency and not fx_history:
        return {}, price_currency, f"no {currency}/{price_currency} exchange rates; price-based ratios skipped"
    period_dates = _period_dates(data, statements)

    def fx_at(year: int) -> float:
        return 1.0 if currency == price_currency else _close_near(fx_history, period_dates.get(year))

    if not np.isfinite(ratio):
        if not is_adr:
            ratio, source = 1.0, "ordinary shares"
        else:
            print("[*] Valuation: ADS ratio not in the filings; estimating it from Yahoo's market cap")
            latest = int(pd.to_numeric(data["fiscal_year"], errors="coerce").max())
            ratio, source = _yahoo_ratio(ticker, data, facts, quote_info, latest, fx_at(latest))
    if not np.isfinite(ratio):
        return {}, price_currency, "ADS ratio unknown; price-based ratios skipped"
    scale = {}
    for year in pd.to_numeric(data["fiscal_year"], errors="coerce").dropna().astype(int):
        rate = fx_at(int(year))
        if np.isfinite(rate) and rate > 0:
            scale[int(year)] = 1.0 / (rate * ratio)
    note = (f"{price_currency} price per ADS = {ratio:g} ordinary shares ({source}), converted to {currency}"
            if is_adr else f"{price_currency} price converted to {currency}")
    return scale, price_currency, note


def _yahoo_ratio(ticker: str, data: pd.DataFrame, facts: pd.DataFrame, quote_info: dict,
                 latest: int, fx_latest: float) -> tuple[float, str]:
    """Ordinary shares per ADS from Yahoo, snapped to a usual ratio; (nan, "") when unsure.

    First the market cap: quoted price x ordinary shares / market cap (Shell
    2,00, TSMC 5,00, Toyota 11,1 with its treasury shares, so 10). Else the
    trailing EPS per ADS against the last annual EPS per share, which drifts
    when earnings move.
    """
    try:
        summary = _yahoo_summary(ticker, "summaryDetail,defaultKeyStatistics")
    except Exception:  # noqa: BLE001 - best effort
        return np.nan, ""
    details = summary.get("summaryDetail") or {}
    market_cap = _finite((details.get("marketCap") or {}).get("raw"))
    price = _finite(quote_info.get("price"))
    shares = _extract_shares(facts, latest)
    same_currency = str(details.get("currency") or quote_info.get("currency") or "").upper() == \
        str(quote_info.get("currency") or "USD").upper()
    if same_currency and np.isfinite(market_cap) and market_cap > 0 and np.isfinite(price) and np.isfinite(shares):
        guess = snap_ratio(price * shares / market_cap, tolerance=0.15, ratios=COMMON_RATIOS)
        if guess:
            return guess, "Yahoo market cap"
    eps_ads = _finite(((summary.get("defaultKeyStatistics") or {}).get("trailingEps") or {}).get("raw"))
    eps = _latest_eps(facts, _reporting_currency(data))
    if not np.isfinite(eps):
        # No EPS fact (Shell's recent years): net income over shares.
        income = _numeric(data, "NetIncomeLoss")[pd.to_numeric(data["fiscal_year"], errors="coerce") == latest]
        if len(income) and np.isfinite(shares) and shares > 0:
            eps = float(income.iloc[-1]) * USD_TO_MILLIONS / shares
    eps_share = eps * fx_latest
    guess = (snap_ratio(eps_ads / eps_share, tolerance=0.25, ratios=COMMON_RATIOS)
             if np.isfinite(eps_ads) and np.isfinite(eps_share) and eps_share else None)
    return (guess, "Yahoo EPS") if guess else (np.nan, "")


def _empty_evaluation() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="float64") for column in OUTPUT_COLUMNS})


def build_evaluation(
    ticker: str,
    df: pd.DataFrame,
    statements: pd.DataFrame,
    facts: pd.DataFrame,
    price_history: list[dict],
    price_scale: dict[int, float] | None = None,
    price_currency: str | None = None,
) -> pd.DataFrame:
    """One row per fiscal year with valuation ratios and quality scores.

    ``price_scale`` turns a year's quoted price into a price per ordinary
    share in the reporting currency (see ``market_scale``); a year missing
    from it gets no market-based ratios. ``None`` means the price needs no
    conversion (a US filer reporting in USD).
    """
    if df is None or df.empty or "fiscal_year" not in df.columns:
        return _empty_evaluation()

    data = df.copy()
    data["fiscal_year"] = pd.to_numeric(data["fiscal_year"], errors="coerce")
    data = data.dropna(subset=["fiscal_year"]).copy()
    if data.empty:
        return _empty_evaluation()
    data["fiscal_year"] = data["fiscal_year"].astype(int)
    data = (
        data.sort_values("fiscal_year")
        .drop_duplicates("fiscal_year", keep="last")
        .reset_index(drop=True)
    )
    years = [int(year) for year in data["fiscal_year"]]
    currency = _reporting_currency(data)

    statement_metrics = {
        "total_assets": _statement_metric(statements, years, *TOTAL_ASSETS),
        "total_liabilities": _total_liabilities(statements, years),
        "retained_earnings": _statement_metric(statements, years, *RETAINED_EARNINGS),
        "cash": _statement_metric(statements, years, *CASH),
        "equity": _statement_metric(statements, years, *EQUITY),
        "long_term_debt": _statement_metric(statements, years, *LONG_TERM_DEBT),
        "current_assets": _statement_metric(statements, years, *CURRENT_ASSETS),
        "current_liabilities": _statement_metric(statements, years, *CURRENT_LIABILITIES),
        "depreciation": _statement_metric(statements, years, *DEPRECIATION),
    }
    if statement_metrics["depreciation"].isna().all():
        statement_metrics["depreciation"] = _fact_metric(
            facts, years,
            ["DepreciationDepletionAndAmortization",
             "DepreciationAmortizationAndAccretionNet",
             "DepreciationExpense",
             "DepreciationAndAmortisationExpense"],
            currency,
        )

    # Financial-data columns first; fall back to statements when a filer did
    # not expose the line through the curated metric list.
    revenue = _numeric(data, "Revenues")
    net_income = _numeric(data, "NetIncomeLoss")
    operating_cash_flow = _numeric(data, "OperatingCashFlow")
    capex = _numeric(data, "CapEx")
    if "FreeCashFlow" in data.columns:
        free_cash_flow = pd.to_numeric(data["FreeCashFlow"], errors="coerce")
    else:
        free_cash_flow = operating_cash_flow - capex
    ebit = _numeric(data, "OperatingIncomeLoss")
    if "GrossProfit" in data.columns:
        gross_profit = pd.to_numeric(data["GrossProfit"], errors="coerce")
    else:
        gross_profit = revenue - _numeric(data, "CostOfRevenue")

    def by_year(series: pd.Series) -> pd.Series:
        return data["fiscal_year"].map(series).reset_index(drop=True)

    def pick(primary: pd.Series, fallback: pd.Series) -> pd.Series:
        return primary.where(primary.notna(), fallback)

    equity = pick(_numeric(data, "StockholdersEquity"), by_year(statement_metrics["equity"]))
    long_term_debt = pick(
        _numeric(data, "LongTermDebt"),
        by_year(statement_metrics["long_term_debt"]),
    )
    current_assets = pick(
        _numeric(data, "AssetsCurrent"),
        by_year(statement_metrics["current_assets"]),
    )
    current_liabilities = pick(
        _numeric(data, "LiabilitiesCurrent"),
        by_year(statement_metrics["current_liabilities"]),
    )
    total_assets = by_year(statement_metrics["total_assets"])
    total_liabilities = by_year(statement_metrics["total_liabilities"])
    retained_earnings = by_year(statement_metrics["retained_earnings"])
    cash = by_year(statement_metrics["cash"]).fillna(0.0)
    depreciation = by_year(statement_metrics["depreciation"]).fillna(0.0)

    # Share counts and price history.
    raw_shares = {year: _extract_shares(facts, year) for year in years}
    share_counts = _normalise_splits(raw_shares)
    period_dates = _period_dates(data, statements)
    prices = {year: _close_near(price_history, period_dates.get(year)) for year in years}

    rows: list[dict] = []
    metrics: dict[int, dict] = {}
    for position, year in enumerate(years):
        price = _finite(prices[year])
        shares = _finite(share_counts.get(year, np.nan))
        # Price per ordinary share in the reporting currency.
        share_price = price if price_scale is None else price * _finite(price_scale.get(year, np.nan))
        market_cap = share_price * shares / USD_TO_MILLIONS if (
            np.isfinite(share_price) and np.isfinite(shares)
        ) else np.nan

        net_income_value = _finite(net_income.iloc[position])
        revenue_value = _finite(revenue.iloc[position])
        ocf_value = _finite(operating_cash_flow.iloc[position])
        fcf_value = _finite(free_cash_flow.iloc[position])
        ebit_value = _finite(ebit.iloc[position])
        ebitda_value = ebit_value + _finite(depreciation.iloc[position]) if (
            np.isfinite(ebit_value)
        ) else np.nan
        gross_value = _finite(gross_profit.iloc[position])
        equity_value = _finite(equity.iloc[position])
        debt_value = _finite(long_term_debt.iloc[position])
        assets_value = _finite(total_assets.iloc[position])
        liabilities_value = _finite(total_liabilities.iloc[position])
        cash_value = _finite(cash.iloc[position])
        current_assets_value = _finite(current_assets.iloc[position])
        current_liabilities_value = _finite(current_liabilities.iloc[position])

        enterprise_value = np.nan
        if np.isfinite(market_cap):
            enterprise_value = market_cap + (debt_value if np.isfinite(debt_value) else 0.0) \
                - (cash_value if np.isfinite(cash_value) else 0.0)

        pe = _safe_div(market_cap, net_income_value, positive_only=True)
        pb = _safe_div(market_cap, equity_value, positive_only=True)
        pfcf = _safe_div(market_cap, fcf_value, positive_only=True)
        ev_ebit = _safe_div(enterprise_value, ebit_value, positive_only=True)
        ev_ebitda = _safe_div(enterprise_value, ebitda_value, positive_only=True)
        earnings_yield = _safe_div(ebit_value, enterprise_value, positive_only=True)
        # Greenblatt return on capital: EBIT / (net working capital + net fixed
        # assets). Assets minus current liabilities is the standard practical
        # proxy when PP&E is not broken out.
        invested_capital = (
            assets_value - current_liabilities_value
            if np.isfinite(assets_value) and np.isfinite(current_liabilities_value)
            else np.nan
        )
        return_on_capital = _safe_div(ebit_value, invested_capital, positive_only=True)
        fcf_yield = _safe_div(fcf_value, enterprise_value, positive_only=True)

        # Altman Z-score (public-manufacturer form).
        z_score = np.nan
        retained_value = _finite(retained_earnings.iloc[position])
        if (
            np.isfinite(assets_value) and assets_value > 0
            and np.isfinite(liabilities_value) and liabilities_value > 0
            and np.isfinite(market_cap)
            and np.isfinite(retained_value)
            and np.isfinite(current_assets_value)
            and np.isfinite(current_liabilities_value)
            and np.isfinite(ebit_value)
            and np.isfinite(revenue_value)
        ):
            working_capital = current_assets_value - current_liabilities_value
            z_score = (
                1.2 * working_capital / assets_value
                + 1.4 * retained_value / assets_value
                + 3.3 * ebit_value / assets_value
                + 0.6 * market_cap / liabilities_value
                + 1.0 * revenue_value / assets_value
            )

        accruals_ratio = np.nan
        if np.isfinite(assets_value) and assets_value > 0:
            accruals_ratio = _safe_div(
                net_income_value - ocf_value, assets_value
            ) if np.isfinite(net_income_value) and np.isfinite(ocf_value) else np.nan

        metrics[year] = {
            "prior_year": years[position - 1] if position > 0 else year,
            "net_income": net_income_value,
            "operating_cash_flow": ocf_value,
            "total_assets": assets_value,
            "long_term_debt": debt_value,
            "current_ratio": _safe_div(
                current_assets_value, current_liabilities_value, positive_only=True
            ),
            "gross_margin": _safe_div(gross_value, revenue_value, positive_only=True),
            "revenue": revenue_value,
        }

        rows.append({
            "fiscal_year": year,
            "price": price,
            "shares": shares,
            "market_cap": market_cap,
            "pe": pe,
            "pb": pb,
            "pfcf": pfcf,
            "ev_ebit": ev_ebit,
            "ev_ebitda": ev_ebitda,
            "earnings_yield": earnings_yield,
            "return_on_capital": return_on_capital,
            "fcf_yield": fcf_yield,
            "pe_median_hist": np.nan,
            "pe_percentile": np.nan,
            "f_score": _f_score(year, metrics, share_counts),
            "z_score": z_score,
            "accruals_ratio": accruals_ratio,
            "currency": currency,
            "price_currency": price_currency or "USD",
        })

    evaluation = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    pe_series = pd.to_numeric(evaluation["pe"], errors="coerce").dropna()
    if len(pe_series):
        evaluation["pe_median_hist"] = float(pe_series.median())
        if len(pe_series) >= 3:
            for index, value in evaluation["pe"].items():
                if pd.notna(value):
                    evaluation.at[index, "pe_percentile"] = (
                        (pe_series < value).sum() + 0.5 * (pe_series == value).sum()
                    ) / len(pe_series) * 100.0
    evaluation["fiscal_year"] = evaluation["fiscal_year"].astype(int)
    return evaluation.sort_values("fiscal_year").reset_index(drop=True)


def write_evaluation(ticker: str, evaluation: pd.DataFrame, output_dir: str) -> str:
    """Write ``evaluation.csv`` into ``output_dir`` and return its path."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "evaluation.csv")
    evaluation.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Manual run
# --------------------------------------------------------------------------- #

def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python valuation.py TICKER")
    ticker = sys.argv[1].upper()
    output_dir = os.path.join(BASE_OUTPUT_DIR, ticker)

    def load(name: str) -> pd.DataFrame:
        path = os.path.join(output_dir, name)
        if not os.path.exists(path):
            print(f"[!] Missing {path}; continuing with empty input")
            return pd.DataFrame()
        return pd.read_csv(path)

    df = load("financial_data.csv")
    statements = load("statements.csv")
    facts = load("facts.csv")
    history = fetch_price_history(ticker, PRICE_RANGE_YEARS)
    evaluation = build_evaluation(ticker, df, statements, facts, history)
    path = write_evaluation(ticker, evaluation, output_dir)
    print(evaluation.to_string(index=False))
    print(f"[+] Wrote {path}")


if __name__ == "__main__":
    main()
