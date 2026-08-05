# syntax=docker/dockerfile:1.6
# -----------------------------------------------------------------------------
# The Playwright base image ships Chromium plus every system library it needs
# (fonts, codecs, X libs).  Installing those by hand on a slim Python image is
# the single biggest source of "works locally, breaks in CI" for this project,
# so we start from the image Microsoft maintains for exactly that purpose.
# -----------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependency layer first so source edits don't invalidate the pip cache.
COPY pyproject.toml README.md ./
COPY src/leadgen/__init__.py src/leadgen/__init__.py
RUN pip install --no-cache-dir -e .

COPY . .
RUN pip install --no-cache-dir -e .

# Non-root at runtime.  The Playwright image already defines `pwuser`.
RUN mkdir -p /app/data/exports /app/data/screenshots /app/data/reports \
    && chown -R pwuser:pwuser /app
USER pwuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import httpx,os,sys; sys.exit(0 if httpx.get(f'http://127.0.0.1:{os.getenv(\"LEADGEN_API_PORT\",\"8000\")}/health', timeout=4).status_code==200 else 1)"

EXPOSE 8000

# --- API + dashboard (default) ----------------------------------------------
FROM base AS api
CMD ["uvicorn", "leadgen.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Scheduled pipeline runner ----------------------------------------------
FROM base AS worker
CMD ["leadgen", "run-daily"]
