# 浏览器生命周期修复记录

问题：设计辅助页面和原生运行页面缺少成对的关闭步骤，重复设计、调试和运行后累积标签页。

## 已实现

- 对明确使用 `WebBrowser_CreateV3(openNewTab=true)` 且输出句柄未被覆盖的计划追加原生 `WebBrowser_CloseWeb_V2`。使用上游变量引用，`colse_type=select`、`kill=false`，支持自定义变量名。
- 复用用户页面和 `browser_cleanup_policy=keep_open` 不自动追加关闭组件。原生清理只覆盖正常执行到末尾的路径。
- 增量业务组件插入自动关闭尾节点之前；用户自行添加的关闭节点保留其位置。
- 独立扩展按项目 owner key 复用一个设计标签页，使用 Chrome session storage 保存自己创建的 tab id。相同 URL 的用户标签不视为 MCP 资产。浏览器重启后不继承旧 tab id 的归属。
- `run(wait=true)` 成功、失败、确认停止及异常路径尝试关闭所属设计标签页；等待用户登录/验证时保留页面。`stop` 确认停止后同样清理。
- 状态查询不操作浏览器。`wait=false` 终态查询返回 `browser_cleanup_action`，通过 `browser_session_stop(close_browser=true, allow_write=true)` 结束设计页面；单纯停止捕获默认保留页面，便于继续调试。

## 验证

- Python 全量回归：689 passed；随后新增生命周期用例 3 passed。
- 扩展 DOM 回归：14 passed；真实 service worker 代码配合 Chrome API 夹具的归属回归：5 passed，覆盖并发复用、搜索状态保留、worker 重启、跨项目隔离、手动关闭恢复、非法 owner/URL。
- Python 编译、Node 语法、Git 空白检查通过。
- Windows 冻结包校验：MCP 0.7.31，扩展 0.1.11，full 67 / rpa-authoring 43。npm pack dry-run 通过。
- 已用带 SHA-256 的本地包安装到 `C:\Users\edy\AppData\Local\Programs\ShizaiAgent`，保留 Codex 注册，浏览器 host 注册和包哈希校验通过。

## 尚未验收与发布状态

- 以上归属测试为夹具测试，尚未在重新加载的真实 Chrome 扩展中验收；安装文件更新不等于当前浏览器已加载新代码。需要重启 MCP 客户端并重新加载扩展。
- 未运行用户的抖音完整采集流程来验证关闭步骤。原生异常/强制停止可能跳过末尾关闭步骤；旧页面、句柄被覆盖的页面和旧 Chrome 启动回退路径没有充分归属证据，不会猜测并关闭。
- 源码提交 `9994f9d`，本地标签 `v0.7.31`；Gitee 推送连接无响应后被终止，未确认远端更新。按“Gitee 先、GitHub/npm 后”的发布顺序，GitHub 和 npm 尚未发布 0.7.31。
