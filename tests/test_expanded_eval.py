"""Evidence preservation and catalog ambiguity regressions from broader evaluation."""

import json
import os

import pytest

from scripts import luna_rpa_eval
from scripts.grade_luna_rpa import inspect_artifact
from scripts.luna_rpa_eval import snapshot_flows, snapshot_outputs
from shizai_agent.core import component_catalog, mcp_server, project
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_responses import authoring_result


def test_eval_runner_disables_dynamic_mcp_with_complete_transport(monkeypatch):
    class Result:
        returncode = 0
        stdout = json.dumps({
            "transport": {
                "type": "stdio",
                "command": "node.exe",
                "args": ["launch.mjs"],
                "cwd": "C:/runtime",
            },
        })

    monkeypatch.setattr(luna_rpa_eval.subprocess, "run", lambda *args, **kwargs: Result())

    assert luna_rpa_eval.disabled_mcp_overrides("codex.exe", "cua_repl") == {
        "mcp_servers.cua_repl.command": "node.exe",
        "mcp_servers.cua_repl.args": ["launch.mjs"],
        "mcp_servers.cua_repl.cwd": "C:/runtime",
        "mcp_servers.cua_repl.enabled": False,
    }


def test_eval_runner_skips_missing_dynamic_mcp_override(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(luna_rpa_eval.subprocess, "run", lambda *args, **kwargs: Result())

    assert luna_rpa_eval.disabled_mcp_overrides("codex.exe", "missing") == {}


def test_exact_contract_lookup_exposes_live_family_variants_without_substitution(tmp_path):
    paths = ShizaiPaths(tmp_path, tmp_path / "apps", tmp_path / "db", tmp_path / "run", None, None)
    engine = tmp_path / "engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    records = [{"type": name, "baseType": "File_Write", "label": "写文件", "description": description,
                "definition": {"args0": [{"name": "path"}]},
                "codeDesc": [{"func": "File.Write", "args": ["path"]}]}
               for name, description in (("File_Write", "覆盖已有内容"), ("File_Write_V1", "目标不存在则新建"))]
    dictionary.write_text(json.dumps(records), encoding="utf8")
    for mode in ("full", "compact"):
        value = component_catalog.component_catalog(paths, engine_root=str(engine), scope="builtin",
                                                     component_type="File_Write", detail="contract", response_mode=mode)
        assert [item["type"] for item in value["components"]] == ["File_Write"]
        alternative = value["related_variants"]["items"][0]
        assert alternative["type"] == "File_Write_V1"
        assert alternative["description"] == "目标不存在则新建"
        assert alternative["materializable"]
    summary = component_catalog.component_catalog(paths, engine_root=str(engine), scope="builtin", detail="summary")
    assert "related_variants" not in summary


def test_evaluation_preserves_first_turn_output_after_subsequent_edit(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    output = data / "Luna评测-A.txt"
    output.write_text("original", encoding="utf8")
    os.utime(output, (100, 100))
    (data / "other.txt").write_text("unrelated", encoding="utf8")
    first = snapshot_outputs(data, "Luna评测-A", tmp_path / "turn-1", 99)
    output.write_text("edited", encoding="utf8")
    os.utime(output, (200, 200))
    second = snapshot_outputs(data, "Luna评测-A", tmp_path / "turn-2", 201.5)
    assert len(first) == len(second) == 1
    assert (tmp_path / "turn-1" / output.name).read_text(encoding="utf8") == "original"
    assert (tmp_path / "turn-2" / output.name).read_text(encoding="utf8") == "edited"
    assert first[0]["fresh_for_turn"] and not second[0]["fresh_for_turn"]
    assert first[0]["sha256"] != second[0]["sha256"]


def test_native_file_text_and_table_discovery_has_non_browser_verbs():
    expected = {"File_Read": {"file", "read"}, "File_Write_V1": {"file", "write", "output"},
                "File_Copy": {"file", "copy"}, "StrUtil_Replace": {"text", "transform"},
                "Excel_Cs_ReadRange": {"excel", "table", "read"}, "Bivartable_ReadCsv": {"csv", "table", "read"}}
    for name, required in expected.items():
        value = component_catalog.component_capabilities({"type": name})
        assert required <= set(value["capabilities"])
        assert not value["requires_page_evidence"]
    compact = next(tool for tool in mcp_server._schemas("rpa-authoring") if tool.name == "shizai_rpa_start_design")
    assert "wait_seconds" not in compact.inputSchema["properties"]
    assert "query" not in compact.inputSchema["required"]
    full = next(tool for tool in mcp_server._schemas("full") if tool.name == "shizai_rpa_start_design")
    assert "query" in full.inputSchema["required"]


def test_stopped_run_cannot_succeed_from_a_partially_written_workbook(tmp_path):
    output = tmp_path / "partial.xlsx"
    output.write_bytes(b"partial workbook")
    state = {"status": "submitted", "submitted_at": output.stat().st_mtime, "terminal_status": "stopped"}
    assert project._native_run_status(state, output, []) == "stopped"


def test_empty_page_scan_recovers_through_an_available_edit_context_tool():
    value = {"candidate_count": 0, "scan_token": "current", "next_action": {"tool": "shizai_rpa_components"}}
    actual = authoring_result("shizai_rpa_scan_page", value, {"project_ref": "existing"})
    assert actual["next_action"]["tool"] == "shizai_rpa_read_flow"
    assert actual["next_action"]["arguments"] == {"flow_ref": "existing", "detail": "edit"}
    assert actual["candidate_count"] == 0 and actual["scan_token"] == "current"
    assert value["next_action"]["tool"] == "shizai_rpa_components"


def test_flow_evidence_keeps_original_ids_before_edit_and_excludes_unowned_projects(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    task = root / "main.task"
    task.write_text('{"nodes":["write","read"]}', encoding="utf8")
    application = {"created": True, "name": "Luna评测-test", "local_path": str(root)}
    item = {"tool": "shizai_rpa_start_design", "result": {"content": [
        {"type": "text", "text": json.dumps({"application": application})}]}}
    first = snapshot_flows([{"items": [item]}], tmp_path / "first")
    task.write_text('{"nodes":["write","wait","read"]}', encoding="utf8")
    second = snapshot_flows([{"items": [item]}], tmp_path / "second")
    assert first[0]["sha256"] != second[0]["sha256"]
    assert json.loads((tmp_path / "first/owned/main.task").read_text())["nodes"] == ["write", "read"]
    application["name"] = "用户应用"
    item["result"]["content"][0]["text"] = json.dumps({"application": application})
    assert snapshot_flows([{"items": [item]}], tmp_path / "excluded") == []


def test_pagination_grader_rejects_only_first_page_duplicate_rows_and_wrong_edit_columns(tmp_path):
    output = tmp_path / "result.csv"
    record = {"snapshot": str(output), "fresh_for_turn": True}
    header = "订单号,城市,金额\n"
    rows = [f"HZ-{i:03d},杭州,{i * 12}\n" for i in range(1, 8)]
    for content in (header + "".join(rows[:3]), header + "".join(rows + rows[:1])):
        output.write_text(content, encoding="utf8")
        assert not inspect_artifact(record, "browser-pagination", 1)["matches_expected"]
    output.write_text(header + "".join(rows), encoding="utf8")
    assert inspect_artifact(record, "browser-pagination", 1)["matches_expected"]
    assert not inspect_artifact(record, "browser-pagination", 2)["matches_expected"]


@pytest.mark.parametrize("component", ["Element_SetValue_V5", "Element_Click_V5"])
def test_live_selector_component_binds_without_changing_type_or_other_arguments(tmp_path, monkeypatch, component):
    root = tmp_path / "apps" / "owned"
    root.mkdir(parents=True)
    paths = ShizaiPaths(tmp_path, root.parent, tmp_path / "db", tmp_path / "run", None, None)
    task = root / "main.task"
    nodes = {"MAIN_COMPONENT": {"args0": [{"name": "statements", "value": ["target"]}]},
             "target": {"id": "target", "type": component, "args0": [
                 {"name": "uiSelector", "value": ""}, {"name": "designated_obj", "value": "my_browser"},
                 {"name": "text", "value": '"上海"'}]}}
    task.write_text(json.dumps({"componentModelEntities": nodes}), encoding="utf8")
    (root / "szrpa.json").write_text('{"packageName":"test"}', encoding="utf8")
    (root / "elements.json").write_text(json.dumps({"elementMap": {
        "captured": {"mcpSelectorMode": "web", "selector": {"selector": [{"type": "web"}]}},
        "second": {"selector": {}}}}), encoding="utf8")
    monkeypatch.setattr(project, "_live_rpa_contract", lambda *_: {"parameter_names": ["uiSelector", "designated_obj", "text"]})
    monkeypatch.setattr(project, "_publish_rpa_ui", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project, "_finalize_native_writeback", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project, "_native_rpa_state", lambda *_: {"steps": [{"id": "target", "component": component}],
                        "phase": "elements_bound", "next_tool": "validate", "next_action": {}})
    monkeypatch.setattr(project, "validate_rpa_step", lambda *_: {"valid": True, "errors": [], "warnings": []})
    before = task.read_bytes()
    with pytest.raises(ValueError, match="exactly one"):
        project.set_rpa_step_elements(paths, str(root), "target", ["captured", "second"])
    assert task.read_bytes() == before
    result = project.set_rpa_step_elements(paths, str(root), "target", ["captured"])
    assert result["step_validation"]["valid"]
    node = json.loads(task.read_text(encoding="utf8"))["componentModelEntities"]["target"]
    assert node["type"] == component
    assert project._native_element_ids_from_node(node) == ["captured"]
    assert project._native_task_argument(node, "designated_obj") == "my_browser"
    assert project._native_task_argument(node, "text") == '"上海"'


def test_verified_file_write_precondition_reaches_thin_preflight_without_substitution(tmp_path):
    record = {"type": "File_Write", "version": "3.3.5.0", "definition": {"args0": [
        {"name": "path"}, {"name": "content"}]}, "codeDesc": [{"func": "File.Write", "args": ["path", "content"]}]}
    source = {"engine_root": str(tmp_path)}
    contract = component_catalog._summary(source, record, "contract")
    assert "目标文件" in contract["runtime_preconditions"][0]
    assert "runtime_preconditions" not in component_catalog._summary(source, {**record, "version": "future"}, "contract")
    plan = {"steps": [{"id": "write", "component": "File_Write", "config": {"path": "missing.txt", "content": "test"}}]}
    result = project.preflight_rpa_execution_plan({}, plan, available_components={"File_Write": contract}, planning_policy="mcp")
    assert result["valid"]
    assert any("plan step write" in warning and "不会创建缺失文件" in warning for warning in result["normalization_warnings"])
    assert result["plan"]["steps"][0]["component"] == "File_Write"
    assert len(result["plan"]["steps"]) == 1


def test_authoring_discovery_matches_designer_visibility_but_can_inspect_hidden_nodes(tmp_path):
    paths = ShizaiPaths(tmp_path, tmp_path / "apps", tmp_path / "db", tmp_path / "run", None, None)
    engine = tmp_path / "engine"
    dictionary = engine / "componentDicts/componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    records = [{"type": name, "baseType": "File_Write", "releaseTag": tag, "label": "写文件",
                "definition": {"args0": [{"name": "path"}]}, "codeDesc": [{"func": "File.Write", "args": ["path"]}]}
               for name, tag in (("File_Write", 131), ("File_Write_V1", 3))]
    dictionary.write_text(json.dumps(records), encoding="utf8")
    options = {"engine_root": str(engine), "scope": "builtin", "query": "File_Write", "response_mode": "compact"}
    visible = component_catalog.component_catalog(paths, **options, include_hidden=False)
    assert [c["type"] for c in visible["components"]] == ["File_Write_V1"]
    assert not visible["filters"]["include_hidden"]
    assert not visible["components"][0]["designer_hidden"]
    complete = component_catalog.component_catalog(paths, **options)
    assert {c["type"] for c in complete["components"]} == {"File_Write", "File_Write_V1"}
    exact = component_catalog.component_catalog(paths, **options, include_hidden=False, component_type="File_Write")
    assert exact["components"][0]["designer_hidden"]
    assert exact["components"][0]["materializable"]
    assert exact["related_variants"]["items"][0]["type"] == "File_Write_V1"
    authoring = next(t for t in mcp_server._schemas("rpa-authoring") if t.name == "shizai_rpa_component_catalog")
    assert authoring.inputSchema["properties"]["include_hidden"]["default"] is False


@pytest.mark.parametrize("display_type", ["dropDownSelect", "radioGroup", "buttonGroup"])
def test_live_numeric_choice_is_checked_before_a_generic_plan_is_written(display_type):
    parameter = component_catalog._parameter_contract({"name": "mode", "defaultValue": "3",
        "displayType": display_type, "displayParam": [{"label": "CSV 文件", "value": "3"}]})
    assert parameter["semantic_default"] == 3
    assert parameter["semantic_choices"] == [{"label": "CSV 文件", "value": 3}]
    string_parameter = component_catalog._parameter_contract({"name": "software", "displayType": "dropDownSelect",
        "displayParam": [{"label": "WPS", "value": '"WPS"'}]})
    assert string_parameter["semantic_choices"][0]["value"] == "WPS"
    catalog = {"Bivartable_CreateDf": {"inputs": [parameter], "code": [{"func": "Bivartable.CreateDf", "args": ["mode"]}]}}
    plan = {"steps": [{"id": "read", "component": "Bivartable_CreateDf", "config": {"mode": "3"}}]}
    for incorrect in ("3", {"literal": "3"}):
        plan["steps"][0]["config"]["mode"] = incorrect
        wrong = project.preflight_rpa_execution_plan({}, plan, available_components=catalog, planning_policy="mcp")
        assert not wrong["valid"] and 'config.mode' in wrong["error"] and 'semantic_choices' in wrong["error"]
    for correct in (3, {"literal": 3}, {"variable": "runtime_mode"}):
        plan["steps"][0]["config"]["mode"] = correct
        assert project.preflight_rpa_execution_plan({}, plan, available_components=catalog, planning_policy="mcp")["valid"]


def test_known_native_output_type_mismatch_requires_conversion_and_forgets_reassignments():
    def contract(name, input_type, output_type):
        return {"inputs": [{"name": "var_name", "vType": [{"rule": "const", "types": [output_type]}]},
                           {"name": "data", "vType": [{"rule": "const", "types": [input_type]}]}],
                "outputs": {"assign_to": ["var_name"]},
                "code": [{"func": f"Native.{name}", "args": ["data"], "assignTo": "var_name"}]}
    catalog = {"ReadRows": contract("ReadRows", "rpa_str", "rpa_list"),
               "ToTable": contract("ToTable", "rpa_list", "rpa_pd"),
               "FilterTable": contract("FilterTable", "rpa_pd", "rpa_pd"),
               "DynamicRows": contract("DynamicRows", "rpa_list", "unknown_type")}
    read = {"id": "read", "component": "ReadRows", "config": {"var_name": "rows", "data": "input.csv"}}
    use = {"id": "filter", "component": "FilterTable", "config": {"var_name": "filtered", "data": {"variable": "rows"}}}
    def validate(steps):
        return project.preflight_rpa_execution_plan({}, {"steps": steps}, available_components=catalog, planning_policy="mcp")
    mismatch = validate([read, use])
    assert not mismatch["valid"] and "list" in mismatch["error"] and "dataframe" in mismatch["error"]
    convert = {"id": "convert", "component": "ToTable", "config": {"var_name": "rows", "data": {"variable": "rows"}}}
    assert validate([read, convert, use])["valid"]
    dynamic = {**convert, "component": "DynamicRows"}
    assert validate([read, dynamic, use])["valid"]
    assert validate([use])["valid"]  # Unknown external input is deferred.
    expression_use = {**use, "config": {"data": {"expression": "convert(rows)"}}}
    assert validate([read, expression_use])["valid"]
