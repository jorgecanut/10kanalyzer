"""Calculation-linkbase check: does every total add up to its parts?

Each filing ships a calculation linkbase: for every total on its statements
(total assets, gross profit, operating cash flow, ...) it lists the lines that
make it up and their sign (+1 or -1). This module takes the filing's own
period values and checks each total against the weighted sum of its parts.

A total that does not add up points at a line the filer reported but tagged
oddly, a part that is only tagged in a note, or a filer error. Either way it
names the exact line to look at, which is what the pipeline needs to know to
read the right concept.

Rules, after the XBRL calculation spec:
- Only dimension-free facts of the filing's own period, in its reporting
  currency (some foreign filers add a USD convenience translation).
- A part with no value this period is left out of the sum (it is listed as
  missing); a total none of whose parts has a value is skipped.
- Rounding: each value counts as exact to half a unit of its ``decimals``
  (half a million for figures reported in millions), summed over the total
  and its parts.
- A total that fails within its own statement is tried again with its parts
  from every statement (TSMC puts profit and other comprehensive income
  under different roles); adding up that way counts as adding up.

Status: ``ok``; ``mismatch`` when every part has a value and the sum is
still off (worth a look; ``hint`` says why when one simple fix explains it:
a part or the total tagged with the wrong sign, a part that does not belong,
a part listed next to its own parts); ``incomplete`` when parts have no plain value this
year (usually tagged only per segment or tranche, so the sum cannot close).
``kind`` tells the primary statements from the notes.

The rows are kept in the filing cache and this file's hash is part of its
version (``filing_cache.KINDS``): code that only reads the rows (the counts in
the run log) lives in ``edgar_dashboard.py``.
"""

from __future__ import annotations

import json
import math
import re

import numpy as np
import pandas as pd

# A duration fact must span about a year to be the annual figure.
ANNUAL_MIN_DAYS = 300
# The same total and parts in several roles (statement and its parenthetical,
# income statement and comprehensive income) is checked once.
STATUS_OK, STATUS_MISMATCH, STATUS_INCOMPLETE = "ok", "mismatch", "incomplete"
# Role names of the notes; everything else is a primary statement.
NOTE_ROLE_RE = re.compile(r"detail|summary|disclosure|table|narrative|polic|note|schedule|reconciliation",
                          re.IGNORECASE)


def _element(concept: str) -> str:
    """``us-gaap:Assets`` (facts) and ``us-gaap_Assets`` (linkbase) -> ``us-gaap_Assets``."""
    return str(concept).replace(":", "_", 1)


def _words(element: str) -> str:
    """``us-gaap_AccountsReceivableNetCurrent`` -> ``Accounts Receivable Net Current``."""
    name = element.split("_", 1)[-1]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def _role_name(role: str, definition: str | None = None) -> str:
    """A readable statement name from the role URI or its definition."""
    text = definition or role.rstrip("/").rsplit("/", 1)[-1]
    text = re.sub(r"^\d+\s*-\s*\w+\s*-\s*", "", text)          # "100 - Statement - ..."
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return text.replace("_", " ").strip()


def period_values(facts: pd.DataFrame, period_of_report: str, currency: str | None) -> dict[str, dict]:
    """Element id -> {value, decimals, label} for the filing's own period.

    ``facts`` is edgartools' ``xbrl.facts.to_dataframe()``. Duration facts must
    end on the report date and span a year; instant facts must sit on it. When
    a concept appears twice (a rounded and a precise copy) the precise one wins.
    """
    if facts is None or facts.empty:
        return {}
    target = pd.Timestamp(period_of_report)
    frame = facts[~facts["is_dimensioned"].astype(bool)].copy()
    frame["_value"] = pd.to_numeric(frame["numeric_value"], errors="coerce")
    frame = frame.dropna(subset=["_value"])
    if currency and "currency" in frame.columns:
        own = frame["currency"].isna() | (frame["currency"].astype(str).str.upper() == currency)
        frame = frame[own]
    start = pd.to_datetime(frame.get("period_start"), errors="coerce")
    end = pd.to_datetime(frame.get("period_end"), errors="coerce")
    instant = pd.to_datetime(frame.get("period_instant"), errors="coerce")
    annual = end.eq(target) & ((end - start).dt.days >= ANNUAL_MIN_DAYS)
    frame = frame[annual | instant.eq(target)]
    decimals = frame["decimals"] if "decimals" in frame.columns else pd.Series(None, index=frame.index)
    frame["_decimals"] = pd.to_numeric(decimals, errors="coerce").astype(float)
    # INF (exact) sorts first, then the most decimals.
    frame.loc[decimals.astype(str).str.upper().eq("INF"), "_decimals"] = np.inf
    frame = frame.sort_values("_decimals", ascending=False, na_position="last")
    out: dict[str, dict] = {}
    for _, row in frame.iterrows():
        element = _element(row["concept"])
        if element in out:
            continue
        decimals = row["_decimals"]
        out[element] = {
            "value": float(row["_value"]),
            "decimals": None if pd.isna(decimals) else float(decimals),
            "label": str(row.get("label") or "") or _words(element),
        }
    return out


def convenience_values(facts: pd.DataFrame, period_of_report: str, currency: str | None) -> dict[str, list[float]]:
    """Element id -> the filing's own-period values in another currency than ``currency``.

    Foreign filers such as TSMC add a USD translation of the latest year; the
    rendered statement sometimes shows it in place of the reporting figure.
    """
    if not currency or facts is None or facts.empty or "currency" not in facts.columns:
        return {}
    other = facts[facts["currency"].notna() & (facts["currency"].astype(str).str.upper() != currency)]
    out: dict[str, list[float]] = {}
    for code in other["currency"].astype(str).str.upper().unique():
        for element, item in period_values(other, period_of_report, code).items():
            out.setdefault(element, []).append(item["value"])
    return out


def _half_unit(decimals: float | None) -> float:
    """The rounding a value can carry: half a unit of its last reported digit."""
    if decimals is None or math.isinf(decimals):
        return 0.0
    return 0.5 * 10 ** (-decimals)


def _arcs(tree) -> list[tuple[str, str, float]]:
    """(parent, child, weight) for every arc of one calculation tree."""
    arcs = getattr(tree, "all_arcs", None)
    if arcs:
        return [(arc.parent_id, arc.child_id, float(arc.weight)) for arc in arcs]
    out = []
    for parent_id, node in (getattr(tree, "all_nodes", None) or {}).items():
        for child_id in node.children or []:
            child = tree.all_nodes.get(child_id)
            out.append((parent_id, child_id, float(getattr(child, "weight", 1.0) or 1.0)))
    return out


def _descendants(element: str, children: dict[str, list[tuple[str, float]]]) -> set[str]:
    found: set[str] = set()
    stack = [child for child, _ in children.get(element, [])]
    while stack:
        item = stack.pop()
        if item not in found:
            found.add(item)
            stack.extend(child for child, _ in children.get(item, []))
    return found


def _hint(total: float, computed: float, tolerance: float, parts: list[tuple[str, float]],
          values: dict[str, dict], children: dict[str, list[tuple[str, float]]]) -> str:
    """Why a total that has every part does not add up, when one simple fix explains it.

    Oracle's filings show each case: a tax line tagged with the wrong sign, a
    total and some of its own parts listed side by side, so they count twice.
    """
    label = lambda element: values[element]["label"] if element in values else _words(element)
    present = [(child, weight) for child, weight in parts if child in values]
    if total and abs(total + computed) <= tolerance:
        return "The total has the opposite sign of its parts' sum: the filing tags one side with the wrong sign."
    for child, weight in present:
        if abs(total - (computed - 2 * weight * values[child]["value"])) <= tolerance:
            return f"Adds up if “{label(child)}” had the opposite sign: the filing tags it with the wrong sign."
    for child, weight in present:
        if abs(total - (computed - weight * values[child]["value"])) <= tolerance:
            return f"Adds up without “{label(child)}”: the linkbase counts it in this total by mistake."
    elements = [child for child, _ in parts]
    for child in elements:
        inside = [other for other in elements if other != child and other in _descendants(child, children)]
        if inside:
            names = ", ".join(f"“{label(other)}”" for other in inside[:3]) + (" and more" if len(inside) > 3 else "")
            return (f"“{label(child)}” and its own parts ({names}) are listed in the same total, so they count "
                    "twice: the linkbase mixes two ways of reaching it.")
    return ""


def _sum(parts: list[tuple[str, float]], values: dict[str, dict]) -> tuple[float, float, list[tuple[str, float]]]:
    """Weighted sum of the parts with a value, its rounding tolerance, and those parts."""
    present = [(child, weight) for child, weight in parts if child in values]
    computed = sum(weight * values[child]["value"] for child, weight in present)
    tolerance = sum(_half_unit(values[child]["decimals"]) for child, _ in present)
    return computed, tolerance, present


def check_filing(xbrl, facts: pd.DataFrame, period_of_report: str, currency: str | None = None) -> list[dict]:
    """One row per total in the filing's calculation linkbase that has parts with values."""
    values = period_values(facts, period_of_report, currency)
    try:
        trees = xbrl.calculation_trees or {}
    except Exception:  # noqa: BLE001 - a filing without a usable linkbase has nothing to check
        return []
    by_role: dict[str, tuple[str, dict[str, list[tuple[str, float]]]]] = {}
    every_part: dict[str, dict[str, float]] = {}
    for role, tree in trees.items():
        children: dict[str, list[tuple[str, float]]] = {}
        for parent, child, weight in _arcs(tree):
            children.setdefault(parent, []).append((child, weight))
            every_part.setdefault(parent, {}).setdefault(child, weight)
        by_role[role] = (_role_name(role, getattr(tree, "definition", None)), children)

    every_children = {parent: list(parts.items()) for parent, parts in every_part.items()}
    rows: list[dict] = []
    seen: set[tuple] = set()
    for role, (statement, children) in by_role.items():
        kind = "note" if NOTE_ROLE_RE.search(statement) else "statement"
        for parent, parts in children.items():
            key = (parent, tuple(sorted(parts)))
            if key in seen or parent not in values:
                continue
            seen.add(key)
            computed, tolerance, present = _sum(parts, values)
            if not present:
                continue
            total = values[parent]
            tolerance += _half_unit(total["decimals"])
            status = STATUS_OK if abs(total["value"] - computed) <= tolerance else STATUS_MISMATCH
            where = statement
            across = sorted(every_part.get(parent, {}).items())
            if status == STATUS_MISMATCH and across != sorted(parts):
                other, other_tolerance, other_present = _sum(across, values)
                if abs(total["value"] - other) <= other_tolerance + _half_unit(total["decimals"]):
                    parts, computed, tolerance, present = across, other, other_tolerance, other_present
                    status = STATUS_OK
                    where = f"{statement} (parts from several statements)"
            listing = [{"concept": child, "label": values[child]["label"] if child in values else _words(child),
                        "weight": weight, "value": values[child]["value"] if child in values else None}
                       for child, weight in parts]
            missing = [item["label"] for item in listing if item["value"] is None]
            if status == STATUS_MISMATCH and missing:
                status = STATUS_INCOMPLETE
            # A part's own parts may be listed in another statement (the income
            # statement links operating income to its revenue and costs).
            hint = _hint(total["value"], computed, tolerance, parts, values, every_children) \
                if status == STATUS_MISMATCH else ""
            rows.append({
                "period_of_report": period_of_report,
                "kind": kind,
                "statement": where,
                "concept": parent,
                "label": total["label"],
                "reported": total["value"],
                "computed": computed,
                "difference": total["value"] - computed,
                "tolerance": tolerance,
                "parts": len(parts),
                "missing": "; ".join(missing),
                "status": status,
                "hint": hint,
                "detail": json.dumps(listing, ensure_ascii=False),
            })
    return rows
