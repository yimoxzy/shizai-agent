"""Comparable MCP versus native-designer stress matrix.

The matrix keeps prompts identical across both paths. The MCP side uses one
stdio session; the native side calls the same desktop services directly,
without MCP framing. Read-only checks are preferred so the test does not create
or mutate a user's production workflow for every prompt.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mcp_stress import StressClient, classify_failure, minimal_draft  # noqa: E402
from shizai_agent.core import agent as agent_core  # noqa: E402
from shizai_agent.core import component_catalog as component_catalog_core  # noqa: E402
from shizai_agent.core import project as project_core  # noqa: E402
from shizai_agent.core.cloud_api import ShizaiDesktopAgent  # noqa: E402
from shizai_agent.core.paths import ShizaiPaths  # noqa: E402

OUT_DIR = ROOT / "_tmp"
JSON_PATH = OUT_DIR / "mcp-vs-tars-comparison-report.json"
MD_PATH = OUT_DIR / "mcp-vs-tars-comparison-report.md"

SCENARIOS: tuple[dict[str, Any], ...] = (
    {"id": "agent_minimal", "prompt": "创建一个最小智能体，接收问题并回答。", "kind": "agent"},
    {"id": "agent_branch", "prompt": "创建一个流程，根据输入类型分支后分别回答。", "kind": "agent", "draft": "branch"},
    {"id": "agent_loop", "prompt": "创建一个流程，循环处理文件列表并汇总结果。", "kind": "agent", "draft": "loop"},
    {"id": "agent_knowledge", "prompt": "创建一个知识库问答智能体，检索资料后回答。", "kind": "agent"},
    {"id": "agent_http", "prompt": "创建一个流程，调用 HTTP 接口并整理响应。", "kind": "agent"},
    {"id": "agent_repeat", "prompt": "重复生成同一个最小智能体流程并校验一致性。", "kind": "agent"},
    {"id": "native_excel", "prompt": "从 Excel 逐行读取数据，处理后写回并保存。", "kind": "native", "queries": ("Excel", "loop")},
    {"id": "native_file", "prompt": "批量读取文件夹中的文本文件并输出汇总文件。", "kind": "native", "queries": ("file", "save")},
    {"id": "native_ocr", "prompt": "对桌面截图执行 OCR 并把识别结果保存。", "kind": "native", "queries": ("OCR", "screenshot")},
    {"id": "native_window", "prompt": "打开桌面应用窗口，读取指定字段并记录。", "kind": "native", "queries": ("window", "read")},
    {"id": "native_loop", "prompt": "循环处理一组任务，失败项记录后继续执行。", "kind": "native", "queries": ("loop", "condition")},
    {"id": "browser_jd", "prompt": "打开京东，搜索手机，读取前10条商品标题、价格和链接。", "kind": "browser"},
    {"id": "browser_taobao", "prompt": "打开淘宝，搜索耳机，读取前10条商品标题、价格和链接。", "kind": "browser"},
    {"id": "browser_baidu", "prompt": "打开百度，搜索自动化，提取前10条标题和链接。", "kind": "browser"},
    {"id": "negative_invalid", "prompt": "校验一个缺少目标节点的流程并给出错误原因。", "kind": "negative"},
)


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage_for(tool: str) -> str:
    if "route" in tool:
        return "intent"
    if any(x in tool for x in ("context", "components", "catalog", "apps")):
        return "decompose"
    if any(x in tool for x in ("draft", "start_design", "add_step")):
        return "compose"
    if any(x in tool for x in ("validate", "inspect", "project_list", "read_flow")):
        return "verify"
    if any(x in tool for x in ("run", "status")):
        return "execute"
    return "other"


def summarize_calls(record: dict[str, Any]) -> None:
    calls = record.get("calls", [])
    record["tool_sequence"] = [call.get("tool") for call in calls]
    record["stage_timings"] = {
        stage: sum(float(call.get("elapsed_sec", 0)) for call in calls if stage_for(str(call.get("tool"))) == stage)
        for stage in ("intent", "decompose", "compose", "verify", "execute")
    }
    record["ui_feedback_events"] = [
        {
            "tool": call.get("tool"),
            "phase": (call.get("ui_feedback") or {}).get("phase"),
            "animation": (call.get("ui_feedback") or {}).get("animation"),
            "message": (call.get("ui_feedback") or {}).get("message"),
        }
        for call in calls
        if call.get("ui_feedback")
    ]
    record["failure_events"] = [
        {"stage": stage_for(str(call.get("tool"))), "tool": call.get("tool"), "kind": call.get("failure_kind"), "error": call.get("error")}
        for call in calls
        if not call.get("ok")
    ]
    record["verifiable_output"] = any(
        isinstance(call.get("response"), dict)
        and any(key in call["response"] for key in ("output_evidence", "output_file", "saved", "draft_sha256", "flow_ref", "components"))
        for call in calls
    )


def scenario_actions(item: dict[str, Any], agent_id: str | None, project_ref: str | None) -> list[tuple[str, dict[str, Any]]]:
    prompt = item["prompt"]
    kind = item["kind"]
    actions: list[tuple[str, dict[str, Any]]] = [("shizai_route_intent", {"query": prompt})]
    if kind == "agent":
        draft = minimal_draft(with_branch=item.get("draft") == "branch", with_loop=item.get("draft") == "loop")
        selected = {"agent_id": agent_id} if agent_id else {}
        actions += [
            ("shizai_generation_context", {"query": prompt, **selected}),
            ("shizai_components", selected),
            ("shizai_draft", selected),
            ("shizai_validate_draft", {"draft": draft, **selected}),
        ]
    elif kind == "native":
        for query in item.get("queries", ()):
            actions.append(("shizai_rpa_component_catalog", {"scope": "builtin", "query": query, "detail": "summary", "limit": 10}))
    elif kind == "browser":
        actions.append(("shizai_rpa_component_catalog", {"scope": "builtin", "query": "WebBrowser", "detail": "contract", "limit": 10}))
        actions.append(("shizai_project_list", {}))
        if project_ref:
            actions += [
                ("shizai_project_inspect", {"project_ref": project_ref}),
                ("shizai_rpa_validate_project", {"project_ref": project_ref, "strict": False}),
            ]
    else:
        bad_draft = {"graph": {"nodes": [{"id": "start", "data": {"type": "start"}}], "edges": [{"source": "start", "target": "missing"}]}}
        actions.append(("shizai_validate_draft", {"draft": bad_draft, "strict": True}))
    return actions


async def run_mcp_case(client: StressClient, item: dict[str, Any], agent_id: str | None, project_ref: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {"scenario": item["id"], "prompt": item["prompt"], "path": "codex_mcp", "kind": item["kind"], "started_at": iso(), "calls": [], "retries": 0}
    started = time.perf_counter()
    for tool, arguments in scenario_actions(item, agent_id, project_ref):
        await client.call(tool, arguments, record)
    record["total_elapsed_sec"] = time.perf_counter() - started
    record["finished_at"] = iso()
    expected_failure = item["kind"] == "negative"
    record["expected_failure"] = expected_failure
    record["success"] = (any(not call["ok"] for call in record["calls"]) if expected_failure else all(call["ok"] for call in record["calls"]))
    record["result"] = "expected_rejection" if expected_failure and record["success"] else ("success" if record["success"] else "failure")
    summarize_calls(record)
    return record


def native_call(record: dict[str, Any], stage: str, label: str, operation: Callable[[], Any]) -> Any:
    started = time.perf_counter()
    try:
        response = operation()
        ok = True
        error = None
    except Exception as exc:  # Native desktop availability is a measured outcome.
        response = None
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    record.setdefault("calls", []).append({"tool": label, "stage": stage, "elapsed_sec": time.perf_counter() - started, "ok": ok, "error": error, "response": response})
    if not ok:
        record["failure_events"] = record.get("failure_events", []) + [{"stage": stage, "tool": label, "kind": classify_failure(error, label), "error": error}]
    return response


def run_native_case(item: dict[str, Any], paths: ShizaiPaths, desktop: ShizaiDesktopAgent, project_ref: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {"scenario": item["id"], "prompt": item["prompt"], "path": "shizai_tars_native", "kind": item["kind"], "started_at": iso(), "calls": [], "retries": 0}
    started = time.perf_counter()
    native_call(record, "intent", "native.route_intent", lambda: project_core.route_request(item["prompt"]))
    aid: str | None = None
    if item["kind"] == "agent":
        apps = native_call(record, "decompose", "native.AgentBrainAppService.getAppList", lambda: agent_core.query_apps(client=desktop))
        if isinstance(apps, dict) and isinstance(apps.get("data"), list) and apps["data"] and isinstance(apps["data"][0], dict):
            aid = apps["data"][0].get("id")
        native_call(record, "decompose", "native.AgentBrainAppService.getNodeDefaults", lambda: agent_core.query_components(agent_id=aid, client=desktop))
        native_call(record, "compose", "native.AgentBrainAppService.getWorkflowDraft", lambda: agent_core.query_draft(agent_id=aid, client=desktop))
        native_call(record, "verify", "native.graph.validate", lambda: agent_core.validate_draft(minimal_draft(with_branch=item.get("draft") == "branch", with_loop=item.get("draft") == "loop"), agent_id=aid, client=desktop))
    elif item["kind"] == "native":
        for query in item.get("queries", ()):
            native_call(record, "decompose", f"native.ComponentCatalog[{query}]", lambda query=query: component_catalog_core.component_catalog(paths, scope="builtin", query=query, detail="summary", limit=10))
    elif item["kind"] == "browser":
        native_call(record, "decompose", "native.ComponentCatalog[WebBrowser]", lambda: component_catalog_core.component_catalog(paths, scope="builtin", query="WebBrowser", detail="contract", limit=10))
        native_call(record, "verify", "native.ApplicationService.listProjects", lambda: project_core.list_projects(paths))
        if project_ref:
            native_call(record, "verify", "native.ApplicationService.inspectProject", lambda: project_core.inspect_project(paths, project_ref))
            native_call(record, "verify", "native.ApplicationService.validateProject", lambda: project_core.validate_rpa_project(paths, project_ref, strict=False))
    else:
        native_call(record, "verify", "native.graph.validate-invalid", lambda: project_core.validate_rpa_project(paths, "C:\\TARGET\\missing-project", strict=True))
    record["total_elapsed_sec"] = time.perf_counter() - started
    record["finished_at"] = iso()
    expected_failure = item["kind"] == "negative"
    record["expected_failure"] = expected_failure
    record["success"] = (any(not call["ok"] for call in record["calls"]) if expected_failure else all(call["ok"] for call in record["calls"]))
    record["result"] = "expected_rejection" if expected_failure and record["success"] else ("success" if record["success"] else "failure")
    record["tool_sequence"] = [call["tool"] for call in record["calls"]]
    record["stage_timings"] = {stage: sum(call["elapsed_sec"] for call in record["calls"] if call["stage"] == stage) for stage in ("intent", "decompose", "compose", "verify", "execute")}
    record["verifiable_output"] = any(call.get("response") is not None for call in record["calls"])
    record["environment_blocked"] = any(event.get("kind") == "infrastructure" for event in record.get("failure_events", []))
    return record


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))]


def select_valid_project(paths: ShizaiPaths) -> str | None:
    """Choose a real statically valid project instead of relying on list order."""
    for candidate in project_core.list_projects(paths):
        if not isinstance(candidate, dict) or not candidate.get("path"):
            continue
        project_ref = str(candidate["path"])
        try:
            result = project_core.validate_rpa_project(paths, project_ref, strict=False)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if isinstance(result, dict) and result.get("valid") is True:
            return project_ref
    return None


def path_summary(records: list[dict[str, Any]], path: str) -> dict[str, Any]:
    group = [record for record in records if record["path"] == path]
    applicable = [record for record in group if not record.get("expected_failure") and not record.get("environment_blocked")]
    negatives = [record for record in group if record.get("expected_failure")]
    elapsed = [float(record["total_elapsed_sec"]) for record in group]
    return {
        "records": len(group),
        "scenarios": len({record["scenario"] for record in group}),
        "successes": sum(bool(record.get("success")) for record in group),
        "raw_success_rate": sum(bool(record.get("success")) for record in group) / max(1, len(group)),
        "applicable_success_rate": sum(bool(record.get("success")) for record in applicable) / max(1, len(applicable)),
        "expected_negative_detection_rate": sum(record.get("result") == "expected_rejection" for record in negatives) / max(1, len(negatives)),
        "environment_blocked": sum(bool(record.get("environment_blocked")) for record in group),
        "latency_sec": {"average": statistics.mean(elapsed) if elapsed else None, "p50": percentile(elapsed, 0.5), "p95": percentile(elapsed, 0.95)},
        "output_evidence_rate": sum(bool(record.get("verifiable_output")) for record in applicable) / max(1, len(applicable)),
    }


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = ShizaiPaths.from_environment()
    project_ref = select_valid_project(paths)
    desktop = ShizaiDesktopAgent()
    records: list[dict[str, Any]] = []
    infrastructure: dict[str, Any] = {"project_ref": project_ref, "mcp": {}, "native": {}}
    params = StdioServerParameters(command=sys.executable, args=["-m", "shizai_agent.core.mcp_server"], cwd=ROOT)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_started = time.perf_counter()
            await session.initialize()
            infrastructure["mcp"]["initialize_sec"] = time.perf_counter() - init_started
            listed = await session.list_tools()
            infrastructure["mcp"]["tools"] = len(listed.tools)
            probe_record: dict[str, Any] = {"calls": []}
            apps = await StressClient(session).call("shizai_apps", {"page": 1, "limit": 20}, probe_record)
            agent_id = None
            if isinstance(apps, dict) and isinstance(apps.get("data"), list) and apps["data"] and isinstance(apps["data"][0], dict):
                agent_id = apps["data"][0].get("id")
            infrastructure["mcp"]["selected_agent_id"] = agent_id
            client = StressClient(session)
            for item in SCENARIOS:
                records.append(await run_mcp_case(client, item, agent_id, project_ref))
    for item in SCENARIOS:
        records.append(run_native_case(item, paths, desktop, project_ref))

    report = {
        "generated_at": iso(),
        "prompt_policy": "Each scenario prompt is byte-for-byte identical for Codex MCP and native TARS paths.",
        "matrix": [{"scenario": item["id"], "prompt": item["prompt"], "kind": item["kind"]} for item in SCENARIOS],
        "infrastructure": infrastructure,
        "paths": {"codex_mcp": path_summary(records, "codex_mcp"), "shizai_tars_native": path_summary(records, "shizai_tars_native")},
        "records": records,
        "limitations": [
            "browser 场景使用现有客户端项目做只读校验，避免为每个提示词创建新桌面项目；完整 Chrome 运行仍受当前 RuntimeHost/DeepCapture 环境状态约束。",
            "TARS 原生路径绕过 MCP stdio，直接调用设计器本地 RPC/文件服务，因此可用于衡量协议与编排开销；桌面本身未就绪的记录单独标为 environment_blocked。",
        ],
    }
    JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# MCP 与 TARS 原生对照压测报告",
        "",
        f"生成时间：`{report['generated_at']}`。场景数：`{len(SCENARIOS)}`；两条路径使用完全相同提示词。",
        "",
        "## 汇总",
        "",
        "| 路径 | 原始成功率 | 适用成功率 | 负例识别率 | 输出证据率 | 平均/P50/P95(s) | 环境阻断 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for path, item in report["paths"].items():
        latency = item["latency_sec"]
        lines.append(f"| {path} | {item['raw_success_rate']:.1%} | {item['applicable_success_rate']:.1%} | {item['expected_negative_detection_rate']:.1%} | {item['output_evidence_rate']:.1%} | {latency['average']:.3f} / {latency['p50']:.3f} / {latency['p95']:.3f} | {item['environment_blocked']} |")
    lines += ["", "## 场景明细", "", "| 场景 | 提示词 | MCP结果/耗时(s) | TARS结果/耗时(s) |", "|---|---|---|---|"]
    for item in SCENARIOS:
        pair = [record for record in records if record["scenario"] == item["id"]]
        mcp = next(record for record in pair if record["path"] == "codex_mcp")
        native = next(record for record in pair if record["path"] == "shizai_tars_native")
        lines.append(f"| {item['id']} | {item['prompt']} | {mcp['result']} / {mcp['total_elapsed_sec']:.3f} | {native['result']} / {native['total_elapsed_sec']:.3f} |")
    lines += ["", "## 失败分层", ""]
    for record in records:
        for event in record.get("failure_events", []):
            lines.append(f"- `{record['path']}/{record['scenario']}`：`{event.get('kind')}`，阶段 `{event.get('stage')}`，{event.get('error')}")
    lines += ["", "## 结论", "", "- 适用成功率排除了预期负例和桌面环境阻断；原始成功率保留所有记录，避免掩盖环境问题。", "- MCP 路径额外包含 stdio 初始化、schema 和工具调度开销；TARS 路径直接进入设计器本地 RPC。", "- 浏览器真实运行应以新鲜输出证据和终态 status 为准，静态流程校验通过不等于业务成功。", "", f"完整逐次记录：`{JSON_PATH}`。"]
    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(JSON_PATH), "markdown": str(MD_PATH), "scenarios": len(SCENARIOS), "paths": report["paths"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
