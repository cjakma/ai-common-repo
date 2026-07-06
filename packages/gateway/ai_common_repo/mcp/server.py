"""MCP exposure (design §11). Phase 3.

A transport-agnostic Model Context Protocol server. ``McpServer.handle_message``
speaks JSON-RPC 2.0 (initialize / tools/list / tools/call / ping); the
Streamable-HTTP transport in :mod:`.transport` is a thin shell over it, so the
protocol can be exercised in tests without any network.

Every tool maps 1:1 to a :class:`MemoryService` method — argument validation
reuses the same pydantic request models as the HTTP layer, and the returned
payloads mirror the HTTP routes. There is no business logic here to drift.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from ..core.models import (
    HarnessTaskCreate, RecallRequest, SearchRequest, SessionIngestRequest,
    StoreMemoryRequest,
)
from ..service import MemoryService

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ai-common-repo", "version": "0.1.0"}

# JSON-RPC 2.0 error codes (spec) + MCP usage.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


class McpError(Exception):
    """A JSON-RPC level failure (bad request / unknown method / bad params)."""

    def __init__(self, code: int, message: str, data: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# -- tool argument models without a dedicated API DTO ------------------------
class _ProfileArgs(BaseModel):
    user_id: str | None = None
    project_id: str | None = None


class _ForgetArgs(BaseModel):
    memory_id: str = Field(..., min_length=1)


class _HarnessRequestArgs(HarnessTaskCreate):
    # MCP callers are requesters by definition; default the requester name so a
    # host only has to say who should execute.
    requester: str = "mcp"


class _HarnessResultArgs(BaseModel):
    task_id: str = Field(..., min_length=1)


# -- tool registry -----------------------------------------------------------
@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    model: type[BaseModel]
    invoke: Callable[[MemoryService, BaseModel], dict]


def _store(svc: MemoryService, req: StoreMemoryRequest) -> dict:
    mem = svc.store_memory(req)
    if mem is None:
        return {"stored": False, "deduped": True}
    return {"stored": True, "id": mem.id, "memory": mem.model_dump(exclude_none=True)}


def _search(svc: MemoryService, req: SearchRequest) -> dict:
    return {"results": svc.search(req)}


def _recall(svc: MemoryService, req: RecallRequest) -> dict:
    return svc.recall(req).model_dump()


def _profile(svc: MemoryService, req: _ProfileArgs) -> dict:
    p = svc.get_profile(req.user_id, req.project_id)
    return {"profile": {"stable": p.stable, "dynamic": p.dynamic},
            "user_id": p.user_id, "project_id": p.project_id}


def _forget(svc: MemoryService, req: _ForgetArgs) -> dict:
    svc.forget(req.memory_id)
    return {"forgotten": True, "id": req.memory_id}


def _ingest(svc: MemoryService, req: SessionIngestRequest) -> dict:
    return svc.ingest_session(req)


def _harness_request(svc: MemoryService, req: _HarnessRequestArgs) -> dict:
    task = svc.harness.create(req)
    return {"task_id": task.id, "status": task.status}


def _harness_result(svc: MemoryService, req: _HarnessResultArgs) -> dict:
    task = svc.harness.get(req.task_id)
    if task is None:
        raise ValueError(f"unknown task {req.task_id!r}")
    out: dict = {"task_id": task.id, "status": task.status}
    if task.result is not None:
        out["result"] = task.result
    if task.error is not None:
        out["error"] = task.error
    return out


_TOOLS: tuple[_Tool, ...] = (
    _Tool("memory_store",
          "Store an explicit long-term memory (preference/decision/fact/…).",
          StoreMemoryRequest, _store),
    _Tool("memory_search",
          "Search memories and document chunks (fts/vector/hybrid).",
          SearchRequest, _search),
    _Tool("memory_recall",
          "Recall a fused context block (profile + memories + document excerpts).",
          RecallRequest, _recall),
    _Tool("memory_profile",
          "Get the rolled-up stable/dynamic profile for a user/project scope.",
          _ProfileArgs, _profile),
    _Tool("memory_forget",
          "Forget a memory by id (marks it forgotten).",
          _ForgetArgs, _forget),
    _Tool("session_ingest",
          "Ingest a coding session transcript for extraction into memories.",
          SessionIngestRequest, _ingest),
    _Tool("harness_request",
          "Enqueue a contract task for a peer harness executor "
          "(claudecode/openclaw/opencode). Returns the task_id to poll.",
          _HarnessRequestArgs, _harness_request),
    _Tool("harness_result",
          "Poll a harness task by id: status plus result/error when finished.",
          _HarnessResultArgs, _harness_result),
)

# Back-compat: tool name -> MemoryService method (kept for documentation/tests).
TOOL_MAP = {
    "memory_store": "store_memory",
    "memory_search": "search",
    "memory_recall": "recall",
    "memory_profile": "get_profile",
    "memory_forget": "forget",
    "session_ingest": "ingest_session",
    "harness_request": "harness.create",
    "harness_result": "harness.get",
}


def describe_tools() -> list[str]:
    return [t.name for t in _TOOLS]


def _input_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


class McpServer:
    """JSON-RPC dispatcher bound to a shared :class:`MemoryService`."""

    def __init__(self, service: MemoryService):
        self._service = service
        self._tools = {t.name: t for t in _TOOLS}

    # -- introspection (also used by older callers / tests) ------------------
    def tools(self) -> list[str]:
        return describe_tools()

    def list_tools(self) -> list[dict]:
        return [{"name": t.name, "description": t.description,
                 "inputSchema": _input_schema(t.model)} for t in self._tools.values()]

    def call_tool(self, name: str, arguments: dict | None) -> dict:
        """Validate + run a tool, returning an MCP tools/call result. Domain
        validation errors (ValueError) surface as ``isError`` results, not
        protocol errors, per the MCP spec."""
        tool = self._tools.get(name)
        if tool is None:
            raise McpError(ERR_INVALID_PARAMS, f"unknown tool: {name!r}")
        try:
            req = tool.model.model_validate(arguments or {})
        except ValidationError as exc:
            raise McpError(ERR_INVALID_PARAMS, f"invalid arguments for {name!r}",
                           _validation_data(exc)) from exc
        try:
            result = tool.invoke(self._service, req)
        except ValueError as exc:
            return _tool_result(str(exc), is_error=True)
        return _tool_result(json.dumps(result, ensure_ascii=False), structured=result)

    # -- JSON-RPC ------------------------------------------------------------
    def handle_message(self, msg: object) -> dict | None:
        """Process one JSON-RPC request/notification. Returns the response
        object, or None for notifications (no id)."""
        if not isinstance(msg, dict):
            return _error(None, ERR_INVALID_REQUEST, "request must be a JSON object")
        msg_id = msg.get("id")
        method = msg.get("method")
        is_notification = "id" not in msg
        if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return None if is_notification else _error(
                msg_id, ERR_INVALID_REQUEST, "invalid JSON-RPC request")
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except McpError as exc:
            return None if is_notification else _error(
                msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - last-resort isolation
            return None if is_notification else _error(msg_id, ERR_INTERNAL, str(exc))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict) -> dict | None:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.list_tools()}
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise McpError(ERR_INVALID_PARAMS, "tools/call requires a 'name'")
            return self.call_tool(params["name"], params.get("arguments"))
        if method.startswith("notifications/"):
            return None  # acknowledged; nothing to do
        raise McpError(ERR_METHOD_NOT_FOUND, f"method not found: {method!r}")


# -- module helpers ----------------------------------------------------------
def _tool_result(text: str, *, structured: dict | None = None,
                 is_error: bool = False) -> dict:
    res: dict = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None and not is_error:
        res["structuredContent"] = structured
    return res


def _validation_data(exc: ValidationError) -> list[dict]:
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")}
            for e in exc.errors()]


def _error(msg_id: object, code: int, message: str,
           data: object | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def parse_error() -> dict:
    """A JSON-RPC parse error (used by the transport on malformed bodies)."""
    return _error(None, ERR_PARSE, "parse error: body is not valid JSON")
