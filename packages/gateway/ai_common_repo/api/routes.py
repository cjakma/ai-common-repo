"""HTTP routes — a thin translation layer over MemoryService."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..core.models import (
    DocumentIngestRequest, HarnessClaimRequest, HarnessStatusUpdate,
    HarnessTaskCreate, RecallRequest, RecallResponse, SearchRequest,
    SessionIngestRequest, StoreMemoryRequest,
)
from ..service import MemoryService

router = APIRouter()


def _svc(request: Request) -> MemoryService:
    return request.app.state.service


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.post("/v1/recall", response_model=RecallResponse)
def recall(req: RecallRequest, request: Request) -> RecallResponse:
    return _svc(request).recall(req)


@router.post("/v1/memory")
def store_memory(req: StoreMemoryRequest, request: Request) -> dict:
    mem = _svc(request).store_memory(req)
    if mem is None:
        return {"stored": False, "deduped": True}
    return {"stored": True, "id": mem.id}


@router.post("/v1/sessions/ingest")
def ingest(req: SessionIngestRequest, request: Request) -> dict:
    return _svc(request).ingest_session(req)


@router.post("/v1/search")
def search(req: SearchRequest, request: Request) -> dict:
    return {"results": _svc(request).search(req)}


@router.post("/v1/documents")
def ingest_document(req: DocumentIngestRequest, request: Request) -> dict:
    try:
        return _svc(request).ingest_document(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/profile")
def profile(request: Request, user_id: str | None = None,
            project_id: str | None = None) -> dict:
    p = _svc(request).get_profile(user_id, project_id)
    return {"profile": {"stable": p.stable, "dynamic": p.dynamic},
            "user_id": p.user_id, "project_id": p.project_id}


@router.post("/v1/memory/{memory_id}/forget")
def forget(memory_id: str, request: Request) -> dict:
    _svc(request).forget(memory_id)
    return {"forgotten": True, "id": memory_id}


@router.get("/v1/model-backends/health")
def backend_health(request: Request) -> dict:
    return {"backends": _svc(request).backend_health()}


# -- harness contract-inbox queue (HARNESS_COMMON_SPEC §4.4) ------------------
def _task_view(task) -> dict:
    return task.model_dump(exclude_none=True)


@router.post("/v1/harness/tasks")
def harness_create(req: HarnessTaskCreate, request: Request) -> dict:
    try:
        task = _svc(request).harness.create(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"task_id": task.id, "task": _task_view(task)}


@router.get("/v1/harness/tasks/{task_id}")
def harness_get(task_id: str, request: Request) -> dict:
    task = _svc(request).harness.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"unknown task {task_id!r}")
    return {"task": _task_view(task)}


@router.post("/v1/harness/tasks/claim")
def harness_claim(req: HarnessClaimRequest, request: Request) -> dict:
    try:
        task = _svc(request).harness.claim(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if task is None:
        return {"task": None}
    return {"task": _task_view(task)}


@router.post("/v1/harness/tasks/{task_id}/status")
def harness_status(task_id: str, req: HarnessStatusUpdate, request: Request) -> dict:
    try:
        task = _svc(request).harness.update_status(task_id, req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown task {task_id!r}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task": _task_view(task)}


@router.get("/v1/harness/tasks")
def harness_list(request: Request, status: str | None = None,
                 executor: str | None = None, limit: int = 100) -> dict:
    try:
        tasks = _svc(request).harness.list(status=status, executor=executor,
                                           limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"tasks": [_task_view(t) for t in tasks]}
