#!/usr/bin/env python3
"""Market scan: dump every XBRL filer, a batch at a time, and report what went wrong.

The universe is SEC's ``company_tickers_exchange.json``, one row per company
(CIK), in alphabetical order of its ticker; it is saved the first time, so
the order never moves (``universe --refresh`` only appends new companies).

``next --count N`` takes the next N companies no batch has taken yet. For
each one it:

1. lists its annual reports (10-K, 10-KT, 20-F, 40-F; amendments skipped) and
   the 10-Qs filed after the latest one;
2. dumps every filing not in the dump yet (``dump.py``: the only step that
   talks to SEC);
3. runs stage 2 from the dump: the ratio rows, the statements, the
   calculation check (``edgar_dashboard.assemble``), and records every
   problem as an issue: a total that does not add up, a key metric missing
   for a year, a filing that could not be downloaded or read.

``rerun --batch K`` repeats step 3 for that batch from the dump alone, with
no SEC request: fix the code, rerun, compare. ``--retry-failed`` also
retries the downloads that failed. ``resume --batch K`` finishes a batch that
was stopped (the companies done are skipped).

Everything lives under ``data/market/`` (or ``$STOCKY_MARKET_DIR``):
``scan.db`` (universe, batches, runs, companies, filings, issues),
``logs/run-NNNN.log`` (one per run, every step with its time) and
``companies/<CIK>/`` (``financial_data.csv``, ``calc_check.csv``,
``issues.csv``). The dumps go to the shared filing cache.

Usage:
    python scan.py next --count 100
    python scan.py rerun --batch 1 [--retry-failed] [--cik 320193]
    python scan.py resume --batch 1
    python scan.py status [--batch 1]
    python scan.py issues --batch 1 > issues.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import calc_check
import filing_cache

# Below SEC's 10 requests per second together with a single-ticker run in the
# app (which drops to 4 while a scan runs). Set before edgartools is imported.
RATE_LIMIT = "5"
# Set by the app: the run log file is the only output (stdout would repeat it).
QUIET = os.environ.get("STOCKY_SCAN_QUIET") == "1"

HERE = Path(__file__).resolve().parent
UNIVERSE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F")
# Analysed as fiscal years; a 10-KT covers a transition period (often a few
# months) and stays in the dump until quarterly periods (plan 5.4) handle it.
ANALYSED_FORMS = ("10-K", "20-F", "40-F")
QUARTERLY_FORM = "10-Q"
# A 10-K is overdue when more 10-Qs than this follow it.
MAX_QUARTERS = 3
# Missing in a fiscal year: worth a look (a tag the pipeline does not know yet).
KEY_METRICS = ("Revenues", "NetIncomeLoss", "OperatingIncomeLoss", "OperatingCashFlow", "StockholdersEquity")
MAX_COUNT = 10_000
RESULTS = ("ok", "partial", "failed", "no_filings", "no_xbrl")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS universe (
    cik INTEGER PRIMARY KEY, ticker TEXT, name TEXT, exchange TEXT, position INTEGER UNIQUE, added TEXT);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, created TEXT, size INTEGER, first_position INTEGER, last_position INTEGER);
CREATE TABLE IF NOT EXISTS batch_companies (
    batch INTEGER, cik INTEGER UNIQUE, position INTEGER, PRIMARY KEY (batch, cik));
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, batch INTEGER, kind TEXT, started TEXT, finished TEXT, status TEXT,
    pid INTEGER, log TEXT, total INTEGER, done INTEGER DEFAULT 0, current TEXT, downloaded INTEGER DEFAULT 0,
    from_dump INTEGER DEFAULT 0, download_failed INTEGER DEFAULT 0, message TEXT, reader TEXT, options TEXT);
CREATE TABLE IF NOT EXISTS companies (
    cik INTEGER PRIMARY KEY, ticker TEXT, name TEXT, batch INTEGER, last_run INTEGER, updated TEXT,
    result TEXT, error TEXT, annual_found INTEGER, annual_dumped INTEGER, annual_xbrl INTEGER,
    annual_analysed INTEGER, transition INTEGER, quarterly_found INTEGER, quarterly_dumped INTEGER,
    overdue_10k INTEGER, filings_failed INTEGER, checks_total INTEGER, checks_ok INTEGER,
    checks_mismatch INTEGER, checks_incomplete INTEGER, missing_metrics INTEGER, issues INTEGER,
    currency TEXT, first_fy INTEGER, last_fy INTEGER, seconds REAL, analysed_with TEXT);
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY, cik INTEGER, form TEXT, kind TEXT, report_date TEXT, filed TEXT,
    dumped INTEGER, xbrl INTEGER, bytes INTEGER, seconds REAL, reader TEXT, error TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS filings_cik ON filings (cik);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY, cik INTEGER, ticker TEXT, run INTEGER, severity TEXT, kind TEXT,
    accession TEXT, form TEXT, fiscal_year INTEGER, period TEXT, statement TEXT, concept TEXT,
    label TEXT, reported REAL, computed REAL, difference REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS issues_cik ON issues (cik);
"""


def _pipeline():
    """``edgar_dashboard`` (and edgartools), imported only by a run: the app reads reports without them."""
    os.environ.setdefault("EDGAR_RATE_LIMIT_PER_SEC", RATE_LIMIT)
    import edgar_dashboard

    return edgar_dashboard


def _dump():
    _pipeline()
    import dump

    return dump


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def market_dir() -> Path:
    data = Path(os.environ.get("STOCKY_DATA_DIR", HERE / "data"))
    return Path(os.environ.get("STOCKY_MARKET_DIR") or data / "market")


def connect(root: Path | None = None) -> sqlite3.Connection:
    root = root or market_dir()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "scan.db", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA_SQL)
    return db


# --------------------------------------------------------------------------- #
# The run log
# --------------------------------------------------------------------------- #

class Log:
    """Every line with its time, to the run's log file and to stdout."""

    def __init__(self, path: Path | None):
        self.path = path
        self.handle = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("a", encoding="utf-8")

    def __call__(self, message: str = "") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = "\n".join(f"{stamp} {line}" if line.strip() else line for line in str(message).splitlines()) + "\n"
        if not QUIET or not self.handle:
            sys.stdout.write(text)
            sys.stdout.flush()
        if self.handle:
            self.handle.write(text)
            self.handle.flush()

    def trace(self, prefix: str = "") -> None:
        """The current exception's traceback, indented, for debugging later."""
        self("\n".join(f"        {prefix}{line}" for line in traceback.format_exc().rstrip().splitlines()))

    def close(self) -> None:
        if self.handle:
            self.handle.close()


# --------------------------------------------------------------------------- #
# Universe and batches
# --------------------------------------------------------------------------- #

def fetch_universe() -> list[dict]:
    """SEC's list of companies with a ticker: one row per CIK, sorted by ticker."""
    identity = os.environ.get("EDGAR_IDENTITY", "Data Analyst analyst@example.com")
    request = urllib.request.Request(UNIVERSE_URL, headers={"User-Agent": identity, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    fields = payload["fields"]
    rows: dict[int, dict] = {}
    for values in payload["data"]:
        item = dict(zip(fields, values))
        cik = int(item["cik"])
        ticker = str(item.get("ticker") or "").upper()
        if not ticker:
            continue
        # SEC lists a company's main ticker first (GOOGL before GOOG).
        rows.setdefault(cik, {"cik": cik, "ticker": ticker, "name": item.get("name") or "",
                              "exchange": item.get("exchange") or ""})
    return sorted(rows.values(), key=lambda row: (row["ticker"], row["cik"]))


def load_universe(db: sqlite3.Connection, log, refresh: bool = False) -> int:
    """Save the universe the first time; ``refresh`` appends companies new since then."""
    count = db.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    if count and not refresh:
        return count
    log(f"[*] Universe: downloading {UNIVERSE_URL}")
    rows = fetch_universe()
    known = {row[0] for row in db.execute("SELECT cik FROM universe")}
    position = db.execute("SELECT COALESCE(MAX(position), 0) FROM universe").fetchone()[0]
    added = 0
    with db:
        for row in rows:
            if row["cik"] in known:
                continue
            position += 1
            added += 1
            db.execute("INSERT INTO universe VALUES (?, ?, ?, ?, ?, ?)",
                       (row["cik"], row["ticker"], row["name"], row["exchange"], position, _now()))
    total = count + added
    log(f"[+] Universe: {total} companies ({added} new), in alphabetical order of ticker")
    return total


def new_batch(db: sqlite3.Connection, count: int) -> tuple[int, list[sqlite3.Row]]:
    """The next ``count`` companies no batch has taken, as a new batch."""
    rows = db.execute("""SELECT u.* FROM universe u LEFT JOIN batch_companies b ON b.cik = u.cik
                         WHERE b.cik IS NULL ORDER BY u.position LIMIT ?""", (count,)).fetchall()
    if not rows:
        raise SystemExit("every company in the universe is in a batch already (try universe --refresh)")
    with db:
        cursor = db.execute("INSERT INTO batches (created, size, first_position, last_position) VALUES (?, ?, ?, ?)",
                            (_now(), len(rows), rows[0]["position"], rows[-1]["position"]))
        batch = cursor.lastrowid
        db.executemany("INSERT INTO batch_companies VALUES (?, ?, ?)",
                       [(batch, row["cik"], row["position"]) for row in rows])
    return batch, rows


def batch_members(db: sqlite3.Connection, batch: int, cik: str | None = None) -> list[sqlite3.Row]:
    query = """SELECT u.* FROM batch_companies b JOIN universe u ON u.cik = b.cik WHERE b.batch = ?"""
    args: list = [batch]
    if cik:
        query += " AND u.cik = ?"
        args.append(int(cik))
    return db.execute(query + " ORDER BY b.position", args).fetchall()


# --------------------------------------------------------------------------- #
# Step 1-2: list and dump (the only SEC requests)
# --------------------------------------------------------------------------- #

def _date(value) -> str | None:
    return None if value in (None, "") else str(value)


def _record_filing(db, cik: int, filing, kind: str) -> None:
    accession = filing.accession_no
    db.execute("""INSERT INTO filings (accession, cik, form, kind, report_date, filed, dumped, updated)
                  VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                  ON CONFLICT(accession) DO UPDATE SET form = excluded.form, kind = excluded.kind,
                  report_date = COALESCE(excluded.report_date, filings.report_date),
                  filed = COALESCE(excluded.filed, filings.filed)""",
               (accession, cik, filing.form, kind, _date(_pipeline()._report_date(filing)),
                _date(getattr(filing, "filing_date", None)), _now()))


def list_filings(db, company, cik: int, log) -> dict:
    """Record the company's annual reports and the 10-Qs after the latest one; the live filings by accession."""
    annual = _pipeline().list_annual(company, forms=ANNUAL_FORMS, log=log)
    quarterly = []
    latest = _pipeline()._report_date(annual[0]) if annual else ""
    if latest:
        try:
            for filing in company.get_filings(form=QUARTERLY_FORM, amendments=False):
                if filing.form != QUARTERLY_FORM:
                    continue
                period = _pipeline()._report_date(filing)
                if period and period > latest:
                    quarterly.append(filing)
                elif period and period <= latest:
                    break                         # newest first: the rest are older
        except Exception as exc:  # noqa: BLE001 - quarterly reports never sink the company
            log(f"    [!] 10-Q list failed ({type(exc).__name__}: {exc})")
            log.trace()
    with db:
        for filing in annual:
            _record_filing(db, cik, filing, "transition" if filing.form == "10-KT" else "annual")
        for filing in quarterly:
            _record_filing(db, cik, filing, "quarterly")
    counts = {form: sum(f.form == form for f in annual) for form in ANNUAL_FORMS}
    log(f"    [+] {len(annual)} annual reports ("
        + (", ".join(f"{n} {form}" for form, n in counts.items() if n) or "none")
        + f"), {len(quarterly)} 10-Qs after the latest one" + (f" ({latest})" if latest else ""))
    return {filing.accession_no: filing for filing in annual + quarterly}


def dump_filings(db, cik: int, live: dict, cache_dir: Path, log, stats: dict, retry_failed: bool = True) -> None:
    """Dump every recorded filing of ``cik`` that is not in the dump yet (downloads from SEC)."""
    rows = db.execute("SELECT * FROM filings WHERE cik = ? ORDER BY report_date DESC", (cik,)).fetchall()
    for position, row in enumerate(rows, 1):
        accession = row["accession"]
        path = filing_cache.dump_path(cache_dir, cik, accession)
        if path is not None and path.is_file():
            if not row["dumped"]:
                entry = filing_cache.load_dump(cache_dir, cik, accession) or {}
                _mark_dumped(db, accession, entry, path, None)
            stats["from_dump"] += 1
            continue
        if row["error"] and not retry_failed:
            continue
        filing = live.get(accession)
        if filing is None:
            continue
        label = f"{row['form']} {row['report_date'] or '?'} ({accession})"
        started = time.monotonic()
        try:
            entry = _dump().dump_filing(filing)
            if not filing_cache.store_dump(cache_dir, cik, accession, entry):
                raise OSError("the dump could not be written (see the warning above)")
        except Exception as exc:  # noqa: BLE001 - one filing never stops the company
            stats["download_failed"] += 1
            error = f"{type(exc).__name__}: {exc}"
            log(f"    [!] {position}/{len(rows)} {label}: download or read failed: {error}")
            log.trace()
            with db:
                db.execute("UPDATE filings SET dumped = 0, error = ?, seconds = ?, updated = ? WHERE accession = ?",
                           (error, time.monotonic() - started, _now(), accession))
            continue
        stats["downloaded"] += 1
        seconds = time.monotonic() - started
        size = path.stat().st_size if path is not None and path.is_file() else None
        facts = _fact_count(entry)
        log(f"    [+] {position}/{len(rows)} {label}: dumped in {seconds:.1f} s, "
            + (f"{facts} facts, {(size or 0) / 1024:.0f} KB" if entry.get("xbrl") else "no XBRL")
            + (f"; parts failed: {entry['errors']}" if entry.get("errors") else ""))
        _mark_dumped(db, accession, {**entry, "reader_id": filing_cache.reader_id()}, path, seconds)


def _fact_count(entry: dict) -> int:
    data = (entry.get("facts") or {}).get("data") or []
    return len(data[0]) if data else 0


def _mark_dumped(db, accession: str, entry: dict, path: Path | None, seconds: float | None) -> None:
    size = path.stat().st_size if path is not None and path.is_file() else None
    with db:
        db.execute("""UPDATE filings SET dumped = 1, xbrl = ?, bytes = ?, seconds = COALESCE(?, seconds),
                      reader = ?, error = NULL, updated = ? WHERE accession = ?""",
                   (1 if entry.get("xbrl") else 0, size, seconds, entry.get("reader_id"), _now(), accession))


# --------------------------------------------------------------------------- #
# Step 3: stage 2 from the dump, and the issues
# --------------------------------------------------------------------------- #

def _issue(kind: str, severity: str, **fields) -> dict:
    return {"kind": kind, "severity": severity, **fields}


def analyse_company(db, cik: int, ticker: str, cache_dir: Path, out_dir: Path, log) -> tuple[dict, list[dict]]:
    """Stage 2 from the dump alone: company summary fields and its issues."""
    rows = db.execute("SELECT * FROM filings WHERE cik = ? ORDER BY report_date DESC", (cik,)).fetchall()
    annual = [row for row in rows if row["kind"] == "annual"]
    quarterly = [row for row in rows if row["kind"] == "quarterly"]
    issues: list[dict] = []
    summary = {
        "annual_found": len(annual) + sum(row["kind"] == "transition" for row in rows),
        "annual_dumped": sum(bool(row["dumped"]) for row in rows if row["kind"] != "quarterly"),
        "annual_xbrl": sum(bool(row["xbrl"]) for row in annual),
        "transition": sum(row["kind"] == "transition" for row in rows),
        "quarterly_found": len(quarterly),
        "quarterly_dumped": sum(bool(row["dumped"]) for row in quarterly),
        "overdue_10k": int(len(quarterly) > MAX_QUARTERS),
        "filings_failed": sum(bool(row["error"]) and not row["dumped"] for row in rows),
    }
    for row in rows:
        if row["error"] and not row["dumped"]:
            issues.append(_issue("download_failed", "error", accession=row["accession"], form=row["form"],
                                 period=row["report_date"], detail=row["error"]))
    if summary["overdue_10k"]:
        issues.append(_issue("overdue_10k", "info", form=QUARTERLY_FORM,
                             detail=f"{len(quarterly)} 10-Qs after the latest annual report"))
    if not annual:
        return {**summary, "result": "no_filings", "error": "no annual reports (10-K, 20-F, 40-F)"}, issues

    by_accession = {row["accession"]: row for row in annual}
    candidates = [SimpleNamespace(accession_no=row["accession"], form=row["form"], report_date=row["report_date"])
                  for row in annual if row["dumped"] and row["form"] in ANALYSED_FORMS]

    def entry_of(filing):
        entry = filing_cache.load_dump(cache_dir, cik, filing.accession_no)
        if entry is None:
            raise FileNotFoundError("not in the dump (deleted, or written by another schema)")
        for part, error in (entry.get("errors") or {}).items():
            issues.append(_issue("dump_part_failed", "warning", accession=filing.accession_no, form=filing.form,
                                 period=filing.report_date, concept=part, detail=error))
        return entry, "dump"

    failures: list[dict] = []
    try:
        raw, _statements, _facts, checks = _pipeline().assemble(candidates, None, entry_of, ticker,
                                                             log=log, failures=failures)
    except RuntimeError as exc:
        for failure in failures:
            issues.append(_issue("analysis_failed", "error", accession=failure["accession"], form=failure["form"],
                                 period=failure["period"], detail=failure["error"]))
        result = "no_xbrl" if not failures and summary["annual_xbrl"] == 0 else "failed"
        return {**summary, "annual_analysed": 0, "result": result, "error": str(exc)}, issues
    for failure in failures:
        issues.append(_issue("analysis_failed", "error", accession=failure["accession"], form=failure["form"],
                             period=failure["period"], detail=failure["error"]))
        log(failure["traceback"].rstrip())

    period_accession = {row["report_date"]: row["accession"] for row in annual}
    year_of = dict(zip(raw["period_of_report"], raw["fiscal_year"]))
    derived = _pipeline().clean_and_derive(raw.copy())
    out_dir.mkdir(parents=True, exist_ok=True)
    derived.to_csv(out_dir / "financial_data.csv", index=False)
    if not checks.empty:
        checks.to_csv(out_dir / "calc_check.csv", index=False)

    # Every total that does not add up: the main thing to fix.
    for check in checks.to_dict("records") if not checks.empty else []:
        if check["status"] != calc_check.STATUS_MISMATCH:
            continue
        accession = period_accession.get(check["period_of_report"])
        issues.append(_issue(
            "check_mismatch", "warning" if check["kind"] == "statement" else "info",
            accession=accession, form=(by_accession.get(accession) or {"form": None})["form"],
            fiscal_year=check.get("fiscal_year"), period=check["period_of_report"], statement=check["statement"],
            concept=check["concept"], label=check["label"], reported=check["reported"],
            computed=check["computed"], difference=check["difference"],
            detail=check.get("hint") or f"{check['parts']} parts; tolerance {check['tolerance']:g}"))
    # A key metric the pipeline found no tag for.
    for row in derived.to_dict("records"):
        missing = [metric for metric in KEY_METRICS if pd.isna(row.get(metric))]
        for metric in missing:
            accession = period_accession.get(row["period_of_report"])
            issues.append(_issue("missing_metric", "warning", accession=accession,
                                 form=(by_accession.get(accession) or {"form": None})["form"],
                                 fiscal_year=row.get("fiscal_year"), period=row["period_of_report"],
                                 concept=metric, detail="no tag or statement line found"))

    count = (lambda status: int((checks["status"] == status).sum()) if not checks.empty else 0)
    statuses = {"ok": count(calc_check.STATUS_OK), "mismatch": count(calc_check.STATUS_MISMATCH),
                "incomplete": count(calc_check.STATUS_INCOMPLETE)}
    errors = [issue for issue in issues if issue["severity"] == "error"]
    summary.update({
        "annual_analysed": len(raw),
        "checks_total": len(checks), "checks_ok": statuses["ok"], "checks_mismatch": statuses["mismatch"],
        "checks_incomplete": statuses["incomplete"],
        "missing_metrics": sum(issue["kind"] == "missing_metric" for issue in issues),
        "currency": str(raw["currency"].iloc[-1]) if "currency" in raw.columns and len(raw) else None,
        "first_fy": int(min(year_of.values())), "last_fy": int(max(year_of.values())),
        "result": "partial" if errors else "ok", "error": errors[0]["detail"] if errors else None,
    })
    return summary, issues


def _save_company(db, cik: int, member, batch: int, run: int, summary: dict, issues: list[dict],
                  seconds: float, out_dir: Path) -> None:
    """One transaction: the company's row and its issues replace the previous run's."""
    fields = {"annual_found": 0, "annual_dumped": 0, "annual_xbrl": 0, "annual_analysed": 0, "transition": 0,
              "quarterly_found": 0, "quarterly_dumped": 0, "overdue_10k": 0, "filings_failed": 0,
              "checks_total": 0, "checks_ok": 0, "checks_mismatch": 0, "checks_incomplete": 0,
              "missing_metrics": 0, "currency": None, "first_fy": None, "last_fy": None,
              "result": "failed", "error": None, **summary}
    with db:
        db.execute("DELETE FROM issues WHERE cik = ?", (cik,))
        db.executemany(
            """INSERT INTO issues (cik, ticker, run, severity, kind, accession, form, fiscal_year, period,
               statement, concept, label, reported, computed, difference, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(cik, member["ticker"], run, issue["severity"], issue["kind"], issue.get("accession"),
              issue.get("form"), _int(issue.get("fiscal_year")), issue.get("period"), issue.get("statement"),
              issue.get("concept"), issue.get("label"), _float(issue.get("reported")),
              _float(issue.get("computed")), _float(issue.get("difference")), issue.get("detail"))
             for issue in issues])
        db.execute("DELETE FROM companies WHERE cik = ?", (cik,))
        db.execute(
            """INSERT INTO companies (cik, ticker, name, batch, last_run, updated, result, error, annual_found,
               annual_dumped, annual_xbrl, annual_analysed, transition, quarterly_found, quarterly_dumped,
               overdue_10k, filings_failed, checks_total, checks_ok, checks_mismatch, checks_incomplete,
               missing_metrics, issues, currency, first_fy, last_fy, seconds, analysed_with)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cik, member["ticker"], member["name"], batch, run, _now(), fields["result"], fields["error"],
             fields["annual_found"], fields["annual_dumped"], fields["annual_xbrl"], fields["annual_analysed"],
             fields["transition"], fields["quarterly_found"], fields["quarterly_dumped"], fields["overdue_10k"],
             fields["filings_failed"], fields["checks_total"], fields["checks_ok"], fields["checks_mismatch"],
             fields["checks_incomplete"], fields["missing_metrics"], len(issues), fields["currency"],
             fields["first_fy"], fields["last_fy"], seconds, pipeline_version()))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(issues).to_csv(out_dir / "issues.csv", index=False)
    except OSError:
        pass


def _int(value):
    try:
        return None if value is None or pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return None if value is None or pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


def pipeline_version() -> str:
    """The app release plus the hash of the stage-2 modules, to tell runs apart after a fix."""
    try:
        release = (HERE / "VERSION").read_text().strip()
    except OSError:
        release = "?"
    import hashlib

    digest = hashlib.sha1()
    for name in ("extract.py", "xbrl_extract.py", "calc_check.py", "edgar_dashboard.py", "scan.py"):
        digest.update((HERE / name).read_bytes().replace(b"\r\n", b"\n"))
    return f"{release}+{digest.hexdigest()[:8]}"


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def start_run(db, root: Path, batch: int, kind: str, total: int, options: dict) -> tuple[int, Log]:
    with db:
        cursor = db.execute("""INSERT INTO runs (batch, kind, started, status, pid, total, reader, options)
                               VALUES (?, ?, ?, 'running', ?, ?, ?, ?)""",
                            (batch, kind, _now(), os.getpid(), total, filing_cache.reader_id(), json.dumps(options)))
        run = cursor.lastrowid
        log_path = root / "logs" / f"run-{run:04d}.log"
        db.execute("UPDATE runs SET log = ? WHERE id = ?", (str(log_path.relative_to(root)), run))
    return run, Log(log_path)


def run_batch(db, root: Path, batch: int, kind: str, members: list, cache_dir: Path,
              retry_failed: bool = False, log: Log | None = None, run: int | None = None) -> int:
    """Process ``members`` of ``batch``: online (``next``, ``resume``) or from the dump (``rerun``)."""
    online = kind in ("next", "resume") or retry_failed
    if run is None:
        run, log = start_run(db, root, batch, kind, len(members), {"retry_failed": retry_failed})
    stats = {"downloaded": 0, "from_dump": 0, "download_failed": 0}
    finished = "failed"

    def stop(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        what = {"next": "scanning", "resume": "resuming", "rerun": "rerunning stage 2 from the dump for"}[kind]
        log(f"[*] Run {run}: {what} batch {batch}, {len(members)} companies"
            + (" (failed downloads retried)" if retry_failed and kind == "rerun" else ""))
        log(f"[*] Filing dump {cache_dir}: {filing_cache.explain_reader()}")
        log(f"[*] Stage 2: {pipeline_version()}; SEC requests: "
            + (f"yes, at most {os.environ.get('EDGAR_RATE_LIMIT_PER_SEC', RATE_LIMIT)} per second" if online else "none"))
        if online:
            _disable_download_cache(log)
        for position, member in enumerate(members, 1):
            cik, ticker = member["cik"], member["ticker"]
            with db:
                db.execute("UPDATE runs SET current = ?, done = ? WHERE id = ?", (ticker, position - 1, run))
            if kind == "resume" and db.execute("SELECT 1 FROM companies WHERE cik = ? AND batch = ?",
                                               (cik, batch)).fetchone():
                continue
            log(f"[*] Company {position}/{len(members)}: {ticker} {member['name']} (CIK {cik})")
            started = time.monotonic()
            out_dir = root / "companies" / str(cik)
            try:
                if online:
                    from edgar import Company

                    company = Company(int(cik))
                    live = list_filings(db, company, cik, log)
                    dump_filings(db, cik, live, cache_dir, log, stats, retry_failed=kind != "rerun" or retry_failed)
                elif not db.execute("SELECT 1 FROM companies WHERE cik = ?", (cik,)).fetchone():
                    log("    [!] never scanned (the run was stopped before it): use resume, not rerun")
                    continue
                summary, issues = analyse_company(db, cik, ticker, cache_dir, out_dir, log)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one company never stops the run
                log(f"    [!] {ticker}: failed ({type(exc).__name__}: {exc})")
                log.trace()
                summary = {"result": "failed", "error": f"{type(exc).__name__}: {exc}"}
                issues = [_issue("company_failed", "error", detail=summary["error"])]
            seconds = time.monotonic() - started
            _save_company(db, cik, member, batch, run, summary, issues, seconds, out_dir)
            counts = {}
            for issue in issues:
                counts[issue["kind"]] = counts.get(issue["kind"], 0) + 1
            log(f"[+] {ticker}: {summary['result']} in {seconds:.0f} s"
                + (f"; FY{summary.get('first_fy')}-FY{summary.get('last_fy')}" if summary.get("first_fy") else "")
                + (f"; checks {summary.get('checks_ok', 0)}/{summary.get('checks_total', 0)} add up"
                   if summary.get("checks_total") else "")
                + ("; issues: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) if counts else "")
                + (f" ({summary['error']})" if summary.get("error") and summary["result"] != "ok" else ""))
            with db:
                db.execute("""UPDATE runs SET done = ?, downloaded = ?, from_dump = ?, download_failed = ?
                              WHERE id = ?""",
                           (position, stats["downloaded"], stats["from_dump"], stats["download_failed"], run))
        finished = "done"
    except KeyboardInterrupt as exc:
        finished = "cancelled"
        log(f"[!] Run {run} stopped ({exc}); the companies done so far are saved, resume finishes the batch")
    except Exception as exc:  # noqa: BLE001
        log(f"[!] Run {run} failed: {type(exc).__name__}: {exc}")
        log.trace()
        with db:
            db.execute("UPDATE runs SET message = ? WHERE id = ?", (f"{type(exc).__name__}: {exc}", run))
    finally:
        signal.signal(signal.SIGTERM, previous)
        with db:
            db.execute("UPDATE runs SET status = ?, finished = ?, current = NULL WHERE id = ?",
                       (finished, _now(), run))
        totals = batch_summary(db, batch)
        if online:
            log(f"[+] Run {run} {finished}: {stats['downloaded']} filings downloaded, {stats['from_dump']} already "
                f"in the dump, {stats['download_failed']} downloads failed")
        else:
            log(f"[+] Run {run} {finished}: stage 2 from the dump only, no SEC requests")
        log("[+] Batch {batch}: {companies} companies; {results}; {issues} issues ({kinds})".format(
            batch=batch, companies=totals["companies"],
            results=", ".join(f"{n} {r}" for r, n in totals["results"].items() if n) or "none done",
            issues=totals["issues"], kinds=", ".join(f"{n} {k}" for k, n in totals["issue_kinds"].items()) or "none"))
        log.close()
    return run


def _disable_download_cache(log) -> None:
    """edgartools keeps every download forever; the dump replaces it, so the scan keeps none."""
    if os.environ.get("STOCKY_SCAN_HTTP_CACHE") == "1":
        return
    try:
        import edgar.httpclient as http

        http.HTTP_MGR = http.get_http_mgr(cache_enabled=False,
                                          request_per_sec_limit=http.get_edgar_rate_limit_per_sec())
        log("[*] edgartools download cache off for this run (the dump keeps what is needed)")
    except Exception as exc:  # noqa: BLE001 - only disk space is at stake
        log(f"[!] could not turn off the edgartools download cache ({type(exc).__name__}: {exc})")


# --------------------------------------------------------------------------- #
# Reports (also read by the app)
# --------------------------------------------------------------------------- #

def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def mark_stale_runs(db) -> None:
    """A run whose process is gone (container restart) is marked interrupted."""
    for row in db.execute("SELECT id, pid FROM runs WHERE status = 'running'").fetchall():
        if not _alive(row["pid"]):
            with db:
                db.execute("UPDATE runs SET status = 'interrupted', finished = ?, current = NULL WHERE id = ?",
                           (_now(), row["id"]))


def batch_summary(db, batch: int) -> dict:
    results = {result: 0 for result in RESULTS}
    for row in db.execute("SELECT result, COUNT(*) n FROM companies WHERE batch = ? GROUP BY result", (batch,)):
        results[row["result"]] = row["n"]
    kinds = {row["kind"]: row["n"] for row in db.execute(
        """SELECT kind, COUNT(*) n FROM issues WHERE cik IN (SELECT cik FROM batch_companies WHERE batch = ?)
           GROUP BY kind ORDER BY n DESC""", (batch,))}
    filings = db.execute(
        """SELECT COUNT(*) total, SUM(dumped) dumped, SUM(CASE WHEN error IS NOT NULL AND dumped = 0 THEN 1 ELSE 0 END)
           failed, SUM(COALESCE(bytes, 0)) bytes FROM filings
           WHERE cik IN (SELECT cik FROM batch_companies WHERE batch = ?)""", (batch,)).fetchone()
    meta = db.execute("SELECT * FROM batches WHERE id = ?", (batch,)).fetchone()
    first = db.execute("""SELECT u.ticker FROM batch_companies b JOIN universe u ON u.cik = b.cik
                          WHERE b.batch = ? ORDER BY b.position LIMIT 1""", (batch,)).fetchone()
    last = db.execute("""SELECT u.ticker FROM batch_companies b JOIN universe u ON u.cik = b.cik
                         WHERE b.batch = ? ORDER BY b.position DESC LIMIT 1""", (batch,)).fetchone()
    return {
        "batch": batch, "created": meta["created"] if meta else None, "size": meta["size"] if meta else 0,
        "first": first["ticker"] if first else None, "last": last["ticker"] if last else None,
        "companies": sum(results.values()), "results": results, "issues": sum(kinds.values()),
        "issue_kinds": kinds, "filings": filings["total"] or 0, "dumped": filings["dumped"] or 0,
        "download_failed": filings["failed"] or 0, "bytes": filings["bytes"] or 0,
    }


def overview(db) -> dict:
    mark_stale_runs(db)
    universe = db.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    taken = db.execute("SELECT COUNT(*) FROM batch_companies").fetchone()[0]
    upcoming = db.execute("""SELECT u.ticker FROM universe u LEFT JOIN batch_companies b ON b.cik = u.cik
                             WHERE b.cik IS NULL ORDER BY u.position LIMIT 1""").fetchone()
    batches = [batch_summary(db, row["id"]) for row in db.execute("SELECT id FROM batches ORDER BY id DESC")]
    runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20")]
    return {"universe": universe, "taken": taken, "next": upcoming["ticker"] if upcoming else None,
            "batches": batches, "runs": runs,
            "active": next((run for run in runs if run["status"] == "running"), None),
            "reader": {"id": filing_cache.reader_id(), "schema": filing_cache.schema(),
                       "classified": filing_cache.classification() is not None},
            "stage2": pipeline_version()}


def batch_detail(db, batch: int, kind: str | None = None, limit: int = 2000) -> dict:
    companies = [dict(row) for row in db.execute(
        """SELECT c.*, u.cik AS cik, u.ticker AS ticker, u.name AS name, u.exchange AS exchange
           FROM batch_companies b JOIN universe u ON u.cik = b.cik
           LEFT JOIN companies c ON c.cik = b.cik WHERE b.batch = ? ORDER BY b.position""", (batch,))]
    query = """SELECT * FROM issues WHERE cik IN (SELECT cik FROM batch_companies WHERE batch = ?)"""
    args: list = [batch]
    if kind:
        query += " AND kind = ?"
        args.append(kind)
    query += """ ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, ticker,
                 fiscal_year DESC LIMIT ?"""
    issues = [dict(row) for row in db.execute(query, (*args, limit))]
    return {"summary": batch_summary(db, batch), "companies": companies, "issues": issues}


ISSUE_COLUMNS = ("ticker", "cik", "severity", "kind", "fiscal_year", "period", "form", "accession", "statement",
                 "concept", "label", "reported", "computed", "difference", "detail", "run")


def issues_csv(db, batch: int) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(ISSUE_COLUMNS)
    for row in db.execute("""SELECT * FROM issues WHERE cik IN (SELECT cik FROM batch_companies WHERE batch = ?)
                             ORDER BY ticker, fiscal_year DESC, kind""", (batch,)):
        writer.writerow([row[column] for column in ISSUE_COLUMNS])
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _print_status(db, batch: int | None) -> None:
    info = overview(db)
    print(f"Universe: {info['universe']} companies, {info['taken']} in batches, next: {info['next']}")
    print(f"Dump reader {info['reader']['id']} (schema {info['reader']['schema']}"
          f"{'' if info['reader']['classified'] else ', NOT classified'}); stage 2 {info['stage2']}")
    for item in info["batches"]:
        if batch and item["batch"] != batch:
            continue
        results = ", ".join(f"{n} {r}" for r, n in item["results"].items() if n) or "nothing done"
        print(f"  batch {item['batch']}: {item['first']}..{item['last']} ({item['size']}), {results}; "
              f"{item['dumped']}/{item['filings']} filings dumped, {item['issues']} issues")
        for kind, count in item["issue_kinds"].items():
            print(f"      {count:6} {kind}")
    for run in info["runs"][:5]:
        print(f"  run {run['id']}: {run['kind']} batch {run['batch']} {run['status']} "
              f"{run['done']}/{run['total']} ({run['started']} - {run['finished'] or ''}) {run['log']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=None, help="Scan folder (default: data/market)")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Filing dump (default: the shared cache)")
    commands = parser.add_subparsers(dest="command", required=True)
    universe = commands.add_parser("universe", help="Save SEC's company list (first time) or append new companies")
    universe.add_argument("--refresh", action="store_true")
    nxt = commands.add_parser("next", help="Scan the next N companies as a new batch")
    nxt.add_argument("--count", type=int, required=True)
    resume = commands.add_parser("resume", help="Finish a stopped batch")
    resume.add_argument("--batch", type=int, required=True)
    rerun = commands.add_parser("rerun", help="Stage 2 again for a batch, from the dump (no SEC requests)")
    rerun.add_argument("--batch", type=int, required=True)
    rerun.add_argument("--cik", help="Only this company")
    rerun.add_argument("--retry-failed", action="store_true", help="Also retry the downloads that failed")
    status = commands.add_parser("status", help="Universe, batches, issue counts, recent runs")
    status.add_argument("--batch", type=int)
    issues = commands.add_parser("issues", help="A batch's issues as CSV on stdout")
    issues.add_argument("--batch", type=int, required=True)
    args = parser.parse_args(argv)

    root = args.root or market_dir()
    cache_dir = args.cache_dir or filing_cache.default_root()
    db = connect(root)
    if args.command == "status":
        _print_status(db, args.batch)
        return 0
    if args.command == "issues":
        sys.stdout.write(issues_csv(db, args.batch))
        return 0
    if args.command == "universe":
        load_universe(db, Log(None), refresh=args.refresh)
        return 0
    mark_stale_runs(db)
    active = db.execute("SELECT id FROM runs WHERE status = 'running'").fetchone()
    if active:
        print(f"run {active['id']} is still running; wait for it or stop it first", file=sys.stderr)
        return 2
    if args.command == "next":
        if not 1 <= args.count <= MAX_COUNT:
            parser.error(f"--count must be between 1 and {MAX_COUNT}")
        boot = Log(None)
        load_universe(db, boot)
        batch, members = new_batch(db, args.count)
        run_batch(db, root, batch, "next", members, cache_dir)
        return 0
    if not db.execute("SELECT 1 FROM batches WHERE id = ?", (args.batch,)).fetchone():
        parser.error(f"no batch {args.batch}")
    if args.command == "resume":
        run_batch(db, root, args.batch, "resume", batch_members(db, args.batch), cache_dir)
        return 0
    members = batch_members(db, args.batch, args.cik)
    if not members:
        parser.error("no such company in that batch")
    run_batch(db, root, args.batch, "rerun", members, cache_dir, retry_failed=args.retry_failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
