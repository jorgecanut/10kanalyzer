"""A dumped filing (``dump.py``) that reads like the live edgartools objects.

Stage 2 (``extract.py``, ``xbrl_extract.py``, ``calc_check.py``) was written
against edgartools: ``filing.xbrl()``, ``xbrl.facts.to_dataframe()``,
``xbrl.statements.income_statement().to_dataframe()``,
``xbrl.calculation_trees``. ``FilingView`` offers exactly those, served from
the dump, so the same code runs on a filing downloaded years ago with no
SEC request and no edgartools parse.
"""

from __future__ import annotations

from types import SimpleNamespace

import dump


class _Frame:
    """Anything with ``to_dataframe()``: a fresh copy each call, like edgartools."""

    def __init__(self, encoded: dict):
        self._encoded = encoded

    def to_dataframe(self):
        return dump.decode_frame(self._encoded)


class _Statements:
    def __init__(self, stored: dict):
        self._stored = stored or {}
        for key, method in dump.STATEMENTS.items():
            setattr(self, method, self._reader(key))

    def _reader(self, key: str):
        def read():
            entry = self._stored.get(key)
            if isinstance(entry, dict) and "error" in entry:
                raise RuntimeError(entry["error"])       # edgartools failed on it when dumped
            return None if entry is None else _Frame(entry)
        return read


class XbrlView:
    """The parts of edgartools' ``XBRL`` object that stage 2 uses."""

    def __init__(self, entry: dict):
        self._entry = entry
        self.facts = _Frame(entry["facts"])
        self.statements = _Statements(entry.get("statements"))
        self.calculation_trees = {
            role: SimpleNamespace(
                role_uri=role,
                definition=tree.get("definition"),
                all_arcs=[SimpleNamespace(parent_id=parent, child_id=child, weight=weight, order=order)
                          for parent, child, weight, order in tree.get("arcs", [])],
            )
            for role, tree in (entry.get("calculation") or {}).items()
        }
        self.period_of_report = (entry.get("filing") or {}).get("period_of_report")
        self.entity_info = entry.get("entity") or {}


class FilingView:
    """A filing read from its dump: ``form``, ``accession_no``, ``report_date`` and ``xbrl()``."""

    def __init__(self, entry: dict):
        self.entry = entry
        meta = entry.get("filing") or {}
        self.form = meta.get("form")
        self.accession_no = meta.get("accession")
        self.report_date = meta.get("report_date")
        self.filing_date = meta.get("filed")
        self._xbrl = XbrlView(entry) if entry.get("xbrl") else None

    def xbrl(self):
        return self._xbrl
