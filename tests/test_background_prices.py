"""Yahoo's prices are fetched while the filings are read; nothing is fetched twice. No network.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import time
import unittest
import unittest.mock
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edgar_dashboard  # noqa: E402
import valuation  # noqa: E402

QUOTE = {"price": 10.0, "currency": "USD"}


class BackgroundPricesTest(unittest.TestCase):
    def test_history_splits_and_quote_come_from_the_background(self):
        def slow_history(ticker):
            time.sleep(0.05)
            return [{"date": "2025-12-01", "close": 10.0}]

        with unittest.mock.patch.object(valuation, "fetch_price_history", side_effect=slow_history), \
                unittest.mock.patch.object(valuation, "fetch_splits", return_value=[]), \
                unittest.mock.patch.object(valuation, "fetch_quote", return_value=QUOTE) as quote:
            fetched = edgar_dashboard._prices(edgar_dashboard._start_prices("ACME"))
        self.assertEqual(fetched, {"history": [{"date": "2025-12-01", "close": 10.0}], "splits": [], "quote": QUOTE})
        self.assertEqual(quote.call_count, 1)

    def test_what_failed_is_left_out_so_the_valuation_fetches_it_again(self):
        with unittest.mock.patch.object(valuation, "fetch_price_history", return_value=[]), \
                unittest.mock.patch.object(valuation, "fetch_splits", side_effect=RuntimeError("down")):
            fetched = edgar_dashboard._prices(edgar_dashboard._start_prices("ACME"))
        self.assertEqual(fetched, {"history": []})

    def test_market_scale_uses_the_quote_it_is_given(self):
        data = pd.DataFrame({"fiscal_year": [2025], "currency": ["USD"], "ads": [False]})
        with unittest.mock.patch.object(valuation, "fetch_quote", side_effect=AssertionError("fetched twice")):
            scale, currency, _ = valuation.market_scale("KO", data, pd.DataFrame(), pd.DataFrame(), quote=QUOTE)
        self.assertEqual((scale, currency), (None, "USD"))


if __name__ == "__main__":
    unittest.main()
