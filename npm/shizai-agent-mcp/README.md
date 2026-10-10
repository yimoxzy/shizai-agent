# shizai-agent-mcp

实在 Agent 本地 MCP 的轻量 npm/npx 安装器。默认安装 MCP、准备独立浏览器扩展文件并注册本地连接程序。

## 使用

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest status
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

从 `0.7.29` 起 npm 安装器与 MCP 采用同一版本号，默认下载对应的 Windows x64 发布包。
安装前校验实际构建产生的工具清单、扩展文件和所有载荷哈希，缺件或版本不符直接失败。
首次使用仍须在 `chrome://extensions` 或 `edge://extensions` 中加载返回的扩展目录；
浏览器是否真正连接以 `shizai_browser_status` 为准。可用 `--without-browser-extension` 跳过本地连接程序注册。

`install` 覆盖文件后会自动注册 Codex MCP；需要跳过配置写入时使用
`install --skip-register`，之后可单独运行 `register codex`。

发布 Release 时必须同时上传同名 `.sha256` 文件。安装器先校验、解压到同盘暂存目录，
再替换现有版本；注册成功后才清理旧安装。升级保留扩展连接密钥和端口。测试企业内网镜像时可设置
`SHIZAI_AGENT_RELEASE_BASE_URL`，测试其他版本可设置 `SHIZAI_AGENT_VERSION`。
仅临时调试未签名镜像时可显式设置 `SHIZAI_AGENT_ALLOW_UNVERIFIED=1`。

本地设计器需要正在运行且已登录。MCP 是本地 stdio 服务，Codex 显示 `Auth: Unsupported`
属于正常状态，实在 Agent 的登录会话由桌面设计器提供。
