"""Bridge to the browser extension already bundled with Shizai Agent.

The Shizai desktop process owns the extension's business channel.  DeepCapture
calls therefore go through the authenticated desktop WebSocket instead of
starting a second browser or asking the user to install a Codex-specific
extension.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .cloud_api import ShizaiCloudConfig, ShizaiCloudError, ShizaiDesktopAgent

DEEP_CAPTURE_SERVICE = "DeepCaptureService"
DEFAULT_CAPTURE_LEVEL = 6
DEFAULT_CAPTURE_MODE = 2
DEFAULT_CAPTURE_RPC_TIMEOUT = 6.0
DEFAULT_CAPTURE_OPERATION_TIMEOUT = 20.0
DEFAULT_CAPTURE_READ_ATTEMPTS = 2
_UIA_WEB_CONTROL_NAMES = {
    # English UIA control names used by Chrome's accessibility provider.
    "edit",
    "text",
    "static",
    "label",
    "button",
    "hyperlink",
    "link",
    "image",
    "list",
    "listitem",
    "table",
    "row",
    "cell",
    "checkbox",
    "combobox",
    "textarea",
    "tab",
    "radio",
    "radiobutton",
    "heading",
    "group",
    # Localized names returned by some versions of the native extension.
    "文档",
    "文本框",
    "文本",
    "按钮",
    "超链接",
    "链接",
    "图像",
    "列表",
    "列表项",
    "表格",
    "行",
    "单元格",
    "复选框",
    "组合框",
    "选项卡",
    "单选按钮",
    "标题",
    "区域",
}
_READ_ONLY_METHODS = {
    "getDeepCaptureElementChildrenById",
    "getDeepCaptureElementDetail",
    "getDeepCaptureElementSelector",
}
_CONTROL_METHODS = {
    "startDeepCapture",
    "startDeepCaptureInspect",
    "stopDeepCapture",
    "stopDeepCaptureInspect",
}
_TRANSIENT_CAPTURE_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "请求超时",
    "请求失败，请重试",
    "connection reset",
    "connection closed",
    "channel_unavailable",
    "通道无响应",
)


def _capture_budget(method: Any) -> Any:
    """Give each browser read a finite total budget, including nested reads."""
    @wraps(method)
    def wrapped(self: "ShizaiExtensionBridge", *args: Any, **kwargs: Any) -> Any:
        previous = self._operation_deadline
        deadline = time.monotonic() + self.capture_operation_timeout
        self._operation_deadline = min(previous, deadline) if previous is not None else deadline
        try:
            return method(self, *args, **kwargs)
        finally:
            self._operation_deadline = previous

    return wrapped

# MCP calls are synchronous, while the native extension owns the visible
# picker/highlight overlay. Keep the last truthful phase so a client can show
# progress between calls without pretending that MCP draws the overlay itself.
_CAPTURE_UI_FEEDBACK: dict[str, Any] = {
    "schema": "shizai-rpa-ui-feedback/v1",
    "phase": "idle",
    "activity": "ready",
    "overlay": "native_shizai_extension",
    "overlay_state": "inactive",
    "animation": "idle",
    "message": "实在 Agent 元素拾取待命。",
    "reused_browser": True,
    "uses_current_profile": True,
}
_CAPTURE_UI_FEEDBACK_LOCK = threading.RLock()

# DeepCapture element ids are scoped to the active inspect session.  Keep a
# short-lived, process-local snapshot so a model can scan, decide, and then
# capture/import from exactly the same candidate set without forcing another
# tree traversal.  The native extension remains the source of truth for the
# selector itself; this cache only carries ids and scan metadata.
_PAGE_SCAN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PAGE_SCAN_CACHE_LOCK = threading.RLock()
_PAGE_SCAN_CACHE_TTL = 300.0
_PAGE_SCAN_CACHE_LIMIT = 32
_PAGE_SCAN_DISK_CACHE = Path(tempfile.gettempdir()) / "shizai-agent-page-scans.json"
_CAPTURE_SESSION_LOCK = threading.RLock()
_CAPTURE_SESSION_PACKAGE: str | None = None
_CAPTURE_SESSION_STARTED_AT = 0.0
_CAPTURE_SESSION_STATE_DIR = Path(tempfile.gettempdir()) / "shizai-agent-capture-sessions"
_CAPTURE_SESSION_STATE_TTL = 3600.0
_RUNTIME_EXTENSION_IDS = (
    "pcopnibgkbdnlaeagepigbboebdfejmb",
    "lkfoccifgcklgjffocdgalcgbkjnlleg",
    "nfiffhdmjkohbbolbamdboplicimjnpm",
)
_NATIVE_HOST_PROCESS_NAME = "II.RPA.NativeMessagingHost.exe"

_CHROME_EXTENSION_DISABLE_REASONS = {
    1: "user_action",
    2: "permissions_increase",
    4: "reload",
    8: "unsupported_requirement",
    16: "side_load",
    32: "unknown_from_sync",
    64: "not_verified",
    128: "greylist",
    256: "corrupted",
    512: "remote_install",
    1024: "external_extension_prompt",
    2048: "minimum_version",
    4096: "custodian_approval_required",
    8192: "sideload_wipeout",
    16384: "blocked_by_policy",
    32768: "reinstall",
}


def _read_disk_page_scans() -> dict[str, Any]:
    try:
        payload = json.loads(_PAGE_SCAN_DISK_CACHE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_disk_page_scans(payload: dict[str, Any]) -> None:
    temporary = _PAGE_SCAN_DISK_CACHE.with_name(f"{_PAGE_SCAN_DISK_CACHE.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(_PAGE_SCAN_DISK_CACHE)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _remember_page_scan(snapshot: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(18)
    now = time.monotonic()
    wall_time = time.time()
    with _PAGE_SCAN_CACHE_LOCK:
        expired = [key for key, (created, _value) in _PAGE_SCAN_CACHE.items() if now - created > _PAGE_SCAN_CACHE_TTL]
        for key in expired:
            _PAGE_SCAN_CACHE.pop(key, None)
        while len(_PAGE_SCAN_CACHE) >= _PAGE_SCAN_CACHE_LIMIT:
            oldest = min(_PAGE_SCAN_CACHE, key=lambda key: _PAGE_SCAN_CACHE[key][0])
            _PAGE_SCAN_CACHE.pop(oldest, None)
        _PAGE_SCAN_CACHE[token] = (now, snapshot)
        disk = _read_disk_page_scans()
        disk_now = wall_time
        disk = {
            key: value
            for key, value in disk.items()
            if isinstance(value, dict)
            and isinstance(value.get("created_at"), (int, float))
            and disk_now - float(value["created_at"]) <= _PAGE_SCAN_CACHE_TTL
        }
        disk[token] = {"created_at": wall_time, "snapshot": snapshot}
        if len(disk) > _PAGE_SCAN_CACHE_LIMIT:
            oldest = sorted(disk.items(), key=lambda item: float(item[1].get("created_at", 0)))[
                :-_PAGE_SCAN_CACHE_LIMIT
            ]
            for key, _value in oldest:
                disk.pop(key, None)
        _write_disk_page_scans(disk)
    return token


def _read_page_scan(token: str) -> dict[str, Any] | None:
    value = str(token or "").strip()
    if not value:
        return None
    now = time.monotonic()
    with _PAGE_SCAN_CACHE_LOCK:
        cached = _PAGE_SCAN_CACHE.get(value)
        if cached is not None:
            created, snapshot = cached
            if now - created <= _PAGE_SCAN_CACHE_TTL:
                return dict(snapshot)
            _PAGE_SCAN_CACHE.pop(value, None)
        disk = _read_disk_page_scans()
        entry = disk.get(value)
        if not isinstance(entry, dict):
            return None
        created_at = entry.get("created_at")
        snapshot = entry.get("snapshot")
        if not isinstance(created_at, (int, float)) or time.time() - float(created_at) > _PAGE_SCAN_CACHE_TTL:
            disk.pop(value, None)
            _write_disk_page_scans(disk)
            return None
        if not isinstance(snapshot, dict):
            return None
        _PAGE_SCAN_CACHE[value] = (now, snapshot)
        return dict(snapshot)


def _set_capture_ui_feedback(
    phase: str,
    activity: str,
    message: str,
    *,
    overlay_state: str = "active",
    animation: str = "native_extension",
    **extra: Any,
) -> dict[str, Any]:
    """Store a client-facing phase without replacing the native overlay."""
    global _CAPTURE_UI_FEEDBACK
    with _CAPTURE_UI_FEEDBACK_LOCK:
        _CAPTURE_UI_FEEDBACK = {
            "schema": "shizai-rpa-ui-feedback/v1",
            "phase": str(phase),
            "activity": str(activity),
            "overlay": "native_shizai_extension",
            "overlay_state": str(overlay_state),
            "animation": str(animation),
            "message": str(message),
            "reused_browser": True,
            "uses_current_profile": True,
            **extra,
        }
        return dict(_CAPTURE_UI_FEEDBACK)


def set_rpa_ui_feedback(
    phase: str,
    activity: str,
    message: str,
    *,
    overlay_state: str = "inactive",
    animation: str = "workflow_progress",
    **extra: Any,
) -> dict[str, Any]:
    """Publish a truthful workflow-phase update for MCP and CLI clients.

    Capture operations call the same internal writer with an active native
    overlay.  Workflow operations use this public helper to expose progress
    without claiming that MCP draws a browser overlay.
    """
    return _set_capture_ui_feedback(
        phase,
        activity,
        message,
        overlay_state=overlay_state,
        animation=animation,
        **extra,
    )


def _capture_ui_feedback() -> dict[str, Any]:
    """Return a copy so callers cannot mutate the process-level state."""
    with _CAPTURE_UI_FEEDBACK_LOCK:
        return dict(_CAPTURE_UI_FEEDBACK)


def _capture_read_metadata() -> dict[str, Any]:
    """Describe the non-interactive DeepCapture browser control channel."""
    return {
        "control_mode": "capture_read",
        "browser_control": "deep_capture_read",
        "runtime_execution": False,
        "picker_overlay_started": False,
    }


def _capture_session_state_path(package_path: str | Path) -> Path:
    """Return a stable per-project state path for short-lived CLI commands."""
    normalized = str(Path(str(package_path)).expanduser().resolve())
    digest = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:24]
    return _CAPTURE_SESSION_STATE_DIR / f"{digest}.json"


def _read_capture_session_state(package_path: str | Path) -> dict[str, Any] | None:
    """Read the last durable capture-session handshake for one project."""
    path = _capture_session_state_path(package_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    started_at = payload.get("started_at")
    if isinstance(started_at, (int, float)) and time.time() - float(started_at) > _CAPTURE_SESSION_STATE_TTL:
        return None
    return payload


def _write_capture_session_state(package_path: str | Path, payload: dict[str, Any]) -> None:
    """Atomically persist capture metadata so CLI invocations share it."""
    path = _capture_session_state_path(package_path)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# Labels used by the model when it needs to turn a page into a collection
# schema.  These are intentionally hints rather than selectors: the native
# selector is still captured from the chosen element by ``capture_element``.
_SEMANTIC_FIELDS: dict[str, tuple[str, ...]] = {
    "search": ("搜索", "搜索框", "输入框", "search", "keyword", "关键词", "输入", "查找", "检索"),
    "submit": ("搜索按钮", "搜索", "提交", "确定", "查询", "search", "submit"),
    "product_name": (
        "商品名称",
        "商品名",
        "商品标题",
        "标题",
        "名称",
        "goods_title",
        "goods title",
        "title",
        "product",
        "goods",
    ),
    "merchant": (
        "商家",
        "店铺",
        "店名",
        "卖家",
        "自营",
        "旗舰店",
        "专卖店",
        "专营店",
        "官方店",
        "merchant",
        "seller",
        "shop",
        "store",
    ),
    "price": ("价格", "售价", "折后价", "price", "cost", "￥", "¥"),
    "sales": ("销量", "销售量", "销售", "已售", "成交", "volume", "sales volume", "sales", "sold", "orders"),
    "link": ("链接", "网址", "详情", "link", "href", "url"),
}
_SEARCH_BOX_QUERY_ALIASES = {
    "搜索框",
    "输入框",
    "检索框",
    "关键词输入框",
    "search box",
    "search input",
    "keyword input",
}
_SUBMIT_QUERY_ALIASES = {"搜索按钮", "提交按钮", "查询按钮", "search button", "submit button"}
_SHELL_CONTROL_NAMES = {
    "window",
    "pane",
    "document",
    "窗口",
    "窗格",
    "文档",
}
_CHALLENGE_MARKERS = (
    "人机验证",
    "安全验证",
    "滑块验证",
    "验证码",
    "验证一下",
    "快速验证",
    "请完成验证",
    "security check",
    "captcha",
    "robot check",
    "verify you are human",
)
_LOGIN_MARKERS = ("登录", "登陆", "sign in", "log in", "login")


def _default_extension_roots() -> list[Path]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return [
        Path(os.environ["SHIZAI_EXTENSION_ROOT"]).expanduser()
        if os.environ.get("SHIZAI_EXTENSION_ROOT")
        else local_app_data / "IIRPA" / "RPAChromeExtension",
    ]


def _extension_installation() -> dict[str, Any]:
    roots = _default_extension_roots()
    root = next((item for item in roots if item.is_dir()), roots[0])
    manifest = root / "manifest.json"
    crx_files = sorted(root.glob("*.crx")) if root.is_dir() else []
    return {
        "root": str(root),
        "directory_present": root.is_dir(),
        "manifest_present": manifest.is_file(),
        "crx_present": bool(crx_files),
        "crx_files": [item.name for item in crx_files],
        "manifest_version": None,
        "extension_version": None,
    }


def _read_manifest(status: dict[str, Any]) -> None:
    path = Path(status["root"]) / "manifest.json"
    if not path.is_file():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(manifest, dict):
        status["manifest_version"] = manifest.get("manifest_version")
        status["extension_version"] = manifest.get("version")


def _pid_is_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0 or os.name != "nt":
        return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, value)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _discover_native_host_processes() -> list[dict[str, Any]]:
    """Find live Native Messaging hosts when the vendor state file is unavailable.

    Chrome owns the native host lifetime.  The vendor ``processes.json`` file is
    useful metadata, but it can be temporarily missing, partially written, or
    left behind after Chrome exits.  Process discovery keeps preflight truthful
    without starting a second host outside Chrome's native-messaging channel.
    """
    try:
        import psutil
    except ImportError:
        return []

    discovered: list[dict[str, Any]] = []
    expected_name = _NATIVE_HOST_PROCESS_NAME.casefold()
    try:
        processes = psutil.process_iter(["pid", "name", "create_time", "ppid"])
        for process in processes:
            try:
                info = process.info
                if str(info.get("name") or "").casefold() != expected_name:
                    continue
                discovered.append(
                    {
                        "Pid": int(info["pid"]),
                        "ProcessName": _NATIVE_HOST_PROCESS_NAME.removesuffix(".exe"),
                        "StartTime": info.get("create_time"),
                        "Parent": {"Pid": info.get("ppid")},
                        "source": "process_table",
                    }
                )
            except (KeyError, TypeError, ValueError, psutil.Error):
                continue
    except (OSError, PermissionError, psutil.Error):
        return []
    return discovered


def _runtime_extension_status() -> dict[str, Any]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    profile_root = Path(
        os.environ.get(
            "SHIZAI_CHROME_PROFILE_ROOT",
            local_app_data / "Google" / "Chrome" / "User Data" / "Default",
        )
    )
    settings: dict[str, Any] = {}
    secure_preferences = profile_root / "Secure Preferences"
    try:
        preferences = json.loads(secure_preferences.read_text(encoding="utf-8"))
        raw_settings = (preferences.get("extensions") or {}).get("settings") or {}
        settings = raw_settings if isinstance(raw_settings, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass

    extensions: list[dict[str, Any]] = []
    extensions_root = profile_root / "Extensions"
    for extension_id in _RUNTIME_EXTENSION_IDS:
        extension_root = extensions_root / extension_id
        version_dirs = (
            sorted(
                (item for item in extension_root.iterdir() if item.is_dir()),
                key=lambda item: item.name,
                reverse=True,
            )
            if extension_root.is_dir()
            else []
        )
        manifest_path = next(
            (item / "manifest.json" for item in version_dirs if (item / "manifest.json").is_file()),
            None,
        )
        manifest: dict[str, Any] = {}
        if manifest_path is not None:
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        preference = settings.get(extension_id) if isinstance(settings.get(extension_id), dict) else {}
        disable_reasons = preference.get("disable_reasons") or []
        if not isinstance(disable_reasons, list):
            disable_reasons = [disable_reasons]
        normalized_disable_reasons = []
        for reason in disable_reasons:
            try:
                normalized_disable_reasons.append(int(reason))
            except (TypeError, ValueError):
                continue
        disable_reason_labels = [
            _CHROME_EXTENSION_DISABLE_REASONS.get(reason, f"unknown_{reason}") for reason in normalized_disable_reasons
        ]
        extensions.append(
            {
                "id": extension_id,
                "installed": manifest_path is not None,
                "enabled": manifest_path is not None and not normalized_disable_reasons,
                "disable_reasons": normalized_disable_reasons,
                "disable_reason_labels": disable_reason_labels,
                "manifest_version": manifest.get("manifest_version"),
                "version": manifest.get("version"),
                "name": manifest.get("name"),
                "popup": ((manifest.get("action") or {}).get("default_popup")),
                "path": str(manifest_path.parent) if manifest_path is not None else None,
            }
        )

    process_file = _default_extension_roots()[0] / "processes.json"
    process_records: list[dict[str, Any]] = []
    try:
        # The official Native Messaging host writes this file with a UTF-8 BOM.
        loaded_records = json.loads(process_file.read_text(encoding="utf-8-sig"))
        if isinstance(loaded_records, list):
            process_records = [item for item in loaded_records if isinstance(item, dict)]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    live_processes = [item for item in process_records if _pid_is_running(item.get("Pid"))]
    process_source = "state_file"
    if not live_processes:
        live_processes = _discover_native_host_processes()
        process_source = "process_table" if live_processes else "none"
    preferred = next(
        (item for item in extensions if item["installed"] and item["enabled"] and item.get("manifest_version") == 3),
        None,
    )
    installed = [item for item in extensions if item["installed"]]
    disabled = [item for item in installed if not item["enabled"]]
    state = (
        "ready"
        if preferred and live_processes
        else "enabled_host_offline"
        if preferred
        else "installed_disabled"
        if disabled
        else "not_installed"
    )
    return {
        "kind": "shizai_runtime_extension_status",
        "ready": bool(preferred and live_processes),
        "state": state,
        "interaction_required": state == "installed_disabled",
        "preferred_extension": preferred,
        "disabled_extensions": disabled,
        "extensions": extensions,
        "native_host": {
            "process_name": _NATIVE_HOST_PROCESS_NAME,
            "connected": bool(live_processes),
            "live_processes": live_processes,
            "state_file": str(process_file),
            "process_source": process_source,
        },
        "profile_root": str(profile_root),
    }


def _unwrap(result: Any) -> Any:
    """Return the data payload from the desktop RPC result envelope."""
    if isinstance(result, dict) and "data" in result and result.get("isSuccess") is not None:
        return result.get("data")
    return result


def _window_title(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    for item in detail.get("meta", []):
        if isinstance(item, dict) and item.get("key") in {"Name", "title"}:
            return str(item.get("value") or "").strip()
    return str(detail.get("elementAlias") or "").strip()


def _window_process(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    for item in detail.get("meta", []):
        if isinstance(item, dict) and item.get("key") == "ProcessId":
            return str(item.get("value") or "").strip()
    selector = detail.get("selector")
    if isinstance(selector, dict):
        common = selector.get("commonProps")
        if isinstance(common, dict):
            return str(common.get("processName") or "").strip()
    return ""


def _native_selector(selector: Any) -> dict[str, Any] | None:
    if isinstance(selector, dict):
        # DeepCapture may return an envelope with the actual native selector
        # serialized under ``selector``. Unwrap it before materialization.
        nested = selector.get("selector")
        if isinstance(nested, str) and nested.strip():
            try:
                value = json.loads(nested)
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
        return selector
    if not isinstance(selector, str) or not selector.strip():
        return None
    try:
        value = json.loads(selector)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _selector_context(selector_dict: dict[str, Any]) -> tuple[str, str]:
    """Extract page identity from either a tab selector or a UIA web chain."""
    title = ""
    url = ""
    entries = selector_dict.get("selector")
    if not isinstance(entries, list):
        return title, url
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_name = str(entry.get("name") or "").strip().lower()
        if entry_name not in {"tab", "window", "document"}:
            continue
        props = entry.get("props")
        if not isinstance(props, list):
            continue
        for prop in props:
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or "").strip().lower()
            value = prop.get("textValue") or prop.get("value") or ""
            value = str(value).strip().strip('"')
            if name in {"title", "name"} and value and not title:
                title = value
            elif name == "url" and value and not url:
                url = value
    return title, url


def _normalize_page_title(value: Any) -> str:
    """Normalize a browser title for identity comparison without losing detail."""
    text = " ".join(str(value or "").strip().casefold().split())
    for suffix in (" - google chrome", " - chrome", " | google chrome"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip()
    return text


def _page_titles_match(expected: Any, current: Any) -> bool:
    """Allow browser suffixes and short site-name captures while rejecting page changes."""
    left = _normalize_page_title(expected)
    right = _normalize_page_title(current)
    if not left or not right:
        return True
    if left == right or left in right or right in left:
        return True

    # Search/result pages commonly insert the query between stable title
    # phrases (for example ``京东商品搜索`` -> ``手机 - 商品搜索 - 京东``).
    # Compare CJK bigrams only as a conservative fallback: one shared site
    # name is insufficient, while two stable phrases preserve the identity
    # check without forcing a recapture for a harmless browser title change.
    cjk_left = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", left))
    cjk_right = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", right))
    if len(cjk_left) < 4 or len(cjk_right) < 4:
        return False
    left_grams = {cjk_left[index : index + 2] for index in range(len(cjk_left) - 1)}
    right_grams = {cjk_right[index : index + 2] for index in range(len(cjk_right) - 1)}
    shared = len(left_grams & right_grams)
    return shared >= 2 and shared / max(1, min(len(left_grams), len(right_grams))) >= 0.5


def _page_urls_match(expected: Any, current: Any) -> bool:
    """Compare known page URLs by origin/path and ignore query ordering noise."""
    left = str(expected or "").strip()
    right = str(current or "").strip()
    if not left or not right:
        return True
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
    except ValueError:
        return left == right
    if left_parts.scheme and right_parts.scheme and left_parts.netloc and right_parts.netloc:
        return (left_parts.scheme.casefold(), left_parts.netloc.casefold(), left_parts.path.rstrip("/")) == (
            right_parts.scheme.casefold(),
            right_parts.netloc.casefold(),
            right_parts.path.rstrip("/"),
        )
    return left == right


def _tree_text(nodes: Iterable[Any], *, limit: int = 80) -> list[str]:
    """Collect compact visible text from a bounded native tree for state detection."""
    values: list[str] = []
    seen: set[str] = set()

    def visit(items: Iterable[Any]) -> None:
        if len(values) >= limit:
            return
        for item in items:
            if len(values) >= limit or not isinstance(item, dict):
                return
            for value in _node_property_text(item):
                value = " ".join(value.split())[:240]
                if value and value not in seen:
                    seen.add(value)
                    values.append(value)
            children = item.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return values


def _page_state_markers(title: str, text: Iterable[str], url: str = "") -> tuple[bool, bool]:
    """Detect challenge/login pages from title and visible page text."""
    visible = list(text)
    haystack = " ".join([title, url, *visible]).casefold()
    challenge = any(marker.casefold() in haystack for marker in _CHALLENGE_MARKERS)
    title_url = f"{title} {url}".casefold()
    login_route = any(
        marker in title_url for marker in ("passport.", "/login", "/signin", "passport.jd.com", "账号登录", "账户登录")
    )
    login_form = any(
        marker.casefold() in haystack
        for marker in ("登录密码", "账号密码", "扫码登录", "短信登录", "手机号登录", "password", "username")
    )
    login_label = any(marker.casefold() in haystack for marker in _LOGIN_MARKERS)
    # A normal commerce homepage often contains an optional "请登录" link.
    # Treat it as a blocker only when the page identity or a login form proves
    # that automation has actually navigated into the authentication flow.
    login = login_route or (login_label and login_form)
    return challenge, login


def _waiting_user_interaction(reason: str, message: str, *, step_id: str | None = None) -> dict[str, Any]:
    action = "login" if reason == "login_required" else "complete_verification"
    user_prompt = (
        "当前页面需要登录。请在已经打开的窗口中完成登录，完成后告诉我“已登录”；"
        "如果希望我代为登录，请明确提出并在当前对话提供所需登录信息。登录后我会重新确认状态再继续。"
        if reason == "login_required"
        else "当前页面出现人机验证。请在已经打开的窗口中完成验证，完成后告诉我“已完成验证”，我会重新预检后继续。"
    )
    return {
        "required": True,
        "status": "waiting_for_user",
        "action": action,
        "pause_automation": True,
        "automation_may_login": False,
        "client_login_policy": {
            "default_action": "pause_and_ask_user",
            "may_delegate_login_only_if_all": [
                "user_explicitly_requests_delegated_login",
                "required_credentials_are_provided_in_current_conversation",
            ],
            "credential_scope": "current_interaction_only",
            "forbidden_destinations": ["workflow", "project", "element_library", "logs", "mcp_arguments"],
            "resume_requires": ["user_confirmation", "fresh_runtime_preflight"],
        },
        "delegated_login_policy": (
            "仅当用户在当前对话中明确要求代为登录并主动提供所需登录信息时，客户端模型才可执行登录；"
            "登录信息不得写入流程、项目、元素库、日志或 MCP 参数。"
        ),
        "message": message,
        "user_prompt": user_prompt,
        "resume": {
            "tool": "shizai_rpa_runtime_preflight",
            "arguments": {"step_id": step_id} if step_id else {},
            "rule": "收到用户明确确认后才恢复；恢复时先重新预检和扫描，不复用登录/验证前的 scan_token。",
        },
    }


def _node_control_names(node: dict[str, Any]) -> set[str]:
    """Return possible control names from native-tree or selector fields."""
    names: set[str] = set()
    for key in (
        "tag",
        "role",
        "controlType",
        "control_type",
        "localizedControlType",
        "localized_control_type",
        "elementType",
        "element_type",
        "name",
    ):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lower())
    alias = node.get("elementAlias")
    if isinstance(alias, str) and "_" in alias:
        names.add(alias.split("_", 1)[0].strip().lower())
    return names


def _node_property_map(node: dict[str, Any]) -> dict[str, list[str]]:
    """Return normalized property values from a tree node and its ``props``."""
    values: dict[str, list[str]] = {}

    def add(name: Any, value: Any) -> None:
        key = str(name or "").strip().casefold()
        if not key:
            return
        if isinstance(value, (str, int, float)):
            text = str(value).strip().strip('"')
            if text and text not in values.setdefault(key, []):
                values[key].append(text)

    for key in (
        "id",
        "class",
        "className",
        "name",
        "placeholder",
        "title",
        "href",
        "role",
        "aria-label",
        "ariaLabel",
        "automationid",
        "automation_id",
    ):
        add(key, node.get(key))
    props = node.get("props")
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            name = prop.get("name")
            value = prop.get("textValue") or prop.get("value") or prop.get("expressionValue")
            add(name, value)
    return values


def _node_is_editable(node: dict[str, Any]) -> bool:
    """Recognize editable webpage controls across English and localized UIA trees."""
    names = _node_control_names(node)
    tag = _node_tag(node).casefold()
    return bool(names & {"edit", "input", "textarea", "textbox", "combobox", "编辑", "文本框", "输入框"}) or tag in {
        "edit",
        "input",
        "textarea",
        "textbox",
        "combobox",
        "编辑",
        "文本框",
        "输入框",
    }


def _node_selector_hint(node: dict[str, Any]) -> dict[str, Any]:
    """Build compact hints from ``props`` without pretending they are selectors."""
    properties = _node_property_map(node)

    def first(*keys: str) -> str | None:
        for key in keys:
            found = properties.get(key.casefold())
            if found:
                return found[0]
        return None

    return {
        "id": first("id") or node.get("id"),
        "class": first("class", "classname"),
        "name": first("name") or node.get("name"),
        "placeholder": first("placeholder"),
        "aria_label": first("aria-label", "arialabel", "aria_label"),
        "automation_id": first("automationid", "automation_id"),
        "title": first("title"),
        "href": first("href"),
    }


def _query_semantic_fields(query: str) -> tuple[set[str], str]:
    """Resolve a natural-language scan query to canonical semantic fields."""
    normalized = " ".join(str(query or "").strip().casefold().split())
    if not normalized:
        return set(), ""
    if normalized in {item.casefold() for item in _SEARCH_BOX_QUERY_ALIASES}:
        return {"search"}, "search_box"
    if normalized in {item.casefold() for item in _SUBMIT_QUERY_ALIASES}:
        return {"submit"}, "submit"
    fields = {
        field
        for field, keywords in _SEMANTIC_FIELDS.items()
        if any(keyword.casefold() in normalized for keyword in keywords if keyword)
    }
    return fields, "semantic" if fields else "text"


def _detail_control_names(detail: Any) -> set[str]:
    """Return control names exposed by a DeepCapture detail response."""
    if not isinstance(detail, dict):
        return set()
    names: set[str] = set()
    for item in detail.get("meta", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        if key not in {"controltype", "localizedcontroltype", "control_type", "localized_control_type"}:
            continue
        value = item.get("value")
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lower())
    alias = detail.get("elementAlias")
    if isinstance(alias, str) and alias.strip():
        names.update(part.strip().lower() for part in alias.split("_") if part.strip())
    return names


def _node_property_text(node: dict[str, Any]) -> list[str]:
    """Collect short visible/property text from one DeepCapture tree node."""
    values: list[str] = []
    for key in (
        "name",
        "text",
        "value",
        "innerText",
        "inner_text",
        "title",
        "ariaLabel",
        "aria_label",
        "placeholder",
        "elementAlias",
    ):
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            values.append(str(value).strip())
    props = node.get("props")
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            for key in ("textValue", "value", "expressionValue"):
                value = prop.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    text = str(value).strip().strip('"')
                    if text:
                        values.append(text)
    # Keep the scan response compact. The full native selector is obtained
    # only after the model selects an element id.
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value[:240]
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique[:12]


def _node_label(node: dict[str, Any]) -> str:
    """Prefer meaningful accessible properties over generic control names."""
    values = _node_property_text(node)
    if not values:
        return ""
    generic = {
        "window",
        "pane",
        "document",
        "group",
        "text",
        "static",
        "label",
        "image",
        "hyperlink",
        "link",
        "button",
        "edit",
        "input",
        "textarea",
        "div",
        "span",
        "窗口",
        "窗格",
        "文档",
        "组",
        "文本",
        "静态文本",
        "标签",
        "图像",
        "超链接",
        "链接",
        "按钮",
        "编辑",
        "输入框",
        "文本框",
    }
    for value in values:
        if value.casefold() not in generic:
            return value
    return values[0]


def _node_tag(node: dict[str, Any]) -> str:
    for key in ("tag", "role", "controlType", "control_type", "localizedControlType", "elementType"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _node_is_page_candidate(node: dict[str, Any], *, document_seen: bool) -> bool:
    """Recognize webpage controls while excluding the Chrome shell."""
    if not document_seen or not str(node.get("elementId") or "").strip():
        return False
    names = _node_control_names(node)
    if names & _SHELL_CONTROL_NAMES:
        return False
    node_type = str(node.get("type") or "").strip().lower()
    tag = _node_tag(node).lower()
    return node_type == "web" or tag in _UIA_WEB_CONTROL_NAMES or bool(_node_property_text(node))


_PRODUCT_TITLE_NOISE = {
    "购物指南",
    "配送方式",
    "支付方式",
    "售后服务",
    "特色服务",
    "关于我们",
    "联系我们",
    "联系客服",
    "商家帮助",
    "经营证照",
    "隐私政策",
    "销售联盟",
    "价格保护",
    "价格区间",
    "全部商品",
    "友情链接",
    "营业执照",
    "自营",
    "自营/旗舰",
    "旗舰",
    "店铺",
    "商家",
    "销量",
    "价格",
}
_PRODUCT_BADGE_MARKERS = (
    "热卖榜",
    "新品榜",
    "金榜",
    "榜第",
    "达人浏览",
    "粉丝浏览",
    "预约",
    "经营资质",
    "医疗器械",
    "服务",
    "帮助",
    "政策",
    "资质",
)
_MERCHANT_NOISE_MARKERS = (
    "商家帮助",
    "经营资质",
    "医疗器械",
    "粉丝浏览",
    "达人浏览",
    "热卖榜",
    "新品榜",
    "金榜",
    "预约",
    "店铺被",
    "万人种草",
    "种草",
    "好评",
    "口碑",
    "浏览",
    "评价",
    "反应速度",
)
_MERCHANT_LABEL_NOISE = {"商家", "店铺", "店名", "卖家", "merchant", "seller", "shop", "store"}
_MERCHANT_BADGE_NOISE = {"自营", "自营/旗舰", "旗舰", "店铺", "商家"}
_MERCHANT_BADGE_NOISE_FOLDED = {item.casefold() for item in _MERCHANT_BADGE_NOISE}
_LINK_NOISE_MARKERS = (
    "客服",
    "帮助",
    "隐私",
    "政策",
    "证照",
    "许可证",
    "举报",
    "关于我们",
    "友情链接",
    "销售联盟",
    "经营资质",
    "登录",
    "注册",
    "customer_service",
    "customer-service",
    "customerservice",
    "support",
    "contact_us",
    "contact-us",
)
_CARD_CLASS_MARKERS = (
    "plugingoodscardwrapper",
    "goods-card",
    "goodscontainer",
    "gl-item",
    "productcard",
    "product-item",
    "productitem",
    "goodsitem",
    "searchitem",
    "skuitem",
    "card",
)
_CAPTURE_FIELD_NAMES = ("product_name", "merchant", "price", "sales", "link")
_CAPTURE_FIELD_ALIASES = {
    "商品名称": "product_name",
    "商品名": "product_name",
    "商家名称": "merchant",
    "商家": "merchant",
    "店铺": "merchant",
    "价格": "price",
    "销量": "sales",
    "商品链接": "link",
    "链接": "link",
}

# A live JD page exposes product-card ancestry only after the candidate is
# captured.  Keep the public candidate_limit small, but probe a larger bounded
# window on real pages so fields can be aligned by the same card instance.
_LIVE_FIELD_PROBE_LIMIT = 12
_LIVE_FIELD_EXPANSION_LIMIT = 64


def _node_card_context(
    node: dict[str, Any],
    inherited: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Carry a product-card hint from a tree ancestor to its descendants."""
    properties = _node_property_map(node)
    classes = " ".join(properties.get("class", []))
    folded_classes = classes.casefold().replace("_", "")
    marker = next((item for item in _CARD_CLASS_MARKERS if item in folded_classes), None)
    if not marker:
        return inherited
    index = next((item for item in properties.get("@index", []) if item != ""), "")
    normalized_class = " ".join(classes.split())
    family_key = normalized_class or marker
    instance_key = f"{family_key}|@index={index or 'unknown'}"
    return {
        # Keep the historical strict key while exposing a stable container
        # family for pages whose fields live in separate UIA subtrees.
        "key": instance_key,
        "instance_key": instance_key,
        "family_key": family_key,
        "marker": marker,
        "class": normalized_class,
        "index": index or None,
        "source": "tree_hint",
    }


def _is_product_title_candidate(
    text: str,
    *,
    tag: str,
    card_context: dict[str, Any] | None,
) -> bool:
    """Recognize card titles while rejecting badges and page controls."""
    normalized = " ".join(str(text or "").split())
    if normalized in _PRODUCT_TITLE_NOISE or len(normalized) < 4:
        return False
    if any(marker in normalized for marker in _PRODUCT_BADGE_MARKERS):
        return False
    normalized_tag = str(tag or "").casefold()
    if normalized_tag not in {"text", "static", "label", "文本", "静态文本", "标签", "标题"}:
        return False
    # A card ancestor is the reliable signal on a real result page. For
    # generic fixtures, retain the older mobile-keyword heuristic below.
    return bool(card_context) or any(token in normalized for token in ("手机", "旗舰", "型号"))


def _semantic_matches(
    text: str,
    *,
    tag: str = "",
    path: Iterable[str] = (),
    label: str | None = None,
    properties: dict[str, list[str]] | None = None,
    card_context: dict[str, Any] | None = None,
) -> tuple[dict[str, int], int]:
    """Score a visible node against semantic fields and common page shapes."""
    label_text = str(label if label is not None else text).strip()
    label_folded = label_text.casefold()
    property_values = properties or {}
    scores: dict[str, int] = {}
    for field, keywords in _SEMANTIC_FIELDS.items():
        normalized_tag = str(tag or "").casefold()
        # JD exposes its self-operated badge as an image named ``自营``. Keep
        # the semantic hint so callers can explain the available fallback,
        # while the merchant candidate selector still rejects the badge as a
        # field and prefers actual store text.
        if field == "merchant" and normalized_tag in {"image", "img", "图像"}:
            badge_text = label_folded.strip(" \\\"'")
            if badge_text in _MERCHANT_BADGE_NOISE_FOLDED:
                scores["merchant"] = 1
            continue
        score = 0
        for keyword in keywords:
            token = keyword.casefold()
            if not token:
                continue
            # Semantic labels must come from the node's visible text or
            # properties, not from the synthetic accessibility path.  The
            # old behaviour treated every `标题` node as a product name.
            if token not in label_folded and not any(
                token in str(value).casefold() for values in property_values.values() for value in values
            ):
                continue
            if field == "merchant" and any(marker in label_text for marker in _MERCHANT_NOISE_MARKERS):
                continue
            if field == "product_name" and token in {"标题", "名称", "title", "product", "goods"}:
                if len(label_text) < 8 or label_text in _PRODUCT_TITLE_NOISE:
                    continue
            if field == "price" and not re.search(r"\d", label_text):
                continue
            if field == "link" and label_folded and any(marker in label_folded for marker in _LINK_NOISE_MARKERS):
                continue
            score += 5 if token in {"商品名称", "商家", "店铺", "价格", "销量", "已售", "search"} else 2
        if score:
            scores[field] = score
    normalized_tag = str(tag or "").casefold()
    shape_label = label_text
    title_noise = shape_label in _PRODUCT_TITLE_NOISE or any(
        item in shape_label for item in ("Copyright", "许可证", "备案", "举报电话")
    )
    # Product titles often have no aria label or literal "商品名称" marker.
    # On a Document subtree, a long visible text node below the result-card
    # level is a stronger title signal than the generic text control type.
    if (
        _is_product_title_candidate(shape_label, tag=normalized_tag, card_context=card_context)
        and len(tuple(path)) >= 5
    ):
        scores["product_name"] = max(
            scores.get("product_name", 0),
            8 if len(shape_label) >= 8 else 4,
        )
    # Merchant labels are commonly short links or badges, so use store-name
    # suffixes while excluding the navigation link "商家帮助".
    if (
        normalized_tag not in {"image", "img", "图像"}
        and not title_noise
        and any(token in shape_label for token in ("旗舰店", "专卖店", "专营店", "自营", "官方店"))
    ):
        scores["merchant"] = max(
            scores.get("merchant", 0),
            8
            if (
                len(shape_label) >= 4
                or "自营" in shape_label
                or any(token in shape_label for token in ("旗舰店", "专卖店", "专营店", "官方店"))
            )
            else 4,
        )
    if normalized_tag not in {"image", "img", "图像"} and (
        shape_label.casefold() in _MERCHANT_LABEL_NOISE or shape_label.casefold() in _MERCHANT_BADGE_NOISE_FOLDED
    ):
        scores.pop("merchant", None)
    # Prices are frequently exposed as a separate numeric text node next to a
    # currency symbol. Keep 3+ digit values searchable even when the symbol is
    # represented by a sibling node.
    if normalized_tag in {"text", "static", "label", "文本", "静态文本", "标签"} and re.fullmatch(
        r"\s*\d{3,}(?:\.\d{1,2})?\s*", shape_label
    ):
        scores["price"] = max(scores.get("price", 0), 3)
    if normalized_tag in {"hyperlink", "link", "a", "超链接", "链接"}:
        has_href = any(str(value).strip() for value in property_values.get("href", []))
        if not any(marker in shape_label.casefold() for marker in _LINK_NOISE_MARKERS):
            scores["link"] = max(scores.get("link", 0), 8 if has_href else 4)
    # ``销量`` is a page heading, while ``已售9000+`` is the value inside a
    # product card. Prefer the latter when both are present.
    if any(token in shape_label for token in ("已售", "成交", "月销")) and re.search(r"\d", shape_label):
        scores["sales"] = max(scores.get("sales", 0), 8)
    return scores, max(scores.values(), default=0)


def _scan_tree_nodes(
    nodes: Iterable[Any],
    *,
    query: str,
    max_nodes: int,
    document_seen: bool = True,
    path: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Flatten a Document subtree into model-friendly, selector-free candidates."""
    result: list[dict[str, Any]] = []
    query_text = str(query or "").strip().casefold()
    query_fields, query_kind = _query_semantic_fields(query)
    exact_semantic_alias = query_text in {
        keyword.casefold() for keywords in _SEMANTIC_FIELDS.values() for keyword in keywords if keyword
    }
    seen: set[str] = set()
    scan_order = 0

    def visit(
        items: Iterable[Any],
        inherited_document: bool,
        parent_path: tuple[str, ...],
        inherited_card: dict[str, Any] | None = None,
    ) -> None:
        nonlocal scan_order
        if len(result) >= max_nodes:
            return
        for item in items:
            if len(result) >= max_nodes or not isinstance(item, dict):
                return
            scan_order += 1
            element_id = str(item.get("elementId") or "").strip()
            names = _node_control_names(item)
            current_document = inherited_document or bool(names & {"document", "文档"})
            current_card = _node_card_context(item, inherited_card)
            label = _node_label(item)
            tag = _node_tag(item)
            path_label = tag or str(item.get("name") or "node")
            current_path = parent_path + (path_label[:100],)
            values = _node_property_text(item)
            properties = _node_property_map(item)
            searchable = " ".join(values + [tag])
            semantic, score = _semantic_matches(
                searchable,
                tag=tag,
                path=current_path,
                label=label,
                properties=properties,
                card_context=current_card,
            )
            editable = _node_is_editable(item)
            search_input = editable and (
                any(token in searchable.casefold() for token in ("搜索", "search", "keyword", "query", "检索"))
                or query_kind == "search_box"
            )
            if search_input:
                semantic.pop("submit", None)
                semantic["search"] = max(semantic.get("search", 0), 8)
                score = max(score, 8)
            direct_query_match = bool(query_text and query_text in searchable.casefold())
            if direct_query_match:
                score += 6
            if query_kind == "search_box" and not editable:
                include = False
            elif query_kind == "submit" and not (
                str(tag).casefold() in {"button", "按钮"} or "submit" in searchable.casefold()
            ):
                include = False
            elif query_fields:
                include = bool(set(semantic) & query_fields) or (bool(direct_query_match) and not exact_semantic_alias)
            else:
                # An empty query is the intentional full-page discovery mode
                # used by atomic field capture.  A non-empty free-text query
                # remains a strict label/property match.
                include = True if not query_text else direct_query_match
            if (
                element_id
                and element_id not in seen
                and _node_is_page_candidate(item, document_seen=current_document)
                and (not query_text or include)
            ):
                seen.add(element_id)
                result.append(
                    {
                        "element_id": element_id,
                        "text": label,
                        "values": values,
                        "tag": tag,
                        "role": str(item.get("role") or item.get("localizedControlType") or tag),
                        "level": len(current_path),
                        "path": list(current_path),
                        "semantic_fields": sorted(semantic, key=lambda key: (-semantic[key], key)),
                        "semantic_score": score,
                        "selector_hint": _node_selector_hint(item),
                        "card_context": current_card,
                        "is_editable": editable,
                        "is_search_input": search_input,
                        "scan_order": scan_order,
                    }
                )
            children = item.get("children")
            if isinstance(children, list):
                visit(children, current_document, current_path, current_card)

    visit(nodes, document_seen, path)
    result.sort(key=lambda item: (-int(item.get("semantic_score", 0)), int(item.get("scan_order", 0))))
    return result


def _find_document_ids(nodes: Iterable[Any]) -> list[str]:
    """Find Document ids in a previously captured Chrome window tree."""
    found: list[str] = []
    seen: set[str] = set()

    def visit(items: Iterable[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("elementId") or "").strip()
            if element_id and _node_control_names(item) & {"document", "文档"} and element_id not in seen:
                seen.add(element_id)
                found.append(element_id)
            children = item.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return found


def _tree_has_web_element(
    nodes: Iterable[Any],
    _seen: set[tuple[int, bool]] | None = None,
    _document_seen: bool = False,
) -> bool:
    """Return whether a DeepCapture tree contains an actual web element.

    Chrome's native accessibility shell is returned before the extension's
    inspect mode has produced a peer element.  Treating that shell as a
    captured input would create a selector that cannot be used by a web RPA
    component.
    """
    seen = _seen if _seen is not None else set()
    document_seen = _document_seen
    for node in nodes:
        if not isinstance(node, dict):
            continue
        identity = id(node)
        control_names = _node_control_names(node)
        node_document_seen = document_seen or bool(control_names & {"document", "文档"})
        # Some extension versions flatten a UIA chain into one list instead
        # of nesting each parent under ``children``. Preserve the ordered
        # Document marker for that representation as well.
        document_seen = node_document_seen
        state = (identity, node_document_seen)
        if state in seen:
            continue
        seen.add(state)
        node_type = str(node.get("type") or "").strip().lower()
        tag = str(node.get("tag") or "").strip().lower()
        if node_type == "web" or tag in {
            "a",
            "button",
            "div",
            "form",
            "iframe",
            "img",
            "input",
            "li",
            "option",
            "select",
            "span",
            "table",
            "textarea",
        }:
            return True
        # DeepCapture's native Chrome selector is a UIA chain rather than an
        # HTML chain.  Window/Pane alone is only the Chrome shell; Document
        # followed by a control proves that the page provider was reached.
        if node_document_seen and control_names & _UIA_WEB_CONTROL_NAMES:
            return True
        children = node.get("children")
        if isinstance(children, list) and _tree_has_web_element(children, seen, node_document_seen):
            return True
    return False


def _tree_message(nodes: list[dict[str, Any]], *, document_seen: bool = False) -> tuple[bool, str]:
    """Describe whether a tree is ready for element import."""
    if not nodes:
        return False, "当前没有网页元素；请确认 Chrome 页面已加载，然后启动深度拾取并点击或悬停目标元素。"
    if not _tree_has_web_element(nodes, _document_seen=document_seen):
        return (
            False,
            "当前只读到 Chrome 原生窗口外壳，尚未产生网页元素；请启动 startDeepCaptureInspect(2)，再在 Chrome 页面点击目标元素。",
        )
    return True, "已读取网页元素，可调用 shizai_rpa_capture_element(s) 获取原生 selector。"


def _selector_value(prop: dict[str, Any]) -> str:
    """Read a DeepCapture selector property in both native value formats."""
    value = prop.get("textValue")
    if value in (None, ""):
        value = prop.get("value")
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return text.strip('"')
        return str(decoded or "").strip()
    return text


def _selector_card_context(selector: dict[str, Any]) -> dict[str, Any] | None:
    """Return the nearest recognizable product-card marker in a web chain."""
    entries = selector.get("selector")
    if not isinstance(entries, list):
        return None
    for depth, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        props = entry.get("props")
        if not isinstance(props, list):
            continue
        values: dict[str, list[str]] = {}
        for raw_prop in props:
            if not isinstance(raw_prop, dict):
                continue
            name = str(raw_prop.get("name") or "").strip().casefold()
            if name:
                values.setdefault(name, []).append(_selector_value(raw_prop))
        classes = " ".join(values.get("class", []))
        folded_classes = classes.casefold().replace("_", "")
        marker = next((item for item in _CARD_CLASS_MARKERS if item in folded_classes), None)
        if not marker:
            continue
        index = next((item for item in values.get("@index", []) if item != ""), "")
        normalized_class = " ".join(classes.split())
        family_key = normalized_class or marker
        instance_key = f"{family_key}|@index={index or 'unknown'}"
        return {
            "key": instance_key,
            "instance_key": instance_key,
            "family_key": family_key,
            "marker": marker,
            "class": normalized_class,
            "index": index or None,
            "selector_depth": depth,
        }
    return None


def _selector_link_context(selector: dict[str, Any]) -> dict[str, Any]:
    """Describe link semantics so a customer-service link cannot be selected as a product URL."""
    entries = selector.get("selector")
    css = (
        selector.get("commonProps", {}).get("cssSelector", "") if isinstance(selector.get("commonProps"), dict) else ""
    )
    css = str(css or "").strip().strip('"')
    class_text = ""
    href = ""
    alias_text = ""
    link_tag = False
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip().casefold()
            link_tag = link_tag or name in {"a", "link", "hyperlink", "超链接", "链接"}
            for raw_prop in entry.get("props", []):
                if not isinstance(raw_prop, dict):
                    continue
                prop_name = str(raw_prop.get("name") or "").strip().casefold()
                value = _selector_value(raw_prop)
                if prop_name == "class":
                    class_text = f"{class_text} {value}".strip()
                elif prop_name == "href" and value:
                    href = value
    element_detail = selector.get("elementDetail")
    if isinstance(element_detail, dict):
        alias_text = " ".join(
            str(element_detail.get(key) or "").strip()
            for key in ("elementName", "elementAlias", "desc")
            if str(element_detail.get(key) or "").strip()
        )
    common = selector.get("commonProps")
    if isinstance(common, dict):
        alias_text = f"{alias_text} {common.get('desc') or ''}".strip()
    haystack = f"{css} {class_text} {href} {alias_text}".casefold()
    service_link = any(marker.casefold() in haystack for marker in _LINK_NOISE_MARKERS)
    return {
        "is_link": link_tag or bool(re.match(r"^a(?:[.#\[]|$)", css, re.IGNORECASE)),
        "has_href": bool(href),
        "href": href,
        "service_or_navigation": service_link,
        "css_selector": css,
    }


def _candidate_card_key(candidate: dict[str, Any]) -> str:
    """Return a stable grouping key when scan hints already expose a card."""
    context = candidate.get("card_context")
    if not isinstance(context, dict):
        return ""
    return str(context.get("key") or "").strip()


def _candidate_card_family_key(candidate: dict[str, Any]) -> str:
    """Return the stable product-container family from a scan hint."""
    context = candidate.get("card_context")
    if not isinstance(context, dict):
        return ""
    return str(
        context.get("family_key") or context.get("class") or context.get("marker") or context.get("key") or ""
    ).strip()


def _candidate_group_key(candidate: dict[str, Any], grouping: str) -> str:
    """Choose strict card-instance or relaxed card-family correlation."""
    return _candidate_card_family_key(candidate) if grouping == "family" else _candidate_card_key(candidate)


def _candidate_is_link_noise(candidate: dict[str, Any]) -> bool:
    """Detect navigation/customer-service candidates before native capture."""
    values = [candidate.get("text"), *(candidate.get("values") or [])]
    hint = candidate.get("selector_hint")
    if isinstance(hint, dict):
        values.extend(hint.get(key) for key in ("name", "class", "aria_label", "title", "href"))
    haystack = " ".join(str(value or "") for value in values).casefold()
    return any(marker.casefold() in haystack for marker in _LINK_NOISE_MARKERS)


def _candidate_is_merchant_badge(candidate: dict[str, Any]) -> bool:
    """Reject JD fulfillment badges when selecting a store-name field."""
    tag = str(candidate.get("tag") or candidate.get("role") or "").casefold()
    values = [candidate.get("text"), *(candidate.get("values") or [])]
    hint = candidate.get("selector_hint")
    if isinstance(hint, dict):
        values.extend(hint.get(key) for key in ("name", "class", "aria_label", "title"))
    label = " ".join(str(value or "").strip() for value in values).casefold()
    if tag in {"image", "img", "图像"}:
        return True
    if any(label.startswith(prefix) for prefix in ("image_", "图像_", "img_")):
        return True
    normalized = " ".join(label.split())
    return normalized in _MERCHANT_BADGE_NOISE_FOLDED


def _record_is_merchant_badge(record: dict[str, Any]) -> bool:
    """Apply the merchant-badge rule again after native selector capture."""
    name = str(record.get("name") or "").strip().casefold()
    if name.startswith(("image_", "图像_", "img_")):
        return True
    selector = record.get("selector")
    entries = selector.get("selector") if isinstance(selector, dict) else None
    if isinstance(entries, list) and entries:
        leaf = entries[-1]
        if isinstance(leaf, dict) and str(leaf.get("name") or "").strip().casefold() in {"image", "img", "图像"}:
            return True
    return name in _MERCHANT_BADGE_NOISE_FOLDED


def _element_record(
    element_id: str,
    detail: Any,
    selector: Any,
    *,
    page_title: str | None = None,
    page_url: str | None = None,
) -> dict[str, Any]:
    selector_dict = _native_selector(selector) or {}
    detail_dict = detail if isinstance(detail, dict) else {}
    element_detail = selector_dict.get("elementDetail")
    element_detail = element_detail if isinstance(element_detail, dict) else {}
    selector_title, selector_url = _selector_context(selector_dict)
    title = str(page_title or selector_title or element_detail.get("categoryName") or "").strip()
    if not title:
        title = _window_title(detail_dict)
    resolved_url = str(page_url or selector_url or "").strip()
    common = selector_dict.get("commonProps")
    common = common if isinstance(common, dict) else {}
    process_name = str(common.get("processName") or "chrome.exe").strip()
    gui_type = str(common.get("GUIType") or "Chrome").strip()
    if gui_type.lower() in {"windows", "win32"}:
        entries = selector_dict.get("selector")
        is_chrome_process = process_name.lower() in {"chrome.exe", "chrome"}
        has_web_chain = isinstance(entries, list) and (
            any(str(item.get("type") or "").lower() == "web" for item in entries if isinstance(item, dict))
            or _tree_has_web_element(entries)
        )
        if is_chrome_process or has_web_chain:
            gui_type = "Chrome"
            common = dict(common)
            common["GUIType"] = gui_type
            selector_dict["commonProps"] = common
    card_context = _selector_card_context(selector_dict)
    link_context = _selector_link_context(selector_dict)
    return {
        "id": element_id,
        "name": str(detail_dict.get("elementAlias") or element_detail.get("elementName") or element_id),
        "GUIType": gui_type,
        "categoryName": title,
        "modelType": "Element",
        "selector": selector_dict,
        "selectorType": "comboSelector",
        "selectorParams": {"elementTypes": ["normal", "cv"], "comboSelectType": 0, "isAutoSimilar": 0},
        "selectorVariableNames": [],
        "parentId": None,
        "page_title": title,
        "page_url": resolved_url,
        "process_name": process_name,
        "card_context": card_context,
        "link_context": link_context,
    }


def _enum_windows() -> Iterable[tuple[int, str]]:
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    visible: list[tuple[int, str]] = []

    def process_name(pid: int) -> str:
        """Read one process image name without spawning tasklist per window."""
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                return ""
            return Path(image.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_proc_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = process_name(int(pid.value))
        if process == "chrome.exe":
            visible.append((int(hwnd), title.value))
        return True

    user32.EnumWindows(callback, 0)
    return visible


def _chrome_executable_candidates() -> list[Path]:
    """Return Chrome installation candidates without depending on PATH.

    The desktop app may start the MCP process with a reduced environment, so
    ``ProgramFiles(x86)`` and PATH are not sufficient on their own. Registry
    App Paths is the Windows-supported source of truth; common absolute paths
    cover portable and partially registered installations.
    """
    candidates: list[Path] = []

    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(name)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")

    if os.name == "nt":
        candidates.extend(
            [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            ]
        )
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for subkey in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ):
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                    except OSError:
                        continue
                    if value:
                        candidates.append(Path(str(value).strip().strip('"')))
        except (ImportError, OSError):
            pass

    which = shutil.which("chrome.exe") or shutil.which("chrome")
    if which:
        candidates.insert(0, Path(which))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _find_chrome_executable() -> Path | None:
    """Find an existing Chrome executable using the local installation map."""
    return next((candidate for candidate in _chrome_executable_candidates() if candidate.is_file()), None)


def _detached_browser_popen_kwargs() -> dict[str, Any]:
    """Keep a design-time browser alive when the short-lived MCP client exits."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return kwargs


class ShizaiExtensionBridge:
    """Use the installed Shizai Chrome extension through desktop RPC."""

    def __init__(
        self,
        desktop: ShizaiDesktopAgent | None = None,
        *,
        capture_rpc_timeout: float = DEFAULT_CAPTURE_RPC_TIMEOUT,
        capture_operation_timeout: float = DEFAULT_CAPTURE_OPERATION_TIMEOUT,
    ) -> None:
        self.desktop = desktop or ShizaiDesktopAgent()
        self.capture_rpc_timeout = max(0.1, float(capture_rpc_timeout))
        self.capture_operation_timeout = max(0.5, float(capture_operation_timeout))
        self._operation_deadline: float | None = None

    def _remaining_timeout(self) -> float | None:
        if self._operation_deadline is None:
            return None
        return self._operation_deadline - time.monotonic()

    def _capture_desktop(self, timeout: float | None = None) -> Any:
        """Use a short-lived desktop client for DeepCapture calls only."""
        requested = self.capture_rpc_timeout if timeout is None else float(timeout)
        remaining = self._remaining_timeout()
        if remaining is not None:
            requested = min(requested, remaining)
        if requested <= 0:
            raise ShizaiCloudError(
                "DeepCapture 通道无响应；reason=channel_unavailable；"
                "元素读取已达到总时限，请重新执行 workflow rpa-browser-session-start。"
            )
        config = getattr(self.desktop, "config", None)
        if not isinstance(config, ShizaiCloudConfig):
            return self.desktop
        timeout_value = min(max(0.1, requested), max(0.1, float(config.timeout)))
        if timeout_value >= float(config.timeout):
            return self.desktop
        return ShizaiDesktopAgent(
            replace(config, timeout=timeout_value),
            connection_factory=getattr(self.desktop, "connection_factory", None),
        )

    def _capture_rpc_call(self, method: str, args: list[Any] | None = None, *, timeout: float | None = None) -> Any:
        """Call DeepCapture with a transport timeout independent of cloud defaults."""
        try:
            return self._capture_desktop(timeout).call(DEEP_CAPTURE_SERVICE, method, args or [])
        except (ShizaiCloudError, OSError, TimeoutError) as exc:
            if self._is_transient_capture_error(exc):
                raise ShizaiCloudError(
                    f"DeepCapture 通道无响应；reason=channel_unavailable；method={method}；"
                    "请确认实在 Agent 和 Chrome 扩展已连接，然后重新执行 "
                    "workflow rpa-browser-session-start。"
                ) from exc
            raise

    def status(self, *, timeout: float | None = None) -> dict[str, Any]:
        installation = _extension_installation()
        _read_manifest(installation)
        try:
            auth = self._capture_desktop(timeout).authentication_status()
        except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
            auth = {"ready": False, "error": str(exc)}
        status = {
            "backend": "shizai-agent-native-extension",
            "installation": installation,
            "desktop": auth,
            "deep_capture_rpc": bool(auth.get("ready")),
            "uses_current_chrome_profile": True,
            "requires_separate_codex_extension": False,
            "runtime_extension": _runtime_extension_status(),
            "next_action": "shizai_rpa_capture_windows" if auth.get("ready") else "shizai_auth_status",
            "ui_feedback": _capture_ui_feedback(),
        }
        return status

    def runtime_extension_status(self) -> dict[str, Any]:
        """Read Chrome runtime-extension state without opening or controlling Chrome."""
        return _runtime_extension_status()

    def activate_runtime_extension(self, *, wait_seconds: float = 4.0) -> dict[str, Any]:
        """Ensure the enabled MV3 extension has a live Native Messaging host."""
        before = _runtime_extension_status()
        if before.get("ready"):
            return {"attempted": False, "ready": True, "before": before, "after": before}
        extension = before.get("preferred_extension") or {}
        extension_id = str(extension.get("id") or "").strip()
        popup = str(extension.get("popup") or "pages/popup.html").lstrip("/")
        if not extension_id:
            disabled = before.get("disabled_extensions") or []
            disabled_extension = disabled[0] if disabled and isinstance(disabled[0], dict) else {}
            disabled_id = str(disabled_extension.get("id") or "").strip()
            if disabled_id:
                return {
                    "attempted": False,
                    "ready": False,
                    "interaction_required": True,
                    "before": before,
                    "after": before,
                    "activation_url": f"chrome://extensions/?id={disabled_id}",
                    "message": ("实在 Agent Chrome 插件已安装但被 Chrome 禁用；请在扩展管理页启用后重试。"),
                }
            return {
                "attempted": False,
                "ready": False,
                "before": before,
                "after": before,
                "message": "未找到已启用的实在 Agent MV3 Chrome 插件。",
            }
        executable = _find_chrome_executable()
        if executable is None:
            raise ShizaiCloudError("未找到 Google Chrome，不能激活实在 Agent 运行插件。")
        activation_url = f"chrome-extension://{extension_id}/{popup}"
        subprocess.Popen([str(executable), activation_url], **_detached_browser_popen_kwargs())
        deadline = time.monotonic() + max(0.5, min(float(wait_seconds), 10.0))
        after = before
        while time.monotonic() < deadline:
            time.sleep(0.2)
            after = _runtime_extension_status()
            if after.get("ready"):
                break
        return {
            "attempted": True,
            "ready": bool(after.get("ready")),
            "activation_url": activation_url,
            "before": before,
            "after": after,
            "message": (
                "实在 Agent Chrome 运行插件已激活。"
                if after.get("ready")
                else "已触发实在 Agent Chrome 插件，但 Native Messaging Host 尚未上线。"
            ),
        }

    def ui_status(self) -> dict[str, Any]:
        """Return the last native-capture phase for clients that can poll it."""
        return {
            "kind": "shizai_rpa_ui_feedback",
            "ui_feedback": _capture_ui_feedback(),
            "status": self.status(timeout=self._remaining_timeout()),
        }

    def call(
        self,
        method: str,
        args: list[Any] | None = None,
        *,
        allow_write: bool = False,
        timeout: float | None = None,
    ) -> Any:
        method = str(method).strip()
        if method not in _READ_ONLY_METHODS | _CONTROL_METHODS:
            raise ValueError(
                "extension method is not in the supported capture surface; "
                "use DeepCaptureService methods or add an explicit adapter"
            )
        if method in _CONTROL_METHODS and not allow_write:
            raise PermissionError(f"extension call {method} requires allow_write: true")
        return self._capture_rpc_call(method, args, timeout=timeout)

    @staticmethod
    def _is_transient_capture_error(exc: BaseException) -> bool:
        detail = str(exc).casefold()
        return any(marker.casefold() in detail for marker in _TRANSIENT_CAPTURE_ERROR_MARKERS)

    def _call_control_with_retry(
        self,
        method: str,
        args: list[Any],
        *,
        max_attempts: int = DEFAULT_CAPTURE_READ_ATTEMPTS,
    ) -> tuple[Any, int]:
        """Retry only known transient native-channel failures."""
        attempts = max(1, int(max_attempts))
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                remaining = self._remaining_timeout()
                return self.call(
                    method,
                    args,
                    allow_write=True,
                    timeout=min(self.capture_rpc_timeout, remaining)
                    if remaining is not None
                    else self.capture_rpc_timeout,
                ), attempt
            except (ShizaiCloudError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt >= attempts or not self._is_transient_capture_error(exc):
                    raise
                delay = 0.25 * attempt
                remaining = self._remaining_timeout()
                if remaining is not None:
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _call_read_with_retry(
        self,
        method: str,
        args: list[Any],
        *,
        max_attempts: int = DEFAULT_CAPTURE_READ_ATTEMPTS,
    ) -> tuple[Any, int]:
        """Retry only transient failures on read-only DeepCapture RPCs.

        Page discovery is idempotent, so a short bounded retry absorbs the
        native host's reconnect window without repeating the whole workflow.
        The caller receives the attempt count for truthful telemetry.
        """
        if method not in _READ_ONLY_METHODS:
            raise ValueError(f"read retry is limited to read-only DeepCapture methods: {method}")
        attempts = max(1, int(max_attempts))
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                remaining = self._remaining_timeout()
                return self.call(
                    method,
                    args,
                    timeout=min(self.capture_rpc_timeout, remaining)
                    if remaining is not None
                    else self.capture_rpc_timeout,
                ), attempt
            except (ShizaiCloudError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt >= attempts or not self._is_transient_capture_error(exc):
                    raise
                delay = 0.2 * attempt
                remaining = self._remaining_timeout()
                if remaining is not None:
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _package_path_arg(package_path: str | Path | None) -> list[dict[str, str]]:
        """Build the package context required by the designer DeepCapture API."""
        value = str(package_path or "").strip()
        if not value:
            raise ValueError(
                "project_ref is required to start DeepCapture; pass the flow_ref returned by shizai_rpa_start_design"
            )
        return [{"packagePath": str(Path(value).expanduser().resolve())}]

    @_capture_budget
    def start_capture(
        self,
        *,
        mode: int = DEFAULT_CAPTURE_MODE,
        package_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Initialize DeepCapture and enable the designer's real inspect mode.

        The desktop designer uses mode 2 for mouse-left-down picking and mode 1
        for hover picking.  Starting only ``startDeepCapture`` registers the
        windows but does not make webpage elements available.
        """
        mode = int(mode)
        if mode not in {1, 2}:
            raise ValueError("mode must be 1 (hover) or 2 (mouse-left-down)")
        normalized_package = str(Path(str(package_path or "")).expanduser().resolve())
        self._package_path_arg(package_path)
        _set_capture_ui_feedback(
            "takeover",
            "desktop_channel",
            "正在连接实在 Agent，并准备接管当前 Chrome 页面。",
            animation="takeover",
        )
        try:
            initialized, start_attempts = self._call_control_with_retry(
                "startDeepCapture",
                self._package_path_arg(package_path),
            )
            inspected, inspect_attempts = self._call_control_with_retry(
                "startDeepCaptureInspect",
                [mode],
            )
        except Exception as exc:
            _set_capture_ui_feedback(
                "error",
                "start_capture",
                f"实在 Agent 元素拾取启动失败：{exc}",
                overlay_state="inactive",
                animation="error",
            )
            raise
        ui_feedback = _set_capture_ui_feedback(
            "capturing",
            "element_pick",
            "实在 Agent 已接管当前 Chrome，元素拾取覆盖层已启动；请在页面中点击或悬停目标元素。",
            animation="native_extension_picker",
            mode="hover" if mode == 1 else "mouse_left_down",
        )
        persisted = _read_capture_session_state(normalized_package)
        session_id = (
            str(persisted.get("session_id") or f"dc_{secrets.token_urlsafe(12)}")
            if persisted
            else f"dc_{secrets.token_urlsafe(12)}"
        )
        _write_capture_session_state(
            normalized_package,
            {
                "session_id": session_id,
                "project_ref": normalized_package,
                "package_path": normalized_package,
                "started_at": float(persisted.get("started_at") or time.time()) if persisted else time.time(),
                "last_action": "start_inspect",
                "inspect_started": True,
                "overlay_state": "active",
                "native_session_state": "active_last_known",
            },
        )
        with _CAPTURE_SESSION_LOCK:
            global _CAPTURE_SESSION_PACKAGE, _CAPTURE_SESSION_STARTED_AT
            _CAPTURE_SESSION_PACKAGE = normalized_package
            _CAPTURE_SESSION_STARTED_AT = time.monotonic()
        status = self.status(timeout=self._remaining_timeout())
        status["next_action"] = "shizai_rpa_capture_windows"
        return {
            "result": initialized,
            "session_id": session_id,
            "project_ref": normalized_package,
            "inspect_result": inspected,
            "mode": "hover" if mode == 1 else "mouse_left_down",
            "mode_value": mode,
            "control_mode": "official_picker",
            "browser_control": "deep_capture_official_picker",
            "runtime_execution": False,
            "picker_overlay_started": True,
            "native_session_state": "active_last_known",
            "rpc_attempts": {
                "startDeepCapture": start_attempts,
                "startDeepCaptureInspect": inspect_attempts,
            },
            "recovered_from_transient_error": start_attempts > 1 or inspect_attempts > 1,
            "status": status,
            "ui_feedback": ui_feedback,
            "next_action": {
                "kind": "browser_element_pick",
                "message": "请在用户当前 Chrome 页面悬停或点击目标网页元素；然后重新调用 shizai_rpa_capture_windows/tree。",
            },
        }

    @_capture_budget
    def start_capture_session(self, *, package_path: str | Path | None = None) -> dict[str, Any]:
        """Initialize DeepCapture without enabling the red element-pick overlay.

        Runtime preflight only needs the current Chrome window and element tree.
        ``startDeepCaptureInspect`` is reserved for interactive flow design,
        where the user or model is actively choosing page elements.
        """
        normalized_package = str(Path(str(package_path or "")).expanduser().resolve())
        self._package_path_arg(package_path)
        _set_capture_ui_feedback(
            "scanning",
            "runtime_capture_session",
            "正在连接实在 Agent，并读取当前 Chrome 页面。",
            overlay_state="inactive",
            animation="runtime_preflight",
        )
        try:
            initialized, attempts = self._call_control_with_retry(
                "startDeepCapture",
                self._package_path_arg(package_path),
            )
        except Exception as exc:
            _set_capture_ui_feedback(
                "error",
                "runtime_capture_session",
                f"实在 Agent 页面读取会话启动失败：{exc}",
                overlay_state="inactive",
                animation="error",
            )
            raise
        with _CAPTURE_SESSION_LOCK:
            global _CAPTURE_SESSION_PACKAGE, _CAPTURE_SESSION_STARTED_AT
            _CAPTURE_SESSION_PACKAGE = normalized_package
            _CAPTURE_SESSION_STARTED_AT = time.monotonic()
        session_id = f"dc_{secrets.token_urlsafe(12)}"
        state = {
            "session_id": session_id,
            "project_ref": normalized_package,
            "package_path": normalized_package,
            "started_at": time.time(),
            "last_action": "start_session",
            "inspect_started": False,
            "overlay_state": "inactive",
            "native_session_state": "active_last_known",
        }
        _write_capture_session_state(normalized_package, state)
        return {
            "result": initialized,
            "session_id": session_id,
            "project_ref": normalized_package,
            "inspect_started": False,
            "overlay_state": "inactive",
            "control_mode": "capture_read",
            "browser_control": "deep_capture_read",
            "runtime_execution": False,
            "picker_overlay_started": False,
            "native_session_state": "active_last_known",
            "rpc_attempts": {"startDeepCapture": attempts},
            "recovered_from_transient_error": attempts > 1,
            "ui_feedback": _capture_ui_feedback(),
        }

    def ensure_capture_session(self, *, package_path: str | Path | None = None) -> dict[str, Any]:
        """Initialize DeepCapture once per process and project.

        CLI commands are short-lived while MCP keeps one server process alive.
        Both paths therefore use this idempotent entry point so a scan and a
        subsequent capture do not restart the session and invalidate its
        session-scoped element ids.
        """
        global _CAPTURE_SESSION_PACKAGE, _CAPTURE_SESSION_STARTED_AT
        value = str(package_path or "").strip()
        if not value:
            raise ValueError(
                "project_ref is required to initialize DeepCapture; pass the active flow/project path"
            )
        normalized = str(Path(value).expanduser().resolve())
        with _CAPTURE_SESSION_LOCK:
            if _CAPTURE_SESSION_PACKAGE == normalized:
                result = {
                    "initialized": True,
                    "reused": True,
                    "session_id": None,
                    "project_ref": normalized,
                    "package_path": normalized,
                    "started_at_monotonic": _CAPTURE_SESSION_STARTED_AT,
                    "inspect_started": False,
                    "overlay_state": "inactive",
                    "control_mode": "capture_read",
                    "browser_control": "deep_capture_read",
                    "runtime_execution": False,
                    "picker_overlay_started": False,
                    "native_session_state": "active_last_known",
                    "ui_feedback": _capture_ui_feedback(),
                }
                persisted = _read_capture_session_state(normalized)
                if persisted:
                    result.update(
                        {
                            "session_id": persisted.get("session_id"),
                            "started_at": persisted.get("started_at"),
                        }
                    )
                return result
        persisted = _read_capture_session_state(normalized)
        if persisted and persisted.get("native_session_state") == "active_last_known":
            try:
                # A previous CLI process cannot prove that the native host is
                # still alive. One bounded root read is enough to validate the
                # durable handshake without invalidating session-scoped ids.
                _capture, probe_attempts = self._call_read_with_retry(
                    "getDeepCaptureElementChildrenById", [""], max_attempts=1
                )
            except (ShizaiCloudError, OSError, TimeoutError) as exc:
                result = self.start_capture_session(package_path=normalized)
                result.update(
                    {
                        "reused": False,
                        "reconnected_from_persisted_state": True,
                        "stale_session_reason": str(exc),
                    }
                )
                return result
            with _CAPTURE_SESSION_LOCK:
                _CAPTURE_SESSION_PACKAGE = normalized
                _CAPTURE_SESSION_STARTED_AT = time.monotonic()
            return {
                "initialized": True,
                "reused": True,
                "session_id": persisted.get("session_id"),
                "project_ref": normalized,
                "package_path": normalized,
                "started_at": persisted.get("started_at"),
                "inspect_started": bool(persisted.get("inspect_started")),
                "overlay_state": str(persisted.get("overlay_state") or "inactive"),
                "control_mode": "official_picker" if persisted.get("inspect_started") else "capture_read",
                "browser_control": (
                    "deep_capture_official_picker" if persisted.get("inspect_started") else "deep_capture_read"
                ),
                "runtime_execution": False,
                "picker_overlay_started": bool(persisted.get("inspect_started")),
                "native_session_state": "active_last_known",
                "reused_from_persisted_state": True,
                "remote_probe": {"ok": True, "attempts": probe_attempts},
                "ui_feedback": _capture_ui_feedback(),
            }
        result = self.start_capture_session(package_path=normalized)
        result.update({"initialized": True, "reused": False, "package_path": normalized})
        return result

    def capture_session_status(self, *, package_path: str | Path | None = None) -> dict[str, Any]:
        """Report the last known project capture state without starting capture."""
        value = str(package_path or "").strip()
        if not value:
            raise ValueError("project_ref is required to inspect the capture session")
        normalized = str(Path(value).expanduser().resolve())
        persisted = _read_capture_session_state(normalized)
        current = _capture_ui_feedback()
        if persisted is None:
            return {
                "kind": "shizai_capture_session_status",
                "project_ref": normalized,
                "active": False,
                "native_session_state": "not_started_or_expired",
                "inspect_started": False,
                "overlay_state": "inactive",
                "control_mode": "none",
                "truth_source": "last_known_local_state",
                "ui_feedback": current,
                "next_action": "workflow rpa-browser-session-start",
            }
        active = persisted.get("native_session_state") == "active_last_known"
        inspect_started = bool(persisted.get("inspect_started")) and active
        return {
            "kind": "shizai_capture_session_status",
            "project_ref": normalized,
            "session_id": persisted.get("session_id"),
            "started_at": persisted.get("started_at"),
            "active": active,
            "native_session_state": persisted.get("native_session_state", "unknown"),
            "inspect_started": inspect_started,
            "overlay_state": "active" if inspect_started else "inactive",
            "control_mode": "official_picker" if inspect_started else "capture_read" if active else "none",
            "browser_control": (
                "deep_capture_official_picker"
                if inspect_started
                else "deep_capture_read"
                if active
                else "none"
            ),
            "runtime_execution": False,
            "picker_overlay_started": inspect_started,
            "truth_source": "last_known_local_state",
            "ui_feedback": current,
            "next_action": (
                "extension stop-capture"
                if inspect_started
                else "rpa-scan-page"
                if active
                else "workflow rpa-browser-session-start"
            ),
        }

    def stop_capture(self, *, package_path: str | Path | None = None) -> dict[str, Any]:
        normalized_package = str(Path(str(package_path or "")).expanduser().resolve())
        self._package_path_arg(package_path)
        inspect = self.stop_inspect()
        inspect_result = inspect.get("result")
        inspect_warning = inspect.get("warning")
        try:
            result = self.call(
                "stopDeepCapture",
                self._package_path_arg(package_path),
                allow_write=True,
            )
        except Exception as exc:
            _set_capture_ui_feedback(
                "error",
                "stop_capture",
                f"实在 Agent 元素拾取停止失败：{exc}",
                overlay_state="unknown",
                animation="error",
            )
            raise
        ui_feedback = _set_capture_ui_feedback(
            "idle",
            "ready",
            "元素拾取已结束，当前 Chrome 会话仍可复用。",
            overlay_state="inactive",
            animation="idle",
        )
        response = {
            "project_ref": normalized_package,
            "control_mode": "none",
            "browser_control": "none",
            "runtime_execution": False,
            "picker_overlay_started": False,
            "inspect_result": inspect_result,
            "result": result,
            "status": self.status(timeout=self._remaining_timeout()),
            "ui_feedback": ui_feedback,
        }
        with _CAPTURE_SESSION_LOCK:
            global _CAPTURE_SESSION_PACKAGE, _CAPTURE_SESSION_STARTED_AT
            _CAPTURE_SESSION_PACKAGE = None
            _CAPTURE_SESSION_STARTED_AT = 0.0
        _write_capture_session_state(
            normalized_package,
            {
                "session_id": None,
                "project_ref": normalized_package,
                "package_path": normalized_package,
                "stopped_at": time.time(),
                "last_action": "stop_session",
                "inspect_started": False,
                "overlay_state": "inactive",
                "native_session_state": "stopped",
            },
        )
        if inspect_warning:
            response["inspect_warning"] = inspect_warning
        return response

    def stop_inspect(self) -> dict[str, Any]:
        """Remove the red picker overlay while keeping DeepCapture connected."""
        try:
            result = self.call("stopDeepCaptureInspect", [], allow_write=True)
        except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
            _set_capture_ui_feedback(
                "waiting",
                "stop_inspect",
                f"实在 Agent 红色元素拾取层退出返回警告：{exc}",
                overlay_state="inactive",
                animation="waiting",
            )
            return {"result": None, "warning": str(exc), "overlay_state": "inactive"}
        ui_feedback = _set_capture_ui_feedback(
            "scanning",
            "runtime_capture_session",
            "元素拾取层已退出，保留浏览器采集会话供流程运行。",
            overlay_state="inactive",
            animation="runtime_preflight",
        )
        # CLI and MCP calls are often separate processes. Keep the durable
        # project state aligned with the provider after stopping only the
        # picker; otherwise a later status call can resurrect a stale
        # ``inspect_started`` flag even though the red layer is gone.
        with _CAPTURE_SESSION_LOCK:
            package_path = _CAPTURE_SESSION_PACKAGE
        if package_path:
            persisted = _read_capture_session_state(package_path) or {}
            persisted.update(
                {
                    "project_ref": package_path,
                    "package_path": package_path,
                    "last_action": "stop_inspect",
                    "inspect_started": False,
                    "overlay_state": "inactive",
                    "native_session_state": "active_last_known",
                }
            )
            _write_capture_session_state(package_path, persisted)
        return {"result": result, "overlay_state": "inactive", "ui_feedback": ui_feedback}

    @_capture_budget
    def capture_windows(
        self,
        *,
        include_non_chrome: bool = False,
        resolve_context: bool = True,
    ) -> dict[str, Any]:
        _set_capture_ui_feedback(
            "capturing",
            "window_discovery",
            "正在读取当前 Chrome 窗口，复用现有浏览器会话。",
            animation="native_extension_picker",
        )
        root_payload = _unwrap(self._capture_rpc_call("getDeepCaptureElementChildrenById", [""]))
        root_descendants = (
            [item for item in root_payload.get("descendants", []) if isinstance(item, dict)]
            if isinstance(root_payload, dict)
            else []
        )
        native_windows = {str(item.get("elementId")): item for item in root_descendants if item.get("elementId")}
        roots: list[dict[str, Any]] = []
        for hwnd, title in _enum_windows():
            element_id = f"wnd:{hwnd}"
            root = native_windows.get(element_id, {})
            if not resolve_context:
                roots.append(
                    {
                        "element_id": element_id,
                        "title": title or str(root.get("name") or "Chrome"),
                        "page_title": title or str(root.get("name") or "Chrome"),
                        "page_url": "",
                        "context_resolved": False,
                    }
                )
                continue
            try:
                detail = _unwrap(self._capture_rpc_call("getDeepCaptureElementDetail", [element_id]))
                selector = _unwrap(self._capture_rpc_call("getDeepCaptureElementSelector", [element_id]))
            except (ShizaiCloudError, OSError, ValueError) as exc:
                if self._is_transient_capture_error(exc):
                    raise
                roots.append({"element_id": element_id, "title": title, "error": str(exc)})
                continue
            native_selector = _native_selector(selector)
            selector_title, selector_url = _selector_context(native_selector or {})
            roots.append(
                {
                    "element_id": element_id,
                    "title": title or str(root.get("name") or _window_title(detail)),
                    "page_title": selector_title or title or str(root.get("name") or _window_title(detail)),
                    "page_url": selector_url,
                    "process": _window_process(detail),
                    "detail": detail,
                    "selector": native_selector,
                }
            )
        if include_non_chrome and not roots:
            roots.append({"element_id": None, "title": "", "message": "No visible Chrome window was found"})
        has_chrome = any(str(item.get("element_id") or "").strip() for item in roots if isinstance(item, dict))
        ui_feedback = _set_capture_ui_feedback(
            "capturing" if has_chrome else "waiting",
            "window_discovery",
            "已找到当前 Chrome 窗口；下一步读取 Document 页面元素。"
            if has_chrome
            else "未找到可复用的 Chrome 窗口，请先打开 Chrome 页面。",
            animation="native_extension_picker" if has_chrome else "waiting",
            overlay_state="inactive",
            window_count=sum(1 for item in roots if isinstance(item, dict) and item.get("element_id")),
        )
        return {
            "kind": "shizai_deep_capture_windows",
            **_capture_read_metadata(),
            "count": sum(1 for item in roots if isinstance(item, dict) and item.get("element_id")),
            "windows": roots,
            "chrome_session": {
                "detected": has_chrome,
                "reused": has_chrome,
                "opened_by_capture": False,
                "uses_current_profile": True,
            },
            "status": self.status(timeout=self._remaining_timeout()),
            "ui_feedback": ui_feedback,
            "native_window_count": len(root_descendants),
            "next_action": {
                "tool": "shizai_rpa_capture_tree",
                "reason": "选择目标 Chrome 窗口的 element_id 并先读取页面树；只有已写入的 unbound 组件在普通扫描失败后才进入官方拾取。",
            }
            if has_chrome
            else {
                "tool": "shizai_rpa_open_browser",
                "reason": "先用首节点目标 URL 打开当前 Chrome，再重新捕获窗口。",
            },
        }

    @_capture_budget
    def scan_page(
        self,
        *,
        root_element_id: str | None = None,
        query: str = "",
        level: int = DEFAULT_CAPTURE_LEVEL,
        max_nodes: int = 250,
        page_title: str | None = None,
        page_url: str | None = None,
        scan_token: str | None = None,
        strategy: str = "full",
    ) -> dict[str, Any]:
        """Scan the current page subtree and return semantic element candidates.

        DeepCapture's window response contains the native Chrome shell. A
        usable webpage starts at a Document node, so this method first locates
        that node and then makes a second, direct children request from it.
        It returns ids and hints only; callers must use ``capture_element(s)``
        for the full reusable native selector before importing a candidate.
        """
        if scan_token:
            cached = _read_page_scan(scan_token)
            if cached is None:
                raise ValueError(
                    "scan_token is missing or expired; scan the current page again before capturing elements"
                )
            # Do not traverse the tree again: the ids in this snapshot belong
            # to the same DeepCapture session in which the model selected them.
            cached["scan_token"] = str(scan_token).strip()
            cached["snapshot_reused"] = True
            cached["status"] = self.status(timeout=self._remaining_timeout())
            cached["ui_feedback"] = _set_capture_ui_feedback(
                "ready" if cached.get("element_scan_ready") else "waiting",
                "scan_snapshot_reused",
                "已复用同一 DeepCapture 扫描快照，元素 ID 未重新扫描。",
                animation="scan_snapshot_reused",
                overlay_state="active" if cached.get("capture_ready") else "inactive",
                scan_token=str(scan_token).strip(),
            )
            return cached

        strategy = str(strategy or "full").strip().casefold()
        if strategy not in {"full", "auto", "targeted"}:
            raise ValueError("strategy must be full, auto, or targeted")
        _set_capture_ui_feedback(
            "scanning",
            "document_scan",
            "正在扫描当前 Chrome 页面 Document，分析可复用的网页元素。",
            animation="scan_progress",
        )
        level = max(1, min(int(level), 20))
        max_nodes = max(1, min(int(max_nodes), 1000))
        requested_root = str(root_element_id or "").strip()
        page_candidates: list[dict[str, Any]] = []
        inspected_roots: list[dict[str, Any]] = []
        document_ids: list[str] = []
        visible_text: list[str] = []
        resolved_page_title = str(page_title or "").strip()
        resolved_page_url = str(page_url or "").strip()
        targeted = None
        query_kind = _query_semantic_fields(query)[1]
        if strategy in {"auto", "targeted"} and not requested_root and query_kind in {"search_box", "submit"}:
            targeted = self._targeted_page_search(
                query=query,
                level=level,
                max_nodes=max_nodes,
                page_title=resolved_page_title,
                page_url=resolved_page_url,
            )
            if targeted:
                resolved_page_title = resolved_page_title or str(targeted.get("page_title") or "").strip()
                resolved_page_url = resolved_page_url or str(targeted.get("page_url") or "").strip()
                document_ids.extend(targeted.get("document_ids") or [])
                inspected_roots.extend(targeted.get("inspected_roots") or [])
                page_candidates.extend(targeted.get("candidates") or [])
                visible_text.extend(targeted.get("visible_text") or [])

        def capture_root(
            element_id: str,
            *,
            root_is_document: bool = False,
        ) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
            """Read one Chrome root and deepen once when only shell nodes are returned."""
            tree = self.capture_tree(element_id, level=level, max_nodes=max_nodes)
            descendants = tree.get("descendants", []) if isinstance(tree, dict) else []
            found = [element_id] if root_is_document else _find_document_ids(descendants)
            effective_level = level
            effective_max_nodes = max_nodes
            deepened = False
            if not found:
                retry_level = min(20, max(level * 2, 12))
                retry_max_nodes = min(1000, max(max_nodes * 2, 500))
                if retry_level > level or retry_max_nodes > max_nodes:
                    deeper = self.capture_tree(
                        element_id,
                        level=retry_level,
                        max_nodes=retry_max_nodes,
                    )
                    deeper_descendants = deeper.get("descendants", []) if isinstance(deeper, dict) else []
                    deeper_found = _find_document_ids(deeper_descendants)
                    if deeper_found or int(deeper.get("count") or 0) > int(tree.get("count") or 0):
                        tree = deeper
                        found = deeper_found
                        effective_level = retry_level
                        effective_max_nodes = retry_max_nodes
                        deepened = True
            return (
                tree,
                found,
                {
                    "capture_level": effective_level,
                    "capture_max_nodes": effective_max_nodes,
                    "deepened": deepened,
                },
            )

        targeted_success = bool(targeted and page_candidates)
        if targeted_success:
            # The bounded walk already inspected the selected controls.  Do
            # not immediately traverse the whole Document again; that would
            # erase the latency benefit of the targeted strategy.
            pass
        elif requested_root:
            root_detail = _unwrap(
                self._capture_rpc_call("getDeepCaptureElementDetail", [requested_root])
            )
            resolved_page_title = resolved_page_title or _window_title(root_detail)
            root_is_document = bool(_detail_control_names(root_detail) & {"document", "文档"})
            root_tree, document_ids, root_scan = capture_root(
                requested_root,
                root_is_document=root_is_document,
            )
            inspected_roots.append(
                {
                    "element_id": requested_root,
                    "kind": "requested_root",
                    "count": root_tree.get("count", 0),
                    **root_scan,
                }
            )
            visible_text.extend(_tree_text(root_tree.get("descendants", [])))
        else:
            windows = self.capture_windows()
            for window in windows.get("windows", []):
                if not isinstance(window, dict):
                    continue
                window_id = str(window.get("element_id") or "").strip()
                if not window_id:
                    continue
                resolved_page_title = (
                    resolved_page_title or str(window.get("page_title") or window.get("title") or "").strip()
                )
                resolved_page_url = resolved_page_url or str(window.get("page_url") or "").strip()
                window_tree, found, root_scan = capture_root(window_id)
                inspected_roots.append(
                    {
                        "element_id": window_id,
                        "title": window.get("title", ""),
                        "kind": "chrome_window",
                        "document_ids": found,
                        **root_scan,
                    }
                )
                document_ids.extend(found)
                visible_text.extend(_tree_text(window_tree.get("descendants", [])))

        document_ids = list(dict.fromkeys(document_ids))
        if not targeted_success:
            for document_id in document_ids:
            # This is the important boundary: scan from Document, rather than
            # treating Window/Pane descendants as webpage controls.
                document_tree = self.capture_tree(document_id, level=level, max_nodes=max_nodes)
                candidates = _scan_tree_nodes(
                    document_tree.get("descendants", []),
                    query=query,
                    max_nodes=max_nodes,
                    document_seen=True,
                    path=("Document",),
                )
                for candidate in candidates:
                    candidate["document_id"] = document_id
                    candidate["page_title"] = resolved_page_title
                    candidate["page_url"] = resolved_page_url
                page_candidates.extend(candidates)
                visible_text.extend(_tree_text(document_tree.get("descendants", [])))
                if len(page_candidates) >= max_nodes:
                    break

        unique_candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for candidate in page_candidates:
            element_id = str(candidate.get("element_id") or "")
            if element_id and element_id not in seen_ids:
                seen_ids.add(element_id)
                unique_candidates.append(candidate)
        unique_candidates = unique_candidates[:max_nodes]
        by_field: dict[str, list[dict[str, Any]]] = {field: [] for field in _SEMANTIC_FIELDS}
        for candidate in unique_candidates:
            for field in candidate.get("semantic_fields", []):
                if field in by_field:
                    by_field[field].append(candidate)
        for field in by_field:
            by_field[field] = by_field[field][:20]

        challenge_detected, login_detected = _page_state_markers(
            resolved_page_title,
            visible_text,
            resolved_page_url,
        )
        block_reason = "page_challenge" if challenge_detected else "login_required" if login_detected else None
        interaction = (
            _waiting_user_interaction(
                block_reason,
                "当前页面进入验证页，请先完成人机验证。"
                if block_reason == "page_challenge"
                else "当前页面需要登录，请先完成登录。",
            )
            if block_reason
            else None
        )
        ready = bool(document_ids) and interaction is None
        ui_feedback = _set_capture_ui_feedback(
            "ready" if unique_candidates and not interaction else "waiting",
            "semantic_scan",
            interaction["user_prompt"]
            if interaction
            else f"页面扫描完成，找到 {len(unique_candidates)} 个候选元素；选择后读取完整 native selector。"
            if unique_candidates
            else "页面扫描完成，但当前没有找到候选网页元素。",
            animation="scan_complete" if unique_candidates else "waiting",
            overlay_state="inactive",
            candidate_count=len(unique_candidates),
        )
        result = {
            "kind": "shizai_page_element_candidates",
            **_capture_read_metadata(),
            "root_element_id": requested_root or None,
            "document_ids": document_ids,
            "inspected_roots": inspected_roots,
            "query": str(query or ""),
            "page_title": resolved_page_title,
            "page_url": resolved_page_url,
            "level": level,
            "max_nodes": max_nodes,
            "scan_strategy": "targeted" if targeted and page_candidates else "full",
            "targeted_fallback": bool(targeted is not None and not page_candidates),
            "targeted_nodes_visited": int((targeted or {}).get("nodes_visited") or 0),
            "targeted_rpc_calls": int((targeted or {}).get("rpc_calls") or 0),
            "targeted_elapsed_ms": (targeted or {}).get("elapsed_ms"),
            "candidate_count": len(unique_candidates),
            "candidates": unique_candidates,
            "by_semantic_field": by_field,
            "capture_ready": ready,
            "element_scan_ready": bool(unique_candidates),
            "workflow_status": "waiting_for_user" if interaction else "ready" if ready else "blocked",
            "interaction_required": interaction is not None,
            "interaction": interaction,
            "challenge_detected": challenge_detected,
            "login_detected": login_detected,
            "session_reused": ready,
            "chrome_session": {
                "detected": bool(inspected_roots),
                "reused": ready,
                "opened_by_scan": False,
                "window_count": len(inspected_roots),
                "document_count": len(document_ids),
                "page_title": resolved_page_title,
                "page_url": resolved_page_url,
                "uses_current_profile": True,
            },
            "message": (
                "已从当前页面 Document 子树扫描候选元素；选择 element_id 后调用 shizai_rpa_capture_elements 获取完整 native selector。"
                if ready
                else "当前没有找到 Document 页面节点；请保持当前 Chrome 页面打开，先写入计划中的元素组件，再按该 project_ref + step_id 启动官方拾取。"
            ),
            "next_action": {
                "kind": "user_interaction",
                "action": "pause_and_ask_user",
                "message": interaction["user_prompt"],
                "resume": interaction["resume"],
            }
            if interaction
            else {
                "tool": "shizai_rpa_capture_elements",
                "reason": "候选元素只包含语义提示；导入流程前必须读取真实 selector 并通过 shizai_rpa_validate_elements。",
            }
            if unique_candidates
            else {
                "tool": "shizai_extension_start_capture",
                "arguments": {"allow_write": True, "mode": DEFAULT_CAPTURE_MODE},
                "reason": "返回流程编排层补齐已存在 unbound 组件的 project_ref + step_id 后，再启动官方拾取。",
            },
            "status": self.status(timeout=self._remaining_timeout()),
            "ui_feedback": ui_feedback,
        }
        cache_snapshot = dict(result)
        # Status/feedback contain live diagnostics and are intentionally not
        # used to identify a future capture snapshot.
        cache_snapshot.pop("status", None)
        cache_snapshot.pop("ui_feedback", None)
        result["scan_token"] = _remember_page_scan(cache_snapshot)
        result["snapshot_reused"] = False
        return result

    def _targeted_page_search(
        self,
        *,
        query: str,
        level: int,
        max_nodes: int,
        page_title: str,
        page_url: str,
    ) -> dict[str, Any] | None:
        """Find a small set of interactive controls without materializing the full page tree.

        Search inputs and submit buttons are usually shallow, named controls.
        Walk one child level at a time and stop after the first useful matches;
        the caller falls back to the legacy full-page scan when the provider
        does not expose enough structure for this bounded walk.
        """
        started = time.perf_counter()
        max_visit = min(160, max(24, int(max_nodes) * 2))
        max_depth = min(12, max(4, int(level) + 3))
        rpc_calls = 0
        nodes_visited = 0

        def children(parent_id: str) -> list[dict[str, Any]]:
            nonlocal rpc_calls
            try:
                payload, attempts = self._call_read_with_retry(
                    "getDeepCaptureElementChildrenById",
                    [parent_id, 1],
                )
            except (ShizaiCloudError, OSError, ValueError) as exc:
                if self._is_transient_capture_error(exc):
                    raise
                return []
            rpc_calls += attempts
            payload = _unwrap(payload)
            descendants = payload.get("descendants") if isinstance(payload, dict) else None
            return [item for item in descendants if isinstance(item, dict)] if isinstance(descendants, list) else []

        def flatten(items: Iterable[Any]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            stack = [item for item in items if isinstance(item, dict)]
            while stack and len(result) < max_visit:
                item = stack.pop(0)
                result.append(item)
                nested = item.get("children")
                if isinstance(nested, list):
                    stack.extend(child for child in nested if isinstance(child, dict))
            return result

        # Search controls are normally shallow. Avoid the detail/selector RPCs
        # for every Chrome window until a candidate has actually been found.
        windows = self.capture_windows(resolve_context=False)
        window_rows = [item for item in windows.get("windows", []) if isinstance(item, dict)]
        if not window_rows:
            return None
        document_ids: list[str] = []
        inspected_roots: list[dict[str, Any]] = []
        visible_text: list[str] = []
        for window in window_rows:
            window_id = str(window.get("element_id") or "").strip()
            if not window_id:
                continue
            queue: list[tuple[str, int, dict[str, Any] | None]] = [(window_id, 0, None)]
            seen: set[str] = {window_id}
            found_for_window: list[str] = []
            while queue and nodes_visited < max_visit:
                parent_id, depth, parent_node = queue.pop(0)
                batch = flatten(children(parent_id))
                nodes_visited += len(batch)
                for node in batch:
                    element_id = str(node.get("elementId") or "").strip()
                    if not element_id:
                        continue
                    visible_text.extend(_tree_text([node]))
                    if _node_control_names(node) & {"document", "文档"}:
                        if element_id not in found_for_window:
                            found_for_window.append(element_id)
                        continue
                    if depth + 1 < max_depth and element_id not in seen:
                        seen.add(element_id)
                        queue.append((element_id, depth + 1, node))
                if found_for_window:
                    break
            document_ids.extend(found_for_window)
            inspected_roots.append(
                {
                    "element_id": window_id,
                    "title": window.get("title", ""),
                    "kind": "chrome_window",
                    "document_ids": found_for_window,
                    "capture_level": max_depth,
                    "capture_max_nodes": max_visit,
                    "deepened": False,
                }
            )

        document_ids = list(dict.fromkeys(document_ids))
        candidates: list[dict[str, Any]] = []
        for document_id in document_ids:
            queue = [(document_id, 0)]
            seen = {document_id}
            while queue and nodes_visited < max_visit and len(candidates) < max_nodes:
                parent_id, depth = queue.pop(0)
                batch = flatten(children(parent_id))
                nodes_visited += len(batch)
                for node in batch:
                    element_id = str(node.get("elementId") or "").strip()
                    if not element_id:
                        continue
                    visible_text.extend(_tree_text([node]))
                    found = _scan_tree_nodes(
                        [node], query=query, max_nodes=max_nodes, document_seen=True, path=("Document",)
                    )
                    for candidate in found:
                        candidate["document_id"] = document_id
                        candidate["page_title"] = page_title or str(window_rows[0].get("page_title") or "").strip()
                        candidate["page_url"] = page_url or str(window_rows[0].get("page_url") or "").strip()
                        candidates.append(candidate)
                    if depth + 1 < max_depth and element_id not in seen:
                        seen.add(element_id)
                        queue.append((element_id, depth + 1))
                if candidates:
                    break
        return {
            "document_ids": document_ids,
            "inspected_roots": inspected_roots,
            "candidates": candidates,
            "visible_text": visible_text,
            "page_title": page_title or str(window_rows[0].get("page_title") or window_rows[0].get("title") or "").strip(),
            "page_url": page_url or str(window_rows[0].get("page_url") or "").strip(),
            "nodes_visited": nodes_visited,
            "rpc_calls": rpc_calls,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    @_capture_budget
    def runtime_preflight(
        self,
        *,
        expected_page_title: str | None = None,
        expected_page_url: str | None = None,
        step_id: str | None = None,
        component: str | None = None,
    ) -> dict[str, Any]:
        """Check that the current Chrome page can receive a native element action.

        This is deliberately a read-only check.  It reads the existing Chrome
        window and DeepCapture tree, reports challenge/login pages, and compares
        the page identity stored with a captured selector.  It never opens a
        browser, changes the inspect mode, or executes a workflow component.
        """
        _set_capture_ui_feedback(
            "scanning",
            "runtime_preflight",
            "正在执行网页组件运行前预检，确认当前 Chrome 页面仍与元素捕获页面一致。",
            animation="runtime_preflight",
            step_id=step_id,
            component=component,
        )
        try:
            windows_payload = self.capture_windows()
            windows = [
                item
                for item in windows_payload.get("windows", [])
                if isinstance(item, dict) and str(item.get("element_id") or "").strip()
            ]
            page_windows: list[dict[str, Any]] = []
            for window in windows:
                window_id = str(window.get("element_id") or "").strip()
                title = str(window.get("page_title") or window.get("title") or "").strip()
                url = str(window.get("page_url") or "").strip()
                # Chrome can place the renderer's Document below several
                # native toolbar and tab containers.  A shallow read is cheap,
                # but must not turn a valid page into a false document_missing
                # result after WebBrowser_CreateV3 has reused the window.
                tree = self.capture_tree(window_id, level=DEFAULT_CAPTURE_LEVEL, max_nodes=160)
                descendants = tree.get("descendants", []) if isinstance(tree, dict) else []
                document_ids = _find_document_ids(descendants)
                capture_level = DEFAULT_CAPTURE_LEVEL
                if not document_ids:
                    capture_level = min(20, max(DEFAULT_CAPTURE_LEVEL * 2, 12))
                    tree = self.capture_tree(window_id, level=capture_level, max_nodes=320)
                    descendants = tree.get("descendants", []) if isinstance(tree, dict) else []
                    document_ids = _find_document_ids(descendants)
                page_windows.append(
                    {
                        "element_id": window_id,
                        "title": title,
                        "url": url,
                        "document_ids": document_ids,
                        "document_found": bool(document_ids),
                        "text": _tree_text(descendants),
                        "capture_ready": bool(tree.get("capture_ready")) if isinstance(tree, dict) else False,
                        "capture_level": capture_level,
                    }
                )
            matching_windows = [
                item
                for item in page_windows
                if _page_titles_match(expected_page_title, str(item.get("title") or ""))
                and _page_urls_match(expected_page_url, str(item.get("url") or ""))
            ]
            selected = next((item for item in matching_windows if item["document_found"]), None)
            selected = selected or (matching_windows[0] if matching_windows else None)
            selected = selected or next((item for item in page_windows if item["document_found"]), None)
            selected = selected or (page_windows[0] if page_windows else None)
            current_title = str(selected.get("title") or "") if selected else ""
            current_url = str(selected.get("url") or "") if selected else ""
            visible_text = selected.get("text", []) if selected else []
            challenge_detected, login_detected = _page_state_markers(current_title, visible_text, current_url)
            document_found = bool(selected and selected.get("document_found"))
            title_match = _page_titles_match(expected_page_title, current_title)
            url_match = _page_urls_match(expected_page_url, current_url)
            identity_changed = bool(
                (expected_page_title and current_title and not title_match)
                or (expected_page_url and current_url and not url_match)
            )
            if not page_windows:
                reason = "chrome_not_found"
                message = "当前没有找到可复用的 Chrome 窗口，请先打开实在 Agent 使用的当前 Chrome。"
            elif challenge_detected:
                reason = "page_challenge"
                message = "当前页面进入验证页，请在当前 Chrome 完成人机验证后重新扫描元素，再继续单步。"
            elif login_detected:
                reason = "login_required"
                message = "当前页面需要登录，请在当前 Chrome 完成登录后重新扫描元素，再继续单步。"
            elif not document_found:
                reason = "document_missing"
                message = "当前 Chrome 只有窗口外壳，尚未读取到网页 Document；请等待页面加载并重新启动元素拾取。"
            elif identity_changed:
                reason = "page_identity_changed"
                message = "当前页面已变化，不能继续复用旧元素；请在当前页面重新扫描并捕获目标元素。"
            else:
                reason = "ready"
                message = "当前 Chrome 页面与捕获上下文一致，可以执行网页组件。"
            valid = reason == "ready"
            interaction_required = reason in {"login_required", "page_challenge"}
            interaction = _waiting_user_interaction(reason, message, step_id=step_id) if interaction_required else None
            recovery = (
                {
                    "preserve": ["当前 Chrome 会话", "当前已创建流程"],
                    "steps": [
                        "shizai_rpa_scan_page",
                        "shizai_rpa_capture_elements",
                        "shizai_project_import_elements",
                        "重新绑定或替换结果页元素",
                        "shizai_rpa_debug_command(action=next)",
                    ],
                    "optional_picker_tool": "shizai_extension_start_capture",
                    "capture_arguments": {
                        "allow_rebind": True,
                        "reason": "页面身份已变化，明确允许替换当前 step_id 的旧绑定。",
                    },
                    "rule": "页面跳转后只重新拾取和绑定结果页元素；不要重新创建流程，也不要再次执行 WebBrowser_CreateV3。",
                }
                if reason == "page_identity_changed"
                else None
            )
            ui_feedback = _set_capture_ui_feedback(
                "ready" if valid else "waiting",
                "runtime_preflight",
                message,
                animation="preflight_ready" if valid else "preflight_blocked",
                overlay_state="inactive",
                valid=valid,
                reason=reason,
                step_id=step_id,
                component=component,
            )
            return {
                "kind": "rpa_runtime_preflight",
                **_capture_read_metadata(),
                "status": "ready" if valid else "waiting_for_user" if interaction_required else "blocked",
                "valid": valid,
                "reason": reason,
                "message": message,
                "step_id": step_id,
                "component": component,
                "expected_page_title": str(expected_page_title or ""),
                "expected_page_url": str(expected_page_url or ""),
                "current_page_title": current_title,
                "current_page_url": current_url,
                "identity": {
                    "title_match": title_match,
                    "url_match": url_match,
                    "changed": identity_changed,
                },
                "document_found": document_found,
                "document_ids": list(selected.get("document_ids", [])) if selected else [],
                "challenge_detected": challenge_detected,
                "login_detected": login_detected,
                "interaction_required": interaction_required,
                "interaction": interaction,
                "visible_text": visible_text[:20],
                "chrome_session": {
                    "detected": bool(page_windows),
                    "reused": bool(page_windows),
                    "opened_by_preflight": False,
                    "uses_current_profile": True,
                },
                "recovery": recovery,
                "windows": [{key: value for key, value in item.items() if key != "text"} for item in page_windows],
                "next_action": (
                    {
                        "kind": "user_interaction",
                        "action": "pause_and_ask_user",
                        "message": interaction["user_prompt"],
                        "resume": interaction["resume"],
                    }
                    if interaction_required and interaction
                    else {
                        "tool": "shizai_rpa_debug_command" if valid else "shizai_rpa_scan_page",
                        **({"arguments": {"action": "next"}} if valid else {}),
                        "reason": "页面预检通过，继续执行当前网页组件。" if valid else message,
                        **({"recovery": recovery} if recovery else {}),
                    }
                ),
                "ui_feedback": ui_feedback,
            }
        except Exception as exc:
            message = f"网页组件运行前预检失败：{exc}；请检查实在 Agent 登录状态和 DeepCapture。"
            ui_feedback = _set_capture_ui_feedback(
                "error",
                "runtime_preflight",
                message,
                overlay_state="unknown",
                animation="preflight_error",
                valid=False,
                reason="extension_unavailable",
                step_id=step_id,
                component=component,
            )
            return {
                "kind": "rpa_runtime_preflight",
                **_capture_read_metadata(),
                "valid": False,
                "reason": "extension_unavailable",
                "message": message,
                "step_id": step_id,
                "component": component,
                "expected_page_title": str(expected_page_title or ""),
                "expected_page_url": str(expected_page_url or ""),
                "chrome_session": {
                    "detected": False,
                    "reused": False,
                    "opened_by_preflight": False,
                    "uses_current_profile": True,
                },
                "next_action": {
                    "tool": "shizai_extension_status",
                    "reason": "先确认实在 Agent 与原生扩展已登录并可连接。",
                },
                "error": str(exc),
                "ui_feedback": ui_feedback,
            }

    @_capture_budget
    def capture_tree(
        self,
        element_id: str,
        *,
        level: int = DEFAULT_CAPTURE_LEVEL,
        max_nodes: int = 120,
    ) -> dict[str, Any]:
        if not str(element_id).strip():
            raise ValueError("element_id must not be empty")
        _set_capture_ui_feedback(
            "scanning",
            "tree_scan",
            "正在读取当前 Chrome 的网页元素树。",
            animation="scan_progress",
        )
        level = max(0, min(int(level), 20))
        max_nodes = max(1, min(int(max_nodes), 1000))
        root_id = str(element_id)

        rpc_calls = 0
        read_retries = 0

        def fetch_children(parent_id: str, requested_level: int = level) -> list[dict[str, Any]]:
            nonlocal rpc_calls, read_retries
            payload, attempts = self._call_read_with_retry(
                "getDeepCaptureElementChildrenById",
                [parent_id, requested_level],
            )
            rpc_calls += attempts
            read_retries += max(0, attempts - 1)
            payload = _unwrap(payload)
            if not isinstance(payload, dict):
                return []
            descendants = payload.get("descendants")
            return [item for item in descendants if isinstance(item, dict)] if isinstance(descendants, list) else []

        initial = fetch_children(root_id, level)
        # DeepCapture returns descendants only. When the caller starts from a
        # Document id, the document marker is therefore absent from ``initial``
        # and a valid UIA page tree can be mistaken for the Chrome shell.
        try:
            root_detail_payload, attempts = self._call_read_with_retry(
                "getDeepCaptureElementDetail",
                [root_id],
            )
            rpc_calls += attempts
            read_retries += max(0, attempts - 1)
            root_detail = _unwrap(root_detail_payload)
        except (ShizaiCloudError, OSError, ValueError) as exc:
            if self._is_transient_capture_error(exc):
                raise
            root_detail = None
        root_is_document = bool(_detail_control_names(root_detail) & {"document", "文档"})
        # Newer DeepCapture builds return the requested subtree as one flat
        # ``descendants`` payload.  Refetching every returned node is both
        # redundant and very slow on product pages.  A small one-node response
        # still uses the legacy recursive fallback used by older builds.
        bulk_response = len(initial) >= 16 or any(
            isinstance(item.get("children"), list) and item.get("children")
            for item in initial
            if isinstance(item, dict)
        )
        queue: list[tuple[dict[str, Any], int]] = [(item, 1) for item in initial]
        seen: set[str] = {root_id}
        total = 0
        truncated = False

        while queue and total < max_nodes:
            item, depth = queue.pop(0)
            child_id = str(item.get("elementId") or "").strip()
            if child_id and child_id in seen:
                continue
            if child_id:
                seen.add(child_id)
            item.setdefault("children", [])
            total += 1
            if not bulk_response and depth < level and child_id and total < max_nodes:
                children = item.get("children")
                if not isinstance(children, list) or not children:
                    children = fetch_children(child_id)
                    item["children"] = children
                queue.extend((child, depth + 1) for child in children if isinstance(child, dict))
        if queue:
            truncated = True

        ready, message = _tree_message(initial, document_seen=root_is_document)
        ui_feedback = _set_capture_ui_feedback(
            "ready" if ready else "waiting",
            "tree_scan",
            "已读取网页元素树，可继续捕获目标元素。" if ready else message,
            animation="scan_complete" if ready else "waiting",
            overlay_state="inactive",
            web_element_count=sum(
                1 for item in initial if _tree_has_web_element([item], _document_seen=root_is_document)
            ),
        )
        return {
            "kind": "shizai_deep_capture_tree",
            **_capture_read_metadata(),
            "element_id": root_id,
            "level": level,
            "max_nodes": max_nodes,
            "descendants": initial,
            "count": total,
            "truncated": truncated,
            "message": "tree truncated at max_nodes" if truncated else message,
            "capture_ready": ready,
            "web_element_count": sum(
                1 for item in initial if _tree_has_web_element([item], _document_seen=root_is_document)
            ),
            "rpc_calls": rpc_calls,
            "read_retries": read_retries,
            "recovered_from_transient_error": read_retries > 0,
            "fetch_mode": "bulk" if bulk_response else "recursive_fallback",
            "code": 0,
            "status": self.status(timeout=self._remaining_timeout()),
            "ui_feedback": ui_feedback,
            "next_action": {
                "tool": "shizai_rpa_capture_element",
                "reason": "树中已有网页元素，请使用 element_id 读取原生 selector。",
            }
            if ready
            else {
                "tool": "shizai_extension_start_capture",
                "arguments": {"allow_write": True, "mode": DEFAULT_CAPTURE_MODE},
                "reason": message,
            },
        }

    @_capture_budget
    def capture_element(
        self,
        element_id: str,
        *,
        page_title: str | None = None,
        page_url: str | None = None,
    ) -> dict[str, Any]:
        element_id = str(element_id).strip()
        if not element_id:
            raise ValueError("element_id must not be empty")
        _set_capture_ui_feedback(
            "capturing",
            "element_capture",
            f"正在读取元素 {element_id} 的完整 native selector。",
            animation="native_extension_picker",
            element_id=element_id,
        )
        try:
            detail = _unwrap(self._capture_rpc_call("getDeepCaptureElementDetail", [element_id]))
            selector = _unwrap(self._capture_rpc_call("getDeepCaptureElementSelector", [element_id]))
            selector_dict = _native_selector(selector)
            if not selector_dict or not _tree_has_web_element(selector_dict.get("selector", [])):
                raise ShizaiCloudError(
                    f"{element_id} 仍是 Chrome 原生外壳，没有网页 selector；"
                    "请先启动 startDeepCaptureInspect(2)，在 Chrome 中点击目标元素后重试。"
                )
            record = _element_record(
                element_id,
                detail,
                selector,
                page_title=page_title,
                page_url=page_url,
            )
        except Exception as exc:
            _set_capture_ui_feedback(
                "error",
                "element_capture",
                f"元素 {element_id} 捕获失败：{exc}",
                overlay_state="active",
                animation="error",
                element_id=element_id,
            )
            raise
        ui_feedback = _set_capture_ui_feedback(
            "ready",
            "element_captured",
            f"元素 {element_id} 已捕获，可校验并绑定到流程组件。",
            animation="capture_complete",
            element_id=element_id,
        )
        return {
            "kind": "shizai_captured_element",
            **_capture_read_metadata(),
            "element_id": element_id,
            "record": record,
            "detail": detail,
            "selector": record["selector"],
            "status": self.status(timeout=self._remaining_timeout()),
            "ui_feedback": ui_feedback,
        }

    def capture_elements(
        self,
        element_ids: Iterable[str],
        *,
        page_title: str | None = None,
        page_url: str | None = None,
        scan_token: str | None = None,
    ) -> dict[str, Any]:
        ids = [str(item).strip() for item in element_ids if str(item).strip()]
        if not ids:
            raise ValueError("element_ids must contain at least one id")
        if scan_token:
            snapshot = _read_page_scan(scan_token)
            if snapshot is None:
                raise ValueError(
                    "scan_token is missing or expired; scan the current page again before capturing elements"
                )
            snapshot_ids = {
                str(item.get("element_id") or "").strip()
                for item in snapshot.get("candidates", [])
                if isinstance(item, dict) and str(item.get("element_id") or "").strip()
            }
            missing = [item for item in ids if item not in snapshot_ids]
            if missing:
                raise ValueError("element ids are not present in scan_token snapshot: " + ", ".join(missing))
        records = [
            self.capture_element(item, page_title=page_title, page_url=page_url)["record"]
            for item in dict.fromkeys(ids)
        ]
        ui_feedback = _set_capture_ui_feedback(
            "ready",
            "elements_captured",
            f"已捕获 {len(records)} 个元素，可校验并导入流程。",
            animation="capture_complete",
            element_count=len(records),
        )
        return {
            "kind": "shizai_captured_elements",
            **_capture_read_metadata(),
            "element_count": len(records),
            "elements": records,
            "scan_token": str(scan_token or "").strip() or None,
            "ui_feedback": ui_feedback,
        }

    @_capture_budget
    def capture_page_fields(
        self,
        *,
        fields: Iterable[str] = _CAPTURE_FIELD_NAMES,
        field_element_ids: dict[str, Any] | None = None,
        level: int = DEFAULT_CAPTURE_LEVEL,
        max_nodes: int = 250,
        candidate_limit: int = 4,
        page_title: str | None = None,
        page_url: str | None = None,
        scan_token: str | None = None,
        card_grouping: str = "auto",
    ) -> dict[str, Any]:
        """Scan and capture a coherent set of page fields in one bridge call.

        DeepCapture ids are session-scoped. Keeping scan, candidate selection,
        and selector capture in one operation prevents a second scan from
        invalidating the ids the model just selected. Product-card context is
        derived from the captured native chain, so fields from different cards
        are returned as alternatives instead of being silently mixed.
        """
        normalized_fields: list[str] = []
        for raw_field in fields:
            key = _CAPTURE_FIELD_ALIASES.get(str(raw_field).strip(), str(raw_field).strip())
            if key and key not in normalized_fields:
                normalized_fields.append(key)
        if not normalized_fields:
            raise ValueError("fields must contain at least one supported page field")
        unsupported = [item for item in normalized_fields if item not in _CAPTURE_FIELD_NAMES and item != "search"]
        if unsupported:
            raise ValueError("unsupported page fields: " + ", ".join(unsupported))
        candidate_limit = max(1, min(int(candidate_limit), 20))
        card_grouping = str(card_grouping or "auto").strip().casefold()
        if card_grouping not in {"auto", "instance", "family"}:
            raise ValueError("card_grouping must be auto, instance, or family")
        scan = self.scan_page(
            query="",
            level=level,
            max_nodes=max_nodes,
            page_title=page_title,
            page_url=page_url,
            scan_token=scan_token,
        )
        candidates = [item for item in scan.get("candidates", []) if isinstance(item, dict)]
        explicit = field_element_ids if isinstance(field_element_ids, dict) else {}
        candidates_by_id = {
            str(item.get("element_id") or "").strip(): item
            for item in candidates
            if str(item.get("element_id") or "").strip()
        }
        selected_by_field: dict[str, list[str]] = {}
        selected_ids: list[str] = []
        selection_errors: list[str] = []
        required_fields = [item for item in normalized_fields if item != "search"]

        def explicit_ids(field: str) -> list[str]:
            raw = explicit.get(field)
            if raw is None:
                for alias, canonical in _CAPTURE_FIELD_ALIASES.items():
                    if canonical == field and alias in explicit:
                        raw = explicit[alias]
                        break
            if isinstance(raw, (list, tuple)):
                return [str(item).strip() for item in raw if str(item).strip()]
            if raw is None:
                return []
            value = str(raw).strip()
            return [value] if value else []

        candidate_pools: dict[str, list[dict[str, Any]]] = {}

        def candidate_pool(field: str) -> list[dict[str, Any]]:
            """Return all ordered candidates for a field from this one scan."""
            if field in candidate_pools:
                return candidate_pools[field]
            field_candidates = [
                item
                for item in candidates
                if field in (item.get("semantic_fields") or []) and str(item.get("element_id") or "").strip()
            ]
            if field == "link":
                field_candidates = [item for item in field_candidates if not _candidate_is_link_noise(item)]
            if field == "merchant":
                field_candidates = [item for item in field_candidates if not _candidate_is_merchant_badge(item)]
            field_candidates.sort(
                key=lambda item: (
                    -int(bool(_candidate_card_key(item))),
                    -int(item.get("semantic_score", 0) or 0),
                    int(item.get("scan_order", 0) or 0),
                )
            )
            candidate_pools[field] = field_candidates
            return field_candidates

        def candidate_ids(field: str, limit: int, *, offset: int = 0) -> list[str]:
            """Return ids from a stable scan pool; offsets avoid duplicate RPCs."""
            return list(
                dict.fromkeys(
                    str(item.get("element_id") or "").strip()
                    for item in candidate_pool(field)[offset : offset + limit]
                    if str(item.get("element_id") or "").strip()
                )
            )

        def append_candidate_ids(field: str, target_count: int) -> list[str]:
            """Extend one field's capture window without changing old ids."""
            current = selected_by_field.setdefault(field, [])
            wanted = candidate_ids(field, target_count)
            added = [item for item in wanted if item not in current]
            current.extend(added)
            return added

        # Some extension versions expose card ancestry in the scan tree. Use
        # that hint to avoid capturing unrelated nodes when it is available;
        # ``auto`` starts with the family because a real Chrome UIA provider
        # may expose title, price, merchant, and sales through separate trees.
        hint_groups: dict[str, dict[str, Any]] = {}
        hint_grouping = "family" if card_grouping == "auto" else card_grouping
        for field in required_fields or normalized_fields:
            for item in candidate_pool(field):
                key = _candidate_group_key(item, hint_grouping)
                if not key:
                    continue
                group = hint_groups.setdefault(
                    key,
                    {
                        "key": key,
                        "grouping": hint_grouping,
                        "fields": {},
                        "element_ids": [],
                    },
                )
                group["fields"].setdefault(field, []).append(item)
                element_id = str(item.get("element_id") or "").strip()
                if element_id and element_id not in group["element_ids"]:
                    group["element_ids"].append(element_id)

        hinted_group = next(
            (
                item
                for item in sorted(
                    hint_groups.values(),
                    key=lambda value: (
                        -len(set(value.get("fields", {})) & set(required_fields)),
                        -len(value.get("element_ids", [])),
                        str(value.get("key", "")),
                    ),
                )
                if set(required_fields).issubset(item.get("fields", {}))
            ),
            None,
        )

        live_scan = int(scan.get("candidate_count", 0) or 0) >= 50
        initial_capture_limit = (
            min(_LIVE_FIELD_PROBE_LIMIT, max(candidate_limit, _LIVE_FIELD_PROBE_LIMIT))
            if live_scan and not explicit
            else candidate_limit
        )

        for field in normalized_fields:
            ids = explicit_ids(field)
            if ids:
                missing = [item for item in ids if item not in candidates_by_id]
                if missing:
                    selection_errors.append(
                        f"{field} element ids are not in the same page snapshot: {', '.join(missing)}"
                    )
                ids = [item for item in ids if item in candidates_by_id]
            elif hinted_group and field in hinted_group.get("fields", {}):
                ids = [
                    str(item.get("element_id") or "").strip()
                    for item in hinted_group["fields"][field][:candidate_limit]
                    if str(item.get("element_id") or "").strip()
                ]
            else:
                ids = candidate_ids(field, initial_capture_limit)
            selected_by_field[field] = list(dict.fromkeys(ids))
            for element_id in selected_by_field[field]:
                if element_id and element_id not in selected_ids:
                    selected_ids.append(element_id)

        records_by_id: dict[str, dict[str, Any]] = {}
        capture_errors: list[dict[str, str]] = []

        def capture_ids(ids: Iterable[str]) -> None:
            for element_id in ids:
                if element_id in records_by_id:
                    continue
                try:
                    captured = self.capture_element(
                        element_id,
                        page_title=page_title or scan.get("page_title"),
                        page_url=page_url or scan.get("page_url"),
                    )
                    record = captured.get("record")
                    if isinstance(record, dict):
                        records_by_id[element_id] = record
                except Exception as exc:
                    capture_errors.append({"element_id": element_id, "error": str(exc)})

        def build_groups(grouping: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
            groups: dict[str, dict[str, Any]] = {}
            field_candidates: dict[str, list[dict[str, Any]]] = {}
            for field, ids in selected_by_field.items():
                values: list[dict[str, Any]] = []
                for element_id in ids:
                    record = records_by_id.get(element_id)
                    if not record:
                        continue
                    link_context = record.get("link_context") if isinstance(record.get("link_context"), dict) else {}
                    if field == "link" and link_context.get("service_or_navigation"):
                        values.append(
                            {
                                "element_id": element_id,
                                "name": record.get("name", ""),
                                "rejected": True,
                                "reason": "selector is a customer-service or navigation link",
                                "card_context": record.get("card_context"),
                            }
                        )
                        continue
                    if field == "merchant" and _record_is_merchant_badge(record):
                        values.append(
                            {
                                "element_id": element_id,
                                "name": record.get("name", ""),
                                "rejected": True,
                                "reason": "selector is a JD fulfillment badge/image, not the merchant text",
                                "card_context": record.get("card_context"),
                            }
                        )
                        continue
                    card = record.get("card_context") if isinstance(record.get("card_context"), dict) else None
                    if card:
                        group_key = str(
                            card.get("family_key") or card.get("class") or card.get("marker") or card.get("key")
                            if grouping == "family"
                            else card.get("instance_key") or card.get("key") or "__ungrouped__"
                        )
                    else:
                        group_key = "__ungrouped__"
                    group = groups.setdefault(
                        group_key,
                        {
                            "key": group_key,
                            "grouping": grouping,
                            "fields": {},
                            "element_ids": [],
                        },
                    )
                    group["fields"].setdefault(field, []).append(record)
                    if element_id not in group["element_ids"]:
                        group["element_ids"].append(element_id)
                    values.append(
                        {
                            "element_id": element_id,
                            "name": record.get("name", ""),
                            "card_context": card,
                            "link_context": record.get("link_context"),
                            "rejected": False,
                        }
                    )
                field_candidates[field] = values
            return groups, field_candidates

        def rank_groups(groups: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            ranked = sorted(
                groups.values(),
                key=lambda item: (
                    -len(set(item.get("fields", {})) & set(required_fields)),
                    -int(str(item.get("key") or "") != "__ungrouped__"),
                    -len(item.get("element_ids", [])),
                    str(item.get("key", "")),
                ),
            )
            return ranked, [item for item in ranked if str(item.get("key") or "") != "__ungrouped__"]

        active_grouping = "instance" if card_grouping == "auto" else card_grouping
        capture_ids(selected_ids)
        groups, field_candidates = build_groups(active_grouping)
        ranked_groups, card_groups = rank_groups(groups)
        # UIA trees can place a value far away from the title in scan order.
        # Expand the union of gaps in the best few observed cards. Looking at
        # only the first card can miss a title or merchant that is ranked just
        # after the initial window, especially on virtualized result pages.
        expansion_rounds = 0
        expansion_limit = min(_LIVE_FIELD_EXPANSION_LIMIT, max(candidate_limit, 20))
        for _ in range(4):
            chosen = (card_groups or ranked_groups)[0] if (card_groups or ranked_groups) else None
            chosen_fields = chosen.get("fields", {}) if chosen else {}
            missing_fields = [item for item in required_fields if not chosen_fields.get(item)]
            if not missing_fields:
                break
            expansion_rounds += 1
            expansion_fields: list[str] = []
            observed_groups = card_groups[:5] if card_groups else ranked_groups[:5]
            for group in observed_groups:
                for field in required_fields:
                    if not group.get("fields", {}).get(field) and field not in expansion_fields:
                        expansion_fields.append(field)
            if not expansion_fields:
                expansion_fields = missing_fields
            expanded_ids: list[str] = []
            for field in expansion_fields:
                if explicit_ids(field):
                    continue
                expanded_ids.extend(append_candidate_ids(field, expansion_limit))
            expanded_ids = list(dict.fromkeys(expanded_ids))
            if not expanded_ids:
                break
            for element_id in expanded_ids:
                if element_id not in selected_ids:
                    selected_ids.append(element_id)
            capture_ids(expanded_ids)
            groups, field_candidates = build_groups(active_grouping)
            ranked_groups, card_groups = rank_groups(groups)
            expansion_limit = min(
                _LIVE_FIELD_EXPANSION_LIMIT,
                max(expansion_limit * 2, expansion_limit + 4),
            )

        # ``auto`` remains strict after the bounded search. A shared React/CSS
        # class identifies a repeated family, not one product instance; using
        # it here would silently mix title, merchant, price, and sales from
        # different rows. Build the relaxed view only as diagnostic guidance.
        family_ranked: list[dict[str, Any]] = []
        family_cards: list[dict[str, Any]] = []
        if card_grouping == "auto" and active_grouping == "instance":
            family_groups, family_candidates = build_groups("family")
            family_ranked, family_cards = rank_groups(family_groups)

        chosen = (card_groups or ranked_groups)[0] if (card_groups or ranked_groups) else None
        chosen_fields = chosen.get("fields", {}) if chosen else {}
        missing_fields = [item for item in required_fields if not chosen_fields.get(item)]
        selected_records: list[dict[str, Any]] = []
        selected_field_ids: dict[str, str] = {}
        if chosen:
            for field in normalized_fields:
                records = chosen_fields.get(field) or []
                if records:
                    record = records[0]
                    selected_records.append(record)
                    selected_field_ids[field] = str(record.get("id") or "")

        has_product_card = bool(chosen and str(chosen.get("key") or "") != "__ungrouped__")
        card_context_required = bool(set(required_fields) & {"product_name", "merchant", "price", "sales", "link"})
        if card_context_required and not has_product_card:
            selection_errors.append("selected page fields do not share a product-card context")
        complete = (
            not missing_fields
            and not capture_errors
            and not selection_errors
            and (not card_context_required or has_product_card)
        )
        selected_contexts = {
            field: (
                record.get("card_context")
                if isinstance(record, dict) and isinstance(record.get("card_context"), dict)
                else None
            )
            for field, record_list in chosen_fields.items()
            for record in record_list[:1]
        }
        semantic_warnings: list[str] = []
        for field_values in field_candidates.values():
            for candidate in field_values:
                if candidate.get("rejected") and candidate.get("reason"):
                    reason = str(candidate["reason"])
                    if reason not in semantic_warnings:
                        semantic_warnings.append(reason)
        quality_warnings: list[str] = []
        strict_complete = any(set(required_fields).issubset(item.get("fields", {})) for item in card_groups)
        family_complete = any(set(required_fields).issubset(item.get("fields", {})) for item in family_cards)
        if card_grouping == "auto" and active_grouping == "instance" and family_complete and not strict_complete:
            quality_warnings.append(
                "字段只能按重复商品卡片族对齐，未自动混用不同卡片；请重新捕获同一商品卡片的缺失字段。"
            )
        if semantic_warnings:
            quality_warnings.extend(semantic_warnings)
        pending_recapture_fields = list(
            dict.fromkeys(
                [*missing_fields]
                + [
                    str(field)
                    for field, values in field_candidates.items()
                    if any(item.get("rejected") for item in values)
                ]
            )
        )
        record_alignment = {
            "requested_grouping": card_grouping,
            "selected_grouping": active_grouping if has_product_card else None,
            "selector_scope": chosen.get("key") if has_product_card and chosen else None,
            "same_card_instance": bool(has_product_card and active_grouping == "instance" and not missing_fields),
            "same_card_family": bool(has_product_card and active_grouping == "family"),
            "field_card_contexts": selected_contexts,
            "captured_record_count": len(records_by_id),
            "initial_capture_limit": initial_capture_limit,
        }
        recovery_actions: list[dict[str, Any]] = []
        if "link" in missing_fields:
            recovery_actions.append(
                {
                    "field": "link",
                    "reason": "当前 DeepCapture 候选没有商品详情 href，客服/页脚链接已被排除。",
                    "tool": "shizai_rpa_add_step",
                    "component": "WebBrowser_RunJsInIeBySelector_v3",
                    "config_hint": {
                        "purpose": "在当前 Chrome 页面读取商品卡片的真实详情链接",
                        "output_variable": "商品链接",
                        "selector": ".plugin_goodsCardWrapper",
                    },
                    "next": "重新扫描并使用 JS 输出补充商品链接列，再继续 Excel 写入。",
                }
            )
        message = (
            "已在同一次 DeepCapture 会话中捕获并锁定同一商品卡片的字段，可以导入流程。"
            if complete
            else "页面字段捕获未形成完整同卡片集合；请根据 field_candidates 重新选择，商品链接不能使用客服或页脚链接。"
        )
        ui_feedback = _set_capture_ui_feedback(
            "ready" if complete else "waiting",
            "field_capture",
            message,
            animation="capture_complete" if complete else "capture_needs_review",
            overlay_state="active" if scan.get("capture_ready") else "inactive",
            field_count=len(selected_records),
            missing_fields=missing_fields,
        )
        return {
            "kind": "shizai_page_fields_captured",
            **_capture_read_metadata(),
            "fields": normalized_fields,
            "required_fields": required_fields,
            "complete": complete,
            "scan": scan,
            "scan_token": scan.get("scan_token"),
            "field_candidates": field_candidates,
            "groups": ranked_groups,
            "selected_group": chosen,
            "selected_field_ids": selected_field_ids,
            "elements": selected_records,
            "element_count": len(selected_records),
            "missing_fields": missing_fields,
            "expansion_rounds": expansion_rounds,
            "capture_window_per_field": expansion_limit,
            "selected_card_context": chosen.get("key") if has_product_card else None,
            "selected_card_grouping": active_grouping if has_product_card else None,
            "requested_card_grouping": card_grouping,
            "card_grouping_fallback": card_grouping == "auto" and active_grouping == "family",
            "quality_warning": "; ".join(quality_warnings) or None,
            "quality_warnings": quality_warnings,
            "semantic_warning": "; ".join(semantic_warnings) or None,
            "semantic_warnings": semantic_warnings,
            "pending_recapture_fields": pending_recapture_fields,
            "selector_scope": chosen.get("key") if has_product_card and chosen else None,
            "record_alignment": record_alignment,
            "recovery_actions": recovery_actions,
            "selection_errors": selection_errors,
            "capture_errors": capture_errors,
            "ui_feedback": ui_feedback,
            "message": message,
        }


def open_current_chrome(url: str, *, wait_seconds: float = 1.0) -> dict[str, Any]:
    """Open URL in the user's normal Chrome profile for design-time capture."""
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    executable = _find_chrome_executable()
    if executable is None:
        candidates = [str(item) for item in _chrome_executable_candidates()]
        hint = "; ".join(candidates[:4])
        raise ShizaiCloudError("未找到 Google Chrome；请安装 Chrome 后重试。" + (f" 已检查: {hint}" if hint else ""))
    try:
        process = subprocess.Popen([str(executable), url], **_detached_browser_popen_kwargs())
    except OSError as exc:
        raise ShizaiCloudError(f"打开当前 Chrome 失败: {exc}") from exc
    if wait_seconds > 0:
        time.sleep(min(float(wait_seconds), 10.0))
    ui_feedback = _set_capture_ui_feedback(
        "browser_ready",
        "browser_prepare",
        "目标页面已在当前 Chrome 配置中打开，可以先写入元素组件并扫描页面。",
        overlay_state="inactive",
        animation="browser_ready",
        url=url,
    )
    return {
        "opened": True,
        "url": url,
        "browser": "chrome",
        "pid": process.pid,
        "executable": str(executable),
        "profile": "current-user-profile",
        "design_time_helper": True,
        "message": "已在用户当前 Chrome 配置中打开页面；先读取窗口和页面树，写入 unbound 元素组件后再按需启动官方拾取。",
        "ui_feedback": ui_feedback,
    }
