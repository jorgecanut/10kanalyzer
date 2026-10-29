"""The per-filing cache (filing_cache.py) and its use by the pipeline and insider.py. No network.

Run from the repo root:  python -m unittest discover tests
"""

import ast
import gzip
import io
import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import edgar_dashboard  # noqa: E402
import extract  # noqa: E402
import filing_cache  # noqa: E402
import insider  # noqa: E402

ACCESSION = "0001193125-26-323660"
V1, V2 = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


class StoreLoadTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_round_trip_with_numpy_values_in_a_version_folder(self):
        data = {"row": {"Revenues": np.float64(1.5), "fiscal_year": np.int64(2025), "ads": np.bool_(False)}}
        filing_cache.store(self.root, "annual", V1, 789019, ACCESSION, data)
        folder = Path(self.root) / "annual" / V1
        self.assertTrue((folder / "789019" / f"{ACCESSION}.json.gz").is_file())
        meta = json.loads((folder / "version.json").read_text())
        self.assertEqual(set(meta["code"]), {"extract.py", "xbrl_extract.py", "calc_check.py", "edgartools"})
        self.assertEqual(filing_cache.load(self.root, "annual", V1, "0000789019", ACCESSION),
                         {"row": {"Revenues": 1.5, "fiscal_year": 2025, "ads": False}})

    def test_another_version_is_a_miss_and_is_kept(self):
        filing_cache.store(self.root, "annual", V1, 1, ACCESSION, {"xbrl": False})
        self.assertIsNone(filing_cache.load(self.root, "annual", V2, 1, ACCESSION))
        self.assertTrue(filing_cache.cached_elsewhere(self.root, "annual", V2, 1, ACCESSION))
        filing_cache.store(self.root, "annual", V2, 1, ACCESSION, {"xbrl": True})
        self.assertEqual(filing_cache.load(self.root, "annual", V1, 1, ACCESSION), {"xbrl": False})

    def test_absent_corrupt_or_disabled_is_a_miss(self):
        self.assertIsNone(filing_cache.load(self.root, "annual", V1, 1, ACCESSION))
        path = Path(self.root) / "annual" / V1 / "1" / f"{ACCESSION}.json.gz"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not gzip")
        self.assertIsNone(filing_cache.load(self.root, "annual", V1, 1, ACCESSION))
        path.write_bytes(gzip.compress(b"{half"))
        self.assertIsNone(filing_cache.load(self.root, "annual", V1, 1, ACCESSION))
        filing_cache.store(None, "annual", V1, 1, ACCESSION, {})
        self.assertIsNone(filing_cache.load(None, "annual", V1, 1, ACCESSION))

    def test_only_accessions_kinds_and_versions_become_paths(self):
        filing_cache.store(self.root, "annual", V1, 1, "../escape", {})
        filing_cache.store(self.root, "annual", "../x", 1, ACCESSION, {})
        filing_cache.store(self.root, "other", V1, 1, ACCESSION, {})
        self.assertEqual(list(Path(self.root).rglob("*")), [])


class VersionTest(unittest.TestCase):
    """The rule: only code that shapes what is read from a filing may change the version."""

    def setUp(self):
        self.here = Path(tempfile.mkdtemp())
        for name in ("extract.py", "xbrl_extract.py", "calc_check.py", "insider.py", "edgar_dashboard.py"):
            shutil.copy(ROOT / name, self.here / name)
        patcher = unittest.mock.patch.object(filing_cache, "HERE", self.here)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stage_one_edits_change_the_version_and_say_which_file(self):
        root = tempfile.mkdtemp()
        before = filing_cache.version("annual")
        filing_cache.store(root, "annual", before, 1, ACCESSION, {})
        with open(self.here / "xbrl_extract.py", "a") as handle:
            handle.write("\n# changed\n")
        self.assertNotEqual(filing_cache.version("annual"), before)
        self.assertIn("xbrl_extract.py changed since " + before, filing_cache.explain(root, "annual"))

    def test_stage_two_edits_do_not(self):
        before = (filing_cache.version("annual"), filing_cache.version("form4"))
        with open(self.here / "edgar_dashboard.py", "a") as handle:
            handle.write("\n# margins changed\n")
        self.assertEqual((filing_cache.version("annual"), filing_cache.version("form4")), before)

    def test_stage_two_modules_are_never_hashed(self):
        hashed = {name for names in filing_cache.KINDS.values() for name in names}
        for module in ("edgar_dashboard.py", "charts.py", "valuation.py", "app.py", "textpanel.py"):
            self.assertNotIn(module, hashed)

    def test_hashed_modules_import_only_hashed_local_modules(self):
        # Otherwise a change in the imported module would change what is
        # extracted without changing the version: stale entries served as fresh.
        local = {path.stem for path in ROOT.glob("*.py")}
        for kind, names in filing_cache.KINDS.items():
            for name in names:
                tree = ast.parse((ROOT / name).read_text())
                imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                            if isinstance(node, ast.Import) for alias in node.names}
                imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                             if isinstance(node, ast.ImportFrom) and node.module}
                allowed = {Path(other).stem for other in names} | {"filing_cache"}
                self.assertEqual((imported & local) - allowed, set(), f"{name} ({kind})")


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.current = filing_cache.version("annual")
        for ver, created in ((self.current, "2026-10-29T10:00"), (V1, "2026-10-28T10:00"),
                             (V2, "2026-10-27T10:00")):
            filing_cache.store(self.root, "annual", ver, 1, ACCESSION, {})
            meta = self.root / "annual" / ver / "version.json"
            meta.write_text(json.dumps({**json.loads(meta.read_text()), "created": created}))
        (self.root / "123").mkdir()                 # the 1.12.0 layout

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            filing_cache.main(["--root", str(self.root), *args])
        return out.getvalue()

    def versions(self):
        return {item["version"] for item in filing_cache.versions(self.root, "annual")}

    def test_status_lists_versions_and_what_is_stale(self):
        filing_cache.store(self.root, "annual", V1, 2, ACCESSION, {})
        out = self.run_cli("status")
        self.assertIn(f"{self.current} ", out)
        self.assertIn("current", out)
        self.assertIn("stale: 1 entries of 1 companies", out)
        self.assertIn("legacy: 1", out)

    def test_prune_is_a_dry_run_until_yes_and_keeps_the_current(self):
        out = self.run_cli("prune", "--keep", "2")
        self.assertIn(f"would delete annual/{V2}", out)
        self.assertEqual(self.versions(), {self.current, V1, V2})
        self.run_cli("prune", "--keep", "2", "--yes")
        self.assertEqual(self.versions(), {self.current, V1})
        self.assertFalse((self.root / "123").exists())
        self.run_cli("prune", "--keep", "0", "--yes")
        self.assertEqual(self.versions(), {self.current})

    def test_delete_refuses_the_current_version(self):
        self.assertIn("is the current version", self.run_cli("delete", self.current, "--yes"))
        self.run_cli("delete", V1, "--yes")
        self.assertEqual(self.versions(), {self.current, V2})


class _AnnualFiling:
    def __init__(self, accession, report_date, form="10-K"):
        self.accession_no, self.report_date, self.form = accession, report_date, form


def _extracted(filing, period_key):
    if period_key < "2010":
        return {"xbrl": False}
    return {"xbrl": True,
            "row": {"period_of_report": period_key, "fiscal_year": int(period_key[:4]), "form": filing.form,
                    "currency": "USD", "ads": False, "ads_ratio": None, "ads_ratio_source": None},
            "statements": [], "facts": [], "checks": []}


class BuildDatasetCacheTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        years = range(2025, 2006, -1)       # 19 10-Ks, the last three without XBRL
        self.filings = [_AnnualFiling(f"0000000001-{year % 100:02d}-000001", f"{year}-12-31") for year in years]
        self.company = unittest.mock.Mock(cik=42)
        self.company.name = "ACME"
        self.company.get_filings.side_effect = lambda form, amendments: (
            self.filings if form == "10-K" else [])

    def build(self, n_filings):
        parse = unittest.mock.Mock(side_effect=_extracted)
        with unittest.mock.patch.object(edgar_dashboard, "Company", return_value=self.company), \
                unittest.mock.patch.object(extract, "extract_filing", parse), \
                redirect_stdout(io.StringIO()) as out:
            ratios = edgar_dashboard.build_dataset("ACME", n_filings, self.root)[0]
        return ratios, parse.call_count, out.getvalue()

    def test_max_reads_every_filing_and_a_rerun_parses_none(self):
        ratios, parsed, log = self.build(None)
        self.assertEqual(parsed, 19)
        self.assertEqual(len(ratios), 16)
        self.assertIn("reading all of them", log)
        self.assertIn("0 from the cache, 19 new filings parsed, 0 parsed again", log)
        again, parsed, log = self.build(None)
        self.assertEqual(parsed, 0)                     # filings without XBRL are cached too
        self.assertEqual(again["period_of_report"].tolist(), ratios["period_of_report"].tolist())
        self.assertIn("19 from the cache, 0 new filings parsed", log)

    def test_a_new_filing_is_the_only_one_parsed(self):
        self.build(5)
        self.filings.insert(0, _AnnualFiling("0000000001-26-000001", "2026-12-31"))
        ratios, parsed, _ = self.build(5)
        self.assertEqual(parsed, 1)
        self.assertEqual(ratios["period_of_report"].iloc[-1], "2026-12-31")
        self.assertEqual(len(ratios), 5)

    def test_a_code_change_reparses_and_says_so_and_reverting_finds_the_old_entries(self):
        self.build(3)
        with unittest.mock.patch.object(edgar_dashboard, "CACHE_VERSION", V2):
            _, parsed, log = self.build(3)
        self.assertEqual(parsed, 3)
        self.assertIn("cached only by an older code version", log)
        self.assertIn("0 new filings parsed, 3 parsed again", log)
        _, parsed, _ = self.build(3)                    # back on the first version
        self.assertEqual(parsed, 0)


class InsiderCacheTest(unittest.TestCase):
    def test_a_rerun_reads_only_new_form4s(self):
        root = tempfile.mkdtemp()
        trade = {"date": "2099-01-01", "name": "Ada", "title": "CEO", "type": "buy",
                 "shares": 10.0, "value": 100.0, "value_estimated": False}
        opened = []

        def filing(accession):
            def obj():
                opened.append(accession)
                return SimpleNamespace()
            return SimpleNamespace(filing_date="2099-01-01", accession_no=accession, obj=obj)

        company = unittest.mock.Mock(cik=7)
        company.get_filings.return_value = [filing("a"), filing("b")]
        with unittest.mock.patch.object(insider, "Company", return_value=company), \
                unittest.mock.patch.object(insider, "_same_issuer", return_value=True), \
                unittest.mock.patch.object(insider, "_rows_from_ownership", return_value=[trade]), \
                redirect_stdout(io.StringIO()):
            first = insider._collect("ACME", 36, root)[0]
            company.get_filings.return_value = [filing("c"), filing("a"), filing("b")]
            second = insider._collect("ACME", 36, root)[0]
        self.assertEqual(opened, ["a", "b", "c"])
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 3)
        self.assertTrue((Path(root) / "form4" / insider.CACHE_VERSION / "7" / "form4.json.gz").is_file())


if __name__ == "__main__":
    unittest.main()
