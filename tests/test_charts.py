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


class DerivedSeriesTests(unittest.TestCase):
    def operating(self, flag):
        df = pd.DataFrame([{"fiscal_year": 2025, "Revenues": 1000.0, "OperatingIncomeLoss": 300.0,
                            "operating_income_derived": flag}])
        chart = charts.chart_income_statement("TEST", df, pd.DataFrame(), [2025])
        return next(s for panel in chart["panels"] for s in panel["series"] if s["label"] == "Operating Income")

    def test_a_derived_operating_income_is_starred(self):
        self.assertTrue(self.operating(True)["derived"])

    def test_a_reported_one_is_not(self):
        self.assertFalse(self.operating(False)["derived"])


def quarter_frame():
    return pd.DataFrame([{"slot": "Q2'26", "label": "TTM Q2'26", "period_end": "2026-08-01", "fiscal_year": 2026,
                          "quarter": 2, "Revenues": 102.0, "Revenues__q": 25.0, "NetIncomeLoss": 11.0,
                          "NetIncomeLoss__q": 3.0, "StockholdersEquity": 52.0}])


class QuarterSlotTests(unittest.TestCase):
    def build(self, quarters=None, statements=None):
        return charts.build_spec("TST", "/nonexistent", ratios([2024, 2025]), pd.DataFrame(), pd.DataFrame(),
                                 quarters=quarters, quarter_statements=statements)

    def test_spec_has_quarter_slots(self):
        spec = self.build(quarter_frame())
        income = spec["charts"][0]["panels"][0]
        self.assertEqual(income["years"], [2024, 2025])
        self.assertEqual(income["quarters"], [{"id": "Q2'26", "label": "TTM Q2'26", "period_end": "2026-08-01"}])
        revenue = income["series"][0]
        self.assertEqual(revenue["values"], [100.0, 101.0, 102.0])
        self.assertEqual(revenue["quarter_values"], [25.0])
        balance = next(c for c in spec["charts"] if c["id"] == "03_balance")["panels"][0]
        self.assertEqual(balance["quarters"][0]["label"], "Q2'26")          # a balance is not a TTM
        equity = next(s for s in balance["series"] if s["label"] == "Total Equity")
        self.assertEqual(equity["values"][-1], 52.0)
        self.assertEqual(equity["quarter_values"], [None])
        self.assertEqual(spec["quarters"], ["Q2'26"])
        json.dumps(spec, allow_nan=False)

    def test_statement_lines_in_slots(self):
        line = {"statement": "cashflow", "concept": "us-gaap_PaymentsOfDividends", "standard_concept": None,
                "label": "Dividends paid", "value": -2e6, "fiscal_year": "Q2'26"}
        spec = self.build(quarter_frame(), pd.DataFrame([line]))
        financing = next(c for c in spec["charts"] if c["id"] == "06_cashflow_financing")["panels"][0]
        dividends = next(s for s in financing["series"] if s["label"] == "Dividends Paid")
        self.assertEqual(dividends["values"], [None, None, -2.0])

    def test_spec_without_quarters_unchanged(self):
        plain = charts.build_spec("TST", "/nonexistent", ratios([2024, 2025]), pd.DataFrame(), pd.DataFrame())
        self.assertEqual(plain, self.build(None))
        self.assertEqual(plain["charts"][0]["panels"][0]["quarters"], [])
        self.assertNotIn("quarter_values", plain["charts"][0]["panels"][0]["series"][0])

    def test_slot_issues_reach_the_chart(self):
        issue = {"kind": "quarter_metric_missing", "severity": "warning", "fiscal_year": "Q2'26",
                 "concept": "Revenues", "detail": "x"}
        spec = charts.build_spec("TST", "/nonexistent", ratios([2024, 2025]), pd.DataFrame(), pd.DataFrame(),
                                 issues=[issue], quarters=quarter_frame())
        warnings = spec["charts"][0]["warnings"]
        self.assertEqual([(w["kind"], w["years"]) for w in warnings], [("quarter_metric_missing", ["Q2'26"])])

    def test_png_draws_slots(self):
        spec = self.build(quarter_frame())
        with tempfile.TemporaryDirectory() as folder:
            paths = charts.render_pngs(spec, folder, ids=["01_income_statement", "03_balance"], years_per_image=5)
            self.assertEqual([Path(p).name for p in paths],
                             ["01_income_statement_2024_2025.png", "03_balance_2024_2025.png"])
        panel = spec["charts"][0]["panels"][0]
        self.assertEqual(charts._window_keys(panel, [2024, 2025]), [2024, 2025, "Q2'26"])
        self.assertEqual(charts._window_keys(panel, [2024]), [2024])

    def test_a_derived_slot_is_starred(self):
        df = pd.DataFrame([{"fiscal_year": 2025, "Revenues": 1000.0, "OperatingIncomeLoss": 300.0,
                            "operating_income_derived": False},
                           {"fiscal_year": "Q2'26", "Revenues": 1100.0, "OperatingIncomeLoss": 310.0,
                            "operating_income_derived": True}])
        self.assertTrue(charts._any_flag(df, "operating_income_derived", [2025, "Q2'26"]))
        self.assertFalse(charts._any_flag(df, "operating_income_derived", [2025]))

    def test_valuation_chart_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame([{"fiscal_year": 2024, "pe": 10.0, "f_score": 6, "z_score": 3.0, "pe_median_hist": 11.0},
                          {"fiscal_year": 2025, "pe": 12.0, "f_score": 7, "z_score": 3.1, "pe_median_hist": 11.0},
                          {"fiscal_year": "Q2'26", "pe": 14.0, "f_score": None, "z_score": 3.2,
                           "pe_median_hist": 11.0, "period_end": "2026-08-01"}]).to_csv(
                Path(folder) / "evaluation.csv", index=False)
            chart = charts.chart_valuation("TST", folder)
        pe = chart["panels"][0]
        self.assertEqual(pe["years"], [2024, 2025])
        self.assertEqual(pe["quarters"][0]["id"], "Q2'26")
        self.assertEqual(pe["series"][0]["values"], [10.0, 12.0, 14.0])


if __name__ == "__main__":
    unittest.main()
