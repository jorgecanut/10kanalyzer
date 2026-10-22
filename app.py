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

import io
import json
import hashlib
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD = BASE_DIR / "edgar_dashboard.py"

# Everything the app stores: the account database, the session key and each
# user's results. Read through functions so tests can point it elsewhere.
DATA_DIR = Path(os.environ.get("STOCKY_DATA_DIR", BASE_DIR / "data"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8

# Tickers: letters/digits plus the "." and "-" seen in symbols such as BRK.B.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
ALLOWED_FILES = {".png", ".csv", ".log"}

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
    return {"name": user["name"], "email": user["email"], "identity": _identity(user)}


def _user_dir(uid: str | None = None) -> Path:
    return DATA_DIR / "users" / (uid or g.user["id"])


def _job_key(uid: str, ticker: str) -> str:
    return f"{uid}:{ticker}"


def _ticker_dir(ticker: str, uid: str | None = None) -> Path:
    return _user_dir(uid) / ticker


def _charts(ticker: str, subdir: str = "") -> list[str]:
    directory = _ticker_dir(ticker) / subdir if subdir else _ticker_dir(ticker)
    try:
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.suffix.lower() == ".png")
    except OSError:
        return []


def _results_summary(ticker: str) -> dict | None:
    directory = _ticker_dir(ticker)
    try:
        if not directory.is_dir():
            return None
        files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_FILES]
    except OSError:
        return None
    if not files:
        return None
    modified = max(p.stat().st_mtime for p in files)
    return {
        "ticker": ticker,
        "charts": _charts(ticker),
        # Dark-theme copies of the charts (same names, under dark/).
        "dark_charts": _charts(ticker, "dark"),
        "has_csv": (directory / "financial_data.csv").exists(),
        "has_insider": (directory / "insider.json").is_file(),
        "modified": datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds"),
        "modified_ts": modified,
    }


def _log_tail(ticker: str, max_lines: int = 200) -> str:
    log = _ticker_dir(ticker) / "run.log"
    if not log.is_file():
        return ""
    text = log.read_text(errors="replace").splitlines()
    return "\n".join(text[-max_lines:])


YAHOO_UA = "Mozilla/5.0 (X11; Linux x86_64) Stocky/1.0"
_YAHOO_SESSION = {"opener": None, "crumb": "", "ts": 0.0}
_YAHOO_LOCK = threading.Lock()
_SIM_CACHE: dict = {}
_CURRENT_CACHE: dict = {}


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
    return render_template("index.html")


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


@app.get("/healthz")
def healthz():
    return jsonify(status="ok", **build_info())


def build_info() -> dict:
    """What pipeline code this deployment actually has on disk.

    Lets a stale container be identified from one request: compare
    ``pipeline_sha1`` / ``charts_sha1`` with the repo's
    ``sha1sum edgar_dashboard.py charts.py``.
    """
    info = {"version": os.environ.get("STOCKY_VERSION", "dev"), "features": []}
    for module in ("valuation", "insider"):
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
    if email.lower() != user["email"].lower() or new_password:
        if not check_password_hash(user["password_hash"], str(payload.get("current_password") or "")):
            abort(403, "Enter your current password to change the email or password.")
    password_hash = (generate_password_hash(_clean_password(new_password))
                     if new_password else user["password_hash"])
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE users SET name = ?, email = ?, password_hash = ? WHERE id = ?",
                (name, email, password_hash, user["id"]),
            )
    except sqlite3.IntegrityError:
        abort(409, "An account with that email already exists.")
    return jsonify(user=_public_user(_get_user(user["id"])))


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
    with _db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
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
    ticker = _normalise_ticker(ticker)
    with JOBS_LOCK:
        job = dict(JOBS.get(_job_key(g.user["id"], ticker), {}))
    job.pop("uid", None)

    if job:
        job["log"] = _log_tail(ticker)
        summary = _results_summary(ticker)
        job["charts"] = summary["charts"] if summary else []
        return jsonify(job)

    summary = _results_summary(ticker)
    if summary:
        # No in-memory job (e.g. after a restart) but results exist on disk.
        return jsonify(status="done", ticker=ticker, charts=summary["charts"], log=_log_tail(ticker))
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


@app.get("/api/screener")
def screener():
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
    return jsonify(items=items, pipeline=build_info())


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
    ticker = _normalise_ticker(ticker)
    summary = _results_summary(ticker)
    if not summary:
        abort(404, "No results for that ticker yet.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(_ticker_dir(ticker).iterdir()):
            if path.is_file() and path.suffix.lower() in ALLOWED_FILES:
                bundle.write(path, arcname=f"{ticker}/{path.name}")
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{ticker}_dashboard.zip",
    )


@app.get("/download/<ticker>/<path:name>")
def download_file(ticker: str, name: str):
    ticker = _normalise_ticker(ticker)
    if Path(name).suffix.lower() not in ALLOWED_FILES:
        abort(404)
    return send_from_directory(_ticker_dir(ticker), name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8053")), threaded=True)
