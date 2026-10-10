"use strict";
const PROTOCOL_VERSION = "1.0";
const EXTENSION_VERSION = "0.1.11";
const BUILD_ID = "20261009-owned-design-tabs-1";
const CAPABILITIES = ["status", "list_tabs", "activate_tab", "observe_tab", "capture_viewport", "inspect_points", "validate_locator", "probe_input", "infer_repeated_collection", "performAction", "open_design_tab", "close_design_tab"];
const BRIDGE_WAKE_ALARM = "shizai-browser-bridge-wake";
const screenshotCache = new Map();
let socket = null;
let bootstrapPending = false;
let reconnectTimer = null;
let reconnectDelay = 250;
let keepaliveTimer = null;
let designTabQueue = Promise.resolve();
// Session storage survives worker suspension, but is cleared when Chrome
// exits. A restored browser can never inherit ownership of recycled tab ids.
function manageDesignTab(method, params) {
  const operation = designTabQueue.then(async function () {
    if (!/^[a-f0-9]{64}$/.test(params.ownerKey || "")) throw new Error("invalid_browser_owner");
    const key = "shizai-design-tab:" + params.ownerKey;
    const stored = (await chrome.storage.session.get(key))[key];
    let tab = null;
    if (stored && Number.isInteger(stored.tabId)) {
      try { tab = await chrome.tabs.get(stored.tabId); }
      catch (error) {
        if (!/No tab with id|Invalid tab ID/i.test(String(error && error.message || error))) throw error;
        await chrome.storage.session.remove(key);
      }
    }
    if (method === "close_design_tab") {
      if (!tab) return { status: "already_closed", closed: false, owned: true };
      // No caller-supplied tabId, URL matching, active-tab or process killing.
      await chrome.tabs.remove(tab.id);
      await chrome.storage.session.remove(key);
      for (const [id, shot] of screenshotCache) if (shot.tabId === tab.id) screenshotCache.delete(id);
      return { status: "closed", closed: true, owned: true, tabId: tab.id };
    }
    let url;
    try { url = new URL(params.url); } catch (_) { throw new Error("invalid_url"); }
    if (!/^https?:$/.test(url.protocol)) throw new Error("http_url_required");
    const reused = Boolean(tab);
    if (!tab) {
      tab = await chrome.tabs.create({ url: "about:blank", active: true });
      try { await chrome.storage.session.set({ [key]: { tabId: tab.id, initialUrl: params.url } }); }
      catch (error) { await chrome.tabs.remove(tab.id); throw error; }
    }
    // Repeated requests with the same initial URL preserve search/login state.
    const navigate = !reused || stored.initialUrl !== params.url;
    tab = await chrome.tabs.update(tab.id, { active: true, ...(navigate ? { url: params.url } : {}) });
    if (navigate) await chrome.storage.session.set({ [key]: { tabId: tab.id, initialUrl: params.url } });
    const window = await chrome.windows.get(tab.windowId);
    await chrome.windows.update(tab.windowId, { focused: true, ...(window.state === "minimized" ? { state: "normal" } : {}) });
    return { opened: true, owned: true, reused, tabId: tab.id, windowId: tab.windowId, url: tab.url || params.url };
  });
  designTabQueue = operation.catch(function () {});
  return operation;
}
function envelope(requestId, ok, result, error) { const output = { protocolVersion: PROTOCOL_VERSION, requestId: requestId || null, ok }; if (ok) output.result = result || null; else output.error = error || { code: "request_failed" }; return output; }
function unsupported(url) { return !url || /^(chrome|edge|about|file|view-source):/i.test(url); }
async function screenshotDimensions(dataUrl, viewport) {
  try {
    const bitmap = await createImageBitmap(await (await fetch(dataUrl)).blob());
    const dimensions = { width: bitmap.width, height: bitmap.height };
    bitmap.close();
    return dimensions;
  } catch (_) {
    const ratio = Number(viewport && viewport.devicePixelRatio) || 1;
    return { width: Math.round((viewport && viewport.width || 0) * ratio), height: Math.round((viewport && viewport.height || 0) * ratio) };
  }
}
function withTimeout(promise, timeoutMs, code) {
  let timer = null;
  return Promise.race([
    promise,
    new Promise(function (_, reject) {
      timer = setTimeout(function () { reject(new Error(code)); }, timeoutMs);
    })
  ]).finally(function () { if (timer) clearTimeout(timer); });
}
async function captureTabPng(tab) {
  try {
    return {
      dataUrl: await withTimeout(
        chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }),
        8000,
        "capture_visible_tab_timeout"
      ),
      captureMode: "window"
    };
  } catch (windowError) {
    if (String(windowError && windowError.message || windowError).includes("capture_visible_tab_timeout")) throw windowError;
    if (!tab.active) throw windowError;
    try {
      return {
        dataUrl: await withTimeout(
          chrome.tabs.captureVisibleTab(undefined, { format: "png" }),
          8000,
          "capture_visible_tab_timeout"
        ),
        captureMode: "current_window"
      };
    } catch (currentWindowError) {
      const hasAllUrls = await chrome.permissions.contains({ origins: ["<all_urls>"] });
      throw new Error(
        "capture_visible_tab_failed: " + String(currentWindowError && currentWindowError.message || currentWindowError) +
        "; all_urls=" + String(hasAllUrls) + "; tab_active=" + String(Boolean(tab.active)) +
        "; first_error=" + String(windowError && windowError.message || windowError)
      );
    }
  }
}
async function contentRequest(tabId, message) {
  const tab = await chrome.tabs.get(tabId);
  if (unsupported(tab.url)) throw new Error("unsupported_page");
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    const text = String(error && error.message || error);
    if (!/Receiving end does not exist|Could not establish connection/i.test(text)) throw error;
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["src/core.js", "content_script.js"]
    });
    return chrome.tabs.sendMessage(tabId, message);
  }
}
async function dispatch(message, sender) {
    try {
      if (!message || message.protocolVersion !== PROTOCOL_VERSION) throw new Error("protocol_version_required");
      const method = message.method;
      const params = message.params || {};
      const tabId = params.tabId || (sender.tab && sender.tab.id);
      if (method === "HELLO" || method === "hello" || method === "status") {
        const granted = await chrome.permissions.getAll();
        return envelope(message.requestId, true, {
          state: "ready",
          connected: true,
          extensionVersion: EXTENSION_VERSION,
          buildId: BUILD_ID,
          capabilities: CAPABILITIES,
          permissions: granted.permissions || [],
          origins: granted.origins || []
        });
      }
      if (method === "bridge_wake") {
        bootstrapWebSocket();
        return envelope(message.requestId, true, { state: socket ? "connecting" : "scheduled" });
      }
      if (method === "list_tabs") {
        const tabs = await chrome.tabs.query({});
        return envelope(message.requestId, true, {
          tabs: tabs.filter(function (tab) { return !unsupported(tab.url); }).map(function (tab) {
            return { tabId: tab.id, windowId: tab.windowId, active: Boolean(tab.active), title: tab.title || "", url: tab.url || "" };
          })
        });
      }
      if (method === "open_design_tab" || method === "close_design_tab") {
        return envelope(message.requestId, true, await manageDesignTab(method, params));
      }
      if (!tabId) throw new Error("tab_id_required");
      if (method === "activate_tab") {
        const tab = await chrome.tabs.get(tabId);
        if (unsupported(tab.url)) throw new Error("unsupported_page");
        const before = await chrome.windows.get(tab.windowId);
        if (before.state === "minimized") await chrome.windows.update(tab.windowId, { state: "normal" });
        await chrome.tabs.update(tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        const currentTab = await chrome.tabs.get(tabId);
        const currentWindow = await chrome.windows.get(tab.windowId);
        return envelope(message.requestId, true, {
          tabId,
          windowId: tab.windowId,
          title: currentTab.title || "",
          url: currentTab.url || "",
          active: Boolean(currentTab.active),
          windowFocused: Boolean(currentWindow.focused),
          windowState: currentWindow.state || "unknown",
          previousWindowFocused: Boolean(before.focused),
          previousWindowState: before.state || "unknown"
        });
      }
      if (method === "capture_viewport") {
        const tab = await chrome.tabs.get(tabId);
        if (unsupported(tab.url)) throw new Error("unsupported_page");
        if (!tab.active) throw new Error("target_tab_not_active");
        const windowInfo = await chrome.windows.get(tab.windowId);
        if (!windowInfo.focused || windowInfo.state === "minimized") {
          throw new Error(
            "target_window_not_foreground: focused=" + String(Boolean(windowInfo.focused)) +
            "; state=" + String(windowInfo.state || "unknown") +
            "; next=activate_tab"
          );
        }
        const observation = await contentRequest(tabId, { protocolVersion: PROTOCOL_VERSION, requestId: message.requestId + ":observe", method: "observe_tab", params: {} });
        if (!observation || observation.ok === false) return observation;
        const capture = await captureTabPng(tab);
        const dataUrl = capture.dataUrl;
        const screenshotId = "shot-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
        const result = observation.result || {};
        const image = await screenshotDimensions(dataUrl, result.viewport);
        const coordinateScale = { x: result.viewport.width / image.width, y: result.viewport.height / image.height };
        screenshotCache.set(screenshotId, { tabId, url: tab.url, title: tab.title || "", viewport: result.viewport, image, coordinateScale, scroll: result.scroll, pageRevision: result.pageRevision, createdAt: Date.now() });
        while (screenshotCache.size > 16) screenshotCache.delete(screenshotCache.keys().next().value);
        return envelope(message.requestId, true, { tabId, screenshotId, dataUrl, url: tab.url, title: tab.title || "", viewport: result.viewport || { width: tab.width, height: tab.height }, image, pointCoordinates: "screenshot_pixels", captureMode: capture.captureMode, windowFocused: Boolean(windowInfo.focused), windowState: windowInfo.state || "unknown", scroll: result.scroll, pageRevision: result.pageRevision });
      }
      if (method === "inspect_points" || method === "infer_repeated_collection") {
        const shot = screenshotCache.get(params.screenshotId);
        if (!shot) throw new Error("unknown_screenshot");
        const tab = await chrome.tabs.get(tabId);
        if (typeof BrowserBridgeCore !== "undefined") BrowserBridgeCore.validateScreenshotReference(shot, { tabId, pageRevision: params.pageRevision }, tab.url);
        else if (shot.tabId !== tabId || String(shot.pageRevision) !== String(params.pageRevision) || tab.url !== shot.url) throw new Error("stale_screenshot_revision");
      }
      let forwardedMessage = message;
      if (method === "inspect_points") {
        const shot = screenshotCache.get(params.screenshotId);
        forwardedMessage = Object.assign({}, message, { params: Object.assign({}, params, {
          points: (params.points || []).map(function (point) {
            return Object.assign({}, point, { x: point.x * shot.coordinateScale.x, y: point.y * shot.coordinateScale.y });
          })
        }) });
      }
      const result = await contentRequest(tabId, forwardedMessage);
      if (result && result.ok === false) return result;
      const payload = result && result.result ? result.result : result;
      if (method === "inspect_points" || method === "infer_repeated_collection") {
        const tab = await chrome.tabs.get(tabId);
        const shot = screenshotCache.get(params.screenshotId);
        Object.assign(payload, { tabId, screenshotId: params.screenshotId, url: tab.url, title: tab.title || "", viewport: shot.viewport, image: shot.image, pointCoordinates: "css_viewport_pixels", scroll: shot.scroll });
      }
      return envelope(message.requestId, true, payload);
    } catch (err) { return envelope(message && message.requestId, false, null, { code: err.message || "request_failed" }); }
}
chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  dispatch(message, sender).then(sendResponse);
  return true;
});

function scheduleBootstrap() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(function () { reconnectTimer = null; bootstrapWebSocket(); reconnectDelay = Math.min(reconnectDelay * 2, 30000); }, reconnectDelay);
}
function stopKeepalive() {
  if (keepaliveTimer) clearInterval(keepaliveTimer);
  keepaliveTimer = null;
}
function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(function () {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try { socket.send(JSON.stringify({ type: "keepalive", protocolVersion: PROTOCOL_VERSION, sentAt: Date.now() })); } catch (_) {}
  }, 20000);
}
function ensureWakeAlarm() {
  if (!chrome.alarms) return;
  chrome.alarms.create(BRIDGE_WAKE_ALARM, { periodInMinutes: 0.5 });
}
function bootstrapWebSocket() {
  if (socket || bootstrapPending || !chrome.runtime.sendNativeMessage) return;
  bootstrapPending = true;
  chrome.runtime.sendNativeMessage("com.shizai.agent.browser", { type: "bootstrap", protocolVersion: PROTOCOL_VERSION, extensionVersion: EXTENSION_VERSION, capabilities: CAPABILITIES }, function (reply) {
    bootstrapPending = false;
    if (chrome.runtime.lastError || !reply || !reply.wsUrl || !reply.token) { scheduleBootstrap(); return; }
    try {
      const nextSocket = new WebSocket(reply.wsUrl);
      socket = nextSocket;
      nextSocket.onopen = function () {
        if (socket !== nextSocket) { nextSocket.close(); return; }
        reconnectDelay = 250;
        nextSocket.send(JSON.stringify({ type: "hello", protocolVersion: PROTOCOL_VERSION, token: reply.token, extensionId: chrome.runtime.id, extensionVersion: EXTENSION_VERSION, capabilities: CAPABILITIES }));
        startKeepalive();
      };
      nextSocket.onmessage = function (event) {
        let message;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        if (message.type === "helloAck") return;
        dispatch(message, {}).then(function (response) {
          try { if (socket === nextSocket && nextSocket.readyState === WebSocket.OPEN) nextSocket.send(JSON.stringify(response)); } catch (_) {}
        });
      };
      nextSocket.onclose = function () {
        if (socket !== nextSocket) return;
        stopKeepalive();
        socket = null;
        scheduleBootstrap();
      };
      nextSocket.onerror = function () { try { nextSocket.close(); } catch (_) {} };
    } catch (_) { socket = null; scheduleBootstrap(); }
  });
}
chrome.runtime.onStartup.addListener(function () { ensureWakeAlarm(); bootstrapWebSocket(); });
chrome.runtime.onInstalled.addListener(function () { ensureWakeAlarm(); bootstrapWebSocket(); });
chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm && alarm.name === BRIDGE_WAKE_ALARM) bootstrapWebSocket();
});
ensureWakeAlarm();
bootstrapWebSocket();
