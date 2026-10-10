"""Regression coverage for interface gaps found by independent Luna evaluation."""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp import types

from shizai_agent.core import component_catalog, extension_bridge, mcp_server, project
from shizai_agent.core.cloud_api import ShizaiCloudError
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_contracts import RPA_AUTHORING_INSTRUCTIONS, RPA_AUTHORING_TOOLS, rpa_plan_schema
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


def test_plan_schema_distinguishes_logical_outputs_from_native_assignment_slots():
    outputs = rpa_plan_schema()["properties"]["steps"]["items"]["properties"]["outputs"]
    assert "逻辑变量名" in outputs["description"]
    assert "var_name/var0" in outputs["description"]
    assert "通常省略" in outputs["description"]
    assert "outputs.assign_to" in RPA_AUTHORING_INSTRUCTIONS


def test_thin_plan_canonicalizes_native_slot_names_before_persistence():
    analysis = project.analyze_rpa_request("创建原生RPA，打开 https://www.jd.com 并采集商品标题。")
    plan = {
        "analysis_id": analysis["analysis_id"],
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateV3",
                "config": {"url": "https://www.jd.com", "type": "chrome", "var_name": "浏览器"},
                "outputs": ["var_name"],
            },
            {
                "id": "collect",
                "component": "WebBrowser_DataCollectionV5",
                "config": {"var0": "结果", "var1": "路径", "var2": "空列"},
                "depends_on": ["open"],
                "outputs": ["var0", "var1", "var2"],
            },
        ],
        "output_contract": {"columns": ["商品标题"], "file": "result.xlsx"},
    }

    result = project.validate_mcp_rpa_plan(analysis, plan)

    assert result["valid"]
    assert result["plan"]["steps"][0]["outputs"] == ["网页对象"]
    assert result["plan"]["steps"][1]["outputs"] == ["数据采集结果", "Excel路径", "监控空列数据"]
    assert sum("MCP generated logical outputs" in item for item in result["normalization_warnings"]) == 2


def test_dynamic_component_outputs_are_derived_from_assignment_values():
    analysis = {"analysis_id": "analysis-1", "query": "write a file"}
    catalog = {
        "Custom_Output": {
            "inputs": [{"name": "result", "isExport": 1}],
            "outputs": {"assign_to": ["result"]},
            "code": [{"func": "Custom.Output", "args": ["result"]}],
            "materializable": True,
        }
    }
    plan = {
        "analysis_id": "analysis-1",
        "steps": [
            {
                "id": "custom",
                "component": "Custom_Output",
                "config": {"result": "业务结果"},
                "outputs": ["result"],
            }
        ],
        "output_contract": {},
    }

    result = project.validate_mcp_rpa_plan(analysis, plan, available_components=catalog)

    assert result["plan"]["steps"][0]["outputs"] == ["业务结果"]
    assert "copied native assignment slots" in result["normalization_warnings"][0]


def test_builtin_output_assignment_can_be_referenced_by_following_step():
    analysis = project.analyze_rpa_request("创建原生RPA，打开网页并输入内容。")
    catalog = {
        "WebBrowser_CreateV3": {
            "inputs": [
                {"name": "var_name", "isExport": 3},
                {"name": "type"},
                {"name": "url"},
            ],
            "outputs": {"assign_to": ["var_name"]},
            "code": [{"func": "WebBrowser.CreateV3", "args": ["var_name", "type", "url"]}],
        },
        "Element_SetValue_V6": {
            "inputs": [
                {"name": "var_name", "isExport": 3},
                {"name": "text"},
                {"name": "designated_obj"},
            ],
            "outputs": {"assign_to": ["var_name"]},
            "code": [{"func": "Element.SetValue", "args": ["var_name", "text", "designated_obj"]}],
        },
    }
    plan = {
        "analysis_id": analysis["analysis_id"],
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateV3",
                "config": {"var_name": "京东网页", "type": "chrome", "url": "https://www.jd.com"},
            },
            {
                "id": "search",
                "component": "Element_SetValue_V6",
                "config": {"text": "电脑", "designated_obj": {"variable": "京东网页"}},
            },
        ],
    }

    result = project.validate_mcp_rpa_plan(analysis, plan, available_components=catalog)

    assert result["valid"]
    assert result["plan"]["steps"][0]["outputs"] == ["京东网页"]
    assert result["plan"]["steps"][1]["outputs"] == ["校验结果"]


def test_thin_plan_wires_unambiguous_runtime_inputs_from_live_types():
    analysis = {"analysis_id": "analysis-wiring", "query": "采集网页表格并写入 CSV"}
    catalog = {
        "WebBrowser_CreateV3": {
            "inputs": [
                {"name": "var_name", "isExport": 3, "vType": [{"types": ["rpa_hWeb"], "rule": "const"}]},
                {"name": "type"},
                {"name": "url"},
            ],
            "outputs": {"assign_to": ["var_name"]},
            "code": [{"func": "WebBrowser.CreateV3", "args": ["type", "url"], "assignTo": "var_name"}],
        },
        "Element_SetValue_V6": {
            "inputs": [
                {"name": "var_name", "isExport": 3},
                {"name": "text"},
                {"name": "designated_info"},
                {
                    "name": "designated_obj",
                    "vType": [{"types": ["rpa_hWeb", "rpa_window_new"], "rule": "const"}],
                },
                {"name": "uiSelector"},
            ],
            "outputs": {"assign_to": ["var_name"]},
            "deferred_bindings": ["uiSelector"],
            "code": [{"func": "Element.SetValue", "args": ["text", "designated_obj"], "assignTo": "var_name"}],
        },
        "WebBrowser_DataCollectionV5": {
            "inputs": [
                {"name": "var0", "isExport": 3, "vType": [{"types": ["rpa_list_piliangshuju_v1"], "rule": "const"}]},
                {"name": "var1", "isExport": 3, "vType": [{"types": ["rpa_str"], "rule": "const"}]},
                {"name": "var2", "isExport": 3, "vType": [{"types": ["rpa_dict"], "rule": "const"}]},
                {"name": "config"},
                {"name": "designated_obj", "vType": [{"types": ["rpa_hWeb"], "rule": "const"}]},
                {"name": "toExcel"},
            ],
            "outputs": {"assign_to": ["var0", "var1", "var2"]},
            "deferred_bindings": ["config"],
            "code": [{"func": "WebBrowser.DataCollectionV5", "args": ["config", "designated_obj"], "assignTo": "var0,var1,var2"}],
        },
        "CSV_WriteNew": {
            "inputs": [
                {"name": "path", "vType": [{"types": ["rpa_str_file"], "rule": "const"}]},
                {"name": "encode", "vType": [{"types": ["rpa_str"], "rule": "const"}]},
                {"name": "table", "vType": [{"types": ["rpa_list"], "rule": "const"}]},
            ],
            "outputs": {"assign_to": []},
            "code": [{"func": "CSV.WriteNew", "args": ["path", "encode", "table"]}],
            "materializable": True,
        },
    }
    plan = {
        "analysis_id": analysis["analysis_id"],
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateV3",
                "config": {"var_name": "shop_browser", "type": "chrome", "url": "https://example.test"},
            },
            {
                "id": "search",
                "component": "Element_SetValue_V6",
                "inputs": ["shop_browser"],
                "config": {"var_name": "search_result", "text": "耳机", "designated_obj": ""},
            },
            {
                "id": "collect",
                "component": "WebBrowser_DataCollectionV5",
                "inputs": ["shop_browser"],
                "config": {
                    "var0": "product_rows",
                    "var1": "collection_file",
                    "var2": "empty_rows",
                    "toExcel": 0,
                },
            },
            {
                "id": "write",
                "component": "CSV_WriteNew",
                "inputs": ["product_rows"],
                "config": {"path": "result.csv", "encode": "utf-8"},
            },
        ],
        "output_contract": {"file": "result.csv"},
    }

    result = project.validate_mcp_rpa_plan(analysis, plan, available_components=catalog)
    steps = {item["id"]: item for item in result["plan"]["steps"]}

    assert steps["search"]["config"]["designated_obj"] == {"variable": "shop_browser"}
    assert steps["collect"]["config"]["designated_obj"] == {"variable": "shop_browser"}
    assert steps["write"]["config"]["table"] == {"variable": "product_rows"}
    assert sum("was bound to config" in item for item in result["normalization_warnings"]) == 3


def test_thin_plan_rejects_inputs_without_preceding_producer():
    catalog = {
        "CSV_WriteNew": {
            "inputs": [{"name": "table", "vType": [{"types": ["rpa_list"], "rule": "const"}]}],
            "outputs": {"assign_to": []},
            "code": [{"func": "CSV.WriteNew", "args": ["table"]}],
            "materializable": True,
        }
    }
    plan = {
        "analysis_id": "analysis-dangling",
        "steps": [{"id": "write", "component": "CSV_WriteNew", "inputs": ["missing_rows"], "config": {}}],
    }

    with pytest.raises(ValueError, match="without a preceding producer"):
        project.validate_mcp_rpa_plan(
            {"analysis_id": "analysis-dangling", "query": "写入 CSV"},
            plan,
            available_components=catalog,
        )


def test_thin_plan_rejects_native_dangling_variable_reference():
    analysis = project.analyze_rpa_request("创建原生RPA，打开网页并输入内容。")
    catalog = {
        "WebBrowser_CreateBackground": {
            "inputs": [{"name": "type"}, {"name": "url"}],
            "code": [{"func": "WebBrowser.CreateBackground", "args": ["type", "url"]}],
            "materializable": True,
        },
        "Element_SetValue_V6": {
            "inputs": [{"name": "text"}, {"name": "designated_obj"}],
            "code": [{"func": "Element.SetValue", "args": ["text", "designated_obj"]}],
            "materializable": True,
        },
    }
    plan = {
        "analysis_id": analysis["analysis_id"],
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateBackground",
                "config": {"type": "zexplorer", "url": "https://www.jd.com"},
            },
            {
                "id": "input",
                "component": "Element_SetValue_V6",
                "config": {
                    "text": "电脑",
                    "designated_obj": {"variable": "网页对象"},
                },
            },
        ],
    }

    result = project.preflight_rpa_execution_plan(
        analysis,
        plan,
        available_components=catalog,
        planning_policy="mcp",
    )

    assert result["valid"] is False
    assert result["error_code"] == "dataflow_contract"
    assert "网页对象" in result["error"]
    assert "no preceding producer" in result["error"]


def test_execution_plan_steps_are_authoritative_when_legacy_mirror_drifted():
    flow = {
        "mcpExecutionPlan": {"steps": [{"id": "new", "component": "New_Component"}]},
        "mcpComponentPlan": [{"id": "old", "component": "Old_Component"}],
    }

    steps, source = project._rpa_component_plan(flow)

    assert source == "mcpExecutionPlan.steps"
    assert [item["id"] for item in steps] == ["new"]


def test_parameter_edit_updates_authoritative_plan_and_legacy_mirror_together():
    metadata = {
        "mcpExecutionPlan": {
            "analysis_id": "a",
            "steps": [{"id": "open", "component": "WebBrowser_CreateV3", "config": {"url": "old"}}],
            "output_contract": {},
            "plan_id": "old-plan",
        },
        "mcpComponentPlan": [{"id": "stale", "component": "WebBrowser_CreateV3", "config": {}}],
    }
    contract = {"outputs": ["网页对象"]}

    updates = project._sync_plan_step_metadata(metadata, "open", {"url": "new"}, contract)

    assert updates["mcpExecutionPlan"]["steps"] == updates["mcpComponentPlan"]
    assert updates["mcpExecutionPlan"]["steps"][0]["config"]["url"] == "new"
    assert updates["mcpExecutionPlan"]["steps"][0]["outputs"] == ["网页对象"]
    assert updates["mcpPlanId"] != "old-plan"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for npm launcher tests")
def test_npm_codex_registration_is_idempotent_and_verifies_persisted_config(tmp_path):
    install_root = tmp_path / "ShizaiAgent"
    install_root.mkdir()
    (install_root / "shizai-agent-mcp.exe").touch()
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '[mcp_servers.other]\ncommand = "other.exe"\n\n'
        '[mcp_servers.other.env]\nKEEP = "yes"\n',
        encoding="utf-8",
    )
    launcher = Path(__file__).parents[1] / "npm" / "shizai-agent-mcp" / "bin" / "shizai-agent-mcp.js"
    env = {
        **os.environ,
        "SHIZAI_AGENT_HOME": str(install_root),
        "SHIZAI_CODEX_CONFIG": str(config),
    }

    for _ in range(2):
        subprocess.run(
            [shutil.which("node"), str(launcher), "register", "codex"],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    persisted = config.read_text(encoding="utf-8")
    assert persisted.count("[mcp_servers.shizai-agent]") == 1
    assert persisted.count("[mcp_servers.shizai-agent.env]") == 1
    assert '[mcp_servers.other.env]\nKEEP = "yes"' in persisted
    assert "enabled = true" in persisted
    assert 'SHIZAI_MCP_TOOL_PROFILE = "rpa-authoring"' in persisted


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


def test_authoring_catalog_bounds_broad_and_exact_queries(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server,
        "_dispatch",
        lambda name, arguments: calls.append((name, dict(arguments))) or {"components": []},
    )
    server = mcp_server.build_server(profile="rpa-authoring")

    async def run():
        handler = server.request_handlers[types.CallToolRequest]
        for arguments in (
            {"capability": "navigate,input,extract", "detail": "full", "limit": 100},
            {"component_type": "WebBrowser_DataCollectionV5", "detail": "contract", "limit": 100},
        ):
            await handler(types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="shizai_rpa_component_catalog",
                    arguments=arguments,
                ),
            ))

    asyncio.run(run())

    broad = calls[0][1]
    exact = calls[1][1]
    assert broad["detail"] == "summary"
    assert broad["limit"] == 20
    assert broad["include_hidden"] is False
    assert exact["detail"] == "contract"
    assert exact["limit"] == 5


def test_similar_element_list_is_exposed_as_native_link_collection_fallback():
    contract = component_catalog.component_capabilities({
        "type": "Element_SimilarElementList_V2",
        "category": ["web_element_operation"],
        "inputs": [
            {"name": "var0", "isExport": 3, "defaultValue": "相似元素列表"},
            {"name": "uiSelector"},
            {"name": "action"},
            {"name": "attr_complate"},
        ],
        "outputs": {"primary": "var0", "assign_to": ["var0"]},
    })

    assert {"extract", "list", "link"}.issubset(contract["capabilities"])
    assert contract["configuration_hints"]["similar_element_actions"]["absoluteurl"]
    pattern = next(item for item in contract["planning_patterns"] if item["semantic_config"].get("action") == "absoluteurl")
    assert pattern["semantic_config"]["attr_complate"] is True


def test_extension_resolves_only_real_http_link_attributes():
    candidate = {"selector_hint": {"href": "/item/1001"}}

    assert extension_bridge._candidate_link_url(candidate, "https://shop.example/list") == (
        "https://shop.example/item/1001"
    )
    assert extension_bridge._candidate_link_url(
        {"selector_hint": {"href": "javascript:void(0)"}},
        "https://shop.example/list",
    ) == ""


def test_data_collection_catalog_warns_about_nested_table_href_runtime_limit():
    contract = component_catalog.component_capabilities({
        "type": "WebBrowser_DataCollectionV5",
        "category": "get_web_data",
        "inputs": [
            {"name": name}
            for name in ("config", "collect_items", "collect_pages", "selector_page", "toExcel")
        ],
        "outputs": {"primary": "var0", "assign_to": ["var0"]},
    })

    limit = contract["configuration_hints"]["runtime_capability_limits"]
    assert "tableSelector" in limit["table_nested_attribute"]
    assert "Element_SimilarElementList_V2" in limit["required_action"]


def test_mcp_slow_desktop_call_does_not_block_protocol_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def dispatch(_name, arguments):
        if arguments.get("component_type") == "slow":
            started.set()
            release.wait(1.0)
        return {"components": []}

    monkeypatch.setattr(mcp_server, "_dispatch", dispatch)
    server = mcp_server.build_server(profile="rpa-authoring")

    async def call(component_type):
        handler = server.request_handlers[types.CallToolRequest]
        return await handler(types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="shizai_rpa_component_catalog",
                arguments={"component_type": component_type},
            ),
        ))

    async def run():
        timer = threading.Timer(1.0, release.set)
        timer.start()
        before = time.monotonic()
        slow = asyncio.create_task(call("slow"))
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.wait_for(call("fast"), timeout=0.5)
        elapsed = time.monotonic() - before
        release.set()
        await slow
        timer.cancel()
        assert elapsed < 0.5

    asyncio.run(run())


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


def test_compact_creation_removes_repeated_validation_trace():
    full = {
        "kind": "shizai_rpa_started",
        "flow_ref": "test",
        "planned_components": [{
            "step": {"id": "collect", "component": "WebBrowser_DataCollectionV5", "binding_status": "unbound"},
            "step_validation": {
                "kind": "rpa_step_validation",
                "step_id": "collect",
                "component": "WebBrowser_DataCollectionV5",
                "valid": False,
                "checks": [{"name": f"check-{index}", "detail": "x" * 1000} for index in range(20)],
                "errors": ["bound element count: 0"],
                "warnings": [],
                "output_variables": ["数据采集结果"],
                "verification_gate": {
                    "status": "static_failed",
                    "runtime_validation": {"verified": False, "tools": ["large"] * 100},
                    "next_action": {"tool": "shizai_rpa_validate_step"},
                },
            },
        }],
        "validated_plan": {"valid": True},
    }

    compact = compact_design_result(full)
    validation = compact["steps"][0]["step_validation"]
    assert "checks" not in validation and "verification_gate" not in validation
    assert validation["errors"] == ["bound element count: 0"]
    assert validation["verification"]["status"] == "static_failed"
    assert len(json.dumps(compact, ensure_ascii=False)) < 1500


def test_authoring_validation_suggests_the_run_boundary_without_authorizing_execution():
    value = {"valid": True, "next_tool": "shizai_rpa_validate_project", "errors": []}
    result = authoring_result("shizai_rpa_validate_flow", value, {"flow_ref": "test"})
    names = {tool.name for tool in mcp_server._schemas("rpa-authoring")}
    assert result["next_tool"] in names and result["next_action"]["tool"] in names
    assert result["next_tool"] == "shizai_rpa_run"
    assert result["next_action"]["arguments"] == {"project_ref": "test"}
    assert value["next_tool"] == "shizai_rpa_validate_project"
    failure = {"valid": False, "errors": ["bad parameters"]}
    repaired = authoring_result("shizai_rpa_validate_flow", failure, {"flow_ref": "test"})
    assert repaired["errors"] == failure["errors"]
    assert repaired["next_action"]["tool"] == "shizai_rpa_read_flow"


def test_authoring_profile_exposes_atomic_field_capture_and_pagination_binding():
    tools = {tool.name: tool for tool in mcp_server._schemas("rpa-authoring")}

    assert "shizai_rpa_capture_page_fields" in RPA_AUTHORING_TOOLS
    assert "shizai_rpa_sync_diagnose" in RPA_AUTHORING_TOOLS
    assert "shizai_rpa_capture_page_fields" in tools
    assert "shizai_rpa_sync_diagnose" in tools
    assert "shizai_rpa_materialize" not in tools
    assert set(tools["shizai_rpa_capture_page_fields"].inputSchema["required"]) == {
        "project_ref", "step_id", "allow_write",
    }
    capture = tools["shizai_rpa_capture_elements"].inputSchema["properties"]
    assert capture["as_page_selector"]["type"] == "boolean"
    assert capture["pagination_mode"]["enum"] == ["next-page", "load-more"]


def test_result_capture_requirements_follow_only_requested_columns():
    requested = ["商品标题", "价格", "商品链接", "销量"]

    assert project._result_capture_requirements(requested, use_js_link=False) == {
        "product_name", "price", "link", "sales",
    }
    assert project._result_capture_requirements(["商品标题", "价格"], use_js_link=False) == {
        "product_name", "price",
    }
    assert project._result_capture_requirements(requested, use_js_link=True) == {
        "product_name", "price", "sales",
    }


def test_unbound_collection_next_action_uses_atomic_field_capture():
    action = project._rpa_next_action({
        "flow_ref": "FLOW_REF",
        "required_columns": ["商品标题", "价格", "商品链接", "销量"],
        "steps": [{
            "id": "collect",
            "component": "WebBrowser_DataCollectionV5",
            "binding_status": "unbound",
        }],
        "mcpExecutionPlan": {"steps": [{
            "id": "collect",
            "component": "WebBrowser_DataCollectionV5",
            "element_role": "商品字段",
        }]},
    })

    assert action["tool"] == "shizai_rpa_capture_page_fields"
    assert action["arguments"]["step_id"] == "collect"
    assert action["arguments"]["fields"] == ["product_name", "price", "sales", "link"]


def test_compact_browser_design_opens_page_before_validation():
    result = compact_design_result({
        "kind": "shizai_rpa_started",
        "flow_ref": "FLOW_REF",
        "design_complete": True,
        "planned_components": [{
            "step": {
                "id": "search",
                "component": "Element_SetValue_V6",
                "binding_status": "unbound",
            },
            "step_validation": {"valid": False, "errors": ["bound element count: 0"]},
        }],
    })

    assert result["next_action"]["tool"] == "shizai_rpa_open_browser"
    assert result["next_action"]["arguments"] == {"project_ref": "FLOW_REF", "allow_write": True}


def test_invalid_collection_validation_returns_small_executable_repair_action():
    base = {
        "flow_ref": "FLOW_REF",
        "valid": False,
        "errors": ["step collect: bound element count: 0"],
        "warnings": [],
        "step_validations": [{
            "step_id": "collect",
            "component": "WebBrowser_DataCollectionV5",
            "valid": False,
            "errors": ["bound element count: 0", "selector_page is required"],
            "warnings": [],
            "checks": [{"name": "large", "detail": "x" * 10000}],
            "verification_gate": {"status": "static_failed"},
        }],
    }

    result = authoring_result("shizai_rpa_validate_flow", base, {"flow_ref": "FLOW_REF"})

    assert result["next_action"]["tool"] == "shizai_rpa_capture_page_fields"
    assert result["next_action"]["arguments"]["step_id"] == "collect"
    assert "checks" not in result["step_validations"][0]
    assert len(json.dumps(result, ensure_ascii=False)) < 1000

    pagination = json.loads(json.dumps(base))
    pagination["step_validations"][0]["errors"] = ["selector_page is required"]
    pagination["errors"] = ["step collect: selector_page is required"]
    result = authoring_result("shizai_rpa_validate_flow", pagination, {"flow_ref": "FLOW_REF"})
    assert result["next_action"]["tool"] == "shizai_rpa_scan_page"
    assert result["next_action"]["arguments"]["query"] == "下一页"


def test_mcp_atomic_page_capture_imports_and_binds_only_when_complete(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            self.desktop = desktop

        def ensure_capture_session(self, *, package_path=None):
            return {"started": True, "package_path": package_path}

        def capture_page_fields(self, **kwargs):
            events.append(("capture", kwargs))
            return {
                "kind": "shizai_page_fields_captured",
                "complete": True,
                "fields": kwargs["fields"],
                "required_fields": kwargs["fields"],
                "selected_field_ids": {"product_name": "title", "price": "price"},
                "elements": [{"id": "title"}, {"id": "price"}],
                "element_count": 2,
                "missing_fields": [],
                "selector_scope": "card-a",
            }

        def stop_inspect(self):
            return {"overlay_state": "inactive"}

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, *, force=False, request_id=None):
            return {"visible": False}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", FakeMask)
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", lambda *_args, **_kwargs: {"id": "collect"})
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *_args: {"flow": {"required_columns": ["商品标题", "价格"]}},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "import_elements",
        lambda *_args, **_kwargs: events.append(("import",)) or {"imported": ["title", "price"]},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "set_rpa_step_elements",
        lambda *_args, **_kwargs: events.append(("bind",)) or {"valid": True},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "mark_rpa_result_capture_ready",
        lambda *_args, **_kwargs: events.append(("mark",)) or {
            "result_elements_ready": True,
            "next_action": {"tool": "shizai_rpa_validate_step"},
        },
    )

    result = mcp_server._dispatch_impl("shizai_rpa_capture_page_fields", {
        "project_ref": "FLOW_REF", "step_id": "collect", "allow_write": True,
    })

    assert result["complete"] is True
    assert [item[0] for item in events] == ["capture", "import", "bind", "mark"]
    assert events[0][1]["fields"] == ["product_name", "price"]


def test_mcp_atomic_page_capture_does_not_write_incomplete_fields(monkeypatch):
    class FakeBridge:
        def __init__(self, desktop=None):
            self.desktop = desktop

        def ensure_capture_session(self, *, package_path=None):
            return {"started": True}

        def capture_page_fields(self, **_kwargs):
            return {"complete": False, "missing_fields": ["sales"], "elements": []}

        def stop_inspect(self):
            return {}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", lambda client=None: type("Mask", (), {"close": lambda self, **_: {}})())
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", lambda *_args, **_kwargs: {"id": "collect"})
    monkeypatch.setattr(mcp_server.project_core, "read_rpa_flow", lambda *_args: {"flow": {}})
    monkeypatch.setattr(
        mcp_server.project_core,
        "import_elements",
        lambda *_args, **_kwargs: pytest.fail("incomplete capture must not import elements"),
    )

    result = mcp_server._dispatch_impl("shizai_rpa_capture_page_fields", {
        "project_ref": "FLOW_REF", "step_id": "collect",
        "fields": ["product_name", "sales"], "allow_write": True,
    })

    assert result["complete"] is False
    assert result["next_action"]["tool"] == "shizai_rpa_capture_page_fields"
    assert result["next_action"]["arguments"]["candidate_limit"] == 8


def test_mcp_atomic_page_capture_stops_link_recapture_when_recovery_is_available(monkeypatch):
    class FakeBridge:
        def __init__(self, desktop=None):
            self.desktop = desktop

        def ensure_capture_session(self, *, package_path=None):
            return {"started": True}

        def capture_page_fields(self, **_kwargs):
            return {
                "complete": False,
                "missing_fields": ["link"],
                "elements": [],
                "selected_card_context": "card-family|@index=0",
                "recovery_actions": [{
                    "tool": "shizai_rpa_add_step",
                    "component": "WebBrowser_RunJsInIeBySelector_v3",
                    "workflow_reference": "jd_product_card_link_v1",
                    "config_hint": {
                        "selector_source": "observed_card_context",
                        "card_context": {"class": "card-family", "index": "0"},
                        "input": ".card-family",
                    },
                    "next": "revise the plan from live card context",
                }],
            }

        def stop_inspect(self):
            return {}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", lambda client=None: type("Mask", (), {"close": lambda self, **_: {}})())
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", lambda *_args, **_kwargs: {"id": "collect"})
    monkeypatch.setattr(mcp_server.project_core, "read_rpa_flow", lambda *_args: {"flow": {}})

    result = mcp_server._dispatch_impl("shizai_rpa_capture_page_fields", {
        "project_ref": "FLOW_REF", "step_id": "collect",
        "fields": ["product_name", "price", "sales", "link"], "allow_write": True,
    })

    assert result["next_action"]["tool"] == "shizai_rpa_add_step"
    assert result["next_action"]["arguments"]["before_step_id"] == "collect"
    assert result["next_action"]["arguments"]["config"]["workflow_reference"] == "jd_product_card_link_v1"
    assert result["next_action"]["arguments"]["config"]["input"] == ".card-family"
    assert result["next_action"]["plan_revision"]["config_hint"]["selector_source"] == "observed_card_context"


def test_mcp_atomic_page_capture_uses_native_absolute_url_recovery_without_text_binding(monkeypatch):
    class FakeBridge:
        def __init__(self, desktop=None):
            self.desktop = desktop

        def ensure_capture_session(self, *, package_path=None):
            return {"started": True}

        def capture_page_fields(self, **_kwargs):
            return {
                "complete": False,
                "missing_fields": ["link"],
                "elements": [{"id": "title", "name": "商品标题"}],
                "selected_field_ids": {"product_name": "title"},
                "recovery_actions": [{
                    "tool": "shizai_rpa_add_step",
                    "component": "Element_SimilarElementList_V2",
                    "config_hint": {
                        "selector_source": "native_extension_picker",
                        "config": {
                            "var0": "商品链接列表",
                            "var1": "商品链接数量",
                            "element_position": "default",
                            "designated_info": 1,
                            "designated_obj": {"variable": "网页对象"},
                            "action": "absoluteurl",
                            "attr_complate": 1,
                            "start_index": 1,
                            "end_index": -1,
                        },
                    },
                    "next": "capture one native anchor",
                }],
            }

        def stop_inspect(self):
            return {}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", lambda client=None: type("Mask", (), {"close": lambda self, **_: {}})())
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", lambda *_args: {"id": "collect"})
    monkeypatch.setattr(mcp_server.project_core, "read_rpa_flow", lambda *_args: {"flow": {}})
    monkeypatch.setattr(
        mcp_server.project_core,
        "import_elements",
        lambda *_args, **_kwargs: {"imported": ["title"]},
    )

    result = mcp_server._dispatch_impl("shizai_rpa_capture_page_fields", {
        "project_ref": "FLOW_REF",
        "step_id": "collect",
        "fields": ["product_name", "link"],
        "allow_write": True,
    })

    action = result["next_action"]
    assert action["arguments"]["component"] == "Element_SimilarElementList_V2"
    assert action["arguments"]["element_ids"] == []
    assert action["arguments"]["config"]["action"] == "absoluteurl"
    assert action["arguments"]["config"]["attr_complate"] == 1
    assert action["plan_revision"]["workflow_reference"] is None


def test_capture_read_reconnects_once_for_transient_channel_error(monkeypatch):
    class FakeBridge:
        desktop = object()
        window_probes = 0

        @staticmethod
        def _is_transient_capture_error(error):
            return "channel_unavailable" in str(error)

        def start_capture_session(self, *, package_path=None):
            return {"started": True, "package_path": package_path}

        def capture_windows(self, *, include_non_chrome=False):
            self.window_probes += 1
            return {"count": 1, "include_non_chrome": include_non_chrome}

    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ShizaiCloudError("reason=channel_unavailable")
        return {"complete": True}

    bridge = FakeBridge()
    result, recovery = mcp_server._rpa_capture_read_with_recovery(bridge, "FLOW_REF", operation)

    assert calls == 2
    assert bridge.window_probes == 1
    assert recovery == {"started": True, "package_path": "FLOW_REF"}
    assert result["capture_session_recovery"]["reconnected"] is True
    assert result["capture_session_recovery"]["window_count"] == 1


def test_capture_read_reconnects_for_embedded_transient_capture_errors(monkeypatch):
    class FakeBridge:
        @staticmethod
        def _is_transient_capture_error(error):
            return "channel_unavailable" in str(error)

        def start_capture_session(self, *, package_path=None):
            return {"started": True, "package_path": package_path}

        def capture_windows(self, *, include_non_chrome=False):
            return {"count": 1}

    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "complete": False,
                "capture_errors": [{"element_id": "uia:1", "error": "reason=channel_unavailable"}],
            }
        return {"complete": True, "capture_errors": []}

    result, recovery = mcp_server._rpa_capture_read_with_recovery(FakeBridge(), "FLOW_REF", operation)

    assert calls == 2
    assert recovery == {"started": True, "package_path": "FLOW_REF"}
    assert result["complete"] is True
    assert result["capture_session_recovery"]["reconnected"] is True


def test_capture_read_keeps_actionable_partial_result_without_full_retry(monkeypatch):
    class FakeBridge:
        @staticmethod
        def _is_transient_capture_error(error):
            return "channel_unavailable" in str(error)

        def start_capture_session(self, *, package_path=None):
            pytest.fail("an actionable partial result must not restart and repeat the full capture")

    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return {
            "complete": False,
            "missing_fields": ["link"],
            "capture_errors": [{"element_id": "uia:unused", "error": "reason=channel_unavailable"}],
            "recovery_actions": [{"tool": "shizai_rpa_add_step"}],
        }

    result, recovery = mcp_server._rpa_capture_read_with_recovery(FakeBridge(), "FLOW_REF", operation)

    assert calls == 1
    assert recovery is None
    assert result["missing_fields"] == ["link"]


def test_capture_read_keeps_complete_result_with_incidental_transient_error(monkeypatch):
    class FakeBridge:
        @staticmethod
        def _is_transient_capture_error(error):
            return "channel_unavailable" in str(error)

        def start_capture_session(self, *, package_path=None):
            pytest.fail("a complete result must not restart and repeat the full capture")

    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return {
            "complete": True,
            "missing_fields": [],
            "capture_errors": [{"element_id": "uia:unused", "error": "reason=channel_unavailable"}],
            "capture_errors_blocking": False,
        }

    result, recovery = mcp_server._rpa_capture_read_with_recovery(FakeBridge(), "FLOW_REF", operation)

    assert calls == 1
    assert recovery is None
    assert result["complete"] is True


def test_scan_page_reconnects_once_for_transient_channel_error(monkeypatch):
    scan_calls = 0
    session_calls = 0

    class FakeBridge:
        def __init__(self, desktop=None):
            self.desktop = desktop

        @staticmethod
        def _is_transient_capture_error(error):
            return "channel_unavailable" in str(error)

        def ensure_capture_session(self, *, package_path=None):
            nonlocal session_calls
            session_calls += 1
            return {"started": True, "package_path": package_path}

        start_capture_session = ensure_capture_session

        def capture_windows(self, *, include_non_chrome=False):
            return {"count": 1, "windows": [{"element_id": "wnd:1"}]}

        def scan_page(self, **_kwargs):
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                raise ShizaiCloudError("reason=channel_unavailable")
            return {"candidate_count": 1, "candidates": [{"element_id": "uia:1"}]}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server.project_core, "read_rpa_flow", lambda *_args, **_kwargs: {"flow": {"steps": []}})
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})

    result = mcp_server._dispatch_impl("shizai_rpa_scan_page", {
        "project_ref": "FLOW_REF",
        "step_id": "search",
        "query": "搜索框",
    })

    assert scan_calls == 2
    assert session_calls == 2
    assert result["candidate_count"] == 1
    assert result["capture_session_recovery"]["reconnected"] is True


def test_capture_elements_can_bind_pagination_without_replacing_columns(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def ensure_capture_session(self, *, package_path=None):
            return {"started": True}

        def capture_elements(self, element_ids, **_kwargs):
            return {"elements": [{"id": element_ids[0]}]}

        def stop_inspect(self):
            return {}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", lambda client=None: type("Mask", (), {"close": lambda self, **_: {}})())
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        mcp_server.project_core,
        "import_elements",
        lambda *_args, **_kwargs: {"imported": ["next-selector"]},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "bind_rpa_pagination",
        lambda *_args, **kwargs: events.append(kwargs) or {"mode": kwargs["mode"], "columns_unchanged": True},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "set_rpa_step_elements",
        lambda *_args, **_kwargs: pytest.fail("pagination must not replace collection fields"),
    )

    result = mcp_server._dispatch_impl("shizai_rpa_capture_elements", {
        "project_ref": "FLOW_REF", "step_id": "collect", "element_ids": ["uia:next"],
        "as_page_selector": True, "pagination_mode": "next-page", "allow_write": True,
    })

    assert result["pagination_binding"] is True
    assert result["binding"]["columns_unchanged"] is True
    assert len(events) == 1 and events[0]["mode"] == "next-page"
    assert events[0]["client"] is not None


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

    blocked = authoring_result(
        "shizai_rpa_run",
        {
            "kind": "native_rpa_run",
            "status": "blocked",
            "failure_kind": "runtime_not_ready",
            "preflight": {
                "valid": False,
                "reason": "runtime_not_ready",
                "materialization_required": False,
                "errors": ["collection selector is invalid"],
                "warnings": ["runtime plugin has no current handshake"],
            },
            "next_action": {"tool": "shizai_rpa_read_flow", "arguments": {"step_id": "collect"}},
        },
        {"project_ref": "project"},
    )
    assert blocked["preflight"] == {
        "valid": False,
        "reason": "runtime_not_ready",
        "materialization_required": False,
        "errors": ["collection selector is invalid"],
        "warnings": ["runtime plugin has no current handshake"],
    }
    assert blocked["next_tool"] == "shizai_rpa_read_flow"
    assert blocked["next_action"]["arguments"]["step_id"] == "collect"


def test_authoring_sync_diagnosis_is_compact_and_actionable():
    value = {
        "kind": "native_rpa_runtime_diagnosis",
        "project": "project",
        "can_run": False,
        "can_attempt_run": True,
        "materialization_required": True,
        "errors": ["task index is stale"],
        "warnings": [],
        "package": {
            "matched": True,
            "application_id": "app-1",
            "package_id": "package-1",
            "runtime_version": "7.3.6.1",
            "rows": [{"large": "omitted"}],
        },
        "runtime": {
            "ready": True,
            "runtime_browser_plugin_status": "unknown",
            "runtime_browser_plugin_ready": None,
            "message": "首次运行将实时探测",
            "runtime_host_log": "large.log",
        },
        "validation": {"large": "omitted"},
    }

    result = authoring_result("shizai_rpa_sync_diagnose", value, {"project_ref": "project"})

    assert result["materialization_required"] is True
    assert result["errors"] == ["task index is stale"]
    assert result["next_tool"] == "shizai_rpa_run"
    assert result["next_action"]["arguments"]["allow_write"] is True
    assert "rows" not in result["package"]
    assert "validation" not in result


def test_capture_window_selection_prefers_target_page_over_browser_prompt():
    windows = [
        {
            "element_id": "wnd:restore",
            "page_title": "要恢复页面吗？",
            "page_url": "",
        },
        {
            "element_id": "wnd:shop",
            "page_title": "组件测评商城 - Google Chrome",
            "page_url": "",
        },
        {
            "element_id": "wnd:other",
            "page_title": "其他网页 - Google Chrome",
            "page_url": "https://other.example/products",
        },
    ]

    selected = mcp_server._select_capture_window(
        windows,
        target_url="http://127.0.0.1:18766/shop?layout=cards",
    )

    assert selected["element_id"] == "wnd:shop"


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
    for key in (
        "components", "ready", "source", "filters", "has_more", "next_page",
        "matched_components", "selection_guidance",
    ):
        assert compact[key] == full[key]
    assert "role_selection" not in compact and "planning_guidance" not in compact
    static = component_catalog.component_catalog(paths, **{**options, "query": "Basic_DebugOutput"})["components"][0]
    assert static["materializable"] is False
    assert static["materialization_errors"]


def test_compact_catalog_marks_summary_and_exact_contract_lookup_phases(tmp_path):
    paths = ShizaiPaths(tmp_path, tmp_path / "apps", tmp_path / "db", tmp_path / "run", None, None)
    engine = tmp_path / "engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(json.dumps([{
        "type": "File_Write", "label": "写入文件", "baseType": "File_Write",
        "definition": {"args0": [{"name": "content", "defaultValue": '""'}]},
        "codeDesc": [{"func": "File.Write", "args": ["content"]}],
    }]), encoding="utf8")

    summary = component_catalog.component_catalog(
        paths, engine_root=str(engine), scope="builtin", detail="summary", response_mode="compact",
    )
    exact = component_catalog.component_catalog(
        paths, engine_root=str(engine), scope="builtin", component_type="File_Write",
        detail="contract", response_mode="compact",
    )

    assert summary["selection_guidance"]["phase"] == "shortlist_ready"
    assert summary["selection_guidance"]["component_types"] == [
        item["type"] for item in summary["components"]
    ]
    assert "File_Write" in summary["selection_guidance"]["component_types"]
    assert "不要猜测" in summary["selection_guidance"]["next"]
    assert exact["selection_guidance"] == {
        "phase": "contract_ready",
        "component_types": ["File_Write"],
        "contract_complete": True,
        "repeat_lookup": False,
        "next": (
            "保留并使用本次返回的契约；同一请求不要再次查询这些 component_type。"
            "如果所有最终步骤的契约均已读取，直接构造并校验计划。"
        ),
    }
