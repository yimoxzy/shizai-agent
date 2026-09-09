"""MCP server exposing the same core surface as the Shizai CLI.

The low-level MCP server is used instead of FastMCP because the bundled desktop
runtime currently carries a FastMCP/Pydantic combination that cannot build tool
schemas for ordinary annotated parameters. The protocol surface stays standard.
"""

import hashlib
import json
import os
import socket
import sys
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
from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent
from .extension_bridge import ShizaiExtensionBridge, set_rpa_ui_feedback
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
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
except ImportError:  # pragma: no cover - exercised only when the optional dependency is absent
    Server = None  # type: ignore[assignment,misc]
    InitializationOptions = None  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]
    TextContent = None  # type: ignore[assignment,misc]
    Tool = None  # type: ignore[assignment,misc]


def _paths() -> ShizaiPaths:
    return ShizaiPaths.from_environment()


_DESKTOP_CLIENT: ShizaiDesktopAgent | None = None
_ACTIVE_RPA_DESIGN_UI: dict[str, dict[str, Any]] = {}
_LATEST_RPA_DESIGN_REF: str | None = None
_RPA_SMALL_CARD_OPEN = False
# Keep the immutable analysis available for the next MCP call.  Models often
# compact a large tool result while composing a plan; the id is enough to
# recover the original contract without asking them to re-send every field.
_RPA_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}


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
    planned_navigation = _rpa_navigation_plan_step(analysis, plan, paths=paths)
    step_id = planned_navigation["step_id"]
    materialized = flow.get("steps") if isinstance(flow.get("steps"), list) else []
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


MCP_INSTRUCTIONS = """这是共享 Shizai CLI 核心的薄 stdio 包装，不新增第二套流程引擎，也不内置站点配方。
连接自检：如果初始化后看不到至少一个 shizai_* 工具，尤其是 shizai_rpa_analyze_request 和 shizai_rpa_component_catalog，说明当前对话没有挂载 shizai-agent MCP，不是浏览器故障。此时禁止自行编写 MCP 客户端、另起一套 stdio 进程或声称流程已完成；应由宿主执行 npx -y shizai-agent-mcp@latest install，重启 Codex，并在 /mcp 确认服务和工具列表后再开始。
调用方大模型负责理解意图、选择组件、安排图结构、变量、循环、条件、分页、JavaScript 和输出方案；处理 RPA 请求时先调用 shizai_rpa_analyze_request，再读取实时组件目录，使用真实 component type 和参数名。validate_plan 只是轻量边界检查。
原生 RPA 区分写图和验证：start_design 可先写完整图，再按组件验证；使用 add_step 增量追加时，读取 verification_gate，先静态校验，再按 debug_capabilities -> debug_start/复用 -> debug_command(action=next) -> debug_status 执行当前组件，用页面或输出证据确认后才能追加下一组件。静态通过、请求已接收、DAP 已连接或单纯停靠都不是运行成功；失败修复同一 project_ref。
已有流程先 read_flow(detail=edit)，按稳定 step_id 读取参数；update_step(dry_run=true) 预览，带 expected_revision 提交。版本冲突重读，不重建流程。
start_design 可以持久化模型选择的完整流程，open_browser 默认是 false。捕获、更新、校验、诊断和运行都复用同一个 project_ref；只有明确要求新应用时才使用 force_new。未从同一实时页面通过 DeepCapture 捕获的 selector 只能作为草稿。
只有写入、电脑控制、捕获、调试和运行工具需要 allow_write=true。遇到登录或页面挑战先暂停，不保存凭据。session_start/status/stop 管理项目绑定的 DeepCapture 读取会话，extension_start_capture 管理官方拾取层，debug_start/command 管理 RuntimeHost；OCR 只能确认可见结果，不能替代 selector 或鼠标控制。
页面身份变化时保留 project_ref，重新扫描并捕获；只有明确传 allow_rebind=true 才替换已有绑定。调试停止必须读取 stopped 或 cleanup_timeout 终态；不要等待一个永远停留的 stopping。一个 MCP stdio 进程应从 initialize 持续复用到本次流程结束，不要为每个工具调用重新启动进程，否则内存中的分析、DeepCapture 快照和调试会话会丢失。
新增通用规则时，放入共享核心返回的结构化 context；只有所有任务都适用的安全边界才写入本说明。业务规则使用中文，站点配方和固定流程不要硬编码到 MCP。源码 CLI 入口是 shizai_agent/shizai_agent_cli.py，MCP 入口是 shizai_agent/core/mcp_server.py；客户端安装、注册和新对话测试提示词见 docs/MCP_CLIENT_SETUP.md。"""


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
            "Start design-time validation for a fully written native .task at component N. It connects the debugger and returns the paused component; this is design validation, not a production run. The complete graph must already exist. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "start_from": {"type": "integer", "minimum": 1, "default": 1},
                "start_timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 180, "default": 30},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_debug_capabilities",
            "Probe a native .task's real component-level single-step support, generated breakpoint readiness, and whether the current MCP process already has a reusable debug session. This is read-only and never opens Chrome or starts AppRunService.",
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
            "Advance the active design_validation session over the already-written component graph. next executes through the next component breakpoint; continue runs to the next stop or completion; step_in and step_out send the corresponding DAP command. This does not represent a production run. Requires allow_write.",
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
            "只读分析用户原句，返回意图、操作、字段、数量、分页要求与输出验收条件。保留 query 和 analysis_id；模型依据实时目录选组件并预检，分析结果不规定流程方案。",
            {"query": {"type": "string", "minLength": 1}},
            ["query"],
        ),
        _tool(
            "shizai_rpa_validate_plan",
            "只读预检模型计划的结构、实时组件可用性和物化能力、参数名、引用类型及安全边界。模型决定步骤与输出策略；通过不代表可执行或业务成功。",
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
            "shizai_rpa_ui_status",
            "Read the latest native RPA capture UI phase. Use it to show takeover, element-picker, scanning, ready, waiting, or idle feedback while the real Shizai extension owns the visible overlay.",
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
            "Stop the project-bound DeepCapture session and official picker overlay if active. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_ui_feedback",
            "Control the original designer-owned TARS small card or blue run mask. The RPA tools already update these automatically by real phase; use this only for an explicit UI-only action. scan_demo is a picker demonstration and must not be used as ordinary workflow progress.",
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
            "Return the live designer and algorithm-tool contract plus the component-first planning policy. The MCP client model must decompose the request and design the draft; this tool never calls TaSi or another model to generate it.",
            {"agent_id": {"type": "string"}, "query": {"type": "string"}},
        ),
        _tool(
            "shizai_rpa_start_design",
            "Create or reuse a native RPA application from a structurally validated model plan. Matching requests reuse projects. Strategy stays with the model; component and safety checks remain enforced. No site flow. Requires allow_write.",
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
            "Run the saved navigation step through native DAP before capturing pages. The plan must create or obtain required browser objects first; saving a node does not open Chrome. Call after start_design, then capture_windows and scan_page. Also recovers missing browser windows. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "allow_write"],
        ),
        _tool(
            "shizai_rpa_capture_windows",
            "Read current Chrome windows from the initialized DeepCapture session. This is read-only and does not enable the red click/hover picker. If no Chrome page exists it returns shizai_rpa_open_browser as the only valid next action and does not enter takeover/scanning UI.",
            {
                "project_ref": {"type": "string"},
                "include_non_chrome": {"type": "boolean", "default": False},
            },
        ),
        _tool(
            "shizai_rpa_capture_tree",
            "Read a bounded tree from an already started DeepCapture session. A Chrome UIA chain is webpage-ready only when Document is followed by a web control such as Group, Edit, Text, or Button; Window/Pane alone is the native shell. This read is side-effect free and does not reset inspect mode.",
            {
                "element_id": {"type": "string"},
                "level": {"type": "integer", "minimum": 0, "maximum": 20, "default": 6},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 120},
            },
            ["element_id"],
        ),
        _tool(
            "shizai_rpa_scan_page",
            "Scan the current Chrome Document for semantic candidates. Results contain ids and hints; use scan_token with shizai_rpa_capture_elements for native selectors. Reuses the active session and never opens Chrome. Set allow_rebind=true only for page-change recovery.",
            {
                "root_element_id": {
                    "type": "string",
                    "description": "Optional Chrome window or Document element id; omit to inspect current Chrome windows.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional semantic filter such as 商品名称、价格、销量 or 搜索框.",
                },
                "scan_token": {
                    "type": "string",
                    "description": "Optional existing token to return the same cached scan snapshot without a new tree traversal.",
                },
                "level": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 250},
                "page_title": {"type": "string"},
                "page_url": {"type": "string"},
                "project_ref": {
                    "type": "string",
                    "description": "Active flow reference used to produce an executable recovery action when Chrome or Document is missing.",
                },
                "step_id": {
                    "type": "string",
                    "description": "Existing element component whose role drives this scan and any picker fallback.",
                },
                "allow_rebind": {
                    "type": "boolean",
                    "default": False,
                    "description": "Rescan a bound step for replacement after a page change.",
                },
            },
        ),
        _tool(
            "shizai_rpa_runtime_preflight",
            "Read-only preflight before executing an element-dependent native RPA component. It detects missing Chrome/Document, login or challenge pages, and page-title/URL changes against the captured selector context; it never opens Chrome or sends a DAP command.",
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
            "shizai_rpa_capture_elements",
            "Import native selectors from the current Chrome snapshot and bind them to a written element step. Use the scan_token from shizai_rpa_scan_page when applicable. Set allow_rebind=true only after a page change; force_replace=true rebuilds all bindings.",
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
                "allow_write": {"type": "boolean", "default": False},
            },
            ["element_ids", "project_ref", "step_id", "allow_write"],
        ),
        _tool(
            "shizai_rpa_update_step",
            "修改现有步骤参数。先 read_flow(detail=edit, step_id=...)；dry_run=true 只预览原生差异。提交携带 expected_revision 和 allow_write=true；冲突后重读。保存后静态校验不等于运行成功。",
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
            "shizai_rpa_components",
            "List the complete live native component surface and, when supplied, the current application's component state. The client model chooses components and graph order.",
            {"flow_ref": {"type": "string"}, "source_project_ref": {"type": "string"}},
        ),
        _tool(
            "shizai_rpa_component_catalog",
            "查询实时原生组件的参数、输出、能力及物化状态。先 summary 筛选，再 component_type 精确读取 contract；模型自主编排。筛选条件不规定业务方案。",
            {
                "scope": {"type": "string", "enum": ["builtin", "business", "all"], "default": "all"},
                "query": {"type": "string", "default": "", "description": "组件名称、说明或 type 的关键词；空格分词按 AND 匹配。先用一个关键词，不要把多个替代组件名称拼成一次查询。"},
                "category": {"type": "string", "default": ""},
                "component_type": {"type": "string", "default": ""},
                "include_hidden": {"type": "boolean", "default": True, "description": "是否列出设计器隐藏的组件；精确 component_type 查询始终可读旧流程使用的类型。"},
                "package": {"type": "string", "default": ""},
                "capability": {
                    "type": "string",
                    "default": "",
                    "description": "Comma-separated required capabilities, for example navigate,input,submit,extract,paginate,output.",
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
                    "description": "Visible first-level menu id or label, such as webBrowser or 网页浏览器.",
                },
                "business_category": {"type": "string", "default": ""},
                "business_package": {"type": "string", "default": ""},
                "project_ref": {
                    "type": "string",
                    "description": "Optional local client project; reports whether a business package is imported into it.",
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
            "Append a model-selected component using the live catalog. The response includes verification_gate; verify the current component before appending another. Values support {variable: NAME} and {expression: EXPR}. Requires allow_write.",
            {
                "flow_ref": {"type": "string"},
                "component": {"type": "string"},
                "step_id": {"type": "string"},
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
            "Static-check one written RPA component and return verification_gate; use debug tools for execution evidence. Checks native arguments, elements, outputs, columns, and Excel bindings.",
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
            "Run a validated client-owned native RPA task through CorpBrainService.workflowDebugRunRPA and wait for a truthful terminal status. Runtime success without a fresh configured output is reported as failure_kind=missing_fresh_output. Set wait=false to submit and poll shizai_rpa_status. The MCP resolves application/package/task ids from the local package index. Requires allow_write.",
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
            "Read local Native RPA run state, output-file freshness/quality evidence, matching current-run designer logs, truthful business status, cleanup status, and current runtime diagnosis. It also publishes ui_feedback for running, stopping, completed, blocked, failed, or cleanup-timeout computer operations. This does not call the cloud API.",
            {"project_ref": {"type": "string"}},
            ["project_ref"],
        ),
        _tool(
            "shizai_rpa_stop",
            "Stop the current Native RPA run through CorpBrainService.workflowDebugStop, persist stopping then a confirmed stopped or cleanup_timeout result, and close the native run mask when possible. The operation is repeatable when no run is active. Requires allow_write.",
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
        _tool("shizai_project_list", "List local Shizai projects."),
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
            "Import browser elements into elements.json. JSON/model selectors are unverified drafts and cannot run; only DeepCapture captures are verified. replace is guarded when steps reference elements; force_replace rebuilds bindings. Requires allow_write.",
            {
                "project_ref": {"type": "string"},
                "elements": {"type": "array", "items": {"type": "object"}},
                "replace": {"type": "boolean", "default": False},
                "force_replace": {"type": "boolean", "default": False},
                "allow_write": {"type": "boolean", "default": False},
            },
            ["project_ref", "elements"],
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
            "Validate a native RPA project, including captured elements, live component contracts, graph references, and runtime safety. Model-owned plans may begin with any installed component; browser navigation is required only when the selected workflow needs it.",
            {"project_ref": {"type": "string"}, "strict": {"type": "boolean", "default": False}},
            ["project_ref"],
        ),
    ]


def _schemas(profile: str = "full") -> list[Any]:
    tools = _all_schemas()
    if profile == "full":
        return tools
    if profile == "rpa-authoring":
        for tool in tools:
            if tool.name in {"shizai_rpa_component_catalog", "shizai_rpa_start_design"}:
                tool.inputSchema["properties"].pop("response_mode", None)
            if tool.name == "shizai_rpa_component_catalog":
                tool.inputSchema["properties"]["include_hidden"]["default"] = False
            if tool.name == "shizai_rpa_start_design":
                # A legacy metadata delay, frequently mistaken for an RPC
                # timeout. Authoring clients configure delay nodes in plan.
                tool.inputSchema["properties"].pop("wait_seconds", None)
                tool.inputSchema["required"].remove("query")
                tool.inputSchema["properties"]["query"]["description"] = "可省略，使用 analysis_id 缓存中的原句；显式提供时须与原句一致。"
        return [tool for tool in tools if tool.name in RPA_AUTHORING_TOOLS]
    raise ValueError("SHIZAI_MCP_TOOL_PROFILE 必须为 full 或 rpa-authoring")


def _resolved_agent_id(client: ShizaiDesktopAgent, value: str | None) -> str:
    return value or client.config.agent_id


def _rpa_plan_preflight(
    paths: ShizaiPaths,
    analysis: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Use the shared read-only boundary without an arbitrary correction cap."""
    live_index = component_catalog_core.builtin_component_index(paths)
    model_plan = dict(plan) if isinstance(plan, dict) else plan
    if isinstance(model_plan, dict):
        model_plan.setdefault("validation_mode", "flexible")
    result = project_core.preflight_rpa_execution_plan(
        analysis,
        model_plan,
        available_components=live_index,
        planning_policy="mcp",
    )
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


def _dispatch_impl(name: str, args: dict[str, Any]) -> Any:
    """Dispatch one MCP call to the shared CLI core."""
    global _RPA_SMALL_CARD_OPEN
    paths = _paths()
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
        return ShizaiExtensionBridge(desktop=client).status()
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
        return ShizaiExtensionBridge(desktop=client).stop_capture(package_path=project_ref)
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
        result = _cache_rpa_analysis(project_core.analyze_rpa_request(str(args["query"])))
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
                message="塔斯正在接管你的电脑并单步执行计划中的网页导航步骤。",
            )
            try:
                navigation_run = _rpa_run_navigation_component(paths, client, project_ref)
                browser = {
                    "opened": True,
                    "browser": "chrome",
                    "method": "designer_dap_single_step",
                    "navigation_run": navigation_run,
                }
            except (ShizaiCloudError, OSError, ValueError) as exc:
                # Application creation is already durable. Return a resumable
                # result so a browser discovery issue does not hide the flow.
                browser = {
                    "opened": False,
                    "browser": "chrome",
                    "url": target_url,
                    "error": str(exc),
                    "message": "流程已创建，但计划中的网页导航步骤尚未成功完成原生单步执行。",
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
            message="塔斯正在接管你的电脑并单步执行计划中的网页导航步骤。",
        )
        navigation_run = _rpa_run_navigation_component(paths, client, project_ref)
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
            "navigation_run": navigation_run,
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
        takeover_ui = _rpa_design_ui_ensure_takeover(
            client,
            flow_ref=project_ref or None,
            message="塔斯正在接管你的电脑并识别当前浏览器窗口。",
        )
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
                "reason": "当前不存在可识别的 Chrome 页面；根据已分析的 navigate 操作定位计划步骤并原生单步执行，禁止继续 scan_page。",
            }
            native_ui["state"] = "waiting"
            native_ui["message"] = "当前未检测到 Chrome 页面，准备单步执行计划中的网页导航步骤。"
            native_ui["takeover"] = takeover_ui
            result["native_ui"] = native_ui
            return result
        native_ui["takeover"] = takeover_ui
        result["native_ui"] = native_ui
        return result
    if name == "shizai_rpa_capture_tree":
        project_ref = str(args.get("project_ref") or _LATEST_RPA_DESIGN_REF or "").strip()
        takeover_ui = _rpa_design_ui_ensure_takeover(
            client,
            message="塔斯正在接管你的电脑并识别页面结构。",
        )
        native_ui = _rpa_design_ui_update(
            client,
            state="scanning",
            message="塔斯正在识别页面结构和可用控件。",
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        result = bridge.capture_tree(
            str(args["element_id"]), level=int(args.get("level", 6)), max_nodes=int(args.get("max_nodes", 120))
        )
        if capture_session is not None:
            result["capture_session"] = capture_session
        native_ui["takeover"] = takeover_ui
        result["native_ui"] = native_ui
        return result
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
        takeover_ui = _rpa_design_ui_ensure_takeover(
            client,
            flow_ref=project_ref or None,
            message="塔斯正在接管你的电脑并扫描当前页面。",
        )
        native_ui = _rpa_design_ui_update(
            client,
            state="scanning",
            message="塔斯正在识别页面元素并分析可复用选择器。",
            flow_ref=project_ref or None,
        )
        bridge = ShizaiExtensionBridge(desktop=client)
        capture_session = _rpa_ensure_capture_session(bridge, project_ref)
        windows = bridge.capture_windows(include_non_chrome=False)
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
                    "takeover": takeover_ui,
                },
            }
        result = bridge.scan_page(
            root_element_id=args.get("root_element_id"),
            query=str(args.get("query", "")),
            level=int(args.get("level", 6)),
            max_nodes=int(args.get("max_nodes", 250)),
            page_title=args.get("page_title"),
            page_url=args.get("page_url"),
            scan_token=args.get("scan_token"),
        )
        if isinstance(result.get("next_action"), dict):
            next_action = result["next_action"]
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
        native_ui["takeover"] = takeover_ui
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
    if name == "shizai_rpa_capture_elements":
        project_ref = str(args.get("project_ref") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_capture_elements requires allow_write: true")
        allow_rebind = bool(args.get("allow_rebind", False))
        if allow_rebind:
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
            flow_ref=project_ref or None,
            message="塔斯正在接管你的电脑并捕获页面元素。",
        )
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
                args["element_ids"],
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
        native_ui["takeover"] = takeover_ui
        captured["native_ui"] = native_ui
        if allow_rebind:
            captured["rebind"] = True
        return captured
    if name == "shizai_rpa_update_step":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_update_step requires allow_write: true")
        return project_core.update_rpa_step_config(
            paths, str(args["flow_ref"]), str(args["step_id"]), args["config"], client=client,
            expected_revision=args.get("expected_revision"),
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
                },
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
        return project_core.native_rpa_status(paths, str(args["project_ref"]))
    if name == "shizai_rpa_stop":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_rpa_stop requires allow_write: true")
        return project_core.native_rpa_stop(paths, str(args["project_ref"]), client=client)
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
        return project_core.list_projects(paths)
    if name == "shizai_project_inspect":
        return project_core.inspect_project(paths, args["project_ref"])
    if name == "shizai_project_elements":
        return project_core.read_elements(paths, args["project_ref"])
    if name == "shizai_project_import_elements":
        if not bool(args.get("allow_write", False)):
            raise PermissionError("shizai_project_import_elements requires allow_write: true")
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
    """Dispatch one MCP call and always tear down UI after a formal run."""
    if name != "shizai_rpa_run" or not bool(args.get("allow_write", False)):
        return _dispatch_impl(name, args)

    result: Any = None
    try:
        result = _dispatch_impl(name, args)
        return result
    finally:
        cleanup = _rpa_cleanup_native_ui(_desktop_client())
        if isinstance(result, dict):
            native_ui = result.setdefault("native_ui", {})
            if isinstance(native_ui, dict):
                native_ui["final_cleanup"] = cleanup


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
            result = _dispatch(name, arguments)
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
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))]

    return server


async def _run_stdio() -> None:
    server = build_server()
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
