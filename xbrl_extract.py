#!/usr/bin/env python3
"""Generic, XBRL-driven extraction for the SEC 10-K dashboard.

The point of this module is that no metric is hard-coded. edgartools reads each
filing's presentation linkbase and maps every tagged line onto a
``standard_concept``; we keep whatever the filer actually reported. Charts and
reports are then driven by that data, so a new company needs no per-filer tag
work.

Three extractors, each returning long-form rows (one row per line/fact):

- ``extract_statements`` — the five primary statements, dimension-free.
- ``extract_facts`` — every fact for the filing's period, dimensions included.
- ``extract_note_schedules`` — the raw facts sliced into key schedules
  (debt, segments, EPS, taxes, share counts) for report/chart use.
"""

from __future__ import annotations

import re

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

# Note schedules we surface as their own tables/charts. Matched against the
# concept name and the dimension, so they adapt to whatever a filer tags.
NOTE_SCHEDULES: dict[str, str] = {
    "debt": r"Debt|Borrow|CommercialPaper|NotesPayable|LineOfCredit|Lease",
    "segments": r"Segment|StatementBusinessSegmentsAxis|StatementGeographicalAxis",
    "eps": r"EarningsPerShare",
    "taxes": r"IncomeTax|EffectiveIncomeTaxRate|DeferredTax|UnrecognizedTaxBenefits",
    "shares": r"CommonStockShares|WeightedAverageNumberOfShares|StockRepurchase|TreasuryStock",
}

_FACT_COLUMNS = [
    "concept", "label", "numeric_value", "unit_ref", "decimals",
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
        if isinstance(column, str) and column.startswith(period_of_report):
            return column
    return None


def _clean(value, default=None):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return value


def extract_statements(xbrl, period_of_report: str) -> list[dict]:
    """Every dimension-free line of the five primary statements, long-form."""
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

        section = None
        for _, row in data.iterrows():
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
            if pd.isna(value):
                continue
            standard = row.get("standard_concept")
            parent = row.get("parent_concept")
            rows.append({
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
    return rows


def extract_facts(xbrl, period_of_report: str) -> list[dict]:
    """Every fact whose period ends (or instant) at the report date.

    Keeps dimensional facts too, so segment/note detail is retained.
    """
    try:
        facts = xbrl.facts.to_dataframe()
    except Exception:  # noqa: BLE001
        return []
    if facts is None or facts.empty:
        return []

    target = pd.Timestamp(period_of_report)
    end = pd.to_datetime(facts["period_end"], errors="coerce")
    instant = pd.to_datetime(facts.get("period_instant"), errors="coerce") \
        if "period_instant" in facts.columns else pd.Series(pd.NaT, index=facts.index)
    subset = facts[end.eq(target) | instant.eq(target)]
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


def extract_note_schedules(facts: list[dict]) -> list[dict]:
    """Tag the fact rows with the key schedule they belong to."""
    rows: list[dict] = []
    for fact in facts:
        haystack = f"{fact['concept']} {fact.get('dimension') or ''} {fact.get('member') or ''}"
        for schedule, pattern in NOTE_SCHEDULES.items():
            if re.search(pattern, haystack, re.IGNORECASE):
                rows.append({**fact, "schedule": schedule})
                break
    return rows


def frames(statement_rows, fact_rows, note_rows) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Turn the row lists into DataFrames (empty-safe)."""
    return (
        pd.DataFrame(statement_rows),
        pd.DataFrame(fact_rows),
        pd.DataFrame(note_rows),
    )
