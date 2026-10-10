"""Run real model-driven RPA evaluations in persistent, independent Codex threads.

Unlike mcp_stress.py, this runner does not prescribe the model's tool sequence.
It only injects the local MCP server, sends user prompts, and records evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shizai_agent.core.agent import redact_payload  # noqa: E402


def turn_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Protocol/volume metrics are observations, never business success grades."""
    calls = [item for item in items if item.get("type") == "mcpToolCall"]
    return {
        "tool_calls": len(calls),
        "calls_by_tool": dict(Counter(item.get("tool") for item in calls)),
        "result_characters": sum(len(json.dumps(item.get("result"), ensure_ascii=False)) for item in calls),
        "catalog_result_characters": sum(
            len(json.dumps(item.get("result"), ensure_ascii=False))
            for item in calls if item.get("tool") == "shizai_rpa_component_catalog"
        ),
        "note": "Characters include protocol wrappers; completed turns do not establish business success.",
    }


def snapshot_outputs(data_dir: Path, case_name: str, destination: Path, started_at: float) -> list[dict[str, Any]]:
    """Preserve each turn's real outputs before the next edit can overwrite them."""
    records = []
    for path in data_dir.rglob("*"):
        if not path.is_file() or not path.name.startswith(case_name) or not path.resolve().is_relative_to(data_dir.resolve()):
            continue
        relative = path.relative_to(data_dir)
        stat = path.stat()
        record = {"path": str(path), "relative_path": relative.as_posix(), "size": stat.st_size,
                  "modified_at": stat.st_mtime, "fresh_for_turn": stat.st_mtime >= started_at - 1}
        if stat.st_size <= 5 * 1024 * 1024:
            content = path.read_bytes()
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            record.update(snapshot=str(target), sha256=hashlib.sha256(content).hexdigest())
        else:
            record["snapshot_omitted"] = "file exceeds the 5 MiB evaluation artifact limit"
        records.append(record)
    return records


def tool_payload(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result") or {}
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for block in result.get("content", []):
        try:
            value = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def disabled_mcp_overrides(executable: str, name: str) -> dict[str, Any]:
    """Disable an existing MCP without creating an incomplete transport table."""
    try:
        probe = subprocess.run(
            [executable, "mcp", "get", name, "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        payload = json.loads(probe.stdout) if probe.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    transport = payload.get("transport") if isinstance(payload, dict) else None
    if not isinstance(transport, dict) or transport.get("type") != "stdio" or not transport.get("command"):
        return {}
    prefix = f"mcp_servers.{name}"
    result: dict[str, Any] = {
        f"{prefix}.command": transport["command"],
        f"{prefix}.args": list(transport.get("args") or []),
    }
    if transport.get("cwd"):
        result[f"{prefix}.cwd"] = transport["cwd"]
    result[f"{prefix}.enabled"] = False
    return result


def snapshot_flows(turns: list[dict[str, Any]], destination: Path) -> list[dict[str, Any]]:
    """Read only applications explicitly created by this evaluation conversation."""
    projects = set()
    for turn in turns:
        for item in turn.get("items", []):
            if item.get("tool") != "shizai_rpa_start_design":
                continue
            payload = tool_payload(item)
            app = payload.get("application") or {}
            if app.get("created") and str(app.get("name", "")).startswith("Luna评测") and app.get("local_path"):
                projects.add(app["local_path"])
    records = []
    for project in sorted(projects):
        root = Path(project)
        for path in root.glob("*.task"):
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            content = path.read_bytes()
            target = destination / root.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            records.append({"project": project, "path": str(path), "snapshot": str(target),
                            "sha256": hashlib.sha256(content).hexdigest()})
    return records


class AppServer:
    def __init__(self, executable: str, output: Path, profile: str):
        output.mkdir(parents=True, exist_ok=True)
        self.events = (output / "events.jsonl").open("a", encoding="utf-8")
        self.errors = (output / "app-server.stderr.log").open("a", encoding="utf-8")
        self.incoming: queue.Queue[dict[str, Any]] = queue.Queue()
        self.serial = 0
        self.seen: list[dict[str, Any]] = []
        overrides = {}
        for server_name in ("node_repl", "cua_repl"):
            overrides.update(disabled_mcp_overrides(executable, server_name))
        overrides.update({
            "mcp_servers.shizai-agent.command": sys.executable,
            "mcp_servers.shizai-agent.args": ["-m", "shizai_agent.core.mcp_server"],
            "mcp_servers.shizai-agent.env.SHIZAI_MCP_TOOL_PROFILE": profile,
            "mcp_servers.shizai-agent.enabled": True,
            "mcp_servers.shizai-agent.startup_timeout_sec": 30,
            "mcp_servers.shizai-agent.tool_timeout_sec": 120,
            # The user authorizes fully accessible test sessions. Scope trust
            # to this injected server, without changing the global user config.
            "mcp_servers.shizai-agent.default_tools_approval_mode": "auto",
        })
        command = [executable, "app-server", "--stdio"]
        for key, value in overrides.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
            text=True, encoding="utf-8", cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            try:
                self.incoming.put(json.loads(line))
            except ValueError:
                self.incoming.put({"runner_error": "non_json_stdout", "line": line})
        self.incoming.put({"runner_error": "app_server_closed"})

    def send(self, value: dict[str, Any]) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout: float) -> dict[str, Any]:
        message = self.incoming.get(timeout=timeout)
        clean = redact_payload(message)
        self.events.write(json.dumps({"time": time.time(), "event": clean}, ensure_ascii=False) + "\n")
        self.events.flush()
        self.seen.append(clean)
        if "method" in message and "id" in message:
            # No silent approval of new scopes or model requests for user input.
            # Authorized test mutations use approvalPolicy=never and allow_write.
            self.send({"id": message["id"], "error": {
                "code": -32601, "message": "Evaluation requires autonomous work within the supplied test scope.",
            }})
        if message.get("runner_error"):
            raise RuntimeError(message["runner_error"])
        return message

    def rpc(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(max(0.1, deadline - time.monotonic()))
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message["result"]
        raise TimeoutError(method)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.events.close()
        self.errors.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, help="Absolute path to codex.exe (not a PowerShell shim).")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--profile", choices=["full", "rpa-authoring"], default="rpa-authoring")
    parser.add_argument("--prompts", type=Path, required=True, help="JSON array of user-prompt strings.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900, help="Seconds per model turn.")
    parser.add_argument("--case-name", default=f"Luna评测-{int(time.time())}", help="Replacement for {case_name} in prompts.")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    if not isinstance(prompts, list) or not prompts or not all(isinstance(p, str) for p in prompts):
        raise ValueError("prompts must be a non-empty array of strings")
    if not args.case_name.startswith("Luna评测"):
        raise ValueError("case-name must start with Luna评测")
    data_dir = ROOT / "_tmp" / "luna-eval" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    prompts = [prompt.replace("{case_name}", args.case_name).replace("{data_dir}", str(data_dir)) for prompt in prompts]
    report: dict[str, Any] = {"model_requested": args.model, "profile": args.profile, "case_name": args.case_name, "turns": []}
    report["implementation_sha256"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "shizai_agent/core").glob("*.py"))
    }

    def save() -> None:
        (output / "report.json").write_text(
            json.dumps(redact_payload(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )

    server = AppServer(args.codex, output, args.profile)
    active_turn: str | None = None
    thread_id: str | None = None
    try:
        initialized = server.rpc("initialize", {
            "clientInfo": {"name": "rpa-luna-evaluation", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        report["server"] = initialized
        server.send({"method": "initialized", "params": {}})
        models = server.rpc("model/list", {"includeHidden": True, "limit": 100})
        report["model_catalog_match"] = [
            row for row in models.get("data", []) if row.get("model") == args.model or row.get("id") == args.model
        ]
        workspace = output / "workspace"
        workspace.mkdir(exist_ok=True)
        start = server.rpc("thread/start", {
            "model": args.model, "allowProviderModelFallback": False,
            "cwd": str(workspace), "approvalPolicy": "never", "sandbox": "danger-full-access",
            "ephemeral": False,
            "developerInstructions": (
                "这是独立 RPA 能力测评。使用已经挂载的 shizai-agent MCP 工具完成用户任务，"
                "自行查询实时组件，不读取项目源码或已有测评答案。不得修改工具实现、调用其他模型或派生子代理。"
                "只允许创建名称以 Luna评测 开头的新测试应用，以及编辑本会话创建的测试应用；"
                f"文件输出仅允许 {data_dir} 内。"
                "禁止使用 shell/Python 直接代替业务文件操作、伪造输出或绕过 RPA 运行器。"
                "测试应用创建、保存和用户明确要求的测试运行已获授权，可传 allow_write=true。"
                "工具不可用、失败或缺少运行证据时如实报告，不将静态校验当成业务成功。"
            ),
            "config": {"model_reasoning_effort": "medium"},
        })
        thread_id = start["thread"]["id"]
        report.update({"thread_id": thread_id, "model_resolved": start.get("model"), "model_provider": start.get("modelProvider")})
        if start.get("model") != args.model:
            raise RuntimeError(f"Requested model was changed: {start.get('model')}")
        server.rpc("thread/name/set", {"threadId": thread_id, "name": f"Luna RPA 能力测评 · {args.profile}"})
        save()
        print(json.dumps({"thread_id": thread_id, "model": start.get("model"), "profile": args.profile}), flush=True)
        for index, prompt in enumerate(prompts, 1):
            started_at = time.monotonic()
            started_wall_time = time.time()
            begin = len(server.seen)
            response = server.rpc("turn/start", {
                "threadId": thread_id, "model": args.model, "effort": "medium",
                "input": [{"type": "text", "text": prompt}],
            })
            active_turn = response["turn"]["id"]
            record: dict[str, Any] = {"index": index, "prompt": prompt, "turn_id": active_turn, "status": "running"}
            report["turns"].append(record)
            save()
            deadline = time.monotonic() + args.timeout
            last_progress = 0.0
            while time.monotonic() < deadline:
                try:
                    message = server.receive(min(10, max(0.1, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if message.get("method") == "item/completed":
                    item = message.get("params", {}).get("item", {})
                    if item.get("type") in {"mcpToolCall", "agentMessage"}:
                        text = item.get("text", "")
                        print(json.dumps({"turn": index, "type": item.get("type"),
                                          "tool": item.get("tool"), "status": item.get("status"),
                                          "text": text[:350]}, ensure_ascii=False), flush=True)
                if time.monotonic() - last_progress > 30:
                    save()
                    last_progress = time.monotonic()
                if message.get("method") == "turn/completed":
                    turn = message.get("params", {}).get("turn", {})
                    if turn.get("id") == active_turn:
                        record["status"] = turn.get("status")
                        record["error"] = turn.get("error")
                        active_turn = None
                        break
            if active_turn:
                server.rpc("turn/interrupt", {"threadId": thread_id, "turnId": active_turn})
                record["status"] = "timeout"
                active_turn = None
            record["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
            record["items"] = [
                event.get("params", {}).get("item") for event in server.seen[begin:]
                if event.get("method") == "item/completed"
            ]
            record["metrics"] = turn_metrics(record["items"])
            record["artifacts"] = snapshot_outputs(data_dir, args.case_name, output / f"turn-{index}-artifacts", started_wall_time)
            record["flows"] = snapshot_flows(report["turns"], output / f"turn-{index}-flows")
            save()
            print(json.dumps({"turn": index, "status": record["status"], "seconds": record["elapsed_seconds"]}), flush=True)
            if record["status"] != "completed":
                break
    except Exception as exc:
        report["runner_error"] = f"{type(exc).__name__}: {exc}"
        save()
        if active_turn and thread_id:
            try:
                server.rpc("turn/interrupt", {"threadId": thread_id, "turnId": active_turn}, timeout=10)
            except Exception:
                pass
        raise
    finally:
        server.close()
    print(f"Report: {output / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
