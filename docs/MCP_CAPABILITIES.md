# Shizai Agent MCP 工具目录

版本：`0.7.29`

源码提供可选环境变量 `SHIZAI_MCP_TOOL_PROFILE=rpa-authoring`：43 个原生 RPA 工具，
默认 `full` 为 67 个。`shizai_rpa_read_flow` 新增 `detail=edit`、`step_id`；
`shizai_rpa_update_step` 新增 `dry_run`、`expected_revision`，预览不需要 allow_write；
`shizai_rpa_remove_step` 按 `expected_revision` 删除主流程步骤，预览不落盘、不驱动原生 UI；
`shizai_rpa_save_editor` 保存当前活动 editor，并在保存后复核未保存状态和 revision。
工具集配置、限制和编辑协议见 [中小模型流程设计](SMALL_MODEL_WORKFLOWS.md)。

> 架构基线见 [MCP_ARCHITECTURE.md](MCP_ARCHITECTURE.md)。当前 MCP 的职责是提供实时组件契约并校验结构、参数、元素和安全边界；意图理解、任务拆解、组件组合、循环和分页策略由调用方大模型决定。本文中带有“推荐/规划指导”的字段是建议，不是唯一流程协议。

## 连接模型

MCP 通过 stdio 运行，业务请求连接本机实在设计器。客户端模型负责理解请求、视觉判断和组件编排；MCP 负责实时目录、确定性契约、设计器写入、校验和执行证据。服务不内置流程生成模型。

所有 input schema 都拒绝未知字段。写入、运行和调试操作要求 `allow_write=true`；缺少该字段时会在调用真实后端前失败。

## 原生 UI 反馈

| 工具 | 用途 | 写入 |
| --- | --- | --- |
| `shizai_ui_feedback` | 触发当前实在 Agent 的 TARS 小卡、完整运行遮罩或原生截图拾取演示 | 是 |
| `shizai_rpa_ui_status` | 读取最近一次 RPA 捕获/运行阶段 | 否 |

`shizai_rpa_start_design` 先显示识别/设计小卡并完成计划校验和持久化；只有即将真实打开浏览器或操作桌面时才播放 5 秒原生接管倒计时，随后开启设计器自带的全屏蓝紫呼吸边框。流程物化成功、失败或主动结束时会关闭小卡和边框。

`shizai_ui_feedback` 的动作参数：

```json
{
  "action": "scan_demo",
  "mask_type": "white",
  "interval": 1.5,
  "allow_write": true
}
```

动作说明：

- `small_card`：原生 TARS 卡片，`state` 可用 `takeover`、`designing`、`scanning`、`capturing`、`validating`、`running`、`waiting`、`ready`、`success`、`error`、`hidden`。
- `mask`：原生 `MaskAppService.showRunMask/closeRunMask`，会覆盖首页、当前编辑器和对话窗口；关闭时用 `close_now=true`。
- `scan_demo`：在同一桌面通道中按设计器原版顺序组合窗口最小化、完整运行遮罩、`Element.ScreenshotCapture` 原生拾取和小卡阶段更新，默认自动关闭；传 `keep_open=true` 可保留原生拾取状态。

此工具只控制设计器自带 UI，不负责元素采集和流程写入。元素扫描仍走 `shizai_rpa_scan_page`，原生 selector 捕获仍走 `shizai_rpa_capture_elements`。

红色浏览器遮罩只代表官方交互式元素拾取，不代表识别、设计、校验或运行。普通流程设计使用无红层 DeepCapture 页面树。必须先用 `shizai_rpa_add_step` 写入元素组件，使其进入 `binding_status=unbound`，再按同一 `project_ref + step_id` 扫描或启动拾取；成功、失败、取消和超时都会关闭红层。原生拾取不适用时，客户端模型也可以生成真实 selector，经过校验后绑定到同一组件。

`shizai_rpa_capture_windows`、`shizai_rpa_scan_page`、`shizai_rpa_capture_page_fields` 和 `shizai_rpa_capture_elements` 使用只读 DeepCapture 通道：只更新小卡进度，不播放 5 秒接管倒计时，也不打开接管边框或红色拾取层。返回的 `native_ui.access.mode=capture_read`、`countdown_started=false` 可用于测评和排障。只有打开/改变页面、官方交互式拾取、键鼠操作或运行组件时才进入接管状态。

## 基础与捕获状态

| 工具 | 用途 | 写入 |
| --- | --- | --- |
| `shizai_auth_status` | 设计器连接、登录和云端凭据就绪状态 | 否 |
| `shizai_route_intent` | 分类 `agent_workflow`、`browser_rpa`、`native_rpa` | 否 |
| `shizai_rpa_analyze_request` | 提取原始请求中的操作、字段、数量和交互策略，不选择组件 | 否 |
| `shizai_rpa_validate_plan` | 薄层只读预检：检查 JSON、实时组件/参数、字段类型和安全，不规定流程策略 | 否 |
| `shizai_extension_status` | Chrome 扩展和 DeepCapture 就绪状态 | 否 |
| `shizai_extension_start_capture` | 为已写入的 `unbound` 组件启动原生拾取，必须传 `project_ref + step_id` | 是 |
| `shizai_extension_stop_capture` | 停止原生拾取 | 是 |

## Agent 工作流

| 工具 | 用途 |
| --- | --- |
| `shizai_apps` | 列出设计器中的 Agent 应用 |
| `shizai_create_agent` / `shizai_update_agent` | 创建或更新 Agent 元数据 |
| `shizai_components` | 读取节点默认配置、变量和工具参数 |
| `shizai_generation_context` | 获取当前设计器的组件优先规划契约 |
| `shizai_draft` | 读取现有工作流草稿 |
| `shizai_validate_draft` | 校验节点、边、变量选择器和组件类型 |
| `shizai_apply_flow` | 校验、保存、回读，并可选运行或发布 |
| `shizai_run_draft` | 运行已保存草稿 |
| `shizai_runs` / `shizai_logs` / `shizai_tracing` | 查询真实运行、消息日志和节点追踪 |
| `shizai_datasets` | 查询当前工作区知识库 |

`shizai_create_agent`、`shizai_update_agent`、`shizai_apply_flow` 和 `shizai_run_draft` 是写入或执行操作。Agent graph 应由当前客户端模型根据实时默认配置生成，不根据显示标题猜 type 或变量形状。

推荐顺序：

```text
shizai_generation_context -> shizai_components -> shizai_draft
-> shizai_validate_draft -> shizai_apply_flow
-> shizai_runs / shizai_logs / shizai_tracing
```

## 原生 RPA

### 独立浏览器视觉桥接

| 工具 | 用途 |
| --- | --- |
| `shizai_browser_status` / `shizai_browser_tabs` | 检查独立扩展并列出普通网页标签页 |
| `shizai_browser_activate_tab` | 聚焦目标 Chrome 窗口和标签；截图提示窗口不在前台时调用 |
| `shizai_browser_capture_viewport` | 返回带 URL、视口和 revision 的 MCP PNG 图片 |
| `shizai_browser_inspect_points` | 把少量视觉坐标在浏览器本地命中 DOM，并可绑定原生步骤 |
| `shizai_browser_infer_collection` | 从同一卡片的字段 selector 推断重复行，并可绑定 DataCollectionV5 |
| `shizai_browser_action` | 在设计证据页执行受限点击、填值或按键；原生调试仍负责最终验证 |

扩展不会把完整 DOM 树发送给模型。模型直接按返回 PNG 的像素坐标取点，扩展会在本地处理高 DPI 缩放；控制元素可传 `prefer_interactive=true` 提升到按钮、链接或输入框。截图变化后必须重新取图；URL、tabId 或 pageRevision 不一致时坐标请求会失败。独立扩展优先用于视觉定位，原客户端 DeepCapture 作为兼容回退。

源码支持多个 MCP 窗口共享 `127.0.0.1:18768`：首个进程持有扩展连接，其它进程经同端口的鉴权 `/mcp` 通道复用。`browser_status.connection_mode` 返回 `owner` 或 `shared`。持有者退出后，下一次调用会重新选出持有者，扩展自行重连；关闭共享调用者不会关闭扩展。

`bridge_in_use` 现在表示端口持有者不支持共享，或安装密钥不一致，应更新/重启旧 MCP 或检查配置。只有缺少配置时才建议安装；桥接已监听、扩展未连接时提示打开 Chrome 并检查扩展启用状态。请求发出后断线/超时返回 `execution_outcome=unknown`，不自动重放点击或输入；先检查当前页面，再决定下一步。页面版本与截图证据仍要按原规则验证，共享连接不代表多个模型可以同时修改同一页面而不冲突。

### 设计与元素

| 工具 | 用途 |
| --- | --- |
| `shizai_rpa_start_design` | 创建客户端原生应用并一次写入已预检的完整组件图；默认不打开浏览器 |
| `shizai_rpa_open_browser` | 从已保存导航步骤快速打开真实页面供绑定；不把页面打开误报为原生节点验证成功 |
| `shizai_rpa_capture_windows` | 紧凑读取当前 Chrome 窗口 ID、标题和扩展就绪摘要 |
| `shizai_rpa_capture_page_fields` | 在同一页面快照中对齐并绑定结构化采集字段 |
| `shizai_rpa_scan_page` | 从 Document 扫描语义候选并返回 `scan_token` |
| `shizai_rpa_capture_elements` | 从同一快照捕获完整 selector，可选直接导入/绑定 |
| `shizai_rpa_runtime_preflight` | 执行元素组件前检查 Chrome、Document、页面身份、登录和验证页 |
| `shizai_rpa_components` | 读取网页 RPA 组件契约和当前下一步 |
| `shizai_rpa_component_catalog` | 搜索本机 builtin 和业务扩展组件 |

### 写入与校验

| 工具 | 用途 |
| --- | --- |
| `shizai_rpa_add_step` | 追加一个真实 native 组件，并返回 `step_validation` |
| `shizai_rpa_update_step` | 更新已有步骤参数并重新校验 |
| `shizai_rpa_remove_step` | 按 `expected_revision` 删除一个主流程步骤并重新物化；输出变量仍被下游引用时返回 `step_has_dependents` |
| `shizai_rpa_save_editor` | 按 `expected_revision` 保存当前活动 editor；保存后仍未落盘时返回 `save_unconfirmed`，不谎报成功 |
| `shizai_rpa_set_step_elements` | 绑定已导入元素 |
| `shizai_rpa_read_flow` | 回读有序步骤、参数、绑定和下一步 |
| `shizai_rpa_validate_step` | 单步静态契约校验 |
| `shizai_rpa_validate_flow` | 整条流程静态校验 |
| `shizai_rpa_validate_project` | 项目文件、元素和 native task 校验 |

### 运行与调试

| 工具 | 用途 |
| --- | --- |
| `shizai_rpa_materialize` | 幂等刷新 RuntimeHost 运行树 |
| `shizai_rpa_sync_diagnose` | 检查应用/包/task 映射、索引和运行插件 |
| `shizai_rpa_reload_editor` | MCP 写入后按 revision 刷新当前 editor，并等待活动 editor 连续两次 ready |
| `shizai_rpa_run_component` | 精确运行一个组件，核对本次生成代码和变量缓存；采集缓存必须新鲜且非空 |
| `shizai_rpa_run` / `shizai_rpa_status` / `shizai_rpa_stop` | 普通运行、证据检查和停止 |
| `shizai_rpa_debug_capabilities` | 探测组件断点和会话复用能力 |
| `shizai_rpa_debug_start` | 启动一次真实 DAP 调试会话 |
| `shizai_rpa_debug_status` | 读取线程、栈帧和当前组件 |
| `shizai_rpa_debug_command` | `next`、`continue`、`step_in`、`step_out` |
| `shizai_rpa_debug_stop` | 断开并停止调试会话 |

`shizai_rpa_debug_command` 的调用形状：

```json
{
  "project_ref": "CLIENT_APPLICATION_PATH",
  "action": "next",
  "wait_seconds": 15,
  "allow_write": true
}
```

`next` 是严格的组件级执行：逐行 DAP step-over 当前生成代码，只在精确命中下一组件调用边界时完成，避免断点失效时越界执行下一组件。`continue` 会持续运行到下一断点；`step_in` 和 `step_out` 是代码级 DAP 动作。调试会话复用同一个 AppRunService、线程和 Chrome 窗口。

### 增量验证门

`shizai_rpa_add_step` 和 `shizai_rpa_validate_step` 都返回共享核心生成的
`verification_gate`。模型必须按以下闭环处理每个组件：

```text
add_step
-> step_validation.valid=true
-> debug_capabilities
-> debug_start（或复用）
-> debug_command(action=next)
-> debug_status + 页面/运行证据
-> 再追加下一组件
```

`verification_gate.runtime_validation.verified` 未为 `true` 时，不能把
`next_action` 当作追加许可。静态校验通过、运行请求 accepted、DAP 已连接或单纯停靠
都不是组件成功证据；失败时修复当前 `project_ref` 和当前步骤。CLI 侧使用对应的
`workflow rpa-validate-step`、`workflow rpa-debug-*` 命令。OCR 只用于确认可见页面效果，
不生成 selector，也不替代 DeepCapture。

当 `shizai_rpa_debug_command(action=next)` 确认跨过当前组件边界或当前组件进入终态时，
返回 `runtime_verification`，共享核心会将它适配到 gate 并标记为 `verified`。这只证明
RuntimeHost执行了当前组件；网页组件的预期可见效果仍需模型读取页面树、截图/OCR或输出证据确认。

## 项目与元素

| 工具 | 用途 |
| --- | --- |
| `shizai_project_list` | 按名称轻量查找本地设计器项目，默认最多返回 20 条 |
| `shizai_project_inspect` | 读取项目文件和元数据摘要 |
| `shizai_project_elements` | 回读元素目录和完整 selector |
| `shizai_project_import_elements` | 导入已验证元素 |
| `shizai_rpa_validate_elements` | 写入前校验页面身份和 selector 稳定性 |
| `shizai_rpa_validate_project` | 校验原生 RPA 项目 |

## 浏览器 RPA 顺序

```text
route_intent (可选)
-> analyze_request (可选，只提取证据)
-> component_catalog / components
-> client model creates any component graph
-> validate_plan (可选薄预检)
-> start_design(query + plan, open_browser=false)
-> open_browser
-> follow the persisted next_action exactly
-> scan_page / capture_elements / validate/import when a planned step needs selectors
-> step/flow/project 校验
-> materialize -> sync_diagnose -> run -> status
```

`shizai_rpa_validate_plan` 只检查计划形状、实时组件是否可物化、契约声明的参数名（目录提供时）、数组字段、语义配置值和安全；不检查首节点、操作覆盖、固定字段、固定分页组件或步骤顺序。原生 CLI/设计器在写入和运行时继续返回最终参数、变量、元素和环境错误。`shizai_rpa_start_design` 不接受站点、关键词或固定字段快捷参数。捕获工具读取当前 DeepCapture 会话，不会自行重启另一套浏览器。

## 完成标准

以下证据缺一不可：

1. 每次写入返回的 `step_validation.valid=true`。
2. flow 和 project 校验通过。
3. `sync_diagnose` 未报告过期索引；`runtime.runtime_browser_plugin_status` 不是 `disabled` 或 `not_installed`。`unknown` 表示尚无运行期握手证据，首次运行会实时探测。
4. `run`/`status` 显示终态成功。
5. 输出文件存在且内容正确；表格任务需检查列头、数据行数、必填列和样例值。

运行提交成功、静态校验成功或 DAP 正常断开，都不能单独证明业务结果正确。

## 中小模型精简返回

`SHIZAI_MCP_TOOL_PROFILE=rpa-authoring` 暴露 43 个工具，含组件追加/主流程插入；目录与创建回执固定使用 compact；
组件契约、步骤校验、项目身份和浏览器恢复信息仍保留。full 集保留 `response_mode=full/compact` 选项。
精简集完整静态检查通过后的顶层下一步指向 `shizai_rpa_run`，且明确仅在用户要求执行时调用；
原生 run 内部执行项目诊断，静态检查不替代运行验收。单步调试用于问题定位，不要求先完整调试再重复运行。

`validate_plan` 是不触发桌面 UI 的只读操作；无效计划可以持续修正，不再因两次错误强制退出。
疑似 `$name` 字面量会产生变量语法提示，实际引用使用 `{"variable":"name"}`。

运行结果的 `output_files[].text_evidence` 对 `.txt` 提供最多 8 KiB 的 UTF-8 内容预览。
验收全文应同时检查 `fresh_for_run=true`、`complete=true` 和实际预览内容；截断、脱敏或解码失败时不能宣称全文一致。
独立 Luna 会话实测见 [测评记录](LUNA_RPA_EVALUATION.md)。
