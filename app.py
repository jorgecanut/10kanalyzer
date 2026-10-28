#!/usr/bin/env python3
"""Minimal local web UI for the SEC EDGAR 10-K dashboard.

Enter a ticker, watch the run, then view or download the generated charts.
Local accounts only: users, sessions and results live under ``data/`` (or
``$STOCKY_DATA_DIR``); delete that folder and everything is gone. Each user's
results are kept apart in ``data/users/<id>/<TICKER>/`` and users never see
one another's runs. The pipeline is ``edgar_dashboard.py``, run as a
subprocess with the user's name and email as the SEC identity.

Run locally:   python app.py            (http://localhost:8053)
"""

from __future__ import annotations

import calendar
import collections
import csv
import io
import json
import hashlib
import math
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from functools import lru_cache
from importlib import metadata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pandas as pd
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import charts as chartlib

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD = BASE_DIR / "edgar_dashboard.py"

# Everything the app stores: the account database, the session key and each
# user's results. Read through functions so tests can point it elsewhere.
DATA_DIR = Path(os.environ.get("STOCKY_DATA_DIR", BASE_DIR / "data"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
# Profile colour: the names the UI offers (each has a checked dark and light
# shade there); the first is the default.
COLORS = ("orange", "blue", "teal", "green", "amber", "red", "pink", "violet")
AVATAR_SIZE = 256                    # stored as a square PNG of this many pixels
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_MAX_PIXELS = 40_000_000       # refuse decompression bombs before decoding

# Tickers: letters/digits plus the "." and "-" seen in symbols such as BRK.B.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
ALLOWED_FILES = {".png", ".csv", ".log"}
# A run's data files offered by the download dialog (top level of the ticker
# folder); run.json is bookkeeping and stays out.
DATA_SUFFIXES = {".csv", ".json", ".log"}
HIDDEN_FILES = {"run.json"}
# Years per downloaded chart image; 0 puts every year in one wide image.
CHART_YEAR_CHOICES = (5, 10, 0)
DOWNLOAD_EXTRAS = ("insiders", "news", "screener", "debug")

# Release number (major.minor.patch, see README "Versions"); the commit comes
# from the STOCKY_COMMIT build arg, or from git when run from a checkout.
VERSION_FILE = BASE_DIR / "VERSION"
# Per-user diagnostics (server errors, failed runs, browser errors), kept as
# JSON lines in the user's folder and shipped in Home's download for debugging.
DIAG_FILE = "diagnostics.jsonl"
DIAG_MAX_BYTES = 1_000_000           # trimmed to the newest DIAG_KEEP lines past this
DIAG_KEEP = 2000
DIAG_LOCK = threading.Lock()

app = Flask(__name__)
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Jobs are keyed by "<user id>:<ticker>" (see _job_key) so users never share
# or see each other's runs; the run lock below is still global (SEC limits).
JOBS: dict[str, dict] = {}
# Tickers of each user's latest screener request, so a reloaded page can
# resume its progress. Guarded by JOBS_LOCK; cleared by cancel.
SCREEN_BATCH: dict[str, list[str]] = {}
JOBS_LOCK = threading.Lock()
# Live child processes, keyed like JOBS, so cancel can stop them.
PROCS: dict[str, subprocess.Popen] = {}
# SEC is rate-limited and the scrape is heavy; run one at a time.
RUN_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_ticker(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    if not TICKER_RE.match(ticker):
        abort(400, "Invalid ticker (letters/digits, up to 10 chars, e.g. NFLX).")
    return ticker


@contextmanager
def _db():
    """The local account database; commits on success and always closes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "stocky.db")
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,"
            " password_hash TEXT NOT NULL, created TEXT NOT NULL)"
        )
        # Added after the first release: older databases get the column here.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "color" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN color TEXT")
        # Articles a user saved from the News tab, one row per link.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS saved_news ("
            " user_id TEXT NOT NULL, link TEXT NOT NULL, title TEXT NOT NULL, source TEXT,"
            " published TEXT, ticker TEXT, saved TEXT NOT NULL,"
            " PRIMARY KEY (user_id, link))"
        )
        if "summary" not in {row["name"] for row in conn.execute("PRAGMA table_info(saved_news)")}:
            conn.execute("ALTER TABLE saved_news ADD COLUMN summary TEXT")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _secret_key() -> bytes:
    """Session signing key, created once and kept in the data directory.

    If the directory is not writable the key lives in memory only: the app
    still works, but every restart signs everyone out.
    """
    path = DATA_DIR / "secret_key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[!] cannot read {path}: {exc}; sessions will not survive a restart", file=sys.stderr)
        return secrets.token_bytes(32)
    key = secrets.token_bytes(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        path.chmod(0o600)
    except OSError as exc:
        print(f"[!] cannot write {path}: {exc}; sessions will not survive a restart", file=sys.stderr)
    return key


def _get_user(uid: str | None) -> dict | None:
    if not uid:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def _clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        abort(400, "Name cannot be empty.")
    if len(name) > 100:
        abort(400, "Name is too long (max 100 characters).")
    return name


def _clean_email(raw) -> str:
    email = str(raw or "").strip()
    if not EMAIL_RE.match(email) or len(email) > 200:
        abort(400, "Enter a valid email address.")
    return email


def _clean_password(raw) -> str:
    password = str(raw or "")
    if len(password) < MIN_PASSWORD:
        abort(400, f"Password must be at least {MIN_PASSWORD} characters.")
    return password


def _identity(user: dict) -> str:
    """The SEC User-Agent contact: the account's name and email."""
    return f"{user['name']} {user['email']}"


def _public_user(user: dict) -> dict:
    # The id is internal: it names the data folder and is never sent out.
    return {"name": user["name"], "email": user["email"], "identity": _identity(user),
            "color": user.get("color") or COLORS[0], "avatar": _avatar_path(user["id"]).is_file()}


def _avatar_path(uid: str) -> Path:
    # Outside the user's results folder, so "delete all results" keeps it.
    return DATA_DIR / "avatars" / f"{uid}.png"


def _user_dir(uid: str | None = None) -> Path:
    return DATA_DIR / "users" / (uid or g.user["id"])


def _job_key(uid: str, ticker: str) -> str:
    return f"{uid}:{ticker}"


def _ticker_dir(ticker: str, uid: str | None = None) -> Path:
    return _user_dir(uid) / ticker


def _chart_spec(ticker: str, uid: str | None = None) -> dict | None:
    """The run's ``charts.json``.

    A run from before it (PNGs only) is upgraded once from its CSVs; a
    screener run has neither and stays without charts.
    """
    directory = _ticker_dir(ticker, uid)
    path = directory / chartlib.SPEC_FILE
    if not path.is_file():
        try:
            legacy = directory.is_dir() and any(directory.glob("*.png"))
        except OSError:
            legacy = False
        if not legacy:
            return None
        try:
            chartlib.render_from_dir(ticker, str(directory))
        except Exception as exc:  # noqa: BLE001 - old charts are best-effort
            app.logger.warning("could not upgrade %s charts: %s", ticker, exc)
            return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _charts(ticker: str) -> list[str]:
    spec = _chart_spec(ticker)
    return [chart["id"] for chart in spec["charts"]] if spec else []


def _data_files(directory: Path) -> list[str]:
    try:
        return sorted(p.name for p in directory.iterdir()
                      if p.is_file() and p.suffix.lower() in DATA_SUFFIXES and p.name not in HIDDEN_FILES)
    except OSError:
        return []


def _results_summary(ticker: str) -> dict | None:
    directory = _ticker_dir(ticker)
    try:
        if not directory.is_dir():
            return None
        files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_FILES | DATA_SUFFIXES]
    except OSError:
        return None
    if not files:
        return None
    modified = max(p.stat().st_mtime for p in files)
    return {
        "ticker": ticker,
        "charts": _charts(ticker),
        # What the download dialog offers besides the charts.
        "files": _data_files(directory),
        "has_csv": (directory / "financial_data.csv").exists(),
        "has_insider": (directory / "insider.json").is_file(),
        "modified": datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds"),
        "modified_ts": modified,
    }


def _log_tail(ticker: str, max_lines: int = 200, uid: str | None = None) -> str:
    log = _ticker_dir(ticker, uid) / "run.log"
    if not log.is_file():
        return ""
    text = log.read_text(errors="replace").splitlines()
    return "\n".join(text[-max_lines:])


def _diag(uid: str | None, source: str, level: str, message: str, **detail) -> None:
    """Append one entry to the user's diagnostics log; never raises."""
    if not uid:
        return
    entry = {"time": _now(), "version": app_version(), "source": source, "level": level,
             "message": str(message)[:2000], **detail}
    path = _user_dir(uid) / DIAG_FILE
    with DIAG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry, default=str) + "\n")
            if path.stat().st_size > DIAG_MAX_BYTES:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-DIAG_KEEP:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass


YAHOO_UA = "Mozilla/5.0 (X11; Linux x86_64) Stocky/1.0"
_YAHOO_SESSION = {"opener": None, "crumb": "", "ts": 0.0}
_YAHOO_LOCK = threading.Lock()
_SIM_CACHE: dict = {}
_CURRENT_CACHE: dict = {}
_NEWS_CACHE: dict = {}
NEWS_TTL = 1800          # seconds; news is shared by every user
NEWS_DAYS = 14             # defaults; the News tab offers the choices below
NEWS_LIMIT = 20
NEWS_DAY_CHOICES = (1, 7, 14, 30)
NEWS_LIMIT_CHOICES = (20, 30, 40, 50)
NEWS_SCOPES = ("company", "market")   # News tab / Home
_QUOTE_CACHE: dict = {}  # Yahoo search hits (name, sector) per ticker
INSIDER_MONTH_CHOICES = (1, 3, 6, 12, 24, 36)


def _yahoo_session():
    """Return a cookie-aware opener and the crumb Yahoo's quote API requires."""
    with _YAHOO_LOCK:
        if _YAHOO_SESSION["opener"] and time.time() - _YAHOO_SESSION["ts"] < 3600:
            return _YAHOO_SESSION["opener"], _YAHOO_SESSION["crumb"]
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            # Often answers 404; the response still sets the identity cookie.
            opener.open(Request("https://fc.yahoo.com", headers={"User-Agent": YAHOO_UA}), timeout=10).read()
        except (HTTPError, URLError, TimeoutError):
            pass
        crumb = ""
        try:
            response = opener.open(
                Request("https://query1.finance.yahoo.com/v1/test/getcrumb",
                        headers={"User-Agent": YAHOO_UA}),
                timeout=10,
            )
            crumb = response.read().decode("utf-8", "replace").strip()
        except (HTTPError, URLError, TimeoutError):
            pass
        _YAHOO_SESSION.update(opener=opener, crumb=crumb, ts=time.time())
        return opener, crumb


def _csv_records(path: Path) -> list[dict]:
    """Read a CSV into JSON-safe records (NaN becomes null)."""
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError):
        return []
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records"))


def _current_quote(ticker: str) -> dict:
    """Best-effort Yahoo snapshot (price, currency, trailing PE), cached 300 s."""
    with _YAHOO_LOCK:
        cached = _CURRENT_CACHE.get(ticker)
        if cached and time.time() - cached[0] < 300:
            return cached[1]

    current = {"price": None, "currency": None, "trailing_pe": None}
    yahoo_ticker = ticker.replace(".", "-")
    try:
        query = urlencode({"range": "1d", "interval": "1d"})
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?{query}"
        with urlopen(Request(url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
            payload = json.load(response)
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        current["price"] = meta.get("regularMarketPrice")
        current["currency"] = meta.get("currency")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, IndexError, AttributeError):
        pass

    try:
        opener, crumb = _yahoo_session()
        quote_url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/" + yahoo_ticker
        quote_url += "?" + urlencode({"modules": "summaryDetail", "crumb": crumb})
        with opener.open(Request(quote_url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
            quote = json.load(response)
        details = ((quote.get("quoteSummary") or {}).get("result") or [{}])[0]
        current["trailing_pe"] = ((details.get("summaryDetail") or {}).get("trailingPE") or {}).get("raw")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, IndexError, AttributeError):
        pass

    with _YAHOO_LOCK:
        _CURRENT_CACHE[ticker] = (time.time(), current)
    return current


def _worker(key: str, filings: int, job_id: str) -> None:
    """Run the dashboard in a subprocess, one at a time.

    The job id guards against stale worker threads: a cancelled or superseded
    job must never overwrite the state of a newer run for the same ticker.
    Screener jobs (``charts: False``) skip chart rendering; the flag is read at
    launch, so a single-ticker run can still upgrade a queued screener job.
    ``key`` is ``_job_key(uid, ticker)``: results go to that user's directory
    and the SEC identity is that account's name and email, read at launch.
    """
    with RUN_LOCK:
        with JOBS_LOCK:
            job = JOBS.get(key)
            if not job or job.get("id") != job_id:
                return
            if job.get("cancel_requested") or job.get("status") == "cancelled":
                return
            uid, ticker = job["uid"], job["ticker"]

        user = _get_user(uid)
        if user is None:
            with JOBS_LOCK:
                job = JOBS.get(key)
                if job and job.get("id") == job_id:
                    job.update(status="error", finished=_now(), returncode=-1,
                               error="the account no longer exists")
            return
        directory = _ticker_dir(ticker, uid)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "run.log"
        except OSError as exc:
            # Usually the bind-mounted financial_graphs is not writable by the
            # container user (uid 10001). Report it instead of leaving the job
            # stuck on "queued" forever.
            message = (
                f"cannot write results dir {directory}: {type(exc).__name__}: {exc} "
                f"(uid={os.getuid()}; check the data bind mount ownership)"
            )
            with JOBS_LOCK:
                job = JOBS.get(key)
                if job and job.get("id") == job_id:
                    job.update(status="error", finished=_now(), returncode=-1, error=message)
            return

        with JOBS_LOCK:
            job = JOBS.get(key)
            if not job or job.get("id") != job_id:
                return
            if job.get("cancel_requested"):
                return
            job.update(status="running", started=_now())
            with_charts = job.get("charts", True)

        proc = None
        status, code = "error", -1
        try:
            env = {**os.environ, "EDGAR_IDENTITY": _identity(user), "PYTHONUNBUFFERED": "1"}
            command = [sys.executable, str(DASHBOARD), "--ticker", ticker, "--filings", str(filings),
                       "--output-dir", str(_user_dir(uid))]
            if not with_charts:
                command.append("--no-charts")
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    command,
                    cwd=str(BASE_DIR),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                with JOBS_LOCK:
                    job = JOBS.get(key)
                    if job and job.get("id") == job_id:
                        job["pid"] = proc.pid
                    PROCS[key] = proc
                    # Cancel may have landed between the status check and Popen.
                    cancel_now = bool(job and job.get("id") == job_id and job.get("cancel_requested"))
                if cancel_now and proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=1800)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    code = -1
                    log.write(f"[{_now()}] killed after 30-minute timeout\n")
                    log.flush()
                with JOBS_LOCK:
                    job = JOBS.get(key)
                    cancelled = bool(job and job.get("id") == job_id and job.get("cancel_requested"))
                if cancelled:
                    log.write(f"[{_now()}] cancelled by user; files kept\n")
                    log.flush()
            status = "done" if code == 0 else "error"
        except Exception as exc:  # noqa: BLE001 - surface any launch failure in the UI
            status, code = "error", -1
            message = f"Failed to launch dashboard: {type(exc).__name__}: {exc}"
            try:
                log_path.write_text(message + "\n")
            except OSError:
                pass
            with JOBS_LOCK:
                job = JOBS.get(key)
                if job and job.get("id") == job_id:
                    job["error"] = message
        finally:
            if proc is not None:
                with JOBS_LOCK:
                    if PROCS.get(key) is proc:
                        PROCS.pop(key, None)

        followup = None
        with JOBS_LOCK:
            job = JOBS.get(key)
            if job and job.get("id") == job_id:
                if job.get("cancel_requested"):
                    job.update(status="cancelled", finished=_now(), returncode=code)
                else:
                    job.update(status=status, finished=_now(), returncode=code)
                    if status == "error":
                        _diag(uid, "pipeline", "error", f"{ticker} run failed (exit {code})",
                              ticker=ticker, filings=filings, charts=with_charts,
                              error=job.get("error"), log_tail=_log_tail(ticker, 60, uid))
                    if status == "done":
                        _write_run_meta(directory, filings, with_charts)
                    followup = job.get("followup_filings")
                    if followup:
                        # A graph run was requested while this screener job ran.
                        followup_id = secrets.token_hex(8)
                        JOBS[key] = _new_job(uid, ticker, followup_id, charts=True)
    if followup:
        threading.Thread(target=_worker, args=(key, followup, followup_id), daemon=True).start()


def _new_job(uid: str, ticker: str, job_id: str, charts: bool) -> dict:
    return {"id": job_id, "uid": uid, "ticker": ticker, "status": "queued",
            "queued": _now(), "charts": charts}


def _write_run_meta(directory: Path, filings: int, charts: bool) -> None:
    """Record what the last successful run produced, for the same-day cache."""
    try:
        (directory / "run.json").write_text(json.dumps(
            {"filings": filings, "charts": charts, "finished": _now()}))
    except OSError:
        pass


def _cached_today(ticker: str, filings: int) -> str | None:
    """Finish time of today's graph run with the same filings, if there is one."""
    try:
        meta = json.loads((_ticker_dir(ticker) / "run.json").read_text())
        finished = datetime.fromisoformat(meta["finished"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not meta.get("charts") or meta.get("filings") != filings or not _charts(ticker):
        return None
    if finished.astimezone().date() != datetime.now().astimezone().date():
        return None
    return meta["finished"]




# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.errorhandler(HTTPException)
def handle_http_error(err: HTTPException):
    """Return JSON for API errors instead of Flask's HTML error page."""
    return jsonify(error=err.description, status=err.code), err.code


@app.errorhandler(Exception)
def handle_exception(err: Exception):
    """Unexpected errors: JSON for the UI, full traceback in the diagnostics log."""
    app.logger.exception("unhandled error on %s %s", request.method, request.path)
    user = getattr(g, "user", None)
    _diag(user and user["id"], "server", "error", f"{type(err).__name__}: {err}",
          request=f"{request.method} {request.full_path.rstrip('?')}", traceback=traceback.format_exc())
    g.diag_logged = True
    if not os.access(DATA_DIR, os.W_OK):
        # The usual cause after a redeploy: a bind mount the container user cannot write.
        message = (f"The data folder {DATA_DIR} is not writable by the app (uid {os.getuid()}); "
                   "fix its ownership on the host. /healthz shows the details.")
    elif user:
        message = "Internal error; it is in the diagnostics log."
    else:
        message = "Internal error; it is in the server log."
    return jsonify(error=message, status=500), 500


@app.after_request
def log_failed_request(response):
    """Every failed request of a signed-in user goes to their diagnostics log."""
    user = getattr(g, "user", None)
    if (response.status_code >= 400 and user and not g.get("diag_logged")
            and request.endpoint not in ("static", "client_diag")):
        body = response.get_json(silent=True) if response.is_json else None
        _diag(user["id"], "server", "error" if response.status_code >= 500 else "warn",
              f"{request.method} {request.full_path.rstrip('?')} -> {response.status_code}",
              error=(body or {}).get("error") if isinstance(body, dict) else None)
    return response


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    )
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    return response


app.secret_key = _secret_key()

# Everything else needs a signed-in user.
PUBLIC_ENDPOINTS = {"login_page", "login", "signup", "healthz", "static"}


@app.before_request
def load_user():
    g.user = _get_user(session.get("uid"))
    if g.user is not None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    session.pop("uid", None)
    if request.path.startswith("/api/"):
        abort(401, "Sign in first.")
    return redirect(url_for("login_page"))


def _start_session(uid: str) -> None:
    session.clear()
    session["uid"] = uid
    session.permanent = True


@app.get("/")
def index():
    return render_template("index.html", version=app_version(),
                           commit=(git_commit()["commit"] or "")[:7])


@app.get("/login")
def login_page():
    if g.user is not None:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/api/signup")
def signup():
    payload = request.get_json(silent=True) or {}
    name = _clean_name(payload.get("name"))
    email = _clean_email(payload.get("email"))
    password = _clean_password(payload.get("password"))
    uid = uuid.uuid4().hex
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, password_hash, created) VALUES (?, ?, ?, ?, ?)",
                (uid, name, email, generate_password_hash(password), _now()),
            )
    except sqlite3.IntegrityError:
        abort(409, "An account with that email already exists.")
    _start_session(uid)
    return jsonify(user=_public_user(_get_user(uid))), 201


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    with _db() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        abort(401, "Wrong email or password.")
    _start_session(row["id"])
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/diag")
def client_diag():
    """Browser errors (script errors, failed fetches), for the diagnostics log."""
    payload = request.get_json(silent=True, force=True) or {}
    if not isinstance(payload, dict):
        abort(400, "Expected a JSON object.")
    level = payload.get("level") if payload.get("level") in ("error", "warn", "info") else "error"
    _diag(g.user["id"], "browser", level, str(payload.get("message") or "")[:2000],
          detail=str(payload.get("detail") or "")[:4000], page=str(payload.get("page") or "")[:200],
          viewport=str(payload.get("viewport") or "")[:40],
          agent=request.headers.get("User-Agent", "")[:300])
    return "", 204


@app.get("/healthz")
def healthz():
    return jsonify(status="ok", **build_info())


def build_info() -> dict:
    """What pipeline code this deployment actually has on disk.

    Lets a stale container be identified from one request: compare
    ``pipeline_sha1`` / ``charts_sha1`` with the repo's
    ``sha1sum edgar_dashboard.py charts.py``.
    """
    info = {"version": app_version(), **git_commit(), "code_sha1": code_sha1(), "features": []}
    for module in ("valuation", "insider", "calc_check"):
        if (BASE_DIR / f"{module}.py").is_file():
            info["features"].append(module)
    for name, key in (("edgar_dashboard.py", "pipeline_sha1"), ("charts.py", "charts_sha1")):
        path = BASE_DIR / name
        try:
            info[key] = hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError:
            info[key] = None
    info["data"] = graph_diagnostics(DATA_DIR, list_tickers=False)
    return info


def app_version() -> str:
    try:
        return VERSION_FILE.read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


@lru_cache(maxsize=1)
def git_commit() -> dict:
    """The deployed commit: the build arg in Docker, else ``git`` in a checkout."""
    commit = os.environ.get("STOCKY_COMMIT", "").strip()
    if commit:
        return {"commit": commit, "dirty": None}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE_DIR, capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=BASE_DIR,
                               capture_output=True, text=True, timeout=5).stdout.strip() != ""
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


@lru_cache(maxsize=1)
def code_sha1() -> str:
    """One hash over every root module and template, to match a build to a commit
    even when the commit is unknown (a deploy without the build arg)."""
    digest = hashlib.sha1()
    for path in sorted([*BASE_DIR.glob("*.py"), *(BASE_DIR / "templates").glob("*.html")]):
        digest.update(path.name.encode() + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


def graph_diagnostics(directory: Path, list_tickers: bool = True) -> dict:
    """Why results may be invisible: the path the app reads, and whether it can.

    A bind mount that is not writable by the container user (uid 10001) is the
    usual cause of "the run says it wrote charts but nothing appears". Public
    callers (``/healthz``) pass ``list_tickers=False``: tickers are per user.
    """
    info = {
        "graph_dir": str(directory),
        "exists": directory.is_dir(),
        "writable": os.access(directory, os.W_OK) if directory.is_dir() else False,
        "readable": os.access(directory, os.R_OK) if directory.is_dir() else False,
        "uid": os.getuid(),
    }
    if not list_tickers:
        return info
    try:
        info["tickers"] = sorted(p.name for p in directory.iterdir() if p.is_dir()) if directory.is_dir() else []
    except OSError as exc:
        info["tickers"] = []
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


@app.get("/api/account")
def account():
    return jsonify(user=_public_user(g.user))


@app.patch("/api/account")
def update_account():
    """Change name, email and/or password; email and password need the current one."""
    payload = request.get_json(silent=True) or {}
    user = g.user
    name = _clean_name(payload.get("name", user["name"]))
    email = _clean_email(payload.get("email", user["email"]))
    new_password = str(payload.get("new_password") or "")
    color = payload.get("color", user.get("color") or COLORS[0])
    if color not in COLORS:
        abort(400, "Unknown colour.")
    if email.lower() != user["email"].lower() or new_password:
        if not check_password_hash(user["password_hash"], str(payload.get("current_password") or "")):
            abort(403, "Enter your current password to change the email or password.")
    password_hash = (generate_password_hash(_clean_password(new_password))
                     if new_password else user["password_hash"])
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE users SET name = ?, email = ?, password_hash = ?, color = ? WHERE id = ?",
                (name, email, password_hash, color, user["id"]),
            )
    except sqlite3.IntegrityError:
        abort(409, "An account with that email already exists.")
    return jsonify(user=_public_user(_get_user(user["id"])))


@app.get("/api/account/avatar")
def get_avatar():
    path = _avatar_path(g.user["id"])
    if not path.is_file():
        abort(404, "No profile picture.")
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.put("/api/account/avatar")
def put_avatar():
    """The raw image is the body (PNG, JPEG or WebP); stored as a centred square PNG.

    A raw body, not a form, so a cross-site form post can never set it.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if (request.content_length or 0) > AVATAR_MAX_BYTES:
        abort(413, "Picture is too big (max 5 MB).")
    data = request.stream.read(AVATAR_MAX_BYTES + 1)
    if len(data) > AVATAR_MAX_BYTES:
        abort(413, "Picture is too big (max 5 MB).")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ("PNG", "JPEG", "WEBP"):
            abort(400, "Use a PNG, JPEG or WebP picture.")
        if image.width * image.height > AVATAR_MAX_PIXELS:
            abort(400, "Picture has too many pixels.")
        image = ImageOps.exif_transpose(image).convert("RGBA")
        image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError):
        abort(400, "Use a PNG, JPEG or WebP picture.")
    path = _avatar_path(g.user["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    image.save(tmp, "PNG")
    tmp.replace(path)
    return jsonify(user=_public_user(g.user))


@app.delete("/api/account/avatar")
def delete_avatar():
    _avatar_path(g.user["id"]).unlink(missing_ok=True)
    return jsonify(user=_public_user(g.user))


def _require_confirm(payload) -> None:
    # A JSON body is required, so a cross-site form post can never trigger this.
    if not isinstance(payload, dict) or payload.get("confirm") != "DELETE":
        abort(400, "Type DELETE to confirm.")


def _wipe_user(uid: str) -> int:
    """Stop the user's jobs and delete all their results; return tickers removed."""
    _cancel_jobs(uid)
    directory = _user_dir(uid)
    try:
        removed = sum(1 for p in directory.iterdir() if p.is_dir()) if directory.is_dir() else 0
        if directory.exists():
            shutil.rmtree(directory)
    except OSError as exc:
        abort(500, f"Could not remove data: {exc}")
    with JOBS_LOCK:
        for key in [key for key, job in JOBS.items() if job.get("uid") == uid]:
            JOBS.pop(key, None)
    return removed


@app.delete("/api/data")
def wipe_data():
    _require_confirm(request.get_json(silent=True))
    return jsonify(removed=_wipe_user(g.user["id"]))


@app.delete("/api/account")
def delete_account():
    payload = request.get_json(silent=True)
    _require_confirm(payload)
    if not check_password_hash(g.user["password_hash"], str(payload.get("password") or "")):
        abort(403, "Wrong password.")
    uid = g.user["id"]
    _wipe_user(uid)
    _avatar_path(uid).unlink(missing_ok=True)
    with _db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute("DELETE FROM saved_news WHERE user_id = ?", (uid,))
    session.clear()
    return jsonify(deleted=True)


@app.post("/api/run")
def run():
    payload = request.get_json(silent=True) or {}
    ticker = _normalise_ticker(payload.get("ticker", ""))
    try:
        filings = int(payload.get("filings", 20))
    except (TypeError, ValueError):
        abort(400, "filings must be a number")
    if not 1 <= filings <= 20:
        abort(400, "filings must be between 1 and 20")
    uid = g.user["id"]
    key = _job_key(uid, ticker)

    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job["status"] in ("queued", "running"):
            if not job.get("charts", True):
                # A screener job is pending for this ticker: make it render
                # charts if it has not started, else queue a graph run after it.
                if job["status"] == "queued":
                    job["charts"] = True
                else:
                    job["followup_filings"] = filings
            return jsonify(status=job["status"], ticker=ticker, message="Already running"), 200

    # Same graphs already generated today: serve them unless asked for fresh.
    if not payload.get("fresh"):
        generated = _cached_today(ticker, filings)
        if generated:
            return jsonify(status="cached", ticker=ticker, generated=generated), 200

    with JOBS_LOCK:
        job_id = secrets.token_hex(8)
        JOBS[key] = _new_job(uid, ticker, job_id, charts=True)

    threading.Thread(target=_worker, args=(key, filings, job_id), daemon=True).start()
    return jsonify(status="queued", ticker=ticker), 202


def _cancel_jobs(uid: str) -> list[str]:
    """Stop every queued/running job of one user. Results on disk are kept."""
    procs: list[subprocess.Popen] = []
    tickers: list[str] = []
    with JOBS_LOCK:
        SCREEN_BATCH.pop(uid, None)
        for key, job in JOBS.items():
            if job.get("uid") != uid or job.get("status") not in ("queued", "running"):
                continue
            job["cancel_requested"] = True
            job["status"] = "cancelled"
            job["finished"] = _now()
            tickers.append(job["ticker"])
            proc = PROCS.get(key)
            if proc is not None:
                procs.append(proc)

    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    # Give the pipeline a moment to exit, then make sure it is gone.
    for proc in procs:
        deadline = time.time() + 3
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    return tickers


@app.post("/api/cancel")
def cancel_all():
    """Stop the signed-in user's queued/running jobs; files are never touched."""
    return jsonify(cancelled=_cancel_jobs(g.user["id"]))


@app.get("/api/jobs")
def jobs():
    """Queued/running jobs plus the last screener batch, for resuming on reload."""
    uid = g.user["id"]
    with JOBS_LOCK:
        active = [
            {"ticker": job["ticker"], "status": job["status"], "charts": job.get("charts", True)}
            for job in JOBS.values()
            if job.get("uid") == uid and job.get("status") in ("queued", "running")
        ]
        batch = list(SCREEN_BATCH.get(uid, []))
    return jsonify(jobs=active, batch=batch)


@app.get("/api/status/<ticker>")
def status(ticker: str):
    """The ticker's job and the end of its run log (the whole log, up to 5000 lines, with ``?full=1``)."""
    ticker = _normalise_ticker(ticker)
    lines = 5000 if request.args.get("full") else 200
    with JOBS_LOCK:
        job = dict(JOBS.get(_job_key(g.user["id"], ticker), {}))
    job.pop("uid", None)

    if job:
        job["log"] = _log_tail(ticker, lines)
        summary = _results_summary(ticker)
        job["charts"] = summary["charts"] if summary else []
        return jsonify(job)

    summary = _results_summary(ticker)
    if summary:
        # No in-memory job (e.g. after a restart) but results exist on disk.
        return jsonify(status="done", ticker=ticker, charts=summary["charts"], log=_log_tail(ticker, lines))
    return jsonify(status="idle", ticker=ticker)


@app.get("/api/recent")
def recent():
    directory = _user_dir()
    try:
        dirs = [d for d in directory.iterdir() if d.is_dir()] if directory.is_dir() else []
    except OSError:
        dirs = []
    items = [s for d in dirs for s in [_results_summary(d.name)] if s]
    items.sort(key=lambda s: s["modified_ts"], reverse=True)
    for item in items:
        item.pop("modified_ts", None)
    return jsonify(items=items)


@app.delete("/api/recent/<ticker>")
def remove_recent(ticker: str):
    ticker = _normalise_ticker(ticker)
    key = _job_key(g.user["id"], ticker)
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job["status"] in ("queued", "running"):
            abort(409, "A run is active for that ticker.")
    directory = _ticker_dir(ticker)
    if not directory.is_dir():
        abort(404, "No results for that ticker yet.")
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job["status"] in ("queued", "running"):
            abort(409, "A run is active for that ticker.")
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            abort(500, f"Could not remove results: {exc}")
        JOBS.pop(key, None)
    return jsonify(removed=ticker)


@app.get("/api/files/<ticker>")
def files(ticker: str):
    ticker = _normalise_ticker(ticker)
    summary = _results_summary(ticker)
    if not summary:
        return jsonify(
            error="No results for that ticker yet.",
            ticker=ticker,
            ticker_dir=str(_ticker_dir(ticker)),
            diagnostics=graph_diagnostics(_user_dir()),
        ), 404
    return jsonify(summary)


@app.get("/api/charts/<ticker>")
def chart_spec(ticker: str):
    """The run's chart specs (``charts.json``), drawn by the page with ECharts."""
    ticker = _normalise_ticker(ticker)
    spec = _chart_spec(ticker)
    if not spec:
        abort(404, "No charts for that ticker yet.")
    return jsonify(spec)


# Fact units as the search shows them: money (in the reporting currency),
# money per share, shares and ratios; anything else (counts, text) is left out.
FACT_UNITS = {"shares": "shares", "pure": "pure"}


def _fact_concept(concept) -> str:
    """``us-gaap:Goodwill`` and ``us-gaap_Goodwill`` both become ``Goodwill``."""
    text = str(concept)
    return re.split(r"[:_]", text, maxsplit=1)[-1] if re.match(r"^[\w-]+[:_]", text) else text


def _statement_unit(concept: str, label: str) -> str:
    if "PerShare" in concept or "per share" in label.lower():
        return "money/share"
    if "Shares" in concept or "shares" in label.lower():
        return "shares"
    return "money"


def _run_currency(folder: Path) -> str:
    """The latest filing's reporting currency (USD for runs made before it was kept)."""
    try:
        rows = _csv_records(folder / "financial_data.csv")
    except OSError:
        rows = []
    currencies = [str(row.get("currency") or "").upper() for row in rows if row.get("currency")]
    return currencies[-1] if currencies else "USD"


def _fact_unit(unit, currency, reporting: str) -> str | None:
    """A fact's unit for the search; None for money in another currency or other units."""
    unit = str(unit or "").lower().replace("_", "").replace("unit", "")
    code = str(currency or "").upper()
    if code and code != "NAN":
        if code != reporting:
            return None                       # a USD convenience copy, not the filer's figure
        return "money/share" if "per" in unit else "money"
    if unit in ("usd", "usdpershare"):       # runs from before facts carried a currency
        return "money/share" if "per" in unit else "money"
    return FACT_UNITS.get(unit)


@lru_cache(maxsize=16)
def _fact_index(directory: str, stamp: tuple, reporting: str = "USD") -> list[dict]:
    """Every statement line, then every other plain fact of the filing's own period.

    One row per concept: label, statement, unit and the value per fiscal year.
    ``stamp`` (the files' mtimes) keys the cache so a new run is re-read.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    folder = Path(directory)
    try:
        statements = pd.read_csv(folder / "statements.csv", low_memory=False)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        statements = pd.DataFrame()
    if not statements.empty:
        statements["value"] = pd.to_numeric(statements["value"], errors="coerce")
        statements["fiscal_year"] = pd.to_numeric(statements["fiscal_year"], errors="coerce")
        statements = statements.dropna(subset=["value", "fiscal_year"])
        statements["key"] = statements["concept"].map(_fact_concept)
        for (statement, key), group in statements.groupby(["statement", "key"], sort=False):
            label = str(group["label"].iloc[-1])
            values = group.groupby("fiscal_year")["value"].first()
            rows.append({"concept": key, "label": label, "statement": statement,
                         "unit": _statement_unit(key, label),
                         "values": {int(year): float(value) for year, value in values.items()}})
            seen.add(key)
    try:
        facts = pd.read_csv(folder / "facts.csv", low_memory=False)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        facts = pd.DataFrame()
    if not facts.empty and {"unit", "is_dimensioned", "period_of_report"} <= set(facts.columns):
        currencies = facts["currency"] if "currency" in facts.columns else pd.Series(None, index=facts.index)
        facts["unit"] = [_fact_unit(unit, currency, reporting) for unit, currency in zip(facts["unit"], currencies)]
        facts["value"] = pd.to_numeric(facts["value"], errors="coerce")
        facts["fiscal_year"] = pd.to_numeric(facts["fiscal_year"], errors="coerce")
        # Only the filing's own period: a 10-K also tags the prior years it compares against.
        own = (facts["period_end"].astype(str) == facts["period_of_report"].astype(str)) | (
            facts["period_instant"].astype(str) == facts["period_of_report"].astype(str))
        plain = facts["is_dimensioned"].astype(str).str.lower() != "true"
        facts = facts[own & plain].dropna(subset=["unit", "value", "fiscal_year"])
        facts["key"] = facts["concept"].map(_fact_concept)
        facts = facts[~facts["key"].isin(seen)]
        for key, group in facts.groupby("key", sort=False):
            values = group.groupby("fiscal_year")["value"].first()
            rows.append({"concept": key, "label": str(group["label"].iloc[-1]),
                         "statement": str(group["statement_type"].iloc[-1]), "unit": group["unit"].iloc[-1],
                         "values": {int(year): float(value) for year, value in values.items()}})
    return rows


@app.get("/api/facts/<ticker>")
def fact_index(ticker: str):
    """What the Charts tab's search looks through besides the charted series."""
    ticker = _normalise_ticker(ticker)
    folder = _ticker_dir(ticker)
    stamp = []
    for name in ("statements.csv", "facts.csv"):
        try:
            stamp.append((folder / name).stat().st_mtime_ns)
        except OSError:
            stamp.append(None)
    if stamp == [None, None]:
        abort(404, "No facts for that ticker yet.")
    currency = _run_currency(folder)
    return jsonify(currency=currency, facts=_fact_index(str(folder), tuple(stamp), currency))


@app.get("/api/checks/<ticker>")
def calc_checks(ticker: str):
    """The calculation check of the run: counts, and every total that does not add up."""
    ticker = _normalise_ticker(ticker)
    folder = _ticker_dir(ticker)
    path = folder / "calc_check.csv"
    if not path.is_file():
        abort(404, "No calculation check for that ticker yet.")
    try:
        rows = _csv_records(path)
    except OSError:
        abort(404, "No calculation check for that ticker yet.")
    counts = {kind: {"totals": 0, "ok": 0, "mismatch": 0, "incomplete": 0} for kind in ("statement", "note")}
    issues = []
    for row in rows:
        kind = row.get("kind") if row.get("kind") in counts else "statement"
        status = row.get("status") or ""
        counts[kind]["totals"] += 1
        if status in counts[kind]:
            counts[kind][status] += 1
        if status == "ok":
            continue
        try:
            parts = json.loads(row.get("detail") or "[]")
        except ValueError:
            parts = []
        issues.append({
            "fiscal_year": row.get("fiscal_year"), "kind": kind, "status": status,
            "statement": row.get("statement"), "concept": row.get("concept"), "label": row.get("label"),
            "reported": _number(row.get("reported")), "computed": _number(row.get("computed")),
            "difference": _number(row.get("difference")), "parts": parts,
            "hint": row.get("hint") or "",
        })
    # Newest year first; within a year the real mismatches before untagged parts.
    issues.sort(key=lambda item: (-(int(_number(item["fiscal_year"]) or 0)), item["status"] != "mismatch",
                                  item["kind"] != "statement", str(item["label"])))
    return jsonify(currency=_run_currency(folder), counts=counts,
                   filings=len({row.get("period_of_report") for row in rows}), issues=issues)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@app.get("/api/simulation/<ticker>")
def simulation(ticker: str):
    ticker = _normalise_ticker(ticker)
    period = request.args.get("range", "1y")
    periods = {
        "1mo": ("1mo", 30),
        "4mo": ("6mo", 122),
        "6mo": ("6mo", 183),
        "1y": ("1y", 365),
        "3y": ("5y", 1095),
        "5y": ("5y", 1826),
        "10y": ("10y", 3652),
        "max": ("max", None),
    }
    if period not in periods:
        abort(400, "Unsupported simulation range.")

    cache_key = (ticker, period)
    with _YAHOO_LOCK:
        cached = _SIM_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < 60:
            return jsonify(cached[1])

    yahoo_ticker = ticker.replace(".", "-")
    cutoff_days = periods[period][1]
    chart_range = periods[period][0]
    if cutoff_days and cutoff_days < 365:
        # Always cover a trailing year so dividends are available for projections.
        chart_range = "1y"
    query = urlencode({"range": chart_range, "interval": "1d", "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?{query}"
    req = Request(url, headers={"User-Agent": YAHOO_UA})
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        abort(502, f"Yahoo Finance request failed: {exc}")

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        message = (payload.get("chart") or {}).get("error") or {}
        abort(502, message.get("description") or "Yahoo Finance returned no price data.")

    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days) if cutoff_days else None
    prices = []
    for stamp, close in zip(result.get("timestamp") or [], quotes.get("close") or []):
        if close is None:
            continue
        day = datetime.fromtimestamp(stamp, timezone.utc)
        if cutoff and day < cutoff:
            continue
        prices.append({"date": day.date().isoformat(), "close": close})

    now = datetime.now(timezone.utc)
    dividend_cutoff = now - timedelta(days=365)
    dividends = []
    annual_dividend = 0.0
    for stamp, event in (result.get("events", {}).get("dividends") or {}).items():
        day = datetime.fromtimestamp(int(stamp), timezone.utc)
        amount = event.get("amount", 0) or 0
        if day >= dividend_cutoff:
            annual_dividend += amount
        # Keep the whole visible range for backtests; a shorter range wins.
        if cutoff is None or day >= cutoff:
            dividends.append({"date": day.date().isoformat(), "amount": amount})
    dividends.sort(key=lambda item: item["date"])

    trailing_pe = None
    dividend_rate = None
    try:
        opener, crumb = _yahoo_session()
        quote_url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/" + yahoo_ticker
        quote_url += "?" + urlencode({
            "modules": "summaryDetail,defaultKeyStatistics",
            "crumb": crumb,
        })
        with opener.open(Request(quote_url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
            quote = json.load(response)
        details = ((quote.get("quoteSummary") or {}).get("result") or [{}])[0]
        summary = details.get("summaryDetail", {})
        raw_pe = (summary.get("trailingPE") or {}).get("raw")
        if raw_pe is None:
            raw_pe = (details.get("defaultKeyStatistics", {}).get("trailingPE") or {}).get("raw")
        if raw_pe is None:
            stats = details.get("defaultKeyStatistics", {})
            eps = (stats.get("trailingEps") or {}).get("raw")
            spot = (result.get("meta") or {}).get("regularMarketPrice")
            if eps and spot:
                raw_pe = spot / eps
        trailing_pe = raw_pe
        dividend_rate = (summary.get("dividendRate") or {}).get("raw")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, IndexError, AttributeError):
        pass

    meta = result.get("meta", {})
    payload = {
        "ticker": ticker,
        "currency": meta.get("currency", "USD"),
        "price": meta.get("regularMarketPrice") or (prices[-1]["close"] if prices else None),
        "trailing_pe": trailing_pe,
        "dividend_rate": dividend_rate,
        "annual_dividend": annual_dividend,
        "prices": prices,
        "dividends": dividends,
    }
    with _YAHOO_LOCK:
        _SIM_CACHE[cache_key] = (time.time(), payload)
    return jsonify(payload)


@app.get("/api/evaluation/<ticker>")
def evaluation(ticker: str):
    ticker = _normalise_ticker(ticker)
    path = _ticker_dir(ticker) / "evaluation.csv"
    if not path.is_file():
        abort(404, "No evaluation data for that ticker yet.")

    history = _csv_records(path)
    latest = history[-1] if history else None

    transactions: list = []
    insider_generated = None
    insider_path = _ticker_dir(ticker) / "insider.json"
    if insider_path.is_file():
        try:
            insider = json.loads(insider_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            insider = {}
        transactions = insider.get("transactions") or []
        insider_generated = insider.get("generated")

    return jsonify(
        ticker=ticker,
        latest=latest,
        history=history,
        insiders=transactions,
        insider_generated=insider_generated,
        current=_current_quote(ticker),
    )


_NAME_SUFFIX = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|company|co\.?|ltd\.?|limited|plc|"
    r"holdings?|n\.v\.|s\.a\.|ag|se|l\.p\.)$", re.IGNORECASE)


def _search_quote(ticker: str) -> dict:
    """Yahoo's search hit for ``ticker`` ({} when unreachable); hits are cached per process."""
    with _YAHOO_LOCK:
        if ticker in _QUOTE_CACHE:
            return _QUOTE_CACHE[ticker]
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search?" + urlencode(
            {"q": ticker.replace(".", "-"), "quotesCount": 1, "newsCount": 0})
        with urlopen(Request(url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
            quote = (json.load(response).get("quotes") or [{}])[0]
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, IndexError, AttributeError):
        return {}                          # not cached, so the next call tries again
    quote = quote if isinstance(quote, dict) else {}
    with _YAHOO_LOCK:
        _QUOTE_CACHE[ticker] = quote
    return quote


def _company_name(ticker: str) -> tuple[str, str]:
    """(full name, search name) from Yahoo's search; both fall back to the ticker.

    The search name drops "The" and legal suffixes so news queries match how
    outlets write it; the full name is for display.
    """
    quote = _search_quote(ticker)
    full = (quote.get("longname") or quote.get("shortname") or "").strip()
    name = re.sub(r"^the\s+", "", full, flags=re.IGNORECASE)
    previous = None
    while name and name != previous:      # "Foo Holdings, Inc." -> "Foo"
        previous, name = name, _NAME_SUFFIX.sub("", name).strip()
    return full or ticker, name or ticker


def _company_sector(ticker: str) -> tuple[str, str]:
    """(sector, industry) from Yahoo's search; empty strings when unknown."""
    quote = _search_quote(ticker)
    return ((quote.get("sectorDisp") or quote.get("sector") or "").strip(),
            (quote.get("industryDisp") or quote.get("industry") or "").strip())


def _google_news(query: str, days: int, limit: int) -> list[dict]:
    """Top Google News stories for ``query`` from the last ``days``, in Google's ranking order."""
    url = "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    with urlopen(Request(url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
        root = ET.fromstring(response.read())
    articles, seen = [], set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith("https://") or title.lower() in seen:
            continue
        seen.add(title.lower())
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").isoformat()
        except (TypeError, ValueError):
            published = None
        articles.append({"title": title, "source": source, "link": link, "published": published})
        if len(articles) >= limit:
            break
    return articles


def _summary(html: str, limit: int = 280) -> str:
    """The first words of an RSS description: tags dropped, entities read, cut at a word."""
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", html or "")).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _yahoo_news(ticker: str, days: int, limit: int) -> list[dict]:
    """Yahoo Finance's headlines for ``ticker``, newest first, each with its opening lines."""
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urlencode(
        {"s": ticker.replace(".", "-"), "region": "US", "lang": "en-US"})
    with urlopen(Request(url, headers={"User-Agent": YAHOO_UA}), timeout=10) as response:
        root = ET.fromstring(response.read())
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    articles = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)   # "-0000" is UTC
        except (TypeError, ValueError):
            when = None
        if when and when < cutoff:
            continue
        articles.append({"title": title, "source": "Yahoo Finance", "link": link,
                         "published": when.isoformat() if when else None,
                         "summary": _summary(item.findtext("description") or "")})
    articles.sort(key=lambda a: a["published"] or "", reverse=True)
    return articles[:limit]


def _company_news(query: str, ticker: str, days: int, limit: int) -> list[dict]:
    """Google News (its ranking) and Yahoo Finance (newest first, with opening lines) by
    turns, repeats dropped; Yahoo being down leaves Google's list."""
    google = _google_news(query, days, limit)
    try:
        yahoo = _yahoo_news(ticker, days, limit)
    except NEWS_ERRORS:
        yahoo = []
    articles, seen = [], set()
    while len(articles) < limit and (google or yahoo):
        for feed in (google, yahoo):
            while feed:
                article = feed.pop(0)
                if article["title"].lower() not in seen:
                    seen.add(article["title"].lower())
                    articles.append(article)
                    break
    return articles[:limit]


def _choice(name: str, choices: tuple[int, ...], default: int) -> int:
    """An integer query parameter that must be one of ``choices``."""
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value not in choices:
        abort(400, f"{name} must be one of {', '.join(map(str, choices))}.")
    return value


# Home's headlines are about the stock, not the company's products: the ticker
# with market words, its sector/industry, and the wider market and economy.
# The pattern sets how the three feeds interleave (stock first and most often).
MARKET_QUERY = '("stock market" OR "Wall Street" OR "Federal Reserve" OR inflation OR "interest rates")'
MARKET_PATTERN = ("stock", "stock", "sector", "market")


def _market_queries(ticker: str) -> dict[str, str]:
    """Google News queries per Home topic; sector is left out when Yahoo does not know it."""
    sector, industry = _company_sector(ticker)
    industry = re.split(r"\s*[—–-]\s*", industry)[0]   # "Banks—Diversified" -> "Banks"
    queries = {"stock": f'"{ticker}" (stock OR shares OR "price target" OR analyst OR earnings)'}
    terms = [f'"{industry} stocks"'] if industry else []
    terms += [f'"{sector} sector"', f'"{sector} stocks"'] if sector else []
    if terms:
        queries["sector"] = f"({' OR '.join(terms)})"
    queries["market"] = MARKET_QUERY
    return queries


def _market_news(ticker: str, days: int, limit: int) -> list[dict]:
    """Stock, sector and market stories interleaved by MARKET_PATTERN, each tagged with its topic."""
    feeds = {topic: _google_news(query, days, limit) for topic, query in _market_queries(ticker).items()}
    articles, seen = [], set()
    while len(articles) < limit and any(feeds.values()):
        for topic in MARKET_PATTERN:
            feed = feeds.get(topic)
            while feed:
                article = feed.pop(0)
                if article["title"].lower() not in seen:
                    seen.add(article["title"].lower())
                    articles.append({**article, "topic": topic})
                    break
    return articles[:limit]


def _news_payload(ticker: str, days: int, limit: int, fresh: bool = False,
                  scope: str = "company") -> dict:
    """Articles for one ticker and choice of period/count/scope; cached per combination.

    ``company`` is everything about the company (the News tab); ``market`` is
    Home's stock, sector and economy mix. Raises the network error when Google
    News is unreachable.
    """
    key = (ticker, days, limit, scope)
    with _YAHOO_LOCK:
        cached = _NEWS_CACHE.get(key)
    if cached and time.time() - cached[0] < NEWS_TTL and not fresh:
        return cached[1]
    name, company = _company_name(ticker)
    if scope == "market":
        articles = _market_news(ticker, days, limit)
    else:
        articles = _company_news(f'"{company}"' if company != ticker else f"{ticker} stock", ticker, days, limit)
    payload = {"ticker": ticker, "name": name, "company": company, "days": days, "limit": limit,
               "scope": scope, "fetched": _now(), "articles": articles}
    with _YAHOO_LOCK:
        _NEWS_CACHE[key] = (time.time(), payload)
    return payload


NEWS_ERRORS = (HTTPError, URLError, TimeoutError, ET.ParseError)

@app.get("/api/news/<ticker>")
def news(ticker: str):
    """Recent articles: headline, outlet, date and link (plus topic for ``scope=market``)."""
    ticker = _normalise_ticker(ticker)
    days = _choice("days", NEWS_DAY_CHOICES, NEWS_DAYS)
    limit = _choice("limit", NEWS_LIMIT_CHOICES, NEWS_LIMIT)
    scope = request.args.get("scope") or "company"
    if scope not in NEWS_SCOPES:
        abort(400, f"scope must be one of {', '.join(NEWS_SCOPES)}.")
    try:
        return jsonify(_news_payload(ticker, days, limit, fresh=bool(request.args.get("fresh")),
                                     scope=scope))
    except NEWS_ERRORS as exc:
        abort(502, f"Could not reach Google News: {exc}")


SAVED_NEWS_MAX = 500     # per user
SAVED_FIELDS = ("link", "title", "source", "published", "ticker", "summary", "saved")


def _saved_news(uid: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(f"SELECT {', '.join(SAVED_FIELDS)} FROM saved_news WHERE user_id = ?"
                            " ORDER BY saved DESC", (uid,)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/saved-news")
def saved_news():
    """The articles this user saved from the News tab, newest save first."""
    return jsonify(articles=_saved_news(g.user["id"]))


@app.post("/api/saved-news")
def save_news():
    """Save one article (link, title, source, published, ticker, summary); saving it again is a no-op."""
    payload = request.get_json(silent=True) or {}
    article = {key: " ".join(str(payload.get(key) or "").split())[:1000] for key in SAVED_FIELDS[:-1]}
    if not article["link"].startswith("https://"):
        abort(400, "The article needs an https link.")
    if not article["title"]:
        abort(400, "The article needs a title.")
    uid = g.user["id"]
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM saved_news WHERE user_id = ?", (uid,)).fetchone()[0]
        exists = conn.execute("SELECT 1 FROM saved_news WHERE user_id = ? AND link = ?",
                              (uid, article["link"])).fetchone()
        if not exists and count >= SAVED_NEWS_MAX:
            abort(409, f"You can keep up to {SAVED_NEWS_MAX} saved articles; remove some first.")
        conn.execute(
            "INSERT OR IGNORE INTO saved_news (user_id, link, title, source, published, ticker, summary, saved)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, article["link"], article["title"], article["source"],
             article["published"] or None, article["ticker"].upper() or None, article["summary"] or None, _now()),
        )
    return jsonify(articles=_saved_news(uid))


@app.delete("/api/saved-news")
def unsave_news():
    """Remove one saved article, named by its link (``?link=``)."""
    link = request.args.get("link") or ""
    with _db() as conn:
        conn.execute("DELETE FROM saved_news WHERE user_id = ? AND link = ?", (g.user["id"], link))
    return jsonify(articles=_saved_news(g.user["id"]))


@app.get("/api/insiders/<ticker>")
def insiders(ticker: str):
    ticker = _normalise_ticker(ticker)
    path = _ticker_dir(ticker) / "insider.json"
    if not path.is_file():
        abort(404, "No insider data for that ticker yet.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return jsonify(
        ticker=ticker,
        generated=payload.get("generated"),
        # Files written before the 3-year window cover 6 months.
        months=payload.get("months") or 6,
        since=payload.get("since"),
        transactions=payload.get("transactions") or [],
    )


@app.get("/api/fundamentals/<ticker>")
def fundamentals(ticker: str):
    ticker = _normalise_ticker(ticker)
    path = _ticker_dir(ticker) / "financial_data.csv"
    if not path.is_file():
        abort(404, "No financial data for that ticker yet.")

    rows = _csv_records(path)
    years = [row.get("fiscal_year") or row.get("period_of_report") for row in rows]
    return jsonify(ticker=ticker, years=years, rows=rows)


SCREENER_FIELDS = (
    "fiscal_year",
    "pe",
    "ev_ebit",
    "earnings_yield",
    "return_on_capital",
    "fcf_yield",
    "pe_percentile",
    "f_score",
    "z_score",
)


def _number(value):
    """Finite float or None, for ranking mixed JSON values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _greenblatt_ranks(items: list[dict]) -> None:
    """Attach Magic Formula ranks: EY rank + return-on-capital rank.

    Lower combined rank is better. Items missing either input keep no rank.
    """
    eligible = [
        item for item in items
        if item.get("has_evaluation")
        and _number(item.get("earnings_yield")) is not None
        and _number(item.get("return_on_capital")) is not None
    ]
    for field in ("earnings_yield", "return_on_capital"):
        ranked = sorted(eligible, key=lambda item: _number(item[field]), reverse=True)
        for rank, item in enumerate(ranked, 1):
            item[f"_rank_{field}"] = rank
    for item in eligible:
        item["greenblatt_rank"] = (
            item.pop("_rank_earnings_yield") + item.pop("_rank_return_on_capital")
        )
    for item in items:
        item.pop("_rank_earnings_yield", None)
        item.pop("_rank_return_on_capital", None)


def _roc_fallback(directory: Path, fiscal_year) -> float | None:
    """ROC for an evaluation.csv written before the column existed.

    Older runs have ``earnings_yield`` but no ``return_on_capital``. Rebuild it
    from the artifacts already on disk instead of forcing a full re-run.
    """
    try:
        financial = _csv_records(directory / "financial_data.csv")
        statements = _csv_records(directory / "statements.csv")
    except OSError:
        return None
    row = next((item for item in financial if item.get("fiscal_year") == fiscal_year), None)
    if row is None:
        return None
    ebit = _number(row.get("OperatingIncomeLoss"))
    current_liabilities = _number(row.get("LiabilitiesCurrent"))
    total_assets = None
    for statement in statements:
        if statement.get("fiscal_year") != fiscal_year:
            continue
        concept = statement.get("standard_concept")
        label = str(statement.get("label") or "").strip().lower()
        if concept in ("Assets", "AssetsTotal", "TotalAssets") or label == "total assets":
            total_assets = _number(statement.get("value"))
            if total_assets is not None:
                total_assets /= 1_000_000  # statements are raw USD
                break
    if ebit is None or current_liabilities is None or total_assets is None:
        return None
    invested = total_assets - current_liabilities
    if invested <= 0 or ebit <= 0:
        return None
    return ebit / invested


def _screener_items() -> list[dict]:
    """The latest evaluation row of each of the user's tickers, with Greenblatt ranks."""
    items = []
    try:
        root = _user_dir()
        directories = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    except OSError:
        directories = []
    for directory in directories:
        if not TICKER_RE.match(directory.name):
            continue
        try:
            path = directory / "evaluation.csv"
            if not path.is_file():
                # Results exist but the pipeline wrote no valuation data (older
                # image, or Yahoo unreachable): surface it instead of hiding the
                # ticker from the screener.
                if (directory / "financial_data.csv").is_file():
                    modified = max(
                        (p.stat().st_mtime for p in directory.iterdir() if p.is_file()),
                        default=0,
                    )
                    items.append({
                        "ticker": directory.name,
                        "has_evaluation": False,
                        "modified": datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds"),
                    })
                continue
            rows = _csv_records(path)
            if not rows:
                continue
            latest = rows[-1]
            item = {"ticker": directory.name, "has_evaluation": True}
            for field in SCREENER_FIELDS:
                item[field] = latest.get(field)
            if item.get("return_on_capital") is None:
                item["return_on_capital"] = _roc_fallback(directory, latest.get("fiscal_year"))
            item["modified"] = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat(timespec="seconds")
            items.append(item)
        except OSError:
            continue
    items.sort(key=lambda item: item["ticker"])
    _greenblatt_ranks(items)
    return items


@app.get("/api/screener")
def screener():
    return jsonify(items=_screener_items(), pipeline=build_info())


@app.post("/api/screener")
def screener_run():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("tickers", [])
    if isinstance(raw, str):
        tokens = re.split(r"[,\s]+", raw.strip())
    elif isinstance(raw, list):
        tokens = raw
    else:
        abort(400, "tickers must be a list or a comma/space separated string.")

    tickers: list[str] = []
    for token in tokens:
        if not isinstance(token, str) or not token.strip():
            continue
        ticker = _normalise_ticker(token)
        if ticker not in tickers:
            tickers.append(ticker)
    if not tickers:
        abort(400, "Provide at least one ticker.")
    if len(tickers) > 25:
        abort(400, "Too many tickers (max 25).")
    try:
        filings = int(payload.get("filings", 10))
    except (TypeError, ValueError):
        abort(400, "filings must be a number")
    if not 1 <= filings <= 20:
        abort(400, "filings must be between 1 and 20")
    uid = g.user["id"]
    with JOBS_LOCK:
        SCREEN_BATCH[uid] = tickers

    queued: list[str] = []
    cached: list[str] = []
    items = []
    now = time.time()
    for ticker in tickers:
        path = _ticker_dir(ticker) / "evaluation.csv"
        if path.is_file() and now - path.stat().st_mtime < 7 * 86400:
            cached.append(ticker)
            items.append({"ticker": ticker, "status": "cached"})
            continue
        key = _job_key(uid, ticker)
        with JOBS_LOCK:
            job = JOBS.get(key)
            if job and job["status"] in ("queued", "running"):
                queued.append(ticker)
                items.append({"ticker": ticker, "status": job["status"]})
                continue
            job_id = secrets.token_hex(8)
            JOBS[key] = _new_job(uid, ticker, job_id, charts=False)
        threading.Thread(target=_worker, args=(key, filings, job_id), daemon=True).start()
        queued.append(ticker)
        items.append({"ticker": ticker, "status": "queued"})

    return jsonify(queued=queued, cached=cached, items=items), (202 if queued else 200)


@app.get("/files/<ticker>/<path:name>")
def serve_file(ticker: str, name: str):
    ticker = _normalise_ticker(ticker)
    if Path(name).suffix.lower() not in ALLOWED_FILES:
        abort(404)
    return send_from_directory(_ticker_dir(ticker), name, as_attachment=False)


@app.get("/download/<ticker>")
def download_ticker(ticker: str):
    """The Recent tab's download: the charts and data files, no extras."""
    return _download_bundle(ticker, default_extras=(), download_name=f"{_normalise_ticker(ticker)}_dashboard.zip")


def _csv_text(rows: list[dict]) -> str:
    """Rows as CSV; the columns are every key, in first-seen order."""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _months_ago(months: int) -> str:
    """The ISO date ``months`` before today (UTC); same cut-off as the Insiders tab."""
    today = datetime.now(timezone.utc).date()
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    day = min(today.day, calendar.monthrange(year, month + 1)[1])
    return f"{year:04d}-{month + 1:02d}-{day:02d}"


DEBUG_README = """\
# Stocky debug bundle

Made by Home's download so a bug can be fixed without re-running anything.
`build.json` names the exact code: `version` (VERSION file, major.minor.patch)
and `commit` (git SHA; `dirty: true` means uncommitted local edits). When
`commit` is null, `code_sha1` is a SHA-1 over every root `*.py` and
`templates/*.html` (sorted by name, each as name + NUL + bytes) to match against
a checkout.

- `build.json`: version, commit, Python and package versions, the options used.
- `jobs.json`: this account's runs in memory (status, error, exit code, times).
  Empty after a restart.
- `diagnostics.jsonl`: one JSON object per line, oldest first. `source` is
  `server` (failed requests, with a traceback for crashes), `pipeline` (failed
  runs with the log tail) or `browser` (script errors and failed fetches).
- `runs/<TICKER>.log`: the last pipeline log of every ticker of this account,
  with `runs/<TICKER>.json` (filings, charts, finish time of the last success).

The ticker's own results (CSVs, charts, JSON) are in the ticker folder next to
this one.
"""

# Packages whose versions most often explain a difference in output.
DEBUG_PACKAGES = ("flask", "waitress", "edgartools", "pandas", "numpy", "matplotlib", "requests")


def _debug_files(uid: str, options: dict) -> dict[str, str]:
    """The ``_debug/`` folder of Home's download (see DEBUG_README)."""
    packages = {}
    for name in DEBUG_PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    build = {**build_info(), "python": sys.version.split()[0], "platform": platform.platform(),
             "packages": packages, "generated": _now(), "options": options,
             "agent": request.headers.get("User-Agent", "")}
    build.pop("data", None)
    with JOBS_LOCK:
        jobs = [{k: v for k, v in job.items() if k != "uid"}
                for job in JOBS.values() if job.get("uid") == uid]
    files = {"README.md": DEBUG_README, "build.json": json.dumps(build, indent=2, default=str),
             "jobs.json": json.dumps(jobs, indent=2, default=str)}
    directory = _user_dir(uid)
    try:
        files["diagnostics.jsonl"] = (directory / DIAG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        files["diagnostics.jsonl"] = ""
    try:
        folders = sorted(d for d in directory.iterdir() if d.is_dir()) if directory.is_dir() else []
    except OSError:
        folders = []
    for folder in folders:
        for name, suffix in (("run.log", "log"), ("run.json", "json")):
            try:
                text = (folder / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files[f"runs/{folder.name}.{suffix}"] = "\n".join(text.splitlines()[-400:]) + "\n"
    return files


def _list_arg(name: str) -> list[str] | None:
    """A comma-separated query parameter; None when it is absent (everything)."""
    raw = request.args.get(name)
    if raw is None:
        return None
    return [item for item in raw.split(",") if item]


def _chart_images(folder: Path, spec: dict, ids: list[str], years: int, dark: bool) -> list[Path]:
    """The charts as PNGs, drawn on the first download and kept until the next run."""
    cache = folder / chartlib.PNG_CACHE_DIR / f"{'dark' if dark else 'light'}-{years or 'all'}"
    return [Path(p) for p in chartlib.render_pngs(spec, str(cache), ids=ids, years_per_image=years,
                                                  dark=dark, skip_existing=True)]


def _download_bundle(ticker: str, default_extras: tuple[str, ...], download_name: str):
    """A zip of what the download dialog ticked.

    ``charts`` and ``files`` name the charts and data files (absent: all of
    them), ``years`` the years per chart image (5, 10 or 0 for one image),
    ``theme`` light or dark, and ``extras`` any of insiders, news, screener and
    debug (absent: ``default_extras``). Insiders and news follow ``months``,
    ``news_limit`` and ``news_days``; news is left out when Google News is
    unreachable. ``debug`` adds a ``_debug/`` folder (build, jobs,
    diagnostics, every run log).
    """
    ticker = _normalise_ticker(ticker)
    folder = _ticker_dir(ticker)
    if not folder.is_dir():
        abort(404, "No results for that ticker yet.")
    spec = _chart_spec(ticker)
    chart_ids = [chart["id"] for chart in spec["charts"]] if spec else []
    wanted = _list_arg("charts")
    chart_ids = chart_ids if wanted is None else [cid for cid in chart_ids if cid in wanted]
    files = _data_files(folder)
    wanted = _list_arg("files")
    files = files if wanted is None else [name for name in files if name in wanted]
    extras = _list_arg("extras")
    extras = set(default_extras if extras is None else extras) & set(DOWNLOAD_EXTRAS)
    years = _choice("years", CHART_YEAR_CHOICES, chartlib.CHART_WINDOW)
    theme = request.args.get("theme", "light")
    if theme not in ("light", "dark"):
        abort(400, "theme must be light or dark.")
    months = _choice("months", INSIDER_MONTH_CHOICES, 12)
    news_days = _choice("news_days", NEWS_DAY_CHOICES, NEWS_DAYS)
    news_limit = _choice("news_limit", NEWS_LIMIT_CHOICES, NEWS_LIMIT)
    if not (chart_ids or files or extras):
        abort(400, "Nothing selected to download.")

    extra: dict[str, str] = {}
    if "insiders" in extras:
        try:
            trades = json.loads((folder / "insider.json").read_text(encoding="utf-8")).get("transactions") or []
            cutoff = _months_ago(months)
            extra["insiders.csv"] = _csv_text([t for t in trades if not t.get("date") or t["date"] >= cutoff])
        except (OSError, ValueError, AttributeError):
            pass
    if "news" in extras:
        try:
            extra["news.csv"] = _csv_text(_news_payload(ticker, news_days, news_limit)["articles"])
        except NEWS_ERRORS:
            pass
    if "screener" in extras:
        row = next((item for item in _screener_items() if item["ticker"] == ticker), None)
        if row:
            extra["screener.csv"] = _csv_text([row])
    images = _chart_images(folder, spec, chart_ids, years, theme == "dark") if chart_ids else []

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in files:
            bundle.write(folder / name, arcname=f"{ticker}/{name}")
        for path in images:
            bundle.write(path, arcname=f"{ticker}/charts/{path.name}")
        for name, text in extra.items():
            bundle.writestr(f"{ticker}/{name}", text)
        if "debug" in extras:
            options = {"months": months, "news_days": news_days, "news_limit": news_limit,
                       "charts": chart_ids, "years": years, "theme": theme}
            for name, text in _debug_files(g.user["id"], options).items():
                bundle.writestr(f"_debug/{name}", text)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=download_name)


@app.get("/download/<ticker>/all")
def download_all(ticker: str):
    """The download dialog (Home and Charts): see ``_download_bundle``; everything by default."""
    commit = (git_commit()["commit"] or "")[:7]
    name = f"{_normalise_ticker(ticker)}_everything_v{app_version()}{'_' + commit if commit else ''}.zip"
    return _download_bundle(ticker, default_extras=DOWNLOAD_EXTRAS, download_name=name)


@app.get("/download/<ticker>/<path:name>")
def download_file(ticker: str, name: str):
    ticker = _normalise_ticker(ticker)
    if Path(name).suffix.lower() not in ALLOWED_FILES:
        abort(404)
    return send_from_directory(_ticker_dir(ticker), name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8053")), threaded=True)
