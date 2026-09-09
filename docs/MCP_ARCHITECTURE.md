# Shizai Agent MCP / CLI 架构基线

版本：`1.0`（架构设计稿）
适用范围：本地 CLI、stdio MCP、实在设计器原生 RPA、Chrome DeepCapture 以及后续 Skill。

> 本文是当前 MCP/CLI RPA 实现的架构基线。MCP 是共享 CLI 核心的薄封装：模型负责理解和编排，MCP 只提供实时能力目录、最小边界校验和运行诊断；兼容旧版 staged 调用的 strict 分支仅用于历史脚本和旧流程。

规则的新增位置、中文格式和模型可见范围见 [`MCP_RULES.md`](MCP_RULES.md)。

## 1. 核心结论

大模型负责“理解和设计”，MCP 负责“提供能力和守住边界”，实在设计器负责“保存和执行”。三者不应互相越权。

```text
用户自然语言
    │
    ▼
当前大模型（意图理解、任务拆解、方案选择、流程编排）
    │  查询能力、提交计划、读取证据、修复错误
    ▼
MCP（工具门面、实时组件契约、结构校验、安全与运行诊断）
    │  写入 .task / 元素库 / 应用元数据
    ▼
实在设计器（原生组件、RuntimeHost、浏览器和桌面运行时）
    │
    ▼
运行状态、日志、输出文件和可验证证据
```

MCP 可以告诉模型“有哪些组件、每个组件支持什么参数、当前是否可用、调用是否安全”，但不应该替模型决定业务流程。相同目标允许存在多种合法方案，例如：

- 使用数据采集组件内置分页；
- 使用循环组件点击下一页；
- 使用 JavaScript 返回当前页数据，再由模型编排循环；
- 使用站点提供的接口或下载文件后再用 Excel/文件组件处理。

只要方案使用真实存在的组件、参数和元素证据，并满足运行安全边界，就应该允许提交和验证。

## 2. 分层职责

| 层 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| 当前大模型 | 识别用户意图；拆解原子任务；选择组件；决定顺序、循环、条件、分页和变量；根据页面证据生成 selector/JS；读取错误并修复现有流程 | 不猜测不存在的组件；不绕过 MCP 校验；不把未经验证的 selector 直接标为可运行 |
| MCP 能力层 | 暴露工具；读取实时组件目录；返回参数/输出契约；读写项目；校验结构和参数；管理 DeepCapture；执行权限保护、运行前检查和诊断 | 不内置京东等站点配方；不规定唯一拆解方式；不强制一种分页方式；不替模型完成意图判断 |
| 实在设计器/运行时 | 提供原生组件模板、应用和 `.task` 结构；实际控制浏览器/桌面；执行组件；产生日志和输出 | 不承担自然语言规划；不替 MCP 判断调用权限 |
| Skill | 给模型提供工作方法、工具说明、注意事项和修复策略；解释“如何使用契约” | 不作为第二个意图识别器；不把示例流程变成站点硬编码；不代替 MCP 的运行时校验 |

### 2.1 意图识别的边界

`shizai_route_intent` 可以作为轻量路由提示，帮助区分 Agent graph、浏览器 RPA 和桌面 RPA。但它只能是分类和下一步建议，不能成为完整计划生成器。分类不确定时，应把不确定性返回给模型，由模型决定是否继续询问用户或读取更多上下文。

`shizai_rpa_analyze_request` 的目标是提取原文中的证据，例如目标网站、用户明确要求的字段、数量、输出位置、登录/挑战提示和约束。它可以给出候选操作或建议，但不得把候选操作当成唯一流程，也不得强制模型为每个候选操作提供固定的 `operation_ids` 映射。

## 3. 标准请求生命周期

下面是推荐生命周期，不是必须逐字执行的脚本。模型可以根据任务跳过无关阶段，也可以在一个阶段内多次读取证据；MCP 只在真正需要写入、捕获或运行时执行硬校验。

### 阶段 A：理解请求（只读）

1. 模型读取用户原句。
2. 可选调用 `shizai_route_intent`。
3. RPA 请求可调用一次 `shizai_rpa_analyze_request` 获取证据包。
4. 模型保留原始请求和分析结果，避免把 MCP 的建议误当作用户要求。

输出重点是“用户想达到什么结果”，不是“必须生成哪些步骤”。

### 阶段 B：发现能力（只读）

模型调用 `shizai_rpa_component_catalog` 或 `shizai_components`，读取当前安装版本的实时契约。查询可以按能力、包、分类或关键词筛选；模型应优先看契约字段，而不是根据中文标题猜组件类型。

能力目录至少要能回答：

- 真实 `component_type`/`type` 是什么；
- 组件属于哪个包、版本和运行模式；
- 输入参数的名称、类型、默认值、必填性、枚举/范围和别名；
- 输出变量的名称、类型和绑定方式；
- 是否需要页面元素、是否允许延迟绑定；
- 是否已安装、业务包是否导入、是否可以物化；
- 组件有哪些能力标签（如 `navigate`、`input`、`extract`、`loop`、`paginate`、`output`），这些标签用于匹配，不用于规定流程。

### 阶段 C：模型编排（只读草稿）

模型根据用户目标和实时契约自由组合组件。它可以设计线性步骤、条件分支、循环、子流程、异常恢复或 JS 方案。分页是业务策略，不是 MCP 的固定协议；模型应说明选择该策略的原因，并在目标需要时设计终止条件。

计划可以先作为内存草稿，必要时调用 `shizai_rpa_validate_plan` 做薄边界预检。它只检查 JSON 形状、实时目录中的组件/参数、引用字段类型、语义值和安全边界，不评价“是不是唯一最佳业务方案”，也不检查首节点、步骤覆盖、分页组件或固定输出映射。

### 阶段 D：持久化与页面证据

模型决定方案后，调用 `shizai_rpa_start_design` 创建或复用项目。计划中的循环、分支、分页和组件扩展字段按模型方案原样保存；依赖页面的元素可以保留为 `unbound`/延迟绑定状态。

真正进行浏览器或桌面 I/O 前：

1. MCP 确保 DeepCapture 会话已初始化；
2. 模型读取当前窗口、页面树或 OCR 证据；
3. 只把同一页面快照捕获的真实 selector 绑定到对应步骤；
4. 页面跳转后重新获取证据，不复用过期的 `scan_token` 或元素 ID。

DeepCapture 会话有三个明确的 MCP 入口：

```text
shizai_rpa_browser_session_start(project_ref, allow_write=true)
shizai_rpa_browser_session_status(project_ref)
shizai_rpa_browser_session_stop(project_ref, allow_write=true)
```

`session_start` 只建立 `startDeepCapture(packagePath)` 读取通道，不启动官方红色拾取层；`shizai_extension_start_capture` 才会再调用 `startDeepCaptureInspect(mode)`。`session_status` 是跨 MCP/CLI 进程共享的 last-known 状态，返回 `truth_source=last_known_local_state`，不能替代一次真实读取或运行期预检。读取、官方拾取、原生运行期分别通过 `control_mode=capture_read`、`official_picker`、`workflow_runtime` 区分。

### 阶段 E：校验、物化与运行

模型可按计划调用步骤、流程和项目校验，然后物化、同步诊断并运行。MCP 负责检查环境和安全条件；运行时负责实际执行。运行成功必须同时有状态、日志和输出证据，不能以“提交成功”或“静态校验通过”代替业务成功。

### 3.1 增量验证门

共享核心的 `add_rpa_step` 和 `validate_rpa_step` 返回 `verification_gate`，CLI 与 MCP
不各自实现一套流程规则。使用 `add_step` 增量追加时，每个组件都应完成：

```text
写入当前组件
 -> step_validation.valid=true
 -> debug_capabilities
 -> debug_start 或复用会话
 -> debug_command(action=next)
 -> debug_status 与页面/运行证据
 -> 再写入下一组件
```

`next_action` 描述的是结构上的候选下一步，只有 gate 的静态和运行验证同时通过后才可执行。
运行证据必须能说明当前组件到达了正确断点/终态，并确认了页面可见效果、输出或其它组件专属结果。
静态通过、请求 accepted、DAP 已连接或单纯停靠都不算证据；失败时修复原步骤和同一个
`project_ref`。CLI 使用 `workflow rpa-*` 对应命令，MCP 只是把同一共享核心返回为 stdio 工具。
OCR 可以帮助判断可见页面状态，但不生成 selector，也不替代 DeepCapture 或 RuntimeHost。

`start_design` 已提交完整计划时，组件图可以先整体持久化，再补齐必要捕获、绑定和静态检查。
`rpa-authoring` 对完整图使用 run/status 与实际输出验收，只有定位单步问题时才进入调试；
run 内部仍执行项目诊断。不要重复调用 add_step 创建相同节点，也不要把静态通过当作执行成功。
`debug_command(action=next)` 在跨过当前组件边界或确认当前组件终态后返回
`runtime_verification`，共享核心将其适配为 gate 的运行验证状态；网页可见效果仍需模型用页面证据确认。

## 4. MCP 的硬边界与软建议

### 4.1 必须硬失败的内容

这些是能力或安全问题，MCP 应拒绝写入或运行，并返回机器可读错误：

- 组件类型不存在、未安装或当前项目无法物化；
- 参数名称不存在、类型错误、枚举值/范围非法；
- 必填参数缺失，或参数无法转换为设计器支持的原生格式；
- 步骤 ID 重复、引用字段不是数组，或元素 ID 为空；
- 元素组件仍是 `unbound`，或 selector 没有来自有效 DeepCapture/导入证据；
- 业务组件包要求导入但当前项目未导入；
- 运行前缺少 Chrome/Document、RuntimeHost、运行插件、登录或用户必须确认的挑战；
- 未传 `allow_write=true` 的写入、运行、调试或 UI 控制；
- 路径、凭据、脚本或文件操作违反安全策略。

MCP 薄预检不会替代运行时对变量、边、元素绑定和组件原生参数的最终检查；这些错误应由共享 CLI/设计器返回具体路径和原生错误。

### 4.2 只能作为 warning 的内容

这些是设计选择，不应阻止模型继续尝试：

- 使用了循环而不是组件内置分页；
- 没有优先选择可覆盖目标的原生组件；
- 计划分成多个组件而不是一个“全能”组件；
- 没有使用 MCP 推荐的组件或 planning pattern；
- 步骤顺序与分析结果中的候选顺序不同；
- 计划暂时没有明确的分页实现，或分页由循环/脚本/下载完成；
- 计划使用 JS、下载、接口或 Excel 中间步骤来替代网页采集组件；
- 某个组件能力标签与操作不是一一对应；
- 结构合法但可能无法满足用户的数量、字段或性能目标。

warning 应包含原因、影响、可选改进和验证方式，不应把 warning 改写成隐含的必填字段。原生组件优先属于
`advisory`/`warning_only` 建议：如果实时目录中有可物化且能力匹配的原生组件，模型先评估它；网页结构化
采集优先评估 `WebBrowser_DataCollectionV5`。模型仍可根据页面证据选择 JS、下载、循环或其它组合，
但应说明原因并验证所选方案。MCP 不设置固定纠错次数；模型可以根据错误证据重新设计现有步骤。

### 4.3 结果契约的处理

当用户明确提出“至少 100 条”“必须包含标题和价格”等验收条件时，MCP 负责保存这些条件并在运行后检查结果。MCP 不规定如何达到 100 条；它只报告实际行数、缺失列、空值和输出文件状态。若结果未达标，返回 `incomplete`/`failed` 证据供模型修复，而不是在设计阶段强行指定某个分页组件。

## 5. 组件契约规范

组件目录是 MCP 最重要的公共接口。每个组件应返回稳定的契约对象，建议包含以下字段：

```json
{
  "type": "WebBrowser_DataCollectionV5",
  "label": "数据采集",
  "package": "WebBrowser",
  "version": "7.3.7",
  "description": "从页面元素提取结构化数据",
  "capabilities": ["extract", "output"],
  "operation_types": ["extract"],
  "availability": "installed",
  "requires_import": false,
  "materializable": true,
  "inputs": [
    {
      "name": "designated_obj",
      "type": "element_selector",
      "required": true,
      "deferred_binding": true
    },
    {
      "name": "collect_items",
      "type": "integer",
      "required": false,
      "default": 20,
      "minimum": 1
    }
  ],
  "outputs": [
    {"name": "table", "type": "table", "primary": true}
  ],
  "options": {},
  "config_aliases": {
    "max_rows": "collect_items",
    "output_columns": "columns"
  },
  "element_requirements": ["collection_root"],
  "runtime_requirements": ["chrome_document"],
  "code": {"callable": "designer-native"}
}
```

### 5.1 输入参数

每个输入必须说明：

- `name`：写入 native 组件时的真实名称；
- `type`：`string`、`integer`、`number`、`boolean`、`array`、`object`、`expression`、`variable`、`element_selector` 等；
- `required`、`default`；
- `enum`、`minimum`、`maximum`、长度或格式约束；
- `deferred_binding`：是否可以先写组件、后绑定页面元素；
- `semantic` 与 `native_encoding`：模型可使用的语义值，以及 MCP 如何转换为设计器原生编码；
- `aliases`：兼容 CLI/MCP 的可读别名。别名只能归一化，不能改变语义。

MCP 应接受契约声明的全部合法参数；未知参数可以拒绝，但不能只允许一小组硬编码字段，导致实时目录已经支持的参数无法更新。

### 5.2 输出参数

输出至少说明名称、类型、是否主要输出、是否可导出以及变量绑定格式。模型可以把一个组件的输出连接到任意后续组件，只要引用符合图结构和类型校验。

### 5.3 元素和运行时要求

组件可以声明元素角色、页面身份、登录状态、运行插件、文件权限等要求。声明是校验依据，不是流程编排指令。例如组件声明 `paginate` 能力，不代表模型必须使用它；组件声明需要 `element_selector`，则 MCP 必须在运行前确保 selector 已验证。

## 6. 计划数据模型

计划模型应表达“模型选择的方案”，而不是表达某个 MCP 内部配方。建议最小结构如下：

```json
{
  "name": "京东手机采集",
  "target": {"url": "https://www.jd.com"},
  "steps": [
    {
      "id": "search",
      "component": "Element_SetValue_V6",
      "purpose": "向搜索框输入手机",
      "config": {"text": "手机"},
      "element_ids": ["search-input"]
    },
    {
      "id": "collect",
      "component": "WebBrowser_DataCollectionV5",
      "purpose": "采集商品字段",
      "depends_on": ["search"],
      "config": {"columns": ["标题", "价格", "链接", "销量"]}
    }
  ],
  "output_contract": {
    "file": "C:\\Users\\edy\\Desktop\\rpa文件目录\\2026-9-2\\jd.xlsx",
    "columns": ["标题", "价格", "链接", "销量"],
    "min_rows": 100
  }
}
```

其中：

- `id`、`component`、`config` 是组件写入所需的核心字段；`analysis_id`、`name`、`target` 和 `output_contract` 可按调用上下文提供；
- `purpose`、`depends_on`、`inputs`、`outputs`、`element_ids`、`loop`、`condition`、`retry` 等是模型编排和校验所需的扩展字段；
- `operation_ids`、固定 `role_selection` 或固定 `page_type` 不应作为所有计划的必填字段；只有组件契约确实需要时才出现；
- `steps` 可以是线性列表，也可以在后续版本扩展为显式图结构；循环和分支不应被 MCP 当作非法的“非线性流程”。

## 7. 错误返回与修复协议

MCP 错误必须帮助模型修复当前方案，而不是迫使模型重新猜测整个任务。建议统一返回：

```json
{
  "valid": false,
  "error_code": "invalid_component_config",
  "path": "steps[1].config.collect_items",
  "component": "WebBrowser_DataCollectionV5",
  "expected": {"type": "integer", "minimum": 1},
  "actual": "many",
  "message": "collect_items 必须是正整数",
  "suggestions": ["改为 100", "或删除该参数使用组件默认值"],
  "retryable": true
}
```

规则：

1. 只指出具体错误字段，不重写用户原句；
2. 结构错误可以阻止写入；策略差异返回 warning；
3. 模型优先更新现有项目/步骤，不重复创建项目；
4. 同一请求使用指纹实现幂等复用，但用户明确要求重建时才使用 `force_new`；
5. 修复仍失败时，应报告真实契约和原生错误，让模型重新决策，不由 MCP 自动替换为另一种方案。

调试停止必须在有界 RPC 调用内落到 `stopped` 或 `cleanup_timeout`，并返回
`retryable`；停止顺序是 `AppRunService.stop -> DebugAdapterService.disconnect`。
页面身份变化时保留原 `project_ref`，扫描和捕获工具只有显式
`allow_rebind=true` 时才替换已绑定步骤。MCP 客户端应复用一个 stdio 进程完成
整个设计交换，每次工具调用重启进程会丢失分析缓存、DeepCapture token 和调试会话。

## 8. CLI、MCP 与 Skill 的一致性

CLI 和 MCP 应共用同一套 Python 核心、组件目录解析、参数归一化、项目读写、DeepCapture 初始化和校验函数。差异只在传输方式：

- CLI 面向人和脚本，提供命令行参数、JSON 输出和诊断；
- MCP 面向模型，提供 CLI 同源的小而明确的工具、实时组件目录和结构化错误；
- Skill 面向模型上下文，说明何时查询目录、如何读取契约、如何处理 warning 和证据。

同一个组件在 CLI 和 MCP 中必须使用相同的真实 `type`、参数语义和输出绑定。CLI 的便捷参数只能是别名，不能形成另一套隐藏流程规则。

## 8.1 Windows one-file MCP 的退出契约

Windows one-file MCP 在 SDK 客户端正常关闭 stdio 会话时，必须满足“退出码为 0、stderr 为空、原生 UI 已清理”三个条件。旧版可能出现 `ValueError: I/O operation on closed file`；这是 PyInstaller 与 AnyIO 在解释器结束阶段的 stderr 关闭竞态，不是浏览器通道或流程运行错误。

冻结入口使用 `anyio.run`，在导入业务 MCP 模块前安装 stderr 保护，并排除未使用的 `multiprocessing` 运行时钩子；`_run_stdio` 返回后再以 `os._exit(0)` 结束冻结进程。这个退出方式只允许出现在 frozen MCP 的成功路径，不能提前跳过 MCP transport 或 Native UI 的 `finally` 清理。每次发布都要对实际 exe 完成 initialize、`tools/list` 和正常 client close 的 smoke test。

## 9. 以京东采集为例的正确自由度

用户要求“打开京东、搜索手机、采集至少 100 条并写 Excel”时，模型可以自行选择：

1. `Create -> SetValue -> Submit -> DataCollection -> Excel`，由采集组件处理分页；
2. `Create -> SetValue -> Submit -> CollectCurrentPage -> While -> NextPage -> AppendExcel`；
3. `Create -> SetValue -> Submit -> RunJS(table) -> Loop/分页 -> Excel`；
4. 先下载/调用可用数据源，再用文件和 Excel 组件整理。

MCP 的工作是：确认所选组件和参数真实存在，确认页面元素已捕获，确认循环/分支引用完整，确认文件路径和运行环境安全，并在结束后核对 100 条和四个字段。MCP 不应因为模型选择了第 2 或第 3 种方案，就强制改写为第 1 种。

## 10. 后续开发顺序

按本文实施时，建议分以下阶段，每阶段都保持 CLI/MCP 共用核心：

1. 持续从实时目录补充组件契约和参数类型，避免把新设计器参数复制进静态白名单。
2. 在共享 CLI/设计器运行时补充图结构、循环、条件和运行期证据检查；策略差异继续返回 warning。
3. 保持 `update_step`、`add_step`、CLI 和 MCP 共用同一套契约归一化。
4. 持续统一 DeepCapture 自动初始化、元素验证、运行前 preflight、物化和输出证据检查。
5. 为新增组件组合补充多策略测试，并在发布前验证 MCP 握手、安装包和真实运行。

## 11. 验收标准

后续实现满足以下条件，才认为完成本架构迁移：

- 模型可以使用实时目录中的任意可物化组件和其合法参数；
- 模型可以自由选择线性、循环、条件、JS 或组件内置分页方案；
- MCP 不要求所有计划都包含固定的 `operation_ids`、`role_selection`、`page_type`、首节点或单一组件映射；
- 未知组件、非法参数、未验证元素、缺失运行环境和未授权写入仍会被可靠拦截；
- 已有流程修复不会因重复请求创建第二个项目；
- 运行结果能够以状态、日志和输出内容核验；
- CLI、MCP 和 Skill 对组件类型、参数语义和安全边界的描述一致；
- 原有安全测试、DeepCapture 测试、运行诊断测试和回归测试全部通过。
