# Test Plan

## 2026-09-09：Luna 实测后的 MCP 修复

- 完整 pytest：418 passed（122.45 秒）。新增 17 项实测问题回归，覆盖只读预检、目录与创建精简返回、
  静态入口可物化标记、变量字面量提示、文本结果证据和旧日志误判。
- Ruff、compileall 通过；全部 111 个 CLI 帮助入口通过。full / rpa-authoring 的真实 stdio 握手测试在完整套件中通过。
- 独立 GPT-5.6-Luna 会话执行原生创建、参数预览与提交、真实运行和输出内容核对。
  结果和每轮差异见 `docs/LUNA_RPA_EVALUATION.md`，原始记录在 `_tmp/luna-eval`。
- 未运行 Qwen 模型，也未发布 EXE/npm；后面的 401 项记录是本次连续优化的较早阶段。

## 2026-09-08：中小模型工具集与参数编辑

- 完整 pytest：401 passed（121.55 秒），其中新增编辑与协议回归 22 项。
- Ruff、compileall、git diff --check 通过；全部 41 个 workflow CLI 子命令帮助通过。
- 本机实时目录读取 998 个内置组件；full / rpa-authoring 均通过真实 stdio 初始化、枚举和关闭。
- 新增编辑提交在临时原生文件上验证，设计器写回与执行使用隔离后端；未运行 Qwen 模型评测，
  未将 schema 字符数下降当成生成成功率提升，也未发布新的 EXE/npm 包。

## Scope

- `test_core.py` covers project discovery, API wrappers, authentication redaction,
  component metadata, flow generation, RPA contracts, MCP schemas, dispatch, and write gates.
- `test_debug_session.py` covers debugger lifecycle, command routing, session reuse,
  runtime diagnostics, and cleanup behavior.
- `test_full_e2e.py` covers the installed CLI, live desktop integration, native project
  inspection, authentication response shape, and MCP-safe output.
- `test_workflow_edit.py` covers native edit context, zero-write previews, stale
  revisions, preservation of other nodes, credential-safe diffs, CLI/MCP parity,
  unsupported nested/container edits, compact analysis restoration, and real stdio
  initialize/list/close in both profiles. Native persistence is tested on temporary
  files; designer writeback and execution are isolated.

## Contract Checks

- The default MCP profile exposes exactly 56 uniquely named tools; the optional
  rpa-authoring profile exposes 29 and rejects calls to unadvertised tools.
- Every public schema rejects unknown fields and has a matching dispatcher branch.
- Full tool definitions stay below 39,000 characters; rpa-authoring stays below
  25,000 characters and 65% of the full profile. Full server instructions stay
  below 2,100 characters. Character counts are not model token measurements.
- Read operations redact credential-shaped values.
- Every mutating operation requires `allow_write=true`.
- Native parameter preview uses `dry_run=true` without write permission. It
  performs parameter encoding only, never claiming static or runtime validation.
- Flow validation runs before apply, execution, or debugger commands.
- Agent guidance references only tools present in the current MCP catalog.
- Small-card lifecycle tests verify one window open, renderer-only state updates,
  countdown reuse, and unconditional close on successful, blocked, and failed runs.
- Unit tests isolate the MCP desktop client so pytest never creates native UI on the
  workstation; live desktop access remains explicit in `test_full_e2e.py`.
- `test_full_e2e.py` smoke-tests every installed builtin component contract for
  stable type, parameter, output, and native code shape without executing all
  components against the desktop.
- `test_full_e2e.py` invokes `--help` for every workflow CLI command so the
  discoverable command surface cannot drift from the implementation.
- `test_core.py` verifies the shared `verification_gate` returned by staged and
  native step writes and static step validation.

Pagination capability regression coverage:

- `WebBrowser_DataCollectionV5` reports native pagination as an advisory,
  model-selectable capability; a numeric target alone does not require a selector.
- A numeric target alone does not create pagination evidence or force a
  pagination-capable component; the model can choose single-page collection when
  the live result already satisfies the output contract.
- Selecting native next-page or load-more mode requires the matching selector.
- `rpa-capture-elements --as-page-selector` routes to the pagination binding path,
  and the semantic pagination command does not replace collection columns.
- Rebinding collection columns preserves an existing native pagination selector
  through metadata updates, materialization, and designer writeback.

## Release Checks

- Ruff lint and formatting checks pass.
- The full pytest suite passes in an isolated virtual environment.
- The installed `shizai-agent` and `shizai-agent-mcp` commands work outside the source tree.
- MCP stdio completes `initialize`, `tools/list`, a read-only call, and a rejected write call.
- The wheel contains runtime code and metadata only; tests, caches, demos, and retired
  namespaces are excluded.
- The npm package contains only its manifest, README, and launcher.
- The packaged Skill passes structural validation.
- Repository paths, content, and build artifacts contain no retired generator branding.
- `scripts/mcp_vs_tars_stress.py` runs at least 12 byte-identical prompts through
  both MCP stdio and the direct native designer path, separating environment blocks
  from workflow failures.

## Results

Latest verification (2026-09-01):

```text
python -m pytest -q --tb=short
SHIZAI_FORCE_INSTALLED=1: 289 passed in 318.54s (0:05:18)
```

Static checks:

```text
ruff check shizai_agent tests  All checks passed
python -m compileall -q shizai_agent                  passed
git diff --check                                      passed (line-ending warning only)
```

MCP stdio smoke test (2026-09-04):

```json
{"server_version":"0.7.23","tools":56,"unique_tools":56,
 "readonly_ok":true,"write_without_gate_rejected":true}
```

Packaging checks:

- `python -m build`: sdist and wheel built successfully.
- Wheel contains only the runtime package and metadata; tests and caches are excluded.
- `npm install --package-lock-only`: passed; `npm pack --dry-run`: exactly 3 files.
- Skill structural validation: passed with `Skill is valid!`.
- `pip-audit`: `No known vulnerabilities found` for the installed environment.
- Repository content and filenames contain zero matches for retired generator branding.

Known scope boundary: desktop E2E tests require the local Shizai service and installed
designer data. The final run executed those fixtures successfully on this workstation.

Focused element-picker lifecycle verification (2026-09-01):

```text
python -m pytest tests/test_core.py -q -k "rpa or extension" --disable-warnings --maxfail=8
73 passed, 164 deselected in 22.33s
ruff check shizai_agent tests
All checks passed!
```

This focused run verifies that an element component is persisted as `unbound` before
scanning or official picking, later components are blocked until selector binding,
CLI/MCP picker entry points require the same `project_ref + step_id`, and the red
picker plus native mask close after both successful and failed capture.

Native UI lifecycle verification (2026-09-01):

```text
python -m pytest tests/test_core.py -q -k "native_small_card or mcp_rpa_run" --tb=short
12 passed, 233 deselected in 1.20s
ruff check shizai_agent tests
All checks passed!
```

The formal-run dispatcher now reuses one small-card window for validation, countdown,
takeover, running, and terminal text. A final cleanup closes the card, takeover frame,
native mask, and picker on every return or exception. Unit tests use an isolated MCP
desktop client, and the desktop log showed no new `SmallCardWindow` during pytest.

Native CLI design-and-run verification (2026-09-04):

```text
python -m pytest tests/test_core.py -q -k "native_module_text or pagination" --tb=short
7 passed, 299 deselected
python -m compileall -q shizai_agent
passed
```

The real project `京东手机商品采集100条-CLI边设计边验证-新窗口-20260904` was
created and materialized incrementally. The first run proved the quality gate by
rejecting 81 rows even though RuntimeHost reported success. After adding the
React-page URL pagination fallback, the second run completed with RuntimeHost
success and an Excel output containing 100 data rows, four non-empty required
columns, 100 unique `item.jd.com` links, and no quality warning.

Native runtime cleanup verification (2026-09-04):

```text
python -m pytest -q --tb=short
367 passed in 124.43s (0:02:04)
python -m pytest tests/test_core.py -q -k "native_cleanup or native_rpa_stop_is_idempotent or native_run_status_expires or native_run_timeout_uses"
6 passed
ruff check shizai_agent tests                         All checks passed
python -m compileall -q shizai_agent tests            passed
git diff --check                                      passed (line-ending warning only)
```

The cleanup tests cover `stopping -> stopped`, `code=3001` idempotence, failed
stop requests becoming `cleanup_timeout`, orphaned stopping markers expiring,
and `rpa-run --wait` invoking the common official stop RPC after a timeout.
The direct CLI regression additionally covers stale/final UI cleanup after a
successful run and an exception, plus deferred final cleanup for `--no-wait`.
The output-contract regression confirms that an “at least N” requirement
accepts more than N valid rows and rejects fewer than N.

Pagination planning and selector-preservation verification (2026-09-04):

```text
python -m pytest -q tests/test_core.py --tb=short
319 passed
ruff check shizai_agent tests
All checks passed!
python -m compileall -q shizai_agent tests
passed
```

The analyzer keeps a numeric row target as an output contract and does not infer
pagination from the number alone. The catalog still advertises `paginate`, while
native pagination is validated only after the model selects it. The regression
flow also verifies that rebinding collection columns preserves an already-bound
`selector_page` through metadata updates, materialization, and designer writeback.

Incremental verification gate and complete CLI/component contract verification (2026-09-04):

```text
python -m pytest -q --tb=short
371 passed in 180.08s (0:03:00)
python -m pytest tests/test_core.py -q -k "rpa_step_results_expose_incremental_verification_gate or mcp_server_exposes_a_compact_consistent_contract"
2 passed
python -m pytest tests/test_full_e2e.py -q -k "builtin_component_contracts_have_minimum_shape or all_workflow_command_help"
2 passed
ruff check shizai_agent tests                         All checks passed
python -m compileall -q shizai_agent tests            passed
git diff --check                                      passed (line-ending warning only)
```

The shared CLI/MCP core now returns `verification_gate` after a component write,
static step validation, and native element rebinding. The gate explicitly
requires design-time `debug_start`/reuse, `debug_command(action=next)`,
`debug_status`, and page/runtime evidence before the next component is appended.
The installed designer catalog smoke covered all 998 builtin records (996 engine
records plus 2 frontend-only entries); frontend-only entries without engine code
were checked as discoverable and intentionally non-materializable rather than
executed.

The first installed-CLI check used the existing system installation and exposed
that its executable predated the current `rpa-bind-pagination` command. A fresh
isolated build from this source was then used as the installed CLI; the complete
371-test run and all workflow command help checks passed. The system installation
was not overwritten during verification.

Release candidate verification (`0.7.24`, 2026-09-05):

```text
python -m pytest -q --tb=short
371 passed in 118.59s (0:01:58)

SHIZAI_FORCE_INSTALLED=1 python -m pytest tests/test_full_e2e.py -q -s -k TestCLISubprocess --tb=short
[_resolve_cli] Using installed command: ...\\dependencies\\python\\Scripts\\shizai-agent.EXE
10 passed, 7 deselected in 41.16s

ruff check shizai_agent tests
All checks passed!

python -m compileall -q shizai_agent tests
passed

node --check npm/shizai-agent-mcp/bin/shizai-agent-mcp.js
passed

git diff --check
passed (line-ending warning only)
```

The release candidate was installed editable into an isolated Codex runtime,
and the subprocess suite was forced to resolve that installed command. Version
metadata is synchronized to `0.7.24`; no user project, capture, or intermediate
build directory is part of the source change or release payload.

Release candidate verification (`0.7.25`, 2026-09-05):

```text
python -m pytest -q --tb=short
374 passed in 129.13s (0:02:09)

The frozen MCP shutdown regression tests cover closed-stderr writes, the
post-stdio frozen exit path, and normal source-mode finalization. The one-file
MCP smoke test must additionally verify initialize, tools/list, exit code 0,
and empty stderr against the built executable.

Packaged artifact smoke result: server version `0.7.25`, `tools/list` returned
56 tools, exit code `0`, and stderr was empty. The published ZIP SHA256 is
`13ef7ffe4845ddeb9ffe65d931534a8a6729a33b3ceb44f8b314cc36f1af1694`.
```

Release verification (`0.7.26`, 2026-09-05):

```text
python -m pytest tests -q
374 passed in 109.44s (0:01:49)
ruff check shizai_agent tests                         All checks passed
python -m compileall -q shizai_agent tests            passed

Packaged CLI --help                                      exit code 0
Packaged MCP initialize/tools-list                        initialize ok, 56 tools
Packaged MCP shutdown                                    exit code 0, stderr empty
ZIP SHA256                                               5545c6cc5a4add13cdab57e5c5607d43aa5693c965fc896b9e0066ea13a07f78
```

The release script builds both one-file executables in a system temporary
directory and leaves only the final ZIP and checksum under `release/`.

MCP attachment guidance verification (2026-09-08):

```text
python -m pytest tests -q --tb=short
379 passed in 142.80s (0:02:22)

ruff check shizai_agent tests
All checks passed!

python -m compileall -q shizai_agent tests
passed

node --check npm/shizai-agent-mcp/bin/shizai-agent-mcp.js
passed
```

The MCP initialization instructions now stop an unmounted conversation before
it invents a replacement MCP client. `rpa_generation_context` exposes the same
attachment check and the setup document provides the source entry points,
upgrade command, and copy-paste new-conversation prompt. This guidance does
not prescribe a site workflow, force pagination, or require the native
collection component.

The test-conversation ownership rule is also documented: acceptance runs belong
to a normal conversation created inside the Codex project `一句话生成流程mcp测试`,
not to a fork or collaboration child thread. The project path is
`D:\agent\indeed_mcptest\`.

## Expanded Luna evaluation (2026-09-09)

Seven two-turn cases under `tests/evals/` cover file transformation/copy,
Excel formulas, CSV aggregation, native control flow, browser search,
pagination/deduplication and insertion between stable existing node IDs.
`scripts/luna_rpa_suite.py` runs independent GPT-5.6-Luna conversations serially
against the native desktop, using isolated fixtures and owned test applications.
`scripts/grade_luna_rpa.py` independently checks preserved text/CSV/XLSX outputs.
Protocol completion alone never counts as business success.

Regressions in `test_expanded_eval.py` cover live component variants, file/table
capability discovery, per-turn output/task evidence, false pagination passes,
stopped partial outputs, generic native selector binding, and version-scoped
existing-file preconditions. `test_core.py` additionally checks the pure-file
column validation gate, retained timeout cleanup state, and output-file aliases
through both strict and thin MCP preflight.

Full results and reproduction commands: [LUNA_RPA_EXPANDED_EVALUATION.md](../docs/LUNA_RPA_EXPANDED_EVALUATION.md).

Latest local validation after designer visibility, typed native enum and variable type checks:

```text
python -m pytest -q
435 passed in 140.24s (0:02:20)

python -m ruff check shizai_agent tests scripts/luna_rpa_eval.py scripts/luna_rpa_suite.py scripts/grade_luna_rpa.py scripts/rpa_eval_site.py
All checks passed!

python -m compileall -q shizai_agent tests scripts
passed
```

## Standalone component benchmark pack

`benchmarks/rpa/` contains 32 two-turn cases, 5 optional reference workflows,
seeded browser/file/table inputs and evidence-first grading. `scripts/rpa_benchmark.py`
prepares baseline/reference trials, reuses the existing native runner, checks
artifacts and edit identity, and compares verified paired results. No domain
matching or benchmark answers were added to the MCP runtime.

Validation for this pack:

```text
python -m pytest tests/test_rpa_benchmark.py -q
16 passed

python -m pytest -q
451 passed in 125.87s (0:02:05)

python -m ruff check benchmarks scripts/rpa_benchmark.py tests/test_rpa_benchmark.py
All checks passed!
```

Five HTTP smoke requests checked actual fixture responses (pagination, missing
sales, iframe, login gate and product detail), with the owned server stopped
afterward. These and the synthetic grader reports are not native model results.
The 32-case real-model campaign has not been launched as part of writing the pack.
