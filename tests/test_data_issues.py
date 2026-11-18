"""data_issues.py: a metric is "not in the filing" only after the filing was searched. No network.

Hiding is not fixing: in batch 1 a gap in our own extraction was labelled
"the filing's statements have no such line" while the filing had the number.
These cases (AAUAF, AACO) must stay reported as problems.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_issues  # noqa: E402

PERIOD = "2024-12-31"


def _statement(rows, column=f"{PERIOD} (FY)"):
    frame = pd.DataFrame([{"concept": c, "label": l, "standard_concept": s, column: v, "abstract": False,
                           "dimension": False} for c, l, s, v in rows])
    return SimpleNamespace(to_dataframe=lambda: frame.copy())


def _xbrl(income=None, balance=None, cashflow=None, broken=()):
    def reader(found, name):
        def read():
            if name in broken:
                raise RuntimeError("edgartools could not render it")
            return found
        return read
    statements = SimpleNamespace(income_statement=reader(income, "income"), balance_sheet=reader(balance, "balance"),
                                 cash_flow_statement=reader(cashflow, "cashflow"))
    return SimpleNamespace(statements=statements)


def _facts(rows):
    return pd.DataFrame([{"concept": c, "numeric_value": v, "value": v, "period_start": start, "period_end": end,
                          "period_instant": None, "is_dimensioned": False, "currency": "CAD"}
                         for c, v, start, end in rows])


def _kinds(xbrl, facts, row=None):
    row = row or {}
    evidence = data_issues.metric_evidence(xbrl, facts, PERIOD, row)
    return {metric: data_issues.classify_missing(metric, item)[0] for metric, item in evidence.items()}


class EvidenceTests(unittest.TestCase):
    def test_aauaf_no_income_statement_but_a_net_loss_fact_is_a_missing_metric(self):
        # AAUAF's 20-F: edgartools found no income statement; the facts hold
        # "Net loss for the year" as ProfitLossFromContinuingOperations.
        facts = _facts([("ifrs-full:ProfitLossFromContinuingOperations", -2875061, "2024-01-01", PERIOD)])
        kinds = _kinds(_xbrl(income=None), facts)
        self.assertEqual(kinds["NetIncomeLoss"], "missing_metric")
        self.assertEqual(kinds["Revenues"], "statement_missing")         # not "not_reported": nothing was searched
        self.assertEqual(kinds["OperatingIncomeLoss"], "statement_missing")
        evidence = data_issues.metric_evidence(_xbrl(income=None), facts, PERIOD, {})
        detail = data_issues.classify_missing("NetIncomeLoss", evidence["NetIncomeLoss"])[2]
        self.assertIn("ProfitLossFromContinuingOperations", detail)
        self.assertIn("-2,875,061 CAD", detail)

    def test_aaco_a_line_with_no_value_for_the_year_is_a_missing_metric(self):
        # AACO's first (partial) year: the lines are there, the year's column is empty.
        income = _statement([("us-gaap_OperatingIncomeLoss", "Loss from operations", "OperatingIncomeLoss", np.nan),
                             ("us-gaap_NetIncomeLoss", "Net loss", "NetIncome", np.nan),
                             ("us-gaap_EarningsPerShareBasic", "Basic net loss per share", None, np.nan)])
        kinds = _kinds(_xbrl(income=income, cashflow=_statement([])), _facts([]))
        self.assertEqual(kinds["NetIncomeLoss"], "missing_metric")
        self.assertEqual(kinds["OperatingIncomeLoss"], "missing_metric")
        self.assertEqual(kinds["Revenues"], "not_reported")            # a SPAC: no revenue line, no fact

    def test_a_bank_without_a_revenue_line_or_fact_is_not_reported(self):
        income = _statement([("us-gaap_InterestIncomeExpenseNet", "Net interest income", "NetInterestIncome", 835.0),
                             ("us-gaap_NetIncomeLoss", "Net income", "NetIncome", 269.0)])
        kinds = _kinds(_xbrl(income=income), _facts([]), {"NetIncomeLoss": 269.0})
        self.assertEqual((kinds["Revenues"], kinds["OperatingIncomeLoss"]), ("not_reported", "not_reported"))
        self.assertNotIn("NetIncomeLoss", kinds)                       # read: no evidence needed

    def test_a_statement_that_fails_to_render_is_statement_missing(self):
        kinds = _kinds(_xbrl(broken=("cashflow",)), _facts([]))
        self.assertEqual(kinds["OperatingCashFlow"], "statement_missing")

    def test_a_fact_of_another_year_proves_nothing(self):
        facts = _facts([("us-gaap:Revenues", 100.0, "2023-01-01", "2023-12-31")])
        income = _statement([("us-gaap_CostOfRevenue", "Cost of sales", "CostOfGoodsAndServicesSold", 50.0)])
        self.assertEqual(_kinds(_xbrl(income=income), facts)["Revenues"], "not_reported")

    def test_mgx_a_company_tagged_revenue_line_is_found(self):
        # MGX's 10-Q: "Collaboration revenue" under its own tag (it includes a reversal). It is in the
        # filing, so the gap is a missing_metric naming the line, never "not in the filing".
        income = _statement([("mgx_RevenueAndReversalOfRevenueFromContractWithCustomerExcludingAssessedTax",
                              "Collaboration revenue", None, 991000.0),
                             ("us-gaap_ResearchAndDevelopmentExpense", "Research and development", None, 4.1e7)])
        kinds = _kinds(_xbrl(income=income), _facts([]), {})
        self.assertEqual(kinds["Revenues"], "missing_metric")
        evidence = data_issues.metric_evidence(_xbrl(income=income), _facts([]), PERIOD, {})
        self.assertIn("Collaboration revenue", data_issues.classify_missing("Revenues", evidence["Revenues"])[3])
        facts = _facts([("mgx:RevenueAndReversalOfRevenueFromContractWithCustomerExcludingAssessedTax", 991000.0,
                         "2024-01-01", PERIOD)])
        self.assertEqual(_kinds(_xbrl(income=_statement([])), facts)["Revenues"], "missing_metric")

    def test_revenue_words_that_are_not_revenue(self):
        income = _statement([("us-gaap_CostOfRevenue", "Cost of revenue", "CostOfGoodsAndServicesSold", 50.0),
                             ("abc_DeferredRevenueRecognized", "Deferred revenue recognized", None, 5.0)])
        facts = _facts([("us-gaap:IncreaseDecreaseInDeferredRevenue", -4.0, "2024-01-01", PERIOD),
                        ("us-gaap:RevenueRemainingPerformanceObligation", 9.0, "2024-01-01", PERIOD)])
        self.assertEqual(_kinds(_xbrl(income=income), facts)["Revenues"], "not_reported")

    def test_no_evidence_is_never_not_reported(self):
        self.assertEqual(data_issues.classify_missing("Revenues", None)[0], "missing_metric")
        self.assertEqual(data_issues.classify_missing("Revenues", {})[0], "missing_metric")


class CompanyIssuesTests(unittest.TestCase):
    def test_every_kind_comes_out_with_where_it_sits(self):
        derived = pd.DataFrame([{"period_of_report": PERIOD, "fiscal_year": 2024, "Revenues": 21.8,
                                 "CostOfRevenue": 1773.0, "GrossProfit": 687.0, "OperatingIncomeLoss": 164.0,
                                 "NetIncomeLoss": np.nan, "OperatingCashFlow": 1.0, "StockholdersEquity": 1.0,
                                 "gross_profit_derived": False}])
        checks = pd.DataFrame([{"period_of_report": PERIOD, "fiscal_year": 2024, "kind": "statement",
                                "statement": "Balance Sheet", "concept": "us-gaap_Assets", "label": "Total assets",
                                "reported": 100.0, "computed": 90.0, "difference": 10.0, "tolerance": 0.5,
                                "parts": 2, "status": "mismatch", "hint": ""}])
        filed = {PERIOD: {"form": "10-K", "accession": "0001-24-000001", "evidence": {"NetIncomeLoss": {
            "statement": "income", "state": "missing", "line": None, "fact": None}}}}
        issues = data_issues.company_issues(derived, checks, filed)
        kinds = sorted(issue["kind"] for issue in issues)
        self.assertEqual(kinds, ["check_mismatch", "implausible_value", "implausible_value", "implausible_value",
                                 "statement_missing"])
        self.assertTrue(all(issue["accession"] == "0001-24-000001" for issue in issues))

    def test_a_derived_operating_income_does_not_hide_the_one_not_read(self):
        # Gross profit less SG&A and R&D stood in for it, and no issue was raised.
        derived = pd.DataFrame([{"period_of_report": PERIOD, "fiscal_year": 2024, "Revenues": 1000.0,
                                 "OperatingIncomeLoss": 300.0, "operating_income_derived": True,
                                 "NetIncomeLoss": 50.0, "OperatingCashFlow": 80.0, "StockholdersEquity": 500.0}])
        fact = {"concept": "us-gaap:OperatingIncomeLoss", "value": 120e6, "days": 365, "end": PERIOD,
                "currency": "USD"}
        filed = {PERIOD: {"form": "10-K", "accession": "a", "evidence": {"OperatingIncomeLoss": {
            "statement": "income", "state": "present", "line": None, "fact": fact}}}}
        issues = data_issues.company_issues(derived, pd.DataFrame(), filed)
        self.assertEqual([(issue["kind"], issue["concept"]) for issue in issues],
                         [("missing_metric", "OperatingIncomeLoss")])
        self.assertIn("derived", issues[0]["detail"])
        reported = derived.assign(operating_income_derived=False)
        self.assertEqual(data_issues.company_issues(reported, pd.DataFrame(), filed), [])

    def test_a_statement_that_was_not_read_is_an_issue_of_its_year(self):
        derived = pd.DataFrame([{"period_of_report": PERIOD, "fiscal_year": 2024, "Revenues": 1.0,
                                 "OperatingIncomeLoss": 1.0, "NetIncomeLoss": 1.0, "OperatingCashFlow": 1.0,
                                 "StockholdersEquity": 1.0}])
        filed = {PERIOD: {"form": "10-K", "accession": "a", "statement_problems": [
            {"statement": "cashflow", "detail": "it could not be rendered (KeyError: 'x')"}]}}
        [issue] = data_issues.company_issues(derived, pd.DataFrame(), filed)
        self.assertEqual((issue["kind"], issue["concept"], issue["fiscal_year"], issue["statement"]),
                         ("statement_unread", "CashFlowStatement", 2024, "cash flow statement"))
        self.assertIn("KeyError", issue["detail"])
        [warning] = data_issues.chart_warnings({"CashFlowStatement"}, [2024], [issue])
        self.assertEqual(warning["title"], "Statement not read")

    def test_metrics_that_contradict_each_other(self):
        row = {"Revenues": 21.8, "CostOfRevenue": 1773.0, "GrossProfit": 687.0, "OperatingIncomeLoss": 164.0,
               "gross_profit_derived": False}
        self.assertEqual([metric for metric, _ in data_issues.implausible(row)],
                         ["GrossProfit", "OperatingIncomeLoss", "Revenues"])
        fine = {**row, "Revenues": 2460.0}
        self.assertEqual(data_issues.implausible(fine), [])
        self.assertEqual(data_issues.implausible({"Revenues": 0.0, "GrossProfit": 5.0}), [])


class ChartWarningTests(unittest.TestCase):
    def test_issues_reach_the_charts_drawn_from_them(self):
        import charts

        df = pd.DataFrame({"fiscal_year": [2023, 2024], "period_of_report": ["2023-12-31", PERIOD],
                           "Revenues": [np.nan, np.nan], "NetIncomeLoss": [5.0, 6.0],
                           "StockholdersEquity": [50.0, 60.0]})
        issues = [{"kind": "not_reported", "severity": "info", "concept": "Revenues", "fiscal_year": year,
                   "detail": "the income statement has no such line"} for year in (2023, 2024)]
        issues.append({"kind": "check_mismatch", "severity": "warning", "concept": "us-gaap_Assets",
                       "label": "Total assets", "fiscal_year": 2024, "detail": "2 parts"})
        issues.append({"kind": "missing_metric", "severity": "warning", "concept": "Revenues", "fiscal_year": 2019,
                       "detail": "outside the chart's years"})
        spec = charts.build_spec("TST", "/nonexistent", df, pd.DataFrame(), pd.DataFrame(), issues)
        warnings = {chart["id"]: chart["warnings"] for chart in spec["charts"]}
        # Revenue has no value at all, so its series is left out: the chart still warns.
        self.assertEqual([(w["kind"], w["concept"], w["years"]) for w in warnings["01_income_statement"]],
                         [("not_reported", "Revenues", [2023, 2024])])
        self.assertEqual(warnings["02_margins"][0]["concept"], "Revenues")
        self.assertEqual([w["label"] for w in warnings["07_returns"]], ["Total assets"])   # ROA uses total assets
        self.assertEqual(warnings["06_cashflow_financing"], [])
        self.assertTrue(spec["issues_checked"])

    def test_charts_without_checked_issues_say_so(self):
        import charts

        df = pd.DataFrame({"fiscal_year": [2024], "Revenues": [1.0]})
        spec = charts.build_spec("TST", "/nonexistent", df, pd.DataFrame(), pd.DataFrame())
        self.assertFalse(spec["issues_checked"])
        self.assertTrue(all(chart["warnings"] == [] for chart in spec["charts"]))


class StatementProblemTests(unittest.TestCase):
    """A statement the extraction skips is said, not dropped."""

    def problems(self, **statements):
        import xbrl_extract
        absent = {method: (lambda: None) for method, _ in xbrl_extract.STATEMENTS.values()}
        xbrl = SimpleNamespace(statements=SimpleNamespace(**{**absent, **statements}))
        found = []
        rows = xbrl_extract.extract_statements(xbrl, PERIOD, problems=found)
        return rows, {item["statement"]: item["detail"] for item in found}

    def frame(self, column):
        data = pd.DataFrame([{"concept": "us-gaap_Assets", "label": "Total assets", column: 5.0}])
        return lambda: SimpleNamespace(to_dataframe=lambda: data)

    def test_a_statement_that_fails_to_render(self):
        def broken():
            raise KeyError("role")
        _, found = self.problems(income_statement=broken, balance_sheet=self.frame(PERIOD),
                                 cash_flow_statement=self.frame(PERIOD))
        self.assertEqual(list(found), ["income"])
        self.assertIn("KeyError", found["income"])

    def test_a_statement_without_a_column_for_the_year(self):
        rows, found = self.problems(income_statement=self.frame(PERIOD), balance_sheet=self.frame("2019-12-31"),
                                    cash_flow_statement=self.frame(PERIOD))
        self.assertEqual(list(found), ["balance"])
        self.assertIn("2019-12-31", found["balance"])
        self.assertEqual({row["statement"] for row in rows}, {"income", "cashflow"})

    def test_a_primary_statement_that_is_not_there(self):
        # Equity and comprehensive income are often absent; the three primary ones are not.
        _, found = self.problems(income_statement=self.frame(PERIOD), balance_sheet=self.frame(PERIOD))
        self.assertEqual(list(found), ["cashflow"])


if __name__ == "__main__":
    unittest.main()


class QuarterSlotWarningTests(unittest.TestCase):
    def test_chart_warnings_match_slots(self):
        issues = [
            {"kind": "quarter_metric_missing", "severity": "warning", "fiscal_year": "Q2'26", "concept": "Revenues",
             "detail": "b"},
            {"kind": "quarter_metric_missing", "severity": "warning", "fiscal_year": "Q3'25", "concept": "Revenues",
             "detail": "a"},
            {"kind": "missing_metric", "severity": "warning", "fiscal_year": "2025", "concept": "Revenues",
             "detail": "c"},          # read back from data_issues.csv: a year is text
            {"kind": "quarter_metric_missing", "severity": "warning", "fiscal_year": "Q1'27", "concept": "Revenues",
             "detail": "not drawn"},
        ]
        warnings = data_issues.chart_warnings({"Revenues"}, [2024, 2025, "Q3'25", "Q2'26"], issues)
        by_kind = {w["kind"]: w for w in warnings}
        self.assertEqual(by_kind["quarter_metric_missing"]["years"], ["Q3'25", "Q2'26"])
        self.assertEqual(by_kind["missing_metric"]["years"], [2025])

    def test_slot_order(self):
        self.assertEqual(sorted(["Q1'27", 2025, "Q3'26", 2024], key=data_issues.slot_order),
                         [2024, 2025, "Q3'26", "Q1'27"])

    def test_issue_years_name_slots_without_fy(self):
        page = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const fyLabel", page)
        self.assertNotIn("years.map(y => `FY${y}`)", page)
        self.assertNotIn("<li>FY${d.year}", page)

    def test_titles_cover_quarter_kinds(self):
        page = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        for kind in ("quarter_unread", "quarter_metric_missing", "quarter_concept_differs", "quarter_restated",
                     "quarter_label_mismatch"):
            self.assertIn(kind, data_issues.TITLES)
            self.assertIn(f"{kind}:", page)
