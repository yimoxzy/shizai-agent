"""Authenticated bridge primitives for the MCP browser companion extension.

This module deliberately has no dependency on Chrome or on the existing
Shizai desktop extension.  The extension talks to a Native Messaging host;
the MCP process can use :class:`BrowserBridgeClient` with an injected
transport.  Keeping framing, authentication, and request correlation here
makes the protocol straightforward to test without starting a browser.

The host must authenticate both values supplied by the extension:
``extensionId`` must be in the configured allow-list and ``token`` must match
the per-installation secret.  A browser extension id is not a secret.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import socket
import struct
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

PROTOCOL_VERSION = "1.0"
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 15.0
MAX_EXTENSION_ID_LENGTH = 64
EXTENSION_ORIGIN_PREFIX = "chrome-extension://"
RELAY_PATH = "/mcp"
RELAY_VERSION = "1.0"
MAX_RELAY_TIMEOUT = 60.0


class BrowserBridgeError(RuntimeError):
    """Base class for bridge failures."""


class AuthorizationError(BrowserBridgeError):
    """The peer did not prove that it is the installed companion extension."""


class ProtocolError(BrowserBridgeError):
    """A message was malformed or used an unsupported protocol version."""


class RequestTimeoutError(BrowserBridgeError):
    """The peer did not answer a request before its deadline."""


class BridgeClosedError(BrowserBridgeError):
    """The transport closed while a request was in flight."""


class BridgeUnavailableError(BrowserBridgeError):
    """The optional WebSocket transport is not available."""


def generate_install_token() -> str:
    """Return a URL-safe secret suitable for one local installation."""

    return secrets.token_urlsafe(32)


def _as_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object")
    return value


def _text(value: Any, *, label: str, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ProtocolError(f"{label} must be a non-empty string")
    return value


def validate_peer_hello(
    message: Mapping[str, Any],
    *,
    allowed_extension_ids: Iterable[str],
    expected_token: str,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Validate and normalize the extension's first handshake message.

    The caller should send the returned ``session`` in its handshake reply.
    No network address is treated as authentication: the extension id and
    random installation token are both required.
    """

    data = _as_object(message, label="hello")
    if data.get("type") != "hello":
        raise ProtocolError("first message must have type 'hello'")
    version = data.get("protocolVersion")
    if version != protocol_version:
        raise ProtocolError(f"unsupported protocol version: {version!r}")
    extension_id = _text(data.get("extensionId"), label="extensionId", max_length=MAX_EXTENSION_ID_LENGTH)
    allowed = {item for item in allowed_extension_ids if isinstance(item, str) and item}
    if extension_id not in allowed:
        raise AuthorizationError("extension id is not authorized")
    token = data.get("token")
    if not isinstance(token, str) or not secrets.compare_digest(token, expected_token):
        raise AuthorizationError("invalid installation token")
    client_id = _text(data.get("clientId", "anonymous"), label="clientId", max_length=128)
    extension_version = _text(data.get("extensionVersion", "unknown"), label="extensionVersion", max_length=64)
    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise ProtocolError("capabilities must be a list of strings")
    return {
        "extensionId": extension_id,
        "clientId": client_id,
        "extensionVersion": extension_version,
        "capabilities": list(dict.fromkeys(capabilities)),
        "protocolVersion": protocol_version,
    }


def extension_id_from_origin(origin: str) -> str:
    """Extract and validate an extension id from Chrome's Origin value."""

    if not isinstance(origin, str) or not origin.startswith(EXTENSION_ORIGIN_PREFIX):
        raise AuthorizationError("Native Messaging origin is not a chrome-extension origin")
    value = origin[len(EXTENSION_ORIGIN_PREFIX) :].rstrip("/")
    if "/" in value or not value or len(value) > MAX_EXTENSION_ID_LENGTH:
        raise AuthorizationError("invalid extension origin")
    return value


def validate_extension_origin(origin: str, allowed_extension_ids: Iterable[str]) -> str:
    extension_id = extension_id_from_origin(origin)
    if extension_id not in set(allowed_extension_ids):
        raise AuthorizationError("extension origin is not authorized")
    return extension_id


def make_bootstrap(*, extension_id: str, bridge_port: int, install_token: str) -> dict[str, Any]:
    """Build the one-shot Native Messaging bootstrap sent to the extension."""

    if not (1 <= bridge_port <= 65535):
        raise ValueError("bridge_port must be a TCP port")
    if not install_token:
        raise ValueError("install_token is required")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "type": "bootstrap",
        "extensionId": extension_id,
        "wsUrl": f"ws://127.0.0.1:{bridge_port}",
        "token": install_token,
    }


def make_hello_ack(*, session_id: str, extension_version: str, capabilities: Iterable[str]) -> dict[str, Any]:
    """Build the deterministic host response to a validated hello."""

    return {
        "type": "helloAck",
        "protocolVersion": PROTOCOL_VERSION,
        "sessionId": session_id,
        "extensionVersion": extension_version,
        "capabilities": sorted({str(item) for item in capabilities if item}),
    }


def validate_response(message: Any, request_id: str) -> dict[str, Any]:
    """Validate one response and ensure it belongs to ``request_id``."""

    data = _as_object(message, label="response")
    if data.get("protocolVersion") != PROTOCOL_VERSION:
        raise ProtocolError("response protocol version mismatch")
    if data.get("requestId") != request_id:
        raise ProtocolError("response requestId does not match request")
    if not isinstance(data.get("ok"), bool):
        raise ProtocolError("response ok must be boolean")
    if not data["ok"] and not isinstance(data.get("error"), dict):
        raise ProtocolError("failed response must include an error object")
    return data


def _native_encode(message: Mapping[str, Any], *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> bytes:
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message is not JSON serializable") from exc
    if len(payload) > max_bytes:
        raise ProtocolError("message exceeds maximum size")
    return struct.pack("<I", len(payload)) + payload


def native_read_frame(reader: Any, *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    """Read one Chrome Native Messaging frame from a binary stream.

    ``None`` means clean EOF.  The function intentionally rejects truncated
    frames and oversized lengths before allocating the body.
    """

    header = reader.read(4)
    if header in (b"", None):
        return None
    if len(header) != 4:
        raise ProtocolError("truncated native messaging header")
    (length,) = struct.unpack("<I", header)
    if length > max_bytes:
        raise ProtocolError("native messaging frame exceeds maximum size")
    body = reader.read(length)
    if body is None or len(body) != length:
        raise ProtocolError("truncated native messaging body")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("native messaging body is not valid JSON") from exc
    return _as_object(value, label="native message")


def native_write_frame(writer: Any, message: Mapping[str, Any], *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
    """Write and flush one Chrome Native Messaging frame."""

    writer.write(_native_encode(message, max_bytes=max_bytes))
    flush = getattr(writer, "flush", None)
    if callable(flush):
        flush()


class BridgeTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None


class RequestBroker:
    """Correlate asynchronous responses without trusting response order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._closed = False

    def create(self, request_id: str) -> _PendingRequest:
        with self._lock:
            if self._closed:
                raise BridgeClosedError("bridge is closed")
            pending = _PendingRequest()
            self._pending[request_id] = pending
            return pending

    def resolve(self, message: Any) -> bool:
        request_id = message.get("requestId") if isinstance(message, dict) else None
        if not isinstance(request_id, str):
            return False
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            pending.response = dict(message)
            pending.event.set()
            return True

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def close(self, error: BaseException | None = None) -> None:
        with self._lock:
            self._closed = True
            self._fail_pending(error or BridgeClosedError("bridge is closed"))

    def fail_pending(self, error: BaseException) -> None:
        """Fail this connection's requests without preventing a reconnect."""
        with self._lock:
            self._fail_pending(error)

    def _fail_pending(self, error: BaseException) -> None:
        # Caller holds the lock. Requests remain until their callers unwind.
        for pending in self._pending.values():
            if not pending.event.is_set():
                pending.error = error
                pending.event.set()


class BrowserBridgeClient:
    """Synchronous MCP-facing client over an injected message transport.

    A reader owned by the transport should call :meth:`feed` for every
    inbound message.  This permits multiple in-flight requests and keeps the
    caller independent of Chrome's Native Messaging process model.
    """

    def __init__(
        self,
        transport: BridgeTransport,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        capabilities: Iterable[str] = (),
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.transport = transport
        self.timeout = timeout
        self.capabilities = frozenset(str(item) for item in capabilities if item)
        self.broker = RequestBroker()
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def feed(self, message: Any) -> bool:
        """Deliver an inbound response; return false for an unknown id."""

        return self.broker.resolve(message)

    def request(self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        method = _text(method, label="method", max_length=128)
        request_id = str(uuid.uuid4())
        pending = self.broker.create(request_id)
        message = {
            "protocolVersion": PROTOCOL_VERSION,
            "requestId": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        try:
            self.transport.send(message)
            wait_for = self.timeout if timeout is None else timeout
            if wait_for <= 0 or not pending.event.wait(wait_for):
                raise RequestTimeoutError(f"timed out waiting for {method}")
            if pending.error:
                raise pending.error
            response = validate_response(pending.response, request_id)
            if not response["ok"]:
                error = response["error"]
                code = str(error.get("code") or "browser_bridge_request_failed")
                message = str(error.get("message") or "").strip()
                raise BrowserBridgeError(f"{code}: {message}" if message else code)
            return response.get("result")
        finally:
            self.broker.remove(request_id)

    def call(self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """Stable MCP-facing name for one request/response call."""

        return self.request(method, params, timeout=timeout)

    def status(self, *, timeout: float | None = None) -> Any:
        """Return extension/bridge status through the standard capability call."""

        connected = getattr(self.transport, "connected", None)
        if connected is False:
            status_reader = getattr(self.transport, "status", None)
            if callable(status_reader):
                return status_reader()
            return {"state": "extension_missing", "connected": False}
        return self.call("status", timeout=timeout)

    def close(self) -> None:
        self.broker.close()
        self.transport.close()


class BrowserBridgeWebSocketServer:
    """Loopback WebSocket server with an extension-initiated connection.

    ``websockets`` is an optional runtime dependency.  The server is started
    in a private asyncio thread so synchronous MCP code can keep using the
    stable :meth:`BrowserBridgeClient.call` API.  Only 127.0.0.1 is bound;
    Origin and the token are both checked before the connection is accepted.
    """

    def __init__(
        self,
        *,
        allowed_extension_ids: Iterable[str],
        install_token: str,
        capabilities: Iterable[str] = (),
        host: str = "127.0.0.1",
        port: int = 0,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("browser bridge may bind only to loopback")
        if not install_token:
            raise ValueError("install_token is required")
        self.allowed_extension_ids = frozenset(allowed_extension_ids)
        self.install_token = install_token
        self.capabilities = frozenset(str(item) for item in capabilities if item)
        self.host = host
        self.port = port
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._startup_error: BaseException | None = None
        self._connection: Any = None
        self._peer: dict[str, Any] | None = None
        self.client = BrowserBridgeClient(_WebSocketTransport(self), timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._peer is not None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status_snapshot(self) -> dict[str, Any]:
        if not self.connected:
            return {"state": "extension_missing", "connected": False, "port": self.port}
        return {
            "state": "ready",
            "connected": True,
            "extensionId": self._peer.get("extensionId") if self._peer else None,
            "extensionVersion": self._peer.get("extensionVersion") if self._peer else None,
            "capabilities": sorted(self._peer.get("capabilities", [])) if self._peer else [],
            "port": self.port,
        }

    def start(self, *, timeout: float = 5.0) -> BrowserBridgeClient:
        if self._thread and self._thread.is_alive():
            return self.client
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="shizai-browser-bridge", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise BridgeUnavailableError("timed out starting browser bridge WebSocket server")
        if self._startup_error:
            raise BridgeUnavailableError(str(self._startup_error)) from self._startup_error
        return self.client

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # surfaced by start()
            self._startup_error = exc
            self._ready.set()

    async def _serve(self) -> None:
        try:
            import websockets
        except ImportError:
            self._startup_error = BridgeUnavailableError(
                "WebSocket bridge requires optional dependency 'websockets'"
            )
            self._ready.set()
            return
        self._loop = asyncio.get_running_loop()
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            origins=None,
            max_size=16 * 1024 * 1024,
            max_queue=4,
            close_timeout=1,
        )
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])
        self._ready.set()
        while not self._stop.is_set():
            await asyncio.sleep(0.05)
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket: Any, *_args: Any) -> None:
        try:
            if websocket.path == RELAY_PATH:
                await self._relay_handler(websocket)
            else:
                await self._extension_handler(websocket)
        except (AuthorizationError, ProtocolError, ValueError, asyncio.TimeoutError):
            # Rejected peers aren't server faults; never log their supplied token.
            await websocket.close(code=1008, reason="bridge handshake or request rejected")
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed

            if not isinstance(exc, ConnectionClosed):
                raise

    async def _relay_handler(self, websocket: Any) -> None:
        # Browser pages/extensions must use the extension endpoint. Only a local
        # MCP peer with this installation's secret can use the relay endpoint.
        if websocket.request_headers.get("Origin") is not None:
            raise AuthorizationError("MCP relay does not accept a browser Origin")
        hello = _as_object(json.loads(await asyncio.wait_for(websocket.recv(), 3.0)), label="relay hello")
        token = hello.get("token")
        if (
            hello.get("type") != "mcpHello"
            or hello.get("protocolVersion") != RELAY_VERSION
            or not isinstance(token, str)
            or not secrets.compare_digest(token, self.install_token)
        ):
            raise AuthorizationError("MCP relay authentication failed")
        await websocket.send(json.dumps({"type": "mcpHelloAck", "protocolVersion": RELAY_VERSION}))
        request = _as_object(json.loads(await asyncio.wait_for(websocket.recv(), 3.0)), label="relay request")
        request_id = _text(request.get("requestId"), label="requestId", max_length=128)
        method = _text(request.get("method"), label="method", max_length=128)
        params = _as_object(request.get("params"), label="params")
        timeout = request.get("timeout", DEFAULT_REQUEST_TIMEOUT)
        if (
            request.get("protocolVersion") != RELAY_VERSION
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_RELAY_TIMEOUT
        ):
            raise ProtocolError("invalid relay timeout or protocol version")
        response: dict[str, Any] = {"requestId": request_id, "protocolVersion": RELAY_VERSION}
        try:
            result = (
                self.status_snapshot()
                if method == "bridge_status"
                else await asyncio.to_thread(self.client.call, method, params, timeout=timeout)
            )
            response.update(ok=True, result=result)
        except BrowserBridgeError as exc:
            response.update(ok=False, error={"code": type(exc).__name__, "message": str(exc)})
        await websocket.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")))

    async def _extension_handler(self, websocket: Any) -> None:
        origin = None
        headers = getattr(websocket, "request_headers", None)
        if headers is not None:
            origin = headers.get("Origin")
        extension_id = validate_extension_origin(origin or "", self.allowed_extension_ids)
        if self.connected:
            await websocket.close(code=1013, reason="browser bridge already has a connection")
            return
        raw = await asyncio.wait_for(websocket.recv(), 3.0)
        if not isinstance(raw, str):
            raise ProtocolError("WebSocket hello must be text JSON")
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError("WebSocket hello is not valid JSON") from exc
        if not isinstance(hello, dict) or hello.get("extensionId") != extension_id:
            raise AuthorizationError("hello extension id does not match Origin")
        peer = validate_peer_hello(hello, allowed_extension_ids=[extension_id], expected_token=self.install_token)
        if self.connected:
            await websocket.close(code=1013, reason="browser bridge already has a connection")
            return
        self._connection = websocket
        self._peer = peer
        session_id = str(uuid.uuid4())
        try:
            await websocket.send(
                json.dumps(
                    make_hello_ack(
                        session_id=session_id,
                        extension_version=peer["extensionVersion"],
                        capabilities=self.capabilities,
                    ),
                    separators=(",", ":"),
                )
            )
            async for raw_response in websocket:
                if not isinstance(raw_response, str):
                    raise ProtocolError("WebSocket response must be text JSON")
                message = json.loads(raw_response)
                if isinstance(message, dict) and message.get("type") == "keepalive":
                    continue
                self.client.feed(message)
        finally:
            self._connection = None
            self._peer = None
            self.client.broker.fail_pending(BridgeClosedError("browser extension disconnected; request outcome unknown"))

    def _send(self, message: dict[str, Any]) -> None:
        from websockets.exceptions import ConnectionClosed

        connection, loop = self._connection, self._loop
        if connection is None or loop is None or loop.is_closed():
            raise BridgeClosedError("browser extension is not connected")
        operation = connection.send(json.dumps(message, separators=(",", ":")))
        try:
            future = asyncio.run_coroutine_threadsafe(operation, loop)
        except RuntimeError as exc:
            operation.close()
            raise BridgeClosedError("browser bridge stopped while sending") from exc
        try:
            future.result(timeout=self.timeout)
        except TimeoutError as exc:
            future.cancel()
            raise RequestTimeoutError("browser bridge send timed out; request outcome unknown") from exc
        except (ConnectionClosed, OSError) as exc:
            raise BridgeClosedError("browser extension disconnected while sending; request outcome unknown") from exc

    def stop(self) -> None:
        self._stop.set()
        self.client.broker.close()
        if self._loop and self._thread and self._thread.is_alive():
            self._loop.call_soon_threadsafe(lambda: None)
            self._thread.join(timeout=2.0)


class _WebSocketTransport:
    def __init__(self, server: BrowserBridgeWebSocketServer) -> None:
        self.server = server

    @property
    def connected(self) -> bool:
        return self.server.connected

    def send(self, message: dict[str, Any]) -> None:
        self.server._send(message)

    def status(self) -> dict[str, Any]:
        return self.server.status_snapshot()

    def close(self) -> None:
        self.server.stop()


class NativeMessagingHost:
    """Small, dependency-free Chrome Native Messaging host dispatcher."""

    def __init__(
        self,
        *,
        allowed_extension_ids: Iterable[str],
        install_token: str,
        handlers: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
        capabilities: Iterable[str] = (),
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if not install_token:
            raise ValueError("install_token is required")
        self.allowed_extension_ids = frozenset(allowed_extension_ids)
        self.install_token = install_token
        self.handlers = dict(handlers or {})
        self.capabilities = frozenset(str(item) for item in capabilities if item)
        self.max_message_bytes = max_message_bytes
        self.session_id: str | None = None
        self.extension_id: str | None = None

    def dispatch(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if self.session_id is None:
            peer = validate_peer_hello(
                message,
                allowed_extension_ids=self.allowed_extension_ids,
                expected_token=self.install_token,
            )
            self.extension_id = peer["extensionId"]
            self.session_id = str(uuid.uuid4())
            return make_hello_ack(
                session_id=self.session_id,
                extension_version=peer["extensionVersion"],
                capabilities=self.capabilities,
            )
        data = _as_object(message, label="request")
        if data.get("protocolVersion") != PROTOCOL_VERSION:
            raise ProtocolError("request protocol version mismatch")
        request_id = _text(data.get("requestId"), label="requestId", max_length=128)
        method = _text(data.get("method"), label="method", max_length=128)
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("request params must be an object")
        handler = self.handlers.get(method)
        if handler is None:
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {"code": "unknown_method", "message": method},
            }
        try:
            result = handler(params)
        except Exception as exc:  # Handlers must not tear down the native host process.
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {"code": "handler_error", "message": str(exc)},
            }
        return {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": True, "result": result}

    def serve(self, reader: Any, writer: Any) -> None:
        """Serve frames until Chrome closes stdin."""

        while True:
            message = native_read_frame(reader, max_bytes=self.max_message_bytes)
            if message is None:
                return
            response = self.dispatch(message)
            native_write_frame(writer, response, max_bytes=self.max_message_bytes)


def loopback_bind_address() -> tuple[str, int]:
    """Return a safe default for an optional TCP transport."""

    return ("127.0.0.1", 0)


def create_loopback_listener(*, token: str) -> socket.socket:
    """Create an unadvertised loopback listener; callers must authenticate token.

    This helper intentionally binds only to 127.0.0.1.  It does not implement
    a public listener or infer trust from a source address.
    """

    if not token:
        raise ValueError("token is required for loopback transport")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(loopback_bind_address())
    listener.listen(1)
    return listener


__all__ = [
    "AuthorizationError",
    "BridgeClosedError",
    "BridgeUnavailableError",
    "BrowserBridgeClient",
    "BrowserBridgeWebSocketServer",
    "BrowserBridgeError",
    "NativeMessagingHost",
    "ProtocolError",
    "RequestBroker",
    "RequestTimeoutError",
    "create_loopback_listener",
    "extension_id_from_origin",
    "generate_install_token",
    "make_hello_ack",
    "make_bootstrap",
    "native_read_frame",
    "native_write_frame",
    "validate_peer_hello",
    "validate_extension_origin",
    "validate_response",
]
