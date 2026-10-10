"""Real loopback transports; the extension is a controlled protocol peer.

These tests prove sharing and recovery, not Chrome/native RPA completion.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager

import pytest
import websockets
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from shizai_agent.core.browser_bridge import (
    BridgeClosedError,
    BridgeUnavailableError,
    BrowserBridgeError,
    BrowserBridgeWebSocketServer,
    RequestTimeoutError,
)
from shizai_agent.core.browser_mcp import BrowserBridgeManager
from shizai_agent.core.browser_relay import BrowserBridgeRelay


@pytest.fixture
def bridge(tmp_path):
    server = BrowserBridgeWebSocketServer(allowed_extension_ids=["abc"], install_token="test-only-secret")
    server.start()
    config = tmp_path / "bridge.json"
    config.write_text(json.dumps({
        "allowedExtensionIds": ["abc"], "installToken": "test-only-secret", "bridgePort": server.port,
    }), encoding="utf-8")
    try:
        yield server, config
    finally:
        server.stop()
        assert not server._thread.is_alive()


@asynccontextmanager
async def extension(port, *, respond=True):
    async with websockets.connect(f"ws://127.0.0.1:{port}", origin="chrome-extension://abc") as ws:
        await ws.send(json.dumps({
            "type": "hello", "protocolVersion": "1.0", "extensionId": "abc",
            "extensionVersion": "test-peer", "token": "test-only-secret", "capabilities": ["list_tabs"],
        }))
        assert json.loads(await ws.recv())["type"] == "helloAck"

        async def answer():
            async for raw in ws:
                request = json.loads(raw)
                await reply(ws, request)

        task = asyncio.create_task(answer()) if respond else None
        try:
            yield ws
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def reply(ws, request, *, result=None, error=None):
    payload = {"protocolVersion": "1.0", "requestId": request["requestId"], "ok": error is None}
    if error:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {"tabs": [{"tabId": 7}]}
    await ws.send(json.dumps(payload))


def test_shared_managers_route_out_of_order_without_closing_owner(bridge):
    server, config = bridge

    async def run():
        a, b = BrowserBridgeManager(config), BrowserBridgeManager(config)
        try:
            async with extension(server.port, respond=False) as ws:
                assert a.status()["connection_mode"] == b.status()["connection_mode"] == "shared"
                one = asyncio.create_task(asyncio.to_thread(a.call, "inspect_points", {"tabId": 7}))
                two = asyncio.create_task(asyncio.to_thread(b.call, "inspect_points", {"tabId": 8}))
                requests = [json.loads(await ws.recv()), json.loads(await ws.recv())]
                assert requests[0]["requestId"] != requests[1]["requestId"]
                for request in reversed(requests):
                    await reply(ws, request, result={"tabId": request["params"]["tabId"]})
                assert await one == {"tabId": 7}
                assert await two == {"tabId": 8}
                a.close()
                assert server.connected
                assert b.status()["connected"]
                assert "test-only-secret" not in json.dumps(b.status())
        finally:
            a.close()
            b.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_shared_capture_keeps_large_image_and_extension_errors(bridge):
    server, config = bridge

    async def run():
        manager = BrowserBridgeManager(config)
        try:
            async with extension(server.port, respond=False) as ws:
                call = asyncio.create_task(asyncio.to_thread(manager.call, "capture_viewport", {"tabId": 7}))
                request = json.loads(await ws.recv())
                image = "data:image/png;base64," + "A" * (2 * 1024 * 1024)
                await reply(ws, request, result={"dataUrl": image, "screenshotId": "fresh"})
                assert (await call)["dataUrl"] == image
                call = asyncio.create_task(asyncio.to_thread(manager.call, "inspect_points"))
                request = json.loads(await ws.recv())
                await reply(ws, request, error={"code": "stale_page_revision", "message": "capture again"})
                with pytest.raises(BrowserBridgeError, match="stale_page_revision"):
                    await call
        finally:
            manager.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_owner_exit_is_recovered_on_next_call(bridge):
    server, config = bridge
    manager = BrowserBridgeManager(config)
    assert manager.status()["connection_mode"] == "shared"
    server.stop()
    try:
        recovered = manager.status()
        assert recovered["connection_mode"] == "owner"
        assert recovered["bridge_listening"]
        assert "install_command" not in recovered

        async def run():
            async with extension(server.port):
                assert await asyncio.to_thread(manager.call, "list_tabs") == {"tabs": [{"tabId": 7}]}

        asyncio.run(asyncio.wait_for(run(), 10))
    finally:
        manager.close()


def test_disconnect_returns_unknown_promptly_and_does_not_replay_action(bridge):
    server, config = bridge

    async def run():
        manager = BrowserBridgeManager(config)
        try:
            async with extension(server.port, respond=False) as ws:
                call = asyncio.create_task(asyncio.to_thread(manager.call, "performAction", {"action": "click"}))
                request = json.loads(await ws.recv())
                assert request["method"] == "performAction"
                started = time.monotonic()
                await ws.close()
                result = await call
                assert time.monotonic() - started < 2
                assert result["state"] == "bridge_disconnected"
                assert result["execution_outcome"] == "unknown" and result["retryable"] is False
            async with extension(server.port, respond=False) as ws:
                # Only the explicitly requested read should arrive after reconnect.
                call = asyncio.create_task(asyncio.to_thread(manager.call, "list_tabs"))
                request = json.loads(await ws.recv())
                assert request["method"] == "list_tabs"
                await reply(ws, request)
                assert await call == {"tabs": [{"tabId": 7}]}
        finally:
            manager.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_connection_that_arrives_during_wait_executes_original_read(bridge):
    server, config = bridge

    async def run():
        manager = BrowserBridgeManager(config)
        try:
            call = asyncio.create_task(asyncio.to_thread(manager.call, "list_tabs"))
            await asyncio.sleep(0.1)
            async with extension(server.port):
                assert await call == {"tabs": [{"tabId": 7}]}
        finally:
            manager.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_timeout_reports_unknown_and_late_response_cannot_contaminate_next_call(bridge):
    server, config = bridge

    async def run():
        manager = BrowserBridgeManager(config)
        try:
            async with extension(server.port, respond=False) as ws:
                call = asyncio.create_task(asyncio.to_thread(manager.call, "performAction", timeout=0.05))
                stale = json.loads(await ws.recv())
                result = await call
                assert result["state"] == "request_timeout" and result["execution_outcome"] == "unknown"
                assert result["retryable"] is False
                await reply(ws, stale, result={"stale": True})
                call = asyncio.create_task(asyncio.to_thread(manager.call, "list_tabs"))
                request = json.loads(await ws.recv())
                assert request["method"] == "list_tabs"
                await reply(ws, request)
                assert await call == {"tabs": [{"tabId": 7}]}
        finally:
            manager.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_owner_dies_between_discovery_and_status_is_retryable(bridge, monkeypatch):
    _, config = bridge
    manager = BrowserBridgeManager(config)
    relay = manager.ensure_started()
    assert isinstance(relay, BrowserBridgeRelay)

    def disconnected(*_args, **_kwargs):
        raise BridgeUnavailableError("owner gone")

    monkeypatch.setattr(manager, "ensure_started", lambda: relay)
    monkeypatch.setattr(relay, "status_snapshot", disconnected)
    try:
        result = manager.status()
        assert result["state"] == "bridge_disconnected" and result["retryable"] is True
        relay._snapshot = {"connected": True}
        monkeypatch.setattr(relay, "call", disconnected)
        result = manager.call("performAction")
        assert result["execution_outcome"] == "not_sent"
    finally:
        manager.close()


@pytest.mark.parametrize("failure,error", [(OSError("socket gone"), BridgeClosedError),
                                         (TimeoutError("send stalled"), RequestTimeoutError)])
def test_transport_send_failure_releases_pending_request(bridge, failure, error):
    server, _ = bridge

    class BrokenConnection:
        async def send(self, _message):
            raise failure

    server._connection = BrokenConnection()
    try:
        with pytest.raises(error, match="outcome unknown"):
            server.client.call("performAction", {"action": "click"})
        assert not server.client.broker._pending
    finally:
        server._connection = None


@pytest.mark.parametrize("origin,token", [(None, "wrong"), ("https://example.test", "test-only-secret"),
                                        ("chrome-extension://abc", "test-only-secret")])
def test_relay_rejects_bad_token_and_browser_origins(bridge, origin, token):
    server, _ = bridge

    async def run():
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/mcp", origin=origin) as ws:
            try:
                await ws.send(json.dumps({"type": "mcpHello", "protocolVersion": "1.0", "token": token}))
                await ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.code == 1008
            else:
                pytest.fail("unauthorized relay peer was accepted")
        assert not server.connected

    asyncio.run(asyncio.wait_for(run(), 5))


def test_port_owned_by_incompatible_service_has_actionable_failure(tmp_path):
    async def run():
        async def old_owner(ws):
            await ws.close(code=1008, reason="old server")

        async with websockets.serve(old_owner, "127.0.0.1", 0, close_timeout=0.1) as old:
            config = tmp_path / "bridge.json"
            config.write_text(json.dumps({"allowedExtensionIds": ["abc"], "installToken": "secret",
                                          "bridgePort": old.sockets[0].getsockname()[1]}), encoding="utf-8")
            manager = BrowserBridgeManager(config)
            try:
                result = await asyncio.to_thread(manager.status)
                assert result["state"] == "bridge_in_use"
                assert "older MCP" in result["resolution"]
                assert "install_command" not in result
            finally:
                manager.close()

    asyncio.run(asyncio.wait_for(run(), 5))


@asynccontextmanager
async def mcp_process(config, errlog):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "shizai_agent.core.mcp_server"],
        env={**os.environ, "SHIZAI_BROWSER_BRIDGE_CONFIG": str(config), "SHIZAI_MCP_TOOL_PROFILE": "rpa-authoring"},
    )
    async with stdio_client(params, errlog=errlog) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def decoded(result):
    assert not result.isError
    return json.loads(result.content[0].text)


def test_two_real_mcp_processes_share_extension_and_survive_owner_exit(bridge):
    server, config = bridge
    # Release the reservation; first MCP process becomes the real owner.
    server.stop()

    async def run():
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as first_log, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8") as second_log:
            stop_owner = asyncio.Event()
            owner_ready = asyncio.get_running_loop().create_future()

            async def own():
                try:
                    async with mcp_process(config, first_log) as session:
                        owner_ready.set_result(session)
                        await stop_owner.wait()
                except Exception as exc:
                    if not owner_ready.done():
                        owner_ready.set_exception(exc)
                    raise

            owner_task = asyncio.create_task(own())
            try:
                owner = await owner_ready
                async with mcp_process(config, second_log) as second:
                    async with extension(server.port):
                        states = await asyncio.gather(owner.call_tool("shizai_browser_status", {}),
                                                      second.call_tool("shizai_browser_status", {}))
                        assert [decoded(state)["connection_mode"] for state in states] == ["owner", "shared"]
                        results = await asyncio.gather(owner.call_tool("shizai_browser_tabs", {}),
                                                       second.call_tool("shizai_browser_tabs", {}))
                        assert all(decoded(result) == {"tabs": [{"tabId": 7}]} for result in results)
                    stop_owner.set()
                    await owner_task
                    state = decoded(await second.call_tool("shizai_browser_status", {}))
                    assert state["connection_mode"] == "owner" and state["bridge_listening"]
                    async with extension(server.port):
                        assert decoded(await second.call_tool("shizai_browser_tabs", {})) == {"tabs": [{"tabId": 7}]}
            finally:
                stop_owner.set()
                await owner_task
            for errors in (first_log, second_log):
                errors.seek(0)
                assert errors.read() == ""

    asyncio.run(asyncio.wait_for(run(), 30))
