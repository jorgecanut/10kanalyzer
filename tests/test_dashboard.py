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

        def slow_write(ticker, output_dir):
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
        def broken(ticker, output_dir):
            raise RuntimeError("boom")

        with unittest.mock.patch.object(insider, "write_insiders", broken):
            out = io.StringIO()
            with redirect_stdout(out):
                edgar_dashboard._report_insiders(edgar_dashboard._start_insiders("TEST", "."))
        self.assertIn("[!] insider data skipped: RuntimeError: boom", out.getvalue())


if __name__ == "__main__":
    unittest.main()
