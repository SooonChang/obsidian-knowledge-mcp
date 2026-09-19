import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from knowledge_mcp.knowledge import Knowledge
from knowledge_mcp.server import create_app
from knowledge_mcp.storage import Store


@pytest.fixture
def lifecycle_calls(monkeypatch):
    calls = {"recover": 0, "reindex": 0}
    recover, reindex = Knowledge.recover, Store.reindex

    def counted_recover(self):
        calls["recover"] += 1
        return recover(self)

    def counted_reindex(self, *args, **kwargs):
        calls["reindex"] += 1
        return reindex(self, *args, **kwargs)

    monkeypatch.setattr(Knowledge, "recover", counted_recover)
    monkeypatch.setattr(Store, "reindex", counted_reindex)
    return calls


@pytest.fixture
def endpoint(store, monkeypatch, lifecycle_calls):
    token = "test-token-" + "x" * 40
    monkeypatch.setenv("KNOWLEDGE_TOKEN", token)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store.settings), host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp", token
    server.should_exit = True
    thread.join(10)
    assert not thread.is_alive()


async def test_initialization_runs_once_across_concurrent_requests(endpoint, lifecycle_calls):
    url, token = endpoint
    assert lifecycle_calls == {"recover": 1, "reindex": 1}

    async def connect():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 8
                    result = await session.call_tool("get_status")
                    assert not result.isError

    await asyncio.wait_for(asyncio.gather(*(connect() for _ in range(3))), timeout=10)
    assert lifecycle_calls == {"recover": 1, "reindex": 1}


async def test_two_mcp_clients_search_save_read(endpoint, request_data):
    url, token = endpoint

    async def client_action(write=False):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write_stream, _):
                async with ClientSession(read, write_stream) as session:
                    initialized = await session.initialize()
                    assert "저장" in initialized.instructions
                    tools = await session.list_tools()
                    assert len(tools.tools) == 8
                    result = await session.call_tool("search_notes", {"query": "프리퀀시", "scope": "source"})
                    assert not result.isError
                    assert result.structuredContent["results"][0]["path"] == "실험.md"
                    if write:
                        saved = await session.call_tool("save_knowledge", {"request": request_data})
                        assert not saved.isError, saved
                        assert saved.structuredContent["saved"]
                        read_back = await session.call_tool(
                            "read_note", {"vault": "wiki", "path": request_data["changes"][0]["path"]}
                        )
                        assert "저장 이유" in read_back.structuredContent["content"]

    await asyncio.gather(client_action(True), client_action(False))


async def test_mcp_html_continuation(endpoint, store):
    source = store.settings.policy().source
    text = "처음" + "가" * 32000 + "끝"
    (source / "long.html").write_text(f"<p>{text}</p>", encoding="utf-8")
    (source / "linked.md").write_text("[[long.html]]", encoding="utf-8")
    url, token = endpoint
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                schema = next(t for t in (await session.list_tools()).tools if t.name == "read_attachment")
                assert {"start", "max_chars", "expected_hash"} <= schema.inputSchema["properties"].keys()
                args = {"path": "long.html", "linked_from": "linked.md", "linked_vault": "source"}
                pieces = []
                for _ in range(3):
                    result = await session.call_tool("read_attachment", args)
                    assert not result.isError, result
                    data = result.structuredContent
                    pieces.append(data["content"])
                    if data["next_start"] is None:
                        break
                    args.update(start=data["next_start"], expected_hash=data["hash"])
                assert data["next_start"] is None
                assert "".join(pieces) == text
                invalid = await session.call_tool("read_attachment", args | {"start": 0})
                assert invalid.isError


def test_http_auth_and_origin(endpoint):
    url, token = endpoint
    assert httpx.post(url, json={}).status_code == 401
    response = httpx.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers={
            "Authorization": "Bearer " + token,
            "Origin": "https://untrusted.example",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 403
