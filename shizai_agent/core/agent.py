"""Agent Brain discovery, RPC access, and MCP-facing capability metadata."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent
from .paths import ShizaiPaths

FALLBACK_COMPONENT_TYPES = (
    "start",
    "llm",
    "answer",
    "knowledge-retrieval",
    "code",
    "database",
    "http-request",
    "tool",
    "if-else",
    "iteration",
    "question-classifier",
    "template-transform",
)


# This is the observed surface of the installed Agent Brain bundle. The bundle
# scan below remains authoritative for version-specific additions.
SERVICE_METHODS: dict[str, list[str]] = {
    "AgentBrainAppService": [
        "getAppDetail",
        "getAppList",
        "getBotList",
        "getModelProviders",
        "getModelList",
        "getDefaultModel",
        "getModelParameterRules",
        "getMemoryAndStatus",
        "getAgentLogDetail",
        "getRunDetail",
        "getTracingList",
        "getConversationMessages",
        "getSuggestedQuestions",
        "getWorkflowDraft",
        "saveWorkflowDraft",
        "getAdvancedChatWorkflowRuns",
        "getWorkflowsPublish",
        "getNodesDefaultConfigs",
        "runAdvancedChatWorkflowDraft",
        "runSingleNodeInAdvancedChat",
        "runSingleNodeIterationInAdvancedChat",
        "stopWorkflowRun",
        "stopChatMessageResponding",
        "publishWorkflow",
        "updateAppInfo",
        "uploadImage",
        "getApiBasedExtensionList",
        "getCodeBasedExtensionList",
        "getWorkflowTools",
        "getBuildInTools",
        "getCustomTools",
        "getToolInfoById",
        "getMcpServerList",
        "getDatasets",
        "getDatasetsByIds",
        "getRpaFlowCollectionList",
        "getBuiltinFiles",
        "getSupportRetrievalMethods",
        "getCorpBrainVideoUrls",
        "getHandoverModel",
        "getDeopMenu",
        "getDingTables",
        "getDingTableFields",
        "getFeishuTables",
        "getFeishuTableFields",
        "getNodeDefault",
        "handleTestConnection",
        "parameterMapping",
        "getUserDepList",
        "generateRule",
        "generateRuleCode",
        "getBuiltInToolCredentialSchema",
        "getBuiltInToolCredential",
        "updateBuiltInToolCredential",
        "submitPaper",
        "submitIIForm",
    ],
    "AgentBrainClientService": ["refreshAgentNavigation"],
    # This is the designer's RPA assistant channel.  It is separate from the
    # native .task debug channel exposed by CorpBrainService.
    "AppGenService": [
        "initCompToZBrain",
        "sendProcessPlan",
        "getSteps",
        "getMemoryAndStatus",
        "getExecuteParams",
        "runStep",
        "sendAIRepairDesc",
    ],
    "AgentOpService": [],
    "AgentExportService": ["isAgentExported", "requestExportAgent", "exportAgent"],
    "ApplicationService": [
        "getApplicationByApplicationId",
        "getMyApplicationList",
        "getApplicationConflictInfo",
        "ensureApplicationPackageComplete",
        "importApplicationNormal",
        "createApplication",
    ],
    "ApplicationOpService": ["createApplicationByDefault", "editApplicationInAgent"],
    "ApplicationPublishService": ["publishToAgentBrainBatch"],
    "BrainHttpClientService": [],
    "ChatRPAService": ["uploadFileV2", "copyImageToClipboardByUrl", "downloadFileToDir"],
    "ConfigAppService": ["getConfig", "getServerInfoRequest"],
    "CorpBrainService": [
        "workflowDebugRunRPA",
        "workflowDebugStop",
        "workflowLocalRunMcp",
        "workflowLocalStopMcp",
        "workflowDownloadFile",
    ],
    "AppRunService": ["run", "stop", "suspend", "resume", "reRun"],
    "PackageScriptService": ["createScriptForComponents", "createScriptForArrangement", "createScriptForFlow"],
    "DebugAdapterService": ["connect", "send"],
    "DebugControllerService": ["init", "stop", "continue", "next", "stepIn", "stepOut"],
    "FileDialogService": ["showOpenDialog", "showSaveDialog"],
    "LoginService": ["changeToken", "getCurrentUserAccount"],
    "NativeOperateService": ["selectFile"],
    "NewStorageService": [
        "getOriginConfig",
        "getUserItem",
        "getValue",
        "setValue",
        "setUserItem",
        "updateAppConfig",
    ],
    "PackageContentService": [
        "checkCodeLimit",
        "getGlobalVariable",
        "getPackageFlow",
        "getEditState",
        "getVariableInfo",
        "savePackageCode",
        "savePackageFlow",
        "saveEditState",
        "saveVariableInfo",
    ],
    "WorkspaceService": ["pageActive", "pageClose"],
    "WindowManagerService": ["openWindow", "moveMainTop", "getWindowInfo"],
}

ROUTES = [
    "/agent/process_plan",
    "/agent/run_step",
    "/agent/get_steps",
    "/agent/get_execute_params",
    "/agent/get_memory_and_status",
    "/agent/bot/answer",
    "/agent/bot/query",
    "/agent/history/session/create",
    "/agent/history/session/update",
    "/workspaces/current/model-providers",
    "/workspaces/current/models/model-types/{type}",
]

# Unknown methods are treated as writes and require --allow-write. This keeps
# the generic surface forward-compatible without making a new bundle method
# silently mutate the desktop app.
WRITE_METHODS = {
    "initCompToZBrain",
    "sendProcessPlan",
    "getSteps",
    "getMemoryAndStatus",
    "getExecuteParams",
    "runStep",
    "sendAIRepairDesc",
    "saveWorkflowDraft",
    "runAdvancedChatWorkflowDraft",
    "runSingleNodeInAdvancedChat",
    "runSingleNodeIterationInAdvancedChat",
    "stopWorkflowRun",
    "stopChatMessageResponding",
    "publishWorkflow",
    "updateAppInfo",
    "uploadImage",
    "updateBuiltInToolCredential",
    "submitPaper",
    "submitIIForm",
    "parameterMapping",
    "generateRule",
    "generateRuleCode",
    "updateDatasetSetting",
    "setValue",
    "setUserItem",
    "updateAppConfig",
    "publishToAgentBrainBatch",
    "workflowDebugRunRPA",
    "workflowDebugStop",
    "workflowLocalRunMcp",
    "workflowLocalStopMcp",
    "workflowDownloadFile",
    "uploadFileV2",
    "copyImageToClipboardByUrl",
    "downloadFileToDir",
    "run",
    "stop",
    "suspend",
    "resume",
    "reRun",
    "createScriptForComponents",
    "createScriptForArrangement",
    "createScriptForFlow",
    "connect",
    "send",
    "init",
    "continue",
    "next",
    "stepIn",
    "stepOut",
    "selectFile",
    "openWindow",
    "moveMainTop",
    "pageActive",
    "pageClose",
    "changeToken",
    "exportAgent",
    "requestExportAgent",
    "refreshAgentNavigation",
    "ensureApplicationPackageComplete",
    "createApplicationByDefault",
    "editApplicationInAgent",
    "importApplicationNormal",
    "createApplication",
    "savePackageCode",
    "savePackageFlow",
    "saveEditState",
    "saveVariableInfo",
}

ARGUMENT_HINTS: dict[tuple[str, str], str] = {
    ("AgentBrainAppService", "getAppList"): "[{page, limit, mode}]",
    ("AgentBrainAppService", "getBotList"): "[{runSystems: ['windows']}]",
    ("AgentBrainAppService", "getModelList"): "[{type: 'llm', isSupportFuncCall: bool, supportVision: bool}]",
    ("AgentBrainAppService", "getDefaultModel"): "[{type: 'llm'}]",
    ("AgentBrainAppService", "getModelParameterRules"): "[{provider, modelId}]",
    ("AgentBrainAppService", "getMcpServerList"): "[{page, limit, keyword?}]",
    ("AgentBrainAppService", "getDatasets"): "[{page, owner_type, keyword}]",
    ("AgentBrainAppService", "getWorkflowDraft"): "[{appId}]",
    ("AgentBrainAppService", "getAdvancedChatWorkflowRuns"): "[{appId}]",
    ("AgentBrainAppService", "getWorkflowsPublish"): "[{appId}]",
    ("AgentBrainAppService", "getNodesDefaultConfigs"): "[{appId}]",
    ("AgentBrainAppService", "getAppDetail"): "[{appId}]",
    ("AgentBrainAppService", "getAgentLogDetail"): "[{appId, params: {conversation_id, message_id}}]",
    ("AgentBrainAppService", "getRunDetail"): "[{appId, runID}]",
    ("AgentBrainAppService", "getTracingList"): "[{appId, runID}]",
    ("AgentBrainAppService", "getConversationMessages"): "[{appId, conversationId}]",
    ("AgentBrainAppService", "getSuggestedQuestions"): "[{appId, messageId}]",
    ("AppGenService", "initCompToZBrain"): "[{moduleList: [...]}]",
    ("AppGenService", "sendProcessPlan"): "[{plan, ...}, application?]",
    ("AppGenService", "getSteps"): "[{plan, ...}]",
    ("AppGenService", "getMemoryAndStatus"): "[{plan, ...}, application?]",
    ("AppGenService", "getExecuteParams"): "[{plan, ...}, application?]",
    ("AppGenService", "runStep"): "[{step_id, exec_status}]",
    ("AppGenService", "sendAIRepairDesc"): "[{...}]",
    ("AgentBrainAppService", "getCodeBasedExtensionList"): "[{module}]",
    ("AgentBrainAppService", "getToolInfoById"): "[toolId]",
    ("AgentBrainAppService", "getDatasetsByIds"): "[datasetIds]",
    (
        "ApplicationService",
        "importApplicationNormal",
    ): "[{isZipFile, applicationPath, importMode, isOldTaskCompGroup, applicationType, originApplicationId}]",
    ("ApplicationService", "createApplication"): "[{isExample, templateName, templatePath, applicationType}]",
    ("PackageContentService", "savePackageCode"): "[{pkgId, code}]",
    ("PackageContentService", "savePackageFlow"): "[{pkgId, packageFlow: {mainFlow}, isDraft, clientId}]",
    ("PackageContentService", "saveEditState"): "[{pkgId, editState}]",
    ("PackageContentService", "saveVariableInfo"): "[{pkgId, variableInfo}]",
    (
        "PackageScriptService",
        "createScriptForComponents",
    ): "[arrangementId, componentIds, {runStartNo?, useCache?, tarsExtraVar?}]",
    ("PackageScriptService", "createScriptForArrangement"): "[arrangementId, options?]",
    ("PackageScriptService", "createScriptForFlow"): "[options?]",
    ("AppRunService", "run"): "[{code, root, isDebug, packageName, ...}]",
    ("AppRunService", "stop"): "[options?]",
    ("DebugAdapterService", "connect"): "[port]",
    ("DebugAdapterService", "send"): "[command, arguments]",
}

_SENSITIVE_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|authorization|credential|signature|private[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_URL_RE = re.compile(
    r"([?&](?:token|secret|password|api[_-]?key|access[_-]?key|signature|key)=)[^&#]+",
    re.IGNORECASE,
)
_RAW_SECRET_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~-]{12,})\b")


def _bundle_path(paths: ShizaiPaths) -> Path | None:
    """Return the installed Agent Brain bundle, if present."""
    if not paths.install_root:
        return None
    path = paths.install_root / "resources" / "app" / "resources" / "fe-assets" / "agent-brain"
    return path if path.is_dir() else None


def is_read_only(service: str, method: str) -> bool:
    """Return whether a method is safe for the default generic RPC path."""
    if method in WRITE_METHODS:
        return False
    if method in SERVICE_METHODS.get(service, []):
        return True
    return False


def _discover_strings(bundle: Path | None) -> dict[str, Any]:
    """Scan the frontend bundle for service calls and HTTP routes."""
    if bundle is None:
        return {"files_scanned": 0, "services": [], "methods": [], "routes": [], "calls": []}
    services: set[str] = set()
    methods: set[str] = set()
    routes: set[str] = set()
    calls: set[tuple[str, str]] = set()
    files_scanned = 0
    call_re = re.compile(r"callBizNodeService(?:ByWs)?\(\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']")
    route_re = re.compile(r"[\"'](/(?:agent|workspaces/current)[^\"']+)[\"']")
    for path in bundle.rglob("*.js"):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for service, method in call_re.findall(text):
            services.add(service)
            methods.add(method)
            calls.add((service, method))
        routes.update(route_re.findall(text))
    return {
        "files_scanned": files_scanned,
        "services": sorted(services),
        "methods": sorted(methods),
        "routes": sorted(routes),
        "calls": [
            {"service": service, "method": method, "read_only": is_read_only(service, method)}
            for service, method in sorted(calls)
        ],
    }


def capability_specs(paths: ShizaiPaths | None = None) -> list[dict[str, Any]]:
    """Return MCP-ready method metadata, including bundle-discovered methods."""
    specs: dict[tuple[str, str], dict[str, Any]] = {}
    for service, methods in SERVICE_METHODS.items():
        for method in methods:
            specs[(service, method)] = {
                "service": service,
                "method": method,
                "read_only": is_read_only(service, method),
                "requires_allow_write": not is_read_only(service, method),
                "args_hint": ARGUMENT_HINTS.get((service, method), "JSON array; see the installed bundle call site"),
            }
    if paths is not None:
        for call in _discover_strings(_bundle_path(paths)).get("calls", []):
            key = (call["service"], call["method"])
            specs.setdefault(
                key,
                {
                    **call,
                    "requires_allow_write": not call["read_only"],
                    "args_hint": ARGUMENT_HINTS.get(key, "JSON array; see the installed bundle call site"),
                },
            )
    return sorted(specs.values(), key=lambda item: (item["service"], item["method"]))


def _redact_string(value: str) -> str:
    value = _SENSITIVE_URL_RE.sub(r"\1[REDACTED]", value)
    return _RAW_SECRET_RE.sub("[REDACTED]", value)


def redact_payload(value: Any, *, key: str = "") -> Any:
    """Recursively remove credentials from RPC results before displaying them."""
    if _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact_payload(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def unwrap_rpc_result(value: Any) -> Any:
    """Unwrap the standard desktop ``isSuccess/data`` envelope."""
    if isinstance(value, dict) and "isSuccess" in value and "data" in value:
        if value.get("isSuccess") is False:
            raise ShizaiCloudError(str(value.get("message") or "Desktop RPC failed"))
        return value.get("data")
    return value


def rpc(
    service: str,
    method: str,
    args: list[Any] | None = None,
    *,
    allow_write: bool = False,
    client: Any | None = None,
) -> Any:
    """Call any local business RPC method with an explicit write gate."""
    if not service.strip() or not method.strip():
        raise ValueError("service and method are required")
    args = [] if args is None else args
    if not isinstance(args, list):
        raise ValueError("args must be a JSON array")
    if not allow_write and not is_read_only(service, method):
        raise PermissionError(f"{service}.{method} is not in the read-only allowlist; pass --allow-write explicitly")
    caller = client or ShizaiDesktopAgent()
    return redact_payload(caller.call(service, method, args))


def run_rpa_assistant_step(
    step_id: str,
    *,
    exec_status: bool = True,
    client: Any | None = None,
) -> dict[str, Any]:
    """Execute one step in the designer's RPA assistant session.

    ``AppGenService.runStep`` is stateful inside the designer's flow-assistant
    session.  It is not a generic runner for a client-owned native ``.task``;
    native projects use the dedicated ``shizai_rpa_debug_*`` tools for true
    designer/DAP single-step execution, while this method remains the
    designer's stateful RPA assistant channel. Keeping this distinction in the
    return value prevents an MCP client from confusing the two channels.
    """
    normalized_step_id = str(step_id or "").strip()
    if not normalized_step_id:
        raise ValueError("step_id is required")
    request = {"step_id": normalized_step_id, "exec_status": bool(exec_status)}
    raw = rpc(
        "AppGenService",
        "runStep",
        [request],
        allow_write=True,
        client=client,
    )
    result = unwrap_rpc_result(raw)
    payload = result if isinstance(result, dict) else {"value": result}
    return {
        "kind": "rpa_assistant_step_run",
        "execution_channel": "AppGenService.runStep",
        "step_id": normalized_step_id,
        "exec_status": bool(exec_status),
        "result": payload,
        "next_step_id": payload.get("next_id"),
        "finished": bool(payload.get("finished", False)),
        "step_info": payload.get("step_info"),
        "runtime_check": {
            "performed": True,
            "mode": "rpa_assistant_session",
            "native_task_supported": False,
        },
        "session_reused": True,
        "message": (
            "已通过设计器流程助手执行一个步骤；继续执行时传入返回的 next_step_id。"
            "该通道复用流程助手当前会话；Native .task 的从第 N 步执行请使用 shizai_rpa_debug_start。"
        ),
    }


def read_rpc(service: str, method: str, args: list[Any] | None = None, *, client: Any | None = None) -> Any:
    """Call a known read-only method and return its data payload."""
    return unwrap_rpc_result(rpc(service, method, args, client=client))


def _package_id(value: str) -> str:
    """Validate a designer package id before sending package content to RPC."""
    package_id = str(value or "").strip()
    if not package_id:
        raise ValueError("pkg_id is required; resolve it from application_package.packageId")
    return package_id


def _client_id(client: Any | None) -> str:
    """Read a stable client id from a desktop client without requiring a concrete class."""
    config = getattr(client, "config", None)
    value = getattr(config, "client_id", None)
    return str(value or "shizai-agent")


def save_package_code(
    pkg_id: str,
    code: str,
    *,
    client: Any | None = None,
) -> Any:
    """Save plain generated RPA code through the designer encryption boundary."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    return rpc(
        "PackageContentService",
        "savePackageCode",
        [{"pkgId": _package_id(pkg_id), "code": code}],
        allow_write=True,
        client=client,
    )


def save_package_flow(
    pkg_id: str,
    main_flow: dict[str, Any],
    *,
    is_draft: bool = False,
    client: Any | None = None,
) -> Any:
    """Persist the designer main-flow graph and keep its draft flag explicit."""
    if not isinstance(main_flow, dict):
        raise ValueError("main_flow must be a JSON object")
    return rpc(
        "PackageContentService",
        "savePackageFlow",
        [
            {
                "pkgId": _package_id(pkg_id),
                "packageFlow": {"mainFlow": main_flow},
                "isDraft": bool(is_draft),
                "clientId": _client_id(client),
            }
        ],
        allow_write=True,
        client=client,
    )


def save_edit_state(
    pkg_id: str,
    edit_state: dict[str, Any],
    *,
    client: Any | None = None,
) -> Any:
    """Persist the open arrangement/editor state for a native project."""
    if not isinstance(edit_state, dict):
        raise ValueError("edit_state must be a JSON object")
    return rpc(
        "PackageContentService",
        "saveEditState",
        [{"pkgId": _package_id(pkg_id), "editState": edit_state}],
        allow_write=True,
        client=client,
    )


def save_variable_info(
    pkg_id: str,
    variable_info: dict[str, Any],
    *,
    client: Any | None = None,
) -> Any:
    """Persist the designer variable-reference index for generated code."""
    if not isinstance(variable_info, dict):
        raise ValueError("variable_info must be a JSON object")
    return rpc(
        "PackageContentService",
        "saveVariableInfo",
        [{"pkgId": _package_id(pkg_id), "variableInfo": variable_info}],
        allow_write=True,
        client=client,
    )


def capabilities(paths: ShizaiPaths) -> dict[str, Any]:
    """Return the static and bundle-discovered Agent Brain capability map."""
    bundle = _bundle_path(paths)
    discovered = _discover_strings(bundle)
    return {
        "mode": "local-rpc",
        "write_operations_enabled": False,
        "write_operations_require_allow_write": True,
        "bundle": str(bundle) if bundle else None,
        "services": SERVICE_METHODS,
        "routes": ROUTES,
        "method_specs": capability_specs(paths),
        "discovered": discovered,
    }


def authentication_status(*, client: Any | None = None) -> dict[str, Any]:
    """Return secret-free local desktop and cloud authentication readiness."""
    caller = client or ShizaiDesktopAgent()
    if not hasattr(caller, "authentication_status"):
        raise TypeError("client does not support authentication status checks")
    return redact_payload(caller.authentication_status())


def list_agents(paths: ShizaiPaths) -> dict[str, Any]:
    """Describe available local Agent Brain surfaces."""
    bundle = _bundle_path(paths)
    return {
        "count": 1 if bundle else 0,
        "agents": [
            {
                "id": "agent-brain",
                "name": "Agent Brain",
                "kind": "desktop-module",
                "available": bool(bundle),
                "path": str(bundle) if bundle else None,
                "operations": ["inspect", "rpc", "apps", "models", "tools", "workflow", "runs", "logs", "mcp"],
            }
        ]
        if bundle
        else [],
    }


def inspect_agent(paths: ShizaiPaths, agent_id: str) -> dict[str, Any]:
    """Inspect a known local agent module."""
    if agent_id != "agent-brain":
        raise KeyError(f"Unknown agent: {agent_id}")
    result = list_agents(paths)
    if not result["agents"]:
        raise FileNotFoundError("Agent Brain bundle was not found")
    return {**result["agents"][0], "capabilities": capabilities(paths)}


def _best_effort_read(client: Any, service: str, method: str, args: list[Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": read_rpc(service, method, args, client=client)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def query_apps(*, page: int = 1, limit: int = 20, client: Any | None = None) -> Any:
    """List Agent apps visible to the logged-in desktop workspace."""
    payload = {
        "page": page,
        "limit": limit,
        "mode": "advanced-chat",
    }
    return read_rpc("AgentBrainAppService", "getAppList", [payload], client=client)


def create_agent(
    name: str,
    *,
    agent_type: str = "Agent",
    description: str = "",
    client: Any | None = None,
) -> dict[str, Any]:
    """Create a blank Agent through the designer's application operation service."""
    if not name.strip():
        raise ValueError("name is required")
    if agent_type not in {"Agent", "Chatbot", "Workflow", "Completion"}:
        raise ValueError("agent_type must be Agent, Chatbot, Workflow, or Completion")
    caller = client or ShizaiDesktopAgent()
    result = caller.call(
        "ApplicationOpService",
        "createApplicationByDefault",
        [agent_type, {"examId": "", "name": name.strip()}],
    )
    payload = unwrap_rpc_result(result)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise ShizaiCloudError("Designer did not return the created Agent metadata")
    created = {
        "created": True,
        "agent_type": agent_type,
        "name": name.strip(),
        "description": description,
        "application_id": payload.get("applicationId") or payload.get("appId"),
        "application_package_id": payload.get("applicationPackageId"),
        "raw": redact_payload(payload),
    }
    if description and created["application_id"]:
        created["metadata_update"] = update_agent(
            str(created["application_id"]),
            description=description,
            client=caller,
        )
    return created


def create_native_application(
    paths: ShizaiPaths,
    name: str,
    *,
    template_name: str | None = None,
    template_path: str | None = None,
    application_type: str = "normal",
    client: Any | None = None,
) -> dict[str, Any]:
    """Create a real native RPA application through the running designer.

    The designer creates the application directory and the initial empty
    ``io5cd.task`` itself. Callers therefore do not need an external staging
    directory before adding native components.
    """
    if not name.strip():
        raise ValueError("name is required")
    if application_type not in {"normal", "agent"}:
        raise ValueError("application_type must be normal or agent")
    caller = client or ShizaiDesktopAgent()
    request = {
        "isExample": False,
        "templateName": (template_name or name).strip(),
        "templatePath": template_path,
        "applicationType": application_type,
    }
    result = rpc(
        "ApplicationService",
        "createApplication",
        [request],
        allow_write=True,
        client=caller,
    )
    payload = unwrap_rpc_result(result)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise ShizaiCloudError("Designer did not return the created native application metadata")
    application_id = payload.get("applicationId") or payload.get("application_id")
    package_id = payload.get("applicationPackageId") or payload.get("application_package_id")
    raw_path = payload.get("localPath") or payload.get("local_path")
    local_path = Path(str(raw_path)).expanduser() if raw_path else None
    if local_path is None and application_id:
        candidate = paths.applications_root / str(application_id)
        if candidate.is_dir():
            local_path = candidate
    if local_path is None or not local_path.is_dir():
        raise ShizaiCloudError(
            "Designer created the application but did not return an accessible localPath; "
            "refresh the designer and inspect its application list before continuing"
        )
    return {
        "created": True,
        "kind": "native_shizai_application",
        "name": name.strip(),
        "application_type": application_type,
        "application_id": application_id,
        "application_package_id": package_id,
        "local_path": str(local_path.resolve()),
        "factory_version": payload.get("factoryVersion"),
        "runtime_version": payload.get("runtimeVersion"),
        "version": payload.get("version", "0.0.1"),
        "rpc_result": redact_payload(payload),
        "designer_owned": True,
        "external_staging_required": False,
    }


def update_agent(
    agent_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    client: Any | None = None,
) -> Any:
    """Update editable Agent metadata through the live designer RPC."""
    if not agent_id.strip():
        raise ValueError("agent_id is required")
    data: dict[str, Any] = {}
    if name is not None:
        if not name.strip():
            raise ValueError("name must not be empty")
        data["name"] = name.strip()
    if description is not None:
        data["description"] = description
    if not data:
        raise ValueError("name or description is required")
    caller = client or ShizaiDesktopAgent()
    result = unwrap_rpc_result(
        caller.call(
            "AgentBrainAppService",
            "updateAppInfo",
            [{"appId": agent_id, "data": data}],
        )
    )
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        result = result["data"]
    return redact_payload(result)


def query_models(
    *,
    model_type: str = "llm",
    support_func_call: bool = False,
    support_vision: bool = False,
    client: Any | None = None,
) -> Any:
    """List models for a model type."""
    return read_rpc(
        "AgentBrainAppService",
        "getModelList",
        [{"type": model_type, "isSupportFuncCall": support_func_call, "supportVision": support_vision}],
        client=client,
    )


def query_tools(*, client: Any | None = None) -> dict[str, Any]:
    """Return built-in, custom, workflow, API-extension, and code-extension tools."""
    caller = client or ShizaiDesktopAgent()
    return {
        "builtin": _best_effort_read(caller, "AgentBrainAppService", "getBuildInTools", []),
        "custom": _best_effort_read(caller, "AgentBrainAppService", "getCustomTools", []),
        "workflow": _best_effort_read(caller, "AgentBrainAppService", "getWorkflowTools", []),
        "api_extensions": _best_effort_read(caller, "AgentBrainAppService", "getApiBasedExtensionList", []),
        "code_extensions": _best_effort_read(
            caller, "AgentBrainAppService", "getCodeBasedExtensionList", [{"module": "agent"}]
        ),
    }


def query_draft(*, agent_id: str | None = None, client: Any | None = None) -> Any:
    """Read the advanced-chat workflow draft."""
    caller = client or ShizaiDesktopAgent()
    resolved = agent_id or caller.config.agent_id
    if not str(resolved).strip():
        return {
            "ok": False,
            "failure_kind": "input_validation",
            "error": "agent_id is required when no Agent is configured",
            "next_tool": "shizai_apps",
            "message": "请先调用 shizai_apps 选择目标 Agent，再用 agent_id 读取草稿。",
        }
    if hasattr(caller, "get_workflow_draft"):
        return redact_payload(caller.get_workflow_draft(resolved))
    return read_rpc("AgentBrainAppService", "getWorkflowDraft", [{"appId": resolved}], client=caller)


def save_draft(draft: dict[str, Any], *, agent_id: str | None = None, client: Any | None = None) -> Any:
    """Save one advanced-chat workflow draft through the explicit write path."""
    if not isinstance(draft, dict):
        raise ValueError("draft must be a JSON object")
    caller = client or ShizaiDesktopAgent()
    resolved = agent_id or caller.config.agent_id
    return rpc(
        "AgentBrainAppService",
        "saveWorkflowDraft",
        [{"appId": resolved, "draft": draft}],
        allow_write=True,
        client=caller,
    )


def publish(*, agent_id: str | None = None, publish_range: str = "only_me", client: Any | None = None) -> Any:
    """Publish the current draft through the explicit workflow publish path."""
    if publish_range not in {"only_me", "all_team_members"}:
        raise ValueError("publish_range must be only_me or all_team_members")
    caller = client or ShizaiDesktopAgent()
    resolved = agent_id or caller.config.agent_id
    if hasattr(caller, "publish"):
        return redact_payload(
            caller.publish(
                agent_id=resolved,
                publish_info={"publish_range": publish_range},
            )
        )
    return rpc(
        "AgentBrainAppService",
        "publishWorkflow",
        [{"appId": resolved, "appPublishInfo": {"publish_range": publish_range}}],
        allow_write=True,
        client=caller,
    )


def run_draft(
    query: str,
    *,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    timeout: float | None = None,
    client: Any | None = None,
) -> Any:
    """Run the current advanced-chat draft and collect its streamed result."""
    if not query.strip():
        raise ValueError("query is required")
    caller = client or ShizaiDesktopAgent()
    if not hasattr(caller, "chat"):
        raise TypeError("client does not support streamed Agent runs")
    return redact_payload(
        caller.chat(
            query,
            agent_id=agent_id,
            conversation_id=conversation_id,
            timeout=timeout,
        )
    )


def query_agent_resource(method: str, args: list[Any], *, client: Any | None = None) -> Any:
    """Read one Agent workflow, log, trace, message, or suggestion resource."""
    return read_rpc("AgentBrainAppService", method, args, client=client)


def query_mcp_servers(*, page: int = 1, limit: int = 100, keyword: str = "", client: Any | None = None) -> Any:
    """List configured MCP servers from the current Agent workspace."""
    return read_rpc(
        "AgentBrainAppService",
        "getMcpServerList",
        [{"page": page, "limit": limit, "keyword": keyword}],
        client=client,
    )


def query_datasets(*, page: int = 1, limit: int = 20, keyword: str = "", client: Any | None = None) -> Any:
    """List knowledge bases visible to the current Agent workspace."""
    return read_rpc(
        "AgentBrainAppService",
        "getDatasets",
        [{"page": page, "owner_type": "0", "keyword": keyword, "limit": limit}],
        client=client,
    )


def _find_application_in_listing(
    value: Any, *, project_id: str, project_name: str, project_path: str
) -> dict[str, Any] | None:
    """Find an imported application in the designer's flexible list envelope."""
    if isinstance(value, list):
        for item in value:
            found = _find_application_in_listing(
                item, project_id=project_id, project_name=project_name, project_path=project_path
            )
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    normalized_path = project_path.casefold()
    candidates = {
        str(value.get(key) or "").casefold()
        for key in (
            "applicationId",
            "application_id",
            "id",
            "originApplicationId",
            "applicationPath",
            "path",
            "packagePath",
        )
    }
    names = {str(value.get(key) or "").casefold() for key in ("applicationName", "packageName", "name", "projectName")}
    if project_id.casefold() in candidates or normalized_path in candidates or project_name.casefold() in names:
        return redact_payload(value)
    for item in value.values():
        found = _find_application_in_listing(
            item, project_id=project_id, project_name=project_name, project_path=project_path
        )
        if found is not None:
            return found
    return None


def import_native_project(
    project_path: str,
    *,
    import_mode: str = "create-new",
    application_type: str = "normal",
    verify: bool = True,
    client: Any | None = None,
) -> dict[str, Any]:
    """Register a generated native project in the running Shizai designer.

    The designer owns application-list persistence. This method deliberately
    calls its import service instead of writing the local SQLite database.
    """
    root = Path(project_path).expanduser().resolve()
    metadata_path = root / "szrpa.json"
    if not root.is_dir():
        raise FileNotFoundError(f"native project directory not found: {root}")
    if not metadata_path.is_file():
        raise FileNotFoundError(f"native project metadata not found: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid native project metadata: {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"native project metadata must be an object: {metadata_path}")
    if import_mode not in {"create-new", "overwrite"}:
        raise ValueError("import_mode must be create-new or overwrite")
    if application_type not in {"normal", "agent"}:
        raise ValueError("application_type must be normal or agent")
    project_id = str(metadata.get("id") or root.name)
    project_name = str(metadata.get("packageName") or root.name)
    caller = client or ShizaiDesktopAgent()
    payload = {
        "isZipFile": False,
        "applicationPath": str(root),
        "importMode": import_mode,
        "isOldTaskCompGroup": False,
        "applicationType": application_type,
        "originApplicationId": project_id,
    }
    rpc_result = rpc(
        "ApplicationService",
        "importApplicationNormal",
        [payload],
        allow_write=True,
        client=caller,
    )
    result: dict[str, Any] = {
        "registered": True,
        "project": str(root),
        "project_id": project_id,
        "project_name": project_name,
        "import_mode": import_mode,
        "application_type": application_type,
        "rpc_result": rpc_result,
        "verification": {"checked": False},
        "next_action": {
            "tool": "shizai_project_list",
            "reason": "设计器已收到导入请求；重新读取应用列表确认可见。",
        },
    }
    if verify:
        try:
            listing = read_rpc("ApplicationService", "getMyApplicationList", [], client=caller)
        except (ShizaiCloudError, OSError, ValueError) as exc:
            result["verification"] = {"checked": False, "error": str(exc)}
        else:
            matched = _find_application_in_listing(
                listing, project_id=project_id, project_name=project_name, project_path=str(root)
            )
            result["verification"] = {
                "checked": True,
                "visible": matched is not None,
                "application": matched,
            }
            result["next_action"] = {
                "tool": "shizai_project_list",
                "reason": "已重新读取设计器应用列表；visible=true 表示流程已出现在列表中。",
            }
    return result


def _collect_node_types(value: Any, result: set[str] | None = None) -> set[str]:
    """Find workflow node types in nested default-config and draft payloads."""
    if result is None:
        result = set()
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            result.add(data["type"])
        if "config" in value and isinstance(value.get("type"), str):
            result.add(value["type"])
        for key in ("node_type", "block_type", "blockType"):
            if isinstance(value.get(key), str):
                result.add(value[key])
        for child in value.values():
            _collect_node_types(child, result)
    elif isinstance(value, list):
        for child in value:
            _collect_node_types(child, result)
    return result


def _summarize_tool_providers(value: Any) -> list[dict[str, Any]]:
    """Return a compact tool-provider catalog without dropping tool parameters."""
    payload = unwrap_rpc_result(value)
    if not isinstance(payload, list):
        return []
    providers: list[dict[str, Any]] = []
    for provider in payload:
        if not isinstance(provider, dict):
            continue
        tools = provider.get("tools")
        provider_summary = {
            "id": provider.get("id") or provider.get("name"),
            "name": provider.get("name"),
            "label": provider.get("label"),
            "type": provider.get("type"),
            "tools": [],
        }
        if isinstance(tools, list):
            provider_summary["tools"] = [
                {
                    "name": tool.get("name"),
                    "label": tool.get("label"),
                    "description": tool.get("description"),
                    "parameters": tool.get("parameters", []),
                    "output_parameters": tool.get("output_parameters", []),
                }
                for tool in tools
                if isinstance(tool, dict)
            ]
        providers.append(provider_summary)
    return providers


def _component_contracts(value: Any) -> list[dict[str, Any]]:
    """Normalize the installed node defaults into one contract per node type."""
    if isinstance(value, dict) and value.get("ok") is True and "data" in value:
        value = value["data"]
    payload = unwrap_rpc_result(value)
    if not isinstance(payload, list):
        return []
    contracts: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        node_type = item.get("type") or item.get("node_type") or item.get("blockType")
        if not isinstance(node_type, str) or not node_type:
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        contract = contracts.setdefault(
            node_type,
            {
                "type": node_type,
                "config_keys": set(),
                "output_names": set(),
                "retry_config_keys": set(),
                "available_dependencies": set(),
            },
        )
        contract["config_keys"].update(str(key) for key in config)
        contract["output_names"].update(_output_names(item))
        retry_config = item.get("retry_config")
        if isinstance(retry_config, dict):
            contract["retry_config_keys"].update(str(key) for key in retry_config)
        dependencies = item.get("available_dependencies")
        if isinstance(dependencies, list):
            contract["available_dependencies"].update(str(value) for value in dependencies)
    result = []
    for node_type in sorted(contracts):
        contract = contracts[node_type]
        result.append(
            {
                "type": node_type,
                "config_keys": sorted(contract["config_keys"]),
                "output_names": sorted(contract["output_names"]),
                "retry_config_keys": sorted(contract["retry_config_keys"]),
                "available_dependencies": sorted(contract["available_dependencies"]),
            }
        )
    return result


def _tool_catalog(providers: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Flatten provider metadata into the records an LLM needs for tool nodes."""
    result: list[dict[str, Any]] = []
    for provider_type, provider_rows in providers.items():
        for provider in provider_rows:
            for tool in provider.get("tools", []):
                result.append(
                    {
                        "provider_id": provider.get("id"),
                        "provider_name": provider.get("name"),
                        "provider_type": provider.get("type") or provider_type,
                        "name": tool.get("name"),
                        "label": tool.get("label"),
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters", []),
                        "output_parameters": tool.get("output_parameters", []),
                    }
                )
    return result


def _draft_summary(draft: Any) -> dict[str, Any]:
    """Return a compact graph snapshot with observed selectors and dependencies."""
    graph = _draft_graph(draft)
    if graph is None:
        return {"available": False, "nodes": [], "edges": [], "selectors": []}
    nodes = graph.get("nodes", [])
    node_ids = {node_id for node_id, _ in (_node_id_and_type(node) for node in nodes) if node_id}
    node_summaries = []
    selectors: list[list[str]] = []
    for node in nodes:
        node_id, node_type = _node_id_and_type(node)
        node_selectors = _selector_candidates(node, node_ids)
        selectors.extend(node_selectors)
        data = node.get("data") if isinstance(node, dict) and isinstance(node.get("data"), dict) else {}
        node_summaries.append(
            {
                "id": node_id,
                "type": node_type,
                "data_keys": sorted(str(key) for key in data),
                "selectors": node_selectors,
            }
        )
    edges = []
    for edge in graph.get("edges", []):
        source, target = _edge_endpoints(edge)
        if source and target:
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "source_handle": edge.get("sourceHandle") if isinstance(edge, dict) else None,
                    "target_handle": edge.get("targetHandle") if isinstance(edge, dict) else None,
                }
            )
    return {
        "available": True,
        "node_count": len(nodes),
        "edge_count": len(graph.get("edges", [])),
        "nodes": node_summaries,
        "edges": edges,
        "selectors": selectors,
    }


def query_components(*, agent_id: str | None = None, client: Any | None = None) -> dict[str, Any]:
    """Build a live component catalog for natural-language workflow generation."""
    caller = client or ShizaiDesktopAgent()
    resolved = agent_id or caller.config.agent_id
    defaults = _best_effort_read(caller, "AgentBrainAppService", "getNodesDefaultConfigs", [{"appId": resolved}])
    draft = _best_effort_read(caller, "AgentBrainAppService", "getWorkflowDraft", [{"appId": resolved}])
    builtin = _best_effort_read(caller, "AgentBrainAppService", "getBuildInTools", [])
    workflow_tools = _best_effort_read(caller, "AgentBrainAppService", "getWorkflowTools", [])
    custom = _best_effort_read(caller, "AgentBrainAppService", "getCustomTools", [])
    node_types = _collect_node_types(defaults.get("data"), _collect_node_types(draft.get("data")))
    live_catalog_available = bool(node_types)
    if not node_types:
        # Some designer builds temporarily reject component reads while the
        # workspace is loading. Keep planning usable, but expose the source so
        # callers know to retry before saving a draft.
        node_types = set(FALLBACK_COMPONENT_TYPES)
    providers = {
        "builtin": _summarize_tool_providers(builtin.get("data")),
        "workflow": _summarize_tool_providers(workflow_tools.get("data")),
        "custom": _summarize_tool_providers(custom.get("data")),
    }
    next_tools = [
        "shizai_components",
        "shizai_draft",
        "shizai_validate_draft",
        "shizai_apply_flow",
        "shizai_run_draft",
        "shizai_runs",
        "shizai_logs",
        "shizai_tracing",
    ]
    if not str(resolved).strip():
        next_tools = ["shizai_apps", "shizai_components", "shizai_validate_draft", "shizai_apply_flow"]
    return {
        "agent_id": resolved,
        "node_types": sorted(node_types),
        "node_types_source": "live" if live_catalog_available else "fallback",
        "live_catalog_available": live_catalog_available,
        "live_catalog_errors": [
            item.get("error") for item in (defaults, draft) if item.get("ok") is False and item.get("error")
        ],
        "nodes_default_configs": defaults,
        "draft_snapshot": draft,
        "tool_providers": providers,
        "normalized_components": _component_contracts(defaults),
        "draft_summary": _draft_summary(draft),
        "tool_catalog": _tool_catalog(providers),
        "next_tools": next_tools,
        "rpc_patterns": [spec for spec in capability_specs() if spec["service"] == "AgentBrainAppService"],
        "generation_rules": {
            "read_before_write": True,
            "write_requires_allow_write": True,
            "mcp_write_tools_require_allow_write": ["shizai_apply_flow", "shizai_run_draft"],
            "draft_write_method": "AgentBrainAppService.saveWorkflowDraft",
            "run_method": "AgentBrainAppService.runAdvancedChatWorkflowDraft",
            "publish_method": "AgentBrainAppService.publishWorkflow",
            "variable_selector_shape": ["node_id", "output_name"],
            "system_selector_examples": [["start_node_id", "sys.query"], ["sys", "query"], ["sys", "files"]],
            "system_variable_names": ["sys.query", "query", "files"],
        },
    }


def generation_context(
    *,
    query: str | None = None,
    agent_id: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return the live designer contract plus a 734-style planning contract.

    The 734 service separates intent planning from execution. This helper makes
    that intermediate contract available to any MCP client without coupling the
    current designer to the 734 runtime WebSocket or its model service.
    """
    catalog = query_components(agent_id=agent_id, client=client)
    tool_catalog = _tool_catalog(catalog["tool_providers"])
    return {
        "query": query,
        "agent_id": catalog["agent_id"],
        "model_policy": {
            "designer": "mcp_client_model",
            "designer_description": "The Codex/Claude/other MCP client model must interpret the request and write the draft JSON.",
            "tasi_generation": False,
            "algorithm_service_generation": False,
            "runtime_model": "The model configured on the saved designer llm node is used only when the workflow is executed.",
        },
        "components": catalog,
        "normalized_components": _component_contracts(catalog["nodes_default_configs"]),
        "draft_summary": _draft_summary(catalog["draft_snapshot"]),
        "tool_catalog": tool_catalog,
        "planning_contract": {
            "inspiration": "734 two-stage planning: split tasks, then reflect on execution results",
            "phases": ["intent", "path", "materialize", "validate", "execute", "reflect"],
            "task_fields": [
                "id",
                "title",
                "category",
                "execution_context",
                "depends_on",
                "input_bindings",
                "expected_outputs",
                "status",
            ],
            "category_values": ["designer_node", "workflow_tool", "model", "output"],
            "status_values": ["planned", "ready", "running", "succeeded", "failed", "blocked"],
            "variable_binding": {
                "selector_shape": ["node_id", "output_name"],
                "system_selector_examples": [["start_node_id", "sys.query"], ["sys", "query"], ["sys", "files"]],
                "system_variable_names": ["sys.query", "query", "files"],
                "rule": "Use an upstream output selector for every downstream input; system selectors must follow the live designer convention.",
            },
            "implementation_policy": {
                "default_strategy": "component_first",
                "rule": "After decomposing a request, map every atomic operation to a live designer node or tool before considering code.",
                "code_exception": "Use a short code node only for one atomic capability absent from the live catalog; never replace a complete workflow with code.",
                "required_plan_fields": [
                    "operation",
                    "implementation_kind",
                    "component_type",
                    "input_bindings",
                    "expected_outputs",
                    "reason",
                ],
            },
            "selection_rules": [
                "Treat nodes_default_configs and tool_providers as authoritative for the installed designer.",
                "Preserve existing draft metadata and unrelated nodes unless the request explicitly replaces them.",
                "Split a sentence at dependency boundaries, not merely at punctuation.",
                "Choose a designer component or workflow tool for each atomic operation before selecting a code node.",
                "A code node must be short, local to one atomic calculation, and justified by missing catalog capability.",
                "Record each dependency and input binding before materializing the graph.",
                "After a failed run, keep the failed step and its result as reflection input for the next repair.",
            ],
            "execution_order": [
                "read components and current draft",
                "write a task plan with dependencies and bindings",
                "materialize nodes and edges",
                "validate the draft",
                "save and re-read the draft",
                "run the smallest useful test",
                "publish only after explicit confirmation",
            ],
        },
        "next_tools": catalog["next_tools"],
    }


def _node_config(catalog: dict[str, Any], node_type: str) -> dict[str, Any]:
    """Use the installed designer's config as the base for generated nodes."""
    payload = unwrap_rpc_result(catalog.get("nodes_default_configs"))
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type") or item.get("node_type") or item.get("blockType")
            if item_type == node_type:
                config = item.get("config")
                if isinstance(config, dict):
                    result = deepcopy(config)
                    result.setdefault("type", node_type)
                    return result
    return {"type": node_type}


def _classify_flow(query: str, available: set[str]) -> tuple[list[str], list[str]]:
    """Map common Chinese requests to a conservative linear node path."""
    lowered = query.lower()
    path = ["start"]
    reasons: list[str] = []
    if any(word in query for word in ("数据库", "sql", "查询", "日志", "表")) and "database" in available:
        path.append("database")
        reasons.append("database")
    elif any(word in lowered for word in ("http", "api", "接口", "请求")) and "http-request" in available:
        path.append("http-request")
        reasons.append("http-request")
    elif any(word in query for word in ("知识库", "文档", "检索", "问答")) and "knowledge-retrieval" in available:
        path.append("knowledge-retrieval")
        reasons.append("knowledge-retrieval")
    if "llm" in available:
        path.append("llm")
        reasons.append("llm")
    if "answer" in available:
        path.append("answer")
        reasons.append("answer")
    return path, reasons


def generate_flow(
    query: str,
    *,
    agent_id: str | None = None,
    save: bool = False,
    run: bool = False,
    publish: bool = False,
    allow_write: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    """Materialize a common one-sentence request into a validated workflow draft."""
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if (save or run or publish) and not allow_write:
        raise PermissionError("save, run, or publish requires allow_write: true")
    caller = client or ShizaiDesktopAgent()
    catalog = query_components(agent_id=agent_id, client=caller)
    available = set(catalog.get("node_types", []))
    path, selected = _classify_flow(query, available)
    unsupported = [node_type for node_type in path if node_type not in available and node_type != "start"]
    if unsupported:
        return {
            "query": query,
            "generated": False,
            "reason": "required components are not present in the live designer catalog",
            "missing_components": unsupported,
            "available_components": sorted(available),
            "next_action": "call shizai_components and choose an installed component or provide a custom draft",
        }
    current = catalog.get("draft_snapshot")
    envelope = deepcopy(current) if isinstance(current, dict) else {}
    graph = {"nodes": [], "edges": []}
    node_ids: list[str] = []
    for index, node_type in enumerate(path):
        node_id = "start" if node_type == "start" else f"{node_type.replace('-', '_')}_{index}"
        data = _node_config(catalog, node_type)
        data["type"] = node_type
        data.setdefault("title", {"start": "开始", "llm": "大模型", "answer": "回答"}.get(node_type, node_type))
        if node_type == "llm":
            data.setdefault("prompt_template", [{"role": "user", "text": "{{#sys.query#}}"}])
        if node_type == "database":
            data.setdefault("query", "-- 请在设计器中补充实际 SQL")
        if node_type == "answer" and node_ids:
            data.setdefault("answer", [node_ids[-1], "result"])
        graph["nodes"].append({"id": node_id, "type": "custom", "position": {"x": index * 260, "y": 120}, "data": data})
        if node_ids:
            graph["edges"].append({"id": f"{node_ids[-1]}-{node_id}", "source": node_ids[-1], "target": node_id})
        node_ids.append(node_id)
    if isinstance(envelope.get("draft"), dict):
        envelope["draft"]["graph"] = graph
    else:
        envelope["graph"] = graph
    validation = validate_draft(envelope, agent_id=agent_id, client=caller)
    result: dict[str, Any] = {
        "query": query,
        "generated": True,
        "plan": [
            {
                "id": f"task-{i + 1}",
                "title": node_type,
                "category": "designer_node",
                "depends_on": [f"task-{i}"] if i else [],
                "status": "planned",
            }
            for i, node_type in enumerate(path)
        ],
        "selected_components": selected,
        "draft": envelope,
        "validation": validation,
        "saved": False,
        "ran": False,
        "published": False,
    }
    if not validation["valid"]:
        result["next_action"] = "inspect validation.errors and use the live component defaults to repair the draft"
        return result
    if save:
        result["save_result"] = save_draft(envelope, agent_id=agent_id, client=caller)
        result["saved"] = True
    if run:
        result["run_result"] = run_draft(query, agent_id=agent_id, client=caller)
        result["ran"] = True
    if publish:
        result["publish_result"] = publish_workflow(agent_id=agent_id, allow_write=True, client=caller)
        result["published"] = True
    return result


def _draft_graph(draft: Any) -> dict[str, Any] | None:
    """Find a graph object in the common draft envelope shapes."""
    if isinstance(draft, dict):
        graph = draft.get("graph")
        if isinstance(graph, dict) and isinstance(graph.get("nodes"), list):
            return graph
        nested = draft.get("draft")
        if isinstance(nested, dict):
            found = _draft_graph(nested)
            if found is not None:
                return found
        if isinstance(draft.get("nodes"), list):
            return draft
        for value in draft.values():
            found = _draft_graph(value)
            if found is not None:
                return found
    elif isinstance(draft, list):
        for value in draft:
            found = _draft_graph(value)
            if found is not None:
                return found
    return None


def _node_id_and_type(node: Any) -> tuple[str | None, str | None]:
    if not isinstance(node, dict):
        return None, None
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    node_id = node.get("id") or node.get("node_id") or data.get("id")
    # React Flow stores the renderer kind (usually ``custom``) at the node
    # level; the designer component type lives in data.type.
    node_type = data.get("type") or node.get("node_type") or node.get("type")
    return (str(node_id) if node_id is not None else None, str(node_type) if node_type is not None else None)


def _edge_endpoints(edge: Any) -> tuple[str | None, str | None]:
    if isinstance(edge, dict):
        source = edge.get("source", edge.get("from"))
        target = edge.get("target", edge.get("to"))
        return (
            str(source) if source is not None else None,
            str(target) if target is not None else None,
        )
    if isinstance(edge, list) and len(edge) >= 2:
        return str(edge[0]), str(edge[1])
    return None, None


def _selector_candidates(value: Any, node_ids: set[str], parent_key: str = "") -> list[list[str]]:
    """Find variable selectors without treating arbitrary arrays as bindings."""
    found: list[list[str]] = []
    if isinstance(value, list):
        if len(value) == 2 and all(isinstance(item, str) for item in value):
            first, second = value
            if first == "sys" or first in node_ids:
                found.append([first, second])
        for item in value:
            found.extend(_selector_candidates(item, node_ids, parent_key))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_selector_candidates(item, node_ids, str(key)))
    return found


def _output_names(value: Any, names: set[str] | None = None) -> set[str]:
    """Collect output names from the varying default-config representations."""
    if names is None:
        names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "outputs",
                "output",
                "output_variables",
                "outputvariables",
                "output_parameters",
                "outputparameters",
            }:
                if isinstance(item, dict):
                    names.update(str(name) for name in item if isinstance(name, str))
                elif isinstance(item, list):
                    for entry in item:
                        if isinstance(entry, dict):
                            for name_key in ("name", "variable", "output_name", "outputName"):
                                if isinstance(entry.get(name_key), str):
                                    names.add(entry[name_key])
            _output_names(item, names)
    elif isinstance(value, list):
        for item in value:
            _output_names(item, names)
    return names


def validate_draft(
    draft: dict[str, Any],
    *,
    agent_id: str | None = None,
    strict: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    """Statically validate a workflow graph before it reaches the designer."""
    if not isinstance(draft, dict):
        raise ValueError("draft must be a JSON object")
    graph = _draft_graph(draft)
    errors: list[str] = []
    warnings: list[str] = []
    if graph is None:
        errors.append("draft graph with a nodes list was not found")
        return {
            "valid": False,
            "strict": strict,
            "errors": errors,
            "warnings": warnings,
            "stats": {"nodes": 0, "edges": 0, "selectors": 0},
        }

    raw_nodes = graph.get("nodes", [])
    raw_edges = graph.get("edges", [])
    node_ids: set[str] = set()
    node_types: list[str] = []
    for index, node in enumerate(raw_nodes):
        node_id, node_type = _node_id_and_type(node)
        if not node_id:
            errors.append(f"node[{index}] is missing id")
            continue
        if node_id in node_ids:
            errors.append(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        if not node_type:
            errors.append(f"node {node_id} is missing type")
        else:
            node_types.append(node_type)

    edge_pairs: list[tuple[str, str]] = []
    for index, edge in enumerate(raw_edges):
        source, target = _edge_endpoints(edge)
        if not source or not target:
            errors.append(f"edge[{index}] is missing source or target")
            continue
        edge_pairs.append((source, target))
        if source not in node_ids:
            errors.append(f"edge[{index}] references missing source node: {source}")
        if target not in node_ids:
            errors.append(f"edge[{index}] references missing target node: {target}")

    try:
        catalog = query_components(agent_id=agent_id, client=client)
    except Exception as exc:
        catalog = {"agent_id": agent_id, "node_types": [], "nodes_default_configs": {}}
        warnings.append(f"live component lookup skipped: {type(exc).__name__}: {exc}")
    known_types = set(catalog.get("node_types", []))
    if known_types:
        for node_type in sorted(set(node_types)):
            if node_type not in known_types:
                warnings.append(f"node type is not in the live catalog: {node_type}")

    selectors: list[list[str]] = []
    for node in raw_nodes:
        selectors.extend(_selector_candidates(node, node_ids))
    known_outputs = _output_names(catalog.get("nodes_default_configs"))
    # The live draft exposes outputs such as an LLM's ``result`` even when the
    # corresponding default config does not list that output explicitly.
    known_outputs.update(
        output
        for source, output in _draft_summary(catalog.get("draft_snapshot")).get("selectors", [])
        if source != "sys" and not output.startswith("sys.")
    )
    for source, output in selectors:
        if source != "sys" and source not in node_ids:
            errors.append(f"selector references missing node: [{source}, {output}]")
        if output == "":
            errors.append(f"selector has an empty output name: [{source}, {output}]")
        elif known_outputs and output not in known_outputs and source != "sys" and not output.startswith("sys."):
            warnings.append(f"selector output is not present in discovered defaults: [{source}, {output}]")

    incoming = {target for _, target in edge_pairs}
    outgoing = {source for source, _ in edge_pairs}
    for node_id in sorted(node_ids):
        if len(node_ids) > 1 and node_id not in incoming and node_id not in outgoing:
            warnings.append(f"isolated node: {node_id}")

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for source, target in edge_pairs:
        if source in adjacency:
            adjacency[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle = False

    def visit(node_id: str) -> None:
        nonlocal cycle
        if node_id in visiting:
            cycle = True
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        for child in adjacency.get(node_id, []):
            visit(child)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_ids:
        visit(node_id)
    if cycle:
        warnings.append("graph contains a cycle; confirm the designer supports this edge pattern")

    digest = hashlib.sha256(
        json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "valid": not errors and (not strict or not warnings),
        "strict": strict,
        "errors": errors,
        "warnings": warnings,
        "agent_id": catalog.get("agent_id", agent_id),
        "draft_sha256": digest,
        "node_types": sorted(set(node_types)),
        "selectors": selectors,
        "stats": {
            "nodes": len(raw_nodes),
            "edges": len(raw_edges),
            "selectors": len(selectors),
            "known_node_types": len(known_types),
            "known_output_names": len(known_outputs),
        },
    }


def apply_flow(
    draft: dict[str, Any],
    *,
    agent_id: str | None = None,
    query: str | None = None,
    strict: bool = False,
    run: bool = False,
    publish: bool = False,
    publish_range: str = "only_me",
    allow_write: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    """Validate, optionally save, run, and publish a model-generated draft.

    The MCP does not contain an LLM. The caller supplies the draft produced from
    ``generation_context``; this function provides the deterministic lifecycle.
    """
    if not allow_write and (run or publish):
        raise PermissionError("run or publish requires allow_write: true")
    validation = validate_draft(draft, agent_id=agent_id, strict=strict, client=client)
    result: dict[str, Any] = {"validation": validation, "saved": False, "ran": False, "published": False}
    if not validation["valid"]:
        return result
    if not allow_write:
        result["next_action"] = "set allow_write: true to save the validated draft"
        return result
    saved = save_draft(draft, agent_id=agent_id, client=client)
    result["saved"] = True
    result["save_result"] = saved
    result["draft_after_save"] = query_draft(agent_id=agent_id, client=client)
    if run:
        if not query or not query.strip():
            raise ValueError("query is required when run is true")
        result["run_result"] = run_draft(query, agent_id=agent_id, client=client)
        result["ran"] = True
    if publish:
        result["publish_result"] = publish_workflow(
            agent_id=agent_id,
            publish_range=publish_range,
            allow_write=True,
            client=client,
        )
        result["published"] = True
    return result


def publish_workflow(
    *,
    agent_id: str | None = None,
    publish_range: str = "only_me",
    allow_write: bool = False,
    client: Any | None = None,
) -> Any:
    """Publish the current draft after an explicit write confirmation."""
    if not allow_write:
        raise PermissionError("publish requires allow_write: true")
    return publish(agent_id=agent_id, publish_range=publish_range, client=client)


def inventory(paths: ShizaiPaths, *, client: Any | None = None) -> dict[str, Any]:
    """Collect a bounded live read-only inventory for CLI/MCP discovery."""
    caller = client or ShizaiDesktopAgent()
    checks: dict[str, tuple[str, str, list[Any]]] = {
        "apps": (
            "AgentBrainAppService",
            "getAppList",
            [{"page": 1, "limit": 20, "mode": "advanced-chat"}],
        ),
        "model_providers": ("AgentBrainAppService", "getModelProviders", []),
        "builtin_tools": ("AgentBrainAppService", "getBuildInTools", []),
        "custom_tools": ("AgentBrainAppService", "getCustomTools", []),
        "workflow_tools": ("AgentBrainAppService", "getWorkflowTools", []),
        "api_extensions": ("AgentBrainAppService", "getApiBasedExtensionList", []),
        "mcp_servers": ("AgentBrainAppService", "getMcpServerList", [{"page": 1, "limit": 100, "keyword": ""}]),
        "datasets": (
            "AgentBrainAppService",
            "getDatasets",
            [{"page": 1, "owner_type": "0", "keyword": "", "limit": 20}],
        ),
    }
    live = {name: _best_effort_read(caller, service, method, args) for name, (service, method, args) in checks.items()}
    return {"agent": list_agents(paths), "capabilities": capabilities(paths), "live": live}
