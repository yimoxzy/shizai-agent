"""Real-file and installed-command tests for the Shizai harness."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from shizai_agent.core import agent, component_catalog, project
from shizai_agent.core.cloud_api import ShizaiDesktopAgent
from shizai_agent.core.paths import ShizaiPaths


def _resolve_cli(name: str) -> list[str]:
    """Resolve the installed CLI, with a development fallback."""
    force = os.environ.get("SHIZAI_FORCE_INSTALLED", "") == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH")
    return [sys.executable, "-m", "shizai_agent"]


def _real_paths() -> ShizaiPaths:
    return ShizaiPaths.from_environment()


def _first_real_project() -> Path:
    paths = _real_paths()
    projects = project.list_projects(paths)
    if not projects:
        raise AssertionError("No local Shizai projects were found")
    return Path(projects[0]["path"])


def test_real_local_project_has_native_files():
    target = _first_real_project()
    assert (target / "szrpa.json").is_file()
    assert (target / "elements.json").is_file()


def test_real_project_inspection_reads_metadata():
    target = _first_real_project()
    summary = project.inspect_project(_real_paths(), str(target))
    assert summary["id"] == target.name
    assert summary["file_count"] > 0


def test_real_project_validation_returns_checks():
    target = _first_real_project()
    result = project.validate_project(_real_paths(), target.name)
    assert result["checks"]
    assert result["project"]["path"] == str(target)


def test_real_project_export_is_valid_zip(tmp_path):
    target = _first_real_project()
    output = tmp_path / "real-project.zip"
    result = project.export_project(_real_paths(), target.name, str(output))
    assert result["size"] > 0
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert {"szrpa.json", "elements.json"}.issubset(names)
    print(f"\n  ZIP: {output} ({result['size']:,} bytes)")


def test_real_agent_brain_bundle_is_discoverable():
    result = agent.list_agents(_real_paths())
    assert result["count"] >= 1
    assert result["agents"][0]["id"] == "agent-brain"


def test_real_bundle_lists_rpc_and_component_surface():
    result = agent.capabilities(_real_paths())
    assert "getWorkflowDraft" in result["discovered"]["methods"]
    assert "getMcpServerList" in result["discovered"]["methods"]
    catalog = agent.query_components(client=ShizaiDesktopAgent())
    assert "database" in catalog["node_types"]
    assert "code" in catalog["node_types"]
    assert catalog["generation_rules"]["draft_write_method"].endswith("saveWorkflowDraft")


def test_real_builtin_component_contracts_have_minimum_shape():
    """Smoke-test every installed component contract without executing it."""
    records = component_catalog.builtin_component_index(_real_paths())
    assert len(records) >= 900

    for component_type, record in records.items():
        assert record["type"] == component_type
        assert record["native_type"]
        assert isinstance(record.get("inputs"), list)
        assert isinstance(record.get("options"), list)
        assert isinstance(record.get("outputs"), dict)
        input_names = [item["name"] for item in record["inputs"] if isinstance(item, dict) and item.get("name")]
        option_names = [item["name"] for item in record["options"] if isinstance(item, dict) and item.get("name")]
        assert len(input_names) == len(set(input_names))
        assert len(option_names) == len(set(option_names))
        if record.get("designer_static") and not record.get("code"):
            # These two frontend-only entries are discoverable but have no
            # engine code record and are intentionally not materialized.
            continue
        assert isinstance(record.get("code"), list)
        assert len(record["code"]) == 1
        assert "." in str(record["code"][0].get("func") or "")
        assert isinstance(record["code"][0].get("args"), list)


class TestCLISubprocess:
    CLI_BASE = _resolve_cli("shizai-agent")

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.CLI_BASE + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )

    def test_help(self):
        result = self._run(["--help"])
        assert result.returncode == 0
        assert "project" in result.stdout

    def test_all_workflow_command_help(self):
        commands = [
            "rpa-add-step",
            "rpa-assistant-run-step",
            "rpa-bind-elements",
            "rpa-bind-pagination",
            "rpa-browser-session-start",
            "rpa-browser-session-status",
            "rpa-browser-session-stop",
            "rpa-capture-element",
            "rpa-capture-elements",
            "rpa-capture-page-fields",
            "rpa-capture-tree",
            "rpa-capture-windows",
            "rpa-component-catalog",
            "rpa-components",
            "rpa-context",
            "rpa-debug-capabilities",
            "rpa-debug-continue",
            "rpa-debug-next",
            "rpa-debug-start",
            "rpa-debug-status",
            "rpa-debug-step-in",
            "rpa-debug-step-out",
            "rpa-debug-stop",
            "rpa-materialize",
            "rpa-open-browser",
            "rpa-prune-elements",
            "rpa-read",
            "rpa-reorder-steps",
            "rpa-repair-flow",
            "rpa-run",
            "rpa-runtime-preflight",
            "rpa-scan-page",
            "rpa-start-design",
            "rpa-status",
            "rpa-stop",
            "rpa-sync-diagnose",
            "rpa-update-step",
            "rpa-validate",
            "rpa-validate-flow",
            "rpa-validate-step",
            "validate",
        ]
        for command in commands:
            result = self._run(["workflow", command, "--help"])
            assert result.returncode == 0, f"{command}: {result.stderr}"

    def test_doctor_json(self):
        result = self._run(["--json", "doctor"])
        data = json.loads(result.stdout)
        assert "checks" in data
        assert "business_port" in data

    def test_agent_auth_status_json(self):
        result = self._run(["--json", "agent", "auth-status"])
        data = json.loads(result.stdout)
        assert isinstance(data["ready"], bool)
        assert isinstance(data["desktop"]["reachable"], bool)
        assert isinstance(data["desktop"]["authenticated"], bool)
        assert isinstance(data["cloud_rest"]["ready"], bool)
        assert not {"token", "password", "secret"}.intersection(json.dumps(data).lower().split('"'))

    def test_project_list_json(self):
        result = self._run(["--json", "project", "list"])
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data

    def test_agent_capabilities_json(self):
        result = self._run(["--json", "agent", "capabilities"])
        data = json.loads(result.stdout)
        assert data["mode"] == "local-rpc"
        assert data["method_specs"]
        assert "getWorkflowDraft" in data["discovered"]["methods"]

    def test_agent_components_json(self):
        result = self._run(["--json", "agent", "components"])
        data = json.loads(result.stdout)
        assert "database" in data["node_types"]
        assert data["tool_providers"]["builtin"]

    def test_native_component_catalog_json(self):
        result = self._run(
            [
                "--json",
                "workflow",
                "rpa-component-catalog",
                "--component-type",
                "WebBrowser_DataCrawl",
                "--detail",
                "contract",
                "--limit",
                "1",
            ]
        )
        data = json.loads(result.stdout)
        assert data["ready"] is True
        assert data["total_components"] >= 900
        assert data["components"][0]["type"] == "WebBrowser_DataCrawl"
        assert data["components"][0]["outputs"]["primary"] == "var_name"

    def test_native_debug_capabilities_json(self):
        project_ref = str(_first_real_project())
        result = self._run(["--json", "workflow", "rpa-debug-capabilities", project_ref])
        data = json.loads(result.stdout)
        assert data["kind"] == "native_rpa_debug_capabilities"
        assert data["ready"] is True
        assert data["capabilities"]["component_next"]["supported"] is True

    def test_agent_mcp_servers_json_is_redacted(self):
        result = self._run(["--json", "agent", "mcp-servers", "--limit", "100"])
        data = json.loads(result.stdout)
        assert data["total"] >= 1
        assert "[REDACTED]" in result.stdout or all(
            "api_key" not in json.dumps(item).lower() for item in data.get("data", [])
        )
