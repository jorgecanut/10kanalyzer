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
import os
import time
from urllib.error import URLError
import unittest
import unittest.mock
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

# A one-chart charts.json: seven years of one bar series.
FAKE_SPEC = {"version": 1, "ticker": "AAA", "years": list(range(2019, 2026)), "charts": [{
    "id": "01_fake", "name": "Fake", "panels": [{
        "title": "AAA · Fake", "subtitle": "Fiscal years {span} · USD", "years": list(range(2019, 2026)),
        "series": [{"label": "Revenue", "kind": "bar", "format": "mm", "axis": 0, "derived": False,
                    "color": "#4c72b0", "concepts": ["Revenues"], "values": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}],
        "axes": [{"format": "money"}], "margin": 0.14, "note": None, "reference": None,
        "empty": "No data for this window", "group_width": 0.8}]}]}

# Stands in for edgar_dashboard.py: writes charts.json unless --no-charts, and
# echoes its arguments and the SEC identity it was given into run.log.
FAKE_DASHBOARD = textwrap.dedent("""
    import os, sys, time
    args = sys.argv[1:]
    time.sleep(0.3)
    out, ticker = args[args.index("--output-dir") + 1], args[args.index("--ticker") + 1]
    if "--no-charts" not in args:
        open(os.path.join(out, ticker, "charts.json"), "w").write(%r)
    print("ARGS", " ".join(args))
    print("IDENTITY", os.environ.get("EDGAR_IDENTITY"))
""") % json.dumps(FAKE_SPEC)

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
        with app.THROTTLE_LOCK:
            app.THROTTLE.clear()
        env = unittest.mock.patch.dict("os.environ", {"STOCKY_ADMIN_EMAIL": "boss@example.com"})
        env.start()
        self.addCleanup(env.stop)
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
        body = {"name": name, "email": email, "password": PASSWORD}
        if email == app._admin_email():
            body["admin_code"] = app._admin_setup_code()      # the admin's first sign-up needs it
        res = client.post("/api/signup", json=body)
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
    def test_the_database_schema_is_set_up_once_not_on_every_connection(self):
        with unittest.mock.patch.object(app, "_init_db", wraps=app._init_db) as setup:
            for _ in range(3):
                self.assertEqual(self.client.get("/api/account").status_code, 200)
        self.assertEqual(setup.call_count, 0)                  # done when Ada signed up
        (app.DATA_DIR / "stocky.db").unlink()                  # a wiped data folder is set up again
        with app._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_a_failed_insider_collection_is_passed_on(self):
        folder = app._user_dir(self.uid()) / "AAA"
        folder.mkdir(parents=True)
        (folder / "insider.json").write_text(json.dumps({
            "ticker": "AAA", "months": 36, "since": None, "transactions": [], "error": "OSError: SEC unreachable",
            "skipped": [{"accession": "x", "error": "ValueError: bad XML"}]}))
        data = self.client.get("/api/insiders/AAA").get_json()
        self.assertEqual(data["error"], "OSError: SEC unreachable")
        self.assertEqual(data["skipped"], 1)

    def test_a_huge_request_body_is_refused(self):
        anon = app.app.test_client()
        res = anon.post("/api/login", data=b"x" * (app.MAX_BODY_BYTES + 1), content_type="application/json")
        self.assertEqual(res.status_code, 413)

    def test_the_sec_identity_is_plain_ascii(self):
        # httpx refuses a User-Agent it cannot encode, and every SEC request failed.
        self.assertEqual(app._identity({"name": "Łukasz Żółć", "email": "l@example.com"}),
                         "Lukasz Zolc l@example.com")
        self.assertEqual(app._identity({"name": "Søren Weiß", "email": "s@example.com"}),
                         "Soren Weiss s@example.com")
        self.assertEqual(app._identity({"name": "山田 太郎", "email": "y@example.com"}),
                         "Stocky user y@example.com")
        self.assertEqual(app._identity({"name": "Ada Lovelace", "email": "ada@example.com"}),
                         "Ada Lovelace ada@example.com")

    def test_an_email_the_sec_header_cannot_carry_is_refused(self):
        res = app.app.test_client().post("/api/signup", json={"name": "Zoë", "email": "zoë@example.com",
                                                              "password": PASSWORD})
        self.assertEqual(res.status_code, 400)

    def test_signed_out_requests_are_refused(self):
        anon = app.app.test_client()
        self.assertEqual(anon.get("/api/recent").status_code, 401)
        page = anon.get("/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login", page.headers["Location"])
        self.assertEqual(anon.get("/login").status_code, 200)
        self.assertEqual(anon.get("/healthz").status_code, 200)

    def test_healthz_names_the_filing_cache_version(self):
        info = app.app.test_client().get("/healthz").get_json()["filing_cache"]
        self.assertEqual(info["filings"]["reader"], app.filing_cache.reader_id())
        self.assertEqual(info["filings"]["schema"], app.filing_cache.schema())
        self.assertTrue(info["filings"]["classified"])
        self.assertEqual(info["form4"]["versions"], [])

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
        usage = user.pop("usage")
        self.assertEqual(user, {"name": "Ada Lovelace", "email": "ada@example.com",
                                "identity": "Ada Lovelace ada@example.com",
                                "color": "orange", "avatar": False, "admin": False})
        self.assertEqual((usage["runs"], usage["allowance"]), (0, app.LIMITS["runs_per_day"]))

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
        self._news = (app._google_news, app._company_name, app._company_sector, app._yahoo_news)
        self.fake = FakeNews()
        self.yahoo = []
        app._yahoo_news = lambda ticker, days, limit: [dict(a) for a in self.yahoo]
        app._google_news, app._company_name = self.fake, lambda ticker: ("Acme Inc", "Acme")
        app._company_sector = lambda ticker: ("Technology", "Software")
        app._NEWS_CACHE.clear()

    def tearDown(self):
        app._google_news, app._company_name, app._company_sector, app._yahoo_news = self._news
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

    def test_company_news_takes_turns_with_yahoo_and_keeps_its_opening_lines(self):
        self.yahoo = [{"title": "Yahoo one", "source": "Yahoo Finance", "link": "https://y.example/1",
                       "published": None, "summary": "The first lines."},
                      {"title": "Story 1", "source": "Yahoo Finance", "link": "https://y.example/2",
                       "published": None, "summary": ""}]
        data = self.client.get("/api/news/AAA").get_json()
        self.assertEqual([a["title"] for a in data["articles"][:4]], ["Story 0", "Yahoo one", "Story 1", "Story 2"])
        self.assertEqual(data["articles"][1]["summary"], "The first lines.")
        self.assertEqual(len(data["articles"]), 20)

    def test_company_news_without_yahoo_is_google_alone(self):
        def down(ticker, days, limit):
            raise app.URLError("offline")
        app._yahoo_news = down
        self.assertEqual(len(self.client.get("/api/news/AAA").get_json()["articles"]), 20)

    def test_summary_drops_tags_and_cuts_at_a_word(self):
        self.assertEqual(app._summary("<p>Shares&nbsp;rose <b>5%</b>.</p>"), "Shares rose 5% .")
        self.assertEqual(app._summary("one two three four", limit=12), "one two…")

    def test_rejects_other_limits_and_periods(self):
        self.assertEqual(self.client.get("/api/news/AAA?limit=99").status_code, 400)
        self.assertEqual(self.client.get("/api/news/AAA?days=5").status_code, 400)
        self.assertEqual(self.fake.calls, [])


class SavedNewsTests(AppTestCase):
    ARTICLE = {"link": "https://example.com/1", "title": "Story  one", "source": "Wire",
               "published": "2026-10-01T00:00:00+00:00", "ticker": "aaa", "summary": "First lines."}

    def test_save_list_and_remove(self):
        self.assertEqual(self.client.get("/api/saved-news").get_json()["articles"], [])
        saved = self.client.post("/api/saved-news", json=self.ARTICLE).get_json()["articles"]
        self.assertEqual([(a["title"], a["ticker"], a["summary"]) for a in saved], [("Story one", "AAA", "First lines.")])
        # Saving the same link again keeps one copy.
        self.assertEqual(len(self.client.post("/api/saved-news", json=self.ARTICLE).get_json()["articles"]), 1)
        left = self.client.delete("/api/saved-news?link=https://example.com/1").get_json()["articles"]
        self.assertEqual(left, [])

    def test_rejects_articles_without_https_link_or_title(self):
        self.assertEqual(self.client.post("/api/saved-news", json={**self.ARTICLE, "link": "javascript:x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/saved-news", json={**self.ARTICLE, "title": " "}).status_code, 400)

    def test_saved_articles_are_per_user(self):
        self.client.post("/api/saved-news", json=self.ARTICLE)
        bob = self.signup("Bob", "bob@example.com")
        self.assertEqual(bob.get("/api/saved-news").get_json()["articles"], [])



class DownloadAllTests(FakeNewsCase):
    def make_results(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        folder = app._ticker_dir("AAA", self.uid())
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
        for name in ("charts/01_fake_2019_2023.png", "charts/01_fake_2024_2025.png", "charts.json",
                     "insider.json", "evaluation.csv", "insiders.csv", "news.csv", "screener.csv"):
            self.assertIn(f"AAA/{name}", names)
        self.assertTrue(bundle.read("AAA/charts/01_fake_2019_2023.png").startswith(b"\x89PNG"))
        insiders = bundle.read("AAA/insiders.csv").decode()
        self.assertIn("Jane Doe", insiders)
        self.assertNotIn("Old Trade", insiders)      # outside the chosen 12 months
        self.assertEqual(bundle.read("AAA/news.csv").decode().count("example.com"), 30)
        self.assertEqual(self.fake.calls, [('"Acme"', 7, 30)])
        self.assertIn("12.5", bundle.read("AAA/screener.csv").decode())

    def test_zip_holds_only_what_was_ticked(self):
        self.make_results()
        _, names = self.zip_names(
            "/download/AAA/all?charts=01_fake&years=0&theme=dark&files=evaluation.csv&extras=insiders")
        self.assertEqual(names, {"AAA/charts/01_fake_2019_2025.png", "AAA/evaluation.csv", "AAA/insiders.csv"})
        self.assertEqual(self.fake.calls, [])            # news was not ticked: never fetched
        _, names = self.zip_names("/download/AAA/all?charts=&files=run.log&extras=")
        self.assertEqual(names, {"AAA/run.log"})
        _, names = self.zip_names("/download/AAA/all?charts=01_fake&years=10&files=&extras=")
        self.assertEqual(names, {"AAA/charts/01_fake_2019_2025.png"})

    def test_zip_refuses_bad_choices_and_empty_selections(self):
        self.make_results()
        self.assertEqual(self.client.get("/download/AAA/all?years=7").status_code, 400)
        self.assertEqual(self.client.get("/download/AAA/all?theme=blue").status_code, 400)
        self.assertEqual(self.client.get("/download/AAA/all?charts=&files=&extras=").status_code, 400)

    def test_chart_images_are_drawn_once_per_run(self):
        self.make_results()
        self.zip_names("/download/AAA/all?charts=01_fake&files=&extras=")
        folder = app._ticker_dir("AAA", self.uid())
        cached = folder / ".png_cache" / "light-5" / "01_fake_2019_2023.png"
        self.assertTrue(cached.is_file())
        stamp = cached.stat().st_mtime_ns
        self.zip_names("/download/AAA/all?charts=01_fake&files=&extras=")
        self.assertEqual(cached.stat().st_mtime_ns, stamp)
        self.assertNotIn(".png_cache", self.client.get("/api/files/AAA").get_json()["files"])

    def test_recent_download_has_charts_and_files_but_no_extras(self):
        self.make_results()
        _, names = self.zip_names("/download/AAA")
        self.assertIn("AAA/charts/01_fake_2019_2023.png", names)
        self.assertIn("AAA/evaluation.csv", names)
        self.assertFalse(any(name.startswith("_debug/") or name.endswith("news.csv") for name in names))

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


class ChartSpecTests(AppTestCase):
    def test_spec_is_served_and_listed(self):
        self.client.post("/api/run", json={"ticker": "AAA", "filings": 3})
        self.wait_idle("AAA")
        self.assertEqual(self.client.get("/api/charts/AAA").get_json()["charts"][0]["id"], "01_fake")
        summary = self.client.get("/api/files/AAA").get_json()
        self.assertEqual(summary["charts"], ["01_fake"])
        self.assertIn("charts.json", summary["files"])
        self.assertNotIn("run.json", summary["files"])
        self.assertEqual(self.client.get("/api/charts/ZZZ").status_code, 404)

    def test_run_from_before_charts_json_is_upgraded_from_its_csvs(self):
        folder = app._ticker_dir("OLD", self.uid())
        folder.mkdir(parents=True)
        (folder / "01_income_statement_2024_2025.png").write_bytes(b"png")
        (folder / "financial_data.csv").write_text(
            "fiscal_year,Revenues,NetIncomeLoss\n2024,1000.0,100.0\n2025,1200.0,150.0\n")
        spec = self.client.get("/api/charts/OLD").get_json()
        income = next(chart for chart in spec["charts"] if chart["id"] == "01_income_statement")
        revenue = next(s for s in income["panels"][0]["series"] if s["label"] == "Revenue")
        self.assertEqual(revenue["values"], [1000.0, 1200.0])
        self.assertFalse((folder / "01_income_statement_2024_2025.png").exists())   # stale image gone

    def test_screener_run_has_no_charts(self):
        folder = app._ticker_dir("SCR", self.uid())
        folder.mkdir(parents=True)
        (folder / "financial_data.csv").write_text("fiscal_year,Revenues\n2025,1.0\n")
        self.assertEqual(self.client.get("/api/charts/SCR").status_code, 404)
        self.assertFalse((folder / "charts.json").exists())

    def test_fact_index_has_statement_lines_and_other_facts_of_the_own_period(self):
        folder = app._ticker_dir("FCT", self.uid())
        folder.mkdir(parents=True)
        (folder / "statements.csv").write_text(
            "statement,concept,standard_concept,label,value,fiscal_year\n"
            "balance,us-gaap_Goodwill,Goodwill,Goodwill,5000000.0,2024\n"
            "balance,us-gaap_Goodwill,Goodwill,Goodwill,6000000.0,2025\n")
        (folder / "facts.csv").write_text(
            "period_of_report,concept,label,value,unit,period_end,period_instant,is_dimensioned,"
            "statement_type,fiscal_year\n"
            "2025-12-31,us-gaap:Goodwill,Goodwill,6000000,USD,,2025-12-31,False,BalanceSheet,2025\n"
            "2025-12-31,us-gaap:GoodwillImpairmentLoss,Goodwill impairment,300000,USD,2025-12-31,,False,"
            "Disclosures,2025\n"
            "2025-12-31,us-gaap:GoodwillImpairmentLoss,Goodwill impairment,999,USD,2024-12-31,,False,"
            "Disclosures,2025\n"
            "2025-12-31,us-gaap:GoodwillImpairmentLoss,Goodwill impairment,111,USD,2025-12-31,,True,"
            "Disclosures,2025\n")
        facts = self.client.get("/api/facts/FCT").get_json()["facts"]
        self.assertEqual([f["concept"] for f in facts], ["Goodwill", "GoodwillImpairmentLoss"])
        self.assertEqual(facts[0]["values"], {"2024": 5000000.0, "2025": 6000000.0})
        self.assertEqual(facts[1]["values"], {"2025": 300000.0})      # not the comparative, not the segment
        self.assertEqual(facts[1]["unit"], "money")
        self.assertEqual(self.client.get("/api/facts/NONE").status_code, 404)


class ForeignAndCheckTests(AppTestCase):
    def test_fact_index_keeps_the_reporting_currency_and_drops_the_usd_copy(self):
        folder = app._ticker_dir("TSM", self.uid())
        folder.mkdir(parents=True)
        (folder / "financial_data.csv").write_text("fiscal_year,currency,Revenues\n2025,TWD,3809054.3\n")
        (folder / "facts.csv").write_text(
            "period_of_report,concept,label,value,unit,currency,period_end,period_instant,is_dimensioned,"
            "statement_type,fiscal_year\n"
            "2025-12-31,ifrs-full:FinanceCosts,Finance costs,11750000000,twd,TWD,2025-12-31,,False,Notes,2025\n"
            "2025-12-31,ifrs-full:FinanceCosts,Finance costs,370000000,usd,USD,2025-12-31,,False,Notes,2025\n"
            "2025-12-31,ifrs-full:BasicEarningsLossPerShare,EPS,65.47,twdPerShare,TWD,2025-12-31,,False,Notes,2025\n")
        data = self.client.get("/api/facts/TSM").get_json()
        self.assertEqual(data["currency"], "TWD")
        by_concept = {f["concept"]: f for f in data["facts"]}
        self.assertEqual(by_concept["FinanceCosts"]["values"], {"2025": 11750000000.0})
        self.assertEqual(by_concept["FinanceCosts"]["unit"], "money")
        self.assertEqual(by_concept["BasicEarningsLossPerShare"]["unit"], "money/share")

    def test_checks_list_what_does_not_add_up(self):
        folder = app._ticker_dir("CHK", self.uid())
        folder.mkdir(parents=True)
        detail = json.dumps([{"concept": "x_A", "label": "A", "weight": 1, "value": 4.0}])
        (folder / "calc_check.csv").write_text(
            "fiscal_year,period_of_report,kind,statement,concept,label,reported,computed,difference,tolerance,"
            "parts,missing,status,detail\n"
            f"2025,2025-12-31,statement,Balance,x_Total,Total,10,10,0,0,1,,ok,\"{detail.replace(chr(34), chr(34) * 2)}\"\n"
            f"2025,2025-12-31,statement,Balance,x_Debt,Debt,10,4,6,0,1,,mismatch,\"{detail.replace(chr(34), chr(34) * 2)}\"\n"
            "2024,2024-12-31,note,Debt details,x_Bonds,Bonds,9,1,8,0,2,B,incomplete,[]\n")
        data = self.client.get("/api/checks/CHK").get_json()
        self.assertEqual(data["counts"]["statement"], {"totals": 2, "ok": 1, "mismatch": 1, "incomplete": 0})
        self.assertEqual(data["filings"], 2)
        self.assertEqual([i["label"] for i in data["issues"]], ["Debt", "Bonds"])
        self.assertEqual(data["issues"][0]["parts"][0]["label"], "A")
        self.assertEqual(self.client.get("/api/checks/NONE").status_code, 404)


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

    def test_the_page_loads_its_script_and_styles_as_files(self):
        # 250 KB of inline script and styles were sent again on every page load.
        page = self.client.get("/").get_data(as_text=True)
        asset = app.code_sha1()[:10]
        self.assertIn(f"/static/app.js?v={asset}", page)
        self.assertIn(f"/static/app.css?v={asset}", page)
        self.assertNotIn("function showTab", page)
        self.assertLess(len(page), 80_000)
        script = app.app.test_client().get("/static/app.js")
        self.assertIn("function showTab", script.get_data(as_text=True))
        script.close()
        styles = app.app.test_client().get("/static/app.css")
        self.assertIn("fonts/UbuntuMono-Regular.ttf", styles.get_data(as_text=True))
        styles.close()

    def test_the_code_hash_covers_the_page_script_and_styles(self):
        names = {path.name for path in app.code_files()}
        self.assertLessEqual({"app.py", "index.html", "app.js", "app.css"}, names)
        self.assertNotIn("echarts.min.js", names)

    def test_rejects_path_traversal_ticker(self):
        res = self.client.post("/api/run", json={"ticker": "../../x"})
        self.assertEqual(res.status_code, 400)

    def test_screener_validates_filings(self):
        res = self.client.post("/api/screener", json={"tickers": ["AAA"], "filings": 99})
        self.assertEqual(res.status_code, 400)

    def test_screener_caps_batch_size(self):
        tickers = [f"T{i}" for i in range(app.LIMITS["screener_tickers"] + 1)]
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

    def test_run_defaults_to_every_filing_with_the_shared_cache(self):
        self.client.post("/api/run", json={"ticker": "AAA"})
        self.wait_idle("AAA")
        log = self.log("AAA")
        self.assertIn("--ticker AAA --filings max", log)
        self.assertIn(f"--cache-dir {app.DATA_DIR / app.FILING_CACHE}", log)
        again = self.client.post("/api/run", json={"ticker": "AAA", "filings": "max"}).get_json()
        self.assertEqual(again["status"], "cached")

    def test_run_validates_filings(self):
        for filings in (0, 21, "all", None):
            res = self.client.post("/api/run", json={"ticker": "AAA", "filings": filings})
            self.assertEqual(res.status_code, 400, filings)

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
        # Regenerating at once is refused for a limited user, allowed for the admin.
        fresh = self.client.post("/api/run", json={"ticker": "AAA", "filings": 5, "fresh": True})
        self.assertEqual(fresh.status_code, 429)
        with unittest.mock.patch.dict("os.environ", {"STOCKY_ADMIN_EMAIL": "ada@example.com"}):
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


# Stands in for scan.py: records its arguments and identity, then waits to be stopped.
FAKE_SCAN = textwrap.dedent("""
    import os, sys, time
    root = sys.argv[sys.argv.index("--root") + 1]
    with open(os.path.join(root, "args.txt"), "w") as out:
        out.write(" ".join(sys.argv[1:]) + "\\n" + os.environ.get("EDGAR_IDENTITY", "") + "\\n"
                  + os.environ.get("EDGAR_RATE_LIMIT_PER_SEC", ""))
    time.sleep(float(os.environ.get("FAKE_SCAN_SECONDS", "30")))
""")


class ScanApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        env = unittest.mock.patch.dict("os.environ", {"STOCKY_ADMIN_EMAIL": "ada@example.com"})
        env.start()
        self.addCleanup(env.stop)
        fake = self.tmp / "fake_scan.py"
        fake.write_text(FAKE_SCAN)
        patcher = unittest.mock.patch.object(app, "SCAN_SCRIPT", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_scan)

    def stop_scan(self):
        proc = app.SCAN_PROC["proc"]
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        app.SCAN_PROC["proc"] = None

    def args(self):
        path = self.tmp / "data" / "market" / "args.txt"
        deadline = time.time() + 10
        while not path.is_file() and time.time() < deadline:
            time.sleep(0.05)
        return path.read_text().splitlines()

    def test_overview_of_an_empty_scan(self):
        info = self.client.get("/api/scan").get_json()
        self.assertEqual((info["universe"], info["batches"], info["active"]), (0, [], None))
        self.assertEqual(info["reader"]["id"], app.filing_cache.reader_id())
        self.assertEqual(self.client.get("/api/scan/batch/1").status_code, 404)
        self.assertEqual(self.client.get("/api/scan/run/1/log").status_code, 404)
        self.assertEqual(app.app.test_client().get("/api/scan").status_code, 401)

    def test_requests_are_checked(self):
        for payload in ({"action": "next", "count": 0}, {"action": "next", "count": "many"},
                        {"action": "delete"}, {"action": "rerun"}, {"action": "rerun", "batch": 1, "cik": "x"}):
            self.assertEqual(self.client.post("/api/scan/run", json=payload).status_code, 400, payload)

    def test_a_scan_runs_alone_with_the_users_identity_and_can_be_stopped(self):
        res = self.client.post("/api/scan/run", json={"action": "next", "count": 25})
        self.assertEqual(res.status_code, 202)
        args, identity, rate = self.args()
        self.assertIn("next --count 25", args)
        self.assertIn("--cache-dir", args)
        self.assertEqual(identity, "Ada Lovelace ada@example.com")
        self.assertEqual(rate, "5")
        again = self.client.post("/api/scan/run", json={"action": "rerun", "batch": 1})
        self.assertEqual(again.status_code, 409)
        self.assertTrue(self.client.post("/api/scan/cancel").get_json()["stopped"])
        app.SCAN_PROC["proc"].wait(timeout=10)
        self.assertIn("scan.py next --count 25", (self.tmp / "data" / "market" / "logs" / "launcher.log").read_text())

    def test_rerun_passes_the_batch_and_the_options(self):
        self.client.post("/api/scan/run", json={"action": "rerun", "batch": 3, "retry_failed": True, "cik": "320193"})
        self.assertIn("rerun --batch 3 --retry-failed --cik 320193", self.args()[0])


    def test_only_the_admin_reaches_the_scan(self):
        other = self.signup("Bob", "bob@example.com")
        self.assertEqual(other.get("/api/scan").status_code, 403)
        self.assertEqual(other.post("/api/scan/run", json={"action": "next", "count": 5}).status_code, 403)
        self.assertEqual(other.post("/api/scan/cancel").status_code, 403)


class LimitTests(AppTestCase):
    """Ada is a limited user; the admin is boss@example.com."""

    def usage(self, client=None):
        return (client or self.client).get("/api/account").get_json()["user"]["usage"]

    def test_runs_count_against_the_daily_allowance(self):
        with unittest.mock.patch.dict(app.LIMITS, {"runs_per_day": 3}):
            self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
            self.wait_idle("AAA", "BBB")
            self.assertEqual(self.usage()["runs"], 2)
            # Two more would pass the allowance: the whole batch is refused.
            res = self.client.post("/api/screener", json={"tickers": ["CCC", "DDD"]})
            self.assertEqual(res.status_code, 429)
            self.assertIn("1 left", res.get_json()["error"])
            self.assertEqual(self.client.post("/api/run", json={"ticker": "CCC"}).status_code, 202)
            self.wait_idle("CCC")
            self.assertEqual(self.client.post("/api/run", json={"ticker": "DDD"}).status_code, 429)
            # Cached answers cost nothing.
            self.assertEqual(self.client.post("/api/run", json={"ticker": "CCC"}).get_json()["status"], "cached")

    def test_active_chart_runs_are_capped(self):
        with unittest.mock.patch.dict(app.LIMITS, {"active_runs": 1}):
            self.assertEqual(self.client.post("/api/run", json={"ticker": "AAA"}).status_code, 202)
            self.assertEqual(self.client.post("/api/run", json={"ticker": "BBB"}).status_code, 429)

    def test_one_screener_batch_at_a_time(self):
        self.client.post("/api/screener", json={"tickers": ["AAA", "BBB"]})
        self.assertEqual(self.client.post("/api/screener", json={"tickers": ["CCC"]}).status_code, 429)

    def test_request_for_more_runs_is_granted_by_the_admin(self):
        boss = self.signup("Boss", "boss@example.com")
        self.assertIsNone(boss.get("/api/account").get_json()["user"]["usage"])
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.client.post("/api/usage/request", json={"note": "screening banks"}).status_code, 201)
        self.assertEqual(self.client.post("/api/usage/request", json={}).status_code, 409)
        self.assertTrue(self.usage()["pending_request"])
        data = boss.get("/api/admin/users").get_json()
        (pending,) = data["requests"]
        self.assertEqual((pending["email"], pending["note"]), ("ada@example.com", "screening banks"))
        res = boss.post(f"/api/admin/requests/{pending['id']}", json={"action": "accept", "grant": 15})
        self.assertEqual(res.status_code, 200)
        usage = self.usage()
        self.assertEqual(usage["allowance"], app.LIMITS["runs_per_day"] + 15)
        self.assertEqual(usage["last_request"]["status"], "accepted")
        self.assertFalse(usage["pending_request"])
        self.assertEqual(boss.post(f"/api/admin/requests/{pending['id']}", json={"action": "decline"}).status_code, 404)

    def test_admin_disables_and_deletes_accounts(self):
        boss = self.signup("Boss", "boss@example.com")
        uid = self.uid()
        self.assertEqual(boss.post(f"/api/admin/users/{self.uid('boss@example.com')}",
                                   json={"disabled": True}).status_code, 400)
        boss.post(f"/api/admin/users/{uid}", json={"disabled": True})
        self.assertEqual(self.client.get("/api/account").status_code, 401)
        login = app.app.test_client().post("/api/login", json={"email": "ada@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 403)
        boss.post(f"/api/admin/users/{uid}", json={"disabled": False})
        login = app.app.test_client().post("/api/login", json={"email": "ada@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(boss.delete(f"/api/admin/users/{uid}", json={}).status_code, 400)
        self.assertEqual(boss.delete(f"/api/admin/users/{uid}", json={"confirm": "DELETE"}).status_code, 200)
        self.assertIsNone(app._get_user(uid))

    def test_the_admin_email_cannot_be_claimed_without_the_setup_code(self):
        # The admin is whoever holds that email, and emails are not verified.
        body = {"name": "Mallory", "email": "boss@example.com", "password": PASSWORD}
        anon = app.app.test_client()
        res = anon.post("/api/signup", json=body)
        self.assertEqual(res.status_code, 403)
        self.assertTrue(res.get_json()["admin_code"])
        self.assertEqual(anon.post("/api/signup", json={**body, "admin_code": "guess"}).status_code, 403)
        with app._db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE email = 'boss@example.com'").fetchone())

    def test_the_setup_code_is_on_the_server_and_works_once(self):
        path = app.DATA_DIR / "admin_setup_code"
        code = app._admin_setup_code()
        self.assertEqual(path.read_text().strip(), code)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        boss = self.signup("Boss", "boss@example.com")
        self.assertTrue(boss.get("/api/account").get_json()["user"]["admin"])
        self.assertFalse(path.exists())
        self.assertNotEqual(app._admin_setup_code(), code)

    def test_nobody_can_take_the_admin_email(self):
        res = self.client.patch("/api/account", json={"email": "boss@example.com", "current_password": PASSWORD})
        self.assertEqual(res.status_code, 409)

    def test_failed_sign_ins_are_throttled(self):
        anon = app.app.test_client()
        for _ in range(app.LOGIN_FAILURES[0]):
            anon.post("/api/login", json={"email": "ada@example.com", "password": "wrong-wrong"})
        res = anon.post("/api/login", json={"email": "ada@example.com", "password": PASSWORD})
        self.assertEqual(res.status_code, 429)

    def test_admin_runs_go_first(self):
        queue = app.RunQueue()
        order = []
        release = threading.Event()

        def hold():
            with queue.slot("a", False):
                release.wait()

        def take(uid, admin):
            with queue.slot(uid, admin):
                order.append(uid)

        holder = threading.Thread(target=hold)
        holder.start()
        time.sleep(0.05)
        threads = []
        for uid, admin in (("a", False), ("a", False), ("b", False), ("boss", True)):
            threads.append(threading.Thread(target=take, args=(uid, admin)))
            threads[-1].start()
            time.sleep(0.05)
        release.set()
        for thread in [holder, *threads]:
            thread.join(5)
        self.assertEqual(order, ["boss", "b", "a", "a"])


if __name__ == "__main__":
    unittest.main()


class TodayCardTests(AppTestCase):
    def results(self, ticker, evaluation, today=None):
        folder = app._ticker_dir(ticker, self.uid())
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "evaluation.csv").write_text(evaluation)
        if today is not None:
            (folder / "valuation_today.json").write_text(json.dumps(today))

    def items(self):
        return {item["ticker"]: item for item in self.client.get("/api/screener").get_json()["items"]}

    def test_screener_uses_today_record(self):
        self.results("AAA", "fiscal_year,pe,earnings_yield,return_on_capital,f_score\n"
                            "2025,15.9,0.076,0.134,5\nQ2'26,14.0,0.08,0.14,\n",
                     {"pe": 17.4, "ev_ebit": 14.0, "earnings_yield": 0.071, "return_on_capital": 0.14,
                      "fcf_yield": 0.03, "pe_percentile": 70.0, "z_score": 3.1, "f_score": 5, "f_score_year": 2025,
                      "price": 167.77, "price_time": "2026-11-18T15:10:00Z", "price_source": "quote",
                      "earnings_period": "TTM Q2'26"})
        item = self.items()["AAA"]
        self.assertEqual(item["pe"], 17.4)
        self.assertEqual(item["earnings_period"], "TTM Q2'26")
        self.assertEqual(item["price"], 167.77)
        self.assertEqual(item["fiscal_year"], "2025")          # the latest 10-K, never a quarter slot
        self.assertEqual(item["f_score_year"], 2025)

    def test_home_falls_back_to_latest_fy(self):
        self.results("AAA", "fiscal_year,pe,earnings_yield,return_on_capital,f_score\n"
                            "2024,10.8,0.1,0.15,7\n2025,15.9,0.076,0.134,5\nQ2'26,14.0,0.08,0.14,\n")
        item = self.items()["AAA"]
        self.assertEqual(item["pe"], 15.9)
        self.assertEqual(item["earnings_period"], "FY2025")
        self.assertNotIn("price", item)

    def test_magic_rank_is_position(self):
        self.results("AAA", "fiscal_year,earnings_yield,return_on_capital\n2025,0.10,0.10\n")
        self.results("BBB", "fiscal_year,earnings_yield,return_on_capital\n2025,0.05,0.20\n")
        items = self.items()
        # Scores 1 + 2 = 3 each: a tie shares the first place.
        self.assertEqual((items["AAA"]["greenblatt_score"], items["AAA"]["greenblatt_rank"]), (3, 1))
        self.assertEqual((items["BBB"]["greenblatt_score"], items["BBB"]["greenblatt_rank"]), (3, 1))
        self.results("CCC", "fiscal_year,earnings_yield,return_on_capital\n2025,0.20,0.30\n")
        items = self.items()
        self.assertEqual(items["CCC"]["greenblatt_rank"], 1)          # 1 + 1 = 2
        self.assertEqual(items["AAA"]["greenblatt_rank"], 2)          # 2 + 3 = 5
        self.assertEqual(items["BBB"]["greenblatt_rank"], 2)          # 3 + 2 = 5


COMPANIES = [
    {"cik": 27419, "ticker": "TGT", "name": "TARGET CORP", "exchange": "NYSE"},
    {"cik": 1, "ticker": "TGTX", "name": "TG THERAPEUTICS, INC.", "exchange": "Nasdaq"},
    {"cik": 2, "ticker": "TRGP", "name": "Targa Resources Corp.", "exchange": "NYSE"},
    {"cik": 3, "ticker": "VICI", "name": "VICI PROPERTIES INC.", "exchange": "NYSE"},
    {"cik": 4, "ticker": "STAR", "name": "Big Star Target Holdings", "exchange": "NYSE"},
    {"cik": 5, "ticker": "AAPL", "name": "Apple Inc.", "exchange": "Nasdaq"},
]


class CompanySearchTests(AppTestCase):
    def search(self, q):
        res = self.client.get("/api/companies", query_string={"q": q})
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_matches_ticker_then_name(self):
        with unittest.mock.patch.object(app, "_download_companies", return_value=COMPANIES):
            found = self.search("targ")
        self.assertTrue(found["available"])
        self.assertEqual([item["ticker"] for item in found["items"]], ["TGT", "TRGP", "STAR"])
        self.assertEqual(found["items"][0], {"ticker": "TGT", "name": "TARGET CORP", "exchange": "NYSE"})
        with unittest.mock.patch.object(app, "_download_companies", return_value=COMPANIES):
            # An exact ticker first, then tickers starting with it; case and punctuation ignored.
            self.assertEqual([i["ticker"] for i in self.search("tgt")["items"]], ["TGT", "TGTX"])
            self.assertEqual([i["ticker"] for i in self.search("vici properties")["items"]], ["VICI"])
            self.assertEqual([i["ticker"] for i in self.search("Apple, Inc")["items"]], ["AAPL"])

    def test_whole_name_first_and_ticker_punctuation(self):
        rows = COMPANIES + [{"cik": 6, "ticker": "TH", "name": "Target Hospitality Corp.", "exchange": "Nasdaq"},
                            {"cik": 7, "ticker": "BRK-B", "name": "BERKSHIRE HATHAWAY INC", "exchange": "NYSE"}]
        self.assertEqual([c["ticker"] for c in app._match_companies(rows, "target")][:2], ["TGT", "TH"])
        self.assertEqual([c["ticker"] for c in app._match_companies(rows, "brk.b")], ["BRK-B"])
        self.assertEqual([c["ticker"] for c in app._match_companies(rows, "BRKB")], ["BRK-B"])

    def test_short_query_gives_nothing(self):
        with unittest.mock.patch.object(app, "_download_companies", return_value=COMPANIES) as download:
            self.assertEqual(self.search("t")["items"], [])
        download.assert_not_called()

    def test_downloaded_once_a_day_with_the_account_as_contact(self):
        with unittest.mock.patch.object(app, "_download_companies", return_value=COMPANIES) as download:
            self.search("targ")
            self.search("vici")
        download.assert_called_once()
        self.assertIn("ada@example.com", download.call_args.args[0])
        self.assertTrue((app.DATA_DIR / "company_tickers.json").is_file())

    def test_sec_down_uses_the_last_copy(self):
        with unittest.mock.patch.object(app, "_download_companies", return_value=COMPANIES):
            self.search("targ")
        path = app.DATA_DIR / "company_tickers.json"
        old = time.time() - 3 * 86400
        os.utime(path, (old, old))
        app._COMPANIES.clear()
        with unittest.mock.patch.object(app, "_download_companies", side_effect=URLError("down")):
            found = self.search("targ")
        self.assertTrue(found["available"])
        self.assertEqual(found["items"][0]["ticker"], "TGT")

    def test_no_copy_and_sec_down_says_so(self):
        app._COMPANIES.clear()
        with unittest.mock.patch.object(app, "_download_companies", side_effect=URLError("down")):
            found = self.search("targ")
        self.assertEqual(found, {"items": [], "available": False})
