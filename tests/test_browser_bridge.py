from __future__ import annotations

import asyncio
import io
import json
import threading
import time

import pytest

from shizai_agent.browser_native_host import bootstrap_for_origin
from shizai_agent.core.browser_bridge import (
    AuthorizationError,
    BrowserBridgeClient,
    BrowserBridgeError,
    BrowserBridgeWebSocketServer,
    NativeMessagingHost,
    ProtocolError,
    RequestTimeoutError,
    generate_install_token,
    make_bootstrap,
    native_read_frame,
    native_write_frame,
    validate_extension_origin,
    validate_peer_hello,
)


def test_native_frames_round_trip_and_reject_truncation() -> None:
    stream = io.BytesIO()
    native_write_frame(stream, {"protocolVersion": "1.0", "ok": True})
    stream.seek(0)
    assert native_read_frame(stream) == {"protocolVersion": "1.0", "ok": True}
    with pytest.raises(ProtocolError, match="truncated"):
        native_read_frame(io.BytesIO(b"\x05\x00\x00\x00{}"))


def test_hello_requires_allowlisted_extension_and_token() -> None:
    token = generate_install_token()
    hello = {
        "type": "hello",
        "protocolVersion": "1.0",
        "extensionId": "abc",
        "extensionVersion": "2.3.4",
        "token": token,
        "capabilities": ["capture_viewport"],
    }
    peer = validate_peer_hello(hello, allowed_extension_ids=["abc"], expected_token=token)
    assert peer["extensionVersion"] == "2.3.4"
    with pytest.raises(AuthorizationError):
        validate_peer_hello({**hello, "extensionId": "other"}, allowed_extension_ids=["abc"], expected_token=token)
    with pytest.raises(AuthorizationError):
        validate_peer_hello({**hello, "token": "wrong"}, allowed_extension_ids=["abc"], expected_token=token)


def test_native_origin_and_bootstrap_are_allowlisted() -> None:
    assert validate_extension_origin("chrome-extension://abc/", ["abc"]) == "abc"
    with pytest.raises(AuthorizationError):
        validate_extension_origin("https://127.0.0.1", ["abc"])
    with pytest.raises(AuthorizationError):
        validate_extension_origin("chrome-extension://other/", ["abc"])
    assert make_bootstrap(extension_id="abc", bridge_port=43121, install_token="tok") == {
        "protocolVersion": "1.0",
        "type": "bootstrap",
        "extensionId": "abc",
        "wsUrl": "ws://127.0.0.1:43121",
        "token": "tok",
    }
    assert bootstrap_for_origin(
        "chrome-extension://abc/",
        {"allowedExtensionIds": ["abc"], "installToken": "tok", "bridgePort": 43121},
    )["wsUrl"] == "ws://127.0.0.1:43121"


def test_native_bootstrap_waits_for_a_listening_bridge(monkeypatch) -> None:
    monkeypatch.setattr(
        "shizai_agent.browser_native_host._bridge_is_listening",
        lambda _port: False,
    )
    result = bootstrap_for_origin(
        "chrome-extension://abc/",
        {"allowedExtensionIds": ["abc"], "installToken": "tok", "bridgePort": 43121},
        require_listening=True,
    )
    assert result == {
        "protocolVersion": "1.0",
        "type": "bridgeUnavailable",
        "extensionId": "abc",
        "retryAfterMs": 500,
    }
    assert "wsUrl" not in result


def test_host_dispatches_unified_envelope() -> None:
    token = "secret"
    host = NativeMessagingHost(
        allowed_extension_ids=["abc"],
        install_token=token,
        capabilities=["status"],
        handlers={"status": lambda params: {"ready": True, "params": params}},
    )
    ack = host.dispatch(
        {
            "type": "hello",
            "protocolVersion": "1.0",
            "extensionId": "abc",
            "extensionVersion": "1.2.0",
            "token": token,
            "capabilities": [],
        }
    )
    assert ack["extensionVersion"] == "1.2.0"
    response = host.dispatch({"protocolVersion": "1.0", "requestId": "r1", "method": "status", "params": {}})
    assert response == {
        "protocolVersion": "1.0",
        "requestId": "r1",
        "ok": True,
        "result": {"ready": True, "params": {}},
    }


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client: BrowserBridgeClient | None = None

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def close(self) -> None:
        pass


def test_client_correlates_request_id_and_exposes_status() -> None:
    transport = _FakeTransport()
    client = BrowserBridgeClient(transport, timeout=0.2)

    def answer() -> None:
        while not transport.sent:
            time.sleep(0.001)
        request = transport.sent[0]
        client.feed({"protocolVersion": "1.0", "requestId": request["requestId"], "ok": True, "result": {"ready": True}})

    thread = threading.Thread(target=answer)
    thread.start()
    assert client.status() == {"ready": True}
    thread.join()
    assert transport.sent[0]["method"] == "status"


def test_client_times_out_and_unknown_response_is_ignored() -> None:
    transport = _FakeTransport()
    client = BrowserBridgeClient(transport, timeout=0.01)
    assert client.feed({"protocolVersion": "1.0", "requestId": "missing", "ok": True, "result": {}}) is False
    with pytest.raises(RequestTimeoutError):
        client.call("capture_viewport")


def test_client_preserves_extension_error_code() -> None:
    transport = _FakeTransport()
    client = BrowserBridgeClient(transport, timeout=0.2)

    def answer() -> None:
        while not transport.sent:
            time.sleep(0.001)
        request = transport.sent[0]
        client.feed({
            "protocolVersion": "1.0",
            "requestId": request["requestId"],
            "ok": False,
            "error": {"code": "target_tab_not_active"},
        })

    thread = threading.Thread(target=answer)
    thread.start()
    with pytest.raises(BrowserBridgeError, match="target_tab_not_active"):
        client.call("capture_viewport")
    thread.join()


def test_websocket_server_reports_extension_missing_without_waiting() -> None:
    server = BrowserBridgeWebSocketServer(allowed_extension_ids=["abc"], install_token="secret")
    assert server.client.status() == {"state": "extension_missing", "connected": False, "port": 0}


def test_websocket_server_handshake_and_request_round_trip() -> None:
    async def scenario() -> None:
        import websockets

        server = BrowserBridgeWebSocketServer(allowed_extension_ids=["abc"], install_token="secret")
        server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.port}", origin="chrome-extension://abc") as socket:
                await socket.send(json.dumps({
                    "type": "hello",
                    "protocolVersion": "1.0",
                    "extensionId": "abc",
                    "extensionVersion": "0.1.0",
                    "token": "secret",
                    "capabilities": ["list_tabs"],
                }))
                acknowledgement = json.loads(await socket.recv())
                assert acknowledgement["type"] == "helloAck"
                await socket.send(json.dumps({
                    "type": "keepalive",
                    "protocolVersion": "1.0",
                    "sentAt": 1,
                }))
                request_task = asyncio.create_task(asyncio.to_thread(server.client.call, "list_tabs", {}))
                request = json.loads(await socket.recv())
                await socket.send(json.dumps({
                    "protocolVersion": "1.0",
                    "requestId": request["requestId"],
                    "ok": True,
                    "result": {"tabs": [{"tabId": 7, "url": "https://example.test"}]},
                }))
                assert await request_task == {"tabs": [{"tabId": 7, "url": "https://example.test"}]}

                large_png = "A" * (2 * 1024 * 1024)
                capture_task = asyncio.create_task(asyncio.to_thread(server.client.call, "capture_viewport", {}))
                capture_request = json.loads(await socket.recv())
                await socket.send(json.dumps({
                    "protocolVersion": "1.0",
                    "requestId": capture_request["requestId"],
                    "ok": True,
                    "result": {"dataUrl": "data:image/png;base64," + large_png},
                }))
                capture = await capture_task
                assert len(capture["dataUrl"]) > 2 * 1024 * 1024
                assert server.status_snapshot()["extensionVersion"] == "0.1.0"
        finally:
            server.stop()

    asyncio.run(scenario())
