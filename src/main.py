from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, cluster, portal, service, yescaptcha
from .core.auth import set_database
from .core.config import config
from .core.database import Database
from .core.logger import debug_logger
from .services.captcha_runtime import CaptchaRuntime
from .services.cluster_manager import ClusterManager
from .services.yescaptcha_manager import YesCaptchaTaskManager


db = Database()
runtime = CaptchaRuntime(db)
cluster_manager = ClusterManager(db, runtime)
yescaptcha_task_manager = YesCaptchaTaskManager(task_ttl_seconds=max(600, int(config.session_ttl_seconds or 1200)))

set_database(db)
service.set_dependencies(db, runtime, cluster_manager)
admin.set_dependencies(db, runtime, cluster_manager)
cluster.set_dependencies(db, cluster_manager)
portal.set_dependencies(db, runtime, cluster_manager)
yescaptcha.set_dependencies(db, runtime, cluster_manager, yescaptcha_task_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    debug_logger.log_info("=" * 60)
    debug_logger.log_info("flow_captcha_service starting...")

    await db.init_db()
    await db.initialize_log_store()
    startup_cleanup = await db.startup_log_maintenance()
    cleaned_total = (
        int(startup_cleanup.get("captcha_jobs") or 0)
        + int(startup_cleanup.get("portal_user_jobs") or 0)
        + int(startup_cleanup.get("cluster_node_heartbeats") or 0)
        + int(startup_cleanup.get("cluster_node_errors") or 0)
    )
    if cleaned_total > 0:
        debug_logger.log_info(
            "[startup] cleared sqlite logs "
            f"captcha_jobs={int(startup_cleanup.get('captcha_jobs') or 0)} "
            f"portal_user_jobs={int(startup_cleanup.get('portal_user_jobs') or 0)} "
            f"cluster_node_heartbeats={int(startup_cleanup.get('cluster_node_heartbeats') or 0)} "
            f"cluster_node_errors={int(startup_cleanup.get('cluster_node_errors') or 0)} "
            f"backfilled_complete_events={int(startup_cleanup.get('backfilled_complete_events') or 0)}"
        )
    await db.start_periodic_log_cleanup()
    await runtime.start()
    await cluster_manager.start()
    await yescaptcha_task_manager.start()

    debug_logger.log_info(f"node={config.node_name}, role={config.cluster_role}")
    debug_logger.log_info("startup complete")
    debug_logger.log_info("=" * 60)

    yield

    debug_logger.log_info("flow_captcha_service shutting down...")
    await yescaptcha_task_manager.close()
    await cluster_manager.close()
    await runtime.close()
    await db.close()
    debug_logger.log_info("shutdown complete")


app = FastAPI(
    title="flow_captcha_service",
    version="0.1.0",
    description="Headed captcha service for Flow2API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(service.router)
app.include_router(admin.router)
app.include_router(cluster.router)
app.include_router(portal.router)
app.include_router(yescaptcha.router)

static_dir = config.root_dir / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _static_page(filename: str, missing_message: str):
    page_path = static_dir / filename
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=missing_message)
    return FileResponse(page_path)


def _public_page_filename() -> str:
    return "subnode.html" if config.cluster_role == "subnode" else "portal.html"


@app.get("/", include_in_schema=False)
async def root(request: Request):
    accept = str(request.headers.get("accept") or "")
    if "text/html" in accept:
        filename = _public_page_filename()
        return _static_page(filename, "公共页面不存在")

    return {
        "service": "flow_captcha_service",
        "status": "ok",
        "node": config.node_name,
        "role": config.cluster_role,
        "portal": "/portal" if config.cluster_role != "subnode" else None,
        "public_page": "/" if config.cluster_role == "subnode" else "/portal",
        "admin": "/admin",
    }


@app.get("/portal", include_in_schema=False)
async def portal_alias():
    filename = _public_page_filename()
    return _static_page(filename, "公共页面不存在")


@app.get("/subnode", include_in_schema=False)
async def subnode_page():
    return _static_page("subnode.html", "子节点页面不存在")


@app.get("/admin", include_in_schema=False)
async def admin_panel():
    return _static_page("admin.html", "管理面板页面不存在")
