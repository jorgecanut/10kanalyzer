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

    def test_a_collection_that_failed_is_not_reported_as_written(self):
        def failed(ticker, output_dir, cache_dir=None):
            insider._warn("failed to collect insiders for TEST: SEC unreachable")
            path = Path(output_dir) / "insider.json"
            path.write_text(json.dumps({"transactions": []}), encoding="utf-8")
            return str(path)

        with tempfile.TemporaryDirectory() as folder, \
                unittest.mock.patch.object(insider, "write_insiders", failed), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            out = io.StringIO()
            with redirect_stdout(out):
                edgar_dashboard._report_insiders(edgar_dashboard._start_insiders("TEST", folder))
        self.assertIn("[!] insider trades not collected: SEC unreachable", out.getvalue())
        self.assertNotIn("Wrote insider transactions", out.getvalue())

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


class _Quarterly:
    def __init__(self, form, report_date, accession):
        self.form, self.report_date, self.accession_no = form, report_date, accession


class _QuarterCompany:
    cik = 99

    def __init__(self, quarterly):
        self.quarterly = quarterly
        self.asked = []

    def get_filings(self, form, amendments=False):
        self.asked.append((form, amendments))
        return self.quarterly if form == "10-Q" else []


class ListQuarterlyTest(unittest.TestCase):
    def test_list_quarterly_after_latest(self):
        # Newest first, as edgartools lists them; the listing stops at the first older one.
        later = [_Quarterly("10-Q", "2026-08-01", "q2"), _Quarterly("10-Q", "2026-05-02", "q1")]
        older = _Quarterly("10-Q", "2025-11-01", "q3-old")
        poison = _Quarterly("10-Q", None, "never-read")      # after the break: never looked at
        company = _QuarterCompany(later + [older, poison])
        found = edgar_dashboard.list_quarterly(company, "2026-01-31", log=lambda *_: None)
        self.assertEqual([f.accession_no for f in found], ["q1", "q2"])
        self.assertEqual(company.asked, [("10-Q", False)])

    def test_other_forms_and_a_failed_listing(self):
        company = _QuarterCompany([_Quarterly("10-Q/A", "2026-08-01", "amended")])
        self.assertEqual(edgar_dashboard.list_quarterly(company, "2026-01-31", log=lambda *_: None), [])

        class Broken:
            def get_filings(self, form, amendments=False):
                raise ConnectionError("SEC down")
        lines = []
        self.assertEqual(edgar_dashboard.list_quarterly(Broken(), "2026-01-31", log=lines.append), [])
        self.assertTrue(any("10-Q list failed" in line for line in lines))

    def test_no_annual_report_means_no_quarters(self):
        company = _QuarterCompany([_Quarterly("10-Q", "2026-08-01", "q2")])
        self.assertEqual(edgar_dashboard.list_quarterly(company, "", log=lambda *_: None), [])


class ObtainQuartersTest(unittest.TestCase):
    def test_build_dataset_quarter_failure_is_recorded(self):
        filings = [_Quarterly("10-Q", "2026-05-02", "q1"), _Quarterly("10-Q", "2026-08-01", "q2")]

        def entry_of(filing):
            if filing.accession_no == "q1":
                raise ConnectionError("SEC returned 503")
            return {"xbrl": True, "filing": {"accession": filing.accession_no}}, "read"

        failures = []
        with redirect_stdout(io.StringIO()):
            entries = edgar_dashboard.obtain_quarters(filings, entry_of, failures)
        self.assertEqual([e["filing"]["accession"] for e in entries], ["q2"])
        self.assertEqual(failures[0]["accession"], "q1")
        self.assertEqual(failures[0]["period"], "2026-05-02")
        self.assertIn("503", failures[0]["error"])


class QuarterStepTest(unittest.TestCase):
    def test_run_writes_quarters_csv_and_reports(self):
        frame = pd.DataFrame([{"slot": "Q2'26", "label": "TTM Q2'26", "period_end": "2026-08-01", "Revenues": 1.0}])
        issue = {"kind": "quarter_metric_missing", "severity": "warning", "fiscal_year": "Q2'26"}
        with tempfile.TemporaryDirectory() as out, \
                unittest.mock.patch.object(edgar_dashboard, "OUTPUT_DIR", out), \
                unittest.mock.patch("quarters.build", return_value=(frame, [issue], {})) as build, \
                redirect_stdout(io.StringIO()) as log:
            df = pd.DataFrame([{"period_of_report": "2026-01-31", "fiscal_year": 2025}])
            filed = {"2026-01-31": {"sources": {"Revenues": "Revenues"}}}
            failures = [{"accession": "q1", "form": "10-Q", "period": "2026-05-02", "error": "ConnectionError: 503"}]
            got, issues, _ = edgar_dashboard.quarter_step(df, filed, [{"x": 1}] * 4, failures)
            written = pd.read_csv(Path(out) / "quarters.csv")
        build.assert_called_once()
        self.assertEqual(build.call_args.args[1], {"Revenues": "Revenues"})
        self.assertEqual(list(written["slot"]), ["Q2'26"])
        kinds = [i["kind"] for i in issues]
        self.assertIn("quarter_metric_missing", kinds)
        self.assertIn("quarter_unread", kinds)           # the download that failed
        self.assertIn("overdue_10k", kinds)              # 5 10-Qs after the 10-K
        self.assertIn("Quarters: Q2'26", log.getvalue())

    def test_no_quarters_says_so(self):
        with tempfile.TemporaryDirectory() as out, \
                unittest.mock.patch.object(edgar_dashboard, "OUTPUT_DIR", out), \
                redirect_stdout(io.StringIO()) as log:
            df = pd.DataFrame([{"period_of_report": "2026-01-31", "fiscal_year": 2025}])
            got, issues, _ = edgar_dashboard.quarter_step(df, {}, [], [])
            self.assertFalse((Path(out) / "quarters.csv").exists())
        self.assertTrue(got.empty)
        self.assertEqual(issues, [])
        self.assertIn("none since the latest annual report", log.getvalue())


class _AnyForm:
    def __init__(self, form):
        self.form, self.report_date, self.accession_no = form, "2026-06-30", f"x-{form}"


class _Fund:
    """A closed-end fund (GGT): fund reports only, no 10-K, 20-F or 40-F."""
    name, cik = "GABELLI MULTIMEDIA TRUST INC.", 921671

    def get_filings(self, form=None, amendments=True):
        everything = [_AnyForm(f) for f in ["N-CSR", "N-CSRS", "N-CEN", "N-CSR", "N-PX", "N-CSRS", "N-PORT"]]
        return everything if form is None else [f for f in everything if f.form == form]


class NoAnnualReportTests(unittest.TestCase):
    def test_a_fund_stops_with_what_it_files_instead(self):
        with unittest.mock.patch.object(edgar_dashboard, "Company", return_value=_Fund()), \
                redirect_stdout(io.StringIO()) as log:
            with self.assertRaises(edgar_dashboard.NoAnnualReports) as raised:
                edgar_dashboard.build_dataset("GGT", None, None, quarters=[], quarter_failures=[])
        message = str(raised.exception)
        self.assertIn("no 10-K, 20-F or 40-F", message)
        self.assertIn("N-CSR", message)
        self.assertIn("fund", message)
        self.assertNotIn("10-Qs after", log.getvalue())          # no pointless 10-Q listing

    def test_main_reports_it_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as out, \
                unittest.mock.patch.object(edgar_dashboard, "_run",
                                           side_effect=edgar_dashboard.NoAnnualReports("GGT files no 10-K")), \
                unittest.mock.patch.object(edgar_dashboard, "_start_insiders", return_value=None), \
                redirect_stdout(io.StringIO()) as log, self.assertRaises(SystemExit) as stopped:
            edgar_dashboard.main(["--ticker", "GGT", "--output-dir", out])
        self.assertEqual(stopped.exception.code, edgar_dashboard.NOT_ANALYSABLE_EXIT)
        self.assertIn("[!] GGT files no 10-K", log.getvalue())
        self.assertNotIn("Traceback", log.getvalue())
