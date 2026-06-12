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
import os
import shutil
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import hoffroute as hr

JOBS_DIR = Path(os.environ.get("HOFFROUTE_JOBS_DIR", "jobs")).resolve()
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_PDF_BYTES = 50 * 1024 * 1024
RENDER_DPI = 300

app = FastAPI(title="hoffroute")


def job_dir(job_id: str) -> Path:
    if not all(c in "0123456789abcdef-" for c in job_id):
        raise HTTPException(400, "bad job id")
    d = JOBS_DIR / job_id
    if not d.is_dir():
        raise HTTPException(404, "unknown job")
    return d


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/prepare")
def prepare(file: UploadFile | None = None, url: str = Form(None)):
    if not file and not url:
        raise HTTPException(400, "provide a PDF file or a URL")
    jid = str(uuid.uuid4())
    d = JOBS_DIR / jid
    d.mkdir(parents=True)
    try:
        if file:
            pdf = d / "map.pdf"
            data = file.file.read(MAX_PDF_BYTES + 1)
            if len(data) > MAX_PDF_BYTES:
                raise HTTPException(413, "PDF larger than 50 MB")
            if not data.startswith(b"%PDF"):
                raise HTTPException(400, "not a PDF file")
            pdf.write_bytes(data)
        else:
            if not url.lower().startswith(("http://", "https://")):
                raise HTTPException(400, "URL must be http(s)")
            try:
                pdf = hr.fetch_pdf(url, d)
            except Exception as e:
                raise HTTPException(400, f"could not download PDF: {e}")

        doc, img = hr.render_page(pdf, RENDER_DPI)
        doc[0].get_pixmap(dpi=RENDER_DPI).save(d / "page.png")
        h, w = img.shape[:2]
        icons = hr.detect_station_icons(img)
        # full-page dot candidates as a preview; the solve step re-detects
        # inside the user-chosen bbox
        dots = hr.detect_dots(img, (0, 0, w, h))
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(500, f"failed to process PDF: {e}")

    return {
        "job_id": jid,
        "image": f"/jobs/{jid}/page.png",
        "pdf": f"/jobs/{jid}/{pdf.name}",
        "width": w, "height": h, "dpi": RENDER_DPI,
        "icons": [{"kind": k, "px": x, "py": y} for k, x, y in icons],
        "dots": [{"px": x, "py": y} for x, y in dots],
    }


@app.post("/api/solve")
def solve(payload: dict):
    d = job_dir(payload.get("job_id", ""))
    calib = payload.get("calib") or {}
    if len(calib.get("control_points", [])) < 3:
        raise HTTPException(400, "need at least 3 control points")
    for cp in calib["control_points"]:
        if cp.get("lat") is None or cp.get("lon") is None:
            raise HTTPException(400, f"control point '{cp.get('name')}' has no lat/lon")
    pdfs = [p for p in d.glob("*.pdf") if not p.name.startswith("route_")]
    if not pdfs:
        raise HTTPException(404, "job has no PDF")

    out = d / "out"
    shutil.rmtree(out, ignore_errors=True)
    log_lines = []
    try:
        summary = hr.run_pipeline(
            pdfs[0], calib, out, dpi=RENDER_DPI,
            start=payload.get("start") or None,
            end=payload.get("end") or None,
            use_osrm=bool(payload.get("use_osrm", True)),
            log=log_lines.append)
    except Exception as e:
        raise HTTPException(422, f"pipeline failed: {e}")

    (d / "calib.json").write_text(json.dumps(calib, indent=2, ensure_ascii=False))
    base = f"/jobs/{d.name}/out"
    summary["log"] = log_lines
    summary["base"] = base
    summary["files"] = [f"{base}/{f}" for f in summary["files"]]
    for v in summary["variants"]:
        for k in ("pdf", "gpx", "kml", "png"):
            v[k] = f"{base}/{v[k]}"
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
