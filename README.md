# Shizai Agent

面向实在设计器的本地 CLI 和 MCP 服务。它复用正在运行的桌面设计器会话，向 AI 客户端提供 Agent 工作流设计、原生 RPA、Chrome 元素采集、组件发现、静态校验、真实调试和运行结果检查。

## 设计原则

- MCP 默认暴露 56 个工具；可用 `SHIZAI_MCP_TOOL_PROFILE=rpa-authoring` 切换为 29 个原生 RPA 创建与参数编辑工具。两者共用 CLI 核心，不暴露通用 RPC。
- 所有写入、运行和调试操作都要求 `allow_write=true`。
- Agent graph 与原生 RPA 分开路由，网页任务不会误写成 Agent graph。
- RPA 先读取实时组件契约，再写入客户端原生应用；不创建重复项目。
- RPA 可先写完整计划，再逐组件验证；使用 `add_step` 增量追加时，当前组件需先通过静态和运行验证。两种路径均检查页面或输出证据。
- 静态校验不等于运行成功。最终结果必须检查运行状态、日志和输出文件内容。
- 返回值和日志中的凭据字段会脱敏，源码仓库不保存个人工作区标识。

## 安装

要求 Windows、Python 3.10+，并在本机安装实在设计器。需要调用设计器能力时，设计器必须正在运行且已登录。

源码安装：

```powershell
python -m pip install -e ".[dev]"
shizai-agent --json doctor
shizai-agent --json agent auth-status
```

注册 stdio MCP：

```toml
[mcp_servers.shizai-agent]
command = 'shizai-agent-mcp'
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
omit_tools_from = ["deferred"]
```

也可以使用 npm 安装器：

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
```

`Auth: Unsupported` 对本地 stdio MCP 是正常状态。服务本身不使用 MCP OAuth，设计器操作复用桌面登录会话。

新对话测试前必须确认 MCP 已挂载。提示词不能把工具注入空白对话；如果看不到
`shizai_*` 工具，应先升级、注册、完全重启 Codex，并在 `/mcp` 检查工具列表。
源码 CLI 入口是 `shizai_agent/shizai_agent_cli.py`，MCP 入口是
`shizai_agent/core/mcp_server.py`。完整路径、诊断命令和可复制测试提示词见
[MCP 客户端接入与测试提示词](docs/MCP_CLIENT_SETUP.md)。

## AI 调用入口

面向 Qwen 等中小模型的源码优化、精简工具集配置、持续编辑调用示例和评测建议见
[流程生成与持续编辑](docs/SMALL_MODEL_WORKFLOWS.md)。已有原生流程可先调用
`shizai_rpa_read_flow(detail="edit", step_id="目标ID")`，再通过
`shizai_rpa_update_step(dry_run=true)` 预览参数差异；提交携带 `expected_revision` 防止旧上下文覆盖新修改。
独立 GPT-5.6-Luna 的真实创建、修改、运行测试及修复记录见 [Luna 测评](docs/LUNA_RPA_EVALUATION.md)。

请求类型不明确时先调用 `shizai_route_intent`：

- `agent_workflow`：调用 `shizai_generation_context`、`shizai_components`、`shizai_draft`、`shizai_validate_draft`、`shizai_apply_flow`。
- `browser_rpa`：原句只分析一次；按分析返回的 `recommended_catalog_query` 查询一次能力目录，预检本次请求专属计划后再写入。
- `native_rpa`：同样先分析原始请求并查询实时组件目录，按原子操作选择真实组件。

网页 RPA 主链：

```text
shizai_rpa_analyze_request
-> shizai_rpa_component_catalog
-> shizai_rpa_validate_plan
-> shizai_rpa_start_design
-> shizai_rpa_open_browser
-> shizai_rpa_capture_windows / shizai_rpa_scan_page
-> shizai_rpa_capture_elements
-> shizai_rpa_validate_elements / shizai_project_import_elements
-> shizai_rpa_add_step + shizai_rpa_validate_step
-> shizai_rpa_validate_flow / shizai_rpa_validate_project
-> shizai_rpa_materialize / shizai_rpa_sync_diagnose
-> shizai_rpa_run / shizai_rpa_status
```

`shizai_rpa_add_step` 和 `shizai_rpa_validate_step` 返回的
`verification_gate` 是 CLI/MCP 共用的操作契约。模型不能只看到 `valid=true` 就继续追加；应按门中的
`debug_capabilities -> debug_start/reuse -> debug_command(next) -> debug_status` 执行当前组件，结合页面截图/OCR或运行输出确认效果，再继续设计。OCR只能确认可见结果，不能替代 DeepCapture selector。

识别、拆解、计划预检、应用创建和完整计划写入期间只显示设计状态，不接管电脑。`shizai_rpa_start_design` 默认 `open_browser=false`；完整图写入后调用 `shizai_rpa_open_browser`，此时才显示 5 秒倒计时和官方蓝紫接管边框。

组件顺序、等待策略、输出字段和输出格式只来自当前请求、实时组件契约和客户端模型提交的计划；MCP 不内置站点流程或默认字段。数量是完成契约：请求 100 条并允许翻页时，模型选择的方案需要持续到 100 条有效结果或已无下一页；可以使用组件内分页、显式下一页、循环、JavaScript、下载或多个组件组合。

组件级调试统一调用：

```json
{"name":"shizai_rpa_debug_command","arguments":{"project_ref":"PROJECT_PATH","action":"next","allow_write":true}}
```

`action` 支持 `next`、`continue`、`step_in` 和 `step_out`。先用 `shizai_rpa_debug_capabilities` 探测，再启动一次 `shizai_rpa_debug_start`，后续复用同一运行进程和 Chrome 会话。

## CLI

CLI 为诊断、运维和脚本提供 JSON 输出：

原生 Tars 小卡反馈：

```powershell
shizai-agent --json ui small-card --state designing --allow-write
shizai-agent --json ui small-card --state scanning --duration 5 --close --allow-write
shizai-agent --json ui small-card --demo --allow-write
```

原版四周遮罩、截图拾取和小卡反馈：

```powershell
# 验证遮罩时不要带 --close，命令会保持显示；另开命令用 --close-now 关闭
shizai-agent --json ui mask --mask-type white --tips "TARS 正在扫描当前页面..." --allow-write
shizai-agent --json ui scan-demo --mask-type white --interval 1.5 --allow-write
```

状态、调用边界和小卡/遮罩/扫描动画的区别见 [docs/UI_FEEDBACK.md](docs/UI_FEEDBACK.md)。

```powershell
shizai-agent --json doctor
shizai-agent --json project list
shizai-agent --json agent components
shizai-agent --json workflow rpa-component-catalog --scope builtin --query OCR --limit 20
shizai-agent --json workflow rpa-debug-capabilities PROJECT_PATH
```

无参数运行显示帮助。使用 `python -m shizai_agent` 与 `shizai-agent` 等价。

## 文档

- [MCP/CLI 架构基线（大模型规划、MCP 契约与安全边界）](docs/MCP_ARCHITECTURE.md)
- [CLI 完整使用手册](docs/CLI_USAGE.md)
- [MCP 工具目录](docs/MCP_CAPABILITIES.md)
- [原生 RPA 协议](docs/RPA_WORKFLOW.md)
- [组件目录查询](docs/COMPONENT_CATALOG.md)
- [认证与凭据](docs/AUTHENTICATION.md)
- [MCP 客户端接入与测试提示词](docs/MCP_CLIENT_SETUP.md)
- [npm 分发](docs/NPM_DISTRIBUTION.md)
- [测试计划与结果](tests/TEST.md)
- [Luna 多场景 RPA 创建与编辑实测](docs/LUNA_RPA_EXPANDED_EVALUATION.md)
- [独立组件场景测评集：32 场景、京东销量与预设参考对照](benchmarks/rpa/README.md)
- [仓库结构与发布路径](docs/REPOSITORY_LAYOUT.md)

对照压测可运行：

```powershell
.\.venv\Scripts\python.exe scripts\mcp_vs_tars_stress.py
```

它使用 15 个相同提示词分别测量 Codex MCP 和实在 Agent 原生设计器通道，结果写入 `_tmp/mcp-vs-tars-comparison-report.{json,md}`。

## 仓库结构

```text
shizai_agent/              Python 运行时
tests/                     单元、协议和真实环境测试
skills/shizai-agent/       唯一 AI Skill
docs/                      运维与协议文档
npm/shizai-agent-mcp/      npm/npx 安装器
release/                   Windows 发布脚本
```

源码入口是 `shizai_agent/`；不要在 `build/`、`dist/` 或 `release/` 的历史目录中修改代码。
新 Windows 包使用 `scripts/build-release.ps1` 构建，临时 PyInstaller 文件写入仓库外，最终 ZIP
和 SHA-256 文件才进入 `release/`。
