"""Native RPA single-step debugging through the Shizai designer DAP bridge.

The designer's normal debug button is a two-part operation: it asks the
package script service for a code slice and starts ``AppRunService`` with
``isDebug=1``.  The debug controller then drives the same runtime through
``DebugAdapterService``.  This module mirrors that protocol without opening a
second browser or rebuilding a session for every component.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agent import redact_payload
from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent
from .extension_bridge import ShizaiExtensionBridge, set_rpa_ui_feedback
from .paths import ShizaiPaths

_SESSION_DIR_NAME = "mcp-debug-sessions"
_DEFAULT_START_TIMEOUT = 30.0
_DEFAULT_WAIT_SECONDS = 15.0
_DEBUG_COMMANDS = {"next", "continue", "stepIn", "stepOut"}
_TERMINAL_SESSION_STATUSES = {
    "stopped",
    "runtime_error",
    "start_timeout",
    "succeeded",
    "cleanup_timeout",
    "cleanup_failed",
}
_DEBUG_LIFECYCLE_COMMANDS = {
    "initialize",
    "attach",
    "configurationDone",
    "setBreakpoints",
    "threads",
    "stackTrace",
    "disconnect",
}
_POSITION_RE = re.compile(
    r"_position=\(\s*(['\"])(?P<source>.*?)\1\s*,\s*"
    r"(['\"])(?P<component_id>.*?)\3\s*,\s*"
    r"(['\"])(?P<block_label>.*?)\5\s*,\s*"
    r"(['\"])(?P<label>.*?)\7\s*,\s*"
    r"(['\"])(?P<ordinal>.*?)\9\s*,\s*"
    r"(['\"])(?P<function>.*?)\11\s*\)",
)
_NATIVE_ELEMENT_REF_RE = re.compile(r"SHIZAI_ELEMENT_DICT\[\s*['\"]([^'\"]+)['\"]\s*\]")
_ELEMENT_DEPENDENT_COMPONENTS = {
    "Element_SetValue_V6",
    "WebBrowser_DataCollectionV5",
    "WebBrowser_RunJsInIeBySelector_v3",
}


def _call_unlocked(client: Any, service: str, method: str, args: list[Any]) -> Any:
    """Call a desktop RPC while leaving the channel available to DAP calls."""
    method_fn = getattr(client, "call_unlocked", None)
    if callable(method_fn):
        return method_fn(service, method, args)
    return client.call(service, method, args)


def _app_run_already_stopped(exc: Exception) -> bool:
    """Return true for the vendor's idempotent AppRun stop response."""
    return bool(re.search(r"(?:code\s*=\s*|\b)3001\b", str(exc), flags=re.IGNORECASE))


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "isSuccess" in value and "data" in value:
        if value.get("isSuccess") is False:
            raise ShizaiCloudError(str(value.get("message") or "Desktop RPC failed"))
        return value.get("data")
    return value


def _as_payload(value: Any) -> dict[str, Any]:
    value = _unwrap(value)
    return value if isinstance(value, dict) else {"value": value}


def _finite_timeout(value: Any, default: float, maximum: float = 300.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if result != result or result <= 0:
        return default
    return min(result, maximum)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _session_key(project_dir: Path) -> str:
    # A readable key makes the state directory diagnosable while the absolute
    # path itself never needs to be exposed as a filename.
    import hashlib

    return hashlib.sha256(os.path.normcase(str(project_dir)).encode("utf-8")).hexdigest()[:24]


def _state_path(paths: ShizaiPaths, project_dir: Path) -> Path:
    return paths.data_root / _SESSION_DIR_NAME / f"{_session_key(project_dir)}.json"


def _port_path(paths: ShizaiPaths) -> Path:
    return paths.data_root / "debugpyPort.json"


def _read_port_info(paths: ShizaiPaths) -> tuple[int | None, int | None]:
    """Read the adapter port and the publication timestamp as one snapshot."""
    port_path = _port_path(paths)
    payload = _read_json(port_path)
    try:
        mtime = port_path.stat().st_mtime_ns
    except OSError:
        mtime = None
    if not payload:
        return None, mtime
    try:
        port = int(payload.get("Port") or payload.get("port"))
    except (TypeError, ValueError):
        return None, mtime
    return (port if 1 <= port <= 65535 else None), mtime


def _read_port(paths: ShizaiPaths) -> int | None:
    """Read only the adapter port for callers that do not need freshness."""
    return _read_port_info(paths)[0]


def _listening_pids(port: int) -> set[int] | None:
    """Return listener PIDs without opening a socket to the DAP adapter.

    A TCP connect is a destructive probe for debugpy: the first accepted socket
    is the DAP client, and closing it before a handshake can terminate the
    session.  ``psutil`` lets us inspect the Windows TCP table without touching
    the adapter.  ``None`` means the optional inspection dependency is absent or
    the operating system denied the table; callers then require a fresh port
    file instead of trusting an old port.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        connections = psutil.net_connections(kind="tcp")
    except (OSError, PermissionError, psutil.Error):
        return None
    result: set[int] = set()
    for connection in connections:
        local = connection.laddr
        local_port = getattr(local, "port", None)
        if local_port is None and isinstance(local, tuple) and len(local) > 1:
            local_port = local[1]
        if local_port != port or str(connection.status).upper() != "LISTEN":
            continue
        if connection.pid is not None:
            result.add(int(connection.pid))
    return result


def _native_statement_ids(payload: dict[str, Any]) -> list[str]:
    entities = payload.get("componentModelEntities")
    main = entities.get("MAIN_COMPONENT") if isinstance(entities, dict) else None
    if not isinstance(main, dict):
        return []
    for argument in main.get("args0", []):
        if isinstance(argument, dict) and argument.get("name") == "statements":
            value = argument.get("value")
            if isinstance(value, list):
                return [str(item) for item in value]
    return []


def _native_task(project_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(
        project_dir.rglob("*.task"),
        key=lambda item: (item.name != "io5cd.task", str(item).lower()),
    )
    for task_path in candidates:
        payload = _read_json(task_path)
        if payload and isinstance(payload.get("componentModelEntities"), dict):
            if isinstance(payload["componentModelEntities"].get("MAIN_COMPONENT"), dict):
                return task_path, payload
    raise FileNotFoundError(f"no native .task flow found in: {project_dir}")


def _task_arguments(node: dict[str, Any]) -> dict[str, Any]:
    """Return a task node's argument values for runtime diagnostics."""
    return {
        str(item.get("name")): item.get("value")
        for item in node.get("args0", [])
        if isinstance(item, dict) and item.get("name")
    }


def _element_reference_ids(value: Any) -> list[str]:
    """Extract native element ids from a generated selector expression."""
    return list(dict.fromkeys(_NATIVE_ELEMENT_REF_RE.findall(str(value or ""))))


def _selector_identity(element: dict[str, Any]) -> tuple[str, str]:
    """Read page identity from a simplified or native element record."""
    title = str(element.get("page_title") or element.get("title") or "").strip()
    url = str(element.get("page_url") or element.get("url") or "").strip()
    selector = element.get("selector")
    if not isinstance(selector, dict):
        return title, url
    for entry in selector.get("selector", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "").strip().casefold() not in {"tab", "window", "document"}:
            continue
        for prop in entry.get("props", []):
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or "").strip().casefold()
            value = prop.get("textValue") or prop.get("value") or ""
            value = str(value).strip().strip('"')
            if name in {"title", "name"} and value and not title:
                title = value
            elif name == "url" and value and not url:
                url = value
    return title, url


def _step_runtime_identity(project_dir: Path, task_path: Path, node: dict[str, Any]) -> dict[str, Any] | None:
    """Build expected page identity for an element-dependent native step."""
    component = str(node.get("type") or "").strip()
    if component not in _ELEMENT_DEPENDENT_COMPONENTS:
        return None
    args = _task_arguments(node)
    element_ids = _element_reference_ids(args.get("uiSelector"))
    raw_config = args.get("config")
    config = None
    if isinstance(raw_config, str) and raw_config.strip():
        try:
            config = json.loads(raw_config)
        except (TypeError, ValueError):
            config = None
    if isinstance(config, dict):
        page_info = config.get("pageInfo")
        if isinstance(page_info, dict):
            page_title = str(page_info.get("windowTitle") or "").strip()
            page_url = str(page_info.get("pageUrl") or "").strip()
        else:
            page_title = page_url = ""
    else:
        page_title = page_url = ""
    if not element_ids and isinstance(node.get("mcpElementIds"), list):
        element_ids = [str(item).strip() for item in node["mcpElementIds"] if str(item).strip()]
    element_path = project_dir / "elements.json"
    try:
        elements_payload = json.loads(element_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        elements_payload = {}
    element_map = elements_payload.get("elementMap") if isinstance(elements_payload, dict) else {}
    if isinstance(element_map, dict):
        for element_id in element_ids:
            element = element_map.get(element_id)
            if not isinstance(element, dict):
                continue
            captured_title, captured_url = _selector_identity(element)
            page_title = page_title or captured_title
            page_url = page_url or captured_url
            if page_title and page_url:
                break
    if component == "WebBrowser_RunJsInIeBySelector_v3" and not page_url:
        package = _read_json(project_dir / "szrpa.json") or {}
        page_url = str(package.get("mcpTargetUrl") or "").strip()
    if not element_ids and not page_title and not page_url:
        return None
    return {
        "component": component,
        "step_id": str(node.get("id") or ""),
        "element_ids": element_ids,
        "expected_page_title": page_title,
        "expected_page_url": page_url,
    }


def _arrangement_id(project_dir: Path, task_path: Path) -> str:
    edit_state = _read_json(project_dir / "edit-state.json") or {}
    current = edit_state.get("currentEditorId")
    if isinstance(current, str) and current.strip():
        return current.strip()
    arrangements = edit_state.get("arrangementInfoList")
    if isinstance(arrangements, list):
        for item in arrangements:
            if isinstance(item, dict) and item.get("sourceId") == task_path.stem and item.get("editorId"):
                return str(item["editorId"])
    return f"{task_path.stem}-flowBlock"


def _generated_module(project_dir: Path, task_path: Path) -> Path:
    return project_dir / "projects" / "flow_modules" / f"{task_path.stem}.py"


def _execution_definition_sha256(project_dir: Path, task_path: Path) -> str:
    """Track the saved task and element definitions loaded by a debug runtime."""
    digest = hashlib.sha256()
    for path in (task_path, project_dir / "elements.json"):
        digest.update(path.name.encode("utf8"))
        if path.exists():
            raw = path.read_bytes()
            try:
                raw = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False).encode("utf8")
            except (ValueError, UnicodeError):
                pass
            digest.update(raw)
        else:
            digest.update(b"<absent>")
    return digest.hexdigest()


def _archive_debug_outputs(
    paths: ShizaiPaths,
    project_dir: Path,
    component_ids: list[str],
    start_from: int,
) -> list[dict[str, str]]:
    """Archive exact Excel targets before a debug run creates them again.

    ``Excel_Cs_NewV4`` rejects an existing workbook.  Normal native runs
    already use the same recoverable archive policy; native debug runs need it
    too, otherwise retrying after a partial run fails before the write step.
    Keep this after the existing-session takeover probe so a live session is
    never changed by a repeated ``debug_start`` call.
    """
    from .project import (
        _archive_existing_native_output,
        _native_rpa_state,
        _native_run_output_names,
    )

    flow_state = _native_rpa_state(paths, str(project_dir))
    steps = flow_state.get("steps") if isinstance(flow_state.get("steps"), list) else []
    has_pending_excel = any(
        index + 1 >= int(start_from) and isinstance(step, dict) and str(step.get("component") or "") == "Excel_Cs_NewV4"
        for index, step in enumerate(steps)
    )
    if not has_pending_excel:
        return []
    archived: list[dict[str, str]] = []
    for output_name in _native_run_output_names(flow_state):
        archived.extend(_archive_existing_native_output(project_dir, output_name))
    return archived


def _position_records(project_dir: Path, task_path: Path) -> dict[int, dict[str, Any]]:
    module = _generated_module(project_dir, task_path)
    if not module.is_file():
        return {}
    result: dict[int, dict[str, Any]] = {}
    try:
        lines = module.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line_number, line in enumerate(lines, start=1):
        match = _POSITION_RE.search(line)
        if not match:
            continue
        item = match.groupdict()
        try:
            ordinal = int(item["ordinal"])
        except (TypeError, ValueError):
            continue
        item["line"] = line_number
        item["ordinal"] = ordinal
        result[line_number] = item
        result.setdefault(ordinal, item)
    return result


def _normalize_path(value: Any) -> str:
    return os.path.normcase(str(value or "").replace("/", "\\"))


_LOG_TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def _log_line_timestamp(line: str) -> float | None:
    """Parse the local RuntimeHost log timestamp when one is present."""
    match = _LOG_TIMESTAMP_RE.match(str(line or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _thread_ids(payload: dict[str, Any]) -> list[int]:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    raw = body.get("threads") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(int(item.get("id")))
        except (TypeError, ValueError):
            continue
    return result


def _frames(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    frames = body.get("stackFrames") if isinstance(body, dict) else None
    return [item for item in frames if isinstance(item, dict)] if isinstance(frames, list) else []


def _dap_terminal_event(payload: dict[str, Any]) -> str | None:
    """Extract a terminal DAP event when the desktop proxy exposes one."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if isinstance(event, str) and event in {"terminated", "exited"}:
        return event
    nested = payload.get("body")
    if isinstance(nested, dict):
        event = nested.get("event")
        if isinstance(event, str) and event in {"terminated", "exited"}:
            return event
    return None


def _is_terminal_debug_error(message: str) -> bool:
    """Recognize DAP transport errors that mean the runtime already ended."""
    lowered = str(message or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "disconnected unexpectedly",
            "server[",
            "connection closed",
            "debug adapter",
            "terminated",
            "进程已结束",
            "运行进程已结束",
        )
    )


def _load_native_debug_code(project_dir: Path, task_path: Path) -> tuple[str, str]:
    """Load the same RPAProject entry point used by the native runtime.

    ``PackageScriptService`` belongs to the renderer-side designer container;
    it is not a node service on the desktop business WebSocket.  The generated
    encrypted ``main.sz`` is the durable equivalent of the renderer's
    ``createMainScript`` result, so use it when present and generate the same
    source shape only for projects that have not materialized it yet.
    """
    from .project import _decrypt_native_main, _native_main_text

    main_path = project_dir / "main.sz"
    if main_path.is_file():
        try:
            code = _decrypt_native_main(main_path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
            code = ""
        if "class RPAProject" in code and "def main(self)" in code:
            return code, "decrypted_main_sz"
    try:
        return _native_main_text(project_dir, task_path), "local_main_bridge"
    except (OSError, RuntimeError, ValueError) as exc:
        raise ShizaiCloudError(f"native debug entry point could not be materialized: {exc}") from exc


def _component_records(records: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one generated-code position per component ordinal."""
    by_ordinal: dict[int, dict[str, Any]] = {}
    for record in records.values():
        try:
            ordinal = int(record.get("ordinal"))
        except (TypeError, ValueError):
            continue
        by_ordinal.setdefault(ordinal, record)
    return [by_ordinal[key] for key in sorted(by_ordinal)]


def runtime_preflight(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    step_id: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run a read-only current-page check for one native element step."""
    project_dir = _resolve_project(paths, project_ref)
    task_path, payload = _native_task(project_dir)
    entities = payload.get("componentModelEntities")
    node: dict[str, Any] | None = None
    requested_step_id = str(step_id or "").strip()
    if requested_step_id and isinstance(entities, dict):
        candidate = entities.get(requested_step_id)
        node = candidate if isinstance(candidate, dict) else None
    if node is None and isinstance(entities, dict):
        with _SESSIONS_LOCK:
            active = _SESSIONS.get(str(project_dir))
        active_id = str((active.current or {}).get("component_id") or "").strip() if active else ""
        candidate = entities.get(active_id) if active_id else None
        if isinstance(candidate, dict):
            node = candidate
        if node is None:
            for component_id in _native_statement_ids(payload):
                candidate = entities.get(component_id)
                if isinstance(candidate, dict) and str(candidate.get("type") or "") in _ELEMENT_DEPENDENT_COMPONENTS:
                    node = candidate
                    break
    if node is None:
        return {
            "kind": "rpa_runtime_preflight",
            "valid": False,
            "reason": "step_not_found",
            "message": "没有找到需要预检的 native RPA 组件步骤。",
            "project_ref": str(project_dir),
            "step_id": requested_step_id or None,
            "task": str(task_path),
            "next_action": {"tool": "shizai_rpa_debug_status", "reason": "先读取当前停靠组件。"},
        }
    identity = _step_runtime_identity(project_dir, task_path, node)
    if identity is None:
        return {
            "kind": "rpa_runtime_preflight",
            "valid": True,
            "reason": "not_applicable",
            "message": "当前组件不依赖网页元素，不需要页面身份预检。",
            "project_ref": str(project_dir),
            "step_id": str(node.get("id") or requested_step_id),
            "component": str(node.get("type") or ""),
            "task": str(task_path),
        }
    result = ShizaiExtensionBridge(desktop=client or ShizaiDesktopAgent()).runtime_preflight(
        expected_page_title=identity["expected_page_title"],
        expected_page_url=identity["expected_page_url"],
        step_id=identity["step_id"],
        component=identity["component"],
    )
    result.update(
        {
            "project_ref": str(project_dir),
            "task": str(task_path),
            "element_ids": identity["element_ids"],
        }
    )
    return result


@dataclass
class NativeDebugSession:
    paths: ShizaiPaths
    project_dir: Path
    task_path: Path
    arrangement_id: str
    component_ids: list[str]
    start_from: int
    client: Any
    state_file: Path
    port: int | None = None
    thread_id: int | None = None
    status: str = "starting"
    current: dict[str, Any] | None = None
    last_line: int | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    run_result: Any = None
    run_error: str | None = None
    runtime_error_evidence: dict[str, Any] | None = None
    run_started_at: float | None = None
    dap_terminal_event: str | None = None
    run_thread: threading.Thread | None = field(default=None, repr=False)
    command_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    breakpoint_lines: list[int] = field(default_factory=list)
    code_source: str = ""
    adapter_mode: str = "mcp_created"
    run_owned: bool = False
    # A terminal persisted session can leave a responsive adapter on the
    # shared port. Do not mistake that stale adapter for a new active run.
    reuse_existing_adapter: bool = True
    listener_probe: Callable[[int], set[int] | None] = _listening_pids
    port_before: int | None = None
    port_mtime_before: int | None = None
    listener_pids_before: list[int] = field(default_factory=list)
    listener_probe_before: bool | None = None
    archived_outputs: list[dict[str, str]] = field(default_factory=list)
    async_debug_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    async_debug_errors: list[str] = field(default_factory=list, repr=False)
    definition_sha256: str | None = None

    def _definition_changed_result(self) -> dict[str, Any] | None:
        if not self.definition_sha256 or self.definition_sha256 == _execution_definition_sha256(self.project_dir, self.task_path):
            return None
        current_id = (self.current or {}).get("component_id")
        _, payload = _native_task(self.project_dir)
        ids = _native_statement_ids(payload)
        position = ids.index(current_id) + 1 if current_id in ids else 1
        return {
            "kind": "native_rpa_debug_reload_required", "status": self.status,
            "project_ref": str(self.project_dir), "preflight_blocked": True,
            "runtime_execution": False, "requires_restart": True, "reason": "definition_changed",
            "current": self.current, "runtime_verification": {"verified": False, "component_executed": False},
            "message": "流程参数或元素绑定已改变；当前调试进程仍加载旧版本，不能继续单步。停止并重新启动同一流程的调试，在当前组件前停靠；此前步骤会重新执行。",
            "next_action": {"tool": "shizai_rpa_debug_stop", "arguments": {
                "project_ref": str(self.project_dir), "allow_write": True}},
            "then": {"tool": "shizai_rpa_debug_start", "arguments": {
                "project_ref": str(self.project_dir), "start_from": position, "allow_write": True}},
        }

    def _persist(self) -> None:
        self.updated_at = time.time()
        payload = {
            "kind": "shizai_native_debug_session",
            "project": str(self.project_dir),
            "task": str(self.task_path),
            "arrangement_id": self.arrangement_id,
            "component_ids": self.component_ids,
            "start_from": self.start_from,
            "definition_sha256": self.definition_sha256,
            "port": self.port,
            "thread_id": self.thread_id,
            "status": self.status,
            "current": redact_payload(self.current),
            "error": self.error,
            "run_error": self.run_error,
            "runtime_error_evidence": redact_payload(self.runtime_error_evidence),
            "run_started_at": self.run_started_at,
            "dap_terminal_event": self.dap_terminal_event,
            "breakpoint_lines": self.breakpoint_lines,
            "code_source": self.code_source,
            "adapter_mode": self.adapter_mode,
            "run_owned": self.run_owned,
            "port_before": self.port_before,
            "port_mtime_before": self.port_mtime_before,
            "listener_pids_before": self.listener_pids_before,
            "listener_probe_before": self.listener_probe_before,
            "archived_outputs": self.archived_outputs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        _atomic_write_json(self.state_file, payload)

    def _rpc(self, service: str, method: str, args: list[Any]) -> Any:
        return _unwrap(_call_unlocked(self.client, service, method, args))

    def _debug(self, command: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if command not in _DEBUG_COMMANDS | _DEBUG_LIFECYCLE_COMMANDS:
            raise ValueError(f"unsupported debug adapter command: {command}")
        payload = self._rpc("DebugAdapterService", "send", [command, arguments or {}])
        response = _as_payload(payload)
        if response.get("success") is False:
            raise ShizaiCloudError(str(response.get("message") or f"Debug Adapter command failed: {command}"))
        event = _dap_terminal_event(response)
        if event:
            self.dap_terminal_event = event
            self._persist()
        return response

    def _debug_async(self, command: str, arguments: dict[str, Any] | None = None) -> None:
        """Send a DAP command whose response may be deferred until a stop event.

        The bundled debugpy 1.5 adapter can emit ``stopped`` for ``continue``
        without resolving the desktop RPC that carried the request. Waiting
        synchronously would prevent the polling loop from observing that stop.
        Keep the request on a daemon worker and let ``_snapshot`` read the
        shared adapter state through a separate RPC.
        """
        if command not in _DEBUG_COMMANDS | _DEBUG_LIFECYCLE_COMMANDS:
            raise ValueError(f"unsupported debug adapter command: {command}")

        def worker() -> None:
            try:
                self._debug(command, arguments)
            except Exception as exc:
                with self.async_debug_lock:
                    self.async_debug_errors.append(str(exc))

        threading.Thread(
            target=worker,
            name=f"shizai-native-debug-{command}-async",
            daemon=True,
        ).start()

    def _consume_async_debug_error(self) -> str | None:
        """Return the oldest deferred DAP error, if a worker reported one."""
        with self.async_debug_lock:
            if not self.async_debug_errors:
                return None
            return self.async_debug_errors.pop(0)

    def _runtime_error_from_logs(self) -> dict[str, Any] | None:
        """Read a RuntimeHost error belonging to this project and run.

        RuntimeHost writes several applications into shared log trees. Component
        ids are generated by the designer and are often reused between flows,
        so matching only on a component id can surface an unrelated historical
        failure. Require the current project path (or its project-specific user
        log) before accepting evidence, and ignore files that predate this run.
        """
        log_root = self.paths.data_root / "logs"
        if not log_root.is_dir():
            return None
        application_name = self.project_dir.name.casefold()
        project_identity = _normalize_path(self.project_dir)
        module_identity = _normalize_path(_generated_module(self.project_dir, self.task_path))
        run_started_at = float(self.run_started_at or self.created_at or 0)
        candidates: list[Path] = []
        for pattern in (
            f"{self.project_dir.name}.log*",
            "*.jsonl",
            "node-main.log*",
            "node-rpc.error.log*",
            "II.RPA.RuntimeHost*.txt*",
        ):
            try:
                candidates.extend(log_root.rglob(pattern))
            except OSError:
                continue
        seen_paths: set[str] = set()
        ordered: list[Path] = []
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
            key = str(path).casefold()
            if key in seen_paths or not path.is_file():
                continue
            seen_paths.add(key)
            ordered.append(path)
            if len(ordered) >= 32:
                break
        for path in ordered:
            try:
                if run_started_at and path.stat().st_mtime < run_started_at - 2.0:
                    continue
            except OSError:
                continue
            try:
                lines = path.read_bytes()[-524288:].decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line_timestamp = _log_line_timestamp(line)
                if line_timestamp is not None and run_started_at and line_timestamp < run_started_at - 2.0:
                    continue
                lowered = line.casefold()
                if not any(
                    marker in lowered
                    for marker in ("运行错误", '"runstatus":"error"', "componentvalidateexception", "errorcode")
                ):
                    continue
                normalized_line = _normalize_path(line)
                project_log = application_name in path.name.casefold()
                matches_application = (
                    project_identity in normalized_line or module_identity in normalized_line or project_log
                )
                if not matches_application:
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    payload = None
                data = (
                    payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
                )
                message = str(data.get("text") or "").strip()
                if not message:
                    message = line.strip()
                evidence: dict[str, Any] = {
                    "message": str(redact_payload(message)),
                    "log_path": str(path),
                }
                for key in ("errorCode", "errorTraceback", "position", "statusText"):
                    if data.get(key) not in (None, "", []):
                        evidence[key] = redact_payload(data[key])
                return evidence
        return None

    def _runtime_success_from_logs(self) -> dict[str, Any] | None:
        """Read a success marker from this project's current run log."""
        log_root = self.paths.data_root / "logs"
        if not log_root.is_dir():
            return None
        application_name = self.project_dir.name.casefold()
        run_started_at = float(self.run_started_at or self.created_at or 0)
        candidates: list[Path] = []
        try:
            candidates.extend(log_root.rglob(f"{self.project_dir.name}.log*"))
        except OSError:
            return None
        for path in sorted(
            (item for item in candidates if item.is_file()),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        ):
            try:
                if run_started_at and path.stat().st_mtime < run_started_at - 2.0:
                    continue
                lines = path.read_bytes()[-131072:].decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if application_name not in path.name.casefold():
                continue
            for line in reversed(lines):
                line_timestamp = _log_line_timestamp(line)
                # The user log is append-only and contains every historical
                # run for this project. A current file mtime alone is not
                # enough to identify a success marker from this run.
                if line_timestamp is not None and run_started_at and line_timestamp < run_started_at - 2.0:
                    continue
                lowered = line.casefold()
                if any(
                    marker in lowered for marker in ("运行错误", '"runstatus":"error"', "componentvalidateexception")
                ):
                    continue
                if not any(marker in lowered for marker in ("success", '"runstatus":"success"', "运行成功")):
                    continue
                return {
                    "message": str(redact_payload(line.strip())),
                    "log_path": str(path),
                }
        return None

    def _run_result_completed(self) -> bool:
        """Return whether AppRunService explicitly reported completion."""
        if not isinstance(self.run_result, dict):
            return False
        terminal_values = {"success", "succeeded", "complete", "completed", "finished", "done"}
        for key in ("state", "status", "runState", "run_status"):
            value = self.run_result.get(key)
            if isinstance(value, str) and value.strip().casefold() in terminal_values:
                return True
        return False

    def _mark_runtime_error(self, message: str, evidence: dict[str, Any] | None = None) -> None:
        """Freeze a failed debug session so later calls do not hit a dead DAP."""
        self.runtime_error_evidence = evidence or {"message": str(redact_payload(message))}
        self.error = str(evidence.get("message") if evidence else message)
        self.status = "runtime_error"
        self._persist()

    def _mark_terminal_if_needed(self, message: str | None = None) -> bool:
        """Convert a confirmed runtime termination into a stable result."""
        if self.status not in {"running", "connected", "paused"} or self.current is None:
            return False
        evidence = self._runtime_error_from_logs()
        if evidence:
            self._mark_runtime_error(str(evidence.get("message") or message or "运行错误"), evidence)
        elif self._runtime_success_from_logs():
            self.status = "succeeded"
            self.error = None
            self.runtime_error_evidence = None
            self._persist()
        elif message and _is_terminal_debug_error(message):
            self._mark_runtime_error(message)
        elif self.dap_terminal_event or self._run_result_completed():
            self.status = "succeeded"
            self._persist()
        else:
            return False
        return True

    def _is_flow_frame(self, frame: dict[str, Any]) -> bool:
        source = frame.get("source") if isinstance(frame.get("source"), dict) else {}
        source_path = _normalize_path(source.get("path"))
        expected = _normalize_path(_generated_module(self.project_dir, self.task_path))
        return source_path.endswith(expected) or source_path.endswith(
            _normalize_path(f"flow_modules/{self.task_path.stem}.py")
        )

    def _set_breakpoints(self) -> None:
        records = _position_records(self.project_dir, self.task_path)
        selected = [record for record in _component_records(records) if int(record["ordinal"]) >= self.start_from]
        lines = sorted({int(record["line"]) for record in selected})
        if not lines:
            raise ShizaiCloudError("no executable component positions were found in the generated flow module")
        module_path = _generated_module(self.project_dir, self.task_path)
        self._debug(
            "setBreakpoints",
            {
                "source": {
                    "name": f"flow_modules/{self.task_path.stem}.py",
                    "path": str(module_path),
                },
                "lines": lines,
                "breakpoints": [{"line": line} for line in lines],
                "sourceModified": False,
            },
        )
        self.breakpoint_lines = lines
        self._persist()

    def _probe_existing_adapter(self) -> bool:
        """Check whether the designer already owns the shared DAP connection.

        ``DebugAdapterService.send`` uses the connection created by the
        designer when no new ``connect`` call is made.  A short-lived probe
        avoids blocking the start path for the full normal RPC timeout when
        the designer has no active debug session.
        """
        probe_client = self.client
        config = getattr(self.client, "config", None)
        if config is not None and hasattr(config, "timeout"):
            try:
                probe_client = ShizaiDesktopAgent(
                    replace(config, timeout=min(max(float(config.timeout), 0.25), 0.75)),
                    connection_factory=getattr(self.client, "connection_factory", None),
                )
            except (TypeError, ValueError):
                probe_client = self.client
        payload = _call_unlocked(probe_client, "DebugAdapterService", "send", ["threads", {}])
        response = _as_payload(payload)
        if response.get("success") is False:
            raise ShizaiCloudError(str(response.get("message") or "Debug Adapter probe failed"))
        # The desktop RPC itself can return a generic ``body`` acknowledgement
        # while no adapter is connected.  Only a DAP ``threads`` response is a
        # usable signal; an empty list is still valid during early startup.
        body = response.get("body")
        return isinstance(body, dict) and isinstance(body.get("threads"), list)

    @staticmethod
    def _require_paused(snapshot: dict[str, Any], phase: str) -> dict[str, Any]:
        """Never report a debug start/step as successful without a stop event."""
        if snapshot.get("status") == "paused":
            return snapshot
        detail = snapshot.get("message") or snapshot.get("error") or "未收到组件停靠事件"
        raise ShizaiCloudError(f"{phase} 未停在组件位置：{detail}")

    def _wait_for_existing_adapter(self, deadline: float, *, max_seconds: float = 5.0) -> bool:
        """Give the designer's DebugControllerService time to attach first."""
        probe_deadline = min(deadline, time.monotonic() + max(0.1, max_seconds))
        while time.monotonic() < probe_deadline:
            # ``DebugAdapterService.send`` is a thin proxy around one shared
            # socket in the designer.  Probing it while the port file points at
            # a stale or not-yet-listening adapter makes the packaged app log an
            # unhandled ``sendData`` rejection for every poll.  A real desktop
            # client can use the TCP table as a non-destructive readiness check;
            # test clients intentionally remain probeable without a socket.
            if isinstance(self.client, ShizaiDesktopAgent):
                port, _ = _read_port_info(self.paths)
                if port:
                    try:
                        listeners = self.listener_probe(port)
                    except Exception:
                        listeners = None
                    if listeners is not None and not listeners:
                        time.sleep(0.15)
                        continue
            try:
                if self._probe_existing_adapter():
                    self.adapter_mode = "designer_reused"
                    self._persist()
                    return True
            except Exception:
                # No active designer DAP connection is the expected result
                # before the fallback MCP-owned handshake starts.
                pass
            if self.run_thread and not self.run_thread.is_alive() and self.run_error:
                self.status = "runtime_error"
                self.error = self.run_error
                self._persist()
                return False
            time.sleep(0.15)
        return False

    def _is_current_adapter_port(
        self,
        port: int | None,
        port_mtime: int | None,
        *,
        listener_pids: set[int] | None = None,
    ) -> bool:
        """Accept only an adapter published by the current run.

        The installed runtime commonly keeps the same adapter port (5679), so
        the numeric value alone is not an identity.  A changed port file or a
        newly created listener process proves a new run.  When the optional TCP
        table is unavailable, only a changed port file is accepted; this avoids
        reconnecting to a stale adapter after a crashed run.
        """
        if not port:
            return False
        port_changed = port != self.port_before or port_mtime != self.port_mtime_before
        if listener_pids is None or self.listener_probe_before is False:
            return port_changed
        if not listener_pids:
            return False
        if port_changed:
            return True
        previous = set(self.listener_pids_before)
        return bool(listener_pids - previous)

    def _advance_to_start(self, snapshot: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
        """Resume a pre-existing first breakpoint until component N."""
        current = snapshot.get("current") or {}
        sequence = current.get("component_sequence")
        if not isinstance(sequence, int) or sequence >= self.start_from:
            return snapshot
        for _ in range(len(self.component_ids)):
            if self.thread_id is None:
                break
            self.status = "running"
            self._persist()
            self._debug_async("continue", {"threadId": self.thread_id})
            snapshot = self._wait_after_command("continue", wait_seconds)
            current = snapshot.get("current") or {}
            sequence = current.get("component_sequence")
            if not isinstance(sequence, int) or sequence >= self.start_from or self.status != "paused":
                break
        return snapshot

    def _snapshot(self, *, wait_seconds: float = _DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, wait_seconds)
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                thread_payload = self._debug("threads")
                ids = _thread_ids(thread_payload)
                if not ids:
                    if self._mark_terminal_if_needed():
                        return self._result(
                            "snapshot",
                            message=(
                                "Native RPA 运行已结束；已保留运行错误证据。"
                                if self.status == "runtime_error"
                                else "Native RPA 运行已结束，DAP 会话已关闭。"
                            ),
                        )
                    time.sleep(0.15)
                    continue
                candidates = [self.thread_id] if self.thread_id in ids else []
                candidates.extend(item for item in ids if item not in candidates)
                fallback: tuple[int, dict[str, Any]] | None = None
                for thread_id in candidates:
                    # DAP accepts ``levels`` without ``startFrame`` in theory,
                    # but the bundled debugpy 1.5 adapter intermittently leaves
                    # that request pending while the runtime is stopping.  The
                    # designer always starts from frame zero, so mirror its
                    # complete request shape here.
                    stack_payload = self._debug(
                        "stackTrace",
                        {"threadId": thread_id, "startFrame": 0, "levels": 20},
                    )
                    frames = _frames(stack_payload)
                    if not frames:
                        continue
                    fallback = fallback or (thread_id, frames[0])
                    flow_frame = next((frame for frame in frames if self._is_flow_frame(frame)), None)
                    if flow_frame is not None:
                        self.thread_id = thread_id
                        self.current = self._map_frame(flow_frame)
                        self.last_line = int(flow_frame.get("line") or 0) or self.last_line
                        self.status = "paused"
                        # A late DAP timeout can be observed between two
                        # successful polls. Once the adapter returns a real
                        # flow frame, that transport error is stale and must
                        # not make a valid paused component look failed.
                        self.error = None
                        self._persist()
                        return self._result("snapshot")
                # A stack trace from a component implementation is not a
                # component stop. The runtime can answer ``stackTrace`` while
                # it is still inside WebBrowser/Element/Excel code, so using
                # the first frame here makes a component-level step appear to
                # finish early. Keep the last component as context, but wait
                # for a frame in the generated flow module before reporting a
                # paused step.
                if fallback is not None:
                    self.thread_id = fallback[0]
                    self.status = "running"
                    self._persist()
                    return self._result(
                        "snapshot",
                        message="运行进程仍在组件实现内部；等待下一个 Native RPA 组件断点。",
                    )
            except Exception as exc:
                last_error = str(exc)
                if (_is_terminal_debug_error(last_error) or self.dap_terminal_event) and self._mark_terminal_if_needed(
                    last_error
                ):
                    return self._result(
                        "snapshot",
                        message=(
                            "Native RPA 组件运行失败；已读取 RuntimeHost 错误证据。"
                            if self.status == "runtime_error"
                            else "Native RPA 运行已结束，DAP 会话已关闭。"
                        ),
                    )
            time.sleep(0.15)
        if last_error:
            self.error = last_error
        self._persist()
        return self._result("snapshot", message="等待调试器停在组件位置超时；请调用 status 查看运行线程和错误信息。")

    def _map_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        source = frame.get("source") if isinstance(frame.get("source"), dict) else {}
        source_path = str(source.get("path") or "")
        line = int(frame.get("line") or 0)
        records = _position_records(self.project_dir, self.task_path)
        record = records.get(line)
        exact_component_boundary = bool(record and int(record.get("line") or 0) == line)
        if not exact_component_boundary:
            record = None
        if record is None:
            normalized_source = _normalize_path(source_path)
            expected = _normalize_path(_generated_module(self.project_dir, self.task_path))
            if normalized_source.endswith(expected) or normalized_source.endswith(
                _normalize_path(self.task_path.stem + ".py")
            ):
                record = min(records.values(), key=lambda item: abs(int(item.get("line", 0)) - line), default=None)
        result: dict[str, Any] = {
            "frame_id": frame.get("id"),
            "function": frame.get("name"),
            "line": line,
            "column": frame.get("column"),
            "source": source_path,
            "component_boundary": exact_component_boundary,
        }
        if record:
            result.update(
                {
                    "component_id": record.get("component_id"),
                    "component_sequence": record.get("ordinal"),
                    "component_type": str(record.get("function") or "").replace(".", "_", 1),
                    "component_function": record.get("function"),
                    "component_label": record.get("label"),
                    "arrangement_id": self.arrangement_id,
                }
            )
        return result

    def _run_finished_without_stack(self) -> bool:
        """Confirm runtime termination without trusting ``AppRunService.run``.

        The designer resolves ``AppRunService.run`` when the request is
        accepted in some builds, while the debug runtime is still alive.  A
        returned RPC value is therefore only telemetry.  We treat a run as
        finished after its worker returned *and* the DAP adapter exposes no
        threads or stack frames.
        """
        if self.run_thread is None or self.run_thread.is_alive() or self.run_error:
            return False
        # AppRunService.run can resolve before RuntimeHost has finished. An
        # empty DAP thread list is therefore only a transport observation;
        # require fresh completion evidence before reporting success.
        if not (self.dap_terminal_event or self._runtime_success_from_logs() or self._run_result_completed()):
            return False
        try:
            thread_ids = _thread_ids(self._debug("threads"))
            if not thread_ids:
                return True
            for thread_id in thread_ids:
                if _frames(self._debug("stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 20})):
                    return False
        except Exception:
            # A transient DAP response must not be converted into a false
            # success. The next polling iteration can confirm termination.
            return False
        return True

    def _run_state(self) -> str | None:
        """Extract a small diagnostic state from the accepted run response."""
        if not isinstance(self.run_result, dict):
            return None
        for key in ("state", "status", "runState", "run_status"):
            value = self.run_result.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        return None

    def _result(self, operation: str, *, message: str | None = None) -> dict[str, Any]:
        result = {
            "kind": "native_rpa_debug_session",
            "execution_mode": "design_validation",
            "production_run": False,
            "control_mode": "workflow_runtime",
            "browser_control": "runtimehost",
            "runtime_execution": True,
            "operation": operation,
            "project_ref": str(self.project_dir),
            "task": {"path": str(self.task_path), "node_id": self.task_path.stem},
            "arrangement_id": self.arrangement_id,
            "start_from": self.start_from,
            "component_count": len(self.component_ids),
            "status": self.status,
            "port": self.port,
            "thread_id": self.thread_id,
            "current": redact_payload(self.current),
            "error": self.error or self.run_error,
            "runtime_error": redact_payload(self.runtime_error_evidence),
            "dap_terminal_event": self.dap_terminal_event,
            "session_reused": operation not in {"start", "stopped"},
            "adapter_mode": self.adapter_mode,
            "dap_session_reused": self.adapter_mode == "designer_reused",
            "run_owned": self.run_owned,
            "run_thread_alive": bool(self.run_thread and self.run_thread.is_alive()),
            "run_state": self._run_state(),
            "archived_outputs": self.archived_outputs,
            "single_step": {
                "component_next": bool(self.breakpoint_lines),
                "dap_step_in": True,
                "dap_step_out": True,
                "requires_paused_thread": True,
                "current_can_step": self.status == "paused" and self.thread_id is not None,
            },
        }
        if message:
            result["message"] = message
        return result

    def _attach_runtime_preflight(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attach page-preflight evidence to a paused component result."""
        current = result.get("current") if isinstance(result, dict) else None
        component_id = str(current.get("component_id") or "").strip() if isinstance(current, dict) else ""
        if not component_id:
            return result
        preflight = runtime_preflight(
            self.paths,
            str(self.project_dir),
            step_id=component_id,
            client=self.client,
        )
        if preflight.get("reason") != "not_applicable":
            result["preflight"] = preflight
        if not preflight.get("valid", True) and preflight.get("reason") != "not_applicable":
            result["preflight_blocked"] = True
            result["message"] = str(preflight.get("message") or "网页组件运行前预检未通过。")
        return result

    def _current_runtime_preflight(self) -> dict[str, Any] | None:
        """Return a preflight result only for the currently paused element step."""
        current_id = str((self.current or {}).get("component_id") or "").strip()
        if not current_id:
            return None
        result = runtime_preflight(
            self.paths,
            str(self.project_dir),
            step_id=current_id,
            client=self.client,
        )
        return None if result.get("reason") == "not_applicable" else result

    def start(self, timeout: float) -> dict[str, Any]:
        code, self.code_source = _load_native_debug_code(self.project_dir, self.task_path)
        self.definition_sha256 = _execution_definition_sha256(self.project_dir, self.task_path)
        port_before, port_mtime_before = _read_port_info(self.paths)
        self.port_before = port_before
        self.port_mtime_before = port_mtime_before
        if port_before:
            try:
                listener_pids = self.listener_probe(port_before)
            except Exception:
                listener_pids = None
        else:
            listener_pids = set()
        self.listener_probe_before = listener_pids is not None
        self.listener_pids_before = sorted(listener_pids or set())
        self._persist()
        deadline = time.monotonic() + timeout

        # A designer debug session may already be paused with a browser open.
        # Probe before starting AppRunService so attaching to that session never
        # creates a second runtime or opens a second browser window.
        if self.reuse_existing_adapter and self._wait_for_existing_adapter(deadline, max_seconds=0.75):
            try:
                self._set_breakpoints()
                snapshot = self._snapshot(wait_seconds=max(1.0, min(5.0, deadline - time.monotonic())))
                snapshot = self._advance_to_start(
                    snapshot,
                    max(1.0, min(5.0, deadline - time.monotonic())),
                )
                snapshot = self._require_paused(snapshot, "复用设计器调试会话")
                snapshot["operation"] = "start"
                snapshot["session_reused"] = False
                snapshot["message"] = "已复用设计器现有调试会话；未重复启动运行进程或浏览器。"
                return snapshot
            except Exception as exc:
                self.error = str(exc)
                self.status = "runtime_error"
                self._persist()
                raise ShizaiCloudError(
                    f"检测到设计器已有调试会话，但接管失败：{exc}；请在设计器中停止当前调试后重试"
                ) from exc

        config = getattr(self.client, "config", None)
        self.archived_outputs = _archive_debug_outputs(
            self.paths,
            self.project_dir,
            self.component_ids,
            self.start_from,
        )
        if self.archived_outputs:
            self._persist()
        run_timeout = max(timeout + 30.0, 120.0)
        run_client = self.client
        if isinstance(config, object) and hasattr(config, "timeout"):
            try:
                run_client = ShizaiDesktopAgent(
                    replace(config, timeout=max(float(config.timeout), run_timeout)),
                    connection_factory=getattr(self.client, "connection_factory", None),
                )
            except (TypeError, ValueError):
                run_client = self.client
        run_request = {
            "code": code,
            "root": str(self.project_dir),
            "isDebug": 1,
            "arrangementId": self.arrangement_id,
            "packageName": self.project_dir.name,
            "isExternal": False,
            "isCustomComponent": 0,
            "isRunFloatingModalFold": 0,
            "isVirtual": False,
            "isOpenFloatWindow": False,
            "isIPA": False,
            "hiddenWindow": False,
            "closeRunFloatWindow": False,
            "isShowHeader": False,
            "isNoTips": False,
            "isUseTrigger": False,
            "moduleList": [],
            "useCache": False,
        }
        self.run_started_at = time.time()
        self._persist()

        def run_worker() -> None:
            try:
                self.run_result = _call_unlocked(run_client, "AppRunService", "run", [run_request])
            except Exception as exc:
                self.run_error = str(exc)
                if self.status not in {"stopped", "succeeded"}:
                    self.status = "runtime_error"
                self._persist()

        self.run_thread = threading.Thread(target=run_worker, name="shizai-native-debug-run", daemon=True)
        self.run_owned = True
        self.run_thread.start()

        # AppRunService.run publishes an app-run event.  If the package editor
        # is open, its DebugControllerService attaches to the same debugpy
        # process asynchronously.  Reuse that connection first; connecting
        # another DAP client would make debugpy reject the second attach.
        # Leave a bounded caller timeout for the MCP-owned handshake, but give
        # the installed designer enough time to publish debugpy and attach its
        # own DAP client. On real Windows runs the adapter is often published
        # several seconds after AppRunService.run is accepted. Connecting MCP
        # first would create a competing DAP client and make setBreakpoints
        # hang. Short test timeouts still get a two-second handoff window.
        # Reserve most of a short caller timeout for the actual DAP handshake.
        # The designer-reuse probe is useful on a live desktop, but spending
        # half of a four-second test/request timeout here can leave no time
        # for connect + initialize + the first stopped event.  Longer real
        # runs still get the full bounded handoff window.
        post_run_probe_seconds = min(15.0, max(1.0, timeout / 4.0))
        if self._wait_for_existing_adapter(deadline, max_seconds=post_run_probe_seconds):
            try:
                self._set_breakpoints()
                snapshot = self._snapshot(wait_seconds=max(1.0, min(5.0, deadline - time.monotonic())))
                snapshot = self._advance_to_start(
                    snapshot,
                    max(1.0, min(5.0, deadline - time.monotonic())),
                )
                snapshot = self._require_paused(snapshot, "设计器接管调试会话")
                snapshot["operation"] = "start"
                snapshot["session_reused"] = False
                snapshot["message"] = "已复用设计器现有调试会话；后续 next/continue 会复用当前 Chrome 和运行进程。"
                return snapshot
            except Exception as exc:
                self.error = str(exc)
                self.status = "runtime_error"
                self._persist()
                raise ShizaiCloudError(
                    f"设计器已接管调试会话，但 MCP 配置断点失败：{exc}；请在设计器中停止当前调试后重试"
                ) from exc

        dap_connected = False
        while time.monotonic() < deadline:
            self.port, port_mtime = _read_port_info(self.paths)
            listener_pids = None
            if self.port:
                try:
                    listener_pids = self.listener_probe(self.port)
                except Exception:
                    listener_pids = None
            # Never open a raw TCP probe here.  debugpy treats the first socket
            # as the DAP client and may end the debug session when that probe
            # closes before the designer's DebugAdapterService connects.
            port_ready = self._is_current_adapter_port(
                self.port,
                port_mtime,
                listener_pids=listener_pids,
            )
            if port_ready:
                try:
                    self._rpc("DebugAdapterService", "connect", [self.port])
                    dap_connected = True
                    self._debug(
                        "initialize",
                        {
                            "clientID": "szrpa",
                            "clientName": "SZRPA",
                            "adapterID": "debugpy",
                            "pathFormat": "path",
                            "linesStartAt1": True,
                            "columnsStartAt1": True,
                            "supportsVariableType": True,
                            "supportsRunInTerminalRequest": True,
                            "locale": "en",
                        },
                    )

                    # debugpy keeps the DAP ``attach`` request pending until the
                    # client sends configurationDone. The designer sends
                    # attach, setBreakpoints, and configurationDone in that
                    # order without waiting for attach's response. Waiting for
                    # attach here deadlocks that handshake and causes debugpy
                    # to reject configurationDone as out of lifecycle.
                    attach_done = threading.Event()
                    attach_error: list[BaseException] = []

                    def attach_worker() -> None:
                        try:
                            self._debug("attach", {"justMyCode": True, "__restart": True})
                        except BaseException as exc:  # propagate the DAP error on the caller thread
                            attach_error.append(exc)
                        finally:
                            attach_done.set()

                    attach_thread = threading.Thread(
                        target=attach_worker,
                        name="shizai-native-debug-attach",
                        daemon=True,
                    )
                    attach_thread.start()
                    # Give the worker time to put attach on the desktop RPC
                    # channel before the following DAP requests are submitted.
                    attach_done.wait(0.05)
                    # The designer's Initialized-event handler installs the
                    # component breakpoints before configurationDone.  A DAP
                    # next request without these breakpoints only steps Python
                    # lines and is not a component-level debugger.
                    self._set_breakpoints()
                    self._debug("configurationDone")
                    # The installed designer does not promise that the RPC
                    # carrying ``attach`` resolves before the first stopped
                    # event.  ConfigurationDone completes the DAP lifecycle;
                    # allow a short grace period, then let _snapshot observe
                    # the real stop instead of blocking the session on a late
                    # desktop-RPC acknowledgement.
                    attach_done.wait(max(0.25, min(1.0, deadline - time.monotonic())))
                    if attach_error:
                        raise attach_error[0]
                    self.status = "connected"
                    self._persist()
                    snapshot = self._snapshot(wait_seconds=max(1.0, min(5.0, deadline - time.monotonic())))
                    snapshot = self._advance_to_start(
                        snapshot,
                        max(1.0, min(5.0, deadline - time.monotonic())),
                    )
                    snapshot = self._require_paused(snapshot, "MCP 自建调试会话")
                    snapshot["operation"] = "start"
                    snapshot["session_reused"] = False
                    snapshot["message"] = "Native RPA 调试会话已连接；后续 next/continue 会复用当前 Chrome 和运行进程。"
                    return snapshot
                except Exception as exc:
                    self.error = str(exc)
                    if dap_connected:
                        self.status = "runtime_error"
                        self._persist()
                        try:
                            self._debug("disconnect")
                        except Exception:
                            pass
                        raise ShizaiCloudError(f"Native DAP 已连接，但初始化或首个组件停靠失败：{exc}") from exc
                    self.status = "connecting"
            time.sleep(0.2)
        self.status = "start_timeout"
        self.error = self.error or "debugpy port was not published or DebugAdapterService.connect timed out"
        try:
            # AppRunService.run is intentionally kept in flight during a live
            # debug session. Always release it when the DAP handshake never
            # becomes usable, otherwise the next start may reuse a stale run.
            self._rpc("AppRunService", "stop", [{}])
        except Exception as exc:
            self.error = f"{self.error}; cleanup failed: {exc}"
        self._persist()
        raise ShizaiCloudError(self.error)

    def command(self, operation: str, wait_seconds: float) -> dict[str, Any]:
        if operation not in _DEBUG_COMMANDS:
            raise ValueError(f"unsupported native debug operation: {operation}")
        with self.command_lock:
            if self.status in _TERMINAL_SESSION_STATUSES:
                raise ValueError(f"debug session is not active: {self.status}")
            if self.thread_id is None:
                self._snapshot(wait_seconds=2.0)
            if self.thread_id is None:
                raise ValueError("debug session has no stopped thread yet; call status and retry")
            previous_component = (self.current or {}).get("component_id")
            previous_sequence = (self.current or {}).get("component_sequence")
            attempts = 32 if operation == "next" else 1
            result: dict[str, Any] = self._result(operation)
            preflight = self._current_runtime_preflight()
            if preflight is not None and not preflight.get("valid", False):
                self.status = "paused"
                self.error = None
                self._persist()
                result = self._result(
                    operation,
                    message=str(preflight.get("message") or "网页组件运行前预检未通过，已保持当前组件停靠。"),
                )
                result["preflight"] = preflight
                result["preflight_blocked"] = True
                return result
            # Component-level ``next`` must never rely solely on a later
            # breakpoint. A DAP ``continue`` can execute the following
            # component when that breakpoint is stale or is not resolved by
            # the packaged debug adapter. Step over generated Python lines and
            # accept completion only at the exact next component call site.
            dap_operation = operation
            for _ in range(attempts):
                self.status = "running"
                self._persist()
                try:
                    if dap_operation == "continue":
                        self._debug_async(dap_operation, {"threadId": self.thread_id})
                    else:
                        self._debug(dap_operation, {"threadId": self.thread_id})
                except Exception as exc:
                    if self._mark_terminal_if_needed(str(exc)):
                        return self._result(
                            operation,
                            message=(
                                "组件运行失败；已保留 RuntimeHost 错误证据。"
                                if self.status == "runtime_error"
                                else "运行已结束，未再发送调试命令。"
                            ),
                        )
                    raise
                result = self._wait_after_command(operation, wait_seconds)
                current_component = (result.get("current") or {}).get("component_id")
                if operation != "next":
                    return result
                if result.get("status") in {"runtime_error", "succeeded", "stopped"}:
                    if result.get("status") == "succeeded" and previous_component:
                        result["runtime_verification"] = {
                            "verified": True,
                            "component_executed": True,
                            "component_id": previous_component,
                            "component_sequence": previous_sequence,
                            "evidence_kind": "dap_terminal_state",
                        }
                    return result
                current = result.get("current") if isinstance(result.get("current"), dict) else {}
                current_sequence = current.get("component_sequence")
                if (
                    result.get("status") == "paused"
                    and isinstance(previous_sequence, int)
                    and previous_sequence == len(self.component_ids)
                    and current_sequence == previous_sequence
                    and current.get("component_boundary") is False
                    and isinstance(self.last_line, int)
                ):
                    previous_record = next(
                        (
                            record
                            for record in _component_records(_position_records(self.project_dir, self.task_path))
                            if int(record.get("ordinal") or 0) == previous_sequence
                        ),
                        None,
                    )
                    if previous_record and self.last_line > int(previous_record.get("line") or 0):
                        result["component_completed"] = True
                        result["completed_component_id"] = previous_component
                        result["flow_tail_pending"] = True
                        if previous_component:
                            result["runtime_verification"] = {
                                "verified": True,
                                "component_executed": True,
                                "component_id": previous_component,
                                "component_sequence": previous_sequence,
                                "evidence_kind": "dap_component_boundary",
                            }
                        result["message"] = "已执行最后一个组件并停在流程尾部；没有继续发送可能跨出流程的调试命令。"
                        return result
                if (
                    result.get("status") == "paused"
                    and current.get("component_boundary") is True
                    and current_component != previous_component
                    and (
                        not isinstance(previous_sequence, int)
                        or not isinstance(current_sequence, int)
                        or current_sequence > previous_sequence
                    )
                ):
                    if previous_component:
                        result["runtime_verification"] = {
                            "verified": True,
                            "component_executed": True,
                            "component_id": previous_component,
                            "component_sequence": previous_sequence,
                            "evidence_kind": "dap_component_boundary",
                        }
                    return result
            return result

    def _wait_after_command(self, operation: str, wait_seconds: float) -> dict[str, Any]:
        old_line = self.last_line
        deadline = time.monotonic() + wait_seconds
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                async_error = self._consume_async_debug_error()
                if async_error:
                    if self._mark_terminal_if_needed(async_error):
                        result = self._result(
                            operation,
                            message=(
                                "组件运行失败；已保留 RuntimeHost 错误证据。"
                                if self.status == "runtime_error"
                                else "运行已结束，未再发送调试命令。"
                            ),
                        )
                        return result
                    raise ShizaiCloudError(async_error)
                snapshot = self._snapshot(wait_seconds=0.5)
                if snapshot.get("status") in {"runtime_error", "succeeded", "stopped"}:
                    snapshot["operation"] = operation
                    return snapshot
                if self.status == "paused" and (old_line is None or self.last_line != old_line):
                    snapshot["operation"] = operation
                    snapshot["message"] = "已完成一步调试；会话和浏览器窗口保持复用。"
                    return snapshot
            except Exception as exc:
                # Keep transport noise local to this polling attempt. A
                # subsequent valid stop frame clears it; only an actual
                # timeout without a stop is persisted below.
                last_error = str(exc)
            if self.run_thread and not self.run_thread.is_alive() and self.run_error:
                self.status = "runtime_error"
                self._persist()
                return self._result(operation, message="运行进程已结束，请查看 error 和 status。")
            if self._run_finished_without_stack():
                self.status = "succeeded"
                self._persist()
                return self._result(operation, message="运行进程已结束，未收到新的组件停靠位置。")
            time.sleep(0.15)
        if self.status == "paused":
            self.error = None
        elif last_error:
            self.error = last_error
        self._persist()
        return self._result(
            operation, message="调试命令已发送，但在等待窗口内尚未收到新的停靠位置；请再次调用 status。"
        )

    def stop(self) -> dict[str, Any]:
        previous_status = self.status
        already_terminal = previous_status in _TERMINAL_SESSION_STATUSES
        errors: list[str] = []
        stop_attempted = False
        stop_acknowledged = False
        disconnect_attempted = False
        self.status = "stopping"
        self._persist()

        # Stop RuntimeHost while the DAP connection is still available.  The
        # previous order disconnected DAP first, which could leave the desktop
        # service waiting on the run and turn a normal stop into a long RPC
        # timeout.  DAP disconnect remains best-effort and is always attempted
        # after the official run-stop request.
        if self.run_owned:
            stop_attempted = True
            try:
                self._rpc("AppRunService", "stop", [{}])
                stop_acknowledged = True
            except Exception as exc:
                if not _app_run_already_stopped(exc):
                    errors.append(str(exc))
                else:
                    stop_acknowledged = True
        try:
            adapter_alive = not already_terminal and not self.runtime_error_evidence
            if adapter_alive and (self.port or self.adapter_mode == "designer_reused"):
                disconnect_attempted = True
                self._debug("disconnect")
        except Exception as exc:
            errors.append(str(exc))
        self.status = "cleanup_timeout" if errors else "stopped"
        if errors:
            self.error = "; ".join(errors) if not self.error else f"{self.error}; cleanup failed: {'; '.join(errors)}"
        self._persist()
        result = self._result("stopped")
        result.update(
            {
                "stopped": not errors,
                "already_stopped": already_terminal and self.status == "stopped",
                "cleanup_complete": not errors,
                "cleanup_status": "complete" if not errors else "timeout",
                "errors": errors,
                "stop_attempted": stop_attempted,
                "stop_acknowledged": stop_acknowledged,
                "disconnect_attempted": disconnect_attempted,
                "retryable": bool(errors),
                "next_tool": "shizai_rpa_debug_stop" if errors else None,
                "terminal_cleanup_state": self.status,
            }
        )
        return result

    def reconnect(self, *, wait_seconds: float = 5.0) -> dict[str, Any]:
        """Reattach this process to a persisted native debug session.

        The CLI is normally invoked once per command, while the runtime and
        Chrome window belong to the desktop designer.  Rebuilding the Python
        object is therefore not enough: first reuse the designer's existing
        DebugAdapterService connection, then fall back to the persisted adapter
        port without starting AppRunService again.
        """
        if self.status in _TERMINAL_SESSION_STATUSES:
            raise ValueError(f"debug session is not active: {self.status}")
        try:
            if self._probe_existing_adapter():
                self.adapter_mode = self.adapter_mode or "designer_reused"
                self._set_breakpoints()
                snapshot = self._snapshot(wait_seconds=wait_seconds)
                snapshot["message"] = "已恢复设计器现有 DAP 连接；未重复启动运行进程或浏览器。"
                return snapshot
        except Exception as exc:
            probe_error = str(exc)
        else:
            probe_error = ""

        if not self.port:
            detail = f"；设计器连接探测失败：{probe_error}" if probe_error else ""
            raise ValueError(f"持久化调试会话没有可恢复的 DAP 连接或端口{detail}；请在设计器中停止当前调试后重新启动")
        try:
            listeners = self.listener_probe(self.port)
        except Exception:
            listeners = None
        if listeners is not None and not listeners:
            raise ValueError(f"持久化 DAP 端口 {self.port} 当前没有监听进程；运行会话可能已经结束，未重新打开浏览器")

        # This is the same debugpy lifecycle used by start(), but it is a
        # reconnect only: no AppRunService.run call is made here.
        self._rpc("DebugAdapterService", "connect", [self.port])
        self._debug(
            "initialize",
            {
                "clientID": "szrpa-cli-reconnect",
                "clientName": "SZRPA CLI reconnect",
                "adapterID": "debugpy",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        attach_done = threading.Event()
        attach_error: list[BaseException] = []

        def attach_worker() -> None:
            try:
                self._debug("attach", {"justMyCode": True, "__restart": True})
            except BaseException as exc:
                attach_error.append(exc)
            finally:
                attach_done.set()

        threading.Thread(
            target=attach_worker,
            name="shizai-native-debug-reconnect-attach",
            daemon=True,
        ).start()
        attach_done.wait(0.05)
        self._set_breakpoints()
        self._debug("configurationDone")
        attach_done.wait(max(0.25, min(1.0, wait_seconds)))
        if attach_error:
            raise ShizaiCloudError(str(attach_error[0])) from attach_error[0]
        self.adapter_mode = "mcp_reconnected"
        self.status = "connected"
        self._persist()
        snapshot = self._snapshot(wait_seconds=wait_seconds)
        snapshot["message"] = "已按持久化端口恢复 DAP 连接；未重复启动运行进程或浏览器。"
        return snapshot


_SESSIONS: dict[str, NativeDebugSession] = {}
_SESSIONS_LOCK = threading.RLock()


def _resolve_project(paths: ShizaiPaths, project_ref: str) -> Path:
    candidate = Path(project_ref).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    if paths.applications_root.is_dir():
        candidate = paths.applications_root / project_ref
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(f"native project not found: {project_ref}")


def debug_start(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    start_from: int = 1,
    start_timeout: float = _DEFAULT_START_TIMEOUT,
    client: Any | None = None,
    listener_probe: Callable[[int], set[int] | None] | None = None,
) -> dict[str, Any]:
    """Start design-time validation at the requested component number."""
    project_dir = _resolve_project(paths, project_ref)
    set_rpa_ui_feedback(
        "debug_starting",
        "design_validation",
        "正在启动流程设计验证，会复用当前 Chrome 窗口；这不是正式运行。",
        animation="debug_takeover",
        flow_ref=str(project_dir),
        start_from=int(start_from),
    )
    task_path, payload = _native_task(project_dir)
    component_ids = _native_statement_ids(payload)
    if not component_ids:
        raise ValueError("native task contains no ordered components")
    metadata = _read_json(project_dir / "szrpa.json") or {}
    component_plan = metadata.get("mcpComponentPlan") if isinstance(metadata, dict) else None
    if isinstance(component_plan, list) and component_plan and len(component_ids) != len(component_plan):
        raise ValueError(
            "design-time execution requires the complete component graph: "
            f"planned={len(component_plan)}, written={len(component_ids)}"
        )
    try:
        start_from = int(start_from)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_from must be an integer starting at 1") from exc
    if not 1 <= start_from <= len(component_ids):
        raise ValueError(f"start_from must be between 1 and {len(component_ids)}")
    caller = client or ShizaiDesktopAgent()
    key = str(project_dir)
    persisted = _read_json(_state_path(paths, project_dir)) or {}
    persisted_status = str(persisted.get("status") or "").strip().casefold()
    with _SESSIONS_LOCK:
        old = _SESSIONS.get(key)
        if old and old.status not in _TERMINAL_SESSION_STATUSES:
            changed = old._definition_changed_result()
            if changed:
                return changed
            result = old._result("start", message="当前流程已有调试会话，已复用现有会话；未重复打开浏览器。")
            result = old._attach_runtime_preflight(result)
            result["ui_feedback"] = set_rpa_ui_feedback(
                "debug_paused" if old.status == "paused" else "debug_running",
                "design_validation",
                "已复用当前设计验证会话和 Chrome 窗口。",
                animation="debug_reused",
                flow_ref=str(project_dir),
                status=old.status,
                session_reused=True,
            )
            return result
        session = NativeDebugSession(
            paths=paths,
            project_dir=project_dir,
            task_path=task_path,
            arrangement_id=_arrangement_id(project_dir, task_path),
            component_ids=component_ids,
            start_from=start_from,
            client=caller,
            state_file=_state_path(paths, project_dir),
            reuse_existing_adapter=persisted_status not in _TERMINAL_SESSION_STATUSES,
            listener_probe=listener_probe or _listening_pids,
        )
        _SESSIONS[key] = session
    try:
        result = session.start(_finite_timeout(start_timeout, _DEFAULT_START_TIMEOUT, 180.0))
        result = session._attach_runtime_preflight(result)
        result["ui_feedback"] = set_rpa_ui_feedback(
            "debug_paused" if result.get("status") == "paused" else "debug_running",
            "design_validation",
            str(result.get("message") or "设计验证会话已就绪，当前组件已停靠；下一步可执行组件级单步。"),
            animation="debug_ready",
            flow_ref=str(project_dir),
            status=result.get("status"),
            session_reused=bool(result.get("session_reused")),
        )
        return result
    except Exception:
        set_rpa_ui_feedback(
            "debug_error",
            "design_validation",
            "流程设计验证启动失败，请查看返回的错误信息。",
            animation="debug_error",
            flow_ref=str(project_dir),
        )
        with _SESSIONS_LOCK:
            if _SESSIONS.get(key) is session:
                _SESSIONS.pop(key, None)
        raise


def _restored_session(
    paths: ShizaiPaths,
    project_dir: Path,
    persisted: dict[str, Any],
    *,
    client: Any | None = None,
) -> NativeDebugSession:
    """Rebuild a session object from disk without starting a runtime."""
    task_path, payload = _native_task(project_dir)
    component_ids = _native_statement_ids(payload)
    if not component_ids:
        raise ValueError("persisted debug project contains no ordered components")
    current = persisted.get("current")
    current = current if isinstance(current, dict) else None
    try:
        start_from = int(persisted.get("start_from") or 1)
    except (TypeError, ValueError):
        start_from = 1
    try:
        thread_id = int(persisted["thread_id"]) if persisted.get("thread_id") is not None else None
    except (TypeError, ValueError):
        thread_id = None
    try:
        port = int(persisted["port"]) if persisted.get("port") is not None else None
    except (TypeError, ValueError):
        port = None
    raw_lines = persisted.get("breakpoint_lines")
    breakpoint_lines = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            try:
                breakpoint_lines.append(int(item))
            except (TypeError, ValueError):
                continue
    raw_pids = persisted.get("listener_pids_before")
    listener_pids = []
    if isinstance(raw_pids, list):
        for item in raw_pids:
            try:
                listener_pids.append(int(item))
            except (TypeError, ValueError):
                continue
    session = NativeDebugSession(
        paths=paths,
        project_dir=project_dir,
        task_path=task_path,
        arrangement_id=str(persisted.get("arrangement_id") or _arrangement_id(project_dir, task_path)),
        component_ids=component_ids,
        start_from=max(1, min(start_from, len(component_ids))),
        client=client or ShizaiDesktopAgent(),
        state_file=_state_path(paths, project_dir),
        port=port,
        thread_id=thread_id,
        status=str(persisted.get("status") or "starting"),
        definition_sha256=persisted.get("definition_sha256"),
        current=current,
        last_line=(
            int(current.get("line")) if isinstance(current, dict) and str(current.get("line", "")).isdigit() else None
        ),
        error=str(persisted.get("error")) if persisted.get("error") else None,
        created_at=float(persisted.get("created_at") or time.time()),
        updated_at=float(persisted.get("updated_at") or time.time()),
        runtime_error_evidence=(
            persisted.get("runtime_error_evidence")
            if isinstance(persisted.get("runtime_error_evidence"), dict)
            else None
        ),
        dap_terminal_event=str(persisted.get("dap_terminal_event")) if persisted.get("dap_terminal_event") else None,
        breakpoint_lines=breakpoint_lines,
        code_source=str(persisted.get("code_source") or ""),
        adapter_mode=str(persisted.get("adapter_mode") or "mcp_created"),
        run_owned=bool(persisted.get("run_owned", False)),
        reuse_existing_adapter=True,
        run_started_at=(float(persisted["run_started_at"]) if persisted.get("run_started_at") is not None else None),
        port_before=(int(persisted["port_before"]) if persisted.get("port_before") is not None else None),
        port_mtime_before=(
            int(persisted["port_mtime_before"]) if persisted.get("port_mtime_before") is not None else None
        ),
        listener_pids_before=listener_pids,
        listener_probe_before=persisted.get("listener_probe_before"),
        archived_outputs=(
            persisted.get("archived_outputs") if isinstance(persisted.get("archived_outputs"), list) else []
        ),
    )
    return session


def _get_session(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    client: Any | None = None,
    reconnect: bool = True,
) -> NativeDebugSession:
    project_dir = _resolve_project(paths, project_ref)
    key = str(project_dir)
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
    if session is None:
        persisted = _read_json(_state_path(paths, project_dir))
        if persisted and persisted.get("status") not in _TERMINAL_SESSION_STATUSES:
            session = _restored_session(paths, project_dir, persisted, client=client)
            with _SESSIONS_LOCK:
                existing = _SESSIONS.get(key)
                if existing is not None:
                    session = existing
                else:
                    _SESSIONS[key] = session
            if reconnect:
                try:
                    session.reconnect()
                except Exception:
                    with _SESSIONS_LOCK:
                        if _SESSIONS.get(key) is session:
                            _SESSIONS.pop(key, None)
                    raise
            return session
        raise ValueError("no active debug session; call shizai_rpa_debug_start first")
    return session


def debug_status(paths: ShizaiPaths, project_ref: str, *, client: Any | None = None) -> dict[str, Any]:
    """Return the last known debug state without changing the runtime.

    A status probe is intentionally read-only.  Reconnecting DAP merely to
    display status can cause debugpy/designer builds to stop at the first
    breakpoint again, making a cross-process status call appear to rewind a
    successful component step.  Commands that advance execution still use
    ``_get_session(..., reconnect=True)`` through ``debug_command``.
    """
    try:
        session = _get_session(paths, project_ref, client=client, reconnect=False)
    except ValueError:
        project_dir = _resolve_project(paths, project_ref)
        persisted = _read_json(_state_path(paths, project_dir))
        if persisted:
            persisted["kind"] = "native_rpa_debug_session"
            persisted["execution_mode"] = "design_validation"
            persisted["production_run"] = False
            persisted["control_mode"] = "workflow_runtime"
            persisted["browser_control"] = "runtimehost"
            persisted["runtime_execution"] = True
            persisted["live_connection"] = False
            persisted["session_reused"] = False
            persisted["ui_feedback"] = set_rpa_ui_feedback(
                "debug_paused" if persisted.get("status") == "paused" else "idle",
                "design_validation",
                "读取已保存的设计验证状态；当前 MCP 进程没有活动调试连接。",
                animation="debug_status",
                overlay_state="inactive",
                flow_ref=str(project_dir),
                status=persisted.get("status"),
            )
            return persisted
        result = {
            "kind": "native_rpa_debug_session",
            "execution_mode": "design_validation",
            "production_run": False,
            "control_mode": "workflow_runtime",
            "browser_control": "runtimehost",
            "runtime_execution": False,
            "project_ref": str(project_dir),
            "status": "never_started",
            "live_connection": False,
        }
        result["ui_feedback"] = set_rpa_ui_feedback(
            "idle",
            "design_validation",
            "当前流程还没有活动设计验证会话。",
            animation="idle",
            overlay_state="inactive",
            flow_ref=str(project_dir),
        )
        return result
    result = session._result("status")
    status = str(result.get("status") or "")
    result["ui_feedback"] = set_rpa_ui_feedback(
        "debug_paused"
        if status == "paused"
        else "debug_complete"
        if status == "succeeded"
        else "debug_error"
        if status == "runtime_error"
        else "debug_running",
        "design_validation",
        "当前组件已停靠，可继续单步。" if status == "paused" else str(result.get("message") or "调试状态已更新。"),
        animation="debug_status",
        overlay_state="inactive",
        flow_ref=str(session.project_dir),
        status=status,
        component_id=(result.get("current") or {}).get("component_id")
        if isinstance(result.get("current"), dict)
        else None,
    )
    return result


def debug_capabilities(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Report whether a native flow can use component-level single stepping.

    This is a read-only probe. It inspects the real task and generated module,
    and reports the live in-memory session when this MCP process owns one. It
    does not open Chrome, start AppRunService, or connect a second DAP client.
    """
    project_dir = _resolve_project(paths, project_ref)
    task_path, payload = _native_task(project_dir)
    component_ids = _native_statement_ids(payload)
    records = _position_records(project_dir, task_path)
    positions = _component_records(records)
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(str(project_dir))
    persisted = _read_json(_state_path(paths, project_dir)) or {}
    persisted_active = bool(
        persisted.get("status") not in {None, *_TERMINAL_SESSION_STATUSES}
    )
    active = (
        bool(session and session.status not in _TERMINAL_SESSION_STATUSES)
        or persisted_active
    )
    port, port_mtime = _read_port_info(paths)
    return {
        "kind": "native_rpa_debug_capabilities",
        "execution_mode": "design_validation",
        "production_run": False,
        "control_mode": "workflow_runtime",
        "browser_control": "runtimehost",
        "runtime_execution": False,
        "project_ref": str(project_dir),
        "task": {"path": str(task_path), "node_id": task_path.stem},
        "component_count": len(component_ids),
        "generated_position_count": len(positions),
        "ready": bool(component_ids and positions),
        "active_session": active,
        "live_connection": bool(
            session and session.status not in _TERMINAL_SESSION_STATUSES
        ),
        "persisted_session": persisted_active,
        "session_reused": active,
        "port": port,
        "port_file_present": port_mtime is not None,
        "capabilities": {
            "component_next": {
                "supported": bool(component_ids and positions),
                "tool": "shizai_rpa_debug_command",
                "arguments": {"action": "next"},
                "implementation": "strict DAP step-over to the next exact generated component boundary",
            },
            "component_continue": {
                "supported": bool(component_ids and positions),
                "tool": "shizai_rpa_debug_command",
                "arguments": {"action": "continue"},
            },
            "dap_step_in": {
                "supported": True,
                "tool": "shizai_rpa_debug_command",
                "arguments": {"action": "step_in"},
            },
            "dap_step_out": {
                "supported": True,
                "tool": "shizai_rpa_debug_command",
                "arguments": {"action": "step_out"},
            },
            "browser_session_reuse": {
                "supported": True,
                "description": "同一 MCP 调试会话复用 AppRunService、DAP 线程和流程打开的 Chrome 窗口",
            },
        },
        "next_action": (
            {"tool": "shizai_rpa_debug_status", "reason": "已有活动会话，读取当前停靠组件后再执行下一步。"}
            if active
            else {"tool": "shizai_rpa_debug_start", "reason": "先启动或接管 Native DAP，会话启动后再使用组件级单步。"}
        ),
    }


def debug_command(
    paths: ShizaiPaths,
    project_ref: str,
    operation: str,
    *,
    wait_seconds: float = _DEFAULT_WAIT_SECONDS,
    client: Any | None = None,
) -> dict[str, Any]:
    # Inspect persisted definitions before reconnecting DAP or advancing an
    # old Python frame; binding an element does not hot-reload that frame.
    session = _get_session(paths, project_ref, client=client, reconnect=False)
    changed = session._definition_changed_result()
    if changed:
        return changed
    session = _get_session(paths, project_ref, client=client)
    set_rpa_ui_feedback(
        "debug_running",
        "design_validation",
        f"正在执行设计态组件验证动作 {operation}，等待下一个组件停靠。",
        animation="debug_step",
        flow_ref=str(session.project_dir),
        operation=operation,
    )
    result = session.command(operation, _finite_timeout(wait_seconds, _DEFAULT_WAIT_SECONDS, 120.0))
    status = str(result.get("status") or "")
    if result.get("preflight_blocked"):
        feedback_message = str(result.get("message") or "网页组件运行前预检未通过，已保持当前组件停靠。")
        feedback_animation = "preflight_blocked"
    else:
        feedback_message = (
            "已完成一步设计验证，当前 Chrome 和验证会话保持复用。"
            if status == "paused"
            else str(result.get("message") or "调试状态已更新。")
        )
        feedback_animation = (
            "debug_step_complete"
            if status == "paused"
            else "debug_complete"
            if status == "succeeded"
            else "debug_error"
            if status == "runtime_error"
            else "debug_step"
        )
    ui_feedback = set_rpa_ui_feedback(
        "debug_paused"
        if status == "paused"
        else (
            "debug_complete"
            if status == "succeeded"
            else "debug_error"
            if status == "runtime_error"
            else "debug_running"
        ),
        "design_validation",
        feedback_message,
        animation=feedback_animation,
        flow_ref=str(session.project_dir),
        operation=operation,
        status=status,
        component_id=(result.get("current") or {}).get("component_id")
        if isinstance(result.get("current"), dict)
        else None,
    )
    result["ui_feedback"] = ui_feedback
    return result


def debug_stop(paths: ShizaiPaths, project_ref: str, *, client: Any | None = None) -> dict[str, Any]:
    try:
        session = _get_session(paths, project_ref, client=client)
    except ValueError:
        project_dir = _resolve_project(paths, project_ref)
        persisted = _read_json(_state_path(paths, project_dir)) or {}
        was_stopped = persisted.get("status") == "stopped"
        errors: list[str] = []
        stop_attempted = False
        stop_acknowledged = False
        if persisted.get("run_owned"):
            stop_attempted = True
            try:
                (client or ShizaiDesktopAgent()).call("AppRunService", "stop", [{}])
                stop_acknowledged = True
            except Exception as exc:
                if _app_run_already_stopped(exc):
                    stop_acknowledged = True
                else:
                    errors.append(str(exc))
        persisted.update(
            {
                "kind": "native_rpa_debug_session",
                "control_mode": "workflow_runtime",
                "browser_control": "runtimehost",
                "runtime_execution": False,
                "project_ref": str(project_dir),
                "status": "cleanup_timeout" if errors else "stopped",
                "stopped": not errors,
                "already_stopped": was_stopped and not persisted.get("run_owned"),
                "cleanup_complete": not errors,
                "cleanup_status": "complete" if not errors else "timeout",
                "errors": errors,
                "stop_attempted": stop_attempted,
                "stop_acknowledged": stop_acknowledged,
                "disconnect_attempted": False,
                "retryable": bool(errors),
                "next_tool": "shizai_rpa_debug_stop" if errors else None,
                "terminal_cleanup_state": "cleanup_timeout" if errors else "stopped",
            }
        )
        # This branch is used after a short-lived CLI process has lost its
        # in-memory session object. The returned payload is not enough: the
        # next CLI invocation reads this marker from disk, so persist the
        # terminal cleanup state before returning.
        _atomic_write_json(_state_path(paths, project_dir), persisted)
        persisted["ui_feedback"] = set_rpa_ui_feedback(
            "idle" if not errors else "debug_cleanup_timeout",
            "design_validation",
            "流程设计验证已停止，当前 Chrome 会话仍可复用。"
            if not errors
            else "流程设计验证清理未完成，请重试停止操作。",
            animation="idle" if not errors else "debug_cleanup_timeout",
            overlay_state="inactive" if not errors else "unknown",
            flow_ref=str(project_dir),
        )
        return persisted
    with _SESSIONS_LOCK:
        result = session.stop()
        _SESSIONS.pop(str(session.project_dir), None)
    result["ui_feedback"] = set_rpa_ui_feedback(
        "idle" if result.get("cleanup_complete") else "debug_cleanup_timeout",
        "design_validation",
        "流程设计验证已停止，当前 Chrome 会话仍可复用。"
        if result.get("cleanup_complete")
        else "流程设计验证清理未完成，请重试停止操作。",
        animation="idle" if result.get("cleanup_complete") else "debug_cleanup_timeout",
        overlay_state="inactive" if result.get("cleanup_complete") else "unknown",
        flow_ref=str(session.project_dir),
    )
    return result
