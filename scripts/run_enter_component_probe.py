"""Append an Enter key component through MCP and execute it in the active designer."""

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


async def append_enter(flow_ref: str, step_id: str) -> dict[str, Any]:
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
                {"flow_ref": flow_ref, "detail": "edit"},
            )
            steps = before.get("steps") if isinstance(before.get("steps"), list) else []
            existing = next(
                (item for item in steps if isinstance(item, dict) and item.get("id") == step_id),
                None,
            )
            if existing is not None:
                if existing.get("component") != "Keyboard_PressNew":
                    raise RuntimeError(f"step id {step_id} is already used by {existing.get('component')}")
                added: dict[str, Any] = {"reused_existing": True, "step": existing}
            else:
                added = await _call(
                    session,
                    "shizai_rpa_add_step",
                    {
                        "flow_ref": flow_ref,
                        "component": "Keyboard_PressNew",
                        "step_id": step_id,
                        "label": "按 Enter 搜索手机",
                        "config": {
                            "key": "13",
                            "type": "press",
                            "auxiliary_key": [],
                            "input_us": "1",
                        },
                        "allow_write": True,
                    },
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
                "before": before,
                "added": added,
                "after": after,
            }


def _run_components(flow_ref: str, step_ids: list[str]) -> dict[str, Any]:
    if not step_ids:
        raise ValueError("step_ids must not be empty")
    paths = ShizaiPaths.from_environment()
    project_dir = project_core.resolve_project(paths, flow_ref)
    package = project_core._native_package_summary(paths, project_dir)
    edit_state = json.loads((project_dir / "edit-state.json").read_text(encoding="utf-8"))
    editor_id = str(edit_state.get("currentEditorId") or "").strip()
    application_id = str(package.get("application_id") or "").strip()
    if not editor_id or not application_id:
        raise RuntimeError("active editor identity is incomplete")

    request = {
        "projectId": application_id,
        "editorId": editor_id,
        "componentIds": step_ids,
        "isIPA": False,
        "isUseTrigger": False,
        "useCache": True,
        "createCodeParams": {"useCache": True},
    }
    client = ShizaiDesktopAgent()
    editor_ready = call_active_editor_renderer(client, "WorkbenchService", "isNeedSave", [])
    started = time.monotonic()
    result = call_active_editor_renderer(
        client,
        "RunService",
        "runComponentInEditor",
        [request],
    )
    elapsed = round(time.monotonic() - started, 3)
    code_path = project_dir / "projects" / "_temp" / "component_run.py"
    code = code_path.read_text(encoding="utf-8") if code_path.is_file() else ""
    return {
        "step_ids": step_ids,
        "editor_ready": editor_ready,
        "result": result,
        "elapsed_seconds": elapsed,
        "component_code_path": str(code_path),
        "code_contains_enter": "Keyboard.PressNew" in code and 'key="13"' in code,
        "code_contains_input": "Element.SetValue_V6" in code,
        "code_contains_open_browser": "WebBrowser.CreateV3" in code,
    }


def _run_component(flow_ref: str, step_id: str) -> dict[str, Any]:
    return _run_components(flow_ref, [step_id])


async def run(args: argparse.Namespace) -> dict[str, Any]:
    prepared = await append_enter(args.flow_ref, args.step_id)
    revision = prepared["after"].get("revision")
    reload_result = probe_designer_reload(
        ShizaiPaths.from_environment(),
        args.flow_ref,
        expected_revision=revision,
        allow_reload=True,
        strategy="active-editor-reload",
    )
    time.sleep(args.editor_wait)

    # Run both components in one native request so no debugger pause can move
    # focus away from the browser between setting the value and pressing Enter.
    continuous_run = _run_components(args.flow_ref, [args.input_step_id, args.step_id])
    return {
        "kind": "mcp_enter_component_probe",
        "prepared": prepared,
        "reload": reload_result,
        "continuous_input_enter_run": continuous_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--step-id", default="press-enter-search")
    parser.add_argument("--input-step-id", default="input-jd-phone")
    parser.add_argument("--editor-wait", type=float, default=4.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
