"""Cost of revenue from an income statement's lines. No network: rows are built inline.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402
import xbrl_extract  # noqa: E402


def line(concept, label, value, parent="us-gaap_CostsAndExpenses", standard=None):
    return {"statement": "income", "concept": concept, "label": label, "value": value,
            "parent_concept": parent, "standard_concept": standard}


REVENUE = "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"

# Oracle FY2026: costs by revenue stream, no cost-of-revenue total.
ORACLE = [
    line("orcl_CloudRevenues", "Cloud", 33989, REVENUE),
    line("orcl_SoftwareRevenues", "Software", 24541, REVENUE),
    line("orcl_HardwareRevenues", "Hardware", 3084, REVENUE),
    line("orcl_SalesRevenueServicesNet1", "Services", 5743, REVENUE),
    line(REVENUE, "Total revenues", 67357, "us-gaap_OperatingIncomeLoss", "Revenue"),
    line("orcl_CloudAndSoftwareExpenses", "Cloud and software", 17597),
    line("orcl_HardwareExpenses", "Hardware", 868),
    line("orcl_ServicesExpense", "Services", 4556),
    line("us-gaap_SellingAndMarketingExpense", "Sales and marketing", 8331),
    line("us-gaap_ResearchAndDevelopmentExpense", "Research and development", 10272),
    line("us-gaap_GeneralAndAdministrativeExpense", "General and administrative", 1618),
    line("orcl_RestructuringAndOtherExpenses", "Restructuring and other", 1838),
    line("us-gaap_CostsAndExpenses", "Total operating expenses", 46751, "us-gaap_OperatingIncomeLoss"),
]


class CostOfRevenueTests(unittest.TestCase):
    def test_costs_named_after_revenue_streams_add_up(self):
        lines = extract._cost_lines(ORACLE)
        self.assertEqual(sorted(lines), ["orcl_CloudAndSoftwareExpenses", "orcl_HardwareExpenses",
                                         "orcl_ServicesExpense"])
        row = {"CostOfRevenue": np.nan, "GrossProfit": np.nan}
        extract._cost_of_revenue(row, ORACLE)
        self.assertEqual(row["CostOfRevenue"], 17597 + 868 + 4556)

    def test_a_single_line_taken_for_the_total_is_replaced(self):
        # Oracle FY2010 tags its hardware cost alone with CostOfRevenue; a
        # GrossProfit fact from a segment note is not the income statement's.
        rows = [line("us-gaap_Revenues", "Total revenues", 26820, "us-gaap_OperatingIncomeLoss", "Revenue"),
                line("us-gaap_LicenseCosts", "Cost of Revenue", 1063, standard="CostOfGoodsAndServicesSold"),
                line("us-gaap_CostOfRevenue", "Cost of Revenue", 880, standard="CostOfGoodsAndServicesSold"),
                line("us-gaap_CostOfServices", "Cost of Revenue", 423, standard="CostOfGoodsAndServicesSold")]
        row = {"CostOfRevenue": 880.0, "GrossProfit": 25940.0}
        extract._cost_of_revenue(row, rows)
        self.assertEqual(row["CostOfRevenue"], 1063 + 880 + 423)
        self.assertTrue(np.isnan(row["GrossProfit"]))           # derived later as revenue - cost

    def test_a_reported_total_is_kept(self):
        # Apple: products and services costs under "Total cost of sales".
        total = "us-gaap_CostOfGoodsAndServicesSold"
        rows = [line("us-gaap_Revenues", "Total net sales", 416161, "us-gaap_GrossProfit", "Revenue"),
                line(total, "Total cost of sales", 220960, "us-gaap_GrossProfit", "CostOfGoodsAndServicesSold"),
                line("aapl_CostOfProducts", "Products", 180000, total, "CostOfGoodsAndServicesSold"),
                line("aapl_CostOfServices", "Services", 40960, total, "CostOfGoodsAndServicesSold"),
                line("us-gaap_GrossProfit", "Gross margin", 195201, "us-gaap_OperatingIncomeLoss")]
        row = {"CostOfRevenue": 220960.0, "GrossProfit": 195201.0}
        extract._cost_of_revenue(row, rows)
        self.assertEqual(row, {"CostOfRevenue": 220960.0, "GrossProfit": 195201.0})

    def test_a_bank_interest_expense_is_not_a_cost_of_revenue(self):
        rows = [line("us-gaap_InterestIncome", "Interest income", 193933, "us-gaap_Revenues", "Revenue"),
                line("us-gaap_InterestExpense", "Interest expense", 101350, "us-gaap_InterestIncomeExpenseNet")]
        self.assertEqual(extract._cost_lines(rows), {})

    def test_a_bank_salary_filed_as_cost_of_goods_is_not_one(self):
        # JPM: edgartools files "Compensation expense" as CostOfGoodsAndServicesSold.
        rows = [line("us-gaap_Revenues", "Total net revenue", 182447, "us-gaap_NetIncomeLoss", "Revenue"),
                line("us-gaap_LaborAndRelatedExpense", "Compensation expense", 54487,
                     "us-gaap_NoninterestExpense", "CostOfGoodsAndServicesSold")]
        self.assertEqual(extract._cost_lines(rows), {})


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# A 10-Q read at a chosen span (quarters since the last 10-K).
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402


def qfact(concept, value, start, end=None, instant=None, decimals=-6):
    return {"concept": concept, "numeric_value": value, "value": value, "currency": "USD", "unit_ref": "usd",
            "is_dimensioned": False, "dimension": None, "member": None, "period_instant": instant,
            "period_start": None if instant else start, "period_end": None if instant else end,
            "decimals": decimals, "label": concept.split(":")[-1]}


class _Frame:
    def __init__(self, frame):
        self.frame = frame

    def to_dataframe(self):
        return self.frame.copy()


def _quarter_filing(facts, income=None):
    """A 10-Q-shaped filing: facts and, optionally, an income statement frame."""
    statements = SimpleNamespace(
        income_statement=lambda: _Frame(income) if income is not None else None,
        balance_sheet=lambda: None, cash_flow_statement=lambda: None,
        statement_of_equity=lambda: None, comprehensive_income=lambda: None)
    xbrl = SimpleNamespace(facts=_Frame(pd.DataFrame(facts)), statements=statements, calculation_trees={},
                           period_of_report="2026-08-01", entity_info={})
    return SimpleNamespace(form="10-Q", accession_no="q2", xbrl=lambda: xbrl)


Q2_FACTS = [
    qfact("us-gaap:Revenues", 25.0, "2026-05-03", "2026-08-01"),          # three months
    qfact("us-gaap:Revenues", 49.0, "2026-02-01", "2026-08-01"),          # year to date
    qfact("us-gaap:Revenues", 24.0, "2025-05-04", "2025-08-02"),
    qfact("us-gaap:Revenues", 47.0, "2025-02-02", "2025-08-02"),
    qfact("us-gaap:NetIncomeLoss", 2.0, "2026-05-03", "2026-08-01"),
    qfact("us-gaap:NetIncomeLoss", 3.5, "2026-02-01", "2026-08-01"),
    qfact("us-gaap:StockholdersEquity", 16.0, None, instant="2026-08-01"),
]


class ChosenSpanTests(unittest.TestCase):
    def test_extract_metric_respects_max_days(self):
        facts = pd.DataFrame(Q2_FACTS)
        ytd = extract.extract_metric(facts, ["Revenues"], "2026-08-01", "duration", min_days=160, max_days=200)
        self.assertEqual(ytd, 49.0)
        three = extract.extract_metric(facts, ["Revenues"], "2026-08-01", "duration", min_days=70, max_days=110)
        self.assertEqual(three, 25.0)
        prior = extract.extract_metric(facts, ["Revenues"], "2025-08-02", "duration", min_days=160, max_days=200)
        self.assertEqual(prior, 47.0)

    def test_extract_filing_at_a_span_and_its_sources(self):
        out = extract.extract_filing(_quarter_filing(Q2_FACTS), "2026-08-01", min_days=160, max_days=200)
        row = out["row"]
        self.assertEqual(row["Revenues"], 49.0)
        self.assertEqual(row["NetIncomeLoss"], 3.5)
        self.assertEqual(row["StockholdersEquity"], 16.0)
        self.assertEqual(out["sources"]["Revenues"], "Revenues")
        self.assertEqual(out["sources"]["NetIncomeLoss"], "NetIncomeLoss")
        self.assertNotIn("CapEx", out["sources"])

    def test_10q_statement_takes_ytd_column(self):
        income = pd.DataFrame([{
            "concept": "us-gaap_OperatingIncomeLoss", "label": "Operating income", "level": 3, "abstract": False,
            "dimension": False, "standard_concept": "OperatingIncomeLoss", "parent_concept": None,
            "balance": "credit", "weight": None,
            "2026-08-01 (Q2)": 1.2, "2026-08-01 (YTD)": 2.6, "2025-08-02 (Q2)": 1.1, "2025-08-02 (YTD)": 2.4}])
        self.assertEqual(xbrl_extract._period_column(income, "2026-08-01", span_days=181), "2026-08-01 (YTD)")
        self.assertEqual(xbrl_extract._period_column(income, "2026-08-01", span_days=91), "2026-08-01 (Q2)")
        self.assertEqual(xbrl_extract._period_column(income, "2025-08-02", span_days=181), "2025-08-02 (YTD)")
        # Q1: its year to date is three months, and edgartools labels it (Q1).
        q1 = income.rename(columns={"2026-08-01 (Q2)": "2026-05-02 (Q1)"})
        self.assertEqual(xbrl_extract._period_column(q1, "2026-05-02", span_days=91), "2026-05-02 (Q1)")
        # An annual call is unchanged: the first column for the date.
        self.assertEqual(xbrl_extract._period_column(income, "2026-08-01"), "2026-08-01 (Q2)")

    def test_statement_line_source_is_named(self):
        income = pd.DataFrame([{
            "concept": "us-gaap_OperatingIncomeLoss", "label": "Operating income", "level": 3, "abstract": False,
            "dimension": False, "standard_concept": "OperatingIncomeLoss", "parent_concept": None,
            "balance": "credit", "weight": None, "2026-08-01 (Q2)": 1.2, "2026-08-01 (YTD)": 2.6}])
        # No OperatingIncomeLoss fact: the YTD statement line is taken, and said so.
        out = extract.extract_filing(_quarter_filing(Q2_FACTS, income), "2026-08-01", min_days=160, max_days=200)
        self.assertEqual(out["row"]["OperatingIncomeLoss"], 2.6)
        self.assertTrue(out["sources"]["OperatingIncomeLoss"].startswith("statement:"))
