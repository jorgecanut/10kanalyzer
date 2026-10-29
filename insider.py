#!/usr/bin/env python3
"""Recent insider (Form 4) open-market transactions for a ticker.

Collects ``P`` (purchase) and ``S`` (sale) transactions filed in the last
``months`` months (3 years by default) and writes ``insider.json`` into the
ticker's output directory. The UI then shows 1–3 years of it on request.

Design notes:
    * Only open-market purchases/sales count. Grants (A), option exercises (M),
      tax withholding (F), gifts (G) and conversions (C) are not market trades
      and are ignored on purpose.
    * ``value`` is ``shares * price`` when the filing reports a price. Sales
      made in several lots often report 0 and give only the range in a
      footnote ("weighted average ... $X to $Y"); the midpoint is used then and
      ``value_estimated`` is true. With no price anywhere ``value`` is ``None``.
    * Only filings whose issuer is the company itself count. A company that is
      an insider of another issuer (a >10% holder, an acquirer) files Form 4s
      about that issuer's shares, and those are not its insiders' trades.
    * Failure policy: no exception ever reaches the caller. A failure returns
      ``[]`` (collect) or ``None`` (write) after one printed warning.
    * Each Form 4's trades are kept in the filing cache (``filing_cache``,
      one ``form4`` entry per company) when a cache directory is given, so a
      later run parses only the Form 4s filed since.
    * ``write_insiders`` always writes a file, even when collection fails or
      returns nothing. The JSON then has an empty ``transactions`` list, so the
      UI can tell "no insider data" apart from "pipeline never ran" (absent
      file). Only a filesystem failure makes it return ``None``.

Usage:
    python insider.py NFLX
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import Any

import filing_cache

# The SEC requires a descriptive User-Agent identifying who is making requests.
# Same pattern (and default) as edgar_dashboard.py.
try:
    from edgar import Company, set_identity

    set_identity(os.environ.get("EDGAR_IDENTITY", "Data Analyst analyst@example.com"))
    _EDGAR_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - only when edgar is missing/broken
    Company = None  # type: ignore[assignment]
    _EDGAR_ERROR = exc

# Cap the amount of SEC work per run: each ``filing.obj()`` parses a full XML
# document. The window (3 years by default) is the real limit; this is only a
# safety valve for pathological filing counts. All trades inside the window are
# returned — there is no per-transaction cap. When the valve trips, ``since`` in
# the JSON says how far back the data really goes.
MAX_FILINGS = 1500
DEFAULT_MONTHS = 36
OUTPUT_FILENAME = "insider.json"

# "$601.88 to $602.06", "$1,000.50 - $1,001.50": the price range of a weighted
# average in a Form 4 footnote.
PRICE_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:to|through|and|-|–|—)\s*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
FOOTNOTE_ID_RE = re.compile(r"F\d+")
# A cached Form 4 is reused only while the code that read it is unchanged.
CACHE_KIND = "form4"
CACHE_VERSION = filing_cache.version(CACHE_KIND)
CACHE_NAME = "form4"
# Save the cache this often while reading, so a cancelled run keeps its work.
CACHE_SAVE_EVERY = 100


def _warn(message: str) -> None:
    """Print exactly one warning line; never raise."""
    print(f"insider: {message}", file=sys.stderr)


def _months_ago(months: int) -> date:
    """Calendar-aware cutoff: today minus ``months`` months."""
    today = date.today()
    month_index = today.month - 1 - months
    year = today.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp the day for short months (Jan 31 minus 1 month -> Feb 28).
    day = min(today.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _to_float(value: Any) -> float | None:
    """Best-effort float; ``None`` for NaN, blanks and non-numeric strings."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _parse_date(value: Any) -> date | None:
    """Parse ``YYYY-MM-DD`` (the Form 4 transaction date format)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _footnote_price(ids: Any, footnotes: Any) -> float | None:
    """Midpoint of the first price range in the row's footnotes, if any."""
    for footnote_id in FOOTNOTE_ID_RE.findall(str(ids or "")):
        try:
            text = footnotes.get(footnote_id) or ""
        except Exception:
            continue
        match = PRICE_RANGE_RE.search(str(text))
        if not match:
            continue
        low, high = (_to_float(part.replace(",", "")) for part in match.groups())
        if low and high and 0 < low <= high:
            return (low + high) / 2
    return None


def _same_issuer(ownership: Any, company_cik: int | None) -> bool:
    """False only when the filing clearly names a different issuer."""
    try:
        issuer_cik = int(str(ownership.issuer.cik).strip())
    except Exception:
        return True
    return company_cik is None or issuer_cik == company_cik


def _insider_title(ownership: Any) -> str:
    """Officer title / director flag, falling back to a generic label."""
    try:
        title = (getattr(ownership, "position", "") or "").strip()
    except Exception:
        title = ""
    if title:
        return title
    try:
        owners = getattr(ownership.reporting_owners, "owners", None) or []
        for owner in owners:
            title = (getattr(owner, "position", "") or "").strip()
            if title:
                return title
    except Exception:
        pass
    return "Insider"


def _rows_from_ownership(ownership: Any) -> list[dict]:
    """Convert one Form 4 object into transaction dicts."""
    rows: list[dict] = []
    try:
        market = ownership.market_trades
    except Exception:
        return rows
    if market is None or getattr(market, "empty", True):
        return rows

    name = ""
    try:
        name = (getattr(ownership, "insider_name", "") or "").strip()
    except Exception:
        pass
    title = _insider_title(ownership)

    for _, row in market.iterrows():
        try:
            code = str(row.get("Code") or "").strip().upper()

            # P = purchase, S = sale. Anything else is not a market trade.
            if code == "P":
                trade_type = "buy"
            elif code == "S":
                trade_type = "sell"
            elif code:
                continue
            else:
                # No code: fall back to the signed share delta if present.
                shares_signed = _to_float(row.get("Shares"))
                if shares_signed is None or shares_signed == 0:
                    continue
                trade_type = "buy" if shares_signed > 0 else "sell"

            shares = _to_float(row.get("Shares"))
            if shares is None:
                continue
            shares = abs(shares)

            price = _to_float(row.get("Price"))
            estimated = False
            if not price or price <= 0:
                price = _footnote_price(row.get("footnotes"), getattr(ownership, "footnotes", None))
                estimated = price is not None
            value = round(shares * price, 2) if price and price > 0 else None

            trade_date = _parse_date(row.get("Date"))
            if trade_date is None:
                continue

            rows.append(
                {
                    "date": trade_date.isoformat(),
                    "name": name or "Unknown",
                    "title": title,
                    "type": trade_type,
                    "shares": float(shares),
                    "value": value,
                    "value_estimated": estimated,
                }
            )
        except Exception:
            # One malformed row must not lose the rest of the filing.
            continue
    return rows


def collect_insiders(ticker: str, months: int = DEFAULT_MONTHS, cache_dir: str | None = None) -> list[dict]:
    """Return recent open-market insider trades, newest first.

    Never raises: returns ``[]`` on any failure (bad ticker, network, parse).
    """
    return _collect(ticker, months, cache_dir)[0]


def _collect(ticker: str, months: int, cache_dir: str | None = None) -> tuple[list[dict], str | None]:
    """Trades plus the oldest date they cover (``None`` on failure)."""
    try:
        if _EDGAR_ERROR is not None or Company is None:
            _warn(f"edgartools unavailable: {_EDGAR_ERROR}")
            return [], None
        if not ticker or not str(ticker).strip():
            _warn("empty ticker")
            return [], None
        ticker = str(ticker).strip().upper()
        months = max(1, int(months))
        cutoff = _months_ago(months)

        company = Company(ticker)
        try:
            company_cik = int(company.cik)
        except Exception:
            company_cik = None
        print(f"[*] Insider trades: listing Form 4 filings since {cutoff.isoformat()}", flush=True)
        filings = company.get_filings(form="4")
        if filings is None:
            _warn(f"no Form 4 filings for {ticker}")
            return [], cutoff.isoformat()

        # accession -> that Form 4's trades ([] for another issuer's or none).
        cache: dict[str, list[dict]] = {}
        if company_cik is not None and cache_dir:
            print(f"[*] Insider trades: filing cache {filing_cache.explain(cache_dir, CACHE_KIND)}", flush=True)
        if company_cik is not None:
            cache = filing_cache.load(cache_dir, CACHE_KIND, CACHE_VERSION, company_cik, CACHE_NAME) or {}
        new = 0
        transactions: list[dict] = []
        parsed = 0
        since = cutoff.isoformat()
        last_filed: date | None = None
        for filing in filings:
            if parsed >= MAX_FILINGS:
                if last_filed is not None:
                    since = last_filed.isoformat()
                _warn(f"stopped after {MAX_FILINGS} filings; data starts {since}")
                break
            try:
                filing_date = _parse_date(getattr(filing, "filing_date", None))
                # Filings are newest first: once one predates the window every
                # later one does too. Keep going if the date is unreadable.
                if filing_date is not None and filing_date < cutoff:
                    break
                parsed += 1
                if filing_date is not None:
                    last_filed = filing_date
                if parsed % 25 == 0:
                    cached = f", {parsed - new} from the cache" if parsed > new else ""
                    print(f"[*] Insider trades: read {parsed} Form 4 filings{cached}, back to {last_filed}",
                          flush=True)
                accession = getattr(filing, "accession_no", None)
                rows = cache.get(accession) if accession else None
                if rows is None:
                    ownership = filing.obj()
                    same = ownership is not None and _same_issuer(ownership, company_cik)
                    rows = _rows_from_ownership(ownership) if same else []
                    if accession:
                        cache[accession] = rows
                        new += 1
                        if new % CACHE_SAVE_EVERY == 0:
                            filing_cache.store(cache_dir, CACHE_KIND, CACHE_VERSION, company_cik, CACHE_NAME, cache)
                for row in rows:
                    if row["date"] >= cutoff.isoformat():
                        transactions.append(row)
            except Exception as exc:
                _warn(f"skipped filing {getattr(filing, 'accession_no', '?')}: {exc}")
                continue

        if new and company_cik is not None:
            filing_cache.store(cache_dir, CACHE_KIND, CACHE_VERSION, company_cik, CACHE_NAME, cache)
        if parsed and cache_dir:
            print(f"[*] Insider trades: {parsed} Form 4 filings, {parsed - new} from the cache", flush=True)
        transactions.sort(key=lambda item: item["date"], reverse=True)
        return transactions, since
    except Exception as exc:
        _warn(f"failed to collect insiders for {ticker}: {exc}")
        return [], None


def write_insiders(ticker: str, output_dir: str, months: int = DEFAULT_MONTHS,
                   cache_dir: str | None = None) -> str | None:
    """Write ``<output_dir>/insider.json`` and return its path.

    Always writes a file (empty ``transactions`` when nothing was found or the
    collection failed) so callers can distinguish "no data" from "not run".
    Returns ``None`` only when the file itself could not be written.
    """
    ticker = str(ticker or "").strip().upper()
    try:
        transactions, since = _collect(ticker, months, cache_dir)
        payload = {
            "ticker": ticker,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "months": months,
            "since": since,
            "transactions": transactions,
        }
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, OUTPUT_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return path
    except Exception as exc:
        _warn(f"failed to write {OUTPUT_FILENAME} for {ticker}: {exc}")
        return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python insider.py <TICKER> [months]", file=sys.stderr)
        return 2
    ticker = argv[1].strip().upper()
    months = int(argv[2]) if len(argv) > 2 else DEFAULT_MONTHS
    output_dir = os.path.join("financial_graphs", ticker)
    path = write_insiders(ticker, output_dir, months=months)
    if path is None:
        return 1
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    transactions = payload.get("transactions", [])
    print(f"{ticker}: {len(transactions)} insider transaction(s) -> {path}")
    for row in transactions[:3]:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
