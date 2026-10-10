(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BrowserBridgeCore = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROTOCOL_VERSION = "1.0";
  const EXTENSION_VERSION = "0.1.11";
  const CAPABILITIES = [
    "observe_tab", "capture_viewport", "inspect_points", "validate_locator",
    "status", "list_tabs", "activate_tab", "probe_input", "infer_repeated_collection", "performAction", "open_design_tab", "close_design_tab"
  ];

  function normalizeText(value) {
    return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  }
  function isPassword(el) {
    return !!el && ((el.tagName || "").toLowerCase() === "input" &&
      String(el.type || "").toLowerCase() === "password");
  }
  function isVisible(el, win) {
    if (!el || isPassword(el)) return false;
    const view = win || (el.ownerDocument && el.ownerDocument.defaultView);
    const style = view && view.getComputedStyle ? view.getComputedStyle(el) : null;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    return !!rect && rect.width > 0 && rect.height > 0 &&
      (!style || (style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0"));
  }
  function escapeCss(value) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, function (ch) { return "\\" + ch; });
  }
  function attrSelector(el) {
    const tag = (el.tagName || "div").toLowerCase();
    const attrs = ["data-testid", "data-test", "name", "aria-label", "placeholder"];
    for (const name of attrs) {
      const value = el.getAttribute && el.getAttribute(name);
      if (value && value.length < 120) return tag + "[" + name + "=\"" + String(value).replace(/\\/g, "\\\\").replace(/\"/g, "\\\"") + "\"]";
    }
    return null;
  }
  function simpleSelector(el) {
    const tag = (el.tagName || "div").toLowerCase();
    const attr = attrSelector(el);
    if (attr) return attr;
    if (el.id && String(el.id).length < 80) return "#" + escapeCss(el.id);
    const classes = typeof el.className === "string" ? el.className.split(/\s+/).filter(Boolean).slice(0, 3) : [];
    return tag + classes.map(function (c) { return "." + escapeCss(c); }).join("");
  }
  function uniqueSelector(selector, doc) {
    try { return doc.querySelectorAll(selector).length === 1; } catch (_) { return false; }
  }
  function selectorCandidates(el, doc) {
    const documentRef = doc || el.ownerDocument;
    const candidates = [];
    const add = function (value) {
      if (!value || candidates.indexOf(value) !== -1) return;
      try { if (documentRef.querySelector(value)) candidates.push(value); } catch (_) {}
    };
    const attr = attrSelector(el);
    add(attr);
    if (el.id) add("#" + escapeCss(el.id));
    add(simpleSelector(el));
    if (el.parentElement) {
      const parent = simpleSelector(el.parentElement);
      const same = Array.from(el.parentElement.children || []).filter(function (x) { return x.tagName === el.tagName; });
      const index = same.indexOf(el) + 1;
      add(parent + " > " + (el.tagName || "div").toLowerCase() + ":nth-of-type(" + index + ")");
    }
    let node = el;
    const parts = [];
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth += 1, node = node.parentElement) {
      parts.unshift(simpleSelector(node));
      add(parts.join(" > "));
      if (uniqueSelector(parts.join(" > "), documentRef)) break;
    }
    return candidates.map(function (selector) {
      let matches = 0;
      try { matches = documentRef.querySelectorAll(selector).length; } catch (_) {}
      return { selector: selector, visible_matches: matches ? Array.from(documentRef.querySelectorAll(selector)).filter(function (x) { return isVisible(x); }).length : 0, matches: matches };
    });
  }
  function safeElementRecord(el, doc, point) {
    if (!el || isPassword(el)) return { rejected: true, reason: "password_element" };
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    const text = isPassword(el) ? "" : normalizeText(el.innerText || el.textContent || "");
    return {
      tag: (el.tagName || "").toLowerCase(), type: String(el.type || ""),
      text: text.slice(0, 500), value: isPassword(el) ? "" : (el.value == null ? "" : String(el.value).slice(0, 500)),
      attributes: { id: String(el.id || ""), name: String(el.getAttribute && el.getAttribute("name") || ""),
        placeholder: String(el.getAttribute && el.getAttribute("placeholder") || ""),
        ariaLabel: String(el.getAttribute && el.getAttribute("aria-label") || "") },
      rect: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null,
      point: point || null, visible: isVisible(el), selector_candidates: selectorCandidates(el, doc)
    };
  }
  function localLinkEvidence(el, doc) {
    let container = el;
    for (let depth = 0; container && container.nodeType === 1 && depth < 8; depth += 1, container = container.parentElement) {
      if (!container.querySelectorAll) continue;
      const links = Array.from(container.querySelectorAll("a[href]")).filter(function (link) {
        const href = String(link.href || link.getAttribute && link.getAttribute("href") || "");
        return /^https?:\/\//i.test(href) && !isPassword(link);
      });
      if (!links.length) continue;
      const repeated = selectorCandidates(container, doc).filter(function (candidate) {
        return candidate.visible_matches >= 2 && candidate.visible_matches <= 100;
      });
      if (!repeated.length) continue;
      const identifierNodes = [container];
      if (container.parentElement) identifierNodes.push(container.parentElement);
      identifierNodes.push.apply(identifierNodes, Array.from(container.querySelectorAll("*")).slice(0, 160));
      const identifiers = [];
      const seenIdentifiers = new Set();
      for (const node of identifierNodes) {
        if (!node || !node.attributes) continue;
        for (const attribute of Array.from(node.attributes)) {
          const name = String(attribute.name || "").toLowerCase();
          const value = String(attribute.value || "").trim();
          if (!value || value.length > 300 || !/^(?:data-.+(?:sku|product|item|goods|url|id)|itemprop|content)$/i.test(name)) continue;
          const key = name + "\u0000" + value;
          if (seenIdentifiers.has(key)) continue;
          const candidates = selectorCandidates(node, doc).filter(function (candidate) {
            return candidate.matches >= 2 && candidate.matches <= 100;
          }).slice(0, 4);
          if (!candidates.length) continue;
          seenIdentifiers.add(key);
          identifiers.push({ tag: String(node.tagName || "").toLowerCase(), name, value, selector_candidates: candidates });
          if (identifiers.length >= 12) break;
        }
        if (identifiers.length >= 12) break;
      }
      return {
        container_candidates: repeated.slice(0, 4),
        identifiers,
        links: links.slice(0, 6).map(function (link) {
          return {
            href: String(link.href || link.getAttribute("href") || "").slice(0, 2000),
            text: normalizeText(link.innerText || link.textContent || "").slice(0, 300),
            title: String(link.getAttribute && link.getAttribute("title") || "").slice(0, 300),
            selector_candidates: selectorCandidates(link, doc).filter(function (candidate) {
              return candidate.visible_matches >= 1 && candidate.visible_matches <= 100;
            }).slice(0, 6)
          };
        })
      };
    }
    return { container_candidates: [], identifiers: [], links: [] };
  }
  function inspectPoint(doc, point) {
    if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("point requires finite x and y");
    let el = doc.elementFromPoint(point.x, point.y);
    if (!el) return { point: point, found: false };
    if (point.preferInteractive) {
      for (let candidate = el, depth = 0; candidate && depth < 6; candidate = candidate.parentElement, depth += 1) {
        const tag = String(candidate.tagName || "").toLowerCase();
        const role = String(candidate.getAttribute && candidate.getAttribute("role") || "").toLowerCase();
        if (["a", "button", "input", "textarea", "select"].indexOf(tag) !== -1 || role === "button" || role === "link" || candidate.isContentEditable) {
          el = candidate;
          break;
        }
      }
    }
    const result = Object.assign({ point: point, found: true, point_is_match: true }, safeElementRecord(el, doc, point));
    if (point.includeLocalLinks === true) result.local_link_evidence = localLinkEvidence(el, doc);
    return result;
  }
  function validateLocator(doc, selector, point) {
    if (!selector || typeof selector !== "string") throw new Error("selector is required");
    let nodes;
    try { nodes = Array.from(doc.querySelectorAll(selector)); } catch (_) { return { selector: selector, valid: false, error: "invalid_selector" }; }
    const visible = nodes.filter(function (el) { return isVisible(el, doc.defaultView); });
    let pointMatch = null;
    if (point && doc.elementFromPoint) pointMatch = visible.indexOf(doc.elementFromPoint(point.x, point.y)) !== -1;
    return { selector: selector, valid: visible.length > 0, matches: nodes.length, visible_matches: visible.length, point_is_match: pointMatch };
  }
  function relativeSelector(root, el) {
    const parts = [];
    let node = el;
    while (node && node !== root && node.nodeType === 1) {
      const tag = (node.tagName || "div").toLowerCase();
      const attr = attrSelector(node);
      if (attr) parts.unshift(attr);
      else {
        const siblings = node.parentElement ? Array.from(node.parentElement.children).filter(function (x) { return x.tagName === node.tagName; }) : [];
        const index = siblings.indexOf(node) + 1;
        parts.unshift(tag + (siblings.length > 1 ? ":nth-of-type(" + index + ")" : ""));
      }
      node = node.parentElement;
    }
    return parts.join(" > ");
  }
  function ancestors(el) {
    const list = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) list.push(node);
    return list;
  }
  function inferRepeatedCollection(doc, fieldRecords, sampleLimit) {
    if (!Array.isArray(fieldRecords) || fieldRecords.length < 2) throw new Error("at least two field records are required");
    const fields = fieldRecords;
    if (fields.some(function (field) { return !field || !field.element || !isVisible(field.element, doc.defaultView); })) {
      throw new Error("all requested fields must resolve to visible non-password elements");
    }
    if (new Set(fields.map(function (field) { return field.element; })).size !== fields.length) {
      throw new Error("collection fields must identify distinct elements");
    }
    const common = ancestors(fields[0].element).find(function (candidate) { return fields.every(function (f) { return ancestors(f.element).indexOf(candidate) !== -1; }); });
    if (!common) throw new Error("no common container");
    const fieldCandidates = fields.map(function (field) {
      const source = String(field.selector || "").trim();
      let sourceMatches;
      try { sourceMatches = Array.from(doc.querySelectorAll(source)); } catch (_) { sourceMatches = []; }
      if (sourceMatches.indexOf(field.element) === -1) throw new Error("field source selector does not match the selected element");
      const tag = (field.element.tagName || "div").toLowerCase();
      const classes = typeof field.element.className === "string" ? field.element.className.split(/\s+/).filter(Boolean).slice(0, 3) : [];
      // Generalize from the selected node's identity, never its sibling index.
      // An inserted badge/advert can occupy the same index in another card.
      const selectors = [attrSelector(field.element), classes.length ? tag + classes.map(function (c) { return "." + escapeCss(c); }).join("") : null, source];
      return Array.from(new Set(selectors.filter(Boolean))).filter(function (selector) {
        if (!/[#.\[]/.test(selector) || /:(?:nth-|first-|last-|only-)/i.test(selector)) return false;
        try {
          const matches = Array.from(common.querySelectorAll(selector));
          return matches.length === 1 && matches[0] === field.element;
        } catch (_) { return false; }
      }).map(function (selector) {
        return { selector: selector, matches: Array.from(doc.querySelectorAll(selector)).filter(function (node) { return isVisible(node, doc.defaultView); }) };
      });
    });
    const candidates = selectorCandidates(common, doc).sort(function (left, right) { return left.visible_matches - right.visible_matches; });
    let containerSelector = "";
    let rows = [];
    let columns = [];
    let targets = [];
    for (const candidate of candidates) {
      if (candidate.visible_matches < 2) continue;
      const candidateRows = Array.from(doc.querySelectorAll(candidate.selector)).filter(function (row) { return isVisible(row, doc.defaultView); });
      if (candidateRows.indexOf(common) === -1 || candidateRows.some(function (row) {
        return ancestors(row).slice(1).some(function (parent) { return candidateRows.indexOf(parent) !== -1; });
      })) continue;
      const resolved = fieldCandidates.map(function (choices) {
        for (const choice of choices) {
          const rowTargets = candidateRows.map(function (row) { return Array.from(row.querySelectorAll(choice.selector)); });
          if (rowTargets.some(function (matches) { return matches.length !== 1 || !isVisible(matches[0], doc.defaultView); })) continue;
          const nodes = rowTargets.map(function (matches) { return matches[0]; });
          // Native fields are page-wide: reject extra matches outside these rows.
          if (choice.matches.length !== nodes.length || choice.matches.some(function (node) { return nodes.indexOf(node) === -1; })) continue;
          return { selector: choice.selector, nodes: nodes };
        }
        return null;
      });
      if (resolved.some(function (field) { return !field; })) continue;
      if (candidateRows.some(function (_, rowIndex) {
        return new Set(resolved.map(function (field) { return field.nodes[rowIndex]; })).size !== fields.length;
      })) continue;
      containerSelector = candidate.selector;
      rows = candidateRows;
      targets = resolved;
      columns = fields.map(function (field, index) {
        return {
          name: field.name,
          sourceSelector: String(field.selector),
          fieldSelector: resolved[index].selector,
          relativeSelector: resolved[index].selector
        };
      });
      break;
    }
    if (rows.length < 2) throw new Error("repeated field identity could not be verified; re-inspect missing or ambiguous fields");
    const sampleRows = rows.slice(0, sampleLimit || 5).map(function (_, rowIndex) {
      return targets.map(function (field) {
        const target = field.nodes[rowIndex];
        return normalizeText(target.innerText || target.textContent || target.value || "").slice(0, 500);
      });
    });
    return { commonSelector: containerSelector, rowCount: rows.length, columns: columns, sampleRows: sampleRows, verified: true,
      verification: { method: "field-identity-per-row", candidateRowCount: rows.length, verifiedRowCount: rows.length } };
  }
  function validateScreenshotReference(snapshot, params, tabUrl) {
    if (!snapshot) throw new Error("unknown_screenshot");
    if (snapshot.tabId !== params.tabId || String(snapshot.pageRevision) !== String(params.pageRevision)) throw new Error("stale_screenshot_revision");
    if (tabUrl != null && snapshot.url !== tabUrl) throw new Error("stale_screenshot_url");
    return true;
  }
  return { PROTOCOL_VERSION, EXTENSION_VERSION, CAPABILITIES, normalizeText, isPassword, isVisible, selectorCandidates, safeElementRecord, localLinkEvidence, inspectPoint, validateLocator, inferRepeatedCollection, validateScreenshotReference, relativeSelector, simpleSelector };
});
