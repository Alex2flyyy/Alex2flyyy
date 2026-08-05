"""FastAPI application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from leadgen import __version__
from leadgen.api.routers import dashboard, exports, leads, runs, stats
from leadgen.api.schemas import HealthOut
from leadgen.config import get_settings
from leadgen.db.session import dispose_engine, get_engine
from leadgen.logging import configure_logging, get_logger

log = get_logger(__name__)

DESCRIPTION = """
Automated lead generation for website design services.

**Discovery** finds local businesses by niche and geography. **Evaluation**
audits each website for speed, mobile support, SSL, SEO, and design age.
**Scoring** ranks them by how much they need a new site and how likely they
are to buy one. **Exports** push the result wherever you work.

Read endpoints are open. Write endpoints require the `X-API-Key` header.
"""


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log.info("api.startup", version=__version__, env=settings.env)

    # Fail loudly at boot rather than on the first request.
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("api.database_ready")
    except Exception as exc:  # noqa: BLE001
        log.error(
            "api.database_unavailable",
            error=str(exc)[:300],
            hint="check LEADGEN_DATABASE_URL and that migrations have run",
        )

    yield

    await dispose_engine()
    log.info("api.shutdown")
    _ = app


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="leadgen",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    if origins := settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["*"],
        )

    app.include_router(leads.router)
    app.include_router(stats.router)
    app.include_router(runs.router)
    app.include_router(exports.router)

    if settings.dashboard_enabled:
        static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
        if static_dir.exists():
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        # Screenshots are served so the dashboard can show what a prospect's
        # site actually looks like next to its score.
        screenshot_dir = Path(settings.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/screenshots", StaticFiles(directory=str(screenshot_dir)), name="screenshots"
        )
        app.include_router(dashboard.router)

    @app.get("/health", response_model=HealthOut, tags=["system"])
    async def health() -> HealthOut:
        database = "ok"
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            database = f"error: {str(exc)[:120]}"

        return HealthOut(
            status="ok" if database == "ok" else "degraded",
            version=__version__,
            database=database,
            environment=settings.env,
            providers=settings.provider_order,
            ai_enabled=bool(settings.ai_enabled and settings.anthropic_api_key),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a stack trace to a client.

        The full traceback goes to the logs with the request path; the caller
        gets a generic message.
        """
        log.exception("api.unhandled", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "path": request.url.path},
        )

    return app


app = create_app()


def main() -> None:
    """``python -m leadgen.api.app`` — run the server directly."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "leadgen.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
