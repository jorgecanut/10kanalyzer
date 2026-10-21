#!/usr/bin/env python3
"""Minimal local web UI for the SEC EDGAR 10-K dashboard.

Enter a ticker, watch the run, then view or download the generated charts.
No auth, no database -- it shells out to ``edgar_dashboard.py`` and serves
whatever lands in ``financial_graphs/<TICKER>/``.

Run locally:   python app.py            (http://localhost:8053)
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).resolve().parent
GRAPH_DIR = BASE_DIR / "financial_graphs"
DASHBOARD = BASE_DIR / "edgar_dashboard.py"

# The SEC identity (name + email) sent as the EDGAR User-Agent. Read from the
# env var by default, but the UI can override it. The override is written here
# so it survives restarts and (because ./financial_graphs is bind-mounted)
# container rebuilds. The leading dot keeps it out of the graphs listing.
IDENTITY_FILE = GRAPH_DIR / ".edgar_identity"
DEFAULT_IDENTITY = "Data Analyst analyst@example.com"

# Tickers: letters/digits plus the "." and "-" seen in symbols such as BRK.B.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
ALLOWED_FILES = {".png", ".csv", ".log"}

app = Flask(__name__)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
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


def _get_identity() -> str:
    """Current SEC identity: UI override first, then env var, then default."""
    try:
        saved = IDENTITY_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        saved = ""
    return saved or os.environ.get("EDGAR_IDENTITY", DEFAULT_IDENTITY).strip()


def _set_identity(raw: str) -> str:
    """Validate and persist a new SEC identity; return the stored value."""
    # Collapse whitespace so "Name   you@x.com" and stray newlines normalise.
    identity = " ".join((raw or "").split())
    if not identity:
        abort(400, "Identity cannot be empty.")
    if len(identity) > 200:
        abort(400, "Identity is too long (max 200 characters).")
    # SEC wants a reachable contact; require an email-looking token.
    if "@" not in identity:
        abort(400, "SEC requires a contact email, e.g. Jane Doe jane@example.com.")
    try:
        IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        IDENTITY_FILE.write_text(identity + "\n", encoding="utf-8")
    except OSError as exc:
        abort(500, f"Could not save identity: {exc}")
    return identity


def _ticker_dir(ticker: str) -> Path:
    return GRAPH_DIR / ticker


def _charts(ticker: str) -> list[str]:
    directory = _ticker_dir(ticker)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix.lower() == ".png")


def _results_summary(ticker: str) -> dict | None:
    directory = _ticker_dir(ticker)
    if not directory.is_dir():
        return None
    files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_FILES]
    if not files:
        return None
    modified = max(p.stat().st_mtime for p in files)
    return {
        "ticker": ticker,
        "charts": _charts(ticker),
        "has_csv": (directory / "financial_data.csv").exists(),
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


def _worker(ticker: str, filings: int) -> None:
    """Run the dashboard in a subprocess, one at a time."""
    with RUN_LOCK:
        directory = _ticker_dir(ticker)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "run.log"

        with JOBS_LOCK:
            JOBS[ticker].update(status="running", started=_now())
        try:
            env = {**os.environ, "EDGAR_IDENTITY": _get_identity()}
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    [sys.executable, str(DASHBOARD), "--ticker", ticker, "--filings", str(filings)],
                    cwd=str(BASE_DIR),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                with JOBS_LOCK:
                    JOBS[ticker]["pid"] = proc.pid
                try:
                    proc.wait(timeout=1800)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    code = -1
                    log.write(f"[{_now()}] killed after 30-minute timeout\n")
                    log.flush()
            status = "done" if code == 0 else "error"
        except Exception as exc:  # noqa: BLE001 - surface any launch failure in the UI
            status, code = "error", -1
            log_path.write_text(f"Failed to launch dashboard: {exc}\n")

        with JOBS_LOCK:
            JOBS[ticker].update(status=status, finished=_now(), returncode=code)


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
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/api/identity")
def get_identity():
    return jsonify(identity=_get_identity())


@app.post("/api/identity")
def update_identity():
    payload = request.get_json(silent=True) or {}
    identity = _set_identity(payload.get("identity", ""))
    return jsonify(identity=identity)


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

    with JOBS_LOCK:
        job = JOBS.get(ticker)
        if job and job["status"] in ("queued", "running"):
            return jsonify(status=job["status"], ticker=ticker, message="Already running"), 200

    with JOBS_LOCK:
        JOBS[ticker] = {"ticker": ticker, "status": "queued", "queued": _now()}

    threading.Thread(target=_worker, args=(ticker, filings), daemon=True).start()
    return jsonify(status="queued", ticker=ticker), 202


@app.get("/api/status/<ticker>")
def status(ticker: str):
    ticker = _normalise_ticker(ticker)
    with JOBS_LOCK:
        job = dict(JOBS.get(ticker, {}))

    if job:
        job["log"] = _log_tail(ticker)
        summary = _results_summary(ticker)
        job["charts"] = summary["charts"] if summary else []
        return jsonify(job)

    summary = _results_summary(ticker)
    if summary:
        # No in-memory job (e.g. after a restart) but results exist on disk.
        return jsonify(status="done", ticker=ticker, charts=summary["charts"], log="")
    return jsonify(status="idle", ticker=ticker)


@app.get("/api/recent")
def recent():
    if not GRAPH_DIR.is_dir():
        return jsonify(items=[])
    items = [s for d in GRAPH_DIR.iterdir() if d.is_dir() for s in [_results_summary(d.name)] if s]
    items.sort(key=lambda s: s["modified_ts"], reverse=True)
    for item in items:
        item.pop("modified_ts", None)
    return jsonify(items=items)


@app.delete("/api/recent/<ticker>")
def remove_recent(ticker: str):
    ticker = _normalise_ticker(ticker)
    with JOBS_LOCK:
        job = JOBS.get(ticker)
        if job and job["status"] in ("queued", "running"):
            abort(409, "A run is active for that ticker.")
    directory = _ticker_dir(ticker)
    if not directory.is_dir():
        abort(404, "No results for that ticker yet.")
    with JOBS_LOCK:
        job = JOBS.get(ticker)
        if job and job["status"] in ("queued", "running"):
            abort(409, "A run is active for that ticker.")
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            abort(500, f"Could not remove results: {exc}")
        JOBS.pop(ticker, None)
    return jsonify(removed=ticker)


@app.get("/api/files/<ticker>")
def files(ticker: str):
    ticker = _normalise_ticker(ticker)
    summary = _results_summary(ticker)
    if not summary:
        abort(404, "No results for that ticker yet.")
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

    dividend_cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    dividends = []
    for stamp, event in (result.get("events", {}).get("dividends") or {}).items():
        day = datetime.fromtimestamp(int(stamp), timezone.utc)
        if day >= dividend_cutoff:
            dividends.append({"date": day.date().isoformat(), "amount": event.get("amount", 0)})
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
        "prices": prices,
        "dividends": dividends,
    }
    with _YAHOO_LOCK:
        _SIM_CACHE[cache_key] = (time.time(), payload)
    return jsonify(payload)


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
