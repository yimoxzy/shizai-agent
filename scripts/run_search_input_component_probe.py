"""Bind a verified search element and run only the input component in the designer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from shizai_agent.core import project as project_core
from shizai_agent.core.cloud_api import ShizaiDesktopAgent
from shizai_agent.core.native_ui import call_active_editor_renderer
from shizai_agent.core.paths import ShizaiPaths

try:
    from scripts.probe_designer_reload import probe_designer_reload
except ModuleNotFoundError:  # Direct script execution.
    from probe_designer_reload import probe_designer_reload

ROOT = Path(__file__).resolve().parents[1]


def _payload(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", [])
    raw = getattr(content[0], "text", None) if content else None
    if not isinstance(raw, str):
        raise RuntimeError("MCP tool did not return JSON text")
    payload = json.loads(raw)
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool failed: {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object payload")
    return payload


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    payload = _payload(await session.call_tool(name, arguments))
    payload["probe_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return payload


async def prepare_component(
    flow_ref: str,
    source_project_ref: str,
    source_element_id: str,
    step_id: str,
) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shizai_agent.core.mcp_server"],
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            before = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit", "step_id": step_id},
            )
            steps = before.get("steps") if isinstance(before.get("steps"), list) else []
            if [item.get("id") for item in steps] != ["ybQEw", "calc-one-plus-one", step_id]:
                raise RuntimeError(f"unexpected component order: {steps}")

            try:
                capture_cleanup = await _call(
                    session,
                    "shizai_extension_stop_capture",
                    {"project_ref": flow_ref, "allow_write": True},
                )
            except Exception as exc:
                capture_cleanup = {"error": str(exc)}

            imported = await _call(
                session,
                "shizai_project_import_elements",
                {
                    "project_ref": flow_ref,
                    "source_project_ref": source_project_ref,
                    "element_ids": [source_element_id],
                    "allow_write": True,
                },
            )
            bound = await _call(
                session,
                "shizai_rpa_set_step_elements",
                {
                    "flow_ref": flow_ref,
                    "step_id": step_id,
                    "element_ids": [source_element_id],
                    "allow_write": True,
                },
            )
            rebound = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit", "step_id": step_id},
            )
            selected = rebound.get("selected_step") if isinstance(rebound.get("selected_step"), dict) else {}
            native_parameters = (
                selected.get("native_parameters")
                if isinstance(selected.get("native_parameters"), dict)
                else {}
            )
            output_update = None
            if native_parameters.get("var_name") != "校验结果":
                preview = await _call(
                    session,
                    "shizai_rpa_update_step",
                    {
                        "flow_ref": flow_ref,
                        "step_id": step_id,
                        "config": {"var_name": "校验结果"},
                        "dry_run": True,
                        "expected_revision": rebound.get("revision"),
                    },
                )
                output_update = {
                    "preview": preview,
                    "applied": await _call(
                        session,
                        "shizai_rpa_update_step",
                        {
                            "flow_ref": flow_ref,
                            "step_id": step_id,
                            "config": {"var_name": "校验结果"},
                            "expected_revision": rebound.get("revision"),
                            "allow_write": True,
                        },
                    ),
                }
            validation = await _call(
                session,
                "shizai_rpa_validate_step",
                {"flow_ref": flow_ref, "step_id": step_id, "strict": False},
            )
            after = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit", "step_id": step_id},
            )
            return {
                "server": {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                },
                "capture_cleanup": capture_cleanup,
                "before": before,
                "imported": imported,
                "bound": bound,
                "output_update": output_update,
                "validation": validation,
                "after": after,
            }


def _cached_browser(flow_ref: str) -> dict[str, Any]:
    cache_path = Path(flow_ref) / ".sz" / "variable_cache" / "io5cd.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    browser = payload.get("ybQEw")
    if not isinstance(browser, dict):
        raise RuntimeError("the first component has no cached browser result")
    result = browser.get("result")
    if not isinstance(result, dict) or not str(result.get("url") or "").startswith("https://www.jd.com"):
        raise RuntimeError(f"cached browser is not the expected JD page: {result}")
    return {
        "cache_path": str(cache_path),
        "source_component_id": browser.get("componentId"),
        "variable_names": browser.get("varNames"),
        "url": result.get("url"),
        "title": result.get("title"),
    }


def run_only_component(flow_ref: str, step_id: str) -> dict[str, Any]:
    paths = ShizaiPaths.from_environment()
    project_dir = project_core.resolve_project(paths, flow_ref)
    package = project_core._native_package_summary(paths, project_dir)
    edit_state = json.loads((project_dir / "edit-state.json").read_text(encoding="utf-8"))
    editor_id = str(edit_state.get("currentEditorId") or "").strip()
    if not editor_id or not package.get("application_id"):
        raise RuntimeError("active editor identity is incomplete")
    request = {
        "projectId": package["application_id"],
        "editorId": editor_id,
        "componentIds": [step_id],
        "isIPA": False,
        "isUseTrigger": False,
        "useCache": True,
        "createCodeParams": {"useCache": True},
    }
    client = ShizaiDesktopAgent()
    editor_ready = call_active_editor_renderer(
        client,
        "WorkbenchService",
        "isNeedSave",
        [],
    )
    started = time.monotonic()
    result = call_active_editor_renderer(
        client,
        "RunService",
        "runComponentInEditor",
        [request],
    )
    elapsed = round(time.monotonic() - started, 3)
    component_code_path = project_dir / "projects" / "_temp" / "component_run.py"
    code = component_code_path.read_text(encoding="utf-8") if component_code_path.is_file() else ""
    return {
        "kind": "native_designer_single_component_probe",
        "rpc": "$CurrentActiveEditorClientId$/RunService.runComponentInEditor",
        "editor_ready": editor_ready,
        "request": request,
        "result": result,
        "elapsed_seconds": elapsed,
        "component_code_path": str(component_code_path),
        "code_contains_target_component": step_id in code,
        "code_contains_open_browser": "WebBrowser.CreateV3" in code,
        "code_contains_calculation": "Basic.SetVariable_V1" in code,
        "code_contains_phone": "手机" in code,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    prepared = await prepare_component(
        args.flow_ref,
        args.source_project_ref,
        args.source_element_id,
        args.step_id,
    )
    revision = prepared["after"].get("revision")
    reload_result = probe_designer_reload(
        ShizaiPaths.from_environment(),
        args.flow_ref,
        expected_revision=revision,
        allow_reload=True,
        strategy="active-editor-reload",
    )
    time.sleep(4.0)
    cached_browser = _cached_browser(args.flow_ref)
    component_run = run_only_component(args.flow_ref, args.step_id)
    return {
        "kind": "mcp_bind_and_native_single_component_probe",
        "prepared": prepared,
        "reload": reload_result,
        "cached_browser": cached_browser,
        "component_run": component_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("source_project_ref")
    parser.add_argument("--source-element-id", default="uia:35998227")
    parser.add_argument("--step-id", default="input-jd-phone")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
