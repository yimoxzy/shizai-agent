from scripts.mcp_stress import business_error, classify_failure, find_feedback


def test_business_error_handles_nested_status_object():
    assert business_error({"status": {"ready": True}}) is None
    assert business_error({"valid": False}) == "business response valid=false"
    assert business_error({"ok": False, "error": "agent_id is required"}) == "agent_id is required"
    assert business_error({"status": "runtime_error"}) == "business response status=runtime_error"


def test_stress_failure_classification_separates_layers():
    assert classify_failure("服务器返回错误 (code=3001)", "shizai_draft") == "infrastructure"
    assert classify_failure("Project not found", "shizai_rpa_add_step") == "input_validation"
    assert classify_failure("element lookup failed", "shizai_rpa_run") == "business_runtime"


def test_find_feedback_reads_nested_mcp_response():
    assert find_feedback({"status": {"ui_feedback": {"phase": "running"}}}) == {"phase": "running"}
