"""FastAPI application factory + uvicorn runner."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..api.routes import router
from ..core.config import AppConfig, load_config
from ..events.worker import EventDeliveryWorker
from ..mcp import mcp_router
from ..service import MemoryService
from ..workers.runtime import build_runtime


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = scheduler = webhook_worker = None
        if cfg.workers.enabled:
            worker, scheduler = build_runtime(app.state.service, cfg)
            worker.start()
            scheduler.start()
            app.state.worker, app.state.scheduler = worker, scheduler
        if cfg.notifications.enabled:
            webhook_worker = EventDeliveryWorker(
                app.state.service.event_dispatcher,
                poll_interval_s=cfg.notifications.outbox_poll_interval_s)
            webhook_worker.start()
            app.state.webhook_worker = webhook_worker
        try:
            yield
        finally:
            if webhook_worker is not None:
                webhook_worker.stop()
            if scheduler is not None:
                scheduler.stop()
            if worker is not None:
                worker.stop()

    app = FastAPI(title="ai-common-repo", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.service = MemoryService(cfg)
    app.include_router(router)
    app.include_router(mcp_router)
    return app


def run(cfg: AppConfig | None = None, host: str | None = None,
        port: int | None = None) -> None:
    import uvicorn

    cfg = cfg or load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=host or cfg.server.host, port=port or cfg.server.port)
