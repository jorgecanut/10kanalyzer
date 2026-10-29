#!/usr/bin/env python3
"""What each SEC filing says, kept by accession number.

A filing never changes once it is filed, so it is read once: ``dump.py``
copies everything it says (every fact of every period, the linkbase trees,
the statements) and the dump is kept here for good. Every later run, and
every fix to how numbers are chosen (``extract.py``, ``calc_check.py``),
works from the dump with no SEC request.

Versioning (no change may force re-reading everything):

- The **schema** (``dump.SCHEMA``) names the folder. It changes only when
  what is stored changes, by hand; then, and only then, every filing is read
  again.
- The **reader** (the hash of ``dump.py`` plus the edgartools release) is
  stamped on every entry. A new reader keeps every existing entry valid.
  ``readers.json`` records each reader and how its change was classified:
  ``format`` (the first reader of a schema), ``compatible`` (same data), or
  ``bug`` (the entries of the readers it names were wrong: remove just those
  with ``invalidate --reader``). A test fails until the current reader is
  classified (``classify``).

Form 4 entries keep the older scheme: the hash of ``insider.py`` is their
version, one folder per version (``KINDS``).

Layout, under one root shared by every user (it is public SEC data)::

    <root>/filings/schema-<N>/<CIK>/<accession>.json.gz   one 10-K, 10-KT, 20-F, 40-F or 10-Q
    <root>/form4/<version>/version.json                   when it was made, by which code
    <root>/form4/<version>/<CIK>/form4.json.gz            every Form 4 of the company read so far

Failures are never fatal: an unreadable entry is a miss and a failed write is
skipped with a warning, so the cache can save a run work but never cost it.

Usage (the root defaults to $STOCKY_FILING_CACHE, else the app's data/filing_cache):
    python filing_cache.py status                       schemas, readers, sizes, old folders
    python filing_cache.py invalidate --reader ID       dry run: the entries that reader wrote
    python filing_cache.py invalidate --form 10-Q --before 2015-01-01 --yes
    python filing_cache.py prune --yes                  delete old schemas, old Form 4 versions, old layouts
    python filing_cache.py classify compatible "why"    record the current reader in readers.json
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
# The dump: one file per filing, under a folder named by the schema.
DUMP_KIND = "filings"
READER_FILES = ("dump.py",)
READERS_FILE = "readers.json"
CLASSES = ("format", "compatible", "bug")
# Older kinds versioned by the hash of the modules that shape them. Keep code
# that only uses the data out of these files, or every edit re-reads SEC.
KINDS: dict[str, tuple[str, ...]] = {
    "form4": ("insider.py",),
}
# Folders of earlier layouts, never read: ``prune`` removes them.
LEGACY_KINDS = ("annual",)
META = "version.json"
# Accession numbers ("0001193125-26-323660") and the fixed entry names; nothing
# else may become a file name.
NAME_RE = re.compile(r"^(?:\d{10}-\d{2}-\d{6}|form4)$")
ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
VERSION_RE = re.compile(r"^[0-9a-f]{12}$")
SCHEMA_RE = re.compile(r"^schema-(\d+)$")


def _edgartools() -> str:
    try:
        return metadata.version("edgartools")
    except metadata.PackageNotFoundError:
        return ""


def _hash(name: str) -> str:
    """A file's hash, the same with Windows or Unix line endings."""
    return hashlib.sha1((HERE / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_root() -> Path:
    data = Path(os.environ.get("STOCKY_DATA_DIR", HERE / "data"))
    return Path(os.environ.get("STOCKY_FILING_CACHE") or data / "filing_cache")


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


def _plain(value):
    """JSON for the numpy scalars and dates pandas hands back."""
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _read(path: Path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, EOFError):
        return None


# --------------------------------------------------------------------------- #
# The dump: schema folders, reader stamps
# --------------------------------------------------------------------------- #

def schema() -> int:
    import dump

    return dump.SCHEMA


def reader() -> dict[str, str]:
    """What wrote an entry: the hash of each reader module and the edgartools release."""
    return {**{name: _hash(name) for name in READER_FILES}, "edgartools": _edgartools()}


def reader_id(stamp: dict | None = None) -> str:
    """One short id for a reader stamp (the current one by default)."""
    return hashlib.sha1(json.dumps(stamp or reader(), sort_keys=True).encode()).hexdigest()[:12]


def readers() -> dict:
    """``readers.json``: every reader so far and how its change was classified."""
    try:
        data = json.loads((HERE / READERS_FILE).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("readers", [])
    return data


def classification(rid: str | None = None) -> dict | None:
    """The ``readers.json`` record of a reader id (the current reader by default)."""
    rid = rid or reader_id()
    return next((item for item in readers()["readers"] if item.get("id") == rid), None)


def explain_reader() -> str:
    """One log line: the schema, the reader, and whether the reader is classified."""
    rid, record = reader_id(), classification()
    stamp = ", ".join(f"{key} {value}" for key, value in reader().items())
    if record is None:
        return (f"schema {schema()}, reader {rid} ({stamp}) NOT classified in readers.json: "
                "run `python filing_cache.py classify ...` before trusting new dumps")
    return f"schema {schema()}, reader {rid} ({stamp}; {record.get('class')})"


def _dump_dir(root, ver: int | None = None) -> Path:
    return Path(root) / DUMP_KIND / f"schema-{schema() if ver is None else ver}"


def dump_path(root, cik, accession: str, ver: int | None = None) -> Path | None:
    if not root or not ACCESSION_RE.match(str(accession or "")):
        return None
    try:
        return _dump_dir(root, ver) / str(int(cik)) / f"{accession}.json.gz"
    except (TypeError, ValueError):
        return None


def load_dump(root, cik, accession: str) -> dict | None:
    """The dump of ``accession`` in the current schema, or ``None`` when absent or unreadable."""
    path = dump_path(root, cik, accession)
    if path is None or not path.is_file():
        return None
    entry = _read(path)
    if not isinstance(entry, dict) or entry.get("schema") != schema():
        return None
    return entry


def store_dump(root, cik, accession: str, data: dict) -> bool:
    """Write one filing's dump, stamped with the current reader; False when not written."""
    path = dump_path(root, cik, accession)
    if path is None:
        return False
    stamp = reader()
    entry = {**data, "reader": stamp, "reader_id": reader_id(stamp), "stored": _now()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, json.dumps(entry, default=_plain).encode(), compress=True)
        return True
    except (OSError, TypeError, ValueError) as exc:
        print(f"    [!] filing cache: {accession} not saved ({type(exc).__name__}: {exc})", file=sys.stderr)
        return False


def dumped_elsewhere(root, cik, accession: str) -> list[int]:
    """Older schemas that hold ``accession``: a miss caused by a format change."""
    if not root:
        return []
    found = []
    for folder in _schema_dirs(root):
        ver = int(SCHEMA_RE.match(folder.name).group(1))
        if ver != schema() and (folder / str(int(cik)) / f"{accession}.json.gz").is_file():
            found.append(ver)
    return found


def _schema_dirs(root) -> list[Path]:
    base = Path(root) / DUMP_KIND
    try:
        return sorted((base / name for name in os.listdir(base) if SCHEMA_RE.match(name)), key=lambda p: p.name)
    except OSError:
        return []


def entries(root, ver: int | None = None):
    """Every dump file of a schema: ``(cik, accession, path)``."""
    folder = _dump_dir(root, ver)
    if not folder.is_dir():
        return
    for cik in sorted(os.listdir(folder)):
        if not cik.isdigit():
            continue
        for name in sorted(os.listdir(folder / cik)):
            if name.endswith(".json.gz") and ACCESSION_RE.match(name[:-len(".json.gz")]):
                yield cik, name[:-len(".json.gz")], folder / cik / name


def select(root, reader: str | None = None, form: str | None = None, before: str | None = None,
           cik: str | None = None, accession: str | None = None) -> list[tuple[str, str, Path, dict]]:
    """Current-schema entries matching every filter given: ``(cik, accession, path, filing)``.

    ``before`` compares with the filing date (``YYYY-MM-DD``); ``reader`` is a
    reader id from the stamps (``status`` lists them).
    """
    out = []
    for entry_cik, entry_accession, path in entries(root):
        if cik and str(int(cik)) != entry_cik:
            continue
        if accession and accession != entry_accession:
            continue
        data = _read(path) if (reader or form or before) else {}
        if data is None:
            data = {"reader_id": "unreadable"}
        meta = data.get("filing") or {}
        if reader and data.get("reader_id") != reader:
            continue
        if form and str(meta.get("form") or "").upper() != form.upper():
            continue
        if before and not str(meta.get("filed") or "9999") < before:
            continue
        out.append((entry_cik, entry_accession, path, meta))
    return out


# --------------------------------------------------------------------------- #
# Kinds versioned by a code hash (Form 4)
# --------------------------------------------------------------------------- #

def code(kind: str) -> dict[str, str]:
    """The hash of each module of ``kind`` and the edgartools release."""
    return {**{name: _hash(name) for name in KINDS[kind]}, "edgartools": _edgartools()}


def version(kind: str) -> str:
    """The current version of ``kind``: one hash over ``code(kind)``."""
    return hashlib.sha1(json.dumps(code(kind), sort_keys=True).encode()).hexdigest()[:12]


def _path(root, kind: str, ver: str, cik, name: str) -> Path | None:
    if kind not in KINDS or not VERSION_RE.match(ver) or not NAME_RE.match(name):
        return None
    try:
        return Path(root) / kind / ver / str(int(cik)) / f"{name}.json.gz"
    except (TypeError, ValueError):
        return None


def load(root, kind: str, ver: str, cik, name: str):
    """The cached data of ``name``, or ``None`` when absent or unreadable."""
    if not root:
        return None
    path = _path(root, kind, ver, cik, name)
    if path is None or not path.is_file():
        return None
    entry = _read(path)
    if not isinstance(entry, dict) or entry.get("version") != ver:
        return None
    return entry.get("data")


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
            _write(meta, json.dumps({"version": ver, "created": _now(), "code": code(kind)},
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

def _size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _tree_size(folder: Path) -> tuple[int, int]:
    """Files and bytes under ``folder``."""
    count = size = 0
    for current, _, files in os.walk(folder):
        for file in files:
            if file.endswith(".json.gz"):
                count += 1
                try:
                    size += (Path(current) / file).stat().st_size
                except OSError:
                    pass
    return count, size


def _legacy(root: Path) -> list[Path]:
    """Folders of earlier layouts: unversioned company folders and old kinds."""
    if not root.is_dir():
        return []
    return [path for path in root.iterdir()
            if path.is_dir() and (path.name.isdigit() or path.name in LEGACY_KINDS)]


def _status(root: Path) -> None:
    print(f"Filing cache: {root}")
    print(f"\n{DUMP_KIND}: {explain_reader()}")
    known = {item.get("id"): item for item in readers()["readers"]}
    for folder in _schema_dirs(root):
        ver = int(SCHEMA_RE.match(folder.name).group(1))
        current = ver == schema()
        by_reader: dict[str, int] = {}
        forms: dict[str, int] = {}
        companies: set[str] = set()
        size = 0
        for cik, _accession, path in entries(root, ver):
            companies.add(cik)
            try:
                size += path.stat().st_size
            except OSError:
                pass
            if current:
                data = _read(path) or {}
                rid = data.get("reader_id", "unreadable")
                by_reader[rid] = by_reader.get(rid, 0) + 1
                form = str((data.get("filing") or {}).get("form") or "?")
                forms[form] = forms.get(form, 0) + 1
        count = sum(by_reader.values()) if current else _tree_size(folder)[0]
        mark = "current" if current else "older schema, never read (prune removes it)"
        print(f"  {folder.name}  {len(companies):6} companies {count:8} filings  {_size(size):>9}  {mark}")
        if forms:
            print("    forms: " + ", ".join(f"{count} {form}" for form, count in sorted(forms.items())))
        for rid, count in sorted(by_reader.items(), key=lambda item: -item[1]):
            record = known.get(rid)
            label = f"{record.get('class')}: {record.get('note', '')}" if record else "not in readers.json"
            print(f"    reader {rid}: {count} filings ({label})")
    if not _schema_dirs(root):
        print("  (no dumps yet)")
    for kind in KINDS:
        current = version(kind)
        print(f"\n{kind}: current version {current} ({', '.join(f'{k} {v}' for k, v in code(kind).items())})")
        found = versions(root, kind)
        if not any(item["version"] == current for item in found):
            print("  (no entries yet for the current version)")
        for item in found:
            count, size = _tree_size(root / kind / item["version"])
            mark = "current" if item["version"] == current else ", ".join(changes(item["code"], code(kind)))
            print(f"  {item['version']}  {item['created'][:16] or '?':16}  {count:8} entries  {_size(size):>9}  {mark}")
    legacy = _legacy(root)
    if legacy:
        print(f"\nlegacy: {', '.join(sorted(p.name + '/' for p in legacy))} from earlier layouts "
              "(never read; prune removes them)")


def _remove(paths: list[Path], yes: bool, what: str = "") -> None:
    for path in paths:
        print(f"{'deleting' if yes else 'would delete'} {what}{path}")
        if yes:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
    if not paths:
        print("nothing to delete")
    elif not yes:
        print(f"dry run ({len(paths)} items): add --yes to delete")


def _classify(cls: str, note: str) -> int:
    data = readers()
    rid = reader_id()
    if any(item.get("id") == rid for item in data["readers"]):
        print(f"reader {rid} is already in {READERS_FILE}")
        return 0
    data["readers"].append({"id": rid, **reader(), "schema": schema(), "class": cls,
                            "date": _now()[:10], "note": note})
    (HERE / READERS_FILE).write_text(json.dumps(data, indent=2) + "\n")
    print(f"recorded reader {rid} as {cls} in {READERS_FILE}")
    if cls == "bug":
        print("now remove the entries the bug affected, e.g. "
              "`python filing_cache.py invalidate --reader <old id> --yes`")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect, invalidate and prune the filing cache.")
    parser.add_argument("--root", type=Path, default=default_root(), help="Cache root (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Schemas, readers, forms, sizes and old folders")
    invalidate = commands.add_parser("invalidate", help="Delete the dumps matching every filter given")
    invalidate.add_argument("--reader", help="Reader id that wrote them (see status)")
    invalidate.add_argument("--form", help="10-K, 10-KT, 20-F, 40-F or 10-Q")
    invalidate.add_argument("--before", help="Filed before this date (YYYY-MM-DD)")
    invalidate.add_argument("--cik", help="One company")
    invalidate.add_argument("--accession", help="One filing")
    invalidate.add_argument("--yes", action="store_true", help="Really delete (default: dry run)")
    prune = commands.add_parser("prune", help="Delete old schemas, old Form 4 versions and old layouts")
    prune.add_argument("--keep", type=int, default=1, help="Form 4 versions to keep, current included")
    prune.add_argument("--yes", action="store_true", help="Really delete (default: dry run)")
    classify = commands.add_parser("classify", help=f"Record the current reader in {READERS_FILE}")
    classify.add_argument("cls", choices=CLASSES)
    classify.add_argument("note", help="What changed and why it is this class")
    args = parser.parse_args(argv)
    root = args.root

    if args.command == "status":
        _status(root)
        return 0
    if args.command == "classify":
        return _classify(args.cls, args.note)
    if args.command == "invalidate":
        if not any((args.reader, args.form, args.before, args.cik, args.accession)):
            parser.error("give at least one filter: invalidating everything is a schema change")
        found = select(root, reader=args.reader, form=args.form, before=args.before,
                       cik=args.cik, accession=args.accession)
        _remove([path for _, _, path, _ in found], args.yes)
        if found and args.yes:
            print("the next run of those companies reads these filings again (SEC requests)")
        return 0
    doomed = [folder for folder in _schema_dirs(root) if folder.name != f"schema-{schema()}"]
    for kind in KINDS:
        current = version(kind)
        others = [item["version"] for item in versions(root, kind) if item["version"] != current]
        doomed += [root / kind / ver for ver in others[max(0, args.keep - 1):]]
    _remove(doomed + _legacy(root), args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
