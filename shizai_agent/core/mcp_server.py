"""MCP server exposing the same core surface as the Shizai CLI.

The low-level MCP server is used instead of FastMCP because the bundled desktop
runtime currently carries a FastMCP/Pydantic combination that cannot build tool
schemas for ordinary annotated parameters. The protocol surface stays standard.
"""

import hashlib
import json
import os
import re
import socket
import sys
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import anyio

from shizai_agent import __version__

from . import agent as agent_core
from . import component_catalog as component_catalog_core
from . import debug_session as debug_core
from . import project as project_core
from .browser_bridge import BrowserBridgeError
from .browser_mcp import (
    BrowserBridgeManager,
    bind_verified_elements,
    collection_elements,
    design_owner_key,
    point_elements,
)
from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent
from .extension_bridge import ShizaiExtensionBridge, open_current_chrome, set_rpa_ui_feedback
from .native_ui import NativeMask, NativeScanDemo, NativeSmallCard, NativeTakeoverFrame
from .paths import ShizaiPaths
from .std_streams import install_shutdown_safe_stderr
from .workflow_contracts import RPA_AUTHORING_INSTRUCTIONS, RPA_AUTHORING_TOOLS, rpa_plan_schema
from .workflow_edit import WorkflowEditError, read_edit_context
from .workflow_responses import authoring_result, compact_design_result

try:
    from mcp.server.lowlevel import Server
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations
except ImportError:  # pragma: no cover - exercised only when the optional dependency is absent
    Server = None  # type: ignore[assignment,misc]
    InitializationOptions = None  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]
    ImageContent = None  # type: ignore[assignment,misc]
    TextContent = None  # type: ignore[assignment,misc]
    Tool = None  # type: ignore[assignment,misc]


def _paths() -> ShizaiPaths:
    return ShizaiPaths.from_environment()


_DESKTOP_CLIENT: ShizaiDesktopAgent | None = None
_BROWSER_BRIDGE_MANAGER: BrowserBridgeManager | None = None
_ACTIVE_RPA_DESIGN_UI: dict[str, dict[str, Any]] = {}
_LATEST_RPA_DESIGN_REF: str | None = None
_RPA_SMALL_CARD_OPEN = False
# Keep the immutable analysis available for the next MCP call.  Models often
# compact a large tool result while composing a plan; the id is enough to
# recover the original contract without asking them to re-send every field.
_RPA_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}
_RPA_ANALYSIS_REQUEST_COUNTS: dict[str, int] = {}


def _analysis_fingerprint(analysis: dict[str, Any]) -> str:
    payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _cache_rpa_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Attach a stable fingerprint and retain one immutable analysis copy."""
    enriched = deepcopy(analysis)
    enriched["analysis_fingerprint"] = _analysis_fingerprint(enriched)
    analysis_id = str(enriched.get("analysis_id") or "").strip()
    if analysis_id:
        _RPA_ANALYSIS_CACHE[analysis_id] = deepcopy(enriched)
        # Bound memory for long-lived MCP processes while retaining recent work.
        if len(_RPA_ANALYSIS_CACHE) > 64:
            oldest = next(iter(_RPA_ANALYSIS_CACHE))
            _RPA_ANALYSIS_CACHE.pop(oldest, None)
    return enriched


def _restore_rpa_analysis(value: Any) -> tuple[dict[str, Any] | None, bool]:
    """Return the cached complete analysis when a client sent a compact form."""
    if not isinstance(value, dict):
        return None, False
    analysis_id = str(value.get("analysis_id") or "").strip()
    cached = _RPA_ANALYSIS_CACHE.get(analysis_id) if analysis_id else None
    if not isinstance(cached, dict):
        return value, False
    supplied_query = value.get("query")
    if supplied_query not in (None, cached.get("query")):
        return value, False
    supplied_fingerprint = value.get("analysis_fingerprint")
    if supplied_fingerprint and supplied_fingerprint == cached.get("analysis_fingerprint") and value == cached:
        return value, False
    return deepcopy(cached), True


def _desktop_client() -> ShizaiDesktopAgent:
    """Reuse one authenticated desktop client for this MCP process."""
    global _DESKTOP_CLIENT
    if _DESKTOP_CLIENT is None:
        _DESKTOP_CLIENT = ShizaiDesktopAgent()
    return _DESKTOP_CLIENT


def _browser_bridge_manager() -> BrowserBridgeManager:
    global _BROWSER_BRIDGE_MANAGER
    if _BROWSER_BRIDGE_MANAGER is None:
        _BROWSER_BRIDGE_MANAGER = BrowserBridgeManager()
    return _BROWSER_BRIDGE_MANAGER


def _desktop_channel_available(client: ShizaiDesktopAgent) -> bool:
    """Check the local desktop socket without triggering auto-start retries."""
    config = getattr(client, "config", None)
    ws_url = str(getattr(config, "ws_url", "") or "").strip()
    if not ws_url:
        # Test doubles and custom clients may not expose a URL; preserve the
        # normal UI path for those callers.
        return True
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "wss" else 80)), 0.2):
            return True
    except OSError:
        return False


def _rpa_design_ui_start(client: ShizaiDesktopAgent, *, message: str) -> dict[str, Any]:
    """Countdown immediately before the first real computer interaction."""
    global _RPA_SMALL_CARD_OPEN
    native_ui: dict[str, Any] = {}
    if not _desktop_channel_available(client):
        native_ui["desktop_unavailable"] = True
        return native_ui
    try:
        native_ui["countdown"] = NativeSmallCard(client=client).countdown(
            seconds=5,
            message_template="塔斯将在 {seconds} 秒后接管你的电脑。",
            ensure_open=not _RPA_SMALL_CARD_OPEN,
        )
        _RPA_SMALL_CARD_OPEN = True
    except Exception as exc:  # UI feedback must never hide a durable workflow result
        native_ui["countdown_warning"] = str(exc)
    try:
        native_ui["takeover_frame"] = NativeTakeoverFrame(client=client).show()
    except Exception as exc:
        native_ui["takeover_frame_warning"] = str(exc)
    native_ui["small_card"] = _rpa_design_ui_update(
        client,
        state="takeover",
        message=message,
    )
    return native_ui


def _rpa_design_ui_ensure_takeover(
    client: ShizaiDesktopAgent,
    *,
    flow_ref: str | None = None,
    message: str = "塔斯正在接管你的电脑。",
) -> dict[str, Any]:
    """Start takeover UI once, after planning and before desktop/browser I/O."""
    normalized = str(flow_ref or _LATEST_RPA_DESIGN_REF or "").strip()
    active = _ACTIVE_RPA_DESIGN_UI.setdefault(normalized, {}) if normalized else {}
    if active.get("takeover_started"):
        return {"already_active": True, "flow_ref": normalized}
    takeover = _rpa_design_ui_start(client, message=message)
    takeover["takeover_started"] = True
    if normalized:
        active["takeover"] = takeover
        active["takeover_started"] = True
    return takeover


def _rpa_design_ui_capture_read(*, flow_ref: str | None = None) -> dict[str, Any]:
    """Describe passive DeepCapture access without starting takeover UI."""
    normalized = str(flow_ref or _LATEST_RPA_DESIGN_REF or "").strip()
    active = _ACTIVE_RPA_DESIGN_UI.get(normalized, {}) if normalized else {}
    result: dict[str, Any] = {
        "mode": "capture_read",
        "takeover_required": False,
        "countdown_started": False,
        "picker_overlay_started": False,
    }
    if normalized:
        result["flow_ref"] = normalized
    if active.get("takeover_started"):
        result["takeover_already_active"] = True
    return result


def _rpa_design_ui_register(flow_ref: str, native_ui: dict[str, Any]) -> None:
    global _LATEST_RPA_DESIGN_REF
    normalized = str(flow_ref or "").strip()
    if not normalized:
        return
    _ACTIVE_RPA_DESIGN_UI[normalized] = native_ui
    _LATEST_RPA_DESIGN_REF = normalized


def _rpa_design_ui_update(
    client: ShizaiDesktopAgent,
    *,
    state: str,
    message: str,
    flow_ref: str | None = None,
) -> dict[str, Any]:
    """Update only the original small card; never start the red picker overlay."""
    global _RPA_SMALL_CARD_OPEN
    normalized = str(flow_ref or _LATEST_RPA_DESIGN_REF or "").strip()
    result: dict[str, Any] = {"state": state, "message": message}
    if not _desktop_channel_available(client):
        result["desktop_unavailable"] = True
        return result
    try:
        card = NativeSmallCard(client=client)
        if _RPA_SMALL_CARD_OPEN:
            result["small_card"] = card.update(state=state, message=message)
        else:
            result["small_card"] = card.show(state=state, message=message)
            _RPA_SMALL_CARD_OPEN = True
    except Exception as exc:
        result["small_card_warning"] = str(exc)
    if normalized:
        result["flow_ref"] = normalized
    return result


def _rpa_design_ui_finish(
    client: ShizaiDesktopAgent,
    *,
    flow_ref: str,
    succeeded: bool,
    message: str,
    active_ui: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish a terminal design state and close the card and native mask."""
    global _LATEST_RPA_DESIGN_REF, _RPA_SMALL_CARD_OPEN
    normalized = str(flow_ref or _LATEST_RPA_DESIGN_REF or "").strip()
    if active_ui is None and normalized:
        _ACTIVE_RPA_DESIGN_UI.pop(normalized, None)
    result: dict[str, Any] = {}
    if not _desktop_channel_available(client):
        result["desktop_unavailable"] = True
        if _LATEST_RPA_DESIGN_REF == normalized:
            _LATEST_RPA_DESIGN_REF = None
        _RPA_SMALL_CARD_OPEN = False
        return result
    try:
        card = NativeSmallCard(client=client)
        if _RPA_SMALL_CARD_OPEN:
            result["small_card"] = card.update(
                state="success" if succeeded else "error",
                message=message,
                duration=0.5,
            )
        else:
            result["small_card"] = card.show(
                state="success" if succeeded else "error",
                message=message,
                duration=0.5,
            )
            _RPA_SMALL_CARD_OPEN = True
        result["small_card_close"] = card.close()
        _RPA_SMALL_CARD_OPEN = False
    except Exception as exc:
        result["small_card_warning"] = str(exc)
        try:
            result["small_card_close"] = NativeSmallCard(client=client).close()
        except Exception as close_exc:
            result["small_card_close_warning"] = str(close_exc)
        _RPA_SMALL_CARD_OPEN = False
    try:
        result["takeover_frame"] = NativeTakeoverFrame(client=client).close()
    except Exception as exc:
        result["takeover_frame_warning"] = str(exc)
    if _LATEST_RPA_DESIGN_REF == normalized:
        _LATEST_RPA_DESIGN_REF = None
    return result


def _rpa_cleanup_native_ui(client: ShizaiDesktopAgent | None = None) -> dict[str, Any]:
    """Best-effort cleanup for every native UI surface owned by this MCP process."""
    global _LATEST_RPA_DESIGN_REF, _RPA_SMALL_CARD_OPEN
    active_client = client or _DESKTOP_CLIENT
    if active_client is None:
        _ACTIVE_RPA_DESIGN_UI.clear()
        _LATEST_RPA_DESIGN_REF = None
        _RPA_SMALL_CARD_OPEN = False
        return {"skipped": True, "reason": "desktop_client_not_initialized"}
    result: dict[str, Any] = {}
    actions = {
        "small_card": lambda: NativeSmallCard(client=active_client).close(),
        "takeover_frame": lambda: NativeTakeoverFrame(client=active_client).close(),
        "native_mask": lambda: NativeMask(client=active_client).close(force=True),
        "inspect": lambda: ShizaiExtensionBridge(desktop=active_client).stop_inspect(),
    }
    for key, action in actions.items():
        try:
            result[key] = action()
        except Exception as exc:
            result[f"{key}_warning"] = str(exc)
    _ACTIVE_RPA_DESIGN_UI.clear()
    _LATEST_RPA_DESIGN_REF = None
    _RPA_SMALL_CARD_OPEN = False
    return result


def _rpa_cleanup_stale_design_ui(client: ShizaiDesktopAgent) -> dict[str, Any]:
    """Close UI from an abandoned design session before a new one starts."""
    return _rpa_cleanup_native_ui(client)


def _rpa_close_progress_card(client: ShizaiDesktopAgent) -> dict[str, Any]:
    """End a completed tool's progress hint without stopping an ongoing session."""
    global _RPA_SMALL_CARD_OPEN
    if not _RPA_SMALL_CARD_OPEN:
        return {"skipped": True, "reason": "progress_card_not_open"}
    try:
        result = NativeSmallCard(client=client).close()
    except Exception as exc:
        return {"cleanup_complete": False, "warning": str(exc)}
    confirmed = result.get("visible") is False and not result.get("close_warning")
    if confirmed:
        _RPA_SMALL_CARD_OPEN = False
    return {"cleanup_complete": confirmed, "small_card": result}


def _rpa_navigation_plan_step(
    analysis: dict[str, Any],
    plan: list[dict[str, Any]],
    *,
    paths: ShizaiPaths | None = None,
) -> dict[str, Any]:
    """Resolve a model-selected navigation step from links or capabilities.

    Operation links are useful evidence, but are optional in the flexible MCP
    plan. If the model omitted them or split navigation across multiple steps,
    select the first live navigation-capable component instead of rejecting an
    otherwise structurally valid plan.
    """
    operations = analysis.get("operations") if isinstance(analysis.get("operations"), list) else []
    navigate_ids = {
        str(item.get("id") or "").strip()
        for item in operations
        if isinstance(item, dict) and str(item.get("type") or "").strip().casefold() == "navigate"
    }
    navigate_ids.discard("")
    if not navigate_ids:
        navigate_ids = set()
    candidates = [
        (index, item)
        for index, item in enumerate(plan, start=1)
        if isinstance(item, dict)
        and navigate_ids.intersection(str(value).strip() for value in item.get("operation_ids") or [])
    ]
    if len(candidates) != 1:
        candidates = [
            (index, item)
            for index, item in enumerate(plan, start=1)
            if isinstance(item, dict)
            and (
                str(item.get("component") or "").strip()
                in {"WebBrowser_CreateV3", "WebBrowser_Create"}
                or (
                    paths is not None
                    and "navigate"
                    in set(
                        (
                            project_core._live_rpa_contract(
                                paths,
                                project_core.RPA_COMPONENT_TYPE_ALIASES.get(
                                    str(item.get("component") or "").strip(),
                                    str(item.get("component") or "").strip(),
                                ),
                            )
                            or {}
                        ).get("operation_types")
                        or []
                    )
                )
            )
        ]
    if not candidates:
        raise ValueError("request plan contains no navigation-capable component")
    if len(candidates) > 1:
        # Prefer an explicit URL-bearing navigation step; otherwise the first
        # navigation step is the least surprising recovery target.
        url_candidates = [
            item
            for item in candidates
            if str((item[1].get("config") or {}).get("url") or "").strip()
        ]
        candidates = url_candidates[:1] or candidates[:1]
    position, planned = candidates[0]
    return {
        "step_id": str(planned.get("id") or "").strip(),
        "position": position,
        # Preserve the model-selected native type.  Aliases are only used for
        # compatibility checks; returning the canonical V3 name here would
        # make an intentionally selected legacy alias appear to diverge from
        # the materialized task.
        "component": str(planned.get("component") or "").strip(),
        "operation_ids": sorted(
            navigate_ids.intersection(str(value).strip() for value in planned.get("operation_ids") or [])
        ),
        "purpose": str(planned.get("purpose") or "").strip(),
    }


def _rpa_navigation_step(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Resolve the materialized browser-navigation step from analyzed intent links."""
    flow_result = project_core.read_rpa_flow(paths, project_ref)
    flow = flow_result.get("flow") if isinstance(flow_result.get("flow"), dict) else flow_result
    analysis = flow.get("request_analysis") if isinstance(flow.get("request_analysis"), dict) else {}
    plan = flow.get("component_plan") if isinstance(flow.get("component_plan"), list) else []
    materialized = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    # Manually authored flows have native steps but no model component plan.
    # Keep plan/native mismatch checks when a plan does exist.
    planned_navigation = _rpa_navigation_plan_step(analysis, plan or materialized, paths=paths)
    step_id = planned_navigation["step_id"]
    positions = {
        str(item.get("id") or "").strip(): index
        for index, item in enumerate(materialized, start=1)
        if isinstance(item, dict)
    }
    if step_id not in positions:
        raise ValueError(f"navigate step is not materialized in the native flow: {step_id}")
    native = materialized[positions[step_id] - 1]
    actual_component = str(native.get("component") or "").strip()
    canonical_actual = project_core.RPA_COMPONENT_TYPE_ALIASES.get(actual_component, actual_component)
    canonical_planned = project_core.RPA_COMPONENT_TYPE_ALIASES.get(
        str(planned_navigation["component"] or "").strip(),
        str(planned_navigation["component"] or "").strip(),
    )
    if canonical_actual != canonical_planned:
        raise ValueError(f"navigate step component diverges from persisted plan: {step_id}")
    return {**planned_navigation, "position": positions[step_id]}


def _rpa_open_capture_target(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    wait_seconds: float = 1.0,
) -> dict[str, Any]:
    """Open the saved navigation URL quickly so page-bound steps can be authored."""
    navigation = _rpa_navigation_step(paths, project_ref)
    flow_result = project_core.read_rpa_flow(paths, project_ref)
    flow = flow_result.get("flow") if isinstance(flow_result.get("flow"), dict) else flow_result
    plan = flow.get("component_plan") if isinstance(flow.get("component_plan"), list) else []
    planned = next(
        (
            item
            for item in plan
            if isinstance(item, dict) and str(item.get("id") or "").strip() == navigation["step_id"]
        ),
        None,
    )
    native = next((item for item in flow.get("steps", [])
                   if isinstance(item, dict) and item.get("id") == navigation["step_id"]), {})
    native_config = native.get("config") if isinstance(native.get("config"), dict) else {}
    planned_config = planned.get("config") if isinstance(planned, dict) and isinstance(planned.get("config"), dict) else {}
    # The saved component wins over stale plan metadata after a manual edit.
    target = native_config.get("url") if "url" in native_config else planned_config.get("url")
    target_url = target.strip() if isinstance(target, str) else ""
    if re.match(r"^(?:localhost|(?:[a-z0-9-]+\.)+[a-z0-9-]+)(?::\d+)?(?:[/?#]|$)", target_url, re.I):
        target_url = "https://" + target_url
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"saved navigation step has no valid http(s) url: {navigation['step_id']}")
    owner_key = design_owner_key(project_ref)
    managed = _browser_bridge_manager().call(
        "open_design_tab", {"url": target_url, "ownerKey": owner_key}, timeout=10
    )
    if isinstance(managed, dict) and managed.get("opened") and managed.get("owned"):
        if wait_seconds > 0:
            time.sleep(min(float(wait_seconds), 10.0))
        browser = {
            **managed,
            "design_time_helper": True,
            "owner_key": owner_key,
            "method": "browser_extension_owned_design_tab",
        }
    else:
        browser = open_current_chrome(target_url, wait_seconds=wait_seconds)
        browser["owner_key"] = owner_key
        browser["method"] = "current_chrome_design_helper"
    return {
        **browser,
        "navigation": navigation,
        "runtime_verified": False,
        "message": (
            "已从保存的导航步骤打开真实页面，用于 DeepCapture 绑定；"
            "导航组件仍须在元素绑定完成后通过设计态调试验证。"
        ),
    }


def _rpa_close_owned_design_tab(project_ref: str, *, reason: str) -> dict[str, Any]:
    """Close the design tab owned by one MCP project, if one exists.

    The browser bridge stores ownership by an opaque project key.  Calling
    this helper is therefore safe: it cannot close a tab that the MCP did not
    create, and a missing/old extension returns an explicit retryable result
    instead of guessing from the URL or killing Chrome.
    """
    project_ref = str(project_ref or "").strip()
    if not project_ref:
        return {"status": "skipped", "owned": False, "reason": "missing_project_ref"}
    try:
        result = _browser_bridge_manager().call(
            "close_design_tab",
            {"ownerKey": design_owner_key(project_ref)},
            timeout=10,
        )
    except (BrowserBridgeError, OSError, ShizaiCloudError, TimeoutError, ValueError) as exc:
        return {
            "status": "failed",
            "owned": True,
            "reason": reason,
            "error": str(exc),
            "retryable": True,
        }
    if not isinstance(result, dict):
        return {"status": "failed", "owned": True, "reason": reason, "error": "invalid_bridge_response"}
    return {**result, "reason": reason}


def _rpa_unbound_element_step(
    paths: ShizaiPaths,
    project_ref: str,
    step_id: str,
    *,
    allow_rebind: bool = False,
) -> dict[str, Any]:
    """Validate a capture target, allowing rebind only by explicit request."""
    # Keep the no-flag call compatible with older test doubles and downstream
    # integrations that implement the original three-argument helper.
    if allow_rebind:
        return project_core.unbound_rpa_element_step(
            paths,
            project_ref,
            step_id,
            allow_rebind=True,
        )
    return project_core.unbound_rpa_element_step(paths, project_ref, step_id)


def _rpa_run_navigation_component(
    paths: ShizaiPaths,
    client: ShizaiDesktopAgent,
    project_ref: str,
) -> dict[str, Any]:
    """Restart native debug and single-step the intent-matched navigation component."""
    navigation = _rpa_navigation_step(paths, project_ref)
    capabilities = debug_core.debug_capabilities(paths, project_ref)
    stopped = None
    if capabilities.get("active_session"):
        stopped = debug_core.debug_stop(paths, project_ref, client=client)
    started = debug_core.debug_start(
        paths,
        project_ref,
        start_from=int(navigation["position"]),
        client=client,
    )
    status = str(started.get("status") or "").strip().casefold()
    if status != "paused":
        raise ShizaiCloudError(
            f"native navigation-step debug did not pause before execution: status={status or 'unknown'}"
        )
    current = started.get("current") if isinstance(started.get("current"), dict) else {}
    component_id = str(current.get("component_id") or "").strip()
    if component_id and component_id != navigation["step_id"]:
        raise ShizaiCloudError(
            "native debug paused at a different component than the analyzed navigate step: "
            f"expected={navigation['step_id']}; actual={component_id}"
        )
    stepped = debug_core.debug_command(
        paths,
        project_ref,
        "next",
        wait_seconds=30,
        client=client,
    )
    stepped_status = str(stepped.get("status") or "").strip().casefold()
    if stepped_status not in {"paused", "succeeded"}:
        raise ShizaiCloudError(
            "native navigation-step execution failed: "
            f"status={stepped_status}; message={stepped.get('message') or stepped.get('error') or ''}"
        )
    stepped_current = stepped.get("current") if isinstance(stepped.get("current"), dict) else {}
    if stepped_status == "paused":
        stepped_sequence = stepped_current.get("component_sequence")
        if stepped_current.get("component_boundary") is not True:
            raise ShizaiCloudError("native navigation-step execution did not stop at an exact component boundary")
        if isinstance(stepped_sequence, int) and stepped_sequence <= int(navigation["position"]):
            raise ShizaiCloudError(
                "native navigation-step execution did not advance beyond the requested component: "
                f"expected_after={navigation['position']}; actual={stepped_sequence}"
            )
    return {
        "kind": "shizai_native_navigation_step_run",
        "method": "designer_dap_single_step",
        "project_ref": project_ref,
        "navigation": navigation,
        "component_id": component_id or None,
        "capabilities": capabilities,
        "stopped_previous_session": stopped,
        "debug_start": started,
        "debug_next": stepped,
        "stopped_before_component": stepped_current.get("component_id") or None,
        "status": stepped_status or status,
        "executed": True,
    }


def _rpa_capture_windows_with_recovery(
    bridge: ShizaiExtensionBridge,
    *,
    project_ref: str,
    include_non_chrome: bool,
) -> dict[str, Any]:
    """Recover one stale design-time DeepCapture session without opening the red picker."""
    try:
        return bridge.capture_windows(include_non_chrome=include_non_chrome)
    except (ShizaiCloudError, OSError, TimeoutError, ValueError) as first_error:
        if not project_ref:
            raise
        recovery = bridge.start_capture_session(package_path=project_ref)
        result = bridge.capture_windows(include_non_chrome=include_non_chrome)
        result["capture_session_recovery"] = {
            "recovered": True,
            "reason": "stale_deep_capture_session",
            "first_error": str(first_error),
            "session": recovery,
            "red_picker_started": False,
        }
        return result


def _compact_capture_windows_result(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only the window identity needed by the next authoring call."""
    result = {
        key: value[key]
        for key in (
            "kind", "control_mode", "browser_control", "runtime_execution",
            "picker_overlay_started", "count", "chrome_session", "native_window_count",
            "selected_window", "next_action", "native_ui",
        )
        if key in value
    }
    result["windows"] = [
        {
            key: item.get(key)
            for key in (
                "element_id", "title", "page_title", "page_url", "process",
                "context_resolved", "selected", "error",
            )
            if key in item
        }
        for item in value.get("windows", [])
        if isinstance(item, dict)
    ]
    status = value.get("status") if isinstance(value.get("status"), dict) else {}
    runtime = status.get("runtime_extension") if isinstance(status.get("runtime_extension"), dict) else {}
    if status:
        result["extension"] = {
            "deep_capture_rpc": bool(status.get("deep_capture_rpc")),
            "runtime_ready": bool(runtime.get("ready")),
            "runtime_state": runtime.get("state"),
        }
    session = value.get("capture_session") if isinstance(value.get("capture_session"), dict) else {}
    if session:
        result["capture_session"] = {
            key: session.get(key)
            for key in (
                "initialized", "reused", "session_id", "project_ref", "inspect_started",
                "overlay_state", "native_session_state",
            )
            if key in session
        }
    recovery = value.get("capture_session_recovery")
    if isinstance(recovery, dict):
        result["capture_session_recovery"] = {
            key: recovery.get(key)
            for key in ("recovered", "reason", "first_error", "red_picker_started")
            if key in recovery
        }
    native_ui = value.get("native_ui") if isinstance(value.get("native_ui"), dict) else {}
    if native_ui:
        compact_ui = {
            key: native_ui.get(key)
            for key in ("state", "message", "flow_ref", "desktop_unavailable", "small_card_warning")
            if key in native_ui
        }
        takeover = native_ui.get("takeover") if isinstance(native_ui.get("takeover"), dict) else {}
        if takeover:
            compact_ui["takeover"] = {
                key: takeover.get(key)
                for key in ("takeover_started", "already_active", "flow_ref")
                if key in takeover
            }
        access = native_ui.get("access") if isinstance(native_ui.get("access"), dict) else {}
        if access:
            compact_ui["access"] = {
                key: access.get(key)
                for key in (
                    "mode", "takeover_required", "countdown_started",
                    "picker_overlay_started", "flow_ref", "takeover_already_active",
                )
                if key in access
            }
        result["native_ui"] = compact_ui
    return result


def _select_capture_window(
    windows: Any,
    *,
    target_url: str = "",
) -> dict[str, Any] | None:
    """Choose the most likely webpage window from a DeepCapture window list."""
    candidates = [
        item
        for item in windows if isinstance(item, dict) and str(item.get("element_id") or "").strip()
    ] if isinstance(windows, list) else []
    if not candidates:
        return None
    expected_host = urlparse(str(target_url or "").strip()).netloc.casefold()
    positions = {id(item): index for index, item in enumerate(candidates)}

    def score(item: dict[str, Any]) -> tuple[int, int]:
        title = str(item.get("page_title") or item.get("title") or "").strip()
        page_url = str(item.get("page_url") or "").strip()
        parsed = urlparse(page_url)
        value = 0
        if expected_host and parsed.netloc.casefold() == expected_host:
            value += 100
        elif expected_host and parsed.netloc:
            value -= 80
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            value += 40
        if re.search(r"\s-\s(?:Google Chrome|Chromium|Microsoft Edge)$", title, re.IGNORECASE):
            value += 20
        if title:
            value += 1
        if item.get("error"):
            value -= 100
        return value, -positions[id(item)]

    return max(candidates, key=score)


MCP_INSTRUCTIONS = """这是 Shizai CLI 核心的薄 stdio 包装，不新增流程引擎，也不内置站点配方。
连接自检：如果初始化后看不到至少一个 shizai_* 工具，尤其是 shizai_rpa_analyze_request 和 shizai_rpa_component_catalog，说明当前对话没有挂载 shizai-agent MCP，不是浏览器故障。此时禁止自行编写 MCP 客户端、另起一套 stdio 进程或声称流程已完成；应由宿主执行 npx -y shizai-agent-mcp@latest install，重启 Codex，并在 /mcp 确认服务和工具列表后再开始。
调用方大模型负责理解意图、选择组件、安排图结构、变量、循环、条件、分页、JavaScript 和输出方案；处理 RPA 请求时先调用 shizai_rpa_analyze_request，再读取组件目录，使用真实 component type 和参数名。MCP native-first：模型在 plan.collection_shape 声明层级关系；父子采集未拆成原生阶段时，start_design 拒绝写入。
组件目录先做一次 summary 能力查询，确定最终步骤后只读取已选 component_type 的 contract；不要枚举未选替代组件，也不要重复分析同一句请求。
原生 RPA 区分写图和验证：start_design 可先写完整图，再按组件验证；使用 add_step 增量追加时，读取 verification_gate，先静态校验，再按 debug_capabilities -> debug_start/复用 -> debug_command(action=next) -> debug_status 执行当前组件，用页面或输出证据确认后才能追加下一组件。静态通过、请求已接收、DAP 已连接或单纯停靠都不是运行成功；失败修复同一 project_ref。
同一 project_ref 的 debug_start 因 DAP 停靠或端口超时失败时，先调用一次 debug_stop 清理，再只重试一次；相同错误再次出现后禁止继续 debug_start，必须保留流程并报告运行环境阻断。
已有流程先 read_flow(detail=edit)，按稳定 step_id 读取参数；update_step(dry_run=true) 预览，带 expected_revision 提交。版本冲突重读，不重建流程。
start_design 可以持久化模型选择的完整流程，open_browser 默认是 false；显式打开页面时扩展按 project_ref 复用专属标签页，成功后关闭。捕获、更新、校验、诊断和运行都复用同一个 project_ref；只有明确要求新应用时才使用 force_new。模型提供的 selector 只能作为草稿；用户允许参考预设工作流时，可通过 project_import_elements 的 source_project_ref 模式复用另一个本地流程中由扩展验证的元素，但必须保留来源并在当前页面单步验证。
只有写入、电脑控制、捕获、调试和运行工具需要 allow_write=true。遇到登录或页面挑战先暂停，不保存凭据。session_start/status/stop 管理项目绑定的 DeepCapture 读取会话，extension_start_capture 管理官方拾取层，debug_start/command 管理 RuntimeHost；OCR 只能确认可见结果，不能替代 selector 或鼠标控制。
页面身份变化时保留 project_ref，重新扫描并捕获；只有明确传 allow_rebind=true 才替换已有绑定。浏览器生命周期必须闭环：WebBrowser_CreateV3 使用 openNewTab=true 时，流程末尾要有 WebBrowser_CloseWeb_V2（colse_type=select、web 引用“网页对象”）；缺失时 MCP 会自动追加并在 run 返回 browser_cleanup 状态。openNewTab=false 复用用户页面，不自动关闭。调试停止必须读取 stopped 或 cleanup_timeout 终态；不要等待一个永远停留的 stopping。一个 MCP stdio 进程应从 initialize 持续复用到本次流程结束，不要为每个工具调用重新启动进程，否则内存中的分析、DeepCapture 快照和调试会话会丢失。
新增通用规则时，放入核心 context；普适的安全边界才写入本说明。业务规则用中文，站点配方勿硬编码。CLI: shizai_agent/shizai_agent_cli.py；MCP: shizai_agent/core/mcp_server.py；说明见 docs/MCP_CLIENT_SETUP.md。"""


def _tool(
    name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> Any:
    read_only_tools = {
        "shizai_auth_status", "shizai_route_intent", "shizai_rpa_component_catalog",
        "shizai_rpa_read_flow", "shizai_rpa_validate_plan", "shizai_project_list",
        "shizai_project_inspect", "shizai_project_elements", "shizai_components",
        "shizai_draft", "shizai_apps", "shizai_runs", "shizai_logs", "shizai_tracing", "shizai_datasets",
    }
    return Tool(
        name=name,
        description=description,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False) if name in read_only_tools else None,
        inputSchema={
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    )


def _native_debug_tools() -> list[Any]:
    """Return the true native-task single-step tools."""
    return [
        _tool(
            "shizai_rpa_debug_start",
            "Start native .task design validation at 1-based component N and return its paused state. The full graph must exist. On DAP timeout, debug_stop then retry once; a repeated timeout is terminal. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "start_from": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "1-based；首个组件为 1。",
                },
                "start_timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 180, "default": 30},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_debug_capabilities",
            "Probe a native .task's component-level single-step support, breakpoint readiness, and whether this MCP process has a reusable debug session. Read-only; never opens Chrome or starts AppRunService.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_debug_status",
            "Read the live native RPA debug session, current DAP thread, stack frame, and mapped component. This is read-only.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_debug_command",
            "Advance the active design_validation session over the written graph. next runs to the next component breakpoint; continue to the next stop or completion; step_in/step_out send the DAP command. Not a production run. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "action": {"type": "string", "enum": ["next", "continue", "step_in", "step_out"]},
                "wait_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 120, "default": 15},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "action"],
        ),
        _tool(
            "shizai_rpa_debug_stop",
            "Disconnect and stop the active designer debugpy native RPA session. The operation is repeatable; requires allow_write.",
            {"project_ref": {"type": "string"}, "allow_write": {"type": "boolean", "default": False}},
            ["project_ref"],
        ),
    ]


def _all_schemas() -> list[Any]:
    """Return explicit JSON schemas so the server is independent of Pydantic."""
    return _native_debug_tools() + [
        _tool(
            "shizai_auth_status",
            "Check desktop reachability, login state, and cloud credential readiness without returning secrets.",
        ),
        _tool(
            "shizai_route_intent",
            "Classify a one-sentence request as browser RPA, native RPA, or Agent workflow before selecting tools. This deterministic router never calls a model or writes a project.",
            {"query": {"type": "string"}},
            ["query"],
        ),
        _tool(
            "shizai_rpa_analyze_request",
            "只读分析用户原句，返回意图、操作、字段、数量、分页要求与输出验收条件；层级数据会额外返回 parent_collection、child_collection、record_alignment 阶段契约。保留 query 和 analysis_id；模型依据实时目录选组件并预检，分析结果不写站点选择器。",
            {"query": {"type": "string", "minLength": 1}},
            ["query"],
        ),
        _tool(
            "shizai_rpa_validate_plan",
            "只读预检模型计划的结构、实时组件可用性和物化能力、参数名、引用类型及安全边界。层级采集若未拆成独立 parent/child 原生采集和对齐步骤，会返回 stage_plan_required；模型仍负责页面动作、循环和输出策略，通过不代表可执行或业务成功。",
            {
                "analysis": {"type": "object", "description": "Analysis result; analysis_id restores a compacted result."},
                "plan": rpa_plan_schema(),
            },
            ["analysis", "plan"],
        ),
        _tool(
            "shizai_extension_status",
            "Check the installed Shizai Chrome extension, desktop login, and DeepCapture readiness without returning secrets.",
        ),
        _tool(
            "shizai_browser_status",
            "检查独立 MCP 浏览器扩展及本地桥接状态；未安装时返回安装命令，不启动原客户端扩展。",
            {"connect_wait": {"type": "number", "minimum": 0, "maximum": 2, "default": 0}},
        ),
        _tool(
            "shizai_browser_tabs",
            "列出独立扩展可观察的普通网页标签页，返回 tabId、标题、URL 和活动状态。",
        ),
        _tool(
            "shizai_browser_activate_tab",
            "激活指定普通网页标签并聚焦其 Chrome 窗口；截图返回 target_window_not_foreground 时先调用本工具。需要 allow_write。",
            {
                "tab_id": {"type": "integer", "minimum": 1},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["tab_id", "allow_write"],
        ),
        _tool(
            "shizai_browser_capture_viewport",
            "截取前台标签页当前可见区域并返回可供多模态模型观察的 PNG，以及 screenshotId、URL、视口和 pageRevision；窗口不在前台时先调用 browser_activate_tab。",
            {"tab_id": {"type": "integer", "minimum": 1}},
            ["tab_id"],
        ),
        _tool(
            "shizai_browser_inspect_points",
            "将截图上的少量坐标在浏览器本地命中 DOM，返回紧凑可见元素证据和已验证 selector；可直接绑定到一个原生 RPA 步骤。",
            {
                "tab_id": {"type": "integer", "minimum": 1},
                "screenshot_id": {"type": "string"},
                "page_revision": {"type": "string"},
                "points": {
                    "type": "array", "minItems": 1, "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number", "minimum": 0},
                            "y": {"type": "number", "minimum": 0},
                            "role": {"type": "string"},
                            "id": {"type": "string"},
                                    "prefer_interactive": {"type": "boolean", "default": False},
                                    "include_local_links": {"type": "boolean", "default": False},
                        },
                        "required": ["x", "y"],
                    },
                },
                "project_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["tab_id", "screenshot_id", "page_revision", "points"],
        ),
        _tool(
            "shizai_browser_infer_collection",
            "在浏览器本地从至少两个字段 selector 推断重复行和相对字段，返回小样本；可将字段直接绑定到原生 DataCollectionV5 步骤。",
            {
                "tab_id": {"type": "integer", "minimum": 1},
                "screenshot_id": {"type": "string"},
                "page_revision": {"type": "string"},
                "fields": {
                    "type": "array", "minItems": 2, "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "selector": {"type": "string"}},
                        "required": ["name", "selector"],
                    },
                },
                "sample_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "project_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["tab_id", "screenshot_id", "page_revision", "fields"],
        ),
        _tool(
            "shizai_browser_action",
            "仅在设计证据标签页执行点击、填值、按键或有限滚动以推进页面；原生 RPA 单步运行仍是最终验证。需要 allow_write。",
            {
                "tab_id": {"type": "integer", "minimum": 1},
                "action": {"type": "string", "enum": ["click", "setValue", "pressKey", "scroll"]},
                "target": {
                    "type": "object",
                    "properties": {"selector": {"type": "string", "minLength": 1}},
                    "required": ["selector"],
                },
                "value": {"type": "string"},
                "key": {"type": "string"},
                "delta_x": {"type": "number", "minimum": -4000, "maximum": 4000},
                "delta_y": {"type": "number", "minimum": -4000, "maximum": 4000},
                "into_view": {"type": "boolean", "default": False},
                "page_revision": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["tab_id", "action", "target", "page_revision", "allow_write"],
        ),
        _tool(
            "shizai_rpa_ui_status",
            "Read the latest native RPA capture UI phase to show takeover, picker, scanning, ready, waiting, or idle feedback while the Shizai extension owns the overlay.",
            {
                "project_ref": {
                    "type": "string",
                    "description": "Optional active flow reference for the caller's state.",
                }
            },
        ),
        _tool(
            "shizai_rpa_browser_session_start",
            "Initialize the project-bound DeepCapture read session without starting the official red picker overlay. Requires allow_write because it changes the desktop capture session.",
            {
                "project_ref": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_rpa_browser_session_status",
            "Read the last known project-bound DeepCapture session state. This is local handshake state, not a live native RPC probe, and never starts capture.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_browser_session_stop",
            "Stop capture; close_browser=true closes MCP tab; allow_write required.",
            {
                "project_ref": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
                "close_browser": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_ui_feedback",
            "Control the designer-owned TARS card or blue run mask; RPA tools update these automatically, so use only for an explicit UI-only action. scan_demo is a picker demo, not workflow progress.",
            {
                "action": {"type": "string", "enum": ["small_card", "mask", "scan_demo"]},
                "state": {
                    "type": "string",
                    "default": "designing",
                },
                "message": {"type": "string"},
                "mask_type": {"type": "string", "default": "white"},
                "tips": {"type": "string"},
                "duration": {"type": "number", "minimum": 0, "maximum": 3600, "default": 0},
                "close_after": {"type": "boolean", "default": False},
                "close_now": {"type": "boolean", "default": False},
                "request_id": {"type": "string"},
                "force": {"type": "boolean", "default": True},
                "activate": {"type": "boolean", "default": True},
                "interval": {"type": "number", "minimum": 0.1, "maximum": 60, "default": 1.5},
                "keep_open": {"type": "boolean"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["action"],
        ),
        _tool(
            "shizai_extension_start_capture",
            "Start the official red picker for one already-written unbound component; allow_rebind=true may replace a binding after a page change. mode=2 is click and mode=1 is hover. Requires allow_write.",
            {
                "project_ref": {
                    "type": "string",
                    "description": "The client application directory returned by shizai_rpa_start_design.",
                },
                "step_id": {
                    "type": "string",
                    "description": "Existing element component that will receive the captured selector.",
                },
                "mode": {"type": "integer", "enum": [1, 2], "default": 2},
                "allow_rebind": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace an existing binding after a page change; default false.",
                },
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "step_id", "allow_write"],
        ),
        _tool(
            "shizai_extension_stop_capture",
            "Stop the installed Shizai extension DeepCapture inspection session. Pass the active flow's project_ref so the designer receives packagePath context; requires allow_write.",
            {"project_ref": {"type": "string"}, "allow_write": {"type": "boolean", "default": False}},
            ["project_ref"],
        ),
        _tool(
            "shizai_apps",
            "List published Agent apps.",
            {"page": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        _tool(
            "shizai_create_agent",
            "Create a blank Agent in the desktop designer; optionally set its description.",
            {
                "name": {"type": "string"},
                "agent_type": {
                    "type": "string",
                    "enum": ["Agent", "Chatbot", "Workflow", "Completion"],
                    "default": "Agent",
                },
                "description": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["name"],
        ),
        _tool(
            "shizai_update_agent",
            "Update an existing Agent name or description.",
            {
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["agent_id"],
        ),
        _tool(
            "shizai_components",
            "Agent graph only; RPA uses shizai_rpa_component_catalog; no id redirects.",
            {"agent_id": {"type": "string"}},
        ),
        _tool(
            "shizai_generation_context",
            "Return the live designer and algorithm-tool contract plus the component-first planning policy. The MCP client model designs the draft; this tool never calls TaSi or another model.",
            {"agent_id": {"type": "string"}, "query": {"type": "string"}},
        ),
        _tool(
            "shizai_rpa_start_design",
            "Create or reuse a native RPA app from a validated model plan. Matching requests reuse projects; strategy stays with the model. No site flow. Requires allow_write.",
            {
                "query": {"type": "string", "minLength": 1},
                "analysis": {
                    "type": "object",
                    "description": "Original analysis or {analysis_id} cached in this MCP session.",
                },
                "plan": rpa_plan_schema(for_creation=True),
                "response_mode": {"type": "string", "enum": ["full", "compact"], "default": "full"},
                "open_browser": {"type": "boolean", "default": False},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 10, "default": 1},
                "project_ref": {
                    "type": "string",
                },
                "reuse_existing": {
                    "type": "boolean",
                    "default": True,
                },
                "force_new": {
                    "type": "boolean",
                    "default": False,
                },
                "allow_write": {"type": "boolean", "default": False},
            },
            ["query", "analysis", "plan", "allow_write"],
        ),
        _tool(
            "shizai_rpa_open_browser",
            "Open the saved URL in current Chrome for live binding. This does not verify the native node; use debug tools later. Then capture_windows and scan_page. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_rpa_capture_windows",
            "Read current Chrome windows from the DeepCapture session; read-only, no red picker. With no Chrome page it returns shizai_rpa_open_browser as the only valid next action and does not enter takeover UI.",
            {
                "project_ref": {"type": "string"},
                "include_non_chrome": {"type": "boolean", "default": False},
            },
        ),
        _tool(
            "shizai_rpa_scan_page",
            "Scan the current Chrome Document for semantic candidates. Results hold ids and hints; pass scan_token to shizai_rpa_capture_elements for native selectors. Reuses the active session and never opens Chrome. allow_rebind=true only for page-change recovery.",
            {
                "root_element_id": {
                    "type": "string",
                    "description": "Optional Chrome window/Document element id; omit to inspect current windows.",
                },
                "query": {
                    "type": "string",
                    "description": "Semantic filter, e.g. 商品名称、价格、销量 or 搜索框.",
                },
                "scan_token": {
                    "type": "string",
                    "description": "Reuse a cached scan snapshot without a new tree traversal.",
                },
                "level": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 250},
                "page_title": {"type": "string"},
                "page_url": {"type": "string"},
                "project_ref": {
                    "type": "string",
                    "description": "Active flow reference; supplies an executable recovery action when Chrome/Document is missing.",
                },
                "step_id": {
                    "type": "string",
                    "description": "Element component whose role drives this scan and any picker fallback.",
                },
                "allow_rebind": {
                    "type": "boolean",
                    "default": False,
                    "description": "Rescan a bound step after a page change.",
                },
            },
        ),
        _tool(
            "shizai_rpa_runtime_preflight",
            "Preflight an element-dependent native RPA component: missing Chrome/Document, login or challenge pages, page-title/URL changes against the captured selector context. Never opens Chrome or sends a DAP command.",
            {
                "project_ref": {"type": "string"},
                "step_id": {
                    "type": "string",
                    "description": "Optional native step id; otherwise the active element step or first element step is inspected.",
                },
            },
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_capture_page_fields",
            "捕获同一记录字段并绑定 DataCollectionV5；不完整不写入。",
            {
                "project_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "product_id",
                            "product_name",
                            "merchant",
                            "price",
                            "sales",
                            "sales_metric",
                            "link",
                        ],
                    },
                },
                "candidate_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 4,
                    "description": "每字段候选数（1-20）。",
                },
                "use_js_link": {"type": "boolean", "default": False},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "step_id", "allow_write"],
        ),
        _tool(
            "shizai_rpa_capture_elements",
            "Bind selectors from one scan. For pagination, pass one id with as_page_selector=true.",
            {
                "element_ids": {"type": "array", "items": {"type": "string"}},
                "scan_token": {
                    "type": "string",
                    "description": "Token returned by shizai_rpa_scan_page for the same candidate ids.",
                },
                "page_title": {"type": "string"},
                "page_url": {"type": "string"},
                "project_ref": {"type": "string"},
                "step_id": {
                    "type": "string",
                    "description": "Existing element component to bind after import.",
                },
                "allow_rebind": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace an existing binding after a page change.",
                },
                "replace": {"type": "boolean", "default": False},
                "force_replace": {"type": "boolean", "default": False},
                "prune_unused": {"type": "boolean", "default": False},
                "as_page_selector": {"type": "boolean", "default": False},
                "pagination_mode": {
                    "type": "string",
                    "enum": ["next-page", "load-more"],
                    "default": "next-page",
                },
                "allow_write": {"type": "boolean", "default": False},
            },
            ["element_ids", "project_ref", "step_id", "allow_write"],
        ),
        _tool(
            "shizai_rpa_update_step",
            "修改已有步骤参数。先 read_flow 取 revision；dry_run 预览，提交时传 expected_revision 和 allow_write。",
            {
                "flow_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "config": {"type": "object", "description": "只传需要改变的参数；其余字段自动保留，不要复制整份原生参数。变量引用用 {variable:名称}。"},
                "dry_run": {"type": "boolean", "default": False},
                "expected_revision": {"type": "string", "minLength": 1, "description": "read_flow(detail=edit) 返回的 revision。"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["flow_ref", "step_id", "config"],
        ),
        _tool(
            "shizai_rpa_reload_editor",
            "刷新活动 RPA editor并等待 ready；拒绝 revision 冲突、错 editor 和未保存状态。随后运行组件验证画布。",
            {
                "project_ref": {"type": "string"},
                "expected_revision": {"type": "string", "minLength": 1},
                "ready_timeout": {"type": "number", "minimum": 1, "maximum": 30, "default": 10},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "expected_revision", "allow_write"],
        ),
        _tool(
            "shizai_rpa_run_component",
            "精确运行一个组件，核验本次生成代码和变量缓存；采集结果必须为本次非空表格。",
            {
                "project_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "expected_revision": {"type": "string", "minLength": 1},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "step_id", "expected_revision", "allow_write"],
        ),
        _tool(
            "shizai_rpa_remove_step",
            "按 revision 删除一个主流程步骤并重新物化；输出变量仍被引用时拒绝，除非 allow_dependents=true。dry_run 只预览。",
            {
                "project_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "expected_revision": {"type": "string", "minLength": 1, "description": "read_flow(detail=edit) 返回的 revision。"},
                "allow_dependents": {"type": "boolean", "default": False, "description": "强制删除仍被下游引用的步骤。"},
                "dry_run": {"type": "boolean", "default": False},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "step_id", "expected_revision", "allow_write"],
        ),
        _tool(
            "shizai_rpa_save_editor",
            "保存 designer 画布上的未保存改动，并复核落盘 revision 与步骤；dry_run 只预览。",
            {
                "project_ref": {"type": "string"},
                "expected_revision": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "expected_revision", "allow_write"],
        ),
        _tool(
            "shizai_rpa_components",
            "List the complete live native component surface and, when supplied, the current application's component state. The client model chooses components and graph order.",
            {"flow_ref": {"type": "string"}, "source_project_ref": {"type": "string"}},
        ),
        _tool(
            "shizai_rpa_component_catalog",
            "查询实时原生组件的参数、输出、能力及物化状态。先 summary 筛选，再 component_type 精确读取 contract；模型自主编排。筛选条件不规定业务方案。",
            {
                "scope": {"type": "string", "enum": ["builtin", "business", "all"], "default": "all"},
                "query": {"type": "string", "default": "", "description": "组件名称/说明/type 关键词；空格分词按 AND。先用一个关键词，不要把多个替代组件名拼成一次查询。"},
                "category": {"type": "string", "default": ""},
                "component_type": {"type": "string", "default": ""},
                "include_hidden": {"type": "boolean", "default": True, "description": "是否列出设计器隐藏的组件；精确 component_type 查询始终可读旧流程使用的类型。"},
                "package": {"type": "string", "default": ""},
                "capability": {
                    "type": "string",
                    "default": "",
                    "description": "Required capabilities, comma-separated: navigate,input,submit,extract,paginate,output.",
                },
                "capability_mode": {
                    "type": "string",
                    "enum": ["all", "any"],
                    "default": "any",
                    "description": "any=multi-role shortlist; all=every capability on one component.",
                },
                "top_level": {
                    "type": "string",
                    "default": "",
                    "description": "First-level menu id or label, e.g. webBrowser or 网页浏览器.",
                },
                "business_category": {"type": "string", "default": ""},
                "business_package": {"type": "string", "default": ""},
                "project_ref": {
                    "type": "string",
                    "description": "Optional client project; reports whether a business package is imported.",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "detail": {"type": "string", "enum": ["summary", "contract", "full"], "default": "contract"},
                "response_mode": {"type": "string", "enum": ["full", "compact"], "default": "full"},
                "engine_root": {"type": "string", "description": "Optional local rpa-engine directory override."},
                "business_info_path": {"type": "string", "description": "Optional bizComponentInfo.json override."},
                "business_cache_root": {
                    "type": "string",
                    "description": "Optional child-module-packages/biz override.",
                },
            },
        ),
        _tool(
            "shizai_rpa_add_step",
            "Add a live-catalog component. Use verification_gate before continuing. Supports {variable: NAME} and {expression: EXPR}. Requires allow_write.",
            {
                "flow_ref": {"type": "string"},
                "component": {"type": "string"},
                "step_id": {"type": "string"},
                "before_step_id": {
                    "type": "string",
                    "description": "Insert before this stable step id.",
                },
                "label": {"type": "string"},
                "element_ids": {"type": "array", "items": {"type": "string"}},
                "config": {"type": "object"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["flow_ref", "component"],
        ),
        _tool(
            "shizai_rpa_set_step_elements",
            "Bind imported native element ids to one RPA component step. Requires allow_write.",
            {
                "flow_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "element_ids": {"type": "array", "items": {"type": "string"}},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["flow_ref", "step_id", "element_ids"],
        ),
        _tool(
            "shizai_rpa_read_flow",
            "读取现有流程。detail=edit 返回紧凑节点目录和 revision；同时传 step_id 只展开目标步骤的原生参数及实时契约。full 保留完整旧返回。",
            {
                "flow_ref": {"type": "string"},
                "detail": {"type": "string", "enum": ["full", "edit"], "default": "full"},
                "step_id": {"type": "string", "description": "仅用于 detail=edit。"},
            },
            ["flow_ref"],
        ),
        _tool(
            "shizai_rpa_validate_step",
            "校验已写入的 RPA 组件并返回 verification_gate。DataCollectionV5 会同时对当前真实页面执行原生采集预览，0 数据行直接失败；其它组件仍需 debug 工具提供执行证据。",
            {
                "flow_ref": {"type": "string"},
                "step_id": {"type": "string"},
                "strict": {"type": "boolean", "default": False},
            },
            ["flow_ref", "step_id"],
        ),
        _tool(
            "shizai_rpa_validate_flow",
            "Validate component order, imported element bindings, requested Excel columns, and runtime readiness for a client-owned native flow.",
            {"flow_ref": {"type": "string"}, "strict": {"type": "boolean", "default": False}},
            ["flow_ref"],
        ),
        _tool(
            "shizai_rpa_materialize",
            "流程与元素静态检查后，生成或刷新原生运行代码、变量索引和 source map，再进行运行诊断。可重复调用；需要 allow_write，不代表业务已执行。",
            {
                "project_ref": {"type": "string"},
                "strict": {"type": "boolean", "default": True},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_sync_diagnose",
            "Diagnose a client-owned native RPA project before runtime execution: applicationId/packageId mapping, task node id, file-index freshness, Chrome RPA runtime plugin evidence, and flow readiness.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_run",
            "Run a validated native RPA task via workflowDebugRunRPA and wait for a truthful terminal status; runtime success without a fresh configured output is failure_kind=missing_fresh_output. wait=false submits and returns for shizai_rpa_status. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "input_param": {"type": "object", "default": {}},
                "run_uuid": {"type": "string"},
                "workflow_run_id": {"type": "string"},
                "start_take_work": {"type": "boolean", "default": False},
                "wait": {"type": "boolean", "default": True},
                "wait_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600, "default": 900},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_status",
            "Read local native run state, output-file freshness/quality, matching current-run designer logs, business status, cleanup, and runtime diagnosis; publishes ui_feedback. Does not call the cloud API.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_stop",
            "Stop the active native RPA run via workflowDebugStop, persist stopping then stopped or cleanup_timeout, and close the run mask. Repeatable when no run is active. Requires allow_write.",
            {"project_ref": {"type": "string"}, "allow_write": {"type": "boolean", "default": False}},
            ["project_ref"],
        ),
        _tool("shizai_draft", "Read an advanced-chat workflow draft.", {"agent_id": {"type": "string"}}),
        _tool(
            "shizai_validate_draft",
            "Validate a workflow graph's node ids, edges, variable selectors, and live component types before saving.",
            {
                "agent_id": {"type": "string"},
                "draft": {"type": "object"},
                "strict": {"type": "boolean", "default": False},
            },
            ["draft"],
        ),
        _tool(
            "shizai_apply_flow",
            "Validate, save, re-read, optionally run, and optionally publish a model-generated workflow draft.",
            {
                "agent_id": {"type": "string"},
                "draft": {"type": "object"},
                "query": {"type": "string"},
                "strict": {"type": "boolean", "default": False},
                "run": {"type": "boolean", "default": False},
                "publish": {"type": "boolean", "default": False},
                "publish_range": {"type": "string", "enum": ["only_me", "all_team_members"], "default": "only_me"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["draft"],
        ),
        _tool(
            "shizai_run_draft",
            "Execute an already saved advanced-chat workflow using the model configured in the designer. Requires allow_write.",
            {
                "agent_id": {"type": "string"},
                "query": {"type": "string"},
                "conversation_id": {"type": "string"},
                "timeout": {"type": "number", "exclusiveMinimum": 0},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["query"],
        ),
        _tool("shizai_runs", "List workflow runs for an Agent.", {"agent_id": {"type": "string"}}),
        _tool(
            "shizai_logs",
            "Read detailed execution logs for one conversation message.",
            {"agent_id": {"type": "string"}, "conversation_id": {"type": "string"}, "message_id": {"type": "string"}},
            ["conversation_id", "message_id"],
        ),
        _tool(
            "shizai_tracing",
            "Read node-level tracing for one workflow run.",
            {"agent_id": {"type": "string"}, "run_id": {"type": "string"}},
            ["run_id"],
        ),
        _tool(
            "shizai_datasets",
            "List knowledge bases visible to the current Agent workspace.",
            {
                "page": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
                "keyword": {"type": "string"},
            },
        ),
        _tool(
            "shizai_project_list",
            "Find projects by name for editing.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        ),
        _tool(
            "shizai_project_inspect",
            "Inspect one local Shizai project.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_project_elements",
            "Read native element summaries and full selectors from one local Shizai project.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_project_import_elements",
            "Import model candidates as drafts, or reuse deep_capture + verified records with source_project_ref + element_ids. References keep provenance and require current-page runtime verification. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "elements": {"type": "array", "items": {"type": "object"}},
                "source_project_ref": {"type": "string"},
                "element_ids": {"type": "array", "items": {"type": "string"}},
                "replace": {"type": "boolean", "default": False},
                "force_replace": {"type": "boolean", "default": False},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_rpa_validate_elements",
            "Validate browser-captured element records before writing them; rejects missing page identity, empty selectors, coordinate-only captures, and index-only selectors.",
            {
                "project_ref": {"type": "string"},
                "elements": {"type": "array", "items": {"type": "object"}},
                "strict": {"type": "boolean", "default": False},
            },
        ),
        _tool(
            "shizai_rpa_validate_project",
            "Validate a native RPA project: captured elements, live component contracts, graph references, runtime safety. Model-owned plans may start with any installed component; navigation is required only when the flow needs it.",
            {"project_ref": {"type": "string"}, "strict": {"type": "boolean", "default": False}},
            ["project_ref"],
        ),
    ]


def _schemas(profile: str = "full") -> list[Any]:
    tools = _all_schemas()
    if profile == "full":
        return tools
    if profile == "rpa-authoring":
        compact_descriptions = {
            "shizai_rpa_analyze_request": "分析请求；层级数据返回三阶段。",
            "shizai_rpa_validate_plan": "预检计划；缺层级阶段返回 stage_plan_required。",
            "shizai_rpa_add_step": "按实时契约追加组件；before_step_id 可插入主流程。按 verification_gate 验证，写入须 allow_write。",
            "shizai_browser_status": "检查独立浏览器扩展连接及安装状态；按返回的 next_action 恢复。",
            "shizai_browser_capture_viewport": "截取指定 tab 的当前视口，返回图片及 screenshotId/pageRevision，供视觉取点。",
            "shizai_browser_inspect_points": "用当前截图的少量坐标定位元素；传 project_ref、step_id、allow_write 可绑定原生组件。",
            "shizai_rpa_status": (
                "Read the persisted run state, native runtime evidence, output checks, and cleanup state."
            ),
            "shizai_rpa_capture_windows": (
                "List capturable windows in the active DeepCapture session; choose a Chrome Document for page scans."
            ),
            "shizai_rpa_scan_page": (
                "Scan the current Chrome Document for semantic candidates. Reuse scan_token when capturing selectors."
            ),
            "shizai_rpa_save_editor": (
                "保存 designer 未保存的画布改动并复核落盘 revision；运行前先保存。"
            ),
            "shizai_rpa_stop": (
                "Stop the active native RPA run and persist stopped or cleanup_timeout. Requires allow_write."
            ),
            "shizai_rpa_debug_capabilities": (
                "Probe a native .task's component-level single-step and breakpoint support; read-only."
            ),
            "shizai_rpa_runtime_preflight": (
                "Check Chrome/Document, login challenges and page identity before running a bound component."
            ),
            "shizai_rpa_debug_start": (
                "Pause native validation before 1-based component N; requires allow_write."
            ),
            "shizai_rpa_debug_command": (
                "Execute next component, continue, step_in or step_out in the active debug session."
            ),
            "shizai_project_import_elements": (
                "Import model candidates as drafts, or reuse verified records by source_project_ref + element_ids. Requires allow_write."
            ),
            "shizai_rpa_sync_diagnose": (
                "Diagnose application/package ids, task node id, file-index freshness and flow readiness before running."
            ),
            "shizai_extension_start_capture": (
                "Start the official red picker for one written unbound component; allow_rebind=true may replace a binding. Requires allow_write."
            ),
            "shizai_rpa_open_browser": (
                "Open the saved URL in Chrome for live binding; then capture_windows and scan_page. Requires allow_write."
            ),
            "shizai_rpa_browser_session_start": (
                "Initialize the project-bound DeepCapture read session. Requires allow_write."
            ),
            "shizai_extension_stop_capture": (
                "Stop the installed Shizai extension DeepCapture session. Requires allow_write."
            ),
            "shizai_rpa_run": (
                "Run a validated flow, refresh stale runtime files and return terminal evidence. Requires allow_write."
            ),
        }
        for tool in tools:
            if tool.name in compact_descriptions:
                tool.description = compact_descriptions[tool.name]
            if tool.name in {"shizai_rpa_component_catalog", "shizai_rpa_start_design"}:
                tool.inputSchema["properties"].pop("response_mode", None)
            if tool.name == "shizai_rpa_component_catalog":
                tool.inputSchema["properties"]["include_hidden"]["default"] = False
                # Installation overrides are useful for diagnostics, but they
                # distract authoring models from the normal installed catalog.
                for key in ("engine_root", "business_info_path", "business_cache_root"):
                    tool.inputSchema["properties"].pop(key, None)
            if tool.name == "shizai_rpa_start_design":
                # A legacy metadata delay, frequently mistaken for an RPC
                # timeout. Authoring clients configure delay nodes in plan.
                tool.inputSchema["properties"].pop("wait_seconds", None)
                tool.inputSchema["required"].remove("query")
                tool.inputSchema["properties"]["query"]["description"] = "可省略，使用 analysis_id 缓存中的原句；显式提供时须与原句一致。"
            if tool.name in {"shizai_rpa_validate_plan", "shizai_rpa_start_design"}:
                # The authoring profile keeps the stage fields but omits their
                # verbose nested prose to preserve its compact schema budget.
                plan_schema = tool.inputSchema.get("properties", {}).get("plan", {})
                step_properties = (
                    plan_schema.get("properties", {}).get("steps", {}).get("items", {}).get("properties", {})
                    if isinstance(plan_schema, dict)
                    else {}
                )
                if isinstance(step_properties, dict):
                    stage_schema = step_properties.get("collection_stage")
                    if isinstance(stage_schema, dict):
                        stage_schema.pop("description", None)
                    # These two annotations remain accepted through
                    # additionalProperties and the full profile; omitting
                    # them here keeps the authoring profile below its budget.
                    step_properties.pop("collection_role", None)
                    step_properties.pop("native_alternative_reason", None)
                    step_properties.pop("parent_step_id", None)
                    step_properties.pop("merge_key", None)
        return [tool for tool in tools if tool.name in RPA_AUTHORING_TOOLS]
    raise ValueError("SHIZAI_MCP_TOOL_PROFILE 必须为 full 或 rpa-authoring")


def _resolved_agent_id(client: ShizaiDesktopAgent, value: str | None) -> str:
    return value or client.config.agent_id


def _rpa_plan_preflight(
    paths: ShizaiPaths,
    analysis: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Validate before and after ownership-based lifecycle normalization."""
    from .browser_lifecycle import CLOSE_COMPONENT, normalize_browser_cleanup_plan

    live_index = component_catalog_core.builtin_component_index(paths)
    result = project_core.preflight_rpa_execution_plan(
        analysis, deepcopy(plan), available_components=live_index, planning_policy="native_first",
    )
    if not result.get("valid"):
        return result
    normalized = deepcopy(result["plan"])
    cleanup = normalize_browser_cleanup_plan(normalized, available=CLOSE_COMPONENT in live_index)
    warnings = list(result.get("normalization_warnings") or [])
    if cleanup["status"] == "appended":
        result = project_core.preflight_rpa_execution_plan(
            analysis, normalized, available_components=live_index, planning_policy="native_first",
        )
        warnings.append("已自动追加原生关闭网页组件；只关闭流程创建且未被重新赋值的网页对象。")
    elif cleanup["status"] == "unavailable":
        warnings.append("当前设计器缺少关闭网页组件，无法自动追加浏览器清理。")
    result["normalization_warnings"] = list(dict.fromkeys(
        [*warnings, *(result.get("normalization_warnings") or []), *cleanup.get("warnings", [])]
    ))
    result["browser_cleanup"] = cleanup
    return result


def _rpa_existing_design_result(
    paths: ShizaiPaths,
    existing: dict[str, Any],
    *,
    request_fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    """Return a resumable result without hiding any MCP tools."""
    project_ref = str(existing.get("project_ref") or existing.get("path") or "").strip()
    flow: dict[str, Any] = {}
    try:
        inspected = project_core.read_rpa_flow(paths, project_ref)
        if isinstance(inspected, dict) and isinstance(inspected.get("flow"), dict):
            flow = inspected["flow"]
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        # Native projects may not have the compatibility flow file yet.  The
        # project reference remains useful for inspect/status/update tools.
        flow = {}
    next_action = flow.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {
            "tool": "shizai_rpa_status",
            "arguments": {"project_ref": project_ref},
            "reason": "已有流程已保留；先读取状态，再继续修改或运行。",
        }
    return {
        "kind": "shizai_rpa_existing_flow",
        "mode": "native_client_application",
        "created": False,
        "reused": True,
        "project_created": False,
        "project_ref": project_ref,
        "flow_ref": project_ref,
        "plan_applied": False,
        "saved": False,
        "request_fingerprint": request_fingerprint,
        "fingerprint_source": existing.get("fingerprint_source"),
        "reuse_reason": reason,
        "message": "已找到现有流程；本次仅返回引用，传入的 plan 未应用、节点未替换。请 read_flow(detail=edit) 后用编辑接口修改；start_design 复用不是图补丁。",
        "flow": flow,
        "next_action": next_action,
        "force_new_hint": "如需从头创建，请在用户明确要求后传入 force_new=true。",
    }


def _rpa_ensure_capture_session(bridge: ShizaiExtensionBridge, project_ref: str | None) -> dict[str, Any] | None:
    """Bootstrap the shared DeepCapture session when the active flow is known."""
    normalized = str(project_ref or "").strip()
    ensure = getattr(bridge, "ensure_capture_session", None)
    if not normalized or not callable(ensure):
        return None
    return ensure(package_path=normalized)


def _rpa_capture_read_with_recovery(
    bridge: ShizaiExtensionBridge,
    project_ref: str | None,
    operation: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Retry one transient DeepCapture read after reconnecting its project session."""
    checker = getattr(ShizaiExtensionBridge, "_is_transient_capture_error", None)

    def read_once(*, surface_embedded_errors: bool) -> dict[str, Any]:
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("DeepCapture read returned a non-object response")
        if surface_embedded_errors and project_ref and callable(checker):
            # Candidate probing is intentionally broader than the final field
            # selection. A timeout on an unused candidate must not discard a
            # complete, same-card result and repeat the entire page scan.
            if result.get("complete") is True:
                return result
            capture_errors = result.get("capture_errors")
            recovery_actions = result.get("recovery_actions")
            has_actionable_result = isinstance(recovery_actions, list) and any(
                isinstance(item, dict) and item.get("tool") for item in recovery_actions
            )
            if isinstance(capture_errors, list) and not has_actionable_result:
                transient = next(
                    (
                        str(item.get("error") or "")
                        for item in capture_errors
                        if isinstance(item, dict)
                        and str(item.get("error") or "").strip()
                        and checker(ValueError(str(item.get("error"))))
                    ),
                    "",
                )
                if transient:
                    raise ShizaiCloudError(transient)
        return result

    try:
        return read_once(surface_embedded_errors=True), None
    except (ShizaiCloudError, OSError, TimeoutError) as exc:
        if not project_ref or not callable(checker) or not checker(exc):
            raise
        recovery = bridge.start_capture_session(package_path=str(project_ref))
        probe = getattr(bridge, "capture_windows", None)
        recovery_windows = probe(include_non_chrome=False) if callable(probe) else None
        result = read_once(surface_embedded_errors=False)
        result["capture_session_recovery"] = {
            "reconnected": True,
            "reason": str(exc),
            "session": recovery,
            **({"window_count": int(recovery_windows.get("count") or 0)}
               if isinstance(recovery_windows, dict) else {}),
        }
        return result, recovery


def _bind_browser_result(
    paths: ShizaiPaths,
    args: dict[str, Any],
    result: Any,
    converter: Any,
) -> Any:
    project_ref = str(args.get("project_ref") or "").strip()
    step_id = str(args.get("step_id") or "").strip()
    if bool(project_ref) != bool(step_id):
        raise ValueError("project_ref and step_id must be provided together")
    if not project_ref:
        return result
    if not bool(args.get("allow_write", False)):
        raise PermissionError("binding browser evidence requires allow_write: true")
    if not isinstance(result, dict) or result.get("kind") == "mcp_browser_bridge_status":
        return result
    elements = converter(result, page=result, prefix=f"{step_id}-browser")
    result["native_binding"] = bind_verified_elements(
        paths,
        project_ref=project_ref,
        step_id=step_id,
        elements=elements,
    )
    result["capture_source"] = "browser_bridge"
    result["verification_status"] = "verified"
    return result


def _dispatch_impl(name: str, args: dict[str, Any]) -> Any:
    """Dispatch one MCP call to the shared CLI core."""
    global _RPA_SMALL_CARD_OPEN
    paths = _paths()
    if name == "shizai_browser_status":
        return _browser_bridge_manager().status(connect_wait=float(args.get("connect_wait", 0)))
    if name == "shizai_browser_tabs":
        return _browser_bridge_manager().call("list_tabs")
    if name == "shizai_browser_activate_tab":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_browser_activate_tab requires allow_write: true")
        return _browser_bridge_manager().call("activate_tab", {"tabId": int(args["tab_id"])})
    if name == "shizai_browser_capture_viewport":
        return _browser_bridge_manager().call("capture_viewport", {"tabId": int(args["tab_id"])})
    if name == "shizai_browser_inspect_points":
        result = _browser_bridge_manager().call(
            "inspect_points",
            {
                "tabId": int(args["tab_id"]),
                "screenshotId": str(args["screenshot_id"]),
                "pageRevision": str(args["page_revision"]),
                "points": [
                    {
                        **point,
                        **({"preferInteractive": bool(point.get("prefer_interactive"))} if "prefer_interactive" in point else {}),
                        **({"includeLocalLinks": bool(point.get("include_local_links"))} if "include_local_links" in point else {}),
                    }
                    for point in args["points"]
                ],
            },
        )
        return _bind_browser_result(paths, args, result, point_elements)
    if name == "shizai_browser_infer_collection":
        result = _browser_bridge_manager().call(
            "infer_repeated_collection",
            {
                "tabId": int(args["tab_id"]),
                "screenshotId": str(args["screenshot_id"]),
                "pageRevision": str(args["page_revision"]),
                "fields": list(args["fields"]),
                "sampleLimit": int(args.get("sample_limit", 5)),
            },
        )
        return _bind_browser_result(paths, args, result, collection_elements)
    if name == "shizai_browser_action":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_browser_action requires allow_write: true")
        params = {
            "tabId": int(args["tab_id"]),
            "action": str(args["action"]),
            "target": dict(args["target"]),
        }
        for source, target in (
            ("value", "value"), ("key", "key"), ("delta_x", "deltaX"),
            ("delta_y", "deltaY"), ("into_view", "intoView"),
            ("page_revision", "pageRevision"),
        ):
            if args.get(source) is not None:
                params[target] = args[source]
        return _browser_bridge_manager().call("performAction", params)
    if name == "shizai_rpa_validate_plan":
        analysis, rehydrated = _restore_rpa_analysis(args.get("analysis"))
        if not isinstance(analysis, dict):
            return {"kind": "rpa_plan_preflight", "valid": False, "error_code": "analysis_mismatch",
                    "error": "analysis must be the unchanged shizai_rpa_analyze_request result",
                    "do_not_reanalyze_original_query": True, "next_tool": "shizai_rpa_validate_plan"}
        result = _rpa_plan_preflight(paths, analysis, args.get("plan") if isinstance(args.get("plan"), dict) else {})
        if rehydrated:
            result.update(analysis_rehydrated=True, analysis_id=analysis.get("analysis_id"),
                          analysis_fingerprint=analysis.get("analysis_fingerprint"))
        return result
    # Read/preview editing must not initialize a desktop client whose shutdown
    # cleanup would otherwise control native UI after a read-only session.
    if name == "shizai_rpa_read_flow" and args.get("detail") == "edit":
        return read_edit_context(paths, str(args["flow_ref"]), step_id=args.get("step_id"))
    if name == "shizai_rpa_update_step" and args.get("dry_run") is True:
        return project_core.update_rpa_step_config(
            paths, str(args["flow_ref"]), str(args["step_id"]), args["config"],
            dry_run=True, expected_revision=args.get("expected_revision"),
        )
    client = _desktop_client()
    if name == "shizai_route_intent":
        return project_core.route_request(str(args["query"]))
    if name == "shizai_auth_status":
        return agent_core.authentication_status(client=client)
    if name == "shizai_extension_status":
        return ShizaiExtensionBridge(desktop=client).status(probe_deep_capture=True)
    if name == "shizai_rpa_ui_status":
        return ShizaiExtensionBridge(desktop=client).ui_status()
    if name == "shizai_rpa_browser_session_start":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_browser_session_start requires allow_write: true")
        project_ref = str(args.get("project_ref") or "").strip()
        result = ShizaiExtensionBridge(desktop=client).ensure_capture_session(package_path=project_ref)
        result["next_action"] = "shizai_rpa_capture_windows"
        return result
    if name == "shizai_rpa_browser_session_status":
        project_ref = str(args.get("project_ref") or "").strip()
        return ShizaiExtensionBridge(desktop=client).capture_session_status(package_path=project_ref)
    if name == "shizai_rpa_browser_session_stop":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_browser_session_stop requires allow_write: true")
        project_ref = str(args.get("project_ref") or "").strip()
        result = ShizaiExtensionBridge(desktop=client).stop_capture(package_path=project_ref)
        if bool(args.get("close_browser", False)):
            result["design_tab_cleanup"] = _rpa_close_owned_design_tab(project_ref, reason="task_finished")
        return result
    if name == "shizai_ui_feedback":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_ui_feedback requires allow_write: true")
        action = str(args["action"]).strip()
        if action == "small_card":
            card = NativeSmallCard(client=client)
            result = card.show(
                state=str(args.get("state", "designing")),
                message=args.get("message"),
                duration=float(args.get("duration", 0)),
                close_after=bool(args.get("close_after", False)),
            )
            _RPA_SMALL_CARD_OPEN = bool(result.get("visible", True)) if isinstance(result, dict) else True
            return result
        if action == "mask":
            mask = NativeMask(client=client)
            if bool(args.get("close_now", False)):
                return mask.close(
                    request_id=args.get("request_id"),
                    force=bool(args.get("force", True)),
                )
            return mask.show(
                mask_type=str(args.get("mask_type", "white")),
                request_id=args.get("request_id"),
                tips=args.get("tips"),
                duration=float(args.get("duration", 0)),
                close_after=bool(args.get("close_after", False)),
                activate=bool(args.get("activate", True)),
            )
        if action == "scan_demo":
            return NativeScanDemo(client=client).run(
                interval=float(args.get("interval", 1.5)),
                mask_type=str(args.get("mask_type", "white")),
                close_after=not bool(args.get("keep_open", False)),
                activate=bool(args.get("activate", True)),
            )
        raise ValueError("action must be small_card, mask, or scan_demo")
    if name == "shizai_extension_start_capture":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_extension_start_capture requires allow_write: true")
        project_ref = str(args.get("project_ref") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if bool(args.get("allow_rebind", False)):
            target_step = _rpa_unbound_element_step(
                paths,
                project_ref,
                step_id,
                allow_rebind=True,
            )
        else:
            target_step = _rpa_unbound_element_step(paths, project_ref, step_id)
        takeover_ui = _rpa_design_ui_ensure_takeover(
            client,
            flow_ref=project_ref,
            message=f"塔斯正在接管你的电脑并为组件 {step_id} 启动元素拾取。",
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        try:
            result = bridge.start_capture(
                mode=int(args.get("mode", 2)),
                package_path=project_ref,
            )
        except Exception:
            try:
                bridge.stop_inspect()
            except Exception:
                pass
            try:
                NativeMask(client=client).close(force=True)
            except Exception:
                pass
            raise
        result["target_step"] = target_step
        result["native_ui"] = {"takeover": takeover_ui}
        return result
    if name == "shizai_extension_stop_capture":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_extension_stop_capture requires allow_write: true")
        bridge = ShizaiExtensionBridge(desktop=client)
        try:
            result = bridge.stop_capture(package_path=args.get("project_ref"))
        except Exception:
            try:
                NativeMask(client=client).close(force=True)
            except Exception:
                pass
            raise
        try:
            result["native_mask"] = NativeMask(client=client).close(force=True)
        except Exception as exc:
            result["native_mask_warning"] = str(exc)
        return result
    if name == "shizai_apps":
        return agent_core.query_apps(page=int(args.get("page", 1)), limit=int(args.get("limit", 20)), client=client)
    if name == "shizai_create_agent":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_create_agent requires allow_write: true")
        return agent_core.create_agent(
            str(args["name"]),
            agent_type=str(args.get("agent_type", "Agent")),
            description=str(args.get("description", "")),
            client=client,
        )
    if name == "shizai_update_agent":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_update_agent requires allow_write: true")
        return agent_core.update_agent(
            str(args["agent_id"]), name=args.get("name"), description=args.get("description"), client=client
        )
    if name == "shizai_components":
        if not str(args.get("agent_id") or "").strip():
            return {
                "kind": "agent_component_tool_redirect",
                "valid_for": "agent_graph_only",
                "message": (
                    "这是 Agent 图组件接口，不适用于浏览器/桌面 RPA。请先调用 shizai_rpa_analyze_request，"
                    "再按模型的拆解需要查询 shizai_rpa_component_catalog；筛选条件和调用次数由模型决定。"
                ),
                "next_tool": "shizai_rpa_analyze_request",
                "rpa_rule": (
                    "数字目标是最终输出验收条件；客户端模型根据实时组件契约选择采集、循环、分页、脚本或输出组合。"
                    "MCP 只校验真实组件、参数、引用、已验证元素和运行安全，不规定唯一分页组件或固定步骤顺序。"
                ),
            }
        return agent_core.query_components(agent_id=args.get("agent_id"), client=client)
    if name == "shizai_generation_context":
        return agent_core.generation_context(
            query=args.get("query"),
            agent_id=args.get("agent_id"),
            client=client,
        )
    if name == "shizai_rpa_analyze_request":
        # Planning is read-only.  Do not open/reconnect the desktop channel
        # just to close UI surfaces when this MCP process has no active design
        # state; a closed desktop would otherwise make analysis look hung
        # while the native cleanup retries its socket connection.
        stale_cleanup = None
        if _RPA_SMALL_CARD_OPEN or _ACTIVE_RPA_DESIGN_UI or _LATEST_RPA_DESIGN_REF:
            stale_cleanup = _rpa_cleanup_stale_design_ui(client)
        analyzed = project_core.analyze_rpa_request(str(args["query"]))
        analysis_id = str(analyzed.get("analysis_id") or "").strip()
        existing = _RPA_ANALYSIS_CACHE.get(analysis_id) if analysis_id else None
        if isinstance(existing, dict):
            result = deepcopy(existing)
            result["analysis_reused"] = True
        else:
            result = _cache_rpa_analysis(analyzed)
        if analysis_id:
            _RPA_ANALYSIS_REQUEST_COUNTS[analysis_id] = _RPA_ANALYSIS_REQUEST_COUNTS.get(analysis_id, 0) + 1
            result["analysis_call_count"] = _RPA_ANALYSIS_REQUEST_COUNTS[analysis_id]
        if result.get("analysis_reused"):
            result["do_not_reanalyze_original_query"] = True
            result["next_tool"] = "shizai_rpa_component_catalog"
            result["message"] = "同一原句的分析已存在；复用 analysis_id，继续组件查询或计划预检。"
        result["native_ui"] = _rpa_design_ui_update(
            client,
            state="designing",
            message="塔斯正在识别任务意图并拆解流程步骤。",
        )
        if stale_cleanup is not None:
            result["native_ui"]["stale_cleanup"] = stale_cleanup
        return result
    if name == "shizai_rpa_start_design":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_start_design requires allow_write: true")
        supplied_analysis, _ = _restore_rpa_analysis(args.get("analysis"))
        query = str(args.get("query", (supplied_analysis or {}).get("query", "")) or "").strip()
        analysis = project_core.analyze_rpa_request(query)
        if not isinstance(supplied_analysis, dict):
            return {
                "kind": "rpa_design_rejected",
                "valid": False,
                "error_code": "analysis_mismatch",
                "error": "analysis must be the unchanged shizai_rpa_analyze_request result",
                "do_not_reanalyze_original_query": True,
                "next_tool": "shizai_rpa_validate_plan",
            }
        if supplied_analysis.get("analysis_id") != analysis["analysis_id"] or supplied_analysis.get("query") != query:
            return {
                "kind": "rpa_design_rejected",
                "valid": False,
                "error_code": "analysis_mismatch",
                "error": "analysis does not match the original query",
                "original_query": (_RPA_ANALYSIS_CACHE.get(str(supplied_analysis.get("analysis_id"))) or supplied_analysis).get("query"),
                "analysis_reference": {"analysis_id": supplied_analysis.get("analysis_id")},
                "project_created": False,
                "correction": "保留当前 plan，将 query 恢复为 original_query，用 analysis_reference 重试 start_design；不要重写原句或重新分析。",
                "do_not_reanalyze_original_query": True,
                "next_tool": "shizai_rpa_start_design",
            }
        validated_plan = _rpa_plan_preflight(
            paths,
            analysis,
            args.get("plan") if isinstance(args.get("plan"), dict) else {},
        )
        if not validated_plan.get("valid"):
            return {**validated_plan, "kind": "rpa_design_rejected", "project_created": False}
        plan = validated_plan["plan"]
        first_step = plan["steps"][0]
        target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
        first_config = first_step.get("config") if isinstance(first_step.get("config"), dict) else {}
        target_url = str(first_config.get("url") or target.get("url") or "").strip()
        output_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
        output_file = str(
            output_contract.get("file")
            or output_contract.get("filename")
            or output_contract.get("file_path")
            or output_contract.get("output_path")
            or output_contract.get("destination")
            or "result.xlsx"
        ).strip()
        required_columns = [str(item).strip() for item in output_contract.get("columns", []) if str(item).strip()]
        max_rows = output_contract.get("max_rows")
        flow_name = str(plan.get("name") or "RPA 自动化流程").strip()
        request_fingerprint = project_core.rpa_request_fingerprint(
            query,
            target_url=target_url,
            output_contract=output_contract,
        )
        reuse_existing = bool(args.get("reuse_existing", True))
        force_new = bool(args.get("force_new", False))
        requested_project_ref = str(args.get("project_ref") or "").strip()
        if not force_new:
            existing: dict[str, Any] | None = None
            if requested_project_ref:
                resolved_ref = project_core.resolve_project(paths, requested_project_ref)
                existing = {
                    "project_ref": str(resolved_ref),
                    "path": str(resolved_ref),
                    "fingerprint_source": "explicit_project_ref",
                }
            elif reuse_existing:
                existing = project_core.find_rpa_project_by_fingerprint(paths, request_fingerprint)
            if existing:
                return _rpa_existing_design_result(
                    paths,
                    existing,
                    request_fingerprint=request_fingerprint,
                    reason=("explicit_project_ref" if requested_project_ref else "matching_request_fingerprint"),
                )
        planned_wait_steps = [item for item in plan["steps"] if item.get("component") == "Basic_Delay_V1"]
        planned_wait = float(
            (planned_wait_steps[0].get("config") or {}).get("second", args.get("wait_seconds", 1))
            if planned_wait_steps
            else args.get("wait_seconds", 1)
        )
        native_ui: dict[str, Any] = {
            "designing": _rpa_design_ui_update(
                client,
                state="designing",
                message=f"塔斯正在校验并写入流程设计：{flow_name}",
            )
        }
        browser_plan = analysis.get("intent") == "browser_rpa"
        try:
            result = project_core.start_rpa_design(
                paths,
                name=flow_name,
                target_url=target_url,
                keyword="",
                output_file=output_file,
                required_columns=required_columns,
                use_excel_components=any(item.get("component", "").startswith("Excel_") for item in plan["steps"]),
                open_new_tab=bool(first_config.get("openNewTab", False)),
                max_rows=max_rows,
                wait_seconds=planned_wait,
                wait_after_submit=bool(planned_wait_steps),
                refresh_browser_tab=any(item.get("component") == "WebBrowser_GetTAB_V1" for item in plan["steps"]),
                first_step=first_step,
                component_plan=plan["steps"],
                request_analysis=analysis,
                request_fingerprint=request_fingerprint,
                client=client,
            )
            attached = project_core.attach_rpa_execution_plan(
                paths,
                str(result.get("project_ref") or result.get("flow_ref") or ""),
                analysis=analysis,
                validated_plan=validated_plan,
            )
            project_ref = str(result.get("project_ref") or result.get("flow_ref") or "")
            # Creating a graph is a model-owned planning operation and must
            # not require a browser/navigation node just because the request
            # was classified as browser RPA. Resolve a navigation step only
            # when the caller explicitly asks this call to perform browser
            # I/O; otherwise the persisted graph remains the source of truth.
            navigation_plan = (
                _rpa_navigation_plan_step(analysis, plan["steps"], paths=paths)
                if browser_plan and bool(args.get("open_browser", False))
                else None
            )
            written_count = result.get("written_component_count")
            if written_count is not None and int(written_count) != len(plan["steps"]):
                raise ValueError(
                    "complete component graph was not written before design-time execution: "
                    f"planned={len(plan['steps'])}, written={written_count}"
                )
            current_flow = {"flow": {"next_action": attached["next_action"]}}
            result["request_analysis"] = analysis
            result["validated_plan"] = validated_plan
            result["plan_attachment"] = attached
            result["navigation_plan"] = navigation_plan
            result["design_complete"] = True
            result["reused"] = False
            result["reuse_existing"] = reuse_existing
            result["force_new"] = force_new
            result["request_fingerprint"] = request_fingerprint
            result["written_component_count"] = int(written_count or len(plan["steps"]))
            result["component_catalog"] = project_core.rpa_components(
                paths,
                flow_ref=project_ref,
            )
            result["next_action"] = current_flow.get("flow", {}).get("next_action") or attached["next_action"]
        except Exception:
            native_ui["terminal"] = _rpa_design_ui_finish(
                client,
                flow_ref="",
                succeeded=False,
                message="塔斯识别任务或创建流程时遇到问题。",
                active_ui=native_ui,
            )
            raise
        project_ref = str(result.get("project_ref") or result.get("flow_ref") or "").strip()
        _rpa_design_ui_register(project_ref, native_ui)
        native_ui["plan_ready"] = _rpa_design_ui_update(
            client,
            state="ready",
            message=f"塔斯已完成流程设计，准备开始实际操作：{flow_name}",
            flow_ref=project_ref,
        )
        result["native_ui"] = native_ui
        browser = None
        capture = None
        capture_warning = None
        if browser_plan and bool(args.get("open_browser", False)):
            native_ui["takeover"] = _rpa_design_ui_ensure_takeover(
                client,
                flow_ref=project_ref,
                message="塔斯正在打开计划目标页面并准备采集页面证据。",
            )
            try:
                browser = _rpa_open_capture_target(
                    paths,
                    project_ref,
                    wait_seconds=float(args.get("wait_seconds", 1)),
                )
            except (ShizaiCloudError, OSError, ValueError) as exc:
                # Application creation is already durable. Return a resumable
                # result so a browser discovery issue does not hide the flow.
                browser = {
                    "opened": False,
                    "browser": "chrome",
                    "url": target_url,
                    "error": str(exc),
                    "message": "流程已创建，但尚未从保存的导航步骤打开设计态页面。",
                    "next_action": {
                        "tool": "shizai_rpa_open_browser",
                        "arguments": {"project_ref": project_ref, "allow_write": True},
                    },
                }
                result["browser_open_warning"] = str(exc)
        if isinstance(browser, dict) and browser.get("opened"):
            try:
                capture = ShizaiExtensionBridge(desktop=client).start_capture_session(
                    package_path=project_ref,
                )
                result["ui_feedback"] = capture.get("ui_feedback") or result.get("ui_feedback")
                planned_next = attached.get("next_action") if isinstance(attached, dict) else None
                result["next_action"] = {
                    "kind": "browser_capture",
                    "tool": "shizai_rpa_capture_windows",
                    "arguments": {"project_ref": project_ref, "include_non_chrome": False},
                    "then": "shizai_rpa_scan_page",
                    "planned_step": planned_next,
                    "reason": "当前 Chrome 已复用，DeepCapture 会话已初始化且未打开红色拾取层；按计划中下一步骤的 element_role 扫描 Document。",
                }
            except Exception as exc:
                capture_warning = str(exc)
                result["ui_feedback"] = set_rpa_ui_feedback(
                    "error",
                    "start_capture",
                    f"流程已创建且 Chrome 已打开，但 DeepCapture 会话初始化失败：{exc}",
                    animation="error",
                    overlay_state="unknown",
                    flow_ref=project_ref,
                )
                result["next_action"] = {
                    "tools": ["shizai_rpa_capture_windows", "shizai_project_import_elements"],
                    "optional_picker_tool": "shizai_extension_start_capture",
                    "reason": "Chrome 会话已保留；优先重读页面树，或导入模型识别并校验过的 selector。只有确实需要官方交互式拾取时才启动红色拾取层。",
                }
        if capture is not None:
            result["capture"] = capture
            result["capture_started"] = True
            result["picker_overlay_started"] = False
        elif browser is not None and browser.get("opened"):
            result["capture_started"] = False
            result["capture_warning"] = capture_warning
        result["design_time_browser"] = browser
        return result
    if name == "shizai_rpa_open_browser":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_open_browser requires allow_write: true")
        project_ref = str(args.get("project_ref") or "").strip()
        takeover_ui = _rpa_design_ui_ensure_takeover(
            client,
            flow_ref=project_ref or None,
            message="塔斯正在打开计划目标页面并准备采集页面证据。",
        )
        browser = _rpa_open_capture_target(paths, project_ref)
        capture = None
        capture_warning = None
        if project_ref:
            try:
                capture = ShizaiExtensionBridge(desktop=client).start_capture_session(package_path=project_ref)
            except Exception as exc:
                capture_warning = str(exc)
        return {
            "opened": True,
            "browser": "chrome",
            "project_ref": project_ref or None,
            "design_time_browser": browser,
            "navigation_run": {
                "executed": False,
                "runtime_verified": False,
                "method": browser["method"],
                "navigation": browser["navigation"],
                "message": "页面已打开用于绑定；导航组件等待后续设计态调试验证。",
            },
            "capture": capture,
            "capture_started": capture is not None,
            "capture_warning": capture_warning,
            "native_ui": {"takeover": takeover_ui},
            "next_action": {
                "tool": "shizai_rpa_capture_windows",
                "arguments": {"project_ref": project_ref, "include_non_chrome": False},
                "reason": "目标页面已恢复；必须先确认 Chrome 窗口存在，再单独调用页面扫描。",
            },
        }
    if name == "shizai_rpa_capture_windows":
        project_ref = str(args.get("project_ref") or _LATEST_RPA_DESIGN_REF or "").strip()
        access_ui = _rpa_design_ui_capture_read(flow_ref=project_ref or None)
        native_ui = _rpa_design_ui_update(
            client,
            state="scanning",
            message="塔斯正在识别当前浏览器窗口和页面。",
            flow_ref=project_ref or None,
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        result = _rpa_capture_windows_with_recovery(
            bridge,
            project_ref=project_ref,
            include_non_chrome=bool(args.get("include_non_chrome", False)),
        )
        if capture_session is not None:
            result["capture_session"] = capture_session
        if int(result.get("count") or 0) <= 0:
            result["next_action"] = {
                "tool": "shizai_rpa_open_browser",
                "arguments": {"project_ref": project_ref, "allow_write": True},
                "reason": "当前不存在可识别的 Chrome 页面；从已保存导航步骤打开目标页后再捕获，禁止继续 scan_page。",
            }
            native_ui["state"] = "waiting"
            native_ui["message"] = "当前未检测到 Chrome 页面，准备打开计划中的目标页面。"
            native_ui["access"] = access_ui
            result["native_ui"] = native_ui
            return _compact_capture_windows_result(result)
        native_ui["access"] = access_ui
        result["native_ui"] = native_ui
        target_url = ""
        if project_ref:
            try:
                loaded_flow = project_core.read_rpa_flow(paths, project_ref)
                flow_state = loaded_flow.get("flow") if isinstance(loaded_flow, dict) else {}
                target_url = str((flow_state or {}).get("target_url") or "").strip()
            except (FileNotFoundError, OSError, TypeError, ValueError):
                target_url = ""
        selected_window = _select_capture_window(
            result.get("windows", []),
            target_url=target_url,
        )
        for item in result.get("windows", []):
            if isinstance(item, dict):
                item["selected"] = item is selected_window
        if selected_window is not None:
            result["selected_window"] = {
                key: selected_window.get(key)
                for key in ("element_id", "title", "page_title", "page_url")
            }
        result["next_action"] = {
            "tool": "shizai_rpa_scan_page",
            "arguments": {
                "project_ref": project_ref,
                **(
                    {"root_element_id": selected_window["element_id"]}
                    if selected_window is not None
                    else {}
                ),
            },
            "reason": "已按流程目标与窗口身份选择最可能的 Chrome 页面；直接在其 Document 子树中进行语义扫描。",
        }
        return _compact_capture_windows_result(result)
    if name == "shizai_rpa_scan_page":
        project_ref = str(args.get("project_ref") or _LATEST_RPA_DESIGN_REF or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if step_id:
            if bool(args.get("allow_rebind", False)):
                _rpa_unbound_element_step(
                    paths,
                    project_ref,
                    step_id,
                    allow_rebind=True,
                )
            else:
                _rpa_unbound_element_step(paths, project_ref, step_id)
        access_ui = _rpa_design_ui_capture_read(flow_ref=project_ref or None)
        native_ui = _rpa_design_ui_update(
            client,
            state="scanning",
            message="塔斯正在识别页面元素并分析可复用选择器。",
            flow_ref=project_ref or None,
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        requested_root = str(args.get("root_element_id") or "").strip()
        windows = (
            {
                "count": 1,
                "windows": [{"element_id": requested_root}],
                "preflight_skipped": True,
                "reason": "root_element_id_from_prior_capture",
            }
            if requested_root
            else _rpa_capture_windows_with_recovery(
                bridge,
                project_ref=project_ref,
                include_non_chrome=False,
            )
        )
        if int(windows.get("count") or 0) <= 0:
            return {
                "kind": "shizai_page_element_candidates",
                "control_mode": "capture_read",
                "browser_control": "deep_capture_read",
                "runtime_execution": False,
                "picker_overlay_started": False,
                "root_element_id": None,
                "document_ids": [],
                "query": str(args.get("query", "")),
                "candidate_count": 0,
                "candidates": [],
                "capture_ready": False,
                "element_scan_ready": False,
                "workflow_status": "blocked",
                "message": "当前不存在可识别的 Chrome 页面；已停止扫描，必须先恢复流程目标页面。",
                "preflight_windows": windows,
                "capture_session": capture_session,
                "next_action": {
                    "tool": "shizai_rpa_open_browser",
                    "arguments": {"project_ref": project_ref, "allow_write": True},
                    "reason": "页面扫描依赖 Chrome 窗口；单步执行分析所得的网页导航步骤并确认 capture_windows.count>0 后再重试。",
                },
                "native_ui": {
                    "state": "waiting",
                    "message": "当前未检测到 Chrome 页面，准备单步执行计划中的网页导航步骤。",
                    "access": access_ui,
                },
            }
        scan_root = requested_root
        selected_window: dict[str, Any] | None = None
        if not scan_root:
            target_url = ""
            if project_ref:
                try:
                    loaded_flow = project_core.read_rpa_flow(paths, project_ref)
                    flow_state = loaded_flow.get("flow") if isinstance(loaded_flow, dict) else {}
                    target_url = str((flow_state or {}).get("target_url") or "").strip()
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    target_url = ""
            selected_window = _select_capture_window(
                windows.get("windows", []),
                target_url=target_url,
            )
            if selected_window is not None:
                scan_root = str(selected_window.get("element_id") or "").strip()
        result, _ = _rpa_capture_read_with_recovery(
            bridge,
            project_ref,
            lambda: bridge.scan_page(
                root_element_id=scan_root or None,
                query=str(args.get("query", "")),
                level=int(args.get("level", 6)),
                max_nodes=int(args.get("max_nodes", 250)),
                page_title=args.get("page_title") or (selected_window or {}).get("page_title"),
                page_url=args.get("page_url") or (selected_window or {}).get("page_url"),
                scan_token=args.get("scan_token"),
                strategy="auto",
            ),
        )
        if isinstance(result.get("next_action"), dict):
            next_action = result["next_action"]
            if next_action.get("tool") == "shizai_rpa_scan_page":
                next_action.setdefault("arguments", {})["project_ref"] = project_ref
                if step_id:
                    next_action["arguments"]["step_id"] = step_id
                if bool(args.get("allow_rebind", False)):
                    next_action["arguments"]["allow_rebind"] = True
            if next_action.get("tool") == "shizai_extension_start_capture":
                if step_id:
                    next_action.setdefault("arguments", {})["project_ref"] = project_ref
                    next_action["arguments"]["step_id"] = step_id
                    if bool(args.get("allow_rebind", False)):
                        next_action["arguments"]["allow_rebind"] = True
                else:
                    result["next_action"] = {
                        "tool": "shizai_rpa_components",
                        "arguments": {"flow_ref": project_ref},
                        "reason": "先按计划添加需要元素的组件；官方拾取必须绑定到已存在的 step_id。",
                    }
        native_ui["access"] = access_ui
        if capture_session is not None:
            result["capture_session"] = capture_session
        result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_runtime_preflight":
        return debug_core.runtime_preflight(
            paths,
            str(args["project_ref"]),
            step_id=args.get("step_id"),
            client=client,
        )
    if name == "shizai_rpa_capture_page_fields":
        project_ref = str(args.get("project_ref") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_capture_page_fields requires allow_write: true")
        target_step = _rpa_unbound_element_step(paths, project_ref, step_id)
        flow = project_core.read_rpa_flow(paths, project_ref).get("flow") or {}
        use_js_link = bool(args.get("use_js_link", False))
        requested_fields = args.get("fields")
        required = project_core._result_capture_requirements(
            flow.get("required_columns") or project_core.DEFAULT_RPA_COLUMNS,
            use_js_link=use_js_link,
            use_js_table=bool(flow.get("use_js_table", False)),
        )
        if not isinstance(requested_fields, list) or not requested_fields:
            requested_fields = [
                field
                for field in project_core._RPA_RESULT_FIELD_ORDER
                if field in required
            ]
        else:
            requested_fields = list(requested_fields)
            requested_fields.extend(
                field
                for field in project_core._RPA_RESULT_FIELD_ORDER
                if field in required and field not in requested_fields
            )
        capture_fields = list(requested_fields)
        if use_js_link:
            capture_fields = [field for field in capture_fields if field not in {"merchant", "link"}]
        access_ui = _rpa_design_ui_capture_read(flow_ref=project_ref or None)
        native_ui = _rpa_design_ui_update(
            client,
            state="capturing",
            message="塔斯正在同一次页面快照中对齐并捕获采集字段。",
            flow_ref=project_ref or None,
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        try:
            captured, recovery = _rpa_capture_read_with_recovery(
                bridge,
                project_ref,
                lambda: bridge.capture_page_fields(
                    fields=capture_fields,
                    candidate_limit=int(args.get("candidate_limit", 4)),
                    page_url=str(flow.get("target_url") or "").strip() or None,
                ),
            )
            capture_session = recovery or capture_session
            captured["project_ref"] = project_ref
            captured["step_id"] = step_id
            captured["target_step"] = target_step
            if captured.get("complete"):
                imported = project_core.import_elements(
                    paths,
                    project_ref,
                    captured["elements"],
                    replace=False,
                    force_replace=False,
                    capture_source="deep_capture",
                )
                captured["import"] = imported
                selected_field_ids = captured.get("selected_field_ids") or {}
                binding_ids = [
                    str(selected_field_ids.get(project_core._canonical_result_field(column)) or "").strip()
                    for column in flow.get("required_columns") or project_core.DEFAULT_RPA_COLUMNS
                ]
                binding_ids = [element_id for element_id in binding_ids if element_id]
                captured["binding"] = project_core.set_rpa_step_elements(
                    paths,
                    project_ref,
                    step_id,
                    binding_ids or list(imported["imported"]),
                    client=client,
                )
                captured["result_state"] = project_core.mark_rpa_result_capture_ready(
                    paths,
                    project_ref,
                    captured_fields=captured.get("selected_field_ids") or {},
                    field_element_ids=captured.get("selected_field_ids") or {},
                    use_js_link=use_js_link,
                    selector_scope=captured.get("selector_scope"),
                )
                captured["next_action"] = captured["result_state"].get("next_action")
            else:
                recovery_actions = captured.get("recovery_actions")
                recovery_action = recovery_actions[0] if isinstance(recovery_actions, list) and recovery_actions else None
                if isinstance(recovery_action, dict) and recovery_action.get("tool"):
                    partial_elements = captured.get("elements") if isinstance(captured.get("elements"), list) else []
                    if partial_elements:
                        captured["partial_import"] = project_core.import_elements(
                            paths,
                            project_ref,
                            partial_elements,
                            replace=False,
                            force_replace=False,
                            capture_source="deep_capture",
                        )
                    selected_ids = captured.get("selected_field_ids")
                    selected_ids = selected_ids if isinstance(selected_ids, dict) else {}
                    root_element_id = str(selected_ids.get("product_name") or next(iter(selected_ids.values()), "")).strip()
                    config_hint = recovery_action.get("config_hint")
                    config_hint = config_hint if isinstance(config_hint, dict) else {}
                    workflow_reference = str(recovery_action.get("workflow_reference") or "").strip()
                    if recovery_action["tool"] == "shizai_rpa_add_step":
                        hinted_config = config_hint.get("config")
                        if isinstance(hinted_config, dict):
                            recovery_config = dict(hinted_config)
                            recovery_element_ids: list[str] = []
                        else:
                            recovery_config = {
                                "workflow_reference": workflow_reference,
                                "input": config_hint.get("input"),
                                "output_variable": config_hint.get("output_variable") or "商品链接",
                                "result_mode": "generated_markers",
                                "page_scope": True,
                            }
                            recovery_element_ids = [root_element_id] if root_element_id else []
                        next_arguments = {
                            "flow_ref": project_ref,
                            "component": recovery_action.get("component"),
                            "step_id": f"recover-{step_id}-link",
                            "before_step_id": step_id,
                            "element_ids": recovery_element_ids,
                            "config": recovery_config,
                            "allow_write": True,
                        }
                    else:
                        next_arguments = {"flow_ref": project_ref, "detail": "edit", "step_id": step_id}
                    captured["next_action"] = {
                        "tool": recovery_action["tool"],
                        "arguments": next_arguments,
                        "reason": recovery_action.get("next") or recovery_action.get("reason"),
                        "plan_revision": {
                            "component": recovery_action.get("component"),
                            "workflow_reference": workflow_reference or None,
                            "config_hint": config_hint,
                        },
                    }
                else:
                    next_limit = min(20, max(int(args.get("candidate_limit", 4)) * 2, 8))
                    captured["next_action"] = {
                        "tool": "shizai_rpa_capture_page_fields",
                        "arguments": {
                            "project_ref": project_ref,
                            "step_id": step_id,
                            "fields": list(requested_fields),
                            "candidate_limit": next_limit,
                            "use_js_link": use_js_link,
                            "allow_write": True,
                        },
                        "reason": "本次没有形成同一条记录的完整字段集合；扩大候选窗口后最多再自动尝试一次。",
                    }
            if capture_session is not None:
                captured["capture_session"] = capture_session
        finally:
            try:
                native_ui["inspect_stop"] = bridge.stop_inspect()
            except Exception as exc:
                native_ui["inspect_stop_warning"] = str(exc)
            try:
                native_ui["native_mask"] = NativeMask(client=client).close(force=True)
            except Exception as exc:
                native_ui["native_mask_warning"] = str(exc)
        native_ui["access"] = access_ui
        captured["native_ui"] = native_ui
        return captured
    if name == "shizai_rpa_capture_elements":
        project_ref = str(args.get("project_ref") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_capture_elements requires allow_write: true")
        as_page_selector = bool(args.get("as_page_selector", False))
        element_ids = list(args.get("element_ids") or [])
        if as_page_selector and len(element_ids) != 1:
            raise ValueError("as_page_selector requires exactly one captured element id")
        allow_rebind = bool(args.get("allow_rebind", False))
        if as_page_selector:
            target_step = {"id": step_id, "component": "WebBrowser_DataCollectionV5"}
        elif allow_rebind:
            target_step = _rpa_unbound_element_step(
                paths,
                project_ref,
                step_id,
                allow_rebind=True,
            )
        else:
            target_step = _rpa_unbound_element_step(paths, project_ref, step_id)
        access_ui = _rpa_design_ui_capture_read(flow_ref=project_ref or None)
        native_ui = _rpa_design_ui_update(
            client,
            state="capturing",
            message="塔斯正在捕获并校验元素选择器。",
            flow_ref=project_ref or None,
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        try:
            captured = bridge.capture_elements(
                element_ids,
                page_title=args.get("page_title"),
                page_url=args.get("page_url"),
                scan_token=args.get("scan_token"),
            )
            if project_ref:
                imported = project_core.import_elements(
                    paths,
                    project_ref,
                    captured["elements"],
                    replace=bool(args.get("replace", False)),
                    force_replace=bool(args.get("force_replace", False)),
                    capture_source="deep_capture",
                )
                captured["import"] = imported
                if as_page_selector:
                    captured["binding"] = project_core.bind_rpa_pagination(
                        paths,
                        project_ref,
                        step_id,
                        list(imported["imported"])[0],
                        mode=str(args.get("pagination_mode", "next-page")),
                        client=client,
                    )
                else:
                    captured["binding"] = project_core.set_rpa_step_elements(
                        paths,
                        project_ref,
                        step_id,
                        list(imported["imported"]),
                        client=client,
                    )
                captured["target_step"] = target_step
                if bool(args.get("prune_unused", False)):
                    captured["prune"] = project_core.prune_unused_rpa_elements(
                        paths,
                        str(args["project_ref"]),
                        keep_ids=list(imported["imported"]),
                    )
            if capture_session is not None:
                captured["capture_session"] = capture_session
        finally:
            try:
                native_ui["inspect_stop"] = bridge.stop_inspect()
            except Exception as exc:
                native_ui["inspect_stop_warning"] = str(exc)
            try:
                native_ui["native_mask"] = NativeMask(client=client).close(force=True)
            except Exception as exc:
                native_ui["native_mask_warning"] = str(exc)
        native_ui["access"] = access_ui
        captured["native_ui"] = native_ui
        if allow_rebind:
            captured["rebind"] = True
        if as_page_selector:
            captured["pagination_binding"] = True
        return captured
    if name == "shizai_rpa_update_step":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_update_step requires allow_write: true")
        return project_core.update_rpa_step_config(
            paths, str(args["flow_ref"]), str(args["step_id"]), args["config"], client=client,
            expected_revision=args.get("expected_revision"),
        )
    if name == "shizai_rpa_reload_editor":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_reload_editor requires allow_write: true")
        return project_core.reload_rpa_editor(
            paths,
            str(args["project_ref"]),
            expected_revision=str(args["expected_revision"]),
            client=client,
            ready_timeout=float(args.get("ready_timeout", 10)),
        )
    if name == "shizai_rpa_run_component":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_run_component requires allow_write: true")
        return project_core.run_rpa_component(
            paths,
            str(args["project_ref"]),
            str(args["step_id"]),
            expected_revision=str(args["expected_revision"]),
            client=client,
        )
    if name == "shizai_rpa_remove_step":
        dry_run = bool(args.get("dry_run", False))
        if not dry_run and not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_remove_step requires allow_write: true")
        return project_core.remove_rpa_step(
            paths,
            str(args["project_ref"]),
            str(args["step_id"]),
            expected_revision=str(args["expected_revision"]),
            client=None if dry_run else client,
            allow_dependents=bool(args.get("allow_dependents", False)),
            dry_run=dry_run,
        )
    if name == "shizai_rpa_save_editor":
        dry_run = bool(args.get("dry_run", False))
        if not dry_run and not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_save_editor requires allow_write: true")
        expected_revision = args.get("expected_revision")
        return project_core.save_rpa_editor(
            paths,
            str(args["project_ref"]),
            expected_revision=str(expected_revision) if expected_revision else None,
            dry_run=dry_run,
            client=None if dry_run else client,
        )
    if name == "shizai_rpa_components":
        return project_core.rpa_components(
            paths,
            flow_ref=args.get("flow_ref"),
            source_project_ref=args.get("source_project_ref"),
        )
    if name == "shizai_rpa_component_catalog":
        return component_catalog_core.component_catalog(
            paths,
            scope=str(args.get("scope", "all")),
            query=str(args.get("query", "")),
            category=str(args.get("category", "")),
            component_type=str(args.get("component_type", "")),
            package=str(args.get("package", "")),
            capability=str(args.get("capability", "")),
            capability_mode=str(args.get("capability_mode", "any")),
            top_level=str(args.get("top_level", "")),
            business_category=str(args.get("business_category", "")),
            business_package=str(args.get("business_package", "")),
            project_ref=args.get("project_ref"),
            page=int(args.get("page", 1)),
            limit=int(args.get("limit", 20)),
            detail=str(args.get("detail", "contract")),
            response_mode=str(args.get("response_mode", "full")),
            include_hidden=bool(args.get("include_hidden", True)),
            engine_root=args.get("engine_root"),
            business_info_path=args.get("business_info_path"),
            business_cache_root=args.get("business_cache_root"),
        )
    if name == "shizai_rpa_add_step":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_add_step requires allow_write: true")
        flow_ref = str(args["flow_ref"])
        native_ui = _rpa_design_ui_update(
            client,
            state="designing",
            message=f"塔斯正在设计流程组件：{args['component']}",
            flow_ref=flow_ref,
        )
        result = project_core.add_rpa_step(
            paths,
            flow_ref,
            str(args["component"]),
            step_id=args.get("step_id"),
            before_step_id=args.get("before_step_id"),
            label=args.get("label"),
            element_ids=args.get("element_ids"),
            config=args.get("config"),
            client=client,
        )
        result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_set_step_elements":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_set_step_elements requires allow_write: true")
        return project_core.set_rpa_step_elements(
            paths,
            str(args["flow_ref"]),
            str(args["step_id"]),
            list(args["element_ids"]),
            client=client,
        )
    if name == "shizai_rpa_read_flow":
        if args.get("step_id") is not None:
            raise ValueError("step_id requires detail=edit")
        return project_core.read_rpa_flow(paths, str(args["flow_ref"]))
    if name == "shizai_rpa_validate_step":
        flow_ref = str(args["flow_ref"])
        native_ui = _rpa_design_ui_update(
            client,
            state="validating",
            message="塔斯正在校验流程组件和元素绑定。",
            flow_ref=flow_ref,
        )
        result = project_core.validate_rpa_step(
            paths, flow_ref, str(args["step_id"]), strict=bool(args.get("strict", False))
        )
        if result.get("component") == "WebBrowser_DataCollectionV5":
            try:
                preview = project_core.preview_rpa_collection(
                    paths,
                    flow_ref,
                    str(args["step_id"]),
                    client=client,
                )
            except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                preview = {
                    "kind": "native_collection_preview",
                    "performed": False,
                    "valid": False,
                    "error": str(exc),
                    "message": "当前页面未能完成原生采集预览；静态校验结果不代表采集可运行。",
                }
            result["native_collection_preview"] = preview
            if preview.get("performed") and not preview.get("valid"):
                detail = str(preview.get("message") or "原生数据采集预览没有有效数据行。")
                result["valid"] = False
                result["errors"].append(detail)
                result["checks"].append(
                    {
                        "name": "native_collection_preview",
                        "passed": False,
                        "detail": detail,
                        "severity": "error",
                    }
                )
                result["next_action"] = preview.get("next_action")
                result["verification_gate"] = project_core._rpa_verification_gate(
                    flow_ref,
                    None,
                    {
                        "valid": False,
                        "step_id": str(args["step_id"]),
                        "component": "WebBrowser_DataCollectionV5",
                    },
                    runtime_evidence={"native_collection_preview": preview},
                )
        result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_debug_start":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_debug_start requires allow_write: true")
        return debug_core.debug_start(
            paths,
            str(args["project_ref"]),
            start_from=int(args.get("start_from", 1)),
            start_timeout=float(args.get("start_timeout", 30)),
            client=client,
        )
    if name == "shizai_rpa_debug_capabilities":
        return debug_core.debug_capabilities(paths, str(args["project_ref"]))
    if name == "shizai_rpa_debug_status":
        return debug_core.debug_status(paths, str(args["project_ref"]), client=client)
    if name == "shizai_rpa_debug_command":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_debug_command requires allow_write: true")
        operation = {
            "next": "next",
            "continue": "continue",
            "step_in": "stepIn",
            "step_out": "stepOut",
        }[str(args["action"])]
        result = debug_core.debug_command(
            paths,
            str(args["project_ref"]),
            operation,
            wait_seconds=float(args.get("wait_seconds", 15)),
            client=client,
        )
        return project_core.attach_rpa_debug_verification(
            paths,
            str(args["project_ref"]),
            result,
        )
    if name == "shizai_rpa_debug_stop":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_debug_stop requires allow_write: true")
        result = debug_core.debug_stop(paths, str(args["project_ref"]), client=client)
        # A successful debug stop is also the end of a design takeover. Close
        # every owned surface so a red picker/mask cannot survive the terminal
        # response and make the desktop appear stuck.
        cleanup = _rpa_cleanup_native_ui(client)
        if isinstance(result, dict):
            native_ui = result.get("native_ui")
            if not isinstance(native_ui, dict):
                native_ui = {}
                result["native_ui"] = native_ui
            native_ui["final_cleanup"] = cleanup
        return result
    if name == "shizai_rpa_validate_flow":
        flow_ref = str(args["flow_ref"])
        native_ui = _rpa_design_ui_update(
            client,
            state="validating",
            message="塔斯正在校验完整流程和数据契约。",
            flow_ref=flow_ref,
        )
        result = project_core.validate_rpa_flow(paths, flow_ref, strict=bool(args.get("strict", False)))
        result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_materialize":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_materialize requires allow_write: true")
        project_ref = str(args["project_ref"])
        _rpa_design_ui_update(
            client,
            state="validating",
            message="塔斯正在完成流程生成并校验运行文件。",
            flow_ref=project_ref,
        )
        try:
            result = project_core.materialize_native_rpa(
                paths,
                project_ref,
                strict=bool(args.get("strict", True)),
                client=client,
            )
        except Exception:
            _rpa_design_ui_finish(
                client,
                flow_ref=project_ref,
                succeeded=False,
                message="塔斯生成流程时遇到校验问题。",
            )
            raise
        result["native_ui"] = _rpa_design_ui_finish(
            client,
            flow_ref=project_ref,
            succeeded=True,
            message="塔斯已完成流程设计。",
        )
        return result
    if name == "shizai_rpa_sync_diagnose":
        return project_core.native_rpa_sync_diagnose(paths, str(args["project_ref"]))
    if name == "shizai_rpa_run":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_run requires allow_write: true")
        project_ref = str(args["project_ref"])
        bridge = ShizaiExtensionBridge(desktop=client)
        card = NativeSmallCard(client=client)
        native_ui: dict[str, Any] = {}
        try:
            native_ui["stale_ui_cleanup"] = _rpa_cleanup_native_ui(client)
        except (ShizaiCloudError, OSError, TimeoutError, ValueError, AttributeError) as exc:
            native_ui["stale_ui_cleanup_warning"] = str(exc)
        try:
            native_ui["preflight_card"] = _rpa_design_ui_update(
                client,
                state="validating",
                message="塔斯正在校验流程运行环境，按 Esc 可停止当前电脑操作。",
                flow_ref=project_ref,
            )
        except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
            native_ui["preflight_card_warning"] = str(exc)
        runtime_extension_snapshot: dict[str, Any] = {}
        try:
            status_reader = getattr(bridge, "runtime_extension_status", None)
            if callable(status_reader):
                runtime_extension_snapshot = status_reader()
        except (OSError, ValueError) as exc:
            native_ui["runtime_extension_status_warning"] = str(exc)
        browser_extension_required = False
        try:
            persisted = project_core.read_rpa_flow(paths, project_ref)
            persisted_flow = persisted.get("flow") if isinstance(persisted, dict) else {}
            persisted_steps = persisted_flow.get("steps") if isinstance(persisted_flow, dict) else []
            browser_extension_required = any(
                str(step.get("component") or "").startswith("WebBrowser_")
                for step in persisted_steps
                if isinstance(step, dict)
            )
        except (FileNotFoundError, OSError, ValueError):
            pass
        if browser_extension_required and runtime_extension_snapshot.get("state") == "installed_disabled":
            disabled_extensions = runtime_extension_snapshot.get("disabled_extensions") or []
            disabled_extension = (
                disabled_extensions[0] if disabled_extensions and isinstance(disabled_extensions[0], dict) else {}
            )
            extension_id = str(disabled_extension.get("id") or "").strip()
            activation_url = f"chrome://extensions/?id={extension_id}" if extension_id else "chrome://extensions/"
            user_prompt = (
                "实在 Agent 浏览器插件已安装，但当前被 Chrome 禁用。"
                f"请打开 {activation_url} 并启用插件，完成后确认，我再继续运行；已设计流程会保留。"
            )
            interaction = {
                "required": True,
                "status": "waiting_for_user",
                "action": "enable_browser_extension",
                "pause_automation": True,
                "user_prompt": user_prompt,
                "activation_url": activation_url,
                "extension_id": extension_id or None,
            }
            try:
                native_ui["terminal_card"] = card.update(
                    state="waiting",
                    message=user_prompt,
                    duration=0.8,
                )
            except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                native_ui["terminal_card_warning"] = str(exc)
            return {
                "kind": "native_rpa_run",
                "started": False,
                "status": "waiting_for_user",
                "failure_kind": "runtime_plugin_disabled",
                "interaction_required": True,
                "interaction": interaction,
                "preflight": {
                    "kind": "rpa_runtime_preflight",
                    "status": "waiting_for_user",
                    "valid": False,
                    "reason": "runtime_plugin_disabled",
                    "runtime_extension": runtime_extension_snapshot,
                    "interaction": interaction,
                    "next_action": {"kind": "user_interaction", "action": "pause_and_ask_user"},
                },
                "native_ui": native_ui,
                "message": user_prompt,
                "next_action": {"kind": "user_interaction", "action": "pause_and_ask_user"},
            }
        preflight_recovery: dict[str, Any] = {}
        diagnosis = project_core.native_rpa_sync_diagnose(paths, project_ref)
        if not diagnosis.get("can_run") and diagnosis.get("materialization_required"):
            try:
                preflight_recovery["materialization"] = project_core.materialize_native_rpa(
                    paths,
                    project_ref,
                    strict=True,
                    client=client,
                )
                diagnosis = project_core.native_rpa_sync_diagnose(paths, project_ref)
            except (FileNotFoundError, OSError, TimeoutError, ValueError) as exc:
                preflight_recovery["materialization_error"] = str(exc)
        if not diagnosis.get("can_run"):
            try:
                native_ui["terminal_card"] = card.update(
                    state="error",
                    message="塔斯运行文件或环境诊断未通过。",
                    duration=0.8,
                )
            except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                native_ui["terminal_card_warning"] = str(exc)
            return {
                "kind": "native_rpa_run",
                "started": False,
                "status": "blocked",
                "failure_kind": "runtime_not_ready",
                "preflight": {
                    "kind": "rpa_runtime_preflight",
                    "mode": "direct_cli",
                    "valid": False,
                    "reason": "runtime_not_ready",
                    "errors": diagnosis.get("errors") or [],
                    "warnings": diagnosis.get("warnings") or [],
                    "materialization_required": bool(diagnosis.get("materialization_required")),
                },
                "preflight_recovery": preflight_recovery,
                "native_ui": native_ui,
                "message": "流程运行文件或环境诊断未通过，尚未提交正式运行。",
                "next_action": {"tool": "shizai_rpa_sync_diagnose"},
            }
        try:
            preflight_recovery["runtime_extension"] = bridge.activate_runtime_extension()
        except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
            preflight_recovery["runtime_extension_warning"] = str(exc)
        runtime_extension = preflight_recovery.get("runtime_extension")
        runtime_extension_status = (
            runtime_extension.get("after") or runtime_extension.get("before") or {}
            if isinstance(runtime_extension, dict)
            else {}
        )
        preferred_extension = (
            runtime_extension_status.get("preferred_extension") if isinstance(runtime_extension_status, dict) else None
        )
        extension_installed_enabled = bool(
            isinstance(preferred_extension, dict)
            and preferred_extension.get("installed")
            and preferred_extension.get("enabled")
        )
        runtime_extension_ready = bool(isinstance(runtime_extension, dict) and runtime_extension.get("ready"))
        direct_ready = bool(not browser_extension_required or runtime_extension_ready or extension_installed_enabled)
        preflight = {
            "kind": "rpa_runtime_preflight",
            "mode": "direct_cli",
            "status": "ready" if direct_ready else "blocked",
            "valid": direct_ready,
            "reason": "validated_direct_run" if direct_ready else "runtime_plugin_unavailable",
            "diagnosis": {
                "can_run": bool(diagnosis.get("can_run")),
                "materialization_required": bool(diagnosis.get("materialization_required")),
            },
            "browser_extension_required": browser_extension_required,
            "page_scan_performed": False,
            "capture_session_started": False,
            "deep_capture_touched": False,
            "message": (
                "完整流程已通过严格校验，正式运行将直接提交给实在 Agent CLI 运行通道。"
                if direct_ready
                else "实在 Agent 浏览器插件运行通道尚未就绪，流程未提交。"
            ),
        }
        if not direct_ready:
            preflight.update(
                {
                    "reason": "runtime_plugin_unavailable",
                    "failure_kind": "runtime_plugin_unavailable",
                    "runtime_extension": runtime_extension,
                    "next_action": {
                        "tool": "shizai_extension_status",
                        "reason": "确认 Chrome、MV3 插件和 Native Messaging Host 均为 ready 后重试运行。",
                    },
                }
            )
        elif browser_extension_required and not runtime_extension_ready:
            preflight_recovery["runtime_extension_deferred_probe"] = {
                "accepted": True,
                "reason": "mv3_extension_installed_enabled_host_is_on_demand",
                "message": (
                    "MV3 插件已安装并启用；Native Messaging Host 按需启动，"
                    "提交一次真实运行并仅使用本次新增 RuntimeHost 日志判定结果。"
                ),
            }
        if not preflight.get("valid"):
            try:
                native_ui["terminal_card"] = card.update(
                    state="error",
                    message="塔斯运行前校验未通过。",
                    duration=0.8,
                )
            except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                native_ui["terminal_card_warning"] = str(exc)
            return {
                "kind": "native_rpa_run",
                "started": False,
                "status": "blocked",
                "failure_kind": preflight.get("failure_kind"),
                "interaction_required": False,
                "interaction": None,
                "preflight": preflight,
                "preflight_recovery": preflight_recovery,
                "native_ui": native_ui,
                "message": preflight.get("message"),
                "next_action": preflight.get("next_action"),
                "ui_feedback": preflight.get("ui_feedback"),
            }
        native_ui["takeover"] = _rpa_design_ui_ensure_takeover(
            client,
            flow_ref=project_ref,
            message="塔斯正在接管你的电脑并执行已校验流程。",
        )
        try:
            native_ui["stale_mask_close"] = NativeMask(client=client).close(force=True)
        except (ShizaiCloudError, OSError, TimeoutError, ValueError, AttributeError) as exc:
            native_ui["stale_mask_close_warning"] = str(exc)
        input_param = args.get("input_param")
        if input_param is not None and not isinstance(input_param, dict):
            raise ValueError("input_param must be a JSON object")
        wait = bool(args.get("wait", True))
        native_ui["preflight_recovery"] = preflight_recovery
        try:
            native_ui["running_card"] = card.update(
                state="running",
                message="塔斯正在运行流程，按 Esc 可停止当前电脑操作。",
            )
        except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
            native_ui["running_card_warning"] = str(exc)
        try:
            result = project_core.native_rpa_run(
                paths,
                project_ref,
                input_param=input_param,
                run_uuid=args.get("run_uuid"),
                workflow_run_id=args.get("workflow_run_id"),
                start_take_work=bool(args.get("start_take_work", False)),
                wait=wait,
                wait_seconds=(float(args["wait_seconds"]) if args.get("wait_seconds") is not None else None),
                client=client,
            )
        except Exception:
            # A run can fail before the native CloseWeb component is reached.
            # The design helper is still explicitly owned by this project, so
            # release it on the error path as well.  This never touches a
            # user's pre-existing tab.
            native_ui["design_tab_cleanup"] = _rpa_close_owned_design_tab(
                project_ref, reason="run_exception"
            )
            if wait:
                try:
                    native_ui["terminal_card"] = card.update(
                        state="error",
                        message="塔斯运行流程时遇到问题。",
                        duration=0.5,
                    )
                except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                    native_ui["terminal_card_warning"] = str(exc)
            raise
        if wait:
            business_status = str((result.get("status") or {}).get("status") or "").casefold()
            succeeded = business_status == "succeeded"
            waiting = business_status == "waiting_for_user"
            # Release the MCP-owned design tab for every terminal outcome.
            # Keep it only while the flow is waiting for a user login/challenge
            # so that the user can finish the interaction in the same page.
            terminal_statuses = {"succeeded", "failed", "stopped"}
            if business_status in terminal_statuses:
                result["design_tab_cleanup"] = _rpa_close_owned_design_tab(
                    project_ref, reason=f"run_{business_status}"
                )
            try:
                native_ui["terminal_card"] = card.update(
                    state="waiting" if waiting else "success" if succeeded else "error",
                    message=(
                        str(
                            (result.get("interaction") or {}).get("user_prompt") or "请完成当前网页交互后重新运行流程。"
                        )
                        if waiting
                        else "塔斯已完成流程运行。"
                        if succeeded
                        else "塔斯运行流程时遇到问题。"
                    ),
                    duration=0.8 if waiting else 0.5,
                )
            except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                native_ui["terminal_card_warning"] = str(exc)
        if isinstance(result, dict):
            preflight["design_validation_cleanup"] = result.get("design_validation_cleanup")
            result["preflight"] = preflight
            result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_status":
        project_ref = str(args["project_ref"])
        result = project_core.native_rpa_status(paths, project_ref)
        # Status remains read-only. Async callers receive a write-gated
        # finalization action after observing a confirmed terminal state.
        status_value = str(result.get("status") or "").casefold() if isinstance(result, dict) else ""
        if status_value in {"succeeded", "failed", "stopped"}:
            result["browser_cleanup_action"] = {
                "tool": "shizai_rpa_browser_session_stop",
                "arguments": {"project_ref": project_ref, "close_browser": True, "allow_write": True},
                "reason": "任务已结束；关闭本流程的捕获会话和 MCP 扩展创建的设计标签页。",
            }
        return result
    if name == "shizai_rpa_stop":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_stop requires allow_write: true")
        project_ref = str(args["project_ref"])
        result = project_core.native_rpa_stop(paths, project_ref, client=client)
        if result.get("status") == "stopped":
            result["design_tab_cleanup"] = _rpa_close_owned_design_tab(project_ref, reason="user_stop")
        return result
    if name == "shizai_draft":
        return agent_core.query_draft(agent_id=args.get("agent_id"), client=client)
    if name == "shizai_validate_draft":
        return agent_core.validate_draft(
            args["draft"],
            agent_id=args.get("agent_id"),
            strict=bool(args.get("strict", False)),
            client=client,
        )
    if name == "shizai_apply_flow":
        return agent_core.apply_flow(
            args["draft"],
            agent_id=args.get("agent_id"),
            query=args.get("query"),
            strict=bool(args.get("strict", False)),
            run=bool(args.get("run", False)),
            publish=bool(args.get("publish", False)),
            publish_range=str(args.get("publish_range", "only_me")),
            allow_write=bool(args.get("allow_write", False)),
            client=client,
        )
    if name == "shizai_run_draft":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_run_draft requires allow_write: true")
        return agent_core.run_draft(
            str(args["query"]),
            agent_id=args.get("agent_id"),
            conversation_id=args.get("conversation_id"),
            timeout=float(args["timeout"]) if args.get("timeout") is not None else None,
            client=client,
        )
    if name == "shizai_runs":
        return agent_core.query_agent_resource(
            "getAdvancedChatWorkflowRuns", [{"appId": _resolved_agent_id(client, args.get("agent_id"))}], client=client
        )
    if name == "shizai_logs":
        return agent_core.query_agent_resource(
            "getAgentLogDetail",
            [
                {
                    "appId": _resolved_agent_id(client, args.get("agent_id")),
                    "params": {"conversation_id": args["conversation_id"], "message_id": args["message_id"]},
                }
            ],
            client=client,
        )
    if name == "shizai_tracing":
        return agent_core.query_agent_resource(
            "getTracingList",
            [{"appId": _resolved_agent_id(client, args.get("agent_id")), "runID": args["run_id"]}],
            client=client,
        )
    if name == "shizai_datasets":
        return agent_core.query_datasets(
            page=int(args.get("page", 1)),
            limit=int(args.get("limit", 20)),
            keyword=str(args.get("keyword", "")),
            client=client,
        )
    if name == "shizai_project_list":
        return project_core.list_projects_compact(
            paths,
            query=str(args.get("query") or ""),
            limit=int(args.get("limit", 20)),
        )
    if name == "shizai_project_inspect":
        return project_core.inspect_project(paths, args["project_ref"])
    if name == "shizai_project_elements":
        return project_core.read_elements(paths, args["project_ref"])
    if name == "shizai_project_import_elements":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_project_import_elements requires allow_write: true")
        source_project_ref = str(args.get("source_project_ref") or "").strip()
        if source_project_ref:
            if args.get("elements") not in (None, []):
                raise ValueError("provide elements or source_project_ref + element_ids, not both")
            return project_core.import_verified_reference_elements(
                paths,
                args["project_ref"],
                source_project_ref,
                list(args.get("element_ids") or []),
                replace=bool(args.get("replace", False)),
                force_replace=bool(args.get("force_replace", False)),
            )
        if args.get("elements") is None:
            raise ValueError("provide elements or source_project_ref + element_ids")
        return project_core.import_elements(
            paths,
            args["project_ref"],
            args["elements"],
            replace=bool(args.get("replace", False)),
            force_replace=bool(args.get("force_replace", False)),
            capture_source="model_candidate",
        )
    if name == "shizai_rpa_validate_elements":
        if args.get("project_ref"):
            return project_core.validate_project_elements(
                paths, str(args["project_ref"]), strict=bool(args.get("strict", False))
            )
        if args.get("elements") is None:
            raise ValueError("provide project_ref or elements")
        return project_core.validate_captured_elements(args["elements"], strict=bool(args.get("strict", False)))
    if name == "shizai_rpa_validate_project":
        return project_core.validate_rpa_project(
            paths, str(args["project_ref"]), strict=bool(args.get("strict", False))
        )
    raise ValueError(f"Unknown MCP tool: {name}")


def _dispatch(name: str, args: dict[str, Any]) -> Any:
    """Keep progress cards scoped to finite calls, and clean every surface on stop."""
    close_all = name in {"shizai_rpa_run", "shizai_rpa_stop"} and bool(args.get("allow_write", False))
    finite_read = name in {
        "shizai_rpa_analyze_request", "shizai_rpa_capture_windows", "shizai_rpa_scan_page",
        "shizai_rpa_validate_step", "shizai_rpa_validate_flow",
    }
    finite_write = name in {
        "shizai_rpa_start_design", "shizai_rpa_add_step", "shizai_rpa_capture_page_fields",
        "shizai_rpa_capture_elements", "shizai_rpa_run_component",
    } and bool(args.get("allow_write", False))
    if not (close_all or finite_read or finite_write):
        return _dispatch_impl(name, args)

    result: Any = None
    try:
        result = _dispatch_impl(name, args)
        return result
    finally:
        cleanup = (
            _rpa_cleanup_native_ui(_desktop_client())
            if close_all else _rpa_close_progress_card(_desktop_client())
        )
        if isinstance(result, dict):
            native_ui = result.setdefault("native_ui", {})
            if isinstance(native_ui, dict):
                native_ui["final_cleanup" if close_all else "progress_card_cleanup"] = cleanup


async def _dispatch_in_worker(name: str, arguments: dict[str, Any]) -> Any:
    """Keep blocking desktop RPCs off the MCP protocol event loop."""
    return await anyio.to_thread.run_sync(
        _dispatch,
        name,
        arguments,
        abandon_on_cancel=True,
    )


def _mcp_tool_content(name: str, result: Any) -> list[Any]:
    if name == "shizai_browser_capture_viewport" and isinstance(result, dict):
        data_url = result.get("dataUrl")
        if isinstance(data_url, str) and data_url.startswith("data:image/png;base64,"):
            metadata = {key: value for key, value in result.items() if key != "dataUrl"}
            return [
                TextContent(
                    type="text",
                    text=json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
                ),
                ImageContent(type="image", data=data_url.split(",", 1)[1], mimeType="image/png"),
            ]
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))]


def build_server(*, profile: str | None = None) -> Any:
    """Build the low-level MCP server with explicit JSON schemas."""
    if Server is None:
        raise RuntimeError("MCP support is not installed; run python -m pip install -e .")
    profile = profile or os.environ.get("SHIZAI_MCP_TOOL_PROFILE", "full")
    advertised = _schemas(profile)
    names = {tool.name for tool in advertised}
    instructions = RPA_AUTHORING_INSTRUCTIONS if profile == "rpa-authoring" else MCP_INSTRUCTIONS
    server = Server(
        "shizai-agent",
        version=__version__,
        instructions=instructions,
    )

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return advertised

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        if name not in names:
            raise ValueError(f"工具不在当前 {profile} 工具集中：{name}")
        try:
            arguments = dict(arguments or {})
            if name in {"shizai_rpa_component_catalog", "shizai_rpa_start_design"}:
                if profile == "rpa-authoring":
                    arguments["response_mode"] = "compact"
                else:
                    arguments.setdefault("response_mode", "full")
            if name == "shizai_rpa_component_catalog" and profile == "rpa-authoring":
                arguments.setdefault("include_hidden", False)
                if str(arguments.get("component_type") or "").strip():
                    arguments["limit"] = min(int(arguments.get("limit", 1)), 5)
                else:
                    arguments["detail"] = "summary"
                    arguments["limit"] = min(int(arguments.get("limit", 20)), 20)
            result = await _dispatch_in_worker(name, arguments)
            if profile == "rpa-authoring" and isinstance(result, dict):
                result = authoring_result(name, result, arguments)
            elif name == "shizai_rpa_start_design" and arguments.get("response_mode") == "compact":
                result = compact_design_result(result)
        except WorkflowEditError as exc:
            return CallToolResult(
                isError=True, structuredContent=exc.result,
                content=[TextContent(type="text", text=json.dumps(exc.result, ensure_ascii=False))],
            )
        except Exception as exc:
            # Keep polling clients truthful when a write or capture operation
            # fails after publishing its in-progress state.
            set_rpa_ui_feedback(
                "error",
                "tool_call",
                f"MCP 工具 {name} 执行失败：{exc}",
                animation="error",
                overlay_state="unknown",
                tool=name,
            )
            _rpa_cleanup_native_ui(_DESKTOP_CLIENT)
            raise
        return _mcp_tool_content(name, result)

    return server


async def _run_stdio() -> None:
    server = build_server()
    _browser_bridge_manager().ensure_started()
    notification_options = SimpleNamespace(prompts_changed=False, resources_changed=False, tools_changed=False)
    initialization_options = InitializationOptions(
        server_name="shizai-agent",
        server_version=__version__,
        capabilities=server.get_capabilities(notification_options=notification_options, experimental_capabilities={}),
        instructions=server.instructions,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, initialization_options)
    finally:
        _rpa_cleanup_native_ui(_DESKTOP_CLIENT)
        if _BROWSER_BRIDGE_MANAGER is not None:
            _BROWSER_BRIDGE_MANAGER.close()


def main() -> None:
    """Run the MCP server over stdio for desktop MCP clients."""
    # stdio_server() is implemented on AnyIO.  Use its native runner and guard
    # the frozen process's stderr against late shutdown diagnostics.
    install_shutdown_safe_stderr()
    anyio.run(_run_stdio)
    if getattr(sys, "frozen", False):
        # PyInstaller's frozen interpreter can emit a non-fatal closed-stderr
        # traceback while finalizing AnyIO's stdio workers.  _run_stdio has
        # already completed its native-UI cleanup at this point.
        os._exit(0)


if __name__ == "__main__":
    main()
