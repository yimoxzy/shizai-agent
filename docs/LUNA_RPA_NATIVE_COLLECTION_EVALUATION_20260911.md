# Luna 原生数据采集与同流程编辑测评报告

测评日期：2026-09-11。总体结果：**失败，暂不满足发布条件**。

本次使用独立持久 Codex 线程、精确模型 `gpt-5.6-luna / medium`、`rpa-authoring` 工具集和完整本地访问权限。模型没有回退，也没有通过 shell、Python、JS 或手工文件绕过 RPA。

## 测试条件

- 线程：`01a08e17-f6cf-7c93-b728-51b8ec9de392`
- 应用：`Luna评测-原生采集接线-20260911`
- `project_ref`：`C:\Users\edy\AppData\Roaming\Z-Factory\applications\c_gNw6mF3jrN`
- 页面：`http://127.0.0.1:18766/shop?layout=cards&seed=45`
- 首轮：搜索“耳机”，原生采集六列并写 UTF-8 CSV
- 第二轮：只把同一流程的搜索词改为“相机”，保持采集字段、组件和输出路径不变
- 输出：`D:\agent\indeed_mcp\_tmp\luna-eval\data\Luna评测-原生采集接线-20260911.csv`

## 验收结果

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 精确使用 Luna | 通过 | requested/resolved 均为 `gpt-5.6-luna` |
| 真实打开浏览器 | 通过 | Chrome 窗口标题为“组件测评商城” |
| 优先使用原生采集 | 通过 | 使用 `WebBrowser_DataCollectionV5`，未使用 JS 采集 |
| 六列同卡片绑定 | 通过 | 商品编号、名称、价格、销量原文、销量口径、链接共 6 个已验证元素 |
| 商品链接读取 | 通过（静态） | 链接列 `dataAttrName=href` |
| 浏览器对象接线 | 通过 | 搜索和采集的 `designated_obj` 均引用 `WebBrowser_CreateV3` 输出 `网页对象` |
| 表格变量接线 | 通过 | `CSV_WriteNew.table` 引用采集输出 `数据采集结果` |
| 避免重复写文件 | 通过 | `WebBrowser_DataCollectionV5.toExcel=0` |
| 严格流程校验 | 通过 | 4 个步骤、7 个绑定元素、0 error、0 warning |
| 同流程后续编辑 | 通过 | 同一 `project_ref`，仅 `search_query.text` 从“耳机”改成“相机” |
| 首轮真实 CSV | 失败 | 未取得有效数据行 |
| 编辑后真实 CSV | 失败 | CSV 已生成但只有表头，数据行数为 0 |
| 运行清理 | 通过 | 最终状态 `stopped`，遮罩关闭，Chrome 保留可复用 |

## Luna 行为

第一轮耗时 782.25 秒，共调用 40 次 MCP。Luna 正确完成请求分析、实时目录查询、计划校验、创建流程、打开 Chrome、窗口捕获、页面扫描、搜索框与六字段捕获、严格校验和运行尝试。它查询组件目录 11 次，虽然最终选型正确，但目录契约获取仍明显偏慢。

第二轮耗时 157.19 秒，共调用 9 次 MCP。Luna 先读取同一流程，用一次预览和一次保存更新搜索词，随后严格校验和提交运行。没有创建或复制第二个应用，编辑思路正确。

## 运行失败原因

设计态调试连续出现三类真实错误：已有会话停在组件实现内部、等待首个组件断点超时、`debugpy port was not published`。失败状态保存为 `runtime_error`，但仍带有 `run_owned=true`。旧实现只把非终态会话视为 active，正式运行因此可能跳过清理。

已修复这个通用生命周期问题：调试能力探针现在返回 `cleanup_required=true` 和 `shizai_rpa_debug_stop`，正式运行也会先清理失败但仍持有运行所有权的会话。相关回归测试通过。修复后的真实复跑确认清理确实执行成功。

清理后流程最终能够执行到终态，但业务输出不合格：CSV 只有六列表头，数据行数为 0。流程确实使用了原生 `WebBrowser_DataCollectionV5`，问题出在 MCP 生成的列 selector。DeepCapture 对普通文本返回 Chrome UIA 的 `Group/Text` 路径，旧实现将其猜成 `div/span`；实际页面结构是 `article > p`，因此组件找不到任何数据。链接字段的 `a[innertext=查看商品]` 不能弥补其它五列均未命中的问题。

本轮已增加通用严格校验：卡片字段只有 UIA 路径且没有真实 web/CSS/XPath 证据时，返回 `native_collection_dom_selector` 错误并阻止把静态校验当成成功。该规则不包含站点 selector；原生表格语义、真实网页 selector 和通用预处理生成的稳定元素仍可使用原生采集组件。

## 结论

本次回答了“原生数据采集组件是否使用”：**已使用，但卡片字段的原生采集 selector 不正确**。Luna 能完成同一流程的参数编辑，但旧版静态校验错误地接受了 UIA 推导出的 DOM 路径。

端到端业务验收仍失败。当前不能宣称“原生采集成功”，也不能发布 `0.7.28`。下一步应让 MCP 接入设计器原生数据采集拾取/预览结果，或生成带真实 DOM 证据的通用采集元素，再用相同提示词复测；复测必须同时取得新鲜 CSV、六列表头、相机结果和真实链接证据。

## 代码回归

- Ruff：`All checks passed!`
- pytest：`553 passed in 282.01s`

## 证据

- [原始模型报告](../_tmp/luna-eval/native-collection-edit-v1/report.json)
- [模型事件](../_tmp/luna-eval/native-collection-edit-v1/events.jsonl)
- [首轮流程快照](../_tmp/luna-eval/native-collection-edit-v1/turn-1-flows/c_gNw6mF3jrN/io5cd.task)
- [第二轮流程快照](../_tmp/luna-eval/native-collection-edit-v1/turn-2-flows/c_gNw6mF3jrN/io5cd.task)
- [测评提示词](../_tmp/luna-eval/native-collection-edit-prompts-v1.json)
