"""MCP (Model Context Protocol) exposure for the gateway."""
from __future__ import annotations

from .server import TOOL_MAP, McpError, McpServer, describe_tools
from .transport import router as mcp_router

__all__ = ["TOOL_MAP", "McpError", "McpServer", "describe_tools", "mcp_router"]
