"""FastAPI application factory + uvicorn runner."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from ..api.routes import router
from ..core.config import AppConfig, load_config
from ..events.worker import EventDeliveryWorker
from ..mcp import mcp_router
from ..observability import configure_logging, request_context
from ..privacy.redactor import Redactor
from ..service import MemoryService
from ..workers.runtime import build_runtime

_REQUEST_ID_HEADER = "X-Request-ID"


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

    configure_logging(Redactor(cfg.privacy), level=cfg.observability.log_level,
                      fmt=cfg.observability.log_format)

    app = FastAPI(title="ai-common-repo", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.service = MemoryService(cfg)

    @app.middleware("http")
    async def _correlate(request: Request, call_next):
        # Honor a caller-supplied id (trace continuity) or mint one; bind it for
        # the request so every log line is correlated, and echo it back.
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        with request_context(incoming) as rid:
            response = await call_next(request)
        response.headers[_REQUEST_ID_HEADER] = rid
        return response

    app.include_router(router)
    app.include_router(mcp_router)
    return app


def run(cfg: AppConfig | None = None, host: str | None = None,
        port: int | None = None) -> None:
    import uvicorn

    cfg = cfg or load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=host or cfg.server.host, port=port or cfg.server.port)
