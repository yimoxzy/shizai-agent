# 原生 RPA 组件场景测评集

这是一套独立测评资料和运行工具，目标是检验“一句话生成、优先使用合适的原生组件、准确完成、快速交付、同流程继续编辑”。
**34 个场景，每个 2 轮，共 68 条业务请求。** 它们是待测案例，不是 34 个已通过的流程。

入口：[案例全文](CASES.md) · [机器可读案例](cases.json) · [可选预设参考](presets.json) · [评分规则](rubric.json)。

| 组别 | 场景数 | 重点 |
| --- | ---: | --- |
| 本地网页 | 14 | 查询、分页、加载更多、跨页去重、换品类、换字段、页面改版、原生卡片链接、表格链接降级、销量缺失、下界、无结果、iframe、登录阻塞 |
| 真实京东 | 4 | 手机销量一句话、指定条数、价格筛选、评价数与销量切换 |
| 文件 | 4 | 替换、复制覆盖、追加、中文和 emoji 编码 |
| 数据表 | 5 | 筛选计算汇总、去重、双表关联、数字排序、空值和零 |
| Excel | 3 | 真实公式与重算、工作表编辑、CSV 转工作簿 |
| 原生控制流 | 2 | 循环条件、文件存在分支 |
| 结构编辑 | 2 | 中间插入、删除节点，保留已有 ID |

## 京东的一句话案例

第一轮业务原句：**“帮我去京东采集手机销量数据。”**

第二轮：**“还是刚才的流程，改采笔记本销量，保留输出格式。”**

宿主仅补充测试应用名、输出位置和通用测评约定。模型自己分析业务、查看当前页面、查实时组件、选择编排、执行及核验。
其他真实京东案例明确指定 20/30 条、价格区间，以及从评价数切换到销量，以检查它能否迁移和继续编辑。

“快又准”必须同时满足：

- 页面展示的是销量才按销量记录；评价数、付款人数不能偷换，`1万+` 保留原文及下界口径。
- 缺失销量明确标记缺失；登录/验证阻塞如实记录。诚实报告有价值，但不算已完成销量采集。
- 原生组件优先是适用性判断，不是组件越多越好；通用组件能完成时不直接用整段脚本代替。
- 查询后的页面才是采集依据，不能根据查询前的总页数盲目分页；数量、去重、字段对应和链接必须正确。
- 修改保留同一应用及应保留的节点；预览差异后保存，重新验证真实输出。

真实站点没有固定销量答案，必须保留采集时间、链接、页面原文和当前页面证据供独立核验。

## 可选预设参考的边界

`presets.json` 提供 5 类参考：商品列表采集、数据表处理、文件处理、工作簿、已有流程编辑。
它们只描述目标、能力顺序、证据要求、失配条件，**不包含固定网站选择器、商品结果、组件版本或可直接执行的节点图**。
实际参数、输出变量、页面状态和组件版本必须重新查询。

此测评工具用案例声明的 `resource` / `operation` 事实筛选参考：

- `baseline`：不给预设参考。
- `reference`：只附加匹配的参考；模型可在检查后采用或拒绝。
- 未命中：不给任何参考。例如循环场景不会收到商品采集参考。

这属于测评侧的参考投放，不是生产 MCP 的自然语言分类器。**本次没有向 MCP 添加 `京东/手机/销量` 关键词分支。**
将来接入宿主时，可由模型分析结构化任务事实、检索参考，再检查适用条件；MCP 仍负责通用组件契约和原生操作。

## 快速使用

在仓库根目录执行。先列出/校验测评集：

```powershell
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py validate
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py list --group live-commerce
```

准备京东案例的两组提示词，不会启动模型或操作京东：

```powershell
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py prepare --case jd-phones-sales --mode baseline --seed 27 --trial 1 --out _tmp/rpa-benchmark/jd-baseline-r1
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py prepare --case jd-phones-sales --mode reference --seed 27 --trial 1 --out _tmp/rpa-benchmark/jd-reference-r1
```

准备目录包含：

- `prompts.json`：可直接投喂的两轮提示词，兼容现有 Luna 运行器。
- `manifest.json`：宿主专用的验收答案、输入哈希、参考版本、预算和试验配置；**不能提供给被测模型**。
- 输入在 `_tmp/luna-eval/data/inputs/`，输出使用本次应用名，避免相互覆盖。

以 Codex 可执行文件的实际路径运行独立 Luna 会话：

```powershell
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py run --manifest _tmp/rpa-benchmark/jd-baseline-r1/manifest.json --codex '你的 codex.exe 绝对路径' --model gpt-5.6-luna --profile rpa-authoring --timeout 600
```

再运行 reference 组，使用它的 manifest。每个命令只运行一个案例的两轮；不自动跑完整套件。
生成原生应用、文件和执行测试运行由该命令授权，运行器使用独立完整访问会话，模型回退关闭。
测试名称保留 `Luna评测` 前缀以复用现有取证器；实际模型以报告的 `model_resolved` 为准。

`run` 默认工具集为 full，避免用裁剪后的接口判定整体缺失；普通任务可以显式使用 rpa-authoring。
结构编辑必须另测 full。相同对照组必须使用相同工具集。
本运行入口有跨进程桌面锁，结束后清理自己创建的应用所关联的捕获、调试和运行会话；不要同时运行其他桌面自动化测试。

本地网页测试需另开终端启动合成商城：

```powershell
.\.venv\Scripts\python.exe -m benchmarks.rpa.fixture_site --port 18766
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py prepare --case web-pages --mode baseline --seed 27 --out _tmp/rpa-benchmark/web-pages-baseline-r1
```

商城只绑定本机回环地址，不提供数据 API。查询、翻页、加载更多、卡片布局和 iframe 都通过真实 HTML 页面操作。
`seed` 改变商品价格、销量和订单输入，防止凭记忆填写样例答案；基线与参考组必须使用相同 seed。
HTTP 日志包含 User-Agent，宿主烟雾测试请求不能当作模型的原生浏览器证据。按 Ctrl+C 停止自己启动的站点。

## 验收与速度对照

`run` 会写出原始 `run/report.json`、每轮产物和原生 `.task` 快照、`grade.json` 与待填写的 `review.json`。
业务输入先记录哈希，结束/评分时再比较；它不证明中间从未修改后还原，仍须审查工具轨迹。

自动检查包含：

1. 提示词与试验绑定、两轮完成状态、正式运行成功、原生任务快照和快照哈希。
2. 指定输出文件的新鲜度、UTF-8、完整 CSV 行列和数值、工作簿单元格及真实公式。
3. 参数编辑保持同一应用、节点 ID、类型和顺序；结构编辑还需要审查具体插入/删除位置。
4. 耗时、工具调用、目录重复查询、新建应用次数、实际节点类型和疑似脚本节点。

实际任务语义、选择器来源、原生组件适用性以及是否绕过 RPA 必须独立审查。
未完成审查返回 `needs_review`，不会自动宣布成功；真人复核也不能覆盖已失败的客观检查。
在 `review.json` 填写布尔检查、理由和相对 `run/` 目录的证据文件路径，保留报告/manifest 哈希，然后重新评分：

```powershell
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py grade --manifest _tmp/rpa-benchmark/jd-baseline-r1/manifest.json --report _tmp/rpa-benchmark/jd-baseline-r1/run/report.json --review _tmp/rpa-benchmark/jd-baseline-r1/review.json --out _tmp/rpa-benchmark/jd-baseline-r1/grade.json
.\.venv\Scripts\python.exe scripts/rpa_benchmark.py compare _tmp/rpa-benchmark/jd-baseline-r1/grade.json _tmp/rpa-benchmark/jd-reference-r1/grade.json
```

只有 `passed` 的案例才进入成功耗时统计。`fast_and_accurate` 还要求两轮均达到各自秒数和调用数目标。
配对速度差只计算同 seed/trial 且两组都独立验收通过的试验；不能用快速失败和慢速成功比较“提速”。
比较按案例、模型、服务商、工具集、MCP 源码、测评资料版本、页面地址和设计器版本分组，不跨不同任务混算。
还需固定实际机器、登录状态和模型推理配置，交错运行 baseline/reference，至少各做 3 次；准备下一次时递增 trial 并换目录。

第一版时间目标：文件创建约 90 秒、数据表 150–240 秒、工作簿/网页约 240 秒、真实京东约 480 秒；编辑预算通常减半。
这些是待验证目标，不是已达到的性能。现有运行器的每轮计时包含工具/模型等待，不包括会话初始化；服务重试另查日志，不能静默扣除。

`prompts.json` 与案例本身不依赖模型品牌，可以提供给 Qwen 宿主。当前 `run` 适配的是 Codex app-server；
Qwen 宿主需提供等价的工具轨迹、原生任务/每轮产物快照和耗时，不能把 Luna 结果当作 Qwen 结果。

## 本次验证范围

本次验证测评资料、生成器、夹具和评分器，不把单元测试生成的合成报告算作 RPA 成功。
没有自动启动 34 个真实模型任务，也没有预先写入任何“京东销量答案”。
