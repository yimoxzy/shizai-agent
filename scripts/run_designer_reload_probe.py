"""Append one component through MCP, then request a designer canvas reload."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from shizai_agent.core.paths import ShizaiPaths

try:
    from scripts.probe_designer_reload import probe_designer_reload
except ModuleNotFoundError:  # Direct `python scripts/run_designer_reload_probe.py` execution.
    from probe_designer_reload import probe_designer_reload

ROOT = Path(__file__).resolve().parents[1]


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError("MCP tool returned an error: " + repr(result))
    content = getattr(result, "content", [])
    if not content or not isinstance(getattr(content[0], "text", None), str):
        raise RuntimeError("MCP tool did not return JSON text")
    payload = json.loads(content[0].text)
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object payload")
    return payload


async def append_through_mcp(flow_ref: str, step_id: str, expected_before_count: int) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shizai_agent.core.mcp_server"],
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            initialize = await session.initialize()
            before = _tool_payload(
                await session.call_tool(
                    "shizai_rpa_read_flow",
                    {"flow_ref": flow_ref, "detail": "edit"},
                )
            )
            before_steps = before.get("steps") if isinstance(before.get("steps"), list) else []
            if len(before_steps) != expected_before_count:
                raise RuntimeError(
                    f"expected {expected_before_count} steps before the probe, found {len(before_steps)}; refusing to append"
                )
            if any(item.get("id") == step_id for item in before_steps if isinstance(item, dict)):
                raise RuntimeError(f"probe step already exists: {step_id}")

            added = _tool_payload(
                await session.call_tool(
                    "shizai_rpa_add_step",
                    {
                        "flow_ref": flow_ref,
                        "component": "WebBrowser_CreateV3",
                        "step_id": step_id,
                        "label": "Open JD probe",
                        "config": {"type": "chrome", "url": "www.jd.com", "openNewTab": False},
                        "allow_write": True,
                    },
                )
            )
            after = _tool_payload(
                await session.call_tool(
                    "shizai_rpa_read_flow",
                    {"flow_ref": flow_ref, "detail": "edit"},
                )
            )
            after_steps = after.get("steps") if isinstance(after.get("steps"), list) else []
            if len(after_steps) != expected_before_count + 1 or not any(
                item.get("id") == step_id for item in after_steps if isinstance(item, dict)
            ):
                raise RuntimeError("MCP add_step did not produce the expected readback")
            return {
                "server_name": initialize.serverInfo.name,
                "server_version": initialize.serverInfo.version,
                "before_count": len(before_steps),
                "after_count": len(after_steps),
                "revision": after.get("revision"),
                "added": added,
            }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    mcp_result = await append_through_mcp(args.flow_ref, args.step_id, args.expected_before_count)
    reload_result = probe_designer_reload(
        ShizaiPaths.from_environment(),
        args.flow_ref,
        expected_revision=mcp_result["revision"],
        allow_reload=True,
        strategy="active-editor-reload",
    )
    return {"kind": "mcp_write_then_designer_reload_probe", "mcp": mcp_result, "reload": reload_result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--step-id", default="reload-probe-open-jd-3")
    parser.add_argument("--expected-before-count", type=int, default=2)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
