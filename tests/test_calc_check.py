"""Calculation-linkbase check. No network: the linkbase and facts are built inline.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calc_check  # noqa: E402

PERIOD = "2025-12-31"


def fact(concept, value, *, decimals=-6, instant=PERIOD, start=None, end=None, currency="EUR", dimensioned=False):
    return {"concept": concept, "numeric_value": value, "decimals": decimals, "label": concept.split(":")[-1],
            "period_instant": instant, "period_start": start, "period_end": end, "currency": currency,
            "is_dimensioned": dimensioned}


def tree(*arcs):
    """A calculation tree from (parent, child, weight) triples, as edgartools shapes it."""
    return SimpleNamespace(definition=None, all_arcs=[
        SimpleNamespace(parent_id=parent, child_id=child, weight=weight) for parent, child, weight in arcs])


def filing(trees):
    return SimpleNamespace(calculation_trees=trees)


BALANCE = "http://example.com/role/ConsolidatedBalanceSheet"


class CheckTests(unittest.TestCase):
    def check(self, trees, facts, currency="EUR"):
        return calc_check.check_filing(filing(trees), pd.DataFrame(facts), PERIOD, currency)

    def test_a_total_that_adds_up_within_rounding(self):
        rows = self.check({BALANCE: tree(("ifrs-full_Assets", "ifrs-full_CurrentAssets", 1),
                                         ("ifrs-full_Assets", "ifrs-full_NoncurrentAssets", 1))},
                          [fact("ifrs-full:Assets", 300e6), fact("ifrs-full:CurrentAssets", 100.4e6),
                           fact("ifrs-full:NoncurrentAssets", 199.9e6)])
        self.assertEqual([(r["status"], r["kind"]) for r in rows], [("ok", "statement")])

    def test_a_total_that_does_not_add_up_names_its_parts(self):
        rows = self.check({BALANCE: tree(("us-gaap_Liabilities", "us-gaap_LiabilitiesCurrent", 1),
                                         ("us-gaap_Liabilities", "us-gaap_LongTermDebt", 1))},
                          [fact("us-gaap:Liabilities", 500e6), fact("us-gaap:LiabilitiesCurrent", 200e6),
                           fact("us-gaap:LongTermDebt", 250e6)])
        self.assertEqual(rows[0]["status"], "mismatch")
        self.assertEqual(rows[0]["difference"], 50e6)

    def test_weights_subtract(self):
        income = "http://example.com/role/IncomeStatement"
        rows = self.check({income: tree(("ifrs-full_GrossProfit", "ifrs-full_Revenue", 1),
                                        ("ifrs-full_GrossProfit", "ifrs-full_CostOfSales", -1))},
                          [fact("ifrs-full:GrossProfit", 40e6, instant=None, start="2025-01-01", end=PERIOD),
                           fact("ifrs-full:Revenue", 100e6, instant=None, start="2025-01-01", end=PERIOD),
                           fact("ifrs-full:CostOfSales", 60e6, instant=None, start="2025-01-01", end=PERIOD)])
        self.assertEqual(rows[0]["status"], "ok")

    def test_parts_without_a_plain_value_make_it_incomplete(self):
        rows = self.check({BALANCE: tree(("x_Total", "x_A", 1), ("x_Total", "x_B", 1))},
                          [fact("x:Total", 10e6), fact("x:A", 4e6),
                           fact("x:B", 6e6, dimensioned=True)])       # B only per segment
        self.assertEqual(rows[0]["status"], "incomplete")
        self.assertEqual(rows[0]["missing"], "B")

    def test_parts_from_another_statement_complete_the_sum(self):
        # TSMC: profit in the income statement role, OCI in the comprehensive one.
        income = "http://example.com/role/IncomeStatement"
        other = "http://example.com/role/ComprehensiveIncome"
        duration = {"instant": None, "start": "2025-01-01", "end": PERIOD}
        rows = self.check({income: tree(("x_Comprehensive", "x_Profit", 1)),
                           other: tree(("x_Comprehensive", "x_OtherComprehensive", 1))},
                          [fact("x:Comprehensive", 90e6, **duration), fact("x:Profit", 100e6, **duration),
                           fact("x:OtherComprehensive", -10e6, **duration)])
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"])
        self.assertIn("several statements", rows[0]["statement"])

    def test_convenience_translations_and_other_periods_are_ignored(self):
        rows = self.check({BALANCE: tree(("x_Total", "x_A", 1), ("x_Total", "x_B", 1))},
                          [fact("x:Total", 10e6), fact("x:A", 4e6), fact("x:B", 6e6),
                           fact("x:Total", 0.3e6, currency="USD"),          # USD copy of the year
                           fact("x:A", 99e6, instant="2024-12-31")])        # last year's comparative
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["reported"], 10e6)

    def test_note_roles_are_told_from_statements(self):
        note = "http://example.com/role/DebtDetails"
        rows = self.check({note: tree(("x_Debt", "x_Bonds", 1))}, [fact("x:Debt", 5e6), fact("x:Bonds", 5e6)])
        self.assertEqual(rows[0]["kind"], "note")

    def test_summary_counts_by_kind(self):
        rows = [{"kind": "statement", "status": "ok", "period_of_report": PERIOD},
                {"kind": "statement", "status": "mismatch", "period_of_report": PERIOD},
                {"kind": "note", "status": "incomplete", "period_of_report": "2024-12-31"}]
        counts = calc_check.summary(rows)
        self.assertEqual(counts["filings"], 2)
        self.assertEqual(counts["statement"], {"totals": 2, "ok": 1, "mismatch": 1, "incomplete": 0})
        self.assertEqual(counts["note"]["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
