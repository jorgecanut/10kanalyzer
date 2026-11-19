"""Lookups that prepare a frame once and reuse it: same answers, never another frame's.

``extract_metric`` (a filing's facts), ``valuation._statement_metric`` (the
statement lines) and ``valuation._close_near`` (a price history) keep what
they prepared per object; ``FilingView`` decodes each stored frame once and
hands out copies. No network.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dump  # noqa: E402
import extract  # noqa: E402
import filing_view  # noqa: E402
import valuation  # noqa: E402

PERIOD = "2025-12-31"


def fact(concept, value, start="2025-01-01", end=PERIOD, instant=None, decimals="-6", dimension=None,
         currency="USD"):
    return {"concept": concept, "numeric_value": value, "value": str(value), "currency": currency,
            "unit_ref": "usd", "is_dimensioned": dimension is not None, "dimension": dimension, "member": None,
            "period_start": start if instant is None else None, "period_end": end if instant is None else None,
            "period_instant": instant, "decimals": decimals, "label": concept.split(":")[-1]}


def metric(rows, concepts, period=PERIOD, period_type="duration", **kwargs):
    return extract.extract_metric(pd.DataFrame(rows), concepts, period, period_type, **kwargs)


class ExtractMetricRules(unittest.TestCase):
    def test_the_precise_copy_wins_and_ties_keep_the_filing_order(self):
        rows = [fact("us-gaap:Revenues", 1.0, decimals="-5"), fact("us-gaap:Revenues", 2.0, decimals="-3"),
                fact("us-gaap:Revenues", 3.0, decimals="-3")]
        self.assertEqual(metric(rows, ["Revenues"]), 2.0)

    def test_the_closest_period_end_wins(self):
        rows = [fact("us-gaap:Revenues", 1.0, start="2024-12-20", end="2025-12-20"),
                fact("us-gaap:Revenues", 2.0, start="2025-01-02", end="2025-12-30")]
        self.assertEqual(metric(rows, ["Revenues"]), 2.0)

    def test_a_span_counts_in_whole_days_at_both_bounds(self):
        # 2025-07-01 .. 2025-12-31 is 183 days.
        rows = [fact("us-gaap:Revenues", 5.0, start="2025-07-01")]
        self.assertEqual(metric(rows, ["Revenues"], min_days=183, max_days=183), 5.0)
        self.assertTrue(np.isnan(metric(rows, ["Revenues"], min_days=184)))
        self.assertTrue(np.isnan(metric(rows, ["Revenues"], min_days=100, max_days=182)))

    def test_a_duration_without_a_start_is_not_a_year(self):
        rows = [fact("us-gaap:Revenues", 5.0, start=None)]
        self.assertTrue(np.isnan(metric(rows, ["Revenues"])))

    def test_instants_and_the_scenario_fallback(self):
        rows = [fact("us-gaap:Assets", 7.0, instant=PERIOD),
                fact("us-gaap:Revenues", 9.0, dimension="us-gaap:StatementScenarioAxis"),
                fact("us-gaap:Revenues", 8.0, dimension="srt:ProductOrServiceAxis")]
        self.assertEqual(metric(rows, ["Assets"], period_type="instant"), 7.0)
        self.assertEqual(metric(rows, ["Revenues"]), 9.0)

    def test_another_currency_is_skipped(self):
        rows = [fact("us-gaap:Revenues", 1.0, currency="USD"), fact("us-gaap:Revenues", 2.0, currency="TWD")]
        self.assertEqual(metric(rows, ["Revenues"], currency="TWD"), 2.0)
        self.assertEqual(metric(rows, ["Revenues"], currency="USD"), 1.0)

    def test_each_frame_answers_for_itself(self):
        first = pd.DataFrame([fact("us-gaap:Revenues", 1.0)])
        second = pd.DataFrame([fact("us-gaap:Revenues", 2.0)])
        for _ in range(2):
            self.assertEqual(extract.extract_metric(first, ["Revenues"], PERIOD, "duration"), 1.0)
            self.assertEqual(extract.extract_metric(second, ["Revenues"], PERIOD, "duration"), 2.0)
        # A frame made after another was freed (its id may be reused) is prepared anew.
        for value in (3.0, 4.0, 5.0):
            frame = pd.DataFrame([fact("us-gaap:Revenues", value)])
            self.assertEqual(extract.extract_metric(frame, ["Revenues"], PERIOD, "duration"), value)
            del frame


class FilingViewCopies(unittest.TestCase):
    def test_each_call_gets_its_own_copy(self):
        encoded = dump.encode_frame(pd.DataFrame([fact("us-gaap:Revenues", 1.0)]))
        frame = filing_view._Frame(encoded)
        first = frame.to_dataframe()
        first.loc[0, "numeric_value"] = 99.0
        first["extra"] = 1
        second = frame.to_dataframe()
        self.assertEqual(second.loc[0, "numeric_value"], 1.0)
        self.assertNotIn("extra", second.columns)
        pd.testing.assert_frame_equal(second, dump.decode_frame(encoded))


class ValuationLookups(unittest.TestCase):
    def test_statement_lines_of_each_frame(self):
        def lines(value):
            return pd.DataFrame([{"statement": "balance", "concept": "us-gaap_Assets", "standard_concept": "Assets",
                                  "label": "Total assets", "value": value, "fiscal_year": 2025}])
        first, second = lines(1e6), lines(2e6)
        for _ in range(2):
            self.assertEqual(valuation._statement_metric(first, [2025], ["Assets"], [])[2025], 1.0)
            self.assertEqual(valuation._statement_metric(second, [2025], ["Assets"], [])[2025], 2.0)

    def test_close_of_each_price_history(self):
        target = pd.Timestamp("2025-12-31")
        first = [{"date": "2025-12-01", "close": 10.0}]
        second = [{"date": "2025-12-01", "close": 20.0}, {"date": "2025-01-01", "close": 1.0}]
        for _ in range(2):
            self.assertEqual(valuation._close_near(first, target), 10.0)
            self.assertEqual(valuation._close_near(second, target), 20.0)
        self.assertTrue(np.isnan(valuation._close_near(first, pd.Timestamp("2025-06-30"))))


if __name__ == "__main__":
    unittest.main()
