"""Bind a native collection step from verified DeepCapture reference elements."""

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
DEFAULT_ELEMENT_IDS = ["uia:3452586", "uia:43269440", "uia:26916563"]


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


async def bind(args: argparse.Namespace) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "shizai_agent.core.mcp_server"],
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    config = {
        "var0": "数据采集结果",
        "var1": "Excel路径",
        "var2": "监控空列数据",
        "columns": ["商品名称", "价格", "销量原文"],
        "data_attributes": {
            "商品名称": "innerText",
            "价格": "innerText",
            "销量原文": "innerText",
        },
        "designated_info": 1,
        "designated_obj": "网页对象",
        "region": 1,
        "monitor_data": 0,
        "toExcel": 0,
    }
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            before = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": args.flow_ref, "detail": "edit", "step_id": args.step_id},
            )
            revision = str(before.get("revision") or "")
            if not revision:
                raise RuntimeError("flow revision is missing")
            preview = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": args.flow_ref,
                    "step_id": args.step_id,
                    "config": config,
                    "expected_revision": revision,
                    "dry_run": True,
                },
            )
            updated = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": args.flow_ref,
                    "step_id": args.step_id,
                    "config": config,
                    "expected_revision": revision,
                    "allow_write": True,
                },
            )
            imported = await _call(
                session,
                "shizai_project_import_elements",
                {
                    "project_ref": args.flow_ref,
                    "source_project_ref": args.source_project_ref,
                    "element_ids": args.element_ids,
                    "allow_write": True,
                },
            )
            rebound = await _call(
                session,
                "shizai_rpa_set_step_elements",
                {
                    "flow_ref": args.flow_ref,
                    "step_id": args.step_id,
                    "element_ids": args.element_ids,
                    "allow_write": True,
                },
            )
            after = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": args.flow_ref, "detail": "edit", "step_id": args.step_id},
            )
            validated = await _call(
                session,
                "shizai_rpa_validate_step",
                {"flow_ref": args.flow_ref, "step_id": args.step_id},
            )
            return {
                "kind": "mcp_collection_reference_binding_probe",
                "server": {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                },
                "source_project_ref": args.source_project_ref,
                "element_ids": args.element_ids,
                "before": before,
                "preview": preview,
                "updated": updated,
                "imported": imported,
                "rebound": rebound,
                "after": after,
                "validated": validated,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("source_project_ref")
    parser.add_argument("--step-id", default="collect-jd-phones")
    parser.add_argument("--element-id", dest="element_ids", action="append")
    args = parser.parse_args()
    args.element_ids = args.element_ids or list(DEFAULT_ELEMENT_IDS)
    print(json.dumps(asyncio.run(bind(args)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
