# 原生 UI 反馈

## 小卡调用

实在 Agent 的小卡由桌面端自己创建。CLI 通过当前已登录的本地业务通道复用它，不会创建新的浏览器窗口，也不会复制登录会话。

先打开一个 PowerShell，进入 `shizai-agent` 项目目录，执行：

```powershell
shizai-agent --json ui small-card --state designing --allow-write
```

自定义文案并停留 5 秒后关闭：

```powershell
shizai-agent --json ui small-card --state scanning --message "正在扫描当前页面元素" --duration 5 --close --allow-write
```

一次查看完整状态序列：

```powershell
shizai-agent --json ui small-card --demo --interval 1.5 --allow-write
```

手动隐藏：

```powershell
shizai-agent --json ui small-card --state hidden --allow-write
```

`--allow-write` 只表示允许改变桌面 UI，不会写入流程文件。小卡窗口状态是可复用的：重复打开时可能返回“已打开”类业务提示，但文本更新仍会继续执行。

真实 MCP 先显示 `designing` 小卡完成意图分析、任务拆解、组件计划和计划校验。只有计划已经持久化、即将首次打开浏览器/扫描/捕获/运行时，才显示 5 秒倒计时：`塔斯将在 5 秒后接管你的电脑`，逐秒更新到 1 秒。倒计时结束后打开 `WindowManagerService.openLoadingWindow` 对应的全屏透明原生窗口；该窗口加载设计器自带 `grow-loading.html`，显示蓝紫色呼吸边框且鼠标事件穿透。设计操作结束调用 `closeLoadingWindow`；正式运行需要重新接管时会在运行前再次倒计时。

## 状态

| 状态 | 用途 | 小卡表现 |
| --- | --- | --- |
| `takeover` | 接管当前电脑前 | 原生 Tars Lottie 旋转 |
| `designing` | 规划组件和流程 | 原生 Tars Lottie 旋转 |
| `scanning` | 扫描 Document/OCR | 原生 Tars Lottie 旋转；真实截图拾取时由设计器原生拾取器显示效果 |
| `capturing` | 捕获元素 | 原生 Tars Lottie 旋转 |
| `validating` | 校验组件和绑定 | 原生 Tars Lottie 旋转 |
| `running` | 运行流程组件 | 原生 Tars Lottie 旋转 |
| `waiting` | 等待页面或结果 | 原生 Tars Lottie 旋转 |
| `ready` | 等待下一步操作 | 原生 Tars Lottie 旋转 |
| `success` | 操作完成 | 更新完成文案后可关闭 |
| `error` | 操作失败 | 更新错误文案后可关闭 |
| `hidden` | 结束反馈 | 关闭小卡 |

小卡本身只有“文本 + 循环 Tars Logo”这条渲染能力，没有一个可由 CLI 传入的独立 `scan=true` 参数。扫描是通过 `--state scanning` 更新文案；真实网页采集时，以下 MCP/CLI 工具会同步发布更详细的 `ui_feedback`：

```text
shizai_rpa_start_design
-> shizai_rpa_capture_windows
-> shizai_rpa_scan_page
-> shizai_rpa_capture_elements
```

读取最近一次反馈：

```powershell
shizai-agent --json ui status
```

## 四周遮罩

全桌面蓝紫接管边框由 `WindowManagerService.openLoadingWindow/closeLoadingWindow` 管理。它与下面的 `MaskAppService` 运行遮罩不是同一组件；流程设计和页面识别使用前者。

完整运行遮罩由设计器主进程的 `MaskAppService.showRunMask` 管理。它会根据 `operateFrom` 把遮罩分发到首页、当前编辑器和已打开的对话窗口；`normal` 适合流程设计器，`home` 适合首页。直接对首页渲染器调用 `HomePageMaskService.showMaskV2` 只能覆盖一个渲染器，不能代表完整运行态。

显示 5 秒后关闭：

```powershell
shizai-agent --json ui mask --mask-type white --tips "TARS 正在处理当前任务..." --duration 5 --close --allow-write
```

验证时命令默认会先激活实在 Agent 主窗口。若只想让后台窗口接收遮罩，可追加 `--no-activate`。如果需要肉眼确认显示过程，省略 `--close`；命令会保持遮罩打开，再执行下面的关闭命令。

立即关闭当前遮罩：

```powershell
shizai-agent --json ui mask --close-now --allow-write
```

CLI/MCP 使用的真实协议是：

```text
serviceName: MaskAppService
show: showRunMask([{operateFrom:"normal", runType:"run", tips, transparent:false, isShowHeader:false}])
close: closeRunMask([{requestId}])
```

`MaskAppService.showRunMask` 会返回设计器生成的 `requestId`，CLI/MCP 会回传它并在同一次调用中用它关闭。命令行的 `--close-now` 也会走 `MaskAppService.closeRunMask`，因此可用上一次返回的 request id 精确清理。遮罩和小卡都复用当前实在 Agent 的已认证桌面 WebSocket，不会新开浏览器或复制 Chrome 登录态。

## 扫描动画

一次触发设计器原版遮罩、截图拾取和 TARS 小卡阶段文案：

```powershell
shizai-agent --json ui scan-demo --mask-type white --interval 1.5 --allow-write
```

对应 MCP 调用：

```json
{
  "action": "scan_demo",
  "mask_type": "white",
  "interval": 1.5,
  "allow_write": true
}
```

调用工具名为 `shizai_ui_feedback`。它只负责 UI 反馈，不会伪造元素、不创建流程，也不会替代 DeepCapture。

安装包中与这组视觉效果对应的资源在设计器 `resources/fe-assets` 附近：

```text
home-fusion/pages/small-card.js
home-fusion/pages/small-card.css
dialogs-v1/dialog.js
dialogs-v1/dialog.css
browser/app/tars-element-pick-storyboard-animation.gif
```

`scan-demo` 复用了安装包中截图拾取服务的真实调用顺序：

```text
WindowManagerService.minimizeWindow([{requesterName:"agentScreenShot", excepts:[]}])
-> MaskAppService.showRunMask([{operateFrom:"normal", tips, ...}])
-> 等待 100ms
-> NativeOperateService.transferPluginAction([
     {pluginAction:"Element.ScreenshotCapture",
      jsonParams:{requestId, actionCmd:"start", memoryInfo:{}},
      runType:"common"}
   ])
```

结束时会调用 `restoreWindowWithDelay`、`MaskAppService.closeRunMask` 和同一个截图动作的 `actionCmd:"stop"`。该演示不会调用 `openLoadingWindow`，也不会由 MCP 自己绘制 Tk 遮罩；真实网页元素扫描仍由 `shizai_rpa_scan_page` 和 DeepCapture 完成。

如果 `transferPluginAction` 返回 `code=40009995`，表示当前设计器没有接受这次原生截图拾取上下文。请从支持“截图拾取/选元素”的流程编辑入口触发，并确认目标流程和当前 Chrome 页面已经打开；MCP 不会用自绘遮罩或替代动画绕过这个设计器状态。
