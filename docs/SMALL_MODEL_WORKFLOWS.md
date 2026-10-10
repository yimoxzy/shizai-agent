# 面向中小模型的流程生成与持续编辑

本次检查和实现基于源码版本 0.7.29。目标是让 Qwen 等约 27B 级模型更少猜测工具和状态，
并能继续修改同一流程。当前没有接入具体 Qwen 权重或推理端点，下面的容量建议是实验起点，
不是模型成功率或速度结论。

## 判断

已有 CLI 核心的价值很高：实时组件目录、原生写入、DeepCapture、静态检查和 DAP 调试都已具备。
MCP 不需要另建一个自然语言引擎。下一步应把“如何选工具、如何定位修改、如何证明完成”做成明确契约。

一句话体验可以在内部包含多次工具调用；模型不必一次生成包含全部原生参数的大 JSON。
用户表达目标，模型负责业务决策，程序负责 ID、参数编码、状态恢复、差异与验证。

## 本次已实现

| 问题 | 实现 | 使用效果与边界 |
| --- | --- | --- |
| 完整工具集同时暴露 Agent、RPA、诊断、UI 等 67 个工具 | `rpa-authoring` 保留 43 个工具，补齐组件追加/主流程插入 | 安装器默认启用精简模式，完整计划创建和持续编辑均可使用；嵌套控制流写图仍未支持 |
| 计划预检的 `plan` 只声明 object | 预检与创建共用计划字段 schema | 模型能看到 steps、稳定 id、component、config、引用字段；保留 loop/condition 等扩展字段 |
| 修改流程需要读完整状态 | `read_flow(detail="edit")` 先返回节点目录，再用 step_id 展开单个节点 | 参数直接来自 `.task`，不使用旧计划代替实际值；包含嵌套节点，但不会把它们解释成线性顺序 |
| 参数修改缺少预览 | `update_step(dry_run=true)` 走实际参数转换，返回 changes 和 metadata_updates | 不落盘、不创建桌面客户端、不调用 UI/RPC；只证明参数转换成功，不代表静态或运行通过 |
| 历史预处理节点无法通过 MCP 清理 | `remove_step(expected_revision)` 删除主流程步骤并重新物化 | 预览同样不落盘、不驱动原生 UI；被删步骤的输出变量仍被下游引用时返回 `step_has_dependents`，需先改绑或显式 `allow_dependents=true`；删除唯一 JS 节点会清掉只描述它的 `mcpUseJs*` 标记 |
| 旧对话可能覆盖用户刚在设计器中的修改 | 原生文件内容 revision，编辑支持 expected_revision | 开始准备和首次写入前检查；冲突返回 error_code、当前版本及重读工具参数 |
| 分页 selector 更新可能在其他参数检查前写元素库 | 页作用域元素先在内存准备 | 参数转换失败时不提前改 elements.json；实际提交仍走原生保存、物化和读回 |
| 分析压缩后保留正确指纹却不恢复完整数据 | 修复分析缓存恢复条件 | 同一 MCP 进程中的 analysis_id + fingerprint 可以恢复完整分析 |
| 完整写图与逐步追加规则混杂 | 分开说明完整计划写图和 add_step 增量追加 | 两种方式都保留组件验证要求；完整图无需再重复追加相同节点 |

按 `json.dumps(tool.model_dump(mode="json"), ensure_ascii=False)` 的列表序列化口径：

| 工具集 | 工具数 | 声明字符数 |
| --- | ---: | ---: |
| 修改前 full | 56 | 36,824 |
| 当前 full | 67 | 45,894 |
| 当前 rpa-authoring | 43 | 30,572 |

精简模式相对当前 full 的声明字符量减少约 33.4%；这不是 tokenizer 测量，不等于延迟或成功率提升。
完整模式因增加参数结构和编辑字段略有增长。完整工具集保留详细结果；精简工具集的目录和创建结果固定使用 compact，保留组件契约、校验和恢复证据，省略重复的全局规则与目录。

## 启用精简工具集

使用本工作区的源码运行时，在宿主已有的 MCP 配置中指定环境变量：

```json
{
  "mcpServers": {
    "shizai-agent": {
      "command": "D:\\agent\\indeed_mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "shizai_agent.core.mcp_server"],
      "env": {"SHIZAI_MCP_TOOL_PROFILE": "rpa-authoring"}
    }
  }
}
```

不指定环境变量则保持 full。修改变量后重新连接 MCP，初始化说明和 tools/list 会一起切换；
不在当前工具集中的名字不能靠 tools/call 绕过。已有发布包需要重新构建才会包含此次修改。
安装后的真实版本和载荷完整性可用 `npx -y shizai-agent-mcp@latest status` 核验，
不得仅凭版本标签推断工具齐全。新增组件用 `add_step`；已有节点按稳定 step_id 编辑，避免重建项目。

精简集支持完整计划创建、参数修改和运行验证，不提供通用图编辑器。需要 Agent graph、
追加节点或更多诊断时使用 full；重排步骤目前有 CLI `workflow rpa-reorder-steps`。
任意节点删除、分支重接和撤销仍需要后续实现，不能声称本次已支持。
嵌套节点可在编辑上下文读取，但 `parameter_edit_supported=false`，当前写入器会在写入前拒绝它；
主容器的 statements 也不能通过参数编辑改写。这样避免节点已落盘后才发现无法静态读回。

## 一句话生成的推荐调用方式

1. 读取原句，调用一次 analyze_request；把原句和 analysis_id 留在宿主会话中。
2. 用 component_catalog 的 summary 筛选候选，再按选中的 component_type 读 contract。
   查询条件由当前任务决定，不一次拉取全部组件的完整契约。
   精简目录默认遵循设计器的隐藏标记；显式 type 查询仍可读取旧流程组件。
   本机引擎 996 条记录中 495 条可见、501 条隐藏，隐藏不等于不可执行。
3. 模型只编排业务字段，填写稳定步骤 id；validate_plan 校验后，通过 start_design 写完整计划。
4. 需要网页时再 open_browser，读取当前页面、捕获并绑定；缺少证据的元素保持未绑定。
5. 完成静态检查。用户要求执行完整图时调用 run，检查终态和真实输出；需要定位单步问题时再使用 debug 工具。

如果用户只要求“生成流程”，以保存结果和明确的验证状态交付；不能自动把创建授权扩大为任意业务执行。
如果用户要求“生成并执行”，同一条用户授权可用于对应工具的 allow_write，不必每个工具都重新询问。
登录、验证码和缺少业务信息属于需要用户参与的情况。

## 继续编辑的调用例子

用户说：“刚才那个流程改成打开新标签页。”首先定位原来的 flow_ref，读取节点目录：

```json
{"flow_ref":"原项目路径","detail":"edit"}
```

调用 `shizai_rpa_read_flow`，确认目标 id 后再调用：

```json
{"flow_ref":"原项目路径","detail":"edit","step_id":"open-chrome"}
```

从返回的契约确定参数，然后调用 `shizai_rpa_update_step`：

```json
{
  "flow_ref":"原项目路径",
  "step_id":"open-chrome",
  "config":{"openNewTab":true},
  "expected_revision":"刚读取的 revision",
  "dry_run":true
}
```

检查 changes。已有用户授权且差异符合请求时，用同样参数设置 `dry_run=false, allow_write=true` 提交；
不用把完整流程再发一遍。保存后读取 step_validation，需要时重新调试受影响的组件及下游。
revision_conflict 时保留 flow_ref，重新读取；不要拿旧参数盲目重试。

CLI 使用同一核心：

```powershell
shizai-agent --json workflow rpa-read PROJECT_PATH --detail edit --step-id open-chrome
shizai-agent --json workflow rpa-update-step --flow PROJECT_PATH --step-id open-chrome --config-json '{"openNewTab":true}' --dry-run --expected-revision REVISION
```

变量引用使用 `{"variable":"message_content"}`；普通字符串（包括 `$message_content`）按字面量保存，预检会提醒疑似变量字符串，不能自动猜测并改写。

冲突时 CLI 输出结构化错误并返回退出码 1；MCP 返回 `isError=true` 和 structuredContent。
native_parameters 保留原生表达式，仅供阅读；不要将它们当作普通字符串原样塞回 config。
config 只传本次要修改的键，未传参数自动保留。实时枚举保留 JSON 类型，例如数字 3 不等于字符串“3”；
薄预检还会拒绝已知列表变量接入 DataFrame 参数，未知类型与表达式仍需运行验证。

## 后续最值得投入的工作

按收益与风险排序：

1. **持久化工作会话。** 用 workflow_id 保存用户原句、原项目路径、验收条件、当前节点和验证证据。
   分析缓存现在仍在 MCP 进程内，跨重启不能靠指纹恢复。恢复后要重新探测页面和调试状态，
   不能把旧 scan_token 或曾经运行成功当成新证据。
2. **语义图补丁。** 定义 update_node / insert_after / remove_node / reconnect / set_output_contract，
   使用稳定 ID，先校验引用和变量依赖，再预览、保存、读回。嵌套循环和分支必须有明确语义。
   同时维护 `.task` 和 mcpExecutionPlan/mcpComponentPlan，防止旧计划与实际参数分叉。
3. **事务与恢复。** 当前 revision 是乐观冲突检测，不是外部设计器锁；多文件写入、RPC、物化失败仍可能留下部分结果。
   应增加项目锁、权威文件快照、失败恢复与恢复日志。发布前必须专门测试并发编辑及 RPC 中途失败。
4. **继续压缩其他阶段结果。** 本次已收紧工具面、编辑读取、组件目录和创建回执。analyze_request 和运行返回仍有重复规则、
   大段诊断。将公共规则放初始化说明，常规结果返回 status / evidence / next_calls；详细诊断按需读。
   小模型应拿到“哪一步、哪个字段、预期类型、怎样修”，而非重新读一整套规则。
5. **Agent graph 补丁。** Agent 分支当前仍主要用整份 draft 保存；需要单独实现稳定 node_id 的局部更新，
   保留边、布局和无关配置，增加保存读回一致性检查。不要将原生 RPA 的参数编辑器硬套到 Agent graph。
6. **模块拆分。** project.py 集中了计划、原生编码、元素、调试支持、输出验收等职责。
   在回归覆盖下逐块提取；本次新契约与编辑读取已放独立模块，避免继续扩张 dispatcher 业务逻辑。

## Qwen 验证方法

先固定具体模型 ID、量化方式、推理引擎、工具调用模板和上下文长度。27B 左右的规模可以作为候选，
但中文理解能力不能替代工具调用、复杂图引用和页面异常恢复能力的实测。
推理参数优先使用该模型发布方推荐的工具调用配置，再控制变量调整；不要套一组所谓通用最优采样参数。

建议用同一组任务对比 full 与 rpa-authoring，每个任务至少重复 3 次，分开计创建与编辑成功率：

| 类别 | 用例 | 验收 |
| --- | --- | --- |
| 简单创建 | 延时后显示消息；创建表格并写固定内容 | 原生文件可读、真实参数有效 |
| 网页创建 | 打开站点、搜索、采集指定字段 | selector 有页面证据，实际输出符合字段要求 |
| 循环与数量 | 采集至少 N 条，允许翻页 | 以实际有效行数验收，不强制某种分页组件 |
| 参数编辑 | 改关键词、URL、等待时间、输出位置 | 同一个项目、目标字段变化、无关节点不变 |
| 引用编辑 | 改输出变量并更新消费者 | 当前尚未实现的图补丁要明确标为能力缺口 |
| 外部修改 | 读取后用户在设计器改流程 | 旧 revision 被拒绝，模型能重读并保留新修改 |
| 中断恢复 | MCP 重启、页面跳转、登录挑战 | 不复用旧捕获证据，不重复建项目，不伪报完成 |
| 无效参数 | 不存在的组件或字段 | 错误能定位，失败不会提前写入元素库 |

记录：首轮 schema 合法率、首次静态通过率、最终业务成功率、编辑保真率、重复建项目次数、
工具调用数、实际输入/输出 token、总耗时、失败后恢复调用数。
用户最终体验取决于整条链路成功率；优化目标应是减少不必要决策和可恢复错误，而非只缩短提示词。

## Luna 实测驱动的后续优化（2026-09-08）

- 目录 compact 保留相同组件契约与分页，移除重复的全局选型规则。实机同一查询的 JSON 字符量：写文件摘要 27,288 → 3,638；File_Write_V1 契约 30,032 → 6,456；无结果 25,348 → 1,844。字符量不是 token 数。
- 精简工具集固定 compact，避免模型主动选择 full 再次读入巨量内容；full 集与 CLI 可选择 response_mode。
- 创建设计回执省略重复目录、完整分析和计划回声，保留项目身份、步骤校验、未绑定节点和浏览器恢复动作。静态通过后的顶层下一步指向实际挂载的工具。
- 组件目录中的设计器静态入口明确标为不可物化，避免选择 Basic_DebugOutput 后才失败。
- validate_plan 在创建桌面客户端之前执行，且去除两次错误后的强制停止；只读工具增加 MCP annotations，混合写入/预览工具不冒充只读。
- CLI 新增 workflow rpa-analyze-request 与 rpa-validate-plan，与 MCP 共用预检核心。
- 原生运行的 output_files 对 .txt 提供最多 8 KiB 的 UTF-8/UTF-8 BOM 文本证据、截断/脱敏/完整标记，保留本次运行新鲜度；非 UTF-8 明确解码失败，截断预览不证明全文。

真实模型测试方法、窗口标识与结果见 [LUNA_RPA_EVALUATION.md](LUNA_RPA_EVALUATION.md)。本轮未接入 Qwen，不能将 Luna 的单个任务结果换算成 Qwen 成功率。

2026-09-09 补充：精简集分析建议先读 summary；运行证据按提交时间过滤旧桌面/JSON 记录及截断半行。start_design 与预检一样可恢复缓存中的 analysis_id；原句不匹配时返回准确原句与重试入口。

多场景实测进一步修正了同族组件版本发现、非网页组件能力索引、输出路径契约、
纯表格误触发网页绑定检查，以及运行清理状态被覆盖的问题。测试方法和实际通过范围见
[LUNA_RPA_EXPANDED_EVALUATION.md](LUNA_RPA_EXPANDED_EVALUATION.md)。
