"""Unit tests for the Shizai harness core modules."""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import textwrap
import time
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from shizai_agent import shizai_agent_cli as cli_module
from shizai_agent.core import (
    agent,
    component_catalog,
    debug_session,
    doctor,
    extension_bridge,
    mcp_server,
    project,
)
from shizai_agent.core import (
    cloud_api as cloud_module,
)
from shizai_agent.core.cloud_api import (
    KNOWLEDGE_QA_PROMPT,
    ShizaiCloudApi,
    ShizaiCloudConfig,
    ShizaiCloudError,
    ShizaiDesktopAgent,
    _desktop_channel_lock,
    _extract_file_id,
    _extract_http_urls,
    _extract_retriever_sources,
    _normalize_model_tables,
    merge_native_feishu_config,
    native_feishu_summary,
    remove_native_feishu_config,
)
from shizai_agent.core.extension_bridge import (
    ShizaiExtensionBridge,
    _element_record,
    _find_chrome_executable,
    _native_selector,
    _tree_has_web_element,
)
from shizai_agent.core.native_ui import (
    NativeMask,
    NativeScanDemo,
    NativeScreenshotCapture,
    NativeSmallCard,
    NativeTakeoverFrame,
)
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.project import (
    _collection_property,
    _collection_selector_chain,
    _native_normalize_collection_config,
)
from shizai_agent.shizai_agent_cli import _ingest_source, cli


def _paths(tmp_path: Path, install_root: Path | None = None) -> ShizaiPaths:
    root = tmp_path / "Z-Factory"
    return ShizaiPaths(
        data_root=root,
        applications_root=root / "applications",
        sqlite_db=root / "sqlite.db",
        run_config=root / "run.config.json",
        install_root=install_root,
        executable=None,
    )


def _make_project(paths: ShizaiPaths, project_id: str = "c_test") -> Path:
    target = paths.applications_root / project_id
    target.mkdir(parents=True)
    (target / "szrpa.json").write_text(
        json.dumps({"packageName": "Synthetic Flow", "projectMode": "visual"}), encoding="utf-8"
    )
    (target / "elements.json").write_text("{}", encoding="utf-8")
    (target / "project.flow").write_text("flow", encoding="utf-8")
    (target / "main.sz").write_text("main", encoding="utf-8")
    return target


def test_paths_from_environment(monkeypatch, tmp_path):
    root = tmp_path / "custom"
    monkeypatch.setenv("SHIZAI_DATA_ROOT", str(root))
    resolved = ShizaiPaths.from_environment()
    assert resolved.data_root == root
    assert resolved.applications_root == root / "applications"


def test_read_only_query(tmp_path):
    paths = _paths(tmp_path)
    paths.data_root.mkdir(parents=True)
    with sqlite3.connect(paths.sqlite_db) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER, name TEXT)")
        connection.execute("INSERT INTO sample VALUES (1, 'one')")
    from shizai_agent.core.storage import query_rows

    assert query_rows(paths, "SELECT * FROM sample") == [{"id": 1, "name": "one"}]


def test_list_and_inspect_project(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths)
    listed = project.list_projects(paths)
    assert listed[0]["id"] == "c_test"
    assert project.inspect_project(paths, str(target))["name"] == "Synthetic Flow"


def test_resolve_project_by_id(tmp_path):
    paths = _paths(tmp_path)
    _make_project(paths)
    assert project.resolve_project(paths, "c_test").name == "c_test"


def test_validate_project_passes(tmp_path):
    paths = _paths(tmp_path)
    _make_project(paths)
    result = project.validate_project(paths, "c_test")
    assert result["valid"] is True
    assert all(item["passed"] for item in result["checks"])


def test_validate_project_reports_missing_native_file(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths)
    (target / "main.sz").unlink()
    result = project.validate_project(paths, "c_test")
    assert result["valid"] is False
    assert any(item["name"] == "main.sz" and not item["passed"] for item in result["checks"])


def test_read_elements_returns_native_selector_summary(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths)
    (target / "elements.json").write_text(
        json.dumps(
            {
                "elementMap": {
                    "search": {
                        "id": "search",
                        "name": "搜索框",
                        "GUIType": "Chrome",
                        "selectorType": "comboSelector",
                        "selector": {
                            "commonProps": {"cssSelector": '"#search"'},
                            "elementDetail": {"windowName": "京东"},
                        },
                    }
                },
                "groupMap": {"g": {}},
            }
        ),
        encoding="utf-8",
    )
    result = project.read_elements(paths, "c_test")
    assert result["element_count"] == 1
    assert result["elements"][0]["css_selector"] == "#search"
    assert result["native"]["elementMap"]["search"]["selectorType"] == "comboSelector"


def test_import_elements_converts_capture_and_preserves_native_shape(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths)
    result = project.import_elements(
        paths,
        "c_test",
        [
            {
                "id": "jd_search",
                "name": "京东搜索框",
                "gui_type": "Chrome",
                "title": "京东首页",
                "url": "https://www.jd.com/",
                "tag": "input",
                "css_selector": "#search input",
                "attributes": {"aria-label": "搜索", "type": "text"},
            }
        ],
    )
    assert result["imported"] == ["jd_search"]
    payload = json.loads((target / "elements.json").read_text(encoding="utf-8"))
    element = payload["elementMap"]["jd_search"]
    assert element["selectorType"] == "comboSelector"
    assert element["selector"]["commonProps"]["cssSelector"] == '"#search input"'
    assert element["selector"]["selector"][0]["name"] == "tab"
    assert result["ui_feedback"]["phase"] == "elements_ready"
    assert result["ui_feedback"]["animation"] == "element_ready"


def test_model_candidate_import_is_draft_and_blocks_flow_validation(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "candidate-flow"
    project.create_rpa_flow(
        str(staged),
        name="候选元素流程",
        target_url="https://example.test/",
        use_excel_components=False,
    )
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open")
    record = {
        "id": "candidate-search",
        "name": "搜索框",
        "title": "结果页",
        "url": "https://example.test/",
        "tag": "input",
        "css_selector": "#search",
        "attributes": {"aria-label": "搜索"},
    }
    imported = project.import_elements(paths, str(staged), [record], capture_source="model_candidate")
    assert imported["verification_status"] == "unverified"
    assert imported["staged_flow"]["phase"] == "elements_draft"
    project.add_rpa_step(
        paths,
        str(staged),
        "Element_SetValue_V6",
        step_id="search",
        element_ids=["candidate-search"],
        config={"text": "手机"},
    )
    result_state = project.mark_rpa_result_capture_ready(
        paths,
        str(staged),
        captured_fields={"product_name": "candidate-search"},
        field_element_ids={"product_name": "candidate-search"},
        use_js_table=True,
    )
    assert result_state["result_elements_ready"] is False
    assert result_state["unverified_element_ids"] == ["candidate-search"]
    validation = project.validate_rpa_flow(paths, str(staged))
    assert validation["valid"] is False
    assert validation["unverified_element_ids"] == ["candidate-search"]
    assert any("unverified elements" in item for item in validation["errors"])


def test_deep_capture_import_is_allowed_and_result_ready_requires_verified_fields(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "verified-flow"
    project.create_rpa_flow(
        str(staged),
        name="真实捕获流程",
        target_url="https://example.test/",
        use_excel_components=False,
    )
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open")
    records = [
        {
            "id": "verified-search",
            "name": "搜索框",
            "title": "首页",
            "url": "https://example.test/",
            "tag": "input",
            "css_selector": "#search",
            "attributes": {"aria-label": "搜索"},
        },
        {
            "id": "candidate-name",
            "name": "商品名称",
            "title": "结果页",
            "url": "https://example.test/results",
            "tag": "div",
            "css_selector": ".product-name",
            "attributes": {"data-testid": "product-name"},
        },
    ]
    project.import_elements(paths, str(staged), records, capture_source="deep_capture")
    project.add_rpa_step(
        paths,
        str(staged),
        "Element_SetValue_V6",
        step_id="search",
        element_ids=["verified-search"],
        config={"text": "手机"},
    )
    ready = project.mark_rpa_result_capture_ready(
        paths,
        str(staged),
        captured_fields={"product_name": "candidate-name"},
        field_element_ids={"product_name": "candidate-name"},
        use_js_table=True,
    )
    assert ready["result_elements_ready"] is True
    assert ready["unverified_element_ids"] == []
    validation = project.validate_rpa_flow(paths, str(staged))
    assert validation["unverified_element_ids"] == []


def test_validate_captured_elements_rejects_coordinate_only_and_missing_page_identity():
    result = project.validate_captured_elements(
        [
            {
                "id": "bad",
                "name": "按钮",
                "tag": "button",
                "location": [100, 200],
            }
        ]
    )
    assert result["valid"] is False
    assert any("page title or page URL" in error for error in result["errors"])
    assert any("coordinate-only" in error for error in result["errors"])


def test_validate_captured_elements_rejects_index_only_selector_and_accepts_stable_selector():
    unstable = project.validate_captured_elements(
        [
            {
                "id": "unstable",
                "name": "列表项",
                "title": "结果页",
                "url": "https://example.test/results",
                "tag": "div",
                "css_selector": "#root > div:nth-child(3) > div:nth-child(1)",
            }
        ]
    )
    assert unstable["valid"] is False
    assert any("nth-child" in error for error in unstable["errors"])

    stable = project.validate_captured_elements(
        [
            {
                "id": "stable",
                "name": "搜索框",
                "title": "首页",
                "url": "https://example.test/",
                "tag": "input",
                "css_selector": "#search input",
                "attributes": {"aria-label": "搜索"},
            }
        ]
    )
    assert stable["valid"] is True
    assert stable["stable_element_count"] == 1


def test_strict_element_validation_accepts_title_and_stable_selector_without_url():
    result = project.validate_captured_elements(
        [
            {
                "id": "search-input",
                "name": "搜索框",
                "title": "目标站点 - Google Chrome",
                "tag": "input",
                "css_selector": "input.search-input",
                "attributes": {"aria-label": "搜索"},
            }
        ],
        strict=True,
    )

    assert result["valid"] is True
    assert result["warnings"] == []
    assert any("page URL is missing" in item for item in result["advisories"])


def test_data_collection_properties_use_raw_runtime_values():
    prop = _collection_property("class", "plugin_goodsCardWrapper _row_1")
    assert prop is not None
    assert prop["value"] == "plugin_goodsCardWrapper _row_1"
    assert prop["textValue"] == "plugin_goodsCardWrapper _row_1"
    assert prop["expressionValue"] == ""

    element = {
        "selector": {
            "commonProps": {"cssSelector": '"div.plugin_goodsCardWrapper span"'},
            "selector": [
                {
                    "name": "div",
                    "type": "web",
                    "props": [
                        {"name": "class", "value": '"plugin_goodsCardWrapper"', "textValue": "plugin_goodsCardWrapper"},
                    ],
                },
                {"name": "span", "type": "web", "props": []},
            ],
        },
        "card_context": {"family_key": "plugin_goodsCardWrapper"},
    }
    chain = _collection_selector_chain(element)
    assert chain is not None
    assert chain[0]["props"][0]["value"] == "plugin_goodsCardWrapper"


def test_native_collection_normalizer_repairs_legacy_quoted_task_values():
    config = {
        "collectionConfig": {
            "columnSelectorArray": [
                [
                    {
                        "name": "div",
                        "type": "web",
                        "props": [
                            {
                                "name": "class",
                                "value": '"plugin_goodsCardWrapper"',
                                "textValue": "plugin_goodsCardWrapper",
                                "expressionValue": "",
                            }
                        ],
                    }
                ]
            ],
        },
    }
    payload = {
        "componentModelEntities": {
            "collect": {
                "type": "WebBrowser_DataCollectionV5",
                "args0": [{"name": "config", "value": json.dumps(config)}],
            },
        },
    }

    assert _native_normalize_collection_config(payload) is True
    repaired = json.loads(payload["componentModelEntities"]["collect"]["args0"][0]["value"])
    assert repaired["collectionConfig"]["columnSelectorArray"][0][0]["props"][0]["value"] == "plugin_goodsCardWrapper"
    assert _native_normalize_collection_config(payload) is False


def test_rpa_generation_context_requires_browser_open_first():
    context = project.rpa_generation_context("打开京东搜索手机")
    assert context["first_step"]["policy"] == "model_decides"
    assert context["recommended_sequence"] == []
    assert context["generation_contract"]["component_order_is_strict"] is False
    assert "截图" in " ".join(context["capture_protocol"])
    assert "staged_design_protocol" not in context
    assert context["generation_contract"]["default_output_columns"] == [
        "商品名称",
        "商家名称",
        "价格",
        "销量",
        "商品链接",
    ]
    assert context["generation_contract"]["default_excel_mode"] is True
    assert context["native_recommended_sequence"] == []
    assert context["search_result_wait"]["required"] is False
    assert context["generation_contract"]["explicit_excel_sequence"] == []
    bootstrap = context["mcp_client_bootstrap"]
    assert "shizai_rpa_analyze_request" in bootstrap["attached_check"]
    assert "停止业务设计" in bootstrap["missing_tools_action"]
    assert bootstrap["upgrade_command"] == "npx -y shizai-agent-mcp@latest install"
    assert "不能把工具注入" in bootstrap["prompt_boundary"]
    assert context["rule_authoring"]["language"] == "中文"
    assert context["rule_authoring"]["placement"]["rpa_rules"].endswith("rpa_generation_context")
    rules = context["business_rules"]
    assert [item["id"] for item in rules] == ["native-component-preference"]
    native_rule = rules[0]
    assert native_rule["severity"] == "advisory"
    assert native_rule["enforcement"] == "warning_only"
    assert "WebBrowser_DataCollectionV5" in native_rule["recommendation"]
    assert any("未选择推荐的原生组件" in item and "硬失败" in item for item in native_rule["must_not"])

    analysis = project.analyze_rpa_request(
        "\u751f\u6210\u6d41\u7a0b\u6253\u5f00\u4eac\u4e1c\u641c\u7d22\u624b\u673a"
    )
    assert analysis["rule_authoring"]["language"] == "中文"
    assert analysis["business_rules"] == rules
    assert analysis["business_rules"] is not rules


def test_one_sentence_router_separates_browser_rpa_from_agent_workflow():
    browser = project.route_request("生成流程打开京东搜索手机并保存商品价格到 Excel")
    assert browser["intent"] == "browser_rpa"
    assert browser["next_tool"] == "shizai_rpa_analyze_request"
    assert [item["phase"] for item in browser["workflow_plan"]] == [
        "analyze",
        "design",
        "capture",
        "compose",
        "verify",
        "run",
    ]
    assert "never completes" in browser["completion_rule"]
    assert browser["workflow_plan"][2]["tools"][-1] == "shizai_project_import_elements"
    assert browser["workflow_plan"][2]["optional_tool"] == "shizai_extension_start_capture"

    agent = project.route_request("创建一个知识库问答智能体")
    assert agent["intent"] == "agent_workflow"
    assert agent["next_tool"] == "shizai_generation_context"


def test_one_sentence_router_accepts_plain_native_file_instruction():
    result = project.route_request("读取指定文本文件，把待处理替换为已完成后写入新文件")
    assert result["intent"] == "native_rpa"
    assert result["next_tool"] == "shizai_rpa_analyze_request"


def _generic_browser_plan_fixture():
    query = "请生成一个流程：打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，整理前10条结果并输出。"
    analysis = project.analyze_rpa_request(query)
    operation_ids = {item["type"]: item["id"] for item in analysis["operations"]}
    plan = {
        "analysis_id": analysis["analysis_id"],
        "name": "请求驱动的网页采集",
        "target": {"url": "https://www.jd.com/"},
        "steps": [
            {
                "id": "open-target",
                "component": "WebBrowser_CreateV3",
                "purpose": "打开用户要求的网站",
                "operation_ids": [operation_ids["navigate"]],
                "depends_on": [],
                "inputs": [],
                "outputs": ["网页对象"],
                "config": {"url": "https://www.jd.com/", "type": "chrome", "openNewTab": False},
            },
            {
                "id": "set-query",
                "component": "Element_SetValue_V6",
                "purpose": "向当前页面识别出的搜索输入框写入用户关键词",
                "operation_ids": [operation_ids["search"]],
                "depends_on": ["open-target"],
                "inputs": ["网页对象"],
                "outputs": ["校验结果"],
                "element_role": "当前页面的搜索输入框",
                "config": {"text": "手机", "designated_obj": "网页对象"},
            },
            {
                "id": "submit-query",
                "component": "Keyboard_PressNew",
                "purpose": "提交已填写的搜索条件",
                "operation_ids": [operation_ids["search"]],
                "depends_on": ["set-query"],
                "inputs": [],
                "outputs": [],
                "config": {"key": "13", "type": "press"},
            },
            {
                "id": "wait-results",
                "component": "Basic_Delay_V1",
                "purpose": "等待目标站点完成结果页渲染",
                "operation_ids": [],
                "depends_on": ["submit-query"],
                "inputs": [],
                "outputs": [],
                "config": {"second": 1},
            },
            {
                "id": "refresh-tab",
                "component": "WebBrowser_GetTAB_V1",
                "purpose": "重新绑定当前结果页标签对象",
                "operation_ids": [],
                "depends_on": ["wait-results"],
                "inputs": [],
                "outputs": ["网页对象"],
                "config": {"type": "chrome", "match_type": 2, "isActive": 1},
            },
            {
                "id": "extract-results",
                "component": "WebBrowser_RunJsInIeBySelector_v3",
                "purpose": "按本次页面证据提取并整理请求字段",
                "operation_ids": [operation_ids["extract"], operation_ids["transform"]],
                "depends_on": ["refresh-tab"],
                "inputs": ["网页对象"],
                "outputs": ["数据采集结果"],
                "config": {
                    "jsCode": (
                        "function () { "
                        "if (false) throw new Error('SHIZAI_WAITING_FOR_USER:login_required'); "
                        "if (false) throw new Error('SHIZAI_WAITING_FOR_USER:page_challenge'); "
                        "return [['商品标题', '价格', '商品链接']]; }"
                    ),
                    "output_variable": "数据采集结果",
                    "result_mode": "table",
                    "output_columns": ["商品标题", "价格", "商品链接"],
                    "max_rows": 10,
                    "page_scope": True,
                },
            },
            {
                "id": "create-output",
                "component": "Excel_Cs_NewV4",
                "purpose": "创建用户要求的结果文件",
                "operation_ids": [],
                "depends_on": ["extract-results"],
                "inputs": [],
                "outputs": ["Excel文件"],
                "config": {"path": "res", "name": "result.xlsx"},
            },
            {
                "id": "write-output",
                "component": "Excel_Cs_WriteRange",
                "purpose": "写入并保存本次任务结果",
                "operation_ids": [operation_ids["output"]],
                "depends_on": ["create-output"],
                "inputs": ["Excel文件", "数据采集结果"],
                "outputs": [],
                "config": {"workbook": "Excel文件", "contents": "数据采集结果", "save": 1},
            },
        ],
        "output_contract": {
            "columns": ["商品标题", "价格", "商品链接"],
            "max_rows": 10,
            "file": "result.xlsx",
        },
    }
    return query, analysis, plan


def _generic_native_file_plan_fixture():
    query = "请生成一个流程：读取指定文本文件并输出文件内容。"
    analysis = {
        "kind": "rpa_request_analysis",
        "analysis_id": "rpa-native-file",
        "query": query,
        "intent": "native_rpa",
        "operations": [
            {"id": "op-1", "order": 1, "type": "extract", "instruction": "读取指定文本文件"},
            {"id": "op-2", "order": 2, "type": "output", "instruction": "输出文件内容"},
        ],
        "evidence": {"requested_fields": [], "limits": []},
        "interaction_policy": {"login_or_challenge": "pause_and_ask_user"},
    }
    plan = {
        "analysis_id": analysis["analysis_id"],
        "name": "通用文件读取",
        "steps": [
            {
                "id": "read-file",
                "component": "File_Read",
                "purpose": "读取用户指定的文本文件并输出内容变量",
                "operation_ids": ["op-1", "op-2"],
                "depends_on": [],
                "inputs": [],
                "outputs": ["文件内容"],
                "config": {
                    "var_name": "文件内容",
                    "path": "C:/data/input.txt",
                    "encode": "utf-8",
                },
            }
        ],
        "output_contract": {"columns": []},
    }
    catalog = {
        "File_Read": {
            "type": "File_Read",
            "native_type": "File_Read",
            "label": "读取文件",
            "description": "读取指定文件的全部内容",
            "inputs": [
                {"name": "var_name", "isExport": 3},
                {"name": "path", "defaultValue": '""'},
                {"name": "encode", "defaultValue": '"utf-8"'},
            ],
            "outputs": {"assign_to": ["var_name"], "primary": "var_name"},
            "code": [{"func": "File.Read", "args": ["path", "encode"], "assignTo": "var_name"}],
        }
    }
    return query, analysis, plan, catalog


def test_native_request_plan_uses_live_catalog_contract_and_non_browser_first_step():
    _, analysis, plan, catalog = _generic_native_file_plan_fixture()

    result = project.validate_rpa_execution_plan(
        analysis,
        plan,
        available_components=catalog,
    )

    assert result["valid"] is True
    assert result["plan"]["steps"][0]["component"] == "File_Read"
    assert result["plan"]["steps"][0]["outputs"] == ["文件内容"]

    invalid_catalog = deepcopy(catalog)
    invalid_catalog["File_Read"]["code"] = []
    with pytest.raises(ValueError, match="cannot be materialized"):
        project.validate_rpa_execution_plan(
            analysis,
            plan,
            available_components=invalid_catalog,
        )


def test_native_file_read_plan_materializes_module_and_variable_index(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.applications_root.mkdir(parents=True)
    query, analysis, plan, catalog = _generic_native_file_plan_fixture()
    template = paths.applications_root / "component-template"
    template.mkdir()
    (template / "template.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                    "file-read-template": {
                        "id": "file-read-template",
                        "type": "File_Read",
                        "label": "读取文件",
                        "args0": [
                            {"name": "var_name", "value": "文件内容"},
                            {"name": "path", "value": '""'},
                            {"name": "encode", "value": '"gbk"'},
                        ],
                        "options0": [
                            {"name": "var_ret", "value": "0"},
                            {"name": "delay_before_new", "value": "0.2"},
                            {"name": "delay_after_new", "value": "0.2"},
                        ],
                    },
                },
                "dataTag": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    native = paths.applications_root / "native-file-read"

    def fake_create_native_application(*args, **kwargs):
        native.mkdir()
        (native / "szrpa.json").write_text(
            json.dumps({"id": "native-file-read", "packageName": "读取文件流程"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (native / "elements.json").write_text('{"elementMap":{},"groupMap":{}}', encoding="utf-8")
        (native / "project.flow").write_text("flow", encoding="utf-8")
        (native / "main.sz").write_text("main", encoding="utf-8")
        (native / "flow.task").write_text(
            json.dumps(
                {
                    "componentModelEntities": {
                        "MAIN_COMPONENT": {
                            "id": "MAIN_COMPONENT",
                            "type": "main",
                            "args0": [{"name": "statements", "value": []}],
                        }
                    },
                    "dataTag": 1,
                }
            ),
            encoding="utf-8",
        )
        return {
            "application_id": "native-file-read",
            "package_id": "package-file-read",
            "local_path": str(native),
            "factory_version": "7.3.7",
        }

    monkeypatch.setattr(agent, "create_native_application", fake_create_native_application)
    monkeypatch.setattr(component_catalog, "builtin_component_index", lambda paths: catalog)

    started = project.start_rpa_design(
        paths,
        name="读取文件流程",
        first_step=plan["steps"][0],
        component_plan=plan["steps"],
        request_analysis=analysis,
        required_columns=[],
        client=object(),
    )

    assert started["first_component"]["step_validation"]["valid"] is True
    state = project.read_rpa_flow(paths, str(native))["flow"]
    assert state["steps"][0]["component"] == "File_Read"
    for key, value in plan["steps"][0]["config"].items():
        assert state["steps"][0]["config"][key] == value
    module_path = native / "projects" / "flow_modules" / "flow.py"
    module_text = module_path.read_text(encoding="utf-8")
    assert "import packages.File.py.exports as File" in module_text
    assert "文件内容 = File.Read(" in module_text
    compile(module_text, str(module_path), "exec")
    variable_info = json.loads((native / "variableInfo").read_text(encoding="utf-8"))
    references = [reference for _, item in variable_info["variableMap"] for reference in item["variable"]]
    assert any(item["referenceType"] == "set" and item["variableName"] == "文件内容" for item in references)
    validation = project.validate_rpa_flow(paths, str(native), strict=True)
    assert validation["valid"] is True, (validation["errors"], validation["warnings"])
    # Tabular files may declare columns without collecting browser elements.
    metadata_path = native / "szrpa.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["mcpRequiredColumns"] = ["客户", "金额"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    validation = project.validate_rpa_flow(paths, str(native), strict=True)
    assert validation["required_columns"] == ["客户", "金额"]
    assert validation["valid"] is True, (validation["errors"], validation["warnings"])


def test_rpa_request_analysis_is_site_neutral_and_preserves_exact_contract():
    _, analysis, _ = _generic_browser_plan_fixture()
    assert analysis["evidence"]["quoted_values"] == ["手机"]
    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "商品链接"]
    assert analysis["evidence"]["limits"][-1]["value"] == 10
    assert [item["type"] for item in analysis["operations"]] == [
        "navigate",
        "search",
        "extract",
        "transform",
        "output",
    ]
    assert analysis["planning_requirements"]["no_builtin_site_flow"] is True
    assert "steps" not in analysis


def test_rpa_request_analysis_preserves_field_list_and_keeps_pagination_model_owned():
    query = (
        "请生成一个流程：打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，"
        "100条结果并输出，有必要可翻页。"
    )

    analysis = project.analyze_rpa_request(query)

    assert analysis["query"] == query
    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "商品链接"]
    assert analysis["evidence"]["collection"]["max_rows"] == 100
    assert analysis["evidence"]["pagination"] == {
        "mode": "model_decides",
        "suggested_strategy": "until_max_rows",
        "trigger": "when_the_selected_strategy_needs_more_source_rows",
        "required_for_completion": False,
        "decision": "model_decides",
        "target_rows": 100,
        "evidence": "有必要可翻页",
    }
    assert [item["type"] for item in analysis["operations"]] == [
        "navigate",
        "search",
        "extract",
        "output",
    ]
    assert all(item["instruction"] != "有必要可翻页" for item in analysis["operations"])
    assert analysis["analysis_policy"]["do_not_reanalyze_or_rewrite_query"] is True
    assert analysis["after_catalog"] == "shizai_rpa_validate_plan"
    collection_requirement = analysis["planning_requirements"]["component_requirements"][2]
    assert collection_requirement["required_capabilities"] == ["extract", "output", "limit"]
    assert collection_requirement["prefer_single_component"] is False
    assert analysis["planning_requirements"]["recommended_catalog_query"]["capability_mode"] == "any"
    collection_decision = analysis["planning_requirements"]["collection_decision"]
    assert collection_decision["target_kind"] == "final_valid_row_count"
    assert collection_decision["requires_pagination"] is False
    assert collection_decision["pagination_capability"] == "discover_from_live_component_catalog"
    assert collection_decision["required_capabilities"] == ["extract", "output", "limit"]
    assert collection_decision["must_use_single_component"] is False
    assert collection_decision["strategy"] == "model_decides"
    assert "final valid-row acceptance target" in analysis["planning_requirements"]["component_selection_contract"]["numeric_collection_rule"]
    checkpoint = analysis["planning_requirements"]["planner_checkpoint"]
    assert checkpoint["catalog_call"]["tool"] == "shizai_rpa_component_catalog"
    assert checkpoint["catalog_call"]["arguments"]["capability_mode"] == "any"
    assert checkpoint["catalog_call"]["arguments"]["capability"].endswith("limit")
    assert checkpoint["required_plan_assertions"]


def test_rpa_request_analysis_does_not_treat_output_directory_as_a_field():
    query = (
        "请在实在 Agent 中设计并验证一个可运行的原生 RPA 流程：打开京东，搜索手机，"
        "采集至少 100 条商品标题、价格、链接、销量，支持翻页，结果写入 "
        "C:\\Users\\edy\\Desktop\\rpa文件目录\\2026-9-2。"
    )

    analysis = project.analyze_rpa_request(query)

    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "链接", "销量"]
    assert not any("rpa文件目录" in item for item in analysis["evidence"]["requested_fields"])


def test_rpa_request_analysis_strips_joined_output_clause_and_field_suffix():
    joined = project.analyze_rpa_request(
        "打开京东，搜索手机，采集至少100条商品标题、价格、商品链接、销量并写入 "
        "C:\\Users\\edy\\Desktop\\rpa文件目录\\2026-9-2"
    )
    suffixed = project.analyze_rpa_request(
        "打开京东，搜索手机，采集至少100条商品标题、价格、商品链接、销量等数据，支持翻页。"
    )

    expected = ["商品标题", "价格", "商品链接", "销量"]
    assert joined["evidence"]["requested_fields"] == expected
    assert suffixed["evidence"]["requested_fields"] == expected
    assert not any("rpa文件目录" in item for item in joined["evidence"]["requested_fields"])


def test_rpa_request_analysis_handles_full_width_url_and_explanatory_suffixes():
    query = (
        "打开 https://www.jd.com，搜索“手机”，采集至少 100 条商品标题、价格、商品链接、销量"
        "等可见商品信息，重点验证销量，根据实际页面决定是否分页。"
    )

    analysis = project.analyze_rpa_request(query)

    assert analysis["evidence"]["urls"] == ["https://www.jd.com"]
    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "商品链接", "销量"]
    assert not any("可见商品信息" in field for field in analysis["evidence"]["requested_fields"])
    assert not any("重点验证销量" in field for field in analysis["evidence"]["requested_fields"])
    assert not any("根据实际页面决定是否分页" in field for field in analysis["evidence"]["requested_fields"])


def test_rpa_plan_normalizes_fields_and_destination_into_output_file(tmp_path):
    _, analysis, plan = _generic_browser_plan_fixture()
    output_contract = {
        "fields": ["商品标题", "价格", "商品链接"],
        "max_rows": 10,
        "destination": str(tmp_path / "exports"),
        "format": "xlsx",
    }
    plan["output_contract"] = output_contract

    validated = project.validate_rpa_execution_plan(analysis, plan)

    assert validated["plan"]["output_contract"]["columns"] == output_contract["fields"]
    assert validated["plan"]["output_contract"]["file"] == str(tmp_path / "exports" / "result.xlsx")


@pytest.mark.parametrize("planning_policy", ["strict", "mcp"])
def test_rpa_plan_preserves_output_file_alias_for_runtime_evidence(tmp_path, planning_policy):
    _, analysis, plan = _generic_browser_plan_fixture()
    output_name = Path(plan["output_contract"]["file"]).name
    output_path = str(tmp_path / output_name)
    plan["output_contract"].pop("file", None)
    plan["output_contract"]["output_file"] = output_path
    validated = project.preflight_rpa_execution_plan(analysis, plan, planning_policy=planning_policy)
    assert validated["valid"], validated
    assert validated["plan"]["output_contract"]["file"] == output_path
    plan["output_contract"]["file"] = str(tmp_path / "canonical" / output_name)
    validated = project.preflight_rpa_execution_plan(analysis, plan, planning_policy=planning_policy)
    assert validated["valid"], validated
    assert validated["plan"]["output_contract"]["file"] == str(tmp_path / "canonical" / output_name)


def test_rpa_request_analysis_deduplicates_repeated_numeric_target_in_orchestration_prompt():
    analysis = project.analyze_rpa_request(
        "打开京东，搜索‘手机’，读取商品标题和价格，100条结果并输出。"
        "100条是最终有效行数，分页由采集组件内部完成。"
    )

    assert analysis["evidence"]["limits"] == [{"value": 100, "evidence": "100条结果"}]
    assert analysis["evidence"]["collection"]["max_rows"] == 100


def test_rpa_request_analysis_ignores_client_execution_guidance_as_operations():
    query = (
        "打开京东，搜索‘手机’，读取搜索结果中的商品标题、价格和商品链接，100条结果并输出。"
        "请使用 shizai-agent MCP 完整执行：意图分析、实时组件目录、计划校验、写入流程、打开浏览器。"
        "100条是最终有效行数，分页由采集组件内部翻页。"
    )
    analysis = project.analyze_rpa_request(query)

    assert [item["type"] for item in analysis["operations"]] == ["navigate", "search", "extract", "output"]
    assert all("请使用" not in item["instruction"] for item in analysis["operations"])


def test_rpa_request_analysis_strips_appended_mcp_checklist_from_operations():
    query = (
        "打开京东，搜索‘手机’，读取搜索结果中的商品标题、价格和商品链接，100条结果并输出，有必要可翻页。"
        "严格按 MCP 意图分析、任务拆解、实时组件目录、计划校验、流程写入和页面证据绑定执行。"
        "不要调用 Agent 图组件接口，不要写死站点流程。"
    )
    analysis = project.analyze_rpa_request(query)

    assert [item["type"] for item in analysis["operations"]] == ["navigate", "search", "extract", "output"]
    assert all("严格按" not in item["instruction"] for item in analysis["operations"])
    assert analysis["query"] == query


def test_rpa_request_analysis_keeps_qualified_output_clause_out_of_fields():
    query = "打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，最终输出100条有效结果，必要时翻页。"
    analysis = project.analyze_rpa_request(query)

    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "商品链接"]
    assert [item["type"] for item in analysis["operations"]] == ["navigate", "search", "extract", "output"]
    assert analysis["evidence"]["limits"][-1]["value"] == 100
    assert analysis["evidence"]["pagination"]["required_for_completion"] is False


def test_numeric_collection_target_does_not_infer_pagination_without_explicit_page_wording():
    query = (
        "请生成一个流程：打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，"
        "采集100条结果并输出。"
    )

    analysis = project.analyze_rpa_request(query)

    assert analysis["evidence"]["collection"] == {
        "max_rows": 100,
        "pagination": None,
        "completion": "collect_valid_rows_until_max_rows_or_source_exhaustion",
    }
    collection_requirement = analysis["planning_requirements"]["component_requirements"][2]
    assert collection_requirement["required_capabilities"] == ["extract", "output", "limit"]
    assert "paginate" not in analysis["planning_requirements"]["recommended_catalog_query"]["capability"]


def test_mcp_rehydrates_compacted_analysis_by_analysis_id():
    query = "打开京东，搜索“手机”，采集100条商品标题并输出。"
    original = mcp_server._cache_rpa_analysis(project.analyze_rpa_request(query))
    compact = {"analysis_id": original["analysis_id"], "query": query}

    restored, rehydrated = mcp_server._restore_rpa_analysis(compact)

    assert rehydrated is True
    assert restored["analysis_id"] == original["analysis_id"]
    assert restored["evidence"]["collection"]["max_rows"] == 100
    assert restored["planning_requirements"]["pagination_guidance"]["model_action"]


def test_rpa_analysis_does_not_fold_numeric_suffix_into_requested_fields():
    analysis = project.analyze_rpa_request(
        "打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，采集100条结果并输出。"
    )

    assert analysis["evidence"]["requested_fields"] == ["商品标题", "价格", "商品链接"]
    assert analysis["evidence"]["collection"]["max_rows"] == 100


def test_rpa_analysis_keeps_flow_metadata_out_of_operation_coverage():
    analysis = project.analyze_rpa_request(
        "请生成一个流程：打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，"
        "100条结果并输出。流程名字保存为202691-1京东手机数据采集；记录完整过程和耗时。"
    )

    assert [item["type"] for item in analysis["operations"]] == [
        "navigate",
        "search",
        "extract",
        "output",
    ]
    assert analysis["evidence"]["pagination"] is None
    assert analysis["evidence"]["collection"]["max_rows"] == 100
    extract = next(item for item in analysis["operations"] if item["type"] == "extract")
    assert extract["completion_contract"]["pagination"] == "model_decides"
    assert extract["completion_contract"]["max_rows"] == 100
    assert "explicit_next_page_steps" in extract["completion_contract"]["allowed_strategies"]


def test_catalog_collection_guidance_makes_pagination_configuration_explicit(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCollectionV5",
                    "label": "网页数据采集",
                    "category": "get_web_data",
                    "definition": {
                        "args0": [
                            {"name": "collect_items"},
                            {"name": "page_type"},
                            {"name": "selector_page"},
                            {"name": "selector_more"},
                            {"name": "toExcel"},
                            {"name": "var0", "isExport": 3, "defaultValue": "数据采集结果"},
                        ],
                        "options0": [],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="extract,limit,paginate,output",
        capability_mode="any",
        engine_root=str(engine),
        detail="contract",
    )

    guidance = result["planning_guidance"]["collection"]
    assert guidance["must_select_for_numeric_target"] is False
    assert guidance["selection_policy"] == "advisory"
    assert guidance["selection"]["status"] == "ready"
    assert guidance["selection"]["recommended_component"] == "WebBrowser_DataCollectionV5"
    assert guidance["selection"]["candidate_count"] == 1
    assert "final valid-row count" in guidance["planner_directive"]
    assert "collect_items = output_contract.max_rows" in guidance["must_configure"][0]
    assert "runtime result" in guidance["completion_check"]
    assert guidance["execution_contract"]["mode"] == "model_decides"
    assert "selected component graph" in guidance["execution_contract"]["loop"]

    # The compact decision gate remains available even when a client forgets
    # to send the capability filter and only asks for the full catalog.
    unfiltered = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="",
        capability_mode="any",
        engine_root=str(engine),
        detail="summary",
    )
    numeric = unfiltered["planning_guidance"]["numeric_target"]
    assert numeric["decision"] == "advisory"
    assert numeric["required_capabilities"] == ["extract", "output"]
    assert numeric["required_config"]["collect_items"] == "$output_contract.max_rows_if_supported"
    assert numeric["plan_fragment"]["component"] == "WebBrowser_DataCollectionV5"
    assert numeric["plan_fragment"]["config"]["collect_items"] == "$output_contract.max_rows_if_supported"
    assert "standalone page action" in numeric["execution"]["allowed"]


def test_catalog_emits_role_selection_to_prevent_navigation_component_for_search(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_CreateV3",
                    "label": "打开浏览器",
                    "category": "webBrowser",
                    "definition": {"args0": [{"name": "url"}], "options0": []},
                },
                {
                    "type": "Element_SetValue_V6",
                    "label": "设置网页输入并提交",
                    "category": "webBrowser",
                    "definition": {
                        "args0": [{"name": "uiSelector"}, {"name": "text"}, {"name": "isEnter"}],
                        "options0": [],
                    },
                },
                {
                    "type": "Element_SetValuePassword_V6",
                    "label": "设置密码输入",
                    "category": "webBrowser",
                    "definition": {
                        "args0": [{"name": "uiSelector"}, {"name": "text"}, {"name": "isEnter"}],
                        "options0": [],
                    },
                },
                {
                    "type": "WebBrowser_DataCollectionV5",
                    "label": "网页数据采集",
                    "category": "get_web_data",
                    "definition": {
                        "args0": [
                            {"name": "collect_items"},
                            {"name": "page_type"},
                            {"name": "selector_page"},
                            {"name": "selector_more"},
                            {"name": "toExcel"},
                        ],
                        "options0": [],
                    },
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="navigate,input,submit,extract,limit,paginate,output",
        capability_mode="any",
        engine_root=str(engine),
        detail="contract",
    )

    contract = result["role_selection_contract"]
    assert contract["order"] == []
    assert contract["order_policy"].startswith("model_decides")
    assert contract["select_one_per_required_role"] is False
    assert result["role_selection"]["browser_navigation"]["recommended_component"] == "WebBrowser_CreateV3"
    assert result["role_selection"]["search_interaction"]["recommended_component"] == "Element_SetValue_V6"
    assert result["role_selection"]["structured_collection"]["recommended_component"] == "WebBrowser_DataCollectionV5"
    assert contract["hard_rejections"] == [
        "component or parameter is not in the live contract",
        "required selector or graph reference is unresolved",
    ]
    assert contract["numeric_target"]["pagination_owner"] == "client_model"
    assert contract["numeric_target"]["pagination_mode"] == "model_decides"
    assert result["planning_guidance"]["roles"]["search_interaction"]["rule"].startswith("These candidates")


def test_catalog_collection_guidance_blocks_when_no_pagination_candidate(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCollectionV5",
                    "label": "网页数据采集",
                    "category": "get_web_data",
                    "definition": {
                        "args0": [{"name": "collect_items"}, {"name": "toExcel"}],
                        "options0": [],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="extract,limit,paginate,output",
        capability_mode="any",
        engine_root=str(engine),
        detail="contract",
    )

    selection = result["planning_guidance"]["collection"]["selection"]
    assert selection["status"] == "missing"
    assert selection["recommended_component"] is None
    assert selection["if_missing"]["action"] == "pause_and_report_missing_live_component"


def test_catalog_relaxes_overly_strict_free_text_when_capability_match_exists(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCollectionV5",
                    "label": "网页数据采集",
                    "category": "get_web_data",
                    "definition": {
                        "args0": [
                            {"name": "collect_items"},
                            {"name": "page_type"},
                            {"name": "selector_page"},
                            {"name": "toExcel", "isExport": 3, "defaultValue": "Excel路径"},
                            {"name": "var0", "isExport": 3, "defaultValue": "数据采集结果"},
                        ],
                        "options0": [],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        query="browser navigation search input submit structured collection",
        top_level="webBrowser",
        capability="extract,limit,paginate,output",
        capability_mode="any",
        engine_root=str(engine),
        detail="contract",
    )

    assert result["filter_relaxation"]["applied"] is True
    assert set(result["filter_relaxation"]["ignored"]) == {"query", "top_level"}
    assert result["planning_guidance"]["collection"]["selection"]["status"] == "ready"


def test_catalog_numeric_collection_contract_survives_overbroad_all_capability_filter(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCollectionV5",
                    "label": "网页数据采集",
                    "definition": {
                        "args0": [
                            {"name": "collect_items"},
                            {"name": "page_type"},
                            {"name": "selector_page"},
                            {"name": "toExcel"},
                            {"name": "var0", "isExport": 3, "defaultValue": "数据采集结果"},
                        ],
                        "options0": [],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="navigate,input,submit,extract,limit,paginate,output",
        capability_mode="all",
        engine_root=str(engine),
        detail="contract",
    )

    collection = result["planning_guidance"]["collection"]
    assert collection["selection"]["status"] == "ready"
    assert collection["selection"]["fallback_applied"] is True
    assert collection["selection"]["recommended_component"] == "WebBrowser_DataCollectionV5"


def test_explicit_first_page_or_top_n_request_does_not_infer_pagination():
    top_n = project.analyze_rpa_request(
        "打开京东，搜索“手机”，读取搜索结果中的标题和链接，整理前10条结果并输出。"
    )
    current_page = project.analyze_rpa_request(
        "打开京东，搜索“手机”，仅读取搜索结果并采集当前页100条结果并输出。"
    )

    assert top_n["evidence"]["pagination"] is None
    assert current_page["evidence"]["pagination"] is None
    assert "paginate" not in top_n["planning_requirements"]["component_requirements"][2]["required_capabilities"]
    assert "paginate" not in current_page["planning_requirements"]["component_requirements"][2]["required_capabilities"]


def test_rpa_plan_preflight_returns_fast_structured_semantic_value_correction():
    _, analysis, plan = _generic_browser_plan_fixture()
    plan = deepcopy(plan)
    plan["steps"][0]["config"]["url"] = '"https://www.jd.com/"'

    result = project.preflight_rpa_execution_plan(analysis, plan)

    assert result["valid"] is False
    assert result["error_code"] == "semantic_config_value"
    assert result["do_not_reanalyze_original_query"] is True
    assert result["next_tool"] == "shizai_rpa_validate_plan"
    assert result["elapsed_ms"] < 100


def test_rpa_plan_accepts_query_url_as_search_operation_implementation():
    _, analysis, plan = _generic_browser_plan_fixture()
    plan = deepcopy(plan)
    operation_ids = {item["type"]: item["id"] for item in analysis["operations"]}
    plan["steps"][0]["config"]["url"] = "https://search.jd.com/Search?keyword=手机"
    plan["steps"][0]["operation_ids"] = [operation_ids["navigate"], operation_ids["search"]]

    result = project.preflight_rpa_execution_plan(analysis, plan)

    assert result["valid"] is True
    assert result["constraint_coverage"]["limit_steps"]


def test_mcp_plan_preflight_keeps_repair_available_after_multiple_errors(monkeypatch, tmp_path):
    _, analysis, plan = _generic_browser_plan_fixture()
    invalid = deepcopy(plan)
    invalid["steps"][0]["config"]["url"] = '"https://www.jd.com/"'
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )
    for _ in range(3):
        result = mcp_server._rpa_plan_preflight(_paths(tmp_path), analysis, invalid)
        assert result["valid"] is False
        assert result["next_tool"] == "shizai_rpa_validate_plan"
        assert not result.get("user_interaction_required")

    corrected = mcp_server._rpa_plan_preflight(_paths(tmp_path), analysis, plan)
    assert corrected["valid"] is True


def test_mcp_thin_plan_boundary_leaves_strategy_to_model():
    analysis = project.analyze_rpa_request("打开京东网页，采集商品标题并输出。")
    plan = {
        "analysis_id": analysis["analysis_id"],
        "name": "模型自定义图",
        "steps": [
            {
                "id": "prepare-output",
                "component": "Excel_Cs_NewV4",
                "config": {"path": "res", "name": "items.xlsx"},
                "loop": {"until": "rows >= target"},
            },
            {
                "id": "collect",
                "component": "WebBrowser_DataCollectionV5",
                "config": {"collect_items": 100, "page_type": 1},
                "pagination": {"strategy": "model-selected-loop"},
            },
        ],
        "output_contract": {"columns": ["标题"], "max_rows": 100},
    }

    result = project.preflight_rpa_execution_plan(
        analysis,
        plan,
        available_components={"Excel_Cs_NewV4": {}, "WebBrowser_DataCollectionV5": {}},
        planning_policy="mcp",
    )

    assert result["valid"] is True
    assert result["policy"] == {"mode": "model_decides", "strategy_checks": "none"}
    assert result["component_sequence"] == ["Excel_Cs_NewV4", "WebBrowser_DataCollectionV5"]
    assert result["constraint_coverage"] == {"limit_steps": [], "pagination_steps": []}
    assert result["plan"]["steps"][0]["loop"]["until"] == "rows >= target"


def test_mcp_thin_plan_boundary_still_rejects_unknown_component():
    analysis = project.analyze_rpa_request("打开京东网页并采集商品标题。")
    result = project.preflight_rpa_execution_plan(
        analysis,
        {
            "analysis_id": analysis["analysis_id"],
            "steps": [{"id": "unknown", "component": "MadeUp_Component", "config": {}}],
        },
        available_components={"MadeUp_Component": {}},
        planning_policy="mcp",
    )

    assert result["valid"] is False
    assert result["error_code"] == "component_unavailable"


def test_100_row_collection_plan_uses_one_live_component_for_limit_pagination_and_output():
    query = (
        "请生成一个流程：打开京东，搜索“手机”，读取搜索结果中的商品标题、价格和商品链接，"
        "100条结果并输出，有必要可翻页。"
    )
    analysis = project.analyze_rpa_request(query)
    operation_ids = {item["type"]: item["id"] for item in analysis["operations"]}
    plan = {
        "analysis_id": analysis["analysis_id"],
        "name": "通用多页数据采集",
        "target": {"url": "https://www.jd.com/"},
        "steps": [
            {
                "id": "open-site",
                "component": "WebBrowser_CreateV3",
                "purpose": "打开请求中的目标网站",
                "operation_ids": [operation_ids["navigate"]],
                "depends_on": [],
                "inputs": [],
                "outputs": ["网页对象"],
                "config": {"url": "https://www.jd.com/", "type": "chrome", "openNewTab": False},
            },
            {
                "id": "enter-query",
                "component": "Element_SetValue_V6",
                "purpose": "把关键词写入当前页面识别出的搜索框",
                "operation_ids": [operation_ids["search"]],
                "depends_on": ["open-site"],
                "inputs": ["网页对象"],
                "outputs": ["校验结果"],
                "element_role": "搜索输入框",
                "config": {"text": "手机", "designated_obj": "网页对象"},
            },
            {
                "id": "submit-query",
                "component": "Keyboard_PressNew",
                "purpose": "提交搜索",
                "operation_ids": [operation_ids["search"]],
                "depends_on": ["enter-query"],
                "inputs": [],
                "outputs": [],
                "config": {"key": "13", "type": "press"},
            },
            {
                "id": "collect-results",
                "component": "WebBrowser_DataCollectionV4",
                "purpose": "采集结构化字段并按需翻页直到获得100条有效结果",
                "operation_ids": [operation_ids["extract"], operation_ids["output"]],
                "depends_on": ["submit-query"],
                "inputs": ["网页对象"],
                "outputs": ["商品数据", "Excel路径", "监控结果"],
                "element_role": "结果列表字段及下一页或加载更多控件",
                "config": {
                    "var0": "商品数据",
                    "var1": "Excel路径",
                    "var2": "监控结果",
                    "designated_obj": "网页对象",
                    "region": 3,
                    "collect_items": 100,
                    "page_type": 1,
                    "toExcel": True,
                    "filename_suffix": ["result", ".xlsx"],
                    "filePath": "res",
                },
            },
        ],
        "output_contract": {
            "columns": ["商品标题", "价格", "商品链接"],
            "max_rows": 100,
            "file": "result.xlsx",
        },
    }
    live_catalog = {
        "WebBrowser_CreateV3": {},
        "Element_SetValue_V6": {},
        "Keyboard_PressNew": {},
        "WebBrowser_DataCollectionV4": {
            "type": "WebBrowser_DataCollectionV4",
            "native_type": "WebBrowser_DataCollectionV4",
            "label": "数据采集",
            "inputs": [
                {"name": name}
                for name in (
                    "var0",
                    "var1",
                    "var2",
                    "config",
                    "designated_obj",
                    "region",
                    "collect_items",
                    "page_type",
                    "selector_page",
                    "selector_more",
                    "toExcel",
                    "filename_suffix",
                    "filePath",
                )
            ],
            "outputs": {"assign_to": ["var0", "var1", "var2"], "primary": "var0"},
            "code": [
                {
                    "func": "WebBrowser.DataCollectionV4",
                    "args": ["config", "region", "collect_items", "page_type"],
                    "assignTo": "var0,var1,var2",
                }
            ],
            "capabilities": ["extract", "transform", "limit", "paginate", "output"],
            "operation_types": ["extract", "transform", "output"],
            "requires_page_evidence": True,
            "deferred_bindings": ["config", "selector_page", "selector_more"],
            "configuration_hints": {
                "row_limit_parameters": ["collect_items"],
                "pagination_parameters": ["region", "page_type", "selector_page", "selector_more"],
            },
            "planning_patterns": [
                {
                    "when": "request has max_rows and may need multiple pages",
                    "semantic_config": {
                        "region": 3,
                        "collect_items": "$output_contract.max_rows",
                        "page_type": "$choose_from_live_page_evidence",
                        "selector_page": "$defer_until_next_page_element_is_captured",
                    },
                }
            ],
        },
    }

    result = project.validate_rpa_execution_plan(analysis, plan, available_components=live_catalog)

    assert result["valid"] is True
    assert result["constraint_coverage"] == {
        "limit_steps": ["collect-results"],
        "pagination_steps": ["collect-results"],
    }
    assert result["component_sequence"].count("WebBrowser_DataCollectionV4") == 1

    # Semantic bindings are the canonical model-facing form.  Validation must
    # resolve them against the immutable output contract just like literals.
    semantic_binding_plan = deepcopy(plan)
    semantic_binding_plan["steps"][-1]["config"]["collect_items"] = "$output_contract.max_rows"
    semantic_result = project.validate_rpa_execution_plan(
        analysis,
        semantic_binding_plan,
        available_components=live_catalog,
    )
    assert semantic_result["valid"] is True
    assert semantic_result["constraint_coverage"]["limit_steps"] == ["collect-results"]

    # A live dictionary may expose multiple compatible collection contracts;
    # selecting one is still a model decision when the plan is structurally
    # valid and satisfies the output contract.
    newer_catalog = deepcopy(live_catalog)
    newer_catalog["WebBrowser_DataCollectionV5"] = deepcopy(live_catalog["WebBrowser_DataCollectionV4"])
    newer_catalog["WebBrowser_DataCollectionV5"]["type"] = "WebBrowser_DataCollectionV5"
    newer_result = project.validate_rpa_execution_plan(
        analysis,
        plan,
        available_components=newer_catalog,
    )
    assert newer_result["valid"] is True

    missing_pagination = deepcopy(plan)
    for key in ("region", "page_type"):
        missing_pagination["steps"][-1]["config"].pop(key)
    preflight = project.preflight_rpa_execution_plan(
        analysis,
        missing_pagination,
        available_components=live_catalog,
    )
    assert preflight["valid"] is True

    wrong_collection_mode = deepcopy(plan)
    wrong_collection_mode["steps"][-1]["config"]["region"] = 1
    wrong_mode_preflight = project.preflight_rpa_execution_plan(
        analysis,
        wrong_collection_mode,
        available_components=live_catalog,
    )
    assert wrong_mode_preflight["valid"] is True


def test_rpa_execution_plan_requires_complete_coverage_and_explicit_javascript():
    _, analysis, plan = _generic_browser_plan_fixture()
    validated = project.validate_rpa_execution_plan(analysis, plan)
    assert validated["valid"] is True
    assert validated["component_sequence"] == [item["component"] for item in plan["steps"]]

    missing_coverage = deepcopy(plan)
    missing_coverage["steps"][-1]["operation_ids"] = []
    with pytest.raises(ValueError, match="does not cover analyzed operations"):
        project.validate_rpa_execution_plan(analysis, missing_coverage)


def test_flexible_model_plan_keeps_strategy_choices_advisory():
    query = "打开京东，搜索手机，采集100条商品标题并输出。"
    analysis = project.analyze_rpa_request(query)
    plan = {
        "analysis_id": analysis["analysis_id"],
        "validation_mode": "flexible",
        "name": "模型自定义采集策略",
        "target": {"url": "https://www.jd.com"},
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateV3",
                "config": {"url": "https://www.jd.com"},
            },
            {
                "id": "search",
                "component": "Element_SetValue_V6",
                "config": {"text": "手机"},
                "inputs": ["网页对象"],
            },
            {
                "id": "collect-with-js",
                "component": "WebBrowser_RunJsInIeBySelector_v3",
                "config": {
                    "jsCode": "return [['商品标题']];",
                    "output_variable": "商品数据",
                    "result_mode": "table",
                    "output_columns": ["商品标题"],
                    "max_rows": 100,
                },
                "depends_on": ["search"],
                "inputs": ["网页对象"],
                "outputs": ["商品数据"],
            },
        ],
        "output_contract": {"columns": ["商品标题"], "max_rows": 100, "file": "result.xlsx"},
    }

    result = project.preflight_rpa_execution_plan(
        analysis,
        plan,
        planning_policy="flexible",
    )

    assert result["valid"] is True
    assert result["policy"]["mode"] == "model_decides"
    assert result["policy"]["strategy_checks"] == "advisory"
    assert not any("pagination" in warning for warning in result["normalization_warnings"])


def test_thin_mcp_plan_fills_omitted_native_outputs_but_preserves_explicit_empty():
    analysis = project.analyze_rpa_request("打开京东，搜索“手机”，采集商品标题并输出。")
    plan = {
        "analysis_id": analysis["analysis_id"],
        "name": "输出契约回归",
        "steps": [
            {
                "id": "open",
                "component": "WebBrowser_CreateV3",
                "config": {"url": "https://www.jd.com", "type": "chrome"},
            }
        ],
        "output_contract": {"columns": []},
    }

    filled = project.validate_mcp_rpa_plan(analysis, deepcopy(plan))
    assert filled["plan"]["steps"][0]["outputs"] == ["网页对象"]
    assert any("omitted outputs" in warning for warning in filled["normalization_warnings"])

    explicit_empty = deepcopy(plan)
    explicit_empty["steps"][0]["outputs"] = []
    preserved = project.validate_mcp_rpa_plan(analysis, explicit_empty)
    assert preserved["plan"]["steps"][0]["outputs"] == []
    assert not any("omitted outputs" in warning for warning in preserved["normalization_warnings"])


def test_js_result_mode_aliases_are_normalized_at_plan_boundary():
    _, analysis, raw_plan = _generic_browser_plan_fixture()
    alias_plan = deepcopy(raw_plan)
    js_step = next(
        item for item in alias_plan["steps"]
        if item["component"] == "WebBrowser_RunJsInIeBySelector_v3"
    )
    js_step["config"]["result_mode"] = "js_table_direct_to_excel"

    validated = project.validate_rpa_execution_plan(analysis, alias_plan)
    normalized_js = next(
        item for item in validated["plan"]["steps"]
        if item["component"] == "WebBrowser_RunJsInIeBySelector_v3"
    )

    assert normalized_js["config"]["result_mode"] == "table"
    assert any("result_mode alias" in warning for warning in validated["normalization_warnings"])


def test_strict_plan_policy_remains_available_for_legacy_callers():
    query = "打开京东，搜索手机，采集100条商品标题并输出。"
    analysis = project.analyze_rpa_request(query)
    plan = {
        "analysis_id": analysis["analysis_id"],
        "validation_mode": "strict",
        "name": "严格计划",
        "target": {"url": "https://www.jd.com"},
        "steps": [
            {"id": "open", "component": "WebBrowser_CreateV3", "config": {"url": "https://www.jd.com"}},
        ],
        "output_contract": {"columns": ["商品标题"], "max_rows": 100, "file": "result.xlsx"},
    }

    result = project.preflight_rpa_execution_plan(analysis, plan)

    assert result["valid"] is False
    assert result["policy"]["mode"] == "legacy_strict"


def test_rpa_plan_preserves_role_alias_for_later_element_capture():
    _, analysis, plan = _generic_browser_plan_fixture()
    role_plan = deepcopy(plan)
    role_plan["steps"][1].pop("element_role", None)
    role_plan["steps"][1]["role"] = "search_interaction"

    validated = project.validate_rpa_execution_plan(analysis, role_plan)

    search_step = next(item for item in validated["plan"]["steps"] if item["id"] == role_plan["steps"][1]["id"])
    assert search_step["element_role"] == "search_interaction"


def test_rpa_plan_infers_element_role_for_page_evidence_components():
    _, analysis, plan = _generic_browser_plan_fixture()
    inferred_plan = deepcopy(plan)
    inferred_plan["steps"][1].pop("element_role", None)

    validated = project.validate_rpa_execution_plan(analysis, inferred_plan)

    search_step = next(item for item in validated["plan"]["steps"] if item["id"] == inferred_plan["steps"][1]["id"])
    assert search_step["element_role"] == "搜索输入框"


def test_fixed_native_outputs_canonicalize_extra_catalog_slots():
    _, analysis, plan = _generic_browser_plan_fixture()
    noisy_plan = deepcopy(plan)
    noisy_plan["steps"][5]["outputs"] = [
        "数据采集结果",
        "Excel路径",
        "监控空列数据",
        "1",
        "0",
        "res",
    ]

    validated = project.validate_rpa_execution_plan(analysis, noisy_plan)

    assert validated["plan"]["steps"][5]["outputs"] == ["数据采集结果"]
    assert any("outputs were canonicalized" in item for item in validated["normalization_warnings"])

    missing_javascript = deepcopy(plan)
    missing_javascript["steps"][5]["config"].pop("jsCode")
    with pytest.raises(ValueError, match="page-specific jsCode"):
        project.validate_rpa_execution_plan(analysis, missing_javascript)

    installed = {item["component"] for item in plan["steps"]}
    installed.remove("WebBrowser_RunJsInIeBySelector_v3")
    with pytest.raises(ValueError, match="not installed in the live catalog"):
        project.validate_rpa_execution_plan(analysis, plan, available_components=installed)


def test_live_builtin_output_parameter_does_not_override_stable_logical_output():
    _, analysis, plan = _generic_browser_plan_fixture()
    live_catalog = {item["component"]: {} for item in plan["steps"]}
    live_catalog["WebBrowser_CreateV3"] = {
        "native_type": "WebBrowser_CreateV3",
        "inputs": [
            {"name": "var_name", "isExport": 3},
            {"name": "type"},
            {"name": "url"},
            {"name": "openNewTab"},
        ],
        "outputs": {"assign_to": ["var_name"], "primary": "var_name"},
        "code": [
            {
                "func": "WebBrowser.CreateV3",
                "args": ["type", "url", "openNewTab"],
                "assignTo": "var_name",
            }
        ],
        "capabilities": ["navigate", "produces_variable"],
        "operation_types": ["navigate"],
    }

    result = project.validate_rpa_execution_plan(analysis, plan, available_components=live_catalog)

    assert result["valid"] is True
    assert result["plan"]["steps"][0]["outputs"] == ["网页对象"]


def test_native_rpa_follows_persisted_request_plan_and_uses_supplied_javascript(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "planned-native"
    query, analysis, raw_plan = _generic_browser_plan_fixture()
    validated = project.validate_rpa_execution_plan(analysis, raw_plan)
    plan = validated["plan"]

    class Client:
        def call(self, service, method, args):
            assert service == "ApplicationService"
            assert method == "createApplication"
            native.mkdir(parents=True)
            (native / "szrpa.json").write_text(
                json.dumps({"id": "planned-native", "packageName": plan["name"], "componentsNum": 0}),
                encoding="utf-8",
            )
            (native / "elements.json").write_text("{}", encoding="utf-8")
            (native / "project.flow").write_text("flow", encoding="utf-8")
            (native / "main.sz").write_text("main", encoding="utf-8")
            (native / "main.task").write_text(
                json.dumps(
                    {
                        "componentModelEntities": {
                            "MAIN_COMPONENT": {
                                "id": "MAIN_COMPONENT",
                                "type": "main",
                                "args0": [{"name": "statements", "value": []}],
                            }
                        },
                        "dataTag": 1,
                    }
                ),
                encoding="utf-8",
            )
            return {
                "applicationId": "planned-native",
                "applicationPackageId": "planned-native-package",
                "localPath": str(native),
                "factoryVersion": "7.3.7",
                "runtimeVersion": "7.3.7.0",
            }

    started = project.start_rpa_design(
        paths,
        name=plan["name"],
        target_url=plan["steps"][0]["config"]["url"],
        keyword="",
        output_file=plan["output_contract"]["file"],
        required_columns=plan["output_contract"]["columns"],
        use_excel_components=True,
        first_step=plan["steps"][0],
        client=Client(),
    )
    attached = project.attach_rpa_execution_plan(
        paths,
        started["flow_ref"],
        analysis=analysis,
        validated_plan=validated,
    )
    assert attached["component_sequence"] == [item["component"] for item in plan["steps"]]
    assert attached["next_action"]["arguments"]["component"] == "Element_SetValue_V6"
    assert attached["next_action"]["element_capture"]["role"] == "当前页面的搜索输入框"

    # start_design receives the fast preflight wrapper internally; it must be
    # accepted by the same persistence boundary as the raw validated result.
    preflight_wrapper = deepcopy(validated)
    preflight_wrapper["kind"] = "rpa_plan_preflight"
    attached_wrapper = project.attach_rpa_execution_plan(
        paths,
        started["flow_ref"],
        analysis=analysis,
        validated_plan=preflight_wrapper,
    )
    assert attached_wrapper["plan_id"] == attached["plan_id"]

    unbound_search = project.add_rpa_step(
        paths,
        started["flow_ref"],
        plan["steps"][1]["component"],
        step_id=plan["steps"][1]["id"],
        config=plan["steps"][1]["config"],
    )
    assert unbound_search["step"]["binding_status"] == "unbound"
    assert unbound_search["next_action"]["tool"] == "shizai_rpa_scan_page"
    assert unbound_search["next_action"]["arguments"]["step_id"] == plan["steps"][1]["id"]
    assert (
        project.unbound_rpa_element_step(paths, started["flow_ref"], plan["steps"][1]["id"])["element_role"]
        == "当前页面的搜索输入框"
    )
    # Model-owned plans may add independent components before a selector is
    # captured. The pending element remains unbound and blocks validation/run,
    # but it does not impose a hidden component ordering rule.
    pending_component = project.add_rpa_step(
        paths,
        started["flow_ref"],
        plan["steps"][2]["component"],
        step_id=plan["steps"][2]["id"],
        config=plan["steps"][2]["config"],
    )
    assert pending_component["step"]["binding_status"] == "not_required"

    imported_search = project.import_elements(
        paths,
        started["flow_ref"],
        [
            {
                "id": "search-input",
                "name": "搜索输入框",
                "title": "目标站点",
                "url": plan["steps"][0]["config"]["url"],
                "tag": "input",
                "css_selector": "#search",
                "attributes": {"role": "searchbox"},
            }
        ],
    )
    project.set_rpa_step_elements(
        paths,
        started["flow_ref"],
        plan["steps"][1]["id"],
        list(imported_search["imported"]),
    )
    for step in plan["steps"][3:]:
        project.add_rpa_step(
            paths,
            started["flow_ref"],
            step["component"],
            step_id=step["id"],
            element_ids=[],
            config=step["config"],
        )

    state = project.read_rpa_flow(paths, started["flow_ref"])["flow"]
    assert [item["component"] for item in state["steps"]] == [item["component"] for item in plan["steps"]]
    assert state["next_action"]["tool"] == "shizai_rpa_validate_flow"
    js_step = next(item for item in state["steps"] if item["component"] == "WebBrowser_RunJsInIeBySelector_v3")
    assert js_step["config"]["jsCode"].strip('"') == plan["steps"][5]["config"]["jsCode"]
    js_validation = project.validate_rpa_step(paths, started["flow_ref"], plan["steps"][5]["id"])
    assert js_validation["valid"] is True
    assert next(item for item in js_validation["checks"] if item["name"] == "js_table_script")["passed"] is True
    metadata = json.loads((native / "szrpa.json").read_text(encoding="utf-8"))
    assert metadata["mcpOriginalQuery"] == query
    assert metadata["mcpPlanId"] == plan["plan_id"]
    assert metadata["mcpJsOutputMode"] == "table"


def test_start_rpa_design_writes_complete_plan_before_design_time_execution(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "complete-before-execution"
    _, analysis, raw_plan = _generic_browser_plan_fixture()
    plan = project.validate_rpa_execution_plan(analysis, raw_plan)["plan"]

    class Client:
        def call(self, service, method, args):
            assert (service, method) == ("ApplicationService", "createApplication")
            native.mkdir(parents=True)
            (native / "szrpa.json").write_text(
                json.dumps({"id": native.name, "packageName": plan["name"], "componentsNum": 0}),
                encoding="utf-8",
            )
            (native / "elements.json").write_text("{}", encoding="utf-8")
            (native / "project.flow").write_text("flow", encoding="utf-8")
            (native / "main.sz").write_text("main", encoding="utf-8")
            (native / "main.task").write_text(
                json.dumps(
                    {
                        "componentModelEntities": {
                            "MAIN_COMPONENT": {
                                "id": "MAIN_COMPONENT",
                                "type": "main",
                                "args0": [{"name": "statements", "value": []}],
                            }
                        },
                        "dataTag": 1,
                    }
                ),
                encoding="utf-8",
            )
            return {
                "applicationId": native.name,
                "applicationPackageId": f"{native.name}-package",
                "localPath": str(native),
                "factoryVersion": "7.3.7",
                "runtimeVersion": "7.3.7.0",
            }

    started = project.start_rpa_design(
        paths,
        name=plan["name"],
        target_url=plan["steps"][0]["config"]["url"],
        output_file=plan["output_contract"]["file"],
        required_columns=plan["output_contract"]["columns"],
        use_excel_components=True,
        first_step=plan["steps"][0],
        component_plan=plan["steps"],
        request_analysis=analysis,
        client=Client(),
    )

    state = project.read_rpa_flow(paths, started["flow_ref"])["flow"]
    assert started["design_complete"] is True
    assert started["planned_component_count"] == 8
    assert started["written_component_count"] == 8
    assert [item["id"] for item in state["steps"]] == [item["id"] for item in plan["steps"]]
    assert [item["component"] for item in state["steps"]] == [item["component"] for item in plan["steps"]]
    assert started["unbound_steps"] == ["set-query"]
    assert state["next_action"]["tool"] == "shizai_rpa_scan_page"
    assert state["next_action"]["step_id"] == "set-query"


def test_staged_rpa_flow_is_built_component_by_component(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "jd-plan"
    created = project.create_rpa_flow(
        str(staged),
        name="京东手机采集",
        target_url="https://www.jd.com/",
    )
    assert created["phase"] == "created"
    assert created["next_action"]["tool"] == "shizai_rpa_components"
    catalog = project.rpa_components(paths, flow_ref=str(staged))
    assert catalog["first_component"] is None
    assert catalog["component_types"] == []
    assert catalog["next_action"]["tool"] == "shizai_rpa_add_step"
    assert "component" not in catalog["next_action"]["arguments"]
    assert catalog["next_action"]["selection"] == "model_decides"

    opened = project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open-chrome")
    assert opened["phase"] == "browser_capture_required"
    assert opened["step"]["config"]["type"] == "chrome"
    assert opened["next_action"]["kind"] == "mcp_tool"
    assert opened["next_action"]["tool"] == "shizai_rpa_add_step"
    assert opened["next_action"]["selection"] == "model_decides"
    assert opened["ui_feedback"]["phase"] == "step_ready"
    assert opened["ui_feedback"]["activity"] == "component_validation"

    unbound_search = project.add_rpa_step(
        paths,
        str(staged),
        "Element_SetValue_V6",
        step_id="search-input",
        config={"text": "手机"},
    )
    assert unbound_search["step"]["binding_status"] == "unbound"
    assert unbound_search["next_action"]["tool"] == "shizai_rpa_scan_page"
    assert unbound_search["next_action"]["arguments"]["step_id"] == "search-input"
    pending_submit = project.add_rpa_step(paths, str(staged), "Keyboard_PressNew", step_id="submit-search")
    assert pending_submit["step"]["binding_status"] == "not_required"

    search_element = {
        "id": "jd-search",
        "name": "京东搜索框",
        "title": "京东首页",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
        "attributes": {"aria-label": "搜索"},
    }
    assert project.validate_captured_elements([search_element])["valid"] is True
    imported_search = project.import_elements(paths, str(staged), [search_element])
    assert imported_search["staged_flow"]["phase"] == "elements_captured"
    project.set_rpa_step_elements(
        paths,
        str(staged),
        "search-input",
        ["jd-search"],
    )
    with pytest.raises(ValueError, match="requires an unbound component"):
        project.unbound_rpa_element_step(paths, str(staged), "search-input")
    rebound = project.unbound_rpa_element_step(
        paths,
        str(staged),
        "search-input",
        allow_rebind=True,
    )
    assert rebound["binding_status"] == "bound"
    assert rebound["rebind"] is True
    after_search_capture = project.rpa_components(paths, flow_ref=str(staged))
    assert after_search_capture["next_action"]["selection"] == "model_decides"
    assert after_search_capture["business_rules"][0]["enforcement"] == "warning_only"
    assert "WebBrowser_DataCollectionV5" in after_search_capture["business_rules"][0]["recommendation"]

    result_elements = [
        {
            "id": element_id,
            "name": label,
            "title": "手机 - 商品搜索 - 京东",
            "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
            "tag": "div",
            "css_selector": selector,
            "attributes": {"id": element_id},
        }
        for element_id, label, selector in [
            ("jd-search", "京东搜索框", "#search"),
            ("jd-name", "商品名称", ".goodsContainer .p-name"),
            ("jd-merchant", "商家名称", ".goodsContainer .p-shop"),
            ("jd-price", "商品价格", ".goodsContainer .p-price"),
            ("jd-sales", "商品销量", ".goodsContainer .p-commit"),
            ("jd-link", "商品链接", ".goodsContainer a"),
        ]
    ]
    assert project.validate_captured_elements(result_elements)["valid"] is True
    project.import_elements(paths, str(staged), result_elements)
    project.add_rpa_step(
        paths,
        str(staged),
        "WebBrowser_DataCollectionV5",
        step_id="collect-products",
        element_ids=["jd-name", "jd-merchant", "jd-price", "jd-sales", "jd-link"],
    )
    validated = project.validate_rpa_flow(paths, str(staged))
    assert validated["valid"] is True
    assert validated["step_count"] == 4
    assert validated["bound_element_count"] == 6

    read_back = project.read_rpa_flow(paths, str(staged))
    assert [step["component"] for step in read_back["flow"]["steps"]] == [
        "WebBrowser_CreateV3",
        "Element_SetValue_V6",
        "Keyboard_PressNew",
        "WebBrowser_DataCollectionV5",
    ]


def test_start_rpa_design_creates_components_in_first_step(tmp_path):
    paths = _paths(tmp_path)
    result = project.start_rpa_design(
        paths, str(tmp_path / "started"), target_url="https://example.test/", keyword="手机"
    )
    assert result["flow"]["phase"] == "created"
    assert result["first_component"]["step"]["component"] == "WebBrowser_CreateV3"
    assert result["component_catalog"]["flow"]["step_count"] == 0
    assert result["next_action"]["tool"] == "shizai_rpa_capture_windows"
    assert result["next_action"]["optional_picker_tool"] == "shizai_extension_start_capture"
    assert result["ui_feedback"]["phase"] == "designing"
    assert result["ui_feedback"]["animation"] == "component_ready"
    assert result["ui_feedback"]["step_count"] == 1
    read_back = project.read_rpa_flow(paths, result["flow"]["flow_ref"])
    assert [step["component"] for step in read_back["flow"]["steps"]] == ["WebBrowser_CreateV3"]


def test_rpa_append_guard_rejects_duplicate_collection_and_collection_after_excel(tmp_path):
    paths = _paths(tmp_path)
    element = {
        "id": "product-title",
        "name": "商品标题",
        "title": "京东结果页",
        "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
        "tag": "div",
        "css_selector": ".product-title",
    }

    duplicate_flow = tmp_path / "duplicate-collection"
    project.create_rpa_flow(str(duplicate_flow), required_columns=["商品标题"])
    project.import_elements(paths, str(duplicate_flow), [element])
    project.add_rpa_step(
        paths,
        str(duplicate_flow),
        "WebBrowser_DataCollectionV5",
        step_id="collect-products",
        element_ids=["product-title"],
    )
    with pytest.raises(ValueError, match="designer_state_conflict.*second collection"):
        project.add_rpa_step(
            paths,
            str(duplicate_flow),
            "WebBrowser_DataCollectionV5",
            step_id="collect-products-2",
            element_ids=["product-title"],
        )

    excel_first_flow = tmp_path / "excel-first"
    project.create_rpa_flow(str(excel_first_flow), required_columns=["商品标题"])
    project.import_elements(paths, str(excel_first_flow), [element])
    project.add_rpa_step(paths, str(excel_first_flow), "Excel_Cs_NewV4", step_id="create-excel")
    with pytest.raises(ValueError, match="designer_state_conflict.*Excel output chain"):
        project.add_rpa_step(
            paths,
            str(excel_first_flow),
            "WebBrowser_DataCollectionV5",
            step_id="collect-products",
            element_ids=["product-title"],
        )


def test_native_designer_state_consistency_reports_stale_derived_files(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "stale-native")
    task = native / "io5cd.task"
    task.write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    module = native / "projects" / "flow_modules" / "io5cd.py"
    module.parent.mkdir(parents=True)
    module.write_text("# stale generated module\n", encoding="utf-8")
    (native / "file_index_700.json").write_text(
        json.dumps(
            {
                "flow_modules/io5cd.py": {
                    "taskTime": 1,
                    "codeTime": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    state = project._native_designer_state_consistency(native, task)
    assert state["conflict"] is True
    assert any("taskTime is older" in item for item in state["errors"])


def test_cli_native_write_commands_forward_desktop_client(monkeypatch, tmp_path):
    calls = []
    desktop = object()
    monkeypatch.setattr(cli_module, "_desktop", lambda: desktop)
    monkeypatch.setattr(
        cli_module.project_core,
        "add_rpa_step",
        lambda *args, **kwargs: calls.append(("add", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        cli_module.project_core,
        "set_rpa_step_elements",
        lambda *args, **kwargs: calls.append(("bind", args, kwargs)) or {"ok": True},
    )
    runner = CliRunner()

    added = runner.invoke(
        cli,
        [
            "workflow",
            "rpa-add-step",
            "--flow",
            str(tmp_path),
            "--component",
            "WebBrowser_CreateV3",
            "--allow-write",
        ],
    )
    bound = runner.invoke(
        cli,
        [
            "workflow",
            "rpa-bind-elements",
            "--flow",
            str(tmp_path),
            "--step-id",
            "search",
            "--element-id",
            "search-element",
            "--allow-write",
        ],
    )

    assert added.exit_code == 0, added.output
    assert bound.exit_code == 0, bound.output
    assert calls[0][2]["client"] is desktop
    assert calls[1][2]["client"] is desktop


def test_cli_capture_page_selector_uses_pagination_binding(monkeypatch, tmp_path):
    calls = []
    desktop = object()

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

    monkeypatch.setattr(cli_module, "_desktop", lambda: desktop)
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(cli_module, "_ensure_capture_session", lambda *_args: None)
    monkeypatch.setattr(
        cli_module,
        "_capture_read_with_recovery",
        lambda *_args, **_kwargs: ({"elements": [{"id": "next"}]}, None),
    )
    monkeypatch.setattr(cli_module, "_stop_picker_after_capture", lambda *_args, **_kwargs: {"active": False})
    monkeypatch.setattr(
        cli_module.project_core,
        "import_elements",
        lambda *_args, **_kwargs: {"imported": ["next"]},
    )
    monkeypatch.setattr(
        cli_module.project_core,
        "bind_rpa_pagination",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"pagination_binding": {"mode": "load-more"}},
    )
    monkeypatch.setattr(
        cli_module.project_core,
        "set_rpa_step_elements",
        lambda *_args, **_kwargs: pytest.fail("pagination capture must not bind as collection columns"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "workflow",
            "rpa-capture-elements",
            "--element-id",
            "uia:next",
            "--project",
            str(tmp_path),
            "--step-id",
            "collect",
            "--as-page-selector",
            "--pagination-mode",
            "load-more",
            "--allow-write",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0][2:] == ("collect", "next")
    assert calls[0][1]["mode"] == "load-more"
    assert calls[0][1]["client"] is desktop


def test_native_reorder_steps_requires_exact_permutation_and_rebuilds(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "reorder-native")
    task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {
                "id": "MAIN_COMPONENT",
                "type": "main",
                "args0": [{"name": "statements", "value": ["open", "collect", "extract"]}],
            },
            "open": {"id": "open", "type": "WebBrowser_CreateV3"},
            "collect": {"id": "collect", "type": "WebBrowser_DataCollectionV5"},
            "extract": {"id": "extract", "type": "WebBrowser_RunJsInIeBySelector_v3"},
        }
    }
    (target / "io5cd.task").write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(project, "_finalize_native_writeback", lambda *args, **kwargs: {"ok": True})

    with pytest.raises(ValueError, match="exact permutation"):
        project.reorder_native_rpa_steps(paths, str(target), ["open", "extract"])

    result = project.reorder_native_rpa_steps(paths, str(target), ["open", "extract", "collect"])
    assert result["changed"] is True
    updated = json.loads((target / "io5cd.task").read_text(encoding="utf-8"))
    assert updated["componentModelEntities"]["MAIN_COMPONENT"]["args0"][0]["value"] == [
        "open",
        "extract",
        "collect",
    ]


def test_staged_js_link_recovery_persists_result_state_and_validates(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "jd-js-links"
    created = project.create_rpa_flow(
        str(staged),
        name="京东手机采集 JS 链接",
        target_url="https://www.jd.com/",
        use_excel_components=True,
        use_js_link=True,
    )
    assert created["next_action"]["tool"] == "shizai_rpa_components"
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open-chrome")
    search = {
        "id": "jd-search",
        "name": "京东搜索框",
        "title": "京东首页",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
        "attributes": {"aria-label": "搜索"},
    }
    fields = [
        {
            "id": item_id,
            "name": label,
            "title": "京东搜索结果",
            "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
            "tag": "div",
            "css_selector": selector,
            "attributes": {"id": item_id},
        }
        for item_id, label, selector in [
            ("jd-name", "商品名称", ".goodsContainer .p-name"),
            ("jd-merchant", "商家名称", ".goodsContainer .p-shop"),
            ("jd-price", "价格", ".goodsContainer .p-price"),
            ("jd-sales", "销量", ".goodsContainer .p-commit"),
        ]
    ]
    project.import_elements(paths, str(staged), [search, *fields])
    project.add_rpa_step(
        paths,
        str(staged),
        "Element_SetValue_V6",
        step_id="search-input",
        element_ids=["jd-search"],
        config={"text": "手机"},
    )
    project.add_rpa_step(paths, str(staged), "Keyboard_PressNew", step_id="submit-search")
    state = project.mark_rpa_result_capture_ready(
        paths,
        str(staged),
        captured_fields={item: item for item in ("product_name", "merchant", "price", "sales")},
        field_element_ids={
            "product_name": "jd-name",
            "merchant": "jd-merchant",
            "price": "jd-price",
            "sales": "jd-sales",
        },
        use_js_link=True,
        selector_scope="goods-card-family",
    )
    assert state["result_elements_ready"] is True
    assert state["next_action"]["tool"] == "shizai_rpa_add_step"
    assert state["next_action"]["selection"] == "model_decides"
    assert "component" not in state["next_action"]["arguments"]
    js = project.add_rpa_step(
        paths,
        str(staged),
        "WebBrowser_RunJsInIeBySelector_v3",
        step_id="extract-product-links",
        element_ids=["jd-name"],
    )
    assert js["step_validation"]["valid"] is True, (js["step_validation"]["errors"], js["step_validation"]["warnings"])
    generated = project.read_elements(paths, str(staged))
    assert project.JS_LINK_ELEMENT_ID in generated["native"]["elementMap"]
    assert project.JS_MERCHANT_ELEMENT_ID in generated["native"]["elementMap"]
    assert project.JS_NAME_ELEMENT_ID in generated["native"]["elementMap"]
    assert project.JS_PRICE_ELEMENT_ID in generated["native"]["elementMap"]
    assert project.JS_SALES_ELEMENT_ID in generated["native"]["elementMap"]
    assert project.JS_MERCHANT_MARKER in project.DEFAULT_JS_LINK_SCRIPT
    marker_chain = project._collection_selector_chain(generated["native"]["elementMap"][project.JS_NAME_ELEMENT_ID])
    assert marker_chain == [
        {
            "name": "span",
            "type": "web",
            "accurate": "true",
            "props": [
                {
                    "name": "class",
                    "value": project.JS_NAME_MARKER,
                    "textValue": project.JS_NAME_MARKER,
                    "pattern": "equal",
                    "accurate": "false",
                    "expressionValue": "",
                }
            ],
        }
    ]
    collection = project.add_rpa_step(
        paths,
        str(staged),
        "WebBrowser_DataCollectionV5",
        step_id="collect-products",
        element_ids=["jd-name", "jd-price", "jd-sales"],
    )
    assert collection["step_validation"]["valid"] is True
    assert collection["step"]["element_ids"] == [
        project.JS_NAME_ELEMENT_ID,
        project.JS_MERCHANT_ELEMENT_ID,
        project.JS_PRICE_ELEMENT_ID,
        project.JS_SALES_ELEMENT_ID,
        project.JS_LINK_ELEMENT_ID,
    ]
    task_path = project._rpa_flow_file(paths, str(staged))
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    collection_node = next(item for item in task_payload["steps"] if item["id"] == "collect-products")
    collection_config, collection_count = project._staged_collection_config(
        generated["native"],
        collection_node["element_ids"],
        keyword="手机",
        columns=task_payload["required_columns"],
    )
    assert collection_count == 5
    assert collection_config is not None
    assert collection_config["collectionConfig"]["tableSelector"] == []
    first_selector_prop = collection_config["collectionConfig"]["columnSelectorArray"][0][0]["props"][0]
    assert first_selector_prop["name"] == "class"
    assert first_selector_prop["value"] == project.JS_NAME_MARKER
    project.add_rpa_step(paths, str(staged), "Excel_Cs_NewV4", step_id="create-excel")
    project.add_rpa_step(paths, str(staged), "Excel_Cs_WriteRange", step_id="write-excel")
    read_back = project.read_rpa_flow(paths, str(staged))["flow"]
    assert read_back["result_elements_ready"] is True
    assert [step["component"] for step in read_back["steps"]] == [
        "WebBrowser_CreateV3",
        "Element_SetValue_V6",
        "Keyboard_PressNew",
        "WebBrowser_RunJsInIeBySelector_v3",
        "WebBrowser_DataCollectionV5",
        "Excel_Cs_NewV4",
        "Excel_Cs_WriteRange",
    ]
    validated = project.validate_rpa_flow(paths, str(staged), strict=True)
    assert validated["valid"] is True
    assert validated["use_js_link"] is True
    assert validated["result_elements_ready"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, '[".card"]'),
        ("", '[".card"]'),
        (".card", '[".card"]'),
        ('".card"', '[".card"]'),
        ([".card"], '[".card"]'),
        ('[".card"]', '[".card"]'),
        ("[selector]", "[selector]"),
    ],
)
def test_native_list_expression_normalizes_js_input(value, expected):
    assert project._native_list_expression(value, ".card") == expected


def test_default_js_link_script_accepts_native_list_input():
    assert "Array.isArray(input)" in project.DEFAULT_JS_LINK_SCRIPT
    assert "inputValues.find" in project.DEFAULT_JS_LINK_SCRIPT


def test_default_js_table_script_recovers_nested_jd_seller_from_product_link():
    assert 'searchParams.get("seller")' in project.DEFAULT_JS_TABLE_SCRIPT
    assert "decodeURIComponent(decoded)" in project.DEFAULT_JS_TABLE_SCRIPT


def test_default_js_table_script_filters_price_noise_and_keeps_reservation_price():
    script = project.DEFAULT_JS_TABLE_SCRIPT
    assert "priceNoise" in script
    assert "看过|种草|人付款" in script
    assert "reservationAmount" in script
    assert "预约|预售|定金" in script
    assert "normalizePrice" in script


def test_default_js_table_script_only_accepts_explicit_sales_signals():
    script = project.DEFAULT_JS_TABLE_SCRIPT
    assert "extractSales" in script
    assert "已售|销量|成交" in script
    assert "人付款" in script


def test_default_js_table_script_rejects_chat_links_and_normalizes_price():
    script = project.DEFAULT_JS_TABLE_SCRIPT
    assert "chat.jd.com" in script
    assert "derivedProductHrefs" in script
    assert "item.jd.com/${productId}.html" in script
    assert "cardLink" in script
    assert "sellerHint" in script
    assert "sellerFromUrl" in script
    assert "normalizePrice" in script


def test_start_rpa_design_creates_client_owned_native_flow(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    ui_events = []
    monkeypatch.setattr(
        project,
        "_publish_rpa_ui",
        lambda state, phase, message, **details: (
            ui_events.append({"state": state, "phase": phase, "message": message, **details}) or {"phase": phase}
        ),
    )
    template = _make_project(paths, "native-template")
    template_task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {
                "id": "MAIN_COMPONENT",
                "type": "main",
                "args0": [
                    {"name": "statements", "value": []},
                ],
            },
            "browser": {"id": "browser", "type": "WebBrowser_CreateV3", "label": "打开网页", "args0": []},
            "input": {"id": "input", "type": "Element_SetValue_V6", "label": "输入文本", "args0": []},
            "keyboard": {"id": "keyboard", "type": "Keyboard_PressNew", "label": "键盘模拟按键", "args0": []},
            "collection": {"id": "collection", "type": "WebBrowser_DataCollectionV5", "label": "数据采集", "args0": []},
        },
        "dataTag": 1,
    }
    (template / "io5cd.task").write_text(json.dumps(template_task), encoding="utf-8")
    native = paths.applications_root / "created-native"

    class Client:
        def __init__(self):
            self.calls = []

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            assert service == "ApplicationService"
            assert method == "createApplication"
            native.mkdir(parents=True)
            (native / "szrpa.json").write_text(
                json.dumps(
                    {
                        "id": "created-native",
                        "packageName": "客户端原生流程",
                        "componentsNum": 0,
                    }
                ),
                encoding="utf-8",
            )
            (native / "elements.json").write_text("{}", encoding="utf-8")
            (native / "project.flow").write_text("flow", encoding="utf-8")
            (native / "main.sz").write_text("main", encoding="utf-8")
            (native / "io5cd.task").write_text(
                json.dumps(
                    {
                        "componentModelEntities": {
                            "MAIN_COMPONENT": {
                                "id": "MAIN_COMPONENT",
                                "type": "main",
                                "args0": [{"name": "statements", "value": []}],
                            }
                        },
                        "dataTag": 1,
                    }
                ),
                encoding="utf-8",
            )
            return {
                "applicationId": "created-native",
                "applicationPackageId": "package-native",
                "localPath": str(native),
                "factoryVersion": "7.3.6",
                "runtimeVersion": "7.3.6.1",
                "version": "0.0.1",
            }

    client = Client()
    started = project.start_rpa_design(
        paths,
        name="客户端原生流程",
        target_url="https://example.test/",
        keyword="平板",
        output_file="tablet-products.xls",
        wait_after_submit=False,
        refresh_browser_tab=False,
        client=client,
    )
    assert started["mode"] == "native_client_application"
    assert started["external_staging_required"] is False
    assert started["project_ref"] == str(native.resolve())
    assert started["first_component"]["kind"] == "native_rpa_step_added"
    assert ui_events[0]["phase"] == "flow_creation"
    assert ui_events[0]["component"] == "WebBrowser_CreateV3"
    assert "首个计划组件 WebBrowser_CreateV3" in ui_events[0]["message"]
    assert client.calls[0][1] == "createApplication"
    metadata = json.loads((native / "szrpa.json").read_text(encoding="utf-8"))
    assert metadata["mcpKeyword"] == "平板"
    assert metadata["mcpOutputFile"] == "tablet-products.xls"
    assert metadata["mcpOpenNewTab"] is False

    updated = project.update_rpa_step_config(
        paths,
        str(native),
        "open-chrome",
        {"openNewTab": True},
        client=client,
    )
    assert updated["step_validation"]["valid"] is True
    task_after_update = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    open_args_after_update = {
        item["name"]: item["value"] for item in task_after_update["componentModelEntities"]["open-chrome"]["args0"]
    }
    assert open_args_after_update["openNewTab"] == "1"
    assert json.loads((native / "szrpa.json").read_text(encoding="utf-8"))["mcpOpenNewTab"] is True
    updated = project.update_rpa_step_config(
        paths,
        str(native),
        "open-chrome",
        {"openNewTab": False},
        client=client,
    )
    assert updated["step_validation"]["valid"] is True
    task_after_reuse = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    open_args_after_reuse = {
        item["name"]: item["value"] for item in task_after_reuse["componentModelEntities"]["open-chrome"]["args0"]
    }
    assert open_args_after_reuse["openNewTab"] == "0"

    search = {
        "id": "search",
        "name": "搜索框",
        "title": "首页",
        "url": "https://example.test/",
        "tag": "input",
        "css_selector": "#search",
        "attributes": {"aria-label": "搜索"},
    }
    fields = [
        {
            "id": item_id,
            "name": label,
            "title": "结果页",
            "url": "https://example.test/results",
            "tag": "div",
            "css_selector": selector,
            "attributes": {"id": item_id},
        }
        for item_id, label, selector in [
            ("name", "商品名称", ".name"),
            ("merchant", "商家名称", ".merchant"),
            ("price", "价格", ".price"),
            ("sales", "销量", ".sales"),
            ("link", "商品链接", ".link"),
        ]
    ]
    project.import_elements(paths, str(native), [search])
    project.add_rpa_step(
        paths,
        str(native),
        "Element_SetValue_V6",
        step_id="search-input",
        element_ids=["search"],
        config={"text": "平板"},
    )
    project.add_rpa_step(paths, str(native), "Keyboard_PressNew", step_id="submit-search")
    project.import_elements(paths, str(native), fields)
    project.add_rpa_step(
        paths,
        str(native),
        "WebBrowser_DataCollectionV5",
        step_id="collect-products",
        element_ids=["name", "merchant", "price", "sales", "link"],
    )
    next_page = {
        "id": "next-page",
        "name": "下一页",
        "title": "结果页",
        "url": "https://example.test/results",
        "tag": "a",
        "css_selector": ".next-page",
        "attributes": {"aria-label": "下一页"},
    }
    project.import_elements(paths, str(native), [next_page], capture_source="deep_capture")
    pagination = project.bind_rpa_pagination(
        paths,
        str(native),
        "collect-products",
        "next-page",
        mode="next-page",
        client=client,
    )
    assert pagination["pagination_binding"]["selector_argument"] == "selector_page"
    assert pagination["pagination_binding"]["readback"]["collection_columns_unchanged"] is True
    assert pagination["step"]["config"]["pagination_capability"]["selected_mode"] == "next-page"
    # Updating semantic fields must rewrite the nested RuntimeHost config,
    # even before a fresh four-field capture is rebound.
    project.update_rpa_step_config(
        paths,
        str(native),
        "collect-products",
        {
            "columns": ["商品标题", "商品价格", "商品链接", "商品销量"],
            "data_attributes": {
                "商品标题": "innerText",
                "商品价格": "innerText",
                "商品链接": "href",
                "商品销量": "innerText",
            },
            "page_url": "https://example.test/results?keyword=平板",
            "page_title": "平板结果页",
        },
        client=client,
    )
    updated_task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    updated_node = updated_task["componentModelEntities"]["collect-products"]
    updated_nested = json.loads(next(item for item in updated_node["args0"] if item["name"] == "config")["value"])
    assert updated_nested["pageInfo"]["pageUrl"] == "https://example.test/results?keyword=平板"
    assert updated_nested["pageInfo"]["windowTitle"] == "平板结果页"
    assert [item["columnHeader"] for item in updated_nested["collectionConfig"]["columnConfigArray"]] == [
        "商品标题", "商品价格", "商品链接", "商品销量"
    ]
    assert [item["columnHeader_bk"] for item in updated_nested["collectionConfig"]["columnConfigArray"]] == [
        "商品标题", "商品价格", "商品链接", "商品销量"
    ]
    assert [item["dataAttrName"] for item in updated_nested["collectionConfig"]["columnConfigArray"]] == [
        "innerText", "innerText", "href", "innerText"
    ]
    # Rebinding with a fresh capture regenerates selectors in the same order;
    # RuntimeHost must never retain the previous five-column selector set.
    rebound = project.set_rpa_step_elements(
        paths,
        str(native),
        "collect-products",
        ["name", "price", "link", "sales"],
        client=client,
    )
    assert rebound["step_validation"]["valid"] is True, rebound["step_validation"]
    rebound_task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    rebound_node = rebound_task["componentModelEntities"]["collect-products"]
    rebound_nested = json.loads(next(item for item in rebound_node["args0"] if item["name"] == "config")["value"])
    assert len(rebound_nested["collectionConfig"]["columnSelectorArray"]) == 4
    assert len(rebound_nested["collectionConfig"]["columnConfigArray"]) == 4
    assert rebound_nested["collectionConfig"]["columnSelectorArray"][-1][0]["props"]
    task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    entities = task["componentModelEntities"]
    statement_ids = next(item["value"] for item in entities["MAIN_COMPONENT"]["args0"] if item["name"] == "statements")
    assert [entities[item]["type"] for item in statement_ids] == list(project.RPA_RECOMMENDED_SEQUENCE)
    assert statement_ids[0] == "open-chrome"
    assert statement_ids[1] == "search-input"
    assert statement_ids[3] == "collect-products"
    open_args = {item["name"]: item["value"] for item in entities["open-chrome"]["args0"]}
    assert open_args["openNewTab"] == "0"
    task_args = {item["name"]: item["value"] for item in entities["search-input"]["args0"]}
    assert json.loads(task_args["text"]) == "平板"
    collection_args = {item["name"]: item["value"] for item in entities["collect-products"]["args0"]}
    assert json.loads(collection_args["filename_suffix"][0]) == "tablet-products"
    assert collection_args["designated_obj"] == "网页对象"
    assert json.loads((native / "szrpa.json").read_text(encoding="utf-8"))["componentsNum"] == 4
    assert project.validate_rpa_flow(paths, str(native))["valid"] is True
    assert project.validate_rpa_project(paths, str(native))["valid"] is True


def test_native_rpa_runtime_maps_package_ids_and_task_node(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "runtime-native"

    class Client:
        def __init__(self):
            self.calls = []

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            if method == "createApplication":
                native.mkdir(parents=True)
                (native / "szrpa.json").write_text(
                    json.dumps(
                        {
                            "id": "runtime-native",
                            "packageName": "运行测试流程",
                            "componentsNum": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                (native / "elements.json").write_text("{}", encoding="utf-8")
                (native / "project.flow").write_text("flow", encoding="utf-8")
                (native / "main.sz").write_text("main", encoding="utf-8")
                (native / "io5cd.task").write_text(
                    json.dumps(
                        {
                            "componentModelEntities": {
                                "MAIN_COMPONENT": {
                                    "id": "MAIN_COMPONENT",
                                    "type": "main",
                                    "args0": [{"name": "statements", "value": []}],
                                }
                            },
                            "dataTag": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                return {
                    "applicationId": "app-runtime-001",
                    "applicationPackageId": "pkg-runtime-001",
                    "localPath": str(native),
                }
            if method == "workflowDebugRunRPA":
                return {"isSuccess": True, "data": {"accepted": True}}
            if method == "workflowDebugStop":
                return {"isSuccess": True, "data": {"stopped": True}}
            raise AssertionError(method)

    client = Client()
    project.start_rpa_design(
        paths,
        name="运行测试流程",
        target_url="https://example.test/",
        keyword="手机",
        wait_after_submit=False,
        refresh_browser_tab=False,
        client=client,
    )
    search = {
        "id": "search",
        "name": "搜索框",
        "title": "首页",
        "url": "https://example.test/",
        "tag": "input",
        "css_selector": "#search",
        "attributes": {"aria-label": "搜索"},
    }
    fields = [
        {
            "id": item_id,
            "name": label,
            "title": "结果页",
            "url": "https://example.test/results",
            "tag": "div",
            "css_selector": selector,
            "attributes": {"id": item_id},
        }
        for item_id, label, selector in [
            ("name", "商品名称", ".name"),
            ("merchant", "商家名称", ".merchant"),
            ("price", "价格", ".price"),
            ("sales", "销量", ".sales"),
            ("link", "商品链接", ".link"),
        ]
    ]
    project.import_elements(paths, str(native), [search])
    project.add_rpa_step(paths, str(native), "Element_SetValue_V6", step_id="search", element_ids=["search"])
    project.add_rpa_step(paths, str(native), "Keyboard_PressNew", step_id="submit")
    project.import_elements(paths, str(native), fields)
    project.add_rpa_step(
        paths,
        str(native),
        "WebBrowser_DataCollectionV5",
        step_id="collect",
        element_ids=["name", "merchant", "price", "sales", "link"],
    )

    paths.data_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.sqlite_db) as connection:
        connection.execute(
            "CREATE TABLE application_package (id INTEGER PRIMARY KEY, packageId TEXT, applicationId TEXT, packageLocalFullPath TEXT, runtimeVersion TEXT, factoryVersion TEXT, applicationName TEXT)"
        )
        connection.execute(
            "INSERT INTO application_package VALUES (1, ?, ?, ?, ?, ?, ?)",
            ("pkg-runtime-001", "app-runtime-001", str(native), "7.3.7.0", "7.3.7", "运行测试流程"),
        )

    diagnosis = project.native_rpa_sync_diagnose(paths, str(native))
    assert diagnosis["can_run"] is True
    assert diagnosis["package"]["application_id"] == "app-runtime-001"
    assert diagnosis["package"]["package_id"] == "pkg-runtime-001"
    started = project.native_rpa_run(
        paths,
        str(native),
        run_uuid="test-rpa-uuid",
        workflow_run_id="test-rpa-run",
        client=client,
    )
    request = next(item[2][0] for item in client.calls if item[1] == "workflowDebugRunRPA")
    assert request["app_id"] == "app-runtime-001"
    assert request["applicationId"] == "app-runtime-001"
    assert request["node_id"] == "io5cd"
    assert request["inputParam"] == ""
    assert started["started"] is True
    assert started["status"]["run"]["package_id"] == "pkg-runtime-001"


def test_native_runtime_log_lines_include_following_uncorrelated_error(tmp_path):
    paths = _paths(tmp_path)
    log_path = paths.data_root / "logs" / "2026-08-27" / "front_logs" / "node-main.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "old entry",
                "workflowDebugRunRPA uuid=run-uuid",
                "DebugWorkExecuteService run-writeInputParams '' all",
                "DebugWorkExecuteService run-error BizException: fixture failure",
                "unrelated later entry",
            ]
        ),
        encoding="utf-8",
    )
    lines = project._native_runtime_log_lines(
        paths,
        {"uuid": "run-uuid", "workflow_run_id": "workflow-id"},
    )
    assert any("run-error" in line for line in lines)
    assert project._native_run_status({"status": "submitted", "submitted_at": 0}, None, lines) == "failed"


def test_native_runtime_log_lines_prioritize_current_run_file(tmp_path):
    paths = _paths(tmp_path)
    log_root = paths.data_root / "logs" / "2026-08-29" / "output-logs"
    log_root.mkdir(parents=True)
    current_stamp = 1700000000123
    current = log_root / f"run_{current_stamp}_current.jsonl"
    current.write_text(
        json.dumps({"data": {"engineAction": "error", "text": "run-error current fixture"}}) + "\n",
        encoding="utf-8",
    )
    # Add enough unrelated run files to exercise the bounded evidence list.
    for index in range(30):
        (log_root / f"run_{current_stamp + 1000 + index}_old{index}.jsonl").write_text(
            "old fixture\n",
            encoding="utf-8",
        )

    lines = project._native_runtime_log_lines(
        paths,
        {"uuid": "current-uuid", "workflow_run_id": "current-run", "submitted_at": current_stamp / 1000},
    )

    assert any("run-error current fixture" in line for line in lines)


def test_native_run_status_treats_runtime_success_without_fresh_output_as_failed():
    lines = [
        json.dumps(
            {
                "timestamp": 2_500,
                "data": {"engineAction": "run", "text": "成功", "runStatus": "success"},
            }
        )
    ]

    assert project._native_run_status({"status": "submitted", "submitted_at": 2}, None, lines) == "failed"
    assert (
        project._native_run_status({"status": "submitted", "submitted_at": 2, "terminal_status": "success"}, None, [])
        == "failed"
    )


def test_native_run_evidence_ignores_historical_terminal_record():
    lines = [
        json.dumps(
            {
                "timestamp": 500,
                "data": {"engineAction": "run", "text": "成功", "runStatus": "success"},
            }
        ),
        json.dumps(
            {
                "timestamp": 2_500,
                "data": {"engineAction": "run", "text": "启动", "runStatus": "started"},
            }
        ),
    ]

    evidence = project._native_run_evidence(lines, submitted_at=2)

    assert evidence["terminal_status"] is None
    assert evidence["is_finished"] is False


def test_native_output_archive_is_recoverable_and_expected_path_is_not_previous_file(tmp_path):
    paths = _paths(tmp_path)
    root = paths.applications_root / "native-output"
    output = root / "res" / "phones.xlsx"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old workbook")

    archived = project._archive_existing_native_output(root, "phones.xlsx")

    assert len(archived) == 1
    assert not output.exists()
    archived_path = Path(archived[0]["to"])
    assert archived_path.name.startswith("phones.previous-")
    assert archived_path.read_bytes() == b"old workbook"
    assert project._native_expected_output_path(root, "phones.xlsx") is None
    assert project._native_output_candidates(root, "phones.xlsx") == [archived_path.resolve()]


def test_native_output_archive_supports_external_configured_path(tmp_path):
    paths = _paths(tmp_path)
    root = paths.applications_root / "native-output"
    external = tmp_path / "Desktop" / "rpa" / "phones.xlsx"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"old workbook")

    archived = project._archive_existing_native_output(root, "phones.xlsx", external)

    assert len(archived) == 1
    assert not external.exists()
    archived_path = Path(archived[0]["to"])
    assert archived_path.parent == external.parent
    assert archived_path.name.startswith("phones.previous-")
    assert archived_path.read_bytes() == b"old workbook"


def test_native_status_uses_external_configured_output_path(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    target = _make_project(paths, "external-output")
    external = tmp_path / "Desktop" / "rpa" / "products.xlsx"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"not-an-xlsx")
    monkeypatch.setattr(
        project,
        "native_rpa_sync_diagnose",
        lambda _paths, _project_ref: {"can_run": True, "runtime": {"ready": True}},
    )
    monkeypatch.setattr(
        project,
        "_native_rpa_state",
        lambda _paths, _project_ref: {
            "output_file": "products.xlsx",
            "output_path": str(external),
            "required_columns": [],
            "steps": [],
            "target_url": "https://www.jd.com/",
        },
    )
    project._write_native_run_state(
        paths,
        target,
        {"status": "submitted", "submitted_at": external.stat().st_mtime - 0.1, "uuid": "external-output"},
    )

    result = project.native_rpa_status(paths, str(target))

    assert result["output"]["path"] == str(external.resolve())
    assert result["output"]["exists"] is True
    assert result["failure_kind"] != "missing_fresh_output"


def test_native_run_output_names_include_explicit_excel_component(tmp_path):
    flow = {
        "output_file": "phones.xlsx",
        "steps": [
            {"component": "WebBrowser_DataCollectionV5", "config": {"output_file": "phones.xls"}},
            {"component": "Excel_Cs_NewV4", "config": {"name": "phones.xlsx"}},
        ],
    }

    assert project._native_run_output_names(flow) == ["phones.xlsx", "phones.xls"]


def test_native_rpa_runtime_requires_database_package_mapping(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-without-package")
    with pytest.raises(ValueError, match="application_package"):
        project.native_rpa_run(paths, str(target), client=object())


def test_native_rpa_stop_is_idempotent_when_no_run_is_active(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-stop")

    class Client:
        def call(self, service, method, args):
            assert (service, method, args) == ("CorpBrainService", "workflowDebugStop", [])
            raise ShizaiCloudError("no active debug run")

    result = project.native_rpa_stop(paths, str(target), client=Client())
    assert result["already_stopped"] is True
    assert result["stopped"] is False


def test_native_cleanup_persists_provider_confirmation_and_releases_stopping(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-cleanup")

    class Client:
        def __init__(self):
            self.calls = []

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            return {"isSuccess": True, "data": {"stopped": True}}

    client = Client()
    project._write_native_run_state(
        paths,
        target,
        {"status": "submitted", "submitted_at": time.time(), "uuid": "cleanup-run"},
    )

    result = project._native_cleanup_runtime(
        paths,
        target,
        caller=client,
        reason="test",
        close_mask=False,
    )

    assert result["status"] == "stopped"
    assert result["cleanup_complete"] is True
    assert result["cleanup_status"] == "complete"
    assert result["runtime_confirmation"] == "stop_rpc_ack"
    assert json.loads(project._native_run_state_path(paths, target).read_text(encoding="utf-8"))["status"] == "stopped"
    assert client.calls == [("CorpBrainService", "workflowDebugStop", [])]


def test_native_cleanup_treats_3001_as_already_stopped(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-cleanup-3001")

    class Client:
        def call(self, service, method, args):
            assert (service, method, args) == ("CorpBrainService", "workflowDebugStop", [])
            raise RuntimeError("当前状态不可停止 (code=3001)")

    result = project._native_cleanup_runtime(
        paths,
        target,
        caller=Client(),
        reason="test",
        close_mask=False,
    )

    assert result["status"] == "stopped"
    assert result["already_stopped"] is True
    assert result["cleanup_complete"] is True
    assert result["cleanup_errors"] == []


def test_native_cleanup_never_leaves_a_failed_stop_as_stopping(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-cleanup-timeout")

    class Client:
        def call(self, *_args):
            raise RuntimeError("Desktop RPC timed out")

    result = project._native_cleanup_runtime(
        paths,
        target,
        caller=Client(),
        reason="test",
        close_mask=False,
    )

    assert result["status"] == "cleanup_timeout"
    assert result["cleanup_complete"] is False
    persisted = json.loads(project._native_run_state_path(paths, target).read_text(encoding="utf-8"))
    assert persisted["status"] == "cleanup_timeout"
    assert persisted["cleanup_status"] == "failed"


def test_native_run_status_expires_orphaned_stopping_marker(monkeypatch):
    monkeypatch.setenv("SHIZAI_RPA_CLEANUP_TIMEOUT_SECONDS", "1")

    assert (
        project._native_run_status(
            {"status": "stopping", "cleanup_started_at": time.time() - 2},
            None,
            [],
        )
        == "cleanup_timeout"
    )


def test_native_run_timeout_uses_common_runtime_cleanup(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-run-timeout")
    task = target / "io5cd.task"
    task.write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {"args0": [{"name": "statements", "value": []}]}
                }
            }
        ),
        encoding="utf-8",
    )
    diagnosis = {
        "can_run": True,
        "can_attempt_run": True,
        "errors": [],
        "warnings": [],
        "package": {"application_id": "app", "package_id": "pkg"},
        "task": {"path": str(task), "node_id": "io5cd"},
    }
    monkeypatch.setattr(project, "native_rpa_sync_diagnose", lambda *_args, **_kwargs: diagnosis)
    monkeypatch.setattr(project, "_normalize_native_page_scope_js", lambda *_args, **_kwargs: (task, {}))
    monkeypatch.setattr(project, "_ensure_native_runtime_support", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(project, "_sync_native_designer_content", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project, "_native_rpa_state", lambda *_args, **_kwargs: {"steps": [], "output_file": "result.xlsx"})
    monkeypatch.setattr(
        project,
        "native_rpa_status",
        lambda *_args, **_kwargs: {"status": "stopped", "ui_feedback": {"phase": "idle"}, "run": {}},
    )

    class TimeoutClient:
        def __init__(self):
            self.calls = []

        def run_native_rpa(self, *_args, **_kwargs):
            return {"accepted": True, "completed": False, "timed_out": True, "terminal_status": None}

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            assert (service, method, args) == ("CorpBrainService", "workflowDebugStop", [])
            return {"isSuccess": True, "data": {"stopped": True}}

    client = TimeoutClient()
    result = project.native_rpa_run(paths, str(target), wait=True, client=client)

    assert result["timed_out"] is True
    assert result["runtime_cleanup"]["status"] == "stopped"
    assert client.calls == [("CorpBrainService", "workflowDebugStop", [])]
    persisted = project._read_native_run_state(paths, target)
    assert persisted["status"] == "stopped"
    assert persisted["cleanup_reason"] == "wait_timeout"


def test_cli_rpa_start_design_defaults_to_client_application(monkeypatch):
    calls = []

    def fake_start(paths, output_dir=None, **kwargs):
        calls.append((paths, output_dir, kwargs))
        return {
            "mode": "native_client_application",
            "flow_ref": "CLIENT_APPLICATION_PATH",
            "external_staging_required": False,
        }

    monkeypatch.setattr(cli_module.project_core, "start_rpa_design", fake_start)
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "workflow",
            "rpa-start-design",
            "--name",
            "客户端流程",
            "--url",
            "https://example.test/",
            "--no-open-browser",
            "--allow-write",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls and calls[0][1] is None
    assert calls[0][2]["name"] == "客户端流程"
    assert calls[0][2]["wait_after_submit"] is True
    assert calls[0][2]["wait_seconds"] == 1.0
    assert json.loads(result.output)["external_staging_required"] is False


def test_cli_rpa_start_design_forwards_js_table_mode(monkeypatch):
    calls = []

    def fake_start(paths, output_dir=None, **kwargs):
        calls.append((paths, output_dir, kwargs))
        return {"mode": "native_client_application", "flow_ref": "CLIENT_APPLICATION_PATH"}

    monkeypatch.setattr(cli_module.project_core, "start_rpa_design", fake_start)
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "workflow",
            "rpa-start-design",
            "--url",
            "https://example.test/",
            "--use-js-table",
            "--no-open-browser",
            "--allow-write",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][2]["use_js_table"] is True


def test_cli_rpa_start_design_counts_down_only_after_design_and_before_browser(monkeypatch):
    events = []

    monkeypatch.setattr(
        cli_module.project_core,
        "start_rpa_design",
        lambda *args, **kwargs: (
            events.append("design") or {"mode": "native_client_application", "flow_ref": "CLIENT_APPLICATION_PATH"}
        ),
    )
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())

    class Card:
        def __init__(self, client):
            self.client = client

        def show(self, *, state, message, **kwargs):
            events.append(state)
            return {"state": state}

        def countdown(self, **kwargs):
            events.append("countdown")
            return {"seconds": kwargs["seconds"]}

    class Frame:
        def __init__(self, client):
            self.client = client

        def show(self):
            events.append("frame")
            return {"visible": True}

    monkeypatch.setattr(cli_module, "NativeSmallCard", Card)
    monkeypatch.setattr(cli_module, "NativeTakeoverFrame", Frame)
    monkeypatch.setattr(
        cli_module,
        "open_current_chrome",
        lambda *args, **kwargs: events.append("browser") or {"opened": True},
    )

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "workflow",
            "rpa-start-design",
            "--url",
            "https://example.test/",
            "--allow-write",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == ["design", "ready", "countdown", "frame", "takeover", "browser"]


def test_cli_rpa_start_design_closes_native_ui_after_browser_handoff(monkeypatch):
    events = []

    monkeypatch.setattr(
        cli_module.project_core,
        "start_rpa_design",
        lambda *args, **kwargs: {"flow_ref": "CLIENT_APPLICATION_PATH"},
    )
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())

    class Card:
        def __init__(self, client):
            pass

        def show(self, **kwargs):
            return {"state": kwargs["state"], "visible": True}

        def countdown(self, **kwargs):
            return {"seconds": kwargs["seconds"], "visible": True}

        def close(self):
            events.append("small_card_close")
            return {"visible": False}

    class Frame:
        def __init__(self, client):
            pass

        def show(self):
            return {"visible": True}

        def close(self):
            events.append("takeover_frame_close")
            return {"visible": False}

    class Mask:
        def __init__(self, client):
            pass

        def close(self, **kwargs):
            events.append(("mask_close", kwargs))
            return {"visible": False}

    class Bridge:
        def __init__(self, desktop=None):
            pass

        def stop_inspect(self):
            events.append("inspect_stop")
            return {"overlay_state": "inactive"}

    monkeypatch.setattr(cli_module, "NativeSmallCard", Card)
    monkeypatch.setattr(cli_module, "NativeTakeoverFrame", Frame)
    monkeypatch.setattr(cli_module, "NativeMask", Mask)
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", Bridge)
    monkeypatch.setattr(cli_module, "open_current_chrome", lambda *args, **kwargs: {"opened": True})

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "workflow",
            "rpa-start-design",
            "--url",
            "https://example.test/",
            "--allow-write",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == ["small_card_close", "takeover_frame_close", ("mask_close", {"force": True}), "inspect_stop"]
    payload = json.loads(result.output)
    assert payload["native_ui"]["final_cleanup"]["small_card"]["visible"] is False


def test_mcp_rpa_start_design_returns_created_flow_when_browser_open_fails(monkeypatch):
    calls = []
    query, analysis, plan = _generic_browser_plan_fixture()
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )

    def fake_start(paths, output_dir=None, **kwargs):
        calls.append(kwargs)
        return {
            "kind": "shizai_rpa_started",
            "flow_ref": "CLIENT_APPLICATION_PATH",
            "project_ref": "CLIENT_APPLICATION_PATH",
            "first_component": {"step": {"component": "WebBrowser_CreateV3"}},
        }

    monkeypatch.setattr(mcp_server.project_core, "start_rpa_design", fake_start)
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: {
            "next_action": {"tool": "shizai_rpa_add_step"},
            "plan_id": "fixture-plan",
        },
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: (_ for _ in ()).throw(ShizaiCloudError("navigation fixture unavailable")),
    )
    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
            "open_browser": True,
        },
    )
    assert result["flow_ref"] == "CLIENT_APPLICATION_PATH"
    assert "use_js_table" not in calls[0]
    assert calls[0]["first_step"]["id"] == "open-target"
    assert result["design_time_browser"]["opened"] is False
    assert result["browser_open_warning"] == "navigation fixture unavailable"


def test_mcp_rpa_start_design_initializes_capture_without_red_picker_after_browser_open(monkeypatch):
    calls = []
    query, analysis, plan = _generic_browser_plan_fixture()
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )

    def fake_start(paths, output_dir=None, **kwargs):
        return {
            "kind": "shizai_rpa_started",
            "flow_ref": "CLIENT_APPLICATION_PATH",
            "project_ref": "CLIENT_APPLICATION_PATH",
            "first_component": {"step": {"component": "WebBrowser_CreateV3"}},
        }

    class FakeBridge:
        def __init__(self, desktop=None):
            calls.append(("bridge", desktop))

        def start_capture_session(self, *, package_path):
            calls.append(("capture_session", package_path))
            return {
                "ui_feedback": {"phase": "scanning", "overlay_state": "inactive"},
                "overlay_state": "inactive",
            }

    monkeypatch.setattr(mcp_server.project_core, "start_rpa_design", fake_start)
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: {"next_action": {"tool": "shizai_rpa_add_step"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: {"executed": True, "navigation": {"step_id": "open-target"}},
    )
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
            "open_browser": True,
        },
    )

    assert calls == [("bridge", calls[0][1]), ("capture_session", "CLIENT_APPLICATION_PATH")]
    assert result["capture_started"] is True
    assert result["picker_overlay_started"] is False
    assert result["capture"]["overlay_state"] == "inactive"
    assert result["next_action"]["tool"] == "shizai_rpa_capture_windows"


def test_mcp_rpa_start_design_returns_capture_warning_and_retry_after_capture_failure(monkeypatch):
    query, analysis, plan = _generic_browser_plan_fixture()
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )

    def fake_start(paths, output_dir=None, **kwargs):
        return {
            "kind": "shizai_rpa_started",
            "flow_ref": "CLIENT_APPLICATION_PATH",
            "project_ref": "CLIENT_APPLICATION_PATH",
        }

    class BrokenBridge:
        def __init__(self, desktop=None):
            pass

        def start_capture_session(self, *, package_path):
            raise ShizaiCloudError("DeepCapture fixture unavailable")

    monkeypatch.setattr(mcp_server.project_core, "start_rpa_design", fake_start)
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: {"next_action": {"tool": "shizai_rpa_add_step"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: {"executed": True, "navigation": {"step_id": "open-target"}},
    )
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", BrokenBridge)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
            "open_browser": True,
        },
    )

    assert result["capture_started"] is False
    assert result["capture_warning"] == "DeepCapture fixture unavailable"
    assert result["next_action"]["tools"] == [
        "shizai_rpa_capture_windows",
        "shizai_project_import_elements",
    ]
    assert result["next_action"]["optional_picker_tool"] == "shizai_extension_start_capture"


def test_mcp_rpa_takeover_countdown_starts_after_plan_design_and_before_browser_io(monkeypatch):
    query, analysis, plan = _generic_browser_plan_fixture()
    events = []
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda client, *, state, message, flow_ref=None: events.append(state) or {"state": state},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda client, **kwargs: events.append("takeover") or {"takeover_started": True},
    )
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_server.project_core,
        "start_rpa_design",
        lambda *args, **kwargs: (
            events.append("create") or {"flow_ref": "CLIENT_APPLICATION_PATH", "project_ref": "CLIENT_APPLICATION_PATH"}
        ),
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: events.append("persist_plan") or {"next_action": {"tool": "shizai_rpa_add_step"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: (
            events.append("navigation_step") or {"executed": True, "navigation": {"step_id": "open-target"}}
        ),
    )

    mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "open_browser": True,
            "allow_write": True,
        },
    )

    assert events.index("designing") < events.index("create")
    assert events.index("create") < events.index("persist_plan")
    assert events.index("persist_plan") < events.index("ready")
    assert events.index("ready") < events.index("takeover")
    assert events.index("takeover") < events.index("navigation_step")


def test_mcp_rpa_analyze_request_shows_designing_without_takeover(monkeypatch):
    query, _, _ = _generic_browser_plan_fixture()
    ui_calls = []
    takeover_calls = []
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda client, *, state, message, flow_ref=None: (
            ui_calls.append((state, message)) or {"state": state, "message": message}
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: takeover_calls.append(kwargs) or {},
    )

    result = mcp_server._dispatch("shizai_rpa_analyze_request", {"query": query})

    assert result["kind"] == "rpa_request_analysis"
    assert result["native_ui"]["state"] == "designing"
    assert ui_calls == [("designing", "塔斯正在识别任务意图并拆解流程步骤。")]
    assert takeover_calls == []


def test_mcp_rpa_analyze_request_cleans_stale_design_ui(monkeypatch):
    query, _, _ = _generic_browser_plan_fixture()
    cleanup_calls = []
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)
    monkeypatch.setattr(mcp_server, "_LATEST_RPA_DESIGN_REF", "OLD_FLOW")
    monkeypatch.setattr(
        mcp_server,
        "_rpa_cleanup_native_ui",
        lambda client=None: cleanup_calls.append(client) or {"small_card": {"visible": False}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda client, *, state, message, flow_ref=None: {"state": state, "message": message},
    )

    result = mcp_server._dispatch("shizai_rpa_analyze_request", {"query": query})

    assert len(cleanup_calls) == 1
    assert result["native_ui"]["stale_cleanup"]["small_card"]["visible"] is False
    assert mcp_server._RPA_SMALL_CARD_OPEN is True


def test_mcp_design_ui_update_skips_closed_local_desktop(monkeypatch):
    class FakeClient:
        class Config:
            ws_url = "ws://127.0.0.1:23408/?clientId=test"

        config = Config()

    def unavailable(*_args, **_kwargs):
        raise OSError("desktop is closed")

    monkeypatch.setattr(mcp_server.socket, "create_connection", unavailable)

    result = mcp_server._rpa_design_ui_update(
        FakeClient(),
        state="designing",
        message="planning",
    )

    assert result == {
        "state": "designing",
        "message": "planning",
        "desktop_unavailable": True,
    }


def test_mcp_ui_feedback_hidden_updates_small_card_state(monkeypatch):
    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            assert kwargs["state"] == "hidden"
            return {"visible": False, "state": "hidden"}

    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", True)

    result = mcp_server._dispatch(
        "shizai_ui_feedback",
        {"action": "small_card", "state": "hidden", "allow_write": True},
    )

    assert result["visible"] is False
    assert mcp_server._RPA_SMALL_CARD_OPEN is False


def test_mcp_rpa_start_design_without_browser_defers_takeover(monkeypatch):
    query, analysis, plan = _generic_browser_plan_fixture()
    takeover_calls = []
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: {item["component"]: {} for item in plan["steps"]},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda client, *, state, message, flow_ref=None: {"state": state, "message": message},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: takeover_calls.append(kwargs) or {"takeover_started": True},
    )
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_server.project_core,
        "start_rpa_design",
        lambda *args, **kwargs: {
            "flow_ref": "CLIENT_APPLICATION_PATH",
            "project_ref": "CLIENT_APPLICATION_PATH",
        },
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: {"next_action": {"tool": "shizai_rpa_add_step"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser I/O must be deferred")),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
        },
    )

    assert result["design_time_browser"] is None
    assert result["native_ui"]["plan_ready"]["state"] == "ready"
    assert takeover_calls == []


def test_mcp_native_rpa_design_writes_catalog_first_step_without_browser_takeover(monkeypatch):
    query, analysis, plan, catalog = _generic_native_file_plan_fixture()
    start_calls = []
    takeover_calls = []
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda paths: catalog,
    )
    monkeypatch.setattr(mcp_server.project_core, "analyze_rpa_request", lambda value: analysis)
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda client, *, state, message, flow_ref=None: {"state": state, "message": message},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: takeover_calls.append(kwargs) or {},
    )
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_server.project_core,
        "start_rpa_design",
        lambda *args, **kwargs: (
            start_calls.append(kwargs)
            or {"flow_ref": "CLIENT_APPLICATION_PATH", "project_ref": "CLIENT_APPLICATION_PATH"}
        ),
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *args, **kwargs: {"next_action": {"tool": "shizai_rpa_validate_flow"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *args, **kwargs: {"components": []})
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native design must not open Chrome")),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
            "open_browser": True,
        },
    )

    assert start_calls[0]["first_step"]["component"] == "File_Read"
    assert [item["component"] for item in start_calls[0]["component_plan"]] == ["File_Read"]
    assert result["design_time_browser"] is None
    assert takeover_calls == []


def test_validate_rpa_project_checks_native_first_component(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "rpa")
    project.import_elements(
        paths,
        "rpa",
        [
            {
                "id": "search",
                "name": "搜索框",
                "title": "首页",
                "url": "https://example.test/",
                "tag": "input",
                "css_selector": "#search",
                "attributes": {"aria-label": "搜索"},
            }
        ],
    )
    task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {"args0": [{"name": "statements", "value": ["open", "collect"]}]},
            "open": {"id": "open", "type": "WebBrowser_CreateV3", "label": "打开网页"},
            "collect": {"id": "collect", "type": "Keyboard_Send_V1", "label": "提交"},
        }
    }
    (target / "main.task").write_text(json.dumps(task), encoding="utf-8")
    result = project.validate_rpa_project(paths, "rpa")
    assert result["valid"] is True
    assert result["flow"]["browser_open_first"] is True

    task["componentModelEntities"]["MAIN_COMPONENT"]["args0"][0]["value"] = ["collect", "open"]
    (target / "main.task").write_text(json.dumps(task), encoding="utf-8")
    invalid = project.validate_rpa_project(paths, "rpa")
    assert invalid["valid"] is False
    assert any("WebBrowser_CreateV3" in error for error in invalid["errors"])


def test_cli_validate_elements_accepts_native_element_map(tmp_path):
    target = tmp_path / "elements.json"
    target.write_text(
        json.dumps(
            {
                "elementMap": {
                    "search": {
                        "id": "search",
                        "name": "搜索框",
                        "GUIType": "Chrome",
                        "selector": {
                            "commonProps": {"cssSelector": json.dumps("#search")},
                            "selector": [
                                {
                                    "name": "tab",
                                    "props": [{"name": "url", "textValue": "https://example.test/"}],
                                },
                                {
                                    "name": "input",
                                    "props": [{"name": "id", "textValue": "search"}],
                                },
                            ],
                        },
                    },
                },
                "groupMap": {},
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["--json", "project", "validate-elements", "--input", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["valid"] is True


def test_validate_rpa_project_rejects_coordinate_only_browser_actions(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "rpa")
    project.import_elements(
        paths,
        "rpa",
        [
            {
                "id": "search",
                "name": "搜索框",
                "title": "首页",
                "url": "https://example.test/",
                "tag": "input",
                "css_selector": "#search",
                "attributes": {"aria-label": "搜索"},
            }
        ],
    )
    task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {"args0": [{"name": "statements", "value": ["open", "click"]}]},
            "open": {"id": "open", "type": "WebBrowser_CreateV3", "label": "打开网页"},
            "click": {
                "id": "click",
                "type": "Mouse_ClickCombine_v4",
                "label": "点击",
                "args0": [
                    {"name": "uiSelector", "value": ""},
                    {"name": "location", "value": "[100, 200]"},
                ],
            },
        }
    }
    (target / "main.task").write_text(json.dumps(task), encoding="utf-8")
    result = project.validate_rpa_project(paths, "rpa")
    assert result["valid"] is False
    assert any("coordinate-only" in error for error in result["errors"])


def test_extension_selector_envelope_is_unwrapped_and_normalized():
    selector = {
        "commonProps": {"GUIType": "Windows", "processName": "chrome.exe"},
        "selector": [
            {"name": "Window", "props": [{"name": "title", "value": "京东 - Google Chrome"}]},
            {"name": "input", "type": "web", "props": [{"name": "aria-label", "textValue": "搜索"}]},
        ],
    }
    envelope = {"elementId": "uia:1", "selector": json.dumps(selector, ensure_ascii=False)}
    assert _native_selector(envelope) == selector
    record = _element_record(
        "uia:1",
        {"elementAlias": "edit_搜索", "meta": [{"key": "FrameworkId", "value": "Chrome"}]},
        envelope,
    )
    assert record["GUIType"] == "Chrome"
    assert record["page_title"] == "京东 - Google Chrome"
    assert record["selector"]["commonProps"]["GUIType"] == "Chrome"


def test_extension_uia_chrome_document_chain_is_normalized_to_chrome():
    selector = {
        "commonProps": {"GUIType": "Windows", "processName": "chrome.exe"},
        "selector": [
            {"name": "Window", "type": "32", "props": []},
            {"name": "Pane", "type": "32", "props": []},
            {"name": "Document", "type": "6", "props": []},
            {"name": "Edit", "type": "6", "props": [{"name": "class", "value": "search-input"}]},
        ],
    }
    record = _element_record(
        "uia:2",
        {"elementAlias": "edit_搜索", "meta": [{"key": "FrameworkId", "value": "Chrome"}]},
        selector,
        page_title="京东首页",
        page_url="https://www.jd.com/",
    )
    assert record["GUIType"] == "Chrome"
    assert record["selector"]["commonProps"]["GUIType"] == "Chrome"


def test_native_import_normalizes_static_uia_selector_values_for_runtime():
    selector = {
        "commonProps": {
            "GUIType": "Chrome",
            "processName": "chrome.exe",
            "queryMode": "default",
            "cssSelector": "",
        },
        "selector": [
            {
                "name": "Window",
                "type": "32",
                "props": [
                    {"name": "processname", "value": "chrome", "expressionValue": ""},
                    {"name": "title", "value": "京东 - Google Chrome", "expressionValue": ""},
                ],
            },
            {
                "name": "Edit",
                "type": "6",
                "props": [
                    {"name": "@index", "value": "2", "expressionValue": ""},
                    {"name": "class", "value": "search-input", "expressionValue": ""},
                ],
            },
        ],
    }
    native = project._native_element(
        {"name": "京东搜索框", "GUIType": "Chrome", "selector": selector},
        "search-input",
    )
    props = native["selector"]["selector"]
    assert props[0]["name"] == "tab"
    assert all(item.get("type") == "web" for item in props[1:])
    assert props[-1]["name"] == "input"
    # DeepCapture emits literal UIA values; native runtime selectors use the
    # normalized JSON expression form without an extra quoted string.
    assert next(item for item in props[-1]["props"] if item["name"] == "class")["value"] == "search-input"
    original = native["mcpNativeSelector"]["selector"]
    assert original[0]["props"][0]["value"] == '"chrome"'
    assert original[0]["props"][1]["value"] == '"京东 - Google Chrome"'
    assert original[1]["props"][0]["value"] == "2"
    assert original[1]["props"][1]["value"] == '"search-input"'
    assert original[0]["props"][0]["expressionValue"] == '"chrome"'


def test_native_selector_normalization_is_idempotent_for_designer_values():
    selector = {
        "selector": [
            {
                "name": "Window",
                "props": [
                    {"name": "processname", "value": '"chrome"', "expressionValue": '"chrome"'},
                    {"name": "@index", "value": "0", "expressionValue": "0"},
                ],
            }
        ],
    }
    assert project._normalize_native_selector_runtime_values(selector) == selector


def test_native_import_preserves_chrome_renderer_pane_context_properties():
    selector = {
        "commonProps": {"GUIType": "Chrome", "processName": "chrome.exe"},
        "selector": [
            {
                "name": "Pane",
                "type": "32",
                "props": [
                    {
                        "name": "class",
                        "value": '"Chrome_RenderWidgetHostHWND"',
                        "expressionValue": '"Chrome_RenderWidgetHostHWND"',
                    },
                    {"name": "@index", "value": "2", "expressionValue": "2"},
                    {"name": "automationid", "value": "16326", "expressionValue": "16326"},
                    {"name": "title", "value": '"Chrome Legacy Window"', "expressionValue": '"Chrome Legacy Window"'},
                ],
            },
            {"name": "Document", "type": "6", "props": []},
            {
                "name": "Edit",
                "type": "6",
                "props": [{"name": "class", "value": '"search-input"', "expressionValue": '"search-input"'}],
            },
        ],
    }
    native = project._native_element(
        {
            "name": "京东搜索框",
            "GUIType": "Chrome",
            "page_title": "京东 - Google Chrome",
            "page_url": "https://www.jd.com/",
            "selector": selector,
        },
        "search-input",
    )
    pane = next(item for item in native["mcpNativeSelector"]["selector"] if item["name"] == "Pane")
    names = [item["name"] for item in pane["props"]]
    assert "automationid" not in names
    assert "@index" in names
    assert names == ["class", "@index", "title"]
    assert native["selector"]["selector"][0]["name"] == "tab"
    assert all(item.get("type") == "web" for item in native["selector"]["selector"][1:])


def test_native_element_input_uses_browser_output_binding(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "browser-binding")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    record = {
        "id": "search",
        "name": "搜索框",
        "title": "京东",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
    }
    project.import_elements(paths, str(native), [record], replace=True)
    project.add_rpa_step(paths, str(native), "WebBrowser_CreateV3", step_id="open")
    project.add_rpa_step(paths, str(native), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    node = task["componentModelEntities"]["input"]
    args = {item["name"]: item["value"] for item in node["args0"]}
    assert args["designated_obj"] == "网页对象"
    assert args["uiSelector"] == 'SHIZAI_ELEMENT_DICT["search"]["selector"]'
    assert args["isEnter"] == "0"


def test_selector_property_parser_accepts_designer_serialized_property_text():
    raw = '@{name=class; value="jd_search"; pattern=equal; accurate=false; textValue=jd_search; expressionValue="jd_search"}'
    parsed = project._selector_property_dict(raw)
    assert parsed is not None
    assert parsed["name"] == "class"
    assert project._selector_property_value(parsed) == "jd_search"


def test_compact_web_selector_drops_transient_ancestor_indexes():
    element = {
        "GUIType": "Chrome",
        "page_title": "京东",
        "page_url": "https://www.jd.com/",
        "selector": {
            "commonProps": {"cssSelector": json.dumps("input.jd_pc_search_bar_react_search_input")},
            "selector": [
                {"name": "tab", "props": []},
                {"name": "div", "type": "web", "props": [{"name": "@index", "value": 4}]},
                {
                    "name": "input",
                    "type": "web",
                    "props": [
                        {"name": "class", "value": '"jd_pc_search_bar_react_search_input"'},
                        {"name": "@index", "value": 2},
                    ],
                },
            ],
        },
    }
    compact = project._native_web_selector(element, element["selector"])
    assert [item["name"] for item in compact["selector"]] == ["tab", "input"]
    assert compact["selector"][1]["props"][0]["name"] == "class"
    assert not any(prop["name"] == "@index" for item in compact["selector"] for prop in item["props"])


def test_native_selector_expression_only_wraps_uia_selector():
    web = {"mcpSelectorMode": "web", "selector": {"selector": [{"name": "input", "type": "web"}]}}
    uia = {"selector": {"selector": [{"name": "Edit", "type": "6"}]}}
    assert project._native_selector_expression("search", web) == 'SHIZAI_ELEMENT_DICT["search"]["selector"]'
    assert (
        project._native_selector_expression("search", uia)
        == 'elementsFormatNew(SHIZAI_ELEMENT_DICT["search"]["selector"])'
    )


def test_native_page_selector_always_uses_runtime_page_wrapper():
    assert project._native_page_selector_expression("product") == (
        'elementsFormatNew(SHIZAI_ELEMENT_DICT["product"]["selector"])'
    )


def test_import_elements_replace_is_guarded_after_element_binding(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "bound-flow"
    project.create_rpa_flow(str(staged))
    record = {
        "id": "search",
        "name": "搜索框",
        "title": "京东",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
    }
    project.import_elements(paths, str(staged), [record], replace=True)
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open")
    project.add_rpa_step(paths, str(staged), "Element_SetValue_V6", step_id="input", element_ids=["search"])

    with pytest.raises(ValueError, match="replace would clear elements already referenced"):
        project.import_elements(paths, str(staged), [record], replace=True)

    forced = project.import_elements(paths, str(staged), [record], replace=True, force_replace=True)
    assert forced["force_replace"] is True
    assert forced["imported"] == ["search"]


def test_materialize_repairs_incomplete_native_selector_values(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "native-repair")
    selector = {
        "commonProps": {"GUIType": "Chrome", "queryMode": "default", "cssSelector": ""},
        "selector": [
            {
                "name": "Window",
                "type": "32",
                "props": [{"name": "processname", "value": "chrome", "expressionValue": ""}],
            }
        ],
    }
    (target / "elements.json").write_text(
        json.dumps({"elementMap": {"search": {"id": "search", "name": "搜索", "selector": selector}}, "groupMap": {}}),
        encoding="utf-8",
    )
    changed = project._normalize_native_element_catalog(target)
    assert changed == ["search"]
    repaired = json.loads((target / "elements.json").read_text(encoding="utf-8"))
    prop = repaired["elementMap"]["search"]["selector"]["selector"][0]["props"][0]
    assert prop["value"] == '"chrome"'
    assert prop["expressionValue"] == '"chrome"'


def test_native_selector_css_maps_uia_edit_to_dom_input_and_ignores_browser_shell():
    selector = {
        "commonProps": {"cssSelector": ""},
        "selector": [
            {"name": "Window", "type": "32", "props": [{"name": "title", "value": '"京东"'}]},
            {"name": "Pane", "type": "32", "props": [{"name": "automationid", "value": "29227"}]},
            {"name": "Document", "type": "6", "props": []},
            {"name": "Group", "type": "6", "props": [{"name": "class", "value": '"search-box"'}]},
            {"name": "Edit", "type": "6", "props": [{"name": "class", "value": '"jd_search"'}]},
        ],
    }

    assert project._native_selector_css(selector) == "input.jd_search"
    normalized = project._ensure_native_selector_css(selector)
    assert json.loads(normalized["commonProps"]["cssSelector"]) == "input.jd_search"


def test_native_selector_css_preserves_existing_css_and_keeps_dynamic_shell_out():
    selector = {
        "commonProps": {"cssSelector": '"#stable-search"'},
        "selector": [
            {"name": "Pane", "type": "32", "props": [{"name": "automationid", "value": "99999"}]},
            {"name": "Edit", "type": "6", "props": [{"name": "class", "value": '"other"'}]},
        ],
    }

    assert project._native_selector_css(selector) == "#stable-search"
    normalized = project._ensure_native_selector_css(selector)
    assert json.loads(normalized["commonProps"]["cssSelector"]) == "#stable-search"
    assert normalized["selector"][0]["props"][0]["expressionValue"] == "99999"
    assert normalized["selector"][1]["props"][0]["expressionValue"] == '"other"'


def test_native_selector_css_repairs_persisted_uia_tag_in_existing_css():
    selector = {
        "commonProps": {"cssSelector": '"Edit.jd_search"'},
        "selector": [{"name": "Edit", "type": "6", "props": []}],
    }

    normalized = project._ensure_native_selector_css(selector)
    assert json.loads(normalized["commonProps"]["cssSelector"]) == "input.jd_search"


def test_native_selector_css_falls_back_to_stable_ancestor_for_text_node():
    selector = {
        "commonProps": {"cssSelector": ""},
        "selector": [
            {"name": "Document", "type": "6", "props": []},
            {"name": "Group", "type": "6", "props": [{"name": "id", "value": '"product"'}]},
            {"name": "Text", "type": "6", "props": [{"name": "@index", "value": "0"}]},
        ],
    }

    assert project._native_selector_css(selector) == "div#product span"


def test_native_selector_normalization_drops_chrome_renderer_automation_id():
    selector = {
        "commonProps": {"cssSelector": ""},
        "selector": [
            {
                "name": "Pane",
                "type": "32",
                "props": [
                    {"name": "title", "value": '"Chrome Legacy Window"'},
                    {"name": "class", "value": '"Chrome_RenderWidgetHostHWND"'},
                    {"name": "automationid", "value": "33733"},
                ],
            }
        ],
    }

    normalized = project._normalize_native_selector_runtime_values(selector)
    names = [item["name"] for item in normalized["selector"][0]["props"]]
    assert "automationid" not in names
    assert normalized["selector"][0]["props"][0]["expressionValue"] == '"Chrome Legacy Window"'


def test_find_chrome_executable_uses_candidates_without_path(monkeypatch, tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(extension_bridge, "_chrome_executable_candidates", lambda: [executable])

    assert _find_chrome_executable() == executable


def test_open_current_chrome_reports_resolved_executable(monkeypatch, tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_text("fixture", encoding="utf-8")
    calls = []
    monkeypatch.setattr(extension_bridge, "_find_chrome_executable", lambda: executable)

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Process", (), {"pid": 321})()

    monkeypatch.setattr(extension_bridge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(extension_bridge.time, "sleep", lambda _seconds: None)

    result = extension_bridge.open_current_chrome("https://example.test/", wait_seconds=0)

    assert result["opened"] is True
    assert result["pid"] == 321
    assert result["executable"] == str(executable)
    assert calls[0][0][0] == [str(executable), "https://example.test/"]


def test_browser_launch_is_detached_from_mcp_process_on_windows(monkeypatch):
    monkeypatch.setattr(extension_bridge.os, "name", "nt")

    kwargs = extension_bridge._detached_browser_popen_kwargs()

    assert kwargs["creationflags"] & extension_bridge.subprocess.CREATE_NEW_PROCESS_GROUP
    assert kwargs["creationflags"] & extension_bridge.subprocess.DETACHED_PROCESS
    assert "start_new_session" not in kwargs


class _FakeCaptureDesktop:
    def __init__(self, children=None, selector=None):
        self.calls = []
        self.children = children if children is not None else []
        self.selector = selector

    def authentication_status(self):
        return {"ready": True, "desktop": {"reachable": True, "authenticated": True}}

    def call(self, service, method, args=None):
        self.calls.append((service, method, args or []))
        if method == "getDeepCaptureElementChildrenById":
            return {
                "isSuccess": True,
                "data": {
                    "elementId": args[0],
                    "descendants": self.children,
                    "code": 0,
                },
            }
        if method == "getDeepCaptureElementSelector":
            return {"isSuccess": True, "data": self.selector or {}}
        return {"isSuccess": True, "data": {"code": 0}}


def _fake_page_capture(title: str, *, url: str = "https://www.jd.com/") -> _FakeCaptureDesktop:
    class PageCaptureDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            if method == "getDeepCaptureElementChildrenById" and args and str(args[0]) not in {"", "wnd:1"}:
                self.calls.append((service, method, args or []))
                return {
                    "isSuccess": True,
                    "data": {"elementId": args[0], "descendants": [], "code": 0},
                }
            return super().call(service, method, args)

    return PageCaptureDesktop(
        children=[
            {
                "elementId": "uia:document",
                "name": "Document",
                "type": "6",
            },
            {"elementId": "uia:text", "name": title, "tag": "文本", "type": "6"},
        ],
        selector={
            "commonProps": {"GUIType": "Windows", "processName": "chrome.exe"},
            "selector": [
                {
                    "name": "Window",
                    "props": [
                        {"name": "title", "value": title},
                        {"name": "url", "value": url},
                    ],
                }
            ],
        },
    )


def test_extension_runtime_preflight_accepts_matching_current_page(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东 - Google Chrome")])
    result = ShizaiExtensionBridge(desktop=_fake_page_capture("京东 - Google Chrome")).runtime_preflight(
        expected_page_title="京东 - Google Chrome",
        expected_page_url="https://www.jd.com/",
        step_id="search-input",
        component="Element_SetValue_V6",
    )

    assert result["valid"] is True
    assert result["reason"] == "ready"
    assert result["document_found"] is True
    assert result["identity"] == {"title_match": True, "url_match": True, "changed": False}
    assert result["chrome_session"]["reused"] is True


def test_extension_runtime_preflight_accepts_search_title_drift_when_url_matches(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "手机 - 商品搜索 - 京东 - Google Chrome")])
    result = ShizaiExtensionBridge(
        desktop=_fake_page_capture(
            "手机 - 商品搜索 - 京东 - Google Chrome",
            url="https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA&enc=utf-8",
        )
    ).runtime_preflight(
        expected_page_title="京东商品搜索",
        expected_page_url="https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA&enc=utf-8",
        step_id="collect-products",
        component="WebBrowser_DataCollectionV5",
    )

    assert result["valid"] is True
    assert result["reason"] == "ready"
    assert result["identity"] == {"title_match": True, "url_match": True, "changed": False}


def test_extension_runtime_preflight_prefers_expected_page_over_chrome_restore_window(monkeypatch):
    bridge = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())
    monkeypatch.setattr(
        bridge,
        "capture_windows",
        lambda **_kwargs: {
            "windows": [
                {"element_id": "wnd:restore", "page_title": "要恢复页面吗？", "page_url": ""},
                {
                    "element_id": "wnd:jd",
                    "page_title": "京东(JD.COM)-正品低价 - Google Chrome",
                    "page_url": "",
                },
            ]
        },
    )
    monkeypatch.setattr(
        bridge,
        "capture_tree",
        lambda *_args, **_kwargs: {"descendants": [], "capture_ready": False},
    )

    result = bridge.runtime_preflight(
        expected_page_title="京东",
        expected_page_url="https://www.jd.com/",
    )

    assert result["reason"] == "document_missing"
    assert result["current_page_title"].startswith("京东")
    assert result["identity"]["changed"] is False


def test_extension_runtime_preflight_retries_deeper_tree_for_nested_document(monkeypatch):
    bridge = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())
    monkeypatch.setattr(
        bridge,
        "capture_windows",
        lambda **_kwargs: {
            "windows": [
                {
                    "element_id": "wnd:1",
                    "page_title": "京东 - Google Chrome",
                    "page_url": "https://www.jd.com/",
                }
            ],
        },
    )
    levels = []

    def capture_tree(_element_id, *, level, max_nodes):
        levels.append((level, max_nodes))
        if level <= extension_bridge.DEFAULT_CAPTURE_LEVEL:
            descendants = [{"elementId": "uia:pane", "name": "Pane", "type": "32"}]
        else:
            descendants = [
                {
                    "elementId": "uia:toolbar",
                    "name": "Toolbar",
                    "type": "32",
                    "children": [
                        {
                            "elementId": "uia:document",
                            "name": "Document",
                            "type": "6",
                            "children": [{"elementId": "uia:input", "name": "搜索", "tag": "编辑", "type": "6"}],
                        }
                    ],
                }
            ]
        return {"descendants": descendants, "capture_ready": bool(level > extension_bridge.DEFAULT_CAPTURE_LEVEL)}

    monkeypatch.setattr(bridge, "capture_tree", capture_tree)
    result = bridge.runtime_preflight(
        expected_page_title="京东 - Google Chrome",
        expected_page_url="https://www.jd.com/",
        step_id="search-input",
        component="Element_SetValue_V6",
    )

    assert result["valid"] is True
    assert result["reason"] == "ready"
    assert result["document_found"] is True
    assert result["windows"][0]["capture_level"] == 12
    assert levels == [(extension_bridge.DEFAULT_CAPTURE_LEVEL, 160), (12, 320)]


def test_extension_runtime_preflight_identifies_challenge_page(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东验证 - Google Chrome")])
    result = ShizaiExtensionBridge(desktop=_fake_page_capture("验证一下，购物无忧")).runtime_preflight(
        expected_page_title="京东首页 - Google Chrome",
        expected_page_url="https://www.jd.com/",
        step_id="search-input",
        component="Element_SetValue_V6",
    )

    assert result["valid"] is False
    assert result["reason"] == "page_challenge"
    assert result["challenge_detected"] is True
    assert "当前页面进入验证页" in result["message"]
    assert result["status"] == "waiting_for_user"
    assert result["interaction_required"] is True
    assert result["interaction"]["pause_automation"] is True
    assert result["next_action"]["action"] == "pause_and_ask_user"
    assert result["next_action"]["resume"]["tool"] == "shizai_rpa_runtime_preflight"


def test_extension_runtime_preflight_pauses_and_asks_user_to_login(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东登录 - Google Chrome")])
    result = ShizaiExtensionBridge(desktop=_fake_page_capture("京东账号登录")).runtime_preflight(
        expected_page_title="京东首页 - Google Chrome",
        expected_page_url="https://www.jd.com/",
        step_id="search-input",
        component="Element_SetValue_V6",
    )

    assert result["valid"] is False
    assert result["reason"] == "login_required"
    assert result["status"] == "waiting_for_user"
    assert result["interaction"]["action"] == "login"
    assert result["interaction"]["pause_automation"] is True
    assert result["interaction"]["automation_may_login"] is False
    assert result["interaction"]["client_login_policy"]["may_delegate_login_only_if_all"] == [
        "user_explicitly_requests_delegated_login",
        "required_credentials_are_provided_in_current_conversation",
    ]
    assert "mcp_arguments" in result["interaction"]["client_login_policy"]["forbidden_destinations"]
    assert "完成登录" in result["interaction"]["user_prompt"]
    assert result["next_action"] == {
        "kind": "user_interaction",
        "action": "pause_and_ask_user",
        "message": result["interaction"]["user_prompt"],
        "resume": result["interaction"]["resume"],
    }


def test_extension_runtime_preflight_rejects_changed_page_and_missing_document(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东商品搜索 - Google Chrome")])
    changed = ShizaiExtensionBridge(desktop=_fake_page_capture("京东商品搜索 - Google Chrome")).runtime_preflight(
        expected_page_title="京东首页 - Google Chrome",
        expected_page_url="https://www.jd.com/",
    )
    assert changed["valid"] is False
    assert changed["reason"] == "page_identity_changed"
    assert changed["identity"]["changed"] is True
    assert changed["recovery"]["preserve"] == ["当前 Chrome 会话", "当前已创建流程"]
    assert changed["recovery"]["steps"] == [
        "shizai_rpa_scan_page",
        "shizai_rpa_capture_elements",
        "shizai_project_import_elements",
        "重新绑定或替换结果页元素",
        "shizai_rpa_debug_command(action=next)",
    ]
    assert changed["recovery"]["optional_picker_tool"] == "shizai_extension_start_capture"
    assert "不要重新创建流程" in changed["recovery"]["rule"]

    class ShellCaptureDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            if method == "getDeepCaptureElementChildrenById" and args and str(args[0]) not in {"", "wnd:1"}:
                self.calls.append((service, method, args or []))
                return {"isSuccess": True, "data": {"elementId": args[0], "descendants": [], "code": 0}}
            return super().call(service, method, args)

    shell = ShellCaptureDesktop(
        children=[
            {
                "elementId": "uia:pane",
                "name": "Pane",
                "type": "32",
                "children": [],
            }
        ]
    )
    missing = ShizaiExtensionBridge(desktop=shell).runtime_preflight()
    assert missing["valid"] is False
    assert missing["reason"] == "document_missing"
    assert missing["document_found"] is False


def test_extension_scan_page_inherits_url_from_current_window(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东 - Google Chrome")])
    result = ShizaiExtensionBridge(
        desktop=_fake_page_capture("京东 - Google Chrome", url="https://www.jd.com/")
    ).scan_page(max_nodes=20)
    assert result["page_url"] == "https://www.jd.com/"
    assert result["chrome_session"]["page_url"] == "https://www.jd.com/"


def test_extension_scan_page_retries_deeper_tree_for_nested_document(monkeypatch):
    bridge = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())
    calls = []

    def capture_tree(element_id, *, level, max_nodes):
        calls.append((element_id, level, max_nodes))
        if element_id == "uia:document":
            return {
                "descendants": [{"elementId": "uia:search", "name": "搜索", "tag": "编辑", "type": "6"}],
                "count": 1,
            }
        if level < 12 or max_nodes < 500:
            return {
                "descendants": [{"elementId": "uia:toolbar", "name": "Toolbar", "type": "32"}],
                "count": max_nodes,
            }
        if element_id == "wnd:1":
            return {
                "descendants": [{"elementId": "uia:document", "name": "京东", "tag": "文档", "type": "6"}],
                "count": max_nodes,
            }
        return {"descendants": [], "count": 0}

    monkeypatch.setattr(bridge, "capture_tree", capture_tree)
    monkeypatch.setattr(
        bridge.desktop,
        "call",
        lambda _service, method, args=None: (
            {
                "isSuccess": True,
                "data": {
                    "elementId": (args or [""])[0],
                    "meta": [{"key": "Name", "value": "京东 - Google Chrome"}],
                    "code": 0,
                },
            }
            if method == "getDeepCaptureElementDetail"
            else {"isSuccess": True, "data": {"code": 0}}
        ),
    )

    result = bridge.scan_page(
        root_element_id="wnd:1",
        query="当前页面的搜索输入框",
        level=6,
        max_nodes=250,
    )

    assert result["document_ids"] == ["uia:document"]
    assert result["candidates"][0]["element_id"] == "uia:search"
    assert result["inspected_roots"][0]["deepened"] is True
    assert result["inspected_roots"][0]["capture_level"] == 12
    assert result["inspected_roots"][0]["capture_max_nodes"] == 500
    assert calls == [
        ("wnd:1", 6, 250),
        ("wnd:1", 12, 500),
        ("uia:document", 6, 250),
    ]


def test_extension_scan_page_targeted_search_stops_before_full_document_scan(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东 - Google Chrome")])

    class TargetedDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            self.calls.append((service, method, args or []))
            if method == "getDeepCaptureElementChildrenById":
                parent_id = str((args or [""])[0])
                descendants = {
                    "": [{"elementId": "wnd:1", "name": "Chrome", "type": "32"}],
                    "wnd:1": [{"elementId": "uia:document", "name": "Document", "tag": "文档", "type": "6"}],
                    "uia:document": [
                        {
                            "elementId": "uia:search",
                            "name": "搜索",
                            "placeholder": "搜索商品",
                            "tag": "编辑",
                            "type": "6",
                        }
                    ],
                }.get(parent_id, [])
                return {"isSuccess": True, "data": {"elementId": parent_id, "descendants": descendants, "code": 0}}
            return {"isSuccess": True, "data": {"code": 0}}

    bridge = ShizaiExtensionBridge(desktop=TargetedDesktop())
    monkeypatch.setattr(bridge, "capture_tree", lambda **_: pytest.fail("targeted hit must not scan the full tree"))

    result = bridge.scan_page(query="搜索框", strategy="auto", max_nodes=20)

    assert result["scan_strategy"] == "targeted"
    assert result["targeted_fallback"] is False
    assert result["targeted_nodes_visited"] > 0
    assert result["targeted_rpc_calls"] > 0
    assert result["candidates"][0]["element_id"] == "uia:search"


def test_extension_scan_page_targeted_search_falls_back_to_full_scan(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [(1, "京东 - Google Chrome")])

    class FallbackDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            self.calls.append((service, method, args or []))
            if method == "getDeepCaptureElementChildrenById":
                parent_id = str((args or [""])[0])
                descendants = {
                    "": [{"elementId": "wnd:1", "name": "Chrome", "type": "32"}],
                    "wnd:1": [{"elementId": "uia:document", "name": "Document", "tag": "文档", "type": "6"}],
                    "uia:document": [{"elementId": "uia:body", "name": "商品列表", "tag": "div", "type": "6"}],
                }.get(parent_id, [])
                return {"isSuccess": True, "data": {"elementId": parent_id, "descendants": descendants, "code": 0}}
            return {"isSuccess": True, "data": {"code": 0}}

    bridge = ShizaiExtensionBridge(desktop=FallbackDesktop())
    full_scans = []

    def capture_tree(element_id, *, level, max_nodes):
        full_scans.append((element_id, level, max_nodes))
        if element_id == "wnd:1":
            return {"descendants": [{"elementId": "uia:document", "name": "Document", "tag": "文档", "type": "6"}], "count": 1}
        return {
            "descendants": [{"elementId": "uia:body", "name": "商品列表", "tag": "div", "type": "6"}],
            "count": 1,
        }

    monkeypatch.setattr(bridge, "capture_tree", capture_tree)

    result = bridge.scan_page(query="搜索框", strategy="auto", max_nodes=20)

    assert result["scan_strategy"] == "full"
    assert result["targeted_fallback"] is True
    assert full_scans


def test_extension_start_capture_uses_designer_inspect_protocol():
    desktop = _FakeCaptureDesktop()
    result = ShizaiExtensionBridge(desktop=desktop).start_capture(package_path="C:/tmp/native-flow")
    assert result["mode_value"] == 2
    assert result["ui_feedback"]["phase"] == "capturing"
    assert result["ui_feedback"]["overlay"] == "native_shizai_extension"
    assert result["ui_feedback"]["overlay_state"] == "active"
    assert result["ui_feedback"]["animation"] == "native_extension_picker"
    assert [(item[1], item[2]) for item in desktop.calls[:2]] == [
        ("startDeepCapture", [{"packagePath": "C:\\tmp\\native-flow"}]),
        ("startDeepCaptureInspect", [2]),
    ]
    assert result["rpc_attempts"] == {
        "startDeepCapture": 1,
        "startDeepCaptureInspect": 1,
    }
    assert result["recovered_from_transient_error"] is False
    assert result["control_mode"] == "official_picker"
    assert result["browser_control"] == "deep_capture_official_picker"
    assert result["runtime_execution"] is False
    assert result["picker_overlay_started"] is True


def test_extension_stop_inspect_clears_persisted_picker_state(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    project_dir = tmp_path / "flow"
    bridge = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())

    bridge.start_capture(package_path=project_dir)
    stopped = bridge.stop_inspect()
    status = bridge.capture_session_status(package_path=project_dir)

    assert stopped["overlay_state"] == "inactive"
    assert status["active"] is True
    assert status["inspect_started"] is False
    assert status["control_mode"] == "capture_read"


def test_extension_runtime_capture_session_does_not_start_inspect_overlay():
    desktop = _FakeCaptureDesktop()
    result = ShizaiExtensionBridge(desktop=desktop).start_capture_session(package_path="C:/tmp/native-flow")
    assert desktop.calls[0][1:] == (
        "startDeepCapture",
        [{"packagePath": "C:\\tmp\\native-flow"}],
    )
    assert len(desktop.calls) == 1
    assert result["inspect_started"] is False
    assert result["overlay_state"] == "inactive"
    assert result["control_mode"] == "capture_read"
    assert result["browser_control"] == "deep_capture_read"
    assert result["picker_overlay_started"] is False


def test_extension_ensure_capture_session_is_idempotent_for_one_project(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    desktop = _FakeCaptureDesktop()
    bridge = ShizaiExtensionBridge(desktop=desktop)
    first = bridge.ensure_capture_session(package_path=tmp_path / "flow")
    second = bridge.ensure_capture_session(package_path=tmp_path / "flow")
    assert first["initialized"] is True
    assert first["reused"] is False
    assert second["reused"] is True
    assert [item[1] for item in desktop.calls] == ["startDeepCapture"]


def test_extension_capture_session_state_is_shared_and_stops_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    project_dir = tmp_path / "flow"
    first = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())
    started = first.start_capture_session(package_path=project_dir)

    # Simulate a fresh short-lived CLI process: only the durable handshake is
    # available, while the native call history belongs to the first process.
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    second = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop())
    status = second.capture_session_status(package_path=project_dir)
    reused = second.ensure_capture_session(package_path=project_dir)

    assert status["active"] is True
    assert status["control_mode"] == "capture_read"
    assert status["truth_source"] == "last_known_local_state"
    assert reused["reused"] is True
    assert reused["reused_from_persisted_state"] is True
    assert reused["session_id"] == started["session_id"]
    stopped = second.stop_capture(package_path=project_dir)
    assert stopped["control_mode"] == "none"
    assert second.capture_session_status(package_path=project_dir)["active"] is False


def test_extension_ensure_capture_session_probes_stale_persisted_state(monkeypatch, tmp_path):
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_PACKAGE", None)
    monkeypatch.setattr(extension_bridge, "_CAPTURE_SESSION_STARTED_AT", 0.0)
    project_dir = tmp_path / "stale-flow"
    extension_bridge._write_capture_session_state(
        project_dir,
        {
            "session_id": "dc_stale",
            "project_ref": str(project_dir.resolve()),
            "package_path": str(project_dir.resolve()),
            "started_at": time.time(),
            "native_session_state": "active_last_known",
        },
    )

    class StaleDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            if method == "getDeepCaptureElementChildrenById":
                self.calls.append((service, method, args or []))
                raise ShizaiCloudError("DeepCaptureService timeout")
            return super().call(service, method, args)

    desktop = StaleDesktop()
    result = ShizaiExtensionBridge(desktop=desktop).ensure_capture_session(package_path=project_dir)

    assert result["reconnected_from_persisted_state"] is True
    assert result["reused"] is False
    assert [item[1] for item in desktop.calls] == ["getDeepCaptureElementChildrenById", "startDeepCapture"]


def test_extension_capture_tree_fails_fast_with_channel_reason():
    class UnavailableDesktop(_FakeCaptureDesktop):
        def call(self, service, method, args=None):
            self.calls.append((service, method, args or []))
            raise ShizaiCloudError("DeepCaptureService timeout")

    bridge = ShizaiExtensionBridge(
        desktop=UnavailableDesktop(),
        capture_rpc_timeout=0.1,
        capture_operation_timeout=0.5,
    )
    started = time.perf_counter()
    with pytest.raises(ShizaiCloudError, match="reason=channel_unavailable"):
        bridge.capture_tree("wnd:1")

    assert time.perf_counter() - started < 2


def test_cli_exposes_project_capture_session_lifecycle(monkeypatch):
    calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def ensure_capture_session(self, *, package_path):
            calls.append(("start", package_path))
            return {"project_ref": package_path, "session_id": "dc_test", "control_mode": "capture_read"}

        def capture_session_status(self, *, package_path):
            calls.append(("status", package_path))
            return {"project_ref": package_path, "active": True, "control_mode": "capture_read"}

        def stop_capture(self, *, package_path):
            calls.append(("stop", package_path))
            return {"project_ref": package_path, "active": False, "control_mode": "none"}

    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", FakeBridge)
    runner = CliRunner()

    started = runner.invoke(cli, ["--json", "workflow", "rpa-browser-session-start", "FLOW", "--allow-write"])
    status = runner.invoke(cli, ["--json", "workflow", "rpa-browser-session-status", "FLOW"])
    stopped = runner.invoke(cli, ["--json", "workflow", "rpa-browser-session-stop", "FLOW", "--allow-write"])

    assert started.exit_code == 0, started.output
    assert status.exit_code == 0, status.output
    assert stopped.exit_code == 0, stopped.output
    assert [item[0] for item in calls] == ["start", "status", "stop"]
    assert json.loads(started.output)["next_action"] == "workflow rpa-capture-windows"
    assert json.loads(stopped.output)["control_mode"] == "none"


def test_component_config_adapter_accepts_plan_paging_and_output_aliases():
    adapted = project._adapt_rpa_component_config(
        "WebBrowser_DataCollectionV5",
        {
            "output_columns": ["商品名称", "价格"],
            "max_rows": 100,
            "pagination": {"page_type": 1, "next_page_selector": "jd-next"},
        },
        {"output_file": "result.xlsx"},
    )
    assert adapted["columns"] == ["商品名称", "价格"]
    assert adapted["collect_items"] == 100
    assert adapted["selector_page"] == "jd-next"
    assert adapted["page_type"] == 1


def test_count_limited_collection_infers_pagination_mode_from_live_selector():
    paging = project._collection_paging_settings(
        {
            "region": 3,
            "collect_items": 100,
            "page_type": 1,
            "page_type_num": 0,
            "selector_page": "elementsFormatNew(SHIZAI_ELEMENT_DICT['jd-next-page']['selector'])",
        }
    )

    assert paging["page_type_num"] == "1"
    assert paging["selector_page"].startswith("elementsFormatNew(")


def test_collection_pagination_is_advisory_until_the_model_selects_a_mode():
    capability = project._collection_pagination_capability(
        {
            "region": 3,
            "collect_items": 100,
            "page_type_num": 0,
            "selector_page": "",
            "selector_more": "",
        }
    )

    assert capability["available"] is True
    assert capability["decision"] == "model_decides"
    assert capability["selected_mode"] == "unspecified"
    assert capability["selector_bound"] is False
    assert "next-page" in capability["supported_modes"]
    assert "load-more" in capability["supported_modes"]


def test_selected_native_pagination_reports_missing_matching_selector():
    capability = project._collection_pagination_capability(
        {
            "region": 3,
            "collect_items": 20,
            "page_type_num": 1,
            "selector_page": "",
            "selector_more": "",
        }
    )

    assert capability["selected_mode"] == "next-page"
    assert capability["selector_argument"] == "selector_page"
    assert capability["selector_bound"] is False


def test_collection_paging_reads_project_metadata_aliases():
    paging = project._collection_paging_settings(
        {
            "mcpCollectionRegion": "3",
            "mcpMaxRows": 100,
            "mcpCollectionPageType": "1",
            "mcpCollectionPageTypeNum": "1",
        }
    )

    assert paging["region"] == "3"
    assert paging["collect_items"] == "100"
    assert paging["page_type"] == "1"
    assert paging["page_type_num"] == "1"


def test_runtime_extension_status_prefers_enabled_mv3_and_live_native_host(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
    new_id = "pcopnibgkbdnlaeagepigbboebdfejmb"
    old_id = "nfiffhdmjkohbbolbamdboplicimjnpm"
    for extension_id, version, manifest_version in (
        (new_id, "4.0.1.235_0", 3),
        (old_id, "2.7_0", 2),
    ):
        manifest_dir = profile / "Extensions" / extension_id / version
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": extension_id,
                    "version": version.removesuffix("_0"),
                    "manifest_version": manifest_version,
                    "action": {"default_popup": "pages/popup.html"},
                }
            ),
            encoding="utf-8",
        )
    (profile / "Secure Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        new_id: {},
                        old_id: {"disable_reasons": [8388608]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    native_root = tmp_path / "IIRPA" / "RPAChromeExtension"
    native_root.mkdir(parents=True)
    (native_root / "processes.json").write_text(json.dumps([{"Pid": 321}]), encoding="utf-8-sig")
    monkeypatch.setattr(extension_bridge, "_pid_is_running", lambda pid: pid == 321)

    result = extension_bridge._runtime_extension_status()

    assert result["ready"] is True
    assert result["preferred_extension"]["id"] == new_id
    assert result["native_host"]["connected"] is True
    assert result["native_host"]["process_source"] == "state_file"
    old = next(item for item in result["extensions"] if item["id"] == old_id)
    assert old["enabled"] is False
    assert old["disable_reasons"] == [8388608]


def test_runtime_extension_status_explains_installed_but_disabled_mv3(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    extension_id = "pcopnibgkbdnlaeagepigbboebdfejmb"
    profile = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
    manifest_dir = profile / "Extensions" / extension_id / "4.0.1.235_0"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"name": "Shizai Agent", "version": "4.0.1.235", "manifest_version": 3}),
        encoding="utf-8",
    )
    (profile / "Secure Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {extension_id: {"disable_reasons": [8192]}},
                }
            }
        ),
        encoding="utf-8",
    )

    result = extension_bridge._runtime_extension_status()

    assert result["state"] == "installed_disabled"
    assert result["interaction_required"] is True
    assert result["preferred_extension"] is None
    assert result["disabled_extensions"][0]["disable_reason_labels"] == ["sideload_wipeout"]


def test_runtime_extension_status_falls_back_to_process_table(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = tmp_path / "Google" / "Chrome" / "User Data" / "Default"
    extension_id = "pcopnibgkbdnlaeagepigbboebdfejmb"
    manifest_dir = profile / "Extensions" / extension_id / "4.0.1.235_0"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Shizai Agent",
                "version": "4.0.1.235",
                "manifest_version": 3,
                "action": {"default_popup": "pages/popup.html"},
            }
        ),
        encoding="utf-8",
    )
    (profile / "Secure Preferences").write_text(
        json.dumps({"extensions": {"settings": {extension_id: {}}}}),
        encoding="utf-8",
    )
    native_root = tmp_path / "IIRPA" / "RPAChromeExtension"
    native_root.mkdir(parents=True)
    (native_root / "processes.json").write_text("incomplete", encoding="utf-8")
    discovered = [{"Pid": 654, "ProcessName": "II.RPA.NativeMessagingHost", "source": "process_table"}]
    monkeypatch.setattr(extension_bridge, "_discover_native_host_processes", lambda: discovered)

    result = extension_bridge._runtime_extension_status()

    assert result["ready"] is True
    assert result["native_host"]["connected"] is True
    assert result["native_host"]["live_processes"] == discovered
    assert result["native_host"]["process_source"] == "process_table"


def test_activate_runtime_extension_opens_official_popup_until_native_host_is_ready(monkeypatch, tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_text("fixture", encoding="utf-8")
    extension = {
        "id": "pcopnibgkbdnlaeagepigbboebdfejmb",
        "popup": "pages/popup.html",
    }
    statuses = iter(
        [
            {"ready": False, "preferred_extension": extension},
            {"ready": True, "preferred_extension": extension},
        ]
    )
    calls = []
    monkeypatch.setattr(extension_bridge, "_runtime_extension_status", lambda: next(statuses))
    monkeypatch.setattr(extension_bridge, "_find_chrome_executable", lambda: executable)
    monkeypatch.setattr(extension_bridge.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(extension_bridge.time, "sleep", lambda _seconds: None)

    result = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop()).activate_runtime_extension()

    assert result["attempted"] is True
    assert result["ready"] is True
    assert calls[0][0][0] == [
        str(executable),
        "chrome-extension://pcopnibgkbdnlaeagepigbboebdfejmb/pages/popup.html",
    ]


def test_extension_start_capture_retries_transient_native_failure(monkeypatch):
    class FlakyCaptureDesktop(_FakeCaptureDesktop):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def call(self, service, method, args=None):
            if method == "startDeepCapture" and self.failures:
                self.failures -= 1
                raise extension_bridge.ShizaiCloudError("请求失败，请重试。")
            return super().call(service, method, args)

    monkeypatch.setattr(extension_bridge.time, "sleep", lambda _seconds: None)
    result = ShizaiExtensionBridge(desktop=FlakyCaptureDesktop()).start_capture(package_path="C:/tmp/native-flow")

    assert result["rpc_attempts"]["startDeepCapture"] == 2
    assert result["recovered_from_transient_error"] is True


def test_extension_stop_capture_closes_deep_capture_session():
    desktop = _FakeCaptureDesktop()
    result = ShizaiExtensionBridge(desktop=desktop).stop_capture(package_path="C:/tmp/native-flow")
    assert desktop.calls[0][1:] == ("stopDeepCaptureInspect", [])
    assert desktop.calls[1][1:] == ("stopDeepCapture", [{"packagePath": "C:\\tmp\\native-flow"}])
    assert result["inspect_result"]["isSuccess"] is True
    assert result["ui_feedback"]["phase"] == "idle"
    assert result["ui_feedback"]["overlay_state"] == "inactive"


def test_cli_extension_capture_validates_unbound_component_before_picker(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def start_capture(self, *, mode, package_path):
            events.append(("picker_start", mode, package_path))
            return {"overlay_state": "active"}

        def stop_inspect(self):
            events.append(("inspect_stop",))
            return {"overlay_state": "inactive"}

    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    monkeypatch.setattr(cli_module, "_paths", lambda _ctx: object())
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", FakeBridge)

    def validate(*_args):
        events.append(("component_validate",))
        raise ValueError("element component must be added before capture: missing-step")

    monkeypatch.setattr(cli_module.project_core, "unbound_rpa_element_step", validate)
    result = CliRunner().invoke(
        cli,
        [
            "extension",
            "start-capture",
            "--project-ref",
            "FLOW_REF",
            "--step-id",
            "missing-step",
            "--allow-write",
        ],
    )

    assert result.exit_code != 0
    assert "must be added before capture" in result.output
    assert events == [("component_validate",), ("inspect_stop",)]


def test_cli_extension_capture_starts_picker_only_after_component_validation(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def start_capture(self, *, mode, package_path):
            events.append(("picker_start", mode, package_path))
            return {"overlay_state": "active"}

        def stop_inspect(self):
            events.append(("inspect_stop",))
            return {"overlay_state": "inactive"}

    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    monkeypatch.setattr(cli_module, "_paths", lambda _ctx: object())
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(
        cli_module.project_core,
        "unbound_rpa_element_step",
        lambda *_args: (
            events.append(("component_validate",))
            or {
                "step_id": "search-input",
                "component": "Element_SetValue_V6",
                "element_role": "搜索框",
                "binding_status": "unbound",
            }
        ),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "extension",
            "start-capture",
            "--project-ref",
            "FLOW_REF",
            "--step-id",
            "search-input",
            "--allow-write",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == [("component_validate",), ("picker_start", 2, "FLOW_REF")]
    assert json.loads(result.output)["target_step"]["binding_status"] == "unbound"


def test_extension_stop_inspect_keeps_capture_session_alive():
    desktop = _FakeCaptureDesktop()
    result = ShizaiExtensionBridge(desktop=desktop).stop_inspect()
    assert desktop.calls == [("DeepCaptureService", "stopDeepCaptureInspect", [])]
    assert result["overlay_state"] == "inactive"


def test_extension_capture_requires_native_package_context():
    desktop = _FakeCaptureDesktop()
    with pytest.raises(ValueError, match="project_ref"):
        ShizaiExtensionBridge(desktop=desktop).start_capture()


def test_extension_tree_marks_chrome_shell_as_not_capture_ready():
    desktop = _FakeCaptureDesktop(
        children=[
            {
                "elementId": "uia:tab",
                "name": "京东 - Google Chrome",
                "tag": "区域",
                "children": [],
            }
        ]
    )
    result = ShizaiExtensionBridge(desktop=desktop).capture_tree("wnd:1")
    assert result["capture_ready"] is False
    assert result["web_element_count"] == 0
    assert result["ui_feedback"]["phase"] == "waiting"
    assert result["ui_feedback"]["activity"] == "tree_scan"
    assert "Chrome 原生窗口外壳" in result["message"]
    assert result["next_action"]["tool"] == "shizai_extension_start_capture"


def test_extension_tree_accepts_chrome_uia_document_chain():
    chain = [
        {"elementId": "uia:window", "name": "Window", "type": "32"},
        {"elementId": "uia:pane", "name": "Pane", "type": "32"},
        {"elementId": "uia:document", "name": "Document", "type": "6"},
        {"elementId": "uia:group", "name": "Group", "type": "6", "children": []},
        {"elementId": "uia:edit", "name": "Edit", "tag": "文本框", "type": "6"},
    ]
    assert _tree_has_web_element(chain) is True
    desktop = _FakeCaptureDesktop(children=chain)
    result = ShizaiExtensionBridge(desktop=desktop).capture_tree("wnd:1")
    assert result["capture_ready"] is True
    assert result["web_element_count"] >= 1
    assert all(call[1] != "startDeepCapture" for call in desktop.calls)


def test_extension_tree_retries_transient_read_and_reports_attempts():
    class FlakyCaptureDesktop(_FakeCaptureDesktop):
        def __init__(self):
            super().__init__(
                children=[
                    {"elementId": "uia:document", "name": "Document", "type": "6"},
                    {"elementId": "uia:edit", "name": "搜索", "tag": "文本框", "type": "6"},
                ]
            )
            self.failed = False

        def call(self, service, method, args=None):
            if method == "getDeepCaptureElementChildrenById" and not self.failed:
                self.failed = True
                self.calls.append((service, method, args or []))
                raise ShizaiCloudError("DeepCaptureService timeout")
            return super().call(service, method, args)

    result = ShizaiExtensionBridge(desktop=FlakyCaptureDesktop()).capture_tree("wnd:1")

    assert result["capture_ready"] is True
    assert result["read_retries"] == 1
    assert result["recovered_from_transient_error"] is True


def test_extension_windows_placeholder_is_not_reported_as_chrome(monkeypatch):
    monkeypatch.setattr(extension_bridge, "_enum_windows", lambda: [])
    result = ShizaiExtensionBridge(desktop=_FakeCaptureDesktop()).capture_windows(include_non_chrome=True)
    assert result["count"] == 0
    assert result["chrome_session"]["detected"] is False
    assert result["ui_feedback"]["phase"] == "waiting"
    assert result["next_action"]["tool"] == "shizai_rpa_open_browser"


def test_extension_tree_requires_document_before_uia_web_control():
    chain = [
        {"elementId": "uia:group", "name": "Group", "type": "6"},
        {"elementId": "uia:text", "name": "Text", "type": "6"},
    ]
    assert _tree_has_web_element(chain) is False


def test_extension_scan_query_filters_semantic_candidates_and_detects_search_input():
    nodes = [
        {
            "elementId": "uia:input",
            "name": "搜索",
            "tag": "编辑",
            "props": [{"name": "class", "value": "jd_search_input"}],
        },
        {
            "elementId": "uia:button",
            "name": "搜索",
            "tag": "按钮",
            "props": [{"name": "class", "value": "jd_search_button"}],
        },
        {
            "elementId": "uia:price",
            "name": "¥1299",
            "tag": "文本",
        },
        {
            "elementId": "uia:merchant",
            "name": "店铺旗舰店",
            "tag": "链接",
        },
    ]

    search = extension_bridge._scan_tree_nodes(nodes, query="搜索框", max_nodes=20, document_seen=True)
    assert [item["element_id"] for item in search] == ["uia:input"]
    assert search[0]["is_editable"] is True
    assert search[0]["is_search_input"] is True
    assert search[0]["semantic_fields"] == ["search"]
    assert search[0]["selector_hint"]["class"] == "jd_search_input"

    prices = extension_bridge._scan_tree_nodes(nodes, query="价格", max_nodes=20, document_seen=True)
    assert [item["element_id"] for item in prices] == ["uia:price"]


def test_extension_ui_status_exposes_last_capture_phase():
    desktop = _FakeCaptureDesktop()
    bridge = ShizaiExtensionBridge(desktop=desktop)
    bridge.start_capture(package_path="C:/tmp/native-flow")
    result = bridge.ui_status()
    assert result["kind"] == "shizai_rpa_ui_feedback"
    assert result["ui_feedback"]["phase"] == "capturing"
    assert result["status"]["ui_feedback"]["activity"] == "element_pick"


def test_extension_scan_query_without_semantic_alias_requires_direct_text_match():
    nodes = [
        {"elementId": "uia:one", "name": "商品价格", "tag": "文本"},
        {"elementId": "uia:two", "name": "店铺帮助", "tag": "链接"},
    ]
    result = extension_bridge._scan_tree_nodes(nodes, query="商品价格", max_nodes=20, document_seen=True)
    assert [item["element_id"] for item in result] == ["uia:one"]


def test_extension_scan_infers_product_title_merchant_and_numeric_price():
    nodes = [
        {"elementId": "uia:title", "name": "小米手机旗舰款 16GB 512GB", "tag": "文本", "children": []},
        {"elementId": "uia:merchant", "name": "小米官方旗舰店", "tag": "链接", "children": []},
        {"elementId": "uia:price", "name": "4099", "tag": "文本", "children": []},
    ]
    result = extension_bridge._scan_tree_nodes(
        nodes,
        query="",
        max_nodes=20,
        document_seen=True,
        path=("Document", "组", "组", "组"),
    )
    by_id = {item["element_id"]: item for item in result}
    assert "product_name" in by_id["uia:title"]["semantic_fields"]
    assert "merchant" in by_id["uia:merchant"]["semantic_fields"]
    assert "price" in by_id["uia:price"]["semantic_fields"]


def test_extension_scan_uses_accessible_property_for_jd_self_operated_merchant():
    nodes = [
        {
            "elementId": "uia:card",
            "name": "Group",
            "tag": "组",
            "props": [{"name": "class", "value": "plugin_goodsCardWrapper"}, {"name": "@index", "value": "3"}],
            "children": [
                {
                    "elementId": "uia:self-operated",
                    "name": "Image",
                    "tag": "图像",
                    "props": [{"name": "name", "value": '"自营"'}],
                    "children": [],
                }
            ],
        }
    ]
    result = extension_bridge._scan_tree_nodes(
        nodes,
        query="",
        max_nodes=20,
        document_seen=True,
        path=("Document", "组", "组", "组"),
    )
    merchant = next(item for item in result if item["element_id"] == "uia:self-operated")
    assert merchant["text"] == "自营"
    assert "merchant" in merchant["semantic_fields"]
    assert merchant["card_context"]["index"] == "3"


def test_extension_scan_does_not_classify_footer_headings_as_product_names():
    nodes = [
        {"elementId": "uia:footer", "name": "支付方式", "tag": "标题"},
        {"elementId": "uia:title", "name": "OPPOFind X9s Pro 16GB 手机", "tag": "文本"},
    ]
    result = extension_bridge._scan_tree_nodes(
        nodes,
        query="商品名称",
        max_nodes=20,
        document_seen=True,
        path=("Document", "组", "组", "组"),
    )
    assert [item["element_id"] for item in result] == ["uia:title"]


def test_extension_atomic_page_field_capture_selects_one_product_card(monkeypatch):
    bridge = ShizaiExtensionBridge(desktop=object())
    candidates = [
        {
            "element_id": "name-a",
            "semantic_fields": ["product_name"],
            "semantic_score": 4,
            "scan_order": 1,
            "text": "Phone A",
        },
        {
            "element_id": "merchant-a",
            "semantic_fields": ["merchant"],
            "semantic_score": 4,
            "scan_order": 2,
            "text": "示例旗舰店",
        },
        {"element_id": "price-a", "semantic_fields": ["price"], "semantic_score": 3, "scan_order": 3, "text": "4099"},
        {
            "element_id": "sales-a",
            "semantic_fields": ["sales"],
            "semantic_score": 5,
            "scan_order": 4,
            "text": "已售5万+",
        },
        {
            "element_id": "name-b",
            "semantic_fields": ["product_name"],
            "semantic_score": 4,
            "scan_order": 5,
            "text": "Phone B",
        },
        {
            "element_id": "merchant-b",
            "semantic_fields": ["merchant"],
            "semantic_score": 4,
            "scan_order": 6,
            "text": "旗舰店",
        },
        {"element_id": "price-b", "semantic_fields": ["price"], "semantic_score": 3, "scan_order": 7, "text": "2999"},
        {
            "element_id": "sales-b",
            "semantic_fields": ["sales"],
            "semantic_score": 5,
            "scan_order": 8,
            "text": "已售1万+",
        },
    ]
    monkeypatch.setattr(
        bridge,
        "scan_page",
        lambda **_kwargs: {
            "kind": "shizai_page_element_candidates",
            "capture_ready": True,
            "candidates": candidates,
            "page_title": "结果页",
            "page_url": "https://example.test/results",
        },
    )

    def fake_capture(element_id, **_kwargs):
        suffix = element_id[-1]
        field = next(item for item in candidates if item["element_id"] == element_id)["semantic_fields"][0]
        return {
            "record": {
                "id": element_id,
                "name": field,
                "card_context": {"key": f"product-card-{suffix}"},
                "link_context": {},
            }
        }

    monkeypatch.setattr(bridge, "capture_element", fake_capture)
    result = bridge.capture_page_fields(
        fields=["product_name", "merchant", "price", "sales"],
        candidate_limit=2,
    )
    assert result["complete"] is True
    assert result["selected_group"]["key"] == "product-card-a"
    assert result["selected_field_ids"] == {
        "product_name": "name-a",
        "merchant": "merchant-a",
        "price": "price-a",
        "sales": "sales-a",
    }


def test_extension_atomic_capture_expands_only_missing_field_until_same_card_is_found(monkeypatch):
    bridge = ShizaiExtensionBridge(desktop=object())
    candidates = [
        {
            "element_id": "name-a",
            "semantic_fields": ["product_name"],
            "semantic_score": 8,
            "scan_order": 1,
            "text": "Phone A",
        },
        {
            "element_id": "merchant-a",
            "semantic_fields": ["merchant"],
            "semantic_score": 8,
            "scan_order": 2,
            "text": "示例自营旗舰店",
        },
        {"element_id": "price-a", "semantic_fields": ["price"], "semantic_score": 8, "scan_order": 3, "text": "4099"},
        {"element_id": "link-a", "semantic_fields": ["link"], "semantic_score": 8, "scan_order": 4, "text": "详情"},
        {
            "element_id": "sales-b",
            "semantic_fields": ["sales"],
            "semantic_score": 8,
            "scan_order": 5,
            "text": "已售5万+",
        },
        {
            "element_id": "sales-a",
            "semantic_fields": ["sales"],
            "semantic_score": 7,
            "scan_order": 6,
            "text": "已售9000+",
        },
    ]
    monkeypatch.setattr(
        bridge,
        "scan_page",
        lambda **_kwargs: {
            "kind": "shizai_page_element_candidates",
            "capture_ready": True,
            "candidates": candidates,
            "page_title": "结果页",
            "page_url": "https://example.test/results",
        },
    )
    card_by_id = {
        "name-a": "card-a",
        "merchant-a": "card-a",
        "price-a": "card-a",
        "link-a": "card-a",
        "sales-b": "card-b",
        "sales-a": "card-a",
    }

    def fake_capture(element_id, **_kwargs):
        field = next(item for item in candidates if item["element_id"] == element_id)["semantic_fields"][0]
        return {
            "record": {
                "id": element_id,
                "name": field,
                "card_context": {"key": card_by_id[element_id]},
                "link_context": {},
            }
        }

    captured_ids = []
    original_capture = fake_capture

    def recording_capture(element_id, **kwargs):
        captured_ids.append(element_id)
        return original_capture(element_id, **kwargs)

    monkeypatch.setattr(bridge, "capture_element", recording_capture)
    result = bridge.capture_page_fields(
        fields=["product_name", "merchant", "price", "sales", "link"],
        candidate_limit=1,
    )
    assert result["complete"] is True
    assert result["selected_group"]["key"] == "card-a"
    assert result["selected_field_ids"]["sales"] == "sales-a"
    assert result["expansion_rounds"] == 1
    assert "sales-a" in captured_ids


def test_extension_element_marks_customer_service_selector_as_link_noise():
    selector = {
        "commonProps": {"GUIType": "Chrome", "processName": "chrome.exe"},
        "selector": [
            {
                "name": "Hyperlink",
                "type": "6",
                "props": [{"name": "class", "value": "_customer_service_icon_3z0zc_60"}],
            }
        ],
        "elementDetail": {"elementName": "hyperlink__customer_service_icon"},
    }
    record = _element_record("uia:customer", {"elementAlias": "Hyperlink"}, selector)
    assert record["link_context"]["is_link"] is True
    assert record["link_context"]["service_or_navigation"] is True


def test_collection_binding_rejects_customer_service_link_and_cross_card_mix():
    payload = {
        "elementMap": {
            "name": {"card_context": {"key": "card-a"}, "link_context": {}},
            "merchant": {"card_context": {"key": "card-a"}, "link_context": {}},
            "price": {"card_context": {"key": "card-a"}, "link_context": {}},
            "sales": {"card_context": {"key": "card-a"}, "link_context": {}},
            "link": {"card_context": None, "link_context": {"service_or_navigation": True}},
        }
    }
    issues = project._collection_binding_diagnostics(
        payload,
        ["name", "merchant", "price", "sales", "link"],
        ["商品名称", "商家名称", "价格", "销量", "商品链接"],
    )
    messages = " ".join(item["message"] for item in issues)
    assert any(item["check"] == "same_product_card" for item in issues)
    assert any(item["check"] == "product_link_selector" for item in issues)
    assert "客服" in messages


def test_extension_element_rejects_chrome_shell_selector():
    desktop = _FakeCaptureDesktop(
        selector={
            "commonProps": {"GUIType": "Windows", "processName": "chrome.exe"},
            "selector": [{"name": "Window", "type": "32", "props": []}],
        }
    )
    with pytest.raises(ShizaiCloudError, match="没有网页 selector"):
        ShizaiExtensionBridge(desktop=desktop).capture_element("uia:tab")


def test_collection_selector_rejects_uia_only_chain():
    chain = [
        {"name": "Window", "type": "32", "props": [{"name": "title", "value": "Chrome"}]},
        {"name": "Group", "type": "6", "props": [{"name": "class", "value": "products"}]},
        {"name": "Text", "type": "6", "props": [{"name": "name", "value": "Phone"}]},
    ]
    assert project._has_collection_selector(chain) is False


def test_collection_selector_accepts_uia_chain_after_document():
    chain = [
        {"name": "Window", "type": "32", "props": []},
        {"name": "Pane", "type": "32", "props": []},
        {"name": "Document", "type": "6", "props": []},
        {"name": "Group", "type": "6", "props": [{"name": "class", "value": "goods-card"}]},
        {"name": "Text", "type": "6", "props": [{"name": "name", "value": "Phone"}]},
    ]
    assert project._has_collection_selector(chain) is True


def test_collection_selector_converts_uia_chain_to_web_chain():
    element = {
        "selector": {
            "commonProps": {"GUIType": "Windows", "cssSelector": ""},
            "selector": [
                {"name": "Window", "type": "32", "props": [{"name": "title", "value": "Chrome"}]},
                {"name": "Pane", "type": "32", "props": [{"name": "automationid", "value": "10"}]},
                {
                    "name": "Group",
                    "type": "6",
                    "props": [
                        {"name": "class", "value": "goods-card"},
                        {"name": "@index", "value": "0"},
                    ],
                },
                {
                    "name": "Text",
                    "type": "6",
                    "props": [
                        {"name": "name", "value": "a sampled product"},
                        {"name": "@index", "value": "1"},
                    ],
                },
            ],
        }
    }
    converted = project._collection_selector_chain(element)
    assert converted is not None
    assert all(item["type"] == "web" for item in converted)
    assert [item["name"] for item in converted] == ["div", "span"]
    assert converted[0]["props"][0]["name"] == "class"
    assert not any(prop["name"] == "name" for item in converted for prop in item["props"])
    assert project._has_collection_selector(converted) is True


def test_collection_selector_keeps_web_chain_and_uses_css_fallback():
    element = {
        "selector": {
            "commonProps": {"cssSelector": json.dumps(".goods-card .goods-title")},
            "selector": [
                {"name": "tab", "props": [{"name": "url", "value": "https://example.test"}]},
                {"name": "span", "type": "web", "props": []},
            ],
        }
    }
    converted = project._collection_selector_chain(element)
    assert converted is not None
    assert [item["name"] for item in converted] == ["span"]
    assert converted[0]["props"][-1]["name"] == "class"
    assert converted[0]["props"][-1]["textValue"] == "goods-title"


def test_collection_selector_adds_card_scope_for_js_generated_element():
    element = {
        "card_context": {"family_key": "plugin_goodsCardWrapper"},
        "selector": {
            "commonProps": {"cssSelector": '"span[data-shizai-agent-product-merchant=\\"1\\"]"'},
            "selector": [
                {"name": "tab", "props": []},
                {
                    "name": "span",
                    "type": "web",
                    "props": [
                        {"name": "data-shizai-agent-product-merchant", "value": '"1"'},
                    ],
                },
            ],
        },
    }
    converted = project._collection_selector_chain(element)
    assert converted is not None
    assert [item["name"] for item in converted] == ["div", "span"]
    assert converted[0]["props"][0]["name"] == "class"
    assert converted[0]["props"][0]["textValue"] == "plugin_goodsCardWrapper"


def test_debug_log_line_before_run_is_ignored():
    from shizai_agent.core.debug_session import _log_line_timestamp

    assert _log_line_timestamp("2026-08-29 10:09:33,038 log_util.py[line: 52] INFO: error") < _log_line_timestamp(
        "2026-08-29 10:16:18,211 log_util.py[line: 52] INFO: 开始运行"
    )
    assert _log_line_timestamp("not a timestamp") is None


def test_collection_selector_static_properties_leave_expression_value_empty():
    property_value = project._collection_property("class", "goods-card")

    assert property_value == {
        "name": "class",
        "value": "goods-card",
        "pattern": "equal",
        "accurate": "false",
        "textValue": "goods-card",
        "expressionValue": "",
    }


def test_page_scope_js_uses_empty_selector_for_runtime(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "page-scope-js")
    metadata = {
        "packageName": "Page Scope JS",
        "projectMode": "visual",
        "mcpUseJsLink": True,
    }
    (native / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")
    task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {"args0": [{"name": "statements", "value": ["js"]}]},
            "js": {
                "id": "js",
                "type": "WebBrowser_RunJsInIeBySelector_v3",
                "args0": [{"name": "uiSelector", "value": "stale-selector"}],
            },
        },
    }
    (native / "io5cd.task").write_text(json.dumps(task), encoding="utf-8")

    task_path, changed = project._normalize_native_page_scope_js(native)

    assert changed is True
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    args = payload["componentModelEntities"]["js"]["args0"]
    assert next(item["value"] for item in args if item["name"] == "uiSelector") == ""


def test_page_scope_uses_native_document_context_and_drops_product_card_indexes(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "page-scope-native")
    root = {
        "id": "product-name",
        "name": "商品名称",
        "title": "手机 - 商品搜索 - 京东",
        "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
        "selector": {
            "commonProps": {
                "GUIType": "Chrome",
                "cssSelector": '"div.product-card span.title"',
            },
            "selector": [
                {"name": "tab", "props": []},
                {
                    "name": "div",
                    "type": "web",
                    "props": [
                        {"name": "class", "value": "product-card"},
                        {"name": "@index", "value": "12"},
                    ],
                },
                {
                    "name": "span",
                    "type": "web",
                    "props": [
                        {"name": "class", "value": "title"},
                        {"name": "@index", "value": "0"},
                    ],
                },
            ],
        },
        "mcpNativeSelector": {
            "commonProps": {"GUIType": "Chrome", "processName": "chrome.exe"},
            "selector": [
                {"name": "Window", "type": "32", "props": []},
                {
                    "name": "Pane",
                    "type": "32",
                    "props": [
                        {"name": "class", "value": '"Chrome_WidgetWin_0"'},
                    ],
                },
                {
                    "name": "Document",
                    "type": "6",
                    "props": [
                        {"name": "name", "value": '"手机 - 商品搜索 - 京东"'},
                        {"name": "@index", "value": "0"},
                    ],
                },
                {
                    "name": "Group",
                    "type": "6",
                    "props": [
                        {"name": "class", "value": '"results-root"'},
                        {"name": "@index", "value": "0"},
                    ],
                },
                {
                    "name": "Group",
                    "type": "6",
                    "props": [
                        {"name": "class", "value": '"product-list"'},
                        {"name": "@index", "value": "1"},
                    ],
                },
                {
                    "name": "Group",
                    "type": "6",
                    "props": [
                        {"name": "class", "value": '"product-card"'},
                        {"name": "@index", "value": "12"},
                    ],
                },
                {
                    "name": "Text",
                    "type": "6",
                    "props": [
                        {"name": "name", "value": '"具体商品名称"'},
                        {"name": "@index", "value": "0"},
                    ],
                },
            ],
        },
    }
    (native / "elements.json").write_text(
        json.dumps({"elementMap": {"product-name": root}, "groupMap": {}}),
        encoding="utf-8",
    )

    page_scope_id = project._upsert_page_scope_element(paths, str(native), "product-name")
    payload = json.loads((native / "elements.json").read_text(encoding="utf-8"))
    page_scope = payload["elementMap"][page_scope_id]
    selector_items = page_scope["selector"]["selector"]

    assert [item["name"] for item in selector_items] == ["tab", "div", "div"]
    assert page_scope["mcpPageScopeContext"] == {
        "source": "mcpNativeSelector",
        "strength": "strong",
        "stops_before_product_card": True,
        "contains_product_card_index": False,
    }
    assert [
        next(prop["value"] for prop in item["props"] if prop["name"] == "class") for item in selector_items[1:]
    ] == ['"results-root"', '"product-list"']
    assert [
        next(prop["expressionValue"] for prop in item["props"] if prop["name"] == "class")
        for item in selector_items[1:]
    ] == ['"results-root"', '"product-list"']
    assert not any(prop.get("name") == "@index" for item in selector_items for prop in item.get("props", []))
    assert all("product-card" not in json.dumps(item, ensure_ascii=False) for item in selector_items[1:])


def test_page_scope_uses_css_fallback_for_legacy_capture(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "page-scope-legacy")
    project.import_elements(
        paths,
        str(native),
        [
            {
                "id": "legacy-field",
                "name": "结果字段",
                "title": "结果页",
                "url": "https://example.test/results",
                "tag": "div",
                "css_selector": ".results .title",
            }
        ],
    )

    project._upsert_page_scope_element(paths, str(native), "legacy-field")
    payload = json.loads((native / "elements.json").read_text(encoding="utf-8"))
    page_scope = payload["elementMap"][project.MCP_PAGE_SCOPE_ELEMENT_ID]

    assert page_scope["mcpPageScopeContext"]["source"] == "css_selector"
    assert page_scope["mcpPageScopeContext"]["strength"] == "weak"
    assert [item["name"] for item in page_scope["selector"]["selector"]] == ["tab", "div"]
    assert page_scope["selector"]["selector"][1]["props"][0]["name"] == "class"
    assert page_scope["selector"]["selector"][1]["props"][0]["value"] == '"title"'


def test_page_scope_rejects_tab_only_context(tmp_path):
    paths = _paths(tmp_path)
    native = _make_project(paths, "page-scope-invalid")
    (native / "elements.json").write_text(
        json.dumps(
            {
                "elementMap": {
                    "field": {
                        "id": "field",
                        "title": "结果页",
                        "url": "https://example.test/results",
                        "selector": {"selector": [{"name": "tab", "props": []}]},
                    },
                },
                "groupMap": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no reusable web context"):
        project._upsert_page_scope_element(paths, str(native), "field")


def test_export_project_creates_native_zip(tmp_path):
    paths = _paths(tmp_path)
    _make_project(paths)
    output = tmp_path / "out.zip"
    result = project.export_project(paths, "c_test", str(output))
    assert result["file_count"] == 4
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"szrpa.json", "elements.json", "project.flow", "main.sz"}


def test_agent_capabilities_without_bundle(tmp_path):
    result = agent.capabilities(_paths(tmp_path))
    assert result["write_operations_enabled"] is False
    assert result["discovered"]["services"] == []


def test_agent_capabilities_discovers_bundle(tmp_path):
    install = tmp_path / "install"
    bundle = install / "resources" / "app" / "resources" / "fe-assets" / "agent-brain"
    bundle.mkdir(parents=True)
    (bundle / "page.js").write_text(
        'callBizNodeService("AgentBrainAppService","getAppList"); " /agent/run_step" ',
        encoding="utf-8",
    )
    result = agent.capabilities(_paths(tmp_path, install))
    assert "AgentBrainAppService" in result["discovered"]["services"]
    assert "getAppList" in result["discovered"]["methods"]


class _AgentRpcClient:
    def __init__(self, responses=None):
        self.config = ShizaiCloudConfig(agent_id="agent-test")
        self.responses = responses or {}
        self.calls = []

    def call(self, service, method, args=None):
        self.calls.append((service, method, args))
        return self.responses.get(method, {"isSuccess": True, "data": []})


def test_agent_rpc_read_write_gate_and_redaction():
    client = _AgentRpcClient({"getMcpServerList": {"isSuccess": True, "data": {"env": {"API_KEY": "sk-test-secret"}}}})
    result = agent.rpc("AgentBrainAppService", "getMcpServerList", [{}], client=client)
    assert result["data"]["env"]["API_KEY"] == "[REDACTED]"
    with pytest.raises(PermissionError, match="allow-write"):
        agent.rpc("AgentBrainAppService", "saveWorkflowDraft", [{}], client=client)
    agent.rpc("AgentBrainAppService", "saveWorkflowDraft", [{}], allow_write=True, client=client)
    assert client.calls[-1][1] == "saveWorkflowDraft"


def test_agent_component_catalog_uses_live_defaults_and_draft():
    client = _AgentRpcClient(
        {
            "getNodesDefaultConfigs": {
                "isSuccess": True,
                "data": [{"type": "database", "config": {"outputs": {"result_json": {"type": "object"}}}}],
            },
            "getWorkflowDraft": {
                "isSuccess": True,
                "data": {"draft": {"graph": {"nodes": [{"data": {"type": "code"}}]}}},
            },
            "getBuildInTools": {
                "isSuccess": True,
                "data": [
                    {"name": "builtin", "tools": [{"name": "file_reader", "parameters": [], "output_parameters": []}]}
                ],
            },
        }
    )
    result = agent.query_components(client=client)
    assert result["node_types"] == ["code", "database"]
    assert result["tool_providers"]["builtin"][0]["tools"][0]["name"] == "file_reader"
    assert result["generation_rules"]["variable_selector_shape"] == ["node_id", "output_name"]


def test_agent_generation_context_adds_734_style_plan_contract():
    client = _AgentRpcClient(
        {
            "getNodesDefaultConfigs": {"isSuccess": True, "data": [{"type": "database"}]},
        }
    )
    result = agent.generation_context(query="按日期查询日志并输出结果", client=client)
    assert result["query"] == "按日期查询日志并输出结果"
    assert result["planning_contract"]["phases"] == [
        "intent",
        "path",
        "materialize",
        "validate",
        "execute",
        "reflect",
    ]
    assert "depends_on" in result["planning_contract"]["task_fields"]
    assert "shizai_validate_draft" in result["next_tools"]
    assert result["components"]["generation_rules"]["system_variable_names"] == ["sys.query", "query", "files"]
    assert result["planning_contract"]["variable_binding"]["system_selector_examples"] == [
        ["start_node_id", "sys.query"],
        ["sys", "query"],
        ["sys", "files"],
    ]
    assert result["planning_contract"]["implementation_policy"]["default_strategy"] == "component_first"
    assert "component_type" in result["planning_contract"]["implementation_policy"]["required_plan_fields"]


def test_agent_draft_requires_agent_selection_when_no_default_is_configured():
    client = _AgentRpcClient()
    client.config = ShizaiCloudConfig(agent_id="")

    result = agent.query_draft(client=client)

    assert result == {
        "ok": False,
        "failure_kind": "input_validation",
        "error": "agent_id is required when no Agent is configured",
        "next_tool": "shizai_apps",
        "message": "请先调用 shizai_apps 选择目标 Agent，再用 agent_id 读取草稿。",
    }


def test_generation_context_routes_to_apps_before_draft_without_default_agent():
    client = _AgentRpcClient()
    client.config = ShizaiCloudConfig(agent_id="")

    result = agent.generation_context(query="生成一个回答流程", client=client)

    assert result["agent_id"] == ""
    assert result["next_tools"] == ["shizai_apps", "shizai_components", "shizai_validate_draft", "shizai_apply_flow"]


def test_native_rpa_generation_context_requires_component_first_mapping():
    context = project.native_rpa_generation_context("从 Excel 逐行读取并保存未完成编号")
    policy = context["implementation_policy"]
    assert policy["default_strategy"] == "component_first"
    assert policy["priority_order"][-1] == "只覆盖剩余原子计算的短插入代码"
    assert "打开文件" in policy["short_code_exception"]["must_not_do"]
    assert [item["component_type"] for item in context["example_excel_row_processing"]] == [
        "Excel_Cs_OpenV4",
        "Excel_Cs_ReadRowCount",
        "excel_loop_data_v2",
        "Excel_Cs_ReadCell",
        "base_command_custom",
        "Excel_Cs_WriteCell",
        "Excel_Cs_Save",
    ]


def test_agent_generate_flow_materializes_common_database_qa_path():
    client = _AgentRpcClient(
        {
            "getNodesDefaultConfigs": {
                "isSuccess": True,
                "data": [
                    {"type": "start", "config": {}},
                    {"type": "database", "config": {"outputs": {"result": {}}}},
                    {"type": "llm", "config": {}},
                    {"type": "answer", "config": {}},
                ],
            },
        }
    )
    result = agent.generate_flow("查询数据库中的订单并总结后输出", client=client)
    assert result["generated"] is True
    assert result["validation"]["valid"] is True
    nodes = result["draft"]["graph"]["nodes"]
    assert [node["data"]["type"] for node in nodes] == ["start", "database", "llm", "answer"]
    assert len(result["draft"]["graph"]["edges"]) == 3
    assert result["saved"] is False


def test_agent_generate_flow_requires_explicit_write_for_save():
    client = _AgentRpcClient({"getNodesDefaultConfigs": {"isSuccess": True, "data": [{"type": "start"}]}})
    with pytest.raises(PermissionError, match="allow_write"):
        agent.generate_flow("接收用户问题", save=True, client=client)


def test_agent_validate_draft_checks_graph_edges_and_selectors():
    client = _AgentRpcClient(
        {
            "getNodesDefaultConfigs": {
                "isSuccess": True,
                "data": [{"type": "database", "config": {"outputs": {"result_json": {}}}}],
            },
        }
    )
    draft = {
        "graph": {
            "nodes": [
                {"id": "start", "data": {"type": "database"}},
                {"id": "query", "data": {"type": "database", "input": ["start", "result_json"]}},
            ],
            "edges": [{"source": "start", "target": "query"}],
        }
    }
    result = agent.validate_draft(draft, client=client)
    assert result["valid"] is True
    assert result["stats"]["nodes"] == 2
    assert ["start", "result_json"] in result["selectors"]

    invalid = agent.validate_draft(
        {
            "graph": {
                "nodes": [{"id": "start", "data": {"type": "database"}}],
                "edges": [{"source": "start", "target": "missing"}],
            }
        },
        client=client,
    )
    assert invalid["valid"] is False
    assert any("missing target" in error for error in invalid["errors"])


def test_agent_publish_uses_explicit_publish_rpc():
    client = _AgentRpcClient({"publishWorkflow": {"isSuccess": True, "data": {"published": True}}})
    result = agent.publish(publish_range="only_me", client=client)
    assert result["data"]["published"] is True
    assert client.calls[-1] == (
        "AgentBrainAppService",
        "publishWorkflow",
        [{"appId": "agent-test", "appPublishInfo": {"publish_range": "only_me"}}],
    )


def test_agent_apps_uses_designer_compatible_filters():
    client = _AgentRpcClient({"getAppList": {"isSuccess": True, "data": {"total": 0, "data": []}}})
    agent.query_apps(client=client)
    assert client.calls[-1] == (
        "AgentBrainAppService",
        "getAppList",
        [{"page": 1, "limit": 20, "mode": "advanced-chat"}],
    )


def test_agent_save_draft_uses_explicit_write_path():
    client = _AgentRpcClient({"saveWorkflowDraft": {"isSuccess": True, "data": {"saved": True}}})
    result = agent.save_draft({"graph": {"nodes": [], "edges": []}}, client=client)
    assert result["data"]["saved"] is True
    assert client.calls[-1] == (
        "AgentBrainAppService",
        "saveWorkflowDraft",
        [{"appId": "agent-test", "draft": {"graph": {"nodes": [], "edges": []}}}],
    )


def test_agent_rpc_cli_supports_json_args(monkeypatch):
    client = _AgentRpcClient({"getModelProviders": {"isSuccess": True, "data": {"providers": []}}})
    monkeypatch.setattr(cli_module, "_desktop", lambda: client)
    result = CliRunner().invoke(
        cli, ["--json", "agent", "rpc", "AgentBrainAppService", "getModelProviders", "--args-json", "[]"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["providers"] == []


def test_native_small_card_renderer_call_uses_native_protocol():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            self.sent.append(json.loads(value))
            packet = self.sent[-1]
            body = packet["body"]
            if packet["packetType"] == "methodCall":
                self.responses.append(
                    json.dumps(
                        {
                            "packetType": "callResult",
                            "body": {"callId": body["callId"], "result": {"isSuccess": True, "code": 0}},
                        }
                    )
                )
            else:
                self.responses.append(
                    json.dumps(
                        {
                            "packetType": "render-call-render-res",
                            "body": {"callId": body["callId"], "result": {"isSuccess": True}},
                        }
                    )
                )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()
    result = NativeSmallCard(client=client).show(state="scanning")
    packets = client.socket.sent
    assert result["animation"] == "native_tars_lottie_loop"
    assert [packet["packetType"] for packet in packets] == [
        "methodCall",
        "render-call-render-req",
    ]
    assert packets[0]["body"]["methodName"] == "openSmallCardWindow"
    assert packets[1]["body"]["targetClientId"] == "$SmallCardClientId$"
    assert packets[1]["body"]["serviceName"] == "SmallCardService"
    assert packets[1]["body"]["methodName"] == "updateSmallCardStepText"
    assert packets[1]["body"]["args"] == ["正在扫描页面并分析可用元素。"]


def test_native_small_card_treats_already_open_window_as_reusable(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            body = packet["body"]
            if packet["packetType"] == "methodCall":
                result = {"isSuccess": False, "code": 5000, "message": "window already open"}
                response_type = "callResult"
            else:
                result = {"isSuccess": True, "code": 0}
                response_type = "render-call-render-res"
            self.responses.append(
                json.dumps(
                    {
                        "packetType": response_type,
                        "body": {"callId": body["callId"], "result": result},
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    monkeypatch.setattr("shizai_agent.core.native_ui.time.sleep", lambda _seconds: None)
    result = NativeSmallCard(client=FakeClient()).show(state="scanning")

    assert result["reused_window"] is True
    assert result["open_result_status"] == "reused_window"
    assert "open_nonfatal" in result
    assert "open_warning" not in result


def test_native_small_card_update_does_not_reopen_window():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "render-call-render-res",
                        "body": {"callId": packet["body"]["callId"], "result": {"isSuccess": True}},
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()
    result = NativeSmallCard(client=client).update(state="running")

    assert result["state"] == "running"
    assert len(client.socket.sent) == 1
    assert client.socket.sent[0]["packetType"] == "render-call-render-req"
    assert client.socket.sent[0]["body"]["methodName"] == "updateSmallCardStepText"


def test_native_small_card_takeover_countdown_updates_five_to_one(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "callResult"
                        if packet["packetType"] == "methodCall"
                        else "render-call-render-res",
                        "body": {
                            "callId": packet["body"]["callId"],
                            "result": {"isSuccess": True, "code": 0},
                        },
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    monkeypatch.setattr("shizai_agent.core.native_ui.time.sleep", lambda _seconds: None)
    client = FakeClient()
    result = NativeSmallCard(client=client).countdown(seconds=5)

    assert result["seconds"] == 5
    assert result["messages"] == [
        "塔斯将在 5 秒后接管你的电脑。",
        "塔斯将在 4 秒后接管你的电脑。",
        "塔斯将在 3 秒后接管你的电脑。",
        "塔斯将在 2 秒后接管你的电脑。",
        "塔斯将在 1 秒后接管你的电脑。",
    ]
    assert client.socket.sent[0]["body"]["methodName"] == "openSmallCardWindow"
    assert [packet["body"]["args"][0] for packet in client.socket.sent[1:]] == result["messages"]


def test_native_small_card_countdown_can_reuse_open_window(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "render-call-render-res",
                        "body": {"callId": packet["body"]["callId"], "result": {"isSuccess": True}},
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    monkeypatch.setattr("shizai_agent.core.native_ui.time.sleep", lambda _seconds: None)
    client = FakeClient()
    result = NativeSmallCard(client=client).countdown(seconds=2, ensure_open=False)

    assert result["opened_window"] is False
    assert len(client.socket.sent) == 2
    assert all(packet["packetType"] == "render-call-render-req" for packet in client.socket.sent)


def test_native_takeover_frame_uses_official_loading_window_protocol():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "callResult",
                        "body": {
                            "callId": packet["body"]["callId"],
                            "result": {"isSuccess": True, "code": 0},
                        },
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()
    shown = NativeTakeoverFrame(client=client).show()
    closed = NativeTakeoverFrame(client=client).close()

    assert shown["visible"] is True
    assert closed["visible"] is False
    assert shown["animation"] == "native_blue_purple_breathing_frame"
    assert [packet["body"]["serviceName"] for packet in client.socket.sent] == [
        "WindowManagerService",
        "WindowManagerService",
    ]
    assert [packet["body"]["methodName"] for packet in client.socket.sent] == [
        "openLoadingWindow",
        "closeLoadingWindow",
    ]


def test_native_mask_uses_complete_run_mask_service_protocol():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            self.sent.append(json.loads(value))
            packet = self.sent[-1]
            call_id = packet["body"]["callId"]
            response_type = "callResult"
            result = {"isSuccess": True, "code": 0}
            if packet["body"]["methodName"] == "showRunMask":
                result["data"] = "run-mask-1"
            self.responses.append(
                json.dumps(
                    {
                        "packetType": response_type,
                        "body": {"callId": call_id, "result": result},
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()
    result = NativeMask(client=client).show(mask_type="white", tips="正在扫描", close_after=True)
    packets = client.socket.sent
    assert result["animation"] == "native_run_mask"
    assert result["request_id"] == "run-mask-1"
    assert [packet["packetType"] for packet in packets] == [
        "methodCall",
        "methodCall",
        "methodCall",
    ]
    assert packets[0]["body"]["serviceName"] == "WindowManagerService"
    assert packets[0]["body"]["methodName"] == "showMainWindow"
    assert packets[1]["body"]["serviceName"] == "MaskAppService"
    assert packets[1]["body"]["methodName"] == "showRunMask"
    assert packets[1]["body"]["args"] == [
        {
            "operateFrom": "normal",
            "runType": "run",
            "tips": "正在扫描",
            "transparent": False,
            "isShowHeader": False,
        }
    ]
    assert packets[2]["body"]["serviceName"] == "MaskAppService"
    assert packets[2]["body"]["methodName"] == "closeRunMask"
    assert packets[2]["body"]["args"] == [{"requestId": "run-mask-1"}]


def test_native_mask_force_close_reaches_renderer_protocol():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            packet_type = "render-call-render-res" if packet["packetType"] == "render-call-render-req" else "callResult"
            self.responses.append(
                json.dumps(
                    {
                        "packetType": packet_type,
                        "body": {
                            "callId": packet["body"]["callId"],
                            "result": {"isSuccess": True, "code": 0, "data": None},
                        },
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()

    result = NativeMask(client=client).close(force=True)

    packet = client.socket.sent[0]
    assert result["visible"] is False
    assert result["force"] is True
    assert packet["body"]["serviceName"] == "MaskAppService"
    assert packet["body"]["methodName"] == "closeRunMask"
    assert packet["body"]["args"] == [{"requestId": "", "force": True}]
    renderer_packets = [item for item in client.socket.sent if item["packetType"] == "render-call-render-req"]
    assert [item["body"]["targetClientId"] for item in renderer_packets] == [
        "$HomeClientId$",
        "$AgentBrainClientId$",
        "$CurrentActiveEditorClientId$",
    ]
    assert all(
        item["body"]["args"] == [{"requestId": "", "force": True}]
        and item["body"]["serviceName"] == "MaskService"
        and item["body"]["methodName"] == "closeMaskV2"
        for item in renderer_packets
    )


def test_native_designer_stop_treats_3001_as_idempotent(monkeypatch):
    class Client:
        def call(self, service, method, args):
            if service == "AppRunService":
                assert (method, args) == ("stop", [{}])
                raise ShizaiCloudError("当前状态不可停止 (code=3001)")
            assert (service, method, args) == ("TrayManager", "runStateChange", ["stopped"])
            return {"isSuccess": True, "data": None}

    monkeypatch.setattr(project, "ShizaiDesktopAgent", Client)
    result = project._native_stop_designer_run(Client())
    assert result["attempted"] is True
    assert result["status"] == "already_stopped"
    assert result["tray_status"] == "complete"


def test_native_screenshot_capture_matches_installed_picker_protocol():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "callResult",
                        "body": {
                            "callId": packet["body"]["callId"],
                            "result": {"isSuccess": True, "code": 0, "data": "mask-1"}
                            if packet["body"]["methodName"] == "showRunMask"
                            else {"isSuccess": True, "code": 0},
                        },
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    client = FakeClient()
    started = NativeScreenshotCapture(client=client).start(request_id="capture-1")
    stopped = NativeScreenshotCapture(client=client).stop(request_id="capture-1")
    packets = client.socket.sent

    assert started["active"] is True
    assert stopped["active"] is False
    assert [packet["body"]["serviceName"] for packet in packets] == [
        "WindowManagerService",
        "NativeOperateService",
        "WindowManagerService",
        "NativeOperateService",
    ]
    assert packets[0]["body"]["methodName"] == "minimizeWindow"
    assert packets[0]["body"]["args"] == [{"requesterName": "agentScreenShot", "excepts": []}]
    assert packets[1]["body"]["methodName"] == "transferPluginAction"
    assert packets[1]["body"]["args"] == [
        {
            "pluginAction": "Element.ScreenshotCapture",
            "jsonParams": {"requestId": "capture-1", "actionCmd": "start", "memoryInfo": {}},
            "runType": "common",
        }
    ]
    assert packets[2]["body"]["methodName"] == "restoreWindowWithDelay"
    assert packets[2]["body"]["args"] == [{"requesterName": "agentScreenShot"}]
    assert packets[3]["body"]["methodName"] == "transferPluginAction"
    assert packets[3]["body"]["args"] == [
        {
            "pluginAction": "Element.ScreenshotCapture",
            "jsonParams": {"requestId": "capture-1", "actionCmd": "stop"},
            "runType": "common",
        }
    ]


def test_native_scan_demo_uses_screenshot_capture_instead_of_extra_windows(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.responses = []

        def send(self, value):
            packet = json.loads(value)
            self.sent.append(packet)
            self.responses.append(
                json.dumps(
                    {
                        "packetType": "callResult"
                        if packet["packetType"] == "methodCall"
                        else "render-call-render-res",
                        "body": {
                            "callId": packet["body"]["callId"],
                            "result": {"isSuccess": True, "code": 0, "data": "mask-1"}
                            if packet["body"].get("methodName") == "showRunMask"
                            else {"isSuccess": True, "code": 0},
                        },
                    }
                )
            )

        def recv(self):
            return self.responses.pop(0)

        def close(self):
            pass

    class FakeClient:
        class Config:
            timeout = 2
            client_id = "test-client"

        config = Config()

        def __init__(self):
            self.socket = FakeSocket()

        def _connect(self, timeout):
            return self.socket

    monkeypatch.setattr("shizai_agent.core.native_ui.time.sleep", lambda seconds: None)
    client = FakeClient()
    result = NativeScanDemo(client=client).run(interval=0.1, close_after=True, activate=False)
    packets = client.socket.sent
    method_names = [packet["body"].get("methodName") for packet in packets]

    assert result["screenshot_capture"]["animation"] == "native_element_screenshot_capture"
    assert "openLoadingWindow" not in method_names
    assert "closeLoadingWindow" not in method_names
    assert "transferPluginAction" in method_names
    transfer_packets = [packet for packet in packets if packet["body"].get("methodName") == "transferPluginAction"]
    assert transfer_packets[0]["body"]["args"][0]["pluginAction"] == "Element.ScreenshotCapture"
    assert transfer_packets[0]["body"]["args"][0]["jsonParams"]["actionCmd"] == "start"
    assert transfer_packets[-1]["body"]["args"][0]["jsonParams"]["actionCmd"] == "stop"


def test_native_small_card_cli_requires_write_and_supports_demo(monkeypatch):
    class FakeCard:
        def __init__(self, client=None):
            pass

        def demo(self, interval):
            return {"kind": "demo", "interval": interval}

        def show(self, **kwargs):
            return kwargs

    monkeypatch.setattr(cli_module, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    denied = CliRunner().invoke(cli, ["ui", "small-card", "--demo"])
    assert denied.exit_code != 0
    assert "requires --allow-write" in denied.output
    result = CliRunner().invoke(cli, ["--json", "ui", "small-card", "--demo", "--allow-write"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["kind"] == "demo"


def test_native_mask_cli_requires_write_and_supports_scan_demo(monkeypatch):
    class FakeMask:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            return {"kind": "mask", **kwargs}

        def close(self, **kwargs):
            return {"kind": "closed", **kwargs}

    class FakeScanDemo:
        def __init__(self, client=None):
            pass

        def run(self, **kwargs):
            return {"kind": "scan-demo", **kwargs}

    monkeypatch.setattr(cli_module, "NativeMask", FakeMask)
    monkeypatch.setattr(cli_module, "NativeScanDemo", FakeScanDemo)
    monkeypatch.setattr(cli_module, "_desktop", lambda: object())
    denied = CliRunner().invoke(cli, ["ui", "mask"])
    assert denied.exit_code != 0
    assert "requires --allow-write" in denied.output
    result = CliRunner().invoke(
        cli,
        ["--json", "ui", "mask", "--mask-type", "white", "--close", "--allow-write"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["close_after"] is True
    result = CliRunner().invoke(cli, ["--json", "ui", "scan-demo", "--allow-write"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["kind"] == "scan-demo"


def test_native_rpa_cli_cleans_native_ui_after_success_and_failure(monkeypatch):
    events = []
    client = object()

    class FakeCard:
        def __init__(self, client=None):
            pass

        def close(self):
            events.append("small_card")
            return {"visible": False}

    class FakeFrame:
        def __init__(self, client=None):
            pass

        def close(self):
            events.append("takeover_frame")
            return {"visible": False}

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, **kwargs):
            events.append(("native_mask", kwargs))
            return {"visible": False}

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def stop_inspect(self):
            events.append("inspect")
            return {"overlay_state": "inactive"}

    monkeypatch.setattr(cli_module, "_desktop", lambda: client)
    monkeypatch.setattr(cli_module, "_paths", lambda ctx: object())
    monkeypatch.setattr(cli_module, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(cli_module, "NativeTakeoverFrame", FakeFrame)
    monkeypatch.setattr(cli_module, "NativeMask", FakeMask)
    monkeypatch.setattr(cli_module, "ShizaiExtensionBridge", FakeBridge)
    should_fail = {"value": False}

    def run_native(*args, **kwargs):
        if should_fail["value"]:
            raise ValueError("synthetic run failure")
        return {"kind": "native_rpa_run", "started": True}

    monkeypatch.setattr(cli_module.project_core, "native_rpa_run", run_native)
    command = ["--json", "workflow", "rpa-run", "FLOW_REF", "--allow-write"]

    success = CliRunner().invoke(cli, command)
    assert success.exit_code == 0, success.output
    payload = json.loads(success.output)
    assert payload["native_ui"]["final_cleanup"]["inspect"]["overlay_state"] == "inactive"
    assert events == [
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
    ]

    events.clear()
    should_fail["value"] = False
    submitted = CliRunner().invoke(cli, ["--json", "workflow", "rpa-run", "FLOW_REF", "--no-wait", "--allow-write"])
    assert submitted.exit_code == 0, submitted.output
    submitted_payload = json.loads(submitted.output)
    assert submitted_payload["native_ui"]["final_cleanup"] == {
        "status": "deferred",
        "reason": "wait=false; native run may still be active",
    }
    assert events == [
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
    ]

    events.clear()
    monkeypatch.setattr(
        cli_module.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: {
            "kind": "native_rpa_run",
            "started": True,
            "status": {"status": "waiting_for_user"},
            "interaction_required": True,
        },
    )
    waiting = CliRunner().invoke(cli, command)
    assert waiting.exit_code == 0, waiting.output
    waiting_payload = json.loads(waiting.output)
    assert waiting_payload["native_ui"]["final_cleanup"] == {
        "status": "deferred",
        "reason": "waiting_for_user; preserve browser for user interaction",
    }
    assert events == [
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
    ]

    events.clear()
    should_fail["value"] = True
    monkeypatch.setattr(cli_module.project_core, "native_rpa_run", run_native)
    failure = CliRunner().invoke(cli, command)
    assert failure.exit_code != 0
    assert "synthetic run failure" in failure.output
    assert events == [
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
        "small_card",
        "takeover_frame",
        ("native_mask", {"force": True}),
        "inspect",
    ]


def test_mcp_capture_windows_starts_takeover_then_routes_missing_browser_to_navigation_step(monkeypatch):
    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def capture_windows(self, *, include_non_chrome=False):
            return {"count": 0, "windows": [], "chrome_session": {"detected": False}}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    takeover_calls = []
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: takeover_calls.append(kwargs) or {"takeover_started": True},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda *args, **kwargs: {"state": kwargs["state"], "message": kwargs["message"]},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_capture_windows",
        {"project_ref": "FLOW_REF", "include_non_chrome": False},
    )

    assert result["count"] == 0
    assert result["next_action"] == {
        "tool": "shizai_rpa_open_browser",
        "arguments": {"project_ref": "FLOW_REF", "allow_write": True},
        "reason": "当前不存在可识别的 Chrome 页面；根据已分析的 navigate 操作定位计划步骤并原生单步执行，禁止继续 scan_page。",
    }
    assert len(takeover_calls) == 1
    assert result["native_ui"]["takeover"]["takeover_started"] is True


def test_mcp_scan_page_short_circuits_when_browser_is_missing(monkeypatch):
    calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def capture_windows(self, *, include_non_chrome=False):
            calls.append("capture_windows")
            return {"count": 0, "windows": []}

        def scan_page(self, **kwargs):
            calls.append("scan_page")
            pytest.fail("scan_page must not run without a browser window")

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    takeover_calls = []
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: takeover_calls.append(kwargs) or {"takeover_started": True},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_update",
        lambda *args, **kwargs: {"state": kwargs["state"], "message": kwargs["message"]},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_scan_page",
        {"project_ref": "FLOW_REF", "query": "搜索框"},
    )

    assert calls == ["capture_windows"]
    assert len(takeover_calls) == 1
    assert result["workflow_status"] == "blocked"
    assert result["next_action"]["tool"] == "shizai_rpa_open_browser"
    assert result["next_action"]["arguments"] == {"project_ref": "FLOW_REF", "allow_write": True}


def test_mcp_open_browser_restores_persisted_flow_target(monkeypatch):
    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def start_capture_session(self, *, package_path=None):
            return {"started": True, "package_path": package_path}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(
        mcp_server,
        "_rpa_run_navigation_component",
        lambda *args, **kwargs: {"executed": True, "navigation": {"step_id": "navigate-here"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: {"takeover_started": True},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_open_browser",
        {"project_ref": "FLOW_REF", "allow_write": True},
    )

    assert result["opened"] is True
    assert result["navigation_run"]["navigation"]["step_id"] == "navigate-here"
    assert result["capture_started"] is True
    assert result["next_action"]["tool"] == "shizai_rpa_capture_windows"
    assert result["next_action"]["arguments"]["project_ref"] == "FLOW_REF"


def test_mcp_navigation_step_is_resolved_from_intent_mapping_not_position(monkeypatch):
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *_args: {
            "flow": {
                "request_analysis": {
                    "operations": [
                        {"id": "op-prepare", "type": "transform"},
                        {"id": "op-navigate", "type": "navigate"},
                    ]
                },
                "component_plan": [
                    {"id": "prepare", "component": "Prepare_Component", "operation_ids": ["op-prepare"]},
                    {
                        "id": "navigate-here",
                        "component": "Browser_Component_From_Live_Catalog",
                        "operation_ids": ["op-navigate"],
                        "purpose": "打开请求目标",
                    },
                ],
                "steps": [
                    {"id": "prepare", "component": "Prepare_Component"},
                    {"id": "navigate-here", "component": "Browser_Component_From_Live_Catalog"},
                ],
            }
        },
    )

    result = mcp_server._rpa_navigation_step(object(), "FLOW_REF")

    assert result == {
        "step_id": "navigate-here",
        "position": 2,
        "component": "Browser_Component_From_Live_Catalog",
        "operation_ids": ["op-navigate"],
        "purpose": "打开请求目标",
    }


def test_mcp_navigation_run_starts_debug_at_resolved_position_and_steps_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server,
        "_rpa_navigation_step",
        lambda *_args: {
            "step_id": "navigate-here",
            "position": 2,
            "component": "Browser_Component_From_Live_Catalog",
            "operation_ids": ["op-navigate"],
            "purpose": "打开请求目标",
        },
    )
    monkeypatch.setattr(
        mcp_server.debug_core,
        "debug_capabilities",
        lambda *_args: {"active_session": False},
    )

    def fake_start(*_args, **kwargs):
        calls.append(("start", kwargs["start_from"]))
        return {"status": "paused", "current": {"component_id": "navigate-here"}}

    def fake_command(_paths, _project_ref, operation, **_kwargs):
        calls.append(("command", operation))
        return {
            "status": "paused",
            "current": {
                "component_id": "after-navigation",
                "component_sequence": 3,
                "component_boundary": True,
            },
        }

    monkeypatch.setattr(mcp_server.debug_core, "debug_start", fake_start)
    monkeypatch.setattr(mcp_server.debug_core, "debug_command", fake_command)

    result = mcp_server._rpa_run_navigation_component(object(), object(), "FLOW_REF")

    assert calls == [("start", 2), ("command", "next")]
    assert result["method"] == "designer_dap_single_step"
    assert result["navigation"]["step_id"] == "navigate-here"
    assert result["stopped_before_component"] == "after-navigation"


def test_mcp_navigation_run_rejects_non_boundary_stop(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_rpa_navigation_step",
        lambda *_args: {
            "step_id": "navigate-here",
            "position": 2,
            "component": "Browser_Component_From_Live_Catalog",
            "operation_ids": ["op-navigate"],
            "purpose": "打开请求目标",
        },
    )
    monkeypatch.setattr(mcp_server.debug_core, "debug_capabilities", lambda *_args: {"active_session": False})
    monkeypatch.setattr(
        mcp_server.debug_core,
        "debug_start",
        lambda *_args, **_kwargs: {"status": "paused", "current": {"component_id": "navigate-here"}},
    )
    monkeypatch.setattr(
        mcp_server.debug_core,
        "debug_command",
        lambda *_args, **_kwargs: {
            "status": "paused",
            "current": {"component_id": "navigate-here", "component_sequence": 2},
        },
    )

    with pytest.raises(mcp_server.ShizaiCloudError, match="exact component boundary"):
        mcp_server._rpa_run_navigation_component(object(), object(), "FLOW_REF")


def test_mcp_capture_windows_recovers_stale_session_without_red_picker():
    calls = []

    class FakeBridge:
        def capture_windows(self, *, include_non_chrome=False):
            calls.append(("capture", include_non_chrome))
            if len([item for item in calls if item[0] == "capture"]) == 1:
                raise mcp_server.ShizaiCloudError("DeepCapture connection timed out")
            return {"count": 1, "windows": [{"element_id": "wnd:1"}]}

        def start_capture_session(self, *, package_path=None):
            calls.append(("start_session", package_path))
            return {"inspect_started": False, "overlay_state": "inactive"}

    result = mcp_server._rpa_capture_windows_with_recovery(
        FakeBridge(),
        project_ref="FLOW_REF",
        include_non_chrome=False,
    )

    assert calls == [("capture", False), ("start_session", "FLOW_REF"), ("capture", False)]
    assert result["count"] == 1
    assert result["capture_session_recovery"]["recovered"] is True
    assert result["capture_session_recovery"]["red_picker_started"] is False


def test_mcp_native_ui_cleanup_closes_every_surface_and_clears_state(monkeypatch):
    events = []

    class FakeSmallCard:
        def __init__(self, client=None):
            pass

        def close(self):
            events.append("small_card")
            return {"visible": False}

    class FakeFrame:
        def __init__(self, client=None):
            pass

        def close(self):
            events.append("takeover_frame")
            return {"visible": False}

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, *, force=True):
            events.append(("native_mask", force))
            return {"visible": False}

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def stop_inspect(self):
            events.append("inspect")
            return {"overlay_state": "inactive"}

    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeSmallCard)
    monkeypatch.setattr(mcp_server, "NativeTakeoverFrame", FakeFrame)
    monkeypatch.setattr(mcp_server, "NativeMask", FakeMask)
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "_LATEST_RPA_DESIGN_REF", "FLOW_REF")
    mcp_server._ACTIVE_RPA_DESIGN_UI["FLOW_REF"] = {"takeover_started": True}

    result = mcp_server._rpa_cleanup_native_ui(object())

    assert events == ["small_card", "takeover_frame", ("native_mask", True), "inspect"]
    assert result["small_card"]["visible"] is False
    assert mcp_server._LATEST_RPA_DESIGN_REF is None
    assert mcp_server._ACTIVE_RPA_DESIGN_UI == {}


@pytest.mark.parametrize("capture_fails", [False, True])
def test_mcp_capture_elements_always_closes_picker_and_native_mask(monkeypatch, capture_fails):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def capture_elements(self, element_ids, **_kwargs):
            events.append(("capture", list(element_ids)))
            if capture_fails:
                raise ValueError("capture failed")
            return {"elements": [{"id": "search-selector"}]}

        def stop_inspect(self):
            events.append(("inspect_stop",))
            return {"overlay_state": "inactive"}

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, *, force=False, request_id=None):
            events.append(("mask_close", force))
            return {"visible": False}

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", FakeMask)
    monkeypatch.setattr(
        mcp_server,
        "_rpa_unbound_element_step",
        lambda *_args: {
            "step_id": "search-input",
            "component": "Element_SetValue_V6",
            "element_role": "搜索框",
            "binding_status": "unbound",
        },
    )
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        mcp_server.project_core,
        "import_elements",
        lambda *_args, **_kwargs: events.append(("import",)) or {"imported": ["search-selector"]},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "set_rpa_step_elements",
        lambda *_args, **_kwargs: events.append(("bind",)) or {"binding_status": "bound"},
    )

    arguments = {
        "project_ref": "FLOW_REF",
        "step_id": "search-input",
        "element_ids": ["uia:search"],
        "allow_write": True,
    }
    if capture_fails:
        with pytest.raises(ValueError, match="capture failed"):
            mcp_server._dispatch("shizai_rpa_capture_elements", arguments)
        assert events == [("capture", ["uia:search"]), ("inspect_stop",), ("mask_close", True)]
    else:
        result = mcp_server._dispatch("shizai_rpa_capture_elements", arguments)
        assert events == [
            ("capture", ["uia:search"]),
            ("import",),
            ("bind",),
            ("inspect_stop",),
            ("mask_close", True),
        ]
        assert result["native_ui"]["inspect_stop"]["overlay_state"] == "inactive"
        assert result["native_ui"]["native_mask"]["visible"] is False


def test_mcp_capture_elements_forwards_explicit_rebind(monkeypatch):
    calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def capture_elements(self, element_ids, **_kwargs):
            return {"elements": [{"id": str(element_ids[0])}]}

        def stop_inspect(self):
            return {"overlay_state": "inactive"}

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, *, force=False, request_id=None):
            return {"visible": False}

    def target_step(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "step_id": "search-input",
            "component": "Element_SetValue_V6",
            "element_role": "搜索框",
            "binding_status": "bound",
            "rebind": True,
        }

    monkeypatch.setattr(mcp_server, "_paths", lambda: object())
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeMask", FakeMask)
    monkeypatch.setattr(mcp_server, "_rpa_unbound_element_step", target_step)
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_update", lambda *_args, **_kwargs: {})

    result = mcp_server._dispatch(
        "shizai_rpa_capture_elements",
        {
            "step_id": "search-input",
            "element_ids": ["uia:search"],
            "allow_rebind": True,
            "allow_write": True,
        },
    )

    assert calls == [{"allow_rebind": True}]
    assert result["rebind"] is True


def test_mcp_server_exposes_a_compact_consistent_contract():
    server = mcp_server.build_server()
    tools = mcp_server._schemas()
    names = [tool.name for tool in tools]
    schemas = {tool.name: tool for tool in tools}

    assert len(names) == 56
    assert len(names) == len(set(names))
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert {
        "shizai_auth_status",
        "shizai_ui_feedback",
        "shizai_route_intent",
        "shizai_rpa_analyze_request",
        "shizai_rpa_validate_plan",
        "shizai_generation_context",
        "shizai_apply_flow",
        "shizai_rpa_start_design",
        "shizai_rpa_open_browser",
        "shizai_rpa_component_catalog",
        "shizai_rpa_add_step",
        "shizai_rpa_validate_flow",
        "shizai_rpa_materialize",
        "shizai_rpa_debug_command",
        "shizai_rpa_run",
        "shizai_rpa_status",
    }.issubset(names)
    assert {
        "shizai_rpc",
        "shizai_extension_call",
        "shizai_rpa_create_flow",
        "shizai_generate_rpa_flow",
        "shizai_rpa_enable_result_wait",
        "shizai_project_register",
    }.isdisjoint(names)
    assert "Agent graph only" in schemas["shizai_components"].description
    assert "shizai_rpa_component_catalog" in schemas["shizai_components"].description
    assert "verification_gate" in schemas["shizai_rpa_add_step"].description
    assert "verification_gate" in schemas["shizai_rpa_validate_step"].description

    source = ast.parse(textwrap.dedent(inspect.getsource(mcp_server._dispatch_impl)))
    handlers = {
        comparator.value
        for node in ast.walk(source)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name"
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    }
    assert set(names) == handlers

    encoded_tools = json.dumps([tool.model_dump(mode="json") for tool in tools], ensure_ascii=False)
    # Keep the advertised contract compact while allowing the native UI cleanup
    # controls (message, tips, request_id, and force) to be explicit.
    # Full mode now includes shared plan structure and versioned edit fields;
    # the optional authoring surface has a separate, smaller budget test.
    assert len(encoded_tools) <= 40_000
    assert len(mcp_server.MCP_INSTRUCTIONS) <= 2_100
    assert "薄 stdio 包装" in mcp_server.MCP_INSTRUCTIONS
    assert "verification_gate" in mcp_server.MCP_INSTRUCTIONS
    assert "debug_command(action=next)" in mcp_server.MCP_INSTRUCTIONS
    assert "才能追加下一组件" in mcp_server.MCP_INSTRUCTIONS
    assert "没有挂载 shizai-agent MCP" in mcp_server.MCP_INSTRUCTIONS
    assert "shizai-agent-mcp@latest install" in mcp_server.MCP_INSTRUCTIONS
    assert "禁止自行编写 MCP 客户端" in mcp_server.MCP_INSTRUCTIONS

    assert schemas["shizai_rpa_debug_command"].inputSchema["properties"]["action"]["enum"] == [
        "next",
        "continue",
        "step_in",
        "step_out",
    ]
    assert "allow_write" in schemas["shizai_run_draft"].inputSchema["properties"]
    start_schema = schemas["shizai_rpa_start_design"].inputSchema
    assert start_schema["required"] == ["query", "analysis", "plan", "allow_write"]
    assert {"target_url", "keyword", "use_js_table", "required_columns"}.isdisjoint(start_schema["properties"])
    assert start_schema["properties"]["open_browser"]["default"] is False
    assert start_schema["properties"]["reuse_existing"]["default"] is True
    assert start_schema["properties"]["force_new"]["default"] is False
    assert {"project_ref", "reuse_existing", "force_new"}.issubset(start_schema["properties"])
    assert "No site flow" in schemas["shizai_rpa_start_design"].description
    capture_schema = schemas["shizai_extension_start_capture"]
    assert "already-written unbound component" in capture_schema.description
    assert capture_schema.inputSchema["required"] == ["project_ref", "step_id", "allow_write"]
    assert capture_schema.inputSchema["properties"]["mode"]["enum"] == [1, 2]
    assert schemas["shizai_ui_feedback"].inputSchema["properties"]["action"]["enum"] == [
        "small_card",
        "mask",
        "scan_demo",
    ]
    ui_properties = schemas["shizai_ui_feedback"].inputSchema["properties"]
    assert {"message", "tips", "request_id", "force"}.issubset(ui_properties)

    catalog_schema = schemas["shizai_rpa_component_catalog"].inputSchema
    assert catalog_schema["properties"]["detail"]["enum"] == ["summary", "contract", "full"]
    assert catalog_schema["properties"]["scope"]["enum"] == ["builtin", "business", "all"]
    assert "business_category" in catalog_schema["properties"]
    assert "capability" in catalog_schema["properties"]
    assert catalog_schema["properties"]["capability_mode"]["enum"] == ["all", "any"]
    assert catalog_schema["properties"]["capability_mode"]["default"] == "any"
    assert "project_ref" in catalog_schema["properties"]
    assert len(server.request_handlers) == 3


def test_rpa_step_results_expose_incremental_verification_gate(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "incremental-gate"
    project.create_rpa_flow(
        str(staged),
        name="增量验证门",
        target_url="https://example.test/",
        use_excel_components=False,
    )

    added = project.add_rpa_step(
        paths,
        str(staged),
        "WebBrowser_CreateV3",
        step_id="open-browser",
    )
    gate = added["verification_gate"]
    assert gate["required"] is True
    assert gate["status"] == "runtime_pending"
    assert gate["step_id"] == "open-browser"
    assert gate["static_validation"]["valid"] is True
    assert gate["runtime_validation"]["verified"] is False
    assert gate["runtime_validation"]["mode"] == "design_validation"
    assert gate["next_action"]["tool"] == "shizai_rpa_debug_capabilities"
    assert "shizai_rpa_debug_command" in gate["runtime_validation"]["tools"]

    validated = project.validate_rpa_step(paths, str(staged), "open-browser")
    assert validated["verification_gate"]["status"] == "runtime_pending"
    assert validated["verification_gate"]["advance_only_when"] == [
        "current step static_validation.valid=true",
        "current step runtime_validation.verified=true",
    ]


def test_rpa_debug_verification_reuses_shared_gate(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "debug-gate"
    project.create_rpa_flow(
        str(staged),
        name="调试验证门",
        target_url="https://example.test/",
        use_excel_components=False,
    )
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open-browser")

    result = project.attach_rpa_debug_verification(
        paths,
        str(staged),
        {
            "kind": "native_rpa_debug_session",
            "runtime_verification": {
                "verified": True,
                "component_executed": True,
                "component_id": "open-browser",
                "component_sequence": 1,
                "evidence_kind": "dap_component_boundary",
            },
        },
    )

    assert result["verification_gate"]["status"] == "verified"
    assert result["verification_gate"]["runtime_validation"]["verified"] is True
    assert result["runtime_verification"]["static_validation_valid"] is True


def test_rpa_request_fingerprint_finds_matching_project(tmp_path):
    paths = _paths(tmp_path)
    first = paths.applications_root / "c_first"
    second = paths.applications_root / "c_second"
    for target in (first, second):
        target.mkdir(parents=True)
        (target / "szrpa.json").write_text(
            json.dumps(
                {
                    "packageName": "京东手机采集",
                    "mcpOriginalQuery": "请抓取京东手机销量",
                    "mcpTargetUrl": "https://search.jd.com/",
                    "mcpOutputFile": "京东手机销量.xlsx",
                    "mcpRequiredColumns": ["商品标题", "价格"],
                    "mcpMaxRows": 100,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    fingerprint = project.rpa_request_fingerprint(
        "请抓取京东手机销量",
        target_url="https://search.jd.com/",
        output_contract={"file": "京东手机销量.xlsx", "columns": ["商品标题", "价格"], "max_rows": 100},
    )
    found = project.find_rpa_project_by_fingerprint(paths, fingerprint)
    assert found is not None
    assert found["fingerprint_source"] == "derived"
    assert found["project_ref"] in {str(first.resolve()), str(second.resolve())}
    assert project.rpa_request_fingerprint(
        "请抓取京东手机销量",
        target_url="https://search.jd.com/",
        output_contract={"file": "另一个结果名.xlsx", "columns": ["商品标题", "价格"], "max_rows": 100},
    ) == fingerprint


def test_mcp_start_design_reuses_matching_project_without_creating(monkeypatch, tmp_path):
    query, analysis, plan = _generic_browser_plan_fixture()
    paths = _paths(tmp_path)
    calls = []
    monkeypatch.setattr(mcp_server, "_paths", lambda: paths)
    monkeypatch.setattr(mcp_server.project_core, "analyze_rpa_request", lambda value: analysis)
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda _paths: {item["component"]: {} for item in plan["steps"]},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "find_rpa_project_by_fingerprint",
        lambda _paths, _fingerprint: {
            "project_ref": str(paths.applications_root / "existing"),
            "fingerprint_source": "explicit",
        },
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *_args, **_kwargs: {"flow": {"next_action": {"tool": "shizai_rpa_status"}}},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "start_rpa_design",
        lambda *_args, **_kwargs: calls.append("created") or {"project_ref": "unexpected"},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {"query": query, "analysis": analysis, "plan": plan, "allow_write": True},
    )

    assert result["reused"] is True
    assert result["created"] is False
    assert result["project_ref"].endswith("existing")
    assert result["next_action"]["tool"] == "shizai_rpa_status"
    assert calls == []


def test_mcp_start_design_force_new_bypasses_matching_project(monkeypatch):
    query, analysis, plan = _generic_browser_plan_fixture()
    calls = []
    monkeypatch.setattr(mcp_server.project_core, "analyze_rpa_request", lambda value: analysis)
    monkeypatch.setattr(
        mcp_server.component_catalog_core,
        "builtin_component_index",
        lambda _paths: {item["component"]: {} for item in plan["steps"]},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "find_rpa_project_by_fingerprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("force_new must skip lookup")),
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "start_rpa_design",
        lambda *_args, **kwargs: calls.append(kwargs) or {"project_ref": "CLIENT_APPLICATION_PATH"},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "attach_rpa_execution_plan",
        lambda *_args, **_kwargs: {"next_action": {"tool": "shizai_rpa_add_step"}},
    )
    monkeypatch.setattr(mcp_server.project_core, "rpa_components", lambda *_args, **_kwargs: {"components": []})

    result = mcp_server._dispatch(
        "shizai_rpa_start_design",
        {
            "query": query,
            "analysis": analysis,
            "plan": plan,
            "allow_write": True,
            "force_new": True,
        },
    )

    assert result["reused"] is False
    assert calls[0]["request_fingerprint"]


def test_native_component_catalog_searches_installed_dictionary(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    (engine / "311" / "packages" / "WebBrowser" / "py").mkdir(parents=True)
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCrawl",
                    "label": "数据采集",
                    "category": "get_web_data",
                    "description": "采集网页数据并输出到 Excel",
                    "baseType": "WebBrowser_DataCrawl",
                    "version": "3.3.7.0",
                    "componentMode": ["normal"],
                    "tag": [{"label": "采集"}],
                    "definition": {
                        "args0": [
                            {"name": "var_name", "label": "输出到变量", "defaultValue": "结果", "isExport": 3},
                            {"name": "toExcel", "label": "输出到 Excel", "defaultValue": "0", "isExport": 1},
                        ],
                        "options0": [],
                    },
                    "codeDesc": [{"func": "WebBrowser.DataCrawl", "assignTo": "var_name"}],
                },
                {"type": "Window_Close", "label": "关闭窗口", "category": "window", "baseType": "Window_Close"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        engine_root=str(engine),
        query="Excel",
        detail="contract",
        limit=5,
    )
    assert result["ready"] is True
    assert result["matched_components"] == 1
    item = result["components"][0]
    assert item["type"] == "WebBrowser_DataCrawl"
    assert item["package"] == "WebBrowser"
    assert item["outputs"]["primary"] == "var_name"
    assert [field["name"] for field in item["inputs"]] == ["var_name", "toExcel"]
    assert result["usage"]["implementation_policy"]["default_strategy"] == "component_first"
    policy = result["usage"]["implementation_policy"]
    assert policy["enforcement"] == "advisory_only"
    assert "WebBrowser_DataCollectionV5" in policy["rule"]
    assert "不因未选原生组件而硬失败" in policy["fallback_rule"]


def test_component_catalog_infers_collection_and_click_capabilities_from_contracts(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "WebBrowser_DataCollectionV4",
                    "label": "数据采集",
                    "category": ["data_parse", "get_web_data"],
                    "definition": {
                        "args0": [
                            {"name": "var0", "isExport": 3},
                            {"name": "config"},
                            {"name": "region"},
                            {"name": "collect_items"},
                            {"name": "page_type"},
                            {"name": "selector_page"},
                            {"name": "selector_more"},
                            {"name": "deduplication"},
                            {"name": "toExcel"},
                            {"name": "filename_suffix"},
                            {"name": "filePath"},
                        ],
                        "options0": [],
                    },
                    "codeDesc": [{"func": "WebBrowser.DataCollectionV4", "args": [], "assignTo": "var0"}],
                },
                {
                    "type": "Element_Click_V6",
                    "label": "点击元素",
                    "category": "interface_element",
                    "definition": {"args0": [{"name": "uiSelector"}], "options0": []},
                    "codeDesc": [{"func": "Element.Click_V6", "args": ["uiSelector"]}],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="extract,paginate,output",
        engine_root=str(engine),
        detail="contract",
    )

    assert [item["type"] for item in result["components"]] == ["WebBrowser_DataCollectionV4"]
    collection = result["components"][0]
    assert {"extract", "limit", "paginate", "output"}.issubset(collection["capabilities"])
    assert collection["operation_types"] == ["extract", "output", "transform"]
    assert collection["requires_page_evidence"] is True
    assert collection["deferred_bindings"] == ["config", "selector_page", "selector_more"]
    assert collection["configuration_hints"]["row_limit_parameters"] == ["collect_items"]
    assert collection["planning_patterns"][0]["semantic_config"] == {
        "region": 3,
        "collect_items": "$output_contract.max_rows",
        "page_type": "$choose_from_live_page_evidence",
        "selector_page": "$defer_until_next_page_element_is_captured",
        "selector_more": "$defer_until_load_more_element_is_captured",
    }
    guidance = result["planning_guidance"]["collection"]
    assert guidance["required_capabilities"] == ["extract", "limit", "output", "paginate"]
    assert guidance["candidates"][0]["component"] == "WebBrowser_DataCollectionV4"
    assert guidance["candidates"][0]["outputs"]["primary"] == "var0"
    assert "exactly match" in guidance["candidates"][0]["output_binding_rule"]
    assert "numeric row target" in guidance["selection_rule"]

    ranked = component_catalog.component_catalog(
        paths,
        scope="builtin",
        capability="navigate,input,submit,extract,limit,paginate,output",
        capability_mode="any",
        engine_root=str(engine),
        detail="contract",
    )
    assert ranked["components"][0]["type"] == "WebBrowser_DataCollectionV4"


def test_native_component_catalog_reports_missing_designer(tmp_path):
    result = component_catalog.component_catalog(
        _paths(tmp_path), query="网页", engine_root=str(tmp_path / "missing-engine")
    )
    assert result["ready"] is False
    assert result["components"] == []
    assert result["source"]["source"] == "not_found"


def test_builtin_component_index_returns_exact_types_and_contracts(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "Demo_Action_V2",
                    "label": "演示动作",
                    "category": "base_command",
                    "version": "3.3.7.0",
                    "definition": {
                        "args0": [{"name": "result", "label": "输出", "isExport": 3}],
                        "options0": [],
                    },
                    "codeDesc": [{"func": "Demo.Action", "assignTo": "result"}],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    index = component_catalog.builtin_component_index(paths, engine_root=str(engine))
    assert index["Demo_Action_V2"]["version"] == "3.3.7.0"
    assert index["Demo_Action_V2"]["outputs"]["primary"] == "result"
    assert "logic_control_var" in index
    assert "base_command_custom" in index


def test_component_catalog_adds_designer_static_entries(tmp_path):
    paths = _paths(tmp_path)
    engine = tmp_path / "rpa-engine"
    dictionary = engine / "componentDicts" / "componentsNew.json"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text(
        json.dumps(
            [
                {
                    "type": "Basic_SetVariable",
                    "label": "设置变量",
                    "category": "base_command",
                    "baseType": "Basic_SetVariable",
                    "definition": {"args0": [], "options0": []},
                }
            ]
        ),
        encoding="utf-8",
    )
    result = component_catalog.component_catalog(
        paths,
        scope="builtin",
        engine_root=str(engine),
        detail="summary",
        limit=10,
    )
    assert result["ready"] is True
    assert result["counts"]["builtin"]["engine_records"] == 1
    assert result["counts"]["builtin"]["designer_static_extra"] == 2
    assert result["total_components"] == 3
    static = {item["type"]: item for item in result["components"]}
    assert static["logic_control_var"]["designer_static"] is True
    assert static["base_command_custom"]["designer_static"] is True


def test_component_catalog_reports_business_package_and_project_import(tmp_path):
    paths = _paths(tmp_path)
    paths.data_root.mkdir(parents=True)
    (paths.data_root / "bizComponentInfo.json").write_text(
        json.dumps(
            {
                "bizComponentInfo": [
                    {
                        "typeName": "京东",
                        "childList": [
                            {
                                "typeName": "京准通-互动广告",
                                "releaseComponentTotal": 3,
                                "childList": [
                                    {
                                        "componentCode": "jd-test",
                                        "componentPackageCode": "jd-package",
                                        "componentPackageVersion": "1.0.0",
                                        "componentVersion": "1.0.0",
                                        "typeCode": "jd-type",
                                        "typeName": "京准通-互动广告-采集",
                                        "typeInfo": json.dumps(
                                            {
                                                "componentName": "京准通-互动广告-采集",
                                                "componentDesc": "采集京东广告数据",
                                                "componentIllustrateUrl": "https://example.invalid/doc",
                                            },
                                            ensure_ascii=False,
                                        ),
                                        "componentSourceInfo": json.dumps(
                                            {
                                                "componentMode": ["normal"],
                                                "definition": {"args0": [], "options0": []},
                                            }
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache = paths.data_root / "child-module-packages" / "biz" / "account"
    (cache / "jd-package" / "1.0.0").mkdir(parents=True)
    (cache / "jd-package" / "1.0.0" / "jd-test.task").write_text(
        json.dumps({"componentModelEntities": {"MAIN_COMPONENT": {"type": "main"}}}),
        encoding="utf-8",
    )
    target = _make_project(paths, "business-project")
    (target / "projects" / "dependency_modules" / "jd-package").mkdir(parents=True)

    result = component_catalog.component_catalog(
        paths,
        scope="business",
        business_category="京东",
        project_ref=str(target),
        detail="contract",
        limit=10,
    )
    assert result["ready"] is True
    assert result["advertised_component_total"] == 3
    assert result["matched_components"] == 1
    assert result["business_categories"][0]["advertised"] == 3
    assert result["business_packages"][0]["availability"] == "project_imported"
    assert result["business_packages"][0]["requires_import"] is False
    item = result["components"][0]
    assert item["scope"] == "business"
    assert item["type"] == "TASK_COMPONENT_jd-test"
    assert item["project_imported"] is True
    assert item["requires_import"] is False
    assert item["materializable"] is True
    assert item["contract_source"] == "cached_task"


def test_component_catalog_business_marks_missing_package_as_import_required(tmp_path):
    paths = _paths(tmp_path)
    paths.data_root.mkdir(parents=True)
    (paths.data_root / "bizComponentInfo.json").write_text(
        json.dumps(
            {
                "bizComponentInfo": [
                    {
                        "typeName": "AI",
                        "childList": [
                            {
                                "typeName": "AI扩展",
                                "releaseComponentTotal": 1,
                                "childList": [
                                    {
                                        "componentCode": "ai-test",
                                        "componentPackageCode": "missing-package",
                                        "componentPackageVersion": "2.0.0",
                                        "typeCode": "ai-type",
                                        "typeName": "AI-测试组件",
                                        "typeInfo": "{}",
                                        "componentSourceInfo": "{}",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    target = _make_project(paths, "missing-business-project")
    result = component_catalog.component_catalog(
        paths,
        scope="business",
        project_ref=str(target),
        detail="summary",
        limit=10,
    )
    assert result["components"][0]["availability"] == "catalog_only"
    assert result["components"][0]["requires_import"] is True
    assert result["business_packages"][0]["requires_import"] is True


def test_native_output_summary_reports_csv_columns_and_freshness(tmp_path):
    project_dir = tmp_path / "native"
    output = project_dir / "res" / "products.csv"
    output.parent.mkdir(parents=True)
    output.write_text("商品名称,价格\n手机,1999\n", encoding="utf-8")
    summary = project._native_output_summary(
        project_dir,
        output,
        {"submitted_at": output.stat().st_mtime - 1},
    )
    assert summary["format_valid"] is True
    assert summary["columns"] == ["商品名称", "价格"]
    assert summary["fresh_for_run"] is True


def test_native_status_reports_empty_required_xlsx_column_without_cell_values(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "xlsx-quality")
    columns = ["商品名称", "商家名称", "价格", "销量", "商品链接"]
    (target / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "XLSX quality",
                "mcpOutputFile": "products.xlsx",
                "mcpRequiredColumns": columns,
                "mcpExcelMode": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    output = target / "res" / "products.xlsx"
    output.parent.mkdir(parents=True)
    rows = [
        columns,
        ["Phone A", "", "1999", "100", "https://item.example/a"],
        ["Phone B", "", "2999", "200", "https://item.example/b"],
    ]
    sheet_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(64 + column_index)}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column_index, value in enumerate(values, start=1)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        )
        archive.writestr(
            "xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(sheet_rows)
            + "</sheetData></worksheet>",
        )
    project._write_native_run_state(
        paths,
        target,
        {
            "status": "submitted",
            "submitted_at": output.stat().st_mtime - 0.1,
            "uuid": "xlsx-quality",
            "workflow_run_id": "xlsx-quality-run",
        },
    )

    result = project.native_rpa_status(paths, str(target))

    quality = result["output"]["quality"]
    assert result["status"] == "failed"
    assert result["failure_kind"] == "empty_required_columns"
    assert result["output"]["data_row_count"] == 2
    assert quality["empty_required_columns"] == ["商家名称"]
    assert quality["column_non_empty_rows"]["商家名称"] == 0
    assert quality["complete_required_row_count"] == 0
    assert "商家名称" in result["output"]["quality_warning"]
    assert "Phone A" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("case", "max_rows", "links", "failure_kind"),
    [
        (
            "row-count",
            10,
            ["https://shop.example/products/100001", "https://shop.example/products/100002"],
            "output_row_count_mismatch",
        ),
        (
            "invalid-link",
            2,
            ["https://shop.example/products/100001", "https://shop.example/search?keyword=phone"],
            "invalid_links",
        ),
        (
            "row-count-above-minimum",
            1,
            ["https://shop.example/products/100001", "https://shop.example/products/100002"],
            None,
        ),
    ],
)
def test_native_status_enforces_minimum_row_count_and_detail_links(tmp_path, case, max_rows, links, failure_kind):
    paths = _paths(tmp_path)
    target = _make_project(paths, f"xlsx-{case}")
    columns = ["商品标题", "价格", "商品链接"]
    (target / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "XLSX output contract",
                "mcpOutputFile": "products.xlsx",
                "mcpRequiredColumns": columns,
                "mcpMaxRows": max_rows,
                "mcpExcelMode": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    output = target / "res" / "products.xlsx"
    output.parent.mkdir(parents=True)
    rows = [columns, ["Phone A", "1999", links[0]], ["Phone B", "2999", links[1]]]
    sheet_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(64 + column_index)}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column_index, value in enumerate(values, start=1)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        )
        archive.writestr(
            "xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(sheet_rows)
            + "</sheetData></worksheet>",
        )
    project._write_native_run_state(
        paths,
        target,
        {
            "status": "submitted",
            "submitted_at": output.stat().st_mtime - 0.1,
            "uuid": case,
            "workflow_run_id": f"{case}-run",
        },
    )

    result = project.native_rpa_status(paths, str(target))

    if failure_kind:
        assert result["status"] == "failed"
        assert result["failure_kind"] == failure_kind
    else:
        assert result["status"] == "succeeded"
        assert result["failure_kind"] is None
    assert result["output"]["expected_data_row_count"] == max_rows


def test_native_status_exposes_missing_output_and_runtime_ui_feedback(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    target = _make_project(paths, "missing-output")
    monkeypatch.setattr(
        project,
        "native_rpa_sync_diagnose",
        lambda _paths, _project_ref: {"can_run": True, "runtime": {"ready": True}},
    )
    monkeypatch.setattr(
        project,
        "_native_rpa_state",
        lambda _paths, _project_ref: {
            "output_file": "products.xlsx",
            "output_path": str(tmp_path / "res" / "products.xlsx"),
            "required_columns": ["商品名称"],
            "steps": [],
            "target_url": "https://www.jd.com/",
        },
    )
    _write_log = paths.data_root / "logs" / "2026-08-30" / "output-logs" / "run_2500_fixture.jsonl"
    _write_log.parent.mkdir(parents=True)
    _write_log.write_text(
        json.dumps({"timestamp": 2_500, "data": {"runStatus": "success"}}) + "\n",
        encoding="utf-8",
    )
    project._write_native_run_state(
        paths,
        target,
        {"status": "submitted", "submitted_at": 2, "uuid": "fixture", "workflow_run_id": "fixture-run"},
    )

    result = project.native_rpa_status(paths, str(target))

    assert result["status"] == "failed"
    assert result["failure_kind"] == "missing_fresh_output"
    assert result["next_tool"] == "shizai_rpa_runtime_preflight"
    assert result["ui_feedback"]["phase"] == "runtime_failed"
    assert result["output"]["exists"] is False
    assert result["output"]["size"] == 0
    assert result["output_files"][0]["exists"] is False
    assert "正在操作你的电脑" not in result["ui_feedback"]["message"]
    assert "没有发现本次运行生成的新输出文件" in result["ui_feedback"]["message"]


def test_native_status_blocks_when_runtime_plugin_is_missing(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "blocked-native")
    (target / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    log_path = paths.data_root / "logs" / "2026-08-27" / "dll_logs" / "II.RPA.RuntimeHost.txt"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("检测到未安装【实在智能 RPA 插件】", encoding="utf-8")
    project._write_native_run_state(
        paths,
        target,
        {
            "status": "submitted",
            "submitted_at": 9999999999,
            "uuid": "blocked-test",
            "workflow_run_id": "blocked-run",
        },
    )
    result = project.native_rpa_status(paths, str(target))
    assert result["status"] == "blocked"
    assert result["diagnosis"]["runtime"]["ready"] is False
    assert result["diagnosis"]["runtime"]["runtime_browser_plugin_status"] == "not_installed"
    assert result["diagnosis"]["runtime"]["runtime_browser_plugin_error_code"] == "010400100000964"
    assert result["next_tool"] == "shizai_rpa_sync_diagnose"


def test_native_runtime_plugin_blocker_detects_only_new_chrome_failure(tmp_path):
    paths = _paths(tmp_path)
    log_path = paths.data_root / "logs" / "2026-09-01" / "dll_logs" / "II.RPA.RuntimeHost.txt"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("historical startup\n", encoding="utf-8")
    baseline = project._native_runtime_log_snapshot(paths)

    assert project._native_runtime_plugin_blocker(paths, baseline) is None

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("[010400100000996]检测到未启用【实在智能 RPA 插件】\n")
        handle.write("[010400100000964]检测到未安装【实在智能 RPA 插件】\n")

    blocker = project._native_runtime_plugin_blocker(paths, baseline)
    assert blocker is not None
    assert blocker["kind"] == "runtime_plugin_unavailable"
    assert blocker["status"] == "disabled"
    assert blocker["error_code"] == "010400100000996"
    assert blocker["log_path"] == str(log_path)


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        ("login_required", "login"),
        ("page_challenge", "complete_challenge"),
    ],
)
def test_native_runtime_plugin_blocker_prioritizes_user_interaction_marker(tmp_path, reason, action):
    paths = _paths(tmp_path)
    log_path = paths.data_root / "logs" / "2026-09-01" / "output-logs" / "run_101_fixture.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(json.dumps({"data": {"runStatus": "started"}}) + "\n", encoding="utf-8")
    baseline = project._native_runtime_log_snapshot(paths)

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"SHIZAI_WAITING_FOR_USER:{reason}\n")
        handle.write('{"data":{"engineAction":"error","runStatus":"error"}}\n')

    signal = project._native_runtime_plugin_blocker(paths, baseline)
    assert signal is not None
    assert signal["kind"] == "user_interaction_required"
    assert signal["status"] == "waiting_for_user"
    assert signal["reason"] == reason
    assert signal["interaction"]["action"] == action
    assert signal["interaction"]["automation_may_login"] is False


def test_native_run_status_preserves_waiting_for_user_over_error_terminal(tmp_path):
    run_state = {
        "status": "waiting_for_user",
        "terminal_status": "error",
        "interaction_reason": "login_required",
    }
    assert project._native_run_status(run_state, None, ["run-error"]) == "waiting_for_user"


@pytest.mark.parametrize(
    ("payload", "kind", "terminal_status"),
    [
        ({"data": {"engineAction": "error", "runStatus": "error"}}, "runtime_execution_failed", "error"),
        ({"data": {"engineAction": "run", "runStatus": "success"}}, "runtime_completed", "success"),
    ],
)
def test_native_runtime_log_signal_detects_new_business_terminal(tmp_path, payload, kind, terminal_status):
    paths = _paths(tmp_path)
    log_path = paths.data_root / "logs" / "2026-09-01" / "output-logs" / "run_100_fixture.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(json.dumps({"data": {"runStatus": "started"}}) + "\n", encoding="utf-8")
    baseline = project._native_runtime_log_snapshot(paths)

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    signal = project._native_runtime_plugin_blocker(paths, baseline)
    assert signal is not None
    assert signal["kind"] == kind
    assert signal["terminal_status"] == terminal_status


def test_native_run_publishes_blocked_feedback_before_rejecting_unready_flow(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    target = _make_project(paths, "blocked-before-run")
    feedback = []
    monkeypatch.setattr(
        project,
        "native_rpa_sync_diagnose",
        lambda _paths, _project_ref: {"can_run": False, "errors": ["elementMap is empty"], "warnings": []},
    )
    monkeypatch.setattr(
        project,
        "_publish_rpa_ui",
        lambda *args, **kwargs: (
            feedback.append((args, kwargs)) or {"phase": args[0], "animation": kwargs.get("animation")}
        ),
    )

    with pytest.raises(ValueError, match="not ready to run"):
        project.native_rpa_run(paths, str(target), client=object())

    assert feedback
    args, kwargs = feedback[-1]
    assert args[:3] == (
        "runtime_blocked",
        "computer_control",
        "运行已暂停：运行环境未就绪，暂未操作电脑。elementMap is empty",
    )
    assert kwargs["animation"] == "native_rpa_blocked"
    assert kwargs["status"] == "blocked"
    assert kwargs["failure_kind"] == "runtime_not_ready"

    persisted = project._read_native_run_state(paths, target)
    assert persisted["status"] == "blocked"
    assert persisted["failure_kind"] == "runtime_not_ready"


def test_mcp_write_tools_require_explicit_allow_write():
    with pytest.raises(PermissionError, match="shizai_rpa_start_design"):
        mcp_server._dispatch("shizai_rpa_start_design", {})
    with pytest.raises(PermissionError, match="shizai_run_draft"):
        mcp_server._dispatch("shizai_run_draft", {"query": "test"})
    with pytest.raises(PermissionError, match="shizai_rpa_debug_command"):
        mcp_server._dispatch(
            "shizai_rpa_debug_command",
            {"project_ref": "ignored", "action": "next"},
        )
    with pytest.raises(PermissionError, match="shizai_rpa_run"):
        mcp_server._dispatch("shizai_rpa_run", {"project_ref": "ignored"})
    with pytest.raises(PermissionError, match="shizai_rpa_stop"):
        mcp_server._dispatch("shizai_rpa_stop", {"project_ref": "ignored"})
    with pytest.raises(PermissionError, match="shizai_ui_feedback"):
        mcp_server._dispatch("shizai_ui_feedback", {"action": "scan_demo"})


def test_mcp_rpa_run_uses_direct_cli_without_design_time_page_scan(monkeypatch):
    runtime_calls = []
    page_scan_calls = []
    ui_cleanup_calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def activate_runtime_extension(self):
            return {"attempted": False, "ready": True}

        def stop_inspect(self):
            return {"overlay_state": "inactive"}

        def stop_capture(self, *, package_path=None):
            return {"stopped": True}

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            return kwargs

        def update(self, **kwargs):
            return kwargs

        def countdown(self, **kwargs):
            return kwargs

    class FakeMask:
        def __init__(self, client=None):
            pass

        def close(self, **kwargs):
            ui_cleanup_calls.append(("mask", kwargs))
            return {"visible": False}

    class FakeFrame:
        def __init__(self, client=None):
            pass

        def show(self):
            return {"visible": True}

        def close(self):
            ui_cleanup_calls.append(("frame", {}))
            return {"visible": False}

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(mcp_server, "NativeMask", FakeMask)
    monkeypatch.setattr(mcp_server, "NativeTakeoverFrame", FakeFrame)
    monkeypatch.setattr(
        mcp_server.debug_core,
        "runtime_preflight",
        lambda *args, **kwargs: page_scan_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_sync_diagnose",
        lambda *args, **kwargs: {"can_run": True, "materialization_required": False},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: (
            runtime_calls.append((args, kwargs)) or {"started": True, "status": {"status": "succeeded"}}
        ),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "allow_write": True},
    )

    assert len(runtime_calls) == 1
    assert page_scan_calls == []
    assert result["started"] is True
    assert result["preflight"]["mode"] == "direct_cli"
    assert result["preflight"]["page_scan_performed"] is False
    assert result["preflight"]["capture_session_started"] is False
    assert result["preflight"]["deep_capture_touched"] is False
    assert ui_cleanup_calls == [
        ("frame", {}),
        ("mask", {"force": True}),
        ("mask", {"force": True}),
        ("frame", {}),
        ("mask", {"force": True}),
    ]
    assert result["native_ui"]["final_cleanup"]["native_mask"] == {"visible": False}
    assert result["native_ui"]["final_cleanup"]["takeover_frame"] == {"visible": False}


def test_mcp_rpa_run_dispatch_always_cleans_native_ui_after_exception(monkeypatch):
    events = []
    client = object()

    def fail_dispatch(name, args):
        events.append(("dispatch", name, args["project_ref"]))
        raise RuntimeError("runtime fixture failed")

    monkeypatch.setattr(mcp_server, "_dispatch_impl", fail_dispatch)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: client)
    monkeypatch.setattr(
        mcp_server,
        "_rpa_cleanup_native_ui",
        lambda active_client=None: events.append(("cleanup", active_client)) or {"closed": True},
    )

    with pytest.raises(RuntimeError, match="runtime fixture failed"):
        mcp_server._dispatch(
            "shizai_rpa_run",
            {"project_ref": "CLIENT_APPLICATION_PATH", "allow_write": True},
        )

    assert events == [
        ("dispatch", "shizai_rpa_run", "CLIENT_APPLICATION_PATH"),
        ("cleanup", client),
    ]


def test_mcp_rpa_run_waits_for_disabled_extension_before_takeover(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def runtime_extension_status(self):
            return {
                "state": "installed_disabled",
                "disabled_extensions": [{"id": "pcopnibgkbdnlaeagepigbboebdfejmb"}],
            }

        def activate_runtime_extension(self):
            events.append("activate")
            return {"ready": False}

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            events.append(("card", kwargs["state"]))
            return kwargs

        def update(self, **kwargs):
            events.append(("card", kwargs["state"]))
            return kwargs

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *args, **kwargs: {
            "flow": {"steps": [{"component": "WebBrowser_CreateV3"}]},
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_rpa_design_ui_ensure_takeover",
        lambda *args, **kwargs: events.append("takeover") or {},
    )
    monkeypatch.setattr(
        mcp_server.debug_core,
        "runtime_preflight",
        lambda *args, **kwargs: events.append("preflight") or {},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "allow_write": True},
    )

    assert result["started"] is False
    assert result["status"] == "waiting_for_user"
    assert result["interaction_required"] is True
    assert result["failure_kind"] == "runtime_plugin_disabled"
    assert result["interaction"]["action"] == "enable_browser_extension"
    assert events == [("card", "validating"), ("card", "waiting")]


def test_mcp_rpa_run_blocks_before_runtime_when_native_host_is_offline(monkeypatch):
    runtime_calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def activate_runtime_extension(self):
            return {
                "attempted": True,
                "ready": False,
                "message": "Native Messaging Host 尚未上线。",
            }

        def start_capture_session(self, *, package_path=None):
            return {"started": True, "inspect_started": False}

        def stop_capture(self, *, package_path=None):
            return {"stopped": True}

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            return kwargs

        def update(self, **kwargs):
            return kwargs

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_sync_diagnose",
        lambda *args, **kwargs: {"can_run": True, "materialization_required": False},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *args, **kwargs: {"flow": {"steps": [{"component": "WebBrowser_CreateV3"}]}},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: runtime_calls.append((args, kwargs)),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "allow_write": True},
    )

    assert runtime_calls == []
    assert result["started"] is False
    assert result["status"] == "blocked"
    assert result["failure_kind"] == "runtime_plugin_unavailable"
    assert result["preflight"]["reason"] == "runtime_plugin_unavailable"
    assert result["next_action"]["tool"] == "shizai_extension_status"


def test_mcp_rpa_run_attempts_runtime_when_enabled_mv3_host_is_on_demand(monkeypatch):
    runtime_calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def activate_runtime_extension(self):
            return {
                "attempted": True,
                "ready": False,
                "after": {
                    "preferred_extension": {
                        "installed": True,
                        "enabled": True,
                        "manifest_version": 3,
                    }
                },
            }

        def start_capture_session(self, *, package_path=None):
            return {"started": True, "inspect_started": False}

        def stop_inspect(self):
            return {"overlay_state": "inactive"}

        def stop_capture(self, *, package_path=None):
            return {"stopped": True}

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            return kwargs

        def update(self, **kwargs):
            return kwargs

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_sync_diagnose",
        lambda *args, **kwargs: {"can_run": True, "materialization_required": False},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "read_rpa_flow",
        lambda *args, **kwargs: {"flow": {"steps": [{"component": "WebBrowser_CreateV3"}]}},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: (
            runtime_calls.append((args, kwargs)) or {"started": True, "status": {"status": "succeeded"}}
        ),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "wait": True, "allow_write": True},
    )

    assert len(runtime_calls) == 1
    assert result["started"] is True
    assert result["native_ui"]["preflight_recovery"]["runtime_extension_deferred_probe"]["accepted"] is True


def test_mcp_rpa_run_never_touches_deep_capture_and_uses_native_tars_card(monkeypatch):
    events = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

        def activate_runtime_extension(self):
            return {"attempted": False, "ready": True}

        def start_capture_session(self, *, package_path=None):
            events.append(("capture_session_start", package_path))
            return {"started": True, "inspect_started": False}

        def stop_inspect(self):
            events.append(("inspect_stop",))
            return {"overlay_state": "inactive"}

        def stop_capture(self, *, package_path=None):
            events.append(("capture_stop", package_path))
            return {"stopped": True}

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            events.append(("card", kwargs))
            return {"visible": not kwargs.get("close_after", False), **kwargs}

        def update(self, **kwargs):
            events.append(("card", kwargs))
            return {"visible": True, **kwargs}

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_sync_diagnose",
        lambda *args, **kwargs: {"can_run": True, "materialization_required": False},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: {"started": True, "status": {"status": "succeeded"}},
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "wait": True, "allow_write": True},
    )

    card_events = [item for item in events if item[0] == "card"]
    assert card_events[0][1]["state"] == "validating"
    assert ("capture_session_start", "CLIENT_APPLICATION_PATH") not in events
    assert not any(item[0] == "capture_stop" for item in events)
    assert len([item for item in events if item[0] == "inspect_stop"]) == 2
    assert card_events[1][1]["state"] == "running"
    assert "塔斯正在运行流程" in card_events[1][1]["message"]
    assert card_events[2][1]["state"] == "success"
    assert "close_after" not in card_events[2][1]
    assert result["preflight"]["deep_capture_touched"] is False


def test_mcp_rpa_run_blocks_when_runtime_diagnosis_is_not_ready(monkeypatch):
    runtime_calls = []

    class FakeBridge:
        def __init__(self, desktop=None):
            pass

    class FakeCard:
        def __init__(self, client=None):
            pass

        def show(self, **kwargs):
            return kwargs

        def update(self, **kwargs):
            return kwargs

    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    monkeypatch.setattr(mcp_server, "ShizaiExtensionBridge", FakeBridge)
    monkeypatch.setattr(mcp_server, "NativeSmallCard", FakeCard)
    monkeypatch.setattr(mcp_server, "_rpa_design_ui_ensure_takeover", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_sync_diagnose",
        lambda *args, **kwargs: {
            "can_run": False,
            "materialization_required": True,
            "errors": ["runtime tree is stale"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "native_rpa_run",
        lambda *args, **kwargs: runtime_calls.append((args, kwargs)),
    )

    result = mcp_server._dispatch(
        "shizai_rpa_run",
        {"project_ref": "CLIENT_APPLICATION_PATH", "wait": True, "allow_write": True},
    )

    assert runtime_calls == []
    assert result["started"] is False
    assert result["failure_kind"] == "runtime_not_ready"
    assert result["preflight"]["mode"] == "direct_cli"
    assert result["preflight"]["errors"] == ["runtime tree is stale"]


def test_mcp_ui_feedback_dispatches_scan_demo(monkeypatch):
    class FakeScanDemo:
        def __init__(self, client=None):
            pass

        def run(self, **kwargs):
            return {"kind": "scan-demo", **kwargs}

    monkeypatch.setattr(mcp_server, "NativeScanDemo", FakeScanDemo)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    result = mcp_server._dispatch(
        "shizai_ui_feedback",
        {"action": "scan_demo", "interval": 0.2, "mask_type": "white", "allow_write": True},
    )
    assert result == {
        "kind": "scan-demo",
        "interval": 0.2,
        "mask_type": "white",
        "close_after": True,
        "activate": True,
    }


def test_import_native_project_uses_designer_import_service(tmp_path):
    target = tmp_path / "native-project"
    target.mkdir()
    (target / "szrpa.json").write_text(json.dumps({"id": "native-id", "packageName": "京东采集流程"}), encoding="utf-8")

    class Client:
        def __init__(self):
            self.calls = []

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            if method == "importApplicationNormal":
                return {"isSuccess": True, "data": {"applicationId": "native-id"}}
            if method == "getMyApplicationList":
                return {"isSuccess": True, "data": [{"applicationId": "native-id", "applicationName": "京东采集流程"}]}
            raise AssertionError(method)

    client = Client()
    result = agent.import_native_project(str(target), client=client)
    assert result["registered"] is True
    assert result["verification"]["visible"] is True
    assert client.calls[0] == (
        "ApplicationService",
        "importApplicationNormal",
        [
            {
                "isZipFile": False,
                "applicationPath": str(target.resolve()),
                "importMode": "create-new",
                "isOldTaskCompGroup": False,
                "applicationType": "normal",
                "originApplicationId": "native-id",
            }
        ],
    )


def test_agent_authentication_status_is_secret_free():
    class Client:
        def authentication_status(self):
            return {
                "ready": True,
                "desktop": {"authenticated": True, "userToken": "secret"},
                "cloud_rest": {"source": "logged_in_desktop_session"},
            }

    result = agent.authentication_status(client=Client())
    assert result["ready"] is True
    assert result["desktop"]["userToken"] == "[REDACTED]"


def test_doctor_reports_missing_root(tmp_path):
    result = doctor.diagnose(_paths(tmp_path))
    assert result["ok"] is False
    assert result["checks"]["sqlite_db"] is False


def test_cli_json_project_list_with_overrides(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _make_project(paths)
    monkeypatch.setenv("SHIZAI_DATA_ROOT", str(paths.data_root))
    result = CliRunner().invoke(cli, ["--json", "project", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == "c_test"


def test_extract_file_id_supports_console_shapes():
    assert _extract_file_id({"id": "direct"}) == "direct"
    assert _extract_file_id({"data": {"fileId": "nested"}}) == "nested"


def test_extract_http_urls_deduplicates_and_strips_punctuation():
    assert _extract_http_urls("看 https://example.com/a。再看 https://example.com/a 和 http://x.test/b") == [
        "https://example.com/a",
        "http://x.test/b",
    ]


def test_desktop_exchange_token_unwraps_nested_response():
    socket = _TokenSocket(
        [
            {
                "packetType": "callResult",
                "body": {
                    "callId": "placeholder",
                    "result": {
                        "isSuccess": True,
                        "data": {"code": 0, "data": "temporary-token"},
                    },
                },
            }
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )
    assert client.exchange_token() == "temporary-token"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self.payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/files/upload"):
            return _FakeResponse({"id": "file-1", "name": "notes.txt"})
        return _FakeResponse({"documents": [{"id": "doc-1"}]})


def test_cloud_add_file_uses_expected_info_list_shape(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("knowledge", encoding="utf-8")
    session = _FakeSession()
    api = ShizaiCloudApi(
        ShizaiCloudConfig(token="TOKEN", dataset_id="dataset-1"),
        session=session,
    )
    result = api.add_file(source)
    assert result["document"]["documents"][0]["id"] == "doc-1"
    assert result["document_ids"] == ["doc-1"]
    document_call = session.calls[1]
    body = document_call[2]["json"]
    assert body["indexing_technique"] == "high_quality"
    assert body["data_source"]["info_list"]["file_info_list"]["file_ids"] == ["file-1"]


def test_cloud_add_url_uses_website_crawl_shape():
    session = _FakeSession()
    api = ShizaiCloudApi(
        ShizaiCloudConfig(token="TOKEN", dataset_id="dataset-1"),
        session=session,
    )
    result = api.add_url("https://example.com/docs")
    assert result["url"] == "https://example.com/docs"
    body = session.calls[0][2]["json"]
    assert body["data_source"]["type"] == "website_crawl"
    sources = body["data_source"]["info_list"]["website_crawl_info_list"]
    assert sources == [
        {
            "url": "https://example.com/docs",
            "job_id": "",
            "only_main_content": True,
        }
    ]
    assert result["document_ids"] == ["doc-1"]


def test_cloud_add_url_falls_back_for_legacy_console_shape():
    class LegacyWebsiteSession(_FakeSession):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            body = kwargs.get("json") or {}
            sources = body.get("data_source", {}).get("info_list", {}).get("website_crawl_info_list")
            if isinstance(sources, list):
                return _FakeResponse(
                    {"code": "invalid_param", "message": "Website source info is required"},
                    status_code=400,
                )
            return _FakeResponse({"documents": [{"id": "doc-legacy"}]})

    session = LegacyWebsiteSession()
    api = ShizaiCloudApi(
        ShizaiCloudConfig(token="TOKEN", dataset_id="dataset-1"),
        session=session,
    )
    result = api.add_url("https://example.com/docs")
    assert result["document_ids"] == ["doc-legacy"]
    assert len(session.calls) == 2
    assert session.calls[1][2]["json"]["data_source"]["info_list"]["website_crawl_info_list"]["urls"] == [
        "https://example.com/docs"
    ]


def test_webpage_download_uses_curl_after_requests_failure(monkeypatch):
    calls = []

    def fail_request(*args, **kwargs):
        raise cloud_module.requests.RequestException("WAF timeout")

    class Completed:
        returncode = 0
        stdout = b"<html><body><p>Fetched content</p></body></html>\n__SHIZAI_HTTP_STATUS__:200"
        stderr = b""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(cloud_module.requests, "get", fail_request)
    monkeypatch.setattr(cloud_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(cloud_module.subprocess, "run", fake_run)

    html = cloud_module._download_webpage_html("https://example.com/article?tracking=1")

    assert "Fetched content" in html
    assert calls
    assert calls[0][0][0] == "curl.exe"


def test_cloud_request_refreshes_stale_desktop_token_once():
    class ExpiringSession:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if len(self.calls) == 1:
                return _FakeResponse(
                    {"code": "40002", "message": "token 可能无效或已过期"},
                    status_code=400,
                )
            return _FakeResponse({"documents": []})

    session = ExpiringSession()
    api = ShizaiCloudApi(
        ShizaiCloudConfig(token="stale-token", dataset_id="dataset-1"),
        session=session,
        token_refresher=lambda: "fresh-token",
    )
    result = api.list_documents()
    assert result == {"documents": []}
    assert len(session.calls) == 2
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer stale-token"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer fresh-token"
    assert api.config.token == "fresh-token"


def test_normalize_model_tables_keeps_headers_with_each_model():
    source = """## Qwen 系列

| 模型 | 最大上下文长度 | 硬件规格 |
| --- | --- | --- |
| Qwen3-1.7B | 32k | Atlas 800I A2：1卡 |
"""
    normalized = _normalize_model_tables(source)
    assert "### Qwen3-1.7B" in normalized
    assert "- 最大上下文长度: 32k" in normalized
    assert "- 硬件规格: Atlas 800I A2：1卡" in normalized
    assert "| --- |" not in normalized


def test_wait_for_documents_polls_until_available():
    class SequencedSession:
        def __init__(self):
            self.payloads = [
                {"data": [{"id": "doc-1", "indexing_status": "parsing", "display_status": "processing"}]},
                {
                    "data": [
                        {
                            "id": "doc-1",
                            "name": "notes.txt",
                            "indexing_status": "completed",
                            "display_status": "available",
                        }
                    ]
                },
            ]
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            return _FakeResponse(self.payloads.pop(0))

    session = SequencedSession()
    api = ShizaiCloudApi(ShizaiCloudConfig(token="TOKEN", timeout=1), session=session)
    result = api.wait_for_documents(["doc-1"], interval=0)
    assert result["ready"] is True
    assert result["status"] == "available"
    assert result["documents"][0]["name"] == "notes.txt"
    assert session.calls == 2


def test_wait_for_documents_times_out_with_latest_status():
    session = _FakeSession()
    api = ShizaiCloudApi(ShizaiCloudConfig(token="TOKEN", timeout=1), session=session)
    result = api.wait_for_documents(["missing"], timeout=0, interval=0)
    assert result["ready"] is False
    assert result["status"] == "timed_out"
    assert result["documents"][0]["id"] == "missing"


def test_ingest_source_dispatches_url_and_file(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("knowledge", encoding="utf-8")

    class Api:
        def __init__(self):
            self.calls = []

        def add_url(self, value, **kwargs):
            self.calls.append(("url", value, kwargs))
            return {"document_ids": []}

        def add_file(self, value, **kwargs):
            self.calls.append(("file", value, kwargs))
            return {"document_ids": []}

    api = Api()
    _ingest_source(api, "https://example.com/a", dataset_id="dataset", process_mode="automatic")
    _ingest_source(api, str(source), dataset_id="dataset", process_mode="custom")
    assert api.calls[0][0] == "url"
    assert api.calls[1][0] == "file"
    assert api.calls[1][1] == source


def test_cli_knowledge_ingest_emits_document_ids(monkeypatch):
    class Api:
        def add_url(self, value, **kwargs):
            return {"url": value, "document": {"documents": [{"id": "doc-url"}]}, "document_ids": ["doc-url"]}

    monkeypatch.setattr(cli_module, "_cloud", lambda: Api())
    result = CliRunner().invoke(cli, ["--json", "knowledge", "ingest", "https://example.com/a"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["document_ids"] == ["doc-url"]


def test_cli_knowledge_wait_emits_ready_status(monkeypatch):
    class Api:
        def wait_for_documents(self, ids, **kwargs):
            return {"ready": True, "status": "available", "document_ids": ids, "documents": []}

    monkeypatch.setattr(cli_module, "_cloud", lambda: Api())
    result = CliRunner().invoke(cli, ["--json", "knowledge", "wait", "doc-1", "doc-2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is True
    assert payload["document_ids"] == ["doc-1", "doc-2"]


def test_get_workflow_draft_unwraps_desktop_data_envelope():
    class DraftClient(ShizaiDesktopAgent):
        def call(self, service, method, args=None):
            return {"isSuccess": True, "data": {"hasDraft": True, "draft": {"graph": {}}}}

    draft = DraftClient(ShizaiCloudConfig()).get_workflow_draft()
    assert draft["hasDraft"] is True
    assert "draft" in draft


def test_configure_knowledge_updates_retrieval_and_context_prompt():
    draft = {
        "draft": {
            "graph": {
                "nodes": [
                    {"data": {"type": "knowledge-retrieval", "dataset_ids": ["old"]}},
                    {
                        "data": {
                            "type": "llm",
                            "context": {"enabled": True},
                            "prompt_template": [{"role": "system", "text": "old"}],
                        }
                    },
                ]
            }
        }
    }
    calls = []

    class ConfigureClient(ShizaiDesktopAgent):
        def get_workflow_draft(self, agent_id=None):
            return draft

        def call(self, service, method, args=None):
            calls.append((service, method, args))
            return {"isSuccess": True, "data": {"result": "success"}}

    result = ConfigureClient(ShizaiCloudConfig()).configure_knowledge("dataset-new", top_k=8)
    nodes = draft["draft"]["graph"]["nodes"]
    assert result["nodes_changed"] == 1
    assert result["prompt_nodes_changed"] == 1
    assert nodes[0]["data"]["dataset_ids"] == ["dataset-new"]
    assert nodes[0]["data"]["multiple_retrieval_config"]["top_k"] == 8
    assert nodes[1]["data"]["prompt_template"][0]["text"] == KNOWLEDGE_QA_PROMPT
    assert "完全一致的型号名称" in KNOWLEDGE_QA_PROMPT
    assert calls[0][1] == "saveWorkflowDraft"


def test_configure_ingestion_tools_adds_editor_compatible_branches():
    draft = {
        "draft": {
            "graph": {
                "nodes": [
                    {"id": "start", "data": {"type": "start"}},
                    {"id": "retrieval", "data": {"type": "knowledge-retrieval"}},
                    {"id": "llm", "data": {"type": "llm", "prompt_template": []}},
                    {"id": "answer", "data": {"type": "answer"}},
                ],
                "edges": [{"id": "start-retrieval", "source": "start", "target": "retrieval"}],
            },
            "features": {},
        }
    }
    calls = []

    class ConfigureClient(ShizaiDesktopAgent):
        def get_workflow_draft(self, agent_id=None):
            return draft

        def call(self, service, method, args=None):
            calls.append((service, method, args))
            return {"isSuccess": True, "data": {"result": "success"}}

    result = ConfigureClient(ShizaiCloudConfig()).configure_ingestion_tools()
    nodes = draft["draft"]["graph"]["nodes"]
    node_types = {node["id"]: node["data"]["type"] for node in nodes}
    assert result["saved"] is True
    assert node_types["source-router-kb"] == "if-else"
    assert node_types["file-reader-kb"] == "tool"
    assert node_types["url-extractor-kb"] == "code"
    assert node_types["webscraper-kb"] == "tool"
    web_node = next(node for node in nodes if node["id"] == "webscraper-kb")
    assert web_node["data"]["tool_parameters"]["url"]["value"] == ["url-extractor-kb", "result"]
    assert draft["draft"]["features"]["support_file_types"]
    assert any(
        edge["source"] == "source-router-kb" and edge["sourceHandle"] == "false"
        for edge in draft["draft"]["graph"]["edges"]
    )
    assert calls[-1][1] == "saveWorkflowDraft"


def test_configure_ingestion_tools_removes_stale_direct_web_edge():
    draft = {
        "draft": {
            "graph": {
                "nodes": [
                    {"id": "start", "data": {"type": "start"}},
                    {"id": "retrieval", "data": {"type": "knowledge-retrieval"}},
                    {"id": "llm", "data": {"type": "llm", "prompt_template": []}},
                    {"id": "answer", "data": {"type": "answer"}},
                    {"id": "source-router-kb", "data": {"type": "if-else"}},
                    {"id": "webscraper-kb", "data": {"type": "tool"}},
                ],
                "edges": [
                    {"id": "stale", "source": "source-router-kb", "target": "webscraper-kb", "sourceHandle": "url"},
                ],
            },
            "features": {},
        }
    }

    class ConfigureClient(ShizaiDesktopAgent):
        def get_workflow_draft(self, agent_id=None):
            return draft

        def call(self, service, method, args=None):
            return {"isSuccess": True, "data": {"result": "success"}}

    ConfigureClient(ShizaiCloudConfig()).configure_ingestion_tools()
    edges = draft["draft"]["graph"]["edges"]
    assert not any(edge["source"] == "source-router-kb" and edge["target"] == "webscraper-kb" for edge in edges)
    assert any(edge["source"] == "url-extractor-kb" and edge["target"] == "webscraper-kb" for edge in edges)


class _FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        if not self.messages:
            raise AssertionError("fake socket was read after the workflow finished")
        return json.dumps(self.messages.pop(0), ensure_ascii=False)

    def close(self):
        return None


class _TokenSocket(_FakeSocket):
    def send(self, payload):
        super().send(payload)
        if self.messages:
            self.messages[0]["body"]["callId"] = self.sent[-1]["body"]["callId"]


def test_merge_native_feishu_config_preserves_other_users_and_fields():
    origin = {
        "other": "keep",
        "tarsIM": {
            "other-user": {"checked": True, "dingtalk": {"clientId": "ding-id"}},
            "492082": {"checked": False, "feishu": {"legacy": "keep"}},
        },
    }
    updated = merge_native_feishu_config(
        origin,
        user_id="492082",
        client_id="cli_test",
        client_secret="secret_test",
    )
    assert origin["tarsIM"]["492082"]["checked"] is False
    assert updated["other"] == "keep"
    assert updated["tarsIM"]["other-user"] == origin["tarsIM"]["other-user"]
    assert updated["tarsIM"]["492082"]["feishu"] == {
        "legacy": "keep",
        "clientId": "cli_test",
        "clientSecret": "secret_test",
    }
    assert updated["tarsIM"]["492082"]["activeMode"] is True


def test_native_feishu_summary_does_not_return_credentials():
    summary = native_feishu_summary(
        {"tarsIM": {"492082": {"checked": True, "feishu": {"clientId": "cli_test", "clientSecret": "secret_test"}}}},
        "492082",
    )
    encoded = json.dumps(summary, ensure_ascii=False)
    assert summary["configured"] is True
    assert summary["client_id_present"] is True
    assert summary["client_secret_present"] is True
    assert "cli_test" not in encoded
    assert "secret_test" not in encoded


def test_remove_native_feishu_config_keeps_other_users_and_providers():
    origin = {
        "tarsIM": {
            "492082": {"checked": True, "activeMode": True, "feishu": {"clientId": "cli_test"}},
            "other-user": {"checked": True, "dingtalk": {"clientId": "ding_test"}},
        },
        "tarsIMSessionMap": {"492082": {"feishu_oc_test": 123, "other_session": 456}},
        "other": "keep",
    }
    updated = remove_native_feishu_config(origin, user_id="492082")
    assert "tarsIM" in updated
    assert "492082" not in updated["tarsIM"]
    assert updated["tarsIM"]["other-user"] == origin["tarsIM"]["other-user"]
    assert updated["tarsIMSessionMap"]["492082"] == {"other_session": 456}
    assert updated["other"] == "keep"


def test_desktop_native_feishu_configure_merges_and_saves_with_rpc():
    responses = [
        {"isSuccess": True, "data": {"userId": "492082"}},
        {"isSuccess": True, "data": {"tarsIM": {"other-user": {"checked": True}}}},
        {"isSuccess": True, "data": None},
    ]
    sockets = []

    def factory(url, timeout):
        response = responses.pop(0)
        socket = _TokenSocket([{"packetType": "callResult", "body": {"callId": "placeholder", "result": response}}])
        sockets.append(socket)
        return socket

    client = ShizaiDesktopAgent(ShizaiCloudConfig(timeout=2), connection_factory=factory)
    result = client.configure_native_feishu("cli_test", "secret_test")
    assert result["saved"] is True
    assert result["user_id"] == "492082"
    assert result["configured"] is True
    assert sockets[2].sent[0]["body"]["serviceName"] == "NewStorageService"
    payload = sockets[2].sent[0]["body"]["args"][0]
    assert payload["tarsIM"]["other-user"]["checked"] is True
    assert payload["tarsIM"]["492082"]["feishu"]["clientSecret"] == "secret_test"


class _TimeoutSocket:
    def send(self, payload):
        return None

    def recv(self):
        raise TimeoutError("timed out")

    def close(self):
        return None


def test_desktop_transport_timeout_is_normalized():
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=1),
        connection_factory=lambda url, timeout: _TimeoutSocket(),
    )
    with pytest.raises(ShizaiCloudError, match="desktop business channel"):
        client.chat("问题")


def test_desktop_missing_installation_returns_download_guidance(monkeypatch):
    class FailingWebsocket:
        @staticmethod
        def create_connection(url, timeout):
            raise OSError("connection refused")

    monkeypatch.setattr(cloud_module, "websocket", FailingWebsocket)
    monkeypatch.setattr(ShizaiDesktopAgent, "_start_desktop", staticmethod(lambda: (False, None)))
    client = ShizaiDesktopAgent(ShizaiCloudConfig(timeout=1))
    result = client.authentication_status()
    assert "www.ai-indeed.com" in result["desktop"]["error"]


def test_desktop_channel_lock_reports_the_operation(monkeypatch, tmp_path):
    import shizai_agent.core.cloud_api as cloud_api

    monkeypatch.setattr(cloud_api.tempfile, "gettempdir", lambda: str(tmp_path))
    with _desktop_channel_lock(0, "RPC"):
        with pytest.raises(ShizaiCloudError, match="desktop RPC"):
            with _desktop_channel_lock(0, "RPC"):
                pass


def test_extract_retriever_sources_compacts_workflow_results():
    events = [
        {
            "event": "message_end",
            "metadata": {
                "retriever_resources": [
                    {
                        "position": 1,
                        "document_id": "doc-1",
                        "document_name": "manual.md",
                        "segment_id": "segment-1",
                        "score": 0.98,
                        "content": "first source",
                    }
                ]
            },
        },
        {
            "event": "node_finished",
            "data": {
                "outputs": {
                    "result": [
                        {
                            "metadata": {
                                "position": 1,
                                "document_id": "doc-1",
                                "document_name": "manual.md",
                                "segment_id": "segment-1",
                                "score": 0.98,
                            },
                            "content": "first source",
                        },
                        {
                            "metadata": {
                                "position": 2,
                                "document_id": "doc-2",
                                "document_name": "runbook.md",
                                "segment_id": "segment-2",
                                "score": 0.91,
                            },
                            "content": "second source",
                        },
                    ]
                }
            },
        },
    ]
    sources = _extract_retriever_sources(events)
    assert [source["document_id"] for source in sources] == ["doc-1", "doc-2"]
    assert sources[0]["score"] == 0.98
    assert sources[1]["content"] == "second source"


def test_desktop_chat_collects_streamed_workflow_answer():
    event = {
        "event": "workflow_finished",
        "conversation_id": "conversation-1",
        "task_id": "task-1",
        "data": {"status": "succeeded", "outputs": {"answer": "来自知识库的回答"}},
    }
    socket = _FakeSocket(
        [
            {"packetType": "callResult", "body": {"callId": "other", "result": {"isSuccess": True}}},
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-1",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [{"message": {"data": json.dumps(event, ensure_ascii=False)}}],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )
    result = client.chat("问题")
    assert result["answer"] == "来自知识库的回答"
    assert result["conversation_id"] == "conversation-1"
    assert socket.sent[1]["packetType"] == "node-call-render-res"


def test_desktop_chat_accepts_string_message_envelope():
    event = {
        "event": "workflow_finished",
        "conversation_id": "conversation-2",
        "task_id": "task-2",
        "data": {"status": "succeeded", "outputs": {"answer": "字符串消息答案"}},
    }
    socket = _FakeSocket(
        [
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-2",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [
                        {"message": json.dumps({"data": json.dumps(event, ensure_ascii=False)}, ensure_ascii=False)}
                    ],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )
    result = client.chat("问题")
    assert result["answer"] == "字符串消息答案"
    assert result["task_id"] == "task-2"


def test_desktop_chat_handles_empty_workflow_outputs():
    event = {
        "event": "workflow_finished",
        "conversation_id": "conversation-empty",
        "task_id": "task-empty",
        "data": None,
    }
    socket = _FakeSocket(
        [
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-empty",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [{"message": {"data": event}}],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )
    result = client.chat("网页问题")
    assert result["answer"] == ""
    assert result["conversation_id"] == "conversation-empty"
    assert result["task_id"] == "task-empty"


def test_desktop_chat_ignores_events_from_another_task():
    own_started = {
        "event": "workflow_started",
        "conversation_id": "conversation-own",
        "task_id": "task-own",
    }
    other_message = {"event": "message", "task_id": "task-other", "answer": "别的任务"}
    own_finished = {
        "event": "workflow_finished",
        "conversation_id": "conversation-own",
        "task_id": "task-own",
        "data": {"outputs": {"answer": "当前任务"}},
    }
    socket = _FakeSocket(
        [
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-own-start",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [{"message": {"data": own_started}}],
                },
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-other-message",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [{"message": {"data": other_message}}],
                },
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "render-own-finished",
                    "serviceName": "SinglePointPushClientService",
                    "methodName": "processMessage",
                    "args": [{"message": {"data": own_finished}}],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )
    result = client.chat("问题")
    assert result["answer"] == "当前任务"
    assert len(result["events"]) == 2


def test_desktop_native_rpa_keeps_channel_open_until_terminal_state():
    class NativeRpaSocket(_FakeSocket):
        def __init__(self, messages):
            super().__init__(messages)
            self.initial_call_bound = False

        def send(self, payload):
            super().send(payload)
            if not self.initial_call_bound and self.messages:
                self.messages[0]["body"]["callId"] = self.sent[-1]["body"]["callId"]
                self.initial_call_bound = True

    socket = NativeRpaSocket(
        [
            {
                "packetType": "callResult",
                "body": {"callId": "placeholder", "result": {"isSuccess": True, "data": {"accepted": True}}},
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "mask",
                    "serviceName": "MaskService",
                    "methodName": "showMaskV2",
                    "args": [{"requestId": "runtime-mask-1"}],
                },
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "running",
                    "serviceName": "TrayManager",
                    "methodName": "runStateChange",
                    "args": ["running"],
                },
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "success",
                    "serviceName": "TrayManager",
                    "methodName": "runStateChange",
                    "args": ["success"],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )

    result = client.run_native_rpa({"app_id": "app-1"}, timeout=2)

    assert result["accepted"] is True
    assert result["completed"] is True
    assert result["terminal_status"] == "success"
    assert len(result["render_events"]) == 3
    assert result["runtime_mask_suppressions"] == [
        {
            "suppressed": True,
            "request_id": "runtime-mask-1",
            "close_call_id": result["runtime_mask_suppressions"][0]["close_call_id"],
            "service": "MaskAppService",
            "method": "closeRunMask",
        }
    ]
    assert [item["packetType"] for item in socket.sent] == [
        "methodCall",
        "node-call-render-res",
        "methodCall",
        "node-call-render-res",
        "node-call-render-res",
    ]
    assert socket.sent[2]["body"]["serviceName"] == "MaskAppService"
    assert socket.sent[2]["body"]["methodName"] == "closeRunMask"
    assert socket.sent[2]["body"]["args"] == [{"requestId": "runtime-mask-1"}]
    assert socket.sent[-1]["body"]["callId"] == "success"


def test_desktop_native_rpa_reports_timeout_after_acceptance():
    socket = _TokenSocket(
        [
            {
                "packetType": "callResult",
                "body": {"callId": "placeholder", "result": {"isSuccess": True}},
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )

    result = client.run_native_rpa({"app_id": "app-1"}, timeout=0)

    assert result["accepted"] is False
    assert result["completed"] is False
    assert result["timed_out"] is True


def test_desktop_native_rpa_force_closes_provider_routed_runtime_mask(monkeypatch):
    class ProviderRoutedMaskSocket:
        def __init__(self):
            self.sent = []
            self.recv_count = 0
            self.run_call_id = ""
            self.timeout = None

        def send(self, payload):
            packet = json.loads(payload)
            self.sent.append(packet)
            if not self.run_call_id:
                self.run_call_id = packet["body"]["callId"]

        def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps(
                    {
                        "packetType": "callResult",
                        "body": {"callId": self.run_call_id, "result": {"isSuccess": True}},
                    }
                )
            if self.recv_count == 2:
                cloud_module.time.sleep(0.8)
                return json.dumps(
                    {
                        "packetType": "node-call-render-req",
                        "body": {
                            "callId": "running",
                            "serviceName": "TrayManager",
                            "methodName": "runStateChange",
                            "args": ["running"],
                        },
                    }
                )
            return json.dumps(
                {
                    "packetType": "node-call-render-req",
                    "body": {
                        "callId": "success",
                        "serviceName": "TrayManager",
                        "methodName": "runStateChange",
                        "args": ["success"],
                    },
                }
            )

        def settimeout(self, timeout):
            self.timeout = timeout

        def close(self):
            pass

    socket = ProviderRoutedMaskSocket()
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )

    result = client.run_native_rpa({"app_id": "app-1"}, timeout=2)

    assert result["terminal_status"] == "success"
    assert result["runtime_mask_force_close"]["suppressed"] is True
    close_packet = next(
        packet
        for packet in socket.sent
        if packet["packetType"] == "methodCall" and packet["body"]["serviceName"] == "MaskAppService"
    )
    assert close_packet["body"]["serviceName"] == "MaskAppService"
    assert close_packet["body"]["methodName"] == "closeRunMask"
    assert close_packet["body"]["args"] == [{"requestId": "", "force": True}]


def test_desktop_native_rpa_leaves_non_run_mask_render_events_untouched():
    class NativeRpaSocket(_FakeSocket):
        def __init__(self, messages):
            super().__init__(messages)
            self.initial_call_bound = False

        def send(self, payload):
            super().send(payload)
            if not self.initial_call_bound and self.messages:
                self.messages[0]["body"]["callId"] = self.sent[-1]["body"]["callId"]
                self.initial_call_bound = True

    socket = NativeRpaSocket(
        [
            {
                "packetType": "callResult",
                "body": {"callId": "placeholder", "result": {"isSuccess": True}},
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "picker",
                    "serviceName": "DeepCaptureService",
                    "methodName": "startDeepCaptureInspect",
                    "args": [2],
                },
            },
            {
                "packetType": "node-call-render-req",
                "body": {
                    "callId": "success",
                    "serviceName": "TrayManager",
                    "methodName": "runStateChange",
                    "args": ["success"],
                },
            },
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=2),
        connection_factory=lambda url, timeout: socket,
    )

    result = client.run_native_rpa({"app_id": "app-1"}, timeout=2)

    assert result["runtime_mask_suppressions"] == []
    assert [item["packetType"] for item in socket.sent] == [
        "methodCall",
        "node-call-render-res",
        "node-call-render-res",
    ]


def test_desktop_native_rpa_aborts_when_runtime_log_reports_plugin_failure():
    class PollingSocket(_TokenSocket):
        def __init__(self, messages):
            super().__init__(messages)
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

    socket = PollingSocket(
        [
            {
                "packetType": "callResult",
                "body": {"callId": "placeholder", "result": {"isSuccess": True}},
            }
        ]
    )
    client = ShizaiDesktopAgent(
        ShizaiCloudConfig(timeout=10),
        connection_factory=lambda url, timeout: socket,
    )
    blocker = {
        "kind": "runtime_plugin_unavailable",
        "status": "disabled",
        "error_code": "010400100000996",
    }
    checks = iter([None, blocker])

    result = client.run_native_rpa({"app_id": "app-1"}, timeout=10, abort_check=lambda: next(checks))

    assert socket.timeout == 1.0
    assert result["accepted"] is True
    assert result["completed"] is True
    assert result["timed_out"] is False
    assert result["terminal_status"] == "error"
    assert result["runtime_blocker"] == blocker


def test_create_agent_uses_application_operation_service():
    class CreateClient:
        def __init__(self):
            self.calls = []

        def call(self, service, method, args):
            self.calls.append((service, method, args))
            return {"data": {"applicationId": "agent-new", "applicationPackageId": "pkg-new"}}

    client = CreateClient()
    result = agent.create_agent("日志助手", client=client)
    assert result["application_id"] == "agent-new"
    assert client.calls == [
        ("ApplicationOpService", "createApplicationByDefault", ["Agent", {"examId": "", "name": "日志助手"}])
    ]


def test_update_agent_uses_live_app_info_rpc():
    class UpdateClient:
        def call(self, service, method, args):
            assert service == "AgentBrainAppService"
            assert method == "updateAppInfo"
            assert args == [{"appId": "agent-1", "data": {"description": "新描述"}}]
            return {"data": {"applicationId": "agent-1"}}

    assert agent.update_agent("agent-1", description="新描述", client=UpdateClient())["applicationId"] == "agent-1"


def test_apply_flow_requires_write_for_execution():
    draft = {"graph": {"nodes": [{"id": "start", "data": {"type": "start"}}], "edges": []}}
    result = agent.apply_flow(draft, client=object())
    assert result["saved"] is False
    assert result["validation"]["valid"] is True
    with pytest.raises(PermissionError, match="allow_write"):
        agent.apply_flow(draft, run=True, allow_write=False, client=object())


def test_staged_rpa_step_validation_covers_explicit_excel_and_custom_columns(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "staged-excel"
    columns = ["商品名称", "商家名称", "价格", "销量", "商品链接"]
    project.create_rpa_flow(
        str(staged),
        target_url="https://www.jd.com/",
        keyword="手机",
        output_file="phones.xlsx",
        required_columns=columns,
        use_excel_components=True,
    )
    records = [
        {
            "id": "search",
            "name": "搜索框",
            "title": "京东",
            "url": "https://www.jd.com/",
            "tag": "input",
            "css_selector": "#search",
        }
    ]
    records.extend(
        {
            "id": item_id,
            "name": label,
            "title": "手机 - 商品搜索 - 京东",
            "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
            "tag": "div",
            "css_selector": selector,
        }
        for item_id, label, selector in [
            ("name", "商品名称", ".name"),
            ("merchant", "商家名称", ".merchant"),
            ("price", "价格", ".price"),
            ("sales", "销量", ".sales"),
            ("link", "商品链接", "a.product-link"),
        ]
    )
    project.import_elements(paths, str(staged), records, replace=True)
    results = []
    results.append(project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open"))
    results.append(
        project.add_rpa_step(paths, str(staged), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    )
    results.append(project.add_rpa_step(paths, str(staged), "Keyboard_PressNew", step_id="submit"))
    results.append(
        project.add_rpa_step(
            paths,
            str(staged),
            "WebBrowser_DataCollectionV5",
            step_id="collect",
            element_ids=["name", "merchant", "price", "sales", "link"],
        )
    )
    results.append(project.add_rpa_step(paths, str(staged), "Excel_Cs_NewV4", step_id="excel"))
    results.append(project.add_rpa_step(paths, str(staged), "Excel_Cs_WriteRange", step_id="write"))

    assert all(item["step_validation"]["valid"] for item in results)
    flow = project.read_rpa_flow(paths, str(staged))["flow"]
    assert [
        item["component"] for item in flow["steps"]
    ] == project.RPA_RECOMMENDED_SEQUENCE + project.RPA_EXCEL_SEQUENCE
    assert flow["steps"][3]["config"]["columns"] == columns
    assert flow["steps"][3]["config"]["toExcel"] == 0
    assert flow["steps"][4]["config"]["name"] == "phones.xlsx"
    assert flow["steps"][5]["config"]["contents"] == "数据采集结果"
    validation = project.validate_rpa_flow(paths, str(staged), strict=True)
    assert validation["valid"] is True
    assert len(validation["step_validations"]) == 6


def test_staged_js_table_sequence_skips_data_collection_and_binds_excel(tmp_path):
    paths = _paths(tmp_path)
    staged = tmp_path / "staged-js-table"
    project.create_rpa_flow(
        str(staged),
        target_url="https://www.jd.com/",
        keyword="手机",
        output_file="phones.xlsx",
        required_columns=["商品标题", "价格", "商品链接"],
        use_excel_components=True,
        use_js_table=True,
        max_rows=10,
        wait_after_submit=True,
        refresh_browser_tab=True,
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        project.create_rpa_flow(str(tmp_path / "invalid-js-mode"), use_js_link=True, use_js_table=True)

    project.import_elements(
        paths,
        str(staged),
        [
            {
                "id": "search",
                "name": "搜索框",
                "title": "京东",
                "url": "https://www.jd.com/",
                "tag": "input",
                "css_selector": "#search",
            }
        ],
        replace=True,
    )
    project.add_rpa_step(paths, str(staged), "WebBrowser_CreateV3", step_id="open")
    project.add_rpa_step(paths, str(staged), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    project.add_rpa_step(paths, str(staged), "Keyboard_PressNew", step_id="submit")
    project.add_rpa_step(paths, str(staged), "Basic_Delay_V1", step_id="wait")
    project.add_rpa_step(paths, str(staged), "WebBrowser_GetTAB_V1", step_id="tab")
    project.add_rpa_step(paths, str(staged), "WebBrowser_RunJsInIeBySelector_v3", step_id="table")
    project.add_rpa_step(paths, str(staged), "Excel_Cs_NewV4", step_id="excel")
    project.add_rpa_step(paths, str(staged), "Excel_Cs_WriteRange", step_id="write")

    flow = project.read_rpa_flow(paths, str(staged))["flow"]
    assert [item["component"] for item in flow["steps"]] == [
        "WebBrowser_CreateV3",
        "Element_SetValue_V6",
        "Keyboard_PressNew",
        "Basic_Delay_V1",
        "WebBrowser_GetTAB_V1",
        "WebBrowser_RunJsInIeBySelector_v3",
        "Excel_Cs_NewV4",
        "Excel_Cs_WriteRange",
    ]
    assert "WebBrowser_DataCollectionV5" not in [item["component"] for item in flow["steps"]]
    js_step = flow["steps"][5]
    assert js_step["config"]["output_variable"] == "数据采集结果"
    assert "return result" in js_step["config"]["jsCode"]
    assert 'const requestedColumns = ["商品标题", "价格", "商品链接"];' in js_step["config"]["jsCode"]
    assert "const maxRows = 10;" in js_step["config"]["jsCode"]
    assert "SHIZAI_WAITING_FOR_USER:login_required" in js_step["config"]["jsCode"]
    assert "SHIZAI_WAITING_FOR_USER:page_challenge" in js_step["config"]["jsCode"]
    assert flow["max_rows"] == 10
    assert flow["steps"][7]["config"]["contents"] == "数据采集结果"
    validation = project.validate_rpa_flow(paths, str(staged), strict=True)
    assert validation["valid"] is True
    assert validation["use_js_table"] is True
    assert validation["expected_sequence"] == [item["component"] for item in flow["steps"]]


def test_js_table_pagination_fallback_supports_react_search_pages():
    assert "[class*='paginationNext']:not([disabled])" in project.JS_NEXT_PAGE_SCRIPT
    assert "current.searchParams.set(\"page\", String(nextPage))" in project.JS_NEXT_PAGE_SCRIPT
    assert "current.searchParams.set(\"s\", String((nextPage - 1) * 30))" in project.JS_NEXT_PAGE_SCRIPT
    assert "location.assign(current.href)" in project.JS_NEXT_PAGE_SCRIPT
    assert "window.scrollTo(0, before)" in project.JS_NEXT_PAGE_SCRIPT


def test_page_scoped_js_flow_does_not_require_element_catalog(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    staged = _make_project(paths, "page-scoped-js-only")
    monkeypatch.setattr(
        project,
        "inspect_rpa_flow",
        lambda _paths, _project_ref: {
            "project": str(staged),
            "valid": True,
            "task_files": [{"steps": [{"component": "WebBrowser_RunJsInIeBySelector_v3"}]}],
            "errors": [],
            "warnings": [],
        },
    )

    validation = project.validate_rpa_project(paths, str(staged), strict=True)

    assert validation["valid"] is True
    assert validation["errors"] == []
    assert validation["elements"]["element_count"] == 0


def test_native_rpa_excel_fallback_writes_real_argument_bindings(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "native-excel"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "id": "native-excel",
                "packageName": "Native Excel 流程",
                "componentsNum": 0,
                "mcpTargetUrl": "https://www.jd.com/",
                "mcpKeyword": "手机",
                "mcpOutputFile": "phones.xlsx",
                "mcpRequiredColumns": ["商品名称", "商家名称", "价格", "销量", "商品链接"],
                "mcpExcelMode": True,
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(json.dumps({"elementMap": {}, "groupMap": {}}), encoding="utf-8")
    (native / "project.flow").write_text("flow", encoding="utf-8")
    (native / "main.sz").write_text("main", encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
                "dataTag": 1,
            }
        ),
        encoding="utf-8",
    )
    search = {
        "id": "search",
        "name": "搜索框",
        "title": "京东",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
    }
    fields = [
        {
            "id": item_id,
            "name": label,
            "title": "搜索结果",
            "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
            "tag": "div",
            "css_selector": selector,
        }
        for item_id, label, selector in [
            ("name", "商品名称", ".name"),
            ("merchant", "商家名称", ".merchant"),
            ("price", "价格", ".price"),
            ("sales", "销量", ".sales"),
            ("link", "商品链接", "a.product-link"),
        ]
    ]
    project.import_elements(paths, str(native), [search], replace=True)
    added = [project.add_rpa_step(paths, str(native), "WebBrowser_CreateV3", step_id="open")]
    added.append(
        project.add_rpa_step(paths, str(native), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    )
    added.append(project.add_rpa_step(paths, str(native), "Keyboard_PressNew", step_id="submit"))
    project.import_elements(paths, str(native), fields)
    added.append(
        project.add_rpa_step(
            paths,
            str(native),
            "WebBrowser_DataCollectionV5",
            step_id="collect",
            element_ids=[item["id"] for item in fields],
        )
    )
    added.append(project.add_rpa_step(paths, str(native), "Excel_Cs_NewV4", step_id="excel"))
    added.append(project.add_rpa_step(paths, str(native), "Excel_Cs_WriteRange", step_id="write"))

    assert all(item["step_validation"]["valid"] for item in added)
    task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    entities = task["componentModelEntities"]
    statement_ids = next(item["value"] for item in entities["MAIN_COMPONENT"]["args0"] if item["name"] == "statements")
    assert [
        entities[item]["type"] for item in statement_ids
    ] == project.RPA_RECOMMENDED_SEQUENCE + project.RPA_EXCEL_SEQUENCE
    data_args = {item["name"]: item["value"] for item in entities["collect"]["args0"]}
    assert data_args["toExcel"] == "0"
    collection = json.loads(data_args["config"])["collectionConfig"]
    assert [item["columnHeader"] for item in collection["columnConfigArray"]] == [
        "商品名称",
        "商家名称",
        "价格",
        "销量",
        "商品链接",
    ]
    new_args = {item["name"]: item["value"] for item in entities["excel"]["args0"]}
    assert json.loads(new_args["name"]) == "phones.xlsx"
    write_args = {item["name"]: item["value"] for item in entities["write"]["args0"]}
    assert write_args["workbook"] == "Excel文件"
    assert write_args["contents"] == "数据采集结果"
    assert json.loads(write_args["sheet"]) == "Sheet1"
    assert project.validate_rpa_flow(paths, str(native), strict=True)["valid"] is True

    project._native_set_argument(entities["write"], "sheet", '"京东手机Top10"')
    (native / "io5cd.task").write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    invalid = project.validate_rpa_step(paths, str(native), "write")
    assert invalid["valid"] is False
    assert any(item["name"] == "excel_sheet_exists" and not item["passed"] for item in invalid["checks"])


def test_native_js_table_materialize_keeps_direct_output_variable_type(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "native-js-table"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "id": "native-js-table",
                "packageName": "Native JS Table 流程",
                "componentsNum": 0,
                "mcpTargetUrl": "https://www.jd.com/",
                "mcpKeyword": "手机",
                "mcpOutputFile": "phones.xlsx",
                "mcpRequiredColumns": ["商品名称", "商家名称", "价格", "销量", "商品链接"],
                "mcpExcelMode": True,
                "mcpUseJsTable": True,
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(json.dumps({"elementMap": {}, "groupMap": {}}), encoding="utf-8")
    (native / "project.flow").write_text("flow", encoding="utf-8")
    (native / "main.sz").write_text("main", encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
                "dataTag": 1,
            }
        ),
        encoding="utf-8",
    )
    project.import_elements(
        paths,
        str(native),
        [
            {
                "id": "search",
                "name": "搜索框",
                "title": "京东",
                "url": "https://www.jd.com/",
                "tag": "input",
                "css_selector": "#search",
            }
        ],
        replace=True,
    )

    project.add_rpa_step(paths, str(native), "WebBrowser_CreateV3", step_id="open")
    project.add_rpa_step(paths, str(native), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    project.add_rpa_step(paths, str(native), "Keyboard_PressNew", step_id="submit")
    project.add_rpa_step(paths, str(native), "WebBrowser_RunJsInIeBySelector_v3", step_id="table")
    project.add_rpa_step(paths, str(native), "Excel_Cs_NewV4", step_id="excel")
    project.add_rpa_step(paths, str(native), "Excel_Cs_WriteRange", step_id="write")

    validation = project.validate_rpa_flow(paths, str(native), strict=True)
    assert validation["valid"] is True
    task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    entities = task["componentModelEntities"]
    statement_ids = next(item["value"] for item in entities["MAIN_COMPONENT"]["args0"] if item["name"] == "statements")
    assert [entities[item]["type"] for item in statement_ids] == [
        "WebBrowser_CreateV3",
        "Element_SetValue_V6",
        "Keyboard_PressNew",
        "WebBrowser_RunJsInIeBySelector_v3",
        "Excel_Cs_NewV4",
        "Excel_Cs_WriteRange",
    ]
    js_args = {item["name"]: item["value"] for item in entities["table"]["args0"]}
    assert js_args["var_name"] == "数据采集结果"
    assert "return result" in js_args["jsCode"]
    assert {item["name"]: item["value"] for item in entities["write"]["args0"]}["contents"] == "数据采集结果"

    materialized = project.materialize_native_rpa(paths, str(native), strict=True)
    assert materialized["ready_for_runtime_diagnosis"] is True
    module_text = (native / "projects" / "flow_modules" / "io5cd.py").read_text(encoding="utf-8")
    assert "Excel.Cs_WriteRange(" in module_text
    assert "fromContent=True" not in module_text
    assert "数据采集结果 = json.loads(数据采集结果)" in module_text
    variable_info = json.loads((native / "variableInfo").read_text(encoding="utf-8"))
    assert isinstance(variable_info["sortInfo"], dict)
    assert isinstance(variable_info["variableMap"], list)
    assert all(isinstance(item, list) and len(item) == 2 for item in variable_info["variableMap"])
    table_refs = [
        item
        for _, info in variable_info["variableMap"]
        for item in info["variable"]
        if item.get("variableName") == "数据采集结果" and item.get("referenceType") == "set"
    ]
    assert table_refs
    assert set(table_refs[0]) == {"referenceType", "variableName", "locStart"}

    index = json.loads((native / "file_index_700.json").read_text(encoding="utf-8"))
    module_index = index["flow_modules/io5cd.py"]
    assert isinstance(module_index["variableReferenceList"], list)
    assert any(
        item.get("variableName") == "数据采集结果"
        and item.get("referenceType") == "set"
        and item.get("variableTypeNames") == ["rpa_list_piliangshuju_v1"]
        for item in module_index["variableReferenceList"]
    )


def test_native_variable_info_snapshot_rejects_rich_reference_list(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "variable-info-shape")
    (target / "variableInfo").write_text(
        json.dumps(
            {
                "sortInfo": {},
                "variableMap": [
                    {
                        "id": "s-io5cd-flowBlock-1",
                        "referenceType": "set",
                        "variableName": "网页对象",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    snapshot = project._native_variable_info_snapshot(target)

    assert snapshot["exists"] is True
    assert snapshot["valid"] is False
    assert snapshot["variable_map_type"] == "list"
    assert "pairs" in snapshot["error"]

    (target / "variableInfo").write_text(
        json.dumps({"sortInfo": {}, "variableMap": []}),
        encoding="utf-8",
    )
    assert project._native_variable_info_snapshot(target)["valid"] is True


def test_native_sync_diagnose_requires_native_variable_info_shape(tmp_path):
    paths = _paths(tmp_path)
    target = _make_project(paths, "variable-info-diagnose")
    (target / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    }
                },
                "dataTag": 1,
            }
        ),
        encoding="utf-8",
    )
    project.materialize_native_rpa(paths, str(target), strict=False)
    (target / "variableInfo").write_text(
        json.dumps({"sortInfo": {}, "variableMap": [{"referenceType": "set"}]}),
        encoding="utf-8",
    )

    diagnosis = project.native_rpa_sync_diagnose(paths, str(target))

    assert diagnosis["variable_info"]["valid"] is False
    assert diagnosis["materialization_required"] is True
    assert diagnosis["next_tool"] == "shizai_rpa_materialize"
    assert any("variableInfo.variableMap" in error for error in diagnosis["errors"])


def test_native_collection_binding_ignores_dynamic_uia_indexes(tmp_path):
    elements = {
        "elementMap": {
            "product": {
                "id": "product",
                "selector": {
                    "selector": [
                        {"name": "Window", "type": "32", "props": []},
                        {"name": "Document", "type": "6", "props": []},
                        {
                            "name": "Group",
                            "type": "6",
                            "props": [
                                {"name": "class", "value": '"card"'},
                                {"name": "@index", "value": 16},
                            ],
                        },
                        {
                            "name": "Text",
                            "type": "6",
                            "props": [
                                {"name": "class", "value": '"title"'},
                                {"name": "@index", "value": 3},
                            ],
                        },
                    ],
                },
            },
        },
    }
    task_selector = json.loads(
        json.dumps(
            project._collection_selector_chain(elements["elementMap"]["product"]),
            ensure_ascii=False,
        )
    )
    index_property = next(
        prop for item in task_selector for prop in item.get("props", []) if prop.get("name") == "@index"
    )
    assert index_property["value"] == "3"
    assert not any(prop.get("name") == "@index" for prop in task_selector[0].get("props", []))
    index_property["value"] = "11"
    task = {
                            "args0": [
                                {
                                    "name": "config",
                "value": json.dumps({"collectionConfig": {"columnSelectorArray": [task_selector]}}, ensure_ascii=False),
            }
        ],
    }
    assert project._native_collection_element_ids(task, elements) == ["product"]


def test_native_element_prune_keeps_flow_bindings_and_removes_stale_records(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "prune-native"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "Prune Flow",
                "mcpTargetUrl": "https://www.jd.com/",
                "mcpKeyword": "手机",
                "mcpOutputFile": "phones.xlsx",
                "mcpRequiredColumns": ["商品名称", "商家名称", "价格", "销量", "商品链接"],
                "mcpExcelMode": True,
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(
        json.dumps(
            {
                "elementMap": {
                    "search": {
                        "id": "search",
                        "name": "搜索框",
                        "title": "京东",
                        "url": "https://www.jd.com/",
                        "tag": "input",
                        "css_selector": "#search",
                    },
                    "stale": {
                        "id": "stale",
                        "name": "旧元素",
                        "title": "京东",
                        "url": "",
                        "tag": "input",
                        "css_selector": "#old",
                    },
                },
                "groupMap": {},
            }
        ),
        encoding="utf-8",
    )
    (native / "project.flow").write_text("flow", encoding="utf-8")
    (native / "main.sz").write_text("main", encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": ["open", "input"]}],
                    },
                    "open": {"id": "open", "type": "WebBrowser_CreateV3", "args0": []},
                    "input": {
                        "id": "input",
                        "type": "Element_SetValue_V6",
                        "args0": [
                            {
                                "name": "uiSelector",
                                "value": 'elementsFormatNew(SHIZAI_ELEMENT_DICT["search"]["selector"])',
                            },
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = project.prune_unused_rpa_elements(paths, str(native))

    assert result["removed"] == ["stale"]
    assert result["kept"] == ["search"]
    assert json.loads((native / "elements.json").read_text(encoding="utf-8"))["elementMap"].keys() == {"search"}


def test_native_debug_archives_existing_excel_targets_before_start(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "debug-output"
    native.mkdir(parents=True)
    (native / "res").mkdir()
    (native / "res" / "products.xlsx").write_bytes(b"old workbook")
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "Debug output flow",
                "mcpOutputFile": "products.xlsx",
                "mcpExcelMode": True,
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(json.dumps({"elementMap": {}, "groupMap": {}}), encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {"type": "main", "args0": [{"name": "statements", "value": ["excel"]}]},
                    "excel": {
                        "id": "excel",
                        "type": "Excel_Cs_NewV4",
                        "args0": [
                            {"name": "var_name", "value": "Excel文件"},
                            {"name": "path", "value": '"res"'},
                            {"name": "name", "value": '"products.xlsx"'},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    archived = debug_session._archive_debug_outputs(
        paths,
        native,
        ["excel"],
        1,
    )

    assert len(archived) == 1
    assert archived[0]["from"].endswith("res\\products.xlsx")
    assert not (native / "res" / "products.xlsx").exists()
    assert Path(archived[0]["to"]).read_bytes() == b"old workbook"


def test_native_materialize_repairs_missing_runtime_tree(tmp_path):
    paths = _paths(tmp_path)
    native = paths.applications_root / "materialize-native"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "Materialize Flow",
                "mcpTargetUrl": "https://www.jd.com/",
                "mcpKeyword": "手机",
                "mcpOutputFile": "phones.xlsx",
                "mcpRequiredColumns": ["商品名称", "商家名称", "价格", "销量", "商品链接"],
                "mcpExcelMode": True,
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(json.dumps({"elementMap": {}, "groupMap": {}}), encoding="utf-8")
    (native / "project.flow").write_text("flow", encoding="utf-8")
    (native / "main.sz").write_text("main", encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": []}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    search = {
        "id": "search",
        "name": "搜索框",
        "title": "京东",
        "url": "https://www.jd.com/",
        "tag": "input",
        "css_selector": "#search",
    }
    fields = [
        {
            "id": item_id,
            "name": label,
            "title": "搜索结果",
            "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
            "tag": "div",
            "css_selector": selector,
        }
        for item_id, label, selector in [
            ("name", "商品名称", ".name"),
            ("merchant", "商家名称", ".merchant"),
            ("price", "价格", ".price"),
            ("sales", "销量", ".sales"),
            ("link", "商品链接", "a.product-link"),
        ]
    ]
    project.import_elements(paths, str(native), [search], replace=True)
    project.add_rpa_step(paths, str(native), "WebBrowser_CreateV3", step_id="open")
    project.add_rpa_step(paths, str(native), "Element_SetValue_V6", step_id="input", element_ids=["search"])
    project.add_rpa_step(paths, str(native), "Keyboard_PressNew", step_id="submit")
    project.import_elements(paths, str(native), fields)
    project.add_rpa_step(
        paths,
        str(native),
        "WebBrowser_DataCollectionV5",
        step_id="collect",
        element_ids=[item["id"] for item in fields],
    )
    project.add_rpa_step(paths, str(native), "Excel_Cs_NewV4", step_id="excel")
    project.add_rpa_step(paths, str(native), "Excel_Cs_WriteRange", step_id="write")
    task_payload = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    collect_args = task_payload["componentModelEntities"]["collect"]["args0"]
    next(item for item in collect_args if item["name"] == "selector_page")["value"] = (
        'SHIZAI_ELEMENT_DICT["name"]["selector"]'
    )
    (native / "io5cd.task").write_text(json.dumps(task_payload, ensure_ascii=False), encoding="utf-8")

    for path in [
        native / "file_index_700.json",
        native / "file_sourcemap.json",
        native / "variableInfo",
        native / "projects" / "flow.py",
        native / "projects" / "global_data.py",
        native / "projects" / "rpaRoot.py",
        native / "projects" / "flow_modules" / "io5cd.py",
    ]:
        if path.is_file():
            path.unlink()

    result = project.materialize_native_rpa(paths, str(native))
    assert result["ready_for_runtime_diagnosis"] is True
    assert result["missing_files"] == []
    repaired_task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    repaired_args = repaired_task["componentModelEntities"]["collect"]["args0"]
    assert next(item for item in repaired_args if item["name"] == "region")["value"] == "1"
    assert next(item for item in repaired_args if item["name"] == "selector_page")["value"] == ""
    assert next(item for item in repaired_args if item["name"] == "selector_more")["value"] == ""
    assert (native / "file_index_700.json").is_file()
    sourcemap = json.loads((native / "file_sourcemap.json").read_text(encoding="utf-8"))
    assert "flow_modules/io5cd.py" in sourcemap["files"]
    assert "flow_modules/io5cd.task" not in sourcemap["files"]
    diagnosis = project.native_rpa_sync_diagnose(paths, str(native))
    assert diagnosis["materialization_required"] is False
    assert diagnosis["next_tool"] == "shizai_rpa_validate_project"


def test_native_materialize_restores_paging_after_designer_rpc_round_trip(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    native = paths.applications_root / "materialize-paging"
    native.mkdir(parents=True)
    (native / "szrpa.json").write_text(
        json.dumps(
            {
                "packageName": "Paging Flow",
                "mcpTargetUrl": "https://www.jd.com/",
                "mcpRequiredColumns": ["商品名称"],
                "mcpCollectionRegion": "3",
                "mcpCollectionPageTypeNum": "1",
                "mcpMaxRows": 100,
                "mcpPlanningPolicy": "model_decides",
            }
        ),
        encoding="utf-8",
    )
    (native / "elements.json").write_text(json.dumps({"elementMap": {}, "groupMap": {}}), encoding="utf-8")
    (native / "project.flow").write_text("flow", encoding="utf-8")
    (native / "main.sz").write_text("main", encoding="utf-8")
    (native / "io5cd.task").write_text(
        json.dumps(
            {
                "componentModelEntities": {
                    "MAIN_COMPONENT": {
                        "id": "MAIN_COMPONENT",
                        "type": "main",
                        "args0": [{"name": "statements", "value": ["collect"]}],
                    },
                    "collect": {
                        "id": "collect",
                        "type": "WebBrowser_DataCollectionV5",
                        "args0": [
                            {
                                "name": "config",
                                "value": json.dumps(
                                    {
                                        "pageInfo": {
                                            "windowTitle": "搜索结果",
                                            "pageUrl": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
                                            "windowType": "web",
                                            "processName": "",
                                        },
                                        "collectionConfig": {
                                            "tableSelector": [],
                                            "columnSelectorArray": [[]],
                                            "columnConfigArray": [
                                                {
                                                    "columnHeader": "商品名称",
                                                    "dataAttrName": "innerText",
                                                }
                                            ],
                                        },
                                    },
                                        ensure_ascii=False,
                                    ),
                                },
                                {"name": "var0", "value": "数据采集结果"},
                                {"name": "var1", "value": "Excel路径"},
                                {"name": "var2", "value": "监控空列数据"},
                                {"name": "designated_info", "value": "0"},
                                {"name": "designated_obj", "value": "网页对象"},
                                {"name": "region", "value": "3"},
                            {"name": "collect_pages", "value": "1"},
                            {"name": "collect_items", "value": "100"},
                            {"name": "page_type", "value": "1"},
                            {"name": "page_type_num", "value": "1"},
                            {
                                "name": "selector_page",
                                "value": 'elementsFormatNew(SHIZAI_ELEMENT_DICT["next"]["selector"])',
                            },
                            {"name": "selector_more", "value": ""},
                        ],
                        "mcpElementIds": ["next"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    next_element = {
        "id": "next",
        "name": "下一页",
        "title": "搜索结果",
        "url": "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA",
        "tag": "a",
        "css_selector": ".pn-next",
    }
    project.import_elements(paths, str(native), [next_element], replace=False)

    def old_designer_sync(_paths, root, task_path, *, client=None):
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        collect = payload["componentModelEntities"]["collect"]
        for argument in collect["args0"]:
            if argument["name"] in {"selector_page", "selector_more"}:
                argument["value"] = ""
        task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"mode": "designer_rpc_and_local", "package": {"matched": True}, "rpc": {}, "errors": []}

    monkeypatch.setattr(project, "_sync_native_designer_content", old_designer_sync)
    monkeypatch.setattr(
        project,
        "validate_rpa_project",
        lambda *args, **kwargs: {"valid": True, "errors": [], "warnings": []},
    )
    result = project.materialize_native_rpa(paths, str(native), strict=False, client=object())

    assert result["ready_for_runtime_diagnosis"] is True
    assert result["designer_sync"]["paging_repair"]["changed"] is True
    repaired_task = json.loads((native / "io5cd.task").read_text(encoding="utf-8"))
    repaired_args = repaired_task["componentModelEntities"]["collect"]["args0"]
    assert next(item for item in repaired_args if item["name"] == "selector_page")["value"] == (
        'elementsFormatNew(SHIZAI_ELEMENT_DICT["next"]["selector"])'
    )


def test_native_module_uses_designer_call_names_and_output_bindings():
    payload = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {
                "args0": [{"name": "statements", "value": ["open", "collect", "excel"]}],
            },
            "open": {
                "id": "open",
                "type": "WebBrowser_CreateV3",
                "args0": [
                    {"name": "var_name", "value": "网页对象"},
                    {"name": "type", "value": '"chrome"'},
                    {"name": "url", "value": '"https://example.test/"'},
                ],
            },
            "collect": {
                "id": "collect",
                "type": "WebBrowser_DataCollectionV5",
                "args0": [
                    {"name": "var0", "value": "数据采集结果"},
                    {"name": "var1", "value": "Excel路径"},
                    {"name": "var2", "value": "监控空列数据"},
                    {"name": "config", "value": "{}"},
                ],
            },
            "excel": {
                "id": "excel",
                "type": "Excel_Cs_NewV4",
                "args0": [
                    {"name": "var_name", "value": "Excel文件"},
                    {"name": "path", "value": '"res"'},
                    {"name": "name", "value": '"products.xlsx"'},
                ],
            },
        },
    }
    text = project._native_module_text(payload, Path("io5cd.task"))
    assert "WebBrowser.CreateV3(" in text
    assert "WebBrowser.WebBrowser" not in text
    assert "Excel.Cs_NewV4(" in text
    assert "Excel.Excel" not in text
    assert 'var_names=["网页对象"]' in text
    assert 'var_names=["数据采集结果", "Excel路径", "监控空列数据"]' in text
    assert '"WebBrowser.CreateV3"' in text


def test_native_element_click_data_is_emitted_with_integer_fields():
    rendered = project._native_code_expression(
        "click_data",
        {"cursorPosition": "0", "offset": {"x": "0", "y": "0"}, "random": "0"},
    )

    assert ast.literal_eval(rendered) == {
        "cursorPosition": 0,
        "offset": {"x": 0, "y": 0},
        "random": 0,
    }


def test_native_normalize_node_unquotes_strict_numeric_collection_arguments():
    node = {
        "type": "WebBrowser_DataCollectionV5",
        "args0": [
            {"name": "wait_time_page", "value": '"3"'},
            {"name": "scroll_config", "value": '"2"'},
            {"name": "showTable", "value": '"0"'},
            {"name": "selector_more", "value": '"next-page"'},
        ],
    }

    project._native_normalize_node(node)

    assert node["args0"][0]["value"] == "3"
    assert node["args0"][1]["value"] == "2"
    assert node["args0"][2]["value"] == "0"
    assert node["args0"][3]["value"] == '"next-page"'


def test_native_element_parent_must_reference_group():
    payload = {
        "elementMap": {
            "child": {"parentId": "ordinary-element"},
            "ordinary-element": {},
            "group-child": {"parentId": "group"},
        },
        "groupMap": {"group": {}},
    }

    errors = project._native_element_parent_errors(payload)

    assert len(errors) == 1
    assert "child" in errors[0]
    assert "must reference groupMap" in errors[0]
