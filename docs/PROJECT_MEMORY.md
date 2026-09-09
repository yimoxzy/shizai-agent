# Project Memory

This file is the durable implementation memory for the native RPA CLI. Update it
when the CLI or its workflow contract changes, and follow it before editing code.

## Planning

- The standalone benchmark is `benchmarks/rpa/`, operated through `scripts/rpa_benchmark.py`.
  Its business cases, fixture answers and optional workflow references must not become MCP keyword routing.
  References match explicit benchmark facts, contain no selectors/results/fixed component versions, and are
  compared against a no-reference baseline. Accurate business completion gates all speed claims.

- Broader Luna evaluations live in `tests/evals/` and `docs/LUNA_RPA_EXPANDED_EVALUATION.md`.
  Run native desktop cases serially; preserve turn-specific outputs and task snapshots. A completed model turn,
  a valid plan, and a correct artifact from a replacement application are separate outcomes.
- Plan output contracts expose canonical `file` / `columns` / `max_rows`; preflight also normalizes `output_file`
  to `file`. Never silently track `result.xlsx` when the model explicitly supplied the actual output file.
- Generic file/table workflows can declare columns without webpage element bindings. The column-binding-count
  warning applies to webpage collection, not every flow that writes tabular data.
- After timeout cleanup, retain the persisted stopped/cleanup outcome instead of overwriting it with the earlier
  submitted marker. A stopped run cannot become successful merely because it created a fresh partial file.
- Exact live catalog lookups may show related variants sharing the same baseType. Do not silently substitute
  one variant or infer universal deprecation rules from a releaseTag.
- Native designer source explicitly defines hidden=128 and filters releaseTag & 128 in its component list.
  Authoring discovery follows that proven visibility rule (include_hidden=false); full/CLI keep compatibility
  defaults. Exact type inspection still resolves hidden existing nodes. Visibility is not materializability.
- Native JSON dropdown values are exposed as typed semantic_choices and defaults. Thin preflight rejects
  a literal string "3" when the component enum requires numeric 3; variable/expression values remain deferred.
  Raw native expression metadata is retained, and unparseable expressions are never invented into an enum.
- Thin preflight checks explicit variable references against unconditional concrete live vType declarations.
  A known list output cannot feed a dataframe input; strings/file paths and numeric families are compatible.
  Unknown/conditional types and expressions are deferred. Reassignment clears stale producer type knowledge.
- The designer has frontend codeGenerator-based loop/branch components, but the current generic MCP writer
  only compiles linear engine components. Do not claim the designer itself lacks native control flow.
- File_Write / File.Write 3.3.5.0 has a verified existing-target precondition, exposed in catalog and thin-preflight
  warnings. Keep it advisory and version-scoped; a preceding step can legitimately create the target.
- The authoring profile may omit start_design.query and recover it from the supplied analysis cache reference.
  An explicitly changed query must still fail the original-analysis check. Reusing an application does not apply a new plan.
- Generic native selector binding uses the live uiSelector contract for main-flow nodes, preserving component type,
  browser variable references and unrelated parameters. It accepts one imported element for this primary selector.

- The live component catalog is the source of truth for installed component types,
  parameters, outputs, and capabilities.
- Request analysis treats Chinese full-width punctuation as syntax. A URL must
  stop before `，。；` and similar punctuation; explanatory suffixes such as
  `等可见商品信息` and `重点验证销量` must not become output columns.
- `WebBrowser_DataCollectionV5` exposes optional native pagination through the
  `paginate` capability. It supports `selector_page` for next-page controls and
  `selector_more` for load-more controls.
- Pagination is a model decision. A requested row count is an output acceptance
  criterion, not a hard-coded trigger that forces pagination. Single-page,
  explicit loop, JavaScript, download, and other valid strategies remain allowed.
- The request analyzer must not infer a pagination requirement from a numeric
  target alone. Explicit paging wording is recorded as an optional model
  preference; the live catalog remains the source of the `paginate` capability.

## Binding

- Missing `outputs` in a model plan means "use the native contract"; explicit
  `outputs: []` means "keep this assertion" and must not be silently filled.
- A pagination control is a page-control binding, not an output column. Never add
  it to `collectionConfig.columnSelectorArray`.
- Bind a native pagination control only after the model selects native pagination
  and after the control is captured from the same current-page snapshot as the
  collection fields.
- `selector_page` and `selector_more` are mutually exclusive modes. The CLI must
  preserve deliberate empty values and validate the selected mode's required slot.
- Rebinding collection columns must preserve an existing concrete paging selector.
  `region/page_type_num` metadata without a selector is incomplete planning state,
  not permission to overwrite the task's captured selector. Only a concrete model
  selector or an explicit single-page choice may replace it.
- After `page_identity_changed`, scan and capture the current page with the same
  `project_ref`; pass `allow_rebind=true` only for deliberate replacement of an
  already-bound step. The default capture path remains unbound-only.

## Write And Readback

- Native parameter edits support `dry_run` and `expected_revision` in the shared
  `update_rpa_step_config` core. Preview prepares task/metadata/page-scope changes
  without persistence or UI, and explicitly reports `parameter_encoding_only`.
- `workflow_edit.read_edit_context` reads IDs and selected parameters from the
  actual native task, including nested nodes. It must not substitute cached plan
  config for authoritative arguments. Native expressions remain labelled as such.
- Revisions cover task files, metadata, elements and variable bindings. Check at
  preparation and immediately before mutation. This does not lock external
  designer writes or provide rollback across multiple files/RPC materialization.
- `SHIZAI_MCP_TOOL_PROFILE=rpa-authoring` advertises 29 RPA tools; default `full`
  keeps 56. Initialization instructions and callable tool names must agree.
- Whole-plan creation can persist the graph before component verification;
  the verify-before-append rule applies to incremental `add_step` operations.

Every CLI mutation follows this transaction:

```text
read -> validate -> mutate -> persist -> materialize -> read back task/metadata/
generated files -> validate again
```

The `.task` file is authoritative for native arguments. `szrpa.json` stores the
model plan and metadata. Generated modules and indexes must agree with both before
running or opening the flow in the client. A designer RPC success response alone
is not proof of persistence.

## Verification

- Design incrementally: write one component, validate it, capture/bind its real
  element if needed, then single-step it before adding dependent work.
- Run-time validation must report the selected pagination mode and binding status
  in structured output, without inventing a pagination requirement.
- A numeric output target expressed as “at least N” is a minimum acceptance
  threshold: fewer than N valid rows fails quality validation, while more than N
  valid rows is acceptable. Collection may still cap or stop at the model's
  selected target when the native component supports that behavior.
- Direct CLI `workflow rpa-run` must mirror the MCP run dispatcher lifecycle:
  clean stale native UI before submission, and in wait mode always clean the
  DeepCapture picker plus native card, takeover frame, and run mask after a
  terminal result or exception. Persist `stale_ui_cleanup` and `final_cleanup`
  evidence; never infer that `visible=false` is proof without the cleanup call.
- Keep `--no-wait` cleanup deferred while RuntimeHost may still be active, and
  expose that state explicitly so a caller can finish with `rpa-status`/`rpa-stop`.
- Keep final UI cleanup deferred for `waiting_for_user` results so login or
  challenge interaction remains possible in the current browser.
- A debug stop must leave `stopping` within the bounded RPC window and persist
  either `stopped` or `cleanup_timeout`; use `retryable` and `next_tool` to make
  a timeout actionable. Stop RuntimeHost before disconnecting DAP.
- If `openSmallCardWindow` returns `code=5000` but its renderer update succeeds,
  report a reusable non-fatal window (`open_result_status=reused_window`) rather
  than presenting a tool failure.
- When a write changes native state, add or update a regression test and update
  this memory plus the relevant CLI/workflow documentation in the same change.

## Windows One-File MCP Shutdown

- The Windows one-file MCP previously exited with code `0` but could print a
  non-fatal `ValueError: I/O operation on closed file` traceback to stderr when
  an SDK client closed the stdio session. This was a PyInstaller/AnyIO
  interpreter-finalization race, not a browser, DeepCapture, or workflow error.
- The frozen entry point must exclude the unused `multiprocessing` package,
  run the AnyIO transport with `anyio.run`, install
  `install_shutdown_safe_stderr()` before business imports, and call
  `os._exit(0)` only after `_run_stdio` has returned and its native UI cleanup
  has completed. Source-mode processes keep ordinary Python finalization.
- Release acceptance requires all three checks against the built executable:
  MCP initialize succeeds, `tools/list` returns the expected tool set, and a
  normal client close returns code `0` with empty stderr. A successful exit
  code alone is insufficient.
- This workaround is specific to the frozen stdio executable. Do not use
  `os._exit` in shared business code or before the MCP transport and native UI
  cleanup have completed.

## MCP Rule Placement

- Universal safety and channel rules belong in `shizai_agent/core/mcp_server.py`
  under `MCP_INSTRUCTIONS`; keep them short and write the rule text in Chinese.
- RPA business conditions belong in `shizai_agent/core/project.py` under
  `rpa_generation_context["business_rules"]`; MCP exposes this context through
  `shizai_rpa_analyze_request`.
- Tool-specific preconditions belong in the matching `_schemas()` description.
- Rules that must reject a write or run belong in shared-core validation/state
  checks and require regression coverage. `docs/` and this memory explain the
  contract to developers but are not automatic MCP context.
- Use the structure `when`, `must`, `must_not`, `evidence`, and `on_failure`;
  use Chinese prose while keeping tool/component/parameter identifiers exact.
- A real MCP client keeps one stdio process alive for the whole design/run
  exchange. A per-call subprocess driver loses process-local analysis, scan and
  session state and is only a smoke-test anti-pattern.

## MCP Client Attachment

- A prompt cannot inject tools into a conversation. Before an RPA task, the host
  must expose `shizai_*` tools, especially `shizai_rpa_analyze_request` and
  `shizai_rpa_component_catalog`; an empty tool surface is a host registration
  problem, not a browser or DeepCapture failure.
- When the tools are absent, the model must stop and ask the host to run
  `npx -y shizai-agent-mcp@latest install`, restart Codex, and verify `/mcp`.
  It must not create a replacement MCP client or claim that a flow was created.
- The source CLI entry is `shizai_agent/shizai_agent_cli.py` and the MCP entry is
  `shizai_agent/core/mcp_server.py`. Keep one stdio process alive from
  `initialize` through the complete design/run exchange.
- Keep the reusable setup instructions and test prompt in
  `docs/MCP_CLIENT_SETUP.md`; update it whenever the host registration or CLI
  entry changes.

## Test Conversation Ownership

- RPA MCP acceptance tests must be created as a normal new conversation inside
  the Codex project `一句话生成流程mcp测试` (`D:\agent\indeed_mcptest\`). Do not
  use a fork, collaboration sub-agent, or hidden child thread as the test
  conversation; it may run tools but will not reliably appear in that project's
  conversation list.
- Project ownership and MCP attachment are separate checks. After creating the
  normal conversation, verify the `shizai-agent` tool surface before sending the
  long-running JD request. If the tools are missing, stop and fix the host
  registration first.
- The ongoing acceptance log is `D:\agent\indeed_mcptest\MCP_TEST_LOG.md`.
  Append prompt version, MCP/CLI version, conversation ID, tool trace, outcome,
  evidence, problems, root cause, and optimization direction after every run.

## Release And Remotes

- After a code or documentation change is verified, commit and push the source
  changes to the Gitee remote `origin` in a timely manner.
- Do not push source code to the GitHub remote. GitHub is reserved for the final
  MCP artifact produced by the release workflow; never upload intermediate build
  directories, temporary captures, test output, or generated project data.
- Before pushing either remote, inspect the exact staged file list and verify the
  destination remote. A successful local commit is not a substitute for checking
  the remote push result.
- A release version must be synchronized across `pyproject.toml`,
  `shizai_agent/__init__.py`, the PowerShell installers, npm launcher defaults,
  and user-facing release documentation before building.
- Build the Windows package in an isolated temporary directory, verify the ZIP
  contains only the final compiled distribution files and required install
  metadata, and reject `.py`, `.task`, project data, intermediate build trees,
  and test reports. Push source only to Gitee; GitHub receives the final release
  ZIP and checksum, never the current source branch or intermediate artifacts.
