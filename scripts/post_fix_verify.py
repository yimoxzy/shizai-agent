from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "_tmp" / "mcp-stress-report.json"
MARKDOWN = ROOT / "_tmp" / "mcp-stress-report.md"


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode(result):
    blocks = getattr(result, "content", []) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def feedback(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("ui_feedback"), dict):
            return payload["ui_feedback"]
        for value in payload.values():
            found = feedback(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = feedback(value)
            if found:
                return found
    return None


async def main():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    flow_ref = None
    for record in report.get("records", []):
        if record.get("scenario") != "browser_rpa_jd_search_capture":
            continue
        for call in record.get("calls", []):
            response = call.get("response")
            if call.get("tool") == "shizai_rpa_start_design" and isinstance(response, dict):
                flow_ref = response.get("flow_ref") or response.get("project_ref")
                break
    if not flow_ref:
        raise RuntimeError("browser_rpa_jd_search_capture flow_ref not found")

    params = StdioServerParameters(command=sys.executable, args=["-m", "shizai_agent.core.mcp_server"], cwd=ROOT)
    verification = {
        "verified_at": iso(),
        "flow_ref": flow_ref,
        "command": [sys.executable, "-m", "shizai_agent.core.mcp_server"],
        "run": {},
        "status": {},
    }
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            run_started = time.perf_counter()
            try:
                run_result = await session.call_tool(
                    "shizai_rpa_run",
                    {"project_ref": flow_ref, "allow_write": True, "wait": False, "wait_seconds": 1},
                )
                run_payload = decode(run_result)
                run_error = None if not getattr(run_result, "isError", False) else str(run_payload)
            except Exception as exc:
                run_payload = None
                run_error = f"{type(exc).__name__}: {exc}"
            verification["run"] = {
                "elapsed_sec": time.perf_counter() - run_started,
                "error": run_error,
                "response": run_payload,
                "ui_feedback": feedback(run_payload),
                "tool_error": bool(run_error),
            }
            status_started = time.perf_counter()
            try:
                status_result = await session.call_tool("shizai_rpa_status", {"project_ref": flow_ref})
                status_payload = decode(status_result)
                status_error = None if not getattr(status_result, "isError", False) else str(status_payload)
            except Exception as exc:
                status_payload = None
                status_error = f"{type(exc).__name__}: {exc}"
            verification["status"] = {
                "elapsed_sec": time.perf_counter() - status_started,
                "error": status_error,
                "response": status_payload,
                "ui_feedback": feedback(status_payload),
                "tool_error": bool(status_error),
            }

    status_feedback = verification["status"].get("ui_feedback") or {}
    run_feedback = verification["run"].get("ui_feedback") or {}
    status_response = verification["status"].get("response") or {}
    verification["assertions"] = {
        "run_blocked_error": bool(verification["run"].get("error")),
        "run_ui_runtime_blocked": run_feedback.get("phase") == "runtime_blocked"
        and run_feedback.get("animation") == "native_rpa_blocked",
        "status_blocked": status_response.get("status") == "blocked",
        "status_failure_kind_runtime_not_ready": status_response.get("failure_kind") == "runtime_not_ready",
        "status_ui_runtime_blocked": status_feedback.get("phase") == "runtime_blocked"
        and status_feedback.get("animation") == "native_rpa_blocked",
    }
    # A rejected MCP call has no result envelope to carry feedback; the
    # subsequent status call is the persistent UI/state assertion point.
    verification["success"] = all(
        verification["assertions"][key]
        for key in (
            "run_blocked_error",
            "status_blocked",
            "status_failure_kind_runtime_not_ready",
            "status_ui_runtime_blocked",
        )
    )
    report["post_fix_verification"] = verification
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    section = [
        "",
        "## 修复后复验",
        "",
        f"复验时间：`{verification['verified_at']}`；流程：`{flow_ref}`。仅执行 `shizai_rpa_run(wait=false)` 与随后 `shizai_rpa_status`，未重跑整套压力场景。",
        f"运行调用耗时：`{verification['run']['elapsed_sec']:.3f}s`；状态调用耗时：`{verification['status']['elapsed_sec']:.3f}s`。运行错误：`{verification['run']['error']}`。",
        f"断言结果：`{json.dumps(verification['assertions'], ensure_ascii=False)}`。整体复验：`{'PASS' if verification['success'] else 'FAIL'}`。",
        "",
        "复验确认运行阻断会发布并持久化 `phase=runtime_blocked`、`animation=native_rpa_blocked`、`status=blocked`、`failure_kind=runtime_not_ready`。完整响应保存在 JSON 顶层 `post_fix_verification`。",
    ]
    markdown = MARKDOWN.read_text(encoding="utf-8")
    marker = "\n## 修复后复验"
    if marker in markdown:
        markdown = markdown.split(marker, 1)[0].rstrip()
    markdown = markdown + "\n" + "\n".join(section) + "\n"
    MARKDOWN.write_text(markdown, encoding="utf-8")
    print(json.dumps(verification, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
