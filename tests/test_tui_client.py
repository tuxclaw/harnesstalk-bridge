from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from tui.client import BridgeClient


@pytest.mark.asyncio
async def test_client_constructs_valid_mcp_read_requests() -> None:
    client = BridgeClient("http://bridge/mcp", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": []}))))
    try:
        request = client.build_tool_request("get_audit", {"limit": 200})
    finally:
        await client.close()

    assert request == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_audit", "arguments": {"limit": 200}},
    }


@pytest.mark.asyncio
async def test_client_handles_disconnect_with_cached_state() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = BridgeClient("http://bridge/mcp", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        targets = await client.list_targets()
    finally:
        await client.close()

    assert targets == []
    assert client.connected is False
    assert client.last_error is not None


def test_client_source_never_references_write_tools() -> None:
    source = Path("tui/client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert "consult" not in constants
    assert "open_session" not in constants
    assert "close_session" not in constants
