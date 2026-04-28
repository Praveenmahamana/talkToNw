"""
FastAPI application entry point for the Airline Schedule Intelligence API.
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.routes import router
from app.api.graph_viz_routes import router as graph_router
from app.database.db import configure_db, close_connection
from app.database.models import init_db
from app.utils.logging import setup_logging


# ─────────────────────────────────────────────────────────────────────────────
# Settings (environment-driven)
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH      = os.environ.get("SCHEDAI_DB_PATH",  "data/output/schedules.duckdb")
LOG_LEVEL    = os.environ.get("SCHEDAI_LOG_LEVEL", "INFO")
LOG_FILE     = os.environ.get("SCHEDAI_LOG_FILE",  "logs/app.log")
DATA_FOLDER  = os.environ.get("SCHEDAI_DATA_FOLDER", "data/schedules")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    setup_logging(log_level=LOG_LEVEL, log_file=LOG_FILE)
    logger.info("=== Airline Schedule Intelligence API — starting up ===")

    # Database
    configure_db(DB_PATH)
    init_db()
    logger.info(f"Database: {DB_PATH}")

    # Auto-ingest — skip if DB already populated (avoids 4-min startup on restart)
    from app.database.db import get_connection as _get_conn
    _existing = _get_conn().execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    if _existing > 0:
        logger.info(f"DB already has {_existing:,} flights — skipping auto-ingest (fast restart).")
    else:
        data_path = Path(DATA_FOLDER)
        if data_path.is_dir() and any(data_path.iterdir()):
            from app.services.schedule_service import ScheduleService
            svc = ScheduleService()
            result = svc.ingest_folder(str(data_path))
            logger.info(
                f"Auto-ingested {result.get('rows_inserted', 0)} flights from {DATA_FOLDER}"
            )

    # Initialise Vertex AI
    from app.ai.vertex_client import init_vertex
    if init_vertex():
        logger.info("Vertex AI initialised successfully.")
    else:
        logger.warning("Vertex AI not available — running in deterministic-only mode.")

    # Resolve schedule name for identity prompt (must run after ingestion)
    from app.ai.agent import init_schedule_name
    init_schedule_name()

    # Load WORKSET reference data in background (large files — async to avoid blocking)
    def _load_workset_bg():
        try:
            from app.services.workset_service import init_workset
            init_workset()
        except Exception as exc:
            logger.warning(f"Workset background load failed: {exc}")

        # Re-init schedule name AFTER workset is ready so host_airline is correct
        try:
            from app.ai.agent import init_schedule_name
            init_schedule_name()
            logger.info("Schedule name and host airline re-initialised from workset profile.")
        except Exception as exc:
            logger.debug(f"Re-init schedule name failed: {exc}")

        # Build full KG stack: NetworkX → RDFLib → Kuzu → Analytics
        try:
            from app.knowledge_graph.graph_construction import build_all
            summary = build_all()
            ok_layers = sum(1 for v in summary.values() if isinstance(v, dict) and v.get("ok"))
            logger.info(f"KG construction complete — {ok_layers}/4 layers built.")
        except Exception as exc:
            logger.warning(f"Knowledge graph construction failed: {exc}")

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_workset_bg)
    logger.info("Workset data loading started in background …")

    logger.info("Startup complete.")
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    close_connection()
    logger.info("=== Airline Schedule Intelligence API — shut down ===")


# ─────────────────────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Airline Schedule Intelligence API",
        description=(
            "A hybrid AI + deterministic rule engine for airline schedule analysis, "
            "feasibility simulation, and natural language querying.\n\n"
            "## Architecture\n"
            "* **Data layer**: DuckDB schedule storage with auto-ingestion\n"
            "* **Rule engine**: Pure-Python deterministic feasibility checks\n"
            "* **AI layer**: Vertex AI Gemini for intent classification and explanation\n\n"
            "## Key Endpoints\n"
            "* `POST /ingest` — Load schedule files from a folder\n"
            "* `POST /query` — Natural language schedule queries\n"
            "* `POST /simulate/add-flight` — Feasibility simulation for new flights\n"
            "* `POST /simulate/retime-flight` — Impact analysis for timing changes\n"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS (adjust origins for production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static files (landing page)
    _static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

    # Include API routes
    app.include_router(router, prefix="/api/v1")
    app.include_router(graph_router, prefix="/api/v1")

    # Root → landing page
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(
            str(_static / "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    # Convenience alias
    @app.get("/health", include_in_schema=False)
    async def root_health():
        return RedirectResponse(url="/api/v1/health")

    return app


app = create_app()


# ─────────────────────────────────────────────────────────────────────────────
# Dev server entrypoint
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=LOG_LEVEL.lower(),
    )
