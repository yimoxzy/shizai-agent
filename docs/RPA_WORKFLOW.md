# 原生 RPA 工作流

## 1. 路由与分析

当前客户端模型先理解用户原句；请求类型不明确时再调用 `shizai_route_intent`。需要结构化证据时可调用 `shizai_rpa_analyze_request`，但它不是意图或流程决策器。分析结果包含稳定的 `analysis_id`、原文证据、未知项和登录交互提示，但不包含组件步骤、selector 或 JavaScript。

```text
shizai_route_intent(query)
-> shizai_rpa_analyze_request(query)
```

如果使用了分析结果，保留其中的 `query` 和 `analysis_id` 便于追踪；模型仍可以依据用户上下文自行拆解和改写组件图。若模型上下文压缩，只需在后续工具参数中保留原始 `analysis_id`（以及原始 `query`，如仍可用）；MCP 会从当前会话缓存恢复完整分析，并返回 `analysis_rehydrated=true`。

调用 `shizai_rpa_analyze_request` 后，原生小卡立即显示“正在识别任务意图并拆解流程步骤”。此时只做意图识别和任务拆解：不播放接管倒计时、不显示蓝紫接管边框、不打开或操作浏览器。

## 2. 查询实时组件并制定计划

按模型需要调用 `shizai_rpa_component_catalog` 或 `shizai_rpa_components`。返回的 `capabilities`、`operation_types` 和 `planning_patterns` 是能力参考；客户端模型据此提交本次请求专属计划，MCP 不选择站点模板或追加隐藏步骤。

例如请求 N 条结果时，N 是最终有效行数完成契约，不是 MCP 指定的页面大小。目录会明确告诉模型哪些组件支持组件内部分页；模型根据页面证据、目标数量和其它约束决定是否启用该能力。如果当前页面已经满足 N 条，模型可以选择单页采集，不需要绑定分页控件。可以使用组件内置分页，也可以使用显式下一页、循环、JavaScript、下载或其他已安装能力；MCP 不把某个数量写成强制翻页规则，也不规定唯一分页执行者。

对于 `WebBrowser_DataCollectionV5`，目录中的 `capabilities` 会包含 `paginate`，并在
`configuration_hints.pagination_execution` 中说明它支持 `next-page` 和 `load-more` 两种
原生控制。模型选择这个能力后，才通过 `selector_page` 或 `selector_more` 绑定实时页面元素；
如果模型选择单页、显式点击循环或 JavaScript，就不要为了满足目录提示而额外绑定分页元素。

分析器只把用户明确提到的翻页记录为可选偏好；单独出现“采集 N 条”不会生成分页约束。数量目标始终由运行结果验收，分页能力由实时组件目录提供给模型选择。

目录查询可以按角色或能力筛选。工作流同时需要打开浏览器、搜索和采集时，`capability_mode=any` 通常更容易发现可组合的组件，但客户端模型也可以按自己的拆解多次查询。`planning_guidance.collection` 中的候选和推荐只用于匹配，不能替代模型对整套组件契约的判断。

目录响应顶部的 `role_selection_contract` 是决策参考：模型可以把原子操作映射到一个或多个组件，也可以选择能够直接覆盖多个操作的组件。组件的真实参数、输出和元素要求仍必须遵守；角色顺序、分页归属和是否拆分步骤由模型决定。

目录的 `planning_guidance.collection.selection` 是候选提示：`status=ready` 时可以参考
`recommended_component`，但不要求使用它；`status=missing` 时可以组合其他实时组件或报告
能力缺口。JavaScript、循环和显式页面操作只要有真实契约和可验证元素，同样是合法方案。

组件选择还有一条软建议：如果实时目录中存在已安装、可物化且覆盖当前原子操作的原生组件，
模型先评估原生组件；结构化网页采集优先评估 `WebBrowser_DataCollectionV5`。该建议的返回标记是
`advisory`/`warning_only`，并不要求固定组件。模型选择 JS、下载、循环或其它组合时，记录原因并
按所选方案的静态校验、运行证据和输出契约验收即可。

模型可以参考分析结果里的 `planning_requirements.planner_checkpoint` 和目录中的
`planning_guidance`，但这些字段是建议，不是固定脚本。预检只拒绝不真实的组件、非法参数、
悬空引用、未验证元素或其他安全问题；如果方案可能无法达到数量目标，返回可修复的 warning/验收证据，
由模型决定是调整采集、分页、循环还是输出步骤。

计划步骤的最小写入契约只有：

- `id`、实时目录中的真实 `component` 和完整 `config`。

`purpose`、`operation_ids`、`depends_on`、`inputs`、`outputs`、`element_role`、
`loop`、`condition` 和 `retry` 是模型编排上下文；需要时再提供，不构成固定
的步骤形状。MCP 只校验实际提供的引用、参数、元素和运行安全条件。

使用 `WebBrowser_RunJsInIeBySelector_v3` 时，客户端模型必须提供当前页面专属的 `jsCode`、`output_variable` 和 `result_mode`。脚本必须保留 `SHIZAI_WAITING_FOR_USER:login_required` 与 `SHIZAI_WAITING_FOR_USER:page_challenge` 两个暂停标记。MCP 不提供默认网页脚本。

## 3. 预检计划并创建真实应用

需要时调用只读薄预检：

```json
{"analysis": {"analysis_id": "..."}, "plan": {"analysis_id": "...", "steps": []}}
```

工具为 `shizai_rpa_validate_plan`。它只检查计划形状、实时组件/参数和安全，不判断首节点、操作覆盖、分页方式或步骤顺序。失败时依据具体原生错误修复现有计划，不重写用户意图或自动替换策略。

调用 `shizai_rpa_start_design` 时提交：

```json
{
  "query": "ORIGINAL_USER_REQUEST",
  "analysis": {"kind": "rpa_request_analysis", "analysis_id": "..."},
  "plan": {
    "analysis_id": "...",
    "name": "REQUEST_SPECIFIC_NAME",
    "target": {"url": "https://TARGET/"},
    "steps": [],
    "output_contract": {}
  },
  "open_browser": false,
  "allow_write": true
}
```

`analysis`（如使用）和 `plan.steps` 在真实调用中传完整对象。MCP 不重新规划原句；写入前只检查薄边界，设计器/共享 CLI 再检查原生参数、变量、元素和运行环境。实现策略和组件顺序完全以客户端模型提交的计划为准。

预检通过后，`shizai_rpa_start_design`：

- 创建真实 normal 应用；
- 一次写入客户端模型选择的完整组件图；
- 把原始分析与完整计划持久化到 `szrpa.json`；
- `flow_ref` 和 `project_ref` 指向该应用目录；
- 发布“流程设计完成，准备开始实际操作”状态；
- 不执行浏览器 I/O。随后仅在计划包含需要恢复浏览器目标的步骤时调用 `shizai_rpa_open_browser`；该调用才播放 5 秒原生接管倒计时、执行计划中的导航步骤并初始化不带红色拾取层的 DeepCapture。

`open_browser=false` 是默认值。计划配置传语义 JSON 值，例如 `url: "https://example.com"`，不要把目录里的 native 引号编码复制为字符串内容。

## 4. 按计划逐步写入

每次从返回的 `next_action` 获取下一组件，不自行套用固定顺序。需要元素时：

```text
shizai_rpa_add_step（先写入元素组件，binding_status=unbound）
-> shizai_rpa_capture_windows
-> shizai_rpa_scan_page(project_ref=..., step_id=..., query=NEXT_ACTION.element_capture.role)
-> 必要时 shizai_extension_start_capture(project_ref=..., step_id=..., allow_write=true)
-> shizai_rpa_capture_elements(project_ref=..., step_id=..., element_ids=[...], scan_token=...)
-> 自动导入 selector、绑定同一组件并关闭红色拾取层
-> shizai_rpa_validate_step
-> shizai_rpa_debug_capabilities
-> shizai_rpa_debug_start（或复用已有会话）
-> shizai_rpa_debug_command(action=next)
-> shizai_rpa_debug_status + 页面/运行证据
```

`scan_token` 固定一次页面快照。页面跳转、刷新或登录后重新扫描。元素必须带页面身份和稳定 selector；只有坐标、临时引用、纯 `nth-child/@index`、空 selector 的记录会被拒绝。

重新绑定商品字段时，CLI 必须保留任务中已经捕获的分页 selector。`szrpa.json` 中的
`region/page_type_num` 只能表示模式状态，不能在没有具体 selector 时覆盖任务绑定；只有模型明确提交具体分页 selector 或明确改为单页时，才允许改变分页槽位。

红色遮罩只属于官方交互式拾取器。普通识别不调用 `shizai_extension_start_capture`；仅在元素组件已经写入且处于 `unbound`、页面树证据不足、确实需要用户可见的点击/悬停拾取时使用。启动时必须同时传入 `project_ref` 和该组件的 `step_id`。捕获成功、失败、取消或超时后都立即关闭红色拾取层。模型生成的 selector 也必须先校验再绑定。

## 5. 登录与页面挑战

预检或运行返回 `login_required` / `page_challenge` 时进入 `waiting_for_user`，暂停当前步骤并提示用户在现有窗口完成登录或验证。只有用户明确委托代登录并在当前对话提供当前会话所需信息时才执行；登录信息不得进入流程、项目、元素库、日志或 MCP 参数。用户确认后重新预检和扫描，再继续同一个计划。

## 6. 增量验证门

`shizai_rpa_add_step` 和 `shizai_rpa_validate_step` 返回同一个共享核心字段
`verification_gate`。它把“写入组件”“静态合法”和“真实运行有效”明确分开：

1. 先确认 `step_validation.valid=true`；失败时只修复当前步骤。
2. 按 gate 调用 `debug_capabilities`、`debug_start`/复用、`debug_command(action=next)` 和
   `debug_status`，让设计器 RuntimeHost 真正执行当前组件。
3. 用页面截图/OCR、当前页面状态或运行输出确认预期效果，再追加下一组件。

`rpa-debug-next` 在确认跨过当前组件边界或当前组件进入终态后返回
`runtime_verification`；CLI/MCP 会把它适配回同一个 `verification_gate`，其状态变为
`verified`。这表示 RuntimeHost 确实执行了该组件；网页组件仍应由模型结合页面证据确认预期可见效果。

静态通过、运行请求被接受、DAP 已连接或单纯停靠都不能单独证明组件成功。
如果当前客户端不具备设计态调试能力，gate 会明确显示待验证；不能假装已通过。
CLI 使用对应的 `workflow rpa-validate-step`、`workflow rpa-debug-*` 命令。失败时保留同一个
`project_ref` 修复，不创建第二个流程。OCR只用于可见结果判断，不生成 selector，也不替代官方拾取器。

## 7. 完成组件计划

每次 `shizai_rpa_add_step` 后检查 `step_validation.valid` 和
`verification_gate.runtime_validation.verified`。两者未同时满足时，不要按结构性的
`next_action` 追加组件。所有计划组件写完并逐个验证后，`next_action.tool` 才应变为
`shizai_rpa_validate_flow`。输出文件、工作表、字段和行数只来自 `plan.output_contract`，不得由 MCP 默认补充。

## 8. 静态校验与物化

```text
shizai_rpa_validate_step
-> shizai_rpa_validate_flow
-> shizai_rpa_validate_project
-> shizai_rpa_materialize(allow_write=true)
-> shizai_rpa_sync_diagnose
```

`validate_step`、`validate_flow` 和 `validate_project` 都是静态检查。`materialize` 幂等刷新 `projects/flow_modules`、`flow.py`、`global_data.py`、sourcemap、变量信息和 file index。`sync_diagnose` 检查真实 applicationId/packageId、task id、索引时效和 RuntimeHost 插件。设计期 DeepCapture 与运行期浏览器插件是两条独立通道；读取 `runtime.runtime_browser_plugin_status`，不要把设计期捕获成功当作运行期插件已就绪。

## 9. 组件级调试

```text
shizai_rpa_debug_capabilities
-> shizai_rpa_debug_start(project_ref, start_from=1, allow_write=true)
-> shizai_rpa_debug_status
-> shizai_rpa_debug_command(action=next, allow_write=true)
-> shizai_rpa_debug_stop(allow_write=true)
```

`action` 支持：

- `next`：严格 step-over 当前组件，并在下一组件调用前的精确边界停靠；
- `continue`：继续到下一个停点或终态；
- `step_in`：进入当前代码调用；
- `step_out`：跳出当前代码调用。

元素组件执行前，调试器自动运行 `runtime_preflight`。如果返回 `page_challenge`、`login_required`、`page_identity_changed`、`document_missing` 或 `chrome_not_found`，当前组件保持停靠。登录与验证默认暂停并询问用户；仅当用户明确要求代登录且在当前对话提供所需信息时才执行，登录信息不得写入流程、项目、元素库、日志或 MCP 参数。用户确认完成后先重新预检和扫描，再调用 `debug_command(action=next)`。

如果预检返回 `page_identity_changed`，恢复时沿用原 `project_ref` 和当前 Chrome，
先重新扫描当前 Document，再用同一 `scan_token` 捕获元素。已绑定步骤默认不能被覆盖；
只有明确确认这是页面变化后的替换时，才在 `shizai_rpa_scan_page` 和
`shizai_rpa_capture_elements` 传 `allow_rebind=true`，然后重新做静态校验和单步验证。

## 10. 普通运行与证据

```text
shizai_rpa_run(project_ref, wait=true, allow_write=true)
-> shizai_rpa_status(project_ref)
```

MCP 运行入口和直接 CLI `workflow rpa-run` 都会先关闭 DeepCapture 元素拾取，避免设计期红色遮罩残留；等待模式的终态或异常返回还会执行一次最终 UI 清理。直接 CLI 将
`stale_ui_cleanup` 和 `final_cleanup` 写入结构化结果，便于确认清理动作是否发出；
这些字段是 RPC/本地状态证据，不是屏幕像素级验证；出现 warning 或仍看到红色拾取层时，
应立即调用 `rpa-browser-session-stop`。
`--no-wait` 会延迟最终清理，直到后续 `rpa-status`/`rpa-stop` 完成运行会话收尾。
需要结束整个 DeepCapture 会话时仍使用 `extension stop-capture` 或
`rpa-browser-session-stop`。`Excel_Cs_NewV4` 默认只创建 `Sheet1`，因此没有显式新建工作表组件时，`Excel_Cs_WriteRange.sheet` 必须是 `Sheet1`。

`can_run` 表示最近的运行期插件证据未阻断，`can_attempt_run` 表示流程静态条件已满足、可提交一次实时探测。历史插件错误不会永久锁死重试；运行监听每秒检查本次新增的 RuntimeHost 日志。出现 `010400100000996`（Chrome 插件未启用）或 `010400100000964`（插件未安装）时立即返回 `runtime_plugin_unavailable` 和 `runtime_blocked` UI，不再等到完整运行超时。

运行完成后检查：

- `status` 是基于 RuntimeHost 日志、输出文件和清理结果推导的真实状态，不是单纯
  `accepted=true` 或 `submitted`；运行状态还包括 `stopping`、`stopped` 和
  `cleanup_timeout`。
- RuntimeHost 日志没有插件或变量绑定错误；
- 输出文件创建时间晚于本次运行；
- Excel/CSV 列头与请求一致；
- 数据行数达到用户要求的最小值（“至少 N 条”允许多于 N 条）；
- 必填列非空；
- 商品链接仍指向详情页；
- 抽样值符合页面实际内容。

停止普通运行使用 `shizai_rpa_stop(allow_write=true)`；停止 DAP 会话使用
`shizai_rpa_debug_stop(allow_write=true)`。两者都会先记录 `stopping`，再通过官方
停止 RPC 清理；停止 RPC 超时会记录 `cleanup_timeout`，不会把未确认的运行伪装成
`stopped`，也不会直接杀 RuntimeHost 进程。`shizai_rpa_status` 只读本地状态，发现
遗留 `stopping` 超过清理窗口后会返回 `cleanup_timeout`，下一步应重试 stop。

## 11. 非浏览器原生 RPA

先把请求拆成原子操作，再调用 `shizai_rpa_component_catalog(scope="all", detail="summary")` 搜索候选。选定 type 后使用 `component_type + detail="contract"` 获取参数和输出变量。业务组件必须检查 `availability`、`requires_import` 和当前项目导入状态。

组件目录没有覆盖的单个计算可使用短代码节点。文件打开、Excel 读写、数据库、循环、条件、变量和保存等已有组件能力，不放进一个大代码节点。
