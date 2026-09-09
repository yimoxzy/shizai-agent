# Shizai Agent CLI 结构与 RPA 浏览器会话规范

版本：`1.0`（当前实现盘点与后续优化基线）
适用范围：本地 CLI、实在 Agent 桌面通道、Chrome DeepCapture、原生 RPA `.task`、RuntimeHost 和 Native DAP。
配套文档：`CLI_USAGE.md`（命令用法）、`MCP_ARCHITECTURE.md`（MCP/CLI 分层）、`RPA_WORKFLOW.md`（RPA 操作顺序）、`MCP_RULES.md`（规则编写位置和格式）。

本文不是某个网站的流程配方，也不规定京东等站点必须使用哪一种组件。本文规定 CLI 如何表达浏览器上下文、页面证据、元素 selector、原生组件依赖和运行期验证，作为后续 CLI 优化和回归测试的依据。

## 1. 设计目标

CLI 的职责是把大模型的流程计划可靠地交给实在 Agent：

1. 读取本机真实能力和组件契约。
2. 创建、读取、更新和校验原生流程。
3. 管理设计期浏览器会话和页面证据。
4. 把同一页面快照中的元素 selector 导入并绑定到组件。
5. 在独立的运行期通道中单步验证或完整运行。
6. 返回可判断的结构化状态、错误原因和下一步动作。

CLI 不应承担以下职责：

- 根据一句自然语言偷偷生成站点专用流程。
- 把未捕获或未验证的 `uia:*` ID 当成可运行 selector。
- 用设计期 DeepCapture 状态代替运行期 RuntimeHost/DAP 状态。
- 在设计器未确认写入成功时继续追加组件。
- 并发修改同一个设计器应用。

## 2. 总体架构

```text
用户请求
   |
   v
大模型：意图、组件选择、变量、顺序、分页策略
   |
   v
CLI 命令层：参数、JSON 输出、allow-write、项目锁、状态机
   |
   v
共享核心：组件目录、任务读写、元素目录、selector、校验、诊断
   |                         |
   |                         +--> Chrome 扩展 / DeepCapture
   |                              设计期窗口、Document、页面快照
   |
   +--> 实在 Agent 桌面 RPC
        应用、组件、设计器保存、RuntimeHost、AppRunService、DAP
                                      |
                                      v
                         原生 `.task` -> 生成代码 -> 运行日志/输出文件
```

当前代码入口：

| 层次 | 当前位置 | 主要职责 |
| --- | --- | --- |
| CLI 命令 | `shizai_agent/shizai_agent_cli.py` | Click 命令、参数、权限、JSON 输出、异常转译 |
| 项目核心 | `shizai_agent/core/project.py` | `.task`、元素库、组件计划、绑定、校验、物化 |
| 扩展桥 | `shizai_agent/core/extension_bridge.py` | DeepCapture、Chrome 窗口、Document、页面扫描和运行预检 |
| 调试会话 | `shizai_agent/core/debug_session.py` | AppRunService、DAP、组件级 `next/continue`、会话复用 |
| Agent/RPC | `shizai_agent/core/agent.py`、`cloud_api.py` | 实在 Agent 桌面和云端调用 |
| 组件目录 | `shizai_agent/core/component_catalog.py` | 读取安装版真实组件和参数契约 |
| 文档 | `docs/` | CLI、MCP、RPA、组件和 UI 反馈规范 |

## 3. 命令分层

### 3.1 环境和桌面通道

| 命令 | 作用 | 是否改变状态 |
| --- | --- | --- |
| `doctor` | 检查安装、数据目录、Agent Brain 和端口 | 否 |
| `agent auth-status` | 检查设计器通道、登录和云端就绪状态 | 否 |
| `agent rpc` | 调用已知桌面 RPC，默认只读 | 默认否 |
| `extension status` | 检查 Chrome 扩展和 DeepCapture 状态 | 否 |
| `ui` | 操作实在 Agent 原生小卡、遮罩和 UI 状态 | 视子命令而定 |

### 3.2 原生 RPA 命令

```text
workflow rpa-context
workflow rpa-component-catalog
workflow rpa-start-design
workflow rpa-open-browser
workflow rpa-browser-session-start
workflow rpa-browser-session-status
workflow rpa-browser-session-stop
workflow rpa-add-step
workflow rpa-reorder-steps
workflow rpa-read
workflow rpa-repair-flow
workflow rpa-capture-windows
workflow rpa-capture-tree
workflow rpa-scan-page
workflow rpa-capture-elements
workflow rpa-bind-elements
workflow rpa-capture-page-fields
workflow rpa-validate-step
workflow rpa-validate-flow
workflow rpa-validate
workflow rpa-materialize
workflow rpa-sync-diagnose
workflow rpa-debug-start
workflow rpa-debug-status
workflow rpa-debug-next
workflow rpa-debug-continue
workflow rpa-debug-stop
workflow rpa-run
workflow rpa-status
```

命令分为四类：

| 类别 | 示例 | 关键规则 |
| --- | --- | --- |
| 发现/读取 | `rpa-context`、`rpa-read`、`rpa-scan-page` | 默认只读，不应修改流程 |
| 设计/写入 | `rpa-start-design`、`rpa-add-step`、`rpa-reorder-steps`、`rpa-bind-elements` | 必须 `--allow-write`，同一项目必须串行 |
| 静态验证 | `rpa-validate-step`、`rpa-validate` | 不代表真实浏览器运行成功 |
| 运行/调试 | `rpa-debug-*`、`rpa-run` | 必须先完成物化和运行期预检 |

### 3.3 原生组件选择建议

CLI/MCP 对组件选择采用“原生组件优先、替代方案可用”的建议策略。模型读取实时目录后，
如果发现已安装、可物化且能力覆盖当前原子操作的原生组件，应先评估该组件；结构化网页采集
优先评估 `WebBrowser_DataCollectionV5`。这条策略是 `advisory`/`warning_only`，不是写入或运行的硬条件。

如果原生组件不适合当前页面、参数、输出或运行方式，模型可以选择 JS、下载、循环、条件或其它
已安装组件组合，但需要记录选择原因，并以所选方案的 `step_validation`、运行期证据和输出契约验收。
CLI 不会因为没有选择推荐组件而拒绝流程，也不会把这条建议改写为固定顺序、固定分页或固定站点配方。

模型可从以下三个返回面读取该建议：

1. `rpa_generation_context["business_rules"]`：完整中文规则，包含适用条件、必须、禁止、证据和失败处理。
2. `rpa_components["business_rules"]`：同一规则的共享副本，随流程组件状态返回。
3. `shizai_rpa_component_catalog` 的 `usage.implementation_policy`：目录级简要说明和替代方案提示。

## 4. 三种必须分开的上下文

### 4.1 流程项目上下文

`project_ref` 或 `flow_ref` 是设计器应用目录，当前通常形如：

```text
C:\Users\<user>\AppData\Roaming\Z-Factory\applications\<application_id>
```

它用于定位：

- 原生 `.task` 文件。
- `elements.json` 元素目录。
- `szrpa.json` 应用元数据。
- `main.sz`、`projects/flow_modules/*.py` 运行树。
- `file_index_700.json` 等设计器索引。

项目引用不等于 Chrome 窗口 ID，也不等于 DeepCapture 元素 ID。

### 4.2 设计期浏览器上下文

设计期由 Chrome 扩展和 DeepCapture 提供：

| 对象 | 含义 | 生命周期 |
| --- | --- | --- |
| DeepCapture 会话 | 扩展和实在 Agent 之间的浏览器检查通道 | 按项目/进程复用，停止后失效 |
| `wnd:*` | Chrome 原生窗口根节点 | 当前窗口/会话有效 |
| `Document` 节点 | 页面 DOM/UIA 树的可扫描根 | 页面跳转后应重新获取 |
| `uia:*` | DeepCapture 返回的候选元素 ID | 会话和页面范围内有效 |
| `scan_token` | 页面扫描快照标识 | 只能用于同一次页面快照 |
| `selector` | 可写入元素库、供运行时使用的完整 selector | 导入后持久化，但页面身份仍需验证 |

当前 `extension_bridge.py` 中的 `ensure_capture_session()` 会在捕获命令传入项目路径时尝试幂等启动 DeepCapture。它是内部初始化机制，不是清晰的用户可见会话 API。

### 4.3 运行期上下文

运行期由实在 Agent 的 RuntimeHost、AppRunService 和 Native DAP 提供：

| 对象 | 含义 |
| --- | --- |
| `网页对象` | `WebBrowser_CreateV3` 或 `WebBrowser_GetTAB_V1` 输出的运行时浏览器句柄 |
| AppRun 会话 | 实际执行原生流程的运行进程 |
| DAP 会话 | 调试器与运行进程之间的单步通道 |
| RuntimeHost 浏览器插件 | 执行网页组件所需的运行期插件 |

设计期 DeepCapture 已连接，只能说明“可以尝试读取页面证据”，不能说明“运行期插件可以执行网页组件”。

## 5. 原生组件的绑定模型

### 5.1 打开浏览器组件

`WebBrowser_CreateV3` 的职责是导航和产生浏览器变量：

```text
WebBrowser_CreateV3
  url = https://www.jd.com
  type = chrome
  openNewTab = 0 或 1
  输出变量 = 网页对象
```

它不绑定搜索框、商品标题或下一页元素。

### 5.2 元素操作组件

元素操作组件同时依赖两个不同引用：

```text
Element_SetValue_V6
  designated_obj = 网页对象
  uiSelector = SHIZAI_ELEMENT_DICT["search-element"]["selector"]
  text = 手机
```

其中：

- `网页对象` 决定在哪个浏览器 Tab 上执行。
- `SHIZAI_ELEMENT_DICT[...]` 决定在页面中找哪个元素。
- `search-element` 必须来自已导入且已验证的元素目录。

### 5.3 获取当前 Tab 组件

搜索、跳转或新标签页操作后，原来的浏览器变量可能不是当前结果页。此时使用：

```text
WebBrowser_GetTAB_V1
  type = chrome
  isActive = 1
  输出变量 = 网页对象
```

它会重新获取当前活动 Tab，并重新赋值给 `网页对象`。这不是重新捕获元素，也不是重新初始化 DeepCapture。

### 5.4 数据采集组件

`WebBrowser_DataCollectionV5` 的绑定包括：

```text
designated_obj = 网页对象
columnSelectorArray = 商品标题、价格、链接、销量等字段 selector
selector_page = 下一页按钮的页面上下文 selector（使用分页时）
```

每个字段 selector 必须来自同一页面快照。分页按钮是页面控制元素，与商品字段 selector 是两类不同绑定。
分页控件应使用专用绑定语义，不得追加到列字段列表：

```text
rpa-capture-elements --project PROJECT --step-id collect-products \
  --element-id NEXT_PAGE_ID --scan-token TOKEN \
  --as-page-selector --allow-write
```

CLI 会根据捕获元素生成 `mcp-page-scope` 页面上下文 selector，并写入
`DataCollectionV5.selector_page`；也可以对已导入元素使用
`rpa-bind-elements --as-page-selector`。直接用 `rpa-update-step` 传入已导入的
元素 ID 也会执行同样转换。这样分页控件不会改变四个采集列的数量和顺序。

分页是 `WebBrowser_DataCollectionV5` 的可选能力，不是 CLI 对所有采集任务的强制步骤。
`rpa-component-catalog` 会通过 `paginate` capability 和
`configuration_hints.pagination_execution` 告诉模型该组件支持原生下一页/加载更多；模型根据
用户的数量、页面证据和所选方案决定是否启用。单页采集、显式点击循环、JavaScript 或下载方案
都可以不设置 `selector_page`/`selector_more`。CLI 只在模型已经选择某个原生分页模式时校验其
对应 selector：`next-page` 对应 `selector_page`，`load-more` 对应 `selector_more`，而不会
根据某个固定数量自动改写流程。

分析器不会因为出现某个数量（例如 100）就创建“必须分页”的计划。数量只进入最终有效行数
验收契约；模型读取实时页面后可以选择单页完成，也可以选择目录暴露的原生分页或其它策略。

想显式选择原生分页时，优先使用语义清晰的命令：

```text
workflow rpa-bind-pagination
  --flow PROJECT
  --step-id collect-products
  --element-id NEXT_PAGE_ID
  --mode next-page
  --region 3
  --allow-write
```

该命令只写分页槽位并清理另一种互斥分页槽位，不会改变
`collectionConfig.columnSelectorArray`。`--region 3` 表示由采集组件按数量上限控制；如果模型
选择其它原生区域，传入实时组件契约要求的 `--region`，不要把分页元素当作采集列绑定。

### 5.5 JavaScript 组件

`WebBrowser_RunJsInIeBySelector_v3` 通常使用：

```text
web_page = 网页对象
executionEnvironment = MAIN
input = 页面内商品卡片选择器
```

JS 生成的隐藏标记元素可以随后被 `DataCollectionV5` 使用，但 JS 步骤必须位于采集步骤之前，并且只能在结果页面加载完成后执行。

对于模型选择的页面级表格模式（`use_js_table=true`），分页不是额外的设计器组件，
而是物化运行代码中的有界循环：每页先执行模型提供的表格 JS，合并并按商品链接去重，
达到 `max_rows` 后停止。下一页动作按以下优先级处理：

1. 查询当前页面可见的下一页控件并触发其原生事件；
2. 若新版 React 页面没有把分页控件暴露给运行时 DOM，则在同源京东搜索页递增
   `page` 和 `s` 参数导航；
3. 其他无限滚动页面尝试滚动到底部，但受物化代码的页数上限约束。

因此 `rpa-read` 仍只显示一个 `WebBrowser_RunJsInIeBySelector_v3` 节点，
不能据此判断“没有翻页”。最终是否达标必须查看 `rpa-status.output.data_row_count` 和
实际 Excel 文件。页面级 JS 的分页导航仍走 RuntimeHost，设计期 DeepCapture 只用于
观察页面或捕获元素，不负责运行期翻页。

## 6. 大模型获取元素的标准协议

大模型不直接把候选 ID 写进流程。标准协议如下：

### 阶段 A：创建浏览器上下文

```text
auth-status
-> rpa-context
-> rpa-component-catalog
-> rpa-start-design
-> rpa-read
```

创建流程时只写入已经确定的组件。元素依赖组件可以先写成 `unbound`。

### 阶段 B：打开页面并初始化 DeepCapture

```text
rpa-open-browser --url TARGET_URL --allow-write
-> extension status
-> rpa-capture-windows --project PROJECT
```

这里要区分“打开浏览器”和“接管浏览器”：

- `rpa-open-browser` 只是在用户当前 Chrome 配置中打开页面，不启动元素拾取层。
- `rpa-capture-windows`、`rpa-scan-page` 和 `rpa-capture-tree` 是读取操作。带上 `--project PROJECT` 时，CLI 会先调用 `ensure_capture_session(PROJECT)`，通过实在 Agent RPC 执行 `startDeepCapture(packagePath)`，建立项目绑定的 DeepCapture 读取会话。
- 只有需要鼠标悬停/点击自动拾取时，才启动真正的接管层：`extension start-capture` 会先启动 `startDeepCapture(packagePath)`，再启动 `startDeepCaptureInspect(mode)`。它使用实在 Agent 官方拾取覆盖层，不是 Codex 自己创建的浏览器通道。

#### 显式浏览器会话命令

短生命周期 CLI 命令也必须能复用同一个项目的 DeepCapture 会话，因此提供显式生命周期入口：

```text
workflow rpa-browser-session-start PROJECT --allow-write
workflow rpa-browser-session-status PROJECT
workflow rpa-browser-session-stop PROJECT --allow-write
```

三条命令的语义分别是：

| 命令 | 实际动作 | 是否打开官方拾取层 |
| --- | --- | --- |
| `rpa-browser-session-start` | 调用 `startDeepCapture(packagePath)`，建立读取会话 | 否 |
| `rpa-browser-session-status` | 读取本地跨进程的 last-known 握手状态 | 否 |
| `rpa-browser-session-stop` | 调用 `stopDeepCaptureInspect` 和 `stopDeepCapture`，清理会话状态 | 如已开启则关闭 |

`rpa-browser-session-status` 的 `truth_source` 固定标明为 `last_known_local_state`；它不是实时 RPC 心跳，也不能证明 Chrome 当前仍在线。真正的页面读取必须继续经过 `rpa-capture-windows`、`rpa-scan-page` 或运行期 `rpa-runtime-preflight` 验证。

会话复用有一个快速健康检查：如果 CLI 发现跨进程保存的 `active_last_known`，会先用一次短时限的根节点读取验证 DeepCapture 远端仍可用。验证失败才重新执行 `startDeepCapture`，并返回 `reconnected_from_persisted_state=true`；不能只相信本地状态文件，也不能每次无条件重启，否则会让旧的 `uia:*` ID 和 `scan_token` 失效。

所有读取结果使用 `control_mode=capture_read`、`browser_control=deep_capture_read`、`runtime_execution=false`。官方拾取结果使用 `control_mode=official_picker`、`browser_control=deep_capture_official_picker`。原生单步调试结果使用 `control_mode=workflow_runtime`、`browser_control=runtimehost`、`runtime_execution=true`。这三个字段是客户端判断控制通道的唯一依据。

自动拾取的完整命令如下：

```text
extension status
extension start-capture \
  --project PROJECT \
  --step-id search-input \
  --mode 2 \
  --allow-write
```

其中 `--step-id` 必须是流程中已经存在、但尚未绑定元素的组件步骤；`--mode 2` 表示鼠标左键按下拾取，`--mode 1` 表示悬停拾取。命令返回后，模型或用户在当前 Chrome 页面选择目标元素，再使用 `rpa-capture-windows/tree` 或 `rpa-scan-page` 读取新的 `uia:*` ID，最后用同一 `scan_token` 调用 `rpa-capture-elements` 获取完整 selector。完成后执行：

这里的“自动拾取”不是 Codex OCR 找坐标后模拟鼠标，也不是把屏幕坐标写入流程。CLI 通过实在 Agent 的官方 DeepCapture RPC 启动设计器拾取器；拾取器返回元素 ID，CLI 再通过 `getDeepCaptureElementDetail` 和 `getDeepCaptureElementSelector` 读取完整 selector。截图/OCR 只能作为模型理解页面的辅助证据，不能替代真实 selector；坐标点击也不能作为可复用的网页元素绑定。

```text
extension stop-capture --project PROJECT --allow-write
```

停止的是 DeepCapture/拾取层，不会关闭用户的 Chrome 页面，也不会替流程执行组件。当前 CLI 对 DeepCapture 的已知临时连接错误会做有限重试；如果覆盖层或 Native Messaging Host 处于旧状态，应先停止拾取、确认 `extension status`，再重新启动，不要在多个 CLI 进程中并行启动同一个项目的拾取会话。

#### 浏览器通道时限和快速失败

DeepCapture 读取不应继承云端或桌面通道的长默认等待。当前桥接层的硬性约定是：单次 DeepCapture RPC 默认最多等待 6 秒，单个窗口、树、页面扫描、元素捕获或运行预检默认最多 20 秒，读取最多进行 2 次有限重试。总预算包含嵌套读取和重试；到期后立即返回包含 `reason=channel_unavailable` 的错误，并提示重新执行 `workflow rpa-browser-session-start`。

因此，调用方必须按下面的状态机处理失败：

```text
短 RPC 成功 -> 继续扫描/捕获
短 RPC 超时 -> 有限重试一次
仍然无响应 -> 返回 channel_unavailable + next_action
                 -> 必要时重连一次
                 -> 仍失败则停止，不得继续等待或重复启动流程
```

`extension status` 和 `rpa-browser-session-status` 是诊断信息，不会替代实际读取探测。客户端应记录 `rpc_attempts`、`read_retries`、`targeted_elapsed_ms` 和错误原因，向模型暴露失败而不是显示为“流程仍在运行”。

`extension start-capture` 是一个保持态操作，因此启动后红色层会继续显示。一次性的
`rpa-capture-element(s)` 默认会在成功、失败和取消路径关闭官方拾取层；连续拾取时显式传
`--keep-picker-open`，最后再执行 `extension stop-capture`。关闭拾取层不会关闭 Chrome，且
会把跨进程状态中的 `inspect_started` 同步为 `false`。

查询元素时不需要为了“看见元素”强行启动鼠标接管：优先使用项目绑定的读取会话扫描当前 Chrome 的 `Document`。只有扫描不到可用网页节点、需要用户点击指定控件，或需要官方自动拾取时，才进入 `start-capture`。`wnd:*` 是窗口根节点，`Document` 才是网页扫描根，普通 `uia:*` ID 只是当前会话/页面快照中的候选引用。

### 阶段 C：扫描和选择

```text
rpa-scan-page --project PROJECT --query "搜索框"
```

返回候选元素和 `scan_token`。模型只做选择，例如选择 `element_id=uia:123`，不把它直接当 selector。

搜索框、搜索按钮等浅层交互控件默认使用 `--strategy auto`。该策略先通过 `getDeepCaptureElementChildrenById` 做有界逐层查找，命中后立即返回，不再读取整个 Document；结果会携带 `targeted_elapsed_ms`、`targeted_nodes_visited` 和 `targeted_rpc_calls`，用于判断扫描成本。`--strategy targeted` 强制使用该路径，`--strategy full` 保持完整 Document 扫描。`auto` 或 `targeted` 未命中时会自动回退到完整扫描，并返回 `targeted_fallback=true`，因此优化不会把“快速未命中”误报成“页面没有元素”。

推荐命令：

```text
rpa-scan-page --project PROJECT --query "搜索框" --strategy auto
rpa-scan-page --project PROJECT --query "搜索按钮" --strategy auto
rpa-scan-page --project PROJECT --query "商品标题 价格 销量 商品链接" --strategy full
```

流程设计必须按增量闭环推进，而不是一次性把所有步骤写入目录：

```text
写入一个组件
-> 静态校验
-> 扫描/捕获/绑定元素（需要时）
-> 物化
-> RuntimeHost 单步执行
-> 截图/OCR 或页面状态证据确认成功
-> 再写入下一个组件
```

截图/OCR 只能验证“页面是否打开、输入是否出现、结果页是否加载”等可见状态；它不能替代 DeepCapture 返回的 selector，也不能把坐标写入 `elements.json`。运行结果必须明确区分静态校验成功、单步执行成功和业务数据采集成功。

### 阶段 D：使用同一快照捕获完整 selector

```text
rpa-capture-elements \
  --project PROJECT \
  --step-id search-input \
  --element-id uia:123 \
  --scan-token SCAN_TOKEN \
  --allow-write
```

CLI 应在这一步完成：

1. 检查 token 未过期。
2. 检查元素 ID 属于同一个项目和页面快照。
3. 调用 DeepCapture 获取完整 selector。
4. 写入 `elements.json`。
5. 绑定到指定组件。
6. 回读并执行 `validate-step`。

### 阶段 E：页面变化后的重新捕获

搜索、刷新、登录、翻页或跳转后，必须重新执行：

```text
rpa-capture-windows
-> rpa-scan-page
-> rpa-capture-elements
-> validate-step
```

不得继续使用旧的 `scan_token`、旧的 `uia:*` ID 或旧的页面 Document ID。

### 阶段 F：流程运行期如何控制浏览器

设计期的 DeepCapture 只负责观察、扫描和拾取 selector；原生流程运行时走另一条通道：

```text
WebBrowser_CreateV3
  -> 打开或复用 Chrome Tab
  -> 输出运行时浏览器句柄“网页对象”
Element_SetValue_V6 / Keyboard_PressNew / WebBrowser_RunJs... / DataCollectionV5
  -> 使用“网页对象”执行网页组件
WebBrowser_GetTAB_V1
  -> 页面跳转后重新获取当前活动 Tab 并更新“网页对象”
```

运行期组件的两个绑定不能混用：

- `网页对象` 是 RuntimeHost 当前要操作的浏览器 Tab 句柄，由 `WebBrowser_CreateV3` 或 `WebBrowser_GetTAB_V1` 产生。
- `uiSelector`、`columnSelectorArray` 是已经从 DeepCapture 捕获并导入 `elements.json` 的持久化 selector。
- 运行流程不应调用 `startDeepCaptureInspect`，也不应把临时 `wnd:*`、`uia:*` 或 `scan_token` 当作运行时浏览器句柄。
- 页面跳转后，如果网页组件依赖当前页面证据，设计期重新扫描；运行期重新获取 `网页对象`，不能复用失效的 Tab 或页面元素引用。

因此“接管浏览器”只发生在设计期元素拾取阶段；“流程控制浏览器”发生在运行期原生组件阶段。两者共享同一个 Chrome 用户配置和页面目标，但不共享同一个控制对象。

## 7. 推荐的 RPA 设计状态机

```text
EMPTY
  -> PROJECT_CREATED
  -> BROWSER_READY
  -> CAPTURE_SESSION_READY
  -> COMPONENT_UNBOUND
  -> PAGE_EVIDENCE_READY
  -> ELEMENTS_BOUND
  -> STATIC_VALID
  -> MATERIALIZED
  -> RUNTIME_READY
  -> DEBUG_PAUSED
  -> DEBUG_RUNNING
  -> SUCCEEDED / FAILED
```

状态转换规则：

| 当前状态 | 操作 | 成功条件 | 失败时 |
| --- | --- | --- | --- |
| `PROJECT_CREATED` | 打开目标页面 | Chrome 页面可见 | 返回 `chrome_not_found` 或打开错误 |
| `BROWSER_READY` | 初始化 DeepCapture | 项目绑定的捕获会话可读 | 不允许继续扫描 |
| `CAPTURE_SESSION_READY` | 扫描页面 | 得到 Document 和 `scan_token` | 重新检查窗口、扩展和页面 |
| `PAGE_EVIDENCE_READY` | 捕获并绑定元素 | selector 可回读且页面身份一致 | 保持 `unbound` |
| `ELEMENTS_BOUND` | 静态校验 | 组件、参数、变量和 selector 均通过 | 禁止物化/运行 |
| `STATIC_VALID` | 物化和同步诊断 | 生成文件、索引和包信息一致 | 禁止运行 |
| `RUNTIME_READY` | `rpa-debug-start` | AppRun/DAP 停在首组件 | 返回 RuntimeHost/DAP 错误 |
| `DEBUG_PAUSED` | `rpa-debug-next` | 当前组件执行并停在下一组件 | 保留停靠状态，返回预检错误 |

`rpa-debug-status` 是只读的 last-known 状态查询：跨 CLI 进程读取时不会重新连接 DAP，
也不会把调试器退回第一个断点。只有 `rpa-debug-next`、`rpa-debug-continue` 等执行命令
才会恢复运行连接并推进组件。

## 8. 写入一致性与冲突处理

当前版本的原生写入命令统一经过以下边界：

```text
read task -> guard append shape -> mutate -> persist task
-> materialize main.sz/projects/variableInfo/file_index_700
-> optional PackageContentService RPC sync
-> readback native state -> consistency check
```

同一项目的写入还经过一个跨进程 OS 文件锁。锁位于 `Z-Factory/mcp-project-locks/`
而不是客户端应用目录，覆盖 `rpa-add-step` 的整个读改写和设计器同步过程。若另一个
CLI 进程正在写入，当前命令会返回 `project_write_conflict` 并保持原文件不变；模型应等待
前一个命令结束后重试，不能并发发送同一项目的添加、绑定、物化命令。

原生参数必须按设计器的表达式类型写入：数字槽位使用 `3` 这样的表达式字符串，不能写成 JSON 字符串字面量 `"3"`。CLI 在模板创建、参数更新和物化时都会归一化 `DataCollectionV5` 的 `collect_items`、`wait_time_page`、`scroll_config`、`showTable` 等严格数字参数，并在写回后重新校验。

### 8.1 元素目录的加载安全规则

这是客户端能否打开流程的硬性规则：

- `elements.json.elementMap[*].parentId` 为空，或只能指向 `groupMap` 中真实存在的元素组 ID。
- 普通元素 ID 只能作为组件的 selector 绑定，不能作为另一个元素的持久化父级。
- JS 生成的字段元素属于顶层 selector 元素，默认 `parentId = null`；它们的页面范围由 selector 和页面证据表达，不通过元素树父子关系表达。
- 写入前 CLI 必须检查 `parentId`，写入后还要重新读取并校验元素目录；发现非法关系时直接返回可读错误，不能把坏文件交给客户端加载。

违反这条规则时，设计器在初始化元素模型阶段可能调用普通元素不存在的 `addChild` 方法，表现为 `n.addChild is not a function`，因此不是组件参数顺序问题。修复后应重新物化运行树，并完全关闭、重新打开设计器流程，清除客户端内存中的旧元素模型。

### 8.2 原生写入后的固定检查顺序

任何 CLI 创建或修改流程都必须按以下顺序完成：

```text
read -> validate parentId and argument types -> mutate -> persist
-> materialize derived files -> readback -> validate strict project
-> synchronize designer RPC -> reopen when the client held a stale model
```

`rpa-add-step`、`rpa-bind-elements` 和 `rpa-update-step` 都会把桌面客户端传入核心层。对于已登记在 `application_package` 的项目，RPC 同步失败会返回 `designer_state_conflict`，调用方必须先执行 `rpa-materialize` 或 `rpa-sync-diagnose`，不能继续追加组件。

旧版设计器 RPC 可能在保存其它组件时把 `DataCollectionV5` 的可选分页参数恢复为空。CLI 写回必须在同步后重新回读并恢复 `selector_page`/`selector_more`，随后刷新索引并再次校验；客户端 UI 打开流程时也应以回读后的 `.task` 为准，而不能只相信 RPC 成功响应。没有选择原生分页时，空 selector 是合法状态；选择了原生分页时，空 selector 必须报告结构化错误，不能静默运行。

字段重绑也必须遵守同一规则：`region/page_type_num` 等模式元数据不是具体分页 selector。
如果模型没有提交新的具体 selector，重绑 `columnSelectorArray` 时必须保留任务中已捕获的分页绑定，
避免一次普通字段更新把可运行的分页配置清空。

`rpa-add-step` 默认拒绝跨过未绑定的元素组件。如果恢复过程中必须先追加一个依赖组件，必须显式使用
`--allow-unbound-predecessors`，并在运行前用 `rpa-reorder-steps` 提交完整且唯一的 step-id 排列，再重新绑定和严格校验。该开关不会放宽
组件、参数、元素 selector 或运行期校验。

`rpa-reorder-steps` 是原生任务的原子排列操作：它只接受当前任务 step-id 的精确排列，不创建、不删除节点；写入后同步重建
`main.sz`、源码、索引和变量文件。缺少 step、未知 step 或重复 step 都会在写入前拒绝。这样需要“JS 先于 DataCollectionV5”
等依赖关系时，可以恢复顺序而不直接编辑 `.task` 或生成客户端无法打开的树。

对于已经写入重复最终采集节点的旧项目，使用 `workflow rpa-repair-flow PROJECT --allow-write`。该命令只删除重复的后置 `WebBrowser_DataCollectionV5`，保留第一个采集节点，然后重新生成运行树；修复后应关闭并重新打开客户端流程。

追加组件还会拒绝以下结构：

- 同一流程中第二个 `WebBrowser_DataCollectionV5`。
- 已经出现 Excel 输出组件后再追加采集组件。

这两个规则防止设计器把一个最终输出职责展开成多个互相覆盖的节点；已有坏项目不会被自动重写，校验和诊断会明确指出冲突。

## 9. 当前实现的已知结构问题

以下内容是后续优化的基线，不应被当作已解决：

1. 捕获命令可以省略 `--project`，从而跳过项目级 DeepCapture 初始化。
2. 自动初始化发生在内部，缺少可查询的会话 ID、页面指纹、窗口 ID 和 Document ID。
3. `rpa-start-design` 的浏览器打开或 DeepCapture 初始化失败时可能只返回 warning，造成半成功状态。
4. 写入命令还没有统一的项目级互斥锁，多个 CLI 进程可能同时修改同一个应用。
5. 设计器保存 RPC 仍可能被客户端内部旧内存快照覆盖；当前 CLI 会在本地回读发现这类冲突，但不会替客户端解决并发覆盖。
6. 当前“写入、回读、再次比较”覆盖本地任务树和派生文件；客户端内部模型的最终回读接口仍需补齐。
7. 页面身份和元素捕获之间缺少强制的页面代数或快照校验。
8. 静态 `valid=true`、运行提交成功和业务结果成功的状态边界仍需在 CLI 输出中进一步统一。
9. 调试端口文件可能在异常退出后残留，状态读取需要区分“端口文件存在”和“有效 DAP 会话存在”。

## 10. 后续 CLI 优化顺序

### P0：会话与写入正确性

新增或统一以下能力：

```text
rpa-browser-session-start --project PROJECT
rpa-browser-session-status --project PROJECT
rpa-browser-session-stop --project PROJECT
```

会话状态至少包含：

```json
{
  "project_ref": "...",
  "session_id": "...",
  "window_id": "wnd:...",
  "document_id": "uia:...",
  "page_url": "...",
  "page_title": "...",
  "page_fingerprint": "...",
  "inspect_started": false,
  "overlay_state": "inactive"
}
```

其中 `window_id`、`document_id`、`page_url`、`page_title` 和 `page_fingerprint` 属于具体扫描快照；当前跨进程会话握手文件只持久化项目、会话、拾取层和 last-known 原生状态。不要因为握手文件没有页面 ID 就复用旧快照，也不要把 `session_status` 当作实时页面探测。

同时实现：

- 每个项目一把写锁。
- 所有写入命令统一经过 `read -> mutate -> persist -> readback -> validate`。
- 设计器 RPC 回写后必须再次读取 `.task`，不一致则返回 `designer_state_conflict`。
- 不允许通过追加重复组件来修复已有组件参数。

### P1：页面证据正确性

- `rpa-capture-windows`、`rpa-scan-page`、`rpa-capture-elements` 强制要求 `project_ref`。
- 每次扫描返回 `capture_session_id`、`page_fingerprint` 和 token 过期时间。
- 捕获命令校验 token、项目、窗口、Document 和页面指纹一致。
- 页面发生跳转时自动让旧 token 失效。
- 设计期页面捕获和运行期预检都返回统一的 `reason` 枚举。

### P2：调试和结果证据

- `rpa-debug-start` 前检查生成代码与 `.task` 索引一致。
- `rpa-debug-next` 明确返回当前组件、已执行组件、下一停靠组件和预检结果。
- `rpa-status` 统一区分 `submitted`、`running`、`stopping`、`succeeded`、`failed`、
  `stopped` 和 `cleanup_timeout`。
- 运行成功必须同时检查日志、输出文件、表头、行数、必填列和链接格式。

调试停止的顺序固定为 `AppRunService.stop -> DebugAdapterService.disconnect`。
任一 RPC 超时都必须在有界调用内持久化为 `cleanup_timeout`，返回 `retryable=true`
和 `next_tool=shizai_rpa_debug_stop`，不能把客户端留在没有终态的“流程停止中”。
`openSmallCardWindow` 返回 `code=5000` 但 renderer 更新成功时，应标记为
`open_result_status=reused_window`，这是已有小卡复用，不是 MCP 失败。

### P3：运行会话清理和“停止中”恢复

运行、调试和元素拾取是三种不同的会话，CLI 只清理自己启动的那一种：

| 会话 | 启动入口 | 结束时清理 | 不应清理 |
| --- | --- | --- | --- |
| 设计期 DeepCapture | `rpa-browser-session-start` / `extension start-capture` | 结束拾取时先 `stopDeepCaptureInspect`，需要关闭整个会话时再 `stopDeepCapture` | 不关闭用户正在复用的 Chrome |
| 设计期单步调试 | `rpa-debug-start` | `rpa-debug-stop` 断开 DAP，再停止 CLI 自己创建的 AppRun | 不接管或杀掉外部 debugpy |
| 运行期 Native RPA | `rpa-run` | 启动前清理残留 UI；等待模式终态或异常时走 `workflowDebugStop`，并关闭运行遮罩和红色拾取层；`--no-wait` 延后最终清理 | 不调用 `startDeepCaptureInspect` |

Native RPA 停止采用以下持久化状态机：

```text
submitted/running -> stopping -> stopped
                         |
                         +-----> cleanup_timeout
```

`stopping` 只表示停止请求已经发出，不能当作成功。官方停止 RPC 返回成功或
“没有活动运行/3001”时记录 `cleanup_status=complete` 或幂等确认；RPC 超时、连接
失败或无法确认时记录 `cleanup_timeout`，并返回 `cleanup_errors`、`next_tool=rpa-stop`。
`rpa-status` 不发云端写请求，但会把遗留的 `stopping` 在清理窗口（默认 15 秒）后
转成 `cleanup_timeout`，避免客户端无限显示“流程停止中”。

以下时机必须触发清理：

- `rpa-run --wait` 收到等待超时：自动调用一次 `workflowDebugStop`。
- 运行 RPC 抛出异常：尝试停止可能已经启动的 RuntimeHost，并记录原始运行错误。
- 运行收到 success/error/stopped 终态：关闭 Native RPA 运行遮罩；直接 CLI 的等待模式还
  会关闭 DeepCapture 红色拾取层；`waiting_for_user` 不做最终清理，保留浏览器供用户
  登录或完成人机验证。
- 用户主动执行 `rpa-stop`：始终使用官方停止 RPC，重复执行安全。
- 设计期自动拾取完成：执行 `stopDeepCaptureInspect`（CLI 使用
  `extension stop-capture` 或 `rpa-browser-session-stop`）关闭红色拾取层；不要把该层
  残留状态当成 RuntimeHost 运行状态。
- Native RPA 收尾必须同时执行两条官方清理路径：`CorpBrainService.workflowDebugStop`
  释放 RuntimeHost，`AppRunService.stop` 尝试完成设计器自身的运行状态转换；后者返回
  `3001/当前状态不可停止` 时按幂等结果记录，不得阻塞前者。
- `MaskAppService.closeRunMask` 在当前桌面版本只转发 `requestId`，会丢弃 `force`。
  CLI 的强制清理必须在同一认证 WebSocket 上继续调用官方 renderer
  `MaskService.closeMaskV2({requestId, force:true})`，至少覆盖首页、Agent 页面和当前编辑器，
  这样才能清除“流程停止中”提示残留。

## 11. 京东采集流程应如何套用规范

一个干净的流程应保持以下结构：

```text
1. WebBrowser_CreateV3       打开 https://www.jd.com，输出 网页对象
2. Element_SetValue_V6       使用首页捕获的搜索框，输入 手机
3. Keyboard_PressNew         回车
4. Basic_Delay_V1            等待结果页加载
5. WebBrowser_GetTAB_V1      获取当前结果页 Tab，更新 网页对象
6. WebBrowser_RunJs...        在结果页生成或整理商品字段
7. WebBrowser_DataCollectionV5 采集标题、价格、链接、销量和商家
8. Excel_Cs_NewV4            新建输出文件
9. Excel_Cs_WriteRange       写入 数据采集结果
```

第 2 步的搜索框必须在首页快照中捕获。第 7 步的商品字段和下一页按钮必须在结果页快照中重新捕获。第 1 步输出的 `网页对象` 贯穿运行流程，但它不替代第 2 步和第 7 步所需的元素 selector。

## 12. 验收标准

CLI 优化完成后，至少应通过以下测试：

1. 没有 `--project` 时，元素捕获命令明确失败，不会静默跳过初始化。
2. 同一项目并发写入时，一个请求获得锁，另一个请求返回结构化冲突。
3. 页面跳转后使用旧 `scan_token` 必须失败并要求重新扫描。
4. 设计器保存 RPC 覆盖本地任务时，CLI 必须发现并报告冲突。
5. 捕获成功后，元素 selector、页面指纹和绑定步骤可以回读。
6. DeepCapture 已连接但 RuntimeHost 未连接时，诊断结果必须明确区分两者。
7. `WebBrowser_CreateV3` 输出的浏览器变量可以被元素操作、JS 和采集组件引用。
8. 完整流程只能有一个最终采集节点和一个最终 Excel 写入链路。
9. 单步调试的每个停靠点都能通过 `rpa-debug-status` 查询。
10. 最终成功必须有真实输出文件和业务数据证据，不能只依据静态校验结果；“至少 N 条”
    使用最低数量判断，超过 N 条不得判失败。
11. `rpa-run --wait` 的成功、失败和异常返回都必须有 `final_cleanup` 证据，且红色
    DeepCapture 拾取层不能残留。

## 13. 实施原则

后续代码优化应优先修改共享核心，再由 CLI 和 MCP 复用：

```text
共享核心状态/会话/校验
   -> CLI 参数和 JSON 输出
   -> MCP 工具包装
   -> 文档和回归测试
```

不要在 CLI 命令中另写一套浏览器绑定规则，也不要在 MCP 层硬编码某个网站的分页或字段选择器。组件类型、参数语义、元素生命周期和运行状态必须只有一套来源。

## 14. 后续如何增加 MCP 规则

规则的具体放置位置和生效范围见 [`MCP_RULES.md`](MCP_RULES.md)。简要判断方法：

1. 所有任务都适用的安全边界，改 `shizai_agent/core/mcp_server.py` 的 `MCP_INSTRUCTIONS`。
2. RPA 业务条件和验证证据，改 `shizai_agent/core/project.py` 的 `rpa_generation_context` 中的 `business_rules`；MCP 分析请求时会把它返回给模型。
3. 单个工具的参数前提，改 MCP `_schemas()` 对应工具的中文 `description`。
4. 必须拦截的非法状态，改共享核心校验函数并添加测试；不要只依赖模型提示。

规则正文全部使用中文；工具名、组件 `type`、参数名和状态值保持真实标识符。每条规则都写清“适用条件、必须、禁止、验证证据、失败处理”，让模型可以据此选择下一步，而不是只得到一段含糊建议。
