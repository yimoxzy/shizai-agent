"""Native UI bridges for the installed Shizai desktop application.

The small card and the page mask are renderers owned by the desktop
application. Opening them is a normal business RPC, while changing their
state is a renderer-to-renderer call that must stay on the same authenticated
local WebSocket session.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent, _desktop_channel_lock

SMALL_CARD_SERVICE = "SmallCardService"
SMALL_CARD_TARGET = "$SmallCardClientId$"
SMALL_CARD_UPDATE_METHOD = "updateSmallCardStepText"
SMALL_CARD_OPEN_METHOD = "openSmallCardWindow"
SMALL_CARD_CLOSE_METHOD = "closeSmallCardWindow"

# The open RPC is acknowledged before the renderer call can be sent. A short
# settle period is enough; a full second on every card open made multi-step
# MCP design noticeably slower.
SMALL_CARD_OPEN_SETTLE_SECONDS = 0.2

# The complete run mask is owned by the main-process service.  It fans the
# request out to the home page, active editor, and open dialog renderers.
MASK_SERVICE = "MaskAppService"
MASK_TARGET = None
MASK_SHOW_METHOD = "showRunMask"
MASK_CLOSE_METHOD = "closeRunMask"
MASK_RENDERER_SERVICE = "MaskService"
MASK_RENDERER_CLOSE_METHOD = "closeMaskV2"
# The installed MaskAppService facade drops ``force`` before routing to
# renderers. These are the aliases used by the desktop force-close path.
MASK_FORCE_RENDERER_TARGETS = (
    "$HomeClientId$",
    "$AgentBrainClientId$",
    "$CurrentActiveEditorClientId$",
)
MASK_TYPES = ("gray", "transparent", "white", "whiteWithSlot")
SHOW_MAIN_WINDOW_METHOD = "showMainWindow"
WINDOW_MANAGER_SERVICE = "WindowManagerService"
MINIMIZE_WINDOW_METHOD = "minimizeWindow"
RESTORE_WINDOW_METHOD = "restoreWindowWithDelay"
LOADING_WINDOW_OPEN_METHOD = "openLoadingWindow"
LOADING_WINDOW_CLOSE_METHOD = "closeLoadingWindow"
NATIVE_OPERATE_SERVICE = "NativeOperateService"
TRANSFER_PLUGIN_ACTION_METHOD = "transferPluginAction"
SCREENSHOT_PLUGIN_ACTION = "Element.ScreenshotCapture"
SCREENSHOT_REQUESTER = "agentScreenShot"
MASK_OPERATE_FROM = ("home", "normal")
NATIVE_SCREENSHOT_CONTEXT_ERROR_CODE = "40009995"
ACTIVE_EDITOR_TARGET = "$CurrentActiveEditorClientId$"

SMALL_CARD_MESSAGES: dict[str, str] = {
    "takeover": "正在接管当前电脑环境。",
    "designing": "正在制定流程设计方案。",
    "scanning": "正在扫描页面并分析可用元素。",
    "capturing": "正在捕获网页元素。",
    "validating": "正在校验组件参数和元素绑定。",
    "running": "正在执行流程组件。",
    "waiting": "正在等待页面或运行结果。",
    "ready": "流程步骤已准备就绪。",
    "success": "流程执行完成。",
    "error": "流程执行遇到问题。",
}
SMALL_CARD_STATES = tuple(SMALL_CARD_MESSAGES)
SMALL_CARD_DEMO_STATES = ("takeover", "designing", "scanning", "capturing", "validating", "success")


def _failure_detail(result: Any, operation: str) -> str | None:
    if not isinstance(result, dict) or result.get("isSuccess") is not False:
        return None
    detail = str(result.get("message") or f"{operation} failed")
    code = result.get("code")
    if code not in (None, ""):
        detail = f"{detail} (code={code})"
    return detail


def _cleanup_failure_detail(result: Any, operation: str) -> str | None:
    """A missing or rejected acknowledgement cannot prove a surface is hidden."""
    if isinstance(result, dict) and result.get("isSuccess") is True:
        return None
    return _failure_detail(result, operation) or f"{operation} returned no successful cleanup acknowledgement"


def _native_ui_failure_detail(detail: str, *, service: str, method: str) -> str:
    """Add context to the designer's opaque screenshot-picker rejection."""
    if (
        service == NATIVE_OPERATE_SERVICE
        and method == TRANSFER_PLUGIN_ACTION_METHOD
        and NATIVE_SCREENSHOT_CONTEXT_ERROR_CODE in detail
    ):
        return (
            f"{detail}；设计器拒绝了原生截图拾取动作 "
            f"({SCREENSHOT_PLUGIN_ACTION})。请从支持‘截图拾取/选元素’的流程编辑入口触发，"
            "并确认目标流程和当前 Chrome 页面已打开后重试。MCP 仅复用设计器原版 UI。"
        )
    return detail


class _NativeUiChannel:
    """Shared authenticated RPC channel for native designer UI renderers."""

    def __init__(self, client: ShizaiDesktopAgent | None = None) -> None:
        self.client = client or ShizaiDesktopAgent()

    def _call_method(
        self,
        sock: Any,
        method: str,
        args: list[Any],
        *,
        strict: bool,
        service: str = "WindowManagerService",
    ) -> Any:
        call_id = f"cli-{uuid.uuid4()}"
        sock.send(
            json.dumps(
                {
                    "packetType": "methodCall",
                    "body": {
                        "callId": call_id,
                        "serviceName": service,
                        "methodName": method,
                        "args": args,
                    },
                },
                ensure_ascii=False,
            )
        )
        result = self._wait_for(sock, call_id, "callResult")
        detail = _failure_detail(result, method)
        if detail and strict:
            raise ShizaiCloudError(_native_ui_failure_detail(detail, service=service, method=method))
        return result

    def _call_renderer(
        self,
        sock: Any,
        *,
        target_client_id: str,
        service: str,
        method: str,
        args: list[Any],
        response_timeout: float = 15.0,
    ) -> Any:
        call_id = f"cli-{uuid.uuid4()}"
        sock.send(
            json.dumps(
                {
                    "packetType": "render-call-render-req",
                    "body": {
                        "callId": call_id,
                        "requestClientId": self.client.config.client_id,
                        "targetClientId": target_client_id,
                        "serviceName": service,
                        "methodName": method,
                        "args": args,
                    },
                },
                ensure_ascii=False,
            )
        )
        result = self._wait_for(sock, call_id, "render-call-render-res", timeout=response_timeout)
        detail = _failure_detail(result, method)
        if detail:
            raise ShizaiCloudError(detail)
        return result

    @staticmethod
    def _wait_for(sock: Any, call_id: str, packet_type: str, timeout: float = 15.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = sock.recv()
            except Exception as exc:
                if type(exc).__name__ in {"WebSocketTimeoutException", "TimeoutError", "socket.timeout"}:
                    continue
                raise ShizaiCloudError(f"Native UI call failed: {exc}") from exc
            if raw in (None, "", b""):
                continue
            try:
                packet = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            body = packet.get("body") or {}
            current_type = packet.get("packetType")
            if current_type == packet_type and body.get("callId") == call_id:
                if body.get("error"):
                    return {"isSuccess": False, "message": str(body["error"])}
                if body.get("result") is not None:
                    return body.get("result")
                # Renderer methods such as updateSmallCardStepText return
                # void. Their successful response carries only error=null.
                if "error" in body and not body.get("error"):
                    return {"isSuccess": True, "code": 0, "data": None}
                return {"isSuccess": False, "message": str(body.get("error") or "Renderer call returned no result")}
            if current_type == "node-call-render-req" and body.get("callId"):
                sock.send(
                    json.dumps(
                        {
                            "packetType": "node-call-render-res",
                            "body": {
                                "callId": body["callId"],
                                "result": {"isSuccess": True, "code": 0, "message": "ok", "data": None},
                            },
                        }
                    )
                )
        raise ShizaiCloudError(f"Native UI call timed out: {packet_type}")


def call_active_editor_renderer(
    client: ShizaiDesktopAgent,
    service: str,
    method: str,
    args: list[Any] | None = None,
    *,
    acquire_lock: bool = True,
    response_timeout: float = 15.0,
) -> Any:
    """Call a service exposed by the currently active package editor."""
    channel = _NativeUiChannel(client)
    effective_timeout = max(0.25, min(float(response_timeout), 30.0))
    def call() -> Any:
        sock = client._connect(effective_timeout)
        try:
            return channel._call_renderer(
                sock,
                target_client_id=ACTIVE_EDITOR_TARGET,
                service=service,
                method=method,
                args=args or [],
                response_timeout=effective_timeout,
            )
        finally:
            sock.close()
    if not acquire_lock:
        return call()
    with _desktop_channel_lock(client.config.timeout, f"active editor {service}.{method}"):
        return call()


class NativeScreenshotCapture(_NativeUiChannel):
    """Drive the designer's original screenshot element-picker protocol."""

    def start(
        self,
        *,
        request_id: str | None = None,
        run_type: str = "common",
    ) -> dict[str, Any]:
        """Minimize the designer and start native screenshot element picking."""
        capture_id = str(request_id or uuid.uuid4().hex[:10]).strip()
        normalized_run_type = self._normalize_run_type(run_type)
        with _desktop_channel_lock(self.client.config.timeout, "native screenshot capture"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                return self.start_on_socket(
                    sock,
                    request_id=capture_id,
                    run_type=normalized_run_type,
                )
            finally:
                sock.close()

    def stop(
        self,
        *,
        request_id: str | None = None,
        run_type: str = "common",
    ) -> dict[str, Any]:
        """Stop native screenshot element picking and restore designer windows."""
        capture_id = str(request_id or uuid.uuid4().hex[:10]).strip()
        normalized_run_type = self._normalize_run_type(run_type)
        with _desktop_channel_lock(self.client.config.timeout, "native screenshot capture stop"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                return self.stop_on_socket(
                    sock,
                    request_id=capture_id,
                    run_type=normalized_run_type,
                )
            finally:
                sock.close()

    def start_on_socket(
        self,
        sock: Any,
        *,
        request_id: str,
        run_type: str = "common",
    ) -> dict[str, Any]:
        """Start the picker on an already authenticated native UI socket."""
        normalized_run_type = self._normalize_run_type(run_type)
        minimized = self._call_method(
            sock,
            MINIMIZE_WINDOW_METHOD,
            [{"requesterName": SCREENSHOT_REQUESTER, "excepts": []}],
            strict=True,
            service=WINDOW_MANAGER_SERVICE,
        )
        started = self._transfer_on_socket(
            sock,
            request_id=request_id,
            action_cmd="start",
            run_type=normalized_run_type,
        )
        return {
            "plugin_action": SCREENSHOT_PLUGIN_ACTION,
            "request_id": request_id,
            "run_type": normalized_run_type,
            "minimize_result": minimized,
            "start_result": started,
            "active": True,
        }

    def stop_on_socket(
        self,
        sock: Any,
        *,
        request_id: str,
        run_type: str = "common",
    ) -> dict[str, Any]:
        """Stop the picker and restore the windows hidden by the start call."""
        normalized_run_type = self._normalize_run_type(run_type)
        restored = self._call_method(
            sock,
            RESTORE_WINDOW_METHOD,
            [{"requesterName": SCREENSHOT_REQUESTER}],
            strict=False,
            service=WINDOW_MANAGER_SERVICE,
        )
        stopped = self._transfer_on_socket(
            sock,
            request_id=request_id,
            action_cmd="stop",
            run_type=normalized_run_type,
        )
        return {
            "plugin_action": SCREENSHOT_PLUGIN_ACTION,
            "request_id": request_id,
            "run_type": normalized_run_type,
            "restore_result": restored,
            "stop_result": stopped,
            "active": False,
        }

    def _transfer_on_socket(
        self,
        sock: Any,
        *,
        request_id: str,
        action_cmd: str,
        run_type: str,
    ) -> Any:
        json_params: dict[str, Any] = {
            "requestId": request_id,
            "actionCmd": action_cmd,
        }
        if action_cmd == "start":
            json_params["memoryInfo"] = {}
        return self._call_method(
            sock,
            TRANSFER_PLUGIN_ACTION_METHOD,
            [
                {
                    "pluginAction": SCREENSHOT_PLUGIN_ACTION,
                    "jsonParams": json_params,
                    "runType": run_type,
                }
            ],
            strict=True,
            service=NATIVE_OPERATE_SERVICE,
        )

    @staticmethod
    def _normalize_run_type(run_type: str) -> str:
        value = str(run_type or "common").strip().lower()
        if value not in {"common", "virtual"}:
            raise ValueError("run_type must be common or virtual")
        return value


class NativeSmallCard(_NativeUiChannel):
    """Control the original Tars small card without creating a new window."""

    def update(
        self,
        *,
        state: str = "designing",
        message: str | None = None,
        duration: float = 0,
    ) -> dict[str, Any]:
        """Update an existing card without asking the window manager to reopen it."""
        normalized_state = self._normalize_state(state)
        if normalized_state == "hidden":
            return self.close()
        text = str(message or SMALL_CARD_MESSAGES[normalized_state]).strip()
        if not text:
            raise ValueError("message must not be empty")
        with _desktop_channel_lock(self.client.config.timeout, "native small card update"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                updated = self._call_renderer(sock, text)
                result: dict[str, Any] = {
                    "kind": "shizai_native_small_card",
                    "state": normalized_state,
                    "message": text,
                    "visible": True,
                    "animation": "native_tars_lottie_loop",
                    "update_result": updated,
                }
                if duration > 0:
                    time.sleep(float(duration))
                return result
            finally:
                sock.close()

    def show(
        self,
        *,
        state: str = "designing",
        message: str | None = None,
        duration: float = 0,
        close_after: bool = False,
    ) -> dict[str, Any]:
        """Open/update the card and optionally keep it visible for a duration."""
        normalized_state = self._normalize_state(state)
        if normalized_state == "hidden":
            return self.close()
        text = str(message or SMALL_CARD_MESSAGES[normalized_state]).strip()
        if not text:
            raise ValueError("message must not be empty")
        with _desktop_channel_lock(self.client.config.timeout, "native small card"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                opened = self._call_method(sock, SMALL_CARD_OPEN_METHOD, [], strict=False)
                # The window manager can report an already-open card as a
                # business failure.  The renderer update is still the useful
                # operation, so continue and expose the warning to the caller.
                time.sleep(SMALL_CARD_OPEN_SETTLE_SECONDS)
                updated = self._call_renderer(sock, text)
                open_warning = _failure_detail(opened, "openSmallCardWindow")
                renderer_ready = _failure_detail(updated, "updateSmallCardStepText") is None
                result: dict[str, Any] = {
                    "kind": "shizai_native_small_card",
                    "state": normalized_state,
                    "message": text,
                    "visible": True,
                    "animation": "native_tars_lottie_loop",
                    "open_result": opened,
                    "update_result": updated,
                    "reused_window": bool(open_warning and renderer_ready),
                }
                if open_warning and renderer_ready:
                    result["open_nonfatal"] = open_warning
                    result["open_result_status"] = "reused_window"
                elif open_warning:
                    result["open_warning"] = open_warning
                if duration > 0:
                    time.sleep(float(duration))
                if close_after:
                    result["close_result"] = self._call_method(sock, SMALL_CARD_CLOSE_METHOD, [], strict=False)
                    result["visible"] = False
                return result
            finally:
                sock.close()

    def demo(self, *, interval: float = 1.5) -> dict[str, Any]:
        """Play the native card's design-state sequence once."""
        interval = max(0.1, float(interval))
        messages = [SMALL_CARD_MESSAGES[state] for state in SMALL_CARD_DEMO_STATES]
        with _desktop_channel_lock(self.client.config.timeout, "native small card demo"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                opened = self._call_method(sock, SMALL_CARD_OPEN_METHOD, [], strict=False)
                updates: list[dict[str, Any]] = []
                for state, text in zip(SMALL_CARD_DEMO_STATES, messages):
                    updates.append({"state": state, "result": self._call_renderer(sock, text)})
                    time.sleep(interval)
                closed = self._call_method(sock, SMALL_CARD_CLOSE_METHOD, [], strict=False)
                return {
                    "kind": "shizai_native_small_card_demo",
                    "states": list(SMALL_CARD_DEMO_STATES),
                    "animation": "native_tars_lottie_loop",
                    "open_result": opened,
                    "updates": updates,
                    "close_result": closed,
                    "visible": False,
                }
            finally:
                sock.close()

    def countdown(
        self,
        *,
        seconds: int = 5,
        message_template: str = "塔斯将在 {seconds} 秒后接管你的电脑。",
        ensure_open: bool = True,
    ) -> dict[str, Any]:
        """Show one native pre-takeover countdown without reopening the card."""
        duration = max(1, min(int(seconds), 60))
        template = str(message_template or "").strip()
        if "{seconds}" not in template:
            raise ValueError("message_template must include {seconds}")
        with _desktop_channel_lock(self.client.config.timeout, "native small card countdown"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                opened = None
                if ensure_open:
                    opened = self._call_method(sock, SMALL_CARD_OPEN_METHOD, [], strict=False)
                    time.sleep(SMALL_CARD_OPEN_SETTLE_SECONDS)
                updates: list[dict[str, Any]] = []
                for remaining in range(duration, 0, -1):
                    text = template.format(seconds=remaining)
                    updates.append(
                        {
                            "seconds": remaining,
                            "message": text,
                            "result": self._call_renderer(sock, text),
                        }
                    )
                    time.sleep(1.0)
                result = {
                    "kind": "shizai_native_takeover_countdown",
                    "seconds": duration,
                    "messages": [item["message"] for item in updates],
                    "updates": updates,
                    "visible": True,
                    "animation": "native_tars_lottie_loop",
                    "open_result": opened,
                    "opened_window": bool(ensure_open),
                }
                open_warning = _failure_detail(opened, "openSmallCardWindow")
                if open_warning:
                    # Renderer updates are usable evidence that an existing
                    # card is available even when open reports code=5000.
                    result["reused_window"] = True
                    result["open_nonfatal"] = open_warning
                    result["open_result_status"] = "reused_window"
                return result
            finally:
                sock.close()

    def close(self) -> dict[str, Any]:
        """Close the original small card window idempotently."""
        with _desktop_channel_lock(self.client.config.timeout, "native small card close"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                closed = self._call_method(sock, SMALL_CARD_CLOSE_METHOD, [], strict=False)
                result: dict[str, Any] = {
                    "kind": "shizai_native_small_card",
                    "state": "hidden",
                    "visible": False,
                    "close_result": closed,
                }
                warning = _cleanup_failure_detail(closed, "closeSmallCardWindow")
                result["cleanup_complete"] = warning is None
                if warning:
                    result.update({"visible": None, "state": "unknown", "close_warning": warning})
                return result
            finally:
                sock.close()

    @staticmethod
    def _normalize_state(state: str) -> str:
        value = str(state or "designing").strip().lower()
        if value == "hidden":
            return value
        if value not in SMALL_CARD_MESSAGES:
            choices = ", ".join((*SMALL_CARD_STATES, "hidden"))
            raise ValueError(f"unknown small-card state {state!r}; choose one of: {choices}")
        return value

    def _call_method(self, sock: Any, method: str, args: list[Any], *, strict: bool) -> Any:
        return super()._call_method(sock, method, args, strict=strict)

    def _call_renderer(self, sock: Any, text: str) -> Any:
        return super()._call_renderer(
            sock,
            target_client_id=SMALL_CARD_TARGET,
            service=SMALL_CARD_SERVICE,
            method=SMALL_CARD_UPDATE_METHOD,
            args=[text],
        )


class NativeMask(_NativeUiChannel):
    """Control the designer's complete run mask through ``MaskAppService``."""

    def show(
        self,
        *,
        mask_type: str = "white",
        request_id: str | None = None,
        tips: str | None = None,
        mask_all: bool = True,
        duration: float = 0,
        close_after: bool = False,
        enable_selector_when_run: list[str] | None = None,
        transparent: bool | None = None,
        activate: bool = True,
        operate_from: str = "normal",
        run_type: str = "run",
        is_show_header: bool = False,
    ) -> dict[str, Any]:
        """Show the native run mask and optionally close it after ``duration``."""
        normalized_type = self._normalize_mask_type(mask_type)
        normalized_operate_from = self._normalize_operate_from(operate_from)
        normalized_run_type = str(run_type or "run").strip().lower()
        if normalized_run_type not in {"run", "debug"}:
            raise ValueError("run_type must be run or debug")
        payload: dict[str, Any] = {
            "operateFrom": normalized_operate_from,
            "runType": normalized_run_type,
            "tips": str(tips or "").strip(),
            "transparent": bool(transparent) if transparent is not None else False,
            "isShowHeader": bool(is_show_header),
        }
        with _desktop_channel_lock(self.client.config.timeout, "native mask"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                activated = self._call_method(sock, SHOW_MAIN_WINDOW_METHOD, [], strict=False) if activate else None
                shown = self._call_method(
                    sock,
                    MASK_SHOW_METHOD,
                    [payload],
                    strict=True,
                    service=MASK_SERVICE,
                )
                actual_request_id = self._request_id_from_result(shown) or str(request_id or "").strip() or None
                result: dict[str, Any] = {
                    "kind": "shizai_native_mask",
                    "mask_type": normalized_type,
                    "request_id": actual_request_id,
                    "mask_all": normalized_operate_from == "home",
                    "operate_from": normalized_operate_from,
                    "run_type": normalized_run_type,
                    "visible": True,
                    "animation": "native_run_mask",
                    "activate": bool(activate),
                    "activate_result": activated,
                    "show_result": shown,
                }
                warning = _failure_detail(activated, SHOW_MAIN_WINDOW_METHOD)
                if warning:
                    result["activate_warning"] = warning
                if duration > 0:
                    time.sleep(float(duration))
                if close_after:
                    result["close_result"] = self._close_on_socket(
                        sock,
                        request_id=actual_request_id or str(request_id or "").strip(),
                        force=False,
                    )
                    result["visible"] = False
                return result
            finally:
                sock.close()

    def close(self, *, request_id: str | None = None, force: bool = True) -> dict[str, Any]:
        """Close a mask; ``force`` makes cleanup idempotent across CLI calls."""
        with _desktop_channel_lock(self.client.config.timeout, "native mask close"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                normalized_request_id = str(request_id or "").strip()
                payload: dict[str, Any] = {"requestId": normalized_request_id}
                if force:
                    # The renderer ignores an unknown/empty request id unless
                    # its documented force flag reaches closeMaskV2.
                    payload["force"] = True
                closed = self._call_method(
                    sock,
                    MASK_CLOSE_METHOD,
                    [payload],
                    strict=False,
                    service=MASK_SERVICE,
                )
                renderer_closes: list[dict[str, Any]] = []
                if force:
                    # Repeat the vendor renderer fan-out so a stale tip is
                    # cleared even when the generated request id is gone.
                    for target in MASK_FORCE_RENDERER_TARGETS:
                        try:
                            renderer_closes.append(
                                {
                                    "target": target,
                                    "result": self._call_renderer(
                                        sock,
                                        target_client_id=target,
                                        service=MASK_RENDERER_SERVICE,
                                        method=MASK_RENDERER_CLOSE_METHOD,
                                        args=[{"requestId": normalized_request_id, "force": True}],
                                    ),
                                }
                            )
                        except Exception as exc:
                            renderer_closes.append({"target": target, "warning": str(exc)})
                result = {
                    "kind": "shizai_native_mask",
                    "request_id": normalized_request_id or None,
                    "visible": False,
                    "force": bool(force),
                    "close_result": closed,
                    "renderer_closes": renderer_closes,
                    "animation": "native_run_mask",
                }
                warnings = []
                warning = _cleanup_failure_detail(closed, MASK_CLOSE_METHOD)
                if warning:
                    warnings.append(warning)
                for item in renderer_closes:
                    warning = item.get("warning") or _cleanup_failure_detail(
                        item.get("result"), f"{MASK_RENDERER_CLOSE_METHOD} ({item['target']})"
                    )
                    if warning:
                        warnings.append(warning)
                result["cleanup_complete"] = not warnings
                if warnings:
                    result.update({"visible": None, "close_warning": "; ".join(warnings)})
                return result
            finally:
                sock.close()

    def _close_on_socket(self, sock: Any, *, request_id: str, force: bool) -> Any:
        return self._call_method(
            sock,
            MASK_CLOSE_METHOD,
            [{"requestId": request_id}],
            strict=False,
            service=MASK_SERVICE,
        )

    @staticmethod
    def _request_id_from_result(result: Any) -> str | None:
        if isinstance(result, dict):
            value = result.get("data")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for key in ("requestId", "request_id", "id"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        return None

    @staticmethod
    def _normalize_mask_type(mask_type: str) -> str:
        value = str(mask_type or "white").strip()
        if value not in MASK_TYPES:
            raise ValueError(f"unknown mask type {mask_type!r}; choose one of: {', '.join(MASK_TYPES)}")
        return value

    @staticmethod
    def _normalize_operate_from(operate_from: str) -> str:
        value = str(operate_from or "normal").strip().lower()
        if value not in MASK_OPERATE_FROM:
            raise ValueError(f"unknown operate_from {operate_from!r}; choose one of: {', '.join(MASK_OPERATE_FROM)}")
        return value


class NativeTakeoverFrame(_NativeUiChannel):
    """Control the official full-screen blue-purple breathing frame."""

    def show(self) -> dict[str, Any]:
        with _desktop_channel_lock(self.client.config.timeout, "native takeover frame"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                opened = self._call_method(sock, LOADING_WINDOW_OPEN_METHOD, [], strict=False)
                recovered = False
                initial_warning = _failure_detail(opened, LOADING_WINDOW_OPEN_METHOD)
                cleanup_result: Any = None
                if initial_warning:
                    cleanup_result = self._call_method(
                        sock,
                        LOADING_WINDOW_CLOSE_METHOD,
                        [],
                        strict=False,
                    )
                    time.sleep(0.2)
                    opened = self._call_method(sock, LOADING_WINDOW_OPEN_METHOD, [], strict=False)
                    recovered = _failure_detail(opened, LOADING_WINDOW_OPEN_METHOD) is None
                warning = _failure_detail(opened, LOADING_WINDOW_OPEN_METHOD)
                result: dict[str, Any] = {
                    "kind": "shizai_native_takeover_frame",
                    "visible": warning is None,
                    "animation": "native_blue_purple_breathing_frame",
                    "open_result": opened,
                    "recovered_stale_window": recovered,
                }
                if cleanup_result is not None:
                    result["recovery_close_result"] = cleanup_result
                if warning:
                    result["open_warning"] = warning
                if initial_warning:
                    result["initial_open_warning"] = initial_warning
                return result
            finally:
                sock.close()

    def close(self) -> dict[str, Any]:
        with _desktop_channel_lock(self.client.config.timeout, "native takeover frame close"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            try:
                closed = self._call_method(sock, LOADING_WINDOW_CLOSE_METHOD, [], strict=False)
                result: dict[str, Any] = {
                    "kind": "shizai_native_takeover_frame",
                    "visible": False,
                    "animation": "native_blue_purple_breathing_frame",
                    "close_result": closed,
                }
                warning = _cleanup_failure_detail(closed, LOADING_WINDOW_CLOSE_METHOD)
                result["cleanup_complete"] = warning is None
                if warning:
                    result["visible"] = None
                    result["close_warning"] = warning
                return result
            finally:
                sock.close()


class NativeScanDemo(_NativeUiChannel):
    """Play the designer's original screenshot-picking feedback sequence."""

    def run(
        self,
        *,
        interval: float = 1.5,
        mask_type: str = "white",
        close_after: bool = True,
        activate: bool = True,
        run_type: str = "common",
    ) -> dict[str, Any]:
        """Use the native mask, screenshot picker, and TARS card on one socket."""
        interval = max(0.1, float(interval))
        normalized_type = NativeMask._normalize_mask_type(mask_type)
        normalized_run_type = NativeScreenshotCapture._normalize_run_type(run_type)
        capture_request_id = uuid.uuid4().hex[:10]
        mask_request_id = f"cli-{uuid.uuid4()}"
        mask_tips = "TARS 正在扫描当前页面..."
        with _desktop_channel_lock(self.client.config.timeout, "native scan demo"):
            sock = self.client._connect(max(self.client.config.timeout, 5.0))
            mask = NativeMask(client=self.client)
            screenshot = NativeScreenshotCapture(client=self.client)
            small_card = NativeSmallCard(client=self.client)
            result: dict[str, Any] | None = None
            cleanup_errors: list[str] = []
            card_close: Any = None
            mask_close: Any = None
            screenshot_stop: Any = None
            restore_result: Any = None
            mask_open: Any = None
            screenshot_start: Any = None
            card_open: Any = None
            mask_started = False
            screenshot_started = False
            card_started = False
            minimized = False
            stage = "connect"
            stage_error: str | None = None
            try:
                # This is the same order used by the installed screenshot
                # picker: hide designer windows before showing the mask.
                stage = "takeover"
                activated = mask._call_method(sock, SHOW_MAIN_WINDOW_METHOD, [], strict=False) if activate else None
                stage = "minimize_windows"
                minimized_result = screenshot._call_method(
                    sock,
                    MINIMIZE_WINDOW_METHOD,
                    [{"requesterName": SCREENSHOT_REQUESTER, "excepts": []}],
                    strict=True,
                    service=WINDOW_MANAGER_SERVICE,
                )
                minimized = True
                stage = "show_mask"
                mask_started = True
                mask_open = mask._call_method(
                    sock,
                    MASK_SHOW_METHOD,
                    [
                        {
                            "operateFrom": "normal",
                            "runType": "run",
                            "tips": mask_tips,
                            "transparent": False,
                            "isShowHeader": False,
                        }
                    ],
                    strict=True,
                    service=MASK_SERVICE,
                )
                mask_request_id = mask._request_id_from_result(mask_open) or mask_request_id
                time.sleep(0.1)
                stage = "start_screenshot_capture"
                screenshot_start = screenshot._transfer_on_socket(
                    sock,
                    request_id=capture_request_id,
                    action_cmd="start",
                    run_type=normalized_run_type,
                )
                screenshot_started = True
                stage = "open_small_card"
                card_started = True
                card_open = small_card._call_method(sock, SMALL_CARD_OPEN_METHOD, [], strict=False)
                time.sleep(SMALL_CARD_OPEN_SETTLE_SECONDS)
                updates: list[dict[str, Any]] = []
                for state in SMALL_CARD_DEMO_STATES:
                    stage = state
                    updates.append(
                        {
                            "state": state,
                            "result": small_card._call_renderer(sock, SMALL_CARD_MESSAGES[state]),
                        }
                    )
                    time.sleep(interval)
                result = {
                    "kind": "shizai_native_scan_demo",
                    "mask": {
                        "mask_type": normalized_type,
                        "request_id": mask_request_id,
                        "animation": "native_run_mask",
                        "show_result": mask_open,
                    },
                    "screenshot_capture": {
                        "plugin_action": SCREENSHOT_PLUGIN_ACTION,
                        "request_id": capture_request_id,
                        "run_type": normalized_run_type,
                        "animation": "native_element_screenshot_capture",
                        "minimize_result": minimized_result,
                        "start_result": screenshot_start,
                    },
                    "small_card": {
                        "states": list(SMALL_CARD_DEMO_STATES),
                        "animation": "native_tars_lottie_loop",
                        "open_result": card_open,
                        "updates": updates,
                    },
                    "activate": bool(activate),
                    "activate_result": activated,
                    "reused_window": True,
                    "uses_current_profile": True,
                    "visible": not close_after,
                }
            except Exception as exc:
                stage_error = str(exc)
            finally:
                # Cleanup stays on the authenticated socket and follows the
                # installed app's cancel flow: restore, close mask, stop picker.
                if close_after or result is None:
                    if minimized:
                        try:
                            restore_result = screenshot._call_method(
                                sock,
                                RESTORE_WINDOW_METHOD,
                                [{"requesterName": SCREENSHOT_REQUESTER}],
                                strict=False,
                                service=WINDOW_MANAGER_SERVICE,
                            )
                            minimized = False
                        except Exception as exc:
                            cleanup_errors.append(f"restore windows: {exc}")
                    if mask_started:
                        try:
                            mask_close = mask._close_on_socket(sock, request_id=mask_request_id, force=False)
                        except Exception as exc:
                            cleanup_errors.append(f"mask: {exc}")
                    if screenshot_started:
                        try:
                            screenshot_stop = screenshot._transfer_on_socket(
                                sock,
                                request_id=capture_request_id,
                                action_cmd="stop",
                                run_type=normalized_run_type,
                            )
                        except Exception as exc:
                            cleanup_errors.append(f"screenshot capture: {exc}")
                    if card_started:
                        try:
                            card_close = small_card._call_method(sock, SMALL_CARD_CLOSE_METHOD, [], strict=False)
                        except Exception as exc:
                            cleanup_errors.append(f"small card: {exc}")
                sock.close()
            if result is None:
                if cleanup_errors:
                    detail = "native scan demo cleanup failed: " + "; ".join(cleanup_errors)
                    if stage_error:
                        detail = f"native scan demo failed at {stage}: {stage_error}; {detail}"
                    raise ShizaiCloudError(detail)
                if stage_error:
                    raise ShizaiCloudError(f"native scan demo failed at {stage}: {stage_error}")
                raise ShizaiCloudError("native scan demo failed")
            if close_after:
                result["screenshot_capture"]["restore_result"] = restore_result
                result["screenshot_capture"]["stop_result"] = screenshot_stop
                result["screenshot_capture"]["active"] = False
                result["small_card"]["close_result"] = card_close
                result["mask"]["close_result"] = mask_close
                result["visible"] = False
            if cleanup_errors:
                result["cleanup_warnings"] = cleanup_errors
            return result


__all__ = [
    "MASK_CLOSE_METHOD",
    "MASK_SERVICE",
    "MASK_SHOW_METHOD",
    "MASK_TARGET",
    "MASK_TYPES",
    "MASK_OPERATE_FROM",
    "SHOW_MAIN_WINDOW_METHOD",
    "WINDOW_MANAGER_SERVICE",
    "MINIMIZE_WINDOW_METHOD",
    "RESTORE_WINDOW_METHOD",
    "NATIVE_OPERATE_SERVICE",
    "TRANSFER_PLUGIN_ACTION_METHOD",
    "SCREENSHOT_PLUGIN_ACTION",
    "SCREENSHOT_REQUESTER",
    "NativeMask",
    "NativeScreenshotCapture",
    "NativeScanDemo",
    "NativeSmallCard",
    "SMALL_CARD_CLOSE_METHOD",
    "SMALL_CARD_DEMO_STATES",
    "SMALL_CARD_MESSAGES",
    "SMALL_CARD_OPEN_METHOD",
    "SMALL_CARD_SERVICE",
    "SMALL_CARD_STATES",
    "SMALL_CARD_TARGET",
    "SMALL_CARD_UPDATE_METHOD",
]
