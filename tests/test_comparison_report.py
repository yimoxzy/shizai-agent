from scripts.mcp_vs_tars_stress import SCENARIOS, path_summary, scenario_actions


def test_comparison_matrix_has_at_least_twelve_unique_prompts():
    assert len(SCENARIOS) >= 12
    assert len({item["id"] for item in SCENARIOS}) == len(SCENARIOS)
    assert len({item["prompt"] for item in SCENARIOS}) == len(SCENARIOS)


def test_mcp_and_native_matrix_preserve_expected_negative_case():
    negative = next(item for item in SCENARIOS if item["kind"] == "negative")
    actions = scenario_actions(negative, None, None)
    assert actions[0][0] == "shizai_route_intent"
    assert actions[-1][0] == "shizai_validate_draft"


def test_path_summary_excludes_expected_failures_from_applicable_rate():
    records = [
        {"path": "codex_mcp", "scenario": "ok", "success": True, "total_elapsed_sec": 1, "verifiable_output": True},
        {
            "path": "codex_mcp",
            "scenario": "negative",
            "success": True,
            "result": "expected_rejection",
            "expected_failure": True,
            "total_elapsed_sec": 2,
            "verifiable_output": False,
        },
    ]
    result = path_summary(records, "codex_mcp")
    assert result["raw_success_rate"] == 1
    assert result["applicable_success_rate"] == 1
    assert result["expected_negative_detection_rate"] == 1
