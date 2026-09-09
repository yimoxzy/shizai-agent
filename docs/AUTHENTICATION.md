# 认证与使用方式

这个项目有三条独立的访问路径，登录要求不同。

| 路径 | 访问对象 | 是否需要登录 | 凭据来源 |
| --- | --- | --- | --- |
| MCP stdio 启动 | MCP 工具列表和进程启动 | 启动时不需要 | 无 |
| 本机设计器通道 | 组件、草稿、运行、保存、发布 | 需要设计器当前会话已登录 | 设计器登录态 |
| 云端知识库 REST | 知识库、文档上传、URL 导入 | 需要云端凭据 | 设计器临时 token 或 `SHIZAI_AGENT_TOKEN` |

## 推荐模式：设计器已登录

启动 MCP 客户端后，如果本机已安装实在设计器但业务通道未启动，MCP 会自动打开 `实在设计器.exe` 并等待本机业务通道 `23408`。如果没有找到安装程序，会在错误中提示官网下载地址：<https://www.ai-indeed.com/>。设计器当前用户决定可见的 Agent、组件、模型、MCP 服务和知识库范围。云端知识库命令会调用 `LoginService.changeToken` 换取短期 token，token 只保存在当前进程内，不写入项目和 CLI session 文件。

先检查：

```powershell
shizai-agent --json doctor
shizai-agent --json agent auth-status
```

`auth-status` 只返回通道可达性、登录状态、云端凭据来源和是否 ready，不返回 token、手机号或账户资料。

如需禁止 MCP 自动打开设计器，可设置：

```powershell
$env:SHIZAI_AGENT_AUTO_START = "0"
```

Codex `/mcp` 页面中的 `Auth: Unsupported` 表示这个本地 stdio 服务没有配置 MCP
OAuth，并非实在 Agent 登录失败。安装后重启 Codex，再调用 `shizai_auth_status` 判断
设计器连接和实际登录状态。

## 无桌面模式

只使用云端 REST 操作时，可以设置运行时环境变量：

```powershell
$env:SHIZAI_AGENT_TOKEN = "TOKEN"
shizai-agent --json knowledge list
```

MCP 客户端也要在启动 MCP server 的同一进程环境中提供这个变量。不要把真实 token 写入草稿、skill、源码、项目 JSON 或提交记录。

## 跨设备边界

当前设计器业务通道是 `127.0.0.1:23408`。因此，组件采集、草稿编辑、运行和发布这类设计器操作要求 MCP 进程与设计器在同一台机器上，并使用该机器上的登录会话。跨设备使用时，应把 MCP server 部署到设计器所在机器；只做云端知识库 REST 操作时，可在 MCP 所在机器使用 `SHIZAI_AGENT_TOKEN`。

## 常见判断

- `MCP stdio` 能启动，只代表协议服务启动成功，不代表已经登录设计器。
- `shizai_components`、`shizai_draft`、`shizai_apply_flow` 和 `shizai_run_draft` 走本机设计器会话。
- `knowledge info/list/upload/add-url/ingest/delete` 走云端 REST；设计器登录态和 `SHIZAI_AGENT_TOKEN` 二选一即可。
- `SHIZAI_AGENT_API_KEY` 是飞书网关的旧兼容配置，不能替代知识库 REST 使用的 `SHIZAI_AGENT_TOKEN`。

## MCP 使用顺序

1. 调用 `shizai_auth_status` 确认连接和认证状态。
2. 调用 `shizai_generation_context` 获取当前设计器组件、变量和规划契约。
3. 调用 `shizai_draft` 读取现有流程，保留用户已有变量。
4. 生成草稿后调用 `shizai_validate_draft`。
5. 校验通过后调用 `shizai_apply_flow` 保存并回读；写入时显式传 `allow_write: true`。
6. 使用 `shizai_run_draft(allow_write=true)` 做小样本验证；需要发布时再通过 `shizai_apply_flow(publish=true)` 执行。
