# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# --- builder ---
FROM base AS builder
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# --- runner ---
FROM base AS runner
COPY --from=builder /install /usr/local
COPY --chown=appuser:appgroup hoffroute.py webapp.py ./
COPY --chown=appuser:appgroup static/ static/
RUN mkdir -p /data/jobs && chown appuser:appgroup /data/jobs
ENV HOFFROUTE_JOBS_DIR=/data/jobs \
    APP_PORT=8000
USER appuser
# EXPOSE is documentation only; the actual port follows APP_PORT
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.environ.get('APP_PORT', '8000')}/health\")"
CMD ["sh", "-c", "uvicorn webapp:app --host 0.0.0.0 --port ${APP_PORT:-8000}"]
