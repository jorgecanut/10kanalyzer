"""Series helpers, chart specs and PNG windows in charts.py. No network.

Run from the repo root:  python -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import charts  # noqa: E402

# The order charts.py uses: balance-sheet date, cover page, weighted average.
SHARE_CONCEPTS = ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding",
                  "WeightedAverageNumberOfDilutedSharesOutstanding"]


def fact(concept, value, year=2025, dimension=None, member=None):
    return {"concept": concept, "value": value, "fiscal_year": year,
            "is_dimensioned": dimension is not None, "dimension": dimension, "member": member}


class SharesTests(unittest.TestCase):
    def shares(self, rows):
        return charts._shares_series(pd.DataFrame(rows), SHARE_CONCEPTS, [2025])[2025]

    def test_alternative_concepts_are_not_added_together(self):
        # Balance-sheet count and diluted weighted average are two answers to
        # the same question; adding them doubled the share count.
        rows = [fact("us-gaap:CommonStockSharesOutstanding", 229e6),
                fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 233.5e6),
                fact("dei:EntityCommonStockSharesOutstanding", 230.08e6)]
        self.assertEqual(self.shares(rows), 229.0)

    def test_falls_back_in_order(self):
        rows = [fact("dei:EntityCommonStockSharesOutstanding", 230e6),
                fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 233e6)]
        self.assertEqual(self.shares(rows), 230.0)
        rows = [fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 233e6)]
        self.assertEqual(self.shares(rows), 233.0)

    def test_share_classes_of_one_concept_are_added(self):
        axis = "us-gaap:StatementClassOfStockAxis"
        rows = [fact("dei:EntityCommonStockSharesOutstanding", 300e6, dimension=axis, member="ClassA"),
                fact("dei:EntityCommonStockSharesOutstanding", 300e6, dimension=axis, member="ClassA"),
                fact("dei:EntityCommonStockSharesOutstanding", 50e6, dimension=axis, member="ClassB"),
                fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 999e6)]
        self.assertEqual(self.shares(rows), 350.0)


def ratios(years):
    return pd.DataFrame({
        "fiscal_year": years,
        "Revenues": [100.0 + i for i in range(len(years))],
        "NetIncomeLoss": [10.0] * len(years),
        "StockholdersEquity": [50.0] * len(years),
    })


class SpecTests(unittest.TestCase):
    def build(self, years, output_dir="/nonexistent"):
        return charts.build_spec("TST", output_dir, ratios(years), pd.DataFrame(), pd.DataFrame())

    def test_every_chart_is_plain_json(self):
        spec = self.build(list(range(2010, 2026)))
        self.assertEqual([c["id"] for c in spec["charts"]], [
            "01_income_statement", "02_margins", "03_balance", "04_debt",
            "05_cashflow_operations", "06_cashflow_financing", "07_returns"])
        json.dumps(spec, allow_nan=False)        # no NaN or numpy values
        income = spec["charts"][0]["panels"][0]
        self.assertEqual(income["years"], list(range(2010, 2026)))
        # Metrics without a single value are left out, not drawn empty.
        self.assertEqual([s["label"] for s in income["series"]], ["Revenue", "Net Income"])
        self.assertEqual(income["series"][0]["concepts"], ["Revenues"])

    def test_missing_years_are_null(self):
        df = ratios([2024, 2025])
        df.loc[0, "Revenues"] = float("nan")
        spec = charts.build_spec("TST", "/nonexistent", df, pd.DataFrame(), pd.DataFrame())
        self.assertEqual(spec["charts"][0]["panels"][0]["series"][0]["values"], [None, 101.0])

    def test_near_black_series_follow_the_theme(self):
        self.assertEqual(charts._base_color("Free Cash Flow"), "strong")
        self.assertEqual(charts._base_color("Revenue"), "#4c72b0")


class PngTests(unittest.TestCase):
    def test_windows_and_one_wide_image(self):
        spec = charts.build_spec("TST", "/nonexistent", ratios(list(range(2013, 2026))),
                                 pd.DataFrame(), pd.DataFrame())
        self.assertEqual(charts.png_names(spec, ["01_income_statement"]), [
            "01_income_statement_2013_2017.png", "01_income_statement_2018_2022.png",
            "01_income_statement_2023_2025.png"])
        self.assertEqual(charts.png_names(spec, ["02_margins"], years_per_image=0), ["02_margins_2013_2025.png"])
        with tempfile.TemporaryDirectory() as folder:
            paths = charts.render_pngs(spec, folder, ids=["01_income_statement", "07_returns"],
                                       years_per_image=10, dark=True)
            self.assertEqual([Path(p).name for p in paths], [
                "01_income_statement_2013_2022.png", "01_income_statement_2023_2025.png",
                "07_returns_2013_2022.png", "07_returns_2023_2025.png"])
            for path in paths:
                self.assertTrue(Path(path).read_bytes().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
