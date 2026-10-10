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

- Extension 0.1.10 source verifies repeated fields by identity in every row, not
  sibling positions inferred from the first card. Missing/ambiguous fields, duplicate
  column targets and visible page-wide matches outside the rows fail rather than drop
  rows or substitute another node. Samples and native bindings use the same verified
  `fieldSelector`; `sourceSelector` preserves the original observed input.
- `collection_elements` requires `verification.method=field-identity-per-row` and
  equal candidate/verified/returned row counts. It must not broaden a selector by
  stripping positional predicates in Python. An older extension needs upgrade/reload.
  This change remains unreleased; the live 2026-10-08 success used 0.1.9 and exposed
  the bad positional sample, while native field bindings returned correct raw values.

- The companion bridge can be owned or shared (`connection_mode=owner/shared`).
  Additional MCP processes use the same loopback port's `/mcp` WebSocket endpoint,
  authenticated with the installation token and rejecting every browser Origin.
  It is a relay to the existing extension connection, not another extension session.
  A surviving manager may acquire the port on its next call after the owner exits.
- A lost in-flight browser operation has an unknown outcome: fail pending requests
  promptly, never replay clicks/inputs, and tell the model to re-observe the page.
  Only pre-send transport loss is `not_sent`. Status reads are safe to retry.
  An extension that connects during a call's initial wait must receive that call;
  returning only `ready` would falsely imply the requested action completed.
- Browser sharing tests use both real stdio MCP processes and a controlled WebSocket
  extension peer. Keep that evidence separate from actual Chrome and native RPA tests.
  Version labels alone do not prove installation freshness; compare advertised tools
  (the installed 0.7.28 EXE on 2026-10-08 had 56 tools, source had 67).

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
- Native step removal is revision-driven (`remove_rpa_step` / `shizai_rpa_remove_step`
  / `workflow rpa-remove-step`). It refuses a step whose output variable is still read
  by a later statement unless `allow_dependents=true`, and it drops the plan entry plus
  the JS-link metadata that only described the deleted node. A preview must not publish
  designer UI: `validate_rpa_project` and `validate_rpa_flow` take `publish_ui=False`
  for read-only callers.
- Saving the active editor is revision-driven too (`save_rpa_editor` /
  `shizai_rpa_save_editor` / `workflow rpa-save-editor`). It forwards
  `PackageSaveService.saveAll` and then re-reads the dirty flag, revision and step
  list; a still-dirty editor returns `save_unconfirmed` with `retryable=true`
  instead of reporting success.
- A full run or a debug start must not proceed while the active editor has unsaved
  changes. Both entry points return `editor_unsaved_changes` with a
  `shizai_rpa_save_editor` next action; an absent editor does not block, and the
  result records `editor_present=false`.
- Global keyboard components (`Keyboard_*`, currently `Keyboard_PressNew` and
  `Keyboard_Send_V1`) type into whichever window owns the focus, so their preflight
  verifies the target Chrome window instead of an element binding. The check matches
  the flow's element page title, restores and foregrounds the window, and re-reads the
  foreground handle; an unconfirmed focus returns `window_focus_unverified`, and a
  target that cannot be identified from a title returns `target_window_unknown`. Both
  block the step; neither passes silently.
- Page components consume the upstream browser node's own output variable, which a
  model plan may rename. `collection_web_page_binding` and `js_web_page_binding`
  therefore accept the predecessor's decoded `var_name`, the planned output names and
  the canonical `网页对象` default, and report the accepted names on failure.
- A plan may name fewer outputs than the native component writes, because the designer
  always emits every declared slot. Surplus trailing slots pass only when each one is a
  canonical output of that component (for example `监控空列数据`); any other surplus
  keeps failing and is named in the check detail.
- Revisions cover task files, metadata, elements and variable bindings. Check at
  preparation and immediately before mutation. This does not lock external
  designer writes or provide rollback across multiple files/RPC materialization.
- `SHIZAI_MCP_TOOL_PROFILE=rpa-authoring` advertises 43 RPA tools, including add_step
  and its existing before_step_id main-flow insertion option; default `full`
  keeps 67. Initialization instructions and callable tool names must agree.
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

- Debug task discovery must delegate to the same `_native_task_file` resolver
  used by editing. Never keep a second recursive task selector in debug_session:
  an actual 0.7.29 session selected a 9-step draft backup while read_flow correctly
  selected the 7-step root task. The session was stopped before next; the source
  fix excludes restore backups and fails when only a backup remains.
- Browser preparation supports manual designer flows without a component_plan.
  Resolve their native steps using live navigation contracts, prefer the saved
  native URL over stale plan metadata, and accept a literal bare hostname via
  HTTPS. Unresolved variable objects/names and non-HTTP schemes must not open.
  Plan/native identity mismatch checks still apply when a model plan exists.

## Verification

- Progress capsules must be scoped to finite MCP operations: analysis, creation,
  window/page scans, field capture, step/flow validation and exact component runs
  close their card in dispatch finally, on success or error. Closing this hint
  must not stop a paused debugger, picker, or its browser object cache. Do not
  auto-close cards for ongoing debug/picker commands or explicit UI controls.
- Explicit `rpa_stop` must close the small card and takeover frame as well as the
  runtime mask. A core runtime `cleanup_complete=true` does not prove all desktop
  surfaces disappeared. The 2026-10-08 capsule remained after successful native
  collection because validate_step opened it and run_component/stop left it open.
  A card close warning must not be treated as success or clear the retry marker.
  Source lifecycle repair is not yet installed; live cleanup used the installed
  CLI `ui small-card --state hidden --allow-write` and the window disappeared.

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

- 2026-10-08: 0.7.29 published to Gitee (source), GitHub (ZIP/checksum) and npm
  (latest); local installation passed actual EXE full=67/authoring=43 and hash checks.
  npm may accept publication with HTTP 202 before the version becomes resolvable;
  verify the public version/dist tag separately before claiming availability.
  Root install.ps1 now uses the matching npm installer because GitHub main source
  no longer receives updates. Do not restore the stale raw GitHub installer URL.
- Browser host registration is persistent. After user loading on 2026-10-08,
  actual MCP browser_status returned ready/connected, extension 0.1.9. The existing
  7-step JD flow's native collection returned a fresh 59-by-4 table via the attached
  MCP tools; this supersedes the earlier extension_missing installation observation.
  Three rows repeat; one price is pending publication; heat mixes sales/reviews/views.
  This is human-guided component validation, not Luna/Qwen, pagination, 100-row or
  Excel acceptance. See MCP_BROWSER_EXTENSION_LIVE_EVALUATION_20261008.md.
  The 0.1.10 field-identity fix is source-only and has not replaced the installed package.

- Starting with 0.7.29, the npm launcher and server have the same version. The
  launcher takes its default target from package.json, not another hard-coded version.
  The release build checks all source version declarations before compiling.
- `release_integrity` verifies the frozen MCP's exact schemas for both profiles,
  isolated browser status, quiet shutdown, CLI version and Native Messaging bootstrap.
  `release-manifest.json` includes the verified tool names/counts and payload hashes.
  Installers must validate it before stopping/replacing existing programs; no missing
  extension/host payload may be labelled successful. Exclude extension tests from ZIPs.
- Browser host registration defaults on, but does not prove the unpacked extension
  is loaded by Chrome/Edge. Upgrade preserves the existing installation token and port.
  Frozen MCP and Native Host read config beside their own executable, honoring an
  explicit SHIZAI_BROWSER_BRIDGE_CONFIG override. Never persist a temporary host path.
- npm status distinguishes config, extension files, native host and unknown loaded state.
  MCP status diagnoses incomplete managed installs and stale native registrations.
  The npm stdio launch path refuses stale installs rather than printing install logs
  into protocol stdout or terminating other sessions to upgrade behind the caller.

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
