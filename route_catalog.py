"""Discover and publish shared route results on the local filesystem."""

import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
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
        title = Path(str(source)).stem.replace("_", "-")
    else:
        name = variants[0].get("name") or "Map"
        title = f"{name} ({prefix})"
    return {
        "id": route_id,
        "pdf_hash_prefix": prefix,
        "pdf_hash": meta.get("pdf_hash"),
        "source_name": source,
        "title": title,
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
def map_lock(cache_root: Path, pdf_hash: str):
    """Serialize calculations for a PDF across threads and processes."""
    if not _PDF_HASH.fullmatch(pdf_hash):
        raise ValueError("invalid PDF hash")
    lock_dir = cache_root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{pdf_hash}.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
