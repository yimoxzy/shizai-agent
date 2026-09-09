"""Environment diagnostics for the local Shizai desktop installation."""

from __future__ import annotations

import json
import socket
from typing import Any

from .paths import ShizaiPaths


def _port_from_config(paths: ShizaiPaths) -> int | None:
    if not paths.run_config.is_file():
        return None
    try:
        config = json.loads(paths.run_config.read_text(encoding="utf-8"))
        value = config.get("nodePortInfo", {}).get("nodeBizPort")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def _port_status(port: int | None) -> dict[str, Any]:
    if port is None:
        return {"configured": False, "port": None, "reachable": False}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        reachable = sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()
    return {"configured": True, "port": port, "reachable": reachable}


def diagnose(paths: ShizaiPaths, project_ref: str | None = None) -> dict[str, Any]:
    """Collect non-mutating checks needed by the harness."""
    port = _port_from_config(paths)
    checks = {
        "data_root": paths.data_root.is_dir(),
        "applications_root": paths.applications_root.is_dir(),
        "sqlite_db": paths.sqlite_db.is_file(),
        "run_config": paths.run_config.is_file(),
        "install_root": bool(paths.install_root and paths.install_root.is_dir()),
        "executable": bool(paths.executable and paths.executable.is_file()),
        "agent_brain_bundle": bool(
            paths.install_root
            and (paths.install_root / "resources" / "app" / "resources" / "fe-assets" / "agent-brain").is_dir()
        ),
    }
    runtime_logs = sorted(
        (paths.data_root / "logs").rglob("II.RPA.RuntimeHost.txt*") if (paths.data_root / "logs").is_dir() else [],
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    runtime_message = None
    if runtime_logs:
        try:
            tail = runtime_logs[0].read_bytes()[-131072:].decode("utf-8", errors="replace")
            if "未安装" in tail and "RPA" in tail:
                runtime_message = "检测到运行时日志提示 Chrome RPA 插件未安装或未启用；设计阶段 DeepCapture 正常不代表运行阶段插件已就绪。"
        except OSError:
            pass
    native_rpa = None
    if project_ref:
        try:
            from .project import native_rpa_sync_diagnose

            native_rpa = native_rpa_sync_diagnose(paths, project_ref)
        except (FileNotFoundError, OSError, ValueError) as exc:
            native_rpa = {"project": project_ref, "can_run": False, "errors": [str(exc)], "warnings": []}
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "paths": paths.as_dict(),
        "business_port": _port_status(port),
        "runtime": {
            "runtime_host_log": str(runtime_logs[0]) if runtime_logs else None,
            "runtime_host_log_exists": bool(runtime_logs),
            "message": runtime_message,
        },
        "native_rpa": native_rpa,
    }
