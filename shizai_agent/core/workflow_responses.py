"""Loss-conscious presentation of workflow results for model clients."""

from copy import deepcopy
from typing import Any


def _compact_step_validation(value: Any) -> Any:
    if not isinstance(value, dict) or not ({"checks", "verification_gate"} & value.keys()):
        return value
    result = {
        key: deepcopy(value[key])
        for key in ("kind", "step_id", "component", "valid", "errors", "warnings", "output_variables")
        if key in value
    }
    pagination = value.get("pagination_capability")
    if isinstance(pagination, dict):
        result["pagination"] = {
            key: deepcopy(pagination[key])
            for key in ("available", "selected_mode", "selector_argument", "selector_bound", "message")
            if key in pagination
        }
    gate = value.get("verification_gate")
    if isinstance(gate, dict):
        runtime = gate.get("runtime_validation") if isinstance(gate.get("runtime_validation"), dict) else {}
        result["verification"] = {
            "status": gate.get("status"),
            "runtime_verified": runtime.get("verified"),
            "next_action": deepcopy(gate.get("next_action")),
        }
    return result


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
            "step_validation": _compact_step_validation(written.get("step_validation")),
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
    unbound_steps = [step for step in result["steps"] if step.get("binding_status") == "unbound"]
    browser = value.get("design_time_browser")
    browser_ready = isinstance(browser, dict) and browser.get("ready") is True
    if unbound_steps and not browser_ready:
        result["next_action"] = {
            "tool": "shizai_rpa_open_browser",
            "arguments": {"project_ref": value.get("flow_ref"), "allow_write": True},
            "reason": "流程骨架已保存且仍有网页元素未绑定；先打开计划目标页，再扫描和绑定当前步骤。",
        }
        result["next_tool"] = result["next_action"]["tool"]
    # Complete graphs without browser bindings can proceed directly to static
    # validation. Browser graphs must first establish real page evidence above.
    elif value.get("design_complete") and not browser:
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
    if name == "shizai_rpa_capture_page_fields":
        result = {
            key: deepcopy(value[key])
            for key in (
                "kind", "project_ref", "step_id", "complete", "fields", "required_fields",
                "element_count", "selected_field_ids", "missing_fields", "pending_recapture_fields",
                "selector_scope", "selected_card_context", "selected_card_grouping", "record_alignment",
                "quality_warnings", "selection_errors", "capture_errors", "capture_session_recovery",
                "recovery_actions", "next_action",
            )
            if key in value
        }
        binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
        if binding:
            result["binding"] = {
                key: deepcopy(binding[key])
                for key in ("flow_ref", "step_id", "component", "element_ids", "columns", "valid", "errors", "warnings")
                if key in binding
            }
            if "step_validation" in binding:
                result["binding"]["step_validation"] = _compact_step_validation(binding["step_validation"])
        state = value.get("result_state") if isinstance(value.get("result_state"), dict) else {}
        if state:
            result["result_state"] = {
                key: deepcopy(state[key])
                for key in ("result_elements_ready", "captured_fields", "required_fields", "next_action")
                if key in state
            }
        native_ui = value.get("native_ui") if isinstance(value.get("native_ui"), dict) else {}
        access = native_ui.get("access") if isinstance(native_ui.get("access"), dict) else {}
        if access:
            result["native_ui"] = {"access": deepcopy(access)}
        action = result.get("next_action")
        if not isinstance(action, dict) and isinstance(state.get("next_action"), dict):
            action = deepcopy(state["next_action"])
            result["next_action"] = action
        result["next_tool"] = action.get("tool") if isinstance(action, dict) else None
        return result
    next_action = value.get("next_action")
    next_action = next_action if isinstance(next_action, dict) else {}
    next_arguments = next_action.get("arguments")
    next_arguments = next_arguments if isinstance(next_arguments, dict) else {}
    flow_ref = arguments.get("project_ref") or next_arguments.get("flow_ref")
    if name == "shizai_rpa_scan_page" and flow_ref and (value.get("next_action") or {}).get("tool") == "shizai_rpa_components":
        result = dict(value)
        result["next_action"] = {
            "tool": "shizai_rpa_read_flow",
            "arguments": {"flow_ref": flow_ref, "detail": "edit"},
            "reason": "读取已创建节点的稳定 id，再为目标元素步骤捕获；此工具集不需要重复追加节点。",
        }
        return result
    if name == "shizai_rpa_validate_flow" and not value.get("valid"):
        flow_ref = arguments.get("flow_ref") or value.get("flow_ref")
        validations = [
            _compact_step_validation(item)
            for item in value.get("step_validations", [])
            if isinstance(item, dict) and not item.get("valid")
        ]
        first = validations[0] if validations else {}
        step_id = first.get("step_id")
        component = first.get("component")
        first_errors = [str(item) for item in first.get("errors", [])]
        needs_fields = component == "WebBrowser_DataCollectionV5" and any(
            marker in error
            for error in first_errors
            for marker in ("bound element count", "output columns", "requested output columns")
        )
        needs_pagination = component == "WebBrowser_DataCollectionV5" and any(
            "selector_page" in error or "selector_more" in error for error in first_errors
        )
        if needs_fields:
            action = {
                "tool": "shizai_rpa_capture_page_fields",
                "arguments": {"project_ref": flow_ref, "step_id": step_id, "allow_write": True},
                "reason": "数据采集步骤缺少输出字段绑定；一次捕获同一条记录的所需字段。",
            }
        elif needs_pagination:
            action = {
                "tool": "shizai_rpa_scan_page",
                "arguments": {"project_ref": flow_ref, "step_id": step_id, "query": "下一页"},
                "reason": "采集字段已绑定；扫描分页控件后用 capture_elements(as_page_selector=true) 绑定。",
            }
        elif step_id:
            action = {
                "tool": "shizai_rpa_read_flow",
                "arguments": {"flow_ref": flow_ref, "detail": "edit", "step_id": step_id},
                "reason": "读取首个失败步骤的当前参数和 revision，再按具体错误修复。",
            }
        else:
            action = {
                "tool": "shizai_rpa_read_flow",
                "arguments": {"flow_ref": flow_ref, "detail": "edit"},
                "reason": "读取当前流程状态，再按首个错误修复。",
            }
        return {
            "kind": value.get("kind", "rpa_flow_validation"),
            "flow_ref": flow_ref,
            "valid": False,
            "errors": deepcopy(value.get("errors", [])),
            "warnings": deepcopy(value.get("warnings", [])),
            "step_validations": validations,
            "next_tool": action["tool"],
            "next_action": action,
        }
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
    if name == "shizai_rpa_sync_diagnose":
        package = value.get("package") if isinstance(value.get("package"), dict) else {}
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        result = {
            "kind": value.get("kind", "native_rpa_runtime_diagnosis"),
            "project_ref": arguments.get("project_ref") or value.get("project"),
            "can_run": bool(value.get("can_run")),
            "can_attempt_run": bool(value.get("can_attempt_run")),
            "materialization_required": bool(value.get("materialization_required")),
            "errors": [str(item) for item in value.get("errors", [])[:8]],
            "warnings": [str(item) for item in value.get("warnings", [])[:8]],
            "package": {
                key: package.get(key)
                for key in ("matched", "application_id", "package_id", "runtime_version", "factory_version")
                if key in package
            },
            "runtime": {
                "ready": runtime.get("ready"),
                "runtime_browser_plugin_status": runtime.get("runtime_browser_plugin_status"),
                "runtime_browser_plugin_ready": runtime.get("runtime_browser_plugin_ready"),
                "message": runtime.get("message"),
            },
        }
        if result["can_run"] or result["materialization_required"]:
            result["next_tool"] = "shizai_rpa_run"
            result["next_action"] = {
                "tool": "shizai_rpa_run",
                "arguments": {"project_ref": result["project_ref"], "allow_write": True},
                "reason": (
                    "run 会先重新物化运行文件并复诊，然后再决定是否提交。"
                    if result["materialization_required"]
                    else "运行诊断已通过，可以提交正式运行。"
                ),
            }
        else:
            result["next_tool"] = None
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
        preflight = source.get("preflight") if isinstance(source.get("preflight"), dict) else {}
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
        if preflight:
            compact["preflight"] = {
                key: deepcopy(preflight[key])
                for key in ("valid", "reason", "materialization_required")
                if key in preflight
            }
            compact["preflight"]["errors"] = [str(item) for item in preflight.get("errors", [])[:8]]
            compact["preflight"]["warnings"] = [str(item) for item in preflight.get("warnings", [])[:8]]
        action = source.get("next_action") if isinstance(source.get("next_action"), dict) else preflight.get("next_action")
        if not isinstance(action, dict) and isinstance(nested_status.get("next_action"), dict):
            action = nested_status["next_action"]
        if isinstance(action, dict):
            compact["next_action"] = deepcopy(action)
        compact["next_tool"] = (
            action.get("tool")
            if isinstance(action, dict) and action.get("tool")
            else "shizai_rpa_status"
            if name == "shizai_rpa_run" and compact.get("terminal_status") not in {"success", "failed", "blocked"}
            else None
        )
        return compact
    return value
