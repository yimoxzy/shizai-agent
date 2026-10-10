"""Add a 1+1 variable step and verify two exact MCP component steps."""

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

from shizai_agent.core.paths import ShizaiPaths

try:
    from scripts.probe_designer_reload import probe_designer_reload
except ModuleNotFoundError:  # Direct script execution.
    from probe_designer_reload import probe_designer_reload

ROOT = Path(__file__).resolve().parents[1]
CALC_STEP_ID = "calc-one-plus-one"


def _payload(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", [])
    raw = getattr(content[0], "text", None) if content else None
    if not isinstance(raw, str):
        raise RuntimeError("MCP tool did not return JSON text")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP tool returned non-JSON text: {raw}") from exc
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool failed: {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object payload")
    return payload


async def _call_timed(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    result = _payload(await session.call_tool(name, arguments))
    result["probe_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def add_calculation_step(flow_ref: str) -> dict[str, Any]:
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
            before = await _call_timed(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit"},
            )
            before_steps = before.get("steps") if isinstance(before.get("steps"), list) else []
            existing = next(
                (step for step in before_steps if isinstance(step, dict) and step.get("id") == CALC_STEP_ID),
                None,
            )
            added = None
            if existing is None:
                if len(before_steps) != 1:
                    raise RuntimeError(f"expected one component before append, found {len(before_steps)}")
                added = await _call_timed(
                    session,
                    "shizai_rpa_add_step",
                    {
                        "flow_ref": flow_ref,
                        "component": "Basic_SetVariable_V1",
                        "step_id": CALC_STEP_ID,
                        "label": "计算 1 + 1",
                        "config": {
                            "var_name": "计算结果",
                            "variableType": "rpa_int",
                            "value": {"expression": "1 + 1"},
                            "var_ret": 1,
                        },
                        "allow_write": True,
                    },
                )
            after = await _call_timed(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit"},
            )
            after_steps = after.get("steps") if isinstance(after.get("steps"), list) else []
            if len(after_steps) != 2 or [step.get("id") for step in after_steps] != ["ybQEw", CALC_STEP_ID]:
                raise RuntimeError(f"unexpected two-step readback: {after_steps}")
            calculation = await _call_timed(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit", "step_id": CALC_STEP_ID},
            )
            return {
                "server": {"name": initialized.serverInfo.name, "version": initialized.serverInfo.version},
                "before": before,
                "added": added,
                "after": after,
                "calculation": calculation,
            }


async def verify_two_steps(flow_ref: str) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shizai_agent.core.mcp_server"],
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            flow = await _call_timed(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": flow_ref, "detail": "edit"},
            )
            steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
            if [step.get("id") for step in steps] != ["ybQEw", CALC_STEP_ID]:
                raise RuntimeError("the persisted flow is not the expected two-component probe")

            report: dict[str, Any] = {"flow": flow}
            try:
                report["cleanup_before"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_stop",
                    {"project_ref": flow_ref, "allow_write": True},
                )
                report["started"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_start",
                    {"project_ref": flow_ref, "start_from": 1, "start_timeout": 30, "allow_write": True},
                )
                report["before_first"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_status",
                    {"project_ref": flow_ref},
                )
                report["first_next"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_command",
                    {"project_ref": flow_ref, "action": "next", "wait_seconds": 30, "allow_write": True},
                )
                report["after_first"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_status",
                    {"project_ref": flow_ref},
                )
                first_current = (
                    report["after_first"].get("current")
                    if isinstance(report["after_first"].get("current"), dict)
                    else {}
                )
                first_is_exact = (
                    report["first_next"].get("completed_component_id") == "ybQEw"
                    and first_current.get("component_id") == CALC_STEP_ID
                    and first_current.get("component_boundary") is True
                )
                report["first_next_stopped_at_second_boundary"] = first_is_exact
                if not first_is_exact:
                    raise RuntimeError(
                        f"first next did not stop at the second component boundary: {report['after_first']}"
                    )
                report["second_next"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_command",
                    {"project_ref": flow_ref, "action": "next", "wait_seconds": 30, "allow_write": True},
                )
                report["after_second"] = await _call_timed(
                    session,
                    "shizai_rpa_debug_status",
                    {"project_ref": flow_ref},
                )
            except Exception as exc:
                report["probe_error"] = str(exc)
            finally:
                try:
                    report["cleanup"] = await _call_timed(
                        session,
                        "shizai_rpa_debug_stop",
                        {"project_ref": flow_ref, "allow_write": True},
                    )
                except Exception as exc:
                    report["cleanup"] = {"error": str(exc)}
            return report


async def run(flow_ref: str) -> dict[str, Any]:
    added = await add_calculation_step(flow_ref)
    revision = added["after"].get("revision")
    if added["added"] is None:
        reload_result = {
            "kind": "designer_reload_probe",
            "reload_requested": False,
            "message": "No persisted flow change; the active editor already contains both components.",
        }
    else:
        reload_result = probe_designer_reload(
            ShizaiPaths.from_environment(),
            flow_ref,
            expected_revision=revision,
            allow_reload=True,
            strategy="active-editor-reload",
        )
    stepped = await verify_two_steps(flow_ref)
    return {
        "kind": "mcp_two_component_single_step_probe",
        "added": added,
        "reload": reload_result,
        "stepped": stepped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    args = parser.parse_args()
    result = asyncio.run(run(args.flow_ref))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
