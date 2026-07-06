"""Streamable-HTTP MCP transport — a thin FastAPI shell over :class:`McpServer`.

A single ``POST /mcp`` endpoint accepts a JSON-RPC request (or batch) and
replies with ``application/json``. Notifications (no id) get ``202 Accepted``
with an empty body. All logic lives in :mod:`.server`; this only moves bytes.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .server import McpServer, parse_error

router = APIRouter()


def _server(request: Request) -> McpServer:
    return McpServer(request.app.state.service)


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    server = _server(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body -> JSON-RPC parse error
        return JSONResponse(parse_error(), status_code=400)

    if isinstance(payload, list):
        responses = [r for r in (server.handle_message(m) for m in payload)
                     if r is not None]
        return JSONResponse(responses) if responses else Response(status_code=202)

    resp = server.handle_message(payload)
    if resp is None:
        return Response(status_code=202)
    return JSONResponse(resp)
