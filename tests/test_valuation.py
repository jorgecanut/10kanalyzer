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
