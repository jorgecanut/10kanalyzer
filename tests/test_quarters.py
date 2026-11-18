"""quarters.py: the 10-Qs after the latest annual report. No network: dumps are built inline.

Run from the repo root:  python -m unittest discover tests
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dump  # noqa: E402
import quarters  # noqa: E402

M = 1e6        # the annual frame is in millions; facts are in units
FY_END = "2026-01-31"


def fact(concept, value, start=None, end=None, instant=None):
    return {"concept": concept, "numeric_value": value, "value": value, "currency": "USD", "unit_ref": "usd",
            "is_dimensioned": False, "dimension": None, "member": None, "period_instant": instant,
            "period_start": start, "period_end": end, "decimals": -6, "label": concept.split(":")[-1]}


def text(concept, value, start, end):
    return {"concept": concept, "numeric_value": None, "value": value, "currency": None, "unit_ref": None,
            "is_dimensioned": False, "dimension": None, "member": None, "period_instant": None,
            "period_start": start, "period_end": end, "decimals": None, "label": concept}


def annual(**values):
    """The latest fiscal year as financial_data.csv holds it (millions)."""
    row = {"period_of_report": FY_END, "fiscal_year": 2025, "form": "10-K", "currency": "USD",
           "Revenues": 100.0, "NetIncomeLoss": 4.0, "OperatingCashFlow": 7.0, "CapEx": 3.0,
           "StockholdersEquity": 16.0, "AssetsCurrent": 20.0}
    row.update(values)
    return pd.DataFrame([{"period_of_report": "2025-02-01", "fiscal_year": 2024, "form": "10-K", "currency": "USD"},
                         row])


SOURCES = {"Revenues": "Revenues", "NetIncomeLoss": "NetIncomeLoss", "OperatingCashFlow":
           "NetCashProvidedByUsedInOperatingActivities", "CapEx": "PaymentsToAcquirePropertyPlantAndEquipment",
           "StockholdersEquity": "StockholdersEquity", "AssetsCurrent": "AssetsCurrent"}


def entry(period_end, start, prior_end, prior_start, *, quarter="Q2", year="2026", accession=None,
          ytd=None, prior=None, instants=None, restated=None, revenue_tag="us-gaap:Revenues", extra=()):
    """A 10-Q dump: year-to-date and prior-year facts, cover page and instants."""
    ytd = {"Revenues": 49.0, "NetIncomeLoss": 3.5, "OperatingCashFlow": 4.0, "CapEx": 2.0, **(ytd or {})}
    prior = {"Revenues": 47.0, "NetIncomeLoss": 3.0, "OperatingCashFlow": 3.5, "CapEx": 1.5, **(prior or {})}
    tags = {"Revenues": revenue_tag, "NetIncomeLoss": "us-gaap:NetIncomeLoss",
            "OperatingCashFlow": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "CapEx": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"}
    rows = [text("dei:DocumentType", "10-Q", start, period_end),
            text("dei:DocumentPeriodEndDate", period_end, start, period_end),
            text("dei:DocumentFiscalPeriodFocus", quarter, start, period_end),
            text("dei:DocumentFiscalYearFocus", year, start, period_end)]
    for metric, value in ytd.items():
        if value is not None:
            rows.append(fact(tags[metric], value * M, start, period_end))
    for metric, value in prior.items():
        if value is not None:
            rows.append(fact(tags[metric], value * M, prior_start, prior_end))
    for metric, value in {"StockholdersEquity": 17.0, "AssetsCurrent": 21.0, **(instants or {})}.items():
        rows.append(fact(f"us-gaap:{metric}", value * M, instant=period_end))
    for metric, value in {"StockholdersEquity": 16.0, "AssetsCurrent": 20.0, **(restated or {})}.items():
        rows.append(fact(f"us-gaap:{metric}", value * M, instant=FY_END))
    rows.extend(extra)
    return {"schema": dump.SCHEMA, "xbrl": True,
            "filing": {"form": "10-Q", "accession": accession or f"q-{period_end}", "report_date": period_end},
            "dei": {"dei:DocumentPeriodEndDate": period_end, "dei:DocumentFiscalPeriodFocus": quarter,
                    "dei:DocumentFiscalYearFocus": year},
            "facts": dump.encode_frame(pd.DataFrame(rows)), "statements": {}}


Q1 = dict(period_end="2026-05-02", start="2026-02-01", prior_end="2025-05-03", prior_start="2025-02-02",
          quarter="Q1", ytd={"Revenues": 24.0, "NetIncomeLoss": 1.5, "OperatingCashFlow": 1.0, "CapEx": 0.8},
          prior={"Revenues": 23.0, "NetIncomeLoss": 1.2, "OperatingCashFlow": 0.9, "CapEx": 0.7})
Q2 = dict(period_end="2026-08-01", start="2026-02-01", prior_end="2025-08-02", prior_start="2025-02-02")


def build(entries, frame=None, sources=None):
    with redirect_stdout(io.StringIO()):
        frame, issues, _ = quarters.build(annual() if frame is None else frame,
                                          SOURCES if sources is None else sources, entries)
        return frame, issues


def kinds(issues, kind):
    return [issue for issue in issues if issue["kind"] == kind]


class TtmTests(unittest.TestCase):
    def test_ttm_is_fy_plus_ytd_minus_prior(self):
        frame, issues = build([entry(**Q2)])
        row = frame.iloc[0]
        self.assertAlmostEqual(row["Revenues"], 100 + 49 - 47)
        self.assertAlmostEqual(row["Revenues__ytd"], 49.0)
        self.assertAlmostEqual(row["Revenues__ytd_prior"], 47.0)
        self.assertAlmostEqual(row["NetIncomeLoss"], 4 + 3.5 - 3.0)
        self.assertEqual(row["slot"], "Q2'26")
        self.assertEqual(row["label"], "TTM Q2'26")
        self.assertEqual(row["quarter"], 2)
        self.assertEqual(row["fiscal_year"], 2026)
        self.assertEqual(row["period_end"], "2026-08-01")
        self.assertEqual([i for i in kinds(issues, "quarter_metric_missing") if i["severity"] == "warning"], [])
        # Derived on the TTM values, with clean_and_derive's formulas.
        self.assertAlmostEqual(row["NetMargin"], 4.5 / 102)
        self.assertAlmostEqual(row["FreeCashFlow"], (7 + 4 - 3.5) - (3 + 2 - 1.5))

    def test_q1_three_month_is_ytd(self):
        frame, _ = build([entry(**Q1)])
        self.assertAlmostEqual(frame.iloc[0]["Revenues__q"], 24.0)
        self.assertEqual(frame.iloc[0]["slot"], "Q1'26")

    def test_q2_three_month_is_ytd_difference(self):
        frame, _ = build([entry(**Q1), entry(**Q2)])
        self.assertEqual(list(frame["slot"]), ["Q1'26", "Q2'26"])
        self.assertAlmostEqual(frame.iloc[1]["Revenues__q"], 49 - 24)
        self.assertAlmostEqual(frame.iloc[1]["OperatingCashFlow__q"], 4.0 - 1.0)

    def test_three_month_without_previous_10q_is_blank_and_said(self):
        frame, issues = build([entry(**Q2)])
        self.assertTrue(np.isnan(frame.iloc[0]["Revenues__q"]))
        self.assertTrue(any("previous 10-Q" in issue["detail"] for issue in issues))

    def test_cash_flow_ytd_only(self):
        # A 10-Q's cash-flow statement has year-to-date figures only: TTM still comes out.
        frame, _ = build([entry(**Q2)])
        self.assertAlmostEqual(frame.iloc[0]["OperatingCashFlow"], 7 + 4 - 3.5)

    def test_instant_is_period_end_value(self):
        frame, _ = build([entry(**Q2)])
        self.assertAlmostEqual(frame.iloc[0]["StockholdersEquity"], 17.0)
        self.assertNotIn("StockholdersEquity__q", frame.columns)      # a balance has no three months

    def test_slot_statement_lines_and_eps(self):
        income = lambda value, end: {"statement": "income", "concept": "us-gaap_OperatingIncomeLoss",  # noqa: E731
                                     "value": value, "period_of_report": end}
        annual_lines = pd.DataFrame([{**income(5e6, FY_END), "fiscal_year": 2025}])
        eps = [fact("us-gaap:EarningsPerShareDiluted", 1.6, "2026-02-01", "2026-08-01"),
               fact("us-gaap:EarningsPerShareDiluted", 1.5, "2025-02-02", "2025-08-02")]
        annual_facts = pd.DataFrame([{"concept": "us-gaap:EarningsPerShareDiluted", "fiscal_year": 2025, "value": 8.0,
                                      "period_start": "2025-02-02", "period_end": FY_END, "is_dimensioned": False}])
        q = entry(**Q2, extra=eps)
        q["statements"] = {}
        with redirect_stdout(io.StringIO()):
            frame, issues, extra = quarters.build(annual(), SOURCES, [q], annual_lines, annual_facts)
        self.assertAlmostEqual(frame.iloc[0]["EpsDiluted"], 8.0 + 1.6 - 1.5)
        self.assertTrue(extra["facts"]["fiscal_year"].eq("Q2'26").all())

    def test_slot_lines_ttm(self):
        line = lambda statement, value: {"statement": statement, "concept": "x_Line", "value": value}  # noqa: E731
        annual_lines = pd.DataFrame([{**line("cashflow", 30.0), "fiscal_year": 2025}])
        ytd = {"statements": [line("cashflow", 16.0), line("balance", 9.0),
                              {**line("cashflow", 4.0), "concept": "x_OnlyInQuarter"}]}
        prior = {"statements": [line("cashflow", 14.0)]}
        issues = []
        lines = quarters._slot_lines(annual_lines, 2025, ytd, prior, "Q2'26", "2026-08-01", "q2", issues, False)
        self.assertEqual([(l["statement"], l["value"]) for l in lines], [("cashflow", 32.0), ("balance", 9.0)])
        self.assertEqual([i["concept"] for i in issues], ["x_OnlyInQuarter"])

    def test_no_quarters_ttm_is_fy(self):
        frame, issues = build([])
        self.assertTrue(frame.empty)
        self.assertEqual(issues, [])


class IssueTests(unittest.TestCase):
    def test_missing_metric_issue(self):
        frame, issues = build([entry(**Q2, ytd={"NetIncomeLoss": None})])
        self.assertTrue(np.isnan(frame.iloc[0]["NetIncomeLoss"]))
        missing = kinds(issues, "quarter_metric_missing")
        self.assertEqual([issue["concept"] for issue in missing if "previous 10-Q" not in issue["detail"]],
                         ["NetIncomeLoss"])
        self.assertEqual(missing[0]["fiscal_year"], "Q2'26")
        self.assertEqual(missing[0]["form"], "10-Q")

    def test_missing_prior_year_is_reported(self):
        _, issues = build([entry(**Q2, prior={"Revenues": None})])
        details = [issue["detail"] for issue in kinds(issues, "quarter_metric_missing") if issue["concept"] == "Revenues"]
        self.assertTrue(details and "year earlier" in details[0])

    def test_concept_differs_issue(self):
        frame, issues = build([entry(**Q2, revenue_tag="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")])
        differs = kinds(issues, "quarter_concept_differs")
        self.assertEqual([issue["concept"] for issue in differs], ["Revenues"])
        self.assertIn("RevenueFromContractWithCustomerExcludingAssessedTax", differs[0]["detail"])
        self.assertAlmostEqual(frame.iloc[0]["Revenues"], 102.0)      # still drawn, and said

    def test_restated_issue(self):
        _, issues = build([entry(**Q2, restated={"StockholdersEquity": 15.0})])
        restated = kinds(issues, "quarter_restated")
        self.assertEqual([issue["concept"] for issue in restated], ["StockholdersEquity"])
        self.assertEqual(restated[0]["reported"], 16.0)
        self.assertEqual(restated[0]["computed"], 15.0)

    def test_label_mismatch_issue(self):
        frame, issues = build([entry(**Q2, quarter="Q3")])
        self.assertEqual(frame.iloc[0]["slot"], "Q2'26")              # the dates win
        self.assertEqual(len(kinds(issues, "quarter_label_mismatch")), 1)

    def test_unreadable_entry_is_reported(self):
        bad = {"schema": dump.SCHEMA, "xbrl": False,
               "filing": {"form": "10-Q", "accession": "q-bad", "report_date": "2026-05-02"}}
        frame, issues = build([bad, entry(**Q2)])
        self.assertEqual(list(frame["slot"]), ["Q2'26"])
        self.assertEqual(kinds(issues, "quarter_unread")[0]["accession"], "q-bad")


class FilingShapeTests(unittest.TestCase):
    def test_restated_needs_the_same_tag(self):
        # The 10-K read LongTermDebt (14.398); the 10-Q tags only LongTermDebtAndCapitalLeaseObligations,
        # which the 10-K also showed as 14.326 at the year end. Not a restatement: another tag.
        frame_in = annual(LongTermDebt=14.398)
        sources = {**SOURCES, "LongTermDebt": "LongTermDebt"}
        lt = "us-gaap:LongTermDebtAndCapitalLeaseObligations"
        q = entry(**Q2, extra=[fact(lt, 14.326 * M, instant=FY_END), fact(lt, 14.221 * M, instant="2026-08-01")])
        frame, issues = build([q], frame_in, sources)
        self.assertEqual(kinds(issues, "quarter_restated"), [])
        differs = [i for i in kinds(issues, "quarter_concept_differs") if i["concept"] == "LongTermDebt"]
        self.assertEqual(len(differs), 1)
        self.assertIn("LongTermDebtAndCapitalLeaseObligations", differs[0]["detail"])
        self.assertAlmostEqual(frame.iloc[0]["LongTermDebt"], 14.221)

    def test_restated_same_tag_is_reported(self):
        frame_in = annual(LongTermDebt=14.398)
        sources = {**SOURCES, "LongTermDebt": "LongTermDebt"}
        q = entry(**Q2, extra=[fact("us-gaap:LongTermDebt", 14.0 * M, instant=FY_END),
                               fact("us-gaap:LongTermDebt", 13.9 * M, instant="2026-08-01")])
        _, issues = build([q], frame_in, sources)
        self.assertEqual([i["concept"] for i in kinds(issues, "quarter_restated")], ["LongTermDebt"])

    def test_derived_gross_profit_follows_the_year(self):
        # Neither the 10-K nor the 10-Q tags gross profit: the year's is revenue - cost, so is the TTM.
        frame_in = annual(GrossProfit=30.0, CostOfRevenue=70.0, gross_profit_derived=True)
        sources = {**SOURCES, "CostOfRevenue": "CostOfRevenue"}
        cost = "us-gaap:CostOfRevenue"
        q = entry(**Q2, extra=[fact(cost, 35.0 * M, "2026-02-01", "2026-08-01"),
                               fact(cost, 34.0 * M, "2025-02-02", "2025-08-02")])
        frame, issues = build([q], frame_in, sources)
        self.assertEqual([i for i in kinds(issues, "quarter_metric_missing") if i["concept"] == "GrossProfit"], [])
        self.assertAlmostEqual(frame.iloc[0]["GrossProfit"], 102.0 - 71.0)
        self.assertTrue(frame.iloc[0]["gross_profit_derived"])


class EstimateTests(unittest.TestCase):
    def test_estimated_operating_income_is_flagged_and_said(self):
        # The 10-Q tags no operating income: derive() fills an estimate, which must be flagged and said.
        frame_in = annual(OperatingIncomeLoss=5.0, CostOfRevenue=70.0, SellingGeneralAndAdministrativeExpense=20.0)
        sources = {**SOURCES, "OperatingIncomeLoss": "OperatingIncomeLoss", "CostOfRevenue": "CostOfRevenue",
                   "SellingGeneralAndAdministrativeExpense": "SellingGeneralAndAdministrativeExpense"}
        extra = []
        for tag, now, before in (("us-gaap:CostOfRevenue", 35.0, 34.0),
                                 ("us-gaap:SellingGeneralAndAdministrativeExpense", 10.0, 9.0)):
            extra += [fact(tag, now * M, "2026-02-01", "2026-08-01"), fact(tag, before * M, "2025-02-02", "2025-08-02")]
        frame, issues = build([entry(**Q2, extra=extra)], frame_in, sources)
        self.assertTrue(frame.iloc[0]["operating_income_derived"])
        said = [i for i in kinds(issues, "quarter_metric_missing") if i["concept"] == "OperatingIncomeLoss"]
        self.assertEqual(len(said), 1)
        self.assertIn("the value shown is derived", said[0]["detail"])


class PrecisionInputTests(unittest.TestCase):
    def test_diluted_shares_and_net_income_to_common(self):
        common = "us-gaap:NetIncomeLossAvailableToCommonStockholdersDiluted"
        extra = [fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 78.0e6, "2026-02-01", "2026-08-01"),
                 fact(common, 0.5 * M, "2026-02-01", "2026-08-01"), fact(common, -13.0 * M, "2025-02-02", "2025-08-02")]
        annual_facts = pd.DataFrame([{"concept": common, "fiscal_year": 2025, "value": -2.0 * M,
                                      "period_start": "2025-02-02", "period_end": FY_END, "is_dimensioned": False}])
        with redirect_stdout(io.StringIO()):
            frame, _, _ = quarters.build(annual(), SOURCES, [entry(**Q2, extra=extra)], None, annual_facts)
        self.assertAlmostEqual(frame.iloc[0]["DilutedShares__ytd"], 78.0e6)
        self.assertAlmostEqual(frame.iloc[0]["NetToCommon"], -2.0 + 0.5 + 13.0)


class CalendarTests(unittest.TestCase):
    def test_53_week_prior_period(self):
        # The year before had 53 weeks: its comparative ends 371 days earlier.
        frame, issues = build([entry("2026-08-01", "2026-02-01", "2025-07-26", "2025-01-26")])
        self.assertAlmostEqual(frame.iloc[0]["Revenues"], 102.0)
        self.assertEqual([i for i in kinds(issues, "quarter_metric_missing") if "previous 10-Q" not in i["detail"]], [])

    def test_december_year(self):
        frame_in = annual()
        frame_in.loc[1, "period_of_report"] = "2025-12-31"
        frame, _ = build([entry("2026-06-30", "2026-01-01", "2025-06-30", "2025-01-01", year="2026")], frame_in)
        self.assertEqual(frame.iloc[0]["slot"], "Q2'26")
        self.assertAlmostEqual(frame.iloc[0]["Revenues"], 102.0)

    def test_overdue_10k_has_no_ttm(self):
        # A fifth quarter after the fiscal year: its year to date starts after a
        # fiscal year we do not have, so FY + YTD - prior YTD is not the TTM.
        frame, issues = build([entry("2027-05-01", "2027-01-31", "2026-05-02", "2026-02-01", quarter="Q1",
                                     year="2027")])
        self.assertTrue(np.isnan(frame.iloc[0]["Revenues"]))
        self.assertAlmostEqual(frame.iloc[0]["StockholdersEquity"], 17.0)
        self.assertTrue(any("overdue" in issue["detail"] for issue in kinds(issues, "quarter_metric_missing")))


if __name__ == "__main__":
    unittest.main()
