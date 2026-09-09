"""Loss-conscious presentation of workflow results for model clients."""

from copy import deepcopy
from typing import Any


def compact_design_result(value: dict[str, Any]) -> dict[str, Any]:
    """Keep created identity, step validation and recovery evidence, not catalogs.

    Rejections are returned verbatim. This function never marks a validation or
    a run successful, and never mutates the full result held by the caller.
    """
    if value.get("kind") != "shizai_rpa_started":
        return value
    omitted = {"component_catalog", "first_component", "planned_components", "request_analysis",
               "validated_plan", "plan_attachment", "native_ui"}
    result = {key: item for key, item in value.items() if key not in omitted}
    result["response_mode"] = "compact"
    result["runtime_verified"] = False
    result["steps"] = []
    for written in value.get("planned_components", []):
        step = written.get("step", {})
        result["steps"].append({
            "id": step.get("id"), "component": step.get("component"),
            "binding_status": step.get("binding_status"),
            "step_validation": written.get("step_validation"),
        })
    analysis = value.get("request_analysis") or {}
    result["analysis_id"] = analysis.get("analysis_id")
    validated = value.get("validated_plan") or {}
    result["plan_validation"] = {key: item for key, item in validated.items() if key != "plan"}
    attachment = value.get("plan_attachment") or {}
    result["plan_id"] = attachment.get("plan_id")
    result["read_call"] = {
        "tool": "shizai_rpa_read_flow",
        "arguments": {"flow_ref": value.get("flow_ref"), "detail": "edit"},
    }
    # Complete graph construction needs validation next, not another add_step.
    # Keep an explicit browser/capture recovery action when one exists.
    if value.get("design_complete") and not value.get("design_time_browser"):
        result["next_action"] = {
            "tool": "shizai_rpa_validate_flow",
            "arguments": {"flow_ref": value.get("flow_ref")},
            "reason": "完整图已保存；检查静态状态，再补齐元素和运行证据，不重复追加节点。",
        }
        result["next_tool"] = result["next_action"]["tool"]
    return result


def authoring_result(name: str, value: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Adapt presentation and actionable next calls to the advertised profile."""
    if name == "shizai_rpa_start_design":
        return compact_design_result(value)
    if name == "shizai_rpa_analyze_request":
        result = deepcopy(value)
        query = result.get("planning_requirements", {}).get("recommended_catalog_query")
        if isinstance(query, dict):
            query.update(detail="summary", limit=20)
        return result
    if name == "shizai_rpa_capture_elements":
        binding = value.get("binding") or {}
        gate = value.get("verification_gate") or binding.get("verification_gate") or {}
        action = gate.get("next_action") or {}
        if gate.get("status") in {"static_failed", "runtime_pending"} and action.get("tool"):
            result = deepcopy(value)
            result["after_step_verification"] = result.get("next_action")
            result["next_action"] = action
            result["next_tool"] = action["tool"]
            if isinstance(result.get("binding"), dict):
                result["binding"]["next_action"] = action
            return result
    flow_ref = arguments.get("project_ref") or (value.get("next_action") or {}).get("arguments", {}).get("flow_ref")
    if name == "shizai_rpa_scan_page" and flow_ref and (value.get("next_action") or {}).get("tool") == "shizai_rpa_components":
        result = dict(value)
        result["next_action"] = {
            "tool": "shizai_rpa_read_flow",
            "arguments": {"flow_ref": flow_ref, "detail": "edit"},
            "reason": "读取已创建节点的稳定 id，再为目标元素步骤捕获；此工具集不需要重复追加节点。",
        }
        return result
    if name == "shizai_rpa_validate_flow" and value.get("valid"):
        # validate_project is intentionally omitted from the authoring tools.
        # The run boundary already performs native project diagnosis. Do not
        # send a complete file-only graph into unnecessary browser/DAP checks.
        result = dict(value)
        result["next_tool"] = "shizai_rpa_run"
        result["next_action"] = {
            "tool": "shizai_rpa_run",
            "arguments": {"project_ref": arguments["flow_ref"]},
            "reason": "静态检查通过，尚未运行；仅用户要求执行时调用 run，其内部检查项目和运行条件。只要求生成时可交付保存结果。",
        }
        if isinstance(result.get("ui_feedback"), dict):
            result["ui_feedback"] = {**result["ui_feedback"], "next_tool": result["next_tool"]}
        return result
    if name == "shizai_rpa_materialize" and value.get("ready_for_runtime_diagnosis"):
        # The authoring profile intentionally keeps the run boundary as the
        # single executable handoff.  Materialize used to return
        # ``sync_diagnose`` here, but that tool is outside the compact profile;
        # an unavailable next_tool leaves small models waiting after a valid
        # flow.  shizai_rpa_run performs the same diagnosis before submission.
        result = deepcopy(value)
        project_ref = arguments.get("project_ref") or value.get("project_ref")
        result["next_tool"] = "shizai_rpa_run"
        result["next_action"] = {
            "tool": "shizai_rpa_run",
            "arguments": {"project_ref": project_ref},
            "reason": "运行树已生成；run 会先执行项目与运行诊断，再提交真实流程。",
        }
        if isinstance(result.get("ui_feedback"), dict):
            result["ui_feedback"] = {**result["ui_feedback"], "next_tool": result["next_tool"]}
        return result
    if name in {"shizai_rpa_run", "shizai_rpa_status"}:
        # Runtime responses can contain every input file and historical log
        # entry discovered by diagnosis.  Returning that unbounded payload to
        # a small model makes the final turn needlessly expensive and can
        # prevent it from issuing the next edit.  Keep the current run's
        # contract and evidence pointers; the raw event stream remains the
        # audit record for clients that need full detail.
        source = deepcopy(value)
        nested_status = source.get("status") if isinstance(source.get("status"), dict) else {}
        nested_run = nested_status.get("run") if isinstance(nested_status.get("run"), dict) else {}
        current_output = nested_status.get("output") if isinstance(nested_status.get("output"), dict) else source.get("output")
        runtime_evidence = (
            nested_status.get("runtime_evidence")
            if isinstance(nested_status.get("runtime_evidence"), dict)
            else nested_run.get("runtime_evidence")
        )
        diagnosis = source.get("diagnosis") if isinstance(source.get("diagnosis"), dict) else nested_status.get("diagnosis")
        terminal_status = source.get("terminal_status") or nested_run.get("terminal_status") or nested_status.get("terminal_status")
        status_value = nested_status.get("status") if nested_status.get("status") is not None else source.get("status")
        if not terminal_status:
            terminal_status = {
                "succeeded": "success", "completed": "success", "success": "success",
                "failed": "failed", "error": "failed", "blocked": "blocked",
                "running": "running", "submitted": "running", "stopping": "stopping",
            }.get(str(status_value).casefold())
        completed_value = source.get("completed") or nested_run.get("completed")
        if completed_value is None and terminal_status in {"success", "failed", "blocked"}:
            completed_value = True
        compact: dict[str, Any] = {
            "kind": source.get("kind", name),
            "project_ref": arguments.get("project_ref") or source.get("project_ref") or nested_status.get("project"),
            "status": status_value if not isinstance(status_value, dict) else source.get("status_explanation"),
            "terminal_status": terminal_status,
            "accepted": source.get("accepted") or nested_run.get("accepted"),
            "completed": completed_value,
            "timed_out": source.get("timed_out") if source.get("timed_out") is not None else nested_run.get("timed_out"),
            "run_uuid": source.get("run_uuid") or nested_run.get("uuid"),
            "workflow_run_id": source.get("workflow_run_id") or nested_run.get("workflow_run_id"),
            "output": current_output,
            "runtime_evidence": runtime_evidence,
            "failure_kind": source.get("failure_kind") or nested_status.get("failure_kind"),
            "cleanup_status": source.get("cleanup_status") or nested_status.get("cleanup_status") or (nested_run.get("runtime_cleanup") or {}).get("status"),
            "cleanup_complete": source.get("cleanup_complete") or nested_status.get("cleanup_complete"),
            "interaction_required": source.get("interaction_required") or nested_status.get("interaction_required"),
            "interaction": source.get("interaction") or nested_status.get("interaction"),
            "ui_feedback": source.get("ui_feedback") or nested_status.get("ui_feedback"),
            "message": source.get("message") or source.get("status_explanation"),
        }
        if isinstance(diagnosis, dict):
            runtime = diagnosis.get("runtime") if isinstance(diagnosis.get("runtime"), dict) else {}
            compact["diagnosis"] = {
                "ready": diagnosis.get("ready"),
                "can_run": diagnosis.get("can_run"),
                "message": diagnosis.get("message") or runtime.get("message"),
                "runtime_browser_plugin_ready": runtime.get("runtime_browser_plugin_ready"),
            }
        compact["next_tool"] = "shizai_rpa_status" if name == "shizai_rpa_run" and compact.get("terminal_status") not in {"success", "failed", "blocked"} else None
        return compact
    return value
