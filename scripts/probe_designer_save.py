"""Save the active designer editor through its renderer RPC and verify disk state."""

from __future__ import annotations

import argparse
import json
from typing import Any

from shizai_agent.core import project as project_core
from shizai_agent.core.cloud_api import ShizaiDesktopAgent, _desktop_channel_lock
from shizai_agent.core.native_ui import _NativeUiChannel
from shizai_agent.core.paths import ShizaiPaths
from shizai_agent.core.workflow_edit import flow_revision, read_edit_context

ACTIVE_EDITOR_TARGET = "$CurrentActiveEditorClientId$"


def _call_active_editor(
    client: ShizaiDesktopAgent,
    *,
    service: str,
    method: str,
    args: list[Any],
) -> Any:
    channel = _NativeUiChannel(client)
    with _desktop_channel_lock(client.config.timeout, f"designer {method} probe"):
        sock = client._connect(max(client.config.timeout, 5.0))
        try:
            return channel._call_renderer(
                sock,
                target_client_id=ACTIVE_EDITOR_TARGET,
                service=service,
                method=method,
                args=args,
            )
        finally:
            sock.close()


def _top_level_steps(root: Any) -> list[str]:
    summary = read_edit_context(ShizaiPaths.from_environment(), str(root))
    steps = summary.get("steps") if isinstance(summary, dict) else None
    if not isinstance(steps, list):
        return []
    return [str(step.get("id")) for step in steps if isinstance(step, dict) and step.get("id")]


def save_active_editor(flow_ref: str, *, allow_save: bool = False) -> dict[str, Any]:
    paths = ShizaiPaths.from_environment()
    root = project_core.resolve_project(paths, flow_ref)
    client = ShizaiDesktopAgent()
    revision_before = flow_revision(root)
    steps_before = _top_level_steps(root)
    dirty_before = _call_active_editor(
        client,
        service="WorkbenchService",
        method="isNeedSave",
        args=[],
    )
    result: dict[str, Any] = {
        "kind": "designer_save_probe",
        "flow_ref": str(root),
        "revision_before": revision_before,
        "steps_before": steps_before,
        "dirty_before": dirty_before,
        "save_requested": False,
    }
    if not allow_save:
        result["message"] = "Preview only; pass --allow-save to save the active editor."
        return result

    save_result = _call_active_editor(
        client,
        service="PackageSaveService",
        method="saveAll",
        args=[{"alertOnError": True}],
    )
    dirty_after = _call_active_editor(
        client,
        service="WorkbenchService",
        method="isNeedSave",
        args=[],
    )
    revision_after = flow_revision(root)
    steps_after = _top_level_steps(root)
    result.update(
        {
            "save_requested": True,
            "save_result": save_result,
            "dirty_after": dirty_after,
            "revision_after": revision_after,
            "steps_after": steps_after,
            "component_count_after": len(steps_after),
            "persisted_change": revision_after != revision_before,
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--allow-save", action="store_true")
    args = parser.parse_args()
    result = save_active_editor(args.flow_ref, allow_save=args.allow_save)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
