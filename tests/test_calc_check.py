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

    def test_hints_name_the_simple_fix(self):
        income = "http://example.com/role/IncomeTaxDetails"
        duration = {"instant": None, "start": "2025-01-01", "end": PERIOD}
        # Oracle FY2024: current tax tagged negative; the provision is current + deferred.
        rows = self.check({income: tree(("x_Provision", "x_Current", 1), ("x_Provision", "x_Deferred", 1))},
                          [fact("x:Provision", 1274e6, **duration), fact("x:Current", -3413e6, **duration),
                           fact("x:Deferred", -2139e6, **duration)])
        self.assertIn("“Current” had the opposite sign", rows[0]["hint"])
        rows = self.check({income: tree(("x_Deferred", "x_Federal", 1), ("x_Deferred", "x_State", 1))},
                          [fact("x:Deferred", -2300e6, **duration), fact("x:Federal", 2020e6, **duration),
                           fact("x:State", 280e6, **duration)])
        self.assertIn("opposite sign of its parts' sum", rows[0]["hint"])

    def test_a_line_the_linkbase_left_out_is_named(self):
        # AAPI FY2012: the cash change sums operating and financing, not investing.
        duration = {"instant": None, "start": "2025-01-01", "end": PERIOD}
        role = "http://example.com/role/CashFlows"
        rows = self.check({role: tree(("x_Change", "x_Operating", 1), ("x_Change", "x_Financing", 1))},
                          [fact("x:Change", 5e6, **duration), fact("x:Operating", -30e6, **duration),
                           fact("x:Financing", 205e6, **duration), fact("x:Investing", -170e6, **duration)])
        self.assertEqual(rows[0]["status"], "mismatch")
        self.assertIn("gap equals", rows[0]["hint"])
        self.assertIn("Investing", rows[0]["hint"])

    def test_a_total_listed_beside_its_own_parts_is_explained(self):
        # Oracle FY2020: operating income and its own revenue and costs under income before taxes.
        duration = {"instant": None, "start": "2025-01-01", "end": PERIOD}
        role = "http://example.com/role/StatementOfOperations"
        rows = self.check({role: tree(("x_Pretax", "x_Operating", 1), ("x_Pretax", "x_Revenue", 1),
                                      ("x_Pretax", "x_Costs", -1), ("x_Operating", "x_Revenue", 1),
                                      ("x_Operating", "x_Costs", -1))},
                          [fact("x:Pretax", 40e6, **duration), fact("x:Operating", 40e6, **duration),
                           fact("x:Revenue", 100e6, **duration), fact("x:Costs", 55e6, **duration)])
        pretax = next(r for r in rows if r["concept"] == "x_Pretax")
        self.assertEqual(pretax["status"], "mismatch")
        self.assertIn("count twice", pretax["hint"])


if __name__ == "__main__":
    unittest.main()
