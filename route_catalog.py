"""Discover and publish shared route results on the local filesystem."""

import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


_ROUTE_ID = re.compile(r"[0-9a-f]{20}_[0-9a-f]{20}(?:_[0-9a-f]{12})?\Z")
_PDF_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_file(name):
    return (isinstance(name, str) and name not in {"", ".", ".."}
            and Path(name).name == name and "/" not in name
            and "\\" not in name)


def _job_names(jobs_root):
    names = {}
    for path in jobs_root.glob("*/meta.json"):
        meta = _read_json(path)
        if not isinstance(meta, dict):
            continue
        pdf_hash = meta.get("pdf_hash")
        source = meta.get("source_name") or meta.get("pdf_name")
        if isinstance(pdf_hash, str) and _PDF_HASH.fullmatch(pdf_hash) and source:
            names.setdefault(pdf_hash[:20], str(source))
    return names


def _source_details(source):
    parts = [part for part in re.split(r"[-_\s]+", Path(str(source)).stem)
             if part]
    if parts and parts[0].casefold() in {
            "hofflohmaerkte", "hofflohmärkte", "hofflohmarkt"}:
        parts.pop(0)
    updated = bool(parts and parts[-1].casefold() in {"neu", "new", "updated"})
    if updated:
        parts.pop()
    map_date = None
    for index, part in enumerate(parts):
        if not re.fullmatch(r"\d{6}|\d{8}", part):
            continue
        try:
            parsed = datetime.strptime(part, "%d%m%y" if len(part) == 6
                                       else "%d%m%Y")
        except ValueError:
            continue
        map_date = parsed.date().isoformat()
        parts.pop(index)
        break
    place = []
    for part in parts:
        direction = re.fullmatch(r"(.{5,}?)(nord|sued|süd|west|ost)",
                                 part, re.IGNORECASE)
        place.extend(direction.groups() if direction else [part])
    return " ".join(word.capitalize() for word in place) or "Map", map_date, updated


def _route(cache_root, route_id, job_names):
    if not _ROUTE_ID.fullmatch(route_id):
        return None
    path = cache_root / route_id
    if not path.is_dir():
        return None
    summary = _read_json(path / "raw_summary.json")
    if not isinstance(summary, dict):
        return None
    files = summary.get("files")
    variants = summary.get("variants")
    if (not isinstance(files, list) or "original.png" not in files or
            not isinstance(variants, list) or not variants):
        return None
    if any(not _safe_file(name) or not (path / name).is_file() for name in files):
        return None
    for variant in variants:
        if not isinstance(variant, dict):
            return None
        for key in ("pdf", "png"):
            name = variant.get(key)
            if not _safe_file(name) or name not in files:
                return None
    meta = _read_json(path / "metadata.json")
    if len(route_id) > 41:
        if not isinstance(meta, dict):
            return None
        full_hash = meta.get("pdf_hash")
        if not isinstance(full_hash, str) or not _PDF_HASH.fullmatch(full_hash):
            return None
        if not route_id.startswith(full_hash[:20] + "_"):
            return None
    elif not isinstance(meta, dict):
        meta = {}
    prefix = route_id[:20]
    source = meta.get("source_name") or job_names.get(prefix)
    if source:
        title, map_date, updated_map = _source_details(source)
    else:
        name = variants[0].get("name") or "Map"
        title = re.sub(r"^Hofflohmaerkte\s+", "", str(name),
                       flags=re.IGNORECASE).strip()
        loop = re.fullmatch(r"loop from\s+(.+)", title, re.IGNORECASE)
        if loop:
            title = loop.group(1)
        elif re.fullmatch(r"circular tour \(dots only\)", title,
                          re.IGNORECASE):
            title = "Unnamed flyer"
        elif title:
            title = title[0].upper() + title[1:]
        map_date, updated_map = None, False
    return {
        "id": route_id,
        "pdf_hash_prefix": prefix,
        "pdf_hash": meta.get("pdf_hash"),
        "source_name": source,
        "title": title,
        "map_date": map_date,
        "updated_map": updated_map,
        "created_at": meta.get("created_at") or path.stat().st_mtime,
        "start": meta.get("start"),
        "end": meta.get("end"),
        "calibration_hash": meta.get("calibration_hash"),
        "supersedes": meta.get("supersedes"),
        "variants": [{"key": v.get("key"), "name": v.get("name")}
                     for v in variants],
        "summary": summary,
        "path": path,
    }


def list_routes(cache_root: Path, jobs_root: Path,
                pdf_hash: str | None = None) -> list[dict]:
    """List complete, currently visible route sets, newest first."""
    job_names = _job_names(jobs_root)
    routes = []
    for path in cache_root.iterdir():
        if not path.is_dir() or not _ROUTE_ID.fullmatch(path.name):
            continue
        if pdf_hash and not path.name.startswith(pdf_hash[:20] + "_"):
            continue
        route = _route(cache_root, path.name, job_names)
        if route:
            routes.append(route)
    superseded = {route["supersedes"] for route in routes if route["supersedes"]}
    routes = [route for route in routes if route["id"] not in superseded]
    return sorted(routes, key=lambda route: str(route["created_at"]), reverse=True)


def get_route(cache_root: Path, jobs_root: Path, route_id: str) -> dict | None:
    """Get a complete route, including one superseded after a page opened."""
    return _route(cache_root, route_id, _job_names(jobs_root))


def delete_map(cache_root: Path, pdf_hash_prefix: str) -> int:
    """Remove every cached generation for a map under its calculation lock."""
    if not re.fullmatch(r"[0-9a-f]{20}", pdf_hash_prefix):
        raise ValueError("invalid PDF hash prefix")
    with map_lock(cache_root, pdf_hash_prefix):
        paths = [path for path in cache_root.iterdir()
                 if path.is_dir() and not path.is_symlink()
                 and _ROUTE_ID.fullmatch(path.name)
                 and path.name.startswith(pdf_hash_prefix + "_")]
        for path in paths:
            shutil.rmtree(path)
        return len(paths)


def delete_all(cache_root: Path) -> int:
    """Remove every cached route after active calculations finish."""
    with _catalog_lock(cache_root, exclusive=True):
        paths = [path for path in cache_root.iterdir()
                 if path.is_dir() and not path.is_symlink()
                 and _ROUTE_ID.fullmatch(path.name)]
        for path in paths:
            shutil.rmtree(path)
        return len(paths)


def publish_route(cache_root: Path, cache_key: str, out_dir: Path,
                  summary: dict, metadata: dict,
                  supersedes: str | None = None) -> str:
    """Publish a complete immutable generation using an atomic directory rename."""
    if not re.fullmatch(r"[0-9a-f]{20}_[0-9a-f]{20}", cache_key):
        raise ValueError("invalid cache key")
    full_hash = metadata.get("pdf_hash")
    if not isinstance(full_hash, str) or not _PDF_HASH.fullmatch(full_hash):
        raise ValueError("invalid PDF hash")
    if cache_key[:20] != full_hash[:20]:
        raise ValueError("cache key does not match PDF hash")
    if supersedes and not _ROUTE_ID.fullmatch(supersedes):
        raise ValueError("invalid superseded route ID")
    files = summary.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("summary has no files")
    for name in files:
        if not _safe_file(name):
            raise ValueError("unsafe artifact name")
        if not (out_dir / name).is_file():
            raise FileNotFoundError(out_dir / name)

    route_id = f"{cache_key}_{uuid.uuid4().hex[:12]}"
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=cache_root))
    try:
        for name in files:
            shutil.copy2(out_dir / name, staging / name)
        (staging / "raw_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        saved_meta = dict(metadata)
        saved_meta["supersedes"] = supersedes
        (staging / "metadata.json").write_text(
            json.dumps(saved_meta, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, cache_root / route_id)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return route_id


@contextmanager
def _catalog_lock(cache_root: Path, exclusive: bool):
    lock_dir = cache_root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "catalog.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextmanager
def map_lock(cache_root: Path, pdf_hash: str):
    """Serialize calculations for a PDF across threads and processes."""
    if not re.fullmatch(r"[0-9a-f]{20}(?:[0-9a-f]{44})?", pdf_hash):
        raise ValueError("invalid PDF hash or prefix")
    with _catalog_lock(cache_root, exclusive=False):
        lock_dir = cache_root / ".locks"
        with (lock_dir / f"{pdf_hash[:20]}.lock").open("a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
