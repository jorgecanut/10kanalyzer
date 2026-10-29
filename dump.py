"""Stage 1 of the pipeline: copy what one filing says, in Stocky's own format.

``dump_filing`` reads one 10-K, 10-KT, 20-F, 40-F or 10-Q and returns plain
data (schema ``SCHEMA``) that the filing cache keeps for good:

- ``filing``: accession, CIK, form, dates, the filer's fiscal year and period.
- ``facts``: every fact of every period, dimensions included, as columns
  (``encode_frame``); text facts longer than ``TEXT_LIMIT`` characters (notes,
  narratives) lose their text, cover-page (``dei:``) text is always kept.
- ``presentation`` and ``calculation``: the linkbase trees, per role.
- ``labels``: every label of every concept the presentation names.
- ``statements``: edgartools' rendering of the five primary statements.
- ``entity``: edgartools' entity information.

It makes no choice: which tag is revenue, which year is the filing's own,
whether a total adds up are stage 2 (``extract.py``, ``calc_check.py``, run
on ``filing_view.FilingView``), so fixing them never needs SEC.

This file is the reader: its hash and the edgartools release are stamped on
every entry (``filing_cache.reader``). Changing it must be classified in
``readers.json`` (see ``filing_cache.py``); a change to what is stored bumps
``SCHEMA``. It imports no other local module, so nothing outside it can
change a dump without changing the stamp.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

SCHEMA = 1
TEXT_LIMIT = 500
# statement key -> edgartools statement method (the five primary statements).
STATEMENTS = {
    "income": "income_statement",
    "balance": "balance_sheet",
    "cashflow": "cash_flow_statement",
    "equity": "statement_of_equity",
    "comprehensive": "comprehensive_income",
}


# --------------------------------------------------------------------------- #
# DataFrames as plain columns
# --------------------------------------------------------------------------- #

def _kind(series: pd.Series) -> str:
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_integer_dtype(dtype):
        return "int"
    if pd.api.types.is_float_dtype(dtype):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if dtype != object and pd.api.types.is_string_dtype(dtype):
        return "str"
    return "object"


def _cell(value):
    """A JSON-safe cell: NaN and NA become None, numpy scalars plain Python."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_cell(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _cell(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return [_cell(item) for item in value.tolist()]
    item = getattr(value, "item", None)
    if callable(item):
        value = item()
        return None if isinstance(value, float) and math.isnan(value) else value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def encode_frame(frame: pd.DataFrame) -> dict:
    """``{"columns", "types", "data"}``: one list per column, rebuilt by ``decode_frame``."""
    columns = [str(column) for column in frame.columns]
    types = [_kind(frame[column]) for column in frame.columns]
    data = []
    for column, kind in zip(frame.columns, types):
        values = frame[column]
        if kind == "datetime":
            values = values.dt.strftime("%Y-%m-%dT%H:%M:%S")
        data.append([_cell(value) for value in values.tolist()])
    return {"columns": columns, "types": types, "data": data}


def decode_frame(encoded: dict) -> pd.DataFrame:
    """The DataFrame ``encode_frame`` stored, with the same column types."""
    series = {}
    for column, kind, values in zip(encoded["columns"], encoded["types"], encoded["data"]):
        if kind == "float":
            series[column] = pd.Series([np.nan if value is None else value for value in values], dtype="float64")
        elif kind in ("int", "bool"):
            series[column] = pd.Series(values, dtype="int64" if kind == "int" else "bool")
        elif kind == "datetime":
            series[column] = pd.to_datetime(pd.Series(values, dtype="object"), errors="coerce")
        elif kind == "str":
            series[column] = pd.Series(values, dtype="str")
        else:
            series[column] = pd.Series(values, dtype="object")
    frame = pd.DataFrame(series, columns=encoded["columns"])
    if not encoded["columns"]:
        frame = pd.DataFrame()
    return frame


# --------------------------------------------------------------------------- #
# The parts of a filing
# --------------------------------------------------------------------------- #

def _facts(xbrl) -> tuple[dict, int]:
    facts = xbrl.facts.to_dataframe()
    if facts is None:
        facts = pd.DataFrame()
    dropped = 0
    if not facts.empty and "value" in facts.columns:
        text = facts["value"].astype("str")
        numeric = pd.to_numeric(facts.get("numeric_value"), errors="coerce") if "numeric_value" in facts.columns \
            else pd.Series(np.nan, index=facts.index)
        cover = facts["concept"].astype("str").str.startswith("dei:") if "concept" in facts.columns \
            else pd.Series(False, index=facts.index)
        long_text = numeric.isna() & facts["value"].notna() & (text.str.len() > TEXT_LIMIT) & ~cover
        dropped = int(long_text.sum())
        if dropped:
            facts = facts.copy()
            facts.loc[long_text, "value"] = None
    return encode_frame(facts), dropped


def _presentation(xbrl) -> tuple[dict, dict]:
    """Role -> tree, and concept -> {label role: label} from every node."""
    roles: dict = {}
    labels: dict[str, dict] = {}
    for role, tree in (xbrl.presentation_trees or {}).items():
        nodes = {}
        for element, node in (getattr(tree, "all_nodes", None) or {}).items():
            nodes[element] = {
                "parent": getattr(node, "parent", None),
                "children": list(getattr(node, "children", None) or []),
                "depth": getattr(node, "depth", None),
                "order": _cell(getattr(node, "order", None)),
                "preferred_label": getattr(node, "preferred_label", None),
                "abstract": bool(getattr(node, "is_abstract", False)),
            }
            for label_role, text in (getattr(node, "labels", None) or {}).items():
                labels.setdefault(element, {})[label_role] = text
        roles[role] = {"definition": getattr(tree, "definition", None),
                       "roots": list(getattr(tree, "root_element_ids", None) or []),
                       "nodes": nodes}
    return roles, labels


def _arcs(tree) -> list[list]:
    arcs = getattr(tree, "all_arcs", None)
    if arcs:
        return [[arc.parent_id, arc.child_id, float(arc.weight), _cell(getattr(arc, "order", None))]
                for arc in arcs]
    out = []
    for parent_id, node in (getattr(tree, "all_nodes", None) or {}).items():
        for child_id in getattr(node, "children", None) or []:
            child = tree.all_nodes.get(child_id)
            out.append([parent_id, child_id, float(getattr(child, "weight", 1.0) or 1.0), None])
    return out


def _calculation(xbrl) -> dict:
    return {role: {"definition": getattr(tree, "definition", None), "arcs": _arcs(tree)}
            for role, tree in (xbrl.calculation_trees or {}).items()}


def _statements(xbrl) -> dict:
    """Key -> encoded rendering, ``None`` when the filing has no such statement,
    or ``{"error": ...}`` when edgartools failed on it (replayed by the view)."""
    out: dict = {}
    for key, method in STATEMENTS.items():
        try:
            statement = getattr(xbrl.statements, method)()
            data = statement.to_dataframe() if statement is not None else None
        except Exception as exc:  # noqa: BLE001 - recorded, so stage 2 sees the same failure
            out[key] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out[key] = None if data is None else encode_frame(data)
    return out


def _part(name: str, read, errors: dict):
    """One part of the dump; a failure is recorded and never sinks the filing."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001
        errors[name] = f"{type(exc).__name__}: {exc}"
        return None


def filing_meta(filing) -> dict:
    """What the submissions index says about a filing (no download)."""
    def text(value):
        return None if value is None or value == "" else str(value)

    return {
        "accession": text(getattr(filing, "accession_no", None)),
        "cik": int(getattr(filing, "cik", 0) or 0) or None,
        "company": text(getattr(filing, "company", None)),
        "form": text(getattr(filing, "form", None)),
        "filed": text(getattr(filing, "filing_date", None)),
        "report_date": text(getattr(filing, "report_date", None)),
    }


def _dei(facts: dict) -> dict:
    """The cover-page facts the filing is identified by."""
    wanted = {"dei:DocumentType", "dei:DocumentFiscalYearFocus", "dei:DocumentFiscalPeriodFocus",
              "dei:DocumentPeriodEndDate", "dei:CurrentFiscalYearEndDate", "dei:EntityRegistrantName"}
    if not facts or "concept" not in facts["columns"] or "value" not in facts["columns"]:
        return {}
    concepts = facts["data"][facts["columns"].index("concept")]
    values = facts["data"][facts["columns"].index("value")]
    out: dict = {}
    for concept, value in zip(concepts, values):
        if concept in wanted and concept not in out and value is not None:
            out[concept] = value
    return out


def dump_filing(filing) -> dict:
    """Everything stage 2 needs from ``filing``; raises only when its XBRL cannot be read.

    ``xbrl`` is False for filings without XBRL (10-Ks before 2009-2011).
    """
    meta = filing_meta(filing)
    xbrl = filing.xbrl()
    if xbrl is None:
        return {"schema": SCHEMA, "xbrl": False, "filing": meta}
    errors: dict[str, str] = {}
    facts, dropped = _facts(xbrl)          # without facts there is nothing to store: may raise
    presentation, labels = _part("presentation", lambda: _presentation(xbrl), errors) or ({}, {})
    meta["period_of_report"] = _part("period_of_report",
                                     lambda: _cell(getattr(xbrl, "period_of_report", None)), errors)
    return {
        "schema": SCHEMA,
        "xbrl": True,
        "filing": meta,
        "dei": _dei(facts),
        "facts": facts,
        "dropped_text": dropped,
        "presentation": presentation,
        "calculation": _part("calculation", lambda: _calculation(xbrl), errors) or {},
        "labels": labels,
        "statements": _part("statements", lambda: _statements(xbrl), errors) or {},
        "entity": _part("entity", lambda: _cell(dict(xbrl.entity_info or {})), errors) or {},
        "errors": errors,
    }
