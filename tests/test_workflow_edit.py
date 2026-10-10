"""Editing regression tests use real native files, never the user's designer."""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from copy import deepcopy

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from shizai_agent.core import mcp_server, project
from shizai_agent.core.browser_mcp import bind_verified_elements, point_elements
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_contracts import RPA_AUTHORING_TOOLS
from shizai_agent.core.workflow_edit import WorkflowEditError, flow_revision, read_edit_context
from shizai_agent.shizai_agent_cli import cli


@pytest.fixture
def native_flow(tmp_path, monkeypatch):
    root = tmp_path / "applications" / "edit-test"
    root.mkdir(parents=True)
    paths = ShizaiPaths(tmp_path, root.parent, tmp_path / "sqlite.db", tmp_path / "run.json", None, None)
    metadata = {"packageName": "编辑测试", "mcpPlanComplete": True, "mcpPlanningPolicy": "model_decides"}
    nodes = {
        "MAIN_COMPONENT": {"id": "MAIN_COMPONENT", "type": "main",
                           "args0": [{"name": "statements", "value": ["open", "custom"]}]},
        "open": {"id": "open", "type": "WebBrowser_CreateV3", "args0": [
            {"name": "url", "value": '"https://example.test"'}, {"name": "openNewTab", "value": "0"},
        ]},
        "custom": {"id": "custom", "type": "Custom_Task", "args0": [
            {"name": "message", "value": '"original"'}, {"name": "password", "value": "test-secret"},
        ], "options0": [{"name": "enabled", "value": "1"}], "mcpElementIds": ["field-a"]},
        "nested": {"id": "nested", "type": "Custom_Task", "args0": []},
    }
    (root / "io5cd.task").write_text(json.dumps({"componentModelEntities": nodes}), encoding="utf-8")
    (root / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "elements.json").write_text('{"elementMap":{}}', encoding="utf-8")
    monkeypatch.setattr(project, "_live_rpa_contract", lambda *_: {"output_parameters": [], "requires_elements": False})
    monkeypatch.setattr(mcp_server, "_paths", lambda: paths)
    monkeypatch.setenv("SHIZAI_DATA_ROOT", str(tmp_path))
    return paths, root, nodes


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_browser_bridge_evidence_imports_and_binds_existing_native_step(native_flow):
    paths, root, _ = native_flow
    task = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    task["componentModelEntities"]["custom"].update({
        "type": "Element_SetValue_V6",
        "args0": [
            {"name": "uiSelector", "value": ""},
            {"name": "designated_obj", "value": "None"},
            {"name": "value", "value": '"phone"'},
        ],
    })
    (root / "io5cd.task").write_text(json.dumps(task), encoding="utf-8")
    evidence = {
        "tabId": 4, "screenshotId": "shot-bind", "pageRevision": "3",
        "url": "https://example.test/search", "title": "Search",
        "points": [{
            "found": True, "rejected": False, "point_is_match": True, "visible": True,
            "tag": "button", "text": "Search", "attributes": {"id": "submit"},
            "point": {"x": 420, "y": 90, "role": "submit"},
            "selector_candidates": [{"selector": "button#submit", "matches": 1, "visible_matches": 1}],
        }],
    }
    records = point_elements(evidence, page=evidence, prefix="custom-browser")
    result = bind_verified_elements(paths, project_ref=str(root), step_id="custom", elements=records)

    assert result["imported"]["capture_source"] == "browser_bridge"
    task = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    assert task["componentModelEntities"]["custom"]["mcpElementIds"] == ["custom-browser-submit"]
    elements = json.loads((root / "elements.json").read_text(encoding="utf-8"))["elementMap"]
    assert elements["custom-browser-submit"]["mcpBrowserEvidence"]["screenshotId"] == "shot-bind"


def forbid_side_effects(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("preview attempted desktop I/O or persistence")

    for name in ("_publish_rpa_ui", "_ensure_native_runtime_support", "_finalize_native_writeback", "_update_native_metadata"):
        monkeypatch.setattr(project, name, fail)


def test_edit_context_targets_native_and_nested_nodes_without_expanding_everything(native_flow):
    paths, root, _ = native_flow
    before = snapshot(root)
    summary = read_edit_context(paths, str(root))
    assert [step["id"] for step in summary["steps"]] == ["open", "custom", "nested"]
    assert summary["steps"][-1]["top_level"] is False
    assert "selected_step" not in summary
    selected = read_edit_context(paths, str(root), step_id="custom")
    assert selected["selected_step"]["native_parameters"]["message"] == '"original"'
    assert selected["selected_step"]["element_ids"] == ["field-a"]
    assert "test-secret" not in json.dumps(selected)
    assert selected["revision"] == summary["revision"]
    assert snapshot(root) == before


def test_edit_context_ignores_designer_draft_restore_backup(native_flow):
    paths, root, _ = native_flow
    backup = root / ".sz" / "draft-restore-backup" / "latest" / "io5cd.task"
    backup.parent.mkdir(parents=True)
    backup.write_text(json.dumps({
        "componentModelEntities": {
            "MAIN_COMPONENT": {
                "id": "MAIN_COMPONENT",
                "type": "main",
                "args0": [{"name": "statements", "value": ["stale"]}],
            },
            "stale": {"id": "stale", "type": "Custom_Task", "args0": []},
        }
    }), encoding="utf-8")

    context = read_edit_context(paths, str(root))

    assert context["flow_file"] == str(root / "io5cd.task")
    assert [step["id"] for step in context["steps"]] == ["open", "custom", "nested"]


@pytest.mark.parametrize("config", [{"openNewTab": True}, {"url": "https://example.test/changed"}])
def test_preview_performs_real_encoding_without_writes_or_ui(native_flow, monkeypatch, config):
    paths, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    before = snapshot(root)
    result = project.update_rpa_step_config(paths, str(root), "open", config, dry_run=True)
    assert result["saved"] is False
    assert result["changes"]
    if "openNewTab" in config:
        assert result["changes"] == [{"parameter": "openNewTab", "before": "0", "after": "1",
                                       "before_present": True, "after_present": True}]
    assert result["validation_scope"] == "parameter_encoding_only"
    assert snapshot(root) == before


@pytest.mark.parametrize(("requested", "expected"), [("1", "1"), (True, "1"), ("0", "0"), (False, "0")])
def test_js_async_flag_uses_native_numeric_boolean_encoding(native_flow, monkeypatch, requested, expected):
    """RunJsInIeBySelector must receive 1/0, never a JSON string literal."""
    paths, root, _ = native_flow
    task_path = root / "io5cd.task"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    node = payload["componentModelEntities"]["custom"]
    node["type"] = "WebBrowser_RunJsInIeBySelector_v3"
    node["args0"] = [
        {"name": "jsCode", "value": '"""return 1"""'},
        {"name": "isAsyncCode", "value": '"1"'},
        {"name": "waitResultTimeout", "value": "30"},
    ]
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    forbid_side_effects(monkeypatch)

    preview = project.update_rpa_step_config(
        paths, str(root), "custom", {"isAsyncCode": requested}, dry_run=True,
    )

    changed = {item["parameter"]: item for item in preview["changes"]}
    assert changed["isAsyncCode"]["after"] == expected
    assert '"1"' == next(
        item["value"]
        for item in payload["componentModelEntities"]["custom"]["args0"]
        if item["name"] == "isAsyncCode"
    )
    persisted = json.loads(task_path.read_text(encoding="utf-8"))
    assert next(
        item["value"]
        for item in persisted["componentModelEntities"]["custom"]["args0"]
        if item["name"] == "isAsyncCode"
    ) == '"1"'


def test_sqlite_database_target_is_prepared_before_runtime(tmp_path):
    database = tmp_path / "nested" / "comments.sqlite3"
    config = {
        "dtype": 4,
        "DatabaseConfig": [{"name": "Filepath", "value": str(database)}],
    }

    first = project._ensure_sqlite_database_file(config, project_dir=tmp_path)
    assert first == {"prepared": True, "created": True, "path": str(database)}
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probe (value TEXT)")
    second = project._ensure_sqlite_database_file(config, project_dir=tmp_path)
    assert second["prepared"] is True and second["created"] is False

    unresolved = project._ensure_sqlite_database_file(
        {"dtype": 4, "DatabaseConfig": [{"name": "Filepath", "value": {"variable": "路径"}}]},
        project_dir=tmp_path,
    )
    assert unresolved == {"prepared": False, "path": None}


def test_invalid_edit_does_not_write(native_flow, monkeypatch):
    paths, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    before = snapshot(root)
    with pytest.raises(ValueError, match="unsupported config keys"):
        project.update_rpa_step_config(paths, str(root), "open", {"url": "new", "imaginary": 1})
    assert snapshot(root) == before


@pytest.mark.parametrize("filename", ["io5cd.task", "szrpa.json", "elements.json", "variableInfo", "nested.task"])
def test_stale_edit_refuses_all_authoritative_changes(native_flow, monkeypatch, filename):
    paths, root, _ = native_flow
    revision = flow_revision(root)
    path = root / filename
    path.write_bytes(path.read_bytes() + b" " if path.exists() else b"{}")
    before = snapshot(root)
    forbid_side_effects(monkeypatch)
    with pytest.raises(WorkflowEditError) as error:
        project.update_rpa_step_config(paths, str(root), "open", {"openNewTab": True}, expected_revision=revision)
    assert error.value.result["error_code"] == "revision_conflict"
    assert error.value.result["next_call"]["arguments"]["flow_ref"] == str(root)
    assert snapshot(root) == before


def test_preview_then_commit_preserves_other_nodes(native_flow, monkeypatch):
    paths, root, nodes = native_flow
    before = deepcopy(nodes)
    monkeypatch.setattr(project, "_publish_rpa_ui", lambda *a, **k: {})
    monkeypatch.setattr(project, "_ensure_native_runtime_support", lambda *a, **k: None)
    monkeypatch.setattr(project, "_finalize_native_writeback", lambda *a, **k: {"test_backend": True})
    monkeypatch.setattr(project, "validate_rpa_step", lambda *a, **k: {"valid": True})
    preview = project.update_rpa_step_config(paths, str(root), "open", {"openNewTab": True}, dry_run=True)
    result = project.update_rpa_step_config(
        paths, str(root), "open", {"openNewTab": True}, expected_revision=preview["revision"],
    )
    after = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))["componentModelEntities"]
    assert after["custom"] == before["custom"] and after["nested"] == before["nested"]
    assert after["MAIN_COMPONENT"] == before["MAIN_COMPONENT"]
    assert result["saved"] is True
    assert result["revision"] != preview["revision"]
    assert result["changes"] == preview["changes"]


def _prepare_active_editor(native_flow, monkeypatch):
    paths, root, _ = native_flow
    (root / "edit-state.json").write_text(
        json.dumps({"currentEditorId": "editor-edit-test"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        project,
        "_native_package_summary",
        lambda *_: {"matched": True, "application_id": "app-edit-test"},
    )
    return paths, root


def test_reload_editor_checks_revision_and_waits_for_two_clean_ready_reads(native_flow, monkeypatch):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)
    calls = []

    def renderer(_client, service, method, args, **kwargs):
        calls.append((service, method, args))
        if method in {"getCurrentArrangementInfo", "isNeedSave"} and len(calls) > 3:
            assert 0.25 <= kwargs["response_timeout"] <= 1.5
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return False
        if (service, method) == ("WorkbenchService", "reload"):
            return {"isSuccess": True, "data": None}
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    monkeypatch.setattr(project.time, "sleep", lambda *_: None)
    revision = flow_revision(root)
    result = project.reload_rpa_editor(
        paths, str(root), expected_revision=revision, client=object(), ready_timeout=1
    )

    assert result["revision_before"] == revision == result["revision_after"]
    assert result["editor_ready"] is True and result["ready_checks"] == 2
    assert result["canvas_configuration_verified"] is False
    assert calls.count(("WorkbenchService", "reload", [])) == 1
    assert calls.count(("WorkbenchService", "isNeedSave", [])) == 3


@pytest.mark.parametrize("mode", ["wrong_editor", "unsaved", "stale_revision"])
def test_reload_editor_refuses_unsafe_state_before_reload(native_flow, monkeypatch, mode):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)
    calls = []

    def renderer(_client, service, method, args, **_kwargs):
        calls.append((service, method, args))
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "another-editor" if mode == "wrong_editor" else "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return mode == "unsaved"
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    expected = "stale" if mode == "stale_revision" else flow_revision(root)
    error = WorkflowEditError if mode == "stale_revision" else ValueError
    with pytest.raises(error):
        project.reload_rpa_editor(paths, str(root), expected_revision=expected, client=object())
    assert not any(method == "reload" for _, method, _ in calls)


def test_save_editor_previews_then_persists_and_verifies_persisted_state(native_flow, monkeypatch):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)
    calls = []
    state = {"dirty": True}

    def renderer(_client, service, method, args, **_kwargs):
        calls.append((service, method, args))
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return state["dirty"]
        if (service, method) == ("PackageSaveService", "saveAll"):
            assert args == [{"alertOnError": True}]
            state["dirty"] = False
            payload = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
            payload["componentModelEntities"]["custom"]["args0"][0]["value"] = '"saved-from-canvas"'
            (root / "io5cd.task").write_text(json.dumps(payload), encoding="utf-8")
            return {"isSuccess": True, "data": None}
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    revision = flow_revision(root)
    preview = project.save_rpa_editor(
        paths, str(root), expected_revision=revision, dry_run=True, client=object()
    )

    assert preview["dry_run"] is True and preview["saved"] is False
    assert preview["dirty_before"] is True
    assert preview["steps"] == ["open", "custom"]
    assert preview["next_call"]["tool"] == "shizai_rpa_save_editor"
    assert preview["next_call"]["arguments"]["expected_revision"] == revision
    assert not any(method == "saveAll" for _, method, _ in calls)

    result = project.save_rpa_editor(paths, str(root), expected_revision=revision, client=object())

    assert result["save_requested"] is True and result["saved"] is True
    assert result["dirty_after"] is False
    assert result["persisted_change"] is True
    assert result["revision_after"] != revision
    assert result["steps"] == ["open", "custom"]
    assert result["next_action"]["tool"] == "shizai_rpa_reload_editor"
    assert result["next_action"]["arguments"]["expected_revision"] == result["revision_after"]
    assert calls.count(("PackageSaveService", "saveAll", [{"alertOnError": True}])) == 1


def test_save_editor_does_not_claim_success_while_the_designer_stays_dirty(native_flow, monkeypatch):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)

    def renderer(_client, service, method, _args, **_kwargs):
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return True
        if (service, method) == ("PackageSaveService", "saveAll"):
            return {"isSuccess": True, "data": None}
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    result = project.save_rpa_editor(
        paths, str(root), expected_revision=flow_revision(root), client=object()
    )

    assert result["saved"] is False
    assert result["failure_kind"] == "save_unconfirmed"
    assert result["retryable"] is True
    assert result["next_action"]["tool"] == "shizai_rpa_save_editor"
    assert "reload_editor" not in json.dumps(result["next_action"])


def test_mcp_and_cli_save_editor_gate_write_and_forward_desktop(native_flow, monkeypatch):
    _, root, _ = native_flow
    desktop = object()
    calls = []
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: desktop)
    monkeypatch.setattr(
        mcp_server.project_core,
        "save_rpa_editor",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"saved": True},
    )
    revision = flow_revision(root)
    args = {"project_ref": str(root), "expected_revision": revision}

    with pytest.raises(PermissionError):
        mcp_server._dispatch_impl("shizai_rpa_save_editor", args)

    preview = mcp_server._dispatch_impl("shizai_rpa_save_editor", {**args, "dry_run": True})
    assert preview == {"saved": True}
    assert calls[0][0][1] == str(root)
    assert calls[0][1]["dry_run"] is True and calls[0][1]["client"] is None

    committed = mcp_server._dispatch_impl("shizai_rpa_save_editor", {**args, "allow_write": True})
    assert committed == {"saved": True}
    assert calls[1][1]["dry_run"] is False and calls[1][1]["client"] is desktop
    assert calls[1][1]["expected_revision"] == revision

    runner = CliRunner()
    cli_args = ["--json", "workflow", "rpa-save-editor", str(root), "--expected-revision", revision]
    denied = runner.invoke(cli, cli_args)
    assert denied.exit_code != 0 and "requires --allow-write" in denied.output
    cli_preview = runner.invoke(cli, cli_args + ["--dry-run"])
    assert cli_preview.exit_code == 0, cli_preview.output
    assert json.loads(cli_preview.output) == preview
    assert calls[2][1]["dry_run"] is True and calls[2][1]["client"] is None


def test_run_component_recovers_context_and_verifies_exact_single_node_code(native_flow, monkeypatch):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)
    code_path = root / "projects" / "_temp" / "component_run.py"
    code_path.parent.mkdir(parents=True)
    code_path.write_text("# stale", encoding="utf-8")

    def renderer(_client, service, method, args, **_kwargs):
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return False
        if (service, method) == ("RunService", "runComponentInEditor"):
            request = args[0]
            assert request["componentIds"] == ["custom"]
            assert request["useCache"] is True
            assert request["createCodeParams"]["useCache"] is True
            code_path.write_text(
                "pass  #@!io5cd@!-@!custom@!-@!flow@!-@!custom@!-@!2@!\n",
                encoding="utf-8",
            )
            return {"state": "success", "message": ""}
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    result = project.run_rpa_component(
        paths, str(root), "custom", expected_revision=flow_revision(root), client=object()
    )

    assert result["generated_component_ids"] == ["custom"]
    assert result["generated_code_fresh"] is True
    assert result["exact_component_verified"] is True
    assert result["runtime_verified"] is True


def test_mcp_reload_and_run_component_require_write_and_forward_desktop(native_flow, monkeypatch):
    _, root, _ = native_flow
    desktop = object()
    calls = []
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: desktop)
    monkeypatch.setattr(
        mcp_server.project_core,
        "reload_rpa_editor",
        lambda *args, **kwargs: calls.append(("reload", args, kwargs)) or {"editor_ready": True},
    )
    monkeypatch.setattr(
        mcp_server.project_core,
        "run_rpa_component",
        lambda *args, **kwargs: calls.append(("run", args, kwargs)) or {"runtime_verified": True},
    )
    revision = flow_revision(root)
    reload_args = {"project_ref": str(root), "expected_revision": revision}
    run_args = {**reload_args, "step_id": "custom"}

    with pytest.raises(PermissionError):
        mcp_server._dispatch_impl("shizai_rpa_reload_editor", reload_args)
    with pytest.raises(PermissionError):
        mcp_server._dispatch_impl("shizai_rpa_run_component", run_args)

    assert mcp_server._dispatch_impl(
        "shizai_rpa_reload_editor", {**reload_args, "allow_write": True}
    )["editor_ready"] is True
    assert mcp_server._dispatch_impl(
        "shizai_rpa_run_component", {**run_args, "allow_write": True}
    )["runtime_verified"] is True
    assert calls[0][1][1] == str(root)
    assert calls[0][2]["expected_revision"] == revision and calls[0][2]["client"] is desktop
    assert calls[1][1][1:3] == (str(root), "custom")
    assert calls[1][2]["expected_revision"] == revision and calls[1][2]["client"] is desktop


@pytest.mark.parametrize("write_fresh_cache,verified", [(True, True), (False, False)])
def test_run_collection_requires_fresh_nonempty_variable_cache(
    native_flow, monkeypatch, write_fresh_cache, verified
):
    paths, root = _prepare_active_editor(native_flow, monkeypatch)
    task_path = root / "io5cd.task"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["componentModelEntities"]["custom"]["type"] = "WebBrowser_DataCollectionV5"
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    code_path = root / "projects" / "_temp" / "component_run.py"
    cache_path = root / ".sz" / "variable_cache" / "io5cd.json"
    code_path.parent.mkdir(parents=True)
    cache_path.parent.mkdir(parents=True)
    stale_cache = {
        "custom": {
            "componentId": "custom",
            "result": [[["商品名称"], ["旧数据"]], "", {}],
            "varNames": ["数据采集结果"],
        }
    }
    cache_path.write_text(json.dumps(stale_cache, ensure_ascii=False), encoding="utf-8")

    def renderer(_client, service, method, args, **_kwargs):
        if (service, method) == ("ArrangementEditService", "getCurrentArrangementInfo"):
            return {"currentEditorId": "editor-edit-test"}
        if (service, method) == ("WorkbenchService", "isNeedSave"):
            return False
        if (service, method) == ("RunService", "runComponentInEditor"):
            code_path.write_text(
                "pass  #@!io5cd@!-@!custom@!-@!flow@!-@!collect@!-@!2@!\n",
                encoding="utf-8",
            )
            if write_fresh_cache:
                fresh_cache = deepcopy(stale_cache)
                fresh_cache["custom"]["result"][0].append(["手机 A"])
                cache_path.write_text(json.dumps(fresh_cache, ensure_ascii=False), encoding="utf-8")
            return {"state": "success", "message": ""}
        raise AssertionError((service, method))

    monkeypatch.setattr(project, "call_active_editor_renderer", renderer)
    result = project.run_rpa_component(
        paths, str(root), "custom", expected_revision=flow_revision(root), client=object()
    )

    assert result["cache_fresh"] is write_fresh_cache
    assert result["cache"]["data_row_count"] == (2 if write_fresh_cache else 1)
    assert result["runtime_verified"] is verified


def test_collection_output_variables_are_not_encoded_as_string_literals(native_flow, monkeypatch):
    paths, root, _ = native_flow
    task_path = root / "io5cd.task"
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    entities = payload["componentModelEntities"]
    entities["MAIN_COMPONENT"]["args0"][0]["value"].append("collect")
    entities["collect"] = {
        "id": "collect",
        "type": "WebBrowser_DataCollectionV5",
        "args0": [
            {"name": "var0", "value": '"旧采集结果"'},
            {"name": "var1", "value": '"旧Excel路径"'},
            {"name": "var2", "value": '"旧监控数据"'},
            {"name": "config", "value": json.dumps({
                "collectionConfig": {"columnConfigArray": [], "columnSelectorArray": []},
                "pageInfo": {},
            })},
        ],
        "options0": [],
    }
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        project,
        "_live_rpa_contract",
        lambda *_: {
            "output_parameters": ["var0", "var1", "var2"],
            "requires_elements": False,
        },
    )

    preview = project.update_rpa_step_config(
        paths,
        str(root),
        "collect",
        {"var0": "数据采集结果", "var1": "Excel路径", "var2": "监控空列数据"},
        dry_run=True,
    )

    changed = {item["parameter"]: item["after"] for item in preview["changes"]}
    assert changed["var0"] == "数据采集结果"
    assert changed["var1"] == "Excel路径"
    assert changed["var2"] == "监控空列数据"


def test_mcp_cli_preview_and_conflict_are_equivalent(native_flow, monkeypatch):
    _, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: pytest.fail("preview initialized desktop client"))
    config = {"openNewTab": True}
    mcp_result = mcp_server._dispatch("shizai_rpa_update_step", {
        "flow_ref": str(root), "step_id": "open", "config": config, "dry_run": True,
    })
    runner = CliRunner()
    args = ["--json", "workflow", "rpa-update-step", "--flow", str(root), "--step-id", "open",
            "--config-json", json.dumps(config)]
    cli_result = runner.invoke(cli, args + ["--dry-run"])
    assert cli_result.exit_code == 0, cli_result.output
    assert json.loads(cli_result.output) == mcp_result
    denied = runner.invoke(cli, args)
    assert denied.exit_code != 0 and "requires --allow-write" in denied.output
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: object())
    with pytest.raises(PermissionError):
        mcp_server._dispatch("shizai_rpa_update_step", {"flow_ref": str(root), "step_id": "open", "config": config})
    conflict = runner.invoke(cli, args + ["--dry-run", "--expected-revision", "stale"])
    assert conflict.exit_code == 1
    assert json.loads(conflict.output)["error_code"] == "revision_conflict"


def test_sdk_profile_schema_validation_and_structured_conflict(native_flow, monkeypatch):
    _, root, _ = native_flow
    server = mcp_server.build_server(profile="rpa-authoring")
    forbid_side_effects(monkeypatch)

    async def run():
        tools = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
        assert {t.name for t in tools.root.tools} == RPA_AUTHORING_TOOLS
        handler = server.request_handlers[types.CallToolRequest]
        for arguments, expected in [
            ({"flow_ref": str(root), "step_id": "open", "config": {"openNewTab": True},
              "dry_run": True, "expected_revision": "stale"}, "revision_conflict"),
            ({"flow_ref": str(root), "step_id": "open", "config": {}, "dry_run": "false"}, None),
        ]:
            result = await handler(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                name="shizai_rpa_update_step", arguments=arguments,
            )))
            assert result.root.isError
            if expected:
                assert result.root.structuredContent["error_code"] == expected
        hidden = await handler(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="shizai_apply_flow", arguments={},
        )))
        assert hidden.root.isError

    asyncio.run(run())


def test_shared_plan_schema_explains_editable_ids_and_preserves_extensions():
    tools = {t.name: t for t in mcp_server._schemas()}
    schemas = [tools[name].inputSchema["properties"]["plan"] for name in (
        "shizai_rpa_validate_plan", "shizai_rpa_start_design",
    )]
    assert schemas[0]["properties"] == schemas[1]["properties"]
    plan = {"analysis_id": "a", "name": "loop", "output_contract": {}, "strategy": "client-owned",
            "steps": [{"id": "wait", "component": "Basic_Delay_V1", "config": {"second": 1},
                       "loop": {"until": "done"}, "outputs": []}]}
    for schema in schemas:
        Draft202012Validator(schema).validate(plan)
        bad = deepcopy(plan)
        bad["steps"][0]["config"] = "guess"
        assert list(Draft202012Validator(schema).iter_errors(bad))


def test_authoring_profile_reduces_schema_budget_and_keeps_stop_tools():
    full, compact = mcp_server._schemas(), mcp_server._schemas("rpa-authoring")

    def size(ts):
        return len(json.dumps([t.model_dump(mode="json") for t in ts], ensure_ascii=False))
    # Add/insert must remain available to incremental authoring models without
    # raising the existing schema budget.
    assert len(compact) == 43
    assert size(compact) < size(full) * 0.67
    assert size(compact) < 30_600
    assert {"shizai_rpa_stop", "shizai_rpa_debug_stop", "shizai_rpa_browser_session_stop"} <= RPA_AUTHORING_TOOLS
    assert "shizai_rpa_remove_step" in RPA_AUTHORING_TOOLS
    assert "shizai_rpa_save_editor" in RPA_AUTHORING_TOOLS
    assert "shizai_rpa_add_step" in RPA_AUTHORING_TOOLS
    with pytest.raises(ValueError):
        mcp_server.build_server(profile="typo")


def test_remove_step_preview_reports_graph_and_plan_without_writes(native_flow, monkeypatch):
    paths, root, _ = native_flow
    metadata = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    metadata["mcpExecutionPlan"] = {
        "plan_id": "plan-before",
        "steps": [{"id": "open", "component": "WebBrowser_CreateV3"},
                  {"id": "custom", "component": "Custom_Task"}],
    }
    metadata["mcpComponentPlan"] = metadata["mcpExecutionPlan"]["steps"]
    (root / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")
    # A preview still runs the real validator, but must ask it not to drive the
    # designer overlay; the recorder proves the read-only call.
    validations = []
    real_validate = project.validate_rpa_project

    def record(*args, **kwargs):
        validations.append(kwargs)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(project, "validate_rpa_project", record)
    forbid_side_effects(monkeypatch)
    before = snapshot(root)

    result = project.remove_rpa_step(
        paths, str(root), "custom", expected_revision=flow_revision(root), dry_run=True
    )

    assert validations == [{"publish_ui": False}]
    assert result["saved"] is False
    assert result["dry_run"] is True
    assert result["removal"]["step_id"] == "custom"
    assert result["removal"]["remaining_step_ids"] == ["open"]
    assert result["removal"]["element_ids"] == []
    assert result["plan_updates"] == ["mcpComponentPlan", "mcpExecutionPlan", "mcpPlanId", "mcpPlanSource"]
    assert {"valid", "errors", "warnings", "flow_contract"} <= set(result["validation"])
    # The preview reports the state it can actually read (before removal), so a
    # model must not read its errors as damage caused by the pending deletion.
    assert result["validation_basis"] == "current_flow_state"
    assert "删除前" in result["message"]
    assert result["next_call"]["arguments"] == {
        "project_ref": str(root), "step_id": "custom", "expected_revision": flow_revision(root),
        "dry_run": False, "allow_write": True,
    }
    assert snapshot(root) == before


def test_incremental_native_plan_repairs_removed_dependencies_and_keeps_outputs():
    steps = [
        {"id": "open", "component": "WebBrowser_CreateV3", "outputs": ["网页对象"]},
        {"id": "connect", "component": "Database_DBConnect", "depends_on": ["deleted"],
         "outputs": ["数据库对象"]},
        {"id": "create", "component": "Database_SQLExecute", "depends_on": ["deleted"],
         "outputs": ["SQL执行结果"]},
    ]

    normalized = project._normalize_linear_plan_dependencies(steps)

    assert normalized is steps
    assert "depends_on" not in normalized[0]
    assert normalized[1]["depends_on"] == ["open"]
    assert normalized[2]["depends_on"] == ["connect"]
    assert normalized[1]["outputs"] == ["数据库对象"]


def test_remove_step_refuses_stale_revision_and_dependents(native_flow, monkeypatch):
    paths, root, _ = native_flow
    payload = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    entities = payload["componentModelEntities"]
    entities["open"]["args0"].append({"name": "var_name", "value": "网页对象"})
    entities["custom"]["args0"].append({"name": "web_page", "value": "网页对象"})
    (root / "io5cd.task").write_text(json.dumps(payload), encoding="utf-8")
    revision = flow_revision(root)
    (root / "variableInfo").write_text("external edit", encoding="utf-8")
    forbid_side_effects(monkeypatch)
    before = snapshot(root)

    with pytest.raises(WorkflowEditError) as stale:
        project.remove_rpa_step(paths, str(root), "open", expected_revision=revision)
    assert stale.value.result["error_code"] == "revision_conflict"
    assert snapshot(root) == before

    with pytest.raises(WorkflowEditError) as blocked:
        project.remove_rpa_step(paths, str(root), "open", expected_revision=flow_revision(root))
    assert blocked.value.result["error_code"] == "step_has_dependents"
    assert blocked.value.result["dependents"] == [
        {"step_id": "custom", "component": "Custom_Task", "variable": "网页对象", "parameter": "web_page"}
    ]
    assert blocked.value.result["next_call"]["arguments"]["step_id"] == "custom"
    assert snapshot(root) == before


def test_remove_step_rejects_unknown_nested_main_and_last_targets(native_flow, monkeypatch):
    paths, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    before = snapshot(root)
    for step_id, code in (("missing", "step_not_found"), ("MAIN_COMPONENT", "step_not_found"),
                          ("nested", "unsupported_edit_target")):
        with pytest.raises(WorkflowEditError) as error:
            project.remove_rpa_step(paths, str(root), step_id)
        assert error.value.result["error_code"] == code
        assert error.value.result["retryable"] is False
    assert snapshot(root) == before

    payload = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    payload["componentModelEntities"]["MAIN_COMPONENT"]["args0"][0]["value"] = ["open"]
    (root / "io5cd.task").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="last remaining step"):
        project.remove_rpa_step(paths, str(root), "open")
    assert "custom" in json.loads((root / "io5cd.task").read_text(encoding="utf-8"))["componentModelEntities"]


def test_remove_step_deletes_statement_entity_and_plan_entry_then_validates(native_flow, monkeypatch):
    paths, root, _ = native_flow
    metadata = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    metadata.update({
        "mcpUseJsLink": True, "mcpUseJsTable": True, "mcpJsOutputMode": "link",
        "mcpJsOutputVariable": "链接列表", "mcpResultElementsReady": True,
        "mcpExecutionPlan": {"plan_id": "plan-before", "steps": [
            {"id": "open", "component": "WebBrowser_CreateV3"},
            {"id": "custom", "component": "WebBrowser_RunJsInIeBySelector_v3"},
        ]},
    })
    (root / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")
    payload = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    payload["componentModelEntities"]["custom"]["type"] = "WebBrowser_RunJsInIeBySelector_v3"
    payload["componentModelEntities"]["custom"]["args0"].append(
        {"name": "uiSelector", "value": 'SHIZAI_ELEMENT_DICT["field-a"]'}
    )
    (root / "io5cd.task").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(project, "_finalize_native_writeback", lambda *a, **k: {"test_backend": True})
    monkeypatch.setattr(project, "_publish_rpa_ui", lambda *a, **k: {})

    result = project.remove_rpa_step(
        paths, str(root), "custom", expected_revision=flow_revision(root)
    )

    assert result["saved"] is True
    entities = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))["componentModelEntities"]
    assert entities["MAIN_COMPONENT"]["args0"][0]["value"] == ["open"]
    assert "custom" not in entities
    updated = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    assert [step["id"] for step in updated["mcpExecutionPlan"]["steps"]] == ["open"]
    assert updated["mcpExecutionPlan"]["plan_id"] != "plan-before"
    assert updated["mcpPlanId"] == updated["mcpExecutionPlan"]["plan_id"]
    assert updated["mcpComponentPlan"] == updated["mcpExecutionPlan"]["steps"]
    assert {"mcpUseJsLink", "mcpUseJsTable", "mcpJsOutputMode", "mcpJsOutputVariable",
            "mcpResultElementsReady"}.isdisjoint(updated)
    assert result["removal"]["component"] == "WebBrowser_RunJsInIeBySelector_v3"
    assert result["removal"]["element_ids"] == ["field-a"]


def test_remove_step_keeps_js_metadata_when_another_js_node_remains(native_flow, monkeypatch):
    paths, root, _ = native_flow
    metadata = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    metadata["mcpUseJsLink"] = True
    (root / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")
    payload = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    entities = payload["componentModelEntities"]
    entities["custom"]["type"] = "WebBrowser_RunJsInIeBySelector_v3"
    entities["MAIN_COMPONENT"]["args0"][0]["value"].append("js2")
    entities["js2"] = {"id": "js2", "type": "WebBrowser_RunJsInIeBySelector_v3", "args0": []}
    (root / "io5cd.task").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(project, "_finalize_native_writeback", lambda *a, **k: {"test_backend": True})
    monkeypatch.setattr(project, "_publish_rpa_ui", lambda *a, **k: {})

    project.remove_rpa_step(paths, str(root), "custom", expected_revision=flow_revision(root))

    updated = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    assert updated["mcpUseJsLink"] is True


def test_mcp_remove_step_gates_write_and_forwards_desktop(native_flow, monkeypatch):
    _, root, _ = native_flow
    desktop = object()
    calls = []
    forbid_side_effects(monkeypatch)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: desktop)
    monkeypatch.setattr(
        mcp_server.project_core,
        "remove_rpa_step",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"saved": True},
    )
    revision = flow_revision(root)
    arguments = {"project_ref": str(root), "step_id": "custom", "expected_revision": revision}

    with pytest.raises(PermissionError):
        mcp_server._dispatch_impl("shizai_rpa_remove_step", arguments)

    assert mcp_server._dispatch_impl(
        "shizai_rpa_remove_step", {**arguments, "dry_run": True}
    )["saved"] is True
    assert calls[0][1]["client"] is None and calls[0][1]["dry_run"] is True

    assert mcp_server._dispatch_impl(
        "shizai_rpa_remove_step", {**arguments, "allow_write": True, "allow_dependents": True}
    )["saved"] is True
    assert calls[1][0][2] == "custom"
    assert calls[1][1]["client"] is desktop
    assert calls[1][1]["allow_dependents"] is True and calls[1][1]["dry_run"] is False


def test_mcp_cli_remove_step_preview_and_conflict_are_equivalent(native_flow, monkeypatch):
    _, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    revision = flow_revision(root)
    mcp_result = mcp_server._dispatch("shizai_rpa_remove_step", {
        "project_ref": str(root), "step_id": "custom", "expected_revision": revision, "dry_run": True,
    })
    runner = CliRunner()
    args = ["--json", "workflow", "rpa-remove-step", str(root), "custom",
            "--expected-revision", revision]
    cli_result = runner.invoke(cli, args + ["--dry-run"])
    assert cli_result.exit_code == 0, cli_result.output
    assert json.loads(cli_result.output) == mcp_result
    denied = runner.invoke(cli, args)
    assert denied.exit_code != 0 and "requires --allow-write" in denied.output
    conflict = runner.invoke(cli, args + ["--dry-run", "--expected-revision", "stale"])
    assert conflict.exit_code == 1
    assert json.loads(conflict.output)["error_code"] == "revision_conflict"


def test_authoring_profile_accepts_string_next_action_from_extension_status():
    value = {
        "backend": "shizai-agent-native-extension",
        "deep_capture_rpc": True,
        "next_action": "shizai_rpa_capture_windows",
    }

    assert mcp_server.authoring_result("shizai_extension_status", value, {}) == value


def test_compacted_analysis_with_matching_fingerprint_is_restored(monkeypatch):
    monkeypatch.setattr(mcp_server, "_RPA_ANALYSIS_CACHE", {})
    original = mcp_server._cache_rpa_analysis({"analysis_id": "test", "query": "原句", "fields": ["标题"]})
    compact = {key: original[key] for key in ("analysis_id", "analysis_fingerprint")}
    restored, rehydrated = mcp_server._restore_rpa_analysis(compact)
    assert rehydrated and restored == original
    restored["fields"].clear()
    assert mcp_server._RPA_ANALYSIS_CACHE["test"]["fields"] == ["标题"]


def test_edit_detects_change_during_parameter_preparation(native_flow, monkeypatch):
    paths, root, _ = native_flow
    original = project._adapt_rpa_component_config

    def concurrent_change(*args):
        (root / "variableInfo").write_text("external edit", encoding="utf-8")
        return original(*args)

    monkeypatch.setattr(project, "_adapt_rpa_component_config", concurrent_change)
    forbid_side_effects(monkeypatch)
    before = (root / "io5cd.task").read_bytes()
    with pytest.raises(WorkflowEditError, match="流程已变化"):
        project.update_rpa_step_config(paths, str(root), "open", {"openNewTab": True})
    assert (root / "io5cd.task").read_bytes() == before


def test_pending_page_scope_does_not_persist_until_commit(native_flow):
    paths, root, _ = native_flow
    project.import_elements(paths, str(root), [{
        "id": "field", "name": "字段", "title": "结果页", "url": "https://example.test/results",
        "tag": "div", "css_selector": ".results .title",
    }])
    before = snapshot(root)
    pending = json.loads((root / "elements.json").read_text(encoding="utf-8"))
    element_id = project._upsert_page_scope_element(paths, str(root), "field", pending_elements=pending)
    assert element_id in pending["elementMap"]
    assert snapshot(root) == before


def test_preview_diff_redacts_sensitive_parameter_values(native_flow, monkeypatch):
    paths, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    result = project.update_rpa_step_config(paths, str(root), "custom", {"password": "new-secret"}, dry_run=True)
    serialized = json.dumps(result)
    assert "new-secret" not in serialized and "test-secret" not in serialized
    assert result["changes"][0]["parameter"] == "password"


@pytest.mark.parametrize("step_id", ["nested", "MAIN_COMPONENT"])
def test_unsupported_graph_target_fails_before_any_write(native_flow, monkeypatch, step_id):
    paths, root, _ = native_flow
    forbid_side_effects(monkeypatch)
    before = snapshot(root)
    with pytest.raises(WorkflowEditError) as error:
        project.update_rpa_step_config(paths, str(root), step_id, {"statements": []})
    assert error.value.result["error_code"] == "unsupported_edit_target"
    assert snapshot(root) == before
    if step_id == "nested":
        context = read_edit_context(paths, str(root), step_id=step_id)
        assert context["selected_step"]["parameter_edit_supported"] is False
        assert context["next_call"] is None


@pytest.mark.parametrize("profile,count", [("full", 67), ("rpa-authoring", 43)])
def test_real_stdio_initializes_lists_and_closes_cleanly(profile, count):
    async def run():
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "shizai_agent.core.mcp_server"],
                env={**os.environ, "SHIZAI_MCP_TOOL_PROFILE": profile},
            )
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    assert len(listed.tools) == count
                    assert "expected_revision" in initialized.instructions
                    if profile == "rpa-authoring":
                        assert "精简工具集" in initialized.instructions
            errors.seek(0)
            assert errors.read() == ""

    asyncio.run(asyncio.wait_for(run(), timeout=30))


def _plan_backed_collection_flow(root, *, designated_obj, surplus):
    task = json.loads((root / "io5cd.task").read_text(encoding="utf-8"))
    task["componentModelEntities"] = {
        "MAIN_COMPONENT": {"id": "MAIN_COMPONENT", "type": "main",
                           "args0": [{"name": "statements", "value": ["open", "collect"]}]},
        "open": {"id": "open", "type": "WebBrowser_CreateV3", "args0": [
            {"name": "url", "value": '"https://example.test"'},
            {"name": "var_name", "value": "shop_web"},
        ]},
        "collect": {"id": "collect", "type": "WebBrowser_DataCollectionV5", "args0": [
            {"name": "designated_obj", "value": designated_obj},
            {"name": "var0", "value": "数据采集结果"},
            {"name": "var1", "value": "Excel路径"},
            {"name": "var2", "value": surplus},
        ]},
    }
    (root / "io5cd.task").write_text(json.dumps(task), encoding="utf-8")
    metadata = json.loads((root / "szrpa.json").read_text(encoding="utf-8"))
    metadata["mcpComponentPlan"] = [
        {"id": "open", "component": "WebBrowser_CreateV3", "config": {}},
        {"id": "collect", "component": "WebBrowser_DataCollectionV5", "config": {},
         "outputs": ["数据采集结果", "Excel路径"]},
    ]
    (root / "szrpa.json").write_text(json.dumps(metadata), encoding="utf-8")


def test_plan_backed_browser_object_name_and_surplus_output_slot_validate(native_flow):
    """A renamed browser object and a native-only trailing slot must both pass."""
    paths, root, _ = native_flow
    _plan_backed_collection_flow(root, designated_obj="shop_web", surplus="监控空列数据")

    validation = project.validate_rpa_step(paths, str(root), "collect")

    checks = {item["name"]: item for item in validation["checks"]}
    assert checks["collection_web_page_binding"]["passed"] is True
    assert "shop_web" in checks["collection_web_page_binding"]["detail"]
    assert checks["output_variables"]["passed"] is True
    assert "监控空列数据" in checks["output_variables"]["detail"]
    # The synthetic flow still lacks a native collection config and columns, so
    # only assert that neither fixed check reports an error here.
    assert not [
        item
        for item in validation["errors"]
        if "browser object" in item or "expected outputs" in item
    ]


def test_validation_still_rejects_unknown_browser_object_and_surplus_slot(native_flow):
    """Neither an invented browser name nor an invented slot may pass."""
    paths, root, _ = native_flow
    _plan_backed_collection_flow(root, designated_obj="其他对象", surplus="错误结果")

    validation = project.validate_rpa_step(paths, str(root), "collect")

    checks = {item["name"]: item for item in validation["checks"]}
    assert checks["collection_web_page_binding"]["passed"] is False
    assert "accepted names" in checks["collection_web_page_binding"]["detail"]
    assert "shop_web" in checks["collection_web_page_binding"]["detail"]
    assert checks["output_variables"]["passed"] is False
    assert "surplus native slots: 错误结果" in checks["output_variables"]["detail"]
    assert validation["valid"] is False
