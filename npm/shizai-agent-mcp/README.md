# shizai-agent-mcp

实在 Agent 本地 MCP 的轻量 npm/npx 安装器。它只安装和注册 Shizai MCP，浏览器识别与控制复用实在 Agent 客户端自带的 Chrome 扩展和 DeepCapture。

## 使用

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
```

也可以注册 Cursor 或 Claude Desktop：

```powershell
npx -y shizai-agent-mcp@latest register cursor
npx -y shizai-agent-mcp@latest register claude
```

常用命令：

```powershell
npx -y shizai-agent-mcp@latest status
npx -y shizai-agent-mcp@latest path
npx -y shizai-agent-mcp@latest uninstall
```

当前 npm launcher 版本为 `0.5.5`，默认下载最新的 `v0.7.27` Windows x64 资产：
`shizai-agent-v0.7.27-windows-x64.zip`。npm 包不附带浏览器自动化依赖，也不要求用户安装额外扩展。

`install` 覆盖文件后会自动注册 Codex MCP；需要跳过配置写入时使用
`install --skip-register`，之后可单独运行 `register codex`。

发布 Release 时必须同时上传同名 `.sha256` 文件。安装器先校验、解压到同盘暂存目录，
再原子替换现有版本；失败会恢复旧安装。测试企业内网镜像时可设置
`SHIZAI_AGENT_RELEASE_BASE_URL`，测试其他版本可设置 `SHIZAI_AGENT_VERSION`。
仅临时调试未签名镜像时可显式设置 `SHIZAI_AGENT_ALLOW_UNVERIFIED=1`。

本地设计器需要正在运行且已登录。MCP 是本地 stdio 服务，Codex 显示 `Auth: Unsupported`
属于正常状态，实在 Agent 的登录会话由桌面设计器提供。
