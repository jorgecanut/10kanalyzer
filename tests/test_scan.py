"""The market scan (scan.py): batches, dumping, issues, and reruns from the dump. No network.

Run from the repo root:  python -m unittest discover tests
"""

import io
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dump  # noqa: E402
import edgar_dashboard  # noqa: E402
import extract  # noqa: E402
import filing_cache  # noqa: E402
import scan  # noqa: E402

UNIVERSE = [  # cik, ticker, name: deliberately not in ticker order
    (30, "CCC", "Charlie Corp"), (10, "AAA", "Alpha Inc"), (20, "BBB", "Bravo Fund"), (40, "DDD", "Delta Co"),
]


class _Filing:
    def __init__(self, cik, year, form="10-K", month=12):
        self.cik, self.form = cik, form
        self.accession_no = f"{cik:010d}-{year % 100:02d}-{month:06d}"
        self.report_date = f"{year}-{month:02d}-28"
        self.filing_date = f"{year + (month == 12)}-02-15" if form == "10-K" else f"{year}-{month + 1:02d}-10"


def _filings_of(cik):
    if cik == 20:                                     # a fund: no annual reports
        return {"10-K": [], "10-Q": []}
    annual = [_Filing(cik, year) for year in (2025, 2024, 2023)]
    quarterly = [_Filing(cik, 2026, "10-Q", 6), _Filing(cik, 2026, "10-Q", 3), _Filing(cik, 2025, "10-Q", 9)]
    return {"10-K": annual, "10-Q": quarterly}


class _Company:
    def __init__(self, cik):
        self.cik = cik
        self.filings = _filings_of(cik)

    def get_filings(self, form, amendments=False):
        return self.filings.get(form, [])


def _dumped(filing):
    if filing.accession_no == "0000000030-23-000012":
        raise ConnectionError("SEC returned 503")
    return {"schema": dump.SCHEMA, "xbrl": True, "filing": {"accession": filing.accession_no, "form": filing.form,
                                                            "report_date": filing.report_date},
            "facts": dump.encode_frame(pd.DataFrame())}


def _stage_two(revenue):
    def extract_filing(view, period_key):
        check = {"period_of_report": period_key, "kind": "statement", "statement": "Balance Sheet",
                 "concept": "us-gaap_Assets", "label": "Total assets", "reported": 100.0, "computed": 90.0,
                 "difference": 10.0, "tolerance": 0.5, "parts": 2, "missing": "", "status": "mismatch",
                 "hint": "", "detail": "[]"}
        row = {"period_of_report": period_key, "fiscal_year": int(period_key[:4]), "form": view.form,
               "currency": "USD", "ads": False, "ads_ratio": None, "ads_ratio_source": None,
               "Revenues": revenue, "NetIncomeLoss": 1.0, "OperatingIncomeLoss": 1.0, "OperatingCashFlow": 1.0,
               "StockholdersEquity": 1.0}
        for metric in extract.METRIC_CONCEPTS:
            row.setdefault(metric, None)
        return {"xbrl": True, "row": row, "statements": [], "facts": [], "checks": [check]}
    return extract_filing


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cache = Path(tempfile.mkdtemp())
        self.db = scan.connect(self.root)
        with self.db:
            for position, (cik, ticker, name) in enumerate(sorted(UNIVERSE, key=lambda row: row[1]), 1):
                self.db.execute("INSERT INTO universe VALUES (?, ?, ?, '', ?, '')", (cik, ticker, name, position))
        self.addCleanup(self.db.close)

    def run_scan(self, *args, revenue=5.0, offline=False):
        reader = unittest.mock.Mock(side_effect=_dumped)
        company = unittest.mock.Mock(side_effect=_Company)
        if offline:
            company.side_effect = AssertionError("rerun must not ask SEC")
            reader.side_effect = AssertionError("rerun must not download")
        with unittest.mock.patch("edgar.Company", company), \
                unittest.mock.patch.object(dump, "dump_filing", reader), \
                unittest.mock.patch.object(extract, "extract_filing", _stage_two(revenue)), \
                unittest.mock.patch.object(scan, "_disable_download_cache"), \
                redirect_stdout(io.StringIO()) as out:
            code = scan.main(["--root", str(self.root), "--cache-dir", str(self.cache), *args])
        return code, reader.call_count, out.getvalue()

    def issues(self, kind=None):
        query = "SELECT * FROM issues" + (" WHERE kind = ?" if kind else "")
        return self.db.execute(query, (kind,) if kind else ()).fetchall()

    def test_batches_follow_ticker_order_and_never_repeat_a_company(self):
        self.run_scan("next", "--count", "2")
        self.run_scan("next", "--count", "5")
        batches = self.db.execute("""SELECT b.batch, u.ticker FROM batch_companies b JOIN universe u
                                     ON u.cik = b.cik ORDER BY b.position""").fetchall()
        self.assertEqual([(row["batch"], row["ticker"]) for row in batches],
                         [(1, "AAA"), (1, "BBB"), (2, "CCC"), (2, "DDD")])
        with self.assertRaises(SystemExit):
            self.run_scan("next", "--count", "1")

    def test_a_batch_dumps_annual_and_later_quarterly_reports_and_records_every_issue(self):
        code, read, log = self.run_scan("next", "--count", "3")
        self.assertEqual(code, 0)
        # AAA and CCC: three 10-Ks and the two 10-Qs after the latest one (the 2025-09 10-Q is older).
        self.assertEqual(read, 10)
        results = {row["ticker"]: row for row in self.db.execute("SELECT * FROM companies")}
        self.assertEqual(results["AAA"]["result"], "ok")
        self.assertEqual(results["AAA"]["annual_analysed"], 3)
        self.assertEqual(results["AAA"]["quarterly_found"], 2)
        self.assertEqual(results["BBB"]["result"], "no_filings")
        self.assertEqual(results["CCC"]["result"], "partial")      # one download failed
        self.assertEqual(results["CCC"]["annual_analysed"], 2)
        failed = self.issues("download_failed")
        self.assertEqual([(row["ticker"], row["detail"]) for row in failed],
                         [("CCC", "ConnectionError: SEC returned 503")])
        self.assertEqual(len(self.issues("check_mismatch")), 5)     # one per analysed year
        mismatch = self.issues("check_mismatch")[0]
        self.assertEqual((mismatch["reported"], mismatch["computed"], mismatch["concept"]),
                         (100.0, 90.0, "us-gaap_Assets"))
        self.assertTrue(mismatch["accession"])
        self.assertEqual(self.issues("missing_metric"), [])
        self.assertTrue((self.root / "companies" / "10" / "financial_data.csv").is_file())
        self.assertTrue((self.root / "companies" / "10" / "issues.csv").is_file())
        run = self.db.execute("SELECT * FROM runs").fetchone()
        self.assertEqual((run["status"], run["done"], run["downloaded"], run["download_failed"]), ("done", 3, 9, 1))
        text = (self.root / run["log"]).read_text()
        self.assertIn("download or read failed: ConnectionError", text)
        self.assertIn("Traceback", text)                            # kept for debugging
        self.assertIn("[+] CCC: partial", text)

    def test_rerun_uses_only_the_dump_and_replaces_the_issues(self):
        self.run_scan("next", "--count", "3", revenue=None)
        self.assertEqual(len(self.issues("missing_metric")), 5)    # no revenue tag found
        code, read, log = self.run_scan("rerun", "--batch", "1", revenue=5.0, offline=True)
        self.assertEqual((code, read), (0, 0))
        self.assertEqual(self.issues("missing_metric"), [])        # the fix shows up with no download
        self.assertEqual(len(self.issues("download_failed")), 1)   # still failed: not retried
        self.assertIn("no SEC requests", log)
        runs = self.db.execute("SELECT kind, status FROM runs ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in runs], [("next", "done"), ("rerun", "done")])

    def test_retry_failed_downloads_only_what_is_missing(self):
        self.run_scan("next", "--count", "3")
        reader = unittest.mock.Mock(side_effect=lambda filing: {**_dumped(_Filing(1, 2000)),
                                                                "filing": {"form": filing.form}})
        with unittest.mock.patch("edgar.Company", side_effect=_Company), \
                unittest.mock.patch.object(dump, "dump_filing", reader), \
                unittest.mock.patch.object(extract, "extract_filing", _stage_two(5.0)), \
                unittest.mock.patch.object(scan, "_disable_download_cache"), \
                redirect_stdout(io.StringIO()):
            scan.main(["--root", str(self.root), "--cache-dir", str(self.cache),
                       "rerun", "--batch", "1", "--retry-failed"])
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.issues("download_failed"), [])
        result = self.db.execute("SELECT result FROM companies WHERE ticker = 'CCC'").fetchone()[0]
        self.assertEqual(result, "ok")

    def test_resume_skips_the_companies_already_done(self):
        # Stop after the first company, as a SIGTERM from the app would.
        calls = []
        real = scan.analyse_company

        def stop_after_one(*args, **kwargs):
            if calls:
                raise KeyboardInterrupt("signal 15")
            calls.append(args)
            return real(*args, **kwargs)

        with unittest.mock.patch.object(scan, "analyse_company", side_effect=stop_after_one):
            self.run_scan("next", "--count", "3")
        run = self.db.execute("SELECT status, done FROM runs").fetchone()
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 1)
        _, read, log = self.run_scan("resume", "--batch", "1")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 3)
        self.assertNotIn("Company 1/3", log)

    def test_reports_for_the_app(self):
        self.run_scan("next", "--count", "3")
        info = scan.overview(self.db)
        self.assertEqual((info["universe"], info["taken"], info["next"]), (4, 3, "DDD"))
        batch = info["batches"][0]
        self.assertEqual(batch["results"]["ok"], 1)
        self.assertEqual(batch["issue_kinds"]["check_mismatch"], 5)
        detail = scan.batch_detail(self.db, 1, kind="download_failed")
        self.assertEqual([issue["ticker"] for issue in detail["issues"]], ["CCC"])
        self.assertEqual([company["ticker"] for company in detail["companies"]], ["AAA", "BBB", "CCC"])
        text = scan.issues_csv(self.db, 1)
        self.assertTrue(text.startswith("ticker,cik,severity,kind"))
        self.assertEqual(text.count("check_mismatch"), 5)

    def test_a_dead_run_is_marked_interrupted(self):
        with self.db:
            self.db.execute("INSERT INTO runs (batch, kind, status, pid) VALUES (1, 'next', 'running', 999999999)")
        scan.mark_stale_runs(self.db)
        self.assertEqual(self.db.execute("SELECT status FROM runs").fetchone()[0], "interrupted")


if __name__ == "__main__":
    unittest.main()
