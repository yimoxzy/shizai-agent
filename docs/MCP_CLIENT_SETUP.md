# MCP 客户端接入与测试提示词

本文解决“新对话看不到 MCP 工具”和“模型开始自己搭 MCP 客户端”的问题。

## 测试对话归属

MCP 测试必须在 Codex 项目“ 一句话生成流程mcp测试 ”下点击新建普通对话，
然后在该对话中确认 `shizai-agent` 工具面，再粘贴测试提示词。不要用当前代码
仓库对话的 `fork`、协作子代理或隐藏子线程代替；这些线程可以执行任务，但通常
不会作为目标项目的普通对话记录显示，也不适合作为 GPT-5.5 测试样本。

目标项目路径是：

```text
D:\agent\indeed_mcptest\
```

线程归属和 MCP 挂载是两个独立条件：项目选对了不代表工具已挂载，工具挂载了也
不代表线程会显示在目标项目列表。两者都确认后才开始耗时的 RPA 测试。

## 先判断问题在哪

提示词不能把工具注入一个没有挂载 MCP 的对话。开始 RPA 任务前，模型必须能看到
`shizai_*` 工具，至少包括：

- `shizai_rpa_analyze_request`
- `shizai_rpa_component_catalog`
- `shizai_rpa_start_design`
- `shizai_rpa_add_step`

如果这些工具不存在，模型不得继续设计、不得自己创建 MCP 客户端、不得另起一个
stdio 进程，也不得声称流程已经创建。此时应修复宿主的 MCP 配置。

## 当前入口

源码仓库：

```text
D:\agent\indeed_mcp\
```

源码 CLI：

```text
D:\agent\indeed_mcp\shizai_agent\shizai_agent_cli.py
```

源码 MCP：

```text
D:\agent\indeed_mcp\shizai_agent\core\mcp_server.py
```

Windows 安装后的 MCP 默认入口：

```text
C:\Users\edy\AppData\Local\Programs\ShizaiAgent\shizai-agent-mcp.exe
```

安装器会把实际路径写入 Codex 配置，不建议手工猜测路径。`shizai-agent.exe`
是诊断 CLI，`shizai-agent-mcp.exe` 才是给 Codex 挂载的 stdio MCP 服务。

## 安装或升级最新版

在宿主 PowerShell 中执行，不能把这几条命令当成 MCP 工具调用：

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
```

然后完全退出并重新打开 Codex，在 `/mcp` 中确认：

1. 存在 `shizai-agent` 服务；
2. 服务已连接；
3. 工具列表包含 `shizai_rpa_analyze_request` 和 `shizai_rpa_component_catalog`；
4. `Auth: Unsupported` 对本地 stdio MCP 是正常的，不代表工具不可用。

源码开发时可在仓库根目录检查：

```powershell
python -m shizai_agent --json doctor
python -m shizai_agent --json extension status
python -m shizai_agent --json workflow rpa-component-catalog --scope builtin --limit 20
```

MCP 服务和 CLI 都应使用同一份源码/发布版本。不要用旧的 `shizai-agent-mcp.exe`
注册后再拿新版 CLI 做混合测试。

## 新对话测试提示词

下面这段可以直接粘贴到已确认挂载 `shizai-agent` 的新对话中：

```text
请使用当前对话已经挂载的 shizai-agent MCP 完成下面任务，不要自己编写 MCP 客户端，也不要另起 stdio 进程。
开始前先确认你能看到 shizai_rpa_analyze_request、shizai_rpa_component_catalog、shizai_rpa_start_design 和 shizai_rpa_add_step。
如果看不到这些工具，请立即停止，不要假设工具存在，并明确告诉我需要在宿主执行 `npx -y shizai-agent-mcp@latest install`、重启 Codex、在 `/mcp` 确认服务后再开始。

请重新创建一个全新的原生 RPA 流程，打开 https://www.jd.com，搜索“手机”，采集不少于 100 条商品记录。
请由你根据实时组件契约自行决定是否使用原生数据采集组件、分页、循环、JavaScript 或其他合法组合；不要把 100 条硬编码成必须分页。
设计必须边写边验证：每写入一个组件就读取 verification_gate，静态校验通过后单步调试当前组件，用页面/运行证据确认成功，再进入下一组件。全程复用同一个 MCP stdio 会话、project_ref 和 Chrome 会话；最后核对运行终态、输出字段、有效行数和红色拾取遮罩是否清理。
```

## 责任边界

| 现象 | 正确处理 |
| --- | --- |
| MCP 工具列表为空 | 修复宿主注册并重启客户端；提示词无法修复 |
| 能看到工具但浏览器不通 | 调用 `shizai_extension_status`、`shizai_rpa_browser_session_status` 和运行前预检，按错误修复 |
| 每次调用都出现 `channel_unavailable` | 检查调用方是否每次重新启动 stdio；一个流程只保留一个 MCP 进程 |
| 模型绕过原生组件使用 JS | 允许，但应说明原因并验证结果；原生组件是优先建议，不是硬性限制 |
| 运行结束仍有红色遮罩 | 读取终态和 `final_cleanup`；不要用 `shizai_ui_feedback` 伪造普通流程进度 |
