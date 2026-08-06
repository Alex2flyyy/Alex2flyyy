# syntax=docker/dockerfile:1.6
# -----------------------------------------------------------------------------
# The Playwright base image ships Chromium plus every system library it needs
# (fonts, codecs, X libs).  Installing those by hand on a slim Python image is
# the single biggest source of "works locally, breaks in CI" for this project,
# so we start from the image Microsoft maintains for exactly that purpose.
#
# It MUST be a `-noble` (Ubuntu 24.04) tag: the `-jammy` images ship Python
# 3.10, and this package requires >=3.11, so pip refuses to install on them.
# Keep this tag's version in step with the `playwright` pin in pyproject.toml —
# the image's preinstalled browsers are matched to that release.
# -----------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Ubuntu 24.04 marks its system Python as externally managed (PEP 668) and
    # pip refuses to install into it. In a single-purpose container there is no
    # environment to protect, so opt out rather than add a venv layer.
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependency layer first so source edits don't invalidate the pip cache.
COPY pyproject.toml README.md ./
COPY src/leadgen/__init__.py src/leadgen/__init__.py
RUN pip install --no-cache-dir -e .

COPY . .
RUN pip install --no-cache-dir -e .

# The base image preinstalls browsers for its own Playwright release. If pip
# resolved a different playwright version, those revisions no longer match and
# every render fails at runtime with "executable doesn't exist". This is a
# no-op when they agree, and downloads the correct build when they don't.
RUN python -m playwright install chromium

# Non-root at runtime.  The Playwright image already defines `pwuser`.
RUN mkdir -p /app/data/exports /app/data/screenshots /app/data/reports \
    && chown -R pwuser:pwuser /app
USER pwuser

# Single quotes only inside the outer double quotes. Nested escaped quotes here
# are parsed by both the Dockerfile parser and the shell, and break in ways that
# only surface at container runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,httpx; port=os.getenv('LEADGEN_API_PORT','8000'); sys.exit(0 if httpx.get('http://127.0.0.1:'+port+'/health', timeout=4).status_code==200 else 1)"

EXPOSE 8000

# --- API + dashboard (default) ----------------------------------------------
FROM base AS api
CMD ["uvicorn", "leadgen.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Scheduled pipeline runner ----------------------------------------------
FROM base AS worker
CMD ["leadgen", "run-daily"]
