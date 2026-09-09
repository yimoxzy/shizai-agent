# Luna 多场景原生 RPA 测评

2026-09-09。承接单个文本往返场景的测评，本轮扩展到组件组合、运行和同应用编辑。
独立会话固定 GPT-5.6-Luna / medium，禁止模型回退；每个场景两轮，拥有已授权的完整访问权限。
业务创建、编辑、文件操作和执行均通过本地 shizai-agent MCP。测评模型不能读源码、调用其他模型、
用 shell/Python 代替 RPA 或伪造结果。父会话仅准备隔离输入、测试站点、检查与保存证据。

本轮完成 **7 类场景、14 次独立会话测评、22 个实际发送回合、628 次 MCP 调用**，
实际原生文件中共出现 **32 种组件**。组件物化不代表成功执行。
文件转换已完成创建、运行、同应用编辑的完整闭环；CSV 汇总有金额正确和编辑成功的部分证据，
Excel 有正确产物但未闭环，浏览器、原生控制流和结构编辑仍未完整通过。
最终统计见 `_tmp/luna-eval/expanded/coverage-summary.json`。

## 用例与验收

| 场景 | 第一轮 | 同应用第二轮 |
| --- | --- | --- |
| 文件转换 | 读取 UTF-8 → 文本替换 → 写文件 → 复制 → 读取输出 | 已完成改为已归档，保留输入和路径 |
| Excel 公式 | 真实 xlsx，三行订单、乘法和 SUM 公式，合计 72 | 修改数量与工作表名，合计 96 |
| CSV 汇总 | 筛选已支付、计算金额、按客户汇总排序 | 改成未支付，保留列和路径 |
| 原生控制流 | 原生循环 1…6，条件输出偶数 | 上限 8、改为奇数，保留循环结构 |
| 网页查询 | Chrome 真实捕获表单和表格，输出上海订单 | 城市改为北京 |
| 网页分页 | 杭州 7 条分布于 3 页，去重保序 | 调整 CSV 列顺序，保留全部数据 |
| 结构编辑 | 两个原生写入/读取节点，保存静态检查 | 在中间插入等待，保留原节点 ID |

结构编辑使用 full 工具集，避免把精简工具集的裁剪误判为整个 MCP 的缺失。
其他用例使用 rpa-authoring。涉及桌面的测试严格串行，每例结束后清理本例运行会话。

## 证据与判定

`report.json` 保留模型、会话 ID、工具输入输出、耗时和每轮文件快照；
后续运行器还保存每轮原生 `.task`，用来比较 ID、顺序、参数与实际组件类型。
`completed` 只表示模型结束回合，不证明创建、运行或业务成功。

`scripts/grade_luna_rpa.py` 独立检查 UTF-8、CSV 列顺序/行数/金额与 xlsx 内部的工作表、单元格、
公式和缓存总额。验收记录位于 `_tmp/luna-eval/expanded/artifact-verification.json`。
文件正确仍须结合原应用、运行终态与结构要求人工复核；替代应用补出的正确文件不能证明原流程成功。
目录查询过某个组件、计划包含某个组件、实际物化某个组件和成功执行某个组件分别统计。

## 实测结果

首测共 7 类场景，10 个实际发送的模型回合，292 次 MCP 调用。原生文件中实际写入了
24 种不同组件；这不是 24 种组件全部执行成功。超时后未发送的编辑轮不计作测试通过。

| 首测 | 第一轮 / 第二轮耗时（秒） | MCP 调用 | 结果和主要原因 |
| --- | --- | --- | --- |
| 文件转换 A | 600 / 未进入 | 37 | 读取与替换后，旧 File_Write 因目标不存在失败；模型误改路径斜杠，重跑仍失败 |
| Excel A | 750 / 未进入 | 43 | 原应用未完成；第三个修复应用最终生成正确工作表、公式和总额 72，但模型回合超时，无同应用编辑证据 |
| CSV 汇总 A | 350 / 120 | 45 + 7 | 六组件流程保存，第二轮同应用筛选参数修改成功；两轮运行均被错误的网页绑定检查拦截 |
| 原生控制流 A | 200 / 43 | 65 + 1 | 缺少通用循环与条件的嵌套写图结构，未创建伪流程；模型额外误判“没有文本追加组件” |
| 网页查询 A | 600 / 未进入 | 32 | 实际打开测试页并扫描到元素；绑定 Element_SetValue_V5 时被固定组件白名单拒绝，无 CSV |
| 网页分页 A | 600 / 未进入 | 34 | 已创建多页采集与去重流程；导航调试期望 open_page，实际停在 set_city，未完成翻页和导出 |
| 结构编辑 A（full） | 339 / 49 | 24 + 4 | 首轮按要求保存并通过静态检查；第二轮确认完整 MCP 无插入/重排接口，保留原节点，不伪报成功 |

首测没有一个场景完成“第一轮业务验收 + 第二轮同应用编辑并验收”的完整链路。
Excel 正确产物、CSV 参数编辑和结构编辑首轮静态通过分别记录，不用它们替代完整成功。

实际物化的组件分组：

- 文件/文本：File_Read、File_Write、File_Copy、StrUtil_Replace。
- Excel：Excel_Cs_NewV5、Excel_Cs_OpenV4、Excel_Cs_Rename、Excel_Cs_WriteColumn、Excel_Cs_ReadRange、Excel_Cs_Save。
- 表格：Bivartable_CreateDf、Bivartable_FilterDf、Bivartable_ColumnOperationDf、Bivartable_PivotDf、Bivartable_SortDf、Bivartable_DfWriteToFile、Bivartable_DeleteDuplicateRow、CSV_WriteNew。
- 浏览器/元素：WebBrowser_CreateV3、WebBrowser_DataCollectionV5、Element_SetValue_V5、Element_SetValue_V6、Element_Click_V5、Element_Click_V6。

首测组件清单由实际应用 `.task` 复核，保存在 `baseline-component-inventory.json`。
File A 与 Excel A 启动较早，没有每轮原生文件快照，清单采用事后读取并明确标注；其他场景有每轮快照。
浏览器日志还记录了模型服务的并发限流，耗时包含服务重试，不能全归因于 MCP。

第二阶段保留 A 轮失败，逐步修复并用独立会话复测：

| 复测 | 第一轮 / 第二轮耗时（秒） | MCP 调用 | 结果 |
| --- | --- | --- | --- |
| 文件转换 B | 486 / 121 | 34 + 6 | 仅添加相关版本提示仍选中旧 File_Write，目标不存在失败；参数编辑成功，但不能替换节点类型，仍无产物 |
| CSV 汇总 B | 600 / 未进入 | 45 | 修复错误网页绑定门禁后进入运行；依次暴露旧 CSV_Read 的 GBK 解码、CSV_ReadNew 的 list→DataFrame 类型不匹配、创建表格 mode 误传字符串“3”，三个应用均未完成 |
| 原生控制流 B | 59 / 36 | 12 + 2 | 提前说明线性写图边界后搜索减少，并准确报告不支持；仍未创建或执行循环，不计功能通过 |
| 文件转换 C | 240 / 121 | 27 + 7 | **两轮完整通过**：同一应用、同一 5 个节点，主文件与副本分别为已完成/已归档；两轮 RuntimeHost 均记录 success |
| CSV 汇总 C | 733 / 129 | 61 + 9 | 首轮重建两次、运行五次，最终两轮金额正确且成功执行；第二轮沿用最终应用。排序规则有歧义，不能算明确验收全部通过 |
| CSV 汇总 D | 383 / 117 | 60 + 8 | 改选 Databook_Import；保存后发现写出步骤 outputs 声明缺失，修改 var_name 未同步元数据。两轮运行均被严格检查阻止，无文件 |
| 网页查询 D | 900 / 未进入 | 65 | V6 输入/点击及采集字段完成绑定；四次正式运行均在 city_input 报 JavaScript 错误 010400100020063；随后 DAP next/status 各遇到 120 秒工具超时，无 CSV |

文件 B 的修改预览发现模型意外把变量引用改成字面量，模型在保存前纠正了该差异。
这是预览有效的证据，不代表文件 B 业务已完成。文件 C 的两个 `.task` 快照只有
`replace_status` 的替换词一处差异；实际使用 File_Read、StrUtil_Replace、File_Write_V1、File_Copy_V1。
其第二轮目录额外包含运行器保留的 `.previous-*` 旧副本，旧副本不作为本轮期望输出，
所以通用“所有观察文件都匹配”指标为 false，两个指定产物的人工核验均通过。
源文件文本与夹具一致；原有输入为 CRLF、跟踪夹具为 LF，本例未保存运行前字节哈希，
不将换行归一化比较声称为运行前后字节校验。详情见 `file-transform-c/parent-review.json`。

CSV 原提示词的“客户名称升序”存在中文自然顺序与 Unicode 字符序的歧义。
C 轮曾输出 Unicode 升序的正确金额，随后模型把它当作错误改为甲、乙顺序。
D 轮明确要求 Unicode 字符序，历史提示词保留在各自 report.json，不把这个提示词变化归功于 MCP。
CSV C 的最终应用为 `c_xjeRMZW989`，有 7 个节点，实际包含 Bivartable_Dtype 列类型转换。
编辑快照保留节点 ID 和顺序，除了支付条件，还把 var_ret 的原生字符串“0”规范化成数字 0；
不声称是仅一处原生参数变化。模型第二轮交付文本中的文件链接路径有误，实际文件仍位于
`_tmp/luna-eval/data/Luna评测-扩展-csv-summary-C.csv`；以保存的产物快照为准。
`csv-plan-regression-replay.json` 是旧真实计划在新预检上的只读回放，已证实列表→DataFrame
以及字符串“3”→数字枚举的不兼容能提前被拒绝；它不是新一轮真实 RPA 执行。
CSV D 没有再次走 C 的列表错接方案，也没有触发新的类型拒绝；不能据此声称类型检查
提高了端到端成功率。它又暴露默认输出名/步骤 outputs 元数据未同步的缺口。
第二轮模型曾把筛选输出变量改名，随后发现下游仍引用旧名并恢复；全流程变量重命名仍未实现。

浏览器 D 曾修正元素库 replace/merge 用法，之后通过严格静态检查并正式运行；
修改标签页打开方式、designated_info 与网页对象参数后仍是相同输入错误。
这些是模型尝试的修复方向，不是已证实的根因。初始化 extension_status 另报
`'str' object has no attribute 'get'`，不据此推断扩展已断连。
测试站点的所有 Chrome 请求都为未筛选的第 1 页，没有上海/北京查询，也没有实际翻页请求。
因此未再重复运行已被同类前置问题阻断的分页场景。浏览器 D 最终捕获、DAP、运行清理均确认成功；
本轮结束后仅停止自建测试站点进程，未关闭用户的浏览器或设计器。

可在 Codex 按以下会话 ID 查看代表性复测：

- 文件 C（完整通过）：`01a08266-10fd-7973-8017-ef78f8a1273d`。
- CSV C（金额正确、排序歧义）：`01a0826b-9ff5-71e0-9683-a8aada033ca4`。
- CSV D（输出声明阻塞）：`01a08279-9516-7040-821f-88dd6b385171`。
- 浏览器 D（真实运行失败）：`01a08281-3f78-75a0-9890-87a5462920c1`。

## 测评驱动的修复

- 精确组件契约查询附带同一实时 baseType 的其他版本及描述，不替换模型所选 type。
  文件转换首测选中不能创建缺失目标的旧 File_Write，模型随后误判为路径分隔符问题。
- File B 证明相关版本提示本身不足以纠正选型。为已实测的 File_Write / File.Write 3.3.5.0
  增加“目标须已存在”的明确前置条件，并传递到薄预检的步骤警告；此观察只绑定已验证版本，
  不把旧组件普遍标为不可物化，也不自动替换组件或增加文件操作。
- 从本机设计器前端源码确认 `EReleaseTag.hidden=128` 及实际目录过滤表达式，
  rpa-authoring 默认隐藏设计器也不展示的旧条目。本机引擎 996 条中 501 条隐藏、495 条可见。
  full/CLI 默认保留完整目录；显式 type 查询仍能读取旧节点，提供 include_hidden 开关。
  证据见 `designer-visibility-evidence.json`，这不是按版本号或名称后缀猜测“最新版”。
- 实时下拉、按钮组及单选组参数增加带 JSON 类型的 semantic_choices / semantic_default；薄预检在写图前拒绝
  数字枚举被传成字符串（包括 literal 包装）的错误。表达式及变量引用延后判断，保留原始元数据。
  C 轮再次暴露 list→DataFrame 错接后，增加基于实时 vType 的已知变量类型检查，
  明确不兼容时在创建前拒绝；未知/条件类型及表达式延后判断，变量重赋值清除旧类型知识。
- 为 File、StrUtil、Excel、Bivartable 等家族补充通用能力索引；目录说明多关键词使用 AND。
- 精简 start_design 隐藏旧的顶层 wait_seconds；模型在不同场景里把它当成 RPC 超时，连续触发 schema 错误。
- 输出契约 schema 明确 file、columns、max_rows；预检兼容 output_file，防止实际写 xlsx 却检查默认 result.xlsx。
- 纯文件/表格输出声明列时不再误触发网页元素数量检查。网页采集的真实绑定检查继续保留。
- 运行超时清理后的 stopped 状态不再被旧 submitted 状态覆盖；已停止且只写出部分文件不能由文件新鲜度变成成功。
- schema 明确 steps 是主流程顺序，depends_on 不会生成循环或分支，不宣称完整 MCP 已有插入/删除/重排工具。
  设计器前端实际存在 logic_control_if / logic_control_while_v2 等 codeGenerator 组件；
  缺口在 MCP 的目录暴露与嵌套写图编译，不是设计器缺少循环能力。
- 动态元素绑定读取实时组件的 uiSelector 参数，支持已实际遇到的 Element_SetValue_V5 / Element_Click_V5，
  保留组件 type、浏览器对象引用和其他参数；拒绝把多个元素塞入单 selector 参数。
  V5 泛化有回归测试覆盖；浏览器 D 实际选择的是 V6 输入/点击组件，不能把它的绑定成功
  当成 V5 修复的真实运行验证。
- 精简工具集的空页面扫描恢复提示改为已挂载的 read_flow(detail=edit)，避免引导模型调用被裁剪的组件列表工具。
- 压缩重复工具说明，保留输出与控制流契约，并继续遵守工具 schema 的既定体积预算。
- 精简集创建可省略 query，直接复用 analysis_id 缓存中的原句，减少长请求复制导致的细微不一致。
  显式提供不同原句时仍拒绝；full 工具集保留原来的必填字段。
- 复用现有应用的回执明确 plan_applied=false / saved=false，说明 start_design 复用不会替换节点。
- 文件与 CSV 编辑轮均出现模型复制整份原生参数、误改变量或日志选项的情况。
  update_step 的 config 和编辑上下文现在明确要求只传本次变更字段，未传字段由现有补丁逻辑保留。
  提示文本调整后，编辑与精简工具回归 39 项通过；完整代码检查为 435 项通过。

## 尚未解决的边界

目前仍需补齐的工程能力：默认输出变量与步骤 outputs 的一致性、变量改名后的下游引用维护、
主流程插入/删除/替换以及原生循环分支的嵌套编译。类型预检目前只检查已声明的粗粒度类型，
不能检查 DataFrame 各列的数字类型，也不代替运行验证。
浏览器输入执行、DAP 等待边界与扩展状态返回结构仍需真实运行定位。
运行回执仍包含较多 UI、环境和历史诊断内容；CSV C 首轮工具结果约 115 万字符，
这是含协议包装的字符量，不能当作 token 数，也不能推断 27B 模型能稳定承受这些上下文。

本轮没有证明所有组件组合或复杂流程已可稳定交付。完整结果必须结合下列复现流程，
在固定模型和环境下重复测量；不能把单次探索结果直接换算成模型成功率。

## 复现

启动并登录本机设计器后，另一个终端运行本地测试站点：

```powershell
.\.venv\Scripts\python.exe scripts/rpa_eval_site.py
```

执行串行套件，替换 codex.exe 路径并使用新的 suffix，避免覆盖历史证据：

```powershell
.\.venv\Scripts\python.exe scripts/luna_rpa_suite.py `
  --codex '你的 Codex 0.153.4 或兼容版本 codex.exe 绝对路径' `
  --cases file-transform excel-formulas csv-summary native-control-flow browser-search browser-pagination structural-edit `
  --suffix 新编号 --timeout 600
.\.venv\Scripts\python.exe scripts/grade_luna_rpa.py
```

套件准备已跟踪的只读输入夹具；若已有输入被改变则拒绝重置，保留失败证据。
测试站点只提供 HTML 页面，无业务数据 API；请求日志记录城市、页码和 User-Agent。
浏览器访问与父会话 HTTP 烟雾检查必须区分，不能把后者计作原生 RPA 翻页。

本机 Windows、设计器、Office/WPS 和浏览器状态都会影响结果。这是定性失败探索，
单次不同任务的结果不能当成稳定成功率；本轮未运行 Qwen，也未发布新安装包。
