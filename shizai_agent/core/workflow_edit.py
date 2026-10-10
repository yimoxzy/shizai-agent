"""Read-only native edit context and optimistic revision checks, shared by CLI/MCP."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class WorkflowEditError(ValueError):
    """An actionable edit failure that does not require rebuilding the project."""

    def __init__(self, code: str, message: str, **details: Any):
        self.result = {"error_code": code, "message": message, "saved": False, **details}
        super().__init__(message)


def flow_revision(root: Path) -> str:
    """Hash authoritative files, including subflows and variable/element bindings.

    Runtime logs and generated indexes are deliberately excluded. This is a
    stale-read guard, not a lock on the external designer's filesystem writes.
    """
    files = set(root.rglob("*.task"))
    files.update(root / name for name in (
        "szrpa.json", "elements.json", "globalVariable", "variableInfo", "rpa-flow.json",
    ))
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes() if path.is_file() else b"<missing>"
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def check_revision(root: Path, expected: str | None) -> str:
    current = flow_revision(root)
    if expected is not None and expected != current:
        raise WorkflowEditError(
            "revision_conflict", "流程已变化，请重新读取目标步骤，再根据最新参数修改。",
            expected_revision=expected, current_revision=current, retryable=True,
            next_call={"tool": "shizai_rpa_read_flow", "arguments": {"flow_ref": str(root), "detail": "edit"}},
        )
    return current


def native_parameters(node: dict[str, Any]) -> dict[str, Any]:
    """Keep native expressions intact; callers must not re-encode them as literals."""
    return {
        str(entry["name"]): entry.get("value")
        for group in ("args0", "options0")
        for entry in node.get(group, [])
        if isinstance(entry, dict) and entry.get("name")
    }


def parameter_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    from .agent import redact_payload

    old, new = native_parameters(before), native_parameters(after)
    return [
        {"parameter": key, "before": redact_payload(old.get(key), key=key),
         "after": redact_payload(new.get(key), key=key),
         "before_present": key in old, "after_present": key in new}
        for key in sorted(old.keys() | new.keys()) if key not in old or key not in new or old[key] != new[key]
    ]


def read_edit_context(paths: Any, flow_ref: str, *, step_id: str | None = None) -> dict[str, Any]:
    """Return all node IDs, but parameters/contracts only for a selected node.

    Read the native task directly so components absent from an old compatibility
    whitelist and nested branch nodes remain visible for editing.
    """
    from . import project

    root = project.resolve_project(paths, flow_ref)
    revision = flow_revision(root)
    task_path, payload = project._native_task_file(root)
    entities = payload.get("componentModelEntities", {})
    order = project._native_statement_ids(payload)
    ids = list(dict.fromkeys(order + [str(key) for key in entities if key != "MAIN_COMPONENT"]))
    steps = [
        {"id": key, "component": entities[key].get("type"),
         "label": entities[key].get("label") or entities[key].get("name") or "",
         "top_level": key in order, "parameter_edit_supported": key in order}
        for key in ids if isinstance(entities.get(key), dict)
    ]
    result: dict[str, Any] = {
        "kind": "native_rpa_edit_context", "flow_ref": str(root), "flow_file": str(task_path),
        "revision": revision, "steps": steps, "top_level_order": order,
        "truth_source": "native_task", "runtime_verified": False,
        "rules": [
            "按稳定 step_id 修改现有流程；列表中的嵌套节点不表示线性执行顺序。",
            "native_parameters 是原生表达式；config 只传本次改动的语义参数，其余字段自动保留，不复制整份参数。",
            "先用 dry_run=true 预览；提交携带 expected_revision，冲突后重读，禁止盲目重试。",
            "预览只检查参数转换，不代表静态或运行校验通过；保存后仍须验证当前组件和输出。",
        ],
    }
    if step_id is not None:
        node = entities.get(step_id)
        if not isinstance(node, dict) or step_id == "MAIN_COMPONENT":
            raise WorkflowEditError("step_not_found", "目标步骤不存在，请从 steps 中选择稳定 id。", step_id=step_id)
        component = str(node.get("type") or "")
        declared_element_ids = node.get("mcpElementIds")
        if isinstance(declared_element_ids, list):
            element_ids = [str(item).strip() for item in declared_element_ids if str(item).strip()]
        else:
            element_ids = project._native_element_ids_from_node(node)
        result["selected_step"] = {
            "id": step_id, "component": component,
            "parameter_edit_supported": step_id in order,
            "element_ids": element_ids,
            "native_parameters": native_parameters(node),
            "contract": project._live_rpa_contract(paths, component),
        }
        result["next_call"] = {
            "tool": "shizai_rpa_update_step",
            "arguments": {"flow_ref": str(root), "step_id": step_id, "expected_revision": revision, "dry_run": True},
            "missing_arguments": ["config"],
        }
        if step_id not in order:
            result["next_call"] = None
            result["limitation"] = "嵌套节点可读取；当前参数编辑只支持主流程步骤，不能直接提交此节点。"
    else:
        result["next_call"] = {
            "tool": "shizai_rpa_read_flow", "arguments": {"flow_ref": str(root), "detail": "edit"},
            "missing_arguments": ["step_id"],
        }
    check_revision(root, revision)
    return project.redact_payload(result)
