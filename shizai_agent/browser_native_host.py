"""Chrome Native Messaging bootstrap for the MCP browser companion.

Native Messaging is intentionally one-shot here. Chrome supplies the
extension origin as a process argument; after validating it, this host sends
the loopback WebSocket URL and installation token, then exits. The extension
owns the long-lived connection to the MCP WebSocket server.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from .core.browser_bridge import make_bootstrap, native_write_frame, validate_extension_origin


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("browser bridge config must be an object")
    return value


def _bridge_is_listening(port: int, *, timeout: float = 0.15) -> bool:
    """Probe the loopback listener before telling Chrome to open a WebSocket."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def bootstrap_for_origin(
    origin: str,
    config: dict[str, Any],
    *,
    require_listening: bool = False,
) -> dict[str, Any]:
    extension_ids = config.get("allowedExtensionIds", [])
    token = config.get("installToken")
    port = config.get("bridgePort")
    if not isinstance(extension_ids, list) or not isinstance(token, str) or not token:
        raise ValueError("config must contain allowedExtensionIds and installToken")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError("config must contain a running bridgePort")
    extension_id = validate_extension_origin(origin, extension_ids)
    if require_listening and not _bridge_is_listening(port):
        return {
            "protocolVersion": "1.0",
            "type": "bridgeUnavailable",
            "extensionId": extension_id,
            "retryAfterMs": 500,
        }
    return make_bootstrap(extension_id=extension_id, bridge_port=port, install_token=token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shizai MCP browser Native Messaging bootstrap")
    installed_config = (Path(sys.executable).resolve().parent / "browser-bridge.json") if getattr(sys, "frozen", False) else (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "ShizaiAgent" / "browser-bridge.json"
    )
    default_config = Path(
        os.environ.get(
            "SHIZAI_BROWSER_BRIDGE_CONFIG",
            str(installed_config),
        )
    )
    parser.add_argument("--config", type=Path, default=default_config)
    args, extras = parser.parse_known_args(argv)
    config = _load_config(args.config)
    origin = next((item for item in extras if item.startswith("chrome-extension://")), None)
    if origin is None:
        raise ValueError("Chrome Native Messaging origin argument is required")
    native_write_frame(
        sys.stdout.buffer,
        bootstrap_for_origin(origin, config, require_listening=True),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
