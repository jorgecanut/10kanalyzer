#!/usr/bin/env python3
"""Collect the narrative sections of a 10-K that the numeric pipeline drops.

``edgar_dashboard.py`` keeps numbers only: statements, facts and note schedules.
The MD&A, risk factors, legal proceedings, accounting-policy disclosures and
revenue disaggregation all live in text blocks that never become a fact. This
script pulls them from ``openbb-sec`` (keyless, EDGAR as-filed) into markdown
files next to the CSVs, so the dashboard folder is the whole filing, not half.

Usage:
    python textpanel.py AAPL                 # all sections, newest dashboard year
    python textpanel.py JPM --year 2024      # a specific filing year
    python textpanel.py AAPL --sections mda overview

The default year is the newest ``fiscal_year`` in ``financial_graphs/<TICKER>/
financial_data.csv``, so the text matches the 10-K the numbers came from. Pass
``--year`` to override.

Outputs, under ``financial_graphs/<TICKER>/text/``:
    overview.md  risk_factors.md  mda.md  legal.md  disclosures.md  segments.md
    _meta.json   per-section status, size and period, for other tools

Sections that a filer does not tag are recorded as ``empty``, never invented.
Exit code is 0 unless every section failed (then the module is unusable).
"""

from __future__ import annotations

import argparse
import json
import re

import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_OUTPUT_DIR = Path("financial_graphs")

# name -> openbb.sec function, and whether the section is a list of text blocks.
SECTIONS: dict[str, str] = {
    "overview": "company_overview",
    "risk_factors": "risk_factors",
    "mda": "management_discussion_analysis",
    "legal": "legal_proceedings",
    "disclosures": "disclosures",
    "segments": "segment_revenue",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def latest_fiscal_year(ticker: str) -> int | None:
    """The newest fiscal year the numeric pipeline already extracted.

    Keeps the text sections on the same 10-K the numbers came from instead of
    whatever filing happens to be most recent on EDGAR.
    """
    path = BASE_OUTPUT_DIR / ticker / "financial_data.csv"
    if not path.exists():
        return None
    import pandas as pd

    table = pd.read_csv(path)
    if "fiscal_year" not in table.columns or table.empty:
        return None
    newest = table["fiscal_year"].max()
    if pd.isna(newest):
        return None
    return int(newest)


def render(name: str, payload) -> str:
    """Turn whatever the endpoint returned into one markdown document."""
    if payload is None:
        return ""
    content = getattr(payload, "content", None)
    if isinstance(content, str):
        return content.strip()

    items = payload if isinstance(payload, list) else [payload]
    blocks: list[str] = []
    for item in items:
        if name == "risk_factors":
            heading = getattr(item, "risk_factor", None)
            body = getattr(item, "text", "")
            if body:
                blocks.append(f"## {heading}\n\n{body}".strip() if heading else str(body))
        elif name == "disclosures":
            heading = getattr(item, "name", None)
            body = getattr(item, "text", "")
            if body:
                blocks.append(f"## {heading}\n\n{body}".strip() if heading else str(body))
        else:
            heading = getattr(item, "name", None)
            body = getattr(item, "text", "")
            if body:
                blocks.append(f"## {heading}\n\n{body}".strip() if heading else str(body))
            elif heading:
                blocks.append(str(heading))
    return "\n\n---\n\n".join(b for b in blocks if b).strip()


def collect(ticker: str, sections: list[str], year: int | None) -> dict:
    """Fetch every requested section; failures are recorded, not raised."""
    from openbb import obb

    year = year if year is not None else latest_fiscal_year(ticker)
    folder = BASE_OUTPUT_DIR / ticker / "text"
    folder.mkdir(parents=True, exist_ok=True)
    meta: dict = {
        "ticker": ticker,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calendar_year": year,
        "sections": {},
    }

    for name in sections:
        kwargs: dict = {"symbol": ticker}
        if year is not None:
            kwargs["calendar_year"] = year
        try:
            endpoint = getattr(obb.sec, SECTIONS[name])
            response = endpoint(**kwargs)
            text = render(name, response.results)
        except Exception as exc:  # noqa: BLE001 - one section must not stop the rest
            meta["sections"][name] = {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
            print(f"  [!] {name:<12} error: {exc.__class__.__name__}")
            continue

        status = "ok" if text else "empty"
        if text:
            (folder / f"{name}.md").write_text(text + "\n", encoding="utf-8")
        meta["sections"][name] = {
            "status": status,
            "chars": len(text),
            "file": f"{name}.md" if text else None,
        }
        print(f"  [+] {name:<12} {status:<6} {len(text):>8,} chars")

    (folder / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # keep docker logs streaming
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="one or more ticker symbols")
    parser.add_argument("--year", type=int, default=None, help="calendar year of the filing")
    parser.add_argument(
        "--sections",
        nargs="*",
        default=list(SECTIONS),
        choices=list(SECTIONS),
        help="subset of sections to fetch (default: all)",
    )
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers]
    for ticker in tickers:
        if not TICKER_RE.match(ticker):
            print(f"invalid ticker: {ticker!r}", file=sys.stderr)
            sys.exit(2)
    results = {}
    for ticker in tickers:
        print(f"\n{ticker}")
        try:
            results[ticker] = collect(ticker, args.sections, args.year)
        except ImportError:
            print("  [!] openbb-sec not installed: uv pip install -r requirements-optional.txt")
            sys.exit(1)

    ok = sum(1 for m in results.values() if any(s["status"] == "ok" for s in m["sections"].values()))
    print(f"\n{ok}/{len(results)} tickers produced text")
    statuses = [s["status"] for m in results.values() for s in m["sections"].values()]
    sys.exit(1 if statuses and all(s == "error" for s in statuses) else 0)


if __name__ == "__main__":
    main()
