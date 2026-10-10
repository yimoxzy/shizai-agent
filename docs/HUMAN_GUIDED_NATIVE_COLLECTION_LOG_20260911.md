# 人工引导的原生网页采集设计记录

日期：2026-09-11

目标：逐步观察并记录人工精确设计网页采集流程的操作，把人的判断转换为通用 MCP 协议。记录必须区分可见界面事实、底层协议推断和待验证结论，不能根据站点硬编码 selector。

## 记录格式

每一步记录以下信息：

1. 人工意图：操作者希望完成什么。
2. 前置状态：操作前有哪些窗口、页面和流程状态。
3. 精确动作：点击、按键、输入或选择的目标。
4. 可见结果：操作后界面真实显示什么。
5. 协议观察：客户端、扩展或 RuntimeHost 可能对应的服务和数据。
6. MCP 设计结论：应当提供什么通用工具、返回字段和状态约束。
7. 下一步验证：下一动作需要证明什么。

## 步骤 1：启动实在 Agent

- 时间：2026-09-11 13:50:55 +08:00
- 人工意图：在没有打开实在 Agent 的状态下，确认模型能否找到本机安装并打开客户端。
- 前置状态：Windows Computer Use 的应用列表没有实在 Agent 窗口；Chrome 的“组件测评商城”窗口已存在。
- 精确动作：从本机安装信息解析主程序 `C:\Program Files\Z-Factory\7.3.6.1\实在设计器.exe`，通过 Windows Computer Use 启动该已安装程序；等待 Electron 主进程创建窗口后重新枚举窗口。
- 可见结果：出现唯一标题为“实在Agent”的主窗口。窗口已激活，显示 TARS 首页、“新任务”入口、任务输入区和左侧 RPA 入口；没有登录弹窗、权限提示或启动遮罩。
- 协议观察：首次 `launch_app` 返回“未立即暴露可操作窗口”，约 2.5 秒后 `list_windows` 能读取主窗口。这表明启动是异步的，不能把首次没有窗口当作启动失败。
- MCP 设计结论：桌面启动工具需要返回 `launch_requested`、`window_waiting`、`window_ready` 三阶段状态；应按进程路径和窗口标题重新发现窗口，禁止依赖固定句柄。只有取得唯一窗口并成功截图后才能报告应用已打开。
- 下一步验证：由人工指定进入 RPA 的准确动作。记录目标入口、点击位置或 UIA 元素、界面切换结果，以及是否出现流程列表或设计器窗口。

### 步骤 1 补充：启动路径如何确定

- Computer Use 首次调用 `list_apps` 时没有返回实在 Agent，因此没有可直接使用的应用 id。
- 代码工作区侧只读检查了 `C:\Users\edy\AppData\Roaming\Z-Factory\run.config.json`，其中 `appInstallPath` 为 `C:\Program Files\Z-Factory\7.3.6.1`；随后确认该目录的主程序为 `实在设计器.exe`。
- 真正的启动动作由 Computer Use 调用 `sky.launch_app({app: "C:\\Program Files\\Z-Factory\\7.3.6.1\\实在设计器.exe"})` 完成。没有打开 Windows Terminal、PowerShell 或运行对话框，也没有在桌面界面中键入启动命令。
- `launch_app` 可以接收应用目录返回的 app id，也可以接收明确的 `.exe` 路径。其内部由 Computer Use 的 Windows 控制层启动程序；MCP 不应把这一行为描述成模型点击了快捷方式。
- 面向 shizai-agent MCP 的正式实现应增加安装解析层：优先读取设计器安装配置或已注册应用信息，得到经过验证的绝对路径，再调用桌面启动通道，并等待唯一主窗口出现。路径解析、进程启动和窗口就绪必须分别记录状态。

## 步骤 2：判断登录状态并进入流程列表

- 时间：2026-09-11 14:05 +08:00
- 人工意图：应用打开后先判断账号是否可用；已登录时进入 RPA 流程列表，未登录时停止自动操作并提示用户登录。
- 前置状态：唯一“实在Agent”主窗口已经激活，主界面可见，没有登录弹窗或认证遮罩。
- 精确动作：在主窗口进入 `RPA`，再进入“我的流程”。
- 可见结果：“我的流程”列表正常显示，并提供“新建流程”入口；本次没有出现登录页，因此无需用户介入认证。
- 协议观察：仅凭主窗口出现不能证明已登录。能够读取“我的流程”列表及“新建流程”入口，才是本次采用的已登录证据。
- MCP 设计结论：启动工具应在 `window_ready` 后继续返回 `auth_state`。建议状态至少包含 `authenticated`、`login_required`、`unknown`；检测到 `login_required` 时返回 `requires_user_action=true` 和提示文字，不点击验证码、扫码、密码或授权控件。检测到 `authenticated` 后才允许调用流程管理工具。
- 下一步验证：从流程列表新建一个空白流程，并同时用界面和落盘数据确认没有业务组件。

## 步骤 3：新建空白 RPA 流程

- 时间：2026-09-11 14:07-14:08 +08:00
- 人工意图：按照人工日常操作，从“我的流程”新建流程，并保持刚创建后的原始空白状态。
- 前置状态：当前账号已登录，“我的流程”列表和右上角“新建流程”入口可用。
- 精确动作：点击“新建流程”，等待原生流程编辑器完成初始化；不搜索、拖入或配置任何组件。
- 可见结果：进入流程编辑器，新流程名称为“我的电脑流程2”。画布只显示系统默认的“开始”、“流程块1”和“点击添加组件”占位，没有添加业务组件。
- 落盘证据：新目录为 `C:\Users\edy\AppData\Roaming\Z-Factory\applications\c_rRdLciRpNi`。`szrpa.json` 中 `packageName` 为“我的电脑流程2”、`componentsNum` 为 `0`；`io5cd.task` 中 `MAIN_COMPONENT.args0[name=statements].value` 是空数组；`project.flow` 只包含系统创建的开始节点和流程块容器。
- 协议观察：空流程不是“完全没有节点”。项目初始化会自动生成开始节点和一个主流程块，是否为空应根据业务组件计数和主流程 `statements` 判断，不能根据节点总数判断。
- MCP 设计结论：建议提供 `create_empty_workflow(name?)`，返回 `project_id`、`project_path`、`resolved_name`、`editor_state`、`business_component_count` 和 `verified_empty`。工具应等待编辑器就绪并读取实际项目元数据；当用户没有指定名称时，返回设计器生成的名称，不能自行声称用了其他名称。
- 下一步验证：保持当前空白画布，等待人工给出第一个组件操作；每次添加后记录组件来源、搜索词、插入位置、配置字段、可见结果和落盘结构变化。

## 步骤 4：添加“打开网页”组件但不运行

- 时间：2026-09-11 14:23-14:30 +08:00
- 人工意图：向“我的电脑流程2”添加一个打开谷歌浏览器的组件，网址为 `www.jd.com`，只完成设计和保存，不运行流程。
- 前置状态：流程块1为空；主流程 `statements` 是空数组；设计器停留在流程编辑页。
- 精确动作：展开左侧“网页/浏览器”原生分类，将“打开网页”拖到“点击添加组件”插入位置；在组件弹窗中保持浏览器类型为“谷歌浏览器”，在“网址”填写 `www.jd.com`，点击“确定”；随后按 `Ctrl+S` 保存。
- 可见结果：画布出现唯一“打开网页”组件，右侧属性显示“谷歌浏览器”和 `www.jd.com`；没有点击组件弹窗左下角“运行”，也没有点击顶部“运行/调试”。保存后设计器显示“保存成功”。
- 落盘证据：`io5cd.task` 的主流程 `statements` 由空数组变为 `["ybQEw"]`；组件 `ybQEw.type` 为 `WebBrowser_CreateV3`，参数 `type` 为 `"chrome"`，参数 `url` 为 `"www.jd.com"`。
- 协议观察：点击组件弹窗“确定”只更新编辑器内存，立即回读 `.task` 仍为空；执行编辑器保存后文件才更新。因此界面出现组件不能作为持久化成功证据。
- MCP 设计结论：现有 `shizai_rpa_add_step` 已能直接添加这个组件，可使用 `component="WebBrowser_CreateV3"` 和 `config={"type":"chrome","url":"www.jd.com"}`。工具必须执行持久化、物化和 `.task` 回读，并返回实际组件 id、类型和最终参数；不应通过鼠标拖拽实现，也不能把设计器内存状态当作成功。
- 下一步验证：后续若由 MCP 添加同类组件，应对同一个 `flow_ref` 调用 `shizai_rpa_add_step`，再用 `shizai_rpa_read_flow` 核对步骤和参数；本步骤按用户要求不做运行验证。

## 步骤 5：使用 MCP 向人工流程追加相同组件

- 时间：2026-09-11 14:35-15:09 +08:00
- 人工意图：不再使用鼠标键盘添加组件，直接通过 MCP 向同一个流程追加第二个与首步相同的“打开网页”组件，并且不运行。
- 前置状态：`io5cd.task` 只有步骤 `ybQEw`，类型为 `WebBrowser_CreateV3`，浏览器为 Chrome，网址为 `www.jd.com`。
- 精确动作：使用官方 `mcp` SDK 启动源码 MCP stdio 服务，在同一个会话依次执行 `initialize`、`tools/list`、`shizai_rpa_read_flow`、`shizai_rpa_add_step`、`shizai_rpa_read_flow`；追加步骤 id 为 `open-jd-copy`，组件为 `WebBrowser_CreateV3`，配置为 `type=chrome`、`url=www.jd.com`、`openNewTab=true`。没有调用运行或调试工具。
- 首次结果：MCP 版本为 `0.7.28`，暴露 56 个工具并能正确回读首步，但首次追加被错误拒绝，提示下一个组件必须是 `Element_SetValue_V6`。原因是人工设计器项目没有 MCP 计划元数据，核心错误回退到旧版浏览器固定序列。
- 修正：没有任何 `mcp*` 元数据的设计器项目现在按 `model_decides` 处理；MCP 首次接管写入时持久化 `mcpPlanningPolicy=model_decides`。旧 MCP 项目仍保留原有兼容序列。另修正 `WebBrowser_CreateV3` 物化时从随机模板继承远程浏览器参数的问题，明确写入本地浏览器默认值。
- MCP 结果：第二次 `shizai_rpa_add_step` 返回 `add_is_error=false`，新步骤为 `open-jd-copy`，`step_validation.valid=true`，`verification_gate.status=runtime_pending`。后者符合“不运行”的要求，不代表添加失败。
- 落盘证据：主流程步骤顺序为 `ybQEw,open-jd-copy`，`componentsNum=2`。两个节点的类型、标签、`type="chrome"`、`url="www.jd.com"`、`isRemote=0`、`ipAddress=""`、`port="61619"`、`arguments=""`、`openNewTab=1`、`waitingload=1` 均一致；生成的 `projects/flow_modules/io5cd.py` 也包含两次对应的 `WebBrowser.CreateV3` 调用。
- 可见结果：MCP 写入时，已经打开的编辑器仍显示旧的单组件内存状态；用户退出并重新进入该流程后，设计器重新读取持久化文件并显示第二个组件。
- MCP 设计结论：`shizai_rpa_add_step` 可以直接编辑人工创建的原生流程，不能对无计划项目制造固定组件顺序。工具回执应区分 `persisted=true` 与 `editor_reload_required=true`；当前编辑器未热刷新时应明确提示重新进入流程，避免用户在旧画布上再次保存并覆盖 MCP 结果。
- 下一步验证：后续增加设计器刷新能力时，优先寻找原生 RPC 或事件通知；在没有可靠接口前，不用鼠标模拟刷新，也不把磁盘写入描述为画布已热更新。

## 步骤 6：MCP 写入后的当前画布刷新探针

- 时间：2026-09-11 16:25-16:30 +08:00
- 人工意图：在客户端保持“我的电脑流程2”打开时，通过 MCP 追加第三个组件，再测试原生 RPC 能否让当前画布直接显示新内容。
- MCP 写入：源码 MCP `0.7.28` 在同一个 stdio 会话先后执行 `shizai_rpa_read_flow`、`shizai_rpa_add_step`、`shizai_rpa_read_flow`。组件数从 2 变为 3，新步骤 id 为 `reload-probe-open-jd-3`，类型为 `WebBrowser_CreateV3`，URL 为 `www.jd.com`；四个 PackageContent 保存 RPC、文件一致性检查和 MCP 回读均成功。流程 revision 从 `391226dd25f646656bd5a883bef0eb7e7f19cdd46d6c7de70a679079db1e4e23` 变为 `6a93c7c965dd784e80c6dc17b41967329f73bbcfdae9df7bbf199f58a8839d60`。
- 失败路径：把 `ApplicationOpService.editApplicationInAgent` 当作普通 Node RPC 调用时，`node-main.log` 返回“服务不存在”；该服务属于 FusionHome renderer。改为向 `$HomeClientId$` 发送 `render-call-render-req` 后 RPC 成功，但同 package 会被 `WorkbenchService.editPackage` 的 `currentOpenPkgId` 检查跳过，只重新激活应用，没有强制重新初始化模型。
- 成功路径：向 `$CurrentActiveEditorClientId$` 调用 `WorkbenchService.reload`。RPC 返回成功，编辑器日志随后出现 `_editPackageInternal start`、`初始化模型`、`创建TaskEditorState` 和 `通知页面初始化完成`，证明当前 editor webview 已重新加载流程，而不是仅刷新导航或重新激活标签。
- 数据安全证据：刷新前后 revision 都为 `6a93c7c965dd784e80c6dc17b41967329f73bbcfdae9df7bbf199f58a8839d60`，刷新没有再次改写流程文件；刷新后磁盘回读仍是三个顶层组件。
- MCP 设计结论：下一版应封装当前 editor renderer 的 `WorkbenchService.reload`，不能调用 `refreshAgentNavigation`，也不能只调用 `editApplicationInAgent`。正式工具应在写入回读成功后携带 revision 调用，返回 `persisted`、`reload_requested`、`reload_acknowledged` 和 `revision_unchanged`。由于 `reload()` 会重建 editor webview，正式自动刷新前仍需解决或明确约束尚未落盘的人工编辑。

## 步骤 7：保存人工删除并通过 MCP 单步运行首组件

- 时间：2026-09-11 17:33-17:37 +08:00
- 人工意图：用户在当前画布删除后两个组件，只保留第一个“打开网页”，要求通过 MCP 只运行这个组件。
- 保存前状态：磁盘 `io5cd.task` 仍有 `ybQEw,open-jd-copy,reload-probe-open-jd-3` 三个组件；`WorkbenchService.isNeedSave` 返回 `needSave=true`，证明删除只在编辑器内存中。
- 保存动作：向 `$CurrentActiveEditorClientId$` 调用 `PackageSaveService.saveAll({alertOnError:true})`。RPC 返回 `true`；再次调用 `WorkbenchService.isNeedSave` 返回空对象，磁盘 revision 从 `6a93c7c965dd784e80c6dc17b41967329f73bbcfdae9df7bbf199f58a8839d60` 变为 `2e954556156f77f6b51c0ee04fd2bef7d4c2e57e56d3ced39c139c5c2758b441`，回读只剩 `ybQEw`。
- MCP 运行：源码 MCP `0.7.28` 在一个 stdio 会话先执行 `read_flow(detail=edit)`，硬性确认组件数等于 1；随后执行 `debug_capabilities`、`debug_start(start_from=1)`、`debug_status`、一次 `debug_command(action=next)` 和 `debug_status`。首组件为 `ybQEw / WebBrowser_CreateV3`。
- 运行证据：`debug_start` 停在生成代码第 30 行的 `ybQEw` 精确组件边界；一次 `next` 后停在第 31 行，返回 `component_completed=true`、`completed_component_id=ybQEw`，且 `runtime_verification.verified=true`、`component_executed=true`。流程只有一个组件，因此没有执行后续业务节点。
- 清理结果：会话内首次 `debug_stop` 因桌面业务通道超时返回 `cleanup_timeout`；按返回的 `next_tool=shizai_rpa_debug_stop` 重试后，最终为 `status=stopped`、`cleanup_complete=true`、`cleanup_status=complete`，没有遗留调试会话，Chrome 会话保留为可复用状态。
- MCP 设计结论：MCP 在运行前必须检测客户端未保存状态，并阻止按旧磁盘定义执行；保存能力应封装 `WorkbenchService.isNeedSave` 与 `PackageSaveService.saveAll`，保存后必须用 revision 和组件顺序回读确认。`debug_stop` 的 `cleanup_timeout` 应自动重试，并以持久化会话的最终 `cleanup_complete` 为准。

## 步骤 8：增加计算组件并验证真正的组件级单步

- 时间：2026-09-11 19:25 +08:00
- 人工意图：在“打开网页”后增加一个简单的“计算 1 + 1”组件，用两个业务组件判断 MCP 的 `next` 是否只执行当前组件，并判断最后一步是否能及时识别流程结束。
- 流程定义：主流程顺序为 `ybQEw,calc-one-plus-one`。第二步类型为 `Basic_SetVariable_V1`，生成代码为 `计算结果 = Basic.SetVariable_V1(..., value=1 + 1, ...)`。原生 Basic 插件的 `SetVariable_V1` 实现直接返回传入的 `value`，所以该赋值结果为整数 `2`。
- 原问题：设计器原生 `DebugControllerService.next` 会按自己的源码映射继续跨行；MCP 与设计器同时调用共享 `DebugAdapterService.send(threads/stackTrace)` 时，还可能有一个查询挂起 30 秒。其结果是单次 `next` 看起来像整流程运行，而且设计器已经终止后 MCP 仍然等待。
- 协议修正：MCP 在当前组件前暂停后，仅用 DAP 设置“下一个业务组件”的行断点并发送一次 `continue`。等待阶段不再读取共享 DAP 的 `threads/stackTrace`，而是读取设计器已经维护的 `DebugControllerService.getDebugState` 和 `DebugStackFrameService.getCurrentStackFrame`。最后一个组件没有后续业务断点，直接等待 `terminaled` 且 `getIsDebugging=false`。
- 第一次单步证据：启动停在第 48 行、组件 `ybQEw` 前。第一次 `next` 真实打开 `https://www.jd.com/`，随后设计器记录 `stopped` 和 `nextStackFrame: 56`；MCP 回执为 `completed_component_id=ybQEw`，当前组件为 `calc-one-plus-one`、`component_sequence=2`、`component_boundary=true`。这证明计算组件尚未执行，第一次操作只执行了第一个组件。
- 第二次单步证据：第二次 `next` 执行 `calc-one-plus-one`，设计器收到运行 `StateChange: success` 后进入 `terminaled`。MCP 在约 `1.515` 秒内返回 `status=succeeded`、`completed_component_id=calc-one-plus-one`、`evidence_kind=designer_terminal_state`，没有等待到 30 秒超时。
- 结束与可编辑证据：第二次单步回执已经设置 `dap_terminal_event=terminated`、`run_owned=false`；随后直接读取设计器得到 `debug_state=terminaled`、`is_debugging=false`。最终清理为 `status=stopped`、`cleanup_complete=true`，客户端不再停留在“流程调试中”。
- 缓存边界：`.sz/variable_cache/io5cd.json` 记录了第一步返回的京东浏览器对象。`Basic_SetVariable_V1` 创建的是流程局部变量，运行结束后不会写入这份断点恢复缓存，因此不能把“缓存中没有计算结果”误判为第二步未执行；第二步的执行证据是精确停靠后的继续、原生插件返回语义、设计器成功终止事件和 MCP 的组件完成回执。
- 回归验证：`tests/test_debug_session.py` 共 33 项通过，覆盖下一个组件断点、禁止 DAP 状态争抢、最后组件快速终止和 `run_owned` 释放；Ruff 检查通过。

## 步骤 9：复用已验证元素并运行单个输入组件

- 时间：2026-09-11 20:24-20:25 +08:00
- 人工意图：浏览器已经由第一步打开并保留，继续在当前流程末尾添加“网页输入框填写”组件，定位京东搜索框并输入“手机”；只运行这个新组件，不重新执行“打开网页”和“计算 1 + 1”，也不按回车触发搜索。
- 流程定义：主流程顺序为 `ybQEw,calc-one-plus-one,input-jd-phone`。第三步类型为 `Element_SetValue_V6`，`designated_obj=网页对象`、`text="手机"`、`isEnter=0`、`operation=0`，输出变量为“校验结果”。元素绑定为 `SHIZAI_ELEMENT_DICT["uia:35998227"]["selector"]`。
- 元素来源：当前扩展没有重新拾取元素；在用户允许参考既有流程的前提下，通过 `shizai_project_import_elements(source_project_ref, element_ids)` 从参考流程复制扩展真实 DeepCapture 且标记为 `verified` 的搜索框记录，并保留 `mcpReferenceSource`。该机制不包含京东 URL 或 selector 硬编码，也拒绝模型候选和未验证元素。导入后仍标记 `mcpReferenceRuntimeStatus=pending`，必须在当前页面运行后才能形成新的运行证据。
- 静态校验：第三步回读确认 `element_binding=true`、`imported_elements=true`、`verified_elements=true`、`output_variables=["校验结果"]` 和 `valid=true`；写入后的流程 revision 为 `c744edafaf4366b89993e6beb462996ffda9bbfd0082d967fbdca2fdc345197d`。
- 刷新竞态：第一次在 `WorkbenchService.reload` 返回后立即调用 `RunService.runComponentInEditor` 失败。刷新会销毁旧 editor 并异步创建新 editor；该请求没有进入设计器的 `runComponentInEditor`，引擎也未启动，因此不能把 RPC 失败解释为组件运行失败。
- 成功路径：再次刷新后等待 4 秒，并先调用 `WorkbenchService.isNeedSave` 确认当前 editor 已重新注册；随后向 `$CurrentActiveEditorClientId$` 调用 `RunService.runComponentInEditor`，参数仅包含 `componentIds=["input-jd-phone"]`，同时设置 `useCache=true` 和 `createCodeParams.useCache=true`。RPC 在约 `3.922` 秒后返回 `state=success`。
- 浏览器复用：运行读取 `.sz/variable_cache/io5cd.json` 中第一步留下的 `网页对象`，缓存 URL 为 `https://www.jd.com/`。因此单组件调试可以继续操作已经打开的浏览器，不需要重新执行打开网页组件。
- 单组件证据：设计器生成的 `projects/_temp/component_run.py` 包含 `Element.SetValue_V6(... text="手机", isEnter=0 ...)`；文件为恢复依赖变量而包含缓存的 `网页对象` 和计算结果赋值，但不包含 `WebBrowser.CreateV3` 或 `Basic.SetVariable_V1` 的组件调用。这证明本次只执行第三个业务组件。
- 终态证据：引擎日志在 `20:25:09` 加载 `projects._temp.component_run`，约 `1987ms` 后记录 `CoreServer Code: 0`，`20:25:12` 正常结束；设计器记录 `RunService.runComponentInEditor exit`。运行遮罩已移除，拾取状态为 inactive，旧调试会话为 stopped。
- 当前限制：现有 `shizai_rpa_validate_step` 的 `verification_gate` 仍为 `runtime_pending`，因为 MCP 尚未把设计器的组件级运行结果回写成结构化验证证据。此次实际运行成功与 gate 状态需要分别陈述，不能伪造 gate 已更新。
- MCP 设计结论：下一版应封装 `shizai_rpa_run_component`。工具需要核对目标项目和 editor、检查未保存状态、按 revision 刷新、等待 editor ready、用 `runComponentInEditor + useCache=true` 运行指定组件，并根据原生终态、生成代码和日志更新运行证据。固定等待只能作为临时探针，正式实现应采用 editor 就绪检测和明确超时状态。

## 步骤 10：选择器验证链路与扫描性能定位

- 时间：2026-09-11 21:05-21:35 +08:00
- 人工意图：解释原生拾取生成的 selector 如何验证，并定位“找一个搜索框仍然很慢”的具体耗时，而不是把整个等待归因于模型的视觉能力。
- 当前元素：流程 `c_rRdLciRpNi` 的 `input-jd-phone / Element_SetValue_V6` 绑定元素 `uia:35998227`，CSS 为 `input.jd_pc_search_bar_react_search_input`。
- 静态验证：`project._element_diagnostic()` 检查元素 id、页面身份、稳定属性/CSS、是否只依赖 `nth-child` 等脆弱结构。`mcpCaptureSource=deep_capture` 会使 `mcpVerificationStatus=verified`，这里的 `verified` 表示 selector 来自可信原生捕获记录，不表示它在当前页面已定位成功。
- 运行验证：真正证明 selector 可用的是运行 `Element_SetValue_V6`。原生 Chrome 通道定位元素、聚焦、输入，并在 `verifying_input=1` 时读回值。本次组件执行约 `2.43s`，成功输入“手机”；这是当前页面的运行证据。
- 初始瓶颈一：失效 DeepCapture 会话在 `getDeepCaptureElementChildrenById` 固定等待 `DEFAULT_CAPTURE_RPC_TIMEOUT=6s` 才返回 `channel_unavailable`。
- 初始瓶颈二：定向查找失败后会回退完整递归树。Profiler 记录完整扫描约 `15.410s`、DeepCapture RPC `623` 次，其中子节点读取 `585` 次。`ShizaiDesktopAgent.call()` 每次调用都会新建并关闭一个 WebSocket，逐节点扫描放大了连接成本。
- 初始瓶颈三：回退后的遥测仍错误报告 `scan_strategy=targeted`、`targeted_fallback=false`，使模型和测评误以为快速路径成功。
- 初始瓶颈四：真实 MCP 调度在只读扫描前仍调用 `_rpa_design_ui_ensure_takeover`。首次进程会播放 5 秒倒计时；一次 profiler 中总耗时约 `16.25s`，接管 UI 约 `7.59s`，其中明确睡眠约 `5.20s`。
- 第一轮修正：MCP 对搜索框/提交按钮启用 `strategy=auto`；定向搜索最小深度提高到 9；已知 `root_element_id` 时直接复用目标窗口；按实际命中修正 `scan_strategy` 和 `targeted_fallback`；selector 捕获新增 `selector_rpc_calls` 与 `selector_capture_elapsed_ms` 遥测。
- 第一轮结果：底层定向扫描约 `1.477s`、36 次 RPC，读取完整 selector 约 `367ms`、2 次 RPC，总计约 `1.85s`；原始底层扫描约 `15.41s`。
- 第二轮修正：窗口枚举、页面扫描、批量字段识别和 selector 捕获改为 `capture_read`。这些操作仍显示小卡进度，但不启动接管倒计时、接管边框或红色拾取层；真正的官方交互式拾取、打开浏览器和运行组件仍保留接管提示。返回中显式记录 `takeover_required=false`、`countdown_started=false`、`picker_overlay_started=false`。
- 第二轮实测：当前京东页面通过完整 MCP 调度扫描“搜索框”约 `7.445s`，找到 5 个候选，`scan_strategy=targeted`、`targeted_fallback=false`、`targeted_rpc_calls=36`、`targeted_elapsed_ms=5576.915`，且 `countdown_started=false`。与此前约 `14.56s` 的完整调用相比，已去掉重复接管等待。
- 深度参数探测：对同一窗口分别请求 `level=1/4/9/12`，当前扩展均只返回 2 个直接子节点，没有批量展开深层 Document。因此继续调大 `level` 不能消除 BFS；当前剩余时间主要来自 36 次逐节点 RPC 与每次 WebSocket 重连。
- 被否定的优化：曾尝试优先深入 Pane/Group/Window 等容器，但真实京东 Chrome 树包含大量容器分支，RPC 从 36 增至 46，完整耗时约 `7.64s`，因此已撤回。该结果说明不能凭控件类型猜测 Document 路径。
- 第三轮修正：当调用方已经传入上一轮 `capture_windows` 返回的 `root_element_id` 时，`scan_page` 不再重复枚举窗口。跨进程旧会话的存活探测从最多 6 秒缩短为最多 1 秒；显式 `extension status` 增加真实根节点 RPC 探测，并区分“桌面已登录”与“DeepCapture 方法可调用”，避免只凭 Native Messaging Host 进程报告假 ready。
- 第三轮实测：显式 root 的完整 MCP 调度为 `5.216s`，定向扫描 `4.673s`、36 次 RPC、5 个候选，`scan_strategy=targeted`、`targeted_fallback=false`，仍明确返回 `countdown_started=false` 和 `picker_overlay_started=false`。相对原完整调用约 `14.56s`，本轮稳定路径耗时下降约 64%。
- 连接复用探针：单元测试证明桌面客户端可在一个 socket 上发送连续 callId，但真实复测期间 DeepCapture 在进入扫描前的旧会话探测阶段发生过两次超时，无法隔离证明连续复用在当前桌面版本上稳定，因此没有保留该代码。
- MCP 设计结论：静态可信来源、当前页运行验证和选择器性能必须分开报告。现有 MCP 侧已经去掉可避免的倒计时、完整回退和重复窗口枚举；剩余 36 次逐节点 RPC 需要桌面或扩展端提供批量子树 RPC，或正式支持一个读取事务复用连接。不能按京东 URL、类名、控件类型或固定层级硬编码路径。

## 步骤 11：视觉坐标到原生元素的点命中探针

- 时间：2026-09-11 21:40-22:10 +08:00
- 人工意图：验证“模型看当前截图并给出目标坐标，扩展按坐标返回页面元素，再生成并验证 selector”是否能绕开 DeepCapture 的逐节点页面树扫描。
- 本地商城探针：在尺寸约为 `620 x 484` 的“组件测评商城”当前截图中，模型把搜索框定位到 `(250,129)`；页面端调用 `document.elementFromPoint(250,129)` 立即命中 `<input name="q">`。初始 selector `input` 匹配 3 个元素，结合父级稳定属性收敛为 `form[action="/shop"] input[name="q"]`。
- 无副作用验证：最终 selector 的 `matchCount=1`、`visibleMatches=1`、`pointIsMatch=true`。截图读取、点命中和 selector 验证三次浏览器读取各自均不足 `0.5s`，没有点击、输入、滚动或改变页面状态。
- 真实京东对照：DeepCapture 在真实 Chrome 窗口中定向查找“搜索框”仍访问 209 个节点并发起 36 次 RPC，耗时约 `4.633s`；正确候选为 `uia:49273599`，`ControlType=Edit`，屏幕范围 `X=627,Y=406,Width=151,Height=55`，可点击点为 `(702,433)`。元素 id 已知后，读取完整 selector 只需 2 次 RPC、约 `0.984s`。这说明主要成本是从页面树中找到 element id，不是生成 selector。
- 接口核对：当前 `DeepCaptureService` 公开能力只有启动/停止捕获、按 id 读取子节点/详情/selector，以及启动/停止 Inspect，没有“传入窗口与 x/y 返回元素”的 RPC。子节点响应也不含 `BoundingRectangle`，MCP 无法只靠坐标剪枝，若逐个读取详情仍会退化为大量 RPC。
- 前台约束：Windows UI Automation 的 `AutomationElement.FromPoint` 只命中当前屏幕最上层元素。探针中目标 Chrome 被其他窗口覆盖时，同一坐标命中了覆盖窗口，因此必须按“激活目标窗口 -> 获取新截图 -> 模型选点 -> 立即命中”的顺序执行，并校验命中元素属于目标窗口进程。旧截图和后台窗口坐标不能作为有效证据。
- 原生验证能力：设计器已有 `Element.HighlightScriptElement`，可接收 `webScript` 和 `queryMode`，返回 `ElementCount`、`HiddenElementCount`、`Message`。它适合封装成 selector 的只读验证接口；设计器上下文缺失时会返回 `40009995`，必须与 selector 无效区分。
- 实测结论：浏览器 DOM 的 `elementFromPoint` 路线准确、亚秒级，并且能生成唯一 selector；当前实在扩展没有 point RPC，所以 MCP 暂时不能在真实浏览器上直接复现完整链路。现阶段继续遍历整棵 DeepCapture 树并让模型从候选中猜元素不合理，性能上限受逐节点 RPC 数量控制。
- MCP 设计结论：下一版增加 `shizai_rpa_capture_at_point(project_ref, window_id, x, y, screenshot_id)`。工具先激活并核对目标窗口和截图新鲜度，再优先调用扩展端 point RPC；扩展未提供该能力时，启动官方 Inspect，由具备 Computer Use 的客户端完成一次精确点击，随后读取新增 element id，并复用现有 selector RPC。另增加 `shizai_rpa_verify_selector` 封装 `Element.HighlightScriptElement`，失败结果明确区分 `designer_context_missing`、`window_not_foreground` 和 `extension_unavailable`。
- 下一步验证：先实现不含站点硬编码的 point-capture 适配层和 selector 验证工具；在本地商城、京东搜索框、重复商品卡片、弹窗内输入框和 iframe 五类场景测量命中率、总 RPC 数和端到端耗时，以 `pointIsMatch=true`、唯一可见匹配和不产生页面副作用作为通过条件。

## 步骤 12：重新单步定位京东搜索未触发的原因

- 时间：2026-09-12 10:55-11:15 +08:00
- 人工意图：从第一步重新单步运行 `打开京东 -> 计算 1+1 -> 输入手机 -> Enter`，确认是哪一步失败以及为何没有进入搜索结果页。
- 流程定义：主流程仍为 `ybQEw,calc-one-plus-one,input-jd-phone,press-enter-search`，revision 为 `504da4448e8fed37d0e21c243284c889f5bc4c7140f03648f8ca9e1e56f8cd51`。输入组件使用已验证搜索框元素，`isEnter=0`；Enter 是独立的全局 `Keyboard_PressNew` 组件。
- 首步证据：清理旧调试状态后，`debug_start(start_from=1)` 返回 `session_reused=false` 并停在 `ybQEw`。第一次 `next` 返回 `completed_component_id=ybQEw`，准确停在 `calc-one-plus-one`；Chrome 进程为 `32820`、窗口句柄为 `22545962`，标题是京东首页，且窗口报告 `HasKeyboardFocus=True`。
- 假超时：第二次 `next` 在 30 秒内没有回包，`debug_status` 暂时记录 `status=running` 和 `Native UI call timed out: render-call-render-res`。随后执行 `debug_stop` 时，设计器返回的真实当前边界已经是 `input-jd-phone`。这证明 `calc-one-plus-one` 已执行并跨到第三步，失败的是调试控制 RPC 的回包/状态收敛，不是 `1+1` 组件。
- 从中间恢复失败：清理后尝试 `debug_start(start_from=3)`，设计器仍先停在第一组件，MCP 设置第三、四组件断点后等待超时，最终返回“设计器接管调试会话未停在组件位置”。这说明当前 `start_from` 在复用设计器 DAP 适配器时不可靠，不能作为恢复任意组件的稳定方式。
- 组件对照：用设计器原生 `RunService.runComponentInEditor` 先单独运行 `input-jd-phone`，约 `4.625s` 返回 `state=success`；再单独运行 `press-enter-search`，约 `2.015s` 返回 `state=success`。之后 Chrome 标题变为“手机 - 商品搜索 - 京东 - Google Chrome”，DeepCapture 重新连接后也读取到同一结果页窗口。这证明第三、四步的组件参数和元素绑定本身均可用。
- 根因：独立 Enter 是全局键盘事件，不绑定网页元素。DAP 的暂停、设计器调试控制和 MCP 状态查询可能切换前台焦点；若 Enter 执行时 Chrome 不是前台窗口，组件仍可能报告成功，但按键实际发送给设计器，因此页面不跳转。与此同时，`render-call-render-res` 超时会把已经跨过组件边界的执行暂时标记为 `running`，进一步误导模型重复执行或错误恢复。
- MCP 设计结论：`debug_command(next)` 超时时必须先读取设计器当前栈帧和调试终态；若已到下一个组件边界，应返回“组件已执行、控制回包超时”的可恢复结果，不能保留假 `running`。执行 `Keyboard_PressNew` 前应根据变量缓存中的网页对象激活对应 Chrome 窗口并核对句柄；无法保证焦点时，返回 `window_focus_unverified`。对于连续的元素输入和全局 Enter，可提供不中断的组件组运行，但仍保留两个原生组件的流程结构。

## 步骤 13：结果页商品字段的系统点命中对照

- 时间：2026-09-12 10:40-10:50 +08:00
- 人工意图：在手机搜索结果页对商品名称、价格、热度/评论指标和店铺进行视觉选点，验证 Windows 原生点命中是否能直接取得网页 DOM 元素。
- 实测结果：四个坐标的 `AutomationElement.FromPoint` 分别约为 `97.783ms`、`4.389ms`、`2.827ms` 和 `1.703ms`，均命中正确 Chrome 进程 `32820`，但返回的控件都只是 `ControlType.Document / AutomationId=RootWebArea`，没有商品字段级元素。
- 数据语义：当前京东卡片可见的是评论数或“人看过”等热度指标，页面没有可直接证明为“销量”的字段；采集定义和最终输出不得把这些值改名伪装成销量。
- MCP 设计结论：Windows UIA 点命中很快，但 Chrome 当前只暴露根网页区域。要获得商品字段及 selector，必须由扩展在页面上下文执行 `document.elementFromPoint(x,y)`，或使用官方 Inspect 完成一次点选。缺少扩展 point RPC 时，DeepCapture 树扫描仍是过渡路径。

## 步骤 14：使用原生 DataCollectionV5 完成手机商品采集

- 时间：2026-09-12 19:55-20:15 +08:00
- 人工意图：继续在“我的电脑流程2”中使用原生数据采集组件，采集当前京东手机结果页的商品名称、价格和页面真实展示的热度原文；页面没有统一可证明的销量字段，因此不把评价、浏览或加购量改名为销量。
- 最终流程：`ybQEw -> calc-one-plus-one -> input-jd-phone -> press-enter-search -> wait-jd-results -> refresh-jd-result-tab -> prepare-jd-product-fields -> collect-jd-phones`。新增的 `prepare-jd-product-fields` 使用允许的 `jd_product_card_link_v1` 参考工作流，在当前任务配置中生成稳定字段 marker；`collect-jd-phones` 仍是原生 `WebBrowser_DataCollectionV5`。
- MCP 回读缺口：`read_flow(detail=edit)` 的节点目录只返回 id/type/label；此前即使指定 `step_id`，也没有返回节点已绑定的 `mcpElementIds`。已补充 `selected_step.element_ids`，使模型可以通过 MCP 获取作用域元素，不必绕过工具读取 `.task`。
- 假阳性门禁：旧预览只以 `data_row_count > 0` 判断成功。短链退化后曾返回 993-1163 行，但每行三列均为同一个值，例如 `上传图片搜索`。现在多列样例若每行所有列完全相同，返回 `valid=false`、`failure_kind=column_collapse`，禁止运行正式采集。
- selector 根因：生成 marker 的 class 属性原为 `accurate=false`。RuntimeHost 会忽略该 class 条件并只按末端 `span` 读取全页文本；改为 `accurate=true` 后，同一原生预览稳定返回 29 行正确的名称、价格和热度。对照探针同时证明 raw class/raw data-attribute 可用，带 JSON 引号的属性值和 `cssSelector` 属性在当前 DataCollectionV5 预览中返回 0 行。
- 预览完整性：`preview_rpa_collection` 现在完整透传原生 `collectionConfig`，保留 `commonSelector`、滚动延迟和 `scrollType` 等字段，并为旧任务补默认值。预览一次通过，4 条样例均满足商品名、数字价格、明确热度指标和三列互异。
- 运行参数根因：更新组件时，`designated_obj=网页对象` 若与旧值相同，会被通用回退编码二次覆盖成字符串字面量 `"网页对象"`，正式运行立即报 `Expecting value: line 1 column 1`。已将该参数排除出通用回退，生成代码恢复为 `designated_obj=网页对象` 变量引用。
- 最终运行证据：导航组合 `state=success`、约 `9.844s`；预处理 `state=success`、约 `2.750s`；原生预览 `valid=true`、29 行；正式 DataCollectionV5 `state=success`、约 `12.172s`。`.sz/variable_cache/io5cd.json` 中 `collect-jd-phones` 保存了包含表头的二维结果，共 29 条数据。
- 数据质量：29 条商品名称均非空且不重复，29 条热度原文均非空，28 条价格非空，1 条价格为空；每条记录三列均未发生列坍缩。热度原文保留 `60万+人看过`、`10万+条评价 500+人复购`、`已售70万+` 等页面原文。
- 结束状态：正式采集后执行调试和捕获清理，`debug_stop.status=stopped`，拾取层为 inactive，客户端未停留在流程运行/调整状态。

## 步骤 15：独立浏览器扩展恢复连接并直连原生采集组件

- 时间：2026-09-15 11:45-12:35 +08:00
- 人工意图：在“我的电脑流程2”已有京东手机结果页上继续流程设计；新扩展负责快速取得截图和字段定位证据，MCP 把证据转换成实在 Agent 原生元素并绑定 `WebBrowser_DataCollectionV5`，全过程记录真实耗时和失败原因。
- 连接故障：扩展控制台出现 `WebSocket connection to ws://127.0.0.1:18768/ failed: ERR_CONNECTION_REFUSED`。现场 `Get-NetTCPConnection` 证明报错当时没有桥接监听；启动测评桥接后端口同时出现 `LISTEN` 和 `Established`。用户重新加载扩展后，真实 `status` 返回 `state=ready`、`connected=true`、`extensionVersion=0.1.8`，说明扩展安装、ID、Native Messaging 和协议均正常，问题在桥接生命周期。
- 连接修复：MCP stdio 已在启动阶段调用 `BrowserBridgeManager.ensure_started()`。Native Messaging Host 新增 loopback 端口预探测；端口未监听时返回 `bridgeUnavailable`，扩展按退避策略继续唤醒，不再创建必然失败的 WebSocket。扩展自身保留 `runtime.onStartup`、`runtime.onInstalled`、30 秒 alarm、20 秒 keepalive、单飞 bootstrap 和 `250ms-30s` 退避重连。该 Host 修复需要重新构建安装包后才进入正式安装版本。
- 标签证据：扩展 `list_tabs` 约 `3.6-4.1ms` 返回 5 个京东“手机”结果页标签，并正确标记当前活动标签。该操作不枚举页面 DOM。
- 截图证据：`capture_viewport` 约 `171.8ms` 返回真实 `1920x878` PNG、CSS viewport、滚动位置和 `pageRevision=63`。扩展拒绝内部页面和密码输入框，不把截图成功等同于元素已验证。
- 字段推断：在同一 screenshot 和 revision 上调用 `infer_repeated_collection`，约 `17.4ms` 得到 `commonSelector=div._info_1y1ag_17`、`rowCount=29`，并返回商品名称、价格、热度原文、商家四列 selector 和 5 条样例。扩展只返回局部结构化证据，没有把完整 DOM 交给模型；页面 class 没有写入扩展源码。
- 数据语义：京东当前字段混合展示“评价、人看过、加购、已售”等指标，因此仍命名为“热度原文”。扩展和 MCP 不得把这列统一宣称为销量。
- 第一次原生绑定失败：转换器把卡片 selector 与字段 selector 拼成 descendant chain，例如 `div._info_1y1ag_17 span._price_9y3st_31 > span`。扩展样例正确、静态绑定通过，但原生 `DataCollectionV5` 只返回表头。此结果证明“浏览器 selector 可用”与“原生采集 selector 编码兼容”是两个验证门禁。
- 原生编码对照：`DataCollectService.dataCollectCapturePreview` 证明单节点 raw class、单节点 raw attribute、卡片 + raw attribute 可返回数据；`cssSelector` 属性、带 JSON 引号的 class/attribute、多节点字段链返回 0 行。原生运行时要求属性值保持 raw 字符串。
- 转换修复：`collection_elements()` 现在优先保留扩展已经验证的 `sourceSelector`，去除位置型 `nth-*`，只把字段自身转换成单节点 raw class。`commonSelector` 继续保存在 `mcpBrowserEvidence` 与 `card_context`，用于复核重复结构和页面变化后的重采集，不再进入原生字段 selector 链。
- 最终原生直连证据：独立对照探针把 `span._newStyle_1k2fi_39`、`span._price_9y3st_31`、`span._goods_volume_1d8up_1` 直接交给原生 `DataCollectionV5`，没有运行 JS marker 注入。原生预览返回 `59` 行；样例商品名、数字价格和“2万+条评价 99%好评”“4万+人看过”等热度文本按列对齐，没有列坍缩。
- 旧方案对照：历史探针会在执行前把第 8 步改回三个 JS marker，其正式运行返回 29 行。该结果只证明旧兼容路径可用，不计入新扩展直连原生组件的成绩。
- 当前流程回读：revision 为 `2b3839e8dd13c38dcde090dbd4ddd5dd6fa303011e295910166a56ec04d61ffb`，共 9 个顶层组件。第 7 步是旧 JS 预处理，第 8、9 步是两个 `WebBrowser_DataCollectionV5`；这是实验对照结构，存在重复最终采集节点，不能作为最终简洁流程发布。
- 已安装版本差异：当前已安装的旧 MCP 二进制把项目根目录错误解析成 `.sz/draft-restore-backup/...`，进而误报 `elements.json` 缺失和步骤错位。工作区源码能正确绑定并生成单节点元素；正式发版前必须重新构建并安装当前源码，再在全新 Codex 会话复测。
- 回归验证：浏览器桥接、MCP 绑定、流程编辑和 DAP 共 `75 passed`；Python 编译检查及 `git diff --check` 通过。过程截图、JSONL 遥测、原生预览和完整结论见 `docs/MCP_BROWSER_EXTENSION_LIVE_EVALUATION_20260915.md`。
- 下一步：先保存并回读客户端当前人工编辑，清理旧 `prepare-jd-product-fields` 和重复的采集组件，只保留一个绑定新扩展字段证据的 `WebBrowser_DataCollectionV5`；随后从打开网页开始按组件边界运行，确认最终变量缓存、输出文件、调试终态和客户端可编辑状态。

## 步骤 16：插件恢复后校正活动任务并完成四列单组件运行

- 时间：2026-09-15 16:55-17:20 +08:00
- 人工意图：插件恢复后继续“我的电脑流程2”，把扩展推断出的商品名称、价格、热度原文、商家四个字段真正写入客户端当前活动流程，并用原生采集组件单步运行验证。
- 截图状态：新活动标签连续两次调用 `capture_viewport` 都在 30 秒超时，但同一标签的 DOM 滚动动作约 `12ms` 成功。内容脚本和 WebSocket 正常，阻塞点是 `chrome.tabs.captureVisibleTab`。后续接口需要先激活目标 tab 和窗口，并返回窗口前台状态与截图内部超时，避免把截图阻塞误报成扩展离线。
- 复用证据：使用此前同一京东结果页的 `tabId=1126612872`、`screenshotId=shot-1789446276814-d93mew`、`pageRevision=63` 重新调用 `infer_repeated_collection`。扩展在约 `1.712s` 完成推断和原生写回，返回 `commonSelector=div._info_1y1ag_17`、`rowCount=29` 以及四列样例。
- 写入顺序：必须先把 `collect-jd-phones` 的列定义改成四列，再绑定四个元素。若先绑定，旧三列表头会触发列数归一化并把第四个元素丢弃。活动流程更新后的四个元素为 `collect-jd-phones-browser-1..4`。
- 备份误写：第一次回读与绑定错误选择了 `.sz/draft-restore-backup/20260915_095925_421/io5cd.task`。根因是 `_native_task_file()` 对递归结果只按文件名和完整路径排序，`.sz` 目录排在根目录前。表面上返回“绑定成功”，实际没有改变活动画布，这是严重假阳性。
- 路径修复：原生任务选择现在排除 `draft-restore-backup`，优先项目根目录任务，再考虑正常嵌套任务。新增回归测试模拟更新的备份副本，确认 `read_edit_context()` 仍选择根目录 `io5cd.task`；编辑测试 `26 passed`。
- 活动流程真相：修复后根目录回读只有 8 步，顺序为 `ybQEw,calc-one-plus-one,input-jd-phone,press-enter-search,wait-jd-results,refresh-jd-result-tab,prepare-jd-product-fields,collect-jd-phones`。此前报告的第 9 个重复采集节点只存在于备份副本；`rpa-repair-flow` 因活动流程没有重复节点而拒绝执行。
- 原生编码：活动第 8 步四个 `columnSelectorArray` 都是单节点 raw class：`_newStyle_1k2fi_39`、`_price_9y3st_31`、`_goods_volume_1d8up_1`、`_limit_3z0zc_23`。原生预览返回 `59` 行、4 列，样例按商品名、价格、热度原文、商家对齐，无列坍缩。
- 刷新假成功：`ApplicationOpService.editApplicationInAgent` 返回确认后，第一次单组件运行仍生成旧三列 marker 代码并返回旧三列缓存，耗时 `12.328s`。这证明“重新打开应用 RPC 成功”不代表当前 editor 已重新加载磁盘节点。
- 正确刷新：调用当前 editor 的 `WorkbenchService.reload`，等待约 5 秒后再运行 `RunService.runComponentInEditor(componentIds=[collect-jd-phones], useCache=true)`。第二次耗时 `6.86s`，返回 `state=success`；生成代码包含四个 raw class，不含 `data-shizai-agent-*` marker，也不含 `RunJsInIeBySelector` 调用。
- 最终运行证据：`.sz/variable_cache/io5cd.json` 的 `collect-jd-phones` 保存 59 条四列数据，表头为 `商品名称,价格,热度原文,商家`。前三条商家分别为“小米京东自营旗舰店”“小米京东自营旗舰店”“努比亚官方旗舰店”。
- 当前限制：历史 `prepare-jd-product-fields` 仍留在流程中，但本次单组件运行没有执行它。现有 MCP 没有基于 revision 的 `remove_step`，不能声称已清理。下一版应提供 `remove_step`，并把 `WorkbenchService.reload -> editor ready -> 精确组件运行` 封装成正式刷新/单步接口。

## 步骤 17：把刷新和精确组件运行固化为正式 MCP 接口

- 时间：2026-09-15 17:40-18:00 +08:00
- 人工意图：把步骤 16 的临时探针变成 `shizai_rpa_reload_editor` 和 `shizai_rpa_run_component`，在同一活动流程上验证 revision、editor 身份、刷新就绪、单节点代码和本次输出，不再依赖固定等待或人工观察成功提示。
- 刷新保护：接口先校验 `expected_revision`，再核对 `edit-state.json.currentEditorId` 与设计器活动 editor，并在 `isNeedSave=true` 时拒绝刷新。`WorkbenchService.reload` 返回后必须连续两次读到同一 editor 且无未保存修改；返回仍明确标记 `canvas_configuration_verified=false`，要求继续运行目标组件。
- 首次超时：第一次现场调用虽然 reload 已确认，但 ready 探针报 `render-call-render-res` 超时。根因是底层响应等待为 15 秒，而 WebSocket `recv` 使用 30 秒 timeout；一次阻塞就耗尽整个 ready 窗口。底层现支持显式 `response_timeout`，reload 使用 0.25-1.5 秒短探针轮询。修正后相同 revision 的刷新约 `6.24s` 成功，`ready_checks=2`。
- 旧缓存门禁：组件运行前后记录 `component_run.py` 和 `.sz/variable_cache/io5cd.json` 的 mtime、大小与 SHA-256。一次采集调用报“变量 网页对象 未定义”时，磁盘仍有旧 59 行结果，但缓存没有更新，因此返回 `cache_fresh=false`、`runtime_verified=false`，没有把旧结果误报为本次成功。
- 缓存语义：`RunService.runComponentInEditor` 的 `useCache` 和 `createCodeParams.useCache` 都必须为 `true`。只关闭后者也会失去无法从 JSON 独立重建的上游浏览器对象，不能把它当成普通 Python 文件缓存。双缓存对照中，同一请求只包含 `componentIds=["collect-jd-phones"]`，约 `5.234s` 返回成功。
- 正式接口结果：最终 `shizai_rpa_run_component` 约 `5.125s` 返回 `state=success`；本次 `component_run.py` 只包含 `collect-jd-phones`，没有运行其它七个组件；本次变量缓存更新为 59 行四列，表头为 `商品名称,价格,热度原文,商家`，因此 `exact_component_verified=true`、`cache_fresh=true`、`runtime_verified=true`。
- 测试与模型预算：新增 fake renderer 回归覆盖 revision 冲突、错 editor、未保存状态、连续 ready、单 component ID、代码证据以及采集缓存新鲜度。MCP 当前为 `full=65`、`rpa-authoring=40`；精简工具 schema 为 29,961 字符，继续低于 30,000 字符预算。
- 当前边界：工作区源码已具备两个正式工具，但本对话加载的已安装 MCP 二进制尚未包含它们，因此现场通过共用核心的 CLI 验证。正式安装包需要重新构建、安装并在新会话通过 MCP tools/list 和真实工具调用复测。历史 `prepare-jd-product-fields` 仍未删除，后续需实现 revision 驱动的 `remove_step`。

## 步骤 18：revision 驱动的 remove_step（现场删除已执行，单组件复核待完成）

- 时间：2026-09-20 +08:00
- 人工意图：把步骤 15-17 里反复出现的“历史 `prepare-jd-product-fields` 无法清理”变成正式能力：用 revision 驱动删除主流程步骤，而不是手工改 `.task`，并在删除后重新物化、再校验。
- 核心实现：`project.remove_rpa_step` 读取原生任务后定位目标步骤，支持 `--dry-run` 只读预览；提交要求 `expected_revision`，写入前再次比对内容 revision，冲突返回结构化 `revision_conflict` 和当前版本。
- 依赖保护：删除前用原生语句输出变量表检查被删步骤的输出变量是否仍被后续语句引用（引用关系按无引号字符串参数匹配，带引号的字面量不算引用）。命中时返回 `step_has_dependents`，列出引用它的步骤、组件、变量和参数；改绑下游后重试，或显式 `allow_dependents=true`。
- 计划与元数据同步：删除同时重算 `mcpExecutionPlan` / `mcpComponentPlan` 的 steps 和 `plan_id`；只有当被删步骤是流程中唯一 JS 节点时才清理 `mcpUseJs*` 标记，仍有其它 JS 节点时保留。
- 原生编码修正：现场 `.task` 里 `var_name`、`var0/var1/var2`、`designated_obj`、`web_page` 保存的是不带引号的变量名（如 `网页对象`、`商品链接`、`数据采集结果`），只有字面量参数才带引号。此前测试夹具误写成带引号形式，导致依赖检查看似不生效；产品逻辑本身正确，夹具已按现场编码改正。
- 只读预览的 UI 副作用：预览路径原先会调用 `validate_rpa_project`，而该校验会驱动设计器覆盖层，属于真实缺陷。`validate_rpa_project` 与 `validate_rpa_flow` 现接受关键字参数 `publish_ui`（默认 `True`），预览用 `publish_ui=False` 走同一套真实校验但不发布 UI，所有写入路径行为不变。
- 工具与预算：full `65 → 66` 个工具（声明 45,751 字符 ≤ 46,000），rpa-authoring `40 → 41`（29,603 字符 < 30,000，为 full 的 64.7%），`MCP_INSTRUCTIONS` 1,876 字符。压缩的是冗长说明文字，没有放宽任何预算。
- 证据：完整 pytest `614 passed`（约 142 秒），其中新增 6 项 `remove_step` 回归（只读预览、revision 冲突与依赖拒绝、非法目标拒绝、真实删除并与 statements/实体/计划同步、仍有其它 JS 节点时保留标记、MCP 写入门禁与 CLI/MCP 预览等价）；ruff（改动文件）、compileall、全部 114 个 CLI 帮助入口通过。
- 当前边界：能力已在源码、MCP schema 和 CLI 上验证；步骤 15-17 记录的 `full=65`/`rpa-authoring=40`/29,961 字符已由本步骤更新为上面数值。
- 现场删除（同日，活动流程 `我的电脑流程2`）：先跑 `rpa-remove-step ... --dry-run`，返回 `dependents=[]`、`element_ids=[]`、`metadata_cleanup=[mcpUseJsLink, mcpUseJsTable, mcpJsOutputMode, mcpJsOutputVariable]`；提交后 revision 由 `c7268777…` 变为 `44e8dc10…`，主流程剩 7 步。删除后 `validation.valid=true`、无错误无警告——此前已存在的三个 `use_js_link` 契约错误随过时标记一起消失（它们不是删除造成的）。删除前已把 `io5cd.task`、`szrpa.json`、`variableInfo` 备份到 `_tmp/flow_backup_20260920/`。
- 预览语义修正：dry-run 的 `validation` 是删除前的当前磁盘状态，现显式返回 `validation_basis=current_flow_state`；模型不能把它读成删除造成的破坏，删除后的真实校验只在提交回执里给出。
- 画布刷新：设计器当时没有活动 editor（`found 'none'`），先用 `scripts/probe_designer_reload.py --strategy application-reopen` 重新打开应用，再执行 `rpa-reload-editor`，得到 `editor_ready=true`、`ready_checks=2`、revision 未变、`canvas_configuration_verified=false`。
- 复核受阻（未完成）：`rpa-run-component collect-jd-phones` 返回 `state=error`、`变量 网页对象 未定义`；`.sz/variable_cache/io5cd.json` 当前不存在，`cache=null`、`cache_fresh=false`、`runtime_verified=false`，因此**不能**声称四列采集已验证。按步骤 17 的缓存语义，下一步要先完整运行流程重建上游浏览器对象，再做单组件复核。
- 下一步：重新构建并安装 MCP 与 Native Messaging Host，然后在 `c_rRdLciRpNi` 上完成完整运行 + 单组件复核，并确认表头仍为 `商品名称,价格,热度原文,商家`。

## 步骤 19：0.7.29 重启后的真实 MCP 接入与首组件验证

- 时间：2026-10-08，Asia/Shanghai。
- 用户重启后，本对话真实暴露 43 个 shizai 工具。`auth_status` 返回设计器可达、已登录、ready。
  `browser_status` 返回 `extension_missing`，但 bridge 已在 18768 监听；工具加载与扩展连接是两个状态。
- `read_flow(detail=edit)` 读取根目录 `io5cd.task`，revision `44e8dc10…`，7 个步骤，
  仍是原来的“我的电脑流程2”，没有重建或新增流程。Chrome 的旧扩展仍指向临时目录。
- 第一个现场错误：`rpa_open_browser` 报 `request plan contains no navigation-capable component`。
  实际第一步是 `WebBrowser_CreateV3`，URL 为 `www.jd.com`。此手工流程没有 AI component_plan，
  工具错误地把“无模型计划”当成“无导航组件”。源码已补原生步骤回退与裸域名解析；
  已保存的原生 URL 优先于过时计划，变量对象/变量名/非网页 URL 均拒绝。
- 第二个现场错误：`debug_start(start_from=1)` 暂停在首组件前，但 task 指向
  `.sz/draft-restore-backup/.../io5cd.task`，component_count=9，与 read_flow 的 7 步不一致。
  未发送 next。第一次 stop 返回 cleanup_timeout；第二次 stop 返回 stopped、cleanup_complete=true，
  原生小卡片、接管框和遮罩均已关闭。
- 根因是 debug_session 保留了独立旧任务查找器；源码已改为调用编辑器共用的 `_native_task_file`。
  新回归构造多一个已删除步骤的备份，要求调试只选根任务；只剩备份时必须报找不到任务。
- 通过 Computer Use 打开“我的电脑流程2”画布后，使用本对话正式 MCP
  `run_component(step_id=ybQEw, expected_revision=44e8dc10…)` 精确执行首组件。
  返回 success，耗时 22.234 秒，generated_component_ids=[ybQEw]、exact_component_verified=true、
  cache_fresh=true、runtime_verified=true；网页对象已写入本次缓存。
  Windows 应用清单出现京东标题的 Chrome 窗口。没有执行搜索和采集组件。
- 再次 `browser_status` 仍返回 extension_missing。Computer Use 获取 Chrome 窗口时因无法可靠
  识别 URL 终止本轮 UI 控制；已提示用户从正式安装目录加载独立扩展，未绕过限制。
- 以上两个源码修复尚未打入已安装的 0.7.29。首组件成功证据来自正式已安装 MCP，
  源码任务选择诊断只证明修复后的读取结果，不能充当新版原生调试运行验收。
- 回归结果：导航与任务选择定向测试 11 passed；调试和流程编辑 83 passed（51.30 秒）；
  Ruff 和 git diff --check 通过。现场只读源码诊断确认编辑与调试都选择根任务、7 个步骤。
- 下一步：待正式目录扩展 ready 后，使用已有流程继续视觉取点、原生绑定和精确组件运行；
  调试任务选择修复应进入下一版发布包后再做真实调试验收。

## 步骤 20：扩展加载后的视觉定位、原生采集和字段错位修复

- 时间：2026-10-08，Asia/Shanghai；用户明确告知“已加载”。
- 正式 `browser_status` 返回 ready、connected、扩展 0.1.9。继续原来的“我的电脑流程2”，
  根任务仍 7 步，没有重建。全程业务验证使用当前对话正式附加的 shizai MCP 工具。
- 激活首页截图，单点定位搜索框并绑定 `input-jd-phone`；读取 revision、刷新画布后，
  精确运行输入、Enter、等待三个原生组件。运行页截图显示“手机”，提交后标题变为京东手机搜索。
- 结果页滚动后重新截图；在同一商品卡片取名称、价格、热度原文、商家四个点。
  一次取点约 0.089 秒，一次推断并绑定约 1.315 秒；只读取命中节点及少量祖先证据，
  未向模型枚举整页 DOM。价格/热度选择返回祖先链中的完整字段节点。
- 原生 `validate_step` 预览返回 29 行四列。刷新编辑器、精确运行获取当前标签页组件后，
  运行 `collect-jd-phones`，回执耗时 11.297 秒，返回本次 **59 行四列**。
  generated_component_ids 仅 collect-jd-phones，generated_code_fresh、exact_component_verified、
  cache_fresh、runtime_verified 均为 true。最终 revision 为 `94531f14…`。
- 回读原生缓存：四列无空值，无行宽错误；存在 3 条整行重复，去重后 56 条；一款预约商品价格
  为“待发布”。17 条热度含已售、30 条含评价、12 条含看过，指标可能重叠，不能统一叫销量。
  本轮 `toExcel=0`，JSON 证据保存不等于原生导出 Excel。没有验收分页、100 条和链接字段。
- 发现扩展样例仍用首卡片的子节点位置：其它卡片插入广告/排名后样例价格错位。
  原生绑定使用原始字段 selector，原生预览和缓存正确；没有把错误的扩展样例当正确结果。
- 源码修复为逐行核验字段身份与唯一匹配，缺失/歧义/行外多余匹配直接拒绝；
  样例和原生绑定共用验证后的 fieldSelector。Python 不再自行删除位置条件来扩大匹配。
  扩展源码版本同步为 0.1.10，安装版仍 0.1.9，未构建或发布新版。
- JavaScript 14 passed，Python 浏览器相关回归 37 passed，版本检查 1 passed；Ruff/Node 语法通过。
  修复后的扩展尚未在实际 Chrome 中加载验证。
- 结束时 `rpa_stop` 明确返回 cleanup_complete=true，运行遮罩关闭，设计器已停止。
  原 debug 无活动连接。DeepCapture 停止有通道无响应提示；本轮没有开启官方拾取遮罩。
- 后续瓶颈另记：整图 status 不包含精确组件运行记录，历史日志可能误报插件未启用；
  validate_step 的静态 gate 仍推荐 debug，需统一与 run_component 的验证状态。
- 报告 `docs/MCP_BROWSER_EXTENSION_LIVE_EVALUATION_20261008.md`；原始回执、59 条原文与校验摘要
  仅保存在忽略目录 `_tmp/jd-extension-live-20261008/`。本轮不是 Luna/Qwen 自主能力评测。

## 步骤 21：空闲时桌面胶囊残留的清理与生命周期修复

- 时间：2026-10-08。用户反馈“桌面的胶囊字体还在显示，我现在啥也没干”。
- 对步骤 20 的清理结论作补充：当时 rpa_stop 的 cleanup_complete 证明运行会话/遮罩收尾，
  没有覆盖 NativeSmallCard 小卡窗口，因此不能表述为所有桌面提示已经清理。
- 代码证据：validate_step / validate_flow / add_step 等调用 `_rpa_design_ui_update`，
  NativeSmallCard.show 默认永久保留；返回结果后没有 close。run_component 也没有小卡收尾。
  `rpa_stop` 直接返回 core.native_rpa_stop，仅做运行与 mask 清理；整图 run 和 debug_stop
  已有的 MCP 层全界面清理未覆盖此路径。
- 现场 Computer Use 枚举到实在设计器的四个窗口；小卡截图未能可靠读出文字，
  没有把推测文案当成 OCR 结果。附加 MCP debug_stop 返回 Transport closed，未执行成功。
- 使用已安装的官方 CLI `shizai-agent.exe --json ui small-card --state hidden --allow-write`，
  返回 state=hidden、visible=false、close_result.isSuccess=true、code=0。
  随后的 Computer Use 窗口枚举确认小卡窗口消失，主客户端保留。没有重跑或修改 RPA 流程。
- 源码修复：有限时长的分析、设计、窗口/页面扫描、字段捕获、校验与单组件运行在 finally
  关闭进度胶囊；只关闭卡片，保留调试/拾取会话和上游浏览器缓存。连续调试、官方拾取启动
  和显式 UI 操作不走这条自动关闭路径。调用无写授权被拒时也不触发 UI 收尾。
- 显式 rpa_stop 在 MCP finally 中调用全界面清理，补齐独立小卡和接管框；原生停止失败时
  仍尝试界面清理。关闭胶囊失败不覆盖业务结果，返回 cleanup_complete=false 并保留重试标记。
- 测试：新增生命周期回归 17 passed，现有 MCP/小卡/UI 回归 66 passed，Ruff/diff 检查通过。
  源码尚未发布、没有替换安装版。当前现场问题由官方 CLI 清理，自动收尾等待新版包验收。
