#!/usr/bin/env python3
"""
webapp.py - Browser UI for hoffroute.

Flow:
  POST /api/prepare   upload a PDF (or pass a URL) -> job id, rendered page
                      image, auto-detected station icons + dot candidates
  POST /api/solve     job id + calibration -> runs the pipeline, returns
                      per-variant stats and download links
  GET  /api/geocode   Nominatim proxy for the calibration form
  GET  /health        liveness probe

Run locally:  uvicorn webapp:app --reload
"""

import json
import base64
import hashlib
import hmac
import itertools
import logging
import math
import os
import re
import secrets
import shutil
import concurrent.futures
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from loguru import logger

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import hoffroute as hr


def load_dotenv(path=Path(".env")):
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_dotenv()

JOBS_DIR = Path(os.environ.get("HOFFROUTE_JOBS_DIR", "jobs")).resolve()
JOBS_DIR.mkdir(parents=True, exist_ok=True)
CALIB_CACHE_DIR = Path(os.environ.get(
    "HOFFROUTE_CALIB_CACHE_DIR", "calibration_cache")).resolve()
CALIB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
ROUTE_CACHE_DIR = Path(os.environ.get(
    "HOFFROUTE_ROUTE_CACHE_DIR", "route_cache")).resolve()
ROUTE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAX_PDF_BYTES = 50 * 1024 * 1024
RENDER_DPI = 300
AUTOCALIB_CONTEXT = os.environ.get(
    "HOFFROUTE_AUTOCALIB_CONTEXT", "Germany")
AUTOCALIB_MAX_CANDIDATES = int(os.environ.get(
    "HOFFROUTE_AUTOCALIB_MAX_CANDIDATES", "20"))
AUTOCALIB_MAX_RMS_M = float(os.environ.get(
    "HOFFROUTE_AUTOCALIB_MAX_RMS_M", "250"))
AUTOCALIB_INLIER_M = float(os.environ.get(
    "HOFFROUTE_AUTOCALIB_INLIER_M", "180"))
AUTOCALIB_TRANSIT_RADIUS_M = int(os.environ.get(
    "HOFFROUTE_AUTOCALIB_TRANSIT_RADIUS_M", "500"))
AUTOCALIB_TRANSIT_TIMEOUT_S = int(os.environ.get(
    "HOFFROUTE_AUTOCALIB_TRANSIT_TIMEOUT_S", "20"))
OVERPASS_URL = os.environ.get(
    "HOFFROUTE_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
TESSERACT_CMD = os.environ.get("HOFFROUTE_TESSERACT_CMD") or shutil.which(
    "tesseract")
TESSERACT_LANG = os.environ.get("HOFFROUTE_TESSERACT_LANG", "deu+eng")
_PREPARE_WORKERS = int(os.environ.get("HOFFROUTE_PREPARE_WORKERS", "2"))
AUTH_COOKIE = "hoffroute_auth"
AUTH_TTL_SECONDS = int(os.environ.get("HOFFROUTE_AUTH_TTL_SECONDS", "43200"))
AUTH_COOKIE_SECURE = os.environ.get(
    "HOFFROUTE_AUTH_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
AUTH_SECRET = os.environ.get("HOFFROUTE_AUTH_SECRET") or secrets.token_urlsafe(32)

# ---------- logging (loguru) ----------
_LOG_LEVEL = os.environ.get("HOFFROUTE_LOG_LEVEL", "INFO").upper()
_LOG_FILE = os.environ.get("HOFFROUTE_LOG_FILE", "logs/hoffroute.log")
_LOG_MAX_BYTES = int(os.environ.get("HOFFROUTE_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(os.environ.get("HOFFROUTE_LOG_BACKUP_COUNT", "5"))

_FMT = "{time:YYYY-MM-DD HH:mm:ss,SSS} {level} [{name}] {message}"

# Remove loguru's default stderr sink; replace with stdout.
logger.remove()
logger.add(sys.stdout, level=_LOG_LEVEL, format=_FMT, colorize=True)

if _LOG_FILE:
    _log_path = Path(_LOG_FILE)
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        _log_path,
        level=_LOG_LEVEL,
        format=_FMT,
        rotation=_LOG_MAX_BYTES,
        retention=_LOG_BACKUP_COUNT,
        compression="gz",
        encoding="utf-8",
    )
    logger.info("file logging enabled path={} max_bytes={} backups={}",
                _log_path.resolve(), _LOG_MAX_BYTES, _LOG_BACKUP_COUNT)


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging (uvicorn, fastapi, …) into loguru."""
    def emit(self, record: logging.LogRecord):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage())


logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


class _Logger:
    """Thin shim: existing LOGGER.info("msg %s", val) calls work unchanged.
    Converts %-style format strings to plain strings before passing to loguru,
    so no call sites need updating."""

    @staticmethod
    def _fmt(msg, args):
        try:
            return msg % args if args else str(msg)
        except Exception:
            return str(msg)

    def info(self, msg, *args):
        logger.opt(depth=1).info(self._fmt(msg, args))

    def warning(self, msg, *args):
        logger.opt(depth=1).warning(self._fmt(msg, args))

    def debug(self, msg, *args):
        logger.opt(depth=1).debug(self._fmt(msg, args))

    def exception(self, msg, *args):
        logger.opt(depth=1, exception=True).error(self._fmt(msg, args))


LOGGER = _Logger()

app = FastAPI(title="hoffroute")
_prepare_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_PREPARE_WORKERS, thread_name_prefix="prepare")


def parse_manual_users():
    raw = os.environ.get("HOFFROUTE_MANUAL_USERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        pass

    users = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        user, password = item.split(":", 1)
        user = user.strip()
        password = password.strip()
        if user and password:
            users[user] = password
    return users


MANUAL_USERS = parse_manual_users()
if MANUAL_USERS:
    LOGGER.info("manual calibration auth enabled users=%d", len(MANUAL_USERS))
else:
    LOGGER.info("manual calibration auth disabled: no HOFFROUTE_MANUAL_USERS")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_auth_payload(payload: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), payload.encode(),
                   hashlib.sha256).digest()
    return b64url(sig)


def make_auth_token(username: str) -> str:
    expires = str(int(time.time()) + AUTH_TTL_SECONDS)
    payload = b64url(json.dumps(
        {"u": username, "e": expires},
        separators=(",", ":")).encode())
    return f"{payload}.{sign_auth_payload(payload)}"


def auth_user_from_request(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sign_auth_payload(payload), sig):
        return None
    try:
        data = json.loads(b64url_decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    try:
        expires = int(data.get("e", "0"))
    except (TypeError, ValueError):
        return None
    if expires < time.time():
        return None
    user = data.get("u")
    if user not in MANUAL_USERS:
        return None
    return user


def set_auth_cookie(response: Response, username: str):
    response.set_cookie(
        AUTH_COOKIE,
        make_auth_token(username),
        max_age=AUTH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=AUTH_COOKIE_SECURE,
    )


def clear_auth_cookie(response: Response):
    response.delete_cookie(AUTH_COOKIE)


def job_dir(job_id: str) -> Path:
    if not all(c in "0123456789abcdef-" for c in job_id):
        raise HTTPException(400, "bad job id")
    d = JOBS_DIR / job_id
    if not d.is_dir():
        raise HTTPException(404, "unknown job")
    return d


def pdf_sha256(pdf: Path) -> str:
    h = hashlib.sha256()
    with pdf.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_path(pdf_hash: str) -> Path:
    return CALIB_CACHE_DIR / f"{pdf_hash}.json"


def calib_content_hash(calib) -> str:
    if not calib:
        return "nocal"
    stable = json.dumps(calib, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"))
    return hashlib.sha256(stable.encode()).hexdigest()[:20]


def route_cache_dir(pdf_hash: str | None, calib) -> Path | None:
    if not pdf_hash:
        return None
    return ROUTE_CACHE_DIR / f"{pdf_hash[:20]}_{calib_content_hash(calib)}"


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def validate_calib(calib, strict=True):
    if calib is None:
        return None
    if not isinstance(calib, dict):
        if strict:
            raise HTTPException(400, "calibration must be a JSON object")
        return None
    cps = calib.get("control_points", [])
    if len(cps) < 3:
        return None
    for cp in cps:
        if cp.get("lat") is None or cp.get("lon") is None:
            if not strict:
                return None
            raise HTTPException(
                400, f"control point '{cp.get('name')}' has no lat/lon")
    return calib


def load_cached_calib(pdf_hash: str | None):
    if not pdf_hash:
        return None
    path = cache_path(pdf_hash)
    calib = validate_calib(read_json(path), strict=False)
    if calib is None:
        LOGGER.info("calibration cache miss hash=%s path=%s", pdf_hash, path)
    else:
        LOGGER.info(
            "calibration cache hit hash=%s path=%s control_points=%d stations=%d",
            pdf_hash, path, len(calib.get("control_points", [])),
            len(calib.get("stations", [])))
    return calib


def extract_embedded_text_lines(pdf: Path, dpi: int):
    scale = dpi / 72.0
    page = hr.fitz.open(pdf)[0]
    rows = {}
    for word in page.get_text("words"):
        x0, y0, x1, y1, text, block_no, line_no, word_no = word
        key = (block_no, line_no)
        rows.setdefault(key, []).append((word_no, x0, y0, x1, y1, text))

    lines = []
    for words in rows.values():
        words.sort(key=lambda w: w[0])
        text = " ".join(w[5] for w in words)
        text = re.sub(r"\s+", " ", text).strip(" -.,")
        if len(text) < 4:
            continue
        x0 = min(w[1] for w in words) * scale
        y0 = min(w[2] for w in words) * scale
        x1 = max(w[3] for w in words) * scale
        y1 = max(w[4] for w in words) * scale
        lines.append({
            "text": text,
            "px": round((x0 + x1) / 2, 1),
            "py": round((y0 + y1) / 2, 1),
        })
    return lines


def clean_ocr_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" -.,;:|")


def run_tesseract_tsv(image_path: Path, lang: str, psm: str = "11"):
    cmd = [
        TESSERACT_CMD,
        "stdin",
        "stdout",
        "-l", lang,
        "--psm", psm,
        "tsv",
    ]
    with image_path.open("rb") as img:
        return subprocess.run(
            cmd, input=img.read(), check=True, capture_output=True,
            timeout=45)


def _parse_tsv_lines(stdout: str) -> list:
    rows = {}
    for raw in stdout.splitlines()[1:]:
        parts = raw.split("\t")
        if len(parts) < 12:
            continue
        try:
            level = int(parts[0])
            conf = float(parts[10])
            left, top = int(parts[6]), int(parts[7])
            width, height = int(parts[8]), int(parts[9])
        except ValueError:
            continue
        text = clean_ocr_text(parts[11])
        if level != 5 or conf < 35 or not text:
            continue
        # namespace key by PSM prefix to avoid merging blocks from different runs
        key = tuple(parts[1:5])
        rows.setdefault(key, []).append((left, top, width, height, text))

    lines = []
    for words in rows.values():
        words.sort(key=lambda w: w[0])
        text = clean_ocr_text(" ".join(w[4] for w in words))
        if len(text) < 4:
            continue
        x0 = min(w[0] for w in words)
        y0 = min(w[1] for w in words)
        x1 = max(w[0] + w[2] for w in words)
        y1 = max(w[1] + w[3] for w in words)
        lines.append({
            "text": text,
            "px": round((x0 + x1) / 2, 1),
            "py": round((y0 + y1) / 2, 1),
            "source": "ocr",
        })
    return lines


def extract_ocr_text_lines(image_path: Path):
    if not TESSERACT_CMD:
        LOGGER.info("auto-calibration OCR skipped: tesseract not found")
        return []
    LOGGER.info(
        "auto-calibration OCR starting image=%s cmd=%s lang=%s",
        image_path, TESSERACT_CMD, TESSERACT_LANG)
    langs = [TESSERACT_LANG]
    if TESSERACT_LANG != "eng":
        langs.append("eng")

    best_proc = None
    for lang in langs:
        try:
            best_proc = run_tesseract_tsv(image_path, lang, psm="11")
            break
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace").strip()
            LOGGER.warning(
                "auto-calibration OCR failed lang=%s psm=11 error=%s",
                lang, stderr or e)
        except subprocess.TimeoutExpired:
            LOGGER.warning("auto-calibration OCR timed out (psm=11)")
        except Exception as e:
            LOGGER.warning("auto-calibration OCR failed psm=11 error=%s", e)

    if best_proc is None:
        return []

    lines = _parse_tsv_lines(best_proc.stdout.decode("utf-8", errors="replace"))

    # Second pass with PSM 6 (uniform block) to catch structured text that
    # PSM 11 (sparse) misses — e.g. map labels in regular grid areas.
    try:
        lang6 = langs[0]
        proc6 = run_tesseract_tsv(image_path, lang6, psm="6")
        extra = _parse_tsv_lines(proc6.stdout.decode("utf-8", errors="replace"))
        seen = {normalized_label(ln["text"]) for ln in lines}
        for ln in extra:
            if normalized_label(ln["text"]) not in seen:
                lines.append(ln)
                seen.add(normalized_label(ln["text"]))
        LOGGER.info("auto-calibration OCR psm=6 added %d extra lines", len(extra))
    except Exception as e:
        LOGGER.debug("auto-calibration OCR psm=6 skipped: %s", e)

    LOGGER.info("auto-calibration OCR extracted_text_lines=%d", len(lines))
    return lines


def extract_text_lines(pdf: Path, dpi: int, image_path: Path):
    lines = extract_embedded_text_lines(pdf, dpi)
    if lines:
        for line in lines:
            line["source"] = "embedded"
        return lines
    LOGGER.info("auto-calibration embedded text empty; trying OCR")
    return extract_ocr_text_lines(image_path)


def auto_bbox(dots, width, height):
    if not dots:
        return [0, 0, width, height]
    xs = [p[0] for p in dots]
    ys = [p[1] for p in dots]
    pad = 120
    return [
        max(0, int(min(xs) - pad)),
        max(0, int(min(ys) - pad)),
        min(width, int(max(xs) + pad)),
        min(height, int(max(ys) + pad)),
    ]


def normalized_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("\u00df", "ss")).strip()


def clean_map_label(text: str) -> str:
    text = clean_ocr_text(text)
    text = re.sub(r"\b([A-Za-zÄÖÜäöüß]{2,})\s+tener\s+Str\b",
                  r"\1tener Str", text)
    text = re.sub(r"\b([A-Za-zÄÖÜäöüß]{4,})st\b", r"\1str", text)
    text = re.sub(r"\b([A-Za-zÄÖÜäöüß]{4,})str\b", r"\1str", text)
    text = re.sub(r"\s+[0-9@]+$", "", text)
    text = re.sub(r"\s+[^\wÄÖÜäöüß]+$", "", text)
    text = re.sub(r"\bAmN\s+ordp\s+a\b", "Am Nordpark", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bHohenzollernr\b", "Hohenzollernring", text,
                  flags=re.IGNORECASE)
    return clean_ocr_text(text)


def context_from_filename(pdf: Path):
    stem = normalized_label(pdf.stem)
    parts = [
        p for p in stem.split()
        if p not in {"hofflohmaerkte", "hofflohmärkte", "hoff", "floh", "maerkte"}
        and not p.isdigit()
        and len(p) > 2
    ]
    if not parts:
        return None
    return f"{parts[0].title()}, Germany"


def context_from_lines(lines):
    joined = " ".join(line["text"] for line in lines[:30])
    match = re.search(
        r"hoff\s*floh\s*m[aä]rkte\s+([A-Za-zÄÖÜäöüß-]{3,})",
        joined, re.IGNORECASE)
    if match:
        return f"{match.group(1)}, Germany"
    for line in sorted(lines, key=lambda ln: ln["py"])[:8]:
        text = clean_map_label(line["text"])
        match = re.match(r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]{2,})\b", text)
        if match and normalized_label(match.group(1)) not in {
            "www", "initiative", "weitere"}:
            return f"{match.group(1)}, Germany"
    return None


def detect_autocalib_context(pdf: Path, lines):
    context = context_from_lines(lines) or context_from_filename(pdf) or AUTOCALIB_CONTEXT
    LOGGER.info("auto-calibration context=%r", context)
    return context


def looks_like_map_label(text: str) -> bool:
    label = normalized_label(clean_map_label(text))
    if len(label) < 4 or len(label) > 50:
        return False
    if label.startswith(("an der ", "am ", "auf dem ", "im ", "in der ",
                          "zur ", "zum ", "beim ")):
        return True
    suffixes = (
        "strasse", "str",
        "platz", "pl",
        "weg", "w",
        "gasse",
        "ring",
        "allee",
        "ufer",
        "bruecke", "brucke",
        "markt",
        "bahnhof",
        "tor",
        "park",
        "flora",
        "berg",
        "graben",
        "garten",
        "feld",
        "stieg",
        "steig",
        "grund",
        "hoehe", "hohe",
    )
    parts = label.split()
    return any(part.endswith(suffixes) for part in parts)


def extract_street_tokens(text: str) -> str:
    """Extract the likely street-name tokens from a garbled OCR line.
    Finds the last word group (1-2 words) ending with a known street suffix,
    so 'pe oder Garten POTTENDORFER STR' → 'POTTENDORFER STR'."""
    cleaned = clean_map_label(text)
    words = cleaned.split()
    suffixes = (
        "strasse", "str", "platz", "pl", "weg", "w", "gasse", "ring",
        "allee", "ufer", "bruecke", "brucke", "markt", "bahnhof", "tor",
        "park", "flora", "berg", "graben", "garten", "feld", "stieg",
        "steig", "grund", "hoehe", "hohe",
    )
    for i in range(len(words) - 1, -1, -1):
        norm = normalized_label(words[i])
        if any(norm.endswith(s) for s in suffixes):
            # include 1 preceding word as a street modifier, but skip numbers
            if i > 0 and not re.match(r"^\d+$", words[i - 1]):
                start = i - 1
            else:
                start = i
            candidate = " ".join(words[start:i + 1])
            if len(normalized_label(candidate)) >= 4:
                return candidate
    return cleaned


def map_label_candidates(lines, icons):
    items = []
    for line in lines:
        if looks_like_map_label(line["text"]):
            items.append({
                "name": extract_street_tokens(line["text"]),
                "px": line["px"],
                "py": line["py"],
            })

    seen = set()
    unique = []
    for item in items:
        key = normalized_label(item["name"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:AUTOCALIB_MAX_CANDIDATES]


def geocode_one(label: str, context: str):
    query = f"{label}, {context}"
    LOGGER.info("auto-calibration geocoding label=%r query=%r", label, query)
    qs = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{qs}",
        headers={"User-Agent": "hoffroute/1.0 (auto calibration)"})
    with urllib.request.urlopen(req, timeout=5, context=hr.SSL_CTX) as r:
        results = json.loads(r.read())
    if not results:
        LOGGER.info("auto-calibration geocode miss label=%r", label)
        return None
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    LOGGER.info(
        "auto-calibration geocode hit label=%r lat=%.7f lon=%.7f",
        label, lat, lon)
    return lat, lon


def residual_m(px2ll, point, lon_scale):
    lat, lon = px2ll(point["px"], point["py"])
    dlat = (lat - point["lat"]) * 111320
    dlon = (lon - point["lon"]) * lon_scale
    return math.sqrt(dlat * dlat + dlon * dlon)


def distance_m(a_lat, a_lon, b_lat, b_lon):
    lat1, lon1, lat2, lon2 = map(math.radians, (a_lat, a_lon, b_lat, b_lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    x = math.sin(dlat / 2) ** 2
    y = math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * hr.EARTH_R * math.asin(math.sqrt(x + y))


def fit_auto_points(points):
    if len(points) < 3:
        LOGGER.info(
            "auto-calibration rejected: only %d geocoded point(s)",
            len(points))
        return None
    lon_scale = 111320 * math.cos(math.radians(
        sum(p["lat"] for p in points) / len(points)))
    best = None
    for subset in itertools.combinations(points, 3):
        try:
            px2ll, _ = hr.fit_affine(list(subset))
        except Exception:
            continue
        inliers = [p for p in points
                   if residual_m(px2ll, p, lon_scale) <= AUTOCALIB_INLIER_M]
        if len(inliers) < 3:
            continue
        px2ll2, rms = hr.fit_affine(inliers)
        score = (len(inliers), -rms)
        if rms <= AUTOCALIB_MAX_RMS_M and (best is None or score > best[0]):
            best = (score, inliers, rms, px2ll2)
    if best is None:
        LOGGER.info(
            "auto-calibration rejected: no affine fit within %.0f m rms "
            "(geocoded_points=%d inlier_threshold=%.0f m)",
            AUTOCALIB_MAX_RMS_M, len(points), AUTOCALIB_INLIER_M)
        return None
    LOGGER.info(
        "auto-calibration accepted fit control_points=%d rms=%.1f m",
        len(best[1]), -best[0][1])
    return best[1]


def overpass_transit_candidates(lat, lon):
    query = f"""
[out:json][timeout:8];
(
  node(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["railway"~"station|halt|tram_stop|subway_entrance"];
  way(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["railway"~"station|halt|tram_stop|subway_entrance"];
  relation(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["railway"~"station|halt|tram_stop|subway_entrance"];
  node(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["public_transport"~"station|stop_position|platform"];
  way(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["public_transport"~"station|stop_position|platform"];
  relation(around:{AUTOCALIB_TRANSIT_RADIUS_M},{lat:.7f},{lon:.7f})["name"]["public_transport"~"station|stop_position|platform"];
);
out center body 50;
"""
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"User-Agent": "hoffroute/1.0 (auto calibration)"})
    with urllib.request.urlopen(req, timeout=12, context=hr.SSL_CTX) as r:
        payload = json.loads(r.read())
    return payload.get("elements", [])


def kind_matches_transit(kind, tags):
    route = " ".join(str(tags.get(k, "")) for k in (
        "station", "railway", "subway", "train", "tram", "light_rail",
        "network", "operator", "operator:short"))
    route = route.lower()
    if kind.startswith("U-Bahn"):
        return any(term in route for term in (
            "subway", "u-bahn", "ubahn", "stadtbahn", "light_rail",
            "light rail", "tram", "kvb"))
    if kind.startswith("S-Bahn"):
        return any(term in route for term in ("s-bahn", "sbahn", "train", "rail"))
    return True


def transit_stations_from_icons(icons, control_points):
    if not icons or len(control_points) < 3:
        return []
    px2ll, _ = hr.fit_affine(control_points)
    stations = []
    seen = set()
    deadline = time.monotonic() + AUTOCALIB_TRANSIT_TIMEOUT_S
    for kind, px, py in icons:
        if time.monotonic() >= deadline:
            LOGGER.warning(
                "auto-calibration transit lookup stopped: budget of %ds exceeded",
                AUTOCALIB_TRANSIT_TIMEOUT_S)
            break
        lat, lon = px2ll(px, py)
        LOGGER.info(
            "auto-calibration transit lookup kind=%r px=(%.1f,%.1f) approx=(%.7f,%.7f) radius_m=%d",
            kind, px, py, lat, lon, AUTOCALIB_TRANSIT_RADIUS_M)
        try:
            candidates = overpass_transit_candidates(lat, lon)
        except Exception as e:
            LOGGER.warning(
                "auto-calibration transit lookup failed kind=%r error=%s",
                kind, e)
            continue
        named = []
        for el in candidates:
            tags = el.get("tags", {})
            name = tags.get("name")
            center = el.get("center", {})
            el_lat = el.get("lat", center.get("lat"))
            el_lon = el.get("lon", center.get("lon"))
            if not name or el_lat is None or el_lon is None:
                continue
            if not kind_matches_transit(kind, tags):
                continue
            named.append((distance_m(lat, lon, el_lat, el_lon), name, el_lat, el_lon))
        if not named:
            LOGGER.info(
                "auto-calibration transit lookup found no named match kind=%r",
                kind)
            continue
        dist, name, st_lat, st_lon = min(named, key=lambda item: item[0])
        key = normalized_label(f"{kind} {name}")
        if key in seen:
            continue
        seen.add(key)
        station_name = f"{name} ({'U' if kind.startswith('U-Bahn') else 'S'})"
        stations.append({
            "name": station_name,
            "px": round(px, 1),
            "py": round(py, 1),
            "lat": float(st_lat),
            "lon": float(st_lon),
        })
        LOGGER.info(
            "auto-calibration transit match kind=%r name=%r distance_m=%.0f",
            kind, station_name, dist)
    return stations


def auto_calibrate(pdf: Path, dpi: int, image_path: Path, icons, dots,
                   width: int, height: int):
    LOGGER.info(
        "auto-calibration starting pdf=%s dpi=%d default_context=%r",
        pdf, dpi, AUTOCALIB_CONTEXT)
    lines = extract_text_lines(pdf, dpi, image_path)
    LOGGER.info("auto-calibration extracted_text_lines=%d", len(lines))
    context = detect_autocalib_context(pdf, lines)
    candidates = map_label_candidates(lines, icons)
    LOGGER.info(
        "auto-calibration label_candidates=%d max_candidates=%d",
        len(candidates), AUTOCALIB_MAX_CANDIDATES)
    points = []
    for cand in candidates:
        try:
            ll = geocode_one(cand["name"], context)
        except Exception as e:
            LOGGER.warning(
                "auto-calibration geocode failed label=%r error=%s",
                cand["name"], e)
            continue
        if not ll:
            continue
        points.append({
            "name": cand["name"],
            "px": cand["px"],
            "py": cand["py"],
            "lat": ll[0],
            "lon": ll[1],
        })

    inliers = fit_auto_points(points)
    if not inliers:
        return None
    control_points = [
        {k: p[k] for k in ("name", "px", "py", "lat", "lon")}
        for p in inliers
    ]
    stations = transit_stations_from_icons(icons, control_points)
    calib = {
        "comment": "Auto-generated from embedded PDF text and Nominatim. "
                   "Review before relying on exact distances.",
        "map_bbox_px": auto_bbox(dots, width, height),
        "control_points": control_points,
        "stations": stations,
    }
    LOGGER.info(
        "auto-calibration created control_points=%d stations=%d bbox=%s",
        len(control_points), len(stations), calib["map_bbox_px"])
    return calib


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = auth_user_from_request(request)
    return {
        "authenticated": user is not None,
        "user": user,
        "manual_enabled": bool(MANUAL_USERS),
    }


@app.post("/api/auth/login")
def auth_login(payload: dict):
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    expected = MANUAL_USERS.get(username)
    if not expected or not hmac.compare_digest(expected, password):
        LOGGER.warning("manual calibration login failed user=%r", username)
        raise HTTPException(401, "invalid username or password")
    response = JSONResponse({
        "authenticated": True,
        "user": username,
        "manual_enabled": bool(MANUAL_USERS),
    })
    set_auth_cookie(response, username)
    LOGGER.info("manual calibration login succeeded user=%r", username)
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    user = auth_user_from_request(request)
    response = JSONResponse({"authenticated": False})
    clear_auth_cookie(response)
    LOGGER.info("manual calibration logout user=%r", user)
    return response


@app.post("/api/calibration-cache/delete")
def delete_calibration_cache(payload: dict, request: Request):
    user = auth_user_from_request(request)
    if user is None:
        raise HTTPException(403, "login required for manual calibration")
    d = job_dir(payload.get("job_id", ""))
    meta = read_json(d / "meta.json") or {}
    pdf_hash = meta.get("pdf_hash")
    deleted_cache = False
    deleted_job_calib = False
    if pdf_hash:
        path = cache_path(pdf_hash)
        if path.is_file():
            path.unlink()
            deleted_cache = True
    job_calib = d / "calib.json"
    if job_calib.is_file():
        job_calib.unlink()
        deleted_job_calib = True
    LOGGER.info(
        "calibration cache deleted job_id=%s user=%r hash=%s deleted_cache=%s deleted_job_calib=%s",
        d.name, user, pdf_hash, deleted_cache, deleted_job_calib)
    return {
        "deleted_cache": deleted_cache,
        "deleted_job_calib": deleted_job_calib,
    }


def _run_prepare(jid: str, pdf_path: Path | None, url: str | None):
    d = JOBS_DIR / jid
    result_path = d / "result.json"

    def _fail(detail: str):
        try:
            result_path.write_text(json.dumps({"status": "error", "detail": detail}))
        except Exception:
            pass

    try:
        if url:
            try:
                pdf = hr.fetch_pdf(url, d)
            except Exception as e:
                _fail(f"could not download PDF: {e}")
                return
            LOGGER.info("prepare downloaded pdf job_id=%s url=%s path=%s", jid, url, pdf)
        else:
            pdf = pdf_path

        doc, img = hr.render_page(pdf, RENDER_DPI)
        doc[0].get_pixmap(dpi=RENDER_DPI).save(d / "page.png")
        h, w = img.shape[:2]
        icons = hr.detect_station_icons(img)
        dots = hr.detect_dots(img, (0, 0, w, h))
        pdf_hash = pdf_sha256(pdf)
        LOGGER.info(
            "prepare rendered job_id=%s pdf_hash=%s size=%dx%d icons=%d dots=%d",
            jid, pdf_hash, w, h, len(icons), len(dots))
        (d / "meta.json").write_text(json.dumps({
            "pdf_hash": pdf_hash,
            "pdf_name": pdf.name,
        }, indent=2))
        cached_calib = load_cached_calib(pdf_hash)
        auto_calib = None
        if cached_calib is None:
            LOGGER.info("prepare trying auto-calibration job_id=%s", jid)
            try:
                auto_calib = auto_calibrate(
                    pdf, RENDER_DPI, d / "page.png", icons, dots, w, h)
            except Exception as e:
                LOGGER.exception(
                    "prepare auto-calibration failed job_id=%s error=%s", jid, e)
            if auto_calib is not None:
                (d / "calib.json").write_text(json.dumps(
                    auto_calib, indent=2, ensure_ascii=False))
                LOGGER.info(
                    "prepare saved auto-calibration job_id=%s control_points=%d stations=%d",
                    jid, len(auto_calib.get("control_points", [])),
                    len(auto_calib.get("stations", [])))
            else:
                LOGGER.info("prepare auto-calibration unavailable job_id=%s", jid)
        else:
            LOGGER.info("prepare using cached calibration job_id=%s", jid)

        LOGGER.info(
            "prepare complete job_id=%s cached_calib=%s auto_calib=%s",
            jid, cached_calib is not None, auto_calib is not None)
        result_path.write_text(json.dumps({
            "status": "ready",
            "job_id": jid,
            "image": f"/jobs/{jid}/page.png",
            "pdf": f"/jobs/{jid}/{pdf.name}",
            "width": w, "height": h, "dpi": RENDER_DPI,
            "cached_calib": cached_calib,
            "auto_calib": auto_calib,
            "icons": [{"kind": k, "px": x, "py": y} for k, x, y in icons],
            "dots": [{"px": x, "py": y} for x, y in dots],
        }, ensure_ascii=False))
    except Exception as e:
        LOGGER.exception("prepare failed job_id=%s error=%s", jid, e)
        _fail(f"failed to process PDF: {e}")


@app.post("/api/prepare")
def prepare(file: UploadFile | None = None, url: str = Form(None)):
    if not file and not url:
        raise HTTPException(400, "provide a PDF file or a URL")
    jid = str(uuid.uuid4())
    d = JOBS_DIR / jid
    d.mkdir(parents=True)
    LOGGER.info("prepare start job_id=%s source=%s", jid, "upload" if file else "url")

    pdf_path = None
    if file:
        data = file.file.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(413, "PDF larger than 50 MB")
        if not data.startswith(b"%PDF"):
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, "not a PDF file")
        pdf_path = d / "map.pdf"
        pdf_path.write_bytes(data)
        LOGGER.info(
            "prepare stored uploaded pdf job_id=%s filename=%r bytes=%d",
            jid, file.filename, len(data))
        url = None
    else:
        if not url.lower().startswith(("http://", "https://")):
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, "URL must be http(s)")

    _prepare_executor.submit(_run_prepare, jid, pdf_path, url)
    return {"job_id": jid, "status": "processing"}


@app.get("/api/prepare/{job_id}/status")
def prepare_status(job_id: str):
    d = job_dir(job_id)
    result = read_json(d / "result.json")
    if result is None:
        return {"status": "processing"}
    return result


@app.post("/api/solve")
def solve(payload: dict, request: Request):
    d = job_dir(payload.get("job_id", ""))
    meta = read_json(d / "meta.json") or {}
    pdf_hash = meta.get("pdf_hash")
    auth_user = auth_user_from_request(request)
    submitted_calib = payload.get("calib") or None
    if submitted_calib is not None and auth_user is None:
        LOGGER.warning(
            "solve rejected unauthorized manual calibration job_id=%s",
            d.name)
        raise HTTPException(403, "login required for manual calibration")
    LOGGER.info(
        "solve start job_id=%s pdf_hash=%s use_osrm=%s start=%r end=%r",
        d.name, pdf_hash, bool(payload.get("use_osrm", True)),
        payload.get("start") or None, payload.get("end") or None)
    calib = validate_calib(submitted_calib)
    calib_source = f"manual:{auth_user}" if calib is not None else None
    if calib is None:
        calib = validate_calib(read_json(d / "calib.json"), strict=False)
        calib_source = "job" if calib is not None else None
    if calib is None and pdf_hash:
        calib = load_cached_calib(pdf_hash)
        calib_source = "cache" if calib is not None else None
    LOGGER.info(
        "solve calibration source job_id=%s source=%s control_points=%d stations=%d",
        d.name, calib_source or "none",
        len(calib.get("control_points", [])) if calib else 0,
        len(calib.get("stations", [])) if calib else 0)

    pdfs = [p for p in d.glob("*.pdf") if not p.name.startswith("route_")]
    if not pdfs:
        raise HTTPException(404, "job has no PDF")

    out = d / "out"
    rcache = route_cache_dir(pdf_hash, calib)

    def build_response(raw_summary, log_lines):
        base = f"/jobs/{d.name}/out"
        raw_summary["log"] = log_lines
        raw_summary["base"] = base
        raw_summary["files"] = [f"{base}/{f}" for f in raw_summary["files"]]
        for v in raw_summary["variants"]:
            v["pdf"] = f"{base}/{v['pdf']}"
            v["png"] = f"{base}/{v['png']}"
            if v.get("gpx"):
                v["gpx"] = f"{base}/{v['gpx']}"
            if v.get("kml"):
                v["kml"] = f"{base}/{v['kml']}"
        return raw_summary

    # --- route cache hit ---
    raw_cache_path = rcache / "raw_summary.json" if rcache else None
    if raw_cache_path and raw_cache_path.is_file():
        LOGGER.info(
            "solve route cache hit job_id=%s key=%s", d.name, rcache.name)
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(rcache, out,
                        ignore=shutil.ignore_patterns("raw_summary.json"),
                        dirs_exist_ok=True)
        raw = read_json(raw_cache_path)
        summary = build_response(raw, ["(Ergebnis aus Cache geladen / served from route cache)"])
        LOGGER.info(
            "solve complete (cached) job_id=%s variants=%d files=%d",
            d.name, len(summary["variants"]), len(summary["files"]))
        return JSONResponse(summary)

    # --- compute ---
    shutil.rmtree(out, ignore_errors=True)
    log_lines = []
    def pipeline_log(message):
        log_lines.append(message)
        LOGGER.info("pipeline job_id=%s %s", d.name, message)

    try:
        summary = hr.run_pipeline(
            pdfs[0], calib, out, dpi=RENDER_DPI,
            start=payload.get("start") or None,
            end=payload.get("end") or None,
            use_osrm=bool(payload.get("use_osrm", True)),
            log=pipeline_log,
            resolve_stations=transit_stations_from_icons)
    except Exception as e:
        LOGGER.exception("solve pipeline failed job_id=%s error=%s", d.name, e)
        raise HTTPException(422, f"pipeline failed: {e}")

    if calib:
        calib_json = json.dumps(calib, indent=2, ensure_ascii=False)
        (d / "calib.json").write_text(calib_json)
        if pdf_hash:
            cache_path(pdf_hash).write_text(calib_json)
            LOGGER.info(
                "solve saved calibration cache job_id=%s hash=%s source=%s",
                d.name, pdf_hash, calib_source or "unknown")

    # --- save to route cache (before path adjustment) ---
    if rcache:
        try:
            rcache.mkdir(parents=True, exist_ok=True)
            for f in out.iterdir():
                if f.is_file():
                    shutil.copy2(f, rcache / f.name)
            (rcache / "raw_summary.json").write_text(
                json.dumps({k: v for k, v in summary.items() if k != "log"},
                           indent=2, ensure_ascii=False))
            LOGGER.info("solve route cache saved key=%s", rcache.name)
        except Exception as e:
            LOGGER.warning("solve route cache save failed: %s", e)

    summary = build_response(summary, log_lines)
    LOGGER.info(
        "solve complete job_id=%s variants=%d files=%d",
        d.name, len(summary["variants"]), len(summary["files"]))
    return JSONResponse(summary)


@app.get("/api/geocode")
def geocode(q: str):
    qs = urllib.parse.urlencode({"q": q, "format": "json", "limit": 5})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{qs}",
        headers={"User-Agent": "hoffroute/1.0 (calibration UI)"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=hr.SSL_CTX) as r:
            results = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"geocoder unavailable: {e}")
    return [{"name": r["display_name"], "lat": float(r["lat"]),
             "lon": float(r["lon"])} for r in results]


app.mount("/jobs", StaticFiles(directory=JOBS_DIR), name="jobs")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"),
          name="static")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
