"""Interactive driver for a live MCP browser-extension evaluation.

The process keeps one bridge session alive while a reviewer inspects a captured
viewport and submits screenshot coordinates. Each JSON command read from stdin
produces one JSON response on stdout and one durable JSONL measurement record.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shizai_agent.core.browser_mcp import (
    BrowserBridgeManager,
    bind_verified_elements,
    collection_elements,
    point_elements,
)
from shizai_agent.core.paths import ShizaiPaths


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _without_image(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key != "dataUrl"}


def _append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _call(manager: BrowserBridgeManager, command: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    operation = str(command.get("op") or "").strip()
    if operation == "status":
        result = manager.status(connect_wait=float(command.get("connect_wait", 0)))
    elif operation == "tabs":
        result = manager.call("list_tabs")
    elif operation == "activate":
        if command.get("allow_write") is not True:
            raise PermissionError("tab activation requires allow_write: true")
        result = manager.call("activate_tab", {"tabId": int(command["tab_id"])}, timeout=10)
    elif operation == "capture":
        result = manager.call("capture_viewport", {"tabId": int(command["tab_id"])}, timeout=30)
    elif operation == "inspect":
        result = manager.call(
            "inspect_points",
            {
                "tabId": int(command["tab_id"]),
                "screenshotId": str(command["screenshot_id"]),
                "pageRevision": str(command["page_revision"]),
                "points": list(command["points"]),
            },
            timeout=30,
        )
    elif operation == "infer":
        result = manager.call(
            "infer_repeated_collection",
            {
                "tabId": int(command["tab_id"]),
                "screenshotId": str(command["screenshot_id"]),
                "pageRevision": str(command["page_revision"]),
                "fields": list(command["fields"]),
                "sampleLimit": int(command.get("sample_limit", 5)),
            },
            timeout=30,
        )
    elif operation == "action":
        params = {
            "tabId": int(command["tab_id"]),
            "action": str(command["action"]),
            "target": dict(command["target"]),
        }
        for source, target in (
            ("value", "value"), ("key", "key"), ("delta_x", "deltaX"),
            ("delta_y", "deltaY"), ("into_view", "intoView"),
            ("page_revision", "pageRevision"),
        ):
            if source in command:
                params[target] = command[source]
        result = manager.call("performAction", params, timeout=30)
    else:
        raise ValueError(f"unsupported operation: {operation!r}")

    project_ref = str(command.get("project_ref") or "").strip()
    step_id = str(command.get("step_id") or "").strip()
    if bool(project_ref) != bool(step_id):
        raise ValueError("project_ref and step_id must be provided together")
    if project_ref:
        if command.get("allow_write") is not True:
            raise PermissionError("native binding requires allow_write: true")
        if operation not in {"inspect", "infer"} or not isinstance(result, dict):
            raise ValueError("native binding is supported only for inspect and infer results")
        converter = point_elements if operation == "inspect" else collection_elements
        elements = converter(result, page=result, prefix=f"{step_id}-browser")
        result["native_binding"] = bind_verified_elements(
            ShizaiPaths.from_environment(),
            project_ref=project_ref,
            step_id=step_id,
            elements=elements,
        )
        result["capture_source"] = "browser_bridge"
        result["verification_status"] = "verified"

    metadata: dict[str, Any] = {}
    if operation == "capture" and isinstance(result, dict):
        data_url = str(result.get("dataUrl") or "")
        if not data_url.startswith("data:image/png;base64,"):
            raise ValueError("capture did not return a PNG data URL")
        screenshot_path = Path(str(command["screenshot_path"])).expanduser().resolve()
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        metadata["screenshot_path"] = str(screenshot_path)
        metadata["screenshot_bytes"] = screenshot_path.stat().st_size
    return result, metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    manager = BrowserBridgeManager(config_path=args.config.resolve())
    print(json.dumps({"ready": True, "config": str(args.config.resolve())}), flush=True)
    try:
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            if text in {"quit", "exit"}:
                break
            started = time.perf_counter()
            command: dict[str, Any] = {}
            try:
                command = json.loads(text)
                result, metadata = _call(manager, command)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                response_bytes = _json_bytes(result)
                output = {
                    "ok": True,
                    "op": command.get("op"),
                    "elapsed_ms": elapsed_ms,
                    "response_bytes": response_bytes,
                    **metadata,
                    "result": _without_image(result),
                }
            except Exception as exc:
                output = {
                    "ok": False,
                    "op": command.get("op"),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            _append_record(
                args.log.resolve(),
                {
                    "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
                    **output,
                    "command": command,
                },
            )
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
