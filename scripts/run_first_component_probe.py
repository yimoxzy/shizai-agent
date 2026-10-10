"""Run exactly the first native RPA component through one MCP stdio session."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def _payload(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", [])
    text = getattr(content[0], "text", None) if content else None
    if not isinstance(text, str):
        raise RuntimeError("MCP tool did not return JSON text")
    payload = json.loads(text)
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool failed: {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object payload")
    return payload


async def run_first_component(flow_ref: str) -> dict[str, Any]:
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
            flow = _payload(
                await session.call_tool(
                    "shizai_rpa_read_flow",
                    {"flow_ref": flow_ref, "detail": "edit"},
                )
            )
            steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
            if len(steps) != 1:
                raise RuntimeError(f"expected exactly one component, found {len(steps)}; refusing to run")
            first = steps[0]
            capabilities = _payload(
                await session.call_tool("shizai_rpa_debug_capabilities", {"project_ref": flow_ref})
            )
            cleanup_before = None
            if capabilities.get("cleanup_required") or capabilities.get("active_session"):
                cleanup_before = _payload(
                    await session.call_tool(
                        "shizai_rpa_debug_stop",
                        {"project_ref": flow_ref, "allow_write": True},
                    )
                )

            started = None
            stepped = None
            status = None
            cleanup_after = None
            start_attempts = 0
            try:
                while True:
                    start_attempts += 1
                    try:
                        started = _payload(
                            await session.call_tool(
                                "shizai_rpa_debug_start",
                                {
                                    "project_ref": flow_ref,
                                    "start_from": 1,
                                    "start_timeout": 30,
                                    "allow_write": True,
                                },
                            )
                        )
                        break
                    except RuntimeError:
                        if start_attempts >= 2:
                            raise
                        cleanup_before = _payload(
                            await session.call_tool(
                                "shizai_rpa_debug_stop",
                                {"project_ref": flow_ref, "allow_write": True},
                            )
                        )
                status_before_step = _payload(
                    await session.call_tool("shizai_rpa_debug_status", {"project_ref": flow_ref})
                )
                stepped = _payload(
                    await session.call_tool(
                        "shizai_rpa_debug_command",
                        {
                            "project_ref": flow_ref,
                            "action": "next",
                            "wait_seconds": 30,
                            "allow_write": True,
                        },
                    )
                )
                status = _payload(
                    await session.call_tool("shizai_rpa_debug_status", {"project_ref": flow_ref})
                )
            finally:
                try:
                    cleanup_after = _payload(
                        await session.call_tool(
                            "shizai_rpa_debug_stop",
                            {"project_ref": flow_ref, "allow_write": True},
                        )
                    )
                except Exception as exc:
                    cleanup_after = {"error": str(exc)}

            return {
                "kind": "mcp_first_component_probe",
                "server": {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                },
                "flow_ref": flow_ref,
                "revision": flow.get("revision"),
                "component_count": len(steps),
                "first_component": first,
                "capabilities": capabilities,
                "cleanup_before": cleanup_before,
                "start_attempts": start_attempts,
                "started": started,
                "status_before_step": status_before_step,
                "stepped": stepped,
                "status": status,
                "cleanup_after": cleanup_after,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    args = parser.parse_args()
    result = asyncio.run(run_first_component(args.flow_ref))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
