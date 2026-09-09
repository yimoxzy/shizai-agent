"""Regression coverage for interface gaps found by independent Luna evaluation."""

import json
import os
from datetime import datetime

import pytest
from click.testing import CliRunner

from shizai_agent.core import component_catalog, mcp_server, project
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_responses import authoring_result, compact_design_result
from shizai_agent.shizai_agent_cli import cli


def test_read_only_annotations_do_not_mislabel_preview_and_write_tools():
    tools = {tool.name: tool for tool in mcp_server._schemas()}
    for name in ("shizai_rpa_component_catalog", "shizai_rpa_read_flow", "shizai_rpa_validate_plan"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in ("shizai_rpa_update_step", "shizai_rpa_start_design", "shizai_rpa_run", "shizai_rpa_scan_page"):
        assert tools[name].annotations is None or tools[name].annotations.readOnlyHint is not True
    compact = {tool.name: tool for tool in mcp_server._schemas("rpa-authoring")}
    for name in ("shizai_rpa_component_catalog", "shizai_rpa_start_design"):
        assert "response_mode" not in compact[name].inputSchema["properties"]
        assert "response_mode" in tools[name].inputSchema["properties"]


def test_cli_preflight_uses_same_core_and_exit_code_as_mcp(tmp_path, monkeypatch):
    from shizai_agent import shizai_agent_cli

    catalog = {
        "File_Write_V1": {
            "inputs": [{"name": name} for name in ("path", "content", "encode")],
            "code": [{"func": "File.Write", "args": ["path", "content", "encode"]}],
            "materializable": True,
        },
    }
    monkeypatch.setattr(shizai_agent_cli.component_catalog_core, "builtin_component_index", lambda _: catalog)
    monkeypatch.setattr(shizai_agent_cli, "_desktop", lambda: (_ for _ in ()).throw(AssertionError("desktop called")))
    analysis = {"analysis_id": "a"}
    plan = {"analysis_id": "a", "steps": [{"id": "write", "component": "File_Write_V1",
                                           "config": {"path": "test.txt", "content": "测试", "encode": "utf-8"}}]}
    plan_file = tmp_path / "plan.json"
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps(analysis), encoding="utf-8")
    runner = CliRunner()
    for invalid in (False, True):
        if invalid:
            plan["steps"][0]["config"]["invented"] = 1
        plan_file.write_text(json.dumps(plan), encoding="utf-8-sig")
        result = runner.invoke(cli, ["--json", "workflow", "rpa-validate-plan",
                                     "--plan-file", str(plan_file), "--analysis-file", str(analysis_file)])
        assert result.exit_code == (1 if invalid else 0), result.output
        actual = json.loads(result.output)
        expected = project.preflight_rpa_execution_plan(analysis, plan, available_components=catalog, planning_policy="mcp")
        actual.pop("elapsed_ms")
        expected.pop("elapsed_ms")
        assert actual == expected


def test_cli_analysis_is_available_without_a_desktop():
    result = CliRunner().invoke(cli, ["--json", "workflow", "rpa-analyze-request", "创建原生RPA，写入文本，等待1秒，再读取文本"])
    assert result.exit_code == 0, result.output
    analysis = json.loads(result.output)
    assert analysis["kind"] == "rpa_request_analysis"
    assert analysis["analysis_id"]


def test_plan_validation_never_initializes_desktop(monkeypatch):
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: pytest.fail("read-only preflight touched desktop"))
    monkeypatch.setattr(mcp_server, "_restore_rpa_analysis", lambda _: ({"analysis_id": "a"}, False))
    monkeypatch.setattr(mcp_server, "_rpa_plan_preflight", lambda *args: {"valid": True})
    assert mcp_server._dispatch_impl("shizai_rpa_validate_plan", {"analysis": {}, "plan": {}})["valid"]


def test_compact_creation_keeps_failures_and_browser_recovery():
    failure = {"valid": False, "errors": ["missing element"]}
    full = {"kind": "shizai_rpa_started", "flow_ref": "test", "design_complete": True,
            "component_catalog": {"large": "x" * 100000},
            "planned_components": [{"step": {"id": "a", "component": "C"}, "step_validation": failure}],
            "validated_plan": {"valid": True, "plan": {"large": "x" * 100000}},
            "design_time_browser": {"ready": False}, "next_action": {"tool": "capture"}}
    compact = compact_design_result(full)
    assert compact["steps"][0]["step_validation"] == failure
    assert compact["next_action"] == full["next_action"]
    assert compact["runtime_verified"] is False
    assert len(json.dumps(compact)) < 1000
    assert "component_catalog" in full
    rejected = {"kind": "rejected", "error": "not writable"}
    assert compact_design_result(rejected) == rejected
    full.pop("design_time_browser")
    compact = compact_design_result(full)
    assert compact["next_action"]["tool"] == "shizai_rpa_validate_flow"
    assert compact["next_tool"] == "shizai_rpa_validate_flow"


def test_authoring_validation_suggests_the_run_boundary_without_authorizing_execution():
    value = {"valid": True, "next_tool": "shizai_rpa_validate_project", "errors": []}
    result = authoring_result("shizai_rpa_validate_flow", value, {"flow_ref": "test"})
    names = {tool.name for tool in mcp_server._schemas("rpa-authoring")}
    assert result["next_tool"] in names and result["next_action"]["tool"] in names
    assert result["next_tool"] == "shizai_rpa_run"
    assert result["next_action"]["arguments"] == {"project_ref": "test"}
    assert value["next_tool"] == "shizai_rpa_validate_project"
    failure = {"valid": False, "errors": ["bad parameters"]}
    assert authoring_result("shizai_rpa_validate_flow", failure, {"flow_ref": "test"}) == failure


def test_authoring_runtime_results_keep_current_output_without_history_dump():
    value = {
        "kind": "native_rpa_run",
        "terminal_status": "success",
        "status": {
            "status": "succeeded",
            "project": "project",
            "output": {"path": "result.csv", "exists": True, "format_valid": True},
            "run": {"uuid": "run-1", "workflow_run_id": "workflow-1", "terminal_status": "success"},
            "output_files": [{"path": f"history-{i}.csv"} for i in range(100)],
            "diagnosis": {"runtime": {"message": "ready", "runtime_browser_plugin_ready": True}},
        },
    }
    result = authoring_result("shizai_rpa_run", value, {"project_ref": "project"})
    assert result["terminal_status"] == "success"
    assert result["output"]["path"] == "result.csv"
    assert result["run_uuid"] == "run-1"
    assert "output_files" not in result
    assert len(json.dumps(result)) < len(json.dumps(value)) / 2
    status = authoring_result("shizai_rpa_status", {"kind": "native_rpa_status", "status": "succeeded",
                                                     "output": {"path": "result.xlsx"}},
                              {"project_ref": "project"})
    assert status["terminal_status"] == "success"
    assert status["completed"] is True


def test_authoring_analysis_recommends_summary_without_mutating_cached_analysis():
    analysis = mcp_server._cache_rpa_analysis(project.analyze_rpa_request("创建原生 RPA 写入文本"))
    presented = authoring_result("shizai_rpa_analyze_request", analysis, {})
    assert presented["planning_requirements"]["recommended_catalog_query"]["detail"] == "summary"
    assert analysis["planning_requirements"]["recommended_catalog_query"]["detail"] == "contract"
    assert mcp_server._restore_rpa_analysis(presented)[0] == analysis


def test_creation_restores_compact_analysis_and_returns_exact_query_on_mismatch(monkeypatch):
    original = "创建原生 RPA，写入一段文本"
    analysis = mcp_server._cache_rpa_analysis(project.analyze_rpa_request(original))
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    # Reaching plan validation proves that compact analysis passed the boundary;
    # reject there so this test cannot create a desktop application.
    monkeypatch.setattr(mcp_server, "_rpa_plan_preflight", lambda *args: {"valid": False, "error": "plan sentinel"})
    args = {"query": original, "analysis": {"analysis_id": analysis["analysis_id"]}, "plan": {}, "allow_write": True}
    assert mcp_server._dispatch_impl("shizai_rpa_start_design", args)["error"] == "plan sentinel"
    compact_args = {key: value for key, value in args.items() if key != "query"}
    assert mcp_server._dispatch_impl("shizai_rpa_start_design", compact_args)["error"] == "plan sentinel"
    value = mcp_server._dispatch_impl("shizai_rpa_start_design", {**args, "query": original + "。"})
    assert value["original_query"] == original and not value["project_created"]
    assert value["analysis_reference"] == args["analysis"]
    assert value["next_tool"] == "shizai_rpa_start_design"


def test_historical_desktop_error_cannot_fail_fresh_success(tmp_path):
    submitted = datetime(2026, 9, 9, 0, 0).timestamp()
    path = tmp_path / "out.txt"
    path.write_text("correct", encoding="utf8")
    os.utime(path, (submitted + 3, submitted + 3))
    logs = ["[2026-09-05 19:32:14.094][INFO]运行错误：old failure",
            "BizException old continuation", "[2026-09-05 19:32:14.095]isFinished: true",
            json.dumps({"timestamp": (submitted + 3) * 1000, "data": {"runStatus": "success"}})]
    state = {"submitted_at": submitted, "status": "succeeded", "terminal_status": "success"}
    assert project._native_run_status(state, path, logs) == "succeeded"
    assert project._native_run_evidence(logs, submitted_at=submitted)["error"] is None
    assert not project._native_run_evidence(logs[:3], submitted_at=submitted)["is_finished"]
    # A real error from this run must still win over the mere presence of a file.
    logs.append("[2026-09-09 00:00:04.000][ERROR]run-error current failure")
    assert project._native_run_status(state, path, logs) == "failed"


def test_collector_filters_old_errors_even_in_recently_touched_logs(tmp_path):
    paths = ShizaiPaths(tmp_path, tmp_path / "apps", tmp_path / "db", tmp_path / "run", None, None)
    path = tmp_path / "logs" / "node-main.log"
    path.parent.mkdir()
    path.write_text("[2026-09-05 19:00:00.000]run-error old\n"
                    "[2026-09-09 00:00:01.000]workflowDebugRunRPA uuid=current\n"
                    "[2026-09-09 00:00:02.000]run-error current\n", encoding="utf8")
    lines = project._native_runtime_log_lines(paths, {"uuid": "current", "submitted_at": datetime(2026, 9, 9).timestamp()})
    assert not any("old" in line for line in lines)
    assert any("run-error current" in line for line in lines)
    path.write_text("[2026-09-05 19:00:00.000]" + "x" * 530000 + " run-error old partial\n"
                    "[2026-09-09 00:00:01.000]workflowDebugRunRPA uuid=current\n", encoding="utf8")
    lines = project._native_runtime_log_lines(paths, {"uuid": "current", "submitted_at": datetime(2026, 9, 9).timestamp()})
    assert not any("old partial" in line for line in lines)
    assert any("uuid=current" in line for line in lines)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_text_output_evidence_has_actual_content_and_freshness(tmp_path, encoding):
    path = tmp_path / "result.txt"
    path.write_text("Luna 修改成功", encoding=encoding)
    os.utime(path, (100, 100))
    value = project._native_output_summary(tmp_path, path, {"submitted_at": 100})
    assert value["has_data"] and value["format_valid"] and value["fresh_for_run"]
    assert value["text_evidence"]["preview"] == "Luna 修改成功"
    assert value["text_evidence"]["complete"]
    assert not project._native_output_summary(tmp_path, path, {"submitted_at": 102})["fresh_for_run"]


def test_text_preview_handles_partial_unicode_and_invalid_encoding(tmp_path):
    path = tmp_path / "result.txt"
    path.write_text("中" * 10000, encoding="utf8")
    evidence = project._native_text_evidence(path)
    assert evidence["decoding_valid"] and evidence["truncated"] and not evidence["complete"]
    assert evidence["bytes_sampled"] == 8192
    assert project._native_output_summary(tmp_path, path, None)["format_valid"] is None
    path.write_bytes(b"\xff\xfeBAD")
    evidence = project._native_text_evidence(path)
    assert not evidence["decoding_valid"] and evidence["preview"] is None


def test_variable_warning_preserves_literal_semantics():
    plan = {"steps": [{"component": "C", "config": {"text": "$message"}}]}
    value = project.preflight_rpa_execution_plan({}, plan, available_components={"C": {
        "inputs": [{"name": "text"}], "code": [{"func": "C.Run", "args": ["text"]}],
        "materializable": True}}, planning_policy="mcp")
    assert value["valid"]
    assert value["plan"]["steps"][0]["config"]["text"] == "$message"
    assert any('"variable":"message"' in warning for warning in value["normalization_warnings"])


@pytest.mark.parametrize("query", ["", "File_Write", "does_not_exist"])
def test_compact_catalog_preserves_contract_and_pagination(tmp_path, query):
    paths = ShizaiPaths(tmp_path, tmp_path / "apps", tmp_path / "db", tmp_path / "run", None, None)
    engine = tmp_path / "engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(json.dumps([{
        "type": "File_Write", "label": "写入文件", "baseType": "File_Write",
        "definition": {"args0": [{"name": "content", "defaultValue": '""'}]},
        "codeDesc": [{"func": "File.Write", "args": ["content"]}],
    }, {"type": "Basic_DebugOutput", "label": "输出调试信息", "definition": {"args0": []}}]), encoding="utf8")
    options = {"engine_root": str(engine), "scope": "builtin", "detail": "contract", "query": query, "limit": 1}
    full = component_catalog.component_catalog(paths, **options)
    compact = component_catalog.component_catalog(paths, **options, response_mode="compact")
    for key in ("components", "ready", "source", "filters", "has_more", "next_page", "matched_components"):
        assert compact[key] == full[key]
    assert "role_selection" not in compact and "planning_guidance" not in compact
    static = component_catalog.component_catalog(paths, **{**options, "query": "Basic_DebugOutput"})["components"][0]
    assert static["materializable"] is False
    assert static["materialization_errors"]
