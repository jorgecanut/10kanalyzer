"""Series helpers in charts.py. No network, no rendering.

Run from the repo root:  python -m unittest discover tests
"""

import sys
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


if __name__ == "__main__":
    unittest.main()
