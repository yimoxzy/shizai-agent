发布 `0.7.28` Windows x64 包。

本版本修复小模型在一句话 RPA 生成和后续编辑中的确定性问题：

- 计划 `outputs` 统一表示逻辑变量名；模型省略时由 MCP 从实时组件契约和赋值参数自动生成，并纠正误填的 `var_name`、`var0` 等原生槽位名。
- `mcpExecutionPlan.steps` 成为权威计划源，参数编辑会同步更新兼容镜像，避免 materialize 覆盖刚完成的修复。
- 相同请求的重复分析复用缓存并返回调用次数，减少小模型重复获取大段上下文。
- 页面扫描失败后明确限制加深重试，继续使用当前页面的官方 DeepCapture 拾取，不复制其他流程的 selector。
- 未绑定真实元素的网页组件现在会在运行预检阶段返回 `element_binding_missing`，不会再被误报为“不依赖网页元素”并进入无效调试。
- 当扩展未暴露 Document 时，扫描结果会区分全自动 JS 组件路径与需要用户点击的官方拾取路径；`javascript`、浏览器脚本等常见查询也能找到原生“执行JS”组件。
- 精简创建响应会折叠重复的逐项校验和调试门禁明细，保留错误、分页与下一动作，降低小模型的上下文负担。
- Codex 默认注册 `rpa-authoring` 工具集并启用本地 MCP；npm 注册会回读磁盘验证配置，重复注册不会留下重复表或孤立环境子表。
- `open_browser` 从已保存导航步骤快速打开当前 Chrome 页面，不再让设计态 DAP 阻塞同步 MCP 服务；原生节点仍由后续 debug 工具验证。
- 修复精简 profile 对字符串 `next_action` 的错误假设，并兼容旧扩展清单中的字符串 `action` 字段；`extension_status` 不再触发 `.get` 异常。
- `project_list` 改为浅层名称查找，真实环境由约 33 秒降至约 0.27 秒；`capture_windows` 仅返回后续绑定所需的窗口身份和就绪摘要。
- 失败的设计器 DAP 接管在清理时也会断开共享调试连接，避免旧会话让后续流程持续等待端口；模型说明限制同一 DAP 停靠或端口错误在清理后只重试一次。
- 内置组件显式配置 `var_name`、`var0` 等输出名称时，计划校验与原生物化现在使用同一逻辑变量名，后续步骤可直接引用，不再因固定默认名产生伪数据流错误。
- 新增独立的 Manifest V3 MCP 浏览器扩展，通过截图和少量视觉坐标在浏览器本地命中 DOM，不向模型发送完整页面树。
- 新增 6 个浏览器工具，支持活动标签页截图、过期 revision 拒绝、高 DPI 坐标换算、交互元素提升、重复商品区域推断和原生步骤直接绑定。
- 新增 loopback WebSocket 与 Native Messaging bootstrap，使用扩展 ID 白名单和每次安装随机 token；发布包包含独立 browser host EXE。
- `install --with-browser-extension` 可选准备 Chrome/Edge host 和扩展目录；浏览器仍由用户完成一次“加载已解压的扩展”。
- 浏览器证据导入后保存 `browser_bridge + verified` 来源，并同步替换节点 `mcpElementIds`，保证后续回读和再编辑指向当前 selector。

验证：完整 Python 回归 `594 passed`；浏览器核心 `6 passed`；本次变更文件 Ruff、Node/PowerShell 语法、npm dry-run 和注册/注销测试通过。最终 ZIP 的 SHA-256、19 个文件、冻结版 MCP 62/37 工具初始化及 Native Messaging bootstrap 均已验证。
