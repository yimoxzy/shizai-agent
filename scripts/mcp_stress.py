from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "_tmp"
RAW_PATH = OUT_DIR / "mcp-stress-report.json"
MD_PATH = OUT_DIR / "mcp-stress-report.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(result: Any) -> Any:
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def find_feedback(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        feedback = value.get("ui_feedback")
        if isinstance(feedback, dict):
            return feedback
        for child in value.values():
            found = find_feedback(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_feedback(child)
            if found:
                return found
    return None


def classify_failure(error: str | None, phase: str | None) -> str | None:
    if not error:
        return None
    text = f"{phase or ''} {error}".lower()
    if any(
        x in text for x in ("winerror", "stdio", "initialize", "connection", "transport", "code=3001", "服务器返回错误")
    ):
        return "infrastructure"
    if "valid=false" in text or (phase and "validate" in phase):
        return "input_validation"
    if phase and any(x in phase for x in ("_run", "_status", "run_draft")):
        return "business_runtime"
    if any(x in text for x in ("requires", "missing", "invalid", "unsupported", "must be", "not found")):
        return "input_validation"
    return "business_runtime"


def business_error(payload: Any) -> str | None:
    """Return a business-truth error without confusing nested status objects."""
    if not isinstance(payload, dict):
        return None
    if payload.get("valid") is False:
        return "business response valid=false"
    if payload.get("ok") is False:
        return str(payload.get("error") or "business response ok=false")
    status = payload.get("status")
    if isinstance(status, str) and status in {"failed", "runtime_error", "blocked"}:
        return f"business response status={status}"
    if payload.get("failure_kind") and status != "never_started":
        return f"business response failure_kind={payload.get('failure_kind')}"
    return None


class StressClient:
    def __init__(self, session: ClientSession):
        self.session = session

    async def call(self, name: str, arguments: dict[str, Any], record: dict[str, Any]) -> Any:
        started = time.perf_counter()
        error = None
        payload: Any = None
        try:
            result = await self.session.call_tool(name, arguments)
            payload = json_text(result)
            ok = not bool(getattr(result, "isError", False))
            if ok and isinstance(payload, dict):
                # MCP transport success is distinct from business truth. A
                # validator/status response can be delivered normally while
                # reporting an invalid or failed workflow.
                error = business_error(payload)
                if error:
                    ok = False
            if not ok and error is None:
                error = str(payload) if payload is not None else "MCP tool returned isError"
        except Exception as exc:  # MCP propagates tool errors as McpError
            ok = False
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        feedback = find_feedback(payload)
        record.setdefault("calls", []).append(
            {
                "tool": name,
                "arguments": arguments,
                "elapsed_sec": elapsed,
                "ok": ok,
                "error": error,
                "failure_kind": classify_failure(error, name) if error else None,
                "response": payload,
                "ui_feedback": feedback,
            }
        )
        if not ok:
            record["retries"] = record.get("retries", 0)
            record["error"] = error or "MCP tool returned isError"
            record["failure_stage"] = record.get("failure_stage") or name
            record["failure_kind"] = classify_failure(record["error"], name)
        return payload


def minimal_draft(with_branch: bool = False, with_loop: bool = False) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {"id": "start", "type": "custom", "position": {"x": 0, "y": 0}, "data": {"type": "start"}},
        {
            "id": "llm_1",
            "type": "custom",
            "position": {"x": 260, "y": 0},
            "data": {"type": "llm", "prompt_template": [{"role": "user", "text": "{{#sys.query#}}"}]},
        },
    ]
    edges = [{"id": "start-llm", "source": "start", "target": "llm_1"}]
    if with_branch:
        nodes.append({"id": "if_2", "type": "custom", "position": {"x": 520, "y": 0}, "data": {"type": "if-else"}})
        edges.append({"id": "llm-if", "source": "llm_1", "target": "if_2", "sourceHandle": "source"})
        tail = "if_2"
    else:
        tail = "llm_1"
    if with_loop:
        nodes.append(
            {
                "id": "iteration_3",
                "type": "custom",
                "position": {"x": 780, "y": 0},
                "data": {"type": "iteration", "iterator_input": ["sys", "files"]},
            }
        )
        edges.append({"id": f"{tail}-iteration", "source": tail, "target": "iteration_3", "sourceHandle": "true"})
        tail = "iteration_3"
    nodes.append(
        {
            "id": "answer_4",
            "type": "custom",
            "position": {"x": 1040, "y": 0},
            "data": {"type": "answer", "answer": ["llm_1", "result"]},
        }
    )
    edges.append({"id": f"{tail}-answer", "source": tail, "target": "answer_4"})
    return {"graph": {"nodes": nodes, "edges": edges}}


async def run_case(
    client: StressClient, scenario: str, iteration: int, actions: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scenario": scenario,
        "iteration": iteration,
        "started_at": now_iso(),
        "calls": [],
        "retries": 0,
    }
    started = time.perf_counter()
    for name, args in actions:
        await client.call(name, args, record)
    record["finished_at"] = now_iso()
    record["total_elapsed_sec"] = time.perf_counter() - started
    record["tool_call_count"] = len(record["calls"])
    record["static_validation_elapsed_sec"] = sum(
        c["elapsed_sec"]
        for c in record["calls"]
        if "validate" in c["tool"] or c["tool"] in {"shizai_components", "shizai_generation_context"}
    )
    record["materialize_elapsed_sec"] = sum(c["elapsed_sec"] for c in record["calls"] if "materialize" in c["tool"])
    record["real_run_elapsed_sec"] = sum(
        c["elapsed_sec"]
        for c in record["calls"]
        if c["tool"] in {"shizai_rpa_run", "shizai_rpa_status", "shizai_run_draft"}
    )
    record["success"] = not any(not c["ok"] for c in record["calls"])
    record["failure_events"] = [
        {"stage": c["tool"], "kind": c.get("failure_kind"), "error": c.get("error")}
        for c in record["calls"]
        if not c["ok"]
    ]
    record["ui_feedback_events"] = [
        {
            "tool": c["tool"],
            "phase": (c.get("ui_feedback") or {}).get("phase"),
            "animation": (c.get("ui_feedback") or {}).get("animation"),
            "message": (c.get("ui_feedback") or {}).get("message"),
        }
        for c in record["calls"]
        if c.get("ui_feedback")
    ]
    record["verifiable_output"] = any(
        isinstance(c.get("response"), dict)
        and any(k in c["response"] for k in ("output_evidence", "output_file", "saved", "draft_sha256", "flow_ref"))
        for c in record["calls"]
    )
    return record


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    infra: dict[str, Any] = {
        "command": [sys.executable, "-m", "shizai_agent.core.mcp_server"],
        "initialize": None,
        "tools": None,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "shizai_agent.core.mcp_server"], cwd=ROOT)
    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init_started = time.perf_counter()
                init = await session.initialize()
                infra["initialize"] = {
                    "ok": True,
                    "elapsed_sec": time.perf_counter() - init_started,
                    "server": str(init.serverInfo),
                }
                listed = await session.list_tools()
                infra["tools"] = {"ok": True, "count": len(listed.tools), "names": [tool.name for tool in listed.tools]}
                client = StressClient(session)
                # A fresh MCP process often has no selected Agent. Continue
                # the app-selection step before reading or validating a draft.
                app_probe: dict[str, Any] = {"calls": []}
                app_payload = await client.call("shizai_apps", {"page": 1, "limit": 20}, app_probe)
                agent_id = None
                if isinstance(app_payload, dict) and isinstance(app_payload.get("data"), list):
                    first_app = app_payload["data"][0] if app_payload["data"] else None
                    if isinstance(first_app, dict):
                        agent_id = first_app.get("id")
                infra["selected_agent_id"] = agent_id
                auth_record = await run_case(client, "infrastructure_readiness", 1, [("shizai_auth_status", {})])
                records.append(auth_record)

                for i in range(1, 4):
                    records.append(
                        await run_case(
                            client,
                            "agent_minimal",
                            i,
                            [
                                ("shizai_route_intent", {"query": "最小 Agent 工作流：接收问题并回答"}),
                                ("shizai_generation_context", {"query": "最小 Agent 工作流：接收问题并回答"}),
                                ("shizai_components", {"agent_id": agent_id} if agent_id else {}),
                                ("shizai_draft", {"agent_id": agent_id} if agent_id else {}),
                                (
                                    "shizai_validate_draft",
                                    {"draft": minimal_draft(), **({"agent_id": agent_id} if agent_id else {})},
                                ),
                            ],
                        )
                    )
                    records.append(
                        await run_case(
                            client,
                            "agent_branch_loop",
                            i,
                            [
                                ("shizai_route_intent", {"query": "带变量、分支和循环处理文件后输出"}),
                                ("shizai_generation_context", {"query": "带变量、分支和循环处理文件后输出"}),
                                ("shizai_components", {"agent_id": agent_id} if agent_id else {}),
                                (
                                    "shizai_validate_draft",
                                    {
                                        "draft": minimal_draft(with_branch=True, with_loop=True),
                                        **({"agent_id": agent_id} if agent_id else {}),
                                    },
                                ),
                            ],
                        )
                    )
                    records.append(
                        await run_case(
                            client,
                            "native_file_excel",
                            i,
                            [
                                ("shizai_route_intent", {"query": "从 Excel 逐行读取并保存未完成编号"}),
                                (
                                    "shizai_rpa_component_catalog",
                                    {"scope": "builtin", "query": "Excel", "detail": "contract", "limit": 20},
                                ),
                                (
                                    "shizai_rpa_component_catalog",
                                    {"scope": "builtin", "query": "loop", "detail": "summary", "limit": 20},
                                ),
                            ],
                        )
                    )
                    records.append(
                        await run_case(
                            client,
                            "repeated_same_request",
                            i,
                            [
                                ("shizai_route_intent", {"query": "重复生成同一个最小 Agent 工作流"}),
                                ("shizai_generation_context", {"query": "重复生成同一个最小 Agent 工作流"}),
                                (
                                    "shizai_validate_draft",
                                    {"draft": minimal_draft(), **({"agent_id": agent_id} if agent_id else {})},
                                ),
                            ],
                        )
                    )

                bad_draft = {
                    "graph": {
                        "nodes": [{"id": "start", "data": {"type": "start"}}],
                        "edges": [{"source": "start", "target": "missing"}],
                    }
                }
                for i in range(1, 4):
                    records.append(
                        await run_case(
                            client,
                            "failure_missing_component_or_parameter",
                            i,
                            [
                                ("shizai_validate_draft", {"draft": bad_draft, "strict": True}),
                                (
                                    "shizai_rpa_add_step",
                                    {
                                        "flow_ref": "C:\\TARGET\\missing-project",
                                        "component": "Missing_Component",
                                        "allow_write": True,
                                    },
                                ),
                            ],
                        )
                    )

                # One real browser design/capture attempt; runtime is measured when the service reaches it.
                browser_actions = [
                    ("shizai_route_intent", {"query": "打开京东搜索手机并采集商品到 Excel"}),
                    (
                        "shizai_rpa_start_design",
                        {
                            "name": "MCP压力测试京东",
                            "target_url": "https://www.jd.com/",
                            "keyword": "手机",
                            "output_file": "mcp-stress-jd.xls",
                            "open_browser": True,
                            "wait_seconds": 1,
                            "allow_write": True,
                        },
                    ),
                ]
                browser = await run_case(client, "browser_rpa_jd_search_capture", 1, browser_actions)
                flow_ref = None
                for call in browser["calls"]:
                    if isinstance(call.get("response"), dict) and call["response"].get("flow_ref"):
                        flow_ref = call["response"]["flow_ref"]
                if flow_ref:
                    for name, args in [
                        ("shizai_rpa_capture_windows", {}),
                        ("shizai_rpa_scan_page", {"query": "搜索框", "level": 6, "max_nodes": 250}),
                        ("shizai_rpa_validate_project", {"project_ref": flow_ref}),
                        ("shizai_rpa_materialize", {"project_ref": flow_ref, "strict": True, "allow_write": True}),
                        ("shizai_rpa_sync_diagnose", {"project_ref": flow_ref}),
                        (
                            "shizai_rpa_run",
                            {"project_ref": flow_ref, "wait": True, "wait_seconds": 30, "allow_write": True},
                        ),
                        ("shizai_rpa_status", {"project_ref": flow_ref}),
                    ]:
                        await client.call(name, args, browser)
                    browser["tool_call_count"] = len(browser["calls"])
                    browser["total_elapsed_sec"] = sum(c["elapsed_sec"] for c in browser["calls"])
                    browser["real_run_elapsed_sec"] = sum(
                        c["elapsed_sec"]
                        for c in browser["calls"]
                        if c["tool"] in {"shizai_rpa_run", "shizai_rpa_status"}
                    )
                    browser["success"] = not any(not c["ok"] for c in browser["calls"])
                    browser["failure_events"] = [
                        {"stage": c["tool"], "kind": c.get("failure_kind"), "error": c.get("error")}
                        for c in browser["calls"]
                        if not c["ok"]
                    ]
                    browser["static_validation_elapsed_sec"] = sum(
                        c["elapsed_sec"] for c in browser["calls"] if "validate" in c["tool"]
                    )
                    browser["materialize_elapsed_sec"] = sum(
                        c["elapsed_sec"] for c in browser["calls"] if "materialize" in c["tool"]
                    )
                    browser["ui_feedback_events"] = [
                        {
                            "tool": c["tool"],
                            "phase": (c.get("ui_feedback") or {}).get("phase"),
                            "animation": (c.get("ui_feedback") or {}).get("animation"),
                            "message": (c.get("ui_feedback") or {}).get("message"),
                        }
                        for c in browser["calls"]
                        if c.get("ui_feedback")
                    ]
                    browser["verifiable_output"] = any(
                        isinstance(c.get("response"), dict)
                        and any(k in c["response"] for k in ("output_evidence", "output_file", "output", "flow_ref"))
                        for c in browser["calls"]
                    )
                records.append(browser)
    except Exception as exc:
        infra["initialize"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    totals = [r["total_elapsed_sec"] for r in records if "total_elapsed_sec" in r]

    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
        return ordered[index]

    scenario_summary: dict[str, Any] = {}
    for scenario in sorted({r["scenario"] for r in records}):
        group = [r for r in records if r["scenario"] == scenario]
        scenario_summary[scenario] = {
            "runs": len(group),
            "successes": sum(bool(r.get("success")) for r in group),
            "success_rate": sum(bool(r.get("success")) for r in group) / len(group) if group else 0,
            "avg_total_sec": statistics.mean([r["total_elapsed_sec"] for r in group]) if group else None,
            "failures": [
                {"stage": r.get("failure_stage"), "kind": r.get("failure_kind"), "error": r.get("error")}
                for r in group
                if not r.get("success")
            ],
        }
    materialize_calls = [c for r in records for c in r["calls"] if c["tool"] == "shizai_rpa_materialize"]
    materialize_successes = [
        c
        for c in materialize_calls
        if c["ok"] and isinstance(c.get("response"), dict) and c["response"].get("ready_for_runtime_diagnosis") is True
    ]
    validation_records = [r for r in records if any("validate" in c["tool"] for c in r["calls"])]
    validation_successes = [
        r for r in validation_records if all(c["ok"] for c in r["calls"] if "validate" in c["tool"])
    ]
    negative_records = [r for r in records if r["scenario"] == "failure_missing_component_or_parameter"]
    not_applicable_records = [
        r
        for r in records
        if any("agent_id is required when no Agent is configured" in str(c.get("error")) for c in r["calls"])
    ]
    operational_records = [
        r for r in records if r not in negative_records and r not in not_applicable_records
    ]
    generation_records = [
        r
        for r in records
        if r["scenario"] in {"agent_minimal", "agent_branch_loop"} and r not in not_applicable_records
    ]
    summary = {
        "generated_at": now_iso(),
        "infrastructure": infra,
        "counts": {"records": len(records), "scenarios": len(scenario_summary)},
        "rates": {
            "flow_generation_success_rate": sum(
                r.get("success", False) for r in records if r["scenario"] in {"agent_minimal", "agent_branch_loop"}
            )
            / max(1, sum(r["scenario"] in {"agent_minimal", "agent_branch_loop"} for r in records)),
            "applicable_flow_generation_success_rate": sum(r.get("success", False) for r in generation_records)
            / max(1, len(generation_records)),
            "static_validation_success_rate": len(validation_successes) / max(1, len(validation_records)),
            "materialization_success_rate": len(materialize_successes) / max(1, len(materialize_calls)),
            "real_run_success_rate": sum(
                r.get("success", False) for r in records if r["scenario"] in {"browser_rpa_jd_search_capture"}
            )
            / max(1, sum(r["scenario"] in {"browser_rpa_jd_search_capture"} for r in records)),
            "end_to_end_success_rate": sum(r.get("success", False) for r in records) / max(1, len(records)),
            "operational_end_to_end_success_rate": sum(r.get("success", False) for r in operational_records)
            / max(1, len(operational_records)),
            "expected_negative_detection_rate": sum(not r.get("success", False) for r in negative_records)
            / max(1, len(negative_records)),
        },
        "latency_sec": {
            "average": statistics.mean(totals) if totals else None,
            "p50": percentile(totals, 0.50),
            "p95": percentile(totals, 0.95),
        },
        "scenario_summary": scenario_summary,
        "records": records,
    }
    RAW_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# Shizai MCP 黑盒压力报告",
        "",
        f"生成时间：`{summary['generated_at']}`。本次通过项目 MCP stdio（`{sys.executable} -m shizai_agent.core.mcp_server`）完成真实工具调用。",
        "",
        "## 基础设施",
        "",
        f"initialize：`{infra.get('initialize')}`；tools/list：`{infra.get('tools', {}).get('count') if isinstance(infra.get('tools'), dict) else None}` 个工具。stdio 启动失败与业务失败分开统计。",
        "",
        "## 汇总指标",
        "",
        f"流程生成成功率：`{summary['rates']['flow_generation_success_rate']:.1%}`（适用环境：`{summary['rates']['applicable_flow_generation_success_rate']:.1%}`）；静态校验成功率：`{summary['rates']['static_validation_success_rate']:.1%}`；物化成功率：`{summary['rates']['materialization_success_rate']:.1%}`；真实运行成功率：`{summary['rates']['real_run_success_rate']:.1%}`；原始端到端成功率：`{summary['rates']['end_to_end_success_rate']:.1%}`；排除预期负例/未配置 Agent 后：`{summary['rates']['operational_end_to_end_success_rate']:.1%}`。",
        f"总耗时平均/P50/P95：`{summary['latency_sec']['average']}` / `{summary['latency_sec']['p50']}` / `{summary['latency_sec']['p95']}` 秒。",
        "",
        "| 场景 | 次数 | 成功 | 成功率 | 平均总耗时(s) |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in scenario_summary.items():
        lines.append(
            f"| {name} | {item['runs']} | {item['successes']} | {item['success_rate']:.1%} | {item['avg_total_sec']:.3f} |"
        )
    lines += [
        "",
        "## 失败分层",
        "",
        "原始每次调用、错误阶段、重试次数、ui_feedback.phase/animation/message 和可验证输出标记均在 JSON 中。",
    ]
    failures = [r for r in records if not r.get("success")]
    for item in failures:
        events = item.get("failure_events") or [
            {"stage": item.get("failure_stage"), "kind": item.get("failure_kind"), "error": item.get("error")}
        ]
        for event in events:
            lines.append(
                f"- `{item['scenario']}#{item['iteration']}`：`{event.get('kind')}`，阶段 `{event.get('stage')}`，错误 `{event.get('error')}`"
            )
    lines += [
        "",
        "## 发现与建议",
        "",
        "- 原始端到端率包含故意失败的参数校验用例，以及本机未配置 Agent 的不适用用例；上线判断应优先看排除后的 operational_end_to_end_success_rate。",
        "- expected_negative_detection_rate 衡量 MCP 是否正确拒绝坏输入，不应计入业务成功率。",
        "- 当前桌面会话可达且已登录，Agent 组件实时目录返回 3001 时会回退到内置目录；建议在服务端恢复实时目录后复测生成准确性。",
        "- 失败场景中把参数/组件错误与基础设施启动问题分开，便于告警和重试策略使用。",
        "- Browser JD 场景保留真实 Chrome 捕获与项目校验结果；若运行阶段缺少新鲜输出，应按业务失败处理而非把提交成功当作成功。",
        "",
        f"逐次原始记录：`{RAW_PATH}`。",
    ]
    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"json": str(RAW_PATH), "markdown": str(MD_PATH), "records": len(records), "rates": summary["rates"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
