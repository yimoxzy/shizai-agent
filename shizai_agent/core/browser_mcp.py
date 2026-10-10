"""MCP-facing lifecycle and evidence adapters for the companion browser extension."""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .browser_bridge import (
    BridgeClosedError,
    BridgeUnavailableError,
    BrowserBridgeError,
    BrowserBridgeWebSocketServer,
    RequestTimeoutError,
)
from .browser_relay import BrowserBridgeRelay
from .paths import ShizaiPaths

DEFAULT_EXTENSION_ID = "onlmcbjbkkjllainhobbehhaohnkloem"
DEFAULT_BRIDGE_PORT = 18768
DEFAULT_EXTENSION_VERSION = "0.1.11"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def default_config_path() -> Path:
    configured = str(os.environ.get("SHIZAI_BROWSER_BRIDGE_CONFIG") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "browser-bridge.json"
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return local_app_data / "Programs" / "ShizaiAgent" / "browser-bridge.json"


def design_owner_key(project_ref: str) -> str:
    """Derive a stable opaque ownership key without sending a filesystem path."""
    import hashlib
    return hashlib.sha256(str(project_ref).encode("utf-8")).hexdigest()


def load_bridge_config(path: Path | None = None) -> dict[str, Any]:
    config_path = (path or default_config_path()).resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("browser bridge config must be an object")
    extension_ids = payload.get("allowedExtensionIds")
    token = payload.get("installToken")
    port = payload.get("bridgePort")
    if not isinstance(extension_ids, list) or not extension_ids or any(not isinstance(item, str) for item in extension_ids):
        raise ValueError("browser bridge config requires allowedExtensionIds")
    if not isinstance(token, str) or not token:
        raise ValueError("browser bridge config requires installToken")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("browser bridge config requires bridgePort")
    return {**payload, "configPath": str(config_path)}


def installation_problem(config_path: Path) -> dict[str, Any] | None:
    """Diagnose managed installs without mistaking a development config for one."""
    root = config_path.resolve().parent
    if not (root / "version.json").is_file() and not (root / "release-manifest.json").is_file():
        return None
    missing = [name for name in ("shizai-agent-browser-host.exe", "browser-extension/manifest.json")
               if not (root / name).is_file()]
    if missing:
        return {"state": "installation_incomplete", "missing_files": missing,
                "install_command": "npx -y shizai-agent-mcp@latest install",
                "resolution": "Upgrade the installation; its browser extension or Native Messaging host files are missing."}
    if sys.platform == "win32":
        import winreg

        expected = root / "com.shizai.agent.browser.json"
        registrations = {}
        for browser, key in (("chrome", "Google\\Chrome"), ("edge", "Microsoft\\Edge")):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"Software\\{key}\\NativeMessagingHosts\\com.shizai.agent.browser") as handle:
                    registered, _ = winreg.QueryValueEx(handle, None)
                registrations[browser] = Path(registered).resolve() == expected and expected.is_file()
            except (OSError, TypeError, ValueError):
                registrations[browser] = False
        if not any(registrations.values()):
            return {"state": "native_host_registration_stale", "registrations": registrations,
                    "install_command": "npx -y shizai-agent-mcp@latest register browser",
                    "resolution": "Register the browser host from the persistent installation directory."}
    return None


class BrowserBridgeManager:
    """Own the loopback bridge or reuse its authenticated local owner."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self._lock = threading.Lock()
        self._server: BrowserBridgeWebSocketServer | BrowserBridgeRelay | None = None
        self._config: dict[str, Any] | None = None
        self._startup_error: str | None = None
        self._port_busy = False

    def _missing(self, state: str, *, error: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": "mcp_browser_bridge_status",
            "state": state,
            "connected": False,
            "requires_user_action": state in {"not_installed", "extension_missing"},
            "extension_id": DEFAULT_EXTENSION_ID,
        }
        if state == "not_installed":
            result["install_command"] = "npx -y shizai-agent-mcp@latest install --with-browser-extension"
        elif state == "extension_missing":
            result["retryable"] = True
            result["resolution"] = "Open Chrome with the companion extension enabled; it reconnects automatically."
            result["next_action"] = {"tool": "shizai_browser_status", "arguments": {"connect_wait": 2}}
        if error:
            result["error"] = error
        return result

    def ensure_started(self) -> BrowserBridgeWebSocketServer | BrowserBridgeRelay | None:
        with self._lock:
            if isinstance(self._server, BrowserBridgeRelay):
                try:
                    self._server.status_snapshot()
                    return self._server
                except BrowserBridgeError:
                    self._server = None
            elif self._server is not None:
                if self._server.alive:
                    return self._server
                self._server.stop()
                self._server = None
            self._port_busy = False
            config_path = self.config_path or default_config_path()
            if not config_path.is_file():
                self._startup_error = None
                return None
            try:
                config = load_bridge_config(config_path)
                self._config = config
                server = BrowserBridgeWebSocketServer(
                    allowed_extension_ids=config["allowedExtensionIds"],
                    install_token=config["installToken"],
                    port=int(config["bridgePort"]),
                    capabilities=config.get("capabilities", []),
                )
                try:
                    server.start()
                except BrowserBridgeError as exc:
                    cause = exc.__cause__
                    self._port_busy = isinstance(cause, OSError) and (
                        cause.errno == errno.EADDRINUSE or getattr(cause, "winerror", None) == 10048
                    )
                    if not self._port_busy:
                        raise
                    relay = BrowserBridgeRelay(port=int(config["bridgePort"]), install_token=config["installToken"])
                    relay.status_snapshot()
                    self._server = relay
                    self._startup_error = None
                    return relay
            except Exception as exc:
                self._startup_error = str(exc)
                return None
            self._config = config
            self._server = server
            self._startup_error = None
            return server

    def status(self, *, connect_wait: float = 0.0) -> dict[str, Any]:
        problem = installation_problem(self.config_path or default_config_path())
        if problem:
            # No browser operation is sent by a diagnostic; an old config alone
            # is not proof that the host and extension were installed.
            return {**self._missing(problem["state"]), **problem, "requires_user_action": True}
        server = self.ensure_started()
        if server is None:
            state = "bridge_in_use" if self._port_busy else "bridge_error" if self._startup_error else "not_installed"
            result = self._missing(state, error=self._startup_error)
            if self._port_busy:
                result["requires_user_action"] = True
                result["resolution"] = (
                    "The port owner cannot authenticate or support bridge sharing. "
                    "Update/restart the older MCP session or check that both sessions use the same bridge config."
                )
            return result
        deadline = time.monotonic() + max(0.0, connect_wait)
        try:
            snapshot = server.status_snapshot()
            while not snapshot["connected"] and time.monotonic() < deadline:
                time.sleep(0.05)
                snapshot = server.status_snapshot()
        except BrowserBridgeError:
            # The owner can exit between discovery and this snapshot. A status
            # read has no browser effect and is safe to retry on the next call.
            return {
                **self._missing("bridge_disconnected"),
                "retryable": True,
                "next_action": {"tool": "shizai_browser_status", "arguments": {"connect_wait": 2}},
            }
        result = {
            "kind": "mcp_browser_bridge_status", **snapshot,
            "connection_mode": "shared" if isinstance(server, BrowserBridgeRelay) else "owner",
        }
        if not snapshot["connected"]:
            result.update(self._missing("extension_missing"))
            result["bridge_listening"] = True
            result["port"] = server.port
        elif self._config:
            result["protocol_version"] = "1.0"
            result["config_path"] = self._config["configPath"]
        return result

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        server = self.ensure_started()
        if server is None or not server.connected:
            status = self.status(connect_wait=1.0)
            if not status["connected"]:
                return status
            server = self._server
        try:
            return server.client.call(method, params or {}, timeout=timeout)
        except BridgeUnavailableError:
            return {
                **self._missing("bridge_disconnected"),
                "request_method": method,
                "execution_outcome": "not_sent",
                "retryable": True,
                "next_action": {"tool": "shizai_browser_status", "arguments": {"connect_wait": 2}},
            }
        except (BridgeClosedError, RequestTimeoutError) as exc:
            # The browser may already have applied a click/input. Reconnect on
            # the next call, but never replay the operation or claim success.
            return {
                "kind": "mcp_browser_bridge_status",
                "state": "request_timeout" if isinstance(exc, RequestTimeoutError) else "bridge_disconnected",
                "connected": False,
                "request_method": method,
                "execution_outcome": "unknown",
                "retryable": False,
                "resolution": "Reconnect, capture the current page and inspect the effect before repeating the action.",
                "next_action": {"tool": "shizai_browser_status", "arguments": {"connect_wait": 2}},
            }

    def close(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.stop()


def _best_locator(point: dict[str, Any]) -> dict[str, Any]:
    candidates = [item for item in point.get("selector_candidates", []) if isinstance(item, dict)]
    usable = [
        item
        for item in candidates
        if str(item.get("selector") or "").strip()
        and int(item.get("visible_matches") or 0) > 0
    ]
    if not usable:
        raise ValueError("browser point has no visible reusable selector")
    return min(
        usable,
        key=lambda item: (
            int(item.get("visible_matches") or 0) != 1,
            ":nth-" in str(item.get("selector") or ""),
            len(str(item.get("selector") or "")),
        ),
    )


def _element_id(prefix: str, role: str, index: int) -> str:
    token = _SAFE_ID_RE.sub("-", role.strip()).strip("-").lower() or str(index + 1)
    return f"{prefix}-{token}"[:96]


def _selector_tag(selector: str) -> str:
    match = re.match(r"[A-Za-z][A-Za-z0-9_-]*", selector.lstrip())
    return match.group(0).lower() if match else "div"


def point_elements(
    result: dict[str, Any],
    *,
    page: dict[str, Any],
    prefix: str,
) -> list[dict[str, Any]]:
    """Convert verified point hits into native-import candidates."""

    records: list[dict[str, Any]] = []
    points = result.get("points") if isinstance(result, dict) else None
    if not isinstance(points, list) or not points:
        raise ValueError("inspect_points returned no points")
    for index, point in enumerate(points):
        if not isinstance(point, dict) or not point.get("found") or point.get("rejected"):
            raise ValueError(f"browser point {index} did not resolve to an allowed element")
        if point.get("point_is_match") is not True or point.get("visible") is not True:
            raise ValueError(f"browser point {index} was not visibly verified")
        locator = _best_locator(point)
        request_point = point.get("point") if isinstance(point.get("point"), dict) else {}
        role = str(request_point.get("role") or request_point.get("name") or f"element-{index + 1}")
        attributes = point.get("attributes") if isinstance(point.get("attributes"), dict) else {}
        # DataCollectionV5 repeats a one-node selector across the page. The
        # shared card selector is evidence for schema validation, but adding
        # it to every field creates a descendant chain that the native
        # runtime treats as a single row and returns only the header.
        records.append(
            {
                "id": str(request_point.get("id") or _element_id(prefix, role, index)),
                "name": role,
                "tag": str(point.get("tag") or "div"),
                "text": str(point.get("text") or "")[:500],
                "attributes": {
                    key: value
                    for key, value in {
                        "id": attributes.get("id"),
                        "name": attributes.get("name"),
                        "placeholder": attributes.get("placeholder"),
                        "aria-label": attributes.get("ariaLabel"),
                    }.items()
                    if value not in (None, "")
                },
                "css_selector": str(locator["selector"]),
                "page_title": str(page.get("title") or ""),
                "url": str(page.get("url") or ""),
                "mcpBrowserEvidence": {
                    "tabId": page.get("tabId"),
                    "screenshotId": page.get("screenshotId"),
                    "pageRevision": result.get("pageRevision") or page.get("pageRevision"),
                    "point": request_point,
                    "visibleMatches": locator.get("visible_matches"),
                    "pointIsMatch": True,
                },
            }
        )
    return records


def collection_elements(
    result: dict[str, Any],
    *,
    page: dict[str, Any],
    prefix: str,
) -> list[dict[str, Any]]:
    common = str(result.get("commonSelector") or "").strip()
    columns = result.get("columns")
    if not common or not isinstance(columns, list) or len(columns) < 2:
        raise ValueError("collection inference did not return a reusable repeated schema")
    if int(result.get("rowCount") or 0) < 2:
        raise ValueError("collection inference found fewer than two repeated rows")
    verification = result.get("verification") or {}
    if (
        result.get("verified") is not True
        or not isinstance(verification, dict)
        or verification.get("method") != "field-identity-per-row"
        or verification.get("verifiedRowCount") != result["rowCount"]
        or verification.get("candidateRowCount") != result["rowCount"]
    ):
        raise ValueError(
            "collection field identity was not verified for every row; "
            "load the updated browser extension and re-inspect the fields"
        )
    records = []
    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            raise ValueError(f"collection column {index} is invalid")
        name = str(column.get("name") or f"column-{index + 1}")
        relative = str(column.get("relativeSelector") or "").strip()
        if not relative:
            raise ValueError(f"collection column {name} has no relative selector")
        source_selector = str(column.get("sourceSelector") or "").strip()
        field_selector = str(column.get("fieldSelector") or "").strip()
        if not source_selector or not field_selector:
            raise ValueError(f"collection column {name} has no verified field selector")
        # Generalization must be checked in the browser. Removing positional
        # suffixes here can broaden a locator to unverified sibling elements.
        records.append(
            {
                "id": _element_id(prefix, name, index),
                "name": name,
                "tag": _selector_tag(field_selector),
                "css_selector": field_selector,
                "page_title": str(page.get("title") or ""),
                "url": str(page.get("url") or ""),
                "card_context": {"selector": common, "row_count": int(result.get("rowCount") or 0)},
                "mcpBrowserEvidence": {
                    "tabId": page.get("tabId"),
                    "screenshotId": page.get("screenshotId"),
                    "pageRevision": page.get("pageRevision"),
                    "commonSelector": common,
                    "relativeSelector": relative,
                    "sourceSelector": source_selector,
                    "fieldSelector": field_selector,
                    "verification": dict(verification),
                },
            }
        )
    return records


def bind_verified_elements(
    paths: ShizaiPaths,
    *,
    project_ref: str,
    step_id: str,
    elements: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    from . import project as project_core

    records = list(elements)
    imported = project_core.import_elements(
        paths,
        project_ref,
        records,
        capture_source="browser_bridge",
    )
    bound = project_core.set_rpa_step_elements(paths, project_ref, step_id, list(imported["imported"]))
    return {"imported": imported, "bound": bound}


__all__ = [
    "BrowserBridgeManager",
    "DEFAULT_BRIDGE_PORT",
    "DEFAULT_EXTENSION_ID",
    "bind_verified_elements",
    "collection_elements",
    "default_config_path",
    "load_bridge_config",
    "point_elements",
]
