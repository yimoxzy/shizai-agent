"""Regression coverage for cleanup acknowledgements, retries and project identity."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from shizai_agent import shizai_agent_cli as cli_module
from shizai_agent.core import extension_bridge, mcp_server
from shizai_agent.core.cloud_api import ShizaiCloudError
from shizai_agent.core.native_ui import (
    NativeMask,
    NativeSmallCard,
    NativeTakeoverFrame,
    _NativeUiChannel,
)
from shizai_agent.core.workflow_responses import authoring_result


@pytest.fixture
def capture(monkeypatch, tmp_path):
    project = tmp_path / "c_flow"
    project.mkdir()
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", str(project))
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_UI_FEEDBACK", {})
    extension_bridge._write_capture_session_state(project, {
        "started_at": time.time(),
        "native_session_state": "active_last_known",
        "inspect_started": True,
        "overlay_state": "active",
    })
    outcome = {"inspect": TimeoutError("channel_unavailable")}

    def call(service, method, args):
        assert service == "DeepCaptureService"
        value = outcome["inspect"] if method == "stopDeepCaptureInspect" else {"isSuccess": True}
        if isinstance(value, Exception):
            raise value
        return value

    bridge = extension_bridge.ShizaiExtensionBridge(desktop=SimpleNamespace(call=call))
    monkeypatch.setattr(bridge, "status", lambda **kwargs: {})
    return project, bridge, outcome


def test_inspect_timeout_preserves_pending_state_and_blocks_resume(capture):
    project, bridge, _ = capture
    result = bridge.stop_inspect()
    assert result["overlay_state"] == "unknown"
    assert result["cleanup_complete"] is False
    assert result["retryable"] is True
    state = bridge.capture_session_status(package_path=project)
    assert state["inspect_started"] is True
    assert state["overlay_state"] == "unknown"
    assert state["next_action"] == "extension stop-capture"
    with pytest.raises(ShizaiCloudError, match="清理未确认"):
        bridge.ensure_capture_session(package_path=project)


@pytest.mark.parametrize("ack", [None, {}, {"isSuccess": False, "message": "rejected"}])
def test_inspect_missing_or_rejected_ack_never_reports_inactive(capture, ack):
    _, bridge, outcome = capture
    outcome["inspect"] = ack
    result = bridge.stop_inspect()
    assert result["cleanup_complete"] is False
    assert result["ui_feedback"]["overlay_state"] == "unknown"


def test_session_stop_ack_does_not_hide_failed_picker_cleanup_and_retry_recovers(capture, monkeypatch):
    project, bridge, outcome = capture
    result = bridge.stop_capture(package_path=project)
    assert result["result"]["isSuccess"] is True
    assert result["cleanup_complete"] is False
    assert result["picker_overlay_started"] is None
    # Simulate a subsequent CLI/MCP process with no in-memory capture owner.
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    state = bridge.capture_session_status(package_path=project)
    assert state["active"] is False
    assert state["overlay_state"] == "unknown"
    assert state["control_mode"] == "cleanup_pending"
    assert state["next_action"] == "extension stop-capture"
    outcome["inspect"] = {"isSuccess": True}
    retried = bridge.stop_capture(package_path=project)
    assert retried["cleanup_complete"] is True
    assert retried["overlay_state"] == "inactive"
    state = bridge.capture_session_status(package_path=project)
    assert state["inspect_started"] is False
    assert state["retryable"] is False


def test_pending_cleanup_does_not_expire_into_inactive(capture):
    project, bridge, _ = capture
    bridge.stop_inspect()
    state = extension_bridge._read_capture_session_state(project)
    state["started_at"] = time.time() - extension_bridge._CAPTURE_SESSION_STATE_TTL - 1
    extension_bridge._write_capture_session_state(project, state)
    assert bridge.capture_session_status(package_path=project)["overlay_state"] == "unknown"


def ui_client():
    return SimpleNamespace(
        config=SimpleNamespace(timeout=1, client_id="test"),
        _connect=lambda *_args: SimpleNamespace(close=lambda: None),
    )


@pytest.mark.parametrize("cls", [NativeSmallCard, NativeTakeoverFrame, NativeMask])
@pytest.mark.parametrize("ack", [None, {"isSuccess": False, "message": "close rejected"}])
def test_native_ui_close_requires_success_ack(monkeypatch, cls, ack):
    surface = cls(client=ui_client())
    monkeypatch.setattr(surface, "_call_method", lambda *args, **kwargs: ack)
    monkeypatch.setattr(surface, "_call_renderer", lambda *args, **kwargs: {"isSuccess": True})
    result = surface.close()
    assert result["visible"] is None
    assert result["cleanup_complete"] is False
    assert result["close_warning"]


def test_mask_service_ack_cannot_hide_failed_renderer_cleanup(monkeypatch):
    surface = NativeMask(client=ui_client())
    monkeypatch.setattr(surface, "_call_method", lambda *args, **kwargs: {"isSuccess": True})

    def renderer(*args, **kwargs):
        if kwargs["target_client_id"] == "$CurrentActiveEditorClientId$":
            raise TimeoutError("renderer timeout")
        return {"isSuccess": True}

    monkeypatch.setattr(surface, "_call_renderer", renderer)
    result = surface.close()
    assert result["visible"] is None
    assert result["cleanup_complete"] is False
    assert "renderer timeout" in result["close_warning"]


@pytest.mark.parametrize("error,expected", [(None, True), ("renderer rejected", False)])
def test_null_renderer_result_respects_explicit_error(error, expected):
    packet = {"packetType": "render-call-render-res", "body": {"callId": "test", "result": None, "error": error}}
    sock = SimpleNamespace(recv=lambda: json.dumps(packet))
    assert _NativeUiChannel._wait_for(sock, "test", "render-call-render-res")["isSuccess"] is expected


@pytest.mark.parametrize("entry", ["mcp", "cli"])
@pytest.mark.parametrize("reference", ["id", "removed_absolute"])
def test_stop_capture_resolves_project_id_before_rpc(monkeypatch, tmp_path, entry, reference):
    project = tmp_path / "applications" / "c_flow"
    project.mkdir(parents=True)
    project_ref = "c_flow" if reference == "id" else str(project.resolve())
    if reference == "removed_absolute":
        project.rmdir()
    paths = SimpleNamespace(applications_root=project.parent)
    calls = []

    class Bridge:
        def __init__(self, **kwargs):
            pass

        def stop_capture(self, *, package_path):
            calls.append(package_path)
            return {"cleanup_complete": True}

    module = mcp_server if entry == "mcp" else cli_module
    monkeypatch.setattr(module, "ShizaiExtensionBridge", Bridge)
    monkeypatch.setattr(module, "NativeMask", lambda **kwargs: SimpleNamespace(close=lambda **kwargs: {}))
    if entry == "mcp":
        monkeypatch.setattr(module, "_paths", lambda: paths)
        monkeypatch.setattr(module, "_desktop_client", lambda: object())
        module._dispatch_impl("shizai_extension_stop_capture", {"project_ref": project_ref, "allow_write": True})
    else:
        monkeypatch.setattr(module, "_paths", lambda ctx: paths)
        monkeypatch.setattr(module, "_desktop", lambda: object())
        result = CliRunner().invoke(module.cli, ["--json", "extension", "stop-capture", "--project-ref", project_ref, "--allow-write"])
        assert result.exit_code == 0, result.output
    assert calls == [str(project.resolve())]


def test_execution_stop_does_not_claim_desktop_cleanup_complete(monkeypatch):
    surface = SimpleNamespace(close=lambda **kwargs: {"visible": False})
    for name in ("NativeSmallCard", "NativeTakeoverFrame", "NativeMask"):
        monkeypatch.setattr(mcp_server, name, lambda **kwargs: surface)
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", lambda **kwargs: SimpleNamespace(
        stop_inspect=lambda: {"overlay_state": "unknown", "warning": "timeout"},
    ))
    cleanup = mcp_server._rpa_cleanup_native_ui(object())
    result = {"status": "stopped", "cleanup_complete": True, "ui_feedback": {"overlay_state": "inactive"}}
    mcp_server._attach_ui_cleanup_state(result, cleanup)
    assert result["cleanup_complete"] is True
    assert result["ui_cleanup_complete"] is False
    assert result["ui_feedback"]["overlay_state"] == "unknown"


@pytest.mark.parametrize("name", ["shizai_rpa_run", "shizai_rpa_status"])
@pytest.mark.parametrize("nested", [False, True])
def test_compact_runtime_response_preserves_incomplete_cleanup(name, nested):
    state = {"cleanup_complete": False, "ui_cleanup_complete": False,
             "ui_feedback": {"overlay_state": "unknown"}}
    source = {"status": state} if nested else state
    result = authoring_result(name, source, {"project_ref": "c_flow"})
    assert result["cleanup_complete"] is False
    assert result["ui_cleanup_complete"] is False
    assert result["ui_feedback"]["overlay_state"] == "unknown"


def test_compact_runtime_response_does_not_override_current_cleanup_with_history():
    source = {"cleanup_complete": False, "ui_cleanup_complete": False,
              "status": {"cleanup_complete": True, "ui_cleanup_complete": True}}
    result = authoring_result("shizai_rpa_status", source, {})
    assert result["cleanup_complete"] is False
    assert result["ui_cleanup_complete"] is False
