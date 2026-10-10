# Shizai MCP Browser Bridge

This is an independent Manifest V3 Chrome/Edge extension for MCP authoring. It lets an MCP client observe a tab, capture the visible viewport, turn model supplied coordinates into DOM elements, validate locator candidates, probe a text input, infer a repeated collection from a few selected fields, and manage a project-owned design tab.

The model receives a screenshot and returns coordinates. The extension performs `document.elementFromPoint` and local selector/collection checks. It never sends a full DOM tree. Password inputs and their values are rejected or blanked. The manifest uses `<all_urls>` because Chrome requires it for `captureVisibleTab` calls made without a user gesture; the service worker still rejects Chrome internal pages (`chrome:`, `edge:`, `about:`, `file:` and `view-source:`) before capture or inspection.

Load it locally with `chrome://extensions` or `edge://extensions`, enable Developer mode, and choose **Load unpacked** on this directory. The MCP side sends `{protocolVersion:"1.0", requestId, method, params}`. Every response uses `{protocolVersion:"1.0", requestId, ok, result|error}`. `HELLO`/`hello` returns `extensionVersion` and `capabilities` in both the result and top-level response fields. The MVP keeps `chrome.runtime.sendMessage` for local testing. For MCP transport, the service worker calls `chrome.runtime.sendNativeMessage("com.shizai.agent.browser", {type:"bootstrap", protocolVersion, extensionVersion, capabilities})`, expects `{wsUrl, token}`, then opens a loopback WebSocket. Its first frame is `{type:"hello", protocolVersion, token, extensionId, extensionVersion, capabilities}`; subsequent frames are the same request/response envelopes. The token is supplied by the native host and is never embedded in the extension.

The release installer can prepare the native host and copied extension directory with `npx -y shizai-agent-mcp@latest install --with-browser-extension`. Chrome and Edge still require the user to load or approve an extension; ordinary installers cannot silently load an unpacked extension.

Supported actions:

* `HELLO` / `hello`
* `list_tabs`
* `activate_tab` with `{tabId}` to activate the tab and focus or restore its Chrome window before a screenshot
* `observe_tab`
* `capture_viewport`
* `inspect_points` with screenshot-pixel `points: [{x, y, preferInteractive}]`; the extension converts high-DPI image coordinates to CSS viewport coordinates locally.
* `validate_locator` with `selector` and optional `point`
* `probe_input` with `selector` and a short `value`
* `infer_repeated_collection` with `fields: [{name, selector}]`. From extension 0.1.10 onward, inference verifies a unique field identity in every repeated row. Inserted badges, adverts and wrapper changes do not change the field by sibling position. Missing/ambiguous fields, duplicate column targets and visible field matches outside the selected rows fail explicitly. The response preserves the input in `sourceSelector` and returns the browser-verified `fieldSelector`, used unchanged for native binding and preview. Python must not remove positional predicates or invent a replacement selector. Updated MCP source requires `verification.method=field-identity-per-row`; an older loaded extension must be upgraded and reloaded before binding.
* `performAction` with `{tabId, pageRevision, action:"click"|"setValue"|"pressKey"|"scroll", target:{selector}, value|key|deltaX|deltaY}`. The MCP uses a selector returned by point inspection. This is deliberately constrained and does not execute arbitrary scripts. `pressKey` dispatches synthetic DOM keyboard events; it is not a trusted native keyboard input. `scroll` clamps each request to two current viewports.
* `open_design_tab` and `close_design_tab` with an opaque `ownerKey` and HTTP(S) URL. The extension stores only the tab id in session storage and closes only that tab; it never guesses by URL, closes the active tab, or kills the browser process.

`capture_viewport` accepts an active ordinary web tab and returns a `screenshotId`, tab URL, viewport, scroll position, and page revision. `inspect_points` and `infer_repeated_collection` must send that `screenshotId` and matching `pageRevision`; the service worker rejects an unknown, navigated, or stale screenshot.

Coordinates are valid only for the screenshot and page revision that produced them. Call `observe_tab` again after navigation, scrolling, resizing, or a meaningful page update. The first MVP keeps the screenshot data URL in the response so the MCP/model layer can inspect it; a production bridge should put an explicit size/session limit around that channel.

The bridge uses one loopback port and one extension connection. Multiple MCP processes share the connection through the authenticated `/mcp` relay, reporting `connection_mode=owner/shared`. A surviving manager can acquire the port after its owner exits. An action lost after transmission has an unknown outcome and is never replayed automatically; capture the current page before continuing. Only an incompatible listener reports `bridge_in_use`.

Run the core tests from this directory:

```text
node --test test/core.test.js
node --check service_worker.js
node --check content_script.js
node --check src/core.js
```
