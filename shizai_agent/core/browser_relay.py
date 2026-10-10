"""Authenticated MCP-to-MCP requests on the existing loopback bridge port.

Connections are short lived: closing a caller never shuts down the extension
or its owner. A lost request is never automatically replayed, especially a
click/input. The manager can elect a new owner on the *next* tool call.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from .browser_bridge import (
    DEFAULT_REQUEST_TIMEOUT,
    MAX_RELAY_TIMEOUT,
    RELAY_PATH,
    RELAY_VERSION,
    BridgeClosedError,
    BridgeUnavailableError,
    BrowserBridgeError,
    ProtocolError,
    RequestTimeoutError,
    validate_response,
)


class BrowserBridgeRelay:
    def __init__(self, *, port: int, install_token: str) -> None:
        self.port = port
        self._token = install_token
        self.client = self
        self._snapshot: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._snapshot.get("connected") is True

    def status_snapshot(self) -> dict[str, Any]:
        result = self.call("bridge_status", timeout=1.0)
        if not isinstance(result, dict) or not isinstance(result.get("connected"), bool):
            raise ProtocolError("invalid shared bridge status")
        self._snapshot = result
        return dict(result)

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        import websocket

        budget = DEFAULT_REQUEST_TIMEOUT if timeout is None else timeout
        if isinstance(budget, bool) or not math.isfinite(budget) or not 0 < budget <= MAX_RELAY_TIMEOUT:
            raise ValueError(f"bridge timeout must be between 0 and {MAX_RELAY_TIMEOUT} seconds")
        connection = None
        request_sent = False
        deadline = time.monotonic() + budget + 1.0
        try:
            connection = websocket.create_connection(
                f"ws://127.0.0.1:{self.port}{RELAY_PATH}",
                timeout=min(budget, 1.0),
                suppress_origin=True,
                http_no_proxy=["127.0.0.1"],
            )
            connection.send(json.dumps({"type": "mcpHello", "protocolVersion": RELAY_VERSION, "token": self._token}))
            ack = json.loads(connection.recv())
            if not isinstance(ack, dict) or ack.get("type") != "mcpHelloAck" or ack.get("protocolVersion") != RELAY_VERSION:
                raise ProtocolError("port owner does not support this MCP relay version")
            request_id = str(uuid.uuid4())
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            # From this point a transport failure has an unknown execution outcome.
            request_sent = True
            connection.send(json.dumps({
                "protocolVersion": RELAY_VERSION,
                "requestId": request_id,
                "method": method,
                "params": params or {},
                "timeout": budget,
            }, ensure_ascii=False))
            response = validate_response(json.loads(connection.recv()), request_id)
        except (OSError, websocket.WebSocketException, ValueError, ProtocolError) as exc:
            # Avoid echoing handshake/config data from third-party error messages.
            error = BridgeClosedError if request_sent else BridgeUnavailableError
            raise error("shared browser bridge connection lost" if request_sent else "shared browser bridge unavailable") from exc
        finally:
            if connection is not None:
                connection.close(timeout=0.1)
        if not response["ok"]:
            failure = response["error"]
            error_class = {
                "BridgeClosedError": BridgeClosedError,
                "RequestTimeoutError": RequestTimeoutError,
                "ProtocolError": ProtocolError,
            }.get(failure.get("code"), BrowserBridgeError)
            raise error_class(str(failure.get("message") or "browser bridge request failed"))
        return response.get("result")

    def stop(self) -> None:
        # There is no owned extension connection or background thread to stop.
        self._snapshot = {}
