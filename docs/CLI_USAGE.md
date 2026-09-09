# Shizai Agent CLI 完整使用手册

本文档对应 `Z-Factory 7.3.7.0` 和本仓库当前 CLI。命令以实际 CLI 帮助和当前设计器能力为准；设计器升级后，组件参数、数量和可用状态应重新查询。

CLI 的模块边界、浏览器会话、组件绑定、元素捕获协议和后续优化基线见 [`CLI_STRUCTURE.md`](CLI_STRUCTURE.md)。本文继续作为命令级使用手册；两者内容冲突时，以实际 CLI `--help` 和真实返回的结构化状态为准。

## 1. 一键进入和检查

在 PowerShell 中执行下面的命令。后续示例默认已经进入项目目录，并使用项目虚拟环境中的 Python：

```powershell
Set-Location "D:\path\to\shizai-agent"
$Py = ".\.venv\Scripts\python.exe"

& $Py -m shizai_agent --json doctor
& $Py -m shizai_agent --json agent auth-status
```

如果已经通过 `pip install -e ".[dev]"` 安装，也可以把 `& $Py -m shizai_agent` 替换成 `shizai-agent`：

```powershell
Set-Location "D:\path\to\shizai-agent"
shizai-agent --json doctor
shizai-agent --json agent auth-status
```

本机关键路径：

| 项目 | 路径或地址 |
| --- | --- |
| CLI 源码 | `<项目目录>` |
| Python | `<项目目录>\.venv\Scripts\python.exe` |
| 设计器 | `C:\Program Files\Z-Factory\7.3.7.0\实在设计器.exe` |
| 设计器数据 | `%APPDATA%\Z-Factory` |
| 本机业务通道 | `127.0.0.1:23408` |

## 2. 打开实在 Agent

CLI 没有单独的 `open-agent` 子命令。需要桌面端能力时，`agent auth-status`、组件查询、流程创建等命令会连接本机业务通道；找不到通道时会按当前安装路径尝试唤起设计器并等待通道。

也可以手动启动当前设计器：

```powershell
Start-Process -FilePath "C:\Program Files\Z-Factory\7.3.7.0\实在设计器.exe"
```

设计器启动后，在设计器中完成登录，再执行：

```powershell
& $Py -m shizai_agent --json agent auth-status
```

结果中的 `channel.reachable`、`desktop_logged_in`、`cloud_ready` 和 `ready` 用于判断状态。CLI 只返回状态摘要，不输出 token、手机号或密码。

禁止 CLI 自动唤起设计器时：

```powershell
$env:SHIZAI_AGENT_AUTO_START = "0"
& $Py -m shizai_agent --json agent auth-status
```

找不到安装程序时，先从实在 Agent 官网安装桌面端：<https://www.ai-indeed.com/>。

## 3. 安装和运行方式

### 源码方式

```powershell
Set-Location "D:\path\to\shizai-agent"
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
& ".\.venv\Scripts\python.exe" -m shizai_agent --help
```

### npm/npx 安装器

发布包的安装器负责下载 Windows 发布包并注册本地 MCP；CLI 本身仍在本机运行：

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
npx -y shizai-agent-mcp@latest status
```

注册后重启 Codex。Codex 的 `/mcp` 页面显示 `Auth: Unsupported` 对本地 stdio MCP 是正常的；是否已登录实在 Agent 使用下面的命令判断：

```powershell
& $Py -m shizai_agent --json agent auth-status
```

## 4. 全局参数

全局参数必须放在顶级命令 `shizai_agent` 后、子命令前：

```powershell
& $Py -m shizai_agent --json workflow rpa-context
```

| 参数 | 作用 |
| --- | --- |
| `--json` | 输出格式化 JSON，适合 Codex、脚本和 MCP 调试；建议始终使用。 |
| `--dry-run` | 不持久化 CLI 会话状态；它不等同于所有后端调用都变成只读。需要写入的命令仍须显式传 `--allow-write`。 |
| `--project PROJECT_REF` | 设置当前项目路径或设计器应用 ID；没有位置参数的流程命令会使用它。 |
| `--help` | 查看顶级命令或子命令帮助。 |

写入、运行、调试、桌面 UI 和元素导入命令还必须显式传 `--allow-write`。这可以避免模型只做查询或校验时意外操作设计器、浏览器或文件。

## 5. 顶级命令总览

执行 `& $Py -m shizai_agent --help` 可以查看当前入口。现有顶级命令如下：

| 命令 | 用途 |
| --- | --- |
| `doctor` | 检查本机安装、数据目录、Agent Brain 和业务端口。 |
| `ui` | 调用实在 Agent 自带的小卡、遮罩、扫描演示和 UI 状态。 |
| `project` | 查看、校验、注册、导出项目和管理元素目录。 |
| `extension` | 查看 Chrome 扩展/DeepCapture，并执行元素捕获。 |
| `agent` | 查看 Agent、模型、组件、草稿、运行和云端资源。 |
| `feishu` | 查看或配置桌面端原生飞书路由。 |
| `knowledge` | 管理云端知识库、文档和 URL 导入。 |
| `chat` | 通过当前登录桌面端向云 Agent 提问。 |
| `workflow` | 原生 RPA 流程创建、组件发现、元素绑定、校验、调试和运行。 |

## 6. `doctor` 环境检查

```powershell
& $Py -m shizai_agent --json doctor
```

`doctor` 是本机诊断汇总，适合首次运行或报错后使用。它主要检查安装目录、数据目录、Agent Brain 资源、业务端口和项目环境；真正判断设计器当前登录状态使用：

```powershell
& $Py -m shizai_agent --json agent auth-status
```

## 7. `agent` Agent 客户端能力

`agent` 面向 Agent graph、模型、知识检索和对话工作流，不等同于原生 RPA 的 `.task` 流程。网页、Excel、鼠标、桌面元素等任务应走 `workflow`。

### 查询和发现

```powershell
& $Py -m shizai_agent --json agent list
& $Py -m shizai_agent --json agent inspect agent-brain
& $Py -m shizai_agent --json agent capabilities
& $Py -m shizai_agent --json agent inventory
& $Py -m shizai_agent --json agent apps --page 1 --limit 20
& $Py -m shizai_agent --json agent app-info AGENT_ID
& $Py -m shizai_agent --json agent models --type llm
& $Py -m shizai_agent --json agent models --type llm --support-function-call --support-vision
& $Py -m shizai_agent --json agent model-providers
& $Py -m shizai_agent --json agent tools
& $Py -m shizai_agent --json agent components --agent-id AGENT_ID
& $Py -m shizai_agent --json agent generation-context --agent-id AGENT_ID --query "根据用户问题设计一个 Agent 工作流"
```

### 草稿、校验、保存和运行

读取草稿：

```powershell
& $Py -m shizai_agent --json agent draft --agent-id AGENT_ID
```

校验或保存 JSON 草稿时，`--draft-file` 和 `--draft-json` 必须二选一：

```powershell
& $Py -m shizai_agent --json agent validate-draft --agent-id AGENT_ID --draft-file .\draft.json --strict
& $Py -m shizai_agent --json agent save-draft --agent-id AGENT_ID --draft-file .\draft.json
& $Py -m shizai_agent --json agent run-draft "请回答用户问题" --agent-id AGENT_ID --timeout 120
```

CLI 当前没有名为 `agent apply-flow` 的子命令。Agent graph 的正式 MCP 保存入口是 `shizai_apply_flow`；CLI 对应的本地草稿入口是 `validate-draft` 和 `save-draft`。

### 运行记录和工作区资源

```powershell
& $Py -m shizai_agent --json agent runs --agent-id AGENT_ID
& $Py -m shizai_agent --json agent logs --agent-id AGENT_ID --conversation-id CONVERSATION_ID --message-id MESSAGE_ID
& $Py -m shizai_agent --json agent tracing --agent-id AGENT_ID --run-id RUN_ID
& $Py -m shizai_agent --json agent messages --agent-id AGENT_ID --conversation-id CONVERSATION_ID
& $Py -m shizai_agent --json agent suggested --agent-id AGENT_ID --message-id MESSAGE_ID
& $Py -m shizai_agent --json agent mcp-servers --keyword browser
& $Py -m shizai_agent --json agent datasets --keyword knowledge
& $Py -m shizai_agent --json agent cloud-list --page 1 --limit 20
& $Py -m shizai_agent --json agent cloud-info AGENT_ID
```

### Agent 配置和发布

```powershell
& $Py -m shizai_agent --json agent configure-knowledge --agent-id AGENT_ID --dataset-id DATASET_ID --top-k 5
& $Py -m shizai_agent --json agent configure-ingestion --agent-id AGENT_ID
& $Py -m shizai_agent --json agent publish --agent-id AGENT_ID --publish-range only_me
```

低层 `agent rpc SERVICE METHOD` 只适合排查已知设计器 RPC。默认只允许只读方法；需要变更时必须显式传 `--allow-write`，参数必须是 JSON 数组：

```powershell
& $Py -m shizai_agent --json agent rpc SERVICE METHOD --args-json '[]'
```

## 8. `workflow` 原生 RPA 总流程

原生 RPA 的目标是创建设计器中的真实应用和组件，而不是在 CLI 目录中另写一份模拟 RPA 文件。推荐顺序：

```text
auth-status
-> rpa-context
-> rpa-component-catalog
-> rpa-start-design
-> rpa-read / rpa-components
-> rpa-add-step
-> capture-windows / scan-page / capture-elements
-> rpa-validate-step
-> rpa-validate-flow
-> rpa-validate
-> rpa-materialize
-> rpa-sync-diagnose
-> rpa-run / rpa-status
```

### 8.1 读取设计上下文

```powershell
& $Py -m shizai_agent --json workflow rpa-context
& $Py -m shizai_agent --json workflow rpa-context --query "打开 Chrome，访问京东，搜索手机，采集商品名称、商家名称、销量和价格并写入 Excel"
```

它返回原生 RPA 的数据契约、元素绑定规则和下一步提示，不会创建项目，也不会自动选择站点组件。

### 8.2 创建流程和首个打开浏览器组件

下面是当前 CLI 的便捷创建方式。它会在设计器中创建 normal 应用，并把首个 `WebBrowser_CreateV3` 写入客户端项目；返回值里的 `project_ref` 和 `flow_ref` 是后续命令使用的项目路径。

```powershell
$Start = & $Py -m shizai_agent --json workflow rpa-start-design `
  --name "京东手机数据采集" `
  --url "https://www.jd.com" `
  --keyword "手机" `
  --output-file ".\result.xlsx" `
  --required-column "商品名称" `
  --required-column "商家名称" `
  --required-column "价格" `
  --required-column "销量" `
  --max-rows 20 `
  --allow-write | ConvertFrom-Json

$Project = [string]$Start.project_ref
$Flow = [string]$Start.flow_ref
$Project
```

说明：

- `--url` 必填；CLI 不从一句话中猜 URL。
- `--keyword`、输出文件名和列名只是写入流程元数据的显式请求参数。
- 默认复用当前 Chrome 标签页；需要新标签页时传 `--open-new-tab`。
- `--open-browser` 默认开启，会在设计器流程创建成功后打开当前 Chrome；不希望立即打开时使用 `--no-open-browser`。
- `--use-excel-components` 默认开启，表示后续使用设计器 Excel 组件；不使用时传 `--direct-excel-export`。
- `--use-js-table`、`--use-js-link` 是显式策略开关。新流程应由模型根据实时契约提供具体 JavaScript、输出变量和结果模式，不能把开关当作隐含站点模板。
- 创建成功后先读取返回的 `next_action`，不要根据命令名称自行跳过步骤。

如果只是想打开当前 Chrome 的目标页面：

```powershell
& $Py -m shizai_agent --json workflow rpa-open-browser `
  --url "https://www.jd.com" `
  --wait-seconds 2 `
  --allow-write
```

### 8.3 查询当前流程

```powershell
& $Py -m shizai_agent --json workflow rpa-read "$Flow"
& $Py -m shizai_agent --json workflow rpa-components --flow "$Flow"
```

重点查看：

| 返回字段 | 含义 |
| --- | --- |
| `flow_ref` / `project_ref` | 设计器应用目录，后续命令的稳定引用。 |
| `steps` | 当前有序组件。 |
| `binding_status` | 元素组件是否已经绑定 selector。 |
| `next_action` | 当前计划要求的下一步工具和参数。 |
| `step_validation` | 最近一次组件静态校验结果。 |

### 8.4 逐个添加组件

先查真实组件契约，再添加。通用模板如下：

```powershell
& $Py -m shizai_agent --json workflow rpa-add-step `
  --flow "$Flow" `
  --component "COMPONENT_TYPE_FROM_CATALOG" `
  --step-id "STEP_ID" `
  --label "步骤说明" `
  --config-json '{}' `
  --allow-write
```

PowerShell 多行命令中，反引号必须位于上一行末尾；为了避免换行错误，也可以写成一行。组件需要参数时，把 `config-json` 换成该组件 `detail=contract` 返回的字段，不要根据界面标题猜参数名。

例如，目录确认过类型后，可以使用：

```powershell
& $Py -m shizai_agent --json workflow rpa-add-step --flow "$Flow" --component "Basic_Delay_V1" --step-id "wait-result" --config-json '{"seconds":2}' --allow-write
```

每次添加后立刻检查：

```powershell
& $Py -m shizai_agent --json workflow rpa-read "$Flow"
& $Py -m shizai_agent --json workflow rpa-validate-step "$Flow" "STEP_ID"
```

失败时只更新当前步骤，不重复创建第二个步骤：

```powershell
& $Py -m shizai_agent --json workflow rpa-update-step `
  --flow "$Flow" `
  --step-id "STEP_ID" `
  --config-json '{"参数名":"新值"}' `
  --allow-write
```

如果恢复流程时必须暂时追加一个依赖组件，显式允许未绑定前置步骤，但不要直接运行：

```powershell
& $Py -m shizai_agent --json workflow rpa-add-step `
  --flow "$Flow" `
  --component "WebBrowser_RunJsInIeBySelector_v3" `
  --step-id "extract-links" `
  --allow-unbound-predecessors `
  --allow-write
```

随后用完整 step-id 排列一次性恢复执行顺序。排列必须包含当前流程的每一个 step，不能重复或遗漏：

```powershell
& $Py -m shizai_agent --json workflow rpa-reorder-steps `
  --flow "$Flow" `
  --step-id "open-chrome" `
  --step-id "search-input" `
  --step-id "submit-search" `
  --step-id "wait-result" `
  --step-id "get-result-tab" `
  --step-id "extract-links" `
  --step-id "collect-products" `
  --allow-write
```

重排成功只代表原生任务和运行文件一致，不代表元素已经绑定或业务运行成功；之后仍必须执行
`rpa-bind-elements`、`rpa-validate-step`、`rpa-validate --strict`，再做单步 RuntimeHost 验证。

### 8.5 浏览器窗口、页面树和元素捕获

创建或打开浏览器后，先查看可用窗口：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-windows
```

扫描当前 Document 的语义候选，不要求红色拾取层：

```powershell
& $Py -m shizai_agent --json workflow rpa-scan-page --query "搜索框" --level 6 --max-nodes 250
& $Py -m shizai_agent --json workflow rpa-scan-page --query "商品名称 价格 销量 商家名称"
```

搜索框和提交按钮默认启用有界定向扫描，避免为一个浅层控件先展开整个页面树：

```powershell
& $Py -m shizai_agent --json workflow rpa-scan-page `
  --project "$Project" `
  --query "搜索框" `
  --strategy auto
```

返回中的 `scan_strategy=targeted` 表示命中快速路径；`targeted_elapsed_ms`、`targeted_nodes_visited` 和 `targeted_rpc_calls` 可用于观测成本。`targeted_fallback=true` 表示定向路径没有得到候选，CLI 已自动回退到完整 Document 扫描。商品字段等需要遍历重复卡片的查询使用 `--strategy full` 更合适。

需要读取某个 Document 节点的子树时：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-tree --element-id "DOCUMENT_ELEMENT_ID" --level 6 --max-nodes 120
```

从扫描结果取得 `element_id` 和 `scan_token` 后，捕获一个或多个完整 selector：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-elements `
  --element-id "SEARCH_ELEMENT_ID" `
  --element-id "PRODUCT_NAME_ELEMENT_ID" `
  --project "$Project" `
  --step-id "collect-products" `
  --scan-token "SCAN_TOKEN" `
  --allow-write
```

分页控件不能作为采集列重新绑定。捕获“下一页”后使用专用选项，CLI 会自动生成稳定的页面上下文 selector：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-elements `
  --element-id "NEXT_PAGE_ELEMENT_ID" `
  --scan-token "SCAN_TOKEN" `
  --project "$Project" `
  --step-id "collect-products" `
  --as-page-selector `
  --pagination-mode next-page `
  --allow-write
```

对已经导入的元素也可以使用：

```powershell
& $Py -m shizai_agent --json workflow rpa-bind-elements `
  --flow "$Project" `
  --step-id "collect-products" `
  --element-id "NEXT_PAGE_ELEMENT_ID" `
  --as-page-selector `
  --allow-write
```

`--as-page-selector` 不会替换 `DataCollectionV5` 的四个列字段；它只设置分页控制并保留
采集列。它是兼容入口，默认绑定 `next-page`；需要明确配置原生分页模式时使用：

```powershell
& $Py -m shizai_agent --json workflow rpa-bind-pagination `
  --flow "$Project" `
  --step-id "collect-products" `
  --element-id "NEXT_PAGE_ELEMENT_ID" `
  --mode next-page `
  --region 3 `
  --allow-write
```

该命令只在模型已经选择原生分页时调用。模型也可以选择不分页，或选择循环、JavaScript、
下载等其它方案；CLI 不根据目标数量自动绑定分页。

`rpa-analyze-request` 只把数量写入最终有效行数验收契约；仅有“采集 N 条”不会自动生成分页约束。
模型应先结合实时页面证据和组件目录判断当前页是否已经满足目标，再决定是否绑定分页能力。

元素步骤的顺序必须是：

```text
rpa-add-step（先写入元素组件，binding_status=unbound）
-> rpa-capture-windows
-> rpa-scan-page
-> rpa-capture-elements 或 extension start-capture
-> 导入并绑定同一个 project_ref + step_id
-> rpa-validate-step
```

页面刷新、跳转、登录或搜索后，旧 `scan_token` 和临时 `element_id` 都应视为失效，重新扫描。

设计流程采用增量验证门：每写入一个组件，CLI 返回 `verification_gate`，并执行静态校验；若组件依赖元素，完成扫描、真实 selector 捕获和绑定后物化流程，再通过 RuntimeHost 单步执行该节点。只有静态校验通过、`rpa-debug-next` 到达当前组件边界且截图/OCR或其他页面状态证据确认动作结果，才能继续写入下一步。OCR是结果判断手段，不是 selector 生成或本机鼠标控制通道。

当模型选择 `use_js_table=true` 且目标行数超过单页容量时，物化器会在同一个
`WebBrowser_RunJsInIeBySelector_v3` 运行节点内部生成有界分页循环：先采集当前页，
再优先点击可见下一页；京东新版 React 页面如果没有可用的 DOM 分页节点，则按当前
搜索 URL 递增 `page`/`s` 参数并重新获取 Tab，直到达到 `max_rows` 或没有后续内容。
这不是隐藏的第二个采集组件，流程树仍保持一个最终采集输出和一个 Excel 写入链路。
运行验收以实际文件为准，必须同时检查 `data_row_count`、四列非空和商品详情链接，
不能只看 RuntimeHost 的 `success` 日志。

### 8.6 一次捕获商品字段

对商品卡片这种重复结构，可以用页面字段捕获命令：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-page-fields `
  --field product_name `
  --field merchant `
  --field price `
  --field sales `
  --field link `
  --project "$Project" `
  --step-id "collect-products" `
  --candidate-limit 4 `
  --card-grouping auto `
  --allow-write
```

需要指定扫描候选时，使用 `field=id`：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-page-fields `
  --field product_name `
  --field price `
  --field sales `
  --field-element-id "product_name=ELEMENT_ID_1" `
  --field-element-id "price=ELEMENT_ID_2" `
  --field-element-id "sales=ELEMENT_ID_3" `
  --project "$Project" `
  --step-id "collect-products" `
  --allow-write
```

只有在返回 `complete=true`、字段都有选择结果后才导入。页面字段捕获负责候选识别、selector 导入和可选绑定；它不会替代流程组件本身，也不会自动猜测缺失字段。

### 8.7 使用 Chrome 扩展进行交互式捕获

普通页面树扫描优先使用 `rpa-scan-page`。页面树证据不足、需要用户悬停或鼠标按下拾取时，先确保目标组件已写入且处于 `unbound`，再使用扩展：

```powershell
& $Py -m shizai_agent --json extension status

& $Py -m shizai_agent --json extension start-capture `
  --mode 2 `
  --project "$Project" `
  --step-id "search-input" `
  --allow-write

& $Py -m shizai_agent --json extension stop-capture `
  --project "$Project" `
  --allow-write
```

`--mode 1` 是 hover，`--mode 2` 是鼠标左键按下。扩展是当前实在 Agent 的浏览器能力，不会创建另一套浏览器登录会话。

`extension start-capture` 会保持红色拾取层，直到完成一次元素读取。`rpa-capture-element(s)`
默认在读取/导入成功、失败或取消后自动关闭该层，并在结果中返回 `picker_cleanup`。需要连续
拾取多个元素时传 `--keep-picker-open`；全部拾取完成后必须执行 `extension stop-capture`。

需要在多个 CLI 进程之间明确管理读取会话时，可使用：

```powershell
& $Py -m shizai_agent --json workflow rpa-browser-session-start "$Project" --allow-write
& $Py -m shizai_agent --json workflow rpa-browser-session-status "$Project"
& $Py -m shizai_agent --json workflow rpa-browser-session-stop "$Project" --allow-write
```

`rpa-browser-session-start` 只调用 `startDeepCapture`，不会显示官方红色拾取层；`extension start-capture` 才会额外调用 `startDeepCaptureInspect`。状态命令报告的是跨进程 last-known 状态，不是实时心跳。读取结果应检查 `control_mode=capture_read`，官方拾取应检查 `control_mode=official_picker`，原生单步应检查 `control_mode=workflow_runtime`。

浏览器读取采用快速失败策略：DeepCapture 单次 RPC 默认 6 秒、一次窗口/树/页面/元素捕获操作默认 20 秒，最多两次有限重试。跨 CLI 进程复用磁盘中的 `active_last_known` 前，会先做一次根节点远端探测；探测失败会自动重连并在结果中标记 `reconnected_from_persisted_state=true`。返回 `reason=channel_unavailable` 时，应按 `next_action` 检查 Agent 和 Chrome 扩展并重新启动会话，不要继续等待，也不要并行启动第二个捕获会话。

也可以直接读取单个原生元素：

```powershell
& $Py -m shizai_agent --json workflow rpa-capture-element --element-id "ELEMENT_ID"
```

### 8.8 元素目录管理

```powershell
& $Py -m shizai_agent --json project elements "$Project"
& $Py -m shizai_agent --json project validate-elements "$Project" --strict
& $Py -m shizai_agent --json project import-elements "$Project" --input ".\captured-elements.json" --allow-write
& $Py -m shizai_agent --json workflow rpa-bind-elements --flow "$Flow" --step-id "STEP_ID" --element-id "ELEMENT_ID" --allow-write
& $Py -m shizai_agent --json workflow rpa-prune-elements --flow "$Flow" --allow-write
```

只有真正被流程引用的元素才应保留。`--force-replace` 会替换仍被流程引用的元素，使用前先确认 selector 已重新验证。

## 9. 998 个基础组件的分模块查询

基础组件来自当前设计器安装包，不需要额外导入业务包。本机 `7.3.7.0` 的发现口径是：引擎 `componentsNew.json` 有 996 条唯一记录，设计器静态额外入口 2 个，CLI 的 `scope=builtin` 共 998 个可发现基础类型。版本切换后以 CLI 返回的 `counts` 为准。

一个组件可能同时属于多个内部分类，所以下表按模块相加会重复，不能用模块数量相加推算总数：

| 设计器一级模块 | 查询 ID | 本机组件数 |
| --- | --- | ---: |
| 手机自动化 | `mobile` | 40 |
| 流程控制 | `process_control` | 38 |
| 基础命令 | `base_command` | 17 |
| 桌面软件 | `interface_element` | 149 |
| 网页浏览器 | `webBrowser` | 149 |
| 键盘鼠标外设 | `peripheral_device` | 28 |
| AI 智能 | `ai_software_automation` | 100 |
| Excel/WPS 表格 | `excel` | 172 |
| Google Sheets | `google_sheets` | 13 |
| Word/WPS 文档 | `word` | 30 |
| 数据处理 | `data_parse` | 162 |
| 控制器/智能门户 | `controller` | 19 |
| 对话框通知 | `dialog_operator` | 22 |
| 系统 | `system` | 26 |
| 文件处理 | `file_parse` | 43 |
| 邮件 | `email` | 32 |
| 其他 | `other` | 59 |

查询全部基础组件摘要：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog `
  --scope builtin `
  --detail summary `
  --page 1 `
  --limit 200
```

按一级模块查询：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog `
  --scope builtin `
  --top-level webBrowser `
  --detail summary `
  --limit 200
```

按关键词或包查询：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog --scope builtin --query "OCR" --detail summary --limit 20
& $Py -m shizai_agent --json workflow rpa-component-catalog --scope builtin --package Excel --detail summary --limit 200
& $Py -m shizai_agent --json workflow rpa-component-catalog --scope builtin --category get_web_data --detail summary --limit 50
```

查询一个组件的真实契约：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog `
  --scope builtin `
  --component-type WebBrowser_CreateV3 `
  --detail contract `
  --limit 1
```

`detail` 的区别：

| 值 | 适用场景 |
| --- | --- |
| `summary` | 先缩小候选范围，返回类型、标题、分类、描述和标签。 |
| `contract` | 写入流程前使用，增加输入、默认值、选项、输出和运行函数。 |
| `full` | 需要完整设计器 definition 时使用，数据量较大。 |

每个组件写入流程前都要确认：

- `type`：写入 native task 的真实类型，不是界面显示标题。
- `inputs`：参数名、类型、默认值和校验规则。
- `outputs`：输出变量和是否导出。
- `code`：设计器实际运行函数及参数顺序。
- `package`、`component_modes`、`top_levels`：包和模块归属。

## 10. 业务扩展组件

业务组件与基础组件分开查询。业务组件可能来自 SaaS 扩展包，私有化部署或本机未导入时只能作为目录参考，不能直接物化到流程：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog `
  --scope business `
  --business-category "京东" `
  --detail summary `
  --limit 200

& $Py -m shizai_agent --json workflow rpa-component-catalog `
  --scope business `
  --business-package "京准通-互动广告" `
  --detail contract `
  --limit 200 `
  --project "$Project"
```

查看全部能力时使用 `--scope all`；基础组件会排在业务扩展前：

```powershell
& $Py -m shizai_agent --json workflow rpa-component-catalog --scope all --query "数据采集" --detail contract --limit 20
```

返回中的 `availability`、`requires_import`、`package_cache_available` 和 `project_imported` 用于判断业务包是否已经可用。CLI 不自动导入业务包。

## 11. 流程校验、物化和同步诊断

推荐在每个组件写入后校验一次，全部组件完成后再做整条校验：

```powershell
& $Py -m shizai_agent --json workflow rpa-validate-step "$Flow" "STEP_ID"
& $Py -m shizai_agent --json workflow rpa-validate-flow "$Flow"
& $Py -m shizai_agent --json workflow rpa-validate "$Project" --strict
```

`workflow validate PROJECT_REF` 是 `project validate PROJECT_REF` 的便捷别名：

```powershell
& $Py -m shizai_agent --json workflow validate "$Project"
```

物化和同步诊断：

```powershell
& $Py -m shizai_agent --json workflow rpa-materialize "$Project" --strict --allow-write
& $Py -m shizai_agent --json workflow rpa-sync-diagnose "$Project"
```

校验含义：

| 命令 | 检查范围 |
| --- | --- |
| `rpa-validate-step` | 一个组件的参数、元素绑定和组件契约；不执行组件。 |
| `rpa-validate-flow` | 组件契约、依赖/图引用、元素绑定和输出条件；新 MCP 流程的顺序由模型计划决定。 |
| `rpa-validate` | 项目文件、元素目录、实时组件契约和 native task；浏览器首节点只对旧 strict 流程兼容检查。 |
| `rpa-materialize` | 生成或刷新设计器运行树、源码映射、变量信息和索引。 |
| `rpa-sync-diagnose` | 包 ID、索引新鲜度、RuntimeHost 和运行插件状态。 |

静态校验通过不等于业务运行成功；仍要执行并检查终态和输出文件。

## 12. 单步验证和 Chrome 会话复用

先探测当前项目的调试能力：

```powershell
& $Py -m shizai_agent --json workflow rpa-debug-capabilities "$Project"
```

当前有两条单步通道：

### 设计器流程助手通道

`rpa-assistant-run-step` 调用设计器 `AppGenService.runStep`，要求已有流程助手会话，并传入步骤 ID：

```powershell
& $Py -m shizai_agent --json workflow rpa-assistant-run-step "STEP_ID" --allow-write
```

返回中的 `next_step_id` 用于继续下一步。该通道复用设计器当前流程助手会话。

### Native `.task` DAP 调试通道

对已经物化的 native 项目使用 DAP 单步：

```powershell
& $Py -m shizai_agent --json workflow rpa-debug-start "$Project" --start-from 1 --allow-write
& $Py -m shizai_agent --json workflow rpa-debug-status "$Project"
& $Py -m shizai_agent --json workflow rpa-debug-next "$Project" --wait-seconds 30 --allow-write
& $Py -m shizai_agent --json workflow rpa-debug-continue "$Project" --wait-seconds 30 --allow-write
& $Py -m shizai_agent --json workflow rpa-debug-step-in "$Project" --allow-write
& $Py -m shizai_agent --json workflow rpa-debug-step-out "$Project" --allow-write
& $Py -m shizai_agent --json workflow rpa-debug-stop "$Project" --allow-write
```

`rpa-debug-next` 执行当前组件并停在下一个组件边界；`step-in` 和 `step-out` 是代码级 DAP 动作。调试会话会复用同一个 AppRunService、DAP 线程和流程打开的 Chrome 窗口。页面或登录状态变化后，先执行运行时预检并重新捕获元素：

```powershell
& $Py -m shizai_agent --json workflow rpa-runtime-preflight --project "$Project" --step-id "STEP_ID"
```

设计流程必须按增量闭环推进：每写入一个组件，都要回读并执行对应的
`rpa-validate-step`，并检查返回的 `verification_gate`；网页元素绑定完成后用
`rpa-debug-start`/`rpa-debug-next` 单步验证，再读取 `rpa-debug-status` 和页面/运行证据，
再继续追加下一个组件。`rpa-debug-status` 只读最近一次持久化停靠状态，不会为了显示状态
重新连接 DAP 或把当前停靠点退回首组件；只有 `debug-next`、`debug-continue` 才会恢复运行连接。
同一项目的写入命令必须串行执行，遇到 `project_write_conflict` 时等待前一个命令结束后重试。

## 13. 运行、查看结果和停止

运行前完整执行：

```powershell
& $Py -m shizai_agent --json workflow rpa-validate "$Project" --strict
& $Py -m shizai_agent --json workflow rpa-materialize "$Project" --allow-write
& $Py -m shizai_agent --json workflow rpa-sync-diagnose "$Project"
```

启动并等待 native RPA 进入终态：

```powershell
& $Py -m shizai_agent --json workflow rpa-run "$Project" --wait --wait-seconds 900 --allow-write
& $Py -m shizai_agent --json workflow rpa-status "$Project"
```

可选运行参数：

| 参数 | 作用 |
| --- | --- |
| `--input-param-json` | 传给 native 工作流的输入参数对象。 |
| `--run-uuid` | 重试或查询日志时使用的稳定运行 UUID。 |
| `--workflow-run-id` | 已有工作流运行 ID。 |
| `--start-take-work` | 让设计器启动接管工作。 |
| `--no-wait` | 只提交运行，不保持通道等待终态。 |
| `--wait-seconds` | 等待 native 运行通道的最大秒数，默认 900。 |

运行结果必须同时核对：

- `rpa-status` 的终态是否成功。
- 当前运行日志和错误信息。
- `result.xlsx` 是否存在、时间戳是否更新。
- 工作表列头、数据行数、必填列和样例值。

停止当前运行：

```powershell
& $Py -m shizai_agent --json workflow rpa-stop "$Project" --allow-write
```

停止命令不是简单改本地标记。CLI 会先写入 `status=stopping`，调用官方
`CorpBrainService.workflowDebugStop`，再写入清理结果：

- `status=stopped`：官方 RPC 已确认停止；`cleanup_status=complete` 表示运行会话和
  Native RPA 运行遮罩都已处理。
- `status=stopping`：只可能是上一次 CLI 在停止过程中退出留下的标记，不能据此判断
  已经停止。
- `status=cleanup_timeout`：停止请求没有得到确认。客户端不会继续显示无限的“流程停止中”，
  应根据 `cleanup_errors` 重试 `rpa-stop`，不要直接杀 RuntimeHost 或 Python 进程。

收尾时 CLI 还会尝试设计器原生 `AppRunService.stop`，并对首页、Agent 页面、当前编辑器
发送 `MaskService.closeMaskV2(force=true)`。这是为了清理桌面端运行状态和提示残留；
`3001` 表示设计器认为当前没有可停止的 AppRun，会按幂等成功处理。

以下情况 CLI 会自动进入清理：`rpa-run --wait` 超时、运行 RPC 异常、用户主动执行
`rpa-stop`。直接 CLI 的 `workflow rpa-run` 在启动前会清理残留的设计期 UI，并在
等待模式的 success/error/stopped 或异常返回后执行最终清理：关闭原生小卡、接管边框、
运行遮罩和 DeepCapture 红色拾取层。结果中的 `native_ui.stale_ui_cleanup` 与
`native_ui.final_cleanup` 是清理证据；应同时检查其中的 `warning`、`close_result` 和
`stop_result`。`overlay_state=inactive` 表示通道返回了非活动状态，不等于屏幕像素级
验证；若仍看到红层，应立即重试 `rpa-browser-session-stop`。
`--no-wait` 会返回 `final_cleanup.status=deferred`，因为 RuntimeHost 可能仍在运行，
此时应继续调用 `rpa-status`，结束后再用 `rpa-stop` 完成运行会话清理。
如果结果是 `waiting_for_user`，最终清理同样为 deferred，以保留当前浏览器供用户完成
登录或人机验证；用户处理完成后重新预检并运行。

需要结束整个 DeepCapture 会话时使用 `extension stop-capture` 或
`rpa-browser-session-stop`；运行命令的拾取层清理只关闭红色拾取层，不替代会话停止。

推荐在客户端重启后先读取状态：

```powershell
& $Py -m shizai_agent --json workflow rpa-status "$Project"
```

若返回 `cleanup_timeout`，只重试官方停止命令；若返回 `stopped`，再开始新的
`rpa-run`。`accepted=true` 只代表运行请求被接收，不代表运行或清理成功。

## 14. `project` 项目管理

```powershell
& $Py -m shizai_agent --json project list
& $Py -m shizai_agent --json project inspect "$Project"
& $Py -m shizai_agent --json project info "$Project"
& $Py -m shizai_agent --json project profiles
& $Py -m shizai_agent --json project validate "$Project"
& $Py -m shizai_agent --json project export "$Project" --output ".\project-review.zip"
```

将现有项目注册到当前设计器：

```powershell
& $Py -m shizai_agent --json project register "$Project" `
  --import-mode create-new `
  --application-type normal `
  --allow-write
```

`--import-mode` 可选 `create-new` 或 `overwrite`；`--application-type` 可选 `normal` 或 `agent`；`--no-verify` 可跳过注册后的应用列表复核。覆盖已有应用前先导出备份。

## 15. `extension` Chrome/DeepCapture

```powershell
& $Py -m shizai_agent --json extension status
& $Py -m shizai_agent --json extension start-capture --project "$Project" --step-id "STEP_ID" --mode 2 --allow-write
& $Py -m shizai_agent --json extension stop-capture --project "$Project" --allow-write
```

低层扩展方法调用只传已确认的 DeepCapture 方法和 JSON 数组：

```powershell
& $Py -m shizai_agent --json extension call METHOD --args-json '[]'
```

普通 RPA 设计优先使用 `workflow rpa-scan-page`、`rpa-capture-elements`；红色交互拾取层仅在确实需要鼠标拾取时使用。

## 16. `ui` 原生反馈

这些命令只触发实在 Agent 自带 UI，不创建自定义窗口，不创建流程：

```powershell
# 原生 TARS 小卡，保持显示
& $Py -m shizai_agent --json ui small-card --state scanning --message "正在扫描当前页面元素" --allow-write

# 显示 5 秒后关闭小卡
& $Py -m shizai_agent --json ui small-card --state scanning --duration 5 --close --allow-write

# 设计器原生完整运行遮罩；省略 --close 可保持显示
& $Py -m shizai_agent --json ui mask --mask-type white --tips "TARS 正在处理当前任务..." --allow-write

# 立即关闭遮罩
& $Py -m shizai_agent --json ui mask --close-now --allow-write

# 播放设计器原版四周遮罩、截图拾取和小卡阶段序列
& $Py -m shizai_agent --json ui scan-demo --mask-type white --interval 1.5 --allow-write

# 查看最近一次 UI/捕获状态
& $Py -m shizai_agent --json ui status
```

小卡的 `--state` 可用：`takeover`、`designing`、`scanning`、`capturing`、`validating`、`running`、`waiting`、`ready`、`success`、`error`、`hidden`。`scanning` 表示原生 TARS 转圈状态；真正的网页元素识别仍由 DeepCapture 完成。

`ui mask` 的 `--mask-type` 可选 `gray`、`transparent`、`white`、`whiteWithSlot`。需要肉眼观察遮罩时不要同时传 `--close`；查看完成后再运行 `--close-now`。

## 17. `knowledge` 知识库

知识库命令走云端 REST，使用当前桌面登录态交换短期 token，或使用环境变量 `SHIZAI_AGENT_TOKEN`。常用命令：

```powershell
& $Py -m shizai_agent --json knowledge info
& $Py -m shizai_agent --json knowledge list --page 1 --limit 20
& $Py -m shizai_agent --json knowledge upload ".\manual.pdf" --wait
& $Py -m shizai_agent --json knowledge add-url "https://www.ai-indeed.com/" --wait
& $Py -m shizai_agent --json knowledge ingest ".\manual.pdf" --process-mode automatic --wait
& $Py -m shizai_agent --json knowledge wait DOCUMENT_ID --timeout 300 --interval 2
& $Py -m shizai_agent --json knowledge delete DOCUMENT_ID
```

可选环境变量：

```powershell
$env:SHIZAI_AGENT_TOKEN = "TOKEN"
$env:SHIZAI_AGENT_ID = "AGENT_ID"
$env:SHIZAI_DATASET_ID = "DATASET_ID"
```

不要把真实 token 写进草稿、项目 JSON、skill、README 或 Git 提交。

## 18. `chat` 对话

```powershell
& $Py -m shizai_agent --json chat ask "请说明当前 Agent 的工具能力"
& $Py -m shizai_agent --json chat ask "继续上一次对话" --agent-id AGENT_ID --conversation-id CONVERSATION_ID
```

`chat ask` 是对话入口，不是原生 RPA 流程设计入口。需要创建网页或桌面流程时使用第 8 节的 `workflow` 顺序。

## 19. `feishu` 原生配置

查看当前状态：

```powershell
& $Py -m shizai_agent --json feishu native-status
```

使用环境变量读取配置：

```powershell
$env:FEISHU_APP_ID = "APP_ID"
$env:FEISHU_APP_SECRET = "APP_SECRET"
& $Py -m shizai_agent --json feishu native-configure
```

不落地激活，或从标准输入读取 secret：

```powershell
& $Py -m shizai_agent --json feishu native-configure --client-id APP_ID --secret-stdin --inactive
& $Py -m shizai_agent --json feishu native-disable --user-id USER_ID
```

## 20. JSON 输出和退出状态

推荐所有脚本调用都带 `--json`，这样模型可以读取结构化字段，而不是解析人类提示文案。

常见结果字段：

| 字段 | 说明 |
| --- | --- |
| `kind` | 结果类型，例如 `shizai_rpa_started`。 |
| `valid` | 校验是否通过。流程校验失败时通常以非零退出码结束。 |
| `project_ref` / `flow_ref` | 项目引用。 |
| `next_action` | 下一步工具、参数和原因。 |
| `ui_feedback` | 最近一次原生 UI 阶段。 |
| `error` / `message` | 错误或处理说明。 |
| `warnings` | 可继续但需要人工确认的告警。 |

脚本中可以按退出码判断：命令成功通常为 0；校验失败、知识库等待超时或参数错误为非零。不要只用“进程启动成功”作为流程成功证据。

## 21. 常见问题

### `Auth: Unsupported`

本地 stdio MCP 没有 MCP OAuth，这是传输层状态，不表示实在 Agent 没登录。执行：

```powershell
& $Py -m shizai_agent --json agent auth-status
```

### `requires --allow-write`

这是写入保护。确认命令确实要创建、修改、运行或控制 UI 后，在命令末尾加入 `--allow-write`。

### 找不到项目

先用 `project list` 或 `workflow rpa-start-design` 的返回值取得 `project_ref`。后续优先传完整绝对路径：

```powershell
& $Py -m shizai_agent --json project inspect "$Project"
```

### 元素捕获为空或 selector 失效

确认 Chrome 是由当前实在 Agent 会话打开的，重新执行 `rpa-capture-windows` 和 `rpa-scan-page`。页面发生跳转后重新取得 `scan_token`；不要复用旧页面的 `element_id`。

### 组件查到了，但添加或物化失败

先使用 `detail=contract` 查询真实类型和参数，再检查该组件是否属于业务扩展、是否已导入当前项目。基础组件目录可发现不代表当前项目已经有合法的 task 节点模板。

### 流程校验通过但运行失败

按下面顺序查看：

```powershell
& $Py -m shizai_agent --json workflow rpa-runtime-preflight --project "$Project"
& $Py -m shizai_agent --json workflow rpa-sync-diagnose "$Project"
& $Py -m shizai_agent --json workflow rpa-status "$Project"
```

重点确认 RuntimeHost、运行插件、包索引、Chrome 页面身份、登录状态和输出文件。

### 想查看命令的最新参数

CLI 帮助始终优先于本文档：

```powershell
& $Py -m shizai_agent workflow --help
& $Py -m shizai_agent workflow rpa-add-step --help
& $Py -m shizai_agent workflow rpa-start-design --help
& $Py -m shizai_agent agent --help
```

## 22. 相关文档

- [MCP 工具目录](MCP_CAPABILITIES.md)
- [原生 RPA 工作流](RPA_WORKFLOW.md)
- [组件目录查询](COMPONENT_CATALOG.md)
- [认证与凭据](AUTHENTICATION.md)
- [原生 UI 反馈](UI_FEEDBACK.md)
- [npm 分发](NPM_DISTRIBUTION.md)
- [中文 AI Skill](../skills/shizai-agent/SKILL.md)

## 原生参数编辑补充

`workflow rpa-read PROJECT_PATH --detail edit` 返回紧凑节点目录及 revision；
追加 `--step-id NODE_ID` 读取该节点的原生参数和实时契约。默认 `--detail full` 保留原返回。

`workflow rpa-update-step --flow PROJECT_PATH --step-id NODE_ID --config-json JSON --dry-run`
只预览差异，不需要 `--allow-write`。提交时移除 `--dry-run`，增加 `--allow-write` 和
`--expected-revision REVISION`。旧版本冲突输出结构化 `revision_conflict`，退出码为 1。
流程图删除、插入和重排不属于这个参数接口。完整示例见 [持续编辑](SMALL_MODEL_WORKFLOWS.md)。

## 计划分析和预检补充

`workflow rpa-analyze-request "创建原生 RPA，写入文本后读取"` 返回分析。
`workflow rpa-validate-plan --plan-file PLAN.json --analysis-file ANALYSIS.json`
从 UTF-8（可带 BOM）文件读取 JSON，使用与 MCP 相同的实时组件薄预检；无效计划返回 JSON 和退出码 1。
`--analysis-file` 可省略，此时只检查提供的计划。预检不创建桌面客户端或写入项目。

`workflow rpa-component-catalog --response-mode compact` 保留相同组件契约和分页，省略重复选型说明。
增加 `--visible-only` 可按设计器的 `releaseTag & 128` 隐藏规则筛选组件；默认 `--include-hidden`
保留 CLI 兼容性。MCP 的 rpa-authoring 目录默认仅显示可见组件，full 默认包含隐藏组件；
显式 component_type 查询仍能读取旧流程中的隐藏类型。
变量使用 `{"variable":"变量名"}`，字符串 `$变量名` 仍是字面量。
