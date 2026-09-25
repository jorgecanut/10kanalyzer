#!/usr/bin/env python3
"""Minimal local web UI for the SEC EDGAR 10-K dashboard.

Enter a ticker, watch the run, then view or download the generated charts.
No auth, no database -- it shells out to ``edgar_dashboard.py`` and serves
whatever lands in ``financial_graphs/<TICKER>/``.

Run locally:   python app.py            (http://localhost:8000)
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

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


def _worker(ticker: str, filings: int) -> None:
    """Run the dashboard in a subprocess, one at a time."""
    with RUN_LOCK:
        directory = _ticker_dir(ticker)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "run.log"

        with JOBS_LOCK:
            JOBS[ticker].update(status="running", started=_now())
        try:
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    [sys.executable, str(DASHBOARD), "--ticker", ticker, "--filings", str(filings)],
                    cwd=str(BASE_DIR),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                with JOBS_LOCK:
                    JOBS[ticker]["pid"] = proc.pid
                code = proc.wait()
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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/api/run")
def run():
    payload = request.get_json(silent=True) or {}
    ticker = _normalise_ticker(payload.get("ticker", ""))
    try:
        filings = int(payload.get("filings", 10))
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


@app.get("/api/files/<ticker>")
def files(ticker: str):
    ticker = _normalise_ticker(ticker)
    summary = _results_summary(ticker)
    if not summary:
        abort(404, "No results for that ticker yet.")
    return jsonify(summary)


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
    app.run(host="0.0.0.0", port=8009, threaded=True)
