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
