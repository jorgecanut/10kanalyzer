#!/usr/bin/env python3
"""What each SEC filing yielded, kept by accession number.

A filing never changes once it is filed, so what a run extracted from it can
be reused by every later run: a new run downloads and parses only the filings
it has not seen. The entries are extracted values, never the XBRL itself.

An entry is valid only for the code that wrote it. Each kind of entry names
the modules that shape it (``KINDS``); the hash of those files plus the
edgartools release is the kind's version, and every version has its own
folder. Code that changes what is extracted gets a new, empty folder, while
the old one stays: reverting the change finds its entries again, and
``prune`` deletes versions only when asked. Code outside those modules
(margins, valuation, charts) never touches the cache.

Layout, under one root shared by every user (it is public SEC data)::

    <root>/<kind>/<version>/version.json            when it was made, by which code
    <root>/annual/<version>/<CIK>/<accession>.json.gz   one 10-K, 20-F or 40-F
    <root>/form4/<version>/<CIK>/form4.json.gz          every Form 4 of the company read so far

Failures are never fatal: an unreadable entry is a miss and a failed write is
skipped with a warning, so the cache can save a run work but never cost it.

Usage (the root defaults to $STOCKY_FILING_CACHE, else the app's data/filing_cache):
    python filing_cache.py status                 versions, sizes, what changed, what is stale
    python filing_cache.py prune --keep 3         dry run: what would be deleted
    python filing_cache.py prune --keep 3 --yes   keep the current and the 2 newest other versions
    python filing_cache.py delete <version> --yes
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Kind -> the modules whose code shapes its entries. Keep code that only uses
# the extracted data out of these files, or every edit to it re-parses SEC.
KINDS: dict[str, tuple[str, ...]] = {
    "annual": ("extract.py", "xbrl_extract.py", "calc_check.py"),
    "form4": ("insider.py",),
}
META = "version.json"
# Accession numbers ("0001193125-26-323660") and the fixed entry names; nothing
# else may become a file name.
NAME_RE = re.compile(r"^(?:\d{10}-\d{2}-\d{6}|form4)$")
VERSION_RE = re.compile(r"^[0-9a-f]{12}$")


def _edgartools() -> str:
    try:
        return metadata.version("edgartools")
    except metadata.PackageNotFoundError:
        return ""


def code(kind: str) -> dict[str, str]:
    """The hash of each module of ``kind`` and the edgartools release."""
    hashes = {name: hashlib.sha1((HERE / name).read_bytes()).hexdigest()[:12] for name in KINDS[kind]}
    return {**hashes, "edgartools": _edgartools()}


def version(kind: str) -> str:
    """The current version of ``kind``: one hash over ``code(kind)``."""
    return hashlib.sha1(json.dumps(code(kind), sort_keys=True).encode()).hexdigest()[:12]


def default_root() -> Path:
    data = Path(os.environ.get("STOCKY_DATA_DIR", HERE / "data"))
    return Path(os.environ.get("STOCKY_FILING_CACHE") or data / "filing_cache")


def _path(root, kind: str, ver: str, cik, name: str) -> Path | None:
    if kind not in KINDS or not VERSION_RE.match(ver) or not NAME_RE.match(name):
        return None
    try:
        return Path(root) / kind / ver / str(int(cik)) / f"{name}.json.gz"
    except (TypeError, ValueError):
        return None


def _plain(value):
    """JSON for the numpy scalars and dates pandas hands back."""
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def load(root, kind: str, ver: str, cik, name: str):
    """The cached data of ``name``, or ``None`` when absent or unreadable."""
    if not root:
        return None
    path = _path(root, kind, ver, cik, name)
    if path is None or not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != ver:
        return None
    return entry.get("data")


def _write(path: Path, payload: bytes, compress: bool) -> None:
    """Atomic: a killed run never leaves half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw:
            if compress:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
                    handle.write(payload)
            else:
                raw.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def store(root, kind: str, ver: str, cik, name: str, data) -> None:
    """Write ``data`` for ``name``; the first entry of a version also records its code."""
    if not root:
        return
    path = _path(root, kind, ver, cik, name)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = path.parent.parent / META
        if not meta.is_file():
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _write(meta, json.dumps({"version": ver, "created": created, "code": code(kind)},
                                    indent=2).encode(), compress=False)
        _write(path, json.dumps({"version": ver, "data": data}, default=_plain).encode(), compress=True)
    except (OSError, TypeError, ValueError) as exc:
        print(f"    [!] filing cache: {name} not saved ({type(exc).__name__}: {exc})", file=sys.stderr)


def cached_elsewhere(root, kind: str, ver: str, cik, name: str) -> bool:
    """Whether another version holds ``name``: a miss caused by a code change."""
    if not root:
        return False
    try:
        cik = str(int(cik))
        base = Path(root) / kind
        return any((base / other / cik / f"{name}.json.gz").is_file()
                   for other in os.listdir(base) if other != ver and VERSION_RE.match(other))
    except (OSError, TypeError, ValueError):
        return False


def versions(root, kind: str) -> list[dict]:
    """Every version folder of ``kind``, newest first: ``version``, ``created``, ``code``."""
    out = []
    base = Path(root) / kind
    try:
        names = [name for name in os.listdir(base) if VERSION_RE.match(name) and (base / name).is_dir()]
    except OSError:
        return out
    for name in names:
        try:
            meta = json.loads((base / name / META).read_text())
        except (OSError, ValueError):
            meta = {}
        out.append({"version": name, "created": meta.get("created", ""), "code": meta.get("code", {})})
    out.sort(key=lambda item: item["created"], reverse=True)
    return out


def changes(old: dict, new: dict) -> list[str]:
    """What differs between two ``code`` records, as file names."""
    return [name for name in sorted({*old, *new}) if old.get(name) != new.get(name)]


def explain(root, kind: str) -> str:
    """One log line: the current version and, when it is new, which code made it new."""
    ver = version(kind)
    found = versions(root, kind) if root else []
    if any(item["version"] == ver for item in found):
        return f"version {ver}"
    older = [item for item in found if item["code"]]
    if not older:
        return f"version {ver} (empty: the first run with a filing cache)"
    last = older[0]
    changed = ", ".join(changes(last["code"], code(kind))) or "the code"
    return (f"version {ver} (new: {changed} changed since {last['version']} of {last['created'][:10]}, "
            "so filings cached by older versions are parsed again)")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _entries(folder: Path) -> tuple[dict[str, set[str]], int]:
    """CIK -> entry names under one version folder, and their total size in bytes."""
    found: dict[str, set[str]] = {}
    size = 0
    for current, _, files in os.walk(folder):
        for file in files:
            if not file.endswith(".json.gz"):
                continue
            found.setdefault(Path(current).name, set()).add(file[:-len(".json.gz")])
            try:
                size += (Path(current) / file).stat().st_size
            except OSError:
                pass
    return found, size


def _size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _status(root: Path) -> None:
    print(f"Filing cache: {root}")
    legacy = [p for p in root.iterdir() if p.is_dir() and p.name.isdigit()] if root.is_dir() else []
    for kind in KINDS:
        current = version(kind)
        found = versions(root, kind)
        print(f"\n{kind}: current version {current} ({', '.join(f'{k} {v}' for k, v in code(kind).items())})")
        if not any(item["version"] == current for item in found):
            print("  (no entries yet for the current version)")
        current_entries: dict[str, set[str]] = {}
        stale: dict[str, set[str]] = {}
        for item in found:
            entries, size = _entries(root / kind / item["version"])
            count = sum(len(names) for names in entries.values())
            mark = "current" if item["version"] == current else ", ".join(changes(item["code"], code(kind)))
            print(f"  {item['version']}  {item['created'][:16] or '?':16}  {len(entries):6} companies "
                  f"{count:8} entries  {_size(size):>9}  {mark}")
            if item["version"] == current:
                current_entries = entries
            else:
                for cik, names in entries.items():
                    stale.setdefault(cik, set()).update(names)
        missing = {cik: names - current_entries.get(cik, set()) for cik, names in stale.items()}
        missing = {cik: names for cik, names in missing.items() if names}
        if missing:
            print(f"  stale: {sum(map(len, missing.values()))} entries of {len(missing)} companies exist only "
                  "in older versions; they are parsed again (SEC requests) when those companies run")
    if legacy:
        print(f"\nlegacy: {len(legacy)} company folders in the old unversioned layout (never read; prune removes them)")


def _remove(root: Path, doomed: list[tuple[str, str]], legacy: list[Path], yes: bool) -> None:
    for kind, ver in doomed:
        print(f"{'deleting' if yes else 'would delete'} {kind}/{ver}")
        if yes:
            shutil.rmtree(root / kind / ver, ignore_errors=True)
    for folder in legacy:
        print(f"{'deleting' if yes else 'would delete'} legacy {folder.name}/")
        if yes:
            shutil.rmtree(folder, ignore_errors=True)
    if not doomed and not legacy:
        print("nothing to delete")
    elif not yes:
        print("dry run: add --yes to delete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and prune the filing cache.")
    parser.add_argument("--root", type=Path, default=default_root(), help="Cache root (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Versions, sizes, what changed and what is stale")
    prune = commands.add_parser("prune", help="Delete all but the current and the newest other versions")
    prune.add_argument("--keep", type=int, default=3, help="Versions to keep per kind, current included")
    prune.add_argument("--yes", action="store_true", help="Really delete (default: dry run)")
    delete = commands.add_parser("delete", help="Delete one version (never the current one)")
    delete.add_argument("version")
    delete.add_argument("--yes", action="store_true", help="Really delete (default: dry run)")
    args = parser.parse_args(argv)
    root = args.root

    if args.command == "status":
        _status(root)
        return 0
    legacy = [p for p in root.iterdir() if p.is_dir() and p.name.isdigit()] if root.is_dir() else []
    doomed: list[tuple[str, str]] = []
    for kind in KINDS:
        current = version(kind)
        others = [item["version"] for item in versions(root, kind) if item["version"] != current]
        if args.command == "prune":
            doomed += [(kind, ver) for ver in others[max(0, args.keep - 1):]]
        elif args.version in others:
            doomed.append((kind, args.version))
        elif args.version == current:
            print(f"{kind}/{current} is the current version; it is not deleted")
    if args.command == "delete":
        legacy = []
    _remove(root, doomed, legacy, args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
