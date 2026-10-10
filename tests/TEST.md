# Test Plan

## 2026-10-08：桌面胶囊残留收尾修复（源码，未发布）

- 用户指出空闲时仍显示胶囊。发现 validate_step 打开卡片后不关闭，run_component 不收尾，
  rpa_stop 的原生运行清理只覆盖运行遮罩，遗漏独立小卡；原 cleanup_complete 不能证明胶囊已消失。
- 已安装 CLI 的 `ui small-card --state hidden --allow-write` 返回 code=0；
  Computer Use 前后窗口列表确认该小卡窗口从 4 个设计器窗口中消失，主客户端仍在。
  当时附加 MCP debug_stop 返回 Transport closed，没有把该调用记为清理成功。
- 新增 tests/test_progress_card_lifecycle.py：17 passed。覆盖成功/异常收尾、上一次预览残留、
  stop 成功/超时后的全界面清理、拒绝写入不动 UI、关闭失败保留重试状态、连续调试/拾取保留提示。
- 现有 MCP/小卡/原生 UI 定向回归：66 passed、377 deselected（3.14 秒）；Ruff 和 diff 检查通过。
- 未修改流程或重跑采集，未升级已安装 MCP；自动收尾需下一发布包验收。
  回归报告：`_tmp/capsule-lifecycle-regression-20261008.xml`；人工过程步骤 21。

## 2026-10-08：扩展字段身份推断修复与真实原生采集

- 安装版 MCP 0.7.29 / 扩展 0.1.9 已真实连接；原有京东流程的原生采集预览返回 29 行，
  精确运行 DataCollectionV5 返回本次 59 行四列，代码 ID 与新鲜缓存双重验证通过。
- 数据回读：无缺列/空字段，3 条整行重复，1 条价格为“待发布”；销量/评价/浏览量保留为热度原文。
- 修复扩展根据首卡片子节点位置误读广告文案的问题。源码扩展为 0.1.10，逐行核验字段身份；
  Python 只绑定已核验的 fieldSelector，不再自行删除位置条件。
- JavaScript 14 passed；Python browser_mcp/browser_bridge/browser_sharing 37 passed（11.37 秒）；
  发布源码版本检查 1 passed。Ruff、Node 语法、git diff --check 通过。
- 新覆盖混合卡片布局、广告插入、包装层变化、缺失/重复字段、行外多余命中、危险/丢失字段、
  纯位置证据、selector 与选点不一致，以及旧响应/不完整逐行证据拒绝绑定。
- 修复源码未发布、未加载到实际 Chrome；没有把夹具回归计为新版现场成功。
  现场是人工指导的已有流程验证，不代表 Luna/Qwen、100 行、分页或 Excel 测试通过。
- 证据：`_tmp/jd-extension-live-20261008/`；完整报告：
  `docs/MCP_BROWSER_EXTENSION_LIVE_EVALUATION_20261008.md`；人工过程步骤 20。

## 2026-10-08：重启后手工流程与调试任务选择修复（未发布）

- 导航/任务选择定向测试：11 passed、423 deselected（18.89 秒）。
- 调试与流程编辑回归：83 passed（51.30 秒）；Ruff 与 git diff --check 通过。
- 新增 7 个场景：调试排除恢复备份并拒绝仅备份的目录；无计划手工流程打开页面；
  保存 URL 优先于过时计划；变量对象、变量名、非 HTTP URL 和空 URL 不打开浏览器。
- 现场只读源码诊断确认 `_native_task_file` 和 debug `_native_task` 选择同一根任务、7 步。
- 实际已安装 0.7.29 MCP 的首组件精确运行成功，代码只包含 ybQEw，缓存为本次更新；
  此证据不代表以上未发布源码已完成原生调试验收。独立扩展仍 extension_missing。
- 日志：`_tmp/restart-followup-20261008.txt`、`_tmp/restart-followup-20261008.xml`；
  人工步骤记录：`docs/HUMAN_GUIDED_NATIVE_COLLECTION_LOG_20260911.md` 步骤 19。

## 2026-10-08：0.7.29 发布与安装完整性

- 最终完整 pytest：655 passed（249.35 秒）；浏览器 JavaScript 7 passed；Ruff、Node 与 PowerShell 语法检查通过。
- 实际冻结 EXE initialize/tools/list 与源码 schema 完全匹配：full=67、rpa-authoring=43；
  CLI 版本、Native Host bootstrap、隔离 browser status 与关闭 stderr 均通过。
- 发布清单核对 17 个最终分发文件；缺失扩展、篡改文件、旧版本、越界路径或漏掉核心工具均拒绝。
- 真实 npm 首次安装与重复升级使用含中文的隔离目录，保留原连接密钥和端口；没有修改真实注册表。
- 新增无 curl 的 Windows PowerShell 下载回退测试，覆盖下载、SHA-256、解压、安装核验。
  另验证 install/uninstall 遇到非本产品目录时拒绝并完整保留用户文件。
- 本节为协议与安装验收，不代表京东原生采集、Luna/Qwen 业务测试成功。
- 原始输出：`_tmp/release-0.7.29-final.txt`；JUnit：`_tmp/release-0.7.29-final.xml`；
  安装记录：`_tmp/install-smoke-0.7.29.txt`。

## 2026-10-08：多进程浏览器桥接与断线恢复

- 最终完整 pytest：641 passed（168.28 秒）；浏览器定向回归 31 passed（7.62 秒），新增 14 项。
- 同端口鉴权 relay 支持 owner/shared；覆盖双调用方乱序返回、共享调用方退出、持有者退出后接管、
  2 MB 图片、扩展业务错误保留、错密钥/浏览器 Origin 拒绝、旧进程占用端口提示。
- 覆盖请求发出后断线/超时不重放、迟到响应不串线、扩展在等待期间连上后执行原请求，
  以及发现/读取之间持有者退出、发送瞬间连接丢失/超时。未知执行结果明确返回 unknown。
- 两个真实 stdio MCP 子进程使用受控扩展协议对端完成共享与接管验收，退出 stderr 均为空。
  该测试没有启动 Chrome 或原生 RPA，不能据此判定网页采集成功。
- 实际安装配置的独立源码协议检查确认 owner/shared，但真实 Chrome 未运行、扩展未连接；
  Computer Use 只提供内置浏览器，尝试启动 Chrome 返回 unavailable。现场限制已写入
  `docs/MCP_QUALITY_REVIEW_20261008.md`，原始状态在 `docs/MCP_BROWSER_SHARING_LIVE_20261008.json`。
- 安装核验：本机 EXE 标称 0.7.28、实际 full=56 且无 shizai_browser_*；源码 full=67、
  authoring=42。本轮未发布/替换 EXE。Ruff、compileall、git diff --check 通过。
- 最终回归原始输出：`_tmp/mcp-quality-20261008.txt`；JUnit：`_tmp/mcp-quality-20261008.xml`。

## 2026-09-21：键盘组件焦点守卫与网页对象校验放宽

- 完整 pytest：627 passed（139.97 秒），新增 10 项回归。
- 全局键盘组件（`Keyboard_*`）没有元素绑定，改为在预检中校验目标 Chrome 窗口的前台焦点：
  新增 `_foreground_window_id`、`_activate_window`、`check_chrome_window_focus`；焦点无法确认时
  返回 `window_focus_unverified`（下一步 `debug_capabilities`），目标窗口无法识别时返回
  `target_window_unknown`（下一步 `scan_page`，且不枚举窗口），两种情况都不发送调试 `continue`。
  5 项覆盖未知目标不枚举、前台命中、激活后确认、激活被拒和 Chrome 缺失。
- 静态校验放宽两处误报：页面组件接受上游浏览器节点自身的输出变量、计划 `outputs` 与固定默认
  `网页对象`；计划 `outputs` 少于组件实际写入槽位时，只要多出的槽位是该组件的固定原生输出就通过。
  未知对象名和真正的多余槽位仍失败，并写入 `accepted names` 与 `surplus native slots` 明细。
- 工具数 66→67（full）、41→42（rpa-authoring）；声明字符数 full 45,894（预算 46,600）、
  rpa-authoring 30,001（预算 30,600，为 full 的 65.4%）。`shizai_rpa_save_editor` 与未保存门禁
  在上一个提交落地，本次补齐 `tests/TEST.md` 契约条目和 `RPA_AUTHORING_INSTRUCTIONS` 的保存说明。
- `scripts/probe_collection_selector_variants.py` 不再硬编码个人工作区路径，改为必需的位置参数；
  `tests/test_browser_bridge.py` 的既存 I001 已修复。Ruff、compileall 通过。

## 2026-09-20：MCP 主流程步骤删除（remove_step）

- 完整 pytest：614 passed（142.06 秒）。新增 6 项 `remove_step` 回归：只读预览（不落盘、
  不驱动原生 UI，且以 `publish_ui=False` 调用真实校验器）、revision 冲突与依赖拒绝
  （`step_has_dependents` 返回下游步骤和改绑提示）、未知/嵌套/主容器/最后一步的目标拒绝、
  真实删除并同步 statements/实体/计划与 `mcpUseJs*` 标记、仍有其它 JS 节点时保留标记、
  MCP 写入门禁与 CLI/MCP 预览等价。
- 只读预览不再产生 UI 副作用：`validate_rpa_project` 与 `validate_rpa_flow` 新增
  `publish_ui` 关键字参数，默认 `True` 保持所有写入路径行为不变。
- 工具数 65→66（full）、40→41（rpa-authoring）；声明字符数 full 45,751（预算 46,000）、
  rpa-authoring 29,603（预算 30,000，且低于 full 的 67%）。压缩的是冗长说明，没有放宽预算。
- Ruff（改动文件）、compileall、全部 114 个 CLI 帮助入口通过。仓库中另有 2 处既存
  `I001` 导入排序问题（`scripts/probe_collection_selector_variants.py`、
  `tests/test_browser_bridge.py`），与本次改动无关。

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

- The default MCP profile exposes exactly 67 uniquely named tools; the optional
  rpa-authoring profile exposes 42 and rejects calls to unadvertised tools.
- Every public schema rejects unknown fields and has a matching dispatcher branch.
- Full tool definitions stay below 46,600 characters; rpa-authoring stays below
  30,600 characters and 67% of the full profile. Full server instructions stay
  below 2,100 characters. Character counts are not model token measurements.
- Read operations redact credential-shaped values.
- Every mutating operation requires `allow_write=true`.
- Native parameter preview uses `dry_run=true` without write permission. It
  performs parameter encoding only, never claiming static or runtime validation.
- Native step removal is revision-driven and refuses a step whose output variable
  is still read downstream unless `allow_dependents=true`; its preview performs no
  persistence and no designer UI.
- Saving the active editor is revision-driven and re-reads the dirty flag,
  revision and step list afterwards; a still-dirty editor returns
  `save_unconfirmed` instead of reporting success.
- A full run or a debug start returns `editor_unsaved_changes` while the active
  editor has unsaved changes; an absent editor does not block.
- Global keyboard components (`Keyboard_*`) verify the target Chrome window's
  foreground focus instead of an element binding. An unconfirmed focus returns
  `window_focus_unverified` and an unidentifiable target returns
  `target_window_unknown`; neither sends a debug `continue`.
- Page components accept the upstream browser node's own output variable, the
  planned output names and the canonical `网页对象` default, and a plan may name
  fewer outputs than the component writes only when the surplus slots are that
  component's canonical outputs.
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
