"""The filing dump (dump.py, filing_view.py, filing_cache.py) and its use by the pipeline. No network.

The rule these tests keep: no change may force re-reading every filing. Only
a schema bump does; a new reader keeps every entry, stage-2 code is never
part of the stamp, and invalidation removes only what it is told to.

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
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dump  # noqa: E402
import edgar_dashboard  # noqa: E402
import extract  # noqa: E402
import filing_cache  # noqa: E402
import filing_view  # noqa: E402
import insider  # noqa: E402

ACCESSION = "0001193125-26-323660"
V1, V2 = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
STAGE_TWO = ("extract.py", "xbrl_extract.py", "calc_check.py", "edgar_dashboard.py", "charts.py",
             "valuation.py", "app.py", "textpanel.py", "scan.py", "filing_view.py")


# --------------------------------------------------------------------------- #
# A fake edgartools filing, close enough for extract.extract_filing
# --------------------------------------------------------------------------- #

def _facts_frame() -> pd.DataFrame:
    rows = [
        # concept, value, numeric, unit, currency, decimals, start, end, instant, dimensioned, dimension, member
        ("dei:DocumentFiscalYearFocus", "2025", np.nan, None, None, None, "2025-01-01", "2025-12-31", None,
         False, None, None),
        ("dei:Security12bTitle", "Common stock " + "x" * 600, np.nan, None, None, None, "2025-01-01",
         "2025-12-31", None, False, None, None),
        ("us-gaap:Revenues", "1000", 1000.0, "usd", "USD", "-3", "2025-01-01", "2025-12-31", None,
         False, None, None),
        ("us-gaap:Revenues", "900", 900.0, "usd", "USD", "-3", "2024-01-01", "2024-12-31", None,
         False, None, None),
        ("us-gaap:Revenues", "600", 600.0, "usd", "USD", "-3", "2025-01-01", "2025-12-31", None,
         True, "srt:ProductOrServiceAxis", "acme:WidgetsMember"),
        ("us-gaap:CostOfRevenue", "400", 400.0, "usd", "USD", "-3", "2025-01-01", "2025-12-31", None,
         False, None, None),
        ("us-gaap:GrossProfit", "600", 600.0, "usd", "USD", "-3", "2025-01-01", "2025-12-31", None,
         False, None, None),
        ("us-gaap:Assets", "5000", 5000.0, "usd", "USD", "-3", None, None, "2025-12-31", False, None, None),
        ("us-gaap:IncomeTaxPolicyTextBlock", "<p>" + "Long policy text. " * 80 + "</p>", np.nan, None, None,
         None, "2025-01-01", "2025-12-31", None, False, None, None),
    ]
    columns = ["concept", "value", "numeric_value", "unit_ref", "currency", "decimals", "period_start",
               "period_end", "period_instant", "is_dimensioned", "dimension", "member"]
    frame = pd.DataFrame(rows, columns=columns)
    for column in ("concept", "value", "unit_ref", "currency", "decimals", "period_start", "period_end",
                   "period_instant", "dimension", "member"):
        frame[column] = frame[column].astype("str").where(frame[column].notna(), None).astype("str")
    frame["label"] = frame["concept"].str.split(":").str[-1].astype("str")
    frame["full_dimension_label"] = pd.Series([None] * len(frame), dtype="str")
    frame["statement_type"] = pd.Series(["CoverPage", "CoverPage", "IncomeStatement", "IncomeStatement",
                                         "IncomeStatement", "IncomeStatement", "IncomeStatement",
                                         "BalanceSheet", None], dtype="str")
    frame["statement_types"] = [["CoverPage"]] * 2 + [["IncomeStatement"]] * 5 + [["BalanceSheet"], None]
    frame["fiscal_year"] = np.int64(2025)
    frame["weight"] = 1.0
    return frame


def _income_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "concept": pd.Series(["us-gaap_Revenues", "us-gaap_CostOfRevenue", "us-gaap_GrossProfit"], dtype="str"),
        "label": pd.Series(["Revenue", "Cost of revenue", "Gross profit"], dtype="str"),
        "standard_concept": pd.Series(["Revenue", "CostOfGoodsAndServicesSold", None], dtype="str"),
        "2025-12-31 (FY)": [1000.0, 400.0, 600.0],
        "2024-12-31 (FY)": [900.0, np.nan, np.nan],
        "level": np.array([1, 1, 0], dtype="int64"),
        "abstract": [False, False, False],
        "dimension": [False, False, False],
        "balance": pd.Series(["credit", "debit", "credit"], dtype="str"),
        "weight": [1.0, -1.0, 1.0],
        "parent_concept": pd.Series(["us-gaap_GrossProfit", "us-gaap_GrossProfit", None], dtype="str"),
    })


class _Statement:
    def __init__(self, frame):
        self.frame = frame

    def to_dataframe(self):
        return self.frame.copy()


class _FakeXbrl:
    def __init__(self):
        self._facts = _facts_frame()
        self.facts = SimpleNamespace(to_dataframe=lambda: self._facts.copy())

        def broken():
            raise ValueError("no equity statement")

        self.statements = SimpleNamespace(
            income_statement=lambda: _Statement(_income_frame()),
            balance_sheet=lambda: None,
            cash_flow_statement=lambda: None,
            statement_of_equity=broken,
            comprehensive_income=lambda: None,
        )
        arcs = [SimpleNamespace(parent_id="us-gaap_GrossProfit", child_id="us-gaap_Revenues", weight=1.0, order=1.0),
                SimpleNamespace(parent_id="us-gaap_GrossProfit", child_id="us-gaap_CostOfRevenue", weight=-1.0,
                                order=2.0)]
        self.calculation_trees = {"http://acme/role/Income": SimpleNamespace(definition="100 - Statement - Income",
                                                                             all_arcs=arcs)}
        node = SimpleNamespace(parent=None, children=["us-gaap_Revenues"], depth=0, order=0.0, preferred_label=None,
                               is_abstract=True, labels={"http://www.xbrl.org/2003/role/label": "Income [Abstract]"})
        self.presentation_trees = {"http://acme/role/Income": SimpleNamespace(
            definition="Income", root_element_ids=["us-gaap_IncomeAbstract"],
            all_nodes={"us-gaap_IncomeAbstract": node})}
        self.period_of_report = "2025-12-31"
        self.entity_info = {"entity_name": "ACME", "fiscal_year": 2025}


class _FakeFiling:
    def __init__(self, xbrl=True):
        self.accession_no, self.cik, self.company = ACCESSION, 42, "ACME"
        self.form, self.filing_date, self.report_date = "10-K", "2026-02-20", "2025-12-31"
        self._xbrl = _FakeXbrl() if xbrl else None

    def xbrl(self):
        return self._xbrl


def _round_trip(entry: dict) -> dict:
    return json.loads(gzip.decompress(gzip.compress(json.dumps(entry, default=filing_cache._plain).encode())))


class DumpTest(unittest.TestCase):
    def setUp(self):
        self.filing = _FakeFiling()
        self.entry = _round_trip(dump.dump_filing(self.filing))

    def test_frames_come_back_with_the_same_values_and_types(self):
        frame = _facts_frame()
        frame["when"] = pd.to_datetime("2025-12-31")
        back = dump.decode_frame(_round_trip(dump.encode_frame(frame)))
        pd.testing.assert_frame_equal(back, frame)

    def test_every_fact_is_kept_and_only_long_note_text_is_dropped(self):
        facts = filing_view.FilingView(self.entry).xbrl().facts.to_dataframe()
        self.assertEqual(len(facts), len(_facts_frame()))
        self.assertEqual(self.entry["dropped_text"], 1)
        policy = facts[facts["concept"] == "us-gaap:IncomeTaxPolicyTextBlock"]
        self.assertTrue(policy["value"].isna().all())
        title = facts.loc[facts["concept"] == "dei:Security12bTitle", "value"].iloc[0]
        self.assertGreater(len(title), dump.TEXT_LIMIT)           # cover-page text is always kept

    def test_the_dump_says_what_the_filing_is(self):
        self.assertEqual(self.entry["schema"], dump.SCHEMA)
        self.assertEqual(self.entry["filing"]["accession"], ACCESSION)
        self.assertEqual(self.entry["filing"]["form"], "10-K")
        self.assertEqual(self.entry["dei"]["dei:DocumentFiscalYearFocus"], "2025")
        self.assertEqual(self.entry["labels"]["us-gaap_IncomeAbstract"]["http://www.xbrl.org/2003/role/label"],
                         "Income [Abstract]")
        self.assertEqual(self.entry["calculation"]["http://acme/role/Income"]["arcs"][1][:3],
                         ["us-gaap_GrossProfit", "us-gaap_CostOfRevenue", -1.0])
        self.assertIn("ValueError", self.entry["statements"]["equity"]["error"])

    def test_stage_two_on_the_dump_matches_stage_two_on_the_live_filing(self):
        live = extract.extract_filing(self.filing, "2025-12-31")
        stored = extract.extract_filing(filing_view.FilingView(self.entry), "2025-12-31")
        self.assertEqual(json.dumps(stored, sort_keys=True, default=str), json.dumps(live, sort_keys=True, default=str))
        self.assertEqual(stored["row"]["Revenues"], 1000.0)
        self.assertEqual(len(stored["statements"]), 3)
        self.assertEqual([check["status"] for check in stored["checks"]], ["ok"])

    def test_a_filing_without_xbrl(self):
        entry = dump.dump_filing(_FakeFiling(xbrl=False))
        self.assertFalse(entry["xbrl"])
        self.assertIsNone(filing_view.FilingView(entry).xbrl())
        self.assertEqual(extract.extract_filing(filing_view.FilingView(entry), "2025-12-31"), {"xbrl": False})


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class DumpStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_round_trip_in_the_schema_folder_with_the_reader_stamp(self):
        self.assertTrue(filing_cache.store_dump(self.root, 789019, ACCESSION, {"schema": dump.SCHEMA, "xbrl": False,
                                                                                "n": np.int64(3)}))
        path = Path(self.root) / "filings" / f"schema-{dump.SCHEMA}" / "789019" / f"{ACCESSION}.json.gz"
        self.assertTrue(path.is_file())
        entry = filing_cache.load_dump(self.root, "0000789019", ACCESSION)
        self.assertEqual(entry["n"], 3)
        self.assertEqual(entry["reader"], filing_cache.reader())
        self.assertEqual(entry["reader_id"], filing_cache.reader_id())

    def test_another_schema_is_a_miss_and_is_kept(self):
        old = Path(self.root) / "filings" / "schema-0" / "1" / f"{ACCESSION}.json.gz"
        old.parent.mkdir(parents=True)
        old.write_bytes(gzip.compress(b'{"schema": 0}'))
        self.assertIsNone(filing_cache.load_dump(self.root, 1, ACCESSION))
        self.assertEqual(filing_cache.dumped_elsewhere(self.root, 1, ACCESSION), [0])
        filing_cache.store_dump(self.root, 1, ACCESSION, {"schema": dump.SCHEMA})
        self.assertTrue(old.is_file())

    def test_absent_corrupt_wrong_schema_or_disabled_is_a_miss(self):
        self.assertIsNone(filing_cache.load_dump(self.root, 1, ACCESSION))
        path = filing_cache.dump_path(self.root, 1, ACCESSION)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not gzip")
        self.assertIsNone(filing_cache.load_dump(self.root, 1, ACCESSION))
        path.write_bytes(gzip.compress(b"{half"))
        self.assertIsNone(filing_cache.load_dump(self.root, 1, ACCESSION))
        path.write_bytes(gzip.compress(json.dumps({"schema": dump.SCHEMA + 1}).encode()))
        self.assertIsNone(filing_cache.load_dump(self.root, 1, ACCESSION))
        self.assertFalse(filing_cache.store_dump(None, 1, ACCESSION, {}))
        self.assertIsNone(filing_cache.load_dump(None, 1, ACCESSION))

    def test_only_accessions_become_paths(self):
        for name in ("../escape", "form4", "x" * 20):
            self.assertFalse(filing_cache.store_dump(self.root, 1, name, {}))
        self.assertFalse(filing_cache.store_dump(self.root, "../1", ACCESSION, {}))
        self.assertEqual(list(Path(self.root).rglob("*.gz")), [])


class Form4StoreTest(unittest.TestCase):
    """Form 4 keeps the code-hash versions."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_round_trip_with_numpy_values_in_a_version_folder(self):
        data = {"trades": [{"shares": np.float64(1.5), "year": np.int64(2025), "buy": np.bool_(False)}]}
        filing_cache.store(self.root, "form4", V1, 789019, "form4", data)
        meta = json.loads((Path(self.root) / "form4" / V1 / "version.json").read_text())
        self.assertEqual(set(meta["code"]), {"insider.py", "edgartools"})
        self.assertEqual(filing_cache.load(self.root, "form4", V1, "0000789019", "form4"),
                         {"trades": [{"shares": 1.5, "year": 2025, "buy": False}]})

    def test_another_version_is_a_miss_and_is_kept(self):
        filing_cache.store(self.root, "form4", V1, 1, "form4", {"a": 1})
        self.assertIsNone(filing_cache.load(self.root, "form4", V2, 1, "form4"))
        self.assertTrue(filing_cache.cached_elsewhere(self.root, "form4", V2, 1, "form4"))
        filing_cache.store(self.root, "form4", V2, 1, "form4", {"a": 2})
        self.assertEqual(filing_cache.load(self.root, "form4", V1, 1, "form4"), {"a": 1})

    def test_unknown_kinds_and_names_never_become_paths(self):
        filing_cache.store(self.root, "form4", V1, 1, "../escape", {})
        filing_cache.store(self.root, "annual", V1, 1, ACCESSION, {})      # the old kind is gone
        self.assertEqual(list(Path(self.root).rglob("*")), [])


# --------------------------------------------------------------------------- #
# Readers: what may change the stamp, and the classification guard
# --------------------------------------------------------------------------- #

class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.here = Path(tempfile.mkdtemp())
        for name in ("dump.py", "readers.json", "insider.py", *STAGE_TWO):
            if (ROOT / name).is_file():
                shutil.copy(ROOT / name, self.here / name)
        patcher = unittest.mock.patch.object(filing_cache, "HERE", self.here)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_current_reader_is_classified(self):
        # Fails after any edit to dump.py or an edgartools upgrade until it is
        # classified:  python filing_cache.py classify compatible|format|bug "why"
        record = filing_cache.classification()
        self.assertIsNotNone(record, f"reader {filing_cache.reader_id()} ({filing_cache.reader()}) is not in "
                                     "readers.json: run `python filing_cache.py classify ...`")
        self.assertEqual(record["schema"], dump.SCHEMA)
        self.assertIn(record["class"], filing_cache.CLASSES)

    def test_a_reader_edit_changes_the_stamp_and_needs_a_classification(self):
        before = filing_cache.reader_id()
        with open(self.here / "dump.py", "a") as handle:
            handle.write("\n# changed\n")
        self.assertNotEqual(filing_cache.reader_id(), before)
        self.assertIsNone(filing_cache.classification())
        with redirect_stdout(io.StringIO()):
            filing_cache.main(["classify", "compatible", "a comment only"])
        self.assertEqual(filing_cache.classification()["class"], "compatible")
        self.assertIn(filing_cache.reader_id(), (self.here / "readers.json").read_text())

    def test_line_endings_do_not_change_the_stamp(self):
        before = filing_cache.reader_id()
        path = self.here / "dump.py"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(filing_cache.reader_id(), before)

    def test_stage_two_edits_do_not_change_the_stamp(self):
        before = (filing_cache.reader_id(), filing_cache.version("form4"))
        for name in STAGE_TWO:
            if (self.here / name).is_file():
                with open(self.here / name, "a") as handle:
                    handle.write("\n# margins changed\n")
        self.assertEqual((filing_cache.reader_id(), filing_cache.version("form4")), before)

    def test_stage_two_modules_are_never_hashed(self):
        hashed = set(filing_cache.READER_FILES) | {name for names in filing_cache.KINDS.values() for name in names}
        for module in STAGE_TWO:
            self.assertNotIn(module, hashed)

    def test_hashed_modules_import_only_hashed_local_modules(self):
        # Otherwise a change in the imported module would change what is
        # stored without changing the stamp: stale entries served as fresh.
        local = {path.stem for path in ROOT.glob("*.py")}
        groups = {"reader": filing_cache.READER_FILES, **filing_cache.KINDS}
        for kind, names in groups.items():
            for name in names:
                tree = ast.parse((ROOT / name).read_text())
                imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                            if isinstance(node, ast.Import) for alias in node.names}
                imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                             if isinstance(node, ast.ImportFrom) and node.module}
                allowed = {Path(other).stem for other in names} | {"filing_cache"}
                self.assertEqual((imported & local) - allowed, set(), f"{name} ({kind})")


# --------------------------------------------------------------------------- #
# Command line: status, invalidate, prune
# --------------------------------------------------------------------------- #

def _entry(form="10-K", filed="2020-02-01", reader_id=None):
    return {"schema": dump.SCHEMA, "xbrl": True, "filing": {"form": form, "filed": filed}}


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.accessions = {"0000000001-20-000001": ("10-K", "2020-02-01"),
                           "0000000001-21-000001": ("10-K", "2021-02-01"),
                           "0000000001-21-000002": ("10-Q", "2021-05-01")}
        for accession, (form, filed) in self.accessions.items():
            filing_cache.store_dump(self.root, 1, accession, _entry(form, filed))
        # One entry from an older reader, as if dump.py had a bug then.
        path = filing_cache.dump_path(self.root, 2, "0000000002-19-000001")
        path.parent.mkdir(parents=True)
        path.write_bytes(gzip.compress(json.dumps({**_entry(filed="2019-03-01"), "reader_id": V1}).encode()))
        (self.root / "annual" / V1 / "1").mkdir(parents=True)        # the 1.13 layout
        (self.root / "123").mkdir()                                  # the 1.12 layout
        (self.root / "filings" / "schema-0").mkdir()

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            code = filing_cache.main(["--root", str(self.root), *args])
        return out.getvalue(), code

    def remaining(self):
        return sorted(accession for _, accession, _ in filing_cache.entries(self.root))

    def test_status_counts_forms_and_readers_and_names_old_layouts(self):
        out, _ = self.run_cli("status")
        self.assertIn(f"schema-{dump.SCHEMA}", out)
        self.assertIn("2 companies        4 filings", out)
        self.assertIn("forms: 3 10-K, 1 10-Q", out)
        self.assertIn(f"reader {filing_cache.reader_id()}: 3 filings", out)
        self.assertIn(f"reader {V1}: 1 filings (not in readers.json)", out)
        self.assertIn("legacy: 123/, annual/", out)

    def test_invalidate_removes_only_the_matching_entries_after_a_dry_run(self):
        out, _ = self.run_cli("invalidate", "--reader", V1)
        self.assertIn("dry run (1 items)", out)
        self.assertEqual(len(self.remaining()), 4)
        self.run_cli("invalidate", "--reader", V1, "--yes")
        self.assertEqual(self.remaining(), sorted(self.accessions))
        self.run_cli("invalidate", "--form", "10-Q", "--yes")
        self.assertNotIn("0000000001-21-000002", self.remaining())
        self.run_cli("invalidate", "--form", "10-K", "--before", "2021-01-01", "--yes")
        self.assertEqual(self.remaining(), ["0000000001-21-000001"])

    def test_invalidate_needs_a_filter(self):
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
            self.run_cli("invalidate", "--yes")
        self.assertEqual(len(self.remaining()), 4)

    def test_prune_removes_old_schemas_and_layouts_but_never_the_dump(self):
        out, _ = self.run_cli("prune")
        self.assertIn("dry run", out)
        self.run_cli("prune", "--yes")
        self.assertFalse((self.root / "annual").exists())
        self.assertFalse((self.root / "123").exists())
        self.assertFalse((self.root / "filings" / "schema-0").exists())
        self.assertEqual(len(self.remaining()), 4)


# --------------------------------------------------------------------------- #
# The pipeline: download once, then work from the dump
# --------------------------------------------------------------------------- #

class _AnnualFiling:
    def __init__(self, accession, report_date, form="10-K"):
        self.accession_no, self.report_date, self.form = accession, report_date, form


def _dumped(filing):
    entry = {"schema": dump.SCHEMA, "xbrl": filing.report_date >= "2010",
             "filing": {"accession": filing.accession_no, "form": filing.form, "report_date": filing.report_date}}
    if entry["xbrl"]:
        entry["facts"] = dump.encode_frame(pd.DataFrame())
    return entry


def _extracted(view, period_key):
    return {"xbrl": True,
            "row": {"period_of_report": period_key, "fiscal_year": int(period_key[:4]), "form": view.form,
                    "currency": "USD", "ads": False, "ads_ratio": None, "ads_ratio_source": None},
            "statements": [], "facts": [], "checks": []}


class BuildDatasetDumpTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        years = range(2025, 2006, -1)       # 19 10-Ks, the last three without XBRL
        self.filings = [_AnnualFiling(f"0000000001-{year % 100:02d}-000001", f"{year}-12-31") for year in years]
        self.company = unittest.mock.Mock(cik=42)
        self.company.name = "ACME"
        self.company.get_filings.side_effect = lambda form, amendments: (
            self.filings if form == "10-K" else [])

    def build(self, n_filings, stage_two=_extracted):
        reader = unittest.mock.Mock(side_effect=_dumped)
        with unittest.mock.patch.object(edgar_dashboard, "Company", return_value=self.company), \
                unittest.mock.patch.object(dump, "dump_filing", reader), \
                unittest.mock.patch.object(extract, "extract_filing", stage_two), \
                redirect_stdout(io.StringIO()) as out:
            ratios = edgar_dashboard.build_dataset("ACME", n_filings, self.root)[0]
        return ratios, reader.call_count, out.getvalue()

    def test_max_dumps_every_filing_and_a_rerun_downloads_none(self):
        ratios, read, log = self.build(None)
        self.assertEqual(read, 19)
        self.assertEqual(len(ratios), 16)
        self.assertIn("reading all of them", log)
        self.assertIn("0 from the dump, 19 downloaded and dumped", log)
        again, read, log = self.build(None)
        self.assertEqual(read, 0)                     # filings without XBRL are dumped too
        self.assertEqual(again["period_of_report"].tolist(), ratios["period_of_report"].tolist())
        self.assertIn("19 from the dump, 0 downloaded", log)

    def test_a_new_filing_is_the_only_one_downloaded(self):
        self.build(5)
        self.filings.insert(0, _AnnualFiling("0000000001-26-000001", "2026-12-31"))
        ratios, read, _ = self.build(5)
        self.assertEqual(read, 1)
        self.assertEqual(ratios["period_of_report"].iloc[-1], "2026-12-31")
        self.assertEqual(len(ratios), 5)

    def test_a_stage_two_fix_needs_no_download(self):
        self.build(3)

        def fixed(view, period_key):
            out = _extracted(view, period_key)
            out["row"]["Revenues"] = 1.0
            return out

        ratios, read, _ = self.build(3, stage_two=fixed)
        self.assertEqual(read, 0)
        self.assertEqual(ratios["Revenues"].tolist(), [1.0, 1.0, 1.0])

    def test_only_a_schema_bump_reads_again_and_going_back_finds_the_old_dumps(self):
        self.build(3)
        with unittest.mock.patch.object(dump, "SCHEMA", dump.SCHEMA + 1):
            _, read, _ = self.build(3)
        self.assertEqual(read, 3)
        _, read, _ = self.build(3)
        self.assertEqual(read, 0)


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
