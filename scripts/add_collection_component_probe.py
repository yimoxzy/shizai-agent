"""Append an unbound native DataCollectionV5 component through the MCP server."""

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
    result = _payload(await session.call_tool(name, arguments))
    result["probe_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def append_collection(flow_ref: str, step_id: str) -> dict[str, Any]:
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
                if existing.get("component") != "WebBrowser_DataCollectionV5":
                    raise RuntimeError(f"step id {step_id} is already used by {existing.get('component')}")
                added: dict[str, Any] = {"reused_existing": True, "step": existing}
            else:
                added = await _call(
                    session,
                    "shizai_rpa_add_step",
                    {
                        "flow_ref": flow_ref,
                        "component": "WebBrowser_DataCollectionV5",
                        "step_id": step_id,
                        "label": "采集京东手机商品",
                        "config": {
                            "designated_info": 1,
                            "designated_obj": "网页对象",
                            "region": 1,
                            "var0": "手机商品数据",
                            "monitor_data": 0,
                            "toExcel": 0,
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
                "kind": "mcp_add_collection_component_probe",
                "server": {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                },
                "before": before,
                "added": added,
                "after": after,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--step-id", default="collect-jd-phones")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(append_collection(args.flow_ref, args.step_id)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
