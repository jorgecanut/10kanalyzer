"""Queue and route tests for app.py. No network: the pipeline is a fake script.

Run from the repo root:  python -m unittest discover tests
"""

import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

FAKE_DASHBOARD = textwrap.dedent("""
    import sys, time
    time.sleep(float(__import__("os").environ.get("FAKE_SLEEP", "0.3")))
    print("ARGS", " ".join(sys.argv[1:]))
""")


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "graphs").mkdir()
        fake = self.tmp / "fake_dashboard.py"
        fake.write_text(FAKE_DASHBOARD)
        self._saved = (app.GRAPH_DIR, app.DASHBOARD)
        app.GRAPH_DIR, app.DASHBOARD = self.tmp / "graphs", fake
        with app.JOBS_LOCK:
            app.JOBS.clear()
            app.SCREEN_BATCH.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        self.client.post("/api/cancel")
        app.GRAPH_DIR, app.DASHBOARD = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_idle(self, *tickers, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app.JOBS_LOCK:
                busy = [t for t in tickers
                        if app.JOBS.get(t, {}).get("status") in ("queued", "running")]
            if not busy:
                return
            time.sleep(0.05)
        self.fail(f"jobs still active: {busy}")

    def log(self, ticker):
        return (app.GRAPH_DIR / ticker / "run.log").read_text()


class RouteTests(AppTestCase):
    def test_index_has_graph_tab_not_run_tab(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="tab-graph"', page)
        self.assertNotIn('id="pane-run"', page)

    def test_rejects_path_traversal_ticker(self):
        res = self.client.post("/api/run", json={"ticker": "../../x"})
        self.assertEqual(res.status_code, 400)

    def test_screener_validates_filings(self):
        res = self.client.post("/api/screener", json={"tickers": ["AAA"], "filings": 99})
        self.assertEqual(res.status_code, 400)

    def test_screener_caps_batch_size(self):
        tickers = [f"T{i}" for i in range(26)]
        res = self.client.post("/api/screener", json={"tickers": tickers})
        self.assertEqual(res.status_code, 400)


class QueueTests(AppTestCase):
    def test_single_run_renders_charts(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        self.assertEqual(app.JOBS["AAA"]["status"], "done")
        self.assertIn("--ticker AAA --filings 3", self.log("AAA"))
        self.assertNotIn("--no-charts", self.log("AAA"))

    def test_screener_skips_charts_and_uses_filings(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"], "filings": 5})
        self.wait_idle("AAA", "BBB")
        for ticker in ("AAA", "BBB"):
            self.assertIn(f"--ticker {ticker} --filings 5 --no-charts", self.log(ticker))

    def test_run_upgrades_queued_screener_job(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
        # AAA holds the run lock, so BBB is still queued.
        self.client.post("/api/run", json={"ticker": "BBB", "filings": 4})
        self.wait_idle("AAA", "BBB")
        self.assertIn("--no-charts", self.log("AAA"))
        self.assertNotIn("--no-charts", self.log("BBB"))

    def test_run_during_screener_job_queues_graph_followup(self):
        self.client.post("/api/screener", json={"tickers": ["AAA"]})
        deadline = time.time() + 5
        while app.JOBS["AAA"]["status"] != "running" and time.time() < deadline:
            time.sleep(0.02)
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 7})
        # The follow-up is queued in the same locked step that finishes the
        # screener job, so waiting for idle covers both runs.
        self.wait_idle("AAA")
        # The follow-up run overwrites run.log with a charts run.
        self.assertIn("--ticker AAA --filings 7", self.log("AAA"))
        self.assertNotIn("--no-charts", self.log("AAA"))

    def test_jobs_reports_active_jobs_and_batch(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
        data = self.client.get("/api/jobs").get_json()
        self.assertEqual(data["batch"], ["AAA", "BBB"])
        self.assertEqual({job["ticker"] for job in data["jobs"]}, {"AAA", "BBB"})
        self.assertTrue(all(job["charts"] is False for job in data["jobs"]))

    def test_cancel_stops_jobs_and_clears_batch(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
        cancelled = self.client.post("/api/cancel").get_json()["cancelled"]
        self.assertEqual(set(cancelled), {"AAA", "BBB"})
        data = self.client.get("/api/jobs").get_json()
        self.assertEqual(data, {"jobs": [], "batch": []})


if __name__ == "__main__":
    unittest.main()
