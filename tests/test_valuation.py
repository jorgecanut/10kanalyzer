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
        # With its diluted EPS: without one the P/E falls back to net income, and says so.
        df, statements, frame = company()
        frame = pd.concat([frame, pd.DataFrame([{"concept": "us-gaap:EarningsPerShareDiluted", "fiscal_year": y,
                                                 "value": 1.0, "period_start": f"{y}-01-01",
                                                 "period_end": f"{y}-12-31", "period_instant": None,
                                                 "is_dimensioned": False} for y in (2024, 2025)])])
        out, issues = evaluate(df, statements, frame, splits=[])
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


# --------------------------------------------------------------------------- #
# Diluted EPS, quarter rows and today's record.
# --------------------------------------------------------------------------- #
def eps_fact(year, value, start=None, end=None):
    return {"concept": "us-gaap:EarningsPerShareDiluted", "fiscal_year": year, "value": value,
            "period_instant": None, "period_start": start or f"{year}-01-01", "period_end": end or f"{year}-12-31",
            "is_dimensioned": False, "currency": "USD", "unit": "usdPerShare"}


def with_eps(eps=(1.0, 1.0)):
    df, statements, frame = company()
    frame = pd.concat([frame, pd.DataFrame([eps_fact(2024, eps[0]), eps_fact(2025, eps[1])])], ignore_index=True)
    return df, statements, frame


def quarter_inputs():
    """One quarter slot after FY2025: TTM rows, its statements and facts, as quarters.build gives them."""
    q = pd.DataFrame([{"slot": "Q2'26", "label": "TTM Q2'26", "period_end": "2026-06-30", "fiscal_year": 2026,
                       "quarter": 2, "period_of_report": "2026-06-30", "currency": "USD",
                       "Revenues": 1100.0, "NetIncomeLoss": 120.0, "OperatingIncomeLoss": 170.0,
                       "OperatingCashFlow": 170.0, "CapEx": 40.0, "FreeCashFlow": 130.0, "GrossProfit": 440.0,
                       "StockholdersEquity": 520.0, "LongTermDebt": 200.0, "AssetsCurrent": 310.0,
                       "LiabilitiesCurrent": 150.0, "EpsDiluted": 1.2,
                       "EpsDiluted__ytd": 0.7, "EpsDiluted__ytd_prior": 0.5}])
    st = pd.DataFrame([
        {**line("balance", "us-gaap_Assets", "Total assets", 1000e6, standard="Assets"), "fiscal_year": "Q2'26",
         "period_of_report": "2026-06-30"},
        {**line("balance", "us-gaap_Liabilities", "Total liabilities", 500e6, standard="Liabilities"),
         "fiscal_year": "Q2'26", "period_of_report": "2026-06-30"},
        {**line("balance", "us-gaap_RetainedEarningsAccumulatedDeficit", "Retained earnings", 300e6),
         "fiscal_year": "Q2'26", "period_of_report": "2026-06-30"},
        {**line("balance", "us-gaap_CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents", 50e6),
         "fiscal_year": "Q2'26", "period_of_report": "2026-06-30"},
        {**line("cashflow", "us-gaap_DepreciationDepletionAndAmortization", "Depreciation and amortization", 30e6),
         "fiscal_year": "Q2'26", "period_of_report": "2026-06-30"}])
    fa = pd.DataFrame([{"concept": "dei:EntityCommonStockSharesOutstanding", "fiscal_year": "Q2'26", "value": 100e6,
                        "period_instant": "2026-07-20", "period_end": None, "is_dimensioned": False}])
    return q, st, fa


PRICES_Q = PRICES + [{"date": "2026-06-01", "close": 18.0}]


class DilutedEpsTests(unittest.TestCase):
    def test_pe_uses_diluted_eps(self):
        out, issues = evaluate(*with_eps((1.0, 0.8)), splits=[])
        self.assertAlmostEqual(out.loc[2025, "pe"], 10.0 / 0.8)
        self.assertEqual(out.loc[2025, "pe_basis"], "diluted_eps")
        self.assertAlmostEqual(out.loc[2025, "eps_diluted"], 0.8)
        self.assertEqual(kinds(issues, "EarningsPerShareDiluted"), [])

    def test_pe_falls_back_and_reports(self):
        out, issues = evaluate(*company(), splits=[])
        self.assertAlmostEqual(out.loc[2025, "pe"], 10.0 * 100e6 / 1e6 / 100.0)     # market cap / net income
        self.assertEqual(out.loc[2025, "pe_basis"], "net_income")
        self.assertEqual(kinds(issues, "EarningsPerShareDiluted"),
                         [("valuation_assumed", 2024), ("valuation_assumed", 2025)])

    def test_eps_follows_a_split(self):
        # A 2-for-1 split after FY2024: Yahoo's 2024 price is halved, so is its EPS.
        df, statements, frame = with_eps((2.0, 1.0))
        frame.loc[(frame["concept"] == "us-gaap:CommonStockSharesOutstanding") & (frame["fiscal_year"] == 2024),
                  "value"] = 50e6
        out, _ = evaluate(df, statements, frame, splits=[{"date": "2025-06-01", "ratio": 2.0}])
        self.assertAlmostEqual(out.loc[2024, "eps_diluted"], 1.0)
        self.assertAlmostEqual(out.loc[2024, "pe"], 20.0)


class QuarterRowTests(unittest.TestCase):
    def evaluate_q(self, prices=PRICES_Q):
        q, st, fa = quarter_inputs()
        df, statements, frame = with_eps()
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, prices, issues=issues, splits=[],
                                         quarters=q, quarter_statements=st, quarter_facts=fa)
        return out, issues

    def test_quarter_rows_after_years(self):
        out, issues = self.evaluate_q()
        self.assertEqual(list(out["fiscal_year"]), [2024, 2025, "Q2'26"])
        slot = out.iloc[-1]
        self.assertEqual(slot["period_end"], "2026-06-30")
        self.assertAlmostEqual(slot["price"], 18.0)
        self.assertAlmostEqual(slot["pe"], 18.0 / 1.2)
        self.assertAlmostEqual(slot["ev_ebit"], (18.0 * 100 + 200 - 50) / 170.0)
        self.assertTrue(pd.isna(slot["f_score"]))                    # two full years only
        self.assertTrue(pd.isna(slot["pe_percentile"]))
        self.assertEqual([i for i in issues if i["fiscal_year"] == "Q2'26"], [])

    def test_pe_percentile_vs_fiscal_years_only(self):
        out, _ = self.evaluate_q()
        years = out[out["fiscal_year"].apply(lambda v: isinstance(v, int))]
        self.assertEqual(float(out.iloc[0]["pe_median_hist"]), float(pd.to_numeric(years["pe"]).median()))


class TodayTests(unittest.TestCase):
    def setUp(self):
        q, st, fa = quarter_inputs()
        df, statements, frame = with_eps()
        self.evaluation = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[],
                                                     quarters=q, quarter_statements=st, quarter_facts=fa)
        self.fy_only = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[])

    def test_today_uses_quote_and_ttm(self):
        issues = []
        today = valuation.build_today(self.evaluation, {"price": 24.0, "time": "2026-11-18T15:10:00Z",
                                                        "currency": "USD"}, (100e6, "2026-07-20"), PRICES_Q, issues)
        self.assertAlmostEqual(today["pe"], 24.0 / 1.2)
        self.assertEqual(today["earnings_period"], "TTM Q2'26")
        self.assertEqual(today["price_source"], "quote")
        self.assertEqual(today["price_time"], "2026-11-18T15:10:00Z")
        ev = 24.0 * 100 + 200 - 50
        self.assertAlmostEqual(today["ev_ebit"], ev / 170.0)
        self.assertAlmostEqual(today["earnings_yield"], 170.0 / ev)
        self.assertAlmostEqual(today["fcf_yield"], 130.0 / ev)
        self.assertEqual(today["f_score_year"], 2025)
        self.assertEqual(today["shares_date"], "2026-07-20")
        self.assertEqual(issues, [])
        # A percentile needs three fiscal-year P/Es, as for the years themselves; there are two.
        self.assertIsNone(today["pe_percentile"])

    def test_today_without_quote(self):
        issues = []
        today = valuation.build_today(self.evaluation, None, (100e6, "2026-07-20"), PRICES_Q, issues)
        self.assertEqual(today["price_source"], "month_close")
        self.assertAlmostEqual(today["price"], 18.0)
        self.assertEqual(today["price_time"], "2026-06-01")
        self.assertEqual([i["kind"] for i in issues], ["valuation_assumed"])
        self.assertEqual(issues[0]["concept"], "Price")

    def test_today_no_quarters_uses_fy(self):
        today = valuation.build_today(self.fy_only, {"price": 24.0, "time": "t", "currency": "USD"}, None,
                                      PRICES_Q, [])
        self.assertEqual(today["earnings_period"], "FY2025")
        self.assertAlmostEqual(today["pe"], 24.0 / 1.0)

    def test_latest_shares_from_the_newest_cover_page(self):
        _, _, fa = quarter_inputs()
        older = pd.DataFrame([{"concept": "dei:EntityCommonStockSharesOutstanding", "fiscal_year": 2025,
                               "value": 99e6, "period_instant": "2026-03-10", "period_end": None,
                               "is_dimensioned": False}])
        self.assertEqual(valuation.latest_shares(pd.concat([older, fa])), (100e6, "2026-07-20"))
        self.assertIsNone(valuation.latest_shares(pd.DataFrame()))


class ReviewFixTests(unittest.TestCase):
    """Splits and currencies around the quarter slots and today's record (final review)."""

    def slot_inputs(self, eps_ytd=1.3, eps_prior=0.33, slot_shares=1000e6):
        q, st, fa = quarter_inputs()
        q = q.drop(columns=["EpsDiluted"]).assign(**{"EpsDiluted__ytd": eps_ytd, "EpsDiluted__ytd_prior": eps_prior})
        fa.loc[:, "value"] = slot_shares
        return q, st, fa

    def test_split_between_10k_and_10q_in_ttm_eps(self):
        # FY2025 EPS 11.93 on 100M shares; a 10:1 split on 2026-03-01; the 10-Q (1000M shares)
        # reports YTD 1.3 and a restated prior YTD 0.33. TTM on the new basis: 1.193 + 1.3 - 0.33.
        df, statements, frame = with_eps((10.0, 11.93))
        q, st, fa = self.slot_inputs()
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[
            {"date": "2026-03-01", "ratio": 10.0}], quarters=q, quarter_statements=st, quarter_facts=fa)
        slot = out.iloc[-1]
        self.assertAlmostEqual(slot["eps_diluted"], 1.193 + 1.3 - 0.33)
        self.assertAlmostEqual(out.iloc[1]["eps_diluted"], 1.193)            # FY2025 on today's basis too

    def test_today_shares_follow_a_later_split(self):
        q, st, fa = quarter_inputs()
        df, statements, frame = with_eps()
        evaluation = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[],
                                                quarters=q, quarter_statements=st, quarter_facts=fa)
        today = valuation.build_today(evaluation, {"price": 12.0, "time": "t"}, (100e6, "2026-07-20"), PRICES_Q,
                                      [], splits=[{"date": "2026-10-01", "ratio": 2.0}])
        self.assertAlmostEqual(today["shares"], 200e6)
        self.assertAlmostEqual(today["market_cap"], 12.0 * 200e6 / 1e6)

    def test_no_share_count_eps_still_follows_splits_and_the_issue_is_true(self):
        df, statements, frame = with_eps((2.0, 1.0))
        frame = frame[~((frame["concept"] == "us-gaap:CommonStockSharesOutstanding") & (frame["fiscal_year"] == 2024))]
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES, issues=issues,
                                         splits=[{"date": "2025-06-01", "ratio": 2.0}]).set_index("fiscal_year")
        self.assertAlmostEqual(out.loc[2024, "pe"], 20.0 / 1.0)               # EPS 2.0 / split 2
        shares = [i for i in issues if i["concept"] == "Shares" and i["fiscal_year"] == 2024]
        self.assertTrue(shares and "P/E" in shares[0]["detail"] and "market cap" in shares[0]["detail"])

    def test_foreign_scale_reaches_slots_and_today(self):
        # A CAD reporter quoted in USD: 1 CAD = 0.75 USD, so a USD price x 1/0.75 is the CAD price.
        q, st, fa = quarter_inputs()
        df, statements, frame = with_eps()
        scale = {2024: 1 / 0.75, 2025: 1 / 0.75, "Q2'26": 1 / 0.75, "today": 1 / 0.75}
        issues = []
        evaluation = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[], issues=issues,
                                                price_scale=scale, quarters=q, quarter_statements=st,
                                                quarter_facts=fa)
        self.assertAlmostEqual(evaluation.iloc[-1]["pe"], 18.0 / 0.75 / 1.2)
        today = valuation.build_today(evaluation, {"price": 15.0, "time": "t"}, (100e6, "2026-07-20"), PRICES_Q,
                                      [], price_scale=scale)
        self.assertAlmostEqual(today["pe"], 15.0 / 0.75 / 1.2)

    def test_today_without_a_scale_for_a_foreign_quote_is_blank_and_said(self):
        q, st, fa = quarter_inputs()
        df, statements, frame = with_eps()
        evaluation = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, splits=[],
                                                price_scale={}, quarters=q, quarter_statements=st, quarter_facts=fa)
        issues = []
        today = valuation.build_today(evaluation, {"price": 15.0, "time": "t"}, None, PRICES_Q, issues,
                                      price_scale={})
        self.assertIsNone(today["pe"])
        self.assertIsNone(today["market_cap"])
        self.assertTrue(any(i["concept"] == "Price" for i in issues))


class EnterpriseValueDebtTests(unittest.TestCase):
    """EV = market cap + long-term debt + short-term debt + leases - cash (MSI's 10-Q shape)."""

    def build(self, extra_lines=(), extra_facts=(), **options):
        df, statements, frame = with_eps()
        statements = pd.concat([statements, pd.DataFrame(list(extra_lines))], ignore_index=True)
        frame = pd.concat([frame, pd.DataFrame(list(extra_facts))], ignore_index=True)
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES, issues=issues, splits=[], **options)
        return out.set_index("fiscal_year"), issues

    def test_short_term_debt_and_leases_are_counted(self):
        lines = [line("balance", "us-gaap_ShortTermBorrowings", "Short-term borrowings", 75e6, 2025, "ShortTermDebt"),
                 line("balance", "us-gaap_LongTermDebtCurrent", "Current portion of long-term debt", 0.0, 2025,
                      "CurrentPortionOfLongTermDebt"),
                 line("balance", "us-gaap_OperatingLeaseLiabilityNoncurrent", "Operating lease liabilities", 40e6,
                      2025, "OperatingLeaseNonCurrentDebtEquivalent")]
        # The lease total fact also covers the current part, which sits inside accrued liabilities.
        facts = [{"concept": "us-gaap:OperatingLeaseLiability", "fiscal_year": 2025, "value": 55e6,
                  "period_instant": "2025-12-31", "period_end": None, "is_dimensioned": False}]
        out, issues = self.build(lines, facts)
        row = out.loc[2025]
        self.assertAlmostEqual(row["debt_short"], 75.0)
        self.assertAlmostEqual(row["leases"], 55.0)
        self.assertAlmostEqual(row["enterprise_value"], 1000.0 + 200.0 + 75.0 + 55.0 - 50.0)
        self.assertEqual([i for i in issues if i["concept"] == "ShortTermDebt"], [])
        # 2024 has neither: EV as before.
        self.assertAlmostEqual(out.loc[2024, "enterprise_value"], 2000.0 + 200.0 - 50.0)

    def test_leases_can_be_left_out(self):
        facts = [{"concept": "us-gaap:OperatingLeaseLiability", "fiscal_year": 2025, "value": 55e6,
                  "period_instant": "2025-12-31", "period_end": None, "is_dimensioned": False}]
        original = valuation.EV_INCLUDES_LEASES
        valuation.EV_INCLUDES_LEASES = False
        try:
            out, _ = self.build((), facts)
        finally:
            valuation.EV_INCLUDES_LEASES = original
        self.assertAlmostEqual(out.loc[2025, "leases"], 55.0)                    # still shown
        self.assertAlmostEqual(out.loc[2025, "enterprise_value"], 1000.0 + 200.0 - 50.0)

    def test_a_company_tagged_short_term_line_is_counted_and_said(self):
        # TGT 2013-2015: its own tag for "Current portion of long-term debt and other borrowings".
        lines = [line("balance", "abc_DebtDueNextYear", "Current portion of long-term debt", 12e6, 2025)]
        out, issues = self.build(lines)
        self.assertAlmostEqual(out.loc[2025, "debt_short"], 12.0)
        said = [i for i in issues if i["concept"] == "ShortTermDebt" and i["fiscal_year"] == 2025]
        self.assertEqual(len(said), 1)
        self.assertIn("Current portion of long-term debt", said[0]["detail"])
        self.assertIn("by its label", said[0]["detail"])

    def test_a_standard_tag_that_is_not_debt_is_not_counted_by_label(self):
        lines = [line("balance", "us-gaap_AccountsPayableCurrent", "Accounts payable and borrowings", 9e6, 2025)]
        out, issues = self.build(lines)
        self.assertAlmostEqual(out.loc[2025, "debt_short"], 0.0)
        self.assertEqual([i for i in issues if i["concept"] == "ShortTermDebt"], [])

    def test_long_term_debt_comes_from_the_balance_sheet_lines(self):
        # financial_data's LongTermDebt (200) can be the us-gaap total that already includes the
        # current maturities (TGT FY2025); the sheet's own lines are 180 due later and 20 due within a year.
        lines = [line("balance", "us-gaap_LongTermDebtAndCapitalLeaseObligations", "Long-term debt", 180e6, 2025,
                      "LongTermDebt"),
                 line("balance", "us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent", "Current portion", 20e6,
                      2025, "CurrentPortionOfLongTermDebt")]
        out, _ = self.build(lines)
        self.assertAlmostEqual(out.loc[2025, "debt_long"], 180.0)
        self.assertAlmostEqual(out.loc[2025, "debt_short"], 20.0)
        self.assertAlmostEqual(out.loc[2025, "enterprise_value"], 1000.0 + 180.0 + 20.0 - 50.0)
        self.assertAlmostEqual(out.loc[2024, "debt_long"], 200.0)      # no line on the sheet: financial_data's

    def test_a_long_term_line_is_not_taken_for_short_term_debt(self):
        lines = [line("balance", "abc_TermLoanNoncurrent", "Long-term borrowings, net of current portion", 30e6, 2025)]
        _, issues = self.build(lines)
        self.assertEqual([i for i in issues if i["concept"] == "ShortTermDebt"], [])


class CashAndNegativeEvTests(unittest.TestCase):
    """A biotech's cash is mostly marketable securities (MGX: 41.7M cash, 119.1M securities)."""

    def build(self, extra_lines=()):
        df, statements, frame = with_eps()
        statements = pd.concat([statements, pd.DataFrame(list(extra_lines))], ignore_index=True)
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES, issues=issues, splits=[])
        return out.set_index("fiscal_year"), issues

    def test_short_term_investments_count_as_cash(self):
        lines = [line("balance", "us-gaap_AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                      "Available-for-sale marketable securities", 100e6, 2025, "ShortTermInvestments")]
        out, _ = self.build(lines)
        self.assertAlmostEqual(out.loc[2025, "investments"], 100.0)
        self.assertAlmostEqual(out.loc[2025, "enterprise_value"], 1000.0 + 200.0 - 50.0 - 100.0)

    def test_negative_ev_leaves_its_ratios_blank_and_says_why(self):
        lines = [line("balance", "us-gaap_ShortTermInvestments", "Short-term investments", 1500e6, 2025,
                      "ShortTermInvestments")]
        out, issues = self.build(lines)
        row = out.loc[2025]
        self.assertLess(row["enterprise_value"], 0)
        for column in ("ev_ebit", "ev_ebitda", "earnings_yield", "fcf_yield"):
            self.assertTrue(pd.isna(row[column]), column)
        said = [i for i in issues if i["fiscal_year"] == 2025 and i["concept"] == "EnterpriseValue"]
        self.assertEqual(len(said), 1)
        self.assertIn("cash", said[0]["detail"])

    def test_today_negative_ev_is_blank_and_said(self):
        lines = [line("balance", "us-gaap_ShortTermInvestments", "Short-term investments", 1500e6, 2025,
                      "ShortTermInvestments")]
        out, _ = self.build(lines)
        issues = []
        today = valuation.build_today(out.reset_index(), {"price": 10.0, "time": "t"}, None, PRICES, issues)
        self.assertIsNone(today["earnings_yield"])
        self.assertIsNone(today["fcf_yield"])
        self.assertIsNone(today["ev_ebit"])
        self.assertTrue(any(i["concept"] == "EnterpriseValue" for i in issues))


def year_fact(concept, year, value):
    return {"concept": concept, "fiscal_year": year, "value": value, "period_instant": None,
            "period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "is_dimensioned": False}


class RoundedEpsTests(unittest.TestCase):
    """EPS is rounded to the cent: at a few cents that moves the P/E by tens of percent."""

    def build(self, eps, ni_common=None, shares=100e6, net_income=1.2):
        df, statements, frame = company()
        df["NetIncomeLoss"] = net_income
        rows = [year_fact("us-gaap:EarningsPerShareDiluted", y, eps) for y in (2024, 2025)]
        rows += [year_fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", y, shares) for y in (2024, 2025)]
        if ni_common is not None:
            rows += [year_fact("us-gaap:NetIncomeLossAvailableToCommonStockholdersDiluted", y, ni_common)
                     for y in (2024, 2025)]
        frame = pd.concat([frame, pd.DataFrame(rows)], ignore_index=True)
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES, issues=issues, splits=[])
        return out.set_index("fiscal_year"), issues

    def test_a_few_cents_is_recomputed_from_net_income(self):
        out, issues = self.build(0.01)                         # 1.2M / 100M shares = 0.012
        self.assertAlmostEqual(out.loc[2025, "eps_diluted"], 0.012)
        self.assertAlmostEqual(out.loc[2025, "pe"], 10.0 / 0.012)
        said = [i for i in issues if i["concept"] == "EarningsPerShareDiluted" and i["fiscal_year"] == 2025]
        self.assertTrue(said and "rounded to the cent" in said[0]["detail"])

    def test_net_income_to_common_comes_first(self):
        out, _ = self.build(-0.01, ni_common=-0.6e6, net_income=-0.1)
        self.assertAlmostEqual(out.loc[2025, "eps_diluted"], -0.006)

    def test_a_large_eps_is_kept(self):
        out, issues = self.build(8.13, net_income=813.0)
        self.assertAlmostEqual(out.loc[2025, "eps_diluted"], 8.13)
        self.assertEqual([i for i in issues if i["concept"] == "EarningsPerShareDiluted"], [])

    def test_slot_ttm_eps_from_net_income_when_its_parts_are_cents(self):
        df, statements, frame = with_eps((1.0, -0.03))
        q, st, fa = quarter_inputs()
        q = q.assign(**{"EpsDiluted__ytd": 0.01, "EpsDiluted__ytd_prior": -0.22, "NetIncomeLoss": 12.5,
                        "DilutedShares__ytd": 78.0e6})
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, issues=issues, splits=[],
                                         quarters=q, quarter_statements=st, quarter_facts=fa)
        self.assertAlmostEqual(out.iloc[-1]["eps_diluted"], 12.5e6 / 78.0e6)
        self.assertTrue(any(i["fiscal_year"] == "Q2'26" and "rounded to the cent" in i["detail"] for i in issues))


class NonOperatingProfitTests(unittest.TestCase):
    def test_profit_with_an_operating_loss_is_flagged(self):
        q, st, fa = quarter_inputs()
        q = q.assign(NetIncomeLoss=12.5, OperatingIncomeLoss=-3.8)
        df, statements, frame = with_eps()
        issues = []
        out = valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, issues=issues, splits=[],
                                         quarters=q, quarter_statements=st, quarter_facts=fa)
        flagged = [i for i in issues if i["kind"] == "pe_non_operating"]
        self.assertEqual([i["fiscal_year"] for i in flagged], ["Q2'26"])
        today = valuation.build_today(out, {"price": 20.0, "time": "t"}, None, PRICES_Q, [])
        self.assertIn("non-operating", today["pe_note"])

    def test_profit_well_above_operating_income_is_flagged(self):
        q, st, fa = quarter_inputs()
        q = q.assign(NetIncomeLoss=30.0, OperatingIncomeLoss=10.0)
        df, statements, frame = with_eps()
        issues = []
        valuation.build_evaluation("TEST", df, statements, frame, PRICES_Q, issues=issues, splits=[],
                                   quarters=q, quarter_statements=st, quarter_facts=fa)
        self.assertEqual(len([i for i in issues if i["kind"] == "pe_non_operating"]), 1)

    def test_an_ordinary_year_is_not(self):
        out, issues = evaluate(*with_eps(), splits=[])          # NI 100 against EBIT 150
        self.assertEqual([i for i in issues if i["kind"] == "pe_non_operating"], [])
        today = valuation.build_today(out.reset_index(), {"price": 20.0, "time": "t"}, None, PRICES, [])
        self.assertIsNone(today["pe_note"])
