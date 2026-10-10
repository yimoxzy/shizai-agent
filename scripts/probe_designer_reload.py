"""Manually probe the designer's same-application reload RPC.

This stays outside the production MCP surface until a live test confirms that
the installed designer reloads its canvas without corrupting persisted files.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from shizai_agent.core import project as project_core
from shizai_agent.core.cloud_api import ShizaiDesktopAgent, _desktop_channel_lock
from shizai_agent.core.native_ui import _NativeUiChannel
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_edit import check_revision, flow_revision

HOME_RENDERER_TARGET = "$HomeClientId$"
ACTIVE_EDITOR_TARGET = "$CurrentActiveEditorClientId$"


def _reload_through_home_renderer(client: ShizaiDesktopAgent, request: dict[str, Any]) -> Any:
    channel = _NativeUiChannel(client)
    with _desktop_channel_lock(client.config.timeout, "designer reload probe"):
        sock = client._connect(max(client.config.timeout, 5.0))
        try:
            return channel._call_renderer(
                sock,
                target_client_id=HOME_RENDERER_TARGET,
                service="ApplicationOpService",
                method="editApplicationInAgent",
                args=[request],
            )
        finally:
            sock.close()


def _reload_active_editor(client: ShizaiDesktopAgent) -> Any:
    channel = _NativeUiChannel(client)
    with _desktop_channel_lock(client.config.timeout, "active editor reload probe"):
        sock = client._connect(max(client.config.timeout, 5.0))
        try:
            return channel._call_renderer(
                sock,
                target_client_id=ACTIVE_EDITOR_TARGET,
                service="WorkbenchService",
                method="reload",
                args=[],
            )
        finally:
            sock.close()


def probe_designer_reload(
    paths: ShizaiPaths,
    flow_ref: str,
    *,
    expected_revision: str | None = None,
    allow_reload: bool = False,
    strategy: str = "application-reopen",
    client: Any | None = None,
) -> dict[str, Any]:
    """Preview or request a same-application editor reload through native RPC."""
    root = project_core.resolve_project(paths, flow_ref)
    revision_before = check_revision(root, expected_revision)
    package = project_core._native_package_summary(paths, root)
    application_id = str(package.get("application_id") or "").strip()
    if not package.get("matched") or not application_id:
        raise ValueError("flow is not registered in the designer application_package index")

    if strategy not in {"application-reopen", "active-editor-reload"}:
        raise ValueError("strategy must be application-reopen or active-editor-reload")
    request = {"applicationId": application_id, "packageUuid": "", "status": "local"}
    rpc = (
        "HomeClientId/ApplicationOpService.editApplicationInAgent"
        if strategy == "application-reopen"
        else "CurrentActiveEditorClientId/WorkbenchService.reload"
    )
    rpc_arguments = [request] if strategy == "application-reopen" else []
    result: dict[str, Any] = {
        "kind": "designer_reload_probe",
        "flow_ref": str(root),
        "application_id": application_id,
        "application_name": package.get("application_name"),
        "revision_before": revision_before,
        "strategy": strategy,
        "rpc": rpc,
        "rpc_arguments": rpc_arguments,
        "reload_requested": False,
        "canvas_reload_verified": False,
    }
    if not allow_reload:
        result["message"] = "Preview only; pass --allow-reload to request the editor reload."
        return result

    caller = client or ShizaiDesktopAgent()
    raw = (
        _reload_through_home_renderer(caller, request)
        if strategy == "application-reopen"
        else _reload_active_editor(caller)
    )
    revision_after = flow_revision(root)
    acknowledged = not isinstance(raw, dict) or raw.get("isSuccess") is not False
    result.update(
        {
            "reload_requested": True,
            "rpc_acknowledged": acknowledged,
            "rpc_result": raw,
            "revision_after": revision_after,
            "persisted_revision_unchanged": revision_after == revision_before,
            "message": "The renderer acknowledged the reload request; observe the canvas for the third component.",
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--expected-revision")
    parser.add_argument("--allow-reload", action="store_true")
    parser.add_argument(
        "--strategy",
        choices=("application-reopen", "active-editor-reload"),
        default="application-reopen",
    )
    args = parser.parse_args()
    result = probe_designer_reload(
        ShizaiPaths.from_environment(),
        args.flow_ref,
        expected_revision=args.expected_revision,
        allow_reload=args.allow_reload,
        strategy=args.strategy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
