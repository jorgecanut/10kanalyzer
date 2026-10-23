"""Queue, account and route tests for app.py. No network: the pipeline is a fake script.

Run from the repo root:  python -m unittest discover tests
"""

import io
import json
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
import zipfile
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
                                "identity": "Ada Lovelace ada@example.com",
                                "color": "orange", "avatar": False})

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


def png_bytes(size=(400, 200), color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class ProfileTests(AppTestCase):
    def put_avatar(self, data, client=None):
        return (client or self.client).put("/api/account/avatar", data=data,
                                          headers={"Content-Type": "application/octet-stream"})

    def test_colour_defaults_to_orange_and_only_accepts_presets(self):
        self.assertEqual(self.client.get("/api/account").get_json()["user"]["color"], "orange")
        res = self.client.patch("/api/account", json={"color": "teal"})
        self.assertEqual(res.get_json()["user"]["color"], "teal")
        self.assertEqual(self.client.patch("/api/account", json={"color": "#123456"}).status_code, 400)
        self.assertEqual(self.client.get("/api/account").get_json()["user"]["color"], "teal")

    def test_avatar_upload_is_squared_and_can_be_removed(self):
        from PIL import Image
        self.assertEqual(self.client.get("/api/account/avatar").status_code, 404)
        res = self.put_avatar(png_bytes())
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["user"]["avatar"])
        with self.client.get("/api/account/avatar") as got:
            self.assertEqual(got.mimetype, "image/png")
            self.assertEqual(Image.open(io.BytesIO(got.data)).size, (app.AVATAR_SIZE, app.AVATAR_SIZE))
        res = self.client.delete("/api/account/avatar")
        self.assertFalse(res.get_json()["user"]["avatar"])
        self.assertEqual(self.client.get("/api/account/avatar").status_code, 404)

    def test_avatar_rejects_non_images_and_big_files(self):
        self.assertEqual(self.put_avatar(b"not an image").status_code, 400)
        big = b"\0" * (app.AVATAR_MAX_BYTES + 1)
        self.assertEqual(self.put_avatar(big).status_code, 413)
        self.assertFalse(self.client.get("/api/account").get_json()["user"]["avatar"])

    def test_avatars_are_per_user(self):
        bob = self.signup("Bob", "bob@example.com")
        self.put_avatar(png_bytes())
        self.assertEqual(bob.get("/api/account/avatar").status_code, 404)

    def test_wipe_keeps_avatar_and_account_delete_removes_it(self):
        self.put_avatar(png_bytes())
        self.client.delete("/api/data", json={"confirm": "DELETE"})
        with self.client.get("/api/account/avatar") as got:
            self.assertEqual(got.status_code, 200)
        path = app._avatar_path(self.uid())
        self.client.delete("/api/account", json={"confirm": "DELETE", "password": PASSWORD})
        self.assertFalse(path.exists())


class FakeNews:
    """Stands in for Google News: records each query and returns numbered articles."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, query, days, limit):
        self.calls.append((query, days, limit))
        if self.fail:
            raise app.URLError("offline")
        return [{"title": f"Story {i}", "source": "Wire", "link": f"https://example.com/{i}",
                 "published": "2026-10-01T00:00:00+00:00"} for i in range(limit)]


class FakeNewsCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self._news = (app._google_news, app._company_name, app._company_sector)
        self.fake = FakeNews()
        app._google_news, app._company_name = self.fake, lambda ticker: ("Acme Inc", "Acme")
        app._company_sector = lambda ticker: ("Technology", "Software")
        app._NEWS_CACHE.clear()

    def tearDown(self):
        app._google_news, app._company_name, app._company_sector = self._news
        app._NEWS_CACHE.clear()
        super().tearDown()


class NewsTests(FakeNewsCase):
    def test_defaults_to_20_articles_from_14_days(self):
        data = self.client.get("/api/news/AAA").get_json()
        self.assertEqual((data["days"], data["limit"], len(data["articles"])), (14, 20, 20))
        self.assertEqual(self.fake.calls, [('"Acme"', 14, 20)])

    def test_limit_and_days_are_chosen_and_cached_apart(self):
        data = self.client.get("/api/news/AAA?limit=50&days=30").get_json()
        self.assertEqual((data["days"], data["limit"], len(data["articles"])), (30, 50, 50))
        self.client.get("/api/news/AAA?limit=50&days=30")
        self.client.get("/api/news/AAA?limit=30&days=1")
        self.assertEqual(self.fake.calls, [('"Acme"', 30, 50), ('"Acme"', 1, 30)])

    def test_market_scope_mixes_stock_sector_and_market_without_company_query(self):
        data = self.client.get("/api/news/AAA?scope=market").get_json()
        queries = [query for query, _, _ in self.fake.calls]
        self.assertEqual(len(queries), 3)
        self.assertNotIn('"Acme"', queries)
        self.assertIn('"AAA"', queries[0])
        self.assertIn('"Software stocks"', queries[1])
        self.assertIn('"Technology sector"', queries[1])
        self.assertEqual(queries[2], app.MARKET_QUERY)
        self.assertEqual(data["scope"], "market")
        self.assertEqual(len(data["articles"]), 20)
        # The fake feeds share titles; repeats are skipped, never shown twice.
        self.assertEqual(len({a["title"] for a in data["articles"]}), 20)
        self.assertEqual([a["topic"] for a in data["articles"][:4]], list(app.MARKET_PATTERN))

    def test_market_scope_interleaves_stock_first(self):
        feeds = {"stock": ["S1", "S2", "S3"], "sector": ["X1"], "market": ["M1", "M2"]}
        app._google_news = lambda query, days, limit: [
            {"title": t, "source": "", "link": "https://example.com", "published": None}
            for t in feeds[next(k for k, v in app._market_queries("AAA").items() if v == query)]]
        titles = [a["title"] for a in app._market_news("AAA", 14, 20)]
        self.assertEqual(titles, ["S1", "S2", "X1", "M1", "S3", "M2"])

    def test_market_scope_keeps_the_industry_before_its_dash(self):
        app._company_sector = lambda ticker: ("Financial Services", "Banks—Diversified")
        self.assertIn('"Banks stocks"', app._market_queries("AAA")["sector"])

    def test_market_scope_skips_sector_when_unknown(self):
        app._company_sector = lambda ticker: ("", "")
        self.assertEqual(list(app._market_queries("AAA")), ["stock", "market"])

    def test_company_and_market_scopes_are_cached_apart(self):
        self.client.get("/api/news/AAA")
        self.client.get("/api/news/AAA?scope=market")
        self.client.get("/api/news/AAA?scope=market")
        self.assertEqual(len(self.fake.calls), 4)
        self.assertEqual(self.client.get("/api/news/AAA?scope=other").status_code, 400)

    def test_rejects_other_limits_and_periods(self):
        self.assertEqual(self.client.get("/api/news/AAA?limit=99").status_code, 400)
        self.assertEqual(self.client.get("/api/news/AAA?days=5").status_code, 400)
        self.assertEqual(self.fake.calls, [])


class DownloadAllTests(FakeNewsCase):
    def make_results(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        folder = app._ticker_dir("AAA", self.uid())
        (folder / "dark").mkdir(exist_ok=True)
        (folder / "dark" / "01_fake.png").write_bytes(b"png")
        (folder / "evaluation.csv").write_text("fiscal_year,pe,f_score\n2025,12.5,7\n")
        (folder / "insider.json").write_text(json.dumps({"generated": "2026-10-01", "transactions": [
            {"date": "2026-09-01", "name": "Jane Doe", "type": "buy", "shares": 10, "value": 100.0},
            {"date": "2020-01-01", "name": "Old Trade", "type": "sell", "shares": 5, "value": 50.0},
        ]}))

    def zip_names(self, url):
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200, res.data[:200])
        bundle = zipfile.ZipFile(io.BytesIO(res.data))
        return bundle, set(bundle.namelist())

    def test_zip_holds_every_file_plus_insiders_news_and_screener(self):
        self.make_results()
        bundle, names = self.zip_names("/download/AAA/all?months=12&news_limit=30&news_days=7")
        for name in ("01_fake.png", "dark/01_fake.png", "insider.json", "evaluation.csv",
                     "insiders.csv", "news.csv", "screener.csv"):
            self.assertIn(f"AAA/{name}", names)
        insiders = bundle.read("AAA/insiders.csv").decode()
        self.assertIn("Jane Doe", insiders)
        self.assertNotIn("Old Trade", insiders)      # outside the chosen 12 months
        self.assertEqual(bundle.read("AAA/news.csv").decode().count("example.com"), 30)
        self.assertEqual(self.fake.calls, [('"Acme"', 7, 30)])
        self.assertIn("12.5", bundle.read("AAA/screener.csv").decode())

    def test_zip_skips_news_when_google_is_unreachable(self):
        self.make_results()
        self.fake.fail = True
        _, names = self.zip_names("/download/AAA/all")
        self.assertNotIn("AAA/news.csv", names)
        self.assertIn("AAA/insiders.csv", names)

    def test_zip_needs_results(self):
        self.assertEqual(self.client.get("/download/ZZZ/all").status_code, 404)

    def test_zip_has_debug_folder_with_build_logs_and_diagnostics(self):
        self.make_results()
        self.client.post("/api/diag", json={"message": "TypeError: x is undefined", "page": "/#home"})
        self.client.get("/api/files/NOPE")                 # a failed request is logged too
        res = self.client.get("/download/AAA/all")
        self.assertIn(f"v{app.app_version()}", res.headers["Content-Disposition"])
        bundle = zipfile.ZipFile(io.BytesIO(res.data))
        names = set(bundle.namelist())
        for name in ("README.md", "build.json", "jobs.json", "diagnostics.jsonl", "runs/AAA.log", "runs/AAA.json"):
            self.assertIn(f"_debug/{name}", names)
        build = json.loads(bundle.read("_debug/build.json"))
        self.assertEqual(build["version"], app.app_version())
        self.assertIn("commit", build)
        self.assertEqual(len(build["code_sha1"]), 40)
        diag = [json.loads(line) for line in bundle.read("_debug/diagnostics.jsonl").decode().splitlines()]
        self.assertEqual([d["source"] for d in diag], ["browser", "server"])
        self.assertIn("/api/files/NOPE", diag[1]["message"])
        self.assertIn("ARGS", bundle.read("_debug/runs/AAA.log").decode())
        self.assertNotIn(self.uid(), bundle.read("_debug/jobs.json").decode())


class DiagnosticsTests(AppTestCase):
    def read_diag(self):
        path = app._user_dir(self.uid()) / app.DIAG_FILE
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_failed_run_is_logged_with_its_log_tail(self):
        app.DASHBOARD.write_text("import sys\nprint('boom here')\nsys.exit(3)\n")
        self.client.post("/api/run", json={"ticker": "BAD", "filings": 3})
        self.wait_idle("BAD")
        entry = next(d for d in self.read_diag() if d["source"] == "pipeline")
        self.assertIn("exit 3", entry["message"])
        self.assertIn("boom here", entry["log_tail"])

    def test_crash_is_logged_with_traceback_and_returns_json(self):
        (app._user_dir(self.uid()) / "AAA").mkdir(parents=True)
        with unittest.mock.patch.object(app, "_results_summary", side_effect=RuntimeError("kaput")):
            res = self.client.get("/api/recent")
        self.assertEqual(res.status_code, 500)
        self.assertIn("error", res.get_json())
        entries = [d for d in self.read_diag() if d["level"] == "error"]
        self.assertEqual(len(entries), 1)               # not logged twice
        self.assertIn("kaput", entries[0]["traceback"])

    def test_unwritable_data_folder_is_named_in_the_error(self):
        # A fresh bind mount Docker created as root: no database, nothing writable.
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        with unittest.mock.patch.object(app, "DATA_DIR", locked):
            res = app.app.test_client().post("/api/login", json={"email": "a@example.com", "password": "x" * 8})
        self.assertEqual(res.status_code, 500)
        self.assertIn("not writable", res.get_json()["error"])
        self.assertNotIn("diagnostics log", res.get_json()["error"])   # nobody signed in: nothing logged

    def test_browser_reports_need_a_user_and_an_object(self):
        anon = app.app.test_client()
        self.assertEqual(anon.post("/api/diag", json={"message": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/diag", json=[1]).status_code, 400)
        self.assertEqual(self.client.post("/api/diag", json={"message": "y" * 9000}).status_code, 204)
        self.assertEqual(len(self.read_diag()[-1]["message"]), 2000)

    def test_log_is_trimmed(self):
        with unittest.mock.patch.object(app, "DIAG_MAX_BYTES", 2000), unittest.mock.patch.object(app, "DIAG_KEEP", 5):
            for i in range(30):
                app._diag(self.uid(), "server", "warn", f"entry {i}")
        entries = self.read_diag()
        self.assertLessEqual(len(entries), 30)
        self.assertEqual(entries[-1]["message"], "entry 29")
        self.assertLess(len(entries), 30)


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
