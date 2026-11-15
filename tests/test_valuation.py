"""Share-count extraction for the valuation step. No network: facts are built inline.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import valuation  # noqa: E402


def facts(*rows):
    """(concept, fiscal_year, value, period_instant, period_end) tuples as a facts frame."""
    return pd.DataFrame(
        [{"concept": c, "fiscal_year": y, "value": v, "period_instant": i, "period_end": e,
          "is_dimensioned": False} for c, y, v, i, e in rows]
    )


class ExtractSharesTests(unittest.TestCase):
    def test_reported_outstanding_count_comes_first(self):
        frame = facts(
            ("us-gaap:CommonStockSharesOutstanding", 2025, 100.0, "2025-12-31", None),
            ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 2025, 999.0, None, "2025-12-31"),
        )
        self.assertEqual(valuation._extract_shares(frame, 2025), 100.0)

    def test_issued_minus_treasury_when_outstanding_is_not_reported(self):
        # Pfizer's filings: no outstanding count, only issued and treasury shares,
        # with the treasury concept renamed between years.
        frame = facts(
            ("us-gaap:CommonStockSharesIssued", 2025, 9_621e6, "2025-12-31", None),
            ("us-gaap:TreasuryStockCommonShares", 2025, 3_935e6, "2025-12-31", None),
            ("us-gaap:CommonStockSharesIssued", 2022, 9_519e6, "2022-12-31", None),
            ("us-gaap:TreasuryStockShares", 2022, 3_903e6, "2022-12-31", None),
            ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 2025, 5_713e6, None, "2025-12-31"),
        )
        self.assertEqual(valuation._extract_shares(frame, 2025), 5_686e6)
        self.assertEqual(valuation._extract_shares(frame, 2022), 5_616e6)

    def test_issued_alone_is_not_taken_for_outstanding(self):
        frame = facts(
            ("us-gaap:CommonStockSharesIssued", 2025, 9_621e6, "2025-12-31", None),
            ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 2025, 5_713e6, None, "2025-12-31"),
        )
        self.assertEqual(valuation._extract_shares(frame, 2025), 5_713e6)

    def test_weighted_average_as_last_resort(self):
        frame = facts(("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic", 2025, 5_683e6, None, "2025-12-31"))
        self.assertEqual(valuation._extract_shares(frame, 2025), 5_683e6)

    def test_nothing_reported_gives_nan(self):
        frame = facts(("us-gaap:Revenues", 2025, 1.0, None, "2025-12-31"))
        self.assertTrue(pd.isna(valuation._extract_shares(frame, 2025)))


def line(statement, concept, label, value, year=2025, standard=None):
    return {"statement": statement, "concept": concept, "standard_concept": standard, "label": label,
            "value": value, "fiscal_year": year, "period_of_report": f"{year}-12-31"}


class StatementLookupTests(unittest.TestCase):
    def test_total_assets_is_never_a_line_that_only_mentions_assets(self):
        # "assets" as a substring matched the first line with the word in it.
        frame = pd.DataFrame([line("balance", "us-gaap_OtherAssetsCurrent", "Other current assets", 5e6),
                              line("balance", "x_Everything", "Assets, total", 900e6)])
        self.assertTrue(pd.isna(valuation._statement_metric(frame, [2025], *valuation.TOTAL_ASSETS,
                                                            fuzzy=False, statement="balance")[2025]))

    def test_cash_comes_from_the_balance_sheet_not_the_cash_flow_change(self):
        frame = pd.DataFrame([
            line("cashflow", "us-gaap_CashAndCashEquivalentsPeriodIncreaseDecrease",
                 "Net increase in cash and cash equivalents", 7e6),
            line("balance", "us-gaap_Cash", "Cash and cash equivalents", 120e6)])
        self.assertEqual(valuation._statement_metric(frame, [2025], *valuation.CASH, statement="balance")[2025], 120.0)
        only_flow = frame[frame["statement"] == "cashflow"]
        self.assertTrue(pd.isna(valuation._statement_metric(only_flow, [2025], *valuation.CASH,
                                                            statement="balance")[2025]))


def company(years=(2024, 2025), shares=(100e6, 100e6), cash=True, depreciation=True, ads=False):
    """A two-year company: ratio rows, statements and facts, money in USD."""
    df = pd.DataFrame([{"fiscal_year": y, "period_of_report": f"{y}-12-31", "currency": "USD", "ads": ads,
                        "Revenues": 1000.0, "NetIncomeLoss": 100.0, "OperatingIncomeLoss": 150.0,
                        "OperatingCashFlow": 160.0, "CapEx": 40.0, "FreeCashFlow": 120.0, "GrossProfit": 400.0,
                        "StockholdersEquity": 500.0, "LongTermDebt": 200.0, "AssetsCurrent": 300.0,
                        "LiabilitiesCurrent": 150.0} for y in years])
    rows, fact_rows = [], []
    for y, count in zip(years, shares):
        rows.append(line("balance", "us-gaap_Assets", "Total assets", 1000e6, y, "Assets"))
        rows.append(line("balance", "us-gaap_Liabilities", "Total liabilities", 500e6, y, "Liabilities"))
        rows.append(line("balance", "us-gaap_RetainedEarningsAccumulatedDeficit", "Retained earnings", 300e6, y))
        if cash:
            rows.append(line("balance", "us-gaap_CashAndCashEquivalentsAtCarryingValue",
                             "Cash and cash equivalents", 50e6, y))
        if depreciation:
            rows.append(line("cashflow", "us-gaap_DepreciationDepletionAndAmortization",
                             "Depreciation and amortization", 30e6, y))
        fact_rows.append({"concept": "us-gaap:CommonStockSharesOutstanding", "fiscal_year": y, "value": count,
                          "period_instant": f"{y}-12-31", "period_end": None, "is_dimensioned": False})
    return df, pd.DataFrame(rows), pd.DataFrame(fact_rows)


PRICES = [{"date": "2024-12-01", "close": 20.0}, {"date": "2025-12-01", "close": 10.0}]


def evaluate(df, statements, frame, prices=PRICES, **options):
    issues = []
    out = valuation.build_evaluation("TEST", df, statements, frame, prices, issues=issues, **options)
    return out.set_index("fiscal_year"), issues


def kinds(issues, concept):
    return sorted({(issue["kind"], issue["fiscal_year"]) for issue in issues if issue["concept"] == concept})


class ValuationIssueTests(unittest.TestCase):
    def test_a_complete_year_has_no_issue(self):
        out, issues = evaluate(*company(), splits=[])
        self.assertEqual(issues, [])
        self.assertAlmostEqual(out.loc[2025, "ev_ebitda"], (1000.0 + 200.0 - 50.0) / 180.0)

    def test_cash_not_found_is_reported_not_passed_off_as_zero(self):
        _, issues = evaluate(*company(cash=False), splits=[])
        self.assertEqual(kinds(issues, "Cash"), [("valuation_assumed", 2024), ("valuation_assumed", 2025)])

    def test_without_depreciation_there_is_no_ebitda(self):
        # EBIT used to be shown as EBITDA.
        out, issues = evaluate(*company(depreciation=False), splits=[])
        self.assertTrue(out["ev_ebitda"].isna().all())
        self.assertEqual(kinds(issues, "Depreciation"),
                         [("valuation_input_missing", 2024), ("valuation_input_missing", 2025)])

    def test_a_year_without_a_price_or_a_share_count_is_reported(self):
        df, statements, frame = company()
        _, issues = evaluate(df, statements, frame[frame["fiscal_year"] == 2025], prices=PRICES[1:], splits=[])
        self.assertEqual(kinds(issues, "Price"), [("valuation_input_missing", 2024)])
        self.assertEqual(kinds(issues, "Shares"), [("valuation_input_missing", 2024)])


class SplitTests(unittest.TestCase):
    def test_a_share_issuance_is_not_a_split(self):
        # 100M -> 210M shares (a merger) doubled the earlier year's market cap.
        out, issues = evaluate(*company(shares=(100e6, 210e6)), splits=[])
        self.assertEqual(out.loc[2024, "shares"], 100e6)
        self.assertEqual(kinds(issues, "Splits"), [])

    def test_a_real_split_scales_the_counts_before_it(self):
        out, _ = evaluate(*company(shares=(100e6, 200e6)), splits=[{"date": "2025-06-10", "ratio": 2.0}])
        self.assertEqual(out.loc[2024, "shares"], 200e6)
        self.assertEqual(out.loc[2025, "shares"], 200e6)

    def test_a_split_after_the_last_year_scales_every_count(self):
        out, _ = evaluate(*company(), splits=[{"date": "2026-03-01", "ratio": 4.0}])
        self.assertEqual(list(out["shares"]), [400e6, 400e6])

    def test_a_count_already_restated_for_a_split_is_not_scaled_again(self):
        # A split made before the report went out is already in that year's count.
        out, _ = evaluate(*company(shares=(100e6, 200e6)), splits=[{"date": "2026-01-15", "ratio": 2.0}])
        self.assertEqual(list(out["shares"]), [200e6, 200e6])

    def test_unknown_splits_are_inferred_and_say_so(self):
        out, issues = evaluate(*company(shares=(100e6, 200e6)), splits=None)
        self.assertEqual(out.loc[2024, "shares"], 200e6)
        self.assertEqual(kinds(issues, "Splits"), [("valuation_assumed", 2024)])


class MarketRequestTests(unittest.TestCase):
    def calls(self, function, *args):
        seen = []
        original = valuation._chart
        valuation._chart = lambda ticker, query: seen.append((ticker, query)) or {}
        try:
            function(*args)
        finally:
            valuation._chart = original
        return seen

    def test_a_ticker_with_a_dot_is_asked_for_the_way_yahoo_spells_it(self):
        self.assertEqual(self.calls(valuation.fetch_price_history, "BRK.B")[0][0], "BRK-B")
        self.assertEqual(self.calls(valuation.fetch_quote, "BRK.B")[0][0], "BRK-B")

    def test_the_price_history_covers_every_year_by_default(self):
        self.assertIn("range=max", self.calls(valuation.fetch_price_history, "AAPL")[0][1])

    def test_splits_come_from_yahoo(self):
        original = valuation._chart
        valuation._chart = lambda ticker, query: {"chart": {"result": [{"events": {"splits": {
            "1": {"date": 1598880600, "numerator": 4, "denominator": 1}}}}]}}
        try:
            self.assertEqual(valuation.fetch_splits("AAPL"), [{"date": "2020-08-31", "ratio": 4.0}])
        finally:
            valuation._chart = original

    def test_splits_that_cannot_be_fetched_are_unknown_not_none(self):
        original = valuation._chart
        valuation._chart = lambda ticker, query: (_ for _ in ()).throw(OSError("down"))
        try:
            self.assertIsNone(valuation.fetch_splits("AAPL"))
        finally:
            valuation._chart = original


if __name__ == "__main__":
    unittest.main()


def balance(*rows, year=2025):
    """(standard_concept, label, value in USD) tuples as a statements frame."""
    return pd.DataFrame(
        [{"statement": "balance", "standard_concept": c, "label": l, "value": v, "fiscal_year": year}
         for c, l, v in rows]
    )


class TotalLiabilitiesTests(unittest.TestCase):
    def test_reported_total_comes_first(self):
        frame = balance(
            ("OtherNonOperatingCurrentLiabilities", "Accrued interest", 533e6),
            ("Liabilities", "Total liabilities", 61_000e6),
        )
        self.assertEqual(valuation._total_liabilities(frame, [2025])[2025], 61_000)

    def test_current_plus_noncurrent_when_no_total(self):
        # Eli Lilly's shape: subtotals only; "Other current liabilities" is not the total.
        frame = balance(
            ("Assets", "Total assets", 112_476e6),
            ("OtherNonOperatingCurrentLiabilities", "Other current liabilities", 8_457e6),
            ("CurrentLiabilitiesTotal", "Total current liabilities", 35_228e6),
            ("NonCurrentLiabilitiesTotal", "Total noncurrent liabilities", 50_713e6),
            ("AllEquityBalance", "Total equity", 26_535e6),
            ("LiabilitiesAndEquity", "Total liabilities and equity", 112_476e6),
        )
        self.assertEqual(valuation._total_liabilities(frame, [2025])[2025], 85_941)

    def test_liabilities_and_equity_minus_equity_otherwise(self):
        # McDonald's shape: no liability totals beyond current, and a deficit;
        # the first line containing "liabilities" (accrued interest) is not the total.
        frame = balance(
            ("Assets", "Total assets", 59_515e6),
            ("OtherNonOperatingCurrentLiabilities", "Accrued interest", 533e6),
            ("CurrentLiabilitiesTotal", "Total current liabilities", 4_361e6),
            ("AllEquityBalance", "Total shareholders' equity (deficit)", -1_790e6),
            ("LiabilitiesAndEquity", "Total liabilities and shareholders' equity (deficit)", 59_515e6),
        )
        self.assertEqual(valuation._total_liabilities(frame, [2025])[2025], 61_305)

    def test_assets_minus_equity_without_a_liabilities_and_equity_line(self):
        frame = balance(
            ("Assets", "Total assets", 1_000e6),
            ("AllEquityBalance", "Total equity", 400e6),
        )
        self.assertEqual(valuation._total_liabilities(frame, [2025])[2025], 600)

    def test_no_totals_gives_nan_not_a_random_liability_line(self):
        frame = balance(("OtherNonOperatingCurrentLiabilities", "Accrued interest", 533e6))
        self.assertTrue(pd.isna(valuation._total_liabilities(frame, [2025])[2025]))
