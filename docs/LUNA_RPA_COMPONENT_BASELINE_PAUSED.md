# Luna 原生 RPA 组件场景测评报告

更新时间：2026-09-09T10:01:40+08:00。状态：**paused_for_navigation_fix**。

计划覆盖 **32 个场景、37 个独立会话、74 条业务请求**。当前实际启动 **2** 个会话，已评分 **2** 个；完整创建及同流程编辑独立验收通过 **0** 个。尚未运行项目不计入通过率。
实际发送 2 轮，模型结束 0 轮，已完成 MCP 调用 30 次；正式运行调用 0 次，其中工具报告终态 success 为 0 次。运行成功仍需业务产物和编辑验收。

## 测量条件

- 模型固定 `gpt-5.6-luna / medium`，以每个 report.json 的 model_resolved 核验，禁用回退；provider 为本机配置的 custom。
- 独立持久会话，完整访问权限、无需交互批准；通过 app-server 创建。应用内已打开首个测试会话；未验证独立操作系统窗口。
- 每轮限时 600 秒；桌面操作严格串行，每例清理已创建应用关联的捕获、调试、运行会话。
- 普通场景使用 rpa-authoring；原生控制流和结构编辑使用 full。同一对照组工具集一致。
- 固定 seed=27、trial=1，每组仅一次探索测量；不能据此估计稳定成功率或推断 Qwen 27B 表现。
- 5 组 baseline/reference 对照：京东手机销量、文件替换、分页、数字排序、Excel 公式；相邻配对，交替 A/B 顺序。
- 测评期间冻结 MCP 和测评集；保留 SHA256。参考只来自通用工作流资料，没有向 MCP 添加京东/手机/销量路由或预置答案。
- 宿主只准备输入及测试 HTML、记录及核验产物；模型不能读取源码/答案、调用其他模型或用宿主 shell/Python/HTTP 代替业务。
- 评分先核对文件内容、原生任务、运行终态及同应用编辑，再独立审查语义/原生组件适用性/页面证据/绕过行为。
- 实际物化组件统计覆盖全部原生快照；grader 的 native_task_present 只关联正式运行，不能用它判断未运行流程是否已创建。

## 逐例结果

第一轮是创建，第二轮是同流程编辑。耗时包括模型/工具等待，不包括初始化；未发送的编辑轮不算完成。

| 场景 | 组别 | 完整链路 | 创建/编辑秒 | 创建/编辑 MCP 调用 | 证据 |
| --- | --- | --- | --- | --- | --- |
| jd-phones-sales | baseline | 未通过 | 600.09 / 未发送 | 14 | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-phones-sales-baseline-r1) |
| jd-phones-sales | reference | 未通过 | 600.25 / 未发送 | 16 | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-phones-sales-reference-r1) |
| file-replace | reference | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/file-replace-reference-r1) |
| file-replace | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/file-replace-baseline-r1) |
| web-pages | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-pages-baseline-r1) |
| web-pages | reference | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-pages-reference-r1) |
| table-numeric-sort | reference | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-numeric-sort-reference-r1) |
| table-numeric-sort | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-numeric-sort-baseline-r1) |
| excel-formulas | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/excel-formulas-baseline-r1) |
| excel-formulas | reference | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/excel-formulas-reference-r1) |
| web-single | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-single-baseline-r1) |
| web-overlap | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-overlap-baseline-r1) |
| web-more | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-more-baseline-r1) |
| web-keyword-edit | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-keyword-edit-baseline-r1) |
| web-column-edit | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-column-edit-baseline-r1) |
| web-layout-edit | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-layout-edit-baseline-r1) |
| web-sales-absent | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-sales-absent-baseline-r1) |
| web-sales-bound | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-sales-bound-baseline-r1) |
| web-empty | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-empty-baseline-r1) |
| web-frame | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-frame-baseline-r1) |
| web-login-gate | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/web-login-gate-baseline-r1) |
| jd-sales-20 | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-sales-20-baseline-r1) |
| jd-price-range | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-price-range-baseline-r1) |
| jd-reviews-distinction | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-reviews-distinction-baseline-r1) |
| file-copy | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/file-copy-baseline-r1) |
| file-append | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/file-append-baseline-r1) |
| file-bom | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/file-bom-baseline-r1) |
| table-aggregate | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-aggregate-baseline-r1) |
| table-dedupe | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-dedupe-baseline-r1) |
| table-join | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-join-baseline-r1) |
| table-missing | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/table-missing-baseline-r1) |
| excel-rename | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/excel-rename-baseline-r1) |
| excel-from-csv | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/excel-from-csv-baseline-r1) |
| control-loop | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/control-loop-baseline-r1) |
| control-branch | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/control-branch-baseline-r1) |
| edit-insert | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/edit-insert-baseline-r1) |
| edit-delete | baseline | 尚未运行 | 未发送 / 未发送 | — | [试验目录](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/edit-delete-baseline-r1) |

## 独立核验记录

### jd-phones-sales · baseline

独立核验 report.json 全部业务调用与 task 快照。选择了原生 GoUrl/DataCollectionV5，但 GoUrl.web 需要 rpa_hWeb，实参为字符串 chrome，无上游创建网页对象；预检仍 valid=true，通用 semantic_values 提示有 chrome 示例。open_browser/capture_windows/browser_session_start 均工具超时 120s，首轮 600.09s 超时；无页面采集和输出，无第二轮。仅宿主只读读取 Browser SKILL.md，没有业务文件绕过。保存了两节点骨架；grader 的 native_task_present 仅表示正式运行关联图，不能解释为完全未物化。清理已确认。

会话：`01a083ce-ca7e-7902-867f-b63a868dea45`。 [原始报告](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-phones-sales-baseline-r1/run/report.json)。

### jd-phones-sales · reference

独立核验原始调用。参考组采用 GoUrl + DataCollectionV5；GoUrl.web 使用默认字符串 网页对象，没有先创建或获取网页对象，预检仍通过。browser_session_start 成功且 capture_windows 确认无 Chrome 页面，但调用 open_browser 没有完成有效导航；随后 open_browser、capture_windows、status 超时，首轮 600.25s 超时，无输出、无编辑轮。扩展状态接口另有异常。没有用脚本生成结果；选择原生组件仍不等于正确编排。用户指出应在设计中实际运行浏览器，此后暂停旧版本测评并修复通用指引和对象契约。清理已确认；不是要求用户预先手动开浏览器。

会话：`01a083d8-995b-7a40-b434-ae132588b25d`。 [原始报告](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/jd-phones-sales-reference-r1/run/report.json)。

## 参考预设与速度

只对两组均独立验收通过的配对计算速度差。失败结束更快不能算提速。
当前满足两组均通过的配对：0。
完整通过案例耗时中位数：无可计算样本；同时达到各场景速度目标的完整通过案例：0。

## 实际组件与原始证据

原生任务快照出现 2 种组件。出现不代表执行成功，节点重复快照不代表独立执行次数。

`WebBrowser_DataCollectionV5`, `WebBrowser_GoUrl`

[机器可读汇总](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/summary.json) · [对照统计](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/comparison.json) · [运行队列](D:/agent/indeed_mcp/_tmp/rpa-benchmark/luna-20260909/campaign.json)

原始证据保存在试验目录的 run/report.json、events.jsonl、每轮 task/产物快照、review.json 与 cleanup.json。
本报告不将此前 7 场景测评或单元测试混入本轮结果。
