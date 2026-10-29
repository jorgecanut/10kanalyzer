"""Cost of revenue from an income statement's lines. No network: rows are built inline.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402


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
