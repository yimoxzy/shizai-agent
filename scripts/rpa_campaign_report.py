"""Render measured evidence and independent review notes; never infer task success."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rpa_benchmark import compare, digest, read, write  # noqa: E402


def link(path):
    return Path(path).resolve().as_posix()


def render(folder, destination):
    folder, destination = Path(folder).resolve(), Path(destination).resolve()
    ledger = read(folder / "campaign.json")
    rows, reports, grades, inventory = [], [], [], Counter()
    reviewed, sent, calls, run_calls, run_success = 0, 0, 0, 0, 0
    command_count = 0
    for trial in ledger["trials"]:
        target = Path(trial["folder"])
        report = read(target / "run/report.json") if (target / "run/report.json").exists() else None
        graded = read(target / "grade.json") if (target / "grade.json").exists() else None
        review = read(target / "review.json") if (target / "review.json").exists() else {}
        if report:
            reports.append(report)
            sent += len(report.get("turns", []))
            for turn in report.get("turns", []):
                for item in turn.get("items", []):
                    command_count += item.get("type") == "commandExecution"
                    if item.get("type") == "mcpToolCall":
                        calls += 1
                        if item.get("tool") == "shizai_rpa_run":
                            from scripts.luna_rpa_eval import tool_payload
                            run_calls += 1
                            run_success += tool_payload(item).get("terminal_status") == "success"
                # Count per-turn snapshot component instances, not unique executions.
                for flow in turn.get("flows", []):
                    path = Path(flow["snapshot"])
                    if digest(path) != flow["sha256"]:
                        raise ValueError(f"Changed task snapshot: {path}")
                    for node in read(path).get("componentModelEntities", {}).values():
                        if node.get("type") != "main":
                            inventory[node.get("type", "unknown")] += 1
        if graded:
            grades.append(target / "grade.json")
        reviewed += bool(review.get("notes"))
        timing = [str(t.get("elapsed_seconds", "进行中")) for t in (report or {}).get("turns", [])]
        timing += ["未发送"] * (2 - len(timing))
        n = [sum(i.get("type") == "mcpToolCall" for i in t.get("items", [])) for t in (report or {}).get("turns", [])]
        state = graded["outcome"] if graded else trial["status"]
        state = {"failed": "未通过", "passed": "已复核通过", "needs_review": "待独立复核",
                 "running": "运行中", "prepared": "尚未运行", "finished": "待评分"}.get(state, state)
        objective = []
        for t in (graded or {}).get("turns", []):
            checks = t["checks"]
            objective.append("客观检查通过" if all(v is True for v in checks.values()) else "未通过")
        rows.append({"case_id": trial["case_id"], "mode": trial["mode"], "state": state,
                     "timing": timing, "calls": n, "objective": objective,
                     "thread_id": (report or {}).get("thread_id"), "folder": str(target),
                     "review_notes": review.get("notes", "")})
    comparison = compare(grades) if grades else {"comparisons": []}
    write(folder / "comparison.json", comparison)
    pass_count = sum(read(p)["outcome"] == "passed" for p in grades)
    complete_reports = [r for r in reports if r.get("turns") and all(t["status"] != "running" for t in r["turns"])]
    successful_times = [sum(t["seconds"] for t in read(p)["turns"]) for p in grades if read(p)["outcome"] == "passed"]
    summary = {"campaign_status": ledger["status"], "planned_trials": len(rows),
               "planned_unique_cases": len({r["case_id"] for r in rows}),
               "started_threads": sum(bool(r.get("thread_id")) for r in reports),
               "finished_conversations": len(complete_reports), "graded_trials": len(grades),
               "verified_full_passes": pass_count, "reviewed_trials": reviewed,
               "sent_turns": sent, "completed_model_turns": sum(t["status"] == "completed" for r in reports for t in r["turns"]),
               "mcp_calls_completed": calls, "formal_run_calls": run_calls,
               "formal_run_terminal_successes": run_success, "host_commands_to_review": command_count,
               "snapshot_component_types": dict(sorted(inventory.items())),
               "median_success_seconds": statistics.median(successful_times) if successful_times else None,
               "fast_and_accurate_trials": sum(read(p)["fast_and_accurate"] for p in grades), "trials": rows}
    write(folder / "summary.json", summary)
    lines = ["# Luna 原生 RPA 组件场景测评报告", "",
             f"更新时间：{datetime.now().astimezone().isoformat(timespec='seconds')}。状态：**{ledger['status']}**。",
             "",
             f"计划覆盖 **32 个场景、37 个独立会话、74 条业务请求**。当前实际启动 **{summary['started_threads']}** 个会话，"
             f"已评分 **{len(grades)}** 个；完整创建及同流程编辑独立验收通过 **{pass_count}** 个。尚未运行项目不计入通过率。",
             f"实际发送 {sent} 轮，模型结束 {summary['completed_model_turns']} 轮，已完成 MCP 调用 {calls} 次；"
             f"正式运行调用 {run_calls} 次，其中工具报告终态 success 为 {run_success} 次。运行成功仍需业务产物和编辑验收。",
             "", "## 测量条件", "",
             "- 模型固定 `gpt-5.6-luna / medium`，以每个 report.json 的 model_resolved 核验，禁用回退；provider 为本机配置的 custom。",
             "- 独立持久会话，完整访问权限、无需交互批准；通过 app-server 创建。应用内已打开首个测试会话；未验证独立操作系统窗口。",
             "- 每轮限时 600 秒；桌面操作严格串行，每例清理已创建应用关联的捕获、调试、运行会话。",
             "- 普通场景使用 rpa-authoring；原生控制流和结构编辑使用 full。同一对照组工具集一致。",
             "- 固定 seed=27、trial=1，每组仅一次探索测量；不能据此估计稳定成功率或推断 Qwen 27B 表现。",
             "- 5 组 baseline/reference 对照：京东手机销量、文件替换、分页、数字排序、Excel 公式；相邻配对，交替 A/B 顺序。",
             "- 测评期间冻结 MCP 和测评集；保留 SHA256。参考只来自通用工作流资料，没有向 MCP 添加京东/手机/销量路由或预置答案。",
             "- 宿主只准备输入及测试 HTML、记录及核验产物；模型不能读取源码/答案、调用其他模型或用宿主 shell/Python/HTTP 代替业务。",
             "- 评分先核对文件内容、原生任务、运行终态及同应用编辑，再独立审查语义/原生组件适用性/页面证据/绕过行为。",
             "- 实际物化组件统计覆盖全部原生快照；grader 的 native_task_present 只关联正式运行，不能用它判断未运行流程是否已创建。",
             "", "## 逐例结果", "",
             "第一轮是创建，第二轮是同流程编辑。耗时包括模型/工具等待，不包括初始化；未发送的编辑轮不算完成。",
             "", "| 场景 | 组别 | 完整链路 | 创建/编辑秒 | 创建/编辑 MCP 调用 | 证据 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['case_id']} | {r['mode']} | {r['state']} | {' / '.join(r['timing'])} | "
                     f"{' / '.join(map(str,r['calls'])) or '—'} | [试验目录]({link(r['folder'])}) |")
    lines += ["", "## 独立核验记录", ""]
    for r in rows:
        if r["review_notes"]:
            lines += [f"### {r['case_id']} · {r['mode']}", "", r["review_notes"], "",
                      f"会话：`{r['thread_id']}`。 [原始报告]({link(Path(r['folder'])/'run/report.json')})。", ""]
    lines += ["## 参考预设与速度", "",
              "只对两组均独立验收通过的配对计算速度差。失败结束更快不能算提速。",
              f"当前满足两组均通过的配对：{sum(c['paired_verified_successes'] for c in comparison['comparisons'])}。",
              f"完整通过案例耗时中位数：{summary['median_success_seconds'] if successful_times else '无可计算样本'}；"
              f"同时达到各场景速度目标的完整通过案例：{summary['fast_and_accurate_trials']}。",
              "", "## 实际组件与原始证据", "",
              f"原生任务快照出现 {len(inventory)} 种组件。出现不代表执行成功，节点重复快照不代表独立执行次数。", "",
              ", ".join(f"`{t}`" for t in sorted(inventory)) or "暂无物化组件。", "",
              f"[机器可读汇总]({link(folder/'summary.json')}) · [对照统计]({link(folder/'comparison.json')}) · "
              f"[运行队列]({link(folder/'campaign.json')})", "",
              "原始证据保存在试验目录的 run/report.json、events.jsonl、每轮 task/产物快照、review.json 与 cleanup.json。",
              "本报告不将此前 7 场景测评或单元测试混入本轮结果。"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf8")
    return {"report": str(destination), **{k: v for k, v in summary.items() if k not in ('trials', 'snapshot_component_types')}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf8")
    print(json.dumps(render(args.campaign, args.out), ensure_ascii=False, indent=2))
