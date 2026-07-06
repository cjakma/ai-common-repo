"""Phase 3 — MCP server. All offline: an in-memory MemoryService over a temp DB,
the JSON-RPC dispatcher exercised directly and via the Streamable-HTTP transport
(FastAPI TestClient, in-process — no sockets, no network)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

from fastapi.testclient import TestClient  # noqa: E402

from ai_common_repo.core.config import load_config  # noqa: E402
from ai_common_repo.mcp.server import (  # noqa: E402
    ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, TOOL_MAP, McpServer,
)
from ai_common_repo.server.app import create_app  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402


def _service() -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline => hash embedder
    return MemoryService(load_config())


def _server() -> McpServer:
    return McpServer(_service())


def _req(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


def _call(server, name, arguments):
    return server.handle_message(_req("tools/call", {"name": name, "arguments": arguments}))


# -- protocol handshake ------------------------------------------------------
def test_initialize_reports_server_info_and_capabilities():
    resp = _server().handle_message(_req("initialize", {"protocolVersion": "2025-06-18"}))
    res = resp["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "ai-common-repo"
    assert "tools" in res["capabilities"]


def test_initialized_notification_gets_no_response():
    assert _server().handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping():
    assert _server().handle_message(_req("ping"))["result"] == {}


# -- tools/list --------------------------------------------------------------
def test_tools_list_exposes_eight_tools_with_schemas():
    tools = _server().handle_message(_req("tools/list"))["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(TOOL_MAP)
    assert len(tools) == 8
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


# -- tools/call: each tool works end to end ----------------------------------
def test_call_store_then_search_and_forget():
    server = _server()
    stored = _call(server, "memory_store",
                   {"content": "deploys use blue-green", "type": "decision"})["result"]
    assert stored["isError"] is False
    mem_id = stored["structuredContent"]["id"]

    found = _call(server, "memory_search", {"q": "blue-green", "search_mode": "fts"})
    results = found["result"]["structuredContent"]["results"]
    assert any(r["id"] == mem_id for r in results)

    forgotten = _call(server, "memory_forget", {"memory_id": mem_id})["result"]
    assert forgotten["structuredContent"]["forgotten"] is True


def test_call_recall_returns_context_block():
    server = _server()
    _call(server, "memory_store", {"content": "use ruff for linting", "type": "preference"})
    res = _call(server, "memory_recall", {"query": "linting"})["result"]
    assert "context_block" in res["structuredContent"]


def test_call_session_ingest_and_profile():
    server = _server()
    ingest = _call(server, "session_ingest", {
        "tool": "claude-code",
        "messages": [{"role": "user", "content": "remember: prod region is us-east-1"}],
    })["result"]
    assert ingest["structuredContent"]["accepted"] is True

    profile = _call(server, "memory_profile", {})["result"]
    sc = profile["structuredContent"]["profile"]
    assert "stable" in sc and "dynamic" in sc


# -- error standardization ---------------------------------------------------
def test_unknown_method_is_method_not_found():
    err = _server().handle_message(_req("does/not/exist"))["error"]
    assert err["code"] == ERR_METHOD_NOT_FOUND


def test_unknown_tool_is_invalid_params():
    err = _call(_server(), "no_such_tool", {})["error"]
    assert err["code"] == ERR_INVALID_PARAMS


def test_invalid_arguments_are_invalid_params():
    # memory_store requires 'content'.
    err = _call(_server(), "memory_store", {"type": "decision"})["error"]
    assert err["code"] == ERR_INVALID_PARAMS
    assert err["data"]  # validation detail attached


def test_bad_jsonrpc_envelope_is_invalid_request():
    err = _server().handle_message({"method": "ping", "id": 9})["error"]
    assert err["code"] == -32600


# -- Streamable-HTTP transport (in-process, no network) ----------------------
def test_http_transport_tools_list_and_call():
    app = create_app(_service().cfg)
    with TestClient(app) as client:
        r = client.post("/mcp", json=_req("tools/list"))
        assert r.status_code == 200
        assert len(r.json()["result"]["tools"]) == 8

        r = client.post("/mcp", json=_req("tools/call", {
            "name": "memory_store",
            "arguments": {"content": "http path works", "type": "decision"}}))
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is False


def test_http_transport_notification_returns_202():
    app = create_app(_service().cfg)
    with TestClient(app) as client:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202
