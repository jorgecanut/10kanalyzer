"""Filing ordering and the background insider step in edgar_dashboard.py. No network.

Run from the repo root:  python -m unittest discover tests
"""

import io
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edgar_dashboard  # noqa: E402
import insider  # noqa: E402


class _Filing:
    """A filing whose ``period_of_report`` fails the test if touched (it downloads the submission)."""

    def __init__(self, report_date, period=None):
        self.report_date = report_date
        self._period = period

    @property
    def period_of_report(self):
        if self._period is None:
            raise AssertionError("period_of_report downloads the whole filing")
        return self._period


class ReportDateTest(unittest.TestCase):
    def test_uses_index_date_without_downloading(self):
        self.assertEqual(edgar_dashboard._report_date(_Filing("2025-12-31")), "2025-12-31")

    def test_falls_back_to_period_of_report_when_index_has_no_date(self):
        self.assertEqual(edgar_dashboard._report_date(_Filing("", period="2024-06-30")), "2024-06-30")
        self.assertEqual(edgar_dashboard._report_date(_Filing(None, period="2024-06-30")), "2024-06-30")


class BackgroundInsidersTest(unittest.TestCase):
    def test_runs_while_the_caller_continues_and_reports_the_count(self):
        release = threading.Event()

        def slow_write(ticker, output_dir, cache_dir=None):
            release.wait(5)
            path = Path(output_dir) / "insider.json"
            path.write_text(json.dumps({"transactions": [{}, {}, {}]}), encoding="utf-8")
            return str(path)

        with tempfile.TemporaryDirectory() as folder, \
                unittest.mock.patch.object(insider, "write_insiders", slow_write):
            started = edgar_dashboard._start_insiders("TEST", folder)
            # Still running: the 10-K step would be going on here.
            self.assertTrue(started[0].is_alive())
            release.set()
            out = io.StringIO()
            with redirect_stdout(out):
                edgar_dashboard._report_insiders(started)
        self.assertIn("Wrote insider transactions (3 trades)", out.getvalue())

    def test_a_failure_is_reported_not_raised(self):
        def broken(ticker, output_dir, cache_dir=None):
            raise RuntimeError("boom")

        with unittest.mock.patch.object(insider, "write_insiders", broken):
            out = io.StringIO()
            with redirect_stdout(out):
                edgar_dashboard._report_insiders(edgar_dashboard._start_insiders("TEST", "."))
        self.assertIn("[!] insider data skipped: RuntimeError: boom", out.getvalue())



class CheckSummaryTest(unittest.TestCase):
    def test_summary_counts_by_kind(self):
        rows = [{"kind": "statement", "status": "ok", "period_of_report": "2025-12-31"},
                {"kind": "statement", "status": "mismatch", "period_of_report": "2025-12-31"},
                {"kind": "note", "status": "incomplete", "period_of_report": "2024-12-31"}]
        counts = edgar_dashboard.check_summary(rows)
        self.assertEqual(counts["filings"], 2)
        self.assertEqual(counts["statement"], {"totals": 2, "ok": 1, "mismatch": 1, "incomplete": 0})
        self.assertEqual(counts["note"]["incomplete"], 1)


class DerivedOperatingIncomeTest(unittest.TestCase):
    def frame(self, operating):
        import extract
        row = {metric: float("nan") for metric in extract.METRIC_CONCEPTS}
        row.update(period_of_report="2024-12-31", fiscal_year=2024, Revenues=1000e6, GrossProfit=400e6,
                   SellingGeneralAndAdministrativeExpense=100e6, OperatingIncomeLoss=operating)
        return edgar_dashboard.clean_and_derive(pd.DataFrame([row]))

    def test_a_filled_in_operating_income_is_flagged(self):
        out = self.frame(float("nan"))
        self.assertEqual(out["OperatingIncomeLoss"].iloc[0], 300.0)
        self.assertTrue(out["operating_income_derived"].iloc[0])

    def test_a_reported_one_is_not(self):
        out = self.frame(120e6)
        self.assertEqual(out["OperatingIncomeLoss"].iloc[0], 120.0)
        self.assertFalse(out["operating_income_derived"].iloc[0])


class WriteIssuesTest(unittest.TestCase):
    def test_valuation_issues_are_written_with_the_others(self):
        frame = pd.DataFrame([{"period_of_report": "2024-12-31", "fiscal_year": 2024, "Revenues": 1.0,
                               "NetIncomeLoss": 1.0, "OperatingIncomeLoss": 1.0, "OperatingCashFlow": 1.0,
                               "StockholdersEquity": 1.0}])
        extra = [{"kind": "valuation_assumed", "severity": "warning", "fiscal_year": 2024, "period": "2024-12-31",
                  "concept": "Cash", "label": None, "detail": "no cash line"}]
        with tempfile.TemporaryDirectory() as folder, \
                unittest.mock.patch.object(edgar_dashboard, "OUTPUT_DIR", folder), redirect_stdout(io.StringIO()):
            issues = edgar_dashboard._write_issues(frame, pd.DataFrame(), {}, extra)
            written = pd.read_csv(Path(folder) / "data_issues.csv")
        self.assertEqual([issue["kind"] for issue in issues], ["valuation_assumed"])
        self.assertEqual(list(written["concept"]), ["Cash"])


if __name__ == "__main__":
    unittest.main()
