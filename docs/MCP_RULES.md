# MCP 规则编写规范

本文说明以后如何给 MCP 增加规则，以及怎样让调用方大模型稳定看到并执行规则。
规则描述统一使用中文；工具名、组件 `type`、参数名和状态值必须保留真实英文标识符，避免破坏 CLI、MCP 和设计器契约。

## 先记结论

- 所有任务都适用的安全和通道规则，写入 `shizai_agent/core/mcp_server.py` 的 `MCP_INSTRUCTIONS`。
- RPA 设计、捕获、验证和输出规则，写入 `shizai_agent/core/project.py` 的 `rpa_generation_context`；MCP 的 `shizai_rpa_analyze_request` 会把规则上下文返回给模型。
- 某一个 MCP 工具的调用前提和参数语义，写入同文件 `_schemas()` 中对应工具的 `description` 或字段 `description`。
- 必须阻止的规则，写入共享核心的校验或状态机，并补回归测试；不要只写提示词。
- `skills/shizai-agent/SKILL.md` 是模型工作方法补充；`docs/` 只供开发者阅读，不会自动注入任意 MCP 客户端。

MCP 工具是否出现在当前对话，属于宿主客户端的挂载配置，不属于业务规则。提示词和
`MCP_INSTRUCTIONS` 只能要求模型做工具面自检并在缺失时停止，不能动态注入工具；安装、
注册和重启流程见 [`MCP_CLIENT_SETUP.md`](MCP_CLIENT_SETUP.md)。

## 规则放置表

原生参数编辑的共享规则位于 `core/workflow_edit.py`，参数转换与提交仍复用 `project.py`。
`read_flow(detail=edit)` 返回稳定节点 id、原生参数和 revision；`update_step(dry_run=true)`
只能预览，禁止文件写入和桌面控制。携带 expected_revision 的旧版本提交必须返回
`revision_conflict`，要求重读同一项目。预览参数转换成功不能表述为静态或运行成功。
规则在 full 初始化说明、rpa-authoring 初始化说明及编辑上下文中可见。

`rpa-authoring` 是启动时固定的可选工具集，不是第二套流程引擎。
完整计划先写图再完成静态检查，用户要求执行时用 run/status 和实际输出验收；单步调试用于问题定位。
只有使用 add_step 逐步追加时才执行“验证后继续追加”协议。
两种路径都不能跳过元素证据和运行验证。

| 规则类型 | 修改位置 | 模型何时能看到 | 典型内容 |
| --- | --- | --- | --- |
| 全局硬边界 | `MCP_INSTRUCTIONS` | MCP 初始化时 | 不保存凭据、写入必须 `allow_write=true`、MCP 不内置站点配方 |
| RPA 条件规则 | `rpa_generation_context` 的 `business_rules` | 调用 `shizai_rpa_analyze_request` 后 | 元素捕获、页面变化、分页选择、输出验收 |
| 单工具规则 | `_schemas()` 的工具/字段描述 | `tools/list` 时 | 某工具的必填参数、调用前提、返回证据 |
| 强制执行规则 | `project.py` 的校验/状态机 | 调用工具时以错误或状态返回 | 非法参数、未绑定元素、错误顺序、危险写入 |
| 模型工作顺序 | `SKILL.md` | Skill 被客户端加载时 | 先读目录、边设计边验证、失败修复原流程 |
| 开发说明 | `docs/`、`PROJECT_MEMORY.md` | 默认不可见 | 设计依据、测试要求、变更记录 |

## 推荐规则格式

在 `rpa_generation_context` 的 `business_rules` 数组中，每条规则使用稳定字段，规则内容用中文：

```json
{
  "id": "output-quality",
  "when": "流程产生结构化采集结果时",
  "must": [
    "必须检查实际输出文件的新鲜度",
    "必须检查字段名、有效行数和关键字段非空"
  ],
  "must_not": [
    "禁止把静态校验通过当作业务运行成功"
  ],
  "evidence": [
    "运行终态",
    "当前运行生成的输出文件",
    "输出字段和有效行数"
  ],
  "on_failure": "保留同一个 project_ref，修复当前步骤并重新验证"
}
```

`when` 决定适用范围；`must` 和 `must_not` 写可执行动作；`evidence` 写模型需要读取的证据；
`on_failure` 写失败后的下一动作。避免使用“尽量”“适当”“一般来说”等无法判断的词。

## 本次失败链路的关键边界

下面几条是已经在共享核心中执行、以后修改 MCP 时必须保持的契约：

- 请求分析必须把中文全角标点视为 URL 边界。`https://www.jd.com，搜索电脑` 的 URL 只能是 `https://www.jd.com`；字段解析只能保留字段列表，`等可见商品信息`、`重点验证销量`、`根据实际页面决定分页` 是说明，不是字段。
- 计划步骤缺少 `outputs` 时，可以依据固定原生组件契约补齐，例如 `WebBrowser_CreateV3 -> 网页对象`、`Element_SetValue_V6 -> 校验结果`、`WebBrowser_DataCollectionV5 -> 数据采集结果、Excel路径、监控空列数据`。模型明确写 `outputs: []` 时必须保留原意，不能静默覆盖；后续静态校验再报告真实不一致。
- `page_identity_changed` 后保留原 `project_ref` 和当前 Chrome。先重新 `shizai_rpa_scan_page`，再用同一快照调用 `shizai_rpa_capture_elements`；只有模型明确传 `allow_rebind=true` 才允许绑定已绑定的 step。默认拒绝覆盖旧 selector，避免旧页元素污染结果页。
- 活动 editor 有未保存修改时，完整运行和调试启动必须早退为 `editor_unsaved_changes`，并给出 `shizai_rpa_save_editor` 作为下一步；没有打开 editor 时不阻断。保存本身按 `expected_revision` 执行，保存后仍为脏状态时返回 `save_unconfirmed` 与 `retryable=true`，不得谎报成功。
- 全局键盘组件（`Keyboard_*`）把真实按键发送到当前前台窗口，因此运行前必须核对目标 Chrome 窗口焦点：按流程元素的页面标题定位窗口，必要时还原并切到前台后复核前台句柄。无法确认焦点返回 `window_focus_unverified`，无法从标题确定目标窗口返回 `target_window_unknown`；两者都阻断当前组件，不允许静默放行。
- 网页组件消费的是上游浏览器节点自己的输出变量，计划允许改名。`collection_web_page_binding` 与 `js_web_page_binding` 因此接受前驱节点的实际 `var_name`、计划输出名和规范默认 `网页对象`，失败时列出实际接受的名字。计划声明的输出可以少于组件原生写入的槽位，但多出的尾部槽位必须属于该组件的规范原生输出（例如 `监控空列数据`）；其它多余槽位仍然报错并在详情中点名。
- MCP 客户端必须复用一个 stdio 进程完成 `initialize -> tools/list -> 工具调用 -> close`。每次工具调用重新启动 MCP 会丢失进程内分析缓存、DeepCapture 快照和调试会话，并制造假性的 `channel_unavailable`。
- 调试停止无论官方停止 RPC 是否成功，都必须尽快持久化为 `stopped` 或 `cleanup_timeout`，不能让客户端无限停留在 `stopping`。停止顺序是 `AppRunService.stop` 后 `DebugAdapterService.disconnect`，结果要带 `retryable` 和下一步工具。
- Native 小卡的 `openSmallCardWindow` 返回 `code=5000` 但后续 renderer 更新成功时，表示窗口可复用，不是 MCP 工具失败；返回 `open_nonfatal` 和 `open_result_status=reused_window`，不要把它当成流程阻断。

### 原生组件优先，但不强制

当前共享核心返回的 `native-component-preference` 是 `severity=advisory`、
`enforcement=warning_only` 的软建议：当实时目录中存在已安装、可物化且能力匹配的原生组件时，
模型应先评估它；结构化网页采集应优先评估 `WebBrowser_DataCollectionV5`。这不是要求模型必须采用该组件。

模型选择 JS、下载、循环或其它组合仍然合法，但应说明原生组件不适用、能力不足或页面证据不匹配的原因，
并用所选方案的静态校验、运行证据和输出契约完成验收。CLI/MCP 不得仅因没有选择推荐的原生组件而拒绝计划；
只有组件、参数、引用、元素或运行安全等真实契约问题才进入硬校验。

这条建议同时出现在 `rpa_generation_context["business_rules"]`、`rpa_components["business_rules"]` 和
组件目录的 `usage.implementation_policy`，避免模型只读取其中一个入口时丢失选择语义。

## 什么时候必须改代码

如果规则只是告诉模型如何选择方案，可以放在 `MCP_INSTRUCTIONS`、`business_rules` 或工具描述中。
如果规则必须让错误调用无法落盘、无法运行或无法控制电脑，就必须同步修改共享核心的校验函数，
再由 CLI 和 MCP 复用该校验。规则优先级建议保持为：用户明确要求、MCP 安全边界、业务规则、实时组件契约、默认建议。

不要把某个网站的 selector、固定步骤或“达到某个数字就必须分页”写成全局规则；这类内容应作为当前请求的计划字段，
由模型根据实时页面和组件目录决定。技术标识符可以是英文，规则解释和错误消息应使用中文。

## 变更闭环

每次增加或修改规则，都应同时完成：

1. 修改正确的规则层，并在 `docs/MCP_RULES.md` 记录适用范围。
2. 如果改变共享核心行为，补充 `tests/` 回归测试。
3. 更新 `docs/CLI_STRUCTURE.md`、`docs/MCP_ARCHITECTURE.md` 或 `PROJECT_MEMORY.md` 中受影响的契约。
4. 执行完整 pytest、组件目录 smoke、所有 workflow CLI `--help`、Ruff 和 compileall。
5. 检查 MCP `tools/list`、初始化和未授权写入仍符合契约。

MCP 仍只应包装共享核心。不要在 MCP dispatcher 里复制一套只对 MCP 生效的业务规则。
