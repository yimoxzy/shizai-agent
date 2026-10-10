"""Plan cleanup from explicit tab ownership, never URL or window guesses."""

from copy import deepcopy
from typing import Any

CLOSE_COMPONENT = "WebBrowser_CloseWeb_V2"
CLOSE_COMPONENTS = {CLOSE_COMPONENT, "WebBrowser_CloseWeb", "WebBrowser_CloseWeb_V1"}


def _literal_true(value: Any) -> bool:
    return value is True or (isinstance(value, (str, int)) and str(value).lower() in {"1", "true"})


def browser_cleanup_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return missing tail steps for handles whose ownership is still known.

    A later assignment (including GetTAB) invalidates ownership of a variable.
    Closing its final value could otherwise close a user-owned tab. Callers
    pass authoritative outputs: normalized plan outputs or native output slots.
    """
    owned: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    closed: list[str] = []
    used_ids = {str(step.get("id") or "") for step in steps}
    used_variables: set[str] = set()
    for step in steps:
        component = step.get("component")
        config = step.get("config") or {}
        outputs = list(step.get("outputs") or [])
        if component in {"WebBrowser_CreateV3", "WebBrowser_GetTAB_V1"}:
            outputs = [str(config.get("var_name") or "网页对象")]
        if component in CLOSE_COMPONENTS:
            web = config.get("web")
            reference = web.get("variable") if isinstance(web, dict) else None
            if config.get("colse_type") == "select" and reference in owned:
                owned.pop(reference)
                closed.append(str(step.get("id")))
        for output in outputs:
            used_variables.add(output)
            if output in owned:
                warnings.append(f"browser handle {output} was reassigned; automatic close is unsafe")
                owned.pop(output)
        if component == "WebBrowser_CreateV3" and _literal_true(config.get("openNewTab")):
            owned[outputs[0]] = step
    additions: list[dict[str, Any]] = []
    previous = str(steps[-1].get("id") or "") if steps else ""

    def unique(base: str, used: set[str]) -> str:
        name, count = base, 2
        while name in used:
            name = f"{base}-{count}" if base.isascii() else f"{base}{count}"
            count += 1
        used.add(name)
        return name

    for variable, opener in owned.items():
        step_id = unique("close-created-browser", used_ids)
        result_variable = unique("浏览器关闭结果", used_variables)
        additions.append({
            "id": step_id, "component": CLOSE_COMPONENT,
            "purpose": "关闭本流程创建的浏览器网页",
            "browser_cleanup_for": opener["id"],
            "depends_on": [previous] if previous else [],
            "inputs": [variable], "outputs": [result_variable],
            "config": {
                "var_name": result_variable, "colse_type": "select",
                "browser_type": (opener.get("config") or {}).get("type", "chrome"),
                "web": {"variable": variable}, "kill": False, "is_close_alert": True,
            },
        })
        previous = step_id
    return {
        "status": "appended" if additions else "needs_attention" if warnings else "already_present" if closed else "skipped",
        "steps": additions, "step_ids": [s["id"] for s in additions] or closed,
        "warnings": warnings, "execution_verified": False,
        "scope": "normal_completion_only",
    }


def normalize_browser_cleanup_plan(plan: dict[str, Any], *, available: bool) -> dict[str, Any]:
    if plan.get("browser_cleanup_policy") == "keep_open":
        return {"status": "skipped", "reason": "explicit_keep_open", "execution_verified": False}
    assessment = browser_cleanup_steps(plan.get("steps") or [])
    additions = assessment.pop("steps")
    if additions and not available:
        assessment.update(status="unavailable", reason="close_component_not_installed")
    elif additions:
        plan["steps"] = [*plan["steps"], *deepcopy(additions)]
        plan.pop("plan_id", None)
    return assessment
