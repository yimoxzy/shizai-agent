# MCP 0.7.29 修复与验收记录

## 发布与本机结果

按 Gitee → GitHub → npm 顺序完成发布：

- Gitee 源码提交：`09ea113ffa837060596c5414439e9d66a1fd3c69`，标签 `v0.7.29`。
- GitHub 最新：[v0.7.29](https://github.com/yimoxzy/shizai-agent/releases/tag/v0.7.29)，
  仅上传最终 ZIP 与 SHA-256；远端资产 digest 与本地一致，GitHub main 源码未改动。
- npm：`shizai-agent-mcp@0.7.29` 已发布，Registry 处理后 `latest` 已返回 0.7.29。
  npm tarball shasum：`568bbfb9941b6d1ea71ced826aa02361ac1e2d6c`。
- 本机：0.7.29 已安装并注册 Codex；实际 EXE 重新握手枚举为 full=67、rpa-authoring=43。
  载荷、CLI 版本、Native Host bootstrap、关闭 stderr 通过；Chrome/Edge 注册均指向正式目录，
  原连接密钥与端口保持一致。

本机安装在 npm 索引等待期间使用同版安装器，从已经发布的 GitHub 资产下载并校验。
以后安装或升级用 `npx -y shizai-agent-mcp@latest install`，状态用 `npx -y shizai-agent-mcp@latest status`。
本机安装审计见 [结构化记录](MCP_INSTALLED_0.7.29_20261008.json)。

**尚未完成浏览器真实接入**：安装审计返回 `extension_missing`。只读检查确认 Chrome Default
仍从旧的 `AppData/Local/Temp/shizai-browser-final-…/browser-extension` 加载扩展。
应在 `chrome://extensions` 加载正式目录 `%LOCALAPPDATA%\Programs\ShizaiAgent\browser-extension`，
再重新连接 MCP 并检查 `shizai_browser_status=ready`。本对话既有工具列表不会因磁盘升级自动刷新。

## 问题与根因

2026-10-08 检查时，三个渠道没有同步：GitHub 最新是 v0.7.27，npm 最新是 0.5.5，
本机 EXE 标称 0.7.28，实际 full 工具数却是 56 且没有 `shizai_browser_*`。
源码已有 67 个工具，因此重启客户端无法补齐旧程序本身没有的能力。

本机安装缺少 `browser-extension` 和 `shizai-agent-browser-host.exe`。Chrome/Edge
Native Messaging 注册还指向旧测试临时目录。旧配置存在不等于扩展安装完整。
此外，rpa-authoring 白名单漏掉已实现的 `shizai_rpa_add_step`，导致模型不能在精简工具集里追加组件。

## 本版优化

- npm、源码与 EXE 统一为 0.7.29；默认安装扩展文件与持久 Native Host，升级保留连接身份。
- 实际 EXE 的 full=67、rpa-authoring=43 与源码完整 schema 一起参与发布核验。
  生成发布清单与哈希，安装前验证；缺件、篡改或旧安装不能误报成功。
- 精简工具集恢复追加与主流程插入，仍保持 30,600 字符预算，修正权限与调试动作说明。
- 多 MCP 进程共享同一浏览器桥接；持有者退出后可接管。未知结果不自动重放点击或输入。
- npm 安装支持无 curl 回退，拒绝非产品目录，注册成功后才删除升级备份。
- 根 PowerShell 引导改用对应版本的 npm 安装器，停止引用不再同步源码的 GitHub main。
- 状态区分安装文件、注册信息与真实浏览器连接，不把准备文件标为扩展已连接。

## 验收证据

| 检查 | 结果 |
| --- | --- |
| Python 完整回归 | 655 passed，249.35 秒 |
| 扩展 JavaScript 核心 | 7 passed |
| Ruff、Node、PowerShell 语法 | 通过 |
| 冻结 EXE 两种工具集、版本与完整 schema | 67 / 43，全部一致 |
| CLI 版本、Native Host、隔离 browser status | 通过 |
| 正常关闭 MCP | stderr 为空 |
| npm 真实隔离安装、重复升级、中文路径 | 通过，密钥和端口保留 |
| npm 无 curl 下载回退及目录保护 | 通过 |
| ZIP | 17 个文件，含扩展 0.1.9，不含 Python 源码、流程或测试目录 |

最终 ZIP SHA-256：`5c81507575e9a8670daf318c11636d8f5edd37ff498d2e72a1d26503b58a879c`。
原始日志位于 `_tmp/release-0.7.29-final.txt`、`_tmp/release-0.7.29-final.xml`、
`_tmp/verified-release.json` 与 `_tmp/install-smoke-0.7.29.txt`。

## 现场边界

Computer Use 已成功打开本机 Chrome，但在读取窗口时无法可靠识别当前 URL，工具结束了本轮
界面控制。因此没有在本轮通过界面完成扩展加载，也没有运行原生京东采集。
安装、协议测试与 RPA 业务验收分别记录，不能据此宣称 Luna/Qwen 的任务成功率已提高。

## 后续能力验收

继续使用现有 32 个双轮场景测评集，重点验收“视觉取点 → 原生绑定 → 单步运行 → 继续编辑”。
记录调用次数、时间、token、选择器命中率、首次成功率与编辑后成功率，并与旧版对照。
先补浏览器真实连接、跨页/弹窗、原生数据采集与断线恢复，再扩展组件覆盖。
工作流预设只作为可选参考，不在 MCP 中硬编码京东等站点路由。
