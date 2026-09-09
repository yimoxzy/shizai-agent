# Luna 浏览器现场测评报告

测评日期：2026-09-09。此次单独启动 `gpt-5.6-luna / medium` 会话，使用完整访问权限和本地 `shizai-agent` MCP，验证网页流程是否会在设计阶段真实打开浏览器、扫描页面、捕获元素，并在第二轮继续编辑同一个流程。

## 测试条件

- 案例：`web-pages`，baseline，seed `27`，trial `2`。
- 会话：`01a0846d-9040-7281-9d22-1eb2fa647fd0`。
- 应用：`Luna评测-组件-web-pages-baseline-707b37ed`。
- 页面：`http://127.0.0.1:18766/shop?seed=27`。
- 输出目标：`_tmp/luna-eval/data/Luna评测-组件-web-pages-baseline-707b37ed.csv`。
- 浏览器控制页已显示并保留为交付页，标题为“组件测评商城”；页面 DOM 现场显示搜索框、查询按钮、商品表、销量原文和“下一页”，当前为第 1/5 页、共 20 个商品。
- MCP 运行报告确认使用当前 Chrome profile；扩展已安装、启用，Native Messaging Host 已连接。此次页面打开和元素捕获不是宿主 HTTP/Python 代替。

## 实际轨迹

第一轮自然语言请求是“打开页面，查询手机，采集所有页商品编号、名称、价格、销量原文、销量口径和链接，去重后写 UTF-8 CSV”。Luna 先分析请求并查询实时组件目录，随后创建原生流程。第一次 `start_design` 因把新应用名当作已有项目引用而失败，模型按错误信息复用同一分析和计划重试成功。

重试后真实完成了以下浏览器链路：

`shizai_rpa_open_browser` → `shizai_rpa_capture_windows` → `shizai_rpa_scan_page` → `shizai_rpa_capture_elements`

扫描结果确认搜索框、查询按钮、六列表格和分页入口均在当前页面。实际任务快照包含三个原生节点：`WebBrowser_CreateV3`、`Element_SetValue_V6`、`WebBrowser_DataCollectionV5`。搜索框已绑定，采集组件仍缺少字段和下一页元素绑定。

第二轮继续使用同一应用，只将搜索词从“手机”改成“笔记本”。模型通过 `read_flow`、`update_step` 预览并保存修改，随后严格校验并提交了一次 `shizai_rpa_run`。同一项目引用和节点 ID 保留，但由于第一轮采集绑定未完成，流程校验仍不通过，CSV 没有生成。

## 结果

| 回合 | 状态 | 耗时 | MCP 调用 | 关键证据 |
| --- | --- | ---: | ---: | --- |
| 创建/网页捕获 | completed | 582.41 秒 | 30 | `open_browser`、`capture_windows`、`scan_page`、`capture_elements` 均成功；保存了原生 `.task` |
| 同流程编辑/运行尝试 | completed | 102.92 秒 | 5 | 同一应用更新查询词并校验；`run` 已调用，但没有运行成功或 CSV 产物 |

本例自动评分为 `failed`，不是“没有打开网页”：`created_apps=1`，浏览器捕获链路成功，失败点是设计态调试通道。`debug_start` 和 `debug_command` 多次报告桌面连接/DAP 超时，`validate_flow` 因采集字段和分页绑定缺失而不通过；正式运行未取得业务成功证据。清理阶段确认调试、捕获和浏览器会话已停止，当前交付浏览器页仍由 Codex 保留。

证据目录：

- [原始 Luna 报告](../_tmp/rpa-benchmark/web-pages-live-baseline-r1/run/report.json)
- [自动评分](../_tmp/rpa-benchmark/web-pages-live-baseline-r1/grade.json)
- [第一轮流程快照](../_tmp/rpa-benchmark/web-pages-live-baseline-r1/run/turn-1-flows/c_yxTJ33b7Rn/io5cd.task)
- [第二轮流程快照](../_tmp/rpa-benchmark/web-pages-live-baseline-r1/run/turn-2-flows/c_yxTJ33b7Rn/io5cd.task)
- [调试清理记录](../_tmp/rpa-benchmark/web-pages-live-baseline-r1/run/suite-debug-cleanup.json)

## 结论与下一步

“边设计边运行”的浏览器入口已经生效：模型能在生成流程期间打开真实页面，并用页面证据捕获元素；第二轮也能复用同一个应用编辑参数。当前端到端网页采集仍被 DAP/桌面调试通道超时和采集字段绑定不完整阻塞，不能把这次测评算作成功采集。

优先修复顺序：

1. 将常规网页流程的元素绑定和整体执行从 DAP 单步调试路径中解耦，`run/status` 直接使用已保存流程；调试只作为失败后的诊断入口。
2. 在 `capture_elements` 后返回可直接复用的字段映射和分页绑定模板，并让 `WebBrowser_DataCollectionV5` 的契约预检一次性指出缺失项。
3. 保留当前 `start_design` 的同一分析引用恢复提示，同时把“新建应用”和“编辑已有应用”参数在工具 schema 上分开，减少第一次 `Project not found` 重试。
4. 组件目录默认先返回摘要，再按模型实际选择的类型读取契约，避免网页案例重复查询十次目录拖慢首轮。

## 代码回归

本轮最新源码完整回归：`471 passed in 139.94s`。
