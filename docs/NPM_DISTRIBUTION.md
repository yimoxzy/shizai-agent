# npm / npx 分发

项目提供 `npm/shizai-agent-mcp` 作为本地 MCP 安装器。它不打包 Python 源码，执行时从
GitHub Release 下载对应平台的编译资产，校验 `.sha256` 后安装到用户目录。

## 用户安装

```powershell
npx -y shizai-agent-mcp@latest install
npx -y shizai-agent-mcp@latest register codex
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
当前 launcher 为 `0.5.5`，默认下载 Shizai `v0.7.27` 并自动安装到用户目录。下载阶段显示简洁进度条，不输出完整发布 URL；失败直接返回错误，成功只显示安装和注册状态。重复安装会先停止目标目录中的旧 MCP/CLI 进程，再原子覆盖并清理历史暂存与备份目录。
npm 包不包含第三方浏览器 MCP。注册只写入 `shizai-agent` 服务；浏览器识别、元素捕获和当前登录态复用均由实在 Agent 客户端自带扩展与 DeepCapture 提供。

Codex 注册额外写入 `omit_tools_from = ["deferred"]`，让本地 stdio 工具直接进入新对话的工具面。部分自定义 Responses
提供商不兼容 Codex 的 deferred `tool_search` 请求；该设置避免新窗口工具列表为空，同时保留完整工具集。

## 发布者流程

1. 构建并上传 `shizai-agent-vVERSION-windows-x64.zip` 到 GitHub Release。
2. 对 ZIP 生成同名 `.sha256` 文件并一起上传。
3. 更新 npm launcher 的默认版本，执行 `npm publish`。

当前 launcher 优先支持 Windows x64；企业内网可使用
`SHIZAI_AGENT_RELEASE_BASE_URL` 指向内部 Release 代理。
