"""Form 4 parsing in insider.py. No network: filings are small fakes.

Run from the repo root:  python -m unittest discover tests
"""

import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import insider  # noqa: E402

RANGE_NOTE = ("The price reported in Column 4 is a weighted average price. These shares were sold in "
              "multiple transactions at prices ranging from $600.00 to $602.00, inclusive.")


def ownership(rows, notes=None, issuer_cik="0000000001", name="Jane Doe"):
    return SimpleNamespace(market_trades=pd.DataFrame(rows), footnotes=notes or {}, insider_name=name,
                           position="Director", issuer=SimpleNamespace(cik=issuer_cik))


def trade(**overrides):
    row = {"Date": "2026-08-13", "Code": "S", "Shares": 100.0, "Price": 50.0, "footnotes": ""}
    return {**row, **overrides}


class RowTests(unittest.TestCase):
    def test_reported_price_gives_the_value(self):
        [row] = insider._rows_from_ownership(ownership([trade()]))
        self.assertEqual(row["value"], 5000.0)
        self.assertFalse(row["value_estimated"])

    def test_zero_price_falls_back_to_the_footnote_range(self):
        # Weighted-average sales often report 0 and give the range only in a footnote.
        [row] = insider._rows_from_ownership(ownership([trade(Price=0, footnotes="F1")], {"F1": RANGE_NOTE}))
        self.assertEqual(row["value"], 60100.0)          # 100 shares x midpoint $601
        self.assertTrue(row["value_estimated"])

    def test_footnote_wording_and_id_separators_vary(self):
        note = "Prices ranged from $1,000.50 to $1,001.50, inclusive."
        [row] = insider._rows_from_ownership(
            ownership([trade(Price=None, footnotes="F2\nF3")], {"F2": "Held by a trust.", "F3": note}))
        self.assertEqual(row["value"], 100100.0)

    def test_no_price_anywhere_stays_empty(self):
        [row] = insider._rows_from_ownership(
            ownership([trade(Price=0, footnotes="F1")], {"F1": "Sold under a 10b5-1 plan adopted in 2025."}))
        self.assertIsNone(row["value"])
        self.assertFalse(row["value_estimated"])


class IssuerTests(unittest.TestCase):
    def collect(self, filings):
        company = unittest.mock.Mock(cik=1)
        company.get_filings.return_value = [
            SimpleNamespace(filing_date="2099-01-01", accession_no=str(i), obj=lambda o=o: o)
            for i, o in enumerate(filings)]
        with unittest.mock.patch.object(insider, "Company", return_value=company):
            return insider._collect("ACME", 36)[0]

    def test_filings_about_another_issuer_are_skipped(self):
        # A company that is itself the insider (a >10% holder or acquirer of
        # another issuer) files Form 4s about that other company's shares.
        own = ownership([trade(Date="2099-01-01")], issuer_cik="0000000001")
        other = ownership([trade(Date="2099-01-01", Code="P")], issuer_cik="0000000002", name="ACME CORP")
        rows = self.collect([own, other])
        self.assertEqual([r["name"] for r in rows], ["Jane Doe"])

    def test_unreadable_issuer_is_kept(self):
        own = ownership([trade(Date="2099-01-01")])
        own.issuer = None
        self.assertEqual(len(self.collect([own])), 1)


if __name__ == "__main__":
    unittest.main()
