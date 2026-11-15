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


class PublishTest(unittest.TestCase):
    """A run writes into a staging folder; only a finished run replaces the results."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.live = self.root / "AAA"
        self.live.mkdir()
        self.staging = self.live / edgar_dashboard.STAGING_DIRNAME
        self.staging.mkdir()
        for name in ("financial_data.csv", "evaluation.csv", "charts.json", "run.log"):
            (self.live / name).write_text("old")
        (self.live / ".png_cache").mkdir()

    def publish(self, charts):
        with redirect_stdout(io.StringIO()) as out:
            edgar_dashboard.publish(str(self.staging), str(self.live), with_charts=charts)
        return out.getvalue()

    def test_new_files_replace_the_old_and_the_staging_folder_goes(self):
        (self.staging / "financial_data.csv").write_text("new")
        (self.staging / "charts.json").write_text("new")
        self.publish(charts=True)
        self.assertEqual((self.live / "financial_data.csv").read_text(), "new")
        self.assertEqual((self.live / "charts.json").read_text(), "new")
        self.assertFalse(self.staging.exists())
        self.assertFalse((self.live / ".png_cache").exists())      # drawn from the old charts

    def test_a_file_this_run_did_not_produce_is_removed_and_said_so(self):
        # A valuation that failed left last month's evaluation.csv beside new numbers.
        (self.staging / "financial_data.csv").write_text("new")
        log = self.publish(charts=True)
        self.assertFalse((self.live / "evaluation.csv").exists())
        self.assertIn("evaluation.csv", log)
        self.assertEqual((self.live / "run.log").read_text(), "old")    # not the pipeline's file

    def test_a_run_without_charts_keeps_the_charts_it_did_not_redraw(self):
        (self.staging / "financial_data.csv").write_text("new")
        self.publish(charts=False)
        self.assertEqual((self.live / "charts.json").read_text(), "old")
        self.assertTrue((self.live / ".png_cache").exists())

    def test_a_finished_run_publishes_what_it_made_and_drops_what_it_did_not(self):
        import extract
        import valuation
        row = {metric: float("nan") for metric in extract.METRIC_CONCEPTS}
        row.update(period_of_report="2024-12-31", fiscal_year=2024, form="10-K", currency="USD",
                   Revenues=1000e6, NetIncomeLoss=100e6, OperatingIncomeLoss=150e6, OperatingCashFlow=160e6,
                   StockholdersEquity=500e6)
        frames = (pd.DataFrame([row]), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        saved = sys.stdout, sys.stderr
        try:
            with unittest.mock.patch.object(edgar_dashboard, "build_dataset", return_value=frames), \
                    unittest.mock.patch.object(edgar_dashboard, "_start_insiders", return_value=None), \
                    unittest.mock.patch.object(edgar_dashboard, "_report_insiders"), \
                    unittest.mock.patch.object(valuation, "fetch_price_history", side_effect=OSError("Yahoo down")), \
                    redirect_stdout(io.StringIO()):
                edgar_dashboard.main(["--ticker", "AAA", "--output-dir", str(self.root), "--no-charts"])
        finally:
            sys.stdout, sys.stderr = saved
        self.assertIn("1000.0", (self.live / "financial_data.csv").read_text())
        self.assertFalse((self.live / "evaluation.csv").exists())           # no valuation this time
        issues = pd.read_csv(self.live / "data_issues.csv")
        self.assertEqual(list(issues["kind"]), ["valuation_skipped"])
        self.assertEqual((self.live / "charts.json").read_text(), "old")    # --no-charts
        self.assertFalse(self.staging.exists())

    def test_a_run_that_fails_leaves_the_previous_results_alone(self):
        saved = sys.stdout, sys.stderr
        try:
            with unittest.mock.patch.object(edgar_dashboard, "build_dataset", side_effect=RuntimeError("SEC down")), \
                    unittest.mock.patch.object(edgar_dashboard, "_start_insiders", return_value=None), \
                    redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                edgar_dashboard.main(["--ticker", "AAA", "--output-dir", str(self.root)])
        finally:
            sys.stdout, sys.stderr = saved
        self.assertEqual((self.live / "financial_data.csv").read_text(), "old")
        self.assertEqual((self.live / "evaluation.csv").read_text(), "old")
        self.assertFalse(self.staging.exists())


if __name__ == "__main__":
    unittest.main()
