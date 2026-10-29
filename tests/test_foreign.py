"""Foreign filers (20-F / 40-F, IFRS): currency, ADS ratio, IFRS tags, valuation. No network.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import charts  # noqa: E402
import extract  # noqa: E402
import valuation  # noqa: E402

PERIOD = "2025-12-31"


def fact(concept, value, *, currency="TWD", unit=None, instant=None, start="2025-01-01", end=PERIOD,
         member=None, text=None, decimals=-6):
    return {"concept": concept, "numeric_value": value, "value": text if text is not None else value,
            "currency": currency, "unit_ref": unit or (currency.lower() if currency else "shares"),
            "is_dimensioned": member is not None, "dimension": "x:Axis" if member else None, "member": member,
            "period_instant": instant, "period_start": None if instant else start,
            "period_end": None if instant else end, "decimals": decimals}


class CurrencyTests(unittest.TestCase):
    def test_reporting_currency_is_the_most_used_money_unit(self):
        facts = pd.DataFrame([fact("ifrs-full:Revenue", 1.0), fact("ifrs-full:CostOfSales", 1.0),
                              fact("ifrs-full:Revenue", 0.03, currency="USD"),
                              fact("ifrs-full:BasicEarningsLossPerShare", 2.0, currency="USD", unit="usdPerShare"),
                              fact("ifrs-full:BasicEarningsLossPerShare", 2.0, currency="USD", unit="usdPerShare")])
        self.assertEqual(extract.reporting_currency(facts), "TWD")

    def test_a_usd_convenience_copy_is_not_taken_for_the_metric(self):
        # TSMC tags this year's revenue in TWD and again, translated, in USD.
        facts = pd.DataFrame([fact("ifrs-full:Revenue", 120e9, currency="USD", decimals=-5),
                              fact("ifrs-full:Revenue", 3.8e12)])
        value = extract.extract_metric(facts, ["Revenue"], PERIOD, "duration", currency="TWD")
        self.assertEqual(value, 3.8e12)

    def test_sga_is_the_sum_of_its_ifrs_halves(self):
        facts = pd.DataFrame([fact("ifrs-full:DistributionCosts", 10e9), fact("ifrs-full:AdministrativeExpense", 5e9)])
        groups = extract.COMPONENT_METRICS["SellingGeneralAndAdministrativeExpense"]
        value = extract._component_metric(facts, groups, PERIOD, "duration", {}, "TWD")
        self.assertEqual(value, 15e9)

    def test_ifrs_long_term_debt_without_a_total_is_bonds_plus_loans(self):
        facts = pd.DataFrame([fact("ifrs-full:NoncurrentPortionOfNoncurrentBondsIssued", 856e9, instant=PERIOD),
                              fact("ifrs-full:LongtermBorrowings", 40e9, instant=PERIOD)])
        groups = extract.COMPONENT_METRICS["LongTermDebt"]
        self.assertEqual(extract._component_metric(facts, groups, PERIOD, "instant", {}, "TWD"), 896e9)


class AdsRatioTests(unittest.TestCase):
    def ratio(self, rows, currency="EUR"):
        return extract.ads_ratio(pd.DataFrame(rows), currency)

    def test_cover_page_text(self):
        rows = [fact("dei:Security12bTitle", None, currency=None, member="sap:AmericanDepositarySharesMember",
                     text="American Depositary Shares, each Representing one Ordinary Share, without nominal value")]
        self.assertEqual(self.ratio(rows), (1.0, "cover", True))
        rows[0]["value"] = "American Depositary Shares, each representing 1/2 of one ordinary share"
        self.assertEqual(self.ratio(rows)[0], 0.5)

    def test_eps_per_ads_over_eps_per_share(self):
        # TSMC: 10.44 USD per ADS against 2.09 USD per share, so 5 shares per ADS.
        rows = [fact("ifrs-full:BasicEarningsLossPerShare", 2.09, currency="USD", unit="usdPerShare"),
                fact("ifrs-full:BasicEarningsLossPerShare", 10.44, currency="USD", unit="usdPerShare",
                     member="tsm:AmericanDepositarySharesMember")]
        self.assertEqual(self.ratio(rows, "USD"), (5.0, "eps", True))

    def test_adr_listing_without_a_ratio(self):
        rows = [fact("dei:Security12bTitle", None, currency=None, member="dei:AdrMember",
                     text="American Depositary Shares")]
        self.assertEqual(self.ratio(rows), (None, "", True))

    def test_preferred_depositary_shares_are_not_adrs(self):
        rows = [fact("dei:Security12bTitle", None, currency=None, member="jpm:DepositarySharesSeriesDdMember",
                     text="Depositary Shares, each representing a one-four hundredth interest in a share of "
                          "5.75% Non-Cumulative Preferred Stock, Series DD")]
        self.assertEqual(self.ratio(rows, "USD"), (None, "", False))

    def test_adrs_of_preferred_shares_still_count(self):
        # Itau: each ADS is one preferred share; Shell misspells its member.
        rows = [fact("dei:Security12bTitle", None, currency=None, member="itub:AdsMember",
                     text="American Depositary Shares, each representing one Preferred Share")]
        self.assertEqual(self.ratio(rows, "BRL"), (1.0, "cover", True))
        rows = [fact("dei:Security12bTitle", None, currency=None, member="shel:AmericanDepositorySharesMember",
                     text="American Depository Shares")]
        self.assertEqual(self.ratio(rows, "USD"), (None, "", True))

    def test_capex_from_the_one_clear_investing_line(self):
        def line(concept, value, parent="ifrs-full_CashFlowsFromUsedInInvestingActivities"):
            return {"statement": "cashflow", "concept": concept, "value": value, "parent_concept": parent}
        toyota = [line("tm_PurchaseOfPropertyPlantAndEquipmentExcludingEquipmentLeasedToOthers", -2.1e12),
                  line("tm_PurchaseOfEquipmentLeasedToOthersClassifiedAsInvestingActivities", -2.8e12),
                  line("tm_ProceedsFromSalesOfPropertyPlantAndEquipmentExcludingEquipmentLeasedToOthers", 3e10)]
        self.assertEqual(extract._capex_line(toyota), 2.1e12)
        nextera = [line("nee_CapitalExpendituresOfFPL", -8.7e9, "us-gaap_NetCashProvidedByUsedInInvestingActivities"),
                   line("nee_OtherCapitalExpenditures", -2e6, "us-gaap_NetCashProvidedByUsedInInvestingActivities")]
        self.assertTrue(np.isnan(extract._capex_line(nextera)))   # one per subsidiary: ambiguous

    def test_ordinary_listing(self):
        rows = [fact("dei:Security12bTitle", None, currency=None, text="Common Stock")]
        self.assertEqual(self.ratio(rows), (None, "", False))

    def test_snap_ratio(self):
        self.assertEqual(valuation.snap_ratio(4.995), 5.0)
        self.assertEqual(valuation.snap_ratio(0.49), 0.5)
        self.assertIsNone(valuation.snap_ratio(7.0))        # nothing nice within 3%
        self.assertEqual(valuation.snap_ratio(11.5, tolerance=0.25, ratios=valuation.COMMON_RATIOS), 10.0)
        self.assertIsNone(valuation.snap_ratio(float("nan")))


class ValuationTests(unittest.TestCase):
    def data(self):
        return pd.DataFrame({
            "fiscal_year": [2024, 2025], "period_of_report": ["2024-12-31", PERIOD],
            "currency": ["TWD", "TWD"], "ads": [True, True], "ads_ratio": [5.0, 5.0],
            "ads_ratio_source": ["eps", "eps"],
            "Revenues": [2_900_000.0, 3_800_000.0], "NetIncomeLoss": [1_160_000.0, 1_700_000.0],
            "StockholdersEquity": [4_000_000.0, 5_400_000.0],
        })

    def facts(self):
        return pd.DataFrame([
            {"concept": "ifrs-full:NumberOfSharesOutstanding", "fiscal_year": year, "value": 25.93e9,
             "period_instant": f"{year}-12-31", "period_end": None, "is_dimensioned": False}
            for year in (2024, 2025)])

    def test_price_per_ads_becomes_price_per_share_in_the_reporting_currency(self):
        history = [{"date": "2024-12-31", "close": 197.5}, {"date": PERIOD, "close": 303.9}]
        fx = [{"date": "2024-12-31", "close": 0.0305}, {"date": PERIOD, "close": 0.0318}]   # USD per TWD
        with unittest.mock.patch.object(valuation, "fetch_quote", return_value={"price": 1, "currency": "USD"}), \
             unittest.mock.patch.object(valuation, "fetch_fx_history", return_value=fx):
            scale, price_currency, note = valuation.market_scale("TSM", self.data(), self.facts(), pd.DataFrame())
        self.assertEqual(price_currency, "USD")
        self.assertIn("5 ordinary shares", note)
        self.assertAlmostEqual(scale[2025], 1 / (0.0318 * 5))
        evaluation = valuation.build_evaluation("TSM", self.data(), pd.DataFrame(), self.facts(), history,
                                                price_scale=scale, price_currency=price_currency)
        latest = evaluation.iloc[-1]
        # 303.9 USD/ADS = 1911 TWD per share; x 25.93e9 shares = 49.6T TWD.
        self.assertAlmostEqual(latest["market_cap"], 303.9 / 0.0318 / 5 * 25.93e9 / 1e6, places=0)
        self.assertAlmostEqual(latest["pe"], latest["market_cap"] / 1_700_000.0)
        self.assertEqual((latest["currency"], latest["price_currency"]), ("TWD", "USD"))
        self.assertEqual(latest["price"], 303.9)        # as quoted

    def test_unknown_ads_ratio_leaves_price_ratios_blank(self):
        data = self.data().drop(columns=["ads_ratio", "ads_ratio_source"])
        fx = [{"date": PERIOD, "close": 0.0318}]
        with unittest.mock.patch.object(valuation, "fetch_quote", return_value={"price": 1, "currency": "USD"}), \
             unittest.mock.patch.object(valuation, "fetch_fx_history", return_value=fx), \
             unittest.mock.patch.object(valuation, "_yahoo_summary", side_effect=OSError("offline")):
            scale, _, note = valuation.market_scale("TM", data, self.facts(), pd.DataFrame())
        self.assertEqual(scale, {})
        self.assertIn("unknown", note)
        evaluation = valuation.build_evaluation("TM", data, pd.DataFrame(), self.facts(),
                                                [{"date": PERIOD, "close": 200.0}], price_scale=scale)
        self.assertTrue(np.isnan(evaluation.iloc[-1]["pe"]))

    def test_ads_ratio_from_yahoo_market_cap(self):
        # Shell: 105.25 USD per ADS x 5.69B shares / 299B market cap = 2 shares per ADS.
        data = pd.DataFrame({"fiscal_year": [2025], "period_of_report": [PERIOD], "currency": ["USD"],
                             "ads": [True], "NetIncomeLoss": [17_000.0]})
        facts = pd.DataFrame([{"concept": "ifrs-full:NumberOfSharesOutstanding", "fiscal_year": 2025,
                               "value": 5.69e9, "period_instant": PERIOD, "period_end": None, "is_dimensioned": False}])
        summary = {"summaryDetail": {"marketCap": {"raw": 298.9e9}, "currency": "USD"}}
        with unittest.mock.patch.object(valuation, "fetch_quote", return_value={"price": 105.25, "currency": "USD"}), \
             unittest.mock.patch.object(valuation, "_yahoo_summary", return_value=summary):
            scale, _, note = valuation.market_scale("SHEL", data, facts, pd.DataFrame())
        self.assertIn("2 ordinary shares (Yahoo market cap)", note)
        self.assertAlmostEqual(scale[2025], 0.5)

    def test_us_filer_needs_no_scale(self):
        data = pd.DataFrame({"fiscal_year": [2025], "currency": ["USD"], "ads": [False]})
        with unittest.mock.patch.object(valuation, "fetch_quote", return_value={"price": 1, "currency": "USD"}):
            self.assertEqual(valuation.market_scale("KO", data, pd.DataFrame(), pd.DataFrame())[0], None)


class ChartTests(unittest.TestCase):
    def test_money_is_labelled_in_the_reporting_currency(self):
        df = pd.DataFrame({"fiscal_year": [2024, 2025], "currency": ["EUR", "EUR"],
                           "Revenues": [34_176.0, 36_800.0]})
        spec = charts.build_spec("SAP", "/nonexistent", df, pd.DataFrame(), pd.DataFrame())
        self.assertEqual(spec["currency"], "EUR")
        self.assertIn("· EUR", spec["charts"][0]["panels"][0]["subtitle"])
        self.assertEqual(charts.currency_symbol("TWD"), "NT$")
        self.assertEqual(charts.currency_symbol("XYZ"), "XYZ ")

    def test_statement_lines_match_ifrs_tags_without_a_standard_concept(self):
        statements = pd.DataFrame([{"statement": "income", "concept": "ifrs-full_FinanceCosts",
                                    "standard_concept": None, "label": "Finance costs", "value": 11.75e9,
                                    "fiscal_year": 2025}])
        series = charts._stmt_series(statements, "income", ["InterestExpense", "FinanceCosts"], [2025])
        self.assertEqual(series[2025], 11_750.0)


if __name__ == "__main__":
    unittest.main()
