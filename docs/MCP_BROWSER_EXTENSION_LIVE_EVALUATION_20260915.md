# MCP 独立浏览器扩展真实采集测评

日期：2026-09-15

## 结论

新扩展已经在原流程“我的电脑流程2”（`c_rRdLciRpNi`）留下的京东“手机”搜索结果页上完成真实验证。

- 扩展 `0.1.9` 与本地桥接握手成功。
- 页面截图成功，单次约 `172ms`。
- 重复商品结构推断成功，单次约 `17ms`，页面证据中识别到 `29` 个可见商品卡片。
- 扩展只返回少量结构化字段证据，没有向模型发送完整 DOM。
- 扩展字段转换为原生单节点 class selector 后，实在 Agent `DataCollectionV5` 原生预览返回 `59` 行商品数据。
- 商品名称、价格、热度三列没有坍缩，样例语义对齐。

因此，“截图/模型判断 -> 扩展局部 DOM 验证 -> 原生组件绑定 -> 原生预览”的路径已经跑通。扩展代码没有京东专用 selector；本次京东 class 是运行时页面证据，只用于本次流程绑定和测评。

## 实测环境

- Chrome：`152.0.7977.65`
- 扩展：`Shizai MCP Browser Bridge 0.1.9`
- 扩展 ID：`onlmcbjbkkjllainhobbehhaohnkloem`
- 桥接：`ws://127.0.0.1:18768`
- 原生流程：`C:\Users\edy\AppData\Roaming\Z-Factory\applications\c_rRdLciRpNi`
- 页面：京东“手机”搜索结果页
- 原始遥测：`docs/MCP_BROWSER_EXTENSION_LIVE_EVALUATION_20260915.jsonl`
- 原生结果：`artifacts/native-collection-single-node-result.json`
- 选择器对照：`collection-selector-variants.json`

## 与旧 DeepCapture 的对照

旧方案记录见 `docs/HUMAN_GUIDED_NATIVE_COLLECTION_LOG_20260911.md`：

| 路径 | 耗时/RPC | 结果 |
| --- | ---: | --- |
| 完整页面树扫描 | `15.410s` / `623 RPC` | 可用，但慢且响应大 |
| 显式 root 定向扫描 | `5.216s` / `36 RPC` | 较快，仍依赖树扫描 |
| 新扩展 `list_tabs` | `3.6-4.1ms` | 直接返回标签信息 |
| 新扩展截图 | `171.8ms` | 真实 PNG 与页面 revision |
| 新扩展重复结构推断 | `17.4ms` | 29 个可见卡片和字段样例 |
| 原生 `DataCollectionV5` 预览 | `59` 行 | 四列有效、未坍缩 |

新扩展把模型输入从“枚举整页控件树”缩小为“截图 + 少量点/字段意图 + 局部验证结果”。页面结构分析在浏览器内完成，模型只接收候选 selector、匹配数量、样例行和页面 revision。

## 过程记录

### 1. 安装和路径

Chrome 正式版不会可靠接受普通命令行的 `--load-extension`。用户需要在 `chrome://extensions` 对未上架扩展完成一次“加载已解压的扩展”。安装器负责把扩展复制到稳定安装目录、生成 token、注册 Native Messaging Host，并返回固定扩展目录。

首次测试曾把 Native Messaging manifest 指向临时 release 目录，而冻结 Host 默认从 `%LOCALAPPDATA%\Programs\ShizaiAgent\browser-bridge.json` 读取配置。已确认正式安装必须让 Host、manifest、配置和扩展都使用同一稳定安装根目录，不能依赖父进程继承的环境变量。

### 2. `ERR_CONNECTION_REFUSED`

扩展控制台出现：

```text
WebSocket connection to 'ws://127.0.0.1:18768/' failed:
net::ERR_CONNECTION_REFUSED
```

现场检查确认报错时 `18768` 没有监听。启动桥接后端口进入 `LISTEN`，扩展自动重连并处于 `Established`。随后真实 `status` 返回：

```text
state=ready
connected=true
extensionVersion=0.1.8
```

扩展协议、权限和 ID 均正常，问题属于桥接生命周期。

已完成两层修复：

1. MCP stdio 启动时调用 `BrowserBridgeManager.ensure_started()`，退出时关闭自身桥接。
2. Native Messaging Host 在返回 WebSocket URL 前探测 `127.0.0.1:18768`。未监听时返回 `bridgeUnavailable`，扩展按退避策略安静重试，不再创建一个必然失败并打印红字的 WebSocket。

扩展侧已有 `runtime.onStartup`、`runtime.onInstalled`、30 秒 alarm、内容脚本唤醒、20 秒 keepalive、单飞 bootstrap 和 `250ms-30s` 退避重连。

当前开发测试由 `scripts/browser_bridge_live_eval.py` 持有桥接。正式包需要重新构建 Native Messaging Host 后，上述端口预探测才会进入安装版本。

### 3. 标签和截图

扩展重载后，真实 `list_tabs` 在约 `3.6ms` 返回 5 个京东“手机”标签，并正确标识活动标签。`capture_viewport` 在约 `172ms` 返回 `1920x878` PNG、CSS viewport、滚动位置和 `pageRevision=63`。

截图权限使用 `<all_urls>`，同时拒绝 `chrome:`、`edge:`、`about:`、`file:` 和 `view-source:` 页面。密码输入框仍处于动作拒绝边界内。

### 4. 重复结构推断

扩展在当前页面得到：

```json
{
  "commonSelector": "div._info_1y1ag_17",
  "rowCount": 29,
  "columns": [
    {"name": "商品名称", "sourceSelector": "span._newStyle_1k2fi_39"},
    {"name": "价格", "sourceSelector": "span._price_9y3st_31"},
    {"name": "热度原文", "sourceSelector": "span._goods_volume_1d8up_1"},
    {"name": "商家", "sourceSelector": "span._limit_3z0zc_23"}
  ]
}
```

推断响应包含样例行，因此模型可以在写入流程前检查列是否对齐。京东当前展示的是“评价、人看过、加购、已售”等混合热度，字段必须命名为“热度原文”，不能声称都是销量。

### 5. 原生选择器兼容性

第一版转换把卡片 selector 和字段 selector 拼成多节点 descendant chain。扩展侧样例正确，但原生 `DataCollectionV5` 只返回表头。

对照探针确认原生运行时的行为：

- 单节点 raw class：成功。
- 单节点 raw attribute：成功。
- 卡片 + raw attribute：成功。
- `cssSelector` 属性：失败。
- 带 JSON 引号的 class/attribute 值：失败。
- 当前多节点字段链：失败。

转换现已保留扩展验证过的 `sourceSelector`，去除位置型 `nth-*`，并只写入字段自身的单节点 raw class。`commonSelector` 继续保存在 `mcpBrowserEvidence` 和 `card_context` 中，用于证据和后续重采集，不进入原生字段链。

直接调用实在 Agent `DataCollectService.dataCollectCapturePreview` 的最终结果：

```text
row_count=59
headers=商品名称,价格,热度原文,商家
```

样例包含真实手机名称、数值价格，以及“2万+条评价 99%好评”“4万+人看过”等热度文本。该验证没有执行 JS 字段注入。

### 6. 原流程和旧 MCP 版本问题

活动根任务目前有一个 `WebBrowser_DataCollectionV5` 步骤，以及一个历史 JS 预处理步骤。旧探针会在运行前把第 8 步改回 JS marker，所以其 `29` 行结果只证明旧方案可用，不计入新扩展直接绑定成绩。本报告以四列 raw class 原生预览和精确组件运行的 `59` 行作为最终证据。

现场进一步确认工作区源码原来也可能把项目根目录错误解析到 `.sz\draft-restore-backup\...`。根因是递归 `.task` 候选排序让 `.sz` 备份排在根任务前。现已排除 `draft-restore-backup` 并优先根目录任务，真实回读指向 `c_rRdLciRpNi\io5cd.task`；回归测试覆盖“备份更新但活动任务必须胜出”。正式安装版本仍需重新构建。

### 7. 活动画布刷新与精确组件运行

根任务写入四列 raw class 后，`ApplicationOpService.editApplicationInAgent` 虽然返回确认，设计器第一次生成的 `component_run.py` 仍是旧三列 marker。该运行耗时 `12.328s` 且返回旧三列缓存，证明应用重开确认不能作为 editor 刷新证据。

随后调用 `$CurrentActiveEditorClientId$/WorkbenchService.reload`，等待 editor 重新注册，再精确运行 `collect-jd-phones`。第二次运行耗时 `6.86s`、`state=success`；生成代码包含四个 raw class，不含旧 marker 和 JS 预处理，变量缓存包含 59 条四列数据。

正式 MCP 需要把刷新链路封装为一个有状态操作：核对 revision、刷新当前 editor、等待 ready、回读活动组件配置、再允许精确组件运行。仅报告 RPC acknowledged 会产生假阳性。

### 8. 前台标签激活与截图超时

在扩展 `0.1.8` 上，新活动京东标签两次截图都由桥接层等待满 30 秒；同一标签执行 DOM 滚动只需约 `12ms`。因此 WebSocket 与内容脚本没有故障，阻塞发生在 `chrome.tabs.captureVisibleTab`。

扩展 `0.1.9` 新增 `activate_tab`，会恢复最小化窗口、激活标签、聚焦所属 Chrome 窗口，并返回 `windowFocused/windowState`。`capture_viewport` 现在拒绝后台或最小化窗口，并在错误中返回 `next=activate_tab`；`captureVisibleTab` 本身增加 8 秒内部超时，避免 MCP 只看到笼统的 30 秒超时。

真实复测中，`activate_tab` 用时 `17.799ms`，返回 `active=true`、`windowFocused=true`、`windowState=maximized`；随后截图用时 `166.642ms`，返回非空 `1920x878` PNG、`windowFocused=true` 和当前页面 revision。截图文件为 `artifacts/browser-extension-live-jd-v019-activated.png`。

### 9. 正式刷新和单组件接口验收

源码新增 `shizai_rpa_reload_editor` 与 `shizai_rpa_run_component`。刷新接口使用 `expected_revision` 防止旧读写入，核对活动 editor 和未保存状态，并要求 reload 后连续两次 ready。首次现场调用暴露底层 15 秒响应等待与 30 秒 socket timeout 不一致；改为显式短响应探针后，同一活动 editor 在约 `6.24s` 内通过两次 ready 检查。

单组件接口回读本次 `component_run.py` 和变量缓存。失败对照中，RuntimeHost 报“变量 网页对象 未定义”，磁盘虽然残留旧 59 行缓存，接口仍因 `state=error`、`cache_fresh=false` 返回 `runtime_verified=false`。对照进一步证明 `createCodeParams.useCache=true` 负责恢复设计器生成代码的上游运行上下文，不能为了强制重生成而关闭。

最终正式调用只传 `componentIds=["collect-jd-phones"]`，约 `5.125s` 返回 `state=success`。生成代码只包含目标节点，本次缓存更新为 59 行，表头为 `商品名称,价格,热度原文,商家`，最终 `exact_component_verified=true`、`cache_fresh=true`、`runtime_verified=true`。这组证据同时区分了整图运行、旧缓存假阳性和 editor 刷新确认假阳性。

## 已完成修改

- 扩展版本升级至 `0.1.9`。
- 新增目标标签激活、窗口聚焦诊断和截图内部超时。
- MV3 唤醒、心跳、单飞连接和持续重连。
- 截图权限、错误码透传和页面 revision 校验。
- MCP 启动时托管桥接生命周期。
- Native Messaging Host 端口预探测，避免无监听时创建失败 WebSocket。
- 浏览器证据优先使用已验证 `sourceSelector`。
- 去除字段 selector 的位置型伪类。
- 原生采集字段改为单节点 raw class selector。
- 原生输出变量允许设计器生成的 `_1` 等合法后缀。
- DAP 重复 `configurationDone` 已修复。
- 新增 revision 驱动的 editor reload/ready 工具。
- 新增带生成代码和新鲜变量缓存门禁的精确组件运行工具。

## 验证

- 完整 Python 回归：`606 passed`；新增 dispatch 授权/转发测试后定向回归：`8 passed`
- 浏览器桥接与 MCP：`17 passed`
- 原生 raw class 预览与精确组件运行：`59` 行、4 列
- 扩展握手：`ready / connected / 0.1.9`
- 调试清理：完成，原生遮罩已关闭

## 发版前剩余事项

1. 用当前源码构建新的 MCP 和 Native Messaging Host。
2. 安装到稳定目录，确认 `browser-bridge.json`、Host manifest 和扩展目录一致。
3. 在全新 Codex 会话验证浏览器工具可见，并确认不是旧二进制。
4. 处理多个 MCP stdio 进程争用同一 `18768` 端口：正式方案应使用单实例本地 broker，其他 MCP 进程通过本地 IPC 复用，不应各自竞争扩展连接。
5. ~~增加 `remove_step(expected_revision)`，清理历史 JS 预处理节点。~~ 已完成（2026-09-20）：
   `shizai_rpa_remove_step` / `workflow rpa-remove-step` 支持 revision 驱动的步骤删除，
   预览不落盘、不驱动原生 UI；在真实流程上的删除与 `reload_editor` + `run_component`
   复核仍待执行，见 [人机协同原生采集记录](HUMAN_GUIDED_NATIVE_COLLECTION_LOG_20260911.md) 步骤 18。
6. 重新构建和安装包含正式 editor reload/ready 与单组件运行工具的 MCP，在全新会话复测 tools/list 和真实调用。
