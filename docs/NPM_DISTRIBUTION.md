# npm / npx 分发

项目提供 `npm/shizai-agent-mcp` 作为本地 MCP 安装器。它不打包 Python 源码，执行时从
GitHub Release 下载对应平台的编译资产，校验 `.sha256` 后安装到用户目录。

## 用户安装

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
npx -y shizai-agent-mcp@latest install --with-browser-extension
```

Cursor 和 Claude Desktop：

```powershell
npx -y shizai-agent-mcp@latest register cursor
npx -y shizai-agent-mcp@latest register claude
```

安装后重启客户端。也可以让支持 MCP 的客户端使用：

```json
{
  "mcpServers": {
    "shizai-agent": {
      "command": "npx",
      "args": ["-y", "shizai-agent-mcp"]
    }
  }
}
```

正式环境建议先安装固定版本，再在配置中使用本地 EXE，以减少启动时的网络依赖。
当前 launcher 为 `0.5.6`，默认下载 Shizai `v0.7.28` 并自动安装到用户目录。下载阶段显示简洁进度条，不输出完整发布 URL；失败直接返回错误，成功只显示安装和注册状态。重复安装会先停止目标目录中的旧 MCP/CLI 进程，再原子覆盖并清理历史暂存与备份目录。
npm 包本身仍是轻量启动器。Windows Release 资产包含独立的 MCP 浏览器扩展和 Native Messaging host；只有传入 `--with-browser-extension` 才注册本地 host。安装结果会返回 `browser_extension_path`，用户需要在 `chrome://extensions` 或 `edge://extensions` 开启开发者模式并“加载已解压的扩展”。浏览器不允许安装器静默加载未上架扩展。

Codex 注册额外写入 `omit_tools_from = ["deferred"]`，让本地 stdio 工具直接进入新对话的工具面。部分自定义 Responses
提供商不兼容 Codex 的 deferred `tool_search` 请求；该设置避免新窗口工具列表为空，同时保留完整工具集。

## 发布者流程

1. 构建并上传 `shizai-agent-vVERSION-windows-x64.zip` 到 GitHub Release。
2. 对 ZIP 生成同名 `.sha256` 文件并一起上传。
3. 更新 npm launcher 的默认版本，执行 `npm publish`。

当前 launcher 优先支持 Windows x64；企业内网可使用
`SHIZAI_AGENT_RELEASE_BASE_URL` 指向内部 Release 代理。
