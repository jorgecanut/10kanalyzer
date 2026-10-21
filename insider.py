#!/usr/bin/env python3
"""Recent insider (Form 4) open-market transactions for a ticker.

Collects ``P`` (purchase) and ``S`` (sale) transactions filed in the last
``months`` months and writes ``insider.json`` into the ticker's output
directory.

Design notes:
    * Only open-market purchases/sales count. Grants (A), option exercises (M),
      tax withholding (F), gifts (G) and conversions (C) are not market trades
      and are ignored on purpose.
    * ``value`` is ``shares * price`` when the filing reports a price, else
      ``None`` (many grants and some sales omit it).
    * Failure policy: no exception ever reaches the caller. A failure returns
      ``[]`` (collect) or ``None`` (write) after one printed warning.
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
import sys
from datetime import date, datetime, timezone
from typing import Any

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
# document, so a busy ticker can have thousands of Form 4s.
MAX_FILINGS = 20
MAX_TRANSACTIONS = 100
OUTPUT_FILENAME = "insider.json"


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
                }
            )
        except Exception:
            # One malformed row must not lose the rest of the filing.
            continue
    return rows


def collect_insiders(ticker: str, months: int = 6) -> list[dict]:
    """Return recent open-market insider trades, newest first.

    Never raises: returns ``[]`` on any failure (bad ticker, network, parse).
    """
    try:
        if _EDGAR_ERROR is not None or Company is None:
            _warn(f"edgartools unavailable: {_EDGAR_ERROR}")
            return []
        if not ticker or not str(ticker).strip():
            _warn("empty ticker")
            return []
        ticker = str(ticker).strip().upper()
        months = max(1, int(months))
        cutoff = _months_ago(months)

        filings = Company(ticker).get_filings(form="4")
        if filings is None:
            _warn(f"no Form 4 filings for {ticker}")
            return []

        transactions: list[dict] = []
        parsed = 0
        for filing in filings:
            if parsed >= MAX_FILINGS:
                break
            try:
                filing_date = _parse_date(getattr(filing, "filing_date", None))
                # Filings are newest first: once one predates the window every
                # later one does too. Keep going if the date is unreadable.
                if filing_date is not None and filing_date < cutoff:
                    break
                parsed += 1
                ownership = filing.obj()
                if ownership is None:
                    continue
                for row in _rows_from_ownership(ownership):
                    if row["date"] >= cutoff.isoformat():
                        transactions.append(row)
            except Exception as exc:
                _warn(f"skipped filing {getattr(filing, 'accession_no', '?')}: {exc}")
                continue

        transactions.sort(key=lambda item: item["date"], reverse=True)
        return transactions[:MAX_TRANSACTIONS]
    except Exception as exc:
        _warn(f"failed to collect insiders for {ticker}: {exc}")
        return []


def write_insiders(ticker: str, output_dir: str, months: int = 6) -> str | None:
    """Write ``<output_dir>/insider.json`` and return its path.

    Always writes a file (empty ``transactions`` when nothing was found or the
    collection failed) so callers can distinguish "no data" from "not run".
    Returns ``None`` only when the file itself could not be written.
    """
    ticker = str(ticker or "").strip().upper()
    try:
        transactions = collect_insiders(ticker, months=months)
        payload = {
            "ticker": ticker,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    months = int(argv[2]) if len(argv) > 2 else 6
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
