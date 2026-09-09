"""Editing regression tests use real native files, never the user's designer."""

import asyncio
import json
import os
import sys
import tempfile
from copy import deepcopy

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from shizai_agent.core import mcp_server, project
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
        ], "options0": [{"name": "enabled", "value": "1"}]},
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
    assert "test-secret" not in json.dumps(selected)
    assert selected["revision"] == summary["revision"]
    assert snapshot(root) == before


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
    assert len(compact) == 29
    assert size(compact) < size(full) * 0.65
    assert size(compact) < 25_000
    assert {"shizai_rpa_stop", "shizai_rpa_debug_stop", "shizai_rpa_browser_session_stop"} <= RPA_AUTHORING_TOOLS
    with pytest.raises(ValueError):
        mcp_server.build_server(profile="typo")


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


@pytest.mark.parametrize("profile,count", [("full", 56), ("rpa-authoring", 29)])
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
