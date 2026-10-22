"""Queue, account and route tests for app.py. No network: the pipeline is a fake script.

Run from the repo root:  python -m unittest discover tests
"""

import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

# Stands in for edgar_dashboard.py: writes a chart unless --no-charts, and
# echoes its arguments and the SEC identity it was given into run.log.
FAKE_DASHBOARD = textwrap.dedent("""
    import os, sys, time
    args = sys.argv[1:]
    time.sleep(0.3)
    out, ticker = args[args.index("--output-dir") + 1], args[args.index("--ticker") + 1]
    if "--no-charts" not in args:
        open(os.path.join(out, ticker, "01_fake.png"), "wb").write(b"png")
    print("ARGS", " ".join(args))
    print("IDENTITY", os.environ.get("EDGAR_IDENTITY"))
""")

PASSWORD = "correct horse"


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fake = self.tmp / "fake_dashboard.py"
        fake.write_text(FAKE_DASHBOARD)
        self._saved = (app.DATA_DIR, app.DASHBOARD, app.app.secret_key)
        app.DATA_DIR, app.DASHBOARD = self.tmp / "data", fake
        app.app.secret_key = "test-secret"
        with app.JOBS_LOCK:
            app.JOBS.clear()
            app.SCREEN_BATCH.clear()
        self._threads = threading.active_count()
        self.client = self.signup("Ada Lovelace", "ada@example.com")

    def tearDown(self):
        for client in getattr(self, "_clients", []):
            client.post("/api/cancel")
        # Let every worker thread exit before DATA_DIR is restored, or a late
        # one would create its folder in the real data directory.
        deadline = time.time() + 10
        while threading.active_count() > self._threads and time.time() < deadline:
            time.sleep(0.02)
        app.DATA_DIR, app.DASHBOARD, app.app.secret_key = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def signup(self, name, email):
        client = app.app.test_client()
        res = client.post("/api/signup", json={"name": name, "email": email, "password": PASSWORD})
        self.assertEqual(res.status_code, 201, res.get_json())
        self._clients = getattr(self, "_clients", []) + [client]
        return client

    def uid(self, email="ada@example.com"):
        with app._db() as conn:
            return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]

    def wait_idle(self, *tickers, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app.JOBS_LOCK:
                busy = [job["ticker"] for job in app.JOBS.values()
                        if job["ticker"] in tickers and job["status"] in ("queued", "running")]
            if not busy:
                return
            time.sleep(0.05)
        self.fail(f"jobs still active: {busy}")

    def log(self, ticker, email="ada@example.com"):
        return (app._user_dir(self.uid(email)) / ticker / "run.log").read_text()


class AuthTests(AppTestCase):
    def test_signed_out_requests_are_refused(self):
        anon = app.app.test_client()
        self.assertEqual(anon.get("/api/recent").status_code, 401)
        page = anon.get("/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login", page.headers["Location"])
        self.assertEqual(anon.get("/login").status_code, 200)
        self.assertEqual(anon.get("/healthz").status_code, 200)

    def test_login_logout(self):
        client = app.app.test_client()
        bad = client.post("/api/login", json={"email": "ada@example.com", "password": "nope-nope"})
        self.assertEqual(bad.status_code, 401)
        ok = client.post("/api/login", json={"email": "ADA@example.com", "password": PASSWORD})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(client.get("/api/account").status_code, 200)
        client.post("/api/logout")
        self.assertEqual(client.get("/api/account").status_code, 401)

    def test_signup_validates_and_rejects_duplicates(self):
        client = app.app.test_client()
        short = client.post("/api/signup", json={"name": "X", "email": "x@example.com", "password": "short"})
        self.assertEqual(short.status_code, 400)
        dup = client.post("/api/signup", json={"name": "Ada", "email": "ada@example.com", "password": PASSWORD})
        self.assertEqual(dup.status_code, 409)

    def test_account_never_exposes_the_id(self):
        user = self.client.get("/api/account").get_json()["user"]
        self.assertEqual(user, {"name": "Ada Lovelace", "email": "ada@example.com",
                                "identity": "Ada Lovelace ada@example.com"})

    def test_email_and_password_changes_need_current_password(self):
        res = self.client.patch("/api/account", json={"email": "new@example.com"})
        self.assertEqual(res.status_code, 403)
        res = self.client.patch("/api/account", json={"name": "Ada King"})
        self.assertEqual(res.get_json()["user"]["name"], "Ada King")
        res = self.client.patch("/api/account", json={
            "email": "new@example.com", "new_password": "another pass", "current_password": PASSWORD})
        self.assertEqual(res.status_code, 200)
        client = app.app.test_client()
        ok = client.post("/api/login", json={"email": "new@example.com", "password": "another pass"})
        self.assertEqual(ok.status_code, 200)


class RouteTests(AppTestCase):
    def test_index_has_graph_tab_and_options(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="tab-graph"', page)
        self.assertIn('id="options-btn"', page)
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
    def test_single_run_renders_charts_with_account_identity(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        log = self.log("AAA")
        self.assertIn("--ticker AAA --filings 3", log)
        self.assertIn(f"--output-dir {app._user_dir(self.uid())}", log)
        self.assertNotIn("--no-charts", log)
        self.assertIn("IDENTITY Ada Lovelace ada@example.com", log)

    def test_screener_skips_charts_and_uses_filings(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"], "filings": 5})
        self.wait_idle("AAA", "BBB")
        for ticker in ("AAA", "BBB"):
            log = self.log(ticker)
            self.assertIn(f"--ticker {ticker} --filings 5", log)
            self.assertIn("--no-charts", log)

    def test_run_upgrades_queued_screener_job(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
        # AAA holds the run lock, so BBB is still queued.
        self.client.post("/api/run", json={"ticker": "BBB", "filings": 4})
        self.wait_idle("AAA", "BBB")
        self.assertIn("--no-charts", self.log("AAA"))
        self.assertNotIn("--no-charts", self.log("BBB"))

    def test_run_during_screener_job_queues_graph_followup(self):
        self.client.post("/api/screener", json={"tickers": ["AAA"]})
        key = app._job_key(self.uid(), "AAA")
        deadline = time.time() + 5
        while app.JOBS[key]["status"] != "running" and time.time() < deadline:
            time.sleep(0.02)
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 7})
        # The follow-up is queued in the same locked step that finishes the
        # screener job, so waiting for idle covers both runs.
        self.wait_idle("AAA")
        self.assertIn("--ticker AAA --filings 7", self.log("AAA"))
        self.assertNotIn("--no-charts", self.log("AAA"))

    def test_same_day_graph_run_is_served_from_cache(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        again = self.client.post("/api/run", json={"ticker": "AAA", "filings": 3}).get_json()
        self.assertEqual(again["status"], "cached")
        self.assertTrue(again["generated"])
        other = self.client.post("/api/run", json={"ticker": "AAA", "filings": 5}).get_json()
        self.assertEqual(other["status"], "queued")  # different filings: not the same generation
        self.wait_idle("AAA")
        fresh = self.client.post("/api/run", json={"ticker": "AAA", "filings": 5, "fresh": True}).get_json()
        self.assertEqual(fresh["status"], "queued")

    def test_screener_run_does_not_count_as_cached_graphs(self):
        self.client.post("/api/screener", json={"tickers": ["AAA"], "filings": 3})
        self.wait_idle("AAA")
        res = self.client.post("/api/run", json={"ticker": "AAA", "filings": 3}).get_json()
        self.assertEqual(res["status"], "queued")

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


class IsolationTests(AppTestCase):
    def test_users_never_see_each_other(self):
        bob = self.signup("Bob", "bob@example.com")
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.assertEqual(bob.get("/api/jobs").get_json()["jobs"], [])
        self.assertNotIn("uid", self.client.get("/api/status/AAA").get_json())
        self.wait_idle("AAA")
        self.assertEqual(len(self.client.get("/api/recent").get_json()["items"]), 1)
        self.assertEqual(bob.get("/api/recent").get_json()["items"], [])
        self.assertEqual(bob.get("/api/files/AAA").status_code, 404)
        self.assertEqual(bob.get("/api/status/AAA").get_json()["status"], "idle")
        # Bob's cancel must not touch Ada's jobs.
        self.client.post("/api/run", json={"ticker": "BBB", "filings": 3})
        self.assertEqual(bob.post("/api/cancel").get_json()["cancelled"], [])
        self.wait_idle("BBB")
        self.assertIn("IDENTITY Ada Lovelace", self.log("BBB"))


class DeletionTests(AppTestCase):
    def test_wipe_needs_confirmation(self):
        self.assertEqual(self.client.delete("/api/data").status_code, 400)
        res = self.client.delete("/api/data", json={"confirm": "yes"})
        self.assertEqual(res.status_code, 400)

    def test_wipe_removes_only_own_data(self):
        bob = self.signup("Bob", "bob@example.com")
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        bob.post("/api/run", json={"ticker": "BBB", "filings": 3})
        self.wait_idle("AAA", "BBB")
        res = self.client.delete("/api/data", json={"confirm": "DELETE"})
        self.assertEqual(res.get_json()["removed"], 1)
        self.assertEqual(self.client.get("/api/recent").get_json()["items"], [])
        self.assertEqual(len(bob.get("/api/recent").get_json()["items"]), 1)
        self.assertEqual(self.client.get("/api/account").status_code, 200)  # account stays

    def test_delete_account_needs_password_and_removes_everything(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        uid = self.uid()
        wrong = self.client.delete("/api/account", json={"confirm": "DELETE", "password": "nope-nope"})
        self.assertEqual(wrong.status_code, 403)
        ok = self.client.delete("/api/account", json={"confirm": "DELETE", "password": PASSWORD})
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(app._user_dir(uid).exists())
        self.assertEqual(self.client.get("/api/account").status_code, 401)
        login = app.app.test_client().post("/api/login", json={"email": "ada@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 401)


if __name__ == "__main__":
    unittest.main()
