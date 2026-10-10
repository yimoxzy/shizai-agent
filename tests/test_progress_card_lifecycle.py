"""A completed tool must not leave a permanent 'working' capsule on the desktop."""

import pytest

from shizai_agent.core import mcp_server


@pytest.fixture
def capsule(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", False)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    # Closing the card must not tear down a paused debugger or active picker.
    monkeypatch.setattr(mcp_server, "_ACTIVE_RPA_DESIGN_UI", {"flow": {"takeover_started": True}})

    class Card:
        def __init__(self, client):
            pass

        def close(self):
            events.append("card_close")
            return {"state": "hidden", "visible": False}

    monkeypatch.setattr(mcp_server, "NativeSmallCard", Card)
    return events


@pytest.mark.parametrize("tool,args,core_function", [
    ("shizai_rpa_validate_step", {"flow_ref": "flow", "step_id": "one"}, "validate_rpa_step"),
    ("shizai_rpa_validate_flow", {"flow_ref": "flow"}, "validate_rpa_flow"),
    ("shizai_rpa_add_step", {"flow_ref": "flow", "component": "Basic_Delay_V1", "allow_write": True}, "add_rpa_step"),
])
@pytest.mark.parametrize("failed", [False, True])
def test_finite_design_call_closes_card_on_success_and_exception(monkeypatch, capsule, tool, args, core_function, failed):
    def show(*_args, **_kwargs):
        mcp_server._RPA_SMALL_CARD_OPEN = True
        capsule.append("card_show")
        return {"state": "validating"}

    def work(*_args, **_kwargs):
        assert mcp_server._RPA_SMALL_CARD_OPEN
        capsule.append("work")
        if failed:
            raise ValueError("native operation failed")
        return {"valid": True}

    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", show)
    monkeypatch.setattr(mcp_server.project_core, core_function, work)
    if failed:
        with pytest.raises(ValueError, match="native operation failed"):
            mcp_server._dispatch(tool, args)
    else:
        result = mcp_server._dispatch(tool, args)
        assert result["valid"]
        assert result["native_ui"]["progress_card_cleanup"]["cleanup_complete"]
    assert capsule == ["card_show", "work", "card_close"]
    assert not mcp_server._RPA_SMALL_CARD_OPEN
    assert mcp_server._ACTIVE_RPA_DESIGN_UI == {"flow": {"takeover_started": True}}


def test_exact_component_execution_closes_prior_preview_card(monkeypatch, capsule):
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)
    monkeypatch.setattr(mcp_server.project_core, "run_rpa_component", lambda *a, **k: {"runtime_verified": True})
    result = mcp_server._dispatch("shizai_rpa_run_component", {
        "project_ref": "flow", "step_id": "collect", "expected_revision": "rev", "allow_write": True,
    })
    assert result["runtime_verified"]
    assert result["native_ui"]["progress_card_cleanup"]["cleanup_complete"]
    assert capsule == ["card_close"]


@pytest.mark.parametrize("failed", [False, True])
def test_explicit_stop_always_cleans_all_surfaces(monkeypatch, capsule, failed):
    def stop(*_args, **_kwargs):
        capsule.append("stop")
        if failed:
            raise TimeoutError("stop timed out")
        return {"stopped": True}

    def cleanup(_client):
        capsule.append("all_ui_cleanup")
        return {"small_card": {"visible": False}}

    monkeypatch.setattr(mcp_server.project_core, "native_rpa_stop", stop)
    monkeypatch.setattr(mcp_server, "_rpa_cleanup_native_ui", cleanup)
    if failed:
        with pytest.raises(TimeoutError, match="stop timed out"):
            mcp_server._dispatch("shizai_rpa_stop", {"project_ref": "flow", "allow_write": True})
    else:
        result = mcp_server._dispatch("shizai_rpa_stop", {"project_ref": "flow", "allow_write": True})
        assert result["native_ui"]["final_cleanup"]["small_card"]["visible"] is False
    assert capsule == ["stop", "all_ui_cleanup"]


@pytest.mark.parametrize("tool", ["shizai_rpa_run_component", "shizai_rpa_add_step", "shizai_rpa_stop"])
def test_denied_writes_do_not_close_any_ui(monkeypatch, capsule, tool):
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)
    with pytest.raises(PermissionError):
        mcp_server._dispatch(tool, {})
    assert capsule == []
    assert mcp_server._RPA_SMALL_CARD_OPEN


@pytest.mark.parametrize("raises", [False, True])
def test_close_failure_preserves_business_result_and_allows_cleanup_retry(monkeypatch, capsule, raises):
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)
    monkeypatch.setattr(mcp_server.project_core, "run_rpa_component", lambda *a, **k: {"runtime_verified": True})

    class FailedClose:
        def __init__(self, client):
            pass

        def close(self):
            if raises:
                raise OSError("desktop unavailable")
            return {"visible": False, "close_warning": "window close rejected"}

    monkeypatch.setattr(mcp_server, "NativeSmallCard", FailedClose)
    result = mcp_server._dispatch("shizai_rpa_run_component", {
        "project_ref": "flow", "step_id": "one", "expected_revision": "rev", "allow_write": True,
    })
    assert result["runtime_verified"]
    assert not result["native_ui"]["progress_card_cleanup"]["cleanup_complete"]
    assert mcp_server._RPA_SMALL_CARD_OPEN


@pytest.mark.parametrize("tool", ["shizai_rpa_debug_start", "shizai_rpa_debug_command", "shizai_extension_start_capture"])
def test_active_debug_or_picker_keeps_its_card(monkeypatch, capsule, tool):
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)
    monkeypatch.setattr(mcp_server, "_dispatch_impl", lambda *a, **k: {"status": "active"})
    assert mcp_server._dispatch(tool, {"allow_write": True})["status"] == "active"
    assert capsule == []
    assert mcp_server._RPA_SMALL_CARD_OPEN
