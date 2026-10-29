#!/usr/bin/env python3
"""Generic, XBRL-driven extraction for the SEC 10-K dashboard.

The point of this module is that no metric is hard-coded. edgartools reads each
filing's presentation linkbase and maps every tagged line onto a
``standard_concept``; we keep whatever the filer actually reported. Charts and
reports are then driven by that data, so a new company needs no per-filer tag
work.

Two extractors, each returning long-form rows (one row per line/fact):

- ``extract_statements`` — the five primary statements, dimension-free.
- ``extract_facts`` — every fact for the filing's period, dimensions included.

What they return is kept in the filing cache, and this file's hash is part of
its version (``filing_cache.KINDS``): code that only uses the rows (the note
schedules, for instance) lives in ``edgar_dashboard.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# statement key -> (edgartools statement method, human label)
STATEMENTS: dict[str, tuple[str, str]] = {
    "income": ("income_statement", "Income Statement"),
    "balance": ("balance_sheet", "Balance Sheet"),
    "cashflow": ("cash_flow_statement", "Cash Flow Statement"),
    "equity": ("statement_of_equity", "Statement of Equity"),
    "comprehensive": ("comprehensive_income", "Comprehensive Income"),
}

_FACT_COLUMNS = [
    "concept", "label", "numeric_value", "unit_ref", "currency", "decimals",
    "period_start", "period_end", "period_instant", "is_dimensioned",
    "dimension", "member", "full_dimension_label", "statement_type",
]


def _period_column(data: pd.DataFrame, period_of_report: str) -> str | None:
    """Pick the dataframe column for this filing's own period.

    Column headers look like ``2024-12-28`` (instant) or ``2024-12-28 (FY)``
    (duration). Only the filing's period is used, so restatements in later
    filings do not overwrite the originally reported year.
    """
    for column in data.columns:
        if isinstance(column, str) and column == period_of_report:
            return column
    for column in data.columns:
        if isinstance(column, str) and column.startswith(period_of_report):
            return column
    return None


def _clean(value, default=None):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return value


def _root_of(concept: str, parent_map: dict[str, str | None]) -> str:
    """Walk parent links to the top-level concept of a statement line."""
    seen: set[str] = set()
    while concept in parent_map and parent_map[concept] and concept not in seen:
        seen.add(concept)
        concept = parent_map[concept]
    return concept


def _classify_section(rows: list[dict]) -> None:
    """Assign Assets / Liabilities&Equity from the parent chain.

    Robust to filings (e.g. Trupanion) whose presentation has no top-level
    abstract headers, where header-based sectioning leaves everything unset.
    """
    parent_map = {r["concept"]: r["parent_concept"] for r in rows}
    for r in rows:
        root = _root_of(r["concept"], parent_map)
        if root == "us-gaap_Assets":
            r["section"] = "ASSETS"
        elif root in ("us-gaap_LiabilitiesAndStockholdersEquity",
                      "us-gaap_StockholdersEquity",
                      "us-gaap_Liabilities"):
            r["section"] = "LIABILITIES AND EQUITY"


def extract_statements(
    xbrl,
    period_of_report: str,
    values: dict[str, dict] | None = None,
    convenience: dict[str, list[float]] | None = None,
) -> list[dict]:
    """Every dimension-free line of the five primary statements, long-form.

    For some foreign filers edgartools renders a line's USD convenience copy
    (TSMC's 2019 equity) among lines in TWD. A rendered number that equals
    one of ``convenience`` (``calc_check.convenience_values``) is replaced by
    the reporting-currency value from ``values`` (``calc_check.period_values``),
    keeping the statement's presentation sign. Every other number is kept as
    rendered (signs, opening balances).
    """
    rows: list[dict] = []
    for key, (method, _label) in STATEMENTS.items():
        try:
            statement = getattr(xbrl.statements, method)()
            data = statement.to_dataframe() if statement is not None else None
        except Exception:  # noqa: BLE001 - a missing statement must not sink the filing
            continue
        if data is None or data.empty or "concept" not in data.columns:
            continue
        column = _period_column(data, period_of_report)
        if column is None:
            continue

        line_rows: list[dict] = []
        section = None
        for _, row in data.iterrows():
            try:
                concept = str(row.get("concept") or "")
                label = str(row.get("label") or "")
                level = row.get("level")
                if bool(row.get("abstract")):
                    # A top-level header names the section (ASSETS, REVENUE, ...).
                    if label and (level is None or float(level) <= 2):
                        section = label
                    continue
                if bool(row.get("dimension")):  # breakdown row, not the consolidated line
                    continue
                value = pd.to_numeric(row.get(column), errors="coerce")
                if pd.notna(value) and value and convenience and values and concept in values:
                    for other in convenience.get(concept, []):
                        if other and abs(abs(value) / abs(other) - 1) < 1e-6:
                            value = values[concept]["value"] * (value / other)
                            break
                if pd.isna(value):
                    continue
                standard = row.get("standard_concept")
                parent = row.get("parent_concept")
                line_rows.append({
                    "statement": key,
                    "section": section,
                    "concept": concept,
                    "standard_concept": _clean(standard),
                    "label": label,
                    "value": float(value),
                    "parent_concept": _clean(parent),
                    "level": _clean(level),
                    "balance": _clean(row.get("balance")),
                    "weight": _clean(row.get("weight")),
                    "period_of_report": period_of_report,
                })
            except (ValueError, TypeError):
                continue
        if key == "balance":
            _classify_section(line_rows)
        rows.extend(line_rows)
    return rows


def extract_facts(xbrl, period_of_report: str) -> list[dict]:
    """Every fact whose period ends (or instant) at the report date.

    Keeps dimensional facts too, so segment/note detail is retained.
    """
    try:
        facts = xbrl.facts.to_dataframe()
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] {period_of_report}: facts unreadable ({exc.__class__.__name__}: {exc})")
        return []
    if facts is None or facts.empty:
        return []

    target = pd.Timestamp(period_of_report)
    end = pd.to_datetime(facts["period_end"], errors="coerce")
    instant = pd.to_datetime(facts.get("period_instant"), errors="coerce") \
        if "period_instant" in facts.columns else pd.Series(pd.NaT, index=facts.index)
    subset = facts[end.eq(target) | instant.eq(target)]
    dei = facts[
        (facts["concept"] == "dei:EntityCommonStockSharesOutstanding")
        & instant.notna()
        & (instant > target)
        & (instant <= target + pd.Timedelta(days=45))
    ]
    if not dei.empty:
        dei_instants = instant.loc[dei.index]
        subset = pd.concat([subset, dei[dei_instants == dei_instants.max()]])
    if subset.empty:
        return []

    out: list[dict] = []
    for _, row in subset.iterrows():
        value = pd.to_numeric(row.get("numeric_value"), errors="coerce")
        out.append({
            "period_of_report": period_of_report,
            "concept": str(row.get("concept") or ""),
            "label": str(row.get("label") or ""),
            "value": None if pd.isna(value) else float(value),
            "unit": _clean(row.get("unit_ref")),
            # ISO code of a money fact (TWD, and USD for a convenience copy).
            "currency": _clean(row.get("currency")),
            "decimals": _clean(row.get("decimals")),
            "period_start": _clean(row.get("period_start")),
            "period_end": _clean(row.get("period_end")),
            "period_instant": _clean(row.get("period_instant")),
            "is_dimensioned": bool(row.get("is_dimensioned")),
            "dimension": _clean(row.get("dimension")),
            "member": _clean(row.get("member")),
            "dimension_label": _clean(row.get("full_dimension_label")),
            "statement_type": _clean(row.get("statement_type")),
        })
    return out


def frames(statement_rows, fact_rows, note_rows) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Turn the row lists into DataFrames (empty-safe)."""
    return (
        pd.DataFrame(statement_rows),
        pd.DataFrame(fact_rows),
        pd.DataFrame(note_rows),
    )
