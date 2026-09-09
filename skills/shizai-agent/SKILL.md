---
name: shizai-agent
description: Operate the local Shizai designer through its focused MCP tools for Agent workflow graphs, native RPA design, Chrome element capture, component discovery, validation, debugging, and verified execution.
---

# Shizai Agent

## Route First

Use `shizai_route_intent` when the request type is ambiguous:

- `agent_workflow`: Agent, chatbot, knowledge-base, LLM, or conversation graph.
- `browser_rpa`: Chrome, webpage, element, search, scrape, or collection automation.
- `native_rpa`: desktop, OCR, Excel, file, database, keyboard, mouse, or designer-component automation.

Do not mix Agent graph nodes with native RPA components.

## MCP Availability Gate

Before planning any workflow, confirm that the current conversation exposes the
`shizai_*` MCP tools, especially `shizai_rpa_analyze_request` and
`shizai_rpa_component_catalog`. If they are absent, this is a host configuration
problem, not a browser or DeepCapture problem. Stop the business task and report
that the host must run `npx -y shizai-agent-mcp@latest install`, restart Codex,
and verify `shizai-agent` plus its tools in `/mcp`. Do not write a replacement
MCP client, start a second stdio process, or claim that a flow was created.

The source CLI entry is `shizai_agent/shizai_agent_cli.py`; the source MCP entry
is `shizai_agent/core/mcp_server.py`. The installed Windows MCP is normally
under `%LOCALAPPDATA%\\Programs\\ShizaiAgent\\shizai-agent-mcp.exe`. Keep one
MCP stdio process alive from `initialize` through the whole design, verification,
run, and `close` sequence. The copy-paste setup and test prompt are in
`docs/MCP_CLIENT_SETUP.md`.

## Writes

Pass `allow_write=true` only for an intended mutation, run, or debug action. Read and validate before writing. Preserve existing user nodes, variables, elements, and projects unless the request explicitly replaces them.

## Adding Business Rules

规则正文统一使用中文；工具名、组件 `type`、参数名和状态值保持真实英文标识符。
通用安全规则写入 MCP 初始化说明，RPA 条件规则写入共享核心返回的
`rpa_generation_context["business_rules"]`，单工具前提写入对应工具 description，
必须拦截的规则写入共享核心校验并补测试。每条规则写明“适用条件、必须、禁止、
验证证据、失败处理”，不要只在开发文档中写规则，因为普通 MCP 客户端不会自动读取仓库文档。

## Native UI Feedback

RPA tools publish the TARS card and takeover UI automatically from their real
phase. Do not call `shizai_ui_feedback`, `mask`, or `scan_demo` during ordinary
design, capture, validation, or execution. Use that tool only when the user
explicitly asks for an isolated UI action.

Use the lifecycle consistently. Analysis, task decomposition, plan validation,
application creation, first-node writing, and complete-plan persistence are all
`designing`; these phases do not control the computer. Only after the complete
plan is durable, and immediately before the first real browser or desktop I/O,
show the 5-second countdown and `takeover` frame. Use `scanning`/`capturing` for
DeepCapture or OCR, `validating` for checks, `running` for execution, and
`waiting` for login, challenges, or prerequisites. Publish one terminal state,
then close the frame and card. The red picker is only for a real interactive
pick, never ordinary progress.

## Agent Workflows

`shizai_components` is an Agent-graph-only directory. Browser and desktop RPA
requests must use `shizai_rpa_component_catalog`; never substitute the Agent
node directory, even when the request asks to inspect components.

Use this sequence when it helps the task; it is a reference, not a required
script:

```text
shizai_generation_context
-> shizai_components
-> shizai_draft
-> shizai_validate_draft
-> shizai_apply_flow
-> shizai_runs / shizai_logs / shizai_tracing
```

The client model designs the graph from live node defaults. Copy real component types, branch handles, tool parameters, and variable selectors such as `[node_id, output_name]`; do not infer them from display labels. `shizai_apply_flow` validates, saves, rereads, and can optionally run or publish.

## Browser RPA

The current client model owns intent interpretation and workflow design. Use
`shizai_route_intent`, `shizai_rpa_analyze_request`, and
`shizai_rpa_component_catalog` as read-only context when helpful, then choose
the components, order, variables, loops, conditions, JavaScript, and pagination
strategy that fit the user's target. Catalog capabilities and
`planning_patterns` are matching hints, not a unique plan or pagination
protocol. A numeric target is an output acceptance criterion; it may be met by
component-managed pagination, a loop with a next-page component, JavaScript, a
download, or another valid design.

Do not infer pagination from a numeric target alone. The analyzer treats a row
count as an output contract; the live catalog advertises `paginate`, and the
client model decides whether the current page already satisfies the target.

Submit the model's plan with `validation_mode="model_decides"` (the MCP default).
Use the live catalog's component type and parameter names. The MCP boundary
checks basic JSON shape, installed/materializable components, declared
parameters when the catalog exposes them, and safety. `operation_ids`,
`purpose`, `depends_on`, output labels, loops, conditions, JavaScript, and
pagination are model-owned fields; no one sequence or pagination owner is
required. Final variable/element compatibility is checked by the shared CLI
and designer when the step is written or run.

`WebBrowser_DataCollectionV5` advertises `paginate` when its live contract has
`collect_pages`, `selector_page`, or `selector_more`. Treat this as an available,
model-selectable capability: the model decides whether to use native pagination
after considering the page and output contract. Do not turn a numeric target into
an unconditional pagination requirement. If native pagination is selected, bind
only the required `selector_page` or `selector_more`; never put that control in
`columnSelectorArray`. The shared CLI validates the selected mode, but accepts an
empty selector for a deliberate single-page or alternative strategy.

When collection columns are rebound, preserve any already-captured paging
selector unless the model supplies a concrete replacement or explicitly selects
single-page mode.

Prefer an installed, materializable native component when its live contract
clearly covers the current atomic operation. For structured web collection,
evaluate `WebBrowser_DataCollectionV5` first when its fields, output, and page
controls match the current evidence. This is an advisory `warning_only` policy,
not a mandatory component choice: JS, downloads, loops, or another installed
composition remain valid when they fit the page or contract better. When using
an alternative, state the native-component mismatch or limitation and verify
the selected plan's output; the MCP must not reject a plan merely because it
did not use the recommended native component.

Keep the user's result contract (fields, minimum rows, output path) and verify it
after execution. If the analysis result was compacted, pass its `analysis_id`;
the MCP restores the immutable cached analysis.

### Incremental Design Gate

Design and verification are interleaved. After every `shizai_rpa_add_step` or
step-config update, read `verification_gate` and do not append the next
component until the current one has both:

1. `step_validation.valid=true` from the static contract check.
2. A design-time component run via `shizai_rpa_debug_capabilities`,
   `shizai_rpa_debug_start`/reuse, `shizai_rpa_debug_command(action="next")`,
   and `shizai_rpa_debug_status`, plus page/runtime evidence confirming the
   intended effect.

For the CLI use the corresponding `workflow rpa-validate-step`,
`workflow rpa-debug-*` commands. Static success, an accepted run request, or a
connected debugger is not runtime evidence. On failure repair the current step
and keep the same `project_ref`; do not create a second flow. OCR can confirm a
visible page result, but it cannot replace DeepCapture selectors or become a
mouse-control workaround.

Catalog roles and planning patterns are recommendations for matching, not a
unique workflow. A numeric target is an output acceptance criterion; the model
may use component-managed pagination, a loop, explicit next-page steps,
JavaScript, a download, or another valid design. Preserve the user's fields,
minimum rows, and output path, then verify them from runtime evidence.

```text
shizai_rpa_analyze_request
-> shizai_rpa_component_catalog
-> shizai_rpa_validate_plan
-> shizai_rpa_start_design(open_browser=false)
-> shizai_rpa_open_browser
-> shizai_rpa_scan_page(project_ref, step_id)
-> optional shizai_extension_start_capture(project_ref, step_id)
-> shizai_rpa_capture_elements(project_ref, step_id)
-> shizai_rpa_validate_step
-> shizai_rpa_validate_flow / shizai_rpa_validate_project
-> shizai_rpa_materialize / shizai_rpa_sync_diagnose
-> shizai_rpa_run / shizai_rpa_status
```

On a boundary or native error, use the reported field/path and the live
contract to repair the existing plan. Do not rewrite the user's intent or
silently replace the model's strategy. Keep the same project reference while
iterating.

Every native CLI mutation follows `read -> validate -> mutate -> persist ->
materialize -> readback -> validate`. The `.task` file is authoritative for
native arguments, `szrpa.json` stores model metadata, and generated modules and
indexes must agree before the flow is opened or run. An RPC success response is
not persistence evidence by itself.

`shizai_rpa_start_design` can write a complete model-owned graph before takeover;
CLI-style incremental flows can instead append components with
`shizai_rpa_add_step`. In both cases follow `verification_gate` and
`next_action` together: `next_action` is only a structural suggestion until the
current gate is verified. After navigation, refresh page evidence and never
reuse an old `scan_token` or transient element ID. Find each persisted
element-dependent component and verify `binding_status=unbound`; only then
scan or start the red picker for that exact `project_ref + step_id`. Capture
binds the selector to the same component and closes the picker on success or
failure. Repair the existing flow instead of creating a second project.

If preflight reports login, a page challenge, or a disabled prerequisite, pause
and ask the user. Perform login only when the user explicitly delegates it and
supplies current-session credentials; never persist credentials. Resume from the
same flow after confirmation and fresh preflight.

## Native Components

For non-browser RPA, use the same live catalog and shared CLI components. Query
only the context needed for the task; check `availability` and `requires_import`
for business components.

Prefer installed components for file I/O, Excel, databases, loops, conditions, variables, and saving. A code node is only for one uncovered atomic calculation.

## Validation And Execution

Static path:

```text
shizai_rpa_validate_step
-> shizai_rpa_validate_flow
-> shizai_rpa_validate_project
-> shizai_rpa_materialize
-> shizai_rpa_sync_diagnose
```

Component debugging:

```text
shizai_rpa_debug_capabilities
-> shizai_rpa_debug_start
-> shizai_rpa_debug_status
-> shizai_rpa_debug_command(action=next|continue|step_in|step_out)
-> shizai_rpa_debug_stop
```

Normal execution:

```text
shizai_rpa_run -> shizai_rpa_status
```

Static validation, accepted submission, or a normal DAP disconnect is not completion evidence. Verify terminal status, current-run logs, output freshness, table headers, row count, required non-empty columns, and representative values.

Before an element-dependent debug action, honor `shizai_rpa_runtime_preflight`. If the page changed, preserve the current Chrome and project, recapture and rebind elements, then resume with `shizai_rpa_debug_command(action="next")`.
