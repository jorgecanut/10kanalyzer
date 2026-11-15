"""What went wrong while the insider trades were collected, written into ``insider.json``.

``insider.py`` never raises: a collection that fails gives an empty list and
a Form 4 that cannot be read is skipped, each with one warning line. An empty
or short list then reads as "this company has no insider trades". This module
runs ``insider.write_insiders`` and adds to the file what those warnings
said: ``error`` (why nothing was collected, else ``None``) and ``skipped``
(every Form 4 that could not be read, with its error).

It is a module of its own, listening to the warnings, on purpose: the Form 4
cache is versioned by the hash of ``insider.py`` (``filing_cache.KINDS``), so
any edit there makes every company's Form 4s be read from SEC again. When
``insider.py`` next has to change for a parsing fix, fold this into it.
"""

from __future__ import annotations

import json
import re
import threading

import insider

SKIPPED_RE = re.compile(r"^skipped filing (\S+): (.*)$", re.DOTALL)
# The warnings that mean nothing was collected (see ``insider._collect``).
FAILED_RE = re.compile(r"^(?:failed to collect insiders for \S+: (.*)|(edgartools unavailable: .*)|(empty ticker))$",
                       re.DOTALL)
_LOCK = threading.Lock()


def problems(warnings: list[str]) -> dict:
    """``{"error", "skipped"}`` from the warning lines of one collection."""
    out: dict = {"error": None, "skipped": []}
    for message in warnings:
        skipped = SKIPPED_RE.match(message)
        failed = FAILED_RE.match(message)
        if skipped:
            out["skipped"].append({"accession": None if skipped.group(1) == "?" else skipped.group(1),
                                   "error": skipped.group(2)})
        elif failed:
            out["error"] = next(group for group in failed.groups() if group)
    return out


def write_insiders(ticker: str, output_dir: str, months: int = insider.DEFAULT_MONTHS,
                   cache_dir: str | None = None) -> str | None:
    """``insider.write_insiders``, with ``error`` and ``skipped`` added to the file it wrote."""
    heard: list[str] = []
    with _LOCK:
        warn = insider._warn

        def listen(message: str) -> None:
            heard.append(str(message))
            warn(message)

        insider._warn = listen
        try:
            window = {} if months == insider.DEFAULT_MONTHS else {"months": months}
            path = insider.write_insiders(ticker, output_dir, cache_dir=cache_dir, **window)
        finally:
            insider._warn = warn
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.update(problems(heard))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except (OSError, ValueError) as exc:
        insider._warn(f"could not record the collection's problems in {path}: {exc}")
    return path
