# MCP 独立浏览器扩展

## 目标

独立扩展只服务于 MCP 设计取证，不修改实在 Agent 客户端原扩展。模型观察当前视口截图并给出少量坐标；扩展在浏览器本地完成 `elementFromPoint`、selector 生成、可见性检查和重复区域推断。完整 DOM 树不会发送给模型。

这条路径解决两个问题：模型不再从大量元素枚举中猜目标，原生 RPA 组件仍能获得可执行的 selector。扩展的设计证据不能代替原生组件运行证据。

## 人工精确操作的映射

网页流程按以下顺序边设计边验证：

1. MCP 创建原生流程骨架，并用原生浏览器组件准备运行窗口。
2. 独立扩展用 `shizai_browser_tabs` 选择设计证据标签页；若标签或窗口不在前台，先调用 `shizai_browser_activate_tab`，再用 `shizai_browser_capture_viewport` 返回 PNG、URL、视口、窗口状态、滚动位置和 `pageRevision`。
3. 模型直接看图，只对目标位置调用 `shizai_browser_inspect_points`。输入框、按钮和链接可传 `prefer_interactive=true`，扩展会从视觉命中的子节点提升到最近的交互元素。
4. 调用时同时提供 `project_ref`、`step_id`、`allow_write=true`，MCP 将浏览器本地验证过的 selector 转换为实在原生 `comboSelector` 并绑定现有组件。
5. 使用原生 `debug_start` / `debug_command(action=next)` 运行该组件。只有组件执行和页面结果都正确，才继续设计下一步。
6. 页面发生导航、滚动或结构变化后重新截图。旧 `screenshotId`、URL 或 revision 不可复用。
7. 到结果页后，在同一商品卡片中命中至少两个字段，例如标题和价格，再调用 `shizai_browser_infer_collection`。扩展只接受至少两行且每行都能解析全部字段的重复结构。
8. 将推断字段绑定到原生 `WebBrowser_DataCollectionV5`，执行 `validate_step` 的真实采集预览；`data_row_count=0` 时不能继续运行。

设计证据页可用 `shizai_browser_action` 执行受限的点击、填值和按键，以便快速进入下一状态。原生 RPA 运行窗口仍按相同步骤单步执行，用于验证真实组件对象、参数和 selector。两边状态由 MCP 的步骤顺序协调，不假设两个浏览器共享网页对象。

## MCP 工具

| 工具 | 结果 |
| --- | --- |
| `shizai_browser_status` | 扩展版本、能力、连接状态或安装指导 |
| `shizai_browser_tabs` | 普通 HTTP(S) 标签页的 `tabId`、标题、URL、活动状态 |
| `shizai_browser_activate_tab` | 激活目标标签，并聚焦或恢复所属 Chrome 窗口 |
| `shizai_browser_capture_viewport` | MCP PNG 图片及防过期元数据 |
| `shizai_browser_inspect_points` | 紧凑元素证据、已验证 selector，可选原生绑定 |
| `shizai_browser_infer_collection` | 重复行 selector、相对字段、小样本，可选原生绑定 |
| `shizai_browser_action` | 设计证据页上的受限动作 |

截图坐标使用 PNG 图片像素。扩展在本地读取图片实际尺寸，再换算为 CSS 视口坐标，因此 Windows 高 DPI 缩放不会造成直接偏移。

## 本地通信

扩展 ID 在开发版中固定为 `onlmcbjbkkjllainhobbehhaohnkloem`。Chrome/Edge 通过 `com.shizai.agent.browser` Native Messaging host 获取回环地址和首次安装生成的 token；升级保留 token 与配置端口，默认端口为 18768。扩展主动连接 WebSocket，并用扩展 Origin、ID、协议版本和 token 完成握手。

桥接拒绝浏览器内部页、密码输入框、过期截图、URL 变化和非白名单扩展。动作接口不执行任意 JavaScript。`pressKey` 是合成 DOM 事件，不能代替原生键盘组件；需要可信键盘输入时继续使用原生 RPA 单步。

多个 MCP stdio 进程可通过同一端口的鉴权 relay 共享扩展连接，状态返回 owner/shared。
持有者退出后，存活调用方在下次请求时接管，扩展重连；请求发出后断线则返回 unknown，不重放点击/输入。
`bridge_in_use` 表示端口持有者不支持共享或密钥不一致。每个页面仍按 tabId 与截图版本独立验证。

## 安装

```powershell
npx -y shizai-agent-mcp@latest install
```

0.7.29 起默认复制扩展并注册 Native Messaging host。安装前核验发布清单、实际工具清单和文件哈希。
未上架扩展仍需在 `chrome://extensions` 或 `edge://extensions` 中开启开发者模式，
选择“加载已解压的扩展”，并使用安装结果中的 `browser_extension_path`。
文件已准备和浏览器已加载是两个状态：只有 `shizai_browser_status=ready` 证明真实连接。

普通升级会检测已有 `browser-bridge.json` 并恢复注册，不会无意关闭用户已启用的扩展。
