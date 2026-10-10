(function () {
  "use strict";
  const core = globalThis.BrowserBridgeCore;
  let revision = 0;
  const startedAt = Date.now();
  const bump = function () { revision += 1; };
  new MutationObserver(bump).observe(document.documentElement || document, { subtree: true, childList: true, attributes: true, characterData: true });
  addEventListener("resize", bump, { passive: true });
  addEventListener("scroll", bump, { passive: true, capture: true });

  function response(requestId, ok, result, error) {
    const output = { protocolVersion: core.PROTOCOL_VERSION, requestId: requestId || null, ok: ok };
    if (ok) output.result = result || null; else output.error = error || { code: "request_failed" };
    return output;
  }
  function ensurePage() {
    if (location.protocol === "chrome:" || location.protocol === "edge:" || location.protocol === "about:" || location.protocol === "file:") throw new Error("unsupported_page");
  }
  function targetElement(params) {
    if (params.target && params.target.selector) return document.querySelector(params.target.selector);
    throw new Error("target_required");
  }
  function setNativeValue(target, value) {
    const prototypes = { INPUT: HTMLInputElement.prototype, TEXTAREA: HTMLTextAreaElement.prototype, SELECT: HTMLSelectElement.prototype };
    const descriptor = Object.getOwnPropertyDescriptor(prototypes[target.tagName], "value");
    if (descriptor && descriptor.set) descriptor.set.call(target, value);
    else target.value = value;
  }
  function performAction(params) {
    if (params.pageRevision != null && String(params.pageRevision) !== String(revision)) throw new Error("stale_page_revision");
    const target = targetElement(params);
    if (!target || !core.isVisible(target) || core.isPassword(target)) throw new Error("invalid_or_sensitive_target");
    if (params.action === "click") target.click();
    else if (params.action === "setValue") {
      if (["INPUT", "TEXTAREA", "SELECT"].indexOf(target.tagName) === -1) throw new Error("text_target_required");
      target.focus(); setNativeValue(target, String(params.value == null ? "" : params.value)); target.dispatchEvent(new Event("input", { bubbles: true })); target.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (params.action === "pressKey") {
      const key = String(params.key || "");
      if (!key || key.length > 32) throw new Error("invalid_key");
      target.focus(); target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })); target.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
    } else if (params.action === "scroll") {
      const requestedX = Number(params.deltaX || 0);
      const requestedY = Number(params.deltaY || 0);
      if (!Number.isFinite(requestedX) || !Number.isFinite(requestedY)) throw new Error("invalid_scroll_delta");
      const limitX = Math.max(1, innerWidth * 2);
      const limitY = Math.max(1, innerHeight * 2);
      const deltaX = Math.max(-limitX, Math.min(limitX, requestedX));
      const deltaY = Math.max(-limitY, Math.min(limitY, requestedY));
      let scrollingElement = target;
      while (scrollingElement && scrollingElement !== document.body && scrollingElement !== document.documentElement) {
        const style = getComputedStyle(scrollingElement);
        const canScroll = scrollingElement.scrollHeight > scrollingElement.clientHeight + 1 &&
          /auto|scroll|overlay/i.test(style.overflowY || "");
        if (canScroll) break;
        scrollingElement = scrollingElement.parentElement;
      }
      if (!scrollingElement || scrollingElement === document.body || scrollingElement === document.documentElement) {
        scrollingElement = document.scrollingElement || document.documentElement || document.body;
      }
      const before = { x: scrollingElement.scrollLeft, y: scrollingElement.scrollTop };
      if (params.intoView === true && target !== document.body && target !== document.documentElement) {
        target.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
      }
      scrollingElement.scrollLeft = before.x + deltaX;
      scrollingElement.scrollTop = before.y + deltaY;
      var scrollResult = {
        before,
        after: { x: scrollingElement.scrollLeft, y: scrollingElement.scrollTop },
        changed: scrollingElement.scrollLeft !== before.x || scrollingElement.scrollTop !== before.y,
        container: {
          tag: scrollingElement.tagName.toLowerCase(),
          className: String(scrollingElement.className || "").slice(0, 160),
          maxY: Math.max(0, scrollingElement.scrollHeight - scrollingElement.clientHeight)
        }
      };
    } else throw new Error("unsupported_action");
    bump();
    return { action: params.action, pageRevision: String(revision), scroll: scrollResult || { x: scrollX, y: scrollY }, visibleResult: { tag: target.tagName.toLowerCase(), text: core.normalizeText(target.innerText || target.textContent || "").slice(0, 200), focused: document.activeElement === target } };
  }
  function onMessage(message, sender, sendResponse) {
    try {
      ensurePage();
      const requestId = message && message.requestId;
      if (!message || message.protocolVersion !== core.PROTOCOL_VERSION) throw new Error("protocol_version_required");
      const method = message.method;
      const params = message.params || {};
      let result;
      if (method === "HELLO" || method === "hello") result = { extensionVersion: core.EXTENSION_VERSION, capabilities: core.CAPABILITIES };
      else if (method === "observe_tab") result = { url: location.href, title: document.title, pageRevision: String(revision), viewport: { width: innerWidth, height: innerHeight, devicePixelRatio: devicePixelRatio }, scroll: { x: scrollX, y: scrollY } };
      else if (method === "inspect_points") result = { pageRevision: String(revision), points: (params.points || []).map(function (p) { return core.inspectPoint(document, p); }) };
      else if (method === "validate_locator") result = core.validateLocator(document, params.selector, params.point);
      else if (method === "probe_input") {
        const check = core.validateLocator(document, params.selector, params.point);
        if (!check.valid) throw new Error("locator_not_visible");
        const el = document.querySelector(params.selector);
        if (core.isPassword(el)) throw new Error("password_element");
        el.focus();
        if (params.value != null) { setNativeValue(el, String(params.value)); el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); }
        result = { selector: params.selector, focused: document.activeElement === el, value: String(el.value || "").slice(0, 500) };
      } else if (method === "infer_repeated_collection") {
        const fields = (params.fields || []).map(function (field) {
          const selector = String(field.selector || "").trim();
          const el = selector ? document.querySelector(selector) : null;
          return { name: field.name, selector: selector, element: el };
        });
        result = core.inferRepeatedCollection(document, fields, params.sampleLimit);
      } else if (method === "performAction") result = performAction(params);
      else throw new Error("unsupported_method");
      sendResponse(response(requestId, true, result));
    } catch (err) { sendResponse(response(message && message.requestId, false, null, { code: err.message || "request_failed" })); }
    return true;
  }
  chrome.runtime.onMessage.addListener(onMessage);
  try {
    chrome.runtime.sendMessage(
      { protocolVersion: core.PROTOCOL_VERSION, requestId: "content-wake-" + Date.now(), method: "bridge_wake", params: {} },
      function () { void chrome.runtime.lastError; }
    );
  } catch (_) {}
})();
