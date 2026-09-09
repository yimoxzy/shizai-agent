"""Regressions from Luna trying to capture pages before creating a browser."""

import json

import pytest

from shizai_agent.core import debug_session, mcp_server, project
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_responses import authoring_result


def catalog(types="rpa_hWeb"):
    typed = [{"rule": "const", "types": [types]}]
    return {
        "Resource_Create": {"inputs": [{"name": "result", "vType": typed}],
                            "outputs": {"assign_to": ["result"]},
                            "code": [{"func": "Resource.Create", "args": [], "assignTo": "result"}]},
        "Resource_Use": {"inputs": [{"name": "resource", "vType": typed}],
                         "code": [{"func": "Resource.Use", "args": ["resource"]}]},
    }


@pytest.mark.parametrize("value", ["chrome", "网页对象", {"literal": "chrome"}, 1, None])
def test_handle_inputs_reject_literal_placeholders_before_native_io(value):
    plan = {"steps": [{"component": "Resource_Use", "config": {"resource": value}}]}
    result = project.preflight_rpa_execution_plan({}, plan, available_components=catalog(), planning_policy="mcp")
    assert not result["valid"]
    assert "runtime object" in str(result)


def test_handle_reference_requires_an_earlier_producer():
    use = {"component": "Resource_Use", "config": {"resource": {"variable": "page"}}}
    create = {"component": "Resource_Create", "config": {"result": "page"}}
    for steps, valid in [([use], False), ([use, create], False), ([create, use], True)]:
        result = project.preflight_rpa_execution_plan({}, {"steps": steps},
                                                       available_components=catalog(), planning_policy="mcp")
        assert result["valid"] is valid, result


def test_handle_output_is_a_variable_name_and_handle_types_must_match():
    components = catalog("rpa_hWorkbook")
    components["Resource_Use"] = catalog("rpa_hWeb")["Resource_Use"]
    plan = {"steps": [{"component": "Resource_Create", "config": {"result": "resource"}},
                      {"component": "Resource_Use", "config": {"resource": {"variable": "resource"}}}]}
    result = project.preflight_rpa_execution_plan({}, plan, available_components=components, planning_policy="mcp")
    assert not result["valid"] and "compatible output" in str(result)
    assert "not a literal" not in str(result)  # Output names are not object inputs.


def test_dynamic_types_and_explicit_expressions_defer_to_runtime():
    components = catalog()
    components["Resource_Use"]["inputs"][0]["vType"] = [{"rule": "conditional", "types": ["rpa_hWeb"]}]
    plan = {"steps": [{"component": "Resource_Use", "config": {"resource": "dynamic"}}]}
    assert project.preflight_rpa_execution_plan({}, plan, available_components=components, planning_policy="mcp")["valid"]
    plan["steps"][0]["config"]["resource"] = {"expression": "runtime_resource"}
    assert project.preflight_rpa_execution_plan({}, plan, available_components=catalog(), planning_policy="mcp")["valid"]


@pytest.mark.parametrize("status", ["running", "start_timeout", "runtime_error", "cleanup_failed", "unknown"])
def test_navigation_waiting_or_failure_does_not_claim_browser_open(monkeypatch, status):
    monkeypatch.setattr(mcp_server, "_rpa_navigation_step", lambda *_: {"step_id": "open", "position": 1})
    monkeypatch.setattr(mcp_server.debug_core, "debug_capabilities", lambda *_: {"active_session": False})
    monkeypatch.setattr(mcp_server.debug_core, "debug_start", lambda *a, **kw: {
        "status": "paused", "current": {"component_id": "open"}})
    monkeypatch.setattr(mcp_server.debug_core, "debug_command", lambda *a, **kw: {"status": status})
    with pytest.raises(mcp_server.ShizaiCloudError, match="execution failed"):
        mcp_server._rpa_run_navigation_component(object(), object(), "FLOW")


@pytest.fixture
def native_definition(tmp_path):
    root = tmp_path / "factory"
    paths = ShizaiPaths(root, root / "applications", root / "db", root / "run", None, None)
    native = paths.applications_root / "test"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(json.dumps({"id": "test", "mcpPlanningPolicy": "model_decides"}), encoding="utf8")
    (native / "elements.json").write_text('{"elementMap":{},"groupMap":{}}', encoding="utf8")
    for name in ("project.flow", "main.sz"):
        (native / name).write_text(name, encoding="utf8")
    task = native / "io5cd.task"
    task.write_text(json.dumps({"componentModelEntities": {"MAIN_COMPONENT": {
        "id": "MAIN_COMPONENT", "type": "main", "args0": [{"name": "statements", "value": []}]}}}), encoding="utf8")
    return paths, native, task


def test_native_writer_preserves_model_objects_outputs_and_submit_choice(native_definition):
    paths, native, task = native_definition
    for component, step_id, config in [
        ("WebBrowser_CreateV3", "open", {"url": "https://example.test", "var_name": "page", "waitingload": 0}),
        ("Element_SetValue_V6", "search", {"text": "test", "var_name": "checked", "isEnter": True,
                                         "designated_obj": {"variable": "page"}}),
        ("WebBrowser_DataCollectionV5", "collect", {"var0": "rows", "var1": "output_path",
                                                   "designated_obj": {"variable": "page"}}),
    ]:
        project.add_rpa_step(paths, str(native), component, step_id=step_id, config=config, allow_unbound_predecessors=True)
    nodes = json.loads(task.read_text(encoding="utf8"))["componentModelEntities"]
    args = {key: {p["name"]: p.get("value") for p in n.get("args0", [])} for key, n in nodes.items()}
    assert args["open"]["var_name"] == "page"
    assert args["open"]["waitingload"] == "0"
    assert args["search"]["var_name"] == "checked"
    assert args["search"]["isEnter"] == "1"
    assert args["search"]["designated_obj"] == "page"
    assert args["collect"]["var0"] == "rows"
    assert args["collect"]["var1"] == "output_path"
    assert args["collect"]["designated_obj"] == "page"


@pytest.mark.parametrize("changed", ["task", "elements"])
def test_edit_blocks_old_debug_frame_before_any_dap_reconnect(native_definition, monkeypatch, changed):
    paths, native, task = native_definition
    session = debug_session.NativeDebugSession(paths, native, task, "flow", ["open"], 1, object(), native / "state.json",
                                               status="paused", current={"component_id": "open"})
    session.definition_sha256 = debug_session._execution_definition_sha256(native, task)
    assert session._definition_changed_result() is None
    target = task if changed == "task" else native / "elements.json"
    data = json.loads(target.read_text(encoding="utf8"))
    target.write_text(json.dumps(data, indent=2), encoding="utf8")
    assert session._definition_changed_result() is None  # Formatting is not a runtime change.
    if changed == "task":
        data["componentModelEntities"]["MAIN_COMPONENT"]["args0"][0]["value"] = ["new_step"]
    else:
        data["elementMap"]["search"] = {"selector": {"value": "new_selector"}}
    target.write_text(json.dumps(data), encoding="utf8")

    def get_session(*args, **kwargs):
        assert kwargs.get("reconnect") is False, "Stale debug frame must not reconnect or execute"
        return session

    monkeypatch.setattr(debug_session, "_get_session", get_session)
    result = debug_session.debug_command(paths, str(native), "next")
    assert result["requires_restart"] and not result["runtime_execution"]
    assert not result["runtime_verification"]["verified"]
    assert result["next_action"]["tool"] == "shizai_rpa_debug_stop"
    assert result["then"]["arguments"]["project_ref"] == str(native)
    session._persist()
    assert json.loads((native / "state.json").read_text(encoding="utf8"))["definition_sha256"] == session.definition_sha256


@pytest.mark.parametrize("status", ["static_failed", "runtime_pending"])
def test_capture_validates_current_step_before_scanning_later_components(status):
    action = {"tool": "shizai_rpa_validate_step", "arguments": {"step_id": "search"}}
    following = {"tool": "shizai_rpa_scan_page", "arguments": {"step_id": "collect"}}
    value = {"binding": {"next_action": following}, "next_action": following,
             "verification_gate": {"status": status, "next_action": action}}
    result = authoring_result("shizai_rpa_capture_elements", value, {})
    assert result["next_action"] == action and result["binding"]["next_action"] == action
    assert result["after_step_verification"] == following
    assert value["next_action"] == following
