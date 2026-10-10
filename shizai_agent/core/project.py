"""Inspection and export of native Shizai application projects."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - only used in minimal source checkouts
    padding = Cipher = algorithms = modes = None  # type: ignore[assignment]

from .agent import redact_payload
from .cloud_api import ShizaiCloudError, ShizaiDesktopAgent
from .native_ui import call_active_editor_renderer
from .paths import ShizaiPaths
from .storage import (
    iter_application_dirs,
    package_index,
    package_record_for_path,
    query_rows,
    read_json,
)
from .workflow_edit import (
    WorkflowEditError,
    check_revision,
    flow_revision,
    parameter_changes,
    read_edit_context,
)
from .workflow_references import JD_PRODUCT_LINK_REFERENCE

KNOWN_JSON_FILES = (
    "szrpa.json",
    "elements.json",
    "fileList.json",
    "file_index_700.json",
    "file_sourcemap.json",
    "globalVariable",
    "variableInfo",
    "edit-state.json",
)

RPA_FLOW_FILE = "rpa-flow.json"

_NATIVE_CLEANUP_TIMEOUT_DEFAULT = 15.0
_NATIVE_CLEANUP_TERMINAL_STATUSES = {"stopped", "cleanup_timeout", "cleanup_failed"}

# The native JS component is used only when a page exposes product cards but
# does not expose a reusable href or merchant text node through the Chrome UIA
# tree. It creates stable, in-card elements per card so DataCollectionV5 can
# consume the recovered values as ordinary columns. The nodes stay in the
# card's DOM subtree and use a one-pixel, transparent presentation: RuntimeHost
# can read them, while they do not cover or move the visible product card.
JS_LINK_ELEMENT_ID = "mcp-js-product-link"
JS_LINK_MARKER = "data-shizai-agent-product-link"
JS_MERCHANT_ELEMENT_ID = "mcp-js-product-merchant"
JS_MERCHANT_MARKER = "data-shizai-agent-product-merchant"
JS_NAME_ELEMENT_ID = "mcp-js-product-name"
JS_NAME_MARKER = "data-shizai-agent-product-name"
JS_PRICE_ELEMENT_ID = "mcp-js-product-price"
JS_PRICE_MARKER = "data-shizai-agent-product-price"
JS_SALES_ELEMENT_ID = "mcp-js-product-sales"
JS_SALES_MARKER = "data-shizai-agent-product-sales"
MCP_PAGE_SCOPE_ELEMENT_ID = "mcp-page-scope"
JS_TABLE_OUTPUT_VARIABLE = "数据采集结果"
JS_TABLE_OUTPUT_TYPE = "rpa_list_piliangshuju_v1"


@contextmanager
def _project_write_lock(paths: ShizaiPaths, project_ref: str | Path) -> Iterable[None]:
    """Serialize cross-process mutations for one flow project.

    Each CLI invocation is short-lived, so an in-memory lock cannot protect
    the atomic ``*.tmp -> target`` writeback used by native task updates.  A
    one-byte OS file lock keeps concurrent model/tool invocations from
    interleaving writes while leaving the lock outside the client project.
    """
    import errno

    lock_root = paths.data_root / "mcp-project-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    normalized = str(Path(str(project_ref)).expanduser().resolve()).casefold()
    lock_path = lock_root / f"{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EDEADLK, errno.EAGAIN, 13, 36}:
                    raise ValueError(
                        f"project_write_conflict: another CLI operation is writing {project_ref}; retry after it finishes"
                    ) from exc
                raise
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ValueError(
                        f"project_write_conflict: another CLI operation is writing {project_ref}; retry after it finishes"
                    ) from exc
                raise
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
DEFAULT_JS_LINK_SCRIPT = r"""function (input) {
    const visible = (node) => Boolean(node && (node.offsetWidth || node.offsetHeight || node.getClientRects().length));
    if (Array.from(document.querySelectorAll("input[type='password']")).some(visible)) {
        throw new Error("SHIZAI_WAITING_FOR_USER:login_required");
    }
    if (Array.from(document.querySelectorAll(
        "iframe[src*='captcha'], [class*='captcha'], [id*='captcha'], [class*='geetest']"
    )).some(visible)) {
        throw new Error("SHIZAI_WAITING_FOR_USER:page_challenge");
    }
    const linkMarker = "data-shizai-agent-product-link";
    const merchantMarker = "data-shizai-agent-product-merchant";
    // The designer declares `input` as rpa_list.  RuntimeHost therefore
    // normally passes an array even when the flow has one selector argument.
    // Keep the scalar branch for tasks created by older MCP versions.
    const inputValues = Array.isArray(input) ? input : [input];
    const requested = String(
        inputValues.find((value) => typeof value === "string" && value.trim()) || ""
    ).trim();
    // Prefer the actual repeated product-card containers.  `[data-sku]` is a
    // broad fallback on JD and also matches autocomplete/navigation nodes.
    const selectors = requested && !/\[data-sku\]/.test(requested)
        ? requested
        : ".plugin_goodsCardWrapper, .gl-item";
    let cards = Array.from(document.querySelectorAll(selectors));
    if (!cards.length) {
        cards = Array.from(document.querySelectorAll("[data-sku]"));
    }
    const textOf = (node) => String(
        node && (node.innerText || node.textContent || "")
    ).replace(/\s+/g, " ").trim();
    const normalizeMerchant = (value) => {
        const text = String(value || "").replace(/\s+/g, " ").trim();
        if (!text || text.length > 120) {
            return "";
        }
        if (/^(京东)?自营$/.test(text)) {
            return "京东自营";
        }
        return text;
    };
    const firstText = (root, selectors, predicate = () => true) => {
        for (const selector of selectors) {
            for (const node of root.querySelectorAll(selector)) {
                const value = textOf(node);
                if (value && predicate(value, node)) return value;
            }
        }
        return "";
    };
    const merchantSelectors = [
        ".p-shop", ".shop-name", ".goods-shop",
        "[class*='shop-name']", "[class*='shopName']",
        "[class*='store-name']", "[class*='storeName']",
        "[class*='shop']", "[class*='store']",
    ];
    const links = [];
    const seen = new Set();
    for (const card of cards) {
        let href = "";
        const anchor = Array.from(card.querySelectorAll("a[href]")).find((item) => {
            try {
                const url = new URL(item.href, location.href);
                return url.hostname === "item.jd.com" || url.hostname.endsWith(".item.jd.com");
            } catch (_) {
                return false;
            }
        });
        if (anchor) {
            href = new URL(anchor.href, location.href).href;
        }
        if (!href) {
            const candidates = Array.from(card.querySelectorAll("a[href], [href]"));
            const productLink = candidates.find((item) => {
                try {
                    const url = new URL(item.getAttribute("href") || item.href, location.href);
                    return url.hostname === "item.jd.com" || url.hostname.endsWith(".item.jd.com");
                } catch (_) {
                    return false;
                }
            });
            if (productLink) {
                href = new URL(productLink.getAttribute("href") || productLink.href, location.href).href;
            }
            if (!href) {
                for (const item of candidates) {
                    try {
                        const url = new URL(item.getAttribute("href") || item.href, location.href);
                        const productId = url.searchParams.get("pid") || "";
                        if (url.hostname === "chat.jd.com" && /^[0-9]{5,}$/.test(productId)) {
                            href = `https://item.jd.com/${productId}.html`;
                            break;
                        }
                    } catch (_) {
                        // Ignore malformed card links and continue to SKU attributes.
                    }
                }
            }
        }
        if (!href) {
            const nodes = [card, ...Array.from(card.querySelectorAll("*"))];
            for (const node of nodes) {
                const sku = ["data-sku", "data-spu", "data-skuid", "data-id", "skuId"]
                    .map((attribute) => node.getAttribute && node.getAttribute(attribute))
                    .find((value) => /^[0-9]{5,}$/.test(String(value || "").trim()));
                if (sku) {
                    href = `https://item.jd.com/${String(sku).trim()}.html`;
                    break;
                }
            }
        }
        if (!href) {
            continue;
        }
        let merchant = "";
        for (const selector of merchantSelectors) {
            const candidates = Array.from(card.querySelectorAll(selector));
            for (const candidate of candidates) {
                merchant = normalizeMerchant(textOf(candidate));
                if (merchant) {
                    break;
                }
            }
            if (merchant) {
                break;
            }
        }
        if (!merchant) {
            const badge = Array.from(card.querySelectorAll(
                "img, [aria-label], [title], [name], [data-name]"
            )).find((node) => /自营/.test(
                [node.getAttribute("alt"), node.getAttribute("title"),
                 node.getAttribute("aria-label"), node.getAttribute("name"),
                 node.getAttribute("data-name")].filter(Boolean).join(" ")
            ));
            if (badge || /(?:京东)?自营/.test(textOf(card))) {
                merchant = "京东自营";
            }
        }
        const productName = firstText(card, [
            "[class*='goods_title']", "[class*='goods-title']",
            "[class*='product-title']", "[class*='productTitle']",
            "[class*='newStyle'] span", "[class*='newStyle']",
            "h3", "h4", "a[title]",
        ], (value) => value.length >= 2
            && !/^[0-9,.]+$/.test(value)
            && !/^(?:￥|¥)?\s*[0-9]{1,7}(?:\.[0-9]{1,2})?\s*(?:元)?$/.test(value)
            && !/(?:已售|评价|评论|人付款)/.test(value));
        const price = firstText(card, [
            "[class*='price']", "[class*='Price']",
            "[class*='newStyle'] span", "[class*='newStyle']",
        ], (value) => /^(?:￥|¥)?\s*[0-9]{1,7}(?:\.[0-9]{1,2})?\s*(?:元)?$/.test(value));
        let sales = firstText(card, [
            "[class*='volume']", "[class*='sales']", "[class*='comment']",
            "[class*='text-list']", "[class*='goods_volume']",
        ], (value) => /[0-9]/.test(value) && value.length < 80);
        if (!sales) {
            const candidates = Array.from(card.querySelectorAll("span,em,div"));
            sales = textOf(candidates.find((node) => node.children.length === 0
                && /(?:已售|评价|评论|人付款)/.test(textOf(node)))) || "";
        }
        const outputStyle = [
            "position:absolute", "left:0", "top:0", "width:1px", "height:1px",
            "overflow:hidden", "font-size:1px", "line-height:1px", "color:transparent",
            "background:transparent", "pointer-events:none", "z-index:-1",
        ].join(";");
        let merchantOutput = card.querySelector(`span[${merchantMarker}]`);
        if (!merchantOutput) {
            merchantOutput = document.createElement("span");
            merchantOutput.setAttribute(merchantMarker, "1");
            merchantOutput.className = merchantMarker;
            merchantOutput.style.cssText = outputStyle;
            card.appendChild(merchantOutput);
        }
        merchantOutput.textContent = merchant;

        const markerFields = [
            ["data-shizai-agent-product-name", productName],
            ["data-shizai-agent-product-price", price],
            ["data-shizai-agent-product-sales", sales],
        ];
        for (const [marker, value] of markerFields) {
            let output = card.querySelector(`span[${marker}]`);
            if (!output) {
                output = document.createElement("span");
                output.setAttribute(marker, "1");
                output.className = marker;
                output.style.cssText = outputStyle;
                card.appendChild(output);
            }
            output.textContent = value || "";
        }

        let linkOutput = card.querySelector(`a[${linkMarker}]`);
        if (!linkOutput) {
            linkOutput = document.createElement("a");
            linkOutput.setAttribute(linkMarker, "1");
            linkOutput.className = linkMarker;
            linkOutput.style.cssText = outputStyle;
            card.appendChild(linkOutput);
        }
        linkOutput.href = href;
        linkOutput.textContent = href;
        if (!seen.has(href)) {
            seen.add(href);
            links.push(href);
        }
    }
    return links;
}"""

# RuntimeHost 7.3.7 can execute page-scoped JavaScript reliably, but its
# DataCollectionV5 tree snapshot may not include DOM nodes created by that
# JavaScript. This variant keeps extraction inside the native JS component and
# returns the table directly to Excel_Cs_WriteRange, avoiding the stale tree.
JS_TABLE_SCRIPT_TEMPLATE = r"""function (input) {
    const currentUrl = String(location && location.href || "").toLowerCase();
    const currentTitle = String(document && document.title || "").trim();
    const visibleText = String(document && document.body && document.body.innerText || "")
        .replace(/\s+/g, " ")
        .slice(0, 4000);
    const hasPasswordField = Boolean(document.querySelector("input[type='password']"));
    const isLoginPage = /(?:^|\.)passport\.jd\.com$/.test(String(location.hostname || "").toLowerCase())
        || /(?:^|\/)(?:login|passport)(?:[\/.?]|$)/.test(String(location.pathname || "").toLowerCase())
        || (hasPasswordField && /(?:登录|账号|密码)/.test(`${currentTitle} ${visibleText}`));
    if (isLoginPage) {
        throw new Error("SHIZAI_WAITING_FOR_USER:login_required");
    }
    const hasChallengeWidget = Boolean(document.querySelector(
        "[class*='captcha'], [id*='captcha'], [class*='verify'], [id*='verify'], iframe[src*='captcha'], iframe[src*='verify']"
    ));
    const isChallengePage = /(?:captcha|verify|challenge|risk)/.test(currentUrl)
        || (hasChallengeWidget && /(?:验证|安全校验|滑块|拼图|机器人)/.test(`${currentTitle} ${visibleText}`));
    if (isChallengePage) {
        throw new Error("SHIZAI_WAITING_FOR_USER:page_challenge");
    }
    // JD changes generated class suffixes frequently. The stable card token
    // is still present in the live page, so collect cards first and resolve
    // each column inside that card. A missing link must not discard a row.
    const inputValues = Array.isArray(input) ? input : [input];
    const requestedColumns = __SHIZAI_REQUIRED_COLUMNS__;
    const maxRows = __SHIZAI_MAX_ROWS__;
    const requested = String(
        inputValues.find((value) => typeof value === "string" && value.trim()) || ""
    ).trim();
    const defaultCardSelector = [
        ".plugin_goodsCardWrapper",
        "[class~='plugin_goodsCardWrapper']",
        ".gl-item",
    ].join(", ");
    const selectors = requested && !/\[data-sku\]/.test(requested)
        ? requested
        : defaultCardSelector;
    const unique = (items) => {
        const seen = new Set();
        return items.filter((item) => {
            if (seen.has(item)) return false;
            seen.add(item);
            return true;
        });
    };
    let cards = [];
    try {
        cards = Array.from(document.querySelectorAll(selectors));
    } catch (_) {
        cards = [];
    }
    if (!cards.length) {
        try {
            cards = Array.from(document.querySelectorAll(
                "[class*='goodsCardWrapper'], [class*='goods-card'], [data-sku], [data-spu]"
            ));
        } catch (_) {
            cards = [];
        }
    }
    cards = unique(cards).filter((card) => card && card.nodeType === 1);

    const textOf = (node) => String(
        node && (node.innerText || node.textContent || "")
    ).replace(/\s+/g, " ").trim();
    const leaves = (root) => Array.from(root.querySelectorAll("*"))
        .filter((node) => node.children.length === 0)
        .map((node) => ({node, text: textOf(node)}))
        .filter((item) => item.text);
    const firstText = (root, selectorList, predicate = () => true) => {
        for (const selector of selectorList) {
            let nodes = [];
            try {
                nodes = Array.from(root.querySelectorAll(selector));
            } catch (_) {
                nodes = [];
            }
            for (const node of nodes) {
                const value = textOf(node);
                if (value && predicate(value, node)) return value;
            }
        }
        return "";
    };
    const firstLeaf = (root, predicate) => {
        for (const item of leaves(root)) {
            if (predicate(item.text, item.node)) return item.text;
        }
        return "";
    };
    const extractSales = (value) => {
        const text = String(value || "").replace(/\s+/g, " ").trim();
        const match = text.match(
            /(?:已售|销量|成交)\s*[0-9]+(?:\.[0-9]+)?\s*(?:万|千|百|w|W|k|K)?\s*\+?|[0-9]+(?:\.[0-9]+)?\s*(?:万|千|百|w|W|k|K)?\s*人付款/
        );
        return match ? match[0].replace(/\s+/g, "") : "";
    };
    const normalizePrice = (value) => String(value || "").replace(/\s+/g, "").trim();
    const merchantSelectors = [
        ".p-shop", ".shop-name", ".goods-shop", ".merchant-name",
        "[class*='shop-name']", "[class*='shopName']",
        "[class*='store-name']", "[class*='storeName']",
        "[class*='merchant']", "[class*='shop']", "[class*='store']",
    ];
    const normalizeMerchant = (value) => {
        const text = String(value || "").replace(/\s+/g, " ").trim();
        // A seller id is useful as link metadata, but is not a merchant name.
        if (!text || /^\d+$/.test(text)) return "";
        return text.slice(0, 120);
    };
    const toUrl = (value) => {
        try {
            const url = new URL(String(value || ""), location.href);
            if (!/^https?:$/.test(url.protocol)) return "";
            return url.href;
        } catch (_) {
            return "";
        }
    };
    const linkScore = (url) => {
        try {
            const parsed = new URL(url);
            const host = parsed.hostname.toLowerCase();
            const path = parsed.pathname.toLowerCase();
            if (host === "chat.jd.com" || host.endsWith(".chat.jd.com")) return -1;
            if (host === "item.jd.com" || host.endsWith(".item.jd.com")) return 100;
            if (host === "3.cn" || host.endsWith(".3.cn")) return 95;
            if (/(?:^|\/)item(?:\/|$)/.test(path) || /product/.test(path)) return 85;
            if (host.endsWith("jd.com") && !/(?:search|help|passport|login|cart|order|mall)/.test(path)) return 30;
        } catch (_) {
            return -1;
        }
        return -1;
    };
    const decodeSeller = (value) => {
        let decoded = String(value || "").trim();
        if (!decoded) return "";
        for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
                const next = decodeURIComponent(decoded);
                if (next === decoded) break;
                decoded = next;
            } catch (_) {
                break;
            }
        }
        return normalizeMerchant(decoded);
    };
    const sellerFromUrl = (href) => {
        try {
            return decodeSeller(new URL(href).searchParams.get("seller") || "");
        } catch (_) {
            return "";
        }
    };
    const cardLink = (card) => {
        const candidates = [];
        const derivedProductHrefs = [];
        let sellerHint = "";
        for (const node of card.querySelectorAll("a[href], [href]")) {
            const href = toUrl(node.getAttribute("href") || node.href);
            if (!href) continue;
            sellerHint = sellerHint || sellerFromUrl(href);
            if (linkScore(href) >= 30) {
                candidates.push(href);
                continue;
            }
            // Search cards sometimes expose a customer-service URL with the
            // product id in pid. Convert that id to the actual item page and
            // never export the chat endpoint as 商品链接.
            try {
                const parsed = new URL(href);
                const productId = parsed.searchParams.get("pid") || "";
                if ((parsed.hostname || "").toLowerCase() === "chat.jd.com"
                    && /^[0-9]{5,}$/.test(productId)) {
                    derivedProductHrefs.push(`https://item.jd.com/${productId}.html`);
                }
            } catch (_) {
                // Ignore malformed hrefs and continue with data attributes.
            }
        }
        candidates.sort((left, right) => linkScore(right) - linkScore(left));
        if (candidates.length && linkScore(candidates[0]) >= 30) {
            return {href: candidates[0], seller: sellerHint || sellerFromUrl(candidates[0])};
        }
        if (derivedProductHrefs.length) {
            return {href: derivedProductHrefs[0], seller: sellerHint};
        }
        const nodes = [card, ...Array.from(card.querySelectorAll("*"))];
        for (const node of nodes) {
            for (const attribute of ["data-sku", "data-spu", "data-skuid", "data-id", "skuId"]) {
                const value = String(node.getAttribute && node.getAttribute(attribute) || "").trim();
                if (/^[0-9]{5,}$/.test(value)) {
                    return {href: `https://item.jd.com/${value}.html`, seller: sellerHint};
                }
            }
        }
        return {href: "", seller: sellerHint};
    };
    const result = [requestedColumns];
    const seenRows = new Set();
    cards.forEach((card, cardIndex) => {
        const name = firstText(card, [
            "[class*='goods_title']", "[class*='goods-title']",
            "[class*='product-title']", "[class*='productTitle']",
            "h3", "h4", "a[title]",
        ], (value) => value.length >= 2 && !/^[0-9,.]+$/.test(value));
        // JD often renders review/payment counters inside nodes whose class
        // also contains "price". Keep those out of the price column and
        // preserve a meaningful value for reservation-only cards.
        const priceNoise = /(?:评价|评论|看过|种草|人付款|已售|销量)/;
        const extractPrice = (value) => {
            const text = String(value || "").replace(/\s+/g, " ").trim();
            if (!text || text.length >= 80 || priceNoise.test(text)) return "";
            const reservationAmount = text.match(
                /(?:￥|¥)?\s*[0-9]{1,7}(?:\.[0-9]{1,2})?\s*元/
            );
            if (reservationAmount && /(?:预约|预售|定金)/.test(text)) {
                return normalizePrice(`${reservationAmount[0]}预约`);
            }
            const currencyAmount = text.match(
                /(?:￥|¥)\s*[0-9]{1,7}(?:\.[0-9]{1,2})?/
            );
            if (currencyAmount) return normalizePrice(currencyAmount[0]);
            if (/^[0-9]{1,7}(?:\.[0-9]{1,2})?$/.test(text)) return normalizePrice(text);
            if (/^[0-9]{1,7}(?:\.[0-9]{1,2})?\s*元$/.test(text)) {
                return normalizePrice(text);
            }
            return "";
        };
        const priceCandidate = firstText(card, [
            "[class*='price']", "[class*='Price']", "[class*='newStyle']",
        ], (value) => Boolean(extractPrice(value)));
        const price = extractPrice(priceCandidate)
            || extractPrice(firstLeaf(card, (value) => Boolean(extractPrice(value))))
            || extractPrice(textOf(card));
        const salesText = firstText(card, [
            "[class*='volume']", "[class*='sales']", "[class*='sale']", "[class*='comment']",
        ], (value) => Boolean(extractSales(value)) && value.length < 80);
        let sales = extractSales(salesText);
        if (!sales) {
            sales = extractSales(firstLeaf(card, (value) => Boolean(extractSales(value)) && value.length < 80));
        }
        // Search cards frequently expose reviews or repurchase counts instead
        // of a literal sales counter. Keep the record usable and make the
        // missing source value explicit rather than dropping the whole row.
        if (!sales) sales = "暂无销量";
        let merchant = normalizeMerchant(firstText(card, merchantSelectors, (value) => {
            return value.length <= 120 && !/(?:已售|评价|评论|\d{3,})/.test(value);
        }));
        if (!merchant) {
            merchant = normalizeMerchant(firstLeaf(card, (value) => {
                return /(?:京东)?自营|旗舰店|专卖店|专营店|官方店/.test(value)
                    && value.length <= 80;
            }));
        }
        if (!merchant) {
            const attrNode = [card, ...Array.from(card.querySelectorAll("*"))]
                .find((node) => ["data-shop", "data-store", "data-merchant"].some((attribute) => node.getAttribute && node.getAttribute(attribute)));
            if (attrNode) {
                merchant = normalizeMerchant(
                    attrNode.getAttribute("data-shop") || attrNode.getAttribute("data-store") || attrNode.getAttribute("data-merchant")
                );
            }
        }
        const linkInfo = cardLink(card);
        const href = linkInfo.href;
        if (!merchant) merchant = linkInfo.seller;
        const valuesByColumn = {
            "商品名称": name,
            "商品标题": name,
            "商家名称": merchant,
            "价格": price,
            "商品价格": price,
            "销量": sales,
            "商品销量": sales,
            "商品链接": href,
        };
        const row = requestedColumns.map((column) => valuesByColumn[column] || "");
        // A reported row must satisfy the requested output contract. Skipping
        // incomplete cards is preferable to returning a visually plausible
        // but unusable record.
        if (row.some((value) => !String(value || "").trim())) return;
        if (maxRows > 0 && result.length - 1 >= maxRows) return;
        // Use a stable composite key when the site does not expose a product URL.
        const rowKey = href || `${name}|${price}|${sales}|${cardIndex}`;
        if (seenRows.has(rowKey)) return;
        seenRows.add(rowKey);
        result.push(row);
    });
    return result;
}"""

# The CLI JS-table path can opt into model-requested pagination.  The script
# first uses a live next-page control, then falls back to a URL-level page
# transition for React search pages that do not expose the control to the
# runtime DOM.  The generated Python runner waits and rebinds the active tab.
JS_NEXT_PAGE_SCRIPT = r"""function (input) {
    const selectors = [
        "a.pn-next:not(.disabled)",
        "button.pn-next:not(.disabled)",
        ".pn-next:not(.disabled)",
        "[class*='pagination_next']:not([disabled])",
        "[class*='pagination-next']:not([disabled])",
        "[class*='paginationNext']:not([disabled])",
        "[aria-label*='下一页']:not([disabled])",
        "[title*='下一页']:not([disabled])",
    ];
    let next = null;
    for (const selector of selectors) {
        try {
            next = document.querySelector(selector);
        } catch (_) {
            next = null;
        }
        if (next) break;
    }
    if (next) {
        const className = String(next.className || "").toLowerCase();
        const ariaDisabled = String(next.getAttribute("aria-disabled") || "").toLowerCase();
        if (!next.disabled && ariaDisabled !== "true" && !/disabled|last/.test(className)) {
            try {
                next.click();
                return true;
            } catch (_) {
                // Continue to URL navigation when the React handler is not
                // reachable through the runtime's DOM bridge.
            }
        }
    }

    // JD's current React search page can render 80+ cards without exposing
    // its pagination control in the native accessibility tree.  Keep the
    // transition same-origin and preserve the search query while advancing
    // the explicit page parameter used by the page's own data loader.
    try {
        const current = new URL(location.href);
        const host = String(current.hostname || "").toLowerCase();
        const path = String(current.pathname || "").toLowerCase();
        if (host === "search.jd.com" && /search/.test(path)) {
            const page = Math.max(1, parseInt(current.searchParams.get("page") || "1", 10) || 1);
            const nextPage = page + 1;
            current.searchParams.set("page", String(nextPage));
            current.searchParams.set("s", String((nextPage - 1) * 30));
            current.searchParams.set("enc", current.searchParams.get("enc") || "utf-8");
            location.assign(current.href);
            return true;
        }
    } catch (_) {
        // Fall through to a bounded lazy-load attempt.
    }

    // Some result pages use infinite scroll instead of numbered pages.  A
    // bounded outer loop prevents this fallback from becoming unbounded when
    // the page has no more content.
    try {
        const before = Number(document.documentElement.scrollHeight || 0);
        window.scrollTo(0, before);
        return before > Number(window.innerHeight || 0);
    } catch (_) {
        return false;
    }
}"""


def _publish_rpa_ui(
    phase: str,
    activity: str,
    message: str,
    *,
    animation: str = "workflow_progress",
    overlay_state: str = "inactive",
    **extra: Any,
) -> dict[str, Any]:
    """Publish workflow progress through the same status channel as capture."""
    # Keep the project data layer import-light; the bridge owns the process
    # level status consumed by the MCP UI-status tool.
    from .extension_bridge import set_rpa_ui_feedback

    return set_rpa_ui_feedback(
        phase,
        activity,
        message,
        animation=animation,
        overlay_state=overlay_state,
        **extra,
    )


# These are the native component types observed in the installed designer's
# browser task template. Their argument details remain template-driven; this
# catalog only describes the ordering and binding decisions the client model
# must make before materialization.
RPA_COMPONENT_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "component": "WebBrowser_CreateV3",
        "designer_type": "WebBrowser.CreateV3",
        "order": 1,
        "designer_label": "打开网页",
        "purpose": "打开 Google Chrome 并导航到目标 URL",
        "requires_elements": False,
        "inputs": ["url", "type=chrome", "openNewTab"],
        "plan_input_mode": "none",
        "outputs": ["网页对象"],
    },
    {
        "component": "Element_SetValue_V6",
        "designer_type": "Element.SetValue_V6",
        "order": 2,
        "designer_label": "设置元素值",
        "purpose": "把计划中的文本写入已捕获的网页输入控件",
        "requires_elements": True,
        "inputs": ["uiSelector", "designated_obj", "text", "isEnter"],
        "plan_input_mode": "variables",
        "outputs": ["校验结果"],
    },
    {
        "component": "Keyboard_PressNew",
        "designer_type": "Keyboard.PressNew",
        "order": 3,
        "designer_label": "键盘模拟按键",
        "purpose": "向当前活动网页发送真实键盘按键，例如回车提交搜索",
        "requires_elements": False,
        "inputs": ["key=13", "type=press", "auxiliary_key", "input_us"],
        "plan_input_mode": "none",
        "outputs": [],
    },
    {
        "component": "WebBrowser_DataCollectionV5",
        "designer_type": "WebBrowser.DataCollectionV5",
        "order": 5,
        "designer_label": "网页数据采集",
        "purpose": "按已捕获的列表和字段元素采集结构化表格数据",
        "requires_elements": True,
        "inputs": ["config.collectionConfig", "designated_obj", "toExcel", "filename_suffix"],
        "plan_input_mode": "variables",
        "outputs": ["数据采集结果", "Excel路径", "监控空列数据"],
    },
)

# Keep the default output contract identical across native and staged demos.
# Callers can still pass a custom list when a different table is intentional.
DEFAULT_RPA_COLUMNS = ["商品名称", "商家名称", "价格", "销量", "商品链接"]

# This is a model-facing recommendation, not a workflow validator.  Keep it
# in the shared project core so both generation context and the legacy
# component surface expose the same choice policy.
RPA_BUSINESS_RULES = (
    {
        "id": "native-component-preference",
        "severity": "advisory",
        "enforcement": "warning_only",
        "when": "实时组件目录中存在已安装且可物化、并覆盖当前原子操作的原生组件时",
        "recommendation": "优先评估并使用能覆盖目标的原生组件；结构化网页采集优先评估 WebBrowser_DataCollectionV5。",
        "must": [
            "必须先比较原生组件的 capabilities、inputs、outputs 与当前目标和页面证据是否匹配。",
            "结构化网页采集必须先评估原生采集并验证页面字段、XPath、分页和父子记录关系；不能仅凭 extract+output 标签断言适用。",
            "优先拆分页面准备、原生采集、数据处理/写入；确需脚本替代时，在步骤 native_alternative_reason 中记录具体限制和验证证据。嵌套循环写图未支持时必须报告接口缺口。",
            "如果最终选择 JS、下载、循环或其它组合，必须说明原生组件不适用、能力不足或页面证据不匹配的原因，并验证所选方案的输出。",
            "页面跳转导致 page_identity_changed 时，必须保留同一个 project_ref，重新扫描当前页面，并仅在明确重绑定时传 allow_rebind=true。",
        ],
        "must_not": [
            "禁止仅因为代码方案更容易生成就跳过可以覆盖目标的原生组件。",
            "禁止把本建议解释为必须使用 WebBrowser_DataCollectionV5、固定组件顺序或固定分页方式；不适合时不要硬套。",
            "禁止因未选择推荐的原生组件而硬失败；只应根据真实的组件、参数、引用、元素或运行安全问题阻止流程。",
            "禁止把页面变化后的旧 selector 当作当前页面证据，也禁止默认覆盖已有绑定。",
        ],
        "evidence": [
            "shizai_rpa_component_catalog 或 shizai_rpa_components 返回的 availability、materializable、capabilities、inputs 和 outputs。",
            "模型最终选择的方案，以及选择替代方案时记录的具体原因。",
            "所选步骤的 step_validation、运行期验证和最终输出契约证据。",
            "runtime_preflight 的 reason、当前页面身份，以及重绑定调用中的 allow_rebind=true。",
        ],
        "on_failure": "如果原生候选静态或运行验证失败，保留同一个 project_ref，依据具体错误重新评估合法的 JS、循环、下载或其它组件方案，并重新验证。",
    },
)


def _normalize_max_rows(value: Any, *, default: int = 0) -> int:
    """Normalize an optional output-row contract to a bounded integer."""
    if value is None or value == "":
        return default
    try:
        rows = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_rows must be an integer between 1 and 1000") from exc
    if rows == 0 and default == 0:
        return 0
    if rows < 1 or rows > 1000:
        raise ValueError("max_rows must be an integer between 1 and 1000")
    return rows


def _resolve_plan_semantic_value(value: Any, output_contract: dict[str, Any]) -> Any:
    """Resolve stable output-contract references used by client-model plans."""
    if not isinstance(value, str) or not value.startswith("$output_contract."):
        return value
    current: Any = output_contract
    for key in value[len("$output_contract."):].split("."):
        if not isinstance(current, dict) or key not in current:
            return value
        current = current[key]
    return current


def _js_table_script(required_columns: Iterable[str] | None = None, max_rows: Any = None) -> str:
    """Render the page-table extractor for the exact output contract."""
    columns = [str(item).strip() for item in required_columns or DEFAULT_RPA_COLUMNS if str(item).strip()]
    if not columns:
        columns = list(DEFAULT_RPA_COLUMNS)
    row_limit = _normalize_max_rows(max_rows, default=0)
    return JS_TABLE_SCRIPT_TEMPLATE.replace(
        "__SHIZAI_REQUIRED_COLUMNS__",
        json.dumps(columns, ensure_ascii=False),
    ).replace("__SHIZAI_MAX_ROWS__", str(row_limit))


DEFAULT_JS_TABLE_SCRIPT = _js_table_script()

_RPA_RESULT_FIELD_ALIASES = {
    "商品编号": "product_id",
    "商品ID": "product_id",
    "货号": "product_id",
    "SKU": "product_id",
    "商品名称": "product_name",
    "商品名": "product_name",
    "商品标题": "product_name",
    "商家名称": "merchant",
    "商家": "merchant",
    "店铺": "merchant",
    "价格": "price",
    "销量": "sales",
    "销量原文": "sales",
    "热度原文": "sales",
    "指标口径": "sales_metric",
    "销量口径": "sales_metric",
    "统计口径": "sales_metric",
    "商品链接": "link",
    "链接": "link",
}
_RPA_RESULT_FIELD_ORDER = (
    "product_id",
    "product_name",
    "merchant",
    "price",
    "sales",
    "sales_metric",
    "link",
)
_RPA_RESULT_FIELDS = set(_RPA_RESULT_FIELD_ORDER)


def _canonical_result_field(value: Any) -> str:
    """Normalize a captured result-field label for persisted flow state."""
    text = str(value or "").strip()
    return _RPA_RESULT_FIELD_ALIASES.get(text, text)


def _result_capture_requirements(
    required_columns: Any,
    *,
    use_js_link: bool,
    use_js_table: bool = False,
) -> set[str]:
    """Return the result fields that must exist before collection can be added."""
    if use_js_table:
        # The page-scoped JS component returns the complete table directly;
        # no generated DOM marker or result-page element binding is needed.
        return set()
    columns = [str(item).strip() for item in required_columns or [] if str(item).strip()]
    required = {
        canonical
        for canonical in (_canonical_result_field(item) for item in columns)
        if canonical in _RPA_RESULT_FIELDS
    }
    if use_js_link:
        # The recovery JS step creates merchant and product-link nodes in the
        # same card scope, so native capture only supplies requested UI fields.
        required.difference_update({"merchant", "link"})
    return required


def _normalize_generated_collection_bindings(
    element_ids: list[str],
    columns: list[str],
    element_map: dict[str, Any],
) -> list[str]:
    """Align native captures and JS-generated fields with their output columns.

    A JS-link capture intentionally returns only the native product name,
    price, and sales ids. The generated merchant/link ids must be inserted at
    their column positions rather than replacing ids by index.
    """
    if not isinstance(element_map, dict):
        return list(element_ids)
    if len(element_ids) == len(columns) and all(element_id in element_map for element_id in element_ids):
        # A complete explicit binding is authoritative. This is how the browser
        # bridge replaces an older JS-marker collection during flow editing.
        return list(element_ids)
    generated_by_field = {
        "product_name": JS_NAME_ELEMENT_ID,
        "merchant": JS_MERCHANT_ELEMENT_ID,
        "price": JS_PRICE_ELEMENT_ID,
        "sales": JS_SALES_ELEMENT_ID,
        "link": JS_LINK_ELEMENT_ID,
    }
    normalized: list[str] = []
    native_index = 0
    for header in columns:
        generated_id = generated_by_field.get(_canonical_result_field(header))
        if generated_id and generated_id in element_map:
            normalized.append(generated_id)
            continue
        if native_index < len(element_ids):
            normalized.append(str(element_ids[native_index]).strip())
            native_index += 1
    # Preserve explicitly supplied ids only for still-unmapped columns.  Once
    # JS markers cover the declared columns, appending the original captures
    # would create duplicate bindings and make RuntimeHost reject the table.
    normalized.extend(
        str(item).strip()
        for item in element_ids[native_index:][: max(0, len(columns) - len(normalized))]
        if str(item).strip()
    )
    return normalized


def _output_names_match(expected: list[str], actual: list[str]) -> bool:
    """Accept designer collision suffixes while preserving output order and role."""

    if len(expected) != len(actual):
        return False
    return all(
        observed == canonical
        or bool(re.fullmatch(re.escape(canonical) + r"_[1-9][0-9]*", observed))
        for canonical, observed in zip(expected, actual)
    )


RPA_EXCEL_COMPONENT_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "component": "Excel_Cs_NewV4",
        "designer_type": "Excel.Cs_NewV4",
        "order": 5,
        "designer_label": "新建Excel",
        "purpose": "创建用于保存采集结果的 Excel 文件",
        "requires_elements": False,
        "inputs": ["path", "name", "software", "show"],
        "outputs": ["Excel文件"],
    },
    {
        "component": "Excel_Cs_WriteRange",
        "designer_type": "Excel.Cs_WriteRange",
        "order": 6,
        "designer_label": "写入区域",
        "purpose": "把数据采集结果写入 Excel 并保存",
        "requires_elements": False,
        "inputs": ["workbook", "sheet", "row", "column", "contents", "save"],
        "outputs": [],
    },
)

RPA_EXCEL_SEQUENCE = [item["component"] for item in RPA_EXCEL_COMPONENT_CONTRACTS]
RPA_RECOMMENDED_SEQUENCE = [item["component"] for item in RPA_COMPONENT_CONTRACTS]
RPA_OPTIONAL_COMPONENT_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "component": "Keyboard_Send_V1",
        "designer_type": "Keyboard.Send_V1",
        "order": 3,
        "designer_label": "发送键盘输入",
        "purpose": "兼容已有流程的文本发送组件；新流程使用 Keyboard_PressNew",
        "requires_elements": False,
        "inputs": ["words", "fuctype", "interval"],
        "plan_input_mode": "none",
        "outputs": [],
    },
    {
        "component": "Basic_Delay_V1",
        "designer_type": "Basic.Delay_V1",
        "order": 4,
        "designer_label": "等待",
        "purpose": "等待前一步触发的页面状态或内容完成更新",
        "requires_elements": False,
        "inputs": ["second"],
        "plan_input_mode": "none",
        "outputs": [],
    },
    {
        "component": "WebBrowser_GetTAB_V1",
        "designer_type": "WebBrowser.GetTAB_V1",
        "order": 5,
        "designer_label": "获取网页 Tab 对象",
        "purpose": "页面导航后重新获取当前 Chrome 标签页，刷新后续组件使用的网页对象",
        "requires_elements": False,
        "inputs": ["var_name", "type=chrome", "match_type=2", "title", "url", "isActive=1"],
        "plan_input_mode": "none",
        "outputs": ["网页对象"],
    },
    {
        "component": "WebBrowser_RunJsInIeBySelector_v3",
        "designer_type": "WebBrowser.RunJsInIeBySelector_v3",
        "order": 5,
        "designer_label": "执行网页脚本",
        "purpose": "执行客户端模型为当前页面和任务显式提供的 JavaScript",
        # The link-recovery script scans the current page.  A captured card
        # element is still useful as context for the generated collection
        # element, but the runtime selector is intentionally page-scoped so a
        # result-page navigation cannot fail before the script runs.
        "requires_elements": False,
        "inputs": ["var_name", "web_page", "uiSelector", "jsCode", "input", "executionEnvironment"],
        "plan_input_mode": "variables",
        "outputs": ["商品链接"],
    },
    {
        "component": "WebBrowser_CloseWeb_V2",
        "designer_type": "WebBrowser.CloseWeb_V2",
        "order": 99,
        "designer_label": "关闭网页",
        "purpose": "流程结束时关闭本流程创建的网页对象，避免运行产生大量遗留标签页",
        "requires_elements": False,
        "inputs": ["colse_type=select", "web", "is_close_alert"],
        "plan_input_mode": "variables",
        "outputs": ["操作结果"],
    },
)
RPA_ALL_COMPONENT_CONTRACTS = RPA_COMPONENT_CONTRACTS + RPA_OPTIONAL_COMPONENT_CONTRACTS + RPA_EXCEL_COMPONENT_CONTRACTS
_NATIVE_RPA_COMPONENTS = {item["component"] for item in RPA_ALL_COMPONENT_CONTRACTS}
_NATIVE_ELEMENT_RE = re.compile(r"SHIZAI_ELEMENT_DICT\[\s*[\"']([^\"']+)[\"']\s*\]")
_NATIVE_MAIN_SECRET = "COMMANDER_JOB_INPUT_PARAM_PASSWORD"


def _rpa_submit_component(flow: dict[str, Any]) -> str:
    """Resolve the submit component while keeping old flows compatible."""
    explicit = str(flow.get("submit_component") or flow.get("mcpSubmitComponent") or "").strip()
    if explicit in {"Keyboard_PressNew", "Keyboard_Send_V1"}:
        return explicit
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    if len(steps) >= 3 and isinstance(steps[2], dict):
        observed = str(steps[2].get("component") or "").strip()
        if observed in {"Keyboard_PressNew", "Keyboard_Send_V1"}:
            return observed
    return "Keyboard_PressNew"


def _rpa_model_decides(flow: dict[str, Any] | None) -> bool:
    """Return whether this flow is governed by a client-model plan.

    A model-owned plan describes the selected graph, but it must not turn the
    compatibility sequence below into a hidden recipe.  The flag is persisted
    in native metadata so partial CLI flows and complete MCP plans can both
    use the same component APIs.
    """
    if not isinstance(flow, dict):
        return False
    value = flow.get("planning_policy") or flow.get("validation_mode") or flow.get("mcpPlanningPolicy")
    return str(value or "").strip().casefold() in {"flexible", "advisory", "model_decides"}


_JS_RESULT_MODE_ALIASES: dict[str, str] = {
    # These descriptive names were exposed by older generation-context
    # payloads. Keep accepting them at the model boundary while persisting
    # the compact mode understood by native-plan validation/runtime code.
    "js_table_direct_to_excel": "table",
    "js_table": "table",
    "table_direct_to_excel": "table",
    "js_generated_markers": "text",
    "generated_markers": "text",
    "js_links": "text",
}


def _normalize_js_result_mode(value: Any) -> tuple[str, bool]:
    """Return a canonical JS result mode and whether an alias was used."""
    raw = str(value or "").strip()
    canonical = _JS_RESULT_MODE_ALIASES.get(raw.casefold(), raw)
    return canonical, bool(raw) and canonical != raw


def _rpa_component_plan(flow: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return one authoritative component plan and identify its source."""
    if not isinstance(flow, dict):
        return [], "none"
    for key in ("execution_plan", "mcpExecutionPlan"):
        execution_plan = flow.get(key)
        steps = execution_plan.get("steps") if isinstance(execution_plan, dict) else None
        if isinstance(steps, list):
            return steps, f"{key}.steps"
    for key in ("component_plan", "mcpComponentPlan"):
        component_plan = flow.get(key)
        if isinstance(component_plan, list):
            return component_plan, key
    return [], "none"


def _rpa_sequence(flow: dict[str, Any]) -> list[str]:
    """Return the request plan sequence, or the legacy browser sequence."""
    component_plan, _ = _rpa_component_plan(flow)
    # A partial CLI flow may contain the first planned node only.  Treat a
    # plan as an ordering contract only after it has been attached as a
    # complete MCP execution plan; otherwise the client remains free to add
    # any live component next.
    plan_complete = bool(
        flow.get("plan_complete")
        or flow.get("mcpPlanComplete")
        or flow.get("plan_id")
        or flow.get("mcpPlanId")
        or flow.get("mcpExecutionPlan")
    )
    if _rpa_model_decides(flow) and not plan_complete:
        # There is no recipe to infer for a model-owned partial flow.  Keep
        # only the components that have actually been written; validation and
        # next-action code must not manufacture a hidden sequence.
        actual_steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
        return [
            str(item.get("component") or "").strip()
            for item in actual_steps
            if isinstance(item, dict) and str(item.get("component") or "").strip()
        ]
    if plan_complete and isinstance(component_plan, list):
        planned = [
            str(item.get("component") or "").strip()
            for item in component_plan
            if isinstance(item, dict) and str(item.get("component") or "").strip()
        ]
        if planned:
            return planned
    sequence = list(RPA_RECOMMENDED_SEQUENCE)
    # The direct JS-table fallback replaces DataCollectionV5 rather than
    # adding a second collector. Its output is already the same table variable
    # consumed by Excel_Cs_WriteRange.
    if bool(flow.get("use_js_table")):
        sequence = [item for item in sequence if item != "WebBrowser_DataCollectionV5"]
    # Flows created before the result-wait node was introduced remain readable
    # and valid. New flows set this flag explicitly so the wait is part of the
    # persisted component order rather than an implicit host-side sleep.
    if bool(flow.get("wait_after_submit", False)):
        sequence.insert(3, "Basic_Delay_V1")
    if len(sequence) >= 3:
        sequence[2] = _rpa_submit_component(flow)
    if bool(flow.get("refresh_browser_tab", False)):
        # The search submit can navigate the existing tab. Rebind the browser
        # handle after the explicit wait and before any result-page component.
        submit_index = sequence.index(_rpa_submit_component(flow))
        sequence.insert(submit_index + 1, "WebBrowser_GetTAB_V1")
        if "Basic_Delay_V1" in sequence:
            delay_index = sequence.index("Basic_Delay_V1")
            tab_index = sequence.index("WebBrowser_GetTAB_V1")
            if tab_index < delay_index:
                sequence.pop(tab_index)
                sequence.insert(delay_index, "WebBrowser_GetTAB_V1")
    if bool(flow.get("use_js_link")) or bool(flow.get("use_js_table")):
        # The page must settle and the current tab handle must be refreshed
        # before page-scoped JS reads the result page.
        insert_at = (
            max(
                (sequence.index(item) for item in ("Basic_Delay_V1", "WebBrowser_GetTAB_V1") if item in sequence),
                default=2,
            )
            + 1
        )
        sequence.insert(insert_at, "WebBrowser_RunJsInIeBySelector_v3")
    if bool(flow.get("excel_mode")):
        sequence.extend(RPA_EXCEL_SEQUENCE)
    return sequence


def _rpa_contract_map() -> dict[str, dict[str, Any]]:
    contracts = {item["component"]: dict(item) for item in RPA_ALL_COMPONENT_CONTRACTS}
    for component, contract in contracts.items():
        lowered = component.casefold()
        capabilities: set[str] = set()
        operation_types: set[str] = set()
        if "create" in lowered or "gettab" in lowered:
            capabilities.add("navigate")
            operation_types.add("navigate")
        if any(token in lowered for token in ("setvalue", "keyboard", "press", "send")):
            capabilities.update(("input", "submit"))
            operation_types.update(("search", "interact"))
        if "datacollection" in lowered:
            capabilities.update(("extract", "transform", "output", "limit", "paginate"))
            operation_types.update(("extract", "transform", "output"))
        if "runjs" in lowered:
            capabilities.update(("extract", "transform", "limit"))
            operation_types.update(("extract", "transform"))
        if "excel" in lowered:
            capabilities.add("output")
            operation_types.add("output")
        if "delay" in lowered:
            capabilities.add("wait")
        contract["capabilities"] = sorted(capabilities)
        contract["operation_types"] = sorted(operation_types)
        # Fixed native contracts own their logical output names.  The
        # materializer writes these names regardless of model aliases, so a
        # plan may be canonicalized instead of spending a retry on cosmetic
        # output-label differences.
        contract["canonical_outputs"] = True
    return contracts


def _constant_native_types(parameter: dict[str, Any]) -> set[str]:
    """Only use unconditional, concrete designer types for early dataflow checks."""
    rules = parameter.get("vType")
    if not isinstance(rules, list) or not rules:
        return set()
    result: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("rule") != "const" or not isinstance(rule.get("types"), list):
            return set()
        result.update(t for t in rule["types"] if isinstance(t, str))
    # Other designer types may be dynamic or have undocumented subtyping.
    families = {"rpa_pd": "dataframe", "rpa_list": "list", "rpa_dict": "dict",
                "rpa_str": "string", "rpa_str_file": "string", "rpa_str_folder": "string",
                "rpa_num": "number", "rpa_int": "number", "rpa_bool": "bool"}
    families.update({t: "list" for t in result if t.startswith("rpa_list_")})
    families.update({t: f"handle:{t}" for t in result if t.startswith("rpa_h")})
    families.update({t: f"handle:{t}" for t in result if t.startswith("rpa_window")})
    return {families[t] for t in result} if result and result <= families.keys() else set()


def _catalog_rpa_contract(component: str, record: dict[str, Any]) -> dict[str, Any]:
    """Normalize one live designer catalog row for generic plan validation."""
    inputs = [
        str(item.get("name") or "").strip()
        for item in record.get("inputs", [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    options = [
        str(item.get("name") or "").strip()
        for item in record.get("options", [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    outputs = record.get("outputs") if isinstance(record.get("outputs"), dict) else {}
    output_parameters = [str(item).strip() for item in outputs.get("assign_to", []) if str(item).strip()]
    required_arguments = []
    for item in record.get("inputs", []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        rules = item.get("validatorRules") if isinstance(item.get("validatorRules"), dict) else {}
        value_rules = rules.get("compAttrValue") if isinstance(rules.get("compAttrValue"), list) else []
        if any(isinstance(rule, dict) and str(rule.get("required") or "") == "1" for rule in value_rules):
            required_arguments.append(str(item["name"]))
    code = deepcopy(record.get("code")) if isinstance(record.get("code"), list) else []
    materialization_errors: list[str] = []
    if bool(record.get("designer_static")) or bool(record.get("designer_static_index")):
        materialization_errors.append("designer-static entries are not executable component nodes")
    if record.get("materializable") is False:
        materialization_errors.append("the live catalog marks this component as non-materializable")
    if len(code) != 1:
        materialization_errors.append("generic component generation requires exactly one catalog code record")
    elif not isinstance(code[0], dict):
        materialization_errors.append("the catalog code record must be an object")
    else:
        function_path = str(code[0].get("func") or "").strip()
        if "." not in function_path:
            materialization_errors.append("the catalog code record has no package-qualified function")
        if not isinstance(code[0].get("args"), list):
            materialization_errors.append("the catalog code record args must be a list")
    lowered_type = str(component or "").casefold()
    inferred_plan_input_mode = (
        "none"
        if lowered_type.startswith("keyboard_")
        or lowered_type.startswith("basic_delay")
        or lowered_type.startswith("webbrowser_create")
        or lowered_type.startswith("webbrowser_gettab")
        else "variables"
    )
    return {
        "component": component,
        "designer_type": record.get("native_type") or component,
        "designer_label": record.get("label") or component,
        "purpose": record.get("description") or "",
        "requires_elements": bool(record.get("requires_page_evidence")) or "uiSelector" in inputs,
        "inputs": inputs,
        "options": options,
        "parameter_names": sorted(set(inputs) | set(options)),
        "outputs": [],
        "plan_input_mode": inferred_plan_input_mode,
        "plan_input_rule": (
            "inputs must be empty; configure literal component arguments in config"
            if inferred_plan_input_mode == "none"
            else "inputs lists only runtime variables produced by earlier dependent steps"
        ),
        "output_parameters": output_parameters,
        "native_output_parameters": output_parameters,
        "required_arguments": required_arguments,
        "capabilities": list(record.get("capabilities") or []),
        "operation_types": list(record.get("operation_types") or []),
        "deferred_bindings": list(record.get("deferred_bindings") or []),
        "configuration_hints": deepcopy(record.get("configuration_hints") or {}),
        "conditional_requirements": deepcopy(record.get("conditional_requirements") or []),
        "planning_patterns": deepcopy(record.get("planning_patterns") or []),
        "code": code,
        "catalog_backed": True,
        "materializable": not materialization_errors,
        "materialization_errors": materialization_errors,
        "runtime_preconditions": list(record.get("runtime_preconditions") or []),
        "parameter_choices": {item["name"]: [choice["value"] for choice in item["semantic_choices"]]
                              for item in record.get("inputs", []) if isinstance(item, dict)
                              and item.get("name") and item.get("semantic_choices")},
        "parameter_types": {item["name"]: sorted(_constant_native_types(item))
                            for item in record.get("inputs", []) if isinstance(item, dict) and item.get("name")},
    }


def _live_rpa_contract(paths: ShizaiPaths, component: str) -> dict[str, Any] | None:
    # The installed designer dictionary is authoritative for argument names,
    # options and code generation.  Built-in compatibility metadata supplies
    # only stable runtime defaults (logical outputs and element semantics),
    # never a reduced parameter whitelist.
    compatibility = _rpa_contract_map().get(component)
    from . import component_catalog as component_catalog_core

    record = component_catalog_core.builtin_component_contract(paths, component)
    if isinstance(record, dict):
        live = _catalog_rpa_contract(component, record)
        if compatibility is None:
            return live
        merged = dict(compatibility)
        merged.update(live)
        if not live.get("inputs"):
            merged["inputs"] = list(compatibility.get("inputs") or [])
        if not live.get("outputs") and compatibility.get("outputs"):
            merged["outputs"] = list(compatibility["outputs"])
        merged["capabilities"] = sorted(
            set(compatibility.get("capabilities") or []) | set(live.get("capabilities") or [])
        )
        merged["operation_types"] = sorted(
            set(compatibility.get("operation_types") or []) | set(live.get("operation_types") or [])
        )
        # The page-scoped JavaScript component advertises ``uiSelector`` as
        # an optional scope.  Do not turn that optional argument into a
        # mandatory element binding; other components keep the live evidence
        # requirement unchanged.
        merged["requires_elements"] = (
            bool(compatibility.get("requires_elements"))
            if component == "WebBrowser_RunJsInIeBySelector_v3"
            else bool(compatibility.get("requires_elements") or live.get("requires_elements"))
        )
        merged["catalog_backed"] = True
        merged["materializable"] = bool(live.get("materializable", True))
        return merged
    return dict(compatibility) if compatibility is not None else None


def _preferred_submit_component(paths: ShizaiPaths) -> str:
    """Prefer the newest installed Enter component when its template exists."""
    if paths.applications_root.is_dir():
        for task_path in paths.applications_root.rglob("*.task"):
            ok, payload, _ = _safe_json(task_path)
            if not ok or not isinstance(payload, dict):
                continue
            entities = payload.get("componentModelEntities")
            if isinstance(entities, dict) and any(
                isinstance(node, dict) and node.get("type") == "Keyboard_PressNew" for node in entities.values()
            ):
                return "Keyboard_PressNew"
    return "Keyboard_PressNew"


RPA_STEP_REQUIRED_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "WebBrowser_CreateV3": ("var_name", "type", "url"),
    "Element_SetValue_V6": ("uiSelector", "designated_obj", "text"),
    "Keyboard_Send_V1": ("words",),
    "Keyboard_PressNew": ("key", "type"),
    "Basic_Delay_V1": ("second",),
    "WebBrowser_GetTAB_V1": ("var_name", "type", "match_type", "isActive"),
    "WebBrowser_RunJsInIeBySelector_v3": (
        "var_name",
        "designated_info",
        "web_page",
        "jsCode",
        "input",
        "executionEnvironment",
    ),
    # ``selector_page`` is conditional in the native component contract. It
    # is required only when a multi-page/"all pages" collection explicitly
    # uses the pagination-button mode; a single-page collection must leave it
    # empty instead of binding a page-scope element.
    "WebBrowser_DataCollectionV5": ("var0", "config", "designated_obj"),
    "WebBrowser_CloseWeb_V2": ("colse_type", "web"),
    "Excel_Cs_NewV4": ("var_name", "path", "name"),
    "Excel_Cs_WriteRange": ("workbook", "sheet", "row", "column", "contents"),
}

# Older designer dictionaries may advertise this legacy type although the
# installed runtime template is the versioned V3 component.
RPA_COMPONENT_TYPE_ALIASES: dict[str, str] = {
    "WebBrowser_Create": "WebBrowser_CreateV3",
}

RPA_STEP_OUTPUT_VARIABLES: dict[str, tuple[str, ...]] = {
    "WebBrowser_CreateV3": ("网页对象",),
    "Element_SetValue_V6": ("校验结果",),
    "Keyboard_Send_V1": (),
    "Keyboard_PressNew": (),
    "Basic_Delay_V1": (),
    "WebBrowser_GetTAB_V1": ("网页对象",),
    "WebBrowser_RunJsInIeBySelector_v3": ("商品链接",),
    # DataCollectionV5 always exposes the monitor-empty-column output in the
    # native designer task, even when monitor_data is disabled. Keep the
    # contract aligned with var0/var1/var2 emitted by RuntimeHost.
    "WebBrowser_DataCollectionV5": ("数据采集结果", "Excel路径", "监控空列数据"),
    "WebBrowser_CloseWeb_V2": ("操作结果",),
    "Database_DBConnect": ("数据库对象",),
    "Database_SQLExecute": ("SQL执行结果",),
    "Database_BatchSQLExecute": ("SQL执行结果",),
    "Database_BatchInsertExecute": ("批量插入结果",),
    "Database_CloseDBConnect": ("数据库对象",),
    "JSON_Parse": ("转换结果",),
    "JSON_Stringify": ("转换结果",),
    "Excel_Cs_NewV4": ("Excel文件",),
    "Excel_Cs_WriteRange": (),
}

# Components that publish the browser object consumed by later page components.
RPA_BROWSER_OBJECT_COMPONENTS: tuple[str, ...] = ("WebBrowser_CreateV3", "WebBrowser_GetTAB_V1")
RPA_DEFAULT_WEB_PAGE_OBJECT = "网页对象"


def _output_variables_match(component: str, expected: list[str], actual: list[str]) -> bool:
    """Match declared outputs against the slots the native component actually writes.

    A plan may name fewer outputs than the component emits, because the designer
    always writes every slot the component declares.  Surplus trailing slots are
    therefore acceptable when each one is a canonical output of this component;
    anything else is still a mismatch.
    """
    if len(actual) < len(expected):
        return False
    if not _output_names_match(expected, actual[: len(expected)]):
        return False
    canonical = RPA_STEP_OUTPUT_VARIABLES.get(component, ())
    return all(
        any(_output_names_match([name], [observed]) for name in canonical)
        for observed in actual[len(expected) :]
    )


def _logical_plan_outputs(
    component: str,
    contract: dict[str, Any],
    config: dict[str, Any],
    *,
    require_assignments: bool = False,
) -> list[str]:
    """Resolve logical outputs without exposing native assignment slots."""
    output_parameters = [
        str(item).strip() for item in contract.get("output_parameters", []) if str(item).strip()
    ]
    if output_parameters:
        missing = [name for name in output_parameters if not str(config.get(name) or "").strip()]
        if missing and require_assignments:
            raise ValueError("missing native output assignment parameters: " + ", ".join(missing))
        assigned = [str(config[name]).strip() for name in output_parameters if name not in missing]
        if assigned:
            return assigned
    # Built-in compatibility contracts keep stable default output names, but
    # the native component still accepts explicit names in var_name/var0/etc.
    # Honor those assignments so a later {"variable": "..."} reference uses
    # the same name that the materializer writes into the designer task.
    native_output_parameters = [
        str(item).strip()
        for item in contract.get("native_output_parameters", [])
        if str(item).strip()
    ]
    if (
        component == "WebBrowser_DataCollectionV5"
        and not native_output_parameters
        and str(config.get("collection_stage") or "").strip()
    ):
        native_output_parameters = ["var0", "var1", "var2"]
    canonical_outputs = [str(item).strip() for item in contract.get("outputs", []) if str(item).strip()]
    if component == "WebBrowser_DataCollectionV5" and str(config.get("collection_output") or "").strip():
        configured = str(config["collection_output"]).strip()
        return [configured, *canonical_outputs[1:]] if canonical_outputs else [configured]
    if native_output_parameters and canonical_outputs:
        resolved = []
        for index, default_name in enumerate(canonical_outputs):
            parameter = native_output_parameters[index] if index < len(native_output_parameters) else ""
            configured_name = str(config.get(parameter) or "").strip() if parameter else ""
            resolved.append(configured_name or default_name)
        return resolved
    if component == "WebBrowser_RunJsInIeBySelector_v3":
        output_variable = str(config.get("output_variable") or "").strip()
        if output_variable:
            return [output_variable]
    return canonical_outputs


def _normalize_linear_plan_dependencies(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair the model-facing dependency chain after an incremental write.

    The native designer task is a linear MAIN_COMPONENT statement list.  Older
    MCP metadata could retain dependencies for nodes that had since been
    removed, which made a valid native task fail plan validation as soon as a
    new database or JSON component was appended.  Incremental authoring has no
    graph-edit API, so the only truthful representation here is the current
    native order.
    """
    if not steps or not all(
        isinstance(item, dict) and str(item.get("id") or "").strip() for item in steps
    ):
        return steps
    previous = ""
    for item in steps:
        if previous:
            item["depends_on"] = [previous]
        else:
            item.pop("depends_on", None)
        previous = str(item["id"]).strip()
    return steps


def ensure_native_browser_cleanup_step(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Add missing cleanup steps to complete native MCP flows under the write lock."""
    from .browser_lifecycle import browser_cleanup_steps

    project_dir = resolve_project(paths, project_ref)
    with _project_write_lock(paths, project_ref):
        try:
            state = _native_rpa_state(paths, str(project_dir))
        except (FileNotFoundError, OSError, ValueError):
            return {"status": "skipped", "reason": "native_task_not_found", "execution_verified": False}
        if not state.get("plan_complete"):
            return {"status": "skipped", "reason": "flow_plan_is_not_complete", "execution_verified": False}
        _, metadata, _ = _safe_json(project_dir / "szrpa.json")
        execution_plan = (metadata or {}).get("mcpExecutionPlan") or {}
        if execution_plan.get("browser_cleanup_policy") == "keep_open":
            return {"status": "skipped", "reason": "explicit_keep_open", "execution_verified": False}
        task_path, payload = _native_task_file(project_dir)
        # Use actual output slots, including assignments from non-browser
        # components; a cached plan can lag behind manual designer edits.
        steps = []
        for step in state["steps"]:
            node = payload["componentModelEntities"][step["id"]]
            config = _native_component_config(node)
            if step["component"].startswith("WebBrowser_CloseWeb"):
                config["web"] = {"variable": _native_task_argument(node, "web", "")}
            steps.append({**step, "config": config, "outputs": _native_output_names(node, step["component"])})
        assessment = browser_cleanup_steps(steps)
        additions = assessment.pop("steps")
        # Only auto-generated finalizers are movable during later appends.
        # An explicit user-authored close must retain its place in the flow.
        existing_managed = set((metadata or {}).get("mcpBrowserCleanupStepIds") or [])
        managed_ids = [step["id"] for step in steps if step["id"] in existing_managed]
        if not additions:
            return assessment
        if _live_rpa_contract(paths, "WebBrowser_CloseWeb_V2") is None:
            assessment.update(status="unavailable", reason="close_component_not_installed")
            return assessment
        editor_block = _unsaved_editor_block(project_dir, client)
        if editor_block is not None:
            return editor_block
        for addition in additions:
            result = _add_native_rpa_step(
                paths, str(project_dir), addition["component"],
                step_id=addition["id"], label=addition["purpose"],
                config=addition["config"], client=client,
                allow_unbound_predecessors=True,
            )
            if not result.get("step_validation", {}).get("valid"):
                raise ValueError("browser cleanup component failed static validation")
        _update_native_metadata(project_dir, len(steps) + len(additions), {
            "mcpAutoCloseBrowser": True, "mcpBrowserCleanupPolicy": "close_created_tab",
            "mcpBrowserCleanupStepIds": [*managed_ids, *assessment["step_ids"]],
        })
        return assessment


def _rpa_verification_gate(
    flow_ref: str,
    step: dict[str, Any] | None,
    step_validation: dict[str, Any],
    *,
    runtime_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the incremental verification required before the next step.

    Writing a component and passing its static contract are not proof that the
    designer or the page accepted the action.  This small shared contract is
    returned by both the CLI and MCP paths so client models do not mistake the
    structural ``next_action`` for permission to keep appending components.
    """
    current = step if isinstance(step, dict) else {}
    step_id = str(current.get("id") or step_validation.get("step_id") or "").strip()
    component = str(current.get("component") or step_validation.get("component") or "").strip()
    static_valid = bool(step_validation.get("valid"))
    evidence = runtime_evidence if isinstance(runtime_evidence, dict) else {}
    runtime_verified = bool(
        evidence.get("verified")
        or evidence.get("business_verified")
        or evidence.get("status") in {"paused", "succeeded"}
    )
    if not static_valid:
        status = "static_failed"
    elif runtime_verified:
        status = "verified"
    else:
        status = "runtime_pending"
    runtime_action: dict[str, Any] = {
        "kind": "mcp_tool",
        "tool": "shizai_rpa_debug_capabilities",
        "arguments": {"project_ref": flow_ref},
        "reason": "先确认设计器支持组件级调试，再启动或复用 design_validation 会话。",
    }
    if not static_valid:
        runtime_action = {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_validate_step",
            "arguments": {"flow_ref": flow_ref, "step_id": step_id},
            "reason": "先修复当前组件的静态契约错误，不能追加下一组件。",
        }
    return {
        "required": True,
        "status": status,
        "step_id": step_id,
        "component": component,
        "static_validation": {
            "tool": "shizai_rpa_validate_step",
            "cli_command": "workflow rpa-validate-step",
            "valid": static_valid,
        },
        "runtime_validation": {
            "required": True,
            "verified": runtime_verified,
            "mode": "design_validation",
            "tools": [
                "shizai_rpa_debug_capabilities",
                "shizai_rpa_debug_start",
                "shizai_rpa_debug_command",
                "shizai_rpa_debug_status",
            ],
            "cli_commands": [
                "workflow rpa-debug-capabilities",
                "workflow rpa-debug-start",
                "workflow rpa-debug-next",
                "workflow rpa-debug-status",
            ],
            "evidence_required": [
                "debug result reaches the current component boundary or a confirmed terminal state",
                "page/runtime evidence confirms the intended visible effect or output",
            ],
        },
        "before_next_step": [
            "static_validation.valid=true",
            "start_or_reuse_design_validation_session",
            "execute_current_component_with_debug_next",
            "inspect_runtime_or_page_evidence",
        ],
        "advance_only_when": [
            "current step static_validation.valid=true",
            "current step runtime_validation.verified=true",
        ],
        "runtime_evidence": evidence or None,
        "next_action": runtime_action,
        "on_failure": "修复当前组件并重新验证；不要追加下一组件、创建第二个流程或把静态通过当作运行成功。",
    }


def _rpa_next_action(flow: dict[str, Any]) -> dict[str, Any]:
    """Describe the next concrete design action for an MCP client."""
    flow_ref = str(flow.get("flow_ref") or "")
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    step_count = len(steps)
    component_plan, _ = _rpa_component_plan(flow)
    plan_complete = bool(
        flow.get("plan_complete")
        or flow.get("mcpPlanComplete")
        or flow.get("plan_id")
        or flow.get("mcpPlanId")
        or flow.get("mcpExecutionPlan")
    )
    unbound = next(
        (step for step in steps if isinstance(step, dict) and str(step.get("binding_status") or "") == "unbound"),
        None,
    )
    if unbound is not None:
        step_id = str(unbound.get("id") or "").strip()
        planned = next(
            (
                item
                for item in component_plan or []
                if isinstance(item, dict) and str(item.get("id") or "").strip() == step_id
            ),
            {},
        )
        role = str(planned.get("element_role") or unbound.get("label") or unbound.get("component") or "").strip()
        if str(unbound.get("component") or "") == "WebBrowser_DataCollectionV5":
            required = _result_capture_requirements(
                flow.get("required_columns") or DEFAULT_RPA_COLUMNS,
                use_js_link=bool(flow.get("use_js_link")),
                use_js_table=bool(flow.get("use_js_table")),
            )
            fields = [
                field
                for field in _RPA_RESULT_FIELD_ORDER
                if field in required
            ]
            return {
                "kind": "browser_capture",
                "tool": "shizai_rpa_capture_page_fields",
                "arguments": {
                    "project_ref": flow_ref,
                    "step_id": step_id,
                    "fields": fields,
                    "use_js_link": bool(flow.get("use_js_link")),
                    "allow_write": True,
                },
                "step_id": step_id,
                "component": unbound.get("component"),
                "reason": "数据采集步骤尚未绑定输出列；在同一次页面快照中捕获同一条记录的全部所需字段。",
            }
        return {
            "kind": "browser_capture",
            "tool": "shizai_rpa_scan_page",
            "arguments": {
                "project_ref": flow_ref,
                "step_id": step_id,
                "query": role,
            },
            "step_id": step_id,
            "component": unbound.get("component"),
            "element_role": role,
            "fallback_tool": "shizai_extension_start_capture",
            "reason": "元素组件已写入但尚未绑定；先扫描当前页面，必要时再为该组件启动官方拾取。",
        }
    if plan_complete and isinstance(component_plan, list) and component_plan:
        if step_count >= len(component_plan):
            return {
                "kind": "mcp_tool",
                "tool": "shizai_rpa_validate_flow",
                "arguments": {"flow_ref": flow_ref},
                "reason": "本次请求计划中的组件均已写入；开始校验完整流程和输出契约。",
            }
        planned = component_plan[step_count]
        if not isinstance(planned, dict):
            raise ValueError(f"component plan step {step_count + 1} must be an object")
        component = str(planned.get("component") or "").strip()
        action: dict[str, Any] = {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": component,
                "step_id": str(planned.get("id") or f"step-{step_count + 1}"),
                "config": planned.get("config") or {},
                "allow_write": True,
            },
            "purpose": planned.get("purpose"),
            "operation_ids": planned.get("operation_ids") or [],
            "depends_on": planned.get("depends_on") or [],
            "reason": "严格执行本次请求的已校验原子步骤计划；不使用站点内置流程。",
        }
        element_role = str(planned.get("element_role") or "").strip()
        if element_role:
            action["element_capture"] = {
                "role": element_role,
                "sequence": [
                    "shizai_rpa_add_step",
                    "shizai_rpa_scan_page",
                    "shizai_extension_start_capture",
                    "shizai_rpa_capture_elements",
                ],
                "optional_picker_tool": "shizai_extension_start_capture",
                "fallback_tool": "shizai_project_import_elements",
            }
            action["post_add_binding_required"] = True
        return action
    if _rpa_model_decides(flow):
        # No model-owned plan was attached.  Do not turn compatibility
        # guidance into a mandatory sequence; the caller can select any live
        # component and submit its contract-backed config.
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {"flow_ref": flow_ref, "allow_write": True},
            "reason": "由模型根据实时组件目录决定下一个组件、参数和依赖；MCP 只执行契约校验。",
            "selection": "model_decides",
        }
    if step_count == 0:
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": "WebBrowser_CreateV3",
                "step_id": "open-chrome",
                "config": {
                    "url": flow.get("target_url", ""),
                    "type": "chrome",
                },
                "allow_write": True,
            },
            "reason": "模型可从实时组件目录选择下一个组件；若本次流程需要浏览器导航，可选择导航能力组件。",
        }
    if step_count == 1 and int(flow.get("element_count", 0) or 0) == 0:
        return {
            "kind": "browser_capture",
            "tool": "shizai_rpa_capture_windows",
            "arguments": {"include_non_chrome": False},
            "tools": [
                "shizai_rpa_capture_windows",
                "shizai_rpa_capture_tree",
                "shizai_rpa_scan_page",
                "shizai_rpa_capture_elements",
                "shizai_project_import_elements",
            ],
            "optional_picker_tool": "shizai_extension_start_capture",
            "fallback_tools": ["shizai_project_import_elements"],
            "then": "shizai_rpa_validate_elements",
            "reason": "首节点已经创建；使用已初始化的 DeepCapture 页面树识别并捕获真实元素，不主动打开红色拾取层。只有交互式拾取确有必要时才调用 optional_picker_tool；页面树不适用时导入模型生成并校验过的 selector。",
        }
    if step_count == 1:
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": "Element_SetValue_V6",
                "step_id": "search-input",
                "allow_write": True,
            },
            "required_arguments": ["element_ids"],
            "reason": "搜索框元素已导入；创建输入组件时传入搜索框 element_id。",
        }
    if step_count == 2:
        submit_component = _rpa_submit_component(flow)
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": submit_component,
                "step_id": "submit-search",
                "config": {"key": "13", "type": "press"}
                if submit_component == "Keyboard_PressNew"
                else {"words": "{ENTER}"},
                "allow_write": True,
            },
            "reason": (
                "搜索框输入组件已经绑定；继续使用 Keyboard_PressNew 发送真实回车提交搜索。"
                if submit_component == "Keyboard_PressNew"
                else "搜索框输入组件已经绑定；继续创建兼容模式的键盘提交组件。"
            ),
        }
    sequence = _rpa_sequence(flow)
    if step_count >= len(sequence):
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_validate_flow",
            "arguments": {"flow_ref": flow_ref},
            "reason": "网页和 Excel 组件已按顺序创建并绑定，先进行客户端原生流程校验。",
        }

    expected = sequence[step_count]
    if expected == "Basic_Delay_V1":
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": expected,
                "step_id": "wait-search-results",
                "config": {"second": flow.get("wait_seconds", 1)},
                "allow_write": True,
            },
            "reason": "搜索已提交；先使用设计器原生等待组件等待结果页完成导航和商品卡片渲染。",
        }
    if expected == "WebBrowser_GetTAB_V1":
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": expected,
                "step_id": "refresh-browser-tab",
                "config": {
                    "type": "chrome",
                    "match_type": 2,
                    "title": "",
                    "url": "",
                    "isActive": 1,
                },
                "allow_write": True,
            },
            "reason": "搜索结果已完成等待；重新获取当前 Chrome Tab 并覆盖网页对象，后续结果页组件复用同一会话。",
        }
    if (
        expected in {"WebBrowser_RunJsInIeBySelector_v3", "WebBrowser_DataCollectionV5"}
        and not bool(flow.get("result_elements_ready"))
        and not bool(flow.get("use_js_table"))
    ):
        required = _result_capture_requirements(
            flow.get("required_columns") or DEFAULT_RPA_COLUMNS,
            use_js_link=bool(flow.get("use_js_link")),
            use_js_table=bool(flow.get("use_js_table")),
        )
        result_fields = [
            field
            for field in _RPA_RESULT_FIELD_ORDER
            if field in required
        ]
        return {
            "kind": "browser_capture",
            "tool": "shizai_rpa_capture_page_fields",
            "arguments": {
                "project_ref": flow_ref,
                "step_id": str(
                    next(
                        (
                            item.get("id")
                            for item in steps
                            if isinstance(item, dict)
                            and item.get("component") == "WebBrowser_DataCollectionV5"
                        ),
                        "",
                    )
                ),
                "fields": result_fields,
                "use_js_link": bool(flow.get("use_js_link")),
                "allow_write": True,
            },
            "tools": [
                "shizai_rpa_capture_windows",
                "shizai_rpa_capture_tree",
                "shizai_rpa_scan_page",
                "shizai_rpa_capture_elements",
                "shizai_project_import_elements",
            ],
            "optional_picker_tool": "shizai_extension_start_capture",
            "fallback_tools": ["shizai_project_import_elements"],
            "then": "shizai_rpa_add_step",
            "reason": (
                "搜索结果页句柄已刷新；先在同一 Chrome 会话捕获同一商品卡片的商品名、价格和销量并导入，商家与链接由后续 JS 组件在卡片内生成。"
                if bool(flow.get("use_js_link"))
                else "搜索结果页句柄已刷新；先在同一 Chrome 会话捕获同一商品卡片的商品名、商家、价格和销量并导入。"
            ),
        }
    if expected == "WebBrowser_RunJsInIeBySelector_v3":
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": expected,
                "step_id": "extract-product-links",
                "allow_write": True,
            },
            "reason": (
                "结果页已准备；用当前结果页执行页面级 JS，直接返回表头和二维商品结果，绕过动态 DOM 采集器。"
                if bool(flow.get("use_js_table"))
                else "四个商品字段已导入；用当前结果页执行页面级 JS，生成可复用的商品链接和商家元素。"
            ),
        }
    if expected == "WebBrowser_DataCollectionV5":
        action = {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": expected,
                "step_id": "collect-products",
                "allow_write": True,
            },
            "required_arguments": ["element_ids"],
            "reason": "结果字段已导入；继续添加网页数据采集组件并按列绑定元素。",
        }
        if bool(flow.get("use_js_link")):
            action.update(
                {
                    "generated_element_id": JS_LINK_ELEMENT_ID,
                    "generated_element_ids": [JS_MERCHANT_ELEMENT_ID, JS_LINK_ELEMENT_ID],
                    "reason": "JS 恢复组件已写入；DataCollectionV5 需要三个原生字段加上 JS 生成的商家和商品链接元素。",
                }
            )
        return action
    if expected == "Excel_Cs_NewV4" and bool(flow.get("excel_mode")):
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": "Excel_Cs_NewV4",
                "step_id": "create-excel",
                "allow_write": True,
            },
            "reason": "数据采集组件已完成；按 demo 场景继续使用真实 Excel 组件创建输出文件。",
        }
    if expected == "Excel_Cs_WriteRange" and bool(flow.get("excel_mode")):
        return {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {
                "flow_ref": flow_ref,
                "component": "Excel_Cs_WriteRange",
                "step_id": "write-excel",
                "allow_write": True,
            },
            "reason": "Excel 文件已创建；把数据采集结果写入区域并保存。",
        }
    return {
        "kind": "mcp_tool",
        "tool": "shizai_rpa_validate_flow",
        "arguments": {"flow_ref": flow_ref},
        "reason": "网页和 Excel 组件已按顺序创建并绑定，先进行客户端原生流程校验。",
    }


def unbound_rpa_element_step(
    paths: ShizaiPaths,
    project_ref: str,
    step_id: str,
    *,
    allow_rebind: bool = False,
) -> dict[str, Any]:
    """Return one element component for first capture or explicit rebind."""
    normalized_ref = str(project_ref or "").strip()
    normalized_step_id = str(step_id or "").strip()
    if not normalized_ref:
        raise ValueError("project_ref is required before element capture")
    if not normalized_step_id:
        raise ValueError("step_id is required; add the element component before capture")
    flow_result = read_rpa_flow(paths, normalized_ref)
    flow = flow_result.get("flow") if isinstance(flow_result.get("flow"), dict) else flow_result
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    step = next(
        (item for item in steps if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_step_id),
        None,
    )
    if step is None:
        raise ValueError(f"element component must be added before capture: {normalized_step_id}")
    binding_status = str(step.get("binding_status") or "")
    if binding_status != "unbound" and not allow_rebind:
        raise ValueError(
            f"element capture requires an unbound component: {normalized_step_id}; "
            f"binding_status={binding_status}"
        )
    plan = flow.get("component_plan") if isinstance(flow.get("component_plan"), list) else []
    planned = next(
        (item for item in plan if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_step_id),
        {},
    )
    # CLI-created partial flows do not carry a complete model execution plan.
    # In that mode the step label is the caller's capture intent and is enough
    # to start DeepCapture; complete plans still prefer their explicit role.
    element_role = str(
        planned.get("element_role")
        or step.get("element_role")
        or step.get("label")
        or step.get("component")
        or ""
    ).strip()
    if not element_role:
        raise ValueError(f"persisted plan has no element_role for capture step: {normalized_step_id}")
    return {
        "step_id": normalized_step_id,
        "component": step.get("component"),
        "element_role": element_role,
        "binding_status": binding_status,
        "rebind": bool(allow_rebind and binding_status != "unbound"),
    }


def _safe_json(path: Path) -> tuple[bool, Any | None, str | None]:
    if not path.is_file():
        return False, None, "missing"
    try:
        return True, read_json(path), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, None, str(exc)


def resolve_project(paths: ShizaiPaths, project_ref: str) -> Path:
    """Resolve an application id or filesystem path to an application directory."""
    candidate = Path(project_ref).expanduser()
    if candidate.is_file() and candidate.name.lower() == "szrpa.json":
        candidate = candidate.parent
    if candidate.is_dir():
        return candidate.resolve()
    candidate = (paths.applications_root / project_ref).resolve()
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"Project not found: {project_ref}")


def _fingerprint_text(value: Any) -> str:
    """Normalize user-controlled text before it participates in a request key."""
    return " ".join(str(value or "").split()).strip().casefold()


def rpa_request_fingerprint(
    query: str,
    *,
    target_url: str = "",
    output_contract: dict[str, Any] | None = None,
) -> str:
    """Return a stable identity for one logical RPA request.

    The request identity deliberately excludes transient selector evidence,
    analysis ids, and the output filename.  A plan can be repaired or rebound,
    or its artifact name can change, while still representing the same user
    request and should therefore keep using the same flow.
    """
    contract = output_contract if isinstance(output_contract, dict) else {}
    columns = [
        _fingerprint_text(item)
        for item in contract.get("columns", [])
        if _fingerprint_text(item)
    ]
    raw_max_rows = contract.get("max_rows")
    try:
        max_rows: int | None = int(raw_max_rows) if raw_max_rows not in (None, "") else None
    except (TypeError, ValueError):
        max_rows = None
    payload = {
        "query": _fingerprint_text(query),
        "target_url": _fingerprint_text(target_url),
        "columns": columns,
        "max_rows": max_rows,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _metadata_rpa_request_fingerprint(metadata: dict[str, Any]) -> tuple[str, str]:
    """Read an explicit fingerprint or derive one for older MCP projects."""
    explicit = str(metadata.get("mcpRequestFingerprint") or "").strip()
    if explicit:
        return explicit, "explicit"
    analysis = metadata.get("mcpRequestAnalysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    query = str(metadata.get("mcpOriginalQuery") or analysis.get("query") or "").strip()
    if not query:
        return "", "missing"
    execution_plan = metadata.get("mcpExecutionPlan")
    execution_plan = execution_plan if isinstance(execution_plan, dict) else {}
    output_contract = execution_plan.get("output_contract")
    if not isinstance(output_contract, dict):
        output_contract = {
            "file": metadata.get("mcpOutputFile"),
            "columns": metadata.get("mcpRequiredColumns", []),
            "max_rows": metadata.get("mcpMaxRows"),
        }
    target_url = str(metadata.get("mcpTargetUrl") or "").strip()
    return rpa_request_fingerprint(query, target_url=target_url, output_contract=output_contract), "derived"


def find_rpa_project_by_fingerprint(paths: ShizaiPaths, fingerprint: str) -> dict[str, Any] | None:
    """Find the newest local native application for a logical RPA request."""
    wanted = str(fingerprint or "").strip()
    if not wanted:
        return None
    matches: list[tuple[float, Path, dict[str, Any], str]] = []
    for candidate in iter_application_dirs(paths):
        ok, metadata, _ = _safe_json(candidate / "szrpa.json")
        if not ok or not isinstance(metadata, dict):
            continue
        stored, source = _metadata_rpa_request_fingerprint(metadata)
        if stored != wanted:
            continue
        try:
            modified = max(
                (item.stat().st_mtime for item in candidate.rglob("*") if item.is_file()),
                default=candidate.stat().st_mtime,
            )
        except OSError:
            modified = 0.0
        matches.append((modified, candidate.resolve(), metadata, source))
    if not matches:
        return None
    _, project_dir, metadata, source = max(matches, key=lambda item: (item[0], str(item[1]).casefold()))
    return {
        "project_ref": str(project_dir),
        "path": str(project_dir),
        "name": str(metadata.get("packageName") or project_dir.name),
        "metadata": metadata,
        "fingerprint": wanted,
        "fingerprint_source": source,
    }


def _project_summary(paths: ShizaiPaths, project_dir: Path) -> dict[str, Any]:
    metadata_ok, metadata, metadata_error = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if isinstance(metadata, dict) else {}
    files = [item for item in project_dir.rglob("*") if item.is_file()]
    json_status: dict[str, dict[str, Any]] = {}
    for name in KNOWN_JSON_FILES:
        ok, value, error = _safe_json(project_dir / name)
        json_status[name] = {
            "exists": (project_dir / name).is_file(),
            "valid": ok,
            "error": error,
            "top_level_type": type(value).__name__ if ok else None,
        }
    package = package_index(paths).get(str(project_dir).casefold(), {})
    return {
        "id": project_dir.name,
        "path": str(project_dir),
        "name": metadata.get("packageName") or package.get("applicationName") or project_dir.name,
        "description": metadata.get("description") or package.get("description") or "",
        "mode": metadata.get("projectMode"),
        "open_mode": metadata.get("openMode"),
        "factory_version": metadata.get("factoryVersion") or package.get("factoryVersion"),
        "runtime_version": metadata.get("runtimeVersion") or package.get("runtimeVersion"),
        "metadata_valid": metadata_ok,
        "metadata_error": metadata_error,
        "file_count": len(files),
        "total_bytes": sum(item.stat().st_size for item in files),
        "has_intellect_data": any(item.name == "intellectData.json" for item in files),
        "json_files": json_status,
        "package": package,
    }


def list_projects(paths: ShizaiPaths) -> list[dict[str, Any]]:
    """List all local application projects with metadata when available."""
    return [_project_summary(paths, path) for path in iter_application_dirs(paths)]


def list_projects_compact(
    paths: ShizaiPaths,
    *,
    query: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Find projects from shallow metadata without recursively inspecting every file."""
    wanted = str(query or "").strip().casefold()
    maximum = min(50, max(1, int(limit)))
    matches: list[tuple[float, dict[str, Any]]] = []
    for project_dir in iter_application_dirs(paths):
        ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
        metadata = metadata if ok and isinstance(metadata, dict) else {}
        summary = {
            "id": project_dir.name,
            "path": str(project_dir),
            "name": metadata.get("packageName") or project_dir.name,
            "description": metadata.get("description") or "",
            "mode": metadata.get("projectMode"),
            "open_mode": metadata.get("openMode"),
            "factory_version": metadata.get("factoryVersion"),
            "runtime_version": metadata.get("runtimeVersion"),
        }
        haystack = " ".join(str(value or "") for value in summary.values()).casefold()
        if wanted and wanted not in haystack:
            continue
        try:
            modified = (project_dir / "szrpa.json").stat().st_mtime
        except OSError:
            modified = 0.0
        matches.append((modified, summary))
    matches.sort(key=lambda item: (item[0], str(item[1]["path"]).casefold()), reverse=True)
    selected = [summary for _, summary in matches[:maximum]]
    return {
        "kind": "shizai_project_list",
        "query": str(query or "").strip(),
        "limit": maximum,
        "count": len(selected),
        "total": len(matches),
        "has_more": len(matches) > maximum,
        "projects": selected,
    }


def create_project(output_dir: str, name: str = "New Shizai Workflow") -> dict[str, Any]:
    """Create a minimal native project in an explicitly selected output directory."""
    project_dir = Path(output_dir).expanduser().resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "isNewProject": True,
        "packageName": name,
        "description": "",
        "projectMode": "visual",
        "version": "0.0.1",
        "openMode": "visual",
    }
    files = {
        "szrpa.json": json.dumps(metadata, ensure_ascii=False, indent=2),
        "elements.json": "{}",
        "project.flow": "",
        "main.sz": "",
    }
    for filename, content in files.items():
        target = project_dir / filename
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    return inspect_project(
        ShizaiPaths(
            data_root=project_dir.parent.parent,
            applications_root=project_dir.parent,
            sqlite_db=project_dir.parent.parent / "sqlite.db",
            run_config=project_dir.parent.parent / "run.config.json",
            install_root=None,
            executable=None,
        ),
        str(project_dir),
    )


def open_project(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Open a project for inspection and return its parsed summary."""
    return inspect_project(paths, project_ref)


def save_project(paths: ShizaiPaths, project_ref: str, output: str) -> dict[str, Any]:
    """Save a project snapshot to a ZIP file without modifying the source project."""
    return export_project(paths, project_ref, output)


def info(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Return the concise project information used by agents before operations."""
    summary = inspect_project(paths, project_ref)
    return {
        "id": summary["id"],
        "path": summary["path"],
        "name": summary["name"],
        "mode": summary["mode"],
        "open_mode": summary["open_mode"],
        "file_count": summary["file_count"],
        "total_bytes": summary["total_bytes"],
    }


def list_profiles(paths: ShizaiPaths) -> list[dict[str, Any]]:
    """List project modes observed in the local project collection."""
    profiles: dict[str, int] = {}
    for item in list_projects(paths):
        mode = item.get("mode") or "unknown"
        profiles[mode] = profiles.get(mode, 0) + 1
    return [{"name": name, "project_count": count} for name, count in sorted(profiles.items())]


def inspect_project(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Return a detailed summary for a project id or path."""
    return _project_summary(paths, resolve_project(paths, project_ref))


def _element_file(paths: ShizaiPaths, project_ref: str) -> tuple[Path, dict[str, Any]]:
    """Resolve and load a native element catalog without losing selector data."""
    project_dir = resolve_project(paths, project_ref)
    element_path = project_dir / "elements.json"
    if not element_path.is_file():
        raise FileNotFoundError(f"elements.json is missing: {element_path}")
    payload = read_json(element_path)
    if not isinstance(payload, dict):
        raise ValueError("elements.json must contain a JSON object")
    if "elementMap" not in payload:
        payload = {"elementMap": {}, "groupMap": {}, "legacy": payload}
    payload.setdefault("elementMap", {})
    payload.setdefault("groupMap", {})
    if not isinstance(payload["elementMap"], dict) or not isinstance(payload["groupMap"], dict):
        raise ValueError("elements.json elementMap and groupMap must be objects")
    return element_path, payload


def _native_element_parent_errors(payload: dict[str, Any]) -> list[str]:
    """Return parent links the designer's element manager cannot materialize.

    The native designer creates ``addChild`` only on element-group models.
    A captured element may be used as a selector scope, but it cannot be the
    persisted parent of another element.  Leaving that distinction unchecked
    makes the client fail while initializing the element catalog.
    """
    element_map = payload.get("elementMap") if isinstance(payload, dict) else None
    group_map = payload.get("groupMap") if isinstance(payload, dict) else None
    if not isinstance(element_map, dict) or not isinstance(group_map, dict):
        return ["native element catalog must contain object elementMap and groupMap"]
    errors: list[str] = []
    for element_id, element in element_map.items():
        if not isinstance(element, dict):
            errors.append(f"elementMap[{element_id}] must be an object")
            continue
        parent_id = str(element.get("parentId") or "").strip()
        if not parent_id:
            continue
        if parent_id in group_map:
            continue
        if parent_id in element_map:
            errors.append(
                f"elementMap[{element_id}].parentId={parent_id} points to an element; parentId must reference groupMap"
            )
        else:
            errors.append(
                f"elementMap[{element_id}].parentId={parent_id} does not reference groupMap"
            )
    return errors


_UNSTABLE_SELECTOR_RE = re.compile(r":nth-(?:child|of-type|last-child|last-of-type)\s*\(", re.IGNORECASE)
_STABLE_PROPERTY_NAMES = {
    "id",
    "name",
    "role",
    "aria-label",
    "aria-labelledby",
    "aria-describedby",
    "placeholder",
    "title",
    "href",
    "alt",
    "innertext",
    "text",
    "data-testid",
    "data-test",
    "data-qa",
    "automation-id",
    "class",
}


def _decode_selector_value(value: Any) -> str:
    """Decode the JSON-string form used by native selector properties."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
            return str(decoded).strip() if decoded is not None else ""
        except json.JSONDecodeError:
            return text.strip('"')
    return text


_SELECTOR_PROPERTY_FIELD_RE = re.compile(r";\s*(?=[A-Za-z_@][A-Za-z0-9_@-]*\s*=)")


def _selector_property_dict(value: Any) -> dict[str, Any] | None:
    """Read a selector property from dict or designer ``@{...}`` text.

    Some designer versions serialize selector properties as strings inside
    ``elements.json`` while newer captures return dictionaries.  Treat both
    representations identically at the MCP boundary.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("@{") and text.endswith("}"):
        text = text[2:-1].strip()
    elif text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    if not text or "name=" not in text:
        return None
    fields = _SELECTOR_PROPERTY_FIELD_RE.split(text)
    result: dict[str, Any] = {}
    for field in fields:
        if "=" not in field:
            continue
        name, raw = field.split("=", 1)
        key = name.strip()
        if not key:
            continue
        result[key] = raw.strip()
    return result if result.get("name") else None


def _native_bool(value: Any, default: bool = False) -> bool:
    """Decode the designer's string/JSON boolean conventions."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _decode_selector_value(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", "none", "null", ""}:
        return False
    return default


def _native_designated_object(value: Any, default: str = "网页对象") -> str:
    """Normalize the browser-object handoff used by element actions.

    Element actions following ``WebBrowser_CreateV3`` must receive the
    browser handle produced by that component.  ``None`` is only valid when a
    caller explicitly opts out for a component that does not need a browser
    object; it is not a useful default for a generated browser flow.
    """
    if value is None:
        return default
    if isinstance(value, dict) and set(value) in ({"variable"}, {"expression"}, {"literal"}):
        return _generic_native_value(value)
    text = str(value).strip()
    if not text:
        return default
    if text.casefold() in {"none", "null"}:
        # Native web flows use the literal Python None to resolve the current
        # Chrome page. Keep an explicit opt-out instead of silently replacing
        # it with the CreateV3 output handle.
        return "None"
    return text


def _selector_css(element: dict[str, Any]) -> str:
    common = element.get("selector", {}).get("commonProps", {})
    value = common.get("cssSelector", "") if isinstance(common, dict) else ""
    return _decode_selector_value(value)


_NATIVE_SELECTOR_SHELL_NAMES = {
    "window",
    "pane",
    "document",
    "application",
    "desktop",
    "区域",
    "窗口",
    "窗格",
    "文档",
}
_NATIVE_SELECTOR_TAGS = {
    "edit": "input",
    "textarea": "textarea",
    "button": "button",
    "hyperlink": "a",
    "link": "a",
    "image": "img",
    "text": "span",
    "static": "span",
    "label": "span",
    "listitem": "li",
    "row": "tr",
    "cell": "td",
    "group": "div",
}
_NATIVE_SELECTOR_CSS_ATTRIBUTES = {
    "aria-label",
    "aria-labelledby",
    "aria-describedby",
    "placeholder",
    "title",
    "href",
    "alt",
    "role",
    "name",
    "data-testid",
    "data-test",
    "data-qa",
}


def _css_identifier(value: Any) -> str:
    """Return a CSS identifier only when it can be emitted without escaping."""
    text = _decode_selector_value(value)
    return text if re.fullmatch(r"-?[A-Za-z_][A-Za-z0-9_-]*", text) else ""


def _native_selector_css(selector: dict[str, Any]) -> str:
    """Derive a stable CSS hint from a native Chrome/UIA selector.

    DeepCapture's native chain includes a Chrome Window and a renderer Pane.
    The pane automation id is a per-renderer value, so it is useful as a
    fallback but unsuitable as the primary selector after CreateV3 opens or
    reuses a tab.  The web control's stable id/class/ARIA properties are a
    better runtime hint for browser components.
    """
    common = selector.get("commonProps")
    if isinstance(common, dict):
        existing = _decode_selector_value(common.get("cssSelector"))
        if existing:
            # Some capture builds persisted the UIA control name as if it
            # were a DOM tag (for example ``Edit.foo``).  Preserve normal
            # CSS supplied by the designer, but repair this known shape.
            match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)(\..+)$", existing)
            mapped_tag = _NATIVE_SELECTOR_TAGS.get(match.group(1).lower()) if match else None
            if mapped_tag:
                return mapped_tag + match.group(2)
            return existing
    entries = selector.get("selector")
    if not isinstance(entries, list):
        return ""

    candidates: list[tuple[str, list[tuple[str, str]]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_name = str(entry.get("name") or "div").strip()
        name = raw_name.lower()
        item_type = str(entry.get("type") or "").strip().lower()
        if name in _NATIVE_SELECTOR_SHELL_NAMES:
            continue
        if item_type not in {"", "6", "web"} and name not in _NATIVE_SELECTOR_TAGS:
            continue
        # DeepCapture names UIA controls as Edit/Button/Hyperlink.  Those
        # names are control types, not DOM tags; always prefer the explicit
        # web-tag mapping so RuntimeHost receives valid CSS.
        tag = _NATIVE_SELECTOR_TAGS.get(name) or (
            raw_name if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", raw_name) else "div"
        )
        if tag.lower() in _NATIVE_SELECTOR_SHELL_NAMES:
            continue
        properties: list[tuple[str, str]] = []
        for raw_prop in entry.get("props", []):
            prop = _selector_property_dict(raw_prop)
            if prop is None:
                continue
            prop_name = str(prop.get("name") or "").strip().lower()
            value = (
                _selector_property_value(prop)
                if "_selector_property_value" in globals()
                else _decode_selector_value(prop.get("textValue") or prop.get("value"))
            )
            if not prop_name or not value or prop_name == "@index":
                continue
            properties.append((prop_name, value))
        candidates.append((tag, properties))
    if not candidates:
        return ""

    target_tag, target_props = candidates[-1]

    def prop_value(properties: list[tuple[str, str]], name: str) -> str:
        return next((value for key, value in properties if key == name), "")

    # Prefer the target's id. It is the least ambiguous browser selector.
    target_id = _css_identifier(prop_value(target_props, "id"))
    if target_id:
        return f"{target_tag}#{target_id}"

    classes = [token for token in prop_value(target_props, "class").split() if _css_identifier(token)]
    if classes:
        return f"{target_tag}{''.join('.' + token for token in classes)}"

    attributes = [
        (name, value)
        for name, value in target_props
        if name in _NATIVE_SELECTOR_CSS_ATTRIBUTES
        and name != "role"
        and (name != "name" or target_tag.lower() in {"input", "select", "textarea", "button"})
    ]
    if attributes:
        name, value = attributes[0]
        return f"{target_tag}[{name}={json.dumps(value, ensure_ascii=False)}]"

    # A text node often has only @index. Anchor it to the nearest stable
    # ancestor so generated collection/action hints remain useful.
    for ancestor_tag, ancestor_props in reversed(candidates[:-1]):
        ancestor_id = _css_identifier(prop_value(ancestor_props, "id"))
        if ancestor_id:
            return f"{ancestor_tag}#{ancestor_id} {target_tag}"
        ancestor_classes = [token for token in prop_value(ancestor_props, "class").split() if _css_identifier(token)]
        if ancestor_classes:
            return f"{ancestor_tag}{''.join('.' + token for token in ancestor_classes)} {target_tag}"
    return ""


def _ensure_native_selector_css(selector: dict[str, Any]) -> dict[str, Any]:
    """Add a stable CSS hint while preserving the original native selector."""
    normalized = _normalize_native_selector_runtime_values(selector)
    common = normalized.get("commonProps")
    if not isinstance(common, dict):
        return normalized
    common = dict(common)
    css = _native_selector_css(normalized)
    if css and css != _decode_selector_value(common.get("cssSelector")):
        common["cssSelector"] = json.dumps(css, ensure_ascii=False)
        normalized["commonProps"] = common
    return normalized


def _browser_selector_title(value: Any) -> str:
    """Normalize a captured Chrome window title to the DOM tab title."""
    text = " ".join(str(value or "").strip().split())
    lowered = text.casefold()
    for suffix in (" - google chrome", " - chrome", " | google chrome"):
        if lowered.endswith(suffix):
            return text[: -len(suffix)].rstrip()
    return text


def _native_web_selector(element: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
    """Convert a DeepCapture UIA chain to the selector schema used by web actions.

    DeepCapture exposes Chrome through a Windows UIA tree. The designer's
    browser components, however, resolve ``tab`` plus ``type=web`` nodes.
    Keeping the conversion here makes element actions and collection
    configuration consume the same captured target.
    """
    normalized = _ensure_native_selector_css(selector)
    entries = normalized.get("selector")
    if not isinstance(entries, list):
        return normalized
    has_web_entry = any(
        isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "web" for item in entries
    )
    has_uia_entry = any(
        isinstance(item, dict) and str(item.get("type") or "").strip().lower() in {"6", "32"} for item in entries
    )
    gui_type = str(element.get("GUIType") or element.get("gui_type") or "").strip().lower()
    if has_web_entry:
        # A captured web chain often contains a long list of transient
        # ``@index`` ancestors.  If the capture also carries a stable CSS
        # hint, keep only the tab context and the target node.  This matches
        # the compact selector shape accepted by the designer and avoids
        # failing after JD reorders its React containers.
        compact = _compact_web_selector(element, normalized)
        return compact or normalized
    if not has_uia_entry or gui_type not in {"chrome", "browser", "web"}:
        return normalized

    # _collection_selector_chain owns the UIA-to-DOM tag mapping and property
    # filtering. It is resolved at call time after module initialization.
    web_chain = _collection_selector_chain({"selector": normalized})
    if not web_chain:
        return normalized
    title, url = _selector_page_identity({**element, "selector": normalized})
    title = _browser_selector_title(title)
    tab_props: list[dict[str, Any]] = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    selector_items: list[dict[str, Any]] = []
    if tab_props:
        selector_items.append({"name": "tab", "accurate": "true", "props": tab_props})
    selector_items.extend(web_chain)
    result = deepcopy(normalized)
    result["selector"] = selector_items
    return _compact_web_selector(element, result) or result


def _compact_web_selector(element: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any] | None:
    """Build a minimal ``tab > target`` chain from stable target properties."""
    css = _selector_css({"selector": selector})
    token = re.split(r"\s*[>+~]\s*|\s+", css.strip())[-1] if css else ""
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)", token) if token else None
    fallback_tag = "div"
    entries = selector.get("selector") if isinstance(selector, dict) else []
    if isinstance(entries, list):
        for item in reversed(entries):
            if not isinstance(item, dict) or str(item.get("name") or "").lower() == "tab":
                continue
            fallback_tag = str(item.get("name") or "div")
            break
    tag = tag_match.group(1) if tag_match else fallback_tag
    tag = _NATIVE_SELECTOR_TAGS.get(tag.lower(), tag)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", tag):
        tag = "div"
    props = _css_selector_properties(css) if css else []
    if not props and isinstance(entries, list):
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or "").lower()
            if item_name == "tab":
                continue
            for raw_prop in item.get("props", []):
                prop = _selector_property_dict(raw_prop)
                if prop is None:
                    continue
                name = str(prop.get("name") or "").strip().lower()
                value = _selector_property_value(prop)
                if (
                    name == "name"
                    and tag.lower() not in {"input", "select", "textarea", "button"}
                    and (
                        isinstance(element.get("mcpNativeSelector"), dict)
                        or str(element.get("mcpCaptureSource") or "").casefold() == "deep_capture"
                    )
                ):
                    # UIA Name is the accessible text for links and content;
                    # the web plugin calls this selector property innertext.
                    name = "innertext"
                if (
                    (name in _NATIVE_SELECTOR_CSS_ATTRIBUTES or name == "innertext")
                    and value
                    and name != "role"
                    and (name != "name" or tag.lower() in {"input", "select", "textarea", "button", "a"})
                ):
                    normalized = _collection_property(name, value, prop)
                    if normalized:
                        props.append(normalized)
            if props:
                break
    if not props:
        return None
    title, url = _selector_page_identity({**element, "selector": selector})
    title = _browser_selector_title(title)
    tab_props: list[dict[str, Any]] = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    compact = deepcopy(selector)
    compact["selector"] = ([{"name": "tab", "accurate": "true", "props": tab_props}] if tab_props else []) + [
        {"name": tag, "type": "web", "accurate": "true", "props": props}
    ]
    return compact


def _native_selector_expression(element_id: str, element: dict[str, Any]) -> str:
    """Select the runtime expression matching the imported selector format."""
    selector = element.get("selector") if isinstance(element, dict) else None
    entries = selector.get("selector") if isinstance(selector, dict) else None
    is_web = (
        str(element.get("mcpSelectorMode") or "").strip().lower() == "web"
        or isinstance(entries, list)
        and any(isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "web" for item in entries)
    )
    ref = f'SHIZAI_ELEMENT_DICT[{json.dumps(str(element_id), ensure_ascii=False)}]["selector"]'
    return ref if is_web else f"elementsFormatNew({ref})"


def _native_page_selector_expression(
    element_id: str,
    element: dict[str, Any] | None = None,
) -> str:
    """Render a pagination control in DataCollectionV5's formatter contract."""
    ref = f'SHIZAI_ELEMENT_DICT[{json.dumps(str(element_id), ensure_ascii=False)}]["selector"]'
    return f"elementsFormatNew({ref})"


def _materialize_collection_paging_selectors(
    paths: ShizaiPaths,
    project_ref: str,
    config: dict[str, Any],
    *,
    pending_elements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve captured pagination element ids to native page selectors."""
    resolved = dict(config)
    if pending_elements is not None:
        elements = pending_elements
    else:
        try:
            _, elements = _element_file(paths, project_ref)
        except (FileNotFoundError, OSError, ValueError):
            return resolved
    element_map = elements.get("elementMap") if isinstance(elements, dict) else None
    if not isinstance(element_map, dict):
        return resolved
    for selector_name in ("selector_page", "selector_more"):
        selector_value = str(resolved.get(selector_name) or "").strip()
        if not selector_value:
            continue
        if selector_value == MCP_PAGE_SCOPE_ELEMENT_ID:
            page_scope = element_map.get(selector_value)
            if isinstance(page_scope, dict):
                resolved[selector_name] = _native_page_selector_expression(selector_value, page_scope)
        elif (
            not selector_value.startswith("elementsFormatNew(")
            and not selector_value.startswith("SHIZAI_ELEMENT_DICT[")
            and selector_value in element_map
        ):
            page_scope_id = _upsert_page_scope_element(
                paths,
                project_ref,
                selector_value,
                pending_elements=pending_elements,
            )
            resolved[selector_name] = _native_page_selector_expression(
                page_scope_id,
                element_map.get(page_scope_id),
            )
    return resolved


def _collection_paging_settings(config: dict[str, Any] | None = None) -> dict[str, str]:
    """Return the conditional paging arguments for DataCollectionV5.

    The designer exposes ``selector_page`` and ``selector_more`` only for
    paginated collection modes.  A page-scope selector is not a substitute
    for either control.  Keeping this normalization in one place prevents
    task creation, materialization, and runtime repair from disagreeing.
    """
    source = config if isinstance(config, dict) else {}

    def decoded(name: str, default: str, *aliases: str) -> str:
        # Native project metadata uses mcp-prefixed names, while component
        # configs use the shorter argument names. Prefer an explicit
        # component value, then use metadata aliases before falling back to
        # the template default. This prevents materialization from restoring
        # 1-page/20-row defaults after rpa-update-step wrote 100 rows.
        value = source.get(name)
        if value in (None, ""):
            for alias in aliases:
                value = source.get(alias)
                if value not in (None, ""):
                    break
        if value in (None, ""):
            value = default
        if value is None:
            return default
        result = _decode_selector_value(value)
        if result is None:
            return default
        return str(result).strip()

    def selector(name: str) -> str:
        value = decoded(name, "")
        if value.casefold() in {"none", "null", '""', "''"}:
            return ""
        return value

    region = decoded("region", "1", "mcpCollectionRegion")
    collect_items = decoded("collect_items", "20", "mcpMaxRows")
    page_type = decoded("page_type", "1", "mcpCollectionPageType")
    page_type_num = decoded("page_type_num", "0", "mcpCollectionPageTypeNum")
    settings = {
        "region": region,
        "collect_pages": decoded("collect_pages", "1"),
        "collect_items": collect_items,
        "page_type": page_type,
        "page_type_num": page_type_num,
        "selector_page": selector("selector_page"),
        "selector_more": selector("selector_more"),
    }
    if region == "3" and page_type_num == "0":
        # Older native tasks used 0 as an unspecified page mode. Once a live
        # next-page/load-more selector is present, infer the corresponding
        # mode so count-limited collection actually paginates at runtime.
        if settings["selector_page"]:
            settings["page_type_num"] = "1"
        elif settings["selector_more"]:
            settings["page_type_num"] = "2"
    if region == "1":
        # In the native schema these inputs are hidden for the current-page
        # mode. Explicit empty strings are important: JSON null reaches
        # RuntimeHost as an invalid selector value.
        settings["collect_pages"] = "1"
        settings["page_type_num"] = "0"
        settings["selector_page"] = ""
        settings["selector_more"] = ""
    return settings


def _collection_pagination_capability(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe native pagination without turning it into a global requirement."""
    settings = _collection_paging_settings(config)
    region = settings["region"]
    selector_page = bool(settings["selector_page"])
    selector_more = bool(settings["selector_more"])
    if region == "1":
        selected_mode = "single-page"
        selector_name = None
        configured = True
    elif region in {"0", "2"}:
        selected_mode = "next-page" if settings["page_type"] == "1" else "load-more"
        selector_name = "selector_page" if selected_mode == "next-page" else "selector_more"
        configured = selector_page if selected_mode == "next-page" else selector_more
    elif settings["page_type_num"] == "1":
        selected_mode = "next-page"
        selector_name = "selector_page"
        configured = selector_page
    elif settings["page_type_num"] == "2":
        selected_mode = "load-more"
        selector_name = "selector_more"
        configured = selector_more
    else:
        selected_mode = "unspecified"
        selector_name = None
        configured = False
    return {
        "available": True,
        "component": "WebBrowser_DataCollectionV5",
        "decision": "model_decides",
        "supported_modes": ["single-page", "next-page", "load-more"],
        "selected_mode": selected_mode,
        "selector_argument": selector_name,
        "selector_bound": configured if selector_name else False,
        "configuration_valid": not (selector_page and selector_more),
        "settings": settings,
        "message": (
            "原生组件支持下一页/加载更多；是否启用由模型根据页面证据和输出契约决定。"
            if selected_mode in {"single-page", "unspecified"}
            else f"已选择原生 {selected_mode}，需要保持 {selector_name} 绑定。"
        ),
    }


def _apply_collection_paging_settings(node: dict[str, Any], settings: dict[str, str]) -> None:
    """Write normalized paging arguments onto a native collection node."""
    for name in (
        "region",
        "collect_pages",
        "collect_items",
        "page_type",
        "page_type_num",
        "selector_page",
        "selector_more",
    ):
        if name in settings:
            _native_set_argument(node, name, settings[name])


def _native_collection_config(node: dict[str, Any]) -> dict[str, Any]:
    """Read a DataCollectionV5 config object without exposing malformed JSON."""
    value = _native_task_argument(node, "config", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = {}
    return deepcopy(value) if isinstance(value, dict) else {}


def _sync_native_collection_config(
    node: dict[str, Any],
    native_elements: dict[str, Any],
    *,
    element_ids: list[str] | None = None,
    columns: list[str] | None = None,
    data_attributes: dict[str, Any] | None = None,
    keyword: str = "",
    config_value: dict[str, Any] | None = None,
    page_url: str | None = None,
    page_title: str | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """Synchronize every nested field RuntimeHost reads for DataCollectionV5.

    The public MCP config is semantic, while RuntimeHost consumes the nested
    ``pageInfo`` and ``collectionConfig`` object.  Keeping this conversion in
    one place prevents a top-level update from leaving stale selectors,
    column attributes, or page identity behind.
    """
    base = deepcopy(config_value) if isinstance(config_value, dict) else _native_collection_config(node)
    existing_collection = base.get("collectionConfig")
    existing_collection = deepcopy(existing_collection) if isinstance(existing_collection, dict) else {}
    existing_columns = existing_collection.get("columnConfigArray")
    existing_columns = existing_columns if isinstance(existing_columns, list) else []
    existing_selectors = existing_collection.get("columnSelectorArray")
    existing_selectors = existing_selectors if isinstance(existing_selectors, list) else []

    requested_columns = [str(item).strip() for item in (columns or []) if str(item).strip()]
    if not requested_columns:
        requested_columns = [
            str(item.get("columnHeader") or "").strip()
            for item in existing_columns
            if isinstance(item, dict) and str(item.get("columnHeader") or "").strip()
        ]
    if not requested_columns:
        requested_columns = list(DEFAULT_RPA_COLUMNS)

    declared_ids = [str(item).strip() for item in (element_ids or []) if str(item).strip()]
    if not declared_ids:
        raw_declared = node.get("mcpElementIds")
        if isinstance(raw_declared, list):
            declared_ids = [str(item).strip() for item in raw_declared if str(item).strip()]
    element_map = native_elements.get("elementMap") if isinstance(native_elements, dict) else {}
    element_map = element_map if isinstance(element_map, dict) else {}

    # Prefer captured selectors whenever the node has a complete imported
    # binding. This updates both page identity and every nested column field.
    generated, generated_count = (None, 0)
    if (
        declared_ids
        and len(requested_columns) == len(declared_ids)
        and all(item in element_map for item in declared_ids)
    ):
        generated, generated_count = _staged_collection_config(
            native_elements,
            declared_ids,
            keyword=keyword,
            columns=requested_columns,
            data_attributes=data_attributes,
            fallback_page_url=page_url,
            fallback_page_title=page_title,
        )
    if generated is not None and generated_count == len(declared_ids):
        generated_collection = generated.get("collectionConfig") or {}
        existing_collection.update(deepcopy(generated_collection))
        generated_page = generated.get("pageInfo")
        if isinstance(generated_page, dict):
            page_info = deepcopy(base.get("pageInfo")) if isinstance(base.get("pageInfo"), dict) else {}
            page_info.update(
                {
                    key: value
                    for key, value in generated_page.items()
                    if value not in (None, "") or key not in {"windowTitle", "pageUrl"}
                }
            )
            base["pageInfo"] = page_info
    else:
        # No fresh capture is available. Keep existing selectors, but make
        # column metadata explicit and align the arrays so validation can
        # report an unbound/missing selector instead of using stale indexes.
        normalized_columns: list[dict[str, Any]] = []
        source_attributes = data_attributes if isinstance(data_attributes, dict) else {}
        for index, header in enumerate(requested_columns):
            old = existing_columns[index] if index < len(existing_columns) and isinstance(existing_columns[index], dict) else {}
            item = deepcopy(old)
            item["columnHeader"] = header
            item["columnHeader_bk"] = header
            configured = str(source_attributes.get(header) or "").strip()
            if not configured:
                configured = str(item.get("dataAttrName") or "").strip()
            item["dataAttrName"] = configured or ("href" if header in _COLLECTION_LINK_HEADERS else "innerText")
            normalized_columns.append(item)
        existing_collection["columnConfigArray"] = normalized_columns
        existing_collection["columnSelectorArray"] = existing_selectors[: len(requested_columns)] + [
            [] for _ in range(max(0, len(requested_columns) - len(existing_selectors)))
        ]

    # A caller may intentionally correct page identity without recapturing
    # elements. Support both semantic aliases and the native pageInfo shape.
    page_info = base.get("pageInfo")
    page_info = deepcopy(page_info) if isinstance(page_info, dict) else {}
    if page_url not in (None, ""):
        page_info["pageUrl"] = str(page_url).strip()
    if page_title not in (None, ""):
        page_info["windowTitle"] = str(page_title).strip()
    if page_info:
        base["pageInfo"] = page_info

    # Always synchronize headers/attributes after generated config or an
    # explicit nested config update. This also handles extra fields supplied
    # by a newer designer version without discarding them.
    nested_columns = existing_collection.get("columnConfigArray")
    nested_columns = nested_columns if isinstance(nested_columns, list) else []
    attr_map: dict[str, str] = {}
    for index, header in enumerate(requested_columns):
        if index >= len(nested_columns) or not isinstance(nested_columns[index], dict):
            continue
        item = nested_columns[index]
        item["columnHeader"] = header
        item["columnHeader_bk"] = header
        configured = str((data_attributes or {}).get(header) or "").strip() if isinstance(data_attributes, dict) else ""
        if configured:
            item["dataAttrName"] = configured
        if not str(item.get("dataAttrName") or "").strip():
            item["dataAttrName"] = "href" if header in _COLLECTION_LINK_HEADERS else "innerText"
        attr_map[header] = str(item.get("dataAttrName") or "")
    existing_collection["columnConfigArray"] = nested_columns
    if "columnSelectorArray" in existing_collection:
        selectors = existing_collection.get("columnSelectorArray")
        if isinstance(selectors, list) and len(selectors) != len(requested_columns):
            existing_collection["columnSelectorArray"] = selectors[: len(requested_columns)] + [
                [] for _ in range(max(0, len(requested_columns) - len(selectors)))
            ]
    base["collectionConfig"] = existing_collection
    return base, requested_columns, attr_map


def _selector_page_identity(element: dict[str, Any]) -> tuple[str, str]:
    """Read page URL/title from simplified or native capture records."""
    fallback_title = str(element.get("title") or element.get("page_title") or "").strip()
    fallback_url = str(element.get("url") or element.get("page_url") or "").strip()

    # The selector belongs to the exact captured element, whereas the stored
    # page_title/page_url may come from a window scan. The latter can be a
    # transient Chrome restore prompt when several windows exist. Read native
    # selector identity first and use scan metadata only as a fallback.
    for selector_name in ("mcpNativeSelector", "selector"):
        selector = element.get(selector_name)
        if not isinstance(selector, dict):
            continue
        selector_title = ""
        selector_url = ""
        detail = selector.get("elementDetail")
        detail_title = (
            str(detail.get("windowName") or detail.get("categoryName") or "").strip()
            if isinstance(detail, dict)
            else ""
        )
        for entry in selector.get("selector", []):
            if not isinstance(entry, dict) or str(entry.get("name", "")).lower() not in {
                "tab",
                "window",
                "document",
            }:
                continue
            for raw_prop in entry.get("props", []):
                prop = _selector_property_dict(raw_prop)
                if prop is None:
                    continue
                name = str(prop.get("name", "")).lower()
                value = _decode_selector_value(prop.get("textValue") or prop.get("value"))
                if name in {"title", "name"} and value and not selector_title:
                    selector_title = value
                elif name == "url" and value and not selector_url:
                    selector_url = value
        if selector_title or selector_url:
            return selector_title or detail_title or fallback_title, selector_url or fallback_url
        if detail_title:
            fallback_title = detail_title
    return fallback_title, fallback_url


def _selector_property_names(element: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return all selector property names and the subset that is stable enough for reuse."""
    names: set[str] = set()
    stable: set[str] = set()
    selector = element.get("selector")
    if isinstance(selector, dict):
        entries = selector.get("selector", [])
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for raw_prop in entry.get("props", []):
                    prop = _selector_property_dict(raw_prop)
                    if prop is None:
                        continue
                    name = str(prop.get("name", "")).strip().lower()
                    if not name or name == "@index":
                        continue
                    names.add(name)
                    value = _decode_selector_value(prop.get("textValue") or prop.get("value"))
                    if name in _STABLE_PROPERTY_NAMES or name.startswith("data-"):
                        if value:
                            stable.add(name)
    attributes = element.get("attributes")
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            name = str(key).strip().lower()
            if not name:
                continue
            names.add(name)
            if value not in (None, "") and (name in _STABLE_PROPERTY_NAMES or name.startswith("data-")):
                stable.add(name)
    for key in ("role", "placeholder", "text", "innertext", "aria_label", "test_id"):
        if element.get(key) not in (None, ""):
            normalized = {"aria_label": "aria-label", "test_id": "data-testid"}.get(key, key)
            names.add(normalized)
            stable.add(normalized)
    return names, stable


def _element_diagnostic(element: Any, index: int) -> dict[str, Any]:
    """Validate one browser capture without requiring a live browser connection."""
    if not isinstance(element, dict):
        return {
            "index": index,
            "id": None,
            "valid": False,
            "errors": ["element must be an object"],
            "warnings": [],
            "advisories": [],
            "capture_source": "unknown",
            "verification_status": "unknown",
        }
    element_id = str(element.get("id") or "").strip()
    title, url = _selector_page_identity(element)
    css = (
        _selector_css(element)
        if isinstance(element.get("selector"), dict)
        else _decode_selector_value(element.get("css_selector") or element.get("cssSelector"))
    )
    names, stable_names = _selector_property_names(element)
    has_selector_data = bool(css or stable_names)
    errors: list[str] = []
    warnings: list[str] = []
    advisories: list[str] = []
    if not element_id:
        errors.append("missing id")
    if not title and not url:
        errors.append("missing page title or page URL")
    if not has_selector_data:
        errors.append("missing stable selector; coordinate-only capture is not reusable")
    if ("css_selector" in element or "cssSelector" in element) and not css and not stable_names:
        errors.append("empty CSS selector")
    if css and _UNSTABLE_SELECTOR_RE.search(css):
        if not stable_names:
            errors.append("selector relies only on unstable nth-child indexes")
        else:
            warnings.append("CSS contains nth-child indexing; native attributes will be preferred")
    if names and not stable_names and not css:
        warnings.append("selector properties are present but none are stable attributes")
    if not url:
        advisories.append("page URL is missing; page title and stable selector are used for verification")
    capture_source, verification_status = _element_verification_state(element)
    return {
        "index": index,
        "id": element_id or None,
        "name": str(element.get("name") or "").strip(),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "advisories": advisories,
        "page_title": title,
        "page_url": url,
        "css_selector": css,
        "selector_properties": sorted(names),
        "stable_properties": sorted(stable_names),
        "has_stable_selector": has_selector_data
        and not (css and not stable_names and _UNSTABLE_SELECTOR_RE.search(css)),
        "capture_source": capture_source,
        "verification_status": verification_status,
    }


def _element_verification_state(element: Any) -> tuple[str, str]:
    """Return the provenance gate for an imported element.

    Missing provenance is intentionally treated as legacy rather than invalid
    so existing projects remain runnable. New model/JSON imports are explicitly
    tagged as unverified and are blocked by flow validation until recaptured.
    """
    if not isinstance(element, dict):
        return "unknown", "unknown"
    source = str(element.get("mcpCaptureSource") or "").strip().casefold()
    status = str(element.get("mcpVerificationStatus") or "").strip().casefold()
    if status in {"verified", "unverified"}:
        return source or "unknown", status
    if source == "deep_capture":
        return source, "verified"
    if source == "model_candidate":
        return source, "unverified"
    return source or "legacy", "legacy"


def validate_captured_elements(elements: list[dict[str, Any]], *, strict: bool = False) -> dict[str, Any]:
    """Validate model-prepared browser captures before writing native elements."""
    if not isinstance(elements, list) or not elements:
        raise ValueError("elements must contain at least one captured element")
    diagnostics = [_element_diagnostic(item, index) for index, item in enumerate(elements)]
    errors = [f"elements[{item['index']}]: {error}" for item in diagnostics for error in item["errors"]]
    warnings = [f"elements[{item['index']}]: {warning}" for item in diagnostics for warning in item["warnings"]]
    advisories = [f"elements[{item['index']}]: {advisory}" for item in diagnostics for advisory in item["advisories"]]
    duplicate_ids = [
        item_id
        for item_id in sorted({item["id"] for item in diagnostics if item["id"]})
        if sum(entry["id"] == item_id for entry in diagnostics) > 1
    ]
    errors.extend(f"duplicate element id: {item_id}" for item_id in duplicate_ids)
    return {
        "valid": not errors and (not strict or not warnings),
        "strict": strict,
        "element_count": len(diagnostics),
        "stable_element_count": sum(1 for item in diagnostics if item["has_stable_selector"]),
        "errors": errors,
        "warnings": warnings,
        "advisories": advisories,
        "verified_element_count": sum(1 for item in diagnostics if item["verification_status"] == "verified"),
        "unverified_element_count": sum(1 for item in diagnostics if item["verification_status"] == "unverified"),
        "capture_sources": sorted({item["capture_source"] for item in diagnostics if item["capture_source"]}),
        "elements": diagnostics,
    }


def read_elements(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Return agent-friendly element summaries plus the native selector payload."""
    element_path, payload = _element_file(paths, project_ref)
    summaries = []
    for element_id, element in payload["elementMap"].items():
        if not isinstance(element, dict):
            continue
        selector = element.get("selector") if isinstance(element.get("selector"), dict) else {}
        common = selector.get("commonProps") if isinstance(selector.get("commonProps"), dict) else {}
        detail = selector.get("elementDetail") if isinstance(selector.get("elementDetail"), dict) else {}
        page_title, page_url = _selector_page_identity(element)
        summaries.append(
            {
                "id": str(element.get("id") or element_id),
                "name": element.get("name", ""),
                "gui_type": element.get("GUIType") or common.get("GUIType", ""),
                "category": element.get("categoryName") or detail.get("categoryName", ""),
                "css_selector": _selector_css(element),
                "selector_type": element.get("selectorType", ""),
                "parent_id": element.get("parentId"),
                "page_title": page_title,
                "page_url": page_url,
                "capture_source": _element_verification_state(element)[0],
                "verification_status": _element_verification_state(element)[1],
            }
        )
    return {
        "project": str(resolve_project(paths, project_ref)),
        "elements_file": str(element_path),
        "element_count": len(summaries),
        "group_count": len(payload["groupMap"]),
        "verified_element_count": sum(
            1 for element in payload["elementMap"].values() if _element_verification_state(element)[1] == "verified"
        ),
        "unverified_element_count": sum(
            1 for element in payload["elementMap"].values() if _element_verification_state(element)[1] == "unverified"
        ),
        "elements": summaries,
        "native": payload,
    }


def _normalize_native_selector_runtime_values(selector: Any) -> Any:
    """Normalize DeepCapture properties to the RuntimeHost expression form.

    DeepCapture versions differ: some emit a literal string in ``value`` with
    an empty ``expressionValue`` while the native runtime evaluates ``value``
    as Python.  That makes values such as ``chrome`` look like variable names
    and raises ``NameError`` during Element.SetValue.  The designer's own
    persisted projects store string literals as JSON expressions and numeric
    UIA fields as bare expressions.  Normalize only the incomplete form and
    leave already-runtime-ready selectors unchanged.
    """
    if not isinstance(selector, dict):
        return selector

    normalized = deepcopy(selector)
    selector_items = normalized.get("selector")
    if not isinstance(selector_items, list):
        return normalized

    numeric_names = {
        "@index",
        "automationid",
        "processid",
        "handle",
        "hwnd",
        "windowhandle",
        "controltype",
    }
    for item in selector_items:
        if not isinstance(item, dict) or not isinstance(item.get("props"), list):
            continue
        item_name = str(item.get("name") or "").strip().lower()
        if item_name == "pane":
            pane_values = {
                str(prop.get("name") or "").strip().lower(): _decode_selector_value(
                    prop.get("textValue") if prop.get("textValue") not in (None, "") else prop.get("value")
                )
                for prop in item["props"]
                if isinstance(prop, dict)
            }
            renderer_marker = (
                "chrome_renderwidgethosthwnd" in pane_values.get("class", "").casefold()
                or "chrome legacy window" in pane_values.get("title", "").casefold()
            )
            if renderer_marker:
                # Chrome creates a new UIA renderer Pane id after navigation
                # or tab reuse. Keeping it makes an otherwise valid web
                # selector fail even when the current Document is visible.
                item["props"] = [
                    prop
                    for prop in item["props"]
                    if not isinstance(prop, dict) or str(prop.get("name") or "").strip().lower() != "automationid"
                ]
        for prop in item["props"]:
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or "").strip().lower()
            raw_value = prop.get("value")
            if raw_value is None:
                continue
            if name == "@index":
                # RuntimeHost evaluates selector values even when an
                # expressionValue is present. Designer-created web selectors
                # store this field as a string, while DeepCapture may emit a
                # JSON number.
                text_value = _decode_selector_value(raw_value)
                prop["value"] = text_value
                if prop.get("expressionValue") in (None, ""):
                    prop["expressionValue"] = text_value
                continue
            # A non-empty expressionValue is already authoritative. This
            # preserves variables and selectors written by the designer.
            expression = prop.get("expressionValue")
            if expression not in (None, ""):
                continue
            text_value = _decode_selector_value(raw_value)
            if name in numeric_names:
                prop["value"] = text_value
                prop["expressionValue"] = text_value
                continue
            prop["value"] = json.dumps(text_value, ensure_ascii=False)
            prop["expressionValue"] = json.dumps(text_value, ensure_ascii=False)
    return normalized


def _normalize_native_element_catalog(root: Path) -> list[str]:
    """Repair historical native element selectors in one project directory."""
    element_path = root / "elements.json"
    if not element_path.is_file():
        return []
    try:
        payload = read_json(element_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("elementMap"), dict):
        return []
    changed: list[str] = []
    for element_id, element in payload["elementMap"].items():
        if not isinstance(element, dict) or not isinstance(element.get("selector"), dict):
            continue
        if element.get("mcpPageScope") is True:
            # DataCollectionV5 passes this selector through elementsFormatNew,
            # so its values must remain quoted Python expressions.
            continue
        original = element["selector"]
        normalized = _ensure_native_selector_css(original)
        web_selector = _native_web_selector(element, normalized)
        if web_selector != original:
            if web_selector.get("selector") != normalized.get("selector"):
                # Preserve the first DeepCapture selector. Replacing it with
                # an already-converted web chain loses Table/DataItem row and
                # cell semantics on every later materialization.
                if not isinstance(element.get("mcpNativeSelector"), dict):
                    element["mcpNativeSelector"] = normalized
                element["mcpSelectorMode"] = "web"
            element["selector"] = web_selector
            changed.append(str(element_id))
    if not changed:
        return changed
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(element_path)
    return changed


def _native_element(
    item: dict[str, Any],
    element_id: str,
    *,
    capture_source: str = "legacy",
) -> dict[str, Any]:
    """Convert a browser-capture record into the designer's comboSelector shape."""
    selector = item.get("selector")
    if isinstance(selector, dict) and selector.get("commonProps"):
        result = dict(item)
        normalized = _ensure_native_selector_css(selector)
        web_selector = _native_web_selector(result, normalized)
        if web_selector.get("selector") != normalized.get("selector"):
            # Keep the exact extension payload for diagnostics and future
            # recapture decisions while exposing the runtime-ready web chain
            # through the field consumed by elementsFormatNew.
            result["mcpNativeSelector"] = normalized
            result["mcpSelectorMode"] = "web"
        result["selector"] = web_selector
        result.setdefault("id", element_id)
        result.setdefault("selectorType", "comboSelector")
        result.setdefault("modelType", "Element")
        result.setdefault("GUIType", item.get("gui_type", "Chrome"))
        if capture_source in {"model_candidate", "deep_capture", "browser_bridge"}:
            result["mcpCaptureSource"] = capture_source
            result["mcpVerificationStatus"] = "unverified" if capture_source == "model_candidate" else "verified"
        return result
    gui_type = str(item.get("gui_type") or item.get("GUIType") or "Chrome")
    title = str(item.get("title") or item.get("page_title") or "")
    url = str(item.get("url") or "")
    props = []
    attributes = dict(item.get("attributes", {})) if isinstance(item.get("attributes"), dict) else {}
    for field, native_name in (
        ("role", "role"),
        ("placeholder", "placeholder"),
        ("text", "innertext"),
        ("innertext", "innertext"),
        ("aria_label", "aria-label"),
        ("test_id", "data-testid"),
    ):
        if item.get(field) not in (None, ""):
            attributes.setdefault(native_name, item[field])
    for name, value in attributes.items():
        props.append(
            {
                "name": str(name),
                "value": json.dumps(str(value), ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": str(value),
                "expressionValue": json.dumps(str(value), ensure_ascii=False),
            }
        )
    tag = str(item.get("tag") or "div")
    web_props = list(props)
    css = str(item.get("css_selector") or item.get("cssSelector") or "")
    common = {"GUIType": gui_type, "cssSelector": json.dumps(css, ensure_ascii=False), "processName": "chrome.exe"}
    tab_props = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    selector_items = []
    if tab_props:
        selector_items.append({"name": "tab", "accurate": "true", "props": tab_props})
    bridge_chain = _collection_chain_from_css(css) if capture_source == "browser_bridge" else []
    if bridge_chain:
        for part in bridge_chain:
            part["accurate"] = "true"
            for prop in part.get("props", []):
                if isinstance(prop, dict):
                    prop["accurate"] = "true"
        selector_items.extend(bridge_chain)
    else:
        selector_items.append({"name": tag, "type": "web", "accurate": "true", "props": web_props})
    result = {
        "id": element_id,
        "name": str(item.get("name") or element_id),
        "tag": 2,
        "type": "normal",
        "GUIType": gui_type,
        "categoryName": title,
        "modelType": "Element",
        "selector": {"commonProps": common, "selector": selector_items},
        "selectorType": "comboSelector",
        "selectorParams": {"elementTypes": ["normal", "cv"], "comboSelectType": 0, "isAutoSimilar": 0},
        "selectorVariableNames": [],
        "parentId": item.get("parent_id"),
        "card_context": item.get("card_context"),
        "link_context": item.get("link_context"),
    }
    if capture_source in {"model_candidate", "deep_capture", "browser_bridge"}:
        result["mcpCaptureSource"] = capture_source
        result["mcpVerificationStatus"] = "unverified" if capture_source == "model_candidate" else "verified"
    if capture_source == "browser_bridge" and isinstance(item.get("mcpBrowserEvidence"), dict):
        result["mcpBrowserEvidence"] = dict(item["mcpBrowserEvidence"])
    return result


def _bound_element_ids_in_project(root: Path) -> set[str]:
    """Find element ids currently referenced by staged or native flows."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_name = str(key).casefold()
                if key_name in {"element_ids", "mcpelementids"} and isinstance(item, list):
                    found.update(str(entry).strip() for entry in item if str(entry).strip())
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            found.update(_NATIVE_ELEMENT_RE.findall(value))

    staged_path = root / RPA_FLOW_FILE
    if staged_path.is_file():
        try:
            visit(read_json(staged_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    for task_path in root.rglob("*.task"):
        try:
            visit(read_json(task_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return found


def _unverified_element_ids(paths: ShizaiPaths, project_ref: str, element_ids: Iterable[str]) -> list[str]:
    """Return explicitly model-supplied element ids among a flow binding."""
    try:
        _, payload = _element_file(paths, project_ref)
    except (FileNotFoundError, OSError, ValueError):
        return []
    element_map = payload.get("elementMap") if isinstance(payload, dict) else {}
    if not isinstance(element_map, dict):
        return []
    return sorted(
        {
            str(element_id).strip()
            for element_id in element_ids
            if str(element_id).strip()
            and _element_verification_state(element_map.get(str(element_id).strip()))[1] == "unverified"
        }
    )


def import_elements(
    paths: ShizaiPaths,
    project_ref: str,
    elements: list[dict[str, Any]],
    *,
    replace: bool = False,
    force_replace: bool = False,
    capture_source: str = "legacy",
) -> dict[str, Any]:
    """Merge browser-captured elements into a native elements.json file."""
    if not elements:
        raise ValueError("elements must contain at least one captured element")
    capture_source = str(capture_source or "legacy").strip().casefold()
    if capture_source not in {"legacy", "model_candidate", "deep_capture", "browser_bridge"}:
        raise ValueError("capture_source must be legacy, model_candidate, deep_capture, or browser_bridge")
    _publish_rpa_ui(
        "element_importing",
        "element_import",
        f"正在把 {len(elements)} 个已捕获网页元素写入流程。",
        animation="element_import",
        flow_ref=str(project_ref),
        element_count=len(elements),
    )
    capture_validation = validate_captured_elements(elements)
    if not capture_validation["valid"]:
        raise ValueError("captured elements failed validation: " + "; ".join(capture_validation["errors"]))
    capture_validation["capture_source"] = capture_source
    capture_validation["verification_status"] = (
        "verified"
        if capture_source in {"deep_capture", "browser_bridge"}
        else "unverified"
        if capture_source == "model_candidate"
        else "legacy"
    )
    capture_validation["verified_element_count"] = (
        len(elements) if capture_source in {"deep_capture", "browser_bridge"} else 0
    )
    capture_validation["unverified_element_count"] = len(elements) if capture_source == "model_candidate" else 0
    element_path, payload = _element_file(paths, project_ref)
    bound_ids = _bound_element_ids_in_project(element_path.parent) if replace else set()
    if replace and bound_ids and not force_replace:
        bound_preview = ", ".join(sorted(bound_ids)[:8])
        if len(bound_ids) > 8:
            bound_preview += ", ..."
        raise ValueError(
            "replace would clear elements already referenced by the flow "
            f"({bound_preview}); use replace=false to merge/update by id, "
            "or pass force_replace=true only when rebuilding every binding"
        )
    if replace:
        payload["elementMap"] = {}
    imported = []
    for index, item in enumerate(elements):
        if not isinstance(item, dict):
            raise ValueError(f"elements[{index}] must be an object")
        element_id = str(item.get("id") or f"mcp_element_{index + 1}")
        native = _native_element(item, element_id, capture_source=capture_source)
        payload["elementMap"][element_id] = native
        imported.append(element_id)
    element_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Keep synthetic selectors aligned with the latest capture context. A JS
    # link step may have been created before the page URL was known; refreshing
    # it here makes strict project validation truthful after re-capture.
    generated = payload["elementMap"].get(JS_LINK_ELEMENT_ID)
    if isinstance(generated, dict):
        root_id = str(generated.get("parentId") or "").strip()
        root = payload["elementMap"].get(root_id) if root_id else None
        if isinstance(root, dict) and (root.get("page_url") or root.get("page_title")):
            _upsert_js_generated_elements(paths, str(element_path.parent), root_id)
    staged_flow: dict[str, Any] | None = None
    staged_flow_path = element_path.parent / RPA_FLOW_FILE
    if staged_flow_path.is_file():
        try:
            staged_flow = read_json(staged_flow_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            staged_flow = None
        if isinstance(staged_flow, dict) and staged_flow.get("kind") == "shizai-rpa-draft":
            staged_flow["element_count"] = len(payload["elementMap"])
            if capture_source == "model_candidate":
                staged_flow["status"] = "elements_draft"
                staged_flow["phase"] = "elements_draft"
                staged_flow["next_tool"] = "shizai_rpa_validate_elements"
            else:
                staged_flow["status"] = "elements_captured"
                staged_flow["phase"] = "elements_captured"
                if len(staged_flow.get("steps", [])) == 1:
                    staged_flow["next_tool"] = "shizai_rpa_add_step"
            _write_rpa_flow(staged_flow_path, staged_flow)
    candidate_draft = capture_source == "model_candidate"
    ui_feedback = _publish_rpa_ui(
        "waiting" if candidate_draft else "elements_ready",
        "element_import",
        (
            f"已导入 {len(imported)} 个候选元素，当前仅保存为草稿；必须使用 DeepCapture 重新捕获并验证后才能运行。"
            if capture_source == "model_candidate"
            else f"已导入 {len(imported)} 个网页元素，可以绑定到下一组件。"
        ),
        animation="capture_needs_review" if candidate_draft else "element_ready",
        flow_ref=str(element_path.parent),
        element_count=len(payload["elementMap"]),
        next_tool="shizai_rpa_validate_elements" if capture_source == "model_candidate" else "shizai_rpa_add_step",
    )
    return {
        "project": str(element_path.parent),
        "elements_file": str(element_path),
        "imported": imported,
        "element_count": len(payload["elementMap"]),
        "replace": replace,
        "force_replace": force_replace,
        "capture_source": capture_source,
        "verification_status": (
            "verified"
            if capture_source in {"deep_capture", "browser_bridge"}
            else "unverified"
            if capture_source == "model_candidate"
            else "legacy"
        ),
        "preserved_bound_element_ids": sorted(bound_ids) if replace and not force_replace else [],
        "staged_flow": {
            "phase": staged_flow.get("phase"),
            "next_tool": staged_flow.get("next_tool"),
        }
        if staged_flow
        else None,
        "validation": capture_validation,
        "ui_feedback": ui_feedback,
    }


def import_verified_reference_elements(
    paths: ShizaiPaths,
    project_ref: str,
    source_project_ref: str,
    element_ids: list[str],
    *,
    replace: bool = False,
    force_replace: bool = False,
) -> dict[str, Any]:
    """Reuse extension-verified elements from another local designer project."""
    target_dir = resolve_project(paths, project_ref)
    source_dir = resolve_project(paths, source_project_ref)
    if target_dir == source_dir:
        raise ValueError("source_project_ref must identify a different local project")

    normalized_ids = [str(item).strip() for item in element_ids if str(item).strip()]
    if not normalized_ids:
        raise ValueError("element_ids must contain at least one id")
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError("element_ids contains duplicates")

    _, source_payload = _element_file(paths, str(source_dir))
    source_map = source_payload.get("elementMap")
    if not isinstance(source_map, dict):
        raise ValueError(f"source project has no native element map: {source_dir}")

    records: list[dict[str, Any]] = []
    for element_id in normalized_ids:
        element = source_map.get(element_id)
        if not isinstance(element, dict):
            raise ValueError(f"verified reference element not found: {element_id}")
        capture_source, verification_status = _element_verification_state(element)
        if capture_source != "deep_capture" or verification_status != "verified":
            raise ValueError(
                f"reference element {element_id} is not extension-verified "
                f"(capture_source={capture_source}, verification_status={verification_status})"
            )
        record = deepcopy(element)
        record["id"] = element_id
        record["mcpReferenceSource"] = {
            "projectId": source_dir.name,
            "elementId": element_id,
        }
        record["mcpReferenceRuntimeStatus"] = "pending"
        records.append(record)

    result = import_elements(
        paths,
        str(target_dir),
        records,
        replace=replace,
        force_replace=force_replace,
        capture_source="deep_capture",
    )

    copied_resources: list[str] = []
    resource_paths: set[str] = set()
    for record in records:
        for selector_name in ("selector", "mcpNativeSelector"):
            selector = record.get(selector_name)
            common = selector.get("commonProps") if isinstance(selector, dict) else None
            if not isinstance(common, dict):
                continue
            for field in ("filePath", "resDirPath"):
                value = str(common.get(field) or "").strip()
                if value:
                    resource_paths.add(value)
    for relative_text in sorted(resource_paths):
        relative = Path(relative_text.replace("\\", os.sep))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        source_resource = (source_dir / relative).resolve()
        try:
            source_resource.relative_to(source_dir)
        except ValueError:
            continue
        if not source_resource.exists():
            continue
        target_resource = target_dir / relative
        if source_resource.is_dir():
            shutil.copytree(source_resource, target_resource, dirs_exist_ok=True)
        else:
            target_resource.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_resource, target_resource)
        copied_resources.append(str(relative).replace(os.sep, "/"))

    result.update(
        {
            "kind": "verified_reference_elements_imported",
            "source_project": str(source_dir),
            "reference_element_ids": normalized_ids,
            "copied_resources": copied_resources,
            "runtime_verification_required": True,
            "message": (
                "已复用另一个本地流程中由扩展验证的元素；这不是站点硬编码。"
                "绑定后仍须在当前真实页面单步运行验证。"
            ),
        }
    )
    return result


def validate_project_elements(paths: ShizaiPaths, project_ref: str, *, strict: bool = False) -> dict[str, Any]:
    """Validate the native element catalog after import and before RPA generation."""
    element_path, payload = _element_file(paths, project_ref)
    records = []
    for element_id, element in payload["elementMap"].items():
        if isinstance(element, dict):
            record = dict(element)
            record.setdefault("id", element_id)
            records.append(record)
    result = (
        validate_captured_elements(records, strict=strict)
        if records
        else {
            "valid": False,
            "strict": strict,
            "element_count": 0,
            "stable_element_count": 0,
            "verified_element_count": 0,
            "unverified_element_count": 0,
            "capture_sources": [],
            "errors": ["elementMap is empty"],
            "warnings": [],
            "elements": [],
        }
    )
    result.update({"project": str(element_path.parent), "elements_file": str(element_path)})
    return result


def _task_steps(task_path: Path) -> list[dict[str, Any]]:
    """Read the ordered native component list from one designer task file."""
    ok, payload, _ = _safe_json(task_path)
    if not ok or not isinstance(payload, dict):
        return []
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return []
    main = entities.get("MAIN_COMPONENT")
    if not isinstance(main, dict):
        return []
    statements: list[Any] = []
    for arg in main.get("args0", []):
        if isinstance(arg, dict) and arg.get("name") == "statements" and isinstance(arg.get("value"), list):
            statements = arg["value"]
            break
    result = []
    for node_id in statements:
        node = entities.get(str(node_id))
        if not isinstance(node, dict):
            continue
        result.append(
            {
                "id": str(node.get("id") or node_id),
                "type": str(node.get("type") or ""),
                "label": str(node.get("label") or ""),
            }
        )
    return result


_COORDINATE_ACTION_TYPES = {
    "Mouse_ClickCombine_v4",
    "Mouse_MoveToV2",
}


def _task_argument_map(node: dict[str, Any]) -> dict[str, Any]:
    """Return the visible native arguments for one task node."""
    return {
        str(argument.get("name")): argument.get("value")
        for argument in node.get("args0", [])
        if isinstance(argument, dict) and argument.get("name")
    }


def _has_collection_selector(value: Any) -> bool:
    """Check that a DataCollection selector is a reusable web selector."""
    if not isinstance(value, list) or not value:
        return False
    web_parts = [
        part for part in value if isinstance(part, dict) and str(part.get("type") or "").strip().lower() == "web"
    ]
    # Native DeepCapture on Chrome can return a UIA chain such as
    # Window > Pane > Document > Group > Text/Edit.  It is still a reusable
    # webpage selector, but it must contain Document; Window/Pane-only shells
    # must remain invalid.
    if not web_parts:
        document_seen = False
        web_parts = []
        for part in value:
            if not isinstance(part, dict):
                continue
            name = str(part.get("name") or "").strip().lower()
            if name == "document":
                document_seen = True
                continue
            if not document_seen or name not in _UIA_TO_WEB_TAG:
                continue
            web_parts.append(part)
        if not web_parts:
            return False
    web_names = [str(part.get("name") or "").strip().casefold() for part in web_parts]
    if "table" in web_names and "tr" in web_names and any(name in {"td", "th"} for name in web_names):
        return True
    # DataCollectionV5 resolves column paths relative to collectionConfig.tableSelector.
    # A native table link therefore becomes tr > td > a and may only need
    # relative indexes; requiring a text/class property here rejects a valid,
    # reusable selector that RuntimeHost can evaluate for every repeated row.
    if (
        "tr" in web_names
        and any(name in {"td", "th"} for name in web_names)
        and any(name in {"a", "hyperlink", "link"} for name in web_names)
    ):
        return True
    if "article" in web_names and len(web_parts) >= 2:
        return True
    for part in web_parts:
        for raw_prop in part.get("props", []):
            prop = _selector_property_dict(raw_prop)
            if prop is None:
                continue
            name = str(prop.get("name") or "").strip().lower()
            if name == "@index":
                continue
            raw_value = prop.get("textValue")
            if raw_value is None or raw_value == "":
                raw_value = prop.get("value")
            value_text = _decode_selector_value(raw_value)
            if value_text:
                return True
    return False


def _task_reuse_diagnostics(task_path: Path) -> dict[str, Any]:
    """Find coordinate-only actions and empty collection selectors in a native task."""
    ok, payload, error = _safe_json(task_path)
    if not ok or not isinstance(payload, dict):
        return {"errors": [f"unable to read task JSON: {error or task_path.name}"], "warnings": []}
    entities = payload.get("componentModelEntities")
    main = entities.get("MAIN_COMPONENT") if isinstance(entities, dict) else None
    if not isinstance(main, dict):
        return {"errors": ["MAIN_COMPONENT is missing"], "warnings": []}
    statements: list[Any] = []
    for argument in main.get("args0", []):
        if (
            isinstance(argument, dict)
            and argument.get("name") == "statements"
            and isinstance(argument.get("value"), list)
        ):
            statements = argument["value"]
            break
    errors: list[str] = []
    warnings: list[str] = []
    for node_id in statements:
        node = entities.get(str(node_id)) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        args = _task_argument_map(node)
        if node_type in _COORDINATE_ACTION_TYPES:
            selector = str(args.get("uiSelector") or "").strip()
            location = args.get("location")
            if not selector and location not in (None, "", [], {}):
                errors.append(
                    f"{task_path.name}:{node_id} {node_type} uses coordinate-only location; capture and bind a real element selector"
                )
        if node_type == "WebBrowser_DataCollectionV5":
            config_value = args.get("config")
            try:
                config = json.loads(config_value) if isinstance(config_value, str) else config_value
            except json.JSONDecodeError:
                errors.append(f"{task_path.name}:{node_id} DataCollectionV5 has invalid config JSON")
                continue
            collection = config.get("collectionConfig") if isinstance(config, dict) else None
            selectors = collection.get("columnSelectorArray") if isinstance(collection, dict) else None
            if not isinstance(selectors, list) or not selectors:
                errors.append(f"{task_path.name}:{node_id} DataCollectionV5 has no column selectors")
            else:
                for column_index, selector in enumerate(selectors):
                    if not _has_collection_selector(selector):
                        errors.append(
                            f"{task_path.name}:{node_id} DataCollectionV5 column {column_index} has no reusable selector"
                        )
    return {"errors": errors, "warnings": warnings}


def inspect_rpa_flow(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Inspect native tasks without imposing a model-owned component recipe."""
    project_dir = resolve_project(paths, project_ref)
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    model_owned = str(metadata.get("mcpPlanningPolicy") or "").strip().casefold() in {
        "flexible", "advisory", "model_decides"
    }
    task_files = []
    for task_path in sorted(project_dir.rglob("*.task")):
        steps = _task_steps(task_path)
        if steps:
            reuse = _task_reuse_diagnostics(task_path)
            task_files.append(
                {
                    "file": str(task_path),
                    "steps": steps,
                    "first_component": steps[0]["type"],
                    "errors": reuse["errors"],
                    "warnings": reuse["warnings"],
                }
            )
    first_components = [item["first_component"] for item in task_files]
    browser_first = bool(task_files) and all(component == "WebBrowser_CreateV3" for component in first_components)
    errors = [error for item in task_files for error in item["errors"]]
    warnings = [warning for item in task_files for warning in item["warnings"]]
    if not task_files:
        errors.append("no native .task flow with ordered components was found")
    elif not browser_first and not model_owned:
        errors.append("every native browser task must begin with WebBrowser_CreateV3 (open Chrome)")
    return {
        "project": str(project_dir),
        "valid": not errors,
        "browser_open_first": browser_first,
        "required_first_component": None if model_owned else "WebBrowser_CreateV3",
        "required_first_step": None if model_owned else "打开 Google Chrome 并进入目标网站",
        "planning_policy": "model_decides" if model_owned else "legacy_strict",
        "task_files": task_files,
        "errors": errors,
        "warnings": warnings,
    }


def validate_rpa_project(
    paths: ShizaiPaths, project_ref: str, *, strict: bool = False, publish_ui: bool = True
) -> dict[str, Any]:
    """Combine element and native task checks for a browser RPA project."""
    # A read-only preview still wants the real validation result, but must not
    # drive the designer overlay; only committed writes publish progress.
    if publish_ui:
        _publish_rpa_ui(
            "validating",
            "project_validation",
            "正在校验设计器项目文件、元素目录和原生任务结构。",
            animation="project_validation",
            flow_ref=str(project_ref),
            strict=bool(strict),
        )
    flow = inspect_rpa_flow(paths, project_ref)
    elements = validate_project_elements(paths, project_ref, strict=strict)
    # Page-scoped JavaScript can operate entirely on the live DOM and does
    # not need a persisted DeepCapture element. Keep the general element
    # validator strict for element-bound components, but do not block a
    # browser flow merely because its element catalog is intentionally empty.
    requires_elements = any(
        bool((_live_rpa_contract(paths, str(step.get("component") or "")) or {}).get("requires_elements"))
        for step in flow.get("task_files", [])
        for step in (step.get("steps") or [])
        if isinstance(step, dict)
    )
    if not requires_elements and elements.get("errors") == ["elementMap is empty"]:
        elements = {
            **elements,
            "valid": True,
            "errors": [],
            "advisories": ["elementMap is empty; all current steps are page-scoped or element-free"],
        }
    errors = list(elements["errors"]) + list(flow["errors"])
    warnings = list(elements["warnings"]) + list(flow["warnings"])
    flow_contract: dict[str, Any] | None = None
    contract_required = True
    try:
        candidate = Path(project_ref).expanduser()
        if not candidate.is_dir():
            candidate = resolve_project(paths, project_ref)
        # ``validate_rpa_project`` is also used by the generic project
        # inspector.  A native task can legitimately contain only a browser
        # open node and a keyboard action, so applying the MCP demo's full
        # five-field collection contract to every task creates false errors.
        # Native flows created through this harness carry mcp* metadata; only
        # those flows need the stricter collection/card/link checks here.
        if candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file():
            ok, metadata, _ = _safe_json(candidate / "szrpa.json")
            contract_required = bool(
                ok and isinstance(metadata, dict) and any(str(key).startswith("mcp") for key in metadata)
            )
    except (FileNotFoundError, OSError, ValueError):
        contract_required = True
    if contract_required:
        try:
            # inspect_rpa_flow checks task shape; the component contract check
            # is also needed here so MCP-managed projects catch collection
            # bindings that are structurally valid JSON but point at a service
            # link or a different product card.
            flow_contract = validate_rpa_flow(paths, project_ref, strict=False, publish_ui=publish_ui)
        except (FileNotFoundError, OSError, ValueError):
            flow_contract = None
    if flow_contract:
        for detail in flow_contract.get("errors", []):
            if detail not in errors:
                errors.append(detail)
        for detail in flow_contract.get("warnings", []):
            if detail not in warnings:
                warnings.append(detail)
    valid = not errors and (not strict or not warnings)
    ui_feedback = (
        _publish_rpa_ui(
            "project_ready" if valid else "validation_failed",
            "project_validation",
            "设计器项目校验通过，可以物化或运行。" if valid else "设计器项目校验未通过，请先修复项目和元素问题。",
            animation="project_complete" if valid else "validation_error",
            flow_ref=str(resolve_project(paths, project_ref)),
            valid=valid,
            next_tool="shizai_rpa_materialize" if valid else "shizai_rpa_validate_project",
        )
        if publish_ui
        else None
    )
    return {
        "project": str(resolve_project(paths, project_ref)),
        "valid": valid,
        "strict": strict,
        "errors": errors,
        "warnings": warnings,
        "elements": elements,
        "flow": flow,
        "flow_contract": flow_contract,
        "ui_feedback": ui_feedback,
    }


def rpa_generation_context(query: str | None = None) -> dict[str, Any]:
    """Return the deterministic contract an MCP client model uses to design RPA."""
    return {
        "kind": "browser_rpa",
        "query": query or "",
        "model_responsibility": "The client model interprets the request and chooses the graph from the live component catalog. Use browser/desktop evidence to propose element records; they may be drafts, but only selectors captured from the same live page through DeepCapture are runnable.",
        "first_step": {
            "policy": "model_decides",
            "selection": "choose any installed component that satisfies the first atomic operation",
            "note": "WebBrowser_CreateV3 is available for browser navigation, but MCP does not require it as the first node.",
        },
        "recommended_sequence": [],
        "native_recommended_sequence": [],
        "sequence_policy": "The client model chooses the graph, ordering, loops, branches, pagination and output composition from live contracts.",
        "mcp_client_bootstrap": {
            "attached_check": "当前对话必须能看到 shizai_* 工具，至少包括 shizai_rpa_analyze_request 和 shizai_rpa_component_catalog；能看到本 context 也说明 MCP 已完成调用。",
            "missing_tools_action": "如果工具列表为空或没有 shizai_* 工具，立即停止业务设计，不要自行编写 MCP 客户端、另起 stdio 进程或声称完成；由宿主安装/注册 shizai-agent 后重启客户端，再重新开始。",
            "stdio_lifecycle": "同一流程从 initialize 到 close 复用一个 MCP stdio 进程；不要为每个工具调用重新启动服务。",
            "source_cli_entry": "shizai_agent/shizai_agent_cli.py",
            "source_mcp_entry": "shizai_agent/core/mcp_server.py",
            "upgrade_command": "npx -y shizai-agent-mcp@latest install",
            "after_upgrade": "重启 Codex，在 /mcp 确认 shizai-agent 和工具列表，再提交 RPA 请求。",
            "prompt_boundary": "提示词可以告诉模型如何检查和使用 MCP，但不能把工具注入一个没有挂载 MCP 的对话。",
        },
        "rule_authoring": {
            "language": "中文",
            "priority": ["用户明确要求", "MCP安全边界", "业务规则", "实时组件契约", "默认建议"],
            "placement": {
                "global_rules": "shizai_agent/core/mcp_server.py 的 MCP_INSTRUCTIONS",
                "rpa_rules": "shizai_agent/core/project.py 的 rpa_generation_context",
                "tool_rules": "shizai_agent/core/mcp_server.py 的 _schemas 工具 description",
                "model_workflow": "skills/shizai-agent/SKILL.md",
                "developer_reference": "docs/MCP_RULES.md",
            },
            "format": ["适用条件", "必须", "禁止", "验证证据", "失败处理"],
            "writing_style": "规则文本使用中文，并用‘必须’、‘禁止’、‘只有……才……’表达可执行条件；工具名、组件 type、参数名保留真实英文标识符。",
            "effect_boundary": "文档只供开发者阅读；模型能稳定看到的是 MCP 初始化说明、工具 description 和本工具返回的结构化 context。",
        },
        "business_rules": deepcopy(RPA_BUSINESS_RULES),
        "search_result_wait": {
            "required": False,
            "component": "Basic_Delay_V1",
            "default_seconds": 1,
            "refresh_component": "WebBrowser_GetTAB_V1",
            "rule": "搜索提交后通常需要等待并刷新当前 Tab 的页面证据；是否加入等待或重新获取 Tab 由模型根据组件和页面行为决定。不要复用已失效的 uia:* 结果页元素。",
            "compatibility": "wait_after_submit and refresh behavior are optional model choices; preserve existing flow behavior when repairing an old project.",
        },
        "capture_protocol": [
            "先创建或复用 native 项目，再根据模型选定的组件需要初始化浏览器、桌面窗口或其他运行环境",
            "用浏览器/桌面捕获工具获取当前页面或窗口的截图、DOM/OCR 和页面身份信息",
            "用结构证据交叉确认 selector、tag、属性、URL/title；页面变化后重新捕获，不复用过期引用",
            "调用元素校验工具；候选元素可以先保存为草稿，只有同一实时捕获会话验证通过的元素才能运行",
            "按模型选定的组件和图结构逐步写入参数、变量和依赖，MCP 仅校验实时契约",
            "根据业务方案选择循环、条件、分页、JavaScript、下载或输出组件，不要求某一种组合",
            "完成后调用流程/项目校验、物化、运行诊断，并以状态、日志和输出证据判断结果",
        ],
        "element_record": {
            "required": ["id", "name", "tag", "url 或 title", "css_selector 或 attributes/role/text"],
            "preferred_selector_fields": [
                "id",
                "aria-label",
                "data-testid",
                "role",
                "placeholder",
                "title",
                "href",
                "innertext",
            ],
            "avoid": ["仅截图坐标", "仅 nth-child 路径", "临时浏览器引用编号", "密码、Cookie、Token"],
        },
        "iteration_rule": "操作后重新取得截图和 DOM snapshot；候选元素可先保存草稿，但必须在同一页面用 DeepCapture 复捕后才绑定、物化或运行。",
        "collection_binding_rule": {
            "atomic_tool": "shizai_rpa_capture_page_fields",
            "required_fields": list(DEFAULT_RPA_COLUMNS),
            "same_card_required": True,
            "link_policy": "商品链接必须是商品详情 href；客服、帮助、政策、页脚和导航链接会被拒绝。",
            "stale_id_policy": "扫描得到的 uia:* id 只在同一次 DeepCapture 会话中使用；页面变化后重新扫描。",
            "verification_policy": "mcpCaptureSource=deep_capture 且 mcpVerificationStatus=verified 才能进入可运行流程。",
        },
        "step_validation_protocol": [
            "每次 shizai_rpa_add_step 成功写入后，先读取返回的 step_validation 和 verification_gate。",
            "verification_gate.status=static_failed 时只修复当前组件；不要追加下一组件。",
            "static_validation.valid=true 后，启动或复用 design_validation 调试会话，调用 debug_command(action=next)，并读取 debug_status。",
            "结合页面截图/OCR或运行输出确认当前组件的可见效果；静态通过、请求已接收或 DAP 连接成功都不算运行证据。",
            "只有 verification_gate.runtime_validation.verified=true 才进入下一组件；全部组件完成后再做 flow/project 校验、物化和正式运行。",
        ],
        "output_variable_contracts": {
            "WebBrowser_CreateV3": ["网页对象"],
            "WebBrowser_GetTAB_V1": ["网页对象"],
            "Element_SetValue_V6": ["校验结果"],
            "WebBrowser_DataCollectionV5": ["数据采集结果", "Excel路径", "监控空列数据"],
            "Excel_Cs_NewV4": ["Excel文件"],
            "Excel_Cs_WriteRange": [],
        },
        "default_output_columns": list(DEFAULT_RPA_COLUMNS),
        "native_design_protocol": [
            "调用 shizai_rpa_start_design 创建或复用真实设计器应用；首节点由模型根据计划或当前原子操作选择",
            "调用 shizai_rpa_components 读取实时组件、完整参数和当前状态",
            "只为选定组件获取所需的页面/窗口证据并导入已验证元素",
            "调用 shizai_rpa_add_step 或 shizai_rpa_update_step_config 写入组件的全部合法参数；写入后必须按 verification_gate 单步执行并取得页面/运行证据，再决定下一个组件",
            "按需要多次捕获、校验、绑定和修复，直到流程契约和运行环境满足要求",
            "调用 shizai_rpa_validate_flow、shizai_rpa_validate_project，再进行诊断和运行",
        ],
        "generation_contract": {
            "source_project_ref_required": False,
            "source_project_must_have_elements": True,
            "native_first_component": None,
            "element_dependent_steps_require_imported_ids": True,
            "component_order_is_strict": False,
            "component_order_policy": "The client model chooses order and graph shape; dependencies and component contracts are validated.",
            "output_columns_example": ["商品名称", "商家名称", "价格", "销量", "商品链接"],
            "default_output_columns": list(DEFAULT_RPA_COLUMNS),
            "default_excel_mode": True,
            "explicit_excel_sequence": [],
            "explicit_excel_sequence_policy": "advisory example only; the model may choose any valid output composition",
            "step_validation_tool": "shizai_rpa_validate_step",
            "client_application_creation": "ApplicationService.createApplication",
            "external_staging_required": False,
        },
    }


def native_rpa_generation_context(query: str | None = None) -> dict[str, Any]:
    """Return a component-first planning contract for native RPA requests."""
    return {
        "kind": "native_rpa",
        "query": query or "",
        "model_responsibility": "先把用户请求拆成原子操作，再用实时组件目录为每个原子操作选择真实组件并绑定上下游变量。",
        "implementation_policy": {
            "default_strategy": "component_first",
            "priority_order": [
                "已安装的设计器组件",
                "基础逻辑/变量/循环组件",
                "只覆盖剩余原子计算的短插入代码",
            ],
            "required_behavior": [
                "读写 Excel、网页、文件、数据库、循环、条件、变量和保存等操作，先查找并使用对应组件。",
                "不要用一个代码节点替代整个流程，也不要在插入代码中重新实现组件已经提供的读写或保存。",
                "每个组件节点都要记录真实 type、输入变量、输出变量、依赖节点和校验结果。",
            ],
            "short_code_exception": {
                "allowed_for": "组件目录中没有对应能力的单个原子计算",
                "must_not_do": ["打开文件", "读取 Excel", "写入 Excel", "保存文件", "替代循环或条件组件"],
                "shape": "一个短代码节点只处理当前步骤的数据，默认不超过约 20 行。",
            },
        },
        "decomposition_contract": {
            "step_fields": [
                "id",
                "operation",
                "implementation_kind",
                "component_type",
                "depends_on",
                "input_bindings",
                "expected_outputs",
                "reason",
                "validation",
            ],
            "implementation_kind_values": ["component", "short_code", "manual_input"],
            "rule": "先完成操作到组件的映射，再生成流程；组件未覆盖的步骤才标记为 short_code，并说明原因。",
        },
        "example_excel_row_processing": [
            {
                "order": 1,
                "operation": "打开原 Excel",
                "implementation_kind": "component",
                "component_type": "Excel_Cs_OpenV4",
            },
            {
                "order": 2,
                "operation": "读取已编辑行数",
                "implementation_kind": "component",
                "component_type": "Excel_Cs_ReadRowCount",
            },
            {
                "order": 3,
                "operation": "逐行循环",
                "implementation_kind": "component",
                "component_type": "excel_loop_data_v2",
            },
            {
                "order": 4,
                "operation": "读取当前行输入",
                "implementation_kind": "component",
                "component_type": "Excel_Cs_ReadCell",
            },
            {
                "order": 5,
                "operation": "计算当前行差集",
                "implementation_kind": "short_code",
                "component_type": "base_command_custom",
                "reason": "只保留组件未覆盖的原子计算",
            },
            {
                "order": 6,
                "operation": "写回当前行结果",
                "implementation_kind": "component",
                "component_type": "Excel_Cs_WriteCell",
            },
            {
                "order": 7,
                "operation": "保存原 Excel",
                "implementation_kind": "component",
                "component_type": "Excel_Cs_Save",
            },
        ],
        "execution_order": [
            "route intent",
            "decompose into atomic operations",
            "query shizai_rpa_component_catalog",
            "inspect each selected component contract",
            "build component-first sequence and variable bindings",
            "add short code only for uncovered atomic calculations",
            "validate each step and the complete flow",
            "materialize and run only after validation",
        ],
        "next_tool": "shizai_rpa_component_catalog",
    }


def route_request(query: str) -> dict[str, Any]:
    """Classify a one-sentence request before choosing Agent or native RPA tools.

    The MCP client model remains responsible for the detailed design. This
    deterministic router only prevents webpage automation from being sent to
    the Agent Brain draft generator, which would produce the wrong artifact.
    """
    text = str(query or "").strip()
    lowered = text.casefold()
    browser_terms = (
        "浏览器",
        "网页",
        "chrome",
        "google",
        "搜索",
        "采集",
        "抓取",
        "读取搜索结果",
        "打开网站",
        "商品",
        "网页元素",
        "browser",
        "web",
    )
    native_terms = (
        "rpa",
        "设计器",
        "组件",
        "ocr",
        "excel",
        "wps",
        "桌面",
        "窗口",
        "键盘",
        "鼠标",
        "截图",
        "文件",
        "文件处理",
        "数据库",
        "pdf",
        "邮件",
        "流程组件",
        "native",
    )
    agent_terms = (
        "智能体",
        "知识库",
        "对话",
        "问答",
        "大模型",
        "聊天",
        "提示词",
        "agent",
        "chatbot",
        "completion",
    )
    browser_hits = [term for term in browser_terms if term in lowered]
    native_hits = [term for term in native_terms if term in lowered]
    agent_hits = [term for term in agent_terms if term in lowered]
    # Treat ordinary imperative verbs as executable RPA intent too.  A user
    # should not have to repeat "创建流程" for a request such as "读取文件
    # 并替换文本"; routing it to Agent workflow makes the MCP client retry
    # analysis and obscures the native component path.
    action_hits = [
        term for term in (
            "生成流程", "创建流程", "自动化", "执行", "操作", "保存", "导出",
            "读取", "提取", "获取", "采集", "抓取", "整理", "汇总", "去重", "排序", "筛选",
            "转换", "写入", "替换", "复制", "追加", "删除", "重命名", "等待",
            "检查", "判断", "下载", "上传", "登录", "输入", "点击", "查询",
        ) if term in lowered
    ]

    if browser_hits and (action_hits or len(browser_hits) >= 2):
        intent = "browser_rpa"
        next_tool = "shizai_rpa_start_design"
        reason = "检测到网页/浏览器操作与自动化任务，应创建客户端原生 RPA 流程。"
    elif native_hits and (action_hits or "rpa" in lowered):
        intent = "native_rpa"
        next_tool = "shizai_rpa_component_catalog"
        reason = "检测到设计器组件或桌面自动化任务，应先查询本机组件目录。"
    elif agent_hits:
        intent = "agent_workflow"
        next_tool = "shizai_generation_context"
        reason = "检测到智能体/知识库/对话任务，应进入 Agent 工作流草稿链。"
    else:
        intent = "agent_workflow"
        next_tool = "shizai_generation_context"
        reason = "未检测到明确的网页或原生 RPA 信号，默认按 Agent 工作流规划并保留可复核上下文。"

    workflow_plan: list[dict[str, Any]]
    if intent == "browser_rpa":
        workflow_plan = [
            {
                "phase": "analyze",
                "tools": ["shizai_rpa_analyze_request", "shizai_rpa_component_catalog"],
                "exit_criteria": "operations extracted and every model-selected component confirmed by the live catalog",
            },
            {
                "phase": "design",
                "tools": ["shizai_rpa_start_design"],
                "exit_criteria": "validated request plan persisted and the model-selected graph is ready for evidence binding",
            },
            {
                "phase": "capture",
                "tools": [
                    "shizai_rpa_capture_windows",
                    "shizai_rpa_scan_page",
                    "shizai_rpa_capture_elements",
                    "shizai_project_import_elements",
                ],
                "optional_tool": "shizai_extension_start_capture",
                "exit_criteria": "selectors required by the next planned step are validated and imported",
            },
            {
                "phase": "compose",
                "tools": ["shizai_rpa_add_step", "shizai_rpa_validate_step"],
                "exit_criteria": "every step_validation.valid=true and required output step exists",
            },
            {
                "phase": "verify",
                "tools": [
                    "shizai_rpa_validate_flow",
                    "shizai_rpa_validate_project",
                    "shizai_rpa_materialize",
                    "shizai_rpa_sync_diagnose",
                ],
                "exit_criteria": "project and flow valid with no blocking errors",
            },
            {
                "phase": "run",
                "tools": ["shizai_rpa_run", "shizai_rpa_status"],
                "exit_criteria": "terminal success plus fresh output evidence",
            },
        ]
    elif intent == "native_rpa":
        workflow_plan = [
            {
                "phase": "analyze",
                "tools": ["shizai_rpa_analyze_request", "shizai_rpa_component_catalog"],
                "exit_criteria": "operations extracted and each operation mapped to an installed component",
            },
            {
                "phase": "design",
                "tools": ["shizai_rpa_start_design"],
                "exit_criteria": "validated request plan persisted and the model-selected graph is ready for component writes",
            },
            {
                "phase": "compose",
                "tools": ["shizai_rpa_add_step", "shizai_rpa_validate_step"],
                "exit_criteria": "all steps valid",
            },
            {
                "phase": "verify",
                "tools": ["shizai_rpa_validate_flow", "shizai_rpa_validate_project"],
                "exit_criteria": "no blocking errors",
            },
            {
                "phase": "run",
                "tools": ["shizai_rpa_run", "shizai_rpa_status"],
                "exit_criteria": "terminal success and output evidence",
            },
        ]
    else:
        workflow_plan = [
            {
                "phase": "context",
                "tools": ["shizai_generation_context", "shizai_components"],
                "exit_criteria": "live node contracts loaded",
            },
            {
                "phase": "compose",
                "tools": ["shizai_draft", "shizai_validate_draft", "shizai_apply_flow"],
                "exit_criteria": "saved draft rereads successfully",
            },
            {
                "phase": "run",
                "tools": ["shizai_runs", "shizai_logs"],
                "exit_criteria": "terminal run evidence available",
            },
        ]

    return {
        "query": text,
        "intent": intent,
        "confidence": "high"
        if (browser_hits and intent == "browser_rpa") or (agent_hits and intent == "agent_workflow")
        else "medium",
        "signals": {
            "browser": browser_hits,
            "native_rpa": native_hits,
            "agent": agent_hits,
            "actions": action_hits,
        },
        "reason": reason,
        "next_tool": "shizai_rpa_analyze_request" if intent in {"browser_rpa", "native_rpa"} else next_tool,
        "workflow_plan": workflow_plan,
        "completion_rule": "next_tool is a continuation pointer; intent routing never completes the requested task.",
        "login_interaction_rule": (
            "检测到 login_required 或 page_challenge 时必须暂停并与用户交互；默认由用户在当前窗口完成登录/验证。"
            "只有用户明确要求代登录并在当前对话提供所需信息时才执行登录，且不得把登录信息写入流程、项目或 MCP 参数。"
        ),
        "browser_rpa_rule": "browser_rpa 可先分析原始请求，再由客户端模型根据实时组件目录自由提交组件、循环、条件或分页计划；MCP 不内置站点流程，只校验能力、结构、元素和安全边界。",
        "agent_rule": "agent_workflow 才能使用 shizai_generation_context、shizai_validate_draft 和 shizai_apply_flow。",
    }


def analyze_rpa_request(query: str) -> dict[str, Any]:
    """Build a site-neutral intent envelope from one natural-language request.

    This stage extracts evidence and planning obligations. It deliberately
    does not choose a component sequence or generate selectors/JavaScript;
    those decisions belong to the MCP client model after catalog inspection.
    The analysis does expose a site-neutral stage contract for hierarchical
    records, so a client model can choose native collection components without
    first reverse-engineering that requirement from a long script.
    """
    text = str(query or "").strip()
    if not text:
        raise ValueError("query must not be empty")

    def unique_values(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    routed = route_request(text)
    if routed["intent"] not in {"browser_rpa", "native_rpa"}:
        raise ValueError(f"request is not an RPA intent: {routed['intent']}")
    flow_name_match = re.match(
        r"^\s*(?:请)?(?:新建|创建|生成|设计)\s*(?:一个|一条)?\s*"
        r"(?:名为|名称为|名字为)\s*[“\"‘'](?P<name>[^”\"’']+)[”\"’']\s*"
        r"的?\s*(?:原生\s*)?(?:RPA\s*)?流程\s*[。.!！]?\s*",
        text,
        flags=re.IGNORECASE,
    )
    flow_name = flow_name_match.group("name").strip() if flow_name_match else None
    body = (
        text[flow_name_match.end() :]
        if flow_name_match
        else re.sub(r"^\s*请?(?:新建|生成|创建|设计)(?:一个|一条)?流程\s*[：:]?\s*", "", text)
    )
    # Keep orchestration metadata in the immutable query, but do not turn it
    # into executable RPA operations.  Models often append a flow name or a
    # request for timing reports; those belong to MCP telemetry, not the task
    # graph and should never create uncovered operation ids.
    operation_body = re.split(
        r"(?:流程(?:名称|名字|名)|任务(?:过程|记录)|耗时|成功率|测试报告)\s*(?:保存|命名|记录|统计|输出|写入|为)?",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    operation_body = re.split(
        r"(?:若遇到|如果遇到|遇到)(?:登录|验证|验证码|挑战)[^。；;]*",
        operation_body,
        maxsplit=1,
    )[0]
    # Client orchestration hints are immutable request metadata, not RPA
    # operations. Strip them before clause detection so phrases such as
    # "请使用 shizai-agent MCP ..." or "100 条是最终有效行数" cannot create
    # a phantom extract/output step or duplicate the collection target.
    operation_body = re.split(
        r"(?:请使用|使用)\s+(?:shizai[- ]agent\s+)?(?:MCP|mcp)\b.*$",
        operation_body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    # Prompts used by MCP clients often append a procedural checklist such as
    # "严格按 MCP ... 流程写入".  That checklist is orchestration metadata,
    # not another browser operation; strip it before clause detection while
    # keeping the immutable original query in the analysis envelope.
    operation_body = re.split(
        r"(?:请)?严格按\s+(?:shizai[- ]agent\s+)?MCP\b.*$",
        operation_body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    operation_body = re.split(
        r"\d+\s*(?:条|项|行)\s*(?:是|为)\s*(?:最终|实际|目标)?\s*(?:有效)?\s*(?:行数|结果|记录|数据)\b.*$",
        operation_body,
        maxsplit=1,
    )[0]
    operation_body = re.split(
        r"(?:分页|翻页)\s*(?:由|使用|通过)\s+[^。；;]*",
        operation_body,
        maxsplit=1,
    )[0]
    action_prefix = r"(?:打开|进入|访问|导航到|读取|采集|抓取|提取|获取|整理|汇总|去重|排序|筛选|转换|输出|保存|写入|导出|发送|点击|输入|选择|上传|下载|登录|搜索|查询|检索)"
    url_tokens: dict[str, str] = {}

    def protect_operation_url(match: re.Match[str]) -> str:
        token = f"__SHIZAI_OPERATION_URL_{len(url_tokens)}__"
        url_tokens[token] = match.group(0)
        return token

    protected_operation_body = re.sub(
        r"https?://[^\s\]\[()<>\"'，,。；;：！？、]+",
        protect_operation_url,
        operation_body,
        flags=re.IGNORECASE,
    )

    def restore_operation_urls(value: str) -> str:
        for token, url in url_tokens.items():
            value = value.replace(token, url)
        return value
    # A full stop is always an operation boundary. Commas are boundaries when
    # the following phrase contains a new action after a short context prefix,
    # for example ``在首页搜索`` or ``从真实结果页采集``.
    clauses = [
        restore_operation_urls(item.strip(" 。；;"))
        for item in re.split(
            rf"[。.!！；;]|(?:然后|接着|随后)|"
            rf"[，,](?=\s*(?:(?:最终|实际|共|一共|总共)\s*|(?:在|从)[^，,。；;]{{0,16}}?)?"
            rf"(?:(?:优先|尽量|尽可能)\s*)?(?:(?:使用|采用)\s*[^，,。；;]{{0,32}}?)?{action_prefix})|"
            rf"[，,](?=\s*并(?:且)?(?:将)?\s*(?:Excel\s*)?(?:结果|数据)?\s*"
            rf"(?:输出|保存|写入|导出|发送))",
            protected_operation_body,
        )
        if item.strip(" 。；;")
    ]
    operations: list[dict[str, Any]] = []
    action_patterns = (
        ("navigate", r"(?:打开|进入|访问|导航到)"),
        ("extract", r"(?:读取|采集|抓取|提取|获取)"),
        ("transform", r"(?:整理|汇总|去重|排序|筛选|转换)"),
        ("output", r"(?:输出|保存|写入|导出|发送)"),
        ("interact", r"(?:点击|输入|选择|上传|下载|登录)"),
        ("search", r"(?:搜索|查询|检索)"),
    )
    for index, clause in enumerate(clauses, start=1):
        pieces = [
            item.strip()
            for item in re.split(
                r"(?<=\S)并(?:且)?(?=\s*(?:将\s*)?(?:Excel\s*)?(?:结果|数据)?\s*"
                r"(?:输出|保存|写入|导出|发送))",
                clause,
            )
            if item.strip()
        ]
        for piece in pieces:
            operation_type = "unspecified"
            for candidate, pattern in action_patterns:
                if re.search(pattern, piece, re.IGNORECASE):
                    operation_type = candidate
                    break
            operations.append(
                {
                    "id": f"op-{len(operations) + 1}",
                    "order": len(operations) + 1,
                    "type": operation_type,
                    "instruction": piece,
                    "atomicity": "client_model_must_expand"
                    if operation_type in {"search", "unspecified"}
                    else "atomic",
                }
            )
    # Chinese prompts commonly put a full-width comma directly after the URL
    # without a space.  Treat sentence punctuation as a URL boundary so the
    # next instruction is never persisted as part of the navigation target.
    urls = unique_values(
        match.rstrip(".,，。；;：:！？!?、")
        for match in re.findall(
            r"https?://[^\s\]\[()<>\"'，,。；;：！？、]+",
            text,
            flags=re.IGNORECASE,
        )
    )
    quoted_values = unique_values(
        value.strip()
        for pair in re.findall(r"[“\"]([^”\"]+)[”\"]|[‘']([^’']+)[’']", operation_body)
        for value in pair
        if value.strip()
    )
    windows_paths = unique_values(
        match.strip().rstrip("，,。；;：:！？!?")
        for match in re.findall(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^。；;\r\n]+", text)
    )
    output_destinations = [
        path
        for path in windows_paths
        if re.search(
            rf"(?:输出|保存|写入|导出|放在|存到)\s*(?:至|到)?\s*[“\"']?{re.escape(path)}",
            text,
            flags=re.IGNORECASE,
        )
    ]
    # Keep one numeric completion target even when an orchestration prompt
    # repeats it (for example, "100 条结果" followed by "100 条是最终有效
    # 行数").  Repeated evidence must not become two independent limits in
    # the model's planning context.
    limit_by_value: dict[int, dict[str, Any]] = {}
    for evidence, value in re.findall(
        r"((?:(?:前|最多|限制为?)\s*)?(\d+)\s*(?:条|项|行)(?:有效)?(?:结果|记录|数据)?)",
        text,
    ):
        numeric_value = int(value)
        previous = limit_by_value.get(numeric_value)
        # Prefer a completion-target reading over a repeated "前 N 条" page
        # hint when both refer to the same number.
        if previous is None or (
            re.match(r"^\s*(?:前|最多|限制为?)", str(previous.get("evidence") or ""))
            and not re.match(r"^\s*(?:前|最多|限制为?)", evidence)
        ):
            limit_by_value[numeric_value] = {"value": numeric_value, "evidence": evidence}
    limits = list(limit_by_value.values())
    pagination_match = re.search(r"((?:(?:必要时|有必要|需要|自动|按需|可)\s*)*(?:翻页|下一页|分页))", text)
    pagination = None
    if pagination_match:
        evidence = pagination_match.group(1)
        pagination = {
            "mode": "model_decides",
            "suggested_strategy": "until_max_rows" if limits else "while_next_page_available",
            "trigger": "when_the_selected_strategy_needs_more_source_rows",
            "required_for_completion": False,
            "decision": "model_decides",
            "target_rows": limits[-1]["value"] if limits else None,
            "evidence": evidence,
        }
    # A numeric target is only an output acceptance criterion. Do not infer a
    # pagination requirement from a number such as 100; the client model can
    # inspect the live page and stop after one page when it already satisfies
    # the target. The live catalog advertises pagination as an optional
    # capability when the selected component can provide it.
    requested_fields: list[str] = []
    # Prefer the field list attached to the extraction verb.  The broader
    # legacy ``结果中...`` expression can otherwise read the output clause in
    # prompts such as ``结果写入 C:\\...`` as if the directory were a column.
    collection_field_match = re.search(
        r"(?:采集|抓取|读取|提取|获取)\s*"
        r"(?:(?:至少|最多|前)\s*)?(?:\d+\s*(?:条|项|行)\s*)?"
        r"(?:(?:搜索|商品)?结果|列表)?\s*"
        r"(?:中的?|包括|包含|字段为?)?\s*"
        r"([^，,。；;：:！？!?]+)",
        operation_body,
    )
    if collection_field_match:
        candidate = collection_field_match.group(1).strip()
        # Prompts often omit punctuation before the output clause, for
        # example ``销量并写入 C:\\...``.  Keep the output destination out of
        # the field contract even when the extraction and output actions are
        # joined in one clause.  ``等数据/信息`` is likewise a natural suffix,
        # not a column name.
        candidate = re.split(
            r"\s*(?:并(?:且)?(?:将)?(?:结果|数据)?\s*)?"
            r"(?:输出|保存|写入|导出|发送|放在)\s*",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ，,;；")
        candidate = re.sub(
            r"等(?:可见)?(?:商品)?(?:数据|信息|字段)?\s*$",
            "",
            candidate,
        ).strip()
        # Only accept a collection field list when it contains a field-list
        # separator or a known product field marker.  This keeps generic file
        # extraction requests site-neutral.
        if re.search(r"[、/]|以及|和|与|标题|名称|价格|销量|链接|地址|商家|店铺", candidate):
            requested_fields = unique_values(
                item.strip(" 的字段")
                for item in re.split(r"[、/]|以及|和|与", candidate)
                if item.strip(" 的字段")
            )
    field_match = re.search(
        r"(?:结果|列表|记录|数据)(?:中|中的)?(.+?)(?=[，,；;](?:(?:前|最多|限制为?)?\s*\d+\s*(?:条|项|行)|整理|汇总|取|输出|保存|写入|导出|有必要|必要时|按需|自动|翻页)|[。.]|$)",
        operation_body,
    )
    if field_match and not requested_fields:
        field_text = re.sub(r"^(?:的|包含|包括|字段为?)", "", field_match.group(1).strip())
        # Keep collection constraints out of the field list.  Natural prompts
        # commonly continue with "，采集100条并输出" without a second clause
        # marker, so the broad evidence match above must be trimmed here.
        field_text = re.split(
            r"[，,；;]\s*(?=(?:(?:最终|实际|共|一共|总共|采集|收集|抓取|获取|读取|整理|输出|保存|导出|并)?\s*\d+\s*(?:条|项|行)"
            r"|(?:(?:最终|实际|共|一共|总共)\s*)?(?:有必要|必要时|按需|自动|翻页|分页|输出|保存|导出)))",
            field_text,
            maxsplit=1,
        )[0]
        requested_fields = unique_values(
            item.strip(" 的字段") for item in re.split(r"[、/]|以及|和|与", field_text) if item.strip(" 的字段")
        )
    # Keep the final row target attached to the extraction operation while
    # leaving the implementation strategy to the client model. The model may
    # choose one page, a component-owned paginator, an explicit page action, a
    # loop, JavaScript, a download, or another valid plan.
    if limits or pagination:
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("type") != "extract":
                continue
            operation["completion_contract"] = {
                "target": "final_valid_row_count",
                "max_rows": limits[-1]["value"] if limits else None,
                "pagination": "model_decides",
                "allowed_strategies": [
                    "component_managed_pagination",
                    "explicit_next_page_steps",
                    "loops",
                    "javascript_or_download",
                ],
                "stop_when": [
                    "max_rows valid rows collected",
                    "no further valid source data is available",
                ],
                "required_capabilities": ["extract", "output"],
                "mcp_checks": ["selected components exist", "parameters are valid", "output meets the contract"],
            }
    # A parent/child request needs two page states and two independent native
    # collection outputs. Keep this record shape generic: it never encodes a
    # site's DOM, selector, or business-specific workflow.
    # Keep the shape detector domain-neutral.  The model owns business
    # meaning (comments, products, tickets, etc.); the core only recognizes
    # relationship markers that apply to any nested record task.  A plan may
    # also set ``collection_shape=hierarchical`` when the natural language is
    # implicit (for example, "each item and its details").
    parent_terms = re.search(
        r"(?:一级|第一层|父级|父记录|主记录|上级|primary|parent|master)",
        text,
        flags=re.IGNORECASE,
    )
    child_terms = re.search(
        r"(?:二级|第二层|子级|子记录|从属|下级|嵌套|明细|回复|楼中楼|child|detail|reply|nested)",
        text,
        flags=re.IGNORECASE,
    )
    collection_shape = "hierarchical" if parent_terms and child_terms else "flat"
    collection_stages = (
        [
            {
                "id": "parent_collection",
                "kind": "parent",
                "label": "父记录",
                "required_capabilities": ["extract", "output"],
                "page_state": "parent_records_visible",
                "output_variable": "parent_records",
                "requires": ["parent_key", "parent_fields"],
            },
            {
                "id": "child_collection",
                "kind": "child",
                "label": "子记录",
                "required_capabilities": ["extract", "output"],
                "page_state": "child_records_visible",
                "output_variable": "child_records",
                "requires": ["parent_key", "child_fields", "parent_step_id"],
            },
            {
                "id": "record_alignment",
                "kind": "alignment",
                "label": "父子记录对齐",
                "preferred_capabilities": ["transform", "output"],
                "requires": ["parent_key", "child_key", "merge_key"],
            },
        ]
        if collection_shape == "hierarchical"
        else []
    )
    analysis_id = "rpa-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    required_capabilities = ["navigate", "input", "submit", "extract", "output"]
    collection_capabilities = ["extract", "output"]
    if limits:
        required_capabilities.append("limit")
        collection_capabilities.append("limit")
    # `paginate` is a useful catalog capability, not a mandatory property of
    # the collection step. A model may satisfy pagination with several
    # components or a loop, so do not force it into the catalog query.
    rule_context = rpa_generation_context(text)
    collection_evidence = {
        "max_rows": limits[-1]["value"] if limits else None,
        "pagination": pagination,
        "completion": (
            "collect_valid_rows_until_max_rows_or_source_exhaustion"
            if limits
            else "use_the_selected_pagination_strategy_when_needed"
            if pagination
            else None
        ),
    }
    if collection_shape == "hierarchical":
        collection_evidence.update({"shape": collection_shape, "stages": collection_stages})
    return {
        "kind": "rpa_request_analysis",
        "analysis_id": analysis_id,
        "query": text,
        "intent": routed["intent"],
        "confidence": routed["confidence"],
        "signals": routed["signals"],
        "evidence": {
            "flow_name": flow_name,
            "urls": urls,
            "quoted_values": quoted_values,
            "output_destinations": output_destinations,
            "limits": limits,
            "requested_fields": requested_fields,
            "pagination": pagination,
            "collection": collection_evidence,
            "clauses": clauses,
        },
        "operations": operations,
        "unknowns": [
            item
            for item, missing in (
                ("target_url", not urls and routed["intent"] == "browser_rpa"),
                (
                    "output_destination",
                    not output_destinations and not any(op["type"] == "output" for op in operations),
                ),
                ("output_fields", not requested_fields),
            )
            if missing
        ],
        "planning_requirements": {
            "planning_policy": {
                "owner": "client_model",
                "mode": "model_decides",
                "mcp_role": "live_catalog_and_thin_boundary_validation",
                "strategy_checks": "none",
                "allowed_strategies": [
                    "linear_components",
                    "loops",
                    "conditions",
                    "component_managed_pagination",
                    "explicit_next_page_steps",
                    "javascript_extraction_or_pagination",
                ],
                "hard_constraints": [
                    "component_exists_and_is_materializable",
                    "declared_config_names_are_checked_when_the_catalog_exposes_them",
                    "malformed_reference_fields_are_rejected",
                    "selectors_are_verified_before_run_by_the_shared_cli",
                    "runtime_and_write_permissions_are_checked_before_control",
                ],
                "notice": "All decomposition and strategy fields are model-owned. The MCP list and hints are reference data, not a workflow protocol.",
            },
            "catalog_first": False,
            "one_step_per_atomic_operation": False,
            "component_contract_required": False,
            "explicit_dependencies": False,
            "explicit_variable_bindings": False,
            "selectors_from_current_page_evidence": False,
            "javascript_must_be_supplied_by_client_model": False,
            "no_builtin_site_flow": True,
            "analyze_original_query_once": False,
            "do_not_rewrite_query_for_validation": False,
            "pagination_guidance": {
                "advisory": True,
                "required": False,
                "required_when": (
                    "a numeric collection target may exceed the current page; "
                    "the final output should satisfy the requested valid-row count"
                ),
                "skip_only_when": "the user explicitly limits collection to the current page/首屏",
                "strategies": [
                    "component_managed_pagination",
                    "explicit_next_page_steps",
                    "loops",
                    "javascript_or_download",
                    "another_installed_strategy",
                ],
                "model_action": "Choose a strategy that fits the live page and preserves the output contract.",
                "mcp_checks": {
                    "owner": "mcp",
                    "mode": "acceptance_and_safety",
                    "checks": ["selected components exist", "runtime reports parameters/references", "output is fresh"],
                },
            },
            "component_selection_contract": {
                "required_order": [],
                "order_policy": "model_decides; native CLI validates executable compatibility",
                "numeric_collection_rule": (
                    "Treat N as the final valid-row acceptance target. The model chooses how to reach it from "
                    "the installed components and live page evidence."
                ),
                "evidence_binding": (
                    "Bind only selectors required by the selected component, and capture them from the current "
                    "page after navigation."
                ),
                "advisory": "Use catalog capabilities and planning patterns as hints; do not treat them as a unique plan.",
            },
            "component_requirements": [
                {
                    "role": "browser_navigation",
                    "advisory": True,
                    "required_capabilities": ["navigate"],
                    "covers_operation_types": ["navigate"],
                },
                {
                    "role": "search_interaction",
                    "advisory": True,
                    "required_capabilities": ["input", "submit"],
                    "covers_operation_types": ["search", "interact"],
                    "may_use_multiple_components": True,
                },
                {
                    "role": "structured_collection",
                    "advisory": True,
                    "required_capabilities": collection_capabilities,
                    "covers_operation_types": ["extract", "transform", "output"],
                    "prefer_single_component": False,
                    "completion": {
                        "max_rows": limits[-1]["value"] if limits else None,
                        "pagination": pagination,
                    },
                },
            ],
            "collection_stages": collection_stages,
            "recommended_catalog_query": {
                "scope": "builtin",
                "capability": ",".join(unique_values(required_capabilities)),
                "capability_mode": "any",
                "detail": "contract",
                "limit": 40,
            },
            "collection_decision": {
                "record_shape": collection_shape,
                "target_kind": "final_valid_row_count" if limits else None,
                "requires_pagination": False,
                "pagination_capability": "discover_from_live_component_catalog",
                "advisory": True,
                "required_capabilities": collection_capabilities,
                "must_use_single_component": False,
                "strategy": "model_decides",
                "no_candidate_action": "let_model_compose_multiple_components_or_report_unmet_contract",
                "stage_policy": (
                    "for hierarchical records, use separate native parent and child collection steps, then align"
                    if collection_shape == "hierarchical"
                    else "single native collection is sufficient when one repeated record contains all requested fields"
                ),
            },
            "planner_checkpoint": {
                "purpose": "为模型提供数量目标、组件能力和页面证据的规划参考，避免遗漏最终输出验收条件。",
                "catalog_call": {
                    "tool": "shizai_rpa_component_catalog",
                    "arguments": {
                        "scope": "builtin",
                        "capability": ",".join(unique_values(required_capabilities)),
                        "capability_mode": "any",
                        "detail": "contract",
                        "limit": 40,
                    },
                    "rule": "可优先使用本对象查询实时组件；必要时可按模型的拆解补充查询，筛选方式不构成流程约束。",
                },
                "advisory_checks": [
                    {
                        "when": "evidence.collection.max_rows is a number and pagination is not explicitly disabled",
                        "suggestion": "choose and explain a strategy capable of satisfying the final output target",
                    },
                    {
                        "when": "a structured_collection candidate is returned",
                        "suggestion": "consider planning_guidance.collection candidates and their parameters",
                    },
                    {
                        "when": "the result page has not been opened yet",
                        "suggestion": "defer only the page bindings required by the selected components",
                    },
                ],
                "assertions_are_advisory": True,
                "required_plan_assertions": [
                    "output_contract.max_rows equals evidence.collection.max_rows",
                    "every selected component uses a live contract",
                    "required selectors are bound before the selected component runs",
                ],
                "failure_action": "If the selected strategy cannot satisfy the output contract, report the evidence and let the model revise the relevant step.",
            },
        },
        "rule_authoring": rule_context["rule_authoring"],
        "business_rules": rule_context["business_rules"],
        "interaction_policy": {
            "login_or_challenge": "pause_and_ask_user",
            "delegated_login": "only_when_user_explicitly_requests_it_and_supplies_current_session_credentials",
            "persist_credentials": False,
        },
        "next_tool": "shizai_rpa_component_catalog",
        "after_catalog": "shizai_rpa_validate_plan",
        "analysis_policy": {
            "immutable": True,
            "reuse_analysis_id": analysis_id,
            "do_not_reanalyze_or_rewrite_query": True,
            "correction_policy": "client_model_decides_from_concrete_error_evidence",
        },
        "message": "请求证据已提取；组件目录、步骤组合、变量、循环和分页策略由客户端模型决定。",
    }


def validate_rpa_execution_plan(
    analysis: dict[str, Any],
    plan: dict[str, Any],
    *,
    available_components: Iterable[str] | dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a client-model plan without substituting a built-in flow."""
    if not isinstance(analysis, dict) or analysis.get("kind") != "rpa_request_analysis":
        raise ValueError("analysis must be the result of shizai_rpa_analyze_request")
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if str(plan.get("analysis_id") or "").strip() != str(analysis.get("analysis_id") or "").strip():
        raise ValueError("plan.analysis_id must match the analyzed request")
    # Accept common model-facing aliases at the validation boundary, then
    # validate one canonical schema.  The normalized result still exposes
    # ``steps`` and ``output_contract`` so persisted flows stay consistent.
    plan = dict(plan)
    # ``flexible`` is the MCP-facing policy described in docs/MCP_ARCHITECTURE.md.
    # The legacy direct Python/CLI callers can still request the historical
    # strategy checks explicitly, while MCP clients are free to choose loops,
    # page clicks, JS, or a component-managed pagination implementation.
    flexible_policy = str(
        plan.get("validation_mode") or plan.get("planning_policy") or ""
    ).strip().casefold() in {"flexible", "advisory", "model_decides"}
    pre_normalization_warnings: list[str] = []
    if not isinstance(plan.get("steps"), list) and isinstance(plan.get("components"), list):
        plan["steps"] = plan["components"]
        pre_normalization_warnings.append("plan.components was normalized to plan.steps")
    if not isinstance(plan.get("output_contract"), dict) and isinstance(plan.get("output"), dict):
        output_alias = dict(plan["output"])
        if "columns" not in output_alias and isinstance(output_alias.get("fields"), list):
            output_alias["columns"] = output_alias["fields"]
            pre_normalization_warnings.append("plan.output.fields was normalized to output_contract.columns")
        if "max_rows" not in output_alias and output_alias.get("target_rows") is not None:
            output_alias["max_rows"] = output_alias["target_rows"]
            pre_normalization_warnings.append("plan.output.target_rows was normalized to output_contract.max_rows")
        plan["output_contract"] = output_alias
    if isinstance(plan.get("output_contract"), dict):
        output_alias = dict(plan["output_contract"])
        # fields/required_fields are common model-facing spellings for the
        # canonical ordered columns list. When both are present, fields carry
        # the semantic contract and stale/path-like columns must not become an
        # output header.
        field_alias = output_alias.get("fields")
        if not isinstance(field_alias, list) or not field_alias:
            field_alias = output_alias.get("required_fields")
        if isinstance(field_alias, list) and field_alias:
            normalized_fields = [str(item).strip() for item in field_alias if str(item).strip()]
            existing_columns = [
                str(item).strip() for item in output_alias.get("columns", [])
                if str(item).strip()
            ] if isinstance(output_alias.get("columns"), list) else []
            if existing_columns != normalized_fields:
                if existing_columns:
                    pre_normalization_warnings.append(
                        "output_contract.fields was used to replace stale output_contract.columns"
                    )
                else:
                    pre_normalization_warnings.append(
                        "output_contract.fields was normalized to output_contract.columns"
                    )
                output_alias["columns"] = normalized_fields
        plan["output_contract"] = output_alias
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("plan.steps must contain at least one component step")
    operation_ids = {str(item.get("id")) for item in analysis.get("operations", []) if isinstance(item, dict)}
    component_contracts = _rpa_contract_map()
    live_components = available_components if isinstance(available_components, dict) else {}
    installed_components = (
        {str(item).strip() for item in available_components if str(item).strip()}
        if available_components is not None
        else None
    )
    if installed_components is not None:
        installed_components.update(
            RPA_COMPONENT_TYPE_ALIASES.get(item, item) for item in tuple(installed_components)
        )
    for component_type, record in live_components.items():
        catalog_contract = _catalog_rpa_contract(component_type, record)
        if component_type in component_contracts:
            if not record:
                continue
            fixed_contract = component_contracts[component_type]
            fixed_capabilities = set(fixed_contract.get("capabilities") or [])
            fixed_operations = set(fixed_contract.get("operation_types") or [])
            # Merge compatibility semantics underneath the live contract so
            # newly added native arguments/options and outputs are preserved.
            merged_contract = dict(fixed_contract)
            merged_contract.update(catalog_contract)
            catalog_contract = merged_contract
            # Stable built-in contracts expose default logical variable names
            # (for example "网页对象"). Keep the native assignment slots as
            # metadata so an explicit var_name can replace that default.
            if "outputs" in fixed_contract:
                catalog_contract["outputs"] = list(fixed_contract.get("outputs") or [])
                catalog_contract["output_parameters"] = []
            catalog_contract["catalog_backed"] = True
            catalog_contract["materializable"] = not catalog_contract.get("materialization_errors")
            catalog_contract["capabilities"] = sorted(
                fixed_capabilities.union(record.get("capabilities") or [])
            )
            catalog_contract["operation_types"] = sorted(
                fixed_operations.union(record.get("operation_types") or [])
            )
        component_contracts[component_type] = catalog_contract
    # When a plan explicitly requires pagination, prefer the newest live
    # contract that covers the complete role. An ordinary numeric target does
    # not activate this gate; it is still the model's choice whether one page
    # is enough or a pagination-capable strategy is needed.
    analyzed_pagination_evidence = (analysis.get("evidence") or {}).get("pagination")
    analyzed_pagination = bool(
        isinstance(analyzed_pagination_evidence, dict)
        and analyzed_pagination_evidence.get("required_for_completion")
    )
    live_numeric_candidates: list[tuple[tuple[int, int, str], str]] = []
    if analyzed_pagination and live_components and not flexible_policy:
        required_numeric_capabilities = {"extract", "limit", "paginate", "output"}
        for component_name in live_components:
            contract = component_contracts.get(component_name) or {}
            if not isinstance(contract, dict) or not required_numeric_capabilities.issubset(
                set(contract.get("capabilities") or [])
            ):
                continue
            if contract.get("materializable") is False:
                continue
            match = re.search(r"(?:v|_)(\d+)$", str(component_name), re.IGNORECASE)
            version = int(match.group(1)) if match else 0
            live_numeric_candidates.append(
                ((version, len(contract.get("planning_patterns") or []), str(component_name).casefold()), component_name)
            )
        if live_numeric_candidates:
            live_numeric_candidates.sort(reverse=True)
            recommended_numeric_component = live_numeric_candidates[0][1]
            for raw_step in steps:
                if not isinstance(raw_step, dict):
                    continue
                candidate_component = RPA_COMPONENT_TYPE_ALIASES.get(
                    str(raw_step.get("component") or "").strip(), str(raw_step.get("component") or "").strip()
                )
                candidate_contract = component_contracts.get(candidate_component) or {}
                if required_numeric_capabilities.issubset(set(candidate_contract.get("capabilities") or [])):
                    if candidate_component != recommended_numeric_component:
                        raise ValueError(
                            "numeric collection must use the live recommended component "
                            f"{recommended_numeric_component}; selected {candidate_component}"
                        )
    normalized_steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    covered_operations: set[str] = set()
    produced_variables: set[str] = set()
    variable_producers: dict[str, str] = {}
    dependency_closures: dict[str, set[str]] = {}
    normalization_warnings: list[str] = pre_normalization_warnings
    operation_types = {
        str(item.get("id")): str(item.get("type") or "unspecified")
        for item in analysis.get("operations", [])
        if isinstance(item, dict) and item.get("id")
    }
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"plan.steps[{index - 1}] must be an object")
        raw = dict(raw)
        if not raw.get("id") and raw.get("step_id"):
            raw["id"] = raw["step_id"]
            normalization_warnings.append(f"plan.steps[{index - 1}].step_id was normalized to id")
        step_id = str(raw.get("id") or f"step-{index}").strip()
        component = str(raw.get("component") or "").strip()
        component = RPA_COMPONENT_TYPE_ALIASES.get(component, component)
        purpose = str(raw.get("purpose") or "").strip()
        if not step_id or step_id in seen_ids:
            raise ValueError(f"plan step id is empty or duplicated: {step_id!r}")
        if component not in component_contracts:
            raise ValueError(f"plan step {step_id} uses unsupported component: {component}")
        if installed_components is not None and component not in installed_components:
            raise ValueError(f"plan step {step_id} component is not installed in the live catalog: {component}")
        contract = component_contracts[component]
        if contract.get("catalog_backed") and not contract.get("materializable", False):
            reasons = "; ".join(contract.get("materialization_errors") or ["unsupported catalog code contract"])
            raise ValueError(f"plan step {step_id} component cannot be materialized: {component}: {reasons}")
        if not purpose:
            role = str(raw.get("role") or raw.get("element_role") or "").casefold()
            if "collection" in role or "datacollection" in component.casefold():
                purpose = "采集请求字段并按目标数量持续分页"
            elif "navigation" in role or "create" in component.casefold():
                purpose = "打开目标网站并导航到请求页面"
            elif "search" in role or any(token in component.casefold() for token in ("keyboard", "input", "click")):
                purpose = "完成请求中的页面搜索交互"
            else:
                purpose = f"执行组件 {component} 完成本次流程步骤"
            normalization_warnings.append(
                f"plan step {step_id} purpose was inferred from its role/component; provide an explicit purpose when possible"
            )
        mapped = [str(item).strip() for item in raw.get("operation_ids", []) if str(item).strip()]
        unknown_operations = [item for item in mapped if item not in operation_ids]
        if unknown_operations:
            if flexible_policy:
                normalization_warnings.append(
                    f"plan step {step_id} contains model-defined operation references: {unknown_operations}"
                )
            else:
                raise ValueError(f"plan step {step_id} references unknown operations: {unknown_operations}")
        depends_on = [str(item).strip() for item in raw.get("depends_on", []) if str(item).strip()]
        unknown_dependencies = [item for item in depends_on if item not in seen_ids]
        if unknown_dependencies:
            if flexible_policy:
                normalization_warnings.append(
                    f"plan step {step_id} keeps forward/external dependencies for model-defined graph semantics: {unknown_dependencies}"
                )
            else:
                raise ValueError(f"plan step {step_id} has forward or unknown dependencies: {unknown_dependencies}")
        dependency_closure = set(depends_on)
        for dependency in depends_on:
            dependency_closure.update(dependency_closures.get(dependency, set()))
        config = raw.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError(f"plan step {step_id}.config must be an object")
        # The live component definition is authoritative for native args and
        # options.  Keep only a small set of documented semantic aliases used
        # by the planner-facing schema; do not replace the live set with a
        # hand-maintained component whitelist.
        contract_parameter_names = set(contract.get("parameter_names") or [])
        if not contract_parameter_names:
            contract_parameter_names.update(
                str(item).split("=", 1)[0].strip()
                for item in contract.get("inputs", [])
                if str(item).strip()
            )
            contract_parameter_names.update(
                str(item).split("=", 1)[0].strip()
                for item in contract.get("options", [])
                if str(item).strip()
            )
        semantic_config_aliases = {
            "output_file", "output_path", "file_path", "destination", "file", "filename", "filename_suffix",
            "columns", "max_rows", "pagination", "data_attributes", "output_variable", "outputVariable",
            "output_columns", "result_mode", "page_scope",
            "element_ids",
        }
        unknown_config = sorted(set(config) - contract_parameter_names - semantic_config_aliases)
        if unknown_config:
            raise ValueError(
                f"plan step {step_id} config contains parameters not declared by the live component contract: "
                + ", ".join(unknown_config)
            )
        for config_name, config_value in config.items():
            if not isinstance(config_value, str) or len(config_value) < 2:
                continue
            if config_value[0] != '"' or config_value[-1] != '"':
                continue
            try:
                decoded_value = json.loads(config_value)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded_value, str):
                raise ValueError(
                    f"plan step {step_id} config.{config_name} uses a native quoted default; "
                    f"pass the semantic JSON string {decoded_value!r} without embedded quotes"
                )
        if component == "WebBrowser_RunJsInIeBySelector_v3" and not str(config.get("jsCode") or "").strip():
            raise ValueError(f"plan step {step_id} must supply page-specific jsCode")
        if component == "WebBrowser_RunJsInIeBySelector_v3":
            js_code = str(config.get("jsCode") or "")
            missing_interaction_markers = [
                marker
                for marker in (
                    "SHIZAI_WAITING_FOR_USER:login_required",
                    "SHIZAI_WAITING_FOR_USER:page_challenge",
                )
                if marker not in js_code
            ]
            if missing_interaction_markers and not flexible_policy:
                raise ValueError(
                    f"plan step {step_id} JavaScript must preserve login/challenge interaction markers: "
                    + ", ".join(missing_interaction_markers)
                )
            output_variable = str(config.get("output_variable") or "").strip()
            result_mode, mode_alias_used = _normalize_js_result_mode(config.get("result_mode"))
            if mode_alias_used:
                config["result_mode"] = result_mode
                normalization_warnings.append(
                    f"plan step {step_id} result_mode alias was normalized to {result_mode!r}"
                )
            if not output_variable:
                raise ValueError(f"plan step {step_id} must declare config.output_variable")
            if result_mode not in {"table", "json", "text", "scalar"}:
                raise ValueError(f"plan step {step_id} must declare config.result_mode as table, json, text, or scalar")
        inputs = [str(item).strip() for item in raw.get("inputs", []) if str(item).strip()]
        outputs = [str(item).strip() for item in raw.get("outputs", []) if str(item).strip()]
        expected_outputs = list(contract.get("outputs") or [])
        output_parameters = list(contract.get("output_parameters") or [])
        if output_parameters:
            expected_outputs = []
            for parameter in output_parameters:
                output_name = str(config.get(parameter) or "").strip()
                if not output_name:
                    raise ValueError(f"plan step {step_id} must configure output parameter {parameter}")
                expected_outputs.append(output_name)
        if component == "WebBrowser_RunJsInIeBySelector_v3":
            expected_outputs = [str(config["output_variable"]).strip()]
        if outputs != expected_outputs:
            if contract.get("canonical_outputs"):
                normalization_warnings.append(
                    f"plan step {step_id} outputs were canonicalized to the fixed component contract"
                )
                outputs = expected_outputs
            elif flexible_policy and not outputs:
                outputs = expected_outputs
                normalization_warnings.append(
                    f"plan step {step_id} outputs were filled from the live component contract"
                )
            elif flexible_policy:
                normalization_warnings.append(
                    f"plan step {step_id} output names differ from the live contract; runtime validation will decide compatibility"
                )
            else:
                raise ValueError(f"plan step {step_id} outputs must match the component contract: {expected_outputs}")
        if contract.get("plan_input_mode") == "none" and inputs:
            normalization_warnings.append(
                f"plan step {step_id} inputs were cleared because the component accepts no runtime variables"
            )
            inputs = []
        missing_inputs = [item for item in inputs if item not in produced_variables]
        if missing_inputs:
            if flexible_policy:
                normalization_warnings.append(
                    f"plan step {step_id} references external or not-yet-declared inputs: {missing_inputs}"
                )
            else:
                raise ValueError(f"plan step {step_id} consumes variables before they are produced: {missing_inputs}")
        disconnected_inputs = [
            item
            for item in inputs
            if variable_producers.get(item) and variable_producers[item] not in dependency_closure
        ]
        if disconnected_inputs:
            if flexible_policy:
                normalization_warnings.append(
                    f"plan step {step_id} has inputs whose producer dependency is model-defined: {disconnected_inputs}"
                )
            else:
                raise ValueError(
                    f"plan step {step_id} consumes variables without depending on their producers: {disconnected_inputs}"
                )
        produced_variables.update(outputs)
        variable_producers.update({item: step_id for item in outputs})
        for operation_id in mapped:
            operation_type = operation_types.get(operation_id, "unspecified")
            supported_operations = set(contract.get("operation_types") or [])
            if (
                analysis.get("intent") == "browser_rpa"
                and operation_type != "unspecified"
                and operation_type not in supported_operations
            ):
                # A direct search URL is a valid navigation-level
                # implementation of a search operation.  This keeps plans
                # compact when the site exposes a stable query parameter;
                # interactive input/submit components remain supported when
                # the model needs them.
                if (
                    operation_type == "search"
                    and "navigate" in supported_operations
                    and re.search(r"(?:[?&](?:q|query|keyword|wd|search|term)=|/search(?:[/?#]|$))", str(config.get("url") or ""), re.IGNORECASE)
                ):
                    covered_operations.add(operation_id)
                    continue
                if flexible_policy:
                    normalization_warnings.append(
                        f"plan step {step_id} maps {operation_type} to {component}; capability compatibility is advisory"
                    )
                else:
                    raise ValueError(
                        f"plan step {step_id} component {component} cannot cover {operation_type} operation {operation_id}; "
                        f"live capability operations are {sorted(supported_operations)}"
                    )
            covered_operations.add(operation_id)
        seen_ids.add(step_id)
        dependency_closures[step_id] = dependency_closure
        element_role = str(raw.get("element_role") or raw.get("role") or "").strip()
        # ``role`` is the compact catalog-facing spelling used by several
        # client models. Preserve it as the canonical ``element_role`` so a
        # plan that passes validation still carries enough semantic context
        # for the later scan/bind action. When a model omits both aliases,
        # derive a generic role only for components whose live contract says
        # that page evidence is required.
        if not element_role and contract.get("requires_elements"):
            lowered_component = component.casefold()
            if "setvalue" in lowered_component or "keyboard" in lowered_component:
                element_role = "搜索输入框"
            elif "datacollection" in lowered_component or "collect" in lowered_component:
                element_role = "结果列表字段及下一页或加载更多控件"
            if element_role:
                normalization_warnings.append(
                    f"plan step {step_id} element_role was inferred from its component contract"
                )
        normalized_steps.append(
            {
                "id": step_id,
                "order": index,
                "component": component,
                "purpose": purpose,
                "operation_ids": mapped,
                "depends_on": depends_on,
                "inputs": inputs,
                "outputs": outputs,
                "element_role": element_role or None,
                "config": config,
            }
        )
    if analysis.get("intent") == "browser_rpa" and not flexible_policy:
        first_contract = component_contracts[normalized_steps[0]["component"]]
        if "navigate" not in set(first_contract.get("operation_types") or []):
            raise ValueError("browser RPA plan must start with a live navigation-capable component")
    if analysis.get("intent") == "browser_rpa":
        first_config = normalized_steps[0]["config"]
        target_url = str(first_config.get("url") or "").strip()
        if not re.match(r"^https?://[^\s]+$", target_url, flags=re.IGNORECASE):
            if flexible_policy:
                target_url = str(
                    (plan.get("target") if isinstance(plan.get("target"), dict) else {}).get("url") or ""
                ).strip()
            if not re.match(r"^https?://[^\s]+$", target_url, flags=re.IGNORECASE):
                if flexible_policy:
                    normalization_warnings.append(
                        "browser plan has no explicit first-step URL yet; navigation can be resolved from model-selected config or page evidence"
                    )
                else:
                    raise ValueError("browser RPA first step must provide an explicit http(s) config.url")
    # Flexible plans may omit operation_ids, but an explicit page interaction
    # still needs a component that can perform it. Infer coverage from the live
    # component contracts, then reject plans that would open and collect from
    # the unmodified page.
    missing_interactions: list[str] = []
    for operation_id in sorted(operation_ids - covered_operations):
        operation_type = operation_types.get(operation_id)
        if operation_type not in {"search", "interact"}:
            continue
        implementation = next(
            (
                step
                for step in normalized_steps
                if operation_type
                in set(component_contracts[step["component"]].get("operation_types") or [])
            ),
            None,
        )
        if implementation is None and operation_type == "search":
            implementation = next(
                (
                    step
                    for step in normalized_steps
                    if "navigate"
                    in set(component_contracts[step["component"]].get("operation_types") or [])
                    and re.search(
                        r"(?:[?&](?:q|query|keyword|wd|search|term)=|/search(?:[/?#]|$))",
                        str(step["config"].get("url") or ""),
                        re.IGNORECASE,
                    )
                ),
                None,
            )
        if implementation is None:
            instruction = next(
                (
                    str(item.get("instruction") or "").strip()
                    for item in analysis.get("operations", [])
                    if isinstance(item, dict) and str(item.get("id")) == operation_id
                ),
                "",
            )
            missing_interactions.append(
                f"{operation_id} ({operation_type})" + (f": {instruction}" if instruction else "")
            )
            continue
        implementation["operation_ids"].append(operation_id)
        covered_operations.add(operation_id)
        normalization_warnings.append(
            f"operation {operation_id} was mapped to {implementation['id']} from its live interaction contract"
        )
    if missing_interactions:
        raise ValueError(
            "plan does not implement analyzed page interactions: "
            + "; ".join(missing_interactions)
            + "; add installed input/submit-capable component steps or an explicit search-result URL"
        )

    # A structured collection component may own the output operation as part
    # of its contract.  Client models sometimes split the final "输出" clause
    # into a synthetic extra operation even though no separate output step is
    # needed.  Fold that output operation into the collection step when the
    # live component explicitly advertises output; keep every other uncovered
    # operation strict so missing work is still rejected.
    uncovered_before_repair = sorted(operation_ids - covered_operations)
    for operation_id in uncovered_before_repair:
        if operation_types.get(operation_id) != "output":
            continue
        output_owner = next(
            (
                step
                for step in reversed(normalized_steps)
                if "output" in set(component_contracts[step["component"]].get("operation_types") or [])
                and "extract" in set(component_contracts[step["component"]].get("capabilities") or [])
            ),
            None,
        )
        if output_owner is None:
            continue
        output_owner["operation_ids"].append(operation_id)
        covered_operations.add(operation_id)
        normalization_warnings.append(
            f"operation {operation_id} was merged into {output_owner['id']} because its live collection component owns output"
        )
    uncovered = sorted(operation_ids - covered_operations)
    if uncovered:
        if flexible_policy:
            normalization_warnings.append(
                f"analyzed operations not explicitly mapped by the model plan: {uncovered}"
            )
        else:
            raise ValueError(f"plan does not cover analyzed operations: {uncovered}")
    output_contract = dict(plan.get("output_contract") or {})
    if not isinstance(output_contract, dict):
        raise ValueError("plan.output_contract must be an object")
    plan["output_contract"] = output_contract
    requested_fields = [
        str(item).strip() for item in (analysis.get("evidence") or {}).get("requested_fields", []) if str(item).strip()
    ]
    output_columns = [str(item).strip() for item in output_contract.get("columns", []) if str(item).strip()]
    if requested_fields and output_columns != requested_fields:
        if flexible_policy and not output_columns:
            output_columns = requested_fields
            output_contract["columns"] = requested_fields
            normalization_warnings.append(
                "output_contract.columns was filled from the requested fields"
            )
        elif flexible_policy:
            normalization_warnings.append(
                f"output columns differ from the requested fields; result quality will be checked after execution: {requested_fields}"
            )
        else:
            raise ValueError(
                f"plan.output_contract.columns must exactly preserve the requested field order: {requested_fields}"
            )
    analyzed_limits = [
        int(item["value"])
        for item in (analysis.get("evidence") or {}).get("limits", [])
        if isinstance(item, dict) and item.get("value") is not None
    ]
    if analyzed_limits:
        raw_planned_limit = output_contract.get("max_rows")
        try:
            planned_limit = int(raw_planned_limit) if raw_planned_limit not in (None, "") else None
        except (TypeError, ValueError) as exc:
            if flexible_policy:
                planned_limit = None
                normalization_warnings.append(
                    f"output_contract.max_rows is not numeric ({raw_planned_limit!r}); final output evidence remains authoritative"
                )
            else:
                raise ValueError("plan.output_contract.max_rows must preserve the requested row limit") from exc
        if planned_limit != analyzed_limits[-1]:
            if flexible_policy and planned_limit is None:
                output_contract["max_rows"] = analyzed_limits[-1]
                planned_limit = analyzed_limits[-1]
                normalization_warnings.append(
                    "output_contract.max_rows was filled from the analyzed numeric target"
                )
            elif flexible_policy:
                normalization_warnings.append(
                    f"output_contract.max_rows differs from the analyzed target {analyzed_limits[-1]}; final output evidence remains authoritative"
                )
            else:
                raise ValueError(f"plan.output_contract.max_rows must equal the requested limit {analyzed_limits[-1]}")
        limit_steps = []
        for step in normalized_steps:
            contract = component_contracts[step["component"]]
            if "limit" not in set(contract.get("capabilities") or []):
                continue
            hints = contract.get("configuration_hints") if isinstance(contract.get("configuration_hints"), dict) else {}
            parameters = list(hints.get("row_limit_parameters") or [])
            parameters.extend(name for name in ("max_rows", "collect_items", "limit") if name not in parameters)
            if planned_limit is not None and any(
                str(_resolve_plan_semantic_value(step["config"].get(name), output_contract))
                == str(planned_limit)
                for name in parameters
                if step["config"].get(name) not in (None, "")
            ):
                limit_steps.append(step["id"])
        if not limit_steps:
            if flexible_policy:
                normalization_warnings.append(
                    f"no component explicitly declares the {planned_limit}-row limit; the model-selected loop/output contract will be checked at runtime"
                )
            else:
                raise ValueError(
                    f"plan must configure a live limit-capable component for the requested {planned_limit} rows"
                )
    pagination = (analysis.get("evidence") or {}).get("pagination")
    pagination_required = bool(
        isinstance(pagination, dict) and pagination.get("required_for_completion")
    )
    pagination_capable_steps = [
        step for step in normalized_steps
        if "paginate" in set(component_contracts[step["component"]].get("capabilities") or [])
    ]
    pagination_steps = []
    for step in pagination_capable_steps:
        contract = component_contracts[step["component"]]
        hints = contract.get("configuration_hints") if isinstance(contract.get("configuration_hints"), dict) else {}
        parameters = list(hints.get("pagination_parameters") or [])
        parameters.extend(
            name
            for name in ("region", "collect_pages", "page_type", "selector_page", "selector_more")
            if name not in parameters
        )
        configured = any(step["config"].get(name) not in (None, "", False) for name in parameters)
        planning_patterns = contract.get("planning_patterns") if isinstance(contract.get("planning_patterns"), list) else []
        relevant_patterns = [
            item
            for item in planning_patterns
            if isinstance(item, dict) and "max_rows" in str(item.get("when") or "")
        ]
        if relevant_patterns:
            configured = False
            for pattern in relevant_patterns:
                semantic_config = pattern.get("semantic_config") if isinstance(pattern.get("semantic_config"), dict) else {}
                pattern_matches = True
                for name, expected in semantic_config.items():
                    actual = _resolve_plan_semantic_value(step["config"].get(name), output_contract)
                    if isinstance(expected, str) and expected.startswith("$defer_"):
                        continue
                    if expected == "$output_contract.max_rows":
                        expected = output_contract.get("max_rows")
                    elif isinstance(expected, str) and expected.startswith("$choose_"):
                        if actual in (None, ""):
                            pattern_matches = False
                        continue
                    if actual != expected:
                        pattern_matches = False
                if pattern_matches:
                    configured = True
                    break
        if configured:
            pagination_steps.append(step["id"])
    if pagination_required and not pagination_capable_steps:
        if flexible_policy:
            normalization_warnings.append(
                "the model selected a pagination strategy without a component advertising paginate; verify the loop/page control at runtime"
            )
        else:
            raise ValueError(
                "plan must use a live pagination-capable component for the analyzed pagination constraint"
            )
    if pagination_required and not pagination_steps:
        if flexible_policy:
            normalization_warnings.append(
                "pagination is not configured on a paginate-capable component; the model may implement it with explicit loop/click/JS steps"
            )
        else:
            raise ValueError(
                "plan must configure the pagination strategy on its live pagination-capable component; "
                "collect until max_rows or the next page is unavailable"
            )
    output_file = str(
        output_contract.get("file")
        or output_contract.get("filename")
        or output_contract.get("file_path")
        or output_contract.get("output_file")
        or ""
    ).strip()
    destination = str(
        output_contract.get("destination")
        or output_contract.get("output_path")
        or output_contract.get("filePath")
        or output_contract.get("file_path")
        or ""
    ).strip()
    # Resolve directory-plus-suffix forms into the concrete file path used by
    # output evidence and native Excel components. Prefer the collection
    # step's native filename_suffix when the model supplied one.
    collection_config = next(
        (
            step.get("config")
            for step in reversed(normalized_steps)
            if step.get("component") == "WebBrowser_DataCollectionV5"
            and isinstance(step.get("config"), dict)
        ),
        None,
    )
    if not output_file and isinstance(collection_config, dict):
        output_file = _collection_output_file(collection_config, fallback="")
    if not output_file and destination:
        output_file = _collection_output_file(
            {
                "destination": destination,
                "filename_suffix": output_contract.get("filename_suffix"),
            },
            fallback="result.xlsx",
        )
    if output_file and destination:
        output_path = Path(output_file)
        if not output_path.is_absolute() and output_path.parent == Path("."):
            output_file = str(Path(destination) / output_path.name)
    if output_file and not Path(output_file).suffix:
        output_format = str(output_contract.get("format") or "").strip().lstrip(".")
        if output_format:
            output_file = f"{output_file}.{output_format}"
    if output_file:
        output_contract["file"] = output_file
    for step in normalized_steps:
        config = step["config"]
        if step["component"] == "WebBrowser_RunJsInIeBySelector_v3" and config.get("result_mode") == "table":
            declared_columns = [str(item).strip() for item in config.get("output_columns", []) if str(item).strip()]
            if declared_columns != output_columns and not flexible_policy:
                raise ValueError("table JavaScript config.output_columns must match plan.output_contract.columns")
            if declared_columns != output_columns and flexible_policy:
                normalization_warnings.append(
                    "table JavaScript columns differ from output_contract.columns; output evidence will be checked after execution"
                )
            if output_contract.get("max_rows") not in (None, ""):
                try:
                    js_limit = int(_resolve_plan_semantic_value(config.get("max_rows"), output_contract))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "table JavaScript config.max_rows must match plan.output_contract.max_rows"
                    ) from exc
                if js_limit != int(output_contract["max_rows"]) and not flexible_policy:
                    raise ValueError("table JavaScript config.max_rows must match plan.output_contract.max_rows")
                if js_limit != int(output_contract["max_rows"]) and flexible_policy:
                    normalization_warnings.append(
                        "table JavaScript row limit differs from output_contract.max_rows; output evidence will be checked after execution"
                    )
        if step["component"] == "Excel_Cs_NewV4" and output_file:
            planned_name = Path(str(config.get("name") or "")).name
            if planned_name != Path(output_file).name:
                if flexible_policy:
                    normalization_warnings.append(
                        "Excel output filename differs from output_contract.file; the actual output path will be reported"
                    )
                else:
                    raise ValueError("Excel output filename must match plan.output_contract.file")
    normalized = {
        "schema": "shizai-rpa-execution-plan/v1",
        "validation_mode": "flexible" if flexible_policy else "legacy_strict",
        "analysis_id": analysis["analysis_id"],
        "query": analysis["query"],
        "name": str(plan.get("name") or "RPA 自动化流程").strip(),
        "target": plan.get("target") if isinstance(plan.get("target"), dict) else {},
        "steps": normalized_steps,
        "output_contract": output_contract,
        "interaction_policy": analysis.get("interaction_policy"),
    }
    normalized["plan_id"] = (
        "plan-"
        + hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    )
    return {
        "kind": "validated_rpa_execution_plan",
        "valid": True,
        "plan": normalized,
        "component_sequence": [item["component"] for item in normalized_steps],
        "live_catalog_validated": installed_components is not None,
        "constraint_coverage": {
            "limit_steps": limit_steps if analyzed_limits else [],
            "pagination_steps": pagination_steps,
        },
        "normalization_warnings": normalization_warnings,
        "policy": {
            "mode": "model_decides" if flexible_policy else "legacy_strict",
            "strategy_checks": "advisory" if flexible_policy else "enforced",
        },
        "message": "本次请求的原子步骤计划已通过覆盖、依赖、组件和变量校验。",
    }


def _mcp_component_contracts(
    available_components: Iterable[str] | dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], set[str] | None]:
    """Build the live component view used by the thin MCP planning boundary.

    The normal execution-plan validator intentionally contains compatibility
    checks for the older browser recipe.  MCP must not inherit those recipe
    rules, so this helper only combines the installed catalog with stable
    compatibility metadata and keeps the live argument surface authoritative.
    """
    compatibility = _rpa_contract_map()
    if isinstance(available_components, dict):
        installed = {str(name).strip() for name in available_components if str(name).strip()}
        contracts: dict[str, dict[str, Any]] = {}
        for raw_name, record in available_components.items():
            component = str(raw_name).strip()
            if not component:
                continue
            base = compatibility.get(component, {})
            if isinstance(record, dict) and record:
                live = _catalog_rpa_contract(component, record)
                merged = dict(base)
                merged.update(live)
                # Keep stable default output names for built-in components;
                # native_output_parameters still lets explicit assignments
                # replace those defaults. Dynamic components retain live
                # output parameters as required plan configuration.
                if base.get("outputs"):
                    merged["outputs"] = list(base.get("outputs") or [])
                    merged["output_parameters"] = []
                contracts[component] = merged
            else:
                if not base:
                    # A name-only row without a compatibility contract has no
                    # native shape to materialize. Do not turn arbitrary names
                    # into executable components; real dynamic rows must carry
                    # their catalog definition.
                    continue
                contracts[component] = dict(base)
                # Test fixtures and older designer dictionaries sometimes
                # expose only the component name. Keep the compatibility
                # defaults for materialization, but defer argument checking to
                # the native writer until the live argument surface is known.
                contracts[component]["_catalog_metadata_missing"] = True
        return contracts, installed
    if available_components is None:
        return {
            name: {**dict(contract), "_catalog_metadata_missing": True}
            for name, contract in compatibility.items()
        }, None
    installed = {str(item).strip() for item in available_components if str(item).strip()}
    contracts = {name: dict(compatibility[name]) for name in installed if name in compatibility}
    return contracts, installed


def _bind_unambiguous_plan_inputs(
    *,
    step_id: str,
    component: str,
    contract: dict[str, Any],
    config: dict[str, Any],
    inputs: list[str],
    available_variables: set[str],
    variable_types: dict[str, set[str]],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Map logical step inputs to native parameters when the live contract is unambiguous."""
    missing = [name for name in inputs if name not in available_variables]
    if missing:
        raise ValueError(
            f"plan step {step_id} references inputs without a preceding producer: {missing}"
        )
    if contract.get("_catalog_metadata_missing"):
        return config, []
    explicitly_bound = {
        str(value.get("variable") or "").strip()
        for value in config.values()
        if isinstance(value, dict) and set(value) == {"variable"}
    }
    remaining_variables = [name for name in inputs if name not in explicitly_bound]
    if not remaining_variables:
        return config, []

    output_parameters = {
        str(item).strip()
        for key in ("output_parameters", "native_output_parameters")
        for item in contract.get(key, [])
        if str(item).strip()
    }
    deferred = {str(item).strip() for item in contract.get("deferred_bindings", []) if str(item).strip()}
    candidate_parameters = [
        str(item).split("=", 1)[0].strip()
        for item in contract.get("inputs", [])
        if str(item).split("=", 1)[0].strip()
        and str(item).split("=", 1)[0].strip() not in output_parameters
        and str(item).split("=", 1)[0].strip() not in deferred
        and config.get(str(item).split("=", 1)[0].strip()) in (None, "")
    ]
    parameter_types = contract.get("parameter_types", {})
    matches: dict[str, list[str]] = {}
    for variable in remaining_variables:
        actual_types = variable_types.get(variable, set())
        typed_matches = [
            parameter
            for parameter in candidate_parameters
            if actual_types
            and set(parameter_types.get(parameter, []))
            and not actual_types.isdisjoint(set(parameter_types.get(parameter, [])))
        ]
        matches[variable] = typed_matches or [
            parameter
            for parameter in candidate_parameters
            if not actual_types or not set(parameter_types.get(parameter, []))
        ]
    bindings: list[tuple[str, str]] = []
    pending = list(remaining_variables)
    while pending:
        selected: tuple[str, str] | None = next(
            (
                (variable, choices[0])
                for variable in pending
                if len(choices := [item for item in matches[variable] if item in candidate_parameters]) == 1
            ),
            None,
        )
        if selected is None:
            unique_parameters = [
                parameter
                for parameter in candidate_parameters
                if sum(parameter in matches[variable] for variable in pending) == 1
            ]
            if unique_parameters:
                parameter = unique_parameters[0]
                variable = next(item for item in pending if parameter in matches[item])
                selected = (variable, parameter)
        if selected is None:
            choices = {variable: matches[variable] for variable in pending}
            raise ValueError(
                f"plan step {step_id} cannot map inputs {pending} to {component} parameters unambiguously; "
                f"candidate parameters: {choices}. Set config.<parameter>={{\"variable\":\"name\"}} explicitly"
            )
        variable, parameter = selected
        config[parameter] = {"variable": variable}
        bindings.append((variable, parameter))
        pending.remove(variable)
        candidate_parameters.remove(parameter)
    return config, bindings


def _native_collection_review(
    analysis: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    installed: set[str] | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Review native collection stages without claiming page compatibility."""
    if not isinstance(analysis, dict) or analysis.get("intent") != "browser_rpa":
        return None
    operations = analysis.get("operations") if isinstance(analysis.get("operations"), list) else []
    operation_types = {
        str(item.get("type") or "").strip().casefold()
        for item in operations
        if isinstance(item, dict)
    }
    evidence = analysis.get("evidence") if isinstance(analysis.get("evidence"), dict) else {}
    requested_fields = evidence.get("requested_fields")
    requests_collection = bool({"extract", "collect", "scrape"} & operation_types) or bool(
        isinstance(requested_fields, list) and requested_fields
    )
    if not requests_collection:
        return None
    collection_evidence = evidence.get("collection") if isinstance(evidence.get("collection"), dict) else {}
    record_shape = str(collection_evidence.get("shape") or "flat").strip().casefold()
    plan_shape = str(
        (plan or {}).get("collection_shape")
        or (plan or {}).get("record_shape")
        or ""
    ).strip().casefold()
    if plan_shape in {"flat", "hierarchical"}:
        record_shape = plan_shape
    expected_stages = collection_evidence.get("stages")
    if not isinstance(expected_stages, list):
        expected_stages = []
    expected_stage_ids = [
        str(item.get("id") or "").strip()
        for item in expected_stages
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]

    # Compatibility defaults alone are not evidence of installed components.
    if installed is None:
        return {
            "status": "catalog_unchecked", "runtime_verified": False,
            "model_directive": "尚未提供实时组件目录；先查询原生采集契约，再选择采集方案。",
        }
    candidates: list[str] = []
    for component, contract in contracts.items():
        if not isinstance(contract, dict) or contract.get("materializable") is False:
            continue
        capabilities = {str(item).casefold() for item in contract.get("capabilities") or []}
        if "extract" in capabilities and "output" in capabilities:
            candidates.append(str(component))
    candidates.sort(key=lambda name: ("datacollection" not in name.casefold(), name.casefold()))
    selected: list[str] = []
    selected_steps: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        component = str(step.get("component") or "").strip()
        contract = contracts.get(component) or {}
        capabilities = {str(item).casefold() for item in contract.get("capabilities") or []}
        if "extract" in capabilities:
            selected.append(component)
            selected_steps.append(step)
    native_selected = next((component for component in selected if component in candidates), None)
    native_steps = [
        step
        for step in selected_steps
        if str(step.get("component") or "").strip() in candidates
    ]
    selected_stage_ids: list[str] = []
    selected_stage_outputs: list[str] = []
    child_parent_refs: list[str] = []
    for step in native_steps:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        stage = (
            step.get("collection_stage")
            or step.get("collection_role")
            or config.get("collection_stage")
            or config.get("collection_role")
        )
        stage = str(stage or "").strip()
        if stage:
            selected_stage_ids.append(stage)
        output = config.get("output_file") or config.get("var0") or config.get("output_variable")
        if not output and isinstance(step.get("outputs"), list) and step.get("outputs"):
            output = step.get("outputs")[0]
        output = str(output or "").strip()
        if output:
            selected_stage_outputs.append(output)
        if stage == "child_collection":
            child_parent_refs.append(
                str(step.get("parent_step_id") or config.get("parent_step_id") or "").strip()
            )
    alignment_steps: list[dict[str, Any]] = []
    alignment_keys = [
        str(
            step.get("merge_key")
            or (step.get("config") or {}).get("merge_key")
            or ""
        ).strip()
        for step in steps
        if isinstance(step, dict)
        and (
            str(step.get("collection_stage") or "").strip() == "record_alignment"
            or str((step.get("config") or {}).get("collection_stage") or "").strip() == "record_alignment"
        )
    ]
    for step in steps:
        if not isinstance(step, dict):
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        stage = str(step.get("collection_stage") or config.get("collection_stage") or "").strip()
        if stage == "record_alignment":
            alignment_steps.append(step)
    alignment_capabilities = sorted(
        {
            str(capability).casefold()
            for step in alignment_steps
            for capability in (contracts.get(str(step.get("component") or "").strip(), {}) or {}).get("capabilities", [])
        }
    )
    review = {
        "available_candidates": candidates[:5],
        "selected_extract_components": selected,
        "record_shape": record_shape,
        "expected_stages": expected_stage_ids,
        "selected_native_stages": selected_stage_ids,
        "selected_stage_outputs": selected_stage_outputs,
        "child_parent_refs": child_parent_refs,
        "alignment_keys": alignment_keys,
        "alignment_capabilities": alignment_capabilities,
        "runtime_verified": False,
    }
    if record_shape == "hierarchical":
        # A long script is especially risky for parent/child data: it tends to
        # collapse the two page states into one timeout-prone extraction. Make
        # the native staged choice explicit in the model-visible review.
        parent_present = "parent_collection" in selected_stage_ids
        child_present = "child_collection" in selected_stage_ids
        alignment_present = bool(
            alignment_steps
            and set(alignment_capabilities).intersection({"transform", "output"})
        )
        stage_outputs_distinct = len(selected_stage_outputs) >= 2 and len(set(selected_stage_outputs)) >= 2
        child_reference_present = bool(child_parent_refs and all(child_parent_refs))
        alignment_key_present = bool(alignment_keys and all(alignment_keys))
        if not candidates:
            return {
                **review,
                "status": "no_candidate",
                "recommended_component": None,
                "model_directive": (
                    "层级采集需要原生父记录/子记录阶段，但当前实时目录没有可物化的 extract+output 候选；"
                    "先补查目录或报告能力缺口，不能退化成一个未验证的大脚本。"
                ),
            }
        if not native_steps:
            return {
                **review,
                "status": "stage_plan_required",
                "recommended_component": candidates[0],
                "model_directive": (
                    "检测到父子记录；必须设计 parent_collection、child_collection 两个独立的 "
                    f"{candidates[0]} 步骤，并增加 record_alignment/输出步骤。"
                    "页面展开、切换父记录和分页可以由短动作组件或短脚本补足；不能用一个长 JS 同时抓两层。"
                ),
            }
        if not (
            parent_present
            and child_present
            and len(set(selected_stage_ids)) >= 2
            and alignment_present
            and stage_outputs_distinct
            and child_reference_present
            and alignment_key_present
        ):
            return {
                **review,
                "status": "stage_plan_required",
                "recommended_component": candidates[0],
                "model_directive": (
                    "层级采集的原生阶段尚未完整：分别声明 collection_stage=parent_collection 和 "
                    "collection_stage=child_collection，使用不同输出变量/字段绑定/文件，并补上 "
                    "record_alignment（声明 parent_step_id 与 merge_key）后再运行。"
                ),
            }
        return {
            **review,
            "status": "native_staged",
            "recommended_component": candidates[0],
            "model_directive": (
                "已声明父记录、子记录和对齐阶段；分别在当前页面状态执行两个原生采集预览，"
                "确认 parent_step_id/merge_key 对齐、非空行和输出文件互不覆盖后再运行。"
            ),
        }
    if native_selected:
        return {
            **review,
            "status": "native_selected",
            "recommended_component": native_selected,
            "model_directive": (
                f"已选择原生采集候选 {native_selected}，尚未验证页面兼容性；绑定当前页面字段并执行原生预览，"
                "核对非空行、字段对应关系和完整性后再运行。"
            ),
        }
    if not candidates:
        return {
            **review,
            "status": "no_candidate",
            "recommended_component": None,
            "model_directive": (
                "本次提供的目录中没有可物化的 extract+output 候选；补查目录，"
                "依据实际契约和页面证据组合组件，并报告能力缺口。"
            ),
        }
    # An initial navigation-only skeleton has not made an extraction choice.
    if not selected:
        return {**review, "status": "selection_pending", "recommended_component": candidates[0],
                "model_directive": "采集尚未设计；取得页面后评估原生采集候选，允许先保存导航骨架。"}
    reasons = {
        step["id"]: step["native_alternative_reason"].strip()
        for step in steps if step.get("component") in selected
        and isinstance(step.get("native_alternative_reason"), str) and step["native_alternative_reason"].strip()
    }
    if reasons:
        return {**review, "status": "reason_recorded", "recommended_component": candidates[0],
                "model_reported_reasons": reasons,
                "model_directive": "已记录模型报告的替代原因；原因文本不是验证证据，仍需核对真实页面及输出。"}
    return {
        **review,
        "status": "required_review",
        "recommended_component": candidates[0],
        "model_directive": (
            f"尚未选择原生采集候选 {candidates[0]}；必须先比较其契约和当前页面，适用时优先使用。"
            "替代时在步骤 native_alternative_reason 中记录具体限制与验证证据；不能因脚本容易生成而跳过。"
        ),
    }


def validate_mcp_rpa_plan(
    analysis: dict[str, Any] | None,
    plan: dict[str, Any],
    *,
    available_components: Iterable[str] | dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate only the MCP/CLI boundary of a model-owned RPA plan.

    This is deliberately not a workflow planner.  It does not infer intent,
    enforce operation coverage, require a browser-first graph, select a
    pagination owner, or compare output fields with the analyzed request.
    Those choices belong to the calling model.  The function only protects
    the native writer from malformed graph entries and clearly invalid live
    component/config references; runtime validation remains authoritative.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if analysis is not None and not isinstance(analysis, dict):
        raise ValueError("analysis must be an object when supplied")
    analysis_id = str((analysis or {}).get("analysis_id") or plan.get("analysis_id") or "").strip()
    supplied_analysis_id = str(plan.get("analysis_id") or "").strip()
    if supplied_analysis_id and analysis_id and supplied_analysis_id != analysis_id:
        raise ValueError("plan.analysis_id must match the supplied analysis")

    raw_steps = plan.get("steps")
    warnings: list[str] = []
    if not isinstance(raw_steps, list) and isinstance(plan.get("components"), list):
        raw_steps = plan.get("components")
        warnings.append("plan.components was normalized to plan.steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("plan.steps (or plan.components) must contain at least one component step")

    contracts, installed = _mcp_component_contracts(available_components)
    semantic_aliases = {
        "output_file", "output_path", "file_path", "destination", "file", "filename", "filename_suffix",
        "columns", "max_rows", "pagination", "data_attributes", "output_variable", "outputVariable",
        "output_columns", "result_mode", "page_scope", "element_ids", "collection_stage",
        "collection_role", "parent_step_id", "merge_key", "native_alternative_reason",
        "var0", "var1", "var2", "collection_output",
    }
    normalized_steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    variable_types: dict[str, set[str]] = {}
    available_variables: set[str] = set()
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"plan.steps[{index - 1}] must be an object")
        step = dict(raw_step)
        component_input = str(step.get("component") or step.get("type") or "").strip()
        component = RPA_COMPONENT_TYPE_ALIASES.get(component_input, component_input)
        if not component:
            raise ValueError(f"plan.steps[{index - 1}] is missing component")
        if installed is not None and component not in installed and component_input not in installed:
            raise ValueError(f"plan step {step.get('id') or index} component is not installed in the live catalog: {component}")
        contract = contracts.get(component)
        if contract is None:
            raise ValueError(f"plan step {step.get('id') or index} uses unsupported component: {component}")
        if contract.get("materializable") is False:
            reasons = "; ".join(contract.get("materialization_errors") or ["component is not materializable"])
            raise ValueError(f"plan step {step.get('id') or index} component cannot be materialized: {component}: {reasons}")
        step_id = str(step.get("id") or step.get("step_id") or f"step-{index}").strip()
        if not step_id or step_id in seen_ids:
            raise ValueError(f"plan step id is empty or duplicated: {step_id!r}")
        warnings.extend(f"plan step {step_id}: {note}" for note in contract.get("runtime_preconditions", []))
        config = step.get("config", {})
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError(f"plan step {step_id}.config must be an object")
        config = dict(config)
        if component == "WebBrowser_DataCollectionV5":
            collection_stage = _collection_stage_value(step)
            if collection_stage:
                config["collection_stage"] = collection_stage
        inputs = [str(item).strip() for item in step.get("inputs", []) if str(item).strip()]
        config, inferred_input_bindings = _bind_unambiguous_plan_inputs(
            step_id=step_id,
            component=component,
            contract=contract,
            config=config,
            inputs=inputs,
            available_variables=available_variables,
            variable_types=variable_types,
        )
        warnings.extend(
            f"plan step {step_id} input {variable} was bound to config.{parameter} from the live component contract"
            for variable, parameter in inferred_input_bindings
        )
        parameter_names = set(str(item).strip() for item in contract.get("parameter_names", []) if str(item).strip())
        if not parameter_names:
            for item in (*contract.get("inputs", []), *contract.get("options", [])):
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(item).split("=", 1)[0].strip()
                if name:
                    parameter_names.add(name)
        # A catalog row without argument metadata is still useful for listing
        # and materialization; defer its argument validation to the native CLI.
        if parameter_names and not contract.get("_catalog_metadata_missing"):
            unknown = sorted(set(str(key) for key in config) - parameter_names - semantic_aliases)
            if unknown:
                raise ValueError(
                    f"plan step {step_id} config contains parameters not declared by the live component contract: "
                    + ", ".join(unknown)
                )
        for config_name, config_value in config.items():
            choices = contract.get("parameter_choices", {}).get(config_name)
            # Compatibility components accept documented semantic aliases.
            # Generic nodes are encoded directly, so literal dropdown values
            # must preserve the live enum's JSON type (3 differs from "3").
            is_reference = isinstance(config_value, dict) and set(config_value) in ({"variable"}, {"expression"})
            expected_types = set(contract.get("parameter_types", {}).get(config_name, []))
            handle_input = (expected_types and all(t.startswith("handle:") for t in expected_types)
                            and config_name not in contract.get("native_output_parameters", []))
            if (
                isinstance(config_value, dict)
                and set(config_value) == {"variable"}
                and (component in _NATIVE_RPA_COMPONENTS or handle_input)
            ):
                variable = config_value["variable"]
                if not isinstance(variable, str) or variable not in available_variables:
                    raise ValueError(
                        f"plan step {step_id} config.{config_name}: variable {variable!r} "
                        "has no preceding producer in this plan; select a component whose live contract "
                        "declares that output and place it before this step"
                    )
            if handle_input and not is_reference:
                raise ValueError(
                    f"plan step {step_id} config.{config_name} requires a runtime object {sorted(expected_types)}, "
                    f"not a literal {config_value!r}; select a live component that creates or obtains this object, "
                    'then reference its output with {"variable":"output_name"} and execute that producer '
                    "before using the object or inspecting its page"
                )
            if component not in _NATIVE_RPA_COMPONENTS and isinstance(config_value, dict) and set(config_value) == {"variable"}:
                variable = config_value["variable"]
                actual_types = variable_types.get(variable, set()) if isinstance(variable, str) else set()
                expected_types = set(contract.get("parameter_types", {}).get(config_name, []))
                if actual_types and expected_types and actual_types.isdisjoint(expected_types):
                    raise ValueError(
                        f"plan step {step_id} config.{config_name}: variable {variable!r} has native types "
                        f"{sorted(actual_types)}, but this input requires {sorted(expected_types)}; "
                        "select a producer with a compatible output or add an explicit native conversion step"
                    )
            literal_value = config_value["literal"] if isinstance(config_value, dict) and set(config_value) == {"literal"} else config_value
            if component not in _NATIVE_RPA_COMPONENTS and choices and not is_reference:
                def same_literal(expected: Any) -> bool:
                    both_numbers = (isinstance(expected, (int, float)) and not isinstance(expected, bool)
                                    and isinstance(literal_value, (int, float)) and not isinstance(literal_value, bool))
                    return (type(expected) is type(literal_value) or both_numbers) and expected == literal_value

                if not any(same_literal(choice) for choice in choices):
                    raise ValueError(f"plan step {step_id} config.{config_name} must match a typed semantic_choices value: "
                                     f"{json.dumps(choices, ensure_ascii=False)}; received {json.dumps(config_value, ensure_ascii=False)}")
            if isinstance(config_value, str) and re.fullmatch(r"\$[A-Za-z_]\w*", config_value):
                warnings.append(
                    f"plan step {step_id} config.{config_name}: {config_value!r} is a literal string; "
                    'for a variable reference use {"variable":"' + config_value[1:] + '"}'
                )
            if not isinstance(config_value, str) or len(config_value) < 2:
                continue
            if config_value[0] != '"' or config_value[-1] != '"':
                continue
            try:
                decoded = json.loads(config_value)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, str):
                raise ValueError(
                    f"plan step {step_id} config.{config_name} uses a native quoted default; "
                    f"pass the semantic JSON string {decoded!r} without embedded quotes"
                )
        for field in ("depends_on", "inputs", "outputs", "element_ids", "operation_ids"):
            if field in step and step[field] is not None and not isinstance(step[field], list):
                raise ValueError(f"plan step {step_id}.{field} must be an array when supplied")
        if "element_ids" in step:
            invalid_elements = [item for item in step.get("element_ids") or [] if not str(item).strip()]
            if invalid_elements:
                raise ValueError(f"plan step {step_id}.element_ids contains an empty id")
        # outputs is derived data. Native catalog names such as var_name/var0
        # identify assignment slots; the values configured in those slots are
        # logical variables. Keeping both representations model-editable caused
        # plans that passed preflight but could never pass native validation.
        output_parameters = [
            str(item).strip() for item in contract.get("output_parameters", []) if str(item).strip()
        ]
        try:
            expected_outputs = _logical_plan_outputs(
                component,
                contract,
                config,
                require_assignments=component in _NATIVE_RPA_COMPONENTS,
            )
        except ValueError as exc:
            raise ValueError(f"plan step {step_id} {exc}") from exc
        supplied_outputs = [str(item).strip() for item in step.get("outputs", []) if str(item).strip()]
        if supplied_outputs != expected_outputs:
            native_slots = {
                str(item).strip()
                for item in contract.get("native_output_parameters", [])
                if str(item).strip()
            }
            copied_native_slots = bool(supplied_outputs) and set(supplied_outputs).issubset(native_slots)
            reason = "copied native assignment slots" if copied_native_slots else "differed from the live contract"
            warnings.append(
                f"plan step {step_id} outputs {reason}; MCP generated logical outputs {expected_outputs}"
            )
        step["outputs"] = expected_outputs
        # Reassignment must forget previous type knowledge, including outputs
        # whose type is conditional, unknown, or handled by compatibility code.
        for output in step.get("outputs") or []:
            if isinstance(output, str):
                variable_types.pop(output, None)
                available_variables.add(output)
        assignment_parameters = output_parameters or [
            str(item).strip()
            for item in contract.get("native_output_parameters", [])
            if str(item).strip()
        ]
        for output, parameter in zip(step.get("outputs") or [], assignment_parameters):
            if isinstance(output, str) and output:
                variable_types[output] = set(contract.get("parameter_types", {}).get(parameter, []))
        step.update({"id": step_id, "order": index, "component": component, "config": config})
        normalized_steps.append(step)
        seen_ids.add(step_id)

    output_contract = plan.get("output_contract", plan.get("output", {}))
    if output_contract is None:
        output_contract = {}
    if not isinstance(output_contract, dict):
        raise ValueError("plan.output_contract must be an object when supplied")
    output_contract = dict(output_contract)
    if not any(output_contract.get(key) for key in ("file", "filename", "file_path")) and output_contract.get("output_file"):
        output_contract["file"] = output_contract["output_file"]
        warnings.append("output_contract.output_file was normalized to output_contract.file")
    target = plan.get("target")
    if target is not None and not isinstance(target, dict):
        raise ValueError("plan.target must be an object when supplied")
    normalized_plan = dict(plan)
    normalized_plan.update(
        {
            "schema": "shizai-rpa-execution-plan/v1",
            "validation_mode": "model_decides",
            "analysis_id": analysis_id or supplied_analysis_id,
            "query": str((analysis or {}).get("query") or plan.get("query") or "").strip(),
            "name": str(plan.get("name") or "RPA 自动化流程").strip(),
            "target": target if isinstance(target, dict) else {},
            "steps": normalized_steps,
            "output_contract": dict(output_contract),
        }
    )
    normalized_plan.pop("plan_id", None)
    normalized_plan["plan_id"] = (
        "plan-"
        + hashlib.sha256(json.dumps(normalized_plan, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    )
    native_collection_review = _native_collection_review(
        analysis,
        normalized_steps,
        contracts,
        installed,
        normalized_plan,
    )
    if native_collection_review and native_collection_review.get("status") in {
        "required_review",
        "stage_plan_required",
    }:
        warnings.append(str(native_collection_review.get("model_directive") or ""))
    return {
        "kind": "validated_rpa_execution_plan",
        "valid": True,
        "plan": normalized_plan,
        "component_sequence": [item["component"] for item in normalized_steps],
        "live_catalog_validated": installed is not None,
        "constraint_coverage": {"limit_steps": [], "pagination_steps": []},
        "normalization_warnings": warnings,
        "native_collection_review": native_collection_review,
        "policy": {
            "mode": "model_decides",
            "strategy_checks": "none",
            "component_selection": "client_model",
            "pagination": "client_model",
        },
        "message": "计划已通过 MCP 薄边界校验；步骤组合、循环、分页和输出实现由客户端模型决定。",
    }


def preflight_rpa_execution_plan(
    analysis: dict[str, Any],
    plan: dict[str, Any],
    *,
    available_components: Iterable[str] | dict[str, dict[str, Any]] | None = None,
    planning_policy: str = "strict",
) -> dict[str, Any]:
    """Return a fast, non-throwing plan preflight for MCP clients."""
    started = time.perf_counter()
    normalized_policy = str(planning_policy).strip().casefold()
    native_first_policy = normalized_policy in {"native_first", "mcp_native", "native"}
    thin_mcp_policy = normalized_policy in {"mcp", "mcp_thin", "thin"} or native_first_policy
    try:
        effective_plan = dict(plan) if isinstance(plan, dict) else plan
        if isinstance(effective_plan, dict) and normalized_policy in {
            "flexible",
            "advisory",
            "model_decides",
            "mcp",
            "mcp_thin",
            "thin",
        }:
            effective_plan.setdefault("validation_mode", "flexible")
        if thin_mcp_policy:
            validated = validate_mcp_rpa_plan(
                analysis,
                effective_plan,
                available_components=available_components,
            )
        else:
            validated = validate_rpa_execution_plan(
                analysis,
                effective_plan,
                available_components=available_components,
            )
        # The public MCP authoring path uses ``native_first``.  Keep the
        # generic thin validator model-owned for compatibility, but make a
        # hierarchical collection plan a real gate before start_design writes
        # a native task.  Otherwise a model can acknowledge the review and
        # still persist one large JavaScript extractor for parent/child data.
        review = validated.get("native_collection_review") if isinstance(validated, dict) else None
        if (
            native_first_policy
            and validated.get("valid")
            and isinstance(review, dict)
            and review.get("status") == "stage_plan_required"
        ):
            directive = str(review.get("model_directive") or "").strip()
            return {
                **validated,
                "kind": "rpa_plan_preflight",
                "valid": False,
                "error_code": "collection_stage_plan_required",
                "error": directive or "层级采集必须先补齐父记录、子记录和对齐阶段。",
                "correction": (
                    "保留原始 query、analysis_id 和当前页面证据；补齐两个独立的 "
                    "WebBrowser_DataCollectionV5 阶段（parent_collection、child_collection）以及 "
                    "record_alignment，再重新调用 shizai_rpa_validate_plan。"
                ),
                "do_not_reanalyze_original_query": True,
                "next_tool": "shizai_rpa_validate_plan",
                "policy": {"mode": "native_first", "strategy_checks": "hierarchical_native_gate"},
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
    except (TypeError, ValueError) as exc:
        message = str(exc)
        lowered = message.casefold()
        error_code = "invalid_plan"
        if "analysis_id" in lowered or "analyzed request" in lowered:
            error_code = "analysis_mismatch"
        elif "not installed" in lowered or "unsupported component" in lowered:
            error_code = "component_unavailable"
        elif "recommended component" in lowered or "component selection" in lowered:
            error_code = "component_selection"
        elif "cannot cover" in lowered or "does not cover" in lowered:
            error_code = "capability_coverage"
        elif "semantic json" in lowered or "quoted default" in lowered or "config.url" in lowered:
            error_code = "semantic_config_value"
        elif "pagination" in lowered or "max_rows" in lowered or "requested" in lowered:
            error_code = "output_constraint"
        elif "depend" in lowered or "variable" in lowered or "outputs" in lowered:
            error_code = "dataflow_contract"
        return {
            "kind": "rpa_plan_preflight",
            "valid": False,
            "error_code": error_code,
            "error": message,
            "correction": "只修改当前 plan 中被指出的字段；保留原始 query、analysis 和 analysis_id，不重新分析或改写用户请求。",
            "do_not_reanalyze_original_query": True,
            "next_tool": "shizai_rpa_validate_plan",
            "policy": {
                "mode": "native_first"
                if native_first_policy
                else "model_decides"
                if normalized_policy in {"flexible", "advisory", "model_decides", "mcp", "mcp_thin", "thin"}
                else "legacy_strict",
                "strategy_checks": "hierarchical_native_gate" if native_first_policy else "none" if thin_mcp_policy else "advisory"
                if normalized_policy in {"flexible", "advisory", "model_decides"}
                else "enforced",
            },
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    return {
        **validated,
        "kind": "rpa_plan_preflight",
        "do_not_reanalyze_original_query": True,
        "next_tool": "shizai_rpa_start_design",
        "policy": {
            "mode": "native_first"
            if native_first_policy
            else "model_decides"
            if normalized_policy in {"flexible", "advisory", "model_decides", "mcp", "mcp_thin", "thin"}
            else "legacy_strict",
            "strategy_checks": (
                "hierarchical_native_gate"
                if native_first_policy
                else "none"
                if thin_mcp_policy
                else "advisory"
                if normalized_policy in {"flexible", "advisory", "model_decides"}
                else "enforced"
            ),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _rpa_flow_file(paths: ShizaiPaths, flow_ref: str) -> Path:
    """Resolve a staged RPA flow directory or its ``rpa-flow.json`` file."""
    candidate = Path(flow_ref).expanduser()
    if candidate.is_file() and candidate.name == RPA_FLOW_FILE:
        return candidate.resolve()
    if candidate.is_dir() and (candidate / RPA_FLOW_FILE).is_file():
        return (candidate / RPA_FLOW_FILE).resolve()
    project_dir = resolve_project(paths, flow_ref)
    target = project_dir / RPA_FLOW_FILE
    if not target.is_file():
        raise FileNotFoundError(f"{RPA_FLOW_FILE} is missing: {target}")
    return target.resolve()


def _read_rpa_flow(paths: ShizaiPaths, flow_ref: str) -> tuple[Path, dict[str, Any]]:
    """Read one staged RPA flow draft and reject malformed payloads."""
    flow_path = _rpa_flow_file(paths, flow_ref)
    try:
        payload = read_json(flow_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {RPA_FLOW_FILE}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("kind") != "shizai-rpa-draft":
        raise ValueError(f"{flow_path} is not a staged Shizai RPA flow")
    if not isinstance(payload.get("steps"), list):
        raise ValueError(f"{flow_path} has no steps list")
    return flow_path, payload


def _write_rpa_flow(flow_path: Path, payload: dict[str, Any]) -> None:
    """Persist a staged flow atomically so a partial write is not exposed."""
    temporary = flow_path.with_suffix(flow_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(flow_path)


def create_rpa_flow(
    output_dir: str,
    *,
    name: str = "网页 RPA 流程",
    target_url: str = "",
    keyword: str = "",
    output_file: str = "result.xlsx",
    required_columns: list[str] | None = None,
    use_excel_components: bool = False,
    open_new_tab: bool = False,
    use_js_link: bool = False,
    use_js_table: bool = False,
    max_rows: int | None = None,
    wait_seconds: float = 1.0,
    wait_after_submit: bool = False,
    refresh_browser_tab: bool = False,
    request_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Create the staged RPA plan that the model fills one component at a time."""
    if use_js_table and use_js_link:
        raise ValueError("use_js_table and use_js_link are mutually exclusive")
    output_row_limit = _normalize_max_rows(max_rows, default=0)
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Output directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    output_name = Path(output_file).name or "result.xlsx"
    configured_output_path = _native_configured_output_path(output_file)
    columns = [str(item).strip() for item in (required_columns or DEFAULT_RPA_COLUMNS) if str(item).strip()]
    if not columns:
        columns = list(DEFAULT_RPA_COLUMNS)
    metadata = {
        "isNewProject": True,
        "packageName": name,
        "description": "Staged RPA plan; native .task materialization happens after capture validation.",
        "projectMode": "normal",
        "version": "0.0.1",
        "openMode": "code",
        "mcpRequestFingerprint": str(request_fingerprint or "").strip() or None,
        "mcpOutputPath": str(configured_output_path) if configured_output_path else None,
        "mcpRequiredColumns": columns,
        "mcpExcelMode": bool(use_excel_components),
        "mcpSubmitComponent": "Keyboard_PressNew",
        "mcpOpenNewTab": bool(open_new_tab),
        "mcpUseJsLink": bool(use_js_link),
        "mcpUseJsTable": bool(use_js_table),
        "mcpJsTablePaginate": bool(use_js_table and output_row_limit > 30),
        "mcpMaxRows": output_row_limit,
        "mcpWaitAfterSubmit": bool(wait_after_submit),
        "mcpWaitSeconds": max(0.0, min(float(wait_seconds), 30.0)),
        "mcpRefreshBrowserTab": bool(refresh_browser_tab),
        "mcpPlanningPolicy": "model_decides",
        "mcpPlanComplete": False,
    }
    (root / "szrpa.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "elements.json").write_text(
        json.dumps({"elementMap": {}, "groupMap": {}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for filename in ("project.flow", "main.sz"):
        (root / filename).write_text("", encoding="utf-8")
    flow = {
        "kind": "shizai-rpa-draft",
        "version": 1,
        "name": name,
        "target_url": target_url,
        "keyword": keyword,
        "output_file": output_name,
        "required_columns": columns,
        "excel_mode": bool(use_excel_components),
        "submit_component": "Keyboard_PressNew",
        "use_js_link": bool(use_js_link),
        "use_js_table": bool(use_js_table),
        "max_rows": output_row_limit,
        "wait_after_submit": bool(wait_after_submit),
        "wait_seconds": max(0.0, min(float(wait_seconds), 30.0)),
        "refresh_browser_tab": bool(refresh_browser_tab),
        "result_elements_ready": False,
        "steps": [],
        "status": "draft",
        "phase": "flow_created",
        "next_tool": "shizai_rpa_components",
        "source_project_ref": None,
        "planning_policy": "model_decides",
        "plan_complete": False,
    }
    flow_path = root / RPA_FLOW_FILE
    _write_rpa_flow(flow_path, flow)
    return {
        "flow_ref": str(root),
        "flow_file": str(flow_path),
        "phase": "created",
        "status": "draft",
        "next_action": {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_components",
            "arguments": {"flow_ref": str(root)},
            "reason": "先读取当前设计器可用组件和绑定要求。",
        },
        "steps": [],
        "required_columns": flow["required_columns"],
        "max_rows": flow["max_rows"],
        "next_tool": "shizai_rpa_components",
        "message": "流程草稿已创建；先读取实时组件契约，再由模型选择并调用 shizai_rpa_add_step 写入组件。",
    }


def start_rpa_design(
    paths: ShizaiPaths,
    output_dir: str | None = None,
    *,
    name: str = "网页 RPA 流程",
    target_url: str = "",
    keyword: str = "",
    output_file: str = "result.xlsx",
    required_columns: list[str] | None = None,
    use_excel_components: bool = False,
    open_new_tab: bool = False,
    use_js_link: bool = False,
    use_js_table: bool = False,
    max_rows: int | None = None,
    wait_seconds: float = 1.0,
    wait_after_submit: bool = True,
    refresh_browser_tab: bool = True,
    first_step: dict[str, Any] | None = None,
    component_plan: list[dict[str, Any]] | None = None,
    request_analysis: dict[str, Any] | None = None,
    request_fingerprint: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Create a client-owned native flow and write the complete planned graph.

    When ``output_dir`` is explicitly supplied, retain the old staged behavior
    for scripts that still use it. The default path calls the real designer
    creation RPC and writes directly into the returned application directory.
    Element-dependent nodes are initially written without bindings so the
    complete design exists before any design-time browser execution.
    """
    planned_first = first_step if isinstance(first_step, dict) else {}
    planned_first_component = str(planned_first.get("component") or "WebBrowser_CreateV3").strip()
    complete_plan_supplied = isinstance(component_plan, list) and bool(component_plan)
    _publish_rpa_ui(
        "designing",
        "flow_creation",
        (
            f"正在创建流程“{name}”，准备写入完整计划组件图。"
            if complete_plan_supplied
            else f"正在创建流程“{name}”，准备写入首个计划组件 {planned_first_component}。"
        ),
        animation="flow_creation",
        flow_name=name,
        target_url=target_url,
        component=planned_first_component,
    )
    if use_js_table and use_js_link:
        raise ValueError("use_js_table and use_js_link are mutually exclusive")
    output_row_limit = _normalize_max_rows(max_rows, default=0)
    if output_dir is None:
        # Import locally to keep the project data layer independent from the
        # desktop RPC layer at module import time.
        from . import agent as agent_core

        application = agent_core.create_native_application(
            paths,
            name,
            template_name=name,
            application_type="normal",
            client=client,
        )
        project_ref = application["local_path"]
        configured_output_path = _native_configured_output_path(output_file)
        first_component = planned_first_component
        # An omitted component_plan is a partial CLI flow, not a one-item
        # execution plan.  Keep the metadata empty so debug/runtime checks do
        # not mistake a placeholder object for a complete graph.
        seeded_plan = (
            component_plan
            if isinstance(component_plan, list) and component_plan
            else ([planned_first] if planned_first else [])
        )
        column_source = (
            required_columns
            if required_columns is not None
            else ([] if isinstance(component_plan, list) and component_plan else DEFAULT_RPA_COLUMNS)
        )
        columns = [str(item).strip() for item in column_source if str(item).strip()]
        if not columns and not (isinstance(component_plan, list) and component_plan):
            columns = list(DEFAULT_RPA_COLUMNS)
        # Preserve request parameters for the components added after creation.
        submit_component = _preferred_submit_component(paths)
        _update_native_metadata(
            Path(project_ref),
            0,
            {
                "openMode": "code",
                "isAllowNewEdit": True,
                "supportVersion": str(application.get("factory_version") or "7.3.7"),
                "supportPatch": "0",
                "arch": "x64",
                "reCreateCode": False,
                "mcpTargetUrl": target_url,
                "mcpKeyword": keyword,
                "mcpOutputFile": Path(output_file).name or "result.xlsx",
                "mcpOutputPath": str(configured_output_path) if configured_output_path else None,
                "mcpRequestFingerprint": str(request_fingerprint or "").strip() or None,
                "mcpOriginalQuery": (
                    str(request_analysis.get("query") or "").strip()
                    if isinstance(request_analysis, dict)
                    else ""
                ),
                "mcpRequiredColumns": columns,
                "mcpExcelMode": bool(use_excel_components),
                "mcpSubmitComponent": submit_component,
                "mcpOpenNewTab": bool(open_new_tab),
                "mcpUseJsLink": bool(use_js_link),
                "mcpUseJsTable": bool(use_js_table),
                "mcpJsTablePaginate": bool(use_js_table and output_row_limit > 30),
                "mcpMaxRows": output_row_limit,
                "mcpWaitAfterSubmit": bool(wait_after_submit),
                "mcpWaitSeconds": max(0.0, min(float(wait_seconds), 30.0)),
                "mcpRefreshBrowserTab": bool(refresh_browser_tab),
                "mcpResultElementsReady": False,
                "mcpPlanningPolicy": "model_decides",
                "mcpPlanComplete": bool(complete_plan_supplied),
                "mcpComponentPlan": seeded_plan,
                "mcpRequestAnalysis": request_analysis if isinstance(request_analysis, dict) else None,
            },
        )
        planned_first_config = planned_first.get("config") if isinstance(planned_first.get("config"), dict) else {}
        first = _add_native_rpa_step(
            paths,
            project_ref,
            first_component,
            step_id=str(planned_first.get("id") or "open-chrome"),
            label=str(planned_first.get("label") or planned_first.get("purpose") or first_component),
            config=(
                {
                    **planned_first_config,
                    "url": str(planned_first_config.get("url") or target_url),
                    "type": str(planned_first_config.get("type") or "chrome"),
                    "openNewTab": bool(planned_first_config.get("openNewTab", open_new_tab)),
                }
                if first_component == "WebBrowser_CreateV3"
                else dict(planned_first_config)
            ),
            element_ids=(
                [str(item).strip() for item in planned_first.get("element_ids", []) if str(item).strip()]
                if isinstance(planned_first.get("element_ids"), list)
                else None
            ),
            client=client,
        )
        planned_components = [first]
        for planned_step in seeded_plan[1:]:
            if not isinstance(planned_step, dict):
                raise ValueError("component_plan entries must be objects")
            component_name = str(planned_step.get("component") or "").strip()
            if not component_name:
                raise ValueError("component_plan entry is missing component")
            planned_components.append(
                _add_native_rpa_step(
                    paths,
                    project_ref,
                    component_name,
                    step_id=str(planned_step.get("id") or "").strip() or None,
                    label=str(planned_step.get("label") or planned_step.get("purpose") or component_name),
                    config=(planned_step.get("config") if isinstance(planned_step.get("config"), dict) else {}),
                    element_ids=(
                        [str(item).strip() for item in planned_step.get("element_ids", []) if str(item).strip()]
                        if isinstance(planned_step.get("element_ids"), list)
                        else None
                    ),
                    client=client,
                    allow_unbound_predecessors=True,
                )
            )
        catalog = rpa_components(paths, flow_ref=project_ref)
        complete_state = read_rpa_flow(paths, project_ref)["flow"]
        unbound_steps = [
            str(item.get("id") or "")
            for item in complete_state.get("steps", [])
            if isinstance(item, dict) and item.get("binding_status") == "unbound"
        ]
        ui_feedback = _publish_rpa_ui(
            "designing",
            "flow_skeleton_ready",
            f"流程完整骨架已写入，共 {len(planned_components)} 个组件；等待补齐网页元素后逐步验证。",
            animation="flow_skeleton_ready",
            flow_ref=project_ref,
            component=first_component,
            step_id=str(planned_first.get("id") or "open-chrome"),
            step_index=1,
            step_count=len(planned_components),
            unbound_steps=unbound_steps,
            next_tool=(
                "shizai_rpa_capture_windows"
                if first_component.startswith("WebBrowser_")
                else "shizai_rpa_validate_flow"
            ),
        )
        return {
            "kind": "shizai_rpa_started",
            "mode": "native_client_application",
            "application": application,
            "project_ref": project_ref,
            "flow_ref": project_ref,
            "request_fingerprint": str(request_fingerprint or "").strip() or None,
            "component_catalog": catalog,
            "first_component": first,
            "planned_components": planned_components,
            "design_complete": len(planned_components) == len(seeded_plan),
            "planned_component_count": len(seeded_plan),
            "written_component_count": len(planned_components),
            "unbound_steps": unbound_steps,
            "ui_feedback": ui_feedback,
            "external_staging_required": False,
            "next_action": {
                "tool": "shizai_rpa_capture_windows"
                if first_component.startswith("WebBrowser_")
                else "shizai_rpa_add_step",
                "arguments": {"include_non_chrome": False} if first_component.startswith("WebBrowser_") else {},
                "reason": "完整计划组件图已直接写入设计器应用；接下来只补齐元素绑定并执行设计态单步验证。",
            },
        }
    created = create_rpa_flow(
        output_dir,
        name=name,
        target_url=target_url,
        keyword=keyword,
        output_file=output_file,
        required_columns=required_columns,
        use_excel_components=use_excel_components,
        open_new_tab=open_new_tab,
        use_js_link=use_js_link,
        use_js_table=use_js_table,
        max_rows=output_row_limit or None,
        wait_seconds=wait_seconds,
        wait_after_submit=wait_after_submit,
        refresh_browser_tab=refresh_browser_tab,
        request_fingerprint=request_fingerprint,
    )
    catalog = rpa_components(paths, flow_ref=created["flow_ref"])
    first = add_rpa_step(
        paths,
        created["flow_ref"],
        "WebBrowser_CreateV3",
        step_id="open-chrome",
        label="打开谷歌浏览器并打开目标网站",
        config={"url": target_url, "type": "chrome", "openNewTab": bool(open_new_tab)},
    )
    ui_feedback = _publish_rpa_ui(
        "designing",
        "component_ready",
        "流程已创建，首个打开 Chrome 组件已写入；接下来从 DeepCapture 页面树识别搜索框。",
        animation="component_ready",
        flow_ref=created["flow_ref"],
        component="WebBrowser_CreateV3",
        step_id="open-chrome",
        step_index=1,
        step_count=1,
        next_tool="shizai_rpa_capture_windows",
    )
    return {
        "kind": "shizai_rpa_started",
        "flow": created,
        "component_catalog": catalog,
        "first_component": first,
        "ui_feedback": ui_feedback,
        "mode": "legacy_staged_flow",
        "request_fingerprint": str(request_fingerprint or "").strip() or None,
        "next_action": {
            "tool": "shizai_rpa_capture_windows",
            "arguments": {"include_non_chrome": False},
            "then": "shizai_rpa_scan_page",
            "optional_picker_tool": "shizai_extension_start_capture",
            "fallback_tool": "shizai_project_import_elements",
            "reason": "WebBrowser_CreateV3 已写入 staged 流程；使用已初始化的 DeepCapture 页面树识别搜索框。普通设计不打开红色拾取层。",
        },
    }


def rpa_components(
    paths: ShizaiPaths,
    *,
    flow_ref: str | None = None,
    source_project_ref: str | None = None,
) -> dict[str, Any]:
    """Return the native RPA component catalog and current flow state."""
    components = [dict(item) for item in RPA_ALL_COMPONENT_CONTRACTS]
    # Expose the complete installed dictionary as the public component surface
    # rather than a small browser-RPA whitelist.  Keep the compatibility rows
    # for callers that rely on their stable labels, but enrich them with the
    # live contract and append every additional materializable/non-materializable
    # entry so the model can make an informed choice.
    try:
        from . import component_catalog as component_catalog_core

        live_components = component_catalog_core.builtin_component_index(paths)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        live_components = {}
    known_components = {str(item.get("component") or "") for item in components}
    for item in components:
        live = live_components.get(str(item.get("component") or ""))
        if not isinstance(live, dict):
            continue
        item["live_contract"] = live
        item["inputs"] = deepcopy(live.get("inputs") or item.get("inputs") or [])
        item["options"] = deepcopy(live.get("options") or [])
        item["capabilities"] = list(live.get("capabilities") or [])
        item["operation_types"] = list(live.get("operation_types") or [])
        item["requires_elements"] = bool(
            item.get("requires_elements") or live.get("requires_page_evidence")
        )
        item["materializable"] = bool(live.get("materializable", True))
        item["availability"] = live.get("availability", "installed")
    for component_type, live in live_components.items():
        if component_type in known_components or not isinstance(live, dict):
            continue
        components.append(
            {
                "component": component_type,
                "designer_type": live.get("native_type") or component_type,
                "designer_label": live.get("label") or component_type,
                "purpose": live.get("description") or "实时设计器组件",
                "requires_elements": bool(live.get("requires_page_evidence")),
                "inputs": deepcopy(live.get("inputs") or []),
                "options": deepcopy(live.get("options") or []),
                "outputs": deepcopy(live.get("outputs") or {}),
                "capabilities": list(live.get("capabilities") or []),
                "operation_types": list(live.get("operation_types") or []),
                "materializable": bool(live.get("materializable", True)),
                "availability": live.get("availability", "installed"),
                "live_contract": live,
            }
        )
    observed: list[str] = []
    template_error: str | None = None
    if source_project_ref:
        try:
            template = inspect_rpa_flow(paths, source_project_ref)
            observed = sorted({step["type"] for item in template["task_files"] for step in item["steps"]})
        except (FileNotFoundError, OSError, ValueError) as exc:
            template_error = str(exc)
    state: dict[str, Any] | None = None
    if flow_ref:
        candidate = Path(flow_ref).expanduser()
        native_candidate = candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file()
        if not native_candidate:
            try:
                native_candidate = (
                    resolve_project(paths, flow_ref).is_dir()
                    and not (resolve_project(paths, flow_ref) / RPA_FLOW_FILE).is_file()
                    and any(resolve_project(paths, flow_ref).rglob("*.task"))
                )
            except (FileNotFoundError, OSError):
                native_candidate = False
        if native_candidate:
            flow = _native_rpa_state(paths, flow_ref)
        else:
            flow_path, flow = _read_rpa_flow(paths, flow_ref)
            flow["flow_ref"] = str(flow_path.parent)
            try:
                _, native_elements = _element_file(paths, flow_ref)
                flow["element_count"] = len(native_elements["elementMap"])
            except (FileNotFoundError, OSError, ValueError):
                flow["element_count"] = 0
        state = {
            "kind": flow.get("kind"),
            "flow_ref": flow.get("flow_ref") or str(flow_path.parent),
            "flow_file": flow.get("flow_file") or str(flow_path),
            "target_url": flow.get("target_url", ""),
            "keyword": flow.get("keyword", ""),
            "output_file": flow.get("output_file", "result.xlsx"),
            "required_columns": flow.get("required_columns", list(DEFAULT_RPA_COLUMNS)),
            "excel_mode": bool(flow.get("excel_mode", False)),
            "use_js_link": bool(flow.get("use_js_link", False)),
            "use_js_table": bool(flow.get("use_js_table", False)),
            "wait_after_submit": bool(flow.get("wait_after_submit", False)),
            "refresh_browser_tab": bool(flow.get("refresh_browser_tab", False)),
            "wait_seconds": flow.get("wait_seconds", 1),
            "result_elements_ready": bool(flow.get("result_elements_ready", False)),
            "component_plan": flow.get("component_plan", []),
            "request_analysis": flow.get("request_analysis"),
            "plan_id": flow.get("plan_id"),
            "planning_policy": flow.get("planning_policy") or flow.get("validation_mode") or "",
            "submit_component": _rpa_submit_component(flow),
            "step_count": len(flow["steps"]),
            "steps": flow["steps"],
            "element_count": flow.get("element_count", 0),
            "status": flow.get("status", "draft"),
            "phase": flow.get("phase", "flow_created"),
            "next_tool": flow.get("next_tool"),
            "next_action": _rpa_next_action(flow),
        }
    plan_backed = bool(state and state.get("component_plan") and state.get("plan_complete"))
    if plan_backed:
        planned_by_component = {
            str(item.get("component") or ""): item
            for item in state.get("component_plan", [])
            if isinstance(item, dict) and item.get("component")
        }
        for item in components:
            planned = planned_by_component.get(str(item.get("component") or ""))
            if not planned:
                continue
            item["planned"] = True
            item["purpose"] = planned.get("purpose") or item.get("purpose")
            item["inputs"] = list(planned.get("inputs") or [])
            item["outputs"] = list(planned.get("outputs") or [])
    if state and state.get("use_js_table") and not plan_backed:
        for item in components:
            if item.get("component") == "WebBrowser_RunJsInIeBySelector_v3":
                item["purpose"] = "在当前 Chrome 页面直接返回包含表头的商品二维结果"
                item["outputs"] = [JS_TABLE_OUTPUT_VARIABLE]
    next_action = _rpa_next_action(state or {"steps": []})
    planned_sequence = list(_rpa_sequence(state or {}))
    generic_rules = [
        "原生组件优先是 advisory/warning_only 建议：先评估已安装且可物化的原生组件；结构化网页采集优先评估 WebBrowser_DataCollectionV5。",
        "如果选择 JS、下载、循环或其它替代组合，说明原生组件不适用或能力不足的原因，并以所选方案的运行输出验收；不因未选原生组件而硬失败。",
        "组件顺序、图结构和分页策略由客户端模型根据本次请求与实时契约决定，不从站点名称或关键词推断流程。",
        "operation_ids、depends_on、变量、循环和分支字段由客户端模型按需提供；MCP 不把它们变成固定计划格式，原生写入/运行时再检查实际兼容性。",
        "需要网页元素的步骤先读取 DeepCapture 页面证据并导入真实 selector；官方红色拾取层仅在交互式拾取确有必要时启用。",
        "WebBrowser_RunJsInIeBySelector_v3 的 jsCode、output_variable 和 result_mode 必须由客户端模型在本次计划中显式提供。",
        "登录或页面挑战进入 waiting_for_user，等待用户确认当前会话已可继续后再执行。",
        "完成全部计划步骤后校验输出契约、物化、诊断、运行，并以新鲜输出证据判断成功。",
    ]
    return {
        "kind": "native_browser_rpa_components",
        "components": components,
        "component_types": planned_sequence,
        "recommended_sequence": planned_sequence,
        "sequence_source": "validated_request_plan" if plan_backed else "current_flow_state",
        "submit_component": None if plan_backed else _rpa_submit_component(state or {}),
        "submit_component_options": []
        if plan_backed
        else [
            {
                "component": "Keyboard_PressNew",
                "recommended": True,
                "reason": "真实按键组件，key=13/type=press 明确发送回车。",
            },
            {
                "component": "Keyboard_Send_V1",
                "recommended": False,
                "reason": "兼容旧流程的文本发送组件；已有旧流程可继续使用。",
            },
        ],
        "excel_sequence": [] if plan_backed else list(RPA_EXCEL_SEQUENCE),
        "result_mode": (
            "js_table_direct_to_excel"
            if state and state.get("use_js_table")
            else "js_generated_markers"
            if state and state.get("use_js_link")
            else "native_data_collection"
        ),
        "first_component": planned_sequence[0] if planned_sequence else None,
        "observed_in_template": observed,
        "template_error": template_error,
        "flow": state,
        "next_action": next_action,
        "business_rules": deepcopy(RPA_BUSINESS_RULES),
        "rules": generic_rules,
    }


def _adapt_rpa_component_config(component: str, config: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    """Accept planner/CLI spellings and normalize them to native arguments."""
    adapted = dict(config)
    if component == "WebBrowser_DataCollectionV5":
        if "columns" not in adapted and "output_columns" in adapted:
            adapted["columns"] = adapted["output_columns"]
        if "collect_items" not in adapted and adapted.get("max_rows") not in (None, ""):
            adapted["collect_items"] = adapted["max_rows"]
        paging = adapted.get("pagination")
        if isinstance(paging, dict):
            for source, target in (
                ("region", "region"),
                ("collect_pages", "collect_pages"),
                ("page_type", "page_type"),
                ("page_type_num", "page_type_num"),
                ("selector_page", "selector_page"),
                ("selector_more", "selector_more"),
                ("next_page_selector", "selector_page"),
                ("load_more_selector", "selector_more"),
            ):
                if target not in adapted and source in paging:
                    adapted[target] = paging[source]
        if "output_file" not in adapted:
            for source in ("file", "filename", "file_path"):
                if adapted.get(source):
                    adapted["output_file"] = adapted[source]
                    break
        if "filePath" not in adapted:
            for source in ("file_path", "output_path", "destination"):
                if adapted.get(source):
                    candidate_path = Path(str(adapted[source])).expanduser()
                    adapted["filePath"] = (
                        str(candidate_path.parent)
                        if candidate_path.suffix.casefold() in _NATIVE_OUTPUT_SUFFIXES
                        else adapted[source]
                    )
                    break
    elif component == "WebBrowser_RunJsInIeBySelector_v3":
        if "jsCode" not in adapted:
            for source in ("script", "javascript", "js_code"):
                if adapted.get(source):
                    adapted["jsCode"] = adapted[source]
                    break
        if "output_variable" not in adapted and adapted.get("outputVariable"):
            adapted["output_variable"] = adapted["outputVariable"]
    elif component == "Excel_Cs_NewV4":
        if "name" not in adapted:
            for source in ("output_file", "file", "filename"):
                if adapted.get(source):
                    adapted["name"] = Path(str(adapted[source])).name
                    break
        if "path" not in adapted and adapted.get("output_path"):
            output_path = _native_configured_output_path(adapted["output_path"])
            if output_path is not None:
                adapted["path"] = str(output_path.parent)
    return adapted


def _sqlite_database_path(config: dict[str, Any], *, project_dir: Path | None = None) -> Path | None:
    """Resolve a concrete local SQLite file from a semantic DB component config."""
    if not isinstance(config, dict):
        return None
    dtype = _decode_selector_value(config.get("dtype", ""))
    if dtype != "4":
        return None
    database_config = config.get("DatabaseConfig")
    if isinstance(database_config, str):
        encoded = _decode_selector_value(database_config)
        try:
            database_config = json.loads(encoded)
        except (TypeError, json.JSONDecodeError):
            return None
    if isinstance(database_config, dict):
        database_config = [database_config]
    if not isinstance(database_config, list):
        return None
    raw_path: Any = None
    for item in database_config:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip().casefold() in {"filepath", "file_path"}:
            raw_path = item.get("value")
            break
    if isinstance(raw_path, (dict, list, tuple)):
        return None
    text = _decode_selector_value(raw_path).strip()
    if not text or text.startswith("$") or text.casefold() in {"none", "null"}:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        if project_dir is None:
            return None
        path = (project_dir / path).resolve()
    return path


def _ensure_sqlite_database_file(
    config: dict[str, Any], *, project_dir: Path | None = None
) -> dict[str, Any]:
    """Create a local SQLite file before RuntimeHost opens it.

    RuntimeHost's Database_DBConnect component reports ``CantOpen (14)``
    when the configured file or parent directory does not exist.  The native
    component does not create either one, so MCP prepares concrete local
    SQLite paths at the authoring and run boundaries.  Variable/expression
    paths are deliberately skipped because their value is only known inside
    the runtime.
    """
    path = _sqlite_database_path(config, project_dir=project_dir)
    if path is None:
        return {"prepared": False, "path": None}
    try:
        if path.exists() and path.is_dir():
            raise OSError("configured SQLite path is a directory")
        existed = path.is_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)):
            pass
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"SQLite database file cannot be prepared: {path}: {exc}") from exc
    return {"prepared": True, "created": not existed, "path": str(path)}


def _collection_stage_value(step: dict[str, Any] | None) -> str:
    if not isinstance(step, dict):
        return ""
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    return str(
        step.get("collection_stage")
        or step.get("collection_role")
        or step.get("mcpCollectionStage")
        or config.get("collection_stage")
        or config.get("collection_role")
        or ""
    ).strip()


def _guard_rpa_append_structure(
    steps: list[dict[str, Any]],
    component: str,
    config: dict[str, Any] | None = None,
) -> None:
    """Reject ambiguous collection appends while allowing explicit stages.

    A plain flow still has one final collection node. A model-owned
    hierarchical plan may add multiple native collectors only when every
    collector carries a distinct ``collection_stage`` (for example
    ``parent_collection`` and ``child_collection``). This keeps accidental
    duplicate appends blocked while making the intended two-pass design
    materializable.
    """
    components = [str(item.get("component") or "") for item in steps if isinstance(item, dict)]
    if component == "WebBrowser_DataCollectionV5" and "WebBrowser_DataCollectionV5" in components:
        requested_stage = _collection_stage_value({"config": config or {}})
        existing_stages = [
            _collection_stage_value(item)
            for item in steps
            if isinstance(item, dict) and item.get("component") == "WebBrowser_DataCollectionV5"
        ]
        if not requested_stage or not all(existing_stages) or requested_stage in existing_stages:
            raise ValueError(
                "designer_state_conflict: WebBrowser_DataCollectionV5 already exists; update or rebind it instead of adding a second collection step"
            )
    if component == "WebBrowser_DataCollectionV5" and any(item.startswith("Excel_") for item in components):
        raise ValueError(
            "designer_state_conflict: data collection must be completed before the Excel output chain; do not append collection after Excel steps"
        )


def repair_native_rpa_flow(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Repair duplicate final collection nodes and rebuild native artifacts."""
    project_dir = resolve_project(paths, project_ref)
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        raise ValueError("native task has no componentModelEntities")
    main = entities.get("MAIN_COMPONENT")
    if not isinstance(main, dict):
        raise ValueError("native task has no MAIN_COMPONENT")
    statements_argument = next(
        (
            item
            for item in (main.get("args0") if isinstance(main.get("args0"), list) else [])
            if isinstance(item, dict) and item.get("name") == "statements" and isinstance(item.get("value"), list)
        ),
        None,
    )
    if statements_argument is None:
        raise ValueError("native task MAIN_COMPONENT has no statements list")
    statement_ids = [str(item) for item in statements_argument["value"]]
    collection_ids = [
        node_id
        for node_id in statement_ids
        if isinstance(entities.get(node_id), dict)
        and entities[node_id].get("type") == "WebBrowser_DataCollectionV5"
    ]
    if len(collection_ids) <= 1:
        raise ValueError("native flow does not contain duplicate WebBrowser_DataCollectionV5 nodes")
    staged_ids = [
        _collection_stage_value(entities.get(node_id))
        for node_id in collection_ids
    ]
    if all(staged_ids) and len(set(staged_ids)) == len(staged_ids):
        raise ValueError(
            "native flow contains explicitly staged collection nodes; repair_native_rpa_flow only removes accidental duplicates"
        )
    keep_id = collection_ids[0]
    remove_ids = collection_ids[1:]
    statements_argument["value"] = [node_id for node_id in statement_ids if node_id not in set(remove_ids)]
    for node_id in remove_ids:
        entities.pop(node_id, None)

    metadata_path = project_dir / "szrpa.json"
    metadata_ok, metadata, _ = _safe_json(metadata_path)
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    metadata["componentsNum"] = len(statements_argument["value"])
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    validation = validate_rpa_project(paths, str(project_dir), strict=True)
    return {
        "kind": "native_rpa_flow_repaired",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "removed_step_ids": remove_ids,
        "kept_collection_step_id": keep_id,
        "statement_count": len(statements_argument["value"]),
        "writeback": writeback,
        "validation": validation,
        "valid": bool(validation.get("valid")),
        "message": "已删除后置重复采集节点，并重新物化设计器运行文件。请关闭并重新打开该流程。",
    }


def reorder_native_rpa_steps(
    paths: ShizaiPaths,
    project_ref: str,
    ordered_step_ids: list[str],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Reorder an existing native task without creating or deleting nodes.

    This is intentionally an exact permutation operation.  It gives recovery
    workflows a way to append a dependency temporarily, then put the final
    execution order back into the designer task before binding or running it.
    """
    project_dir = resolve_project(paths, project_ref)
    task_path, payload = _native_task_file(project_dir)
    consistency = _native_designer_state_consistency(project_dir, task_path)
    if consistency.get("conflict"):
        detail = "; ".join(str(item) for item in consistency.get("errors") or [])
        raise ValueError("designer_state_conflict: " + (detail or "native task and designer artifacts disagree"))
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        raise ValueError("native task has no componentModelEntities")
    main = entities.get("MAIN_COMPONENT")
    if not isinstance(main, dict):
        raise ValueError("native task has no MAIN_COMPONENT")
    statements_argument = next(
        (
            item
            for item in (main.get("args0") if isinstance(main.get("args0"), list) else [])
            if isinstance(item, dict) and item.get("name") == "statements" and isinstance(item.get("value"), list)
        ),
        None,
    )
    if statements_argument is None:
        raise ValueError("native task MAIN_COMPONENT has no statements list")
    current_ids = [str(item).strip() for item in statements_argument["value"] if str(item).strip()]
    requested_ids = [str(item).strip() for item in ordered_step_ids if str(item).strip()]
    if len(requested_ids) != len(set(requested_ids)):
        raise ValueError("rpa-reorder-steps contains duplicate step ids")
    if set(requested_ids) != set(current_ids) or len(requested_ids) != len(current_ids):
        missing = sorted(set(current_ids) - set(requested_ids))
        unknown = sorted(set(requested_ids) - set(current_ids))
        detail = []
        if missing:
            detail.append("missing existing steps: " + ", ".join(missing))
        if unknown:
            detail.append("unknown steps: " + ", ".join(unknown))
        raise ValueError("rpa-reorder-steps requires an exact permutation" + (" (" + "; ".join(detail) + ")" if detail else ""))
    if requested_ids == current_ids:
        return {
            "kind": "native_rpa_steps_reordered",
            "flow_ref": str(project_dir),
            "flow_file": str(task_path),
            "ordered_step_ids": current_ids,
            "changed": False,
            "consistency": consistency,
            "message": "步骤顺序已经是目标顺序，未重复写入。",
        }
    statements_argument["value"] = requested_ids
    metadata_path = project_dir / "szrpa.json"
    metadata_ok, metadata, _ = _safe_json(metadata_path)
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    metadata["componentsNum"] = len(requested_ids)
    # Keep the model-facing plan aligned with the authoritative native
    # MAIN_COMPONENT order.  Reordering used to update only io5cd.task, which
    # left stale plan dependencies and caused the next incremental append to
    # point at deleted or out-of-order steps.
    component_plan, _ = _rpa_component_plan(metadata)
    if isinstance(component_plan, list):
        by_id = {
            str(item.get("id") or "").strip(): item
            for item in component_plan
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        if set(by_id) == set(requested_ids) and len(by_id) == len(requested_ids):
            revised_plan = [deepcopy(by_id[step_id]) for step_id in requested_ids]
            _normalize_linear_plan_dependencies(revised_plan)
            for order, item in enumerate(revised_plan, start=1):
                item["order"] = order
            metadata["mcpComponentPlan"] = revised_plan
            execution_plan = metadata.get("mcpExecutionPlan")
            if isinstance(execution_plan, dict):
                revised_execution_plan = deepcopy(execution_plan)
                revised_execution_plan["steps"] = deepcopy(revised_plan)
                revised_execution_plan.pop("plan_id", None)
                revised_execution_plan["plan_id"] = (
                    "plan-"
                    + hashlib.sha256(
                        json.dumps(
                            revised_execution_plan,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                )
                metadata["mcpExecutionPlan"] = revised_execution_plan
                metadata["mcpPlanId"] = revised_execution_plan["plan_id"]
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    updated_consistency = _native_designer_state_consistency(project_dir, task_path)
    if updated_consistency.get("conflict"):
        detail = "; ".join(str(item) for item in updated_consistency.get("errors") or [])
        raise ValueError("designer_state_conflict after reorder: " + (detail or "native artifacts disagree"))
    _publish_rpa_ui(
        "step_order_ready",
        "step_reorder",
        "原生步骤顺序已更新并重新物化；绑定元素后才能运行。",
        animation="step_order_complete",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        ordered_step_ids=requested_ids,
    )
    return {
        "kind": "native_rpa_steps_reordered",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "ordered_step_ids": requested_ids,
        "changed": True,
        "writeback": writeback,
        "consistency": updated_consistency,
        "message": "步骤顺序已原子更新并重新物化；请重新读取、校验并在运行前完成所有元素绑定。",
    }


def _native_output_variables(node: dict[str, Any]) -> set[str]:
    """Return the variable names one node writes into the flow scope."""
    component = str(node.get("type") or "")
    names = {str(item).strip() for item in _native_output_names(node, component) if str(item).strip()}
    if component == "WebBrowser_DataCollectionV5":
        for name in ("var0", "var1", "var2"):
            value = str(_native_task_argument(node, name, "") or "").strip()
            if value:
                names.add(value)
    return names


def _native_removal_dependents(
    payload: dict[str, Any],
    step_id: str,
    *,
    replacement_variables: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Find later steps that read a variable produced by the removed node.

    The native task is the authority: a producer is removable only when no
    remaining statement consumes its output. This is intentionally a
    conservative, structural check, not a full data-flow analysis.
    """
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return []
    target = entities.get(str(step_id))
    if not isinstance(target, dict):
        return []
    produced = _native_output_variables(target) - set(replacement_variables or ())
    if not produced:
        return []
    dependents: list[dict[str, Any]] = []
    for node_id in _native_statement_ids(payload):
        if node_id == str(step_id):
            continue
        node = entities.get(node_id)
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        values = _native_argument_values(node, component)
        for argument_name, value in values.items():
            if not isinstance(value, str) or not value:
                continue
            if value.startswith('"') and value.endswith('"'):
                continue
            hit = next((name for name in produced if name in value), None)
            if hit:
                dependents.append(
                    {"step_id": node_id, "component": component, "variable": hit, "parameter": argument_name}
                )
    return dependents


def _drop_plan_steps(metadata: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Remove one step from the authoritative plan and its legacy mirror."""
    updates: dict[str, Any] = {}
    execution_plan = metadata.get("mcpExecutionPlan")
    if isinstance(execution_plan, dict) and isinstance(execution_plan.get("steps"), list):
        plan = deepcopy(execution_plan)
        plan["steps"] = [
            deepcopy(item)
            for item in plan["steps"]
            if not (isinstance(item, dict) and str(item.get("id") or "") == str(step_id))
        ]
        plan.pop("plan_id", None)
        plan["plan_id"] = (
            "plan-"
            + hashlib.sha256(
                json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
        )
        updates.update(mcpExecutionPlan=plan, mcpPlanId=plan["plan_id"], mcpComponentPlan=deepcopy(plan["steps"]))
        updates["mcpPlanSource"] = "mcpExecutionPlan.steps"
    elif isinstance(metadata.get("mcpComponentPlan"), list):
        steps = [
            deepcopy(item)
            for item in metadata["mcpComponentPlan"]
            if not (isinstance(item, dict) and str(item.get("id") or "") == str(step_id))
        ]
        updates.update(mcpComponentPlan=steps, mcpPlanSource="mcpComponentPlan")
    return updates


def remove_rpa_step(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    *,
    expected_revision: str | None = None,
    client: Any | None = None,
    allow_dependents: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete one main-flow step, guarded by revision and by its consumers.

    A step whose output variable is still read by a later step is refused unless
    ``allow_dependents`` is set, so a model cannot silently break a downstream
    binding. The removal is a real task-graph mutation: it drops the statement
    and its entity, updates the plan metadata, then re-materializes through the
    same writeback path as every other native edit.
    """
    project_dir = resolve_project(paths, flow_ref)
    revision = check_revision(project_dir, expected_revision)
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        raise ValueError("native task has no componentModelEntities")
    target_id = str(step_id).strip()
    if not target_id:
        raise ValueError("step_id is required")
    order = _native_statement_ids(payload)
    node = entities.get(target_id)
    if not isinstance(node, dict) or target_id == "MAIN_COMPONENT":
        raise WorkflowEditError(
            "step_not_found",
            "目标步骤不存在，请从 read_flow(detail=edit) 的 steps 中选择稳定 id。",
            step_id=target_id,
            retryable=False,
            next_call={"tool": "shizai_rpa_read_flow", "arguments": {"flow_ref": str(project_dir), "detail": "edit"}},
        )
    if target_id not in order:
        raise WorkflowEditError(
            "unsupported_edit_target",
            "当前删除仅支持主流程步骤；嵌套节点和主容器需专门的图编辑接口。",
            step_id=target_id,
            retryable=False,
        )
    if len(order) <= 1:
        raise ValueError("cannot remove the last remaining step; a flow must keep at least one component")

    component = str(node.get("type") or "")
    element_ids = _native_element_ids_from_node(node)
    dependents = _native_removal_dependents(payload, target_id)
    if dependents and not allow_dependents:
        raise WorkflowEditError(
            "step_has_dependents",
            "该步骤的输出变量仍被后续步骤引用；先改绑下游步骤，或显式传 allow_dependents=true 强制删除。",
            step_id=target_id,
            dependents=dependents,
            retryable=False,
            next_call={
                "tool": "shizai_rpa_read_flow",
                "arguments": {"flow_ref": str(project_dir), "detail": "edit", "step_id": dependents[0]["step_id"]},
            },
        )

    plan_updates: dict[str, Any] = {}
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    plan_updates = _drop_plan_steps(metadata, target_id)
    # Removing the JS field-preparation node also removes its generated-field
    # contract. Leaving mcpUseJsLink set would make the collection step fail
    # validation for a capture that no longer exists, so clear the flags that
    # only described the deleted node.
    metadata_cleanup: list[str] = []
    if component == "WebBrowser_RunJsInIeBySelector_v3" and not any(
        str((entities.get(item) or {}).get("type") or "") == "WebBrowser_RunJsInIeBySelector_v3"
        for item in order
        if item != target_id
    ):
        for flag in (
            "mcpUseJsLink",
            "mcpUseJsTable",
            "mcpJsOutputMode",
            "mcpJsOutputVariable",
            "mcpResultElementsReady",
        ):
            if flag in metadata:
                metadata.pop(flag, None)
                metadata_cleanup.append(flag)

    removal = {
        "step_id": target_id,
        "component": component,
        "label": str(node.get("label") or ""),
        "element_ids": element_ids,
        "dependents": dependents,
        "remaining_step_ids": [item for item in order if item != target_id],
    }
    if dry_run:
        return redact_payload(
            {
                "kind": "native_rpa_step_removal_preview",
                "flow_ref": str(project_dir),
                "flow_file": str(task_path),
                "revision": revision,
                "saved": False,
                "dry_run": True,
                "removal": removal,
                "plan_updates": sorted(plan_updates),
                "metadata_cleanup": metadata_cleanup,
                "validation": validate_rpa_project(paths, str(project_dir), publish_ui=False),
                "validation_basis": "current_flow_state",
                "next_call": {
                    "tool": "shizai_rpa_remove_step",
                    "arguments": {
                        "project_ref": str(project_dir),
                        "step_id": target_id,
                        "expected_revision": revision,
                        "dry_run": False,
                        "allow_write": True,
                    },
                },
                "message": (
                    "预览不会写入；validation 是删除前的当前磁盘状态，可能包含被删节点引起的既有错误。"
                    "提交时携带 expected_revision 与 allow_write=true，删除后会重新校验并清掉只描述被删节点的元数据标记。"
                ),
            }
        )

    statements_argument = next(
        (
            item
            for item in (entities["MAIN_COMPONENT"].get("args0") if isinstance(entities["MAIN_COMPONENT"].get("args0"), list) else [])
            if isinstance(item, dict) and item.get("name") == "statements" and isinstance(item.get("value"), list)
        ),
        None,
    )
    if statements_argument is None:
        raise ValueError("native task MAIN_COMPONENT has no statements list")
    statements_argument["value"] = [item for item in order if item != target_id]
    entities.pop(target_id, None)
    # The plan metadata is authoritative for ordering contracts; drop the node
    # from it before the writeback so validation sees one consistent graph.
    if plan_updates or metadata_cleanup:
        metadata.update(plan_updates)
        (project_dir / "szrpa.json").write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
    _update_native_metadata(project_dir, len(statements_argument["value"]))
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    validation = validate_rpa_project(paths, str(project_dir), strict=False)
    _publish_rpa_ui(
        "step_removed",
        "step_removal",
        f"已删除步骤 {target_id} 并重新物化；请重新读取后再校验或运行。",
        animation="step_order_complete",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        removed_step_id=target_id,
    )
    return redact_payload(
        {
            "kind": "native_rpa_step_removed",
            "flow_ref": str(project_dir),
            "flow_file": str(task_path),
            "revision_before": revision,
            "revision_after": flow_revision(project_dir),
            "saved": True,
            "removal": removal,
            "plan_updates": sorted(plan_updates),
            "metadata_cleanup": metadata_cleanup,
            "writeback": writeback,
            "validation": validation,
            "valid": bool(validation.get("valid")),
            "message": "已删除目标步骤并重新物化设计器运行文件；请重新读取流程并在运行前复核元素绑定。",
        }
    )


def add_rpa_step(
    paths: ShizaiPaths,
    flow_ref: str,
    component: str,
    *,
    step_id: str | None = None,
    before_step_id: str | None = None,
    label: str | None = None,
    element_ids: list[str] | None = None,
    config: dict[str, Any] | None = None,
    client: Any | None = None,
    allow_unbound_predecessors: bool = False,
) -> dict[str, Any]:
    """Append one component while serializing concurrent CLI writebacks."""
    with _project_write_lock(paths, flow_ref):
        return _add_rpa_step_unlocked(
            paths,
            flow_ref,
            component,
            step_id=step_id,
            before_step_id=before_step_id,
            label=label,
            element_ids=element_ids,
            config=config,
            client=client,
            allow_unbound_predecessors=allow_unbound_predecessors,
        )


def _add_rpa_step_unlocked(
    paths: ShizaiPaths,
    flow_ref: str,
    component: str,
    *,
    step_id: str | None = None,
    before_step_id: str | None = None,
    label: str | None = None,
    element_ids: list[str] | None = None,
    config: dict[str, Any] | None = None,
    client: Any | None = None,
    allow_unbound_predecessors: bool = False,
) -> dict[str, Any]:
    """Append one component to a staged or client-owned native flow."""
    candidate = Path(flow_ref).expanduser()
    if candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(candidate.resolve())
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            return _add_native_rpa_step(
                paths,
                str(candidate.resolve()),
                component,
                step_id=step_id,
                before_step_id=before_step_id,
                label=label,
                element_ids=element_ids,
                config=config,
                client=client,
                allow_unbound_predecessors=allow_unbound_predecessors,
            )
    try:
        resolved = resolve_project(paths, flow_ref)
    except FileNotFoundError:
        resolved = None
    if resolved is not None and not (resolved / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(resolved)
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            return _add_native_rpa_step(
                paths,
                str(resolved),
                component,
                step_id=step_id,
                before_step_id=before_step_id,
                label=label,
                element_ids=element_ids,
                config=config,
                client=client,
                allow_unbound_predecessors=allow_unbound_predecessors,
            )
    flow_path, flow = _read_rpa_flow(paths, flow_ref)
    component_name = str(component).strip()
    step_index = len(flow["steps"]) + 1
    _publish_rpa_ui(
        "component_writing",
        "component_write",
        f"正在把第 {step_index} 个组件 {component_name} 写入流程草稿。",
        animation="component_write",
        flow_ref=str(flow_path.parent),
        component=component_name,
        step_index=step_index,
        step_count=len(flow["steps"]),
    )
    contract = _live_rpa_contract(paths, component_name)
    if contract is None:
        raise ValueError(f"RPA component is not installed in the live catalog: {component_name}")
    if contract.get("catalog_backed") and not contract.get("materializable", False):
        reasons = "; ".join(contract.get("materialization_errors") or ["unsupported catalog code contract"])
        raise ValueError(f"RPA component cannot be materialized: {component_name}: {reasons}")
    steps = flow["steps"]
    _guard_rpa_append_structure(steps, component_name, config)
    pending_binding = next(
        (item for item in steps if isinstance(item, dict) and str(item.get("binding_status") or "") == "unbound"),
        None,
    )
    flexible_flow = str(flow.get("planning_policy") or flow.get("validation_mode") or "").casefold() in {
        "flexible", "advisory", "model_decides"
    }
    if pending_binding is not None and not (allow_unbound_predecessors or flexible_flow):
        raise ValueError(
            f"bind element selector for component {pending_binding.get('id')} before adding another component"
        )
    expected_sequence = _rpa_sequence(flow)
    expected_component = expected_sequence[len(steps)] if len(steps) < len(expected_sequence) else None
    if expected_component is None and not flexible_flow:
        raise ValueError("all supported RPA components are already added; validate the staged flow")
    if expected_component and component_name != expected_component and not flexible_flow:
        if not steps:
            raise ValueError("the first RPA step must be selected from the live component catalog")
        raise ValueError(
            f"the next RPA component must be {expected_component}; follow shizai_rpa_components and add components in order"
        )
    normalized_ids = [str(item).strip() for item in (element_ids or []) if str(item).strip()]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError(f"duplicate element ids in step {step_id or len(steps) + 1}")
    normalized_step_id = str(step_id or f"step-{len(steps) + 1}").strip()
    if not normalized_step_id:
        raise ValueError("step_id must not be empty")
    if any(isinstance(item, dict) and item.get("id") == normalized_step_id for item in steps):
        raise ValueError(f"duplicate RPA step id: {normalized_step_id}")
    if contract["requires_elements"]:
        try:
            _, native = _element_file(paths, flow_ref)
        except (FileNotFoundError, OSError, ValueError) as exc:
            if normalized_ids:
                raise ValueError(f"cannot validate element bindings for {component_name}: {exc}") from exc
            native = {"elementMap": {}}
        available = set(native["elementMap"])
        missing = [item for item in normalized_ids if item not in available]
        if missing:
            raise ValueError(
                f"{component_name} references unimported elements: {', '.join(missing)}; use shizai_project_import_elements first"
            )
    step_config = _adapt_rpa_component_config(
        component_name,
        dict(config) if isinstance(config, dict) else {},
        flow,
    )
    if component_name == "WebBrowser_CreateV3":
        step_config.setdefault("url", flow.get("target_url", ""))
        step_config.setdefault("type", "chrome")
    if component_name == "Basic_Delay_V1":
        step_config.setdefault("second", flow.get("wait_seconds", 1))
    if component_name == "Keyboard_PressNew":
        # Keep staged flows equivalent to native materialization.  The
        # designer component requires an explicit Enter key and action type;
        # omitting them leaves the step present but invalid until a later
        # config update.
        step_config.setdefault("key", 13)
        step_config.setdefault("type", "press")
        step_config.setdefault("auxiliary_key", [])
        step_config.setdefault("input_us", "1")
    if component_name == "WebBrowser_GetTAB_V1":
        step_config.setdefault("var_name", "网页对象")
        step_config.setdefault("type", "chrome")
        step_config.setdefault("match_type", 2)
        step_config.setdefault("title", "")
        step_config.setdefault("url", "")
        step_config.setdefault("isActive", 1)
    if component_name == "WebBrowser_DataCollectionV5":
        step_config.setdefault("designated_obj", "网页对象")
        step_config.setdefault("columns", list(flow.get("required_columns", [])))
        step_config.setdefault("toExcel", 0 if flow.get("excel_mode") else 1)
        step_config.setdefault("output_file", flow.get("output_file", "result.xlsx"))
        step_config.update(
            {
                key: value
                for key, value in _collection_paging_settings(step_config).items()
                if key not in {"selector_page", "selector_more"} or key in step_config
            }
        )
        # A new single-page collection has no pagination control. The
        # selector fields are still persisted as empty strings so the native
        # task never receives Python/JSON null for a hidden selector input.
        step_config.setdefault("selector_page", "")
        step_config.setdefault("selector_more", "")
        columns = [str(item).strip() for item in step_config.get("columns", []) if str(item).strip()] or list(
            DEFAULT_RPA_COLUMNS
        )
        _, staged_elements = _element_file(paths, flow_ref)
        normalized_ids = _normalize_generated_collection_bindings(
            normalized_ids,
            columns,
            staged_elements.get("elementMap", {}),
        )
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("WebBrowser_DataCollectionV5 has duplicate column element bindings")
        collection_stage = _collection_stage_value({"config": step_config})
        if collection_stage:
            step_config["collection_stage"] = collection_stage
    if component_name == "WebBrowser_RunJsInIeBySelector_v3":
        js_table = bool(flow.get("use_js_table"))
        step_config.setdefault(
            "output_variable",
            JS_TABLE_OUTPUT_VARIABLE if js_table else "商品链接",
        )
        step_config.setdefault("designated_info", "1")
        step_config.setdefault("web_page", "网页对象")
        step_config.setdefault("page_scope", True)
        # A page-scoped JS step has no element selector.  The designer task
        # schema expects an empty string here; serializing Python None makes
        # RuntimeHost pass a JSON null into CommonParseSelector.
        step_config.setdefault("uiSelector", "")
        step_config.setdefault("input", ".plugin_goodsCardWrapper, .gl-item")
        step_config.setdefault(
            "jsCode",
            _js_table_script(flow.get("required_columns"), flow.get("max_rows"))
            if js_table
            else DEFAULT_JS_LINK_SCRIPT,
        )
        step_config.setdefault("executionEnvironment", "MAIN")
        step_config.setdefault("isAsyncCode", "0")
        step_config.setdefault("waitResultTimeout", "30")
        step_config.setdefault("generated_element_id", JS_LINK_ELEMENT_ID)
    if component_name == "Excel_Cs_NewV4":
        output_file = Path(str(step_config.get("name") or flow.get("output_file", "result.xlsx"))).name
        step_config.setdefault("path", "res")
        step_config.setdefault("name", output_file or "result.xlsx")
        step_config.setdefault("software", "默认")
        step_config.setdefault("show", "1")
        step_config.setdefault("maximize", "1")
        step_config.setdefault("share", "0")
    if component_name == "Excel_Cs_WriteRange":
        step_config.setdefault("workbook", "Excel文件")
        step_config.setdefault("sheet", "Sheet1")
        step_config.setdefault("row", "1")
        step_config.setdefault("column", "A")
        step_config.setdefault("contents", "数据采集结果")
        step_config.setdefault("save", "1")
    step = {
        "id": normalized_step_id,
        "component": component_name,
        "label": label or contract["designer_label"],
        "element_ids": normalized_ids,
        "config": step_config,
        "binding_status": "bound"
        if normalized_ids
        else ("unbound" if contract["requires_elements"] else "not_required"),
    }
    if component_name == "WebBrowser_DataCollectionV5":
        collection_stage = _collection_stage_value({"config": step_config})
        if collection_stage:
            step["collection_stage"] = collection_stage
    if component_name == "WebBrowser_RunJsInIeBySelector_v3" and normalized_ids and not bool(flow.get("use_js_table")):
        _upsert_js_generated_elements(paths, flow_ref, normalized_ids[0])
    normalized_before_step_id = str(before_step_id or "").strip()
    if normalized_before_step_id:
        insert_index = next(
            (
                index
                for index, item in enumerate(steps)
                if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_before_step_id
            ),
            None,
        )
        if insert_index is None:
            raise ValueError(f"before_step_id is not an existing step: {normalized_before_step_id}")
        steps.insert(insert_index, step)
        component_plan, plan_key = _rpa_component_plan(flow)
        if isinstance(component_plan, list) and component_plan:
            revised_plan = deepcopy(component_plan)
            plan_index = next(
                (
                    index
                    for index, item in enumerate(revised_plan)
                    if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_before_step_id
                ),
                None,
            )
            if plan_index is not None:
                target = revised_plan[plan_index]
                dependencies = list(target.get("depends_on") or []) if isinstance(target, dict) else []
                revised_plan.insert(
                    plan_index,
                    {
                        "id": normalized_step_id,
                        "component": component_name,
                        "purpose": label or contract["designer_label"],
                        "depends_on": dependencies,
                        "config": deepcopy(step_config),
                        "outputs": [str(step_config.get("output_variable") or "")]
                        if component_name == "WebBrowser_RunJsInIeBySelector_v3"
                        else [],
                    },
                )
                if isinstance(target, dict):
                    target["depends_on"] = [normalized_step_id]
                for order, item in enumerate(revised_plan, start=1):
                    if isinstance(item, dict):
                        item["order"] = order
                if plan_key == "execution_plan.steps":
                    flow.setdefault("execution_plan", {})["steps"] = revised_plan
                else:
                    flow[plan_key] = revised_plan
    else:
        steps.append(step)
    flow["status"] = "browser_open_step_added" if component_name == "WebBrowser_CreateV3" else "components_added"
    flow["phase"] = "browser_capture_required" if component_name == "WebBrowser_CreateV3" else "component_added"
    if component_name in {"Keyboard_Send_V1", "Keyboard_PressNew"}:
        flow["phase"] = "results_capture_required"
    if component_name == "WebBrowser_DataCollectionV5":
        flow["phase"] = "excel_components_required" if flow.get("excel_mode") else "ready_for_validation"
    if component_name == "WebBrowser_RunJsInIeBySelector_v3":
        if bool(flow.get("use_js_table")):
            flow["use_js_link"] = False
        else:
            flow["use_js_link"] = True
        flow["phase"] = "data_collection_required"
    if component_name == "Excel_Cs_NewV4":
        flow["phase"] = "excel_write_required"
    if component_name == "Excel_Cs_WriteRange":
        flow["phase"] = "ready_for_validation"
    next_action = _rpa_next_action({**flow, "flow_ref": str(flow_path.parent)})
    flow["next_tool"] = (
        next_action.get("tool")
        or (next_action.get("tools") or [None])[0]
        or next_action.get("then")
        or "shizai_rpa_validate_flow"
    )
    _write_rpa_flow(flow_path, flow)
    step_validation = validate_rpa_step(paths, str(flow_path.parent), normalized_step_id)
    verification_gate = _rpa_verification_gate(
        str(flow_path.parent),
        step,
        step_validation,
    )
    ui_feedback = _publish_rpa_ui(
        "step_ready" if step_validation["valid"] else "validation_failed",
        "component_validation",
        (
            f"第 {step_index} 个组件 {component_name} 已写入并通过静态校验。"
            if step_validation["valid"]
            else f"第 {step_index} 个组件 {component_name} 已写入，但静态校验未通过，请先修复。"
        ),
        animation="step_complete" if step_validation["valid"] else "validation_error",
        flow_ref=str(flow_path.parent),
        component=component_name,
        step_id=normalized_step_id,
        step_index=step_index,
        step_count=len(steps),
        valid=bool(step_validation["valid"]),
        next_tool=flow["next_tool"],
    )
    return {
        "flow_ref": str(flow_path.parent),
        "step": step,
        "step_count": len(steps),
        "next_tool": flow["next_tool"],
        "phase": flow["phase"],
        "next_action": next_action,
        "step_validation": step_validation,
        "verification_gate": verification_gate,
        "ui_feedback": ui_feedback,
        "message": "导航组件已创建；如后续步骤需要网页元素，请连接当前页面并执行 browser_screenshot + browser_snapshot，再校验/导入元素。"
        if component_name == "WebBrowser_CreateV3"
        else "组件已加入，请按 next_action 继续；元素步骤必须使用已导入的真实元素。",
    }


def set_rpa_step_elements(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    element_ids: list[str],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Bind imported element ids to a staged or native RPA step."""
    candidate = Path(flow_ref).expanduser()
    if candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(candidate.resolve())
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            return _set_native_rpa_step_elements(paths, str(candidate.resolve()), step_id, element_ids, client=client)
    try:
        resolved = resolve_project(paths, flow_ref)
    except FileNotFoundError:
        resolved = None
    if resolved is not None and not (resolved / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(resolved)
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            return _set_native_rpa_step_elements(paths, str(resolved), step_id, element_ids, client=client)
    flow_path, flow = _read_rpa_flow(paths, flow_ref)
    normalized_ids = [str(item).strip() for item in element_ids if str(item).strip()]
    if not normalized_ids:
        raise ValueError("element_ids must contain at least one id")
    target_step = next(
        (step for step in flow["steps"] if isinstance(step, dict) and step.get("id") == step_id),
        None,
    )
    if target_step is None:
        raise ValueError(f"RPA step not found: {step_id}")
    contract = _live_rpa_contract(paths, str(target_step.get("component") or ""))
    if contract is None or not contract["requires_elements"]:
        raise ValueError(f"RPA step does not accept element bindings: {step_id}")
    _publish_rpa_ui(
        "element_binding",
        "element_rebind",
        f"正在把 {len(normalized_ids)} 个网页元素绑定到组件 {target_step.get('component')}。",
        animation="element_bind",
        flow_ref=str(flow_path.parent),
        component=str(target_step.get("component")),
        step_id=step_id,
        element_count=len(normalized_ids),
    )
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError(f"duplicate element ids in step {step_id}")
    _, native = _element_file(paths, flow_ref)
    available = set(native["elementMap"])
    missing = [item for item in normalized_ids if item not in available]
    if missing:
        raise ValueError("elements are not imported into this flow: " + ", ".join(missing))
    target_step["element_ids"] = normalized_ids
    target_step["binding_status"] = "bound"
    flow["status"] = "elements_bound"
    flow["phase"] = "component_added"
    next_action = _rpa_next_action({**flow, "flow_ref": str(flow_path.parent)})
    flow["next_tool"] = (
        next_action.get("tool")
        or (next_action.get("tools") or [None])[0]
        or next_action.get("then")
        or "shizai_rpa_validate_flow"
    )
    _write_rpa_flow(flow_path, flow)
    ui_feedback = _publish_rpa_ui(
        "elements_ready",
        "element_rebind",
        f"组件 {target_step.get('component')} 的元素绑定已更新，可以继续下一步。",
        animation="element_ready",
        flow_ref=str(flow_path.parent),
        component=str(target_step.get("component")),
        step_id=step_id,
        element_count=len(normalized_ids),
        next_tool=flow["next_tool"],
    )
    return {
        "flow_ref": str(flow_path.parent),
        "step": target_step,
        "phase": flow["phase"],
        "next_tool": flow["next_tool"],
        "next_action": next_action,
        "ui_feedback": ui_feedback,
    }


def _sync_plan_step_metadata(
    metadata: dict[str, Any],
    step_id: str,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Update the authoritative plan and its legacy mirror as one metadata write."""
    execution_plan = metadata.get("mcpExecutionPlan")
    if isinstance(execution_plan, dict) and isinstance(execution_plan.get("steps"), list):
        plan = deepcopy(execution_plan)
        steps = deepcopy(plan["steps"])
        source = "mcpExecutionPlan.steps"
    elif isinstance(metadata.get("mcpComponentPlan"), list):
        plan = None
        steps = deepcopy(metadata["mcpComponentPlan"])
        source = "mcpComponentPlan"
    else:
        return {}
    target = next(
        (item for item in steps if isinstance(item, dict) and str(item.get("id") or "") == str(step_id)),
        None,
    )
    if target is None:
        return {}
    merged_config = dict(target.get("config") or {})
    merged_config.update(deepcopy(config))
    target["config"] = merged_config
    target["outputs"] = _logical_plan_outputs(str(target.get("component") or ""), contract, merged_config)
    updates: dict[str, Any] = {
        "mcpComponentPlan": deepcopy(steps),
        "mcpPlanSource": source,
    }
    if plan is not None:
        plan["steps"] = deepcopy(steps)
        plan.pop("plan_id", None)
        plan["plan_id"] = (
            "plan-"
            + hashlib.sha256(
                json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
        )
        updates.update(mcpExecutionPlan=plan, mcpPlanId=plan["plan_id"])
    return updates


def update_rpa_step_config(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    config: dict[str, Any],
    *,
    client: Any | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Prepare a native parameter edit, optionally preview it, then revalidate."""
    project_dir = resolve_project(paths, flow_ref)
    revision = check_revision(project_dir, expected_revision)
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    node = entities.get(str(step_id)) if isinstance(entities, dict) else None
    if not isinstance(node, dict):
        raise ValueError(f"RPA step not found: {step_id}")
    if str(step_id) not in _native_statement_ids(payload):
        raise WorkflowEditError(
            "unsupported_edit_target", "当前参数编辑仅支持主流程步骤；嵌套节点和主容器需专门的图编辑接口。",
            step_id=str(step_id), retryable=False,
        )
    if not isinstance(config, dict) or not config:
        raise ValueError("config must be a non-empty JSON object")
    original_node = deepcopy(node)
    pending_elements: dict[str, Any] | None = None
    pending_element_path: Path | None = None
    component = str(node.get("type") or "")
    original_collection_paging: dict[str, Any] = {}
    for candidate in entities.values() if isinstance(entities, dict) else []:
        if not isinstance(candidate, dict) or candidate.get("type") != "WebBrowser_DataCollectionV5":
            continue
        original_collection_paging = {
            name: _native_task_argument(candidate, name, "")
            for name in (
                "collect_pages",
                "page_type",
                "page_type_num",
                "selector_page",
                "selector_more",
            )
        }
        break
    flow_state = _native_rpa_state(paths, str(project_dir))
    metadata_ok, plan_metadata, _ = _safe_json(project_dir / "szrpa.json")
    plan_metadata = plan_metadata if metadata_ok and isinstance(plan_metadata, dict) else {}
    live_contract = _live_rpa_contract(paths, component) or {}
    # Normalize planner/CLI aliases before checking the live native arguments.
    # This keeps the public update surface in sync with component_catalog and
    # avoids a stale hand-maintained whitelist blocking valid settings.
    normalized_config = _adapt_rpa_component_config(component, dict(config), flow_state)
    if component == "WebBrowser_DataCollectionV5":
        # Add and edit paths share the same DataCollectionV5 formatter
        # contract. Raw DeepCapture ids become a dedicated expression-safe
        # mcp-page-scope selector in memory so dry-run stays read-only and the
        # final revision check does not observe this update's own write.
        try:
            element_path, current_elements = _element_file(paths, str(project_dir))
            prepared_elements = deepcopy(current_elements)
        except (FileNotFoundError, OSError, ValueError):
            element_path = None
            current_elements = None
            prepared_elements = None
        normalized_config = _materialize_collection_paging_selectors(
            paths,
            str(project_dir),
            normalized_config,
            pending_elements=prepared_elements,
        )
        if prepared_elements is not None and prepared_elements != current_elements:
            pending_elements = prepared_elements
            pending_element_path = element_path
    if isinstance(normalized_config.get("pagination"), dict):
        normalized_config.pop("pagination", None)
    allowed: dict[str, set[str]] = {
        "WebBrowser_CreateV3": {"url", "openNewTab", "waitingload"},
        "WebBrowser_GetTAB_V1": {"type", "match_type", "title", "url", "isActive"},
        "Element_SetValue_V6": {"text", "designated_obj", "isEnter", "uiSelector"},
        "Keyboard_Send_V1": {"words", "interval", "fuctype"},
        "Keyboard_PressNew": {"key", "type", "auxiliary_key", "input_us"},
        "Basic_Delay_V1": {"second"},
        "WebBrowser_RunJsInIeBySelector_v3": {
            "jsCode", "input", "waitResultTimeout", "uiSelector", "output_variable",
            "page_scope", "result_mode", "output_columns", "max_rows", "isAsyncCode",
        },
        "WebBrowser_DataCollectionV5": {
            "designated_obj", "toExcel", "output_file", "columns", "config", "max_rows",
            "region", "collect_pages", "collect_items", "page_type", "page_type_num",
            "selector_page", "selector_more", "deduplication", "operation_num", "scroll_config",
            "wait_time_page", "showTable", "filename_suffix", "filePath", "monitor_data",
            "share", "append_data", "use_sheet", "sheet_name", "data_attributes",
            "pageInfo", "page_info", "page_url", "page_title", "pageUrl", "windowTitle",
        },
        "Excel_Cs_NewV4": {"path", "name", "software", "show", "maximize", "share"},
        "Excel_Cs_WriteRange": {"workbook", "sheet", "row", "column", "contents", "save"},
    }
    native_parameter_names = {
        str(item.get("name") or "").strip()
        for key in ("args0", "options0")
        for item in (node.get(key) if isinstance(node.get(key), list) else [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    supported = allowed.get(component, set()) | native_parameter_names
    if supported is None:
        raise ValueError(f"component does not expose MCP config updates: {component}")
    unknown = sorted(set(normalized_config) - supported)
    if unknown:
        raise ValueError(f"unsupported config keys for {component}: {', '.join(unknown)}")

    def set_json(name: str, value: Any) -> None:
        _native_set_argument(node, name, json.dumps(value, ensure_ascii=False))

    metadata_updates: dict[str, Any] = {}
    for name, value in normalized_config.items():
        # Remember the live value so compatibility branches can still fall
        # through to the generic contract writer for newly added native
        # arguments/options (for example browser launch flags).
        before_value = next(
            (
                entry.get("value")
                for key in ("args0", "options0")
                for entry in (node.get(key) if isinstance(node.get(key), list) else [])
                if isinstance(entry, dict) and entry.get("name") == name
            ),
            object(),
        )
        if component == "WebBrowser_CreateV3":
            if name == "url":
                set_json(name, str(value))
            elif name in {"openNewTab", "waitingload"}:
                _native_set_argument(node, name, "1" if _native_bool(value) else "0")
        elif component == "WebBrowser_GetTAB_V1":
            if name in {"type", "title", "url"}:
                set_json(name, str(value))
            elif name in {"match_type", "isActive"}:
                try:
                    rendered = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"WebBrowser_GetTAB_V1 {name} must be an integer") from exc
                if name == "isActive" and rendered not in {0, 1}:
                    raise ValueError("WebBrowser_GetTAB_V1 isActive must be 0 or 1")
                _native_set_argument(node, name, str(rendered))
        elif component == "Element_SetValue_V6":
            if name == "text":
                set_json(name, str(value))
            elif name == "isEnter":
                _native_set_argument(node, name, "1" if _native_bool(value) else "0")
            elif name == "designated_obj":
                _native_set_argument(node, name, _native_designated_object(value))
            else:
                _native_set_argument(node, name, str(value))
        elif component == "Keyboard_Send_V1":
            if name == "words":
                set_json(name, str(value))
            else:
                _native_set_argument(node, name, str(value))
        elif component == "Keyboard_PressNew":
            if name == "auxiliary_key":
                _native_set_argument(node, name, value if isinstance(value, list) else [str(value)])
            elif name in {"key", "type"}:
                set_json(name, str(value))
            else:
                _native_set_argument(node, name, str(value))
        elif component == "Basic_Delay_V1":
            try:
                seconds = max(0.0, min(float(value), 30.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Basic_Delay_V1 second must be a number between 0 and 30") from exc
            rendered = str(seconds).rstrip("0").rstrip(".") if "." in str(seconds) else str(int(seconds))
            _native_set_argument(node, "second", rendered)
        elif component == "WebBrowser_RunJsInIeBySelector_v3":
            if name == "jsCode":
                js_code = str(value)
                _native_set_argument(
                    node,
                    name,
                    js_code if js_code.strip().startswith('"""') else f'"""{js_code}"""',
                )
            elif name == "input":
                _native_set_argument(
                    node,
                    name,
                    _native_list_expression(
                        value,
                        ".plugin_goodsCardWrapper, .gl-item",
                    ),
                )
            elif name == "waitResultTimeout":
                _native_set_argument(node, name, str(value))
            elif name == "isAsyncCode":
                # RuntimeHost's RunJsInIeBySelector_v3 contract expects the
                # native checkbox slot as a numeric literal (1/0).  Passing a
                # JSON-encoded string ("1"/"0") makes the generated call use
                # isAsyncCode="1", which fails boolean conversion at runtime.
                _native_set_parameter(node, name, "1" if _native_bool(value) else "0")
            elif name == "output_variable":
                _native_set_argument(node, "var_name", str(value).strip())
            elif name == "page_scope":
                _native_set_argument(node, "uiSelector", "" if _native_bool(value) else _native_task_argument(node, "uiSelector", ""))
            elif name in {"uiSelector", "output_columns", "max_rows", "result_mode"}:
                # result_mode/output_columns are MCP metadata; native selector
                # and row limit values are retained when the template exposes
                # the corresponding argument.
                if name in native_parameter_names:
                    _native_set_parameter(node, name, _generic_native_value(value))
        elif component == "WebBrowser_DataCollectionV5":
            output_parameters = set(live_contract.get("output_parameters") or [])
            if name == "designated_obj":
                _native_set_argument(node, name, _native_designated_object(value))
            elif name == "toExcel":
                _native_set_argument(node, name, "1" if _native_bool(value) else "0")
            elif name == "output_file":
                output_file = _collection_output_file(
                    {**normalized_config, "output_file": value},
                    fallback=flow_state.get("output_file", "result.xlsx"),
                )
                output_path = Path(output_file)
                _native_set_argument(
                    node,
                    "filename_suffix",
                    [
                        json.dumps(output_path.stem or "jd-products", ensure_ascii=False),
                        json.dumps(output_path.suffix or ".xlsx", ensure_ascii=False),
                    ],
                )
                configured = _native_configured_output_path(output_file)
                file_path = (
                    str(configured.parent)
                    if configured is not None
                    else str(normalized_config.get("filePath") or normalized_config.get("output_path") or "res")
                )
                try:
                    _native_set_parameter(node, "filePath", json.dumps(file_path, ensure_ascii=False))
                except ValueError:
                    _native_set_argument(node, "filePath", json.dumps(file_path, ensure_ascii=False))
            elif name == "config" and isinstance(value, (dict, list)):
                _native_set_argument(node, name, json.dumps(value, ensure_ascii=False))
            elif name in {
                "columns", "data_attributes", "pageInfo", "page_info", "page_url", "page_title",
                "pageUrl", "windowTitle",
            }:
                # Applied once below so columns, attributes, selectors and
                # page identity are updated atomically from one config object.
                continue
            elif name in native_parameter_names:
                if name in {"selector_page", "selector_more"}:
                    rendered = str(value).strip()
                    if rendered and rendered in native_parameter_names:
                        rendered = str(value)
                    _native_set_parameter(node, name, rendered)
                elif name in {
                    "region", "collect_pages", "collect_items", "page_type", "page_type_num",
                    "deduplication", "operation_num", "scroll_config", "wait_time_page", "showTable",
                    "toExcel", "monitor_data", "share", "append_data",
                    "use_sheet", "sheet_name",
                }:
                    if name == "sheet_name":
                        _native_set_parameter(node, name, str(value))
                    else:
                        _native_set_parameter(node, name, _native_numeric_expression(value))
                elif name == "filename_suffix":
                    suffix = value if isinstance(value, (list, tuple)) else [value]
                    rendered_suffix = [
                        item if isinstance(item, str) and len(item) >= 2 and item[0] == '"' and item[-1] == '"'
                        else json.dumps(str(item), ensure_ascii=False)
                        for item in suffix
                    ]
                    _native_set_parameter(node, name, rendered_suffix)
                elif name == "filePath":
                    _native_set_parameter(node, name, json.dumps(str(value), ensure_ascii=False))
                else:
                    _native_set_parameter(
                        node,
                        name,
                        _generic_native_value(value, output_parameter=name in output_parameters),
                    )
        elif component == "Excel_Cs_NewV4":
            if name in {"path", "name", "software"}:
                set_json(name, str(value))
            elif name in {"show", "maximize", "share"}:
                _native_set_argument(node, name, "1" if _native_bool(value) else "0")
        elif component == "Excel_Cs_WriteRange":
            if name == "sheet":
                set_json(name, str(value))
            elif name in {"row", "column"}:
                set_json(name, str(value))
            elif name == "save":
                _native_set_argument(node, name, "1" if _native_bool(value) else "0")
        else:
            # Live catalog components are intentionally not narrowed to a
            # compatibility whitelist. Their native args0/options0 shape
            # and catalog output bindings are the complete update contract.
            if name in native_parameter_names:
                output_parameters = set((_live_rpa_contract(paths, component) or {}).get("output_parameters") or [])
                _native_set_parameter(
                    node,
                    name,
                    _generic_native_value(value, output_parameter=name in output_parameters),
                )
            else:
                raise ValueError(f"component {component} has no parameter named {name}")

        # Known compatibility components may expose parameters added by a
        # newer designer build. If the specialized branch did not touch the
        # live slot, encode it exactly as the catalog contract describes.
        if name in native_parameter_names and name not in {
            "output_variable", "page_scope", "output_columns", "max_rows", "result_mode",
            "columns", "output_file", "pagination", "output_path", "data_attributes",
            "designated_obj",
            "pageInfo", "page_info", "page_url", "page_title", "pageUrl", "windowTitle",
        }:
            after_value = next(
                (
                    entry.get("value")
                    for key in ("args0", "options0")
                    for entry in (node.get(key) if isinstance(node.get(key), list) else [])
                    if isinstance(entry, dict) and entry.get("name") == name
                ),
                object(),
            )
            if after_value == before_value:
                contract = _live_rpa_contract(paths, component) or {}
                _native_set_parameter(
                    node,
                    name,
                    _generic_native_value(
                        value,
                        output_parameter=name in set(contract.get("output_parameters") or []),
                    ),
                )

    if component == "WebBrowser_DataCollectionV5":
        # Keep the nested RuntimeHost object authoritative. A config update
        # may change only columns, only page identity, or provide a complete
        # collectionConfig; all forms go through the same synchronizer.
        try:
            _, native_elements = _element_file(paths, str(project_dir))
        except (FileNotFoundError, OSError, ValueError):
            native_elements = {"elementMap": {}, "groupMap": {}}
        if pending_elements is not None:
            native_elements = pending_elements
        metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
        metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
        nested_config = normalized_config.get("config")
        nested_config = nested_config if isinstance(nested_config, dict) else None
        existing_page = nested_config.get("pageInfo") if isinstance(nested_config, dict) else {}
        existing_page = existing_page if isinstance(existing_page, dict) else {}
        page_info_value = normalized_config.get("pageInfo")
        if not isinstance(page_info_value, dict):
            page_info_value = normalized_config.get("page_info")
        if not isinstance(page_info_value, dict):
            page_info_value = existing_page
        page_url = normalized_config.get("page_url") or normalized_config.get("pageUrl")
        page_title = normalized_config.get("page_title") or normalized_config.get("windowTitle")
        if page_url in (None, ""):
            page_url = page_info_value.get("pageUrl") or page_info_value.get("url")
        if page_title in (None, ""):
            page_title = page_info_value.get("windowTitle") or page_info_value.get("title")
        current = _native_collection_config(node)
        if nested_config is not None:
            current.update(deepcopy(nested_config))
        current, synced_columns, synced_attributes = _sync_native_collection_config(
            node,
            native_elements,
            columns=normalized_config.get("columns") if isinstance(normalized_config.get("columns"), list) else None,
            data_attributes=normalized_config.get("data_attributes")
            if isinstance(normalized_config.get("data_attributes"), dict)
            else metadata.get("mcpCollectionAttributes"),
            keyword=str(metadata.get("mcpKeyword") or ""),
            config_value=current,
            page_url=str(page_url).strip() if page_url not in (None, "") else None,
            page_title=str(page_title).strip() if page_title not in (None, "") else None,
        )
        _native_set_argument(node, "config", json.dumps(current, ensure_ascii=False))
        metadata_updates["mcpRequiredColumns"] = synced_columns
        metadata_updates["mcpCollectionAttributes"] = synced_attributes
        effective_page = current.get("pageInfo") if isinstance(current.get("pageInfo"), dict) else {}
        if effective_page.get("pageUrl"):
            metadata_updates["mcpCollectionPageUrl"] = str(effective_page["pageUrl"])
        if effective_page.get("windowTitle"):
            metadata_updates["mcpCollectionPageTitle"] = str(effective_page["windowTitle"])
        # Re-apply paging selectors after nested collection synchronization.
        # Some designer task templates expose selector_page only as an
        # optional argument and their config normalizer can restore its empty
        # default while rebuilding the nested collectionConfig object.
        if "selector_page" in normalized_config:
            page_value = str(normalized_config.get("selector_page") or "").strip()
            _native_set_parameter(node, "selector_page", page_value)
        if "selector_more" in normalized_config:
            more_value = str(normalized_config.get("selector_more") or "").strip()
            _native_set_parameter(node, "selector_more", more_value)
        effective_paging = _collection_paging_settings(
            {
                name: _native_task_argument(node, name, "")
                for name in (
                    "region",
                    "collect_pages",
                    "collect_items",
                    "page_type",
                    "page_type_num",
                    "selector_page",
                    "selector_more",
                )
            }
        )
        metadata_updates.update(
            {
                "mcpCollectionRegion": effective_paging["region"],
                "mcpCollectionPageType": effective_paging["page_type"],
                "mcpCollectionPageTypeNum": effective_paging["page_type_num"],
            }
        )

    if component == "WebBrowser_CreateV3":
        metadata_updates.update(
            {
                "mcpTargetUrl": _decode_selector_value(_native_task_argument(node, "url", "")),
                "mcpOpenNewTab": _native_bool(_native_task_argument(node, "openNewTab", 0)),
            }
        )
    if component == "WebBrowser_GetTAB_V1":
        metadata_updates["mcpRefreshBrowserTab"] = True
    if component == "WebBrowser_DataCollectionV5":
        raw_output = _collection_output_file(
            normalized_config,
            fallback=flow_state.get("output_file", "result.xlsx"),
        )
        metadata_updates["mcpOutputFile"] = Path(raw_output).name or "result.xlsx"
        configured = _native_configured_output_path(raw_output)
        if configured:
            metadata_updates["mcpOutputPath"] = str(configured)
        elif normalized_config.get("filePath") or normalized_config.get("output_path"):
            configured_dir = _native_configured_output_path(
                normalized_config.get("filePath") or normalized_config.get("output_path")
            )
            if configured_dir:
                metadata_updates["mcpOutputPath"] = str(configured_dir / Path(raw_output).name)
    if component == "Excel_Cs_NewV4" and "name" in normalized_config:
        raw_name = str(normalized_config["name"])
        metadata_updates["mcpOutputFile"] = Path(raw_name).name or "result.xlsx"
        configured = _native_configured_output_path(raw_name)
        raw_path = _native_configured_output_path(normalized_config.get("path"))
        if configured is None and raw_path is not None:
            configured = raw_path / (Path(raw_name).name or "result.xlsx")
        if configured:
            metadata_updates["mcpOutputPath"] = str(configured)
    metadata_updates.update(
        _sync_plan_step_metadata(plan_metadata, str(step_id), dict(config), live_contract)
    )
    changes = parameter_changes(original_node, node)
    check_revision(project_dir, revision)
    if dry_run:
        return redact_payload({
            "kind": "native_rpa_step_preview", "flow_ref": str(project_dir), "step_id": str(step_id),
            "revision": revision, "saved": False, "dry_run": True, "updated_config": normalized_config,
            "changes": changes, "metadata_updates": metadata_updates,
            "elements_changed": pending_elements is not None,
            "validation_scope": "parameter_encoding_only", "runtime_verified": False,
            "next_call": {"tool": "shizai_rpa_update_step", "arguments": {
                "flow_ref": str(project_dir), "step_id": str(step_id), "config": config,
                "expected_revision": revision, "dry_run": False, "allow_write": True,
            }},
        })
    _publish_rpa_ui(
        "component_writing", "component_update", f"正在更新组件 {component} 的参数并重新校验。",
        animation="component_update", flow_ref=str(project_dir), component=component, step_id=str(step_id),
    )
    if component == "Database_DBConnect":
        _ensure_sqlite_database_file(
            {
                "dtype": _native_task_argument(node, "dtype", ""),
                "DatabaseConfig": _native_task_argument(node, "DatabaseConfig", []),
            },
            project_dir=project_dir,
        )
    # All parameter conversion succeeds before the first durable mutation.
    if pending_elements is not None and pending_element_path is not None:
        temporary = pending_element_path.with_suffix(pending_element_path.suffix + ".tmp")
        temporary.write_text(json.dumps(pending_elements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(pending_element_path)
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    _update_native_metadata(project_dir, len(_native_statement_ids(payload)), metadata_updates)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    if original_collection_paging:
        # Older designer RPC models do not round-trip the optional paging
        # selector fields and can restore their empty defaults during save.
        # Restore the authoritative values after any component update, then
        # refresh the local task/index before producing readback for callers.
        task_path, persisted_payload = _native_task_file(project_dir)
        persisted_entities = persisted_payload.get("componentModelEntities")
        persisted_collection = next(
            (
                candidate
                for candidate in persisted_entities.values()
                if isinstance(candidate, dict)
                and candidate.get("type") == "WebBrowser_DataCollectionV5"
            ),
            None,
        ) if isinstance(persisted_entities, dict) else None
        if isinstance(persisted_collection, dict):
            desired_paging = dict(original_collection_paging)
            if component == "WebBrowser_DataCollectionV5":
                for paging_name in desired_paging:
                    if paging_name in normalized_config:
                        desired_paging[paging_name] = normalized_config.get(paging_name)
            for paging_name, paging_value in desired_paging.items():
                _native_set_parameter(
                    persisted_collection,
                    paging_name,
                    str(paging_value or "").strip(),
                )
            temporary = task_path.with_suffix(task_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(persisted_payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(task_path)
            _update_native_task_index(project_dir, task_path)
            _update_native_file_list(project_dir, task_path)
        _, readback_payload = _native_task_file(project_dir)
        readback_paging = _native_collection_paging_snapshot(readback_payload)
        expected_paging = dict(original_collection_paging)
        if component == "WebBrowser_DataCollectionV5":
            for paging_name in expected_paging:
                if paging_name in normalized_config:
                    expected_paging[paging_name] = normalized_config.get(paging_name)
        paging_conflicts = {
            name: {"expected": str(value or "").strip(), "actual": readback_paging.get(name, "")}
            for name, value in expected_paging.items()
            if str(value or "").strip() != readback_paging.get(name, "")
        }
        if paging_conflicts:
            raise ValueError(
                "native_paging_readback_conflict: "
                + json.dumps(paging_conflicts, ensure_ascii=False, sort_keys=True)
            )
        if isinstance(writeback, dict):
            writeback["readback_paging"] = readback_paging
    updated = _native_rpa_state(paths, str(project_dir))
    step = next(item for item in updated["steps"] if item["id"] == str(step_id))
    step_validation = validate_rpa_step(paths, str(project_dir), str(step_id))
    ui_feedback = _publish_rpa_ui(
        "step_ready" if step_validation["valid"] else "validation_failed",
        "component_validation",
        f"组件 {component} 参数已更新并通过静态校验。"
        if step_validation["valid"]
        else f"组件 {component} 参数已更新，但静态校验未通过。",
        animation="step_complete" if step_validation["valid"] else "validation_error",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        component=component,
        step_id=str(step_id),
        valid=bool(step_validation["valid"]),
        next_tool=updated["next_tool"],
    )
    return {
        "kind": "native_rpa_step_updated",
        "saved": True,
        "previous_revision": revision,
        "revision": flow_revision(project_dir),
        "changes": changes,
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "step": step,
        "updated_config": normalized_config,
        "step_validation": step_validation,
        "phase": updated["phase"],
        "next_tool": updated["next_tool"],
        "next_action": updated["next_action"],
        "ui_feedback": ui_feedback,
        "writeback": writeback,
    }


def bind_rpa_pagination(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    element_id: str,
    *,
    mode: str = "next-page",
    region: str | int | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Bind one imported page control without touching collection columns."""
    normalized_mode = str(mode or "").strip().casefold().replace("_", "-")
    mode_aliases = {
        "next": "next-page",
        "nextpage": "next-page",
        "loadmore": "load-more",
        "more": "load-more",
    }
    normalized_mode = mode_aliases.get(normalized_mode, normalized_mode)
    if normalized_mode not in {"next-page", "load-more"}:
        raise ValueError("pagination mode must be next-page or load-more")
    normalized_element_id = str(element_id or "").strip()
    if not normalized_element_id:
        raise ValueError("pagination element_id must not be empty")
    project_dir = resolve_project(paths, flow_ref)
    _, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    node = entities.get(str(step_id)) if isinstance(entities, dict) else None
    if not isinstance(node, dict) or node.get("type") != "WebBrowser_DataCollectionV5":
        raise ValueError(f"pagination binding requires a WebBrowser_DataCollectionV5 step: {step_id}")
    _, elements = _element_file(paths, str(project_dir))
    element_map = elements.get("elementMap") if isinstance(elements, dict) else {}
    if not isinstance(element_map, dict) or normalized_element_id not in element_map:
        raise ValueError(f"pagination element is not imported into this flow: {normalized_element_id}")
    source, verification = _element_verification_state(element_map[normalized_element_id])
    if verification == "unverified":
        raise ValueError(
            f"pagination element is unverified: {normalized_element_id}; recapture it with DeepCapture before binding"
        )
    column_ids = _native_collection_element_ids(node, elements)
    if normalized_element_id in column_ids:
        raise ValueError(
            f"pagination element {normalized_element_id} is already a collection column; "
            "use a separate captured control element"
        )
    current = _collection_paging_settings(
        {
            name: _native_task_argument(node, name, "")
            for name in (
                "region",
                "collect_pages",
                "collect_items",
                "page_type",
                "page_type_num",
                "selector_page",
                "selector_more",
            )
        }
    )
    selected_region = str(region).strip() if region not in (None, "") else current["region"]
    if selected_region not in {"0", "2", "3"}:
        selected_region = "3"
    selector_name = "selector_page" if normalized_mode == "next-page" else "selector_more"
    updates: dict[str, Any] = {
        "region": selected_region,
        "selector_page": normalized_element_id if selector_name == "selector_page" else "",
        "selector_more": normalized_element_id if selector_name == "selector_more" else "",
    }
    if selected_region == "3":
        updates["page_type_num"] = 1 if selector_name == "selector_page" else 2
    else:
        updates["page_type"] = 1 if selector_name == "selector_page" else 2
    # A metadata row target is part of the model's output contract. Preserve it
    # when an older task still contains its template default during a paging bind.
    state = _native_rpa_state(paths, str(project_dir))
    if selected_region == "3" and state.get("max_rows") not in (None, "", 0):
        updates["collect_items"] = state["max_rows"]
    result = update_rpa_step_config(paths, str(project_dir), str(step_id), updates, client=client)
    readback_state = _native_rpa_state(paths, str(project_dir))
    readback_step = next(
        (item for item in readback_state.get("steps", []) if item.get("id") == str(step_id)),
        {},
    )
    readback_config = readback_step.get("config") if isinstance(readback_step, dict) else {}
    readback_config = readback_config if isinstance(readback_config, dict) else {}
    readback_selector = str(readback_config.get(selector_name) or "").strip()
    other_selector = str(readback_config.get("selector_more" if selector_name == "selector_page" else "selector_page") or "").strip()
    _, readback_payload = _native_task_file(project_dir)
    readback_entities = readback_payload.get("componentModelEntities")
    readback_node = readback_entities.get(str(step_id)) if isinstance(readback_entities, dict) else None
    readback_column_ids = (
        _native_collection_element_ids(readback_node, elements)
        if isinstance(readback_node, dict)
        else []
    )
    columns_unchanged = readback_column_ids == column_ids
    referenced_page_elements = set(_NATIVE_ELEMENT_RE.findall(readback_selector))
    _, readback_elements = _element_file(paths, str(project_dir))
    readback_element_map = readback_elements.get("elementMap") if isinstance(readback_elements, dict) else {}
    readback_element_map = readback_element_map if isinstance(readback_element_map, dict) else {}
    page_scope = readback_element_map.get(MCP_PAGE_SCOPE_ELEMENT_ID)
    generated_scope_matches = (
        MCP_PAGE_SCOPE_ELEMENT_ID in referenced_page_elements
        and isinstance(page_scope, dict)
        and str(page_scope.get("mcpPageScopeSource") or "").strip() == normalized_element_id
    )
    if (
        normalized_element_id not in referenced_page_elements
        and not generated_scope_matches
    ) or other_selector:
        raise ValueError(
            "pagination_binding_readback_conflict: "
            + json.dumps(
                {
                    "mode": normalized_mode,
                    "selector_argument": selector_name,
                    "selected": readback_selector,
                    "other_selector": other_selector,
                },
                ensure_ascii=False,
            )
        )
    binding = {
        "mode": normalized_mode,
        "element_id": normalized_element_id,
        "selector_element_id": (
            MCP_PAGE_SCOPE_ELEMENT_ID if generated_scope_matches else normalized_element_id
        ),
        "source": source,
        "verification_status": verification,
        "selector_argument": selector_name,
        "region": selected_region,
        "readback": {
            "selector": readback_selector,
            "other_selector_empty": not other_selector,
            "collection_columns_unchanged": columns_unchanged,
        },
    }
    if not columns_unchanged:
        raise ValueError(
            "pagination_binding_readback_conflict: collection column bindings changed during pagination bind"
        )
    result["pagination_binding"] = binding
    result["pagination_capability"] = readback_config.get("pagination") or _collection_pagination_capability(readback_config)
    return result


def prune_unused_rpa_elements(
    paths: ShizaiPaths,
    flow_ref: str,
    *,
    keep_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Remove element records not referenced by the native task bindings."""
    project_dir = resolve_project(paths, flow_ref)
    task_path, payload = _native_task_file(project_dir)
    element_path, element_payload = _element_file(paths, str(project_dir))
    element_map = element_payload.get("elementMap")
    if not isinstance(element_map, dict):
        raise ValueError("elements.json elementMap must be an object")
    referenced: set[str] = set()
    entities = payload.get("componentModelEntities")
    for node_id in _native_statement_ids(payload):
        node = entities.get(node_id) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        if component == "WebBrowser_DataCollectionV5":
            referenced.update(_native_collection_element_ids(node, element_payload))
            for paging_name in ("selector_page", "selector_more"):
                paging_value = _native_task_argument(node, paging_name, "")
                if isinstance(paging_value, str):
                    referenced.update(_NATIVE_ELEMENT_RE.findall(paging_value))
        else:
            referenced.update(_native_element_ids_from_node(node))
    requested = [str(item).strip() for item in (keep_ids or []) if str(item).strip()]
    missing_requested = [item for item in requested if item not in element_map]
    if missing_requested:
        raise ValueError("requested keep_ids are not in elements.json: " + ", ".join(missing_requested))
    keep = referenced | set(requested)
    removed = [str(item) for item in element_map if str(item) not in keep]
    if not removed:
        validation = validate_rpa_project(paths, str(project_dir))
        return {
            "kind": "native_rpa_elements_pruned",
            "flow_ref": str(project_dir),
            "elements_file": str(element_path),
            "kept": sorted(keep),
            "removed": [],
            "element_count": len(element_map),
            "validation": validation,
            "ui_feedback": _publish_rpa_ui(
                "elements_ready",
                "element_cleanup",
                "元素库已经干净，没有发现未使用元素。",
                animation="element_ready",
                overlay_state="inactive",
                flow_ref=str(project_dir),
                element_count=len(element_map),
            ),
        }
    element_payload["elementMap"] = {str(item): value for item, value in element_map.items() if str(item) in keep}
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(json.dumps(element_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(element_path)
    validation = validate_rpa_project(paths, str(project_dir))
    return {
        "kind": "native_rpa_elements_pruned",
        "flow_ref": str(project_dir),
        "elements_file": str(element_path),
        "kept": sorted(keep),
        "removed": removed,
        "element_count": len(element_payload["elementMap"]),
        "validation": validation,
        "ui_feedback": _publish_rpa_ui(
            "elements_ready" if validation["valid"] else "validation_failed",
            "element_cleanup",
            f"已清理 {len(removed)} 个未被流程引用的历史元素。",
            animation="element_ready" if validation["valid"] else "validation_error",
            overlay_state="inactive",
            flow_ref=str(project_dir),
            element_count=len(element_payload["elementMap"]),
            valid=bool(validation["valid"]),
        ),
    }


def read_rpa_flow(paths: ShizaiPaths, flow_ref: str) -> dict[str, Any]:
    """Read a staged or client-owned native RPA flow without changing it."""
    candidate = Path(flow_ref).expanduser()
    if candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(candidate.resolve())
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            state = _native_rpa_state(paths, str(candidate.resolve()))
            return {"flow_ref": state["flow_ref"], "flow_file": state["flow_file"], "flow": state}
    try:
        resolved = resolve_project(paths, flow_ref)
    except FileNotFoundError:
        resolved = None
    if resolved is not None and not (resolved / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(resolved)
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            state = _native_rpa_state(paths, str(resolved))
            return {"flow_ref": state["flow_ref"], "flow_file": state["flow_file"], "flow": state}
    flow_path, flow = _read_rpa_flow(paths, flow_ref)
    return {"flow_ref": str(flow_path.parent), "flow_file": str(flow_path), "flow": flow}


def mark_rpa_result_capture_ready(
    paths: ShizaiPaths,
    flow_ref: str,
    *,
    captured_fields: Iterable[str] | dict[str, Any],
    field_element_ids: dict[str, Any] | None = None,
    use_js_link: bool | None = None,
    use_js_table: bool | None = None,
    selector_scope: str | None = None,
) -> dict[str, Any]:
    """Persist that the current result-page fields are ready for collection.

    The capture operation happens in the extension bridge, while this state is
    owned by the flow. Keeping the handoff explicit prevents a native flow
    from repeatedly rescanning the result page after a successful import.
    """
    loaded = read_rpa_flow(paths, flow_ref)
    flow = loaded["flow"] if isinstance(loaded.get("flow"), dict) else {}
    effective_js_link = bool(flow.get("use_js_link")) if use_js_link is None else bool(use_js_link)
    effective_js_table = bool(flow.get("use_js_table")) if use_js_table is None else bool(use_js_table)
    if effective_js_table and effective_js_link:
        raise ValueError("use_js_table and use_js_link are mutually exclusive")
    if isinstance(captured_fields, dict):
        raw_fields = [key for key, value in captured_fields.items() if str(value or "").strip()]
    else:
        raw_fields = list(captured_fields)
    normalized_fields = sorted(
        {
            canonical
            for canonical in (_canonical_result_field(item) for item in raw_fields)
            if canonical in _RPA_RESULT_FIELDS
        }
    )
    required = _result_capture_requirements(
        flow.get("required_columns") or DEFAULT_RPA_COLUMNS,
        use_js_link=effective_js_link,
        use_js_table=effective_js_table,
    )
    normalized_ids = {
        _canonical_result_field(key): str(value).strip()
        for key, value in (field_element_ids or {}).items()
        if str(value or "").strip()
        and _canonical_result_field(key) in _RPA_RESULT_FIELDS
    }
    unverified_field_ids = _unverified_element_ids(paths, loaded["flow_ref"], normalized_ids.values())
    ready = required.issubset(set(normalized_fields)) and not unverified_field_ids
    updates = {
        "mcpResultElementsReady": ready,
        "mcpResultFields": normalized_fields,
    }
    if normalized_ids:
        updates["mcpResultFieldElementIds"] = normalized_ids
    if selector_scope:
        updates["mcpResultSelectorScope"] = str(selector_scope).strip()
    if use_js_link is not None:
        updates["mcpUseJsLink"] = effective_js_link
    if use_js_table is not None:
        updates["mcpUseJsTable"] = effective_js_table

    is_native = str(flow.get("kind") or "") == "native-shizai-rpa"
    if is_native:
        project_dir = resolve_project(paths, loaded["flow_ref"])
        task_path, payload = _native_task_file(project_dir)
        _update_native_metadata(
            project_dir,
            len(_native_statement_ids(payload)),
            updates,
        )
        current = _native_rpa_state(paths, str(project_dir))
    else:
        flow["result_elements_ready"] = ready
        flow["result_fields"] = normalized_fields
        if normalized_ids:
            flow["result_field_element_ids"] = normalized_ids
        if selector_scope:
            flow["result_selector_scope"] = str(selector_scope).strip()
        if use_js_link is not None:
            flow["use_js_link"] = effective_js_link
        if use_js_table is not None:
            flow["use_js_table"] = effective_js_table
        if ready:
            flow["status"] = "result_elements_captured"
            flow["phase"] = "result_elements_ready"
        next_action = _rpa_next_action({**flow, "flow_ref": loaded["flow_ref"]})
        flow["next_tool"] = (
            next_action.get("tool")
            or (next_action.get("tools") or [None])[0]
            or next_action.get("then")
            or "shizai_rpa_validate_flow"
        )
        _write_rpa_flow(Path(loaded["flow_file"]), flow)
        current = flow

    next_action = _rpa_next_action(current)
    next_tool = (
        next_action.get("tool")
        or (next_action.get("tools") or [None])[0]
        or next_action.get("then")
        or "shizai_rpa_validate_flow"
    )
    ui_feedback = _publish_rpa_ui(
        "elements_ready" if ready else "waiting",
        "result_field_state",
        "结果字段已导入并记录，可以继续添加数据采集组件。"
        if ready
        else "结果字段尚未满足采集契约，请继续捕获缺失字段。",
        animation="element_ready" if ready else "capture_needs_review",
        overlay_state="inactive" if ready else "active",
        flow_ref=loaded["flow_ref"],
        fields=normalized_fields,
        required_fields=sorted(required),
        result_elements_ready=ready,
        next_tool=next_tool,
    )
    return {
        "kind": "rpa_result_capture_state",
        "flow_ref": loaded["flow_ref"],
        "result_elements_ready": ready,
        "use_js_link": effective_js_link,
        "use_js_table": effective_js_table,
        "captured_fields": normalized_fields,
        "required_fields": sorted(required),
        "field_element_ids": normalized_ids,
        "unverified_element_ids": unverified_field_ids,
        "verification_blocked": bool(unverified_field_ids),
        "selector_scope": selector_scope,
        "next_tool": next_tool,
        "next_action": next_action,
        "ui_feedback": ui_feedback,
    }


def validate_rpa_flow(
    paths: ShizaiPaths, flow_ref: str, *, strict: bool = False, publish_ui: bool = True
) -> dict[str, Any]:
    """Validate component order and element bindings for staged or native flows."""
    # Read-only previews reuse this validator but must not drive the designer
    # overlay; only committed writes publish progress.
    if publish_ui:
        _publish_rpa_ui(
            "validating",
            "flow_validation",
            "正在校验流程顺序、元素绑定、输出列和变量交接。",
            animation="flow_validation",
            flow_ref=str(flow_ref),
            strict=bool(strict),
        )
    native_flow = False
    candidate = Path(flow_ref).expanduser()
    if candidate.is_dir() and not (candidate / RPA_FLOW_FILE).is_file():
        try:
            _native_task_file(candidate.resolve())
        except (FileNotFoundError, OSError, ValueError):
            pass
        else:
            native_flow = True
            flow = _native_rpa_state(paths, str(candidate.resolve()))
            flow_path = Path(flow["flow_file"])
    if not native_flow:
        try:
            resolved = resolve_project(paths, flow_ref)
        except FileNotFoundError:
            resolved = None
        if resolved is not None and not (resolved / RPA_FLOW_FILE).is_file():
            try:
                _native_task_file(resolved)
            except (FileNotFoundError, OSError, ValueError):
                pass
            else:
                native_flow = True
                flow = _native_rpa_state(paths, str(resolved))
                flow_path = Path(flow["flow_file"])
    if not native_flow:
        flow_path, flow = _read_rpa_flow(paths, flow_ref)
    errors: list[str] = []
    warnings: list[str] = []
    steps = flow["steps"]
    component_plan, plan_source = _rpa_component_plan(flow)
    plan_backed = isinstance(component_plan, list) and bool(component_plan)
    model_owned = _rpa_model_decides(flow)
    request_analysis = flow.get("request_analysis") or flow.get("mcpRequestAnalysis") or {}
    browser_plan = isinstance(request_analysis, dict) and request_analysis.get("intent") == "browser_rpa"
    if not steps:
        errors.append("flow has no components; write the first validated plan step")
    elif browser_plan and not model_owned and steps[0].get("component") != "WebBrowser_CreateV3":
        errors.append("the first component must be WebBrowser_CreateV3")
    actual_components = [str(step.get("component") or "") for step in steps if isinstance(step, dict)]
    collection_count = actual_components.count("WebBrowser_DataCollectionV5")
    collection_steps = [
        step
        for step in steps
        if isinstance(step, dict) and str(step.get("component") or "") == "WebBrowser_DataCollectionV5"
    ]
    collection_stages = [_collection_stage_value(step) for step in collection_steps]
    explicitly_staged_collections = bool(
        collection_count > 1
        and all(collection_stages)
        and len(set(collection_stages)) == len(collection_stages)
    )
    if collection_count > 1 and not explicitly_staged_collections:
        errors.append(
            "designer_state_conflict: flow contains multiple WebBrowser_DataCollectionV5 steps; keep one final collection step"
        )
    first_collection = next(
        (index for index, component in enumerate(actual_components) if component == "WebBrowser_DataCollectionV5"),
        None,
    )
    if first_collection is not None:
        first_excel = next(
            (index for index, component in enumerate(actual_components) if component.startswith("Excel_")),
            None,
        )
        if first_excel is not None and first_excel < first_collection:
            errors.append(
                "designer_state_conflict: data collection must not be appended after Excel steps"
            )
    expected_sequence = _rpa_sequence(flow)
    plan_complete = bool(
        flow.get("plan_complete")
        or flow.get("mcpPlanComplete")
        or flow.get("plan_id")
        or flow.get("mcpPlanId")
        or flow.get("mcpExecutionPlan")
    )
    if actual_components and (not model_owned or plan_complete) and actual_components != expected_sequence[: len(actual_components)]:
        errors.append("RPA components must be added in order: " + " -> ".join(expected_sequence))
    step_ids: set[str] = set()
    try:
        _, native = _element_file(paths, flow_ref)
        available_elements = set(native["elementMap"])
        errors.extend(_native_element_parent_errors(native))
    except (FileNotFoundError, OSError, ValueError) as exc:
        available_elements = set()
        errors.append(f"unable to read native elements: {exc}")
    seen_collection = False
    seen_js_table = False
    bound_ids: set[str] = set()
    unverified_ids: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        current_id = str(step.get("id") or "").strip()
        component = str(step.get("component") or "").strip()
        if not current_id:
            errors.append(f"steps[{index}] is missing id")
        elif current_id in step_ids:
            errors.append(f"duplicate step id: {current_id}")
        else:
            step_ids.add(current_id)
        contract = _live_rpa_contract(paths, component)
        if contract is None:
            errors.append(f"steps[{index}] uses unsupported component: {component}")
            continue
        ids = (
            [str(item).strip() for item in step.get("element_ids", []) if str(item).strip()]
            if isinstance(step.get("element_ids"), list)
            else []
        )
        bound_ids.update(ids)
        unverified_ids.update(_unverified_element_ids(paths, flow_ref, ids))
        if contract["requires_elements"] and not ids:
            errors.append(f"steps[{index}] {component} has no element binding")
        missing = [item for item in ids if item not in available_elements]
        if missing:
            errors.append(f"steps[{index}] references unimported elements: {', '.join(missing)}")
        if ids and isinstance(native, dict):
            identities = {
                _selector_page_identity(native.get("elementMap", {}).get(element_id, {}))
                for element_id in ids
                if isinstance(native.get("elementMap", {}).get(element_id), dict)
            }
            identities = {identity for identity in identities if identity[0] or identity[1]}
            page_urls = sorted({identity[1] for identity in identities if identity[1]})
            page_titles = sorted({identity[0] for identity in identities if identity[0]})
            if len(page_urls) > 1 or len(page_titles) > 1:
                warnings.append(
                    f"steps[{index}] {component} has mixed page identity evidence; "
                    "recapture all bound elements from the same current page after navigation"
                )
        if component == "WebBrowser_DataCollectionV5":
            seen_collection = True
            step_config = step.get("config") if isinstance(step.get("config"), dict) else {}
            columns = step_config.get("columns")
            stage_columns = columns if plan_backed and _collection_stage_value(step) else flow.get("required_columns", [])
            stage_columns = stage_columns if isinstance(stage_columns, list) else []
            if not isinstance(columns, list) or not columns:
                errors.append("WebBrowser_DataCollectionV5 must declare output columns")
            elif len(columns) < len(stage_columns):
                warnings.append("data collection declares fewer columns than the requested output")
            elif any(
                str(required) not in {str(column) for column in columns}
                for required in stage_columns
            ):
                errors.append("WebBrowser_DataCollectionV5 columns must include the requested output fields")
        if component == "WebBrowser_RunJsInIeBySelector_v3" and bool(flow.get("use_js_table")):
            seen_js_table = True
    if not plan_backed and flow.get("use_js_table"):
        if flow.get("use_js_link"):
            errors.append("use_js_table and use_js_link are mutually exclusive")
        if not seen_js_table:
            errors.append("use_js_table flow has no direct JS table output step")
        if seen_collection:
            errors.append("use_js_table flow must not add WebBrowser_DataCollectionV5")
    elif not plan_backed and not seen_collection:
        errors.append("flow has no WebBrowser_DataCollectionV5 output step")
    if not model_owned and not plan_backed and flow.get("excel_mode") and actual_components != expected_sequence:
        errors.append("Excel demo mode requires Excel_Cs_NewV4 followed by Excel_Cs_WriteRange after data collection")
    if (
        not plan_backed
        and flow.get("use_js_link")
        and any(
            component in actual_components
            for component in {"WebBrowser_RunJsInIeBySelector_v3", "WebBrowser_DataCollectionV5"}
        )
    ):
        if not flow.get("result_elements_ready"):
            errors.append("use_js_link flow must persist a complete result-page field capture before collection")
        collection_step = next(
            (
                item
                for item in steps
                if isinstance(item, dict) and item.get("component") == "WebBrowser_DataCollectionV5"
            ),
            None,
        )
        collection_ids = collection_step.get("element_ids", []) if isinstance(collection_step, dict) else []
        if JS_LINK_ELEMENT_ID not in collection_ids:
            errors.append(f"WebBrowser_DataCollectionV5 must bind generated element {JS_LINK_ELEMENT_ID}")
        collection_columns = (
            collection_step.get("config", {}).get("columns", [])
            if isinstance(collection_step, dict) and isinstance(collection_step.get("config"), dict)
            else []
        )
        if any(_canonical_result_field(item) == "merchant" for item in collection_columns):
            if JS_MERCHANT_ELEMENT_ID not in collection_ids:
                errors.append(f"WebBrowser_DataCollectionV5 must bind generated element {JS_MERCHANT_ELEMENT_ID}")
    step_validations: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict) or not str(step.get("id") or "").strip():
            continue
        validation = validate_rpa_step(paths, str(flow_path.parent), str(step["id"]), strict=strict)
        step_validations.append(validation)
        errors.extend(f"step {step['id']}: {detail}" for detail in validation["errors"] if detail not in errors)
        warnings.extend(f"step {step['id']}: {detail}" for detail in validation["warnings"] if detail not in warnings)
    if seen_collection and len(bound_ids) < len(flow.get("required_columns", [])) and not flow.get("use_js_table"):
        warnings.append("fewer bound elements than requested output columns; confirm list/field selectors")
    if unverified_ids:
        errors.append(
            "flow references unverified elements: "
            + ", ".join(sorted(unverified_ids))
            + "; recapture them with DeepCapture before running"
        )
    valid = not errors and (not strict or not warnings)
    ui_feedback = (
        _publish_rpa_ui(
            "flow_ready" if valid else "validation_failed",
            "flow_validation",
            "整条流程校验通过，可以继续物化和运行。" if valid else "整条流程校验未通过，请根据 errors 修复后重试。",
            animation="flow_complete" if valid else "validation_error",
            flow_ref=str(flow_path.parent),
            step_count=len(steps),
            valid=valid,
            next_tool=(
                "shizai_rpa_validate_project"
                if valid and native_flow
                else ("shizai_rpa_validate_flow" if not valid else None)
            ),
        )
        if publish_ui
        else None
    )
    return {
        "flow_ref": str(flow_path.parent),
        "flow_file": str(flow_path),
        "plan_source": plan_source,
        "valid": valid,
        "strict": strict,
        "errors": errors,
        "warnings": warnings,
        "step_count": len(steps),
        "bound_element_count": len(bound_ids),
        "unverified_element_ids": sorted(unverified_ids),
        "unverified_element_count": len(unverified_ids),
        "required_columns": flow.get("required_columns", []),
        "use_js_link": bool(flow.get("use_js_link")),
        "use_js_table": bool(flow.get("use_js_table")),
        "result_elements_ready": bool(flow.get("result_elements_ready")),
        "expected_sequence": expected_sequence,
        "phase": "validated" if valid else "validation_failed",
        "step_validations": step_validations,
        "ui_feedback": ui_feedback,
        "next_tool": (
            "shizai_rpa_validate_project"
            if valid and native_flow
            else ("shizai_rpa_validate_flow" if not valid else None)
        ),
        "next_action": {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_validate_project",
            "reason": "客户端原生流程已通过校验，可继续执行项目校验。",
        }
        if valid and native_flow
        else {
            "kind": "manual_migration",
            "reason": "该暂存格式只保留读取兼容；请在设计器中迁移为客户端原生应用。",
        }
        if valid
        else {
            "kind": "repair",
            "tool": "shizai_rpa_validate_flow",
            "reason": "先修复 errors，再重新校验；不要直接生成 native 流程。",
        },
    }


def validate_rpa_step(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Validate one component after it is written, without executing it.

    This is deliberately a static contract check.  A designer debug run is a
    separate operation because the current desktop RPC exposes whole-flow
    execution, while this check must remain deterministic and side-effect free.
    """
    loaded = read_rpa_flow(paths, flow_ref)
    flow = loaded["flow"]
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    normalized_id = str(step_id or "").strip()
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    pagination_capability: dict[str, Any] | None = None

    def check(name: str, passed: bool, detail: str, *, warning: bool = False) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail, "severity": "warning" if warning else "error"})
        if passed:
            return
        (warnings if warning else errors).append(detail)

    index = next(
        (
            position
            for position, item in enumerate(steps)
            if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_id
        ),
        None,
    )
    if index is None:
        check("step_exists", False, f"RPA step not found: {normalized_id}")
        failed_validation = {"valid": False, "step_id": normalized_id, "component": ""}
        return {
            "kind": "rpa_step_validation",
            "flow_ref": loaded["flow_ref"],
            "flow_file": loaded["flow_file"],
            "step_id": normalized_id,
            "valid": False,
            "strict": strict,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
            "runtime_check": {"performed": False, "mode": "static_contract"},
            "verification_gate": _rpa_verification_gate(
                loaded["flow_ref"], None, failed_validation
            ),
            "next_action": {"kind": "repair", "tool": "shizai_rpa_read_flow"},
        }

    step = steps[index]
    component = str(step.get("component") or "").strip()
    contract = _live_rpa_contract(paths, component)
    check("step_exists", True, f"step {normalized_id} exists")
    check("component_supported", contract is not None, f"component is supported: {component}")
    if contract is None:
        failed_validation = {"valid": False, "step_id": normalized_id, "component": component}
        return {
            "kind": "rpa_step_validation",
            "flow_ref": loaded["flow_ref"],
            "flow_file": loaded["flow_file"],
            "step_id": normalized_id,
            "component": component,
            "valid": False,
            "strict": strict,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
            "runtime_check": {"performed": False, "mode": "static_contract"},
            "verification_gate": _rpa_verification_gate(
                loaded["flow_ref"], step, failed_validation
            ),
            "next_action": {"kind": "repair", "tool": "shizai_rpa_components"},
        }

    expected_sequence = _rpa_sequence(flow)
    expected_component = expected_sequence[index] if index < len(expected_sequence) else None
    model_owned = _rpa_model_decides(flow)
    plan_complete = bool(
        flow.get("plan_complete")
        or flow.get("mcpPlanComplete")
        or flow.get("plan_id")
        or flow.get("mcpPlanId")
        or flow.get("mcpExecutionPlan")
    )
    check(
        "component_order",
        (model_owned and not plan_complete) or component == expected_component,
        (
            "component position is selected by the client model"
            if model_owned
            else f"step {index + 1} must be {expected_component}; found {component}"
        ),
    )
    if index == 0:
        request_analysis = flow.get("request_analysis") or flow.get("mcpRequestAnalysis") or {}
        if (
            isinstance(request_analysis, dict)
            and request_analysis.get("intent") == "browser_rpa"
            and not model_owned
        ):
            check(
                "browser_open_first",
                component == "WebBrowser_CreateV3",
                "browser RPA must open Chrome with WebBrowser_CreateV3 first",
            )
    else:
        check("predecessor_exists", bool(steps[index - 1]), "the preceding component is present")

    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    component_plan, plan_source = _rpa_component_plan(flow)
    planned_step = (
        component_plan[index]
        if isinstance(component_plan, list) and index < len(component_plan) and isinstance(component_plan[index], dict)
        else {}
    )
    planned_config = planned_step.get("config") if isinstance(planned_step.get("config"), dict) else {}
    plan_backed = bool(
        planned_step
        and (
            flow.get("plan_complete")
            or flow.get("mcpPlanComplete")
            or flow.get("plan_id")
            or flow.get("mcpPlanId")
            or flow.get("mcpExecutionPlan")
        )
    )
    native_node: dict[str, Any] | None = None
    native_payload: dict[str, Any] | None = None
    if flow.get("kind") == "native-shizai-rpa":
        try:
            task_path, payload = _native_task_file(Path(loaded["flow_ref"]))
            native_payload = payload if isinstance(payload, dict) else None
            entities = payload.get("componentModelEntities")
            candidate = entities.get(normalized_id) if isinstance(entities, dict) else None
            native_node = candidate if isinstance(candidate, dict) else None
            check("native_node", native_node is not None, f"native task contains node {normalized_id}")
        except (FileNotFoundError, OSError, ValueError) as exc:
            check("native_node", False, f"native task cannot be read: {exc}")

    native_args = _task_argument_map(native_node) if native_node else {}
    accepted_web_page_names = _accepted_web_page_object_names(native_payload, index, planned_step)

    def argument(name: str, default: Any = "") -> Any:
        if native_node is not None:
            return native_args.get(name, default)
        if name in config:
            return config[name]
        if name == "var_name":
            if component == "WebBrowser_RunJsInIeBySelector_v3":
                planned_output = str(planned_config.get("output_variable") or "").strip()
                if planned_output:
                    return planned_output
                if flow.get("use_js_table"):
                    return JS_TABLE_OUTPUT_VARIABLE
            known_outputs = RPA_STEP_OUTPUT_VARIABLES.get(component, ())
            return known_outputs[0] if known_outputs else default
        if name == "var0":
            return "数据采集结果"
        if name == "var1":
            return "Excel路径"
        if name == "var2":
            # DataCollectionV5 keeps this output slot in staged flows too,
            # including when monitor_data is disabled.
            return "监控空列数据"
        if name == "type":
            return "chrome"
        if name == "url":
            return flow.get("target_url", "")
        if name == "text":
            return flow.get("keyword", "")
        if name == "words":
            return flow.get("keyword", "")
        if name == "uiSelector":
            return "bound-element-selector" if step.get("element_ids") else ""
        if name in {"designated_obj", "workbook", "contents"}:
            return {"designated_obj": "网页对象", "workbook": "Excel文件", "contents": "数据采集结果"}[name]
        if name == "selector_page":
            return "bound-element-selector" if step.get("element_ids") else ""
        if name == "selector_more":
            return ""
        if name == "region":
            return config.get("region", "1")
        if name == "collect_pages":
            return config.get("collect_pages", "1")
        if name == "collect_items":
            return config.get("collect_items", "20")
        if name == "page_type":
            return config.get("page_type", "1")
        if name == "page_type_num":
            return config.get("page_type_num", "0")
        if name == "config":
            return config.get("columns")
        if name == "path":
            return config.get("path", "res")
        if name == "name":
            return config.get("name", flow.get("output_file", "result.xlsx"))
        if name == "sheet":
            return config.get("sheet", "Sheet1")
        if name == "row":
            return config.get("row", "1")
        if name == "column":
            return config.get("column", "A")
        return default

    def present(value: Any) -> bool:
        if isinstance(value, (list, dict)):
            return bool(value)
        return bool(_decode_selector_value(value))

    required_arguments = RPA_STEP_REQUIRED_ARGUMENTS.get(
        component,
        tuple(contract.get("required_arguments") or []),
    )
    missing_arguments = [name for name in required_arguments if not present(argument(name))]
    check(
        "required_arguments",
        not missing_arguments,
        f"required arguments are populated: {', '.join(missing_arguments) or 'yes'}",
    )

    element_ids = (
        [str(item).strip() for item in step.get("element_ids", []) if str(item).strip()]
        if isinstance(step.get("element_ids"), list)
        else []
    )
    if native_node and component == "WebBrowser_DataCollectionV5":
        try:
            _, native_elements = _element_file(paths, loaded["flow_ref"])
            element_ids = _native_collection_element_ids(native_node, native_elements)
        except (FileNotFoundError, OSError, ValueError):
            pass
    if contract["requires_elements"]:
        check("element_binding", bool(element_ids), f"bound element count: {len(element_ids)}")
        try:
            _, native_elements = _element_file(paths, loaded["flow_ref"])
            available = set(native_elements["elementMap"])
            missing = [item for item in element_ids if item not in available]
            check("imported_elements", not missing, f"unimported element ids: {', '.join(missing) or 'none'}")
            unverified = [
                item
                for item in element_ids
                if _element_verification_state(native_elements["elementMap"].get(item))[1] == "unverified"
            ]
            check(
                "verified_elements",
                not unverified,
                "unverified element ids: "
                + (", ".join(unverified) if unverified else "none")
                + ("; recapture with DeepCapture before running" if unverified else ""),
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            check("imported_elements", False, f"element catalog cannot be read: {exc}")

    output_variables = list(RPA_STEP_OUTPUT_VARIABLES.get(component, ()))
    if not output_variables and contract.get("output_parameters"):
        output_variables = [
            str(argument(name) or "").strip()
            for name in contract.get("output_parameters") or []
            if str(argument(name) or "").strip()
        ]
    if plan_backed:
        output_variables = [str(item).strip() for item in planned_step.get("outputs", []) if str(item).strip()]
    if component == "WebBrowser_RunJsInIeBySelector_v3":
        if plan_backed:
            planned_mode, _ = _normalize_js_result_mode(planned_config.get("result_mode"))
            if planned_mode == "table":
                output_variables = [
                    str(planned_config.get("output_variable") or JS_TABLE_OUTPUT_VARIABLE).strip()
                ]
            else:
                output_variables = [str(item).strip() for item in planned_step.get("outputs", []) if str(item).strip()]
        elif bool(flow.get("use_js_table")):
            output_variables = [JS_TABLE_OUTPUT_VARIABLE]
    output_argument_names = {
        "WebBrowser_CreateV3": ("var_name",),
        "WebBrowser_GetTAB_V1": ("var_name",),
        "Element_SetValue_V6": ("var_name",),
        "WebBrowser_RunJsInIeBySelector_v3": ("var_name",),
        "WebBrowser_DataCollectionV5": ("var0", "var1", "var2"),
        "Excel_Cs_NewV4": ("var_name",),
    }.get(component, tuple(contract.get("output_parameters") or []))
    actual_outputs = [str(argument(name)).strip() for name in output_argument_names if present(argument(name))]
    output_detail = f"expected outputs {output_variables}; found {actual_outputs}"
    if output_variables and len(actual_outputs) > len(output_variables):
        output_detail += f"; surplus native slots: {', '.join(actual_outputs[len(output_variables):])}"
    check(
        "output_variables",
        _output_variables_match(component, output_variables, actual_outputs),
        output_detail,
    ) if output_variables else check("output_variables", not actual_outputs, "component has no output variables")

    if component == "WebBrowser_CreateV3":
        check("chrome_target", _decode_selector_value(argument("type")).lower() == "chrome", "browser type is chrome")
        check("target_url", present(argument("url")), "target URL is populated")
    elif component == "WebBrowser_RunJsInIeBySelector_v3":
        planned_result_mode, _ = _normalize_js_result_mode(planned_config.get("result_mode"))
        js_table = planned_result_mode == "table" if plan_backed else bool(flow.get("use_js_table"))
        extraction_label = "JS table extraction" if js_table else "JS link extraction"
        expected_output_variable = (
            str(planned_config.get("output_variable") or "").strip()
            if plan_backed
            else JS_TABLE_OUTPUT_VARIABLE
            if js_table
            else "商品链接"
        )
        check(
            "js_output_variable",
            _decode_selector_value(argument("var_name")) == expected_output_variable,
            f"{extraction_label} output is {expected_output_variable}",
        )
        check(
            "js_web_page_binding",
            _decode_selector_value(argument("web_page")) in accepted_web_page_names,
            f"{extraction_label} uses the upstream browser object; accepted names: "
            + ", ".join(accepted_web_page_names),
        )
        check(
            "js_execution_environment",
            _decode_selector_value(argument("executionEnvironment")).upper() == "MAIN",
            f"{extraction_label} runs in the page MAIN environment",
        )
        script = str(argument("jsCode") or "")
        if js_table:
            requested_columns = [
                str(item).strip()
                for item in planned_config.get("output_columns", flow.get("required_columns", []))
                if str(item).strip()
            ]
            check(
                "js_table_script",
                bool(script.strip())
                and bool(requested_columns)
                and all(header in script for header in requested_columns)
                and "return" in script
                and "SHIZAI_WAITING_FOR_USER:login_required" in script
                and "SHIZAI_WAITING_FOR_USER:page_challenge" in script,
                f"JS code declares the planned table columns: {requested_columns}",
            )
        elif plan_backed:
            check(
                "js_request_script",
                bool(script.strip())
                and "SHIZAI_WAITING_FOR_USER:login_required" in script
                and "SHIZAI_WAITING_FOR_USER:page_challenge" in script,
                "planned JS code is explicit and preserves login/challenge pause markers",
            )
        else:
            check(
                "js_product_link_script",
                JS_LINK_MARKER in script and JS_MERCHANT_MARKER in script and "item.jd.com" in script,
                "JS code creates marked product-detail links and merchant text from product cards",
            )
            try:
                _, native_elements = _element_file(paths, loaded["flow_ref"])
                generated_exists = (
                    isinstance(native_elements.get("elementMap"), dict)
                    and JS_LINK_ELEMENT_ID in native_elements["elementMap"]
                )
                check(
                    "generated_link_element",
                    generated_exists,
                    f"generated element {JS_LINK_ELEMENT_ID} is registered",
                )
                generated_merchant_exists = (
                    isinstance(native_elements.get("elementMap"), dict)
                    and JS_MERCHANT_ELEMENT_ID in native_elements["elementMap"]
                )
                check(
                    "generated_merchant_element",
                    generated_merchant_exists,
                    f"generated element {JS_MERCHANT_ELEMENT_ID} is registered",
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                check("generated_link_element", False, f"generated link element cannot be read: {exc}")
    elif component == "WebBrowser_DataCollectionV5":
        check(
            "collection_web_page_binding",
            _decode_selector_value(argument("designated_obj")) in accepted_web_page_names,
            "DataCollectionV5 uses the upstream browser object; accepted names: "
            + ", ".join(accepted_web_page_names),
        )
        paging = _collection_paging_settings(
            {
                name: argument(name)
                for name in (
                    "region",
                    "collect_pages",
                    "collect_items",
                    "page_type",
                    "page_type_num",
                    "selector_page",
                    "selector_more",
                )
            }
        )
        pagination_capability = _collection_pagination_capability(paging)
        check(
            "pagination_selector_exclusivity",
            pagination_capability["configuration_valid"],
            "native pagination must bind at most one of selector_page and selector_more",
        )
        region = paging["region"]
        page_type = paging["page_type"]
        page_type_num = paging["page_type_num"]
        check(
            "collection_region",
            region in {"0", "1", "2", "3"},
            f"collection region is supported: {region}",
        )
        if region == "1":
            check(
                "single_page_no_pagination",
                not paging["selector_page"] and not paging["selector_more"],
                "single-page collection must leave selector_page and selector_more empty",
            )
        elif region in {"0", "2"}:
            check(
                "pagination_control",
                bool(paging["selector_page"]) if page_type == "1" else bool(paging["selector_more"]),
                "paginated collection must bind the configured next-page or load-more element",
            )
        elif region == "3":
            if page_type_num == "1":
                check(
                    "pagination_control",
                    bool(paging["selector_page"]),
                    "count-limited collection using next-page mode must bind selector_page",
                )
            elif page_type_num == "2":
                check(
                    "pagination_control",
                    bool(paging["selector_more"]),
                    "count-limited collection using load-more mode must bind selector_more",
                )
            else:
                check(
                    "pagination_control",
                    page_type_num == "0",
                    f"count-limited collection page_type_num must be 0, 1, or 2; found {page_type_num}",
                )
        columns = config.get("columns") if isinstance(config.get("columns"), list) else []
        collection: dict[str, Any] = {}
        column_configs: list[dict[str, Any]] = []
        if native_node:
            try:
                parsed = json.loads(argument("config"))
                collection = parsed.get("collectionConfig") if isinstance(parsed, dict) else {}
                column_configs = [item for item in collection.get("columnConfigArray", []) if isinstance(item, dict)]
                columns = [item.get("columnHeader") for item in column_configs if item.get("columnHeader")]
            except (TypeError, json.JSONDecodeError):
                columns = []
        required_columns = [str(item) for item in flow.get("required_columns", DEFAULT_RPA_COLUMNS)]
        if plan_backed and component == "WebBrowser_DataCollectionV5" and _collection_stage_value(step):
            stage_columns = config.get("columns") if isinstance(config.get("columns"), list) else []
            if stage_columns:
                required_columns = [str(item) for item in stage_columns if str(item).strip()]
        check("output_columns", bool(columns), "data collection output columns are declared")
        check(
            "column_binding",
            len(columns) == len(element_ids),
            f"column count {len(columns)} matches element count {len(element_ids)}",
        )
        check(
            "required_columns",
            all(item in columns for item in required_columns),
            f"requested output columns are present: {', '.join(required_columns)}",
        )
        if column_configs:
            link_indexes = [
                index
                for index, header in enumerate(columns)
                if str(header) in {"商品链接", "链接", "网址", "URL", "url"}
            ]
            for index in link_indexes:
                attr = str(column_configs[index].get("dataAttrName") or "").strip()
                check(
                    "link_attribute",
                    attr == "href",
                    f"link column must extract href; found {attr or 'empty'}",
                )
            try:
                _, native_elements = _element_file(paths, loaded["flow_ref"])
                binding_issues = _collection_binding_diagnostics(native_elements, element_ids, columns)
            except (FileNotFoundError, OSError, ValueError):
                binding_issues = []
            for issue in binding_issues:
                check(
                    str(issue.get("check") or "collection_binding"),
                    False,
                    str(issue.get("message") or "invalid collection binding"),
                )
            for issue in _collection_runtime_capability_diagnostics(collection, columns):
                check(
                    str(issue.get("check") or "collection_runtime_capability"),
                    False,
                    str(issue.get("message") or "unsupported collection configuration"),
                )
        to_excel = _decode_selector_value(argument("toExcel", config.get("toExcel", 1)))
        check(
            "excel_handoff_mode",
            not flow.get("excel_mode") or to_excel == "0",
            "explicit Excel mode requires DataCollectionV5.toExcel=0",
        )
        generated_fields_bound = any(
            element_id in {
                JS_NAME_ELEMENT_ID,
                JS_MERCHANT_ELEMENT_ID,
                JS_PRICE_ELEMENT_ID,
                JS_SALES_ELEMENT_ID,
                JS_LINK_ELEMENT_ID,
            }
            for element_id in element_ids
        )
        if flow.get("use_js_link") and generated_fields_bound:
            has_js_step = any(
                isinstance(item, dict) and item.get("component") == "WebBrowser_RunJsInIeBySelector_v3"
                for item in steps
            )
            check(
                "js_link_step",
                has_js_step,
                "use_js_link flow contains WebBrowser_RunJsInIeBySelector_v3 before collection",
            )
    elif component == "Excel_Cs_NewV4":
        check(
            "excel_output_name",
            Path(_decode_selector_value(argument("name"))).suffix.lower() in {".xls", ".xlsx", ".xlsm"},
            "Excel output filename has a supported extension",
        )
    elif component == "Keyboard_PressNew":
        check("press_key", _decode_selector_value(argument("key", "13")) == "13", "Keyboard_PressNew key is Enter (13)")
        check(
            "press_type",
            _decode_selector_value(argument("type", "press")) == "press",
            "Keyboard_PressNew type is press",
        )
    elif component == "Excel_Cs_WriteRange":
        check(
            "excel_workbook_binding",
            _decode_selector_value(argument("workbook")) == "Excel文件",
            "workbook is bound to Excel_Cs_NewV4 output Excel文件",
        )
        check(
            "excel_sheet_exists",
            _decode_selector_value(argument("sheet", "Sheet1")) == "Sheet1",
            "Excel_Cs_NewV4 creates Sheet1; writing another name requires an explicit sheet-creation component",
        )
        producer = (
            "WebBrowser_RunJsInIeBySelector_v3" if bool(flow.get("use_js_table")) else "WebBrowser_DataCollectionV5"
        )
        check(
            "collection_binding",
            _decode_selector_value(argument("contents")) == "数据采集结果",
            f"contents is bound to {producer} output 数据采集结果",
        )

    valid = not errors and (not strict or not warnings)
    next_component = expected_sequence[index + 1] if index + 1 < len(expected_sequence) else None
    next_action: dict[str, Any]
    if not valid:
        next_action = {
            "kind": "repair",
            "tool": "shizai_rpa_validate_step",
            "reason": "先修复当前组件的静态契约问题，再继续添加后续组件。",
        }
    elif next_component:
        next_action = {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_add_step",
            "arguments": {"flow_ref": loaded["flow_ref"], "component": next_component, "allow_write": True},
            "reason": f"当前步骤已通过静态校验，下一步是 {next_component}。",
        }
    else:
        next_action = {
            "kind": "mcp_tool",
            "tool": "shizai_rpa_validate_flow",
            "arguments": {"flow_ref": loaded["flow_ref"], "strict": strict},
            "reason": "当前步骤已通过静态校验，流程组件已完成，继续做整条流程校验。",
        }
    return {
        "kind": "rpa_step_validation",
        "flow_ref": loaded["flow_ref"],
        "flow_file": loaded["flow_file"],
        "plan_source": plan_source,
        "step_id": normalized_id,
        "step_index": index,
        "component": component,
        "valid": valid,
        "strict": strict,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "output_variables": output_variables,
        "pagination_capability": pagination_capability,
        "runtime_check": {
            "performed": False,
            "mode": "static_contract",
            "message": "已检查组件、顺序、参数、元素绑定和输出变量；静态通过后仍须用设计态单步和页面/运行证据确认当前组件。",
        },
        "verification_gate": _rpa_verification_gate(
            loaded["flow_ref"],
            step,
            {"valid": valid, "step_id": normalized_id, "component": component},
        ),
        "next_action": next_action,
    }


def preview_rpa_collection(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    *,
    client: Any | None = None,
    sample_limit: int = 4,
) -> dict[str, Any]:
    """Run DataCollectionV5's native preview against the current browser page."""
    project_dir = resolve_project(paths, flow_ref)
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    node = entities.get(str(step_id)) if isinstance(entities, dict) else None
    if not isinstance(node, dict):
        raise ValueError(f"RPA step not found: {step_id}")
    if node.get("type") != "WebBrowser_DataCollectionV5":
        raise ValueError(f"RPA step is not WebBrowser_DataCollectionV5: {step_id}")

    config = _native_collection_config(node)
    page_info = config.get("pageInfo") if isinstance(config.get("pageInfo"), dict) else {}
    collection = config.get("collectionConfig") if isinstance(config.get("collectionConfig"), dict) else {}
    preview_collection = deepcopy(collection)
    for key, default in (
        ("tableSelector", []),
        ("columnSelectorArray", []),
        ("columnConfigArray", []),
        ("mergeConfigMap", {}),
        ("commonSelector", []),
        ("scrollVerticalDelay", -1),
        ("scrollMaxTotalDelay", 0),
        ("selectorVersion", "1.4"),
        ("scrollType", 0),
    ):
        preview_collection.setdefault(key, deepcopy(default))
    preview_params = {
        "dataCollectConfig": {
            "turningPageConfig": {},
            "pageInfo": deepcopy(page_info),
            "collectionConfig": preview_collection,
        },
        "colorGuidanceConfig": {
            "actionCmd": "preview",
            "highlight": 0,
            "rows": [],
            "columns": [],
        },
        "dataFilePath": tempfile.gettempdir(),
        "collectDataCount": 0,
        "turnPageCount": "1",
        "webData": "",
    }

    caller = client or ShizaiDesktopAgent()
    response = caller.call(
        "DataCollectService",
        "dataCollectCapturePreview",
        [{"jsonParams": json.dumps(preview_params, ensure_ascii=False, separators=(",", ":"))}],
    )
    rows = response.get("data") if isinstance(response, dict) and "data" in response else response
    rows = rows if isinstance(rows, list) else []
    normalized_rows = [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
    headers = normalized_rows[0] if normalized_rows else [
        str(item.get("columnHeader") or "")
        for item in preview_collection["columnConfigArray"]
        if isinstance(item, dict)
    ]
    data_rows = [row for row in normalized_rows[1:] if any(value not in (None, "") for value in row)]
    limit = min(20, max(0, int(sample_limit)))
    comparable_rows = [
        [str(value).strip() for value in row[: len(headers)]]
        for row in data_rows
        if len(row) >= 2
    ]
    collapsed_rows = [
        row
        for row in comparable_rows
        if len(row) >= 2 and len(set(row)) == 1
    ]
    column_collapse = bool(comparable_rows) and len(headers) >= 2 and len(collapsed_rows) == len(comparable_rows)
    valid = bool(data_rows) and not column_collapse
    failure_kind = "column_collapse" if column_collapse else None if valid else "empty_preview"
    return {
        "kind": "native_collection_preview",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "step_id": str(step_id),
        "component": "WebBrowser_DataCollectionV5",
        "performed": True,
        "valid": valid,
        "header_count": len(headers),
        "headers": headers,
        "data_row_count": len(data_rows),
        "sample_rows": data_rows[:limit],
        "failure_kind": failure_kind,
        "message": (
            f"原生数据采集预览返回 {len(data_rows)} 行有效数据。"
            if valid
            else (
                "原生数据采集预览的多列在每个样例行中完全相同，说明列 selector 已坍缩到同一批元素；禁止运行采集步骤。"
                if column_collapse
                else "原生数据采集预览只有表头或为空；禁止运行未修改的采集步骤。"
            )
        ),
        "next_action": (
            None
            if valid
            else {
                "tool": "shizai_rpa_read_flow",
                "arguments": {"flow_ref": str(project_dir), "detail": "edit", "step_id": str(step_id)},
                "reason": "读取当前采集配置并修复同一流程；需要真实网页 selector，不能继续使用 UIA 推导的 DOM 路径。",
            }
        ),
    }


def _renderer_data(value: Any) -> Any:
    if isinstance(value, dict) and "isSuccess" in value and "data" in value:
        if value.get("isSuccess") is False:
            raise ShizaiCloudError(str(value.get("message") or "designer renderer call failed"))
        return value.get("data")
    return value


def _project_editor_id(project_dir: Path) -> str:
    edit_state_path = project_dir / "edit-state.json"
    ok, edit_state, _ = _safe_json(edit_state_path)
    editor_id = str(edit_state.get("currentEditorId") or "").strip() if ok and isinstance(edit_state, dict) else ""
    if not editor_id:
        raise ValueError(f"designer edit-state has no currentEditorId: {edit_state_path}")
    return editor_id


def _active_editor_id(client: ShizaiDesktopAgent, *, response_timeout: float = 15.0) -> str:
    value = _renderer_data(
        call_active_editor_renderer(
            client,
            "ArrangementEditService",
            "getCurrentArrangementInfo",
            [],
            response_timeout=response_timeout,
        )
    )
    return str(value.get("currentEditorId") or "").strip() if isinstance(value, dict) else ""


def _editor_has_unsaved_changes(client: ShizaiDesktopAgent, *, response_timeout: float = 15.0) -> bool:
    value = _renderer_data(
        call_active_editor_renderer(
            client,
            "WorkbenchService",
            "isNeedSave",
            [],
            response_timeout=response_timeout,
        )
    )
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("needSave") or value.get("isNeedSave"))
    return False


def _require_target_editor(client: ShizaiDesktopAgent, expected_editor_id: str) -> str:
    active_editor_id = _active_editor_id(client)
    if active_editor_id != expected_editor_id:
        raise ValueError(
            "active designer editor does not match the requested flow: "
            f"expected {expected_editor_id!r}, found {active_editor_id or 'none'!r}"
        )
    return active_editor_id


# A run must not wait on a designer RPC that may never answer: the guard is a
# safety check on the fast path, and a desktop that cannot answer fails the run
# itself with its own actionable error.
_EDITOR_GUARD_TIMEOUT = 3.0


def _editor_state_for_flow(project_dir: Path, client: Any | None) -> dict[str, Any]:
    """Best-effort evidence about the designer editor that owns this flow.

    The designer holds one editor at a time, so only the flow named by that
    editor can carry unsaved canvas state. A different active editor, an
    unreachable desktop, or a flow that was never opened never blocks.

    Every desktop failure here means "no evidence", never "unsafe": the guard
    must not turn a diagnostic problem into a blocked run.
    """
    state: dict[str, Any] = {"editor_present": False, "editor_dirty": False, "verified": False}
    try:
        editor_id = _project_editor_id(project_dir)
    except ValueError:
        return state
    state["editor_id"] = editor_id
    caller = client or ShizaiDesktopAgent()
    try:
        active_editor_id = _active_editor_id(caller, response_timeout=_EDITOR_GUARD_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - evidence only, never a verdict
        state["check_error"] = str(exc)
        return state
    if active_editor_id != editor_id:
        state["active_editor_id"] = active_editor_id
        return state
    state["editor_present"] = True
    try:
        state["editor_dirty"] = _editor_has_unsaved_changes(caller, response_timeout=_EDITOR_GUARD_TIMEOUT)
        state["verified"] = True
    except Exception as exc:  # noqa: BLE001 - evidence only, never a verdict
        state["check_error"] = str(exc)
        state["editor_dirty"] = None
    return state


def _unsaved_editor_block(project_dir: Path, client: Any | None) -> dict[str, Any] | None:
    """Block a run while the designer holds unsaved edits for this flow.

    The designer owns canvas state. A run started while that state is unsaved
    validates a flow the user cannot see on disk, which is how a run can look
    successful while the persisted task is stale. Only a confirmed dirty flag
    blocks; an unverified flag is reported instead of guessed.
    """
    state = _editor_state_for_flow(project_dir, client)
    if not state.get("editor_present") or state.get("editor_dirty") is not True:
        return None
    return {
        "kind": "native_rpa_editor_unsaved",
        "status": "blocked",
        "failure_kind": "editor_unsaved_changes",
        "requires_save": True,
        "retryable": True,
        "flow_ref": str(project_dir),
        "editor_id": state.get("editor_id"),
        "editor_present": True,
        "editor_dirty": True,
        "editor_state_verified": bool(state.get("verified")),
        "message": (
            "designer 画布上有未保存改动，此时运行会验证磁盘上看不到的配置。"
            "先保存，再重新运行。"
        ),
        "next_action": {
            "tool": "shizai_rpa_save_editor",
            "arguments": {
                "project_ref": str(project_dir),
                "expected_revision": flow_revision(project_dir),
                "dry_run": False,
                "allow_write": True,
            },
            "reason": "保存后 designer 的未保存状态清零，运行才对应真实落盘配置。",
        },
    }


def reload_rpa_editor(
    paths: ShizaiPaths,
    flow_ref: str,
    *,
    expected_revision: str,
    client: ShizaiDesktopAgent | None = None,
    ready_timeout: float = 10.0,
) -> dict[str, Any]:
    """Reload the active designer editor after an MCP write and wait for readiness."""
    project_dir = resolve_project(paths, flow_ref)
    revision_before = check_revision(project_dir, expected_revision)
    editor_id = _project_editor_id(project_dir)
    caller = client or ShizaiDesktopAgent()
    _require_target_editor(caller, editor_id)
    if _editor_has_unsaved_changes(caller):
        raise ValueError("active designer editor has unsaved changes; save them before MCP reload")

    reload_result = call_active_editor_renderer(caller, "WorkbenchService", "reload", [])
    _renderer_data(reload_result)
    deadline = time.monotonic() + max(1.0, min(float(ready_timeout), 30.0))
    last_error = ""
    ready_checks = 0
    # The renderer acknowledges reload before the replacement editor has
    # registered. Require two consecutive checks so callers do not run against
    # the stale editor instance that produced the acknowledgement.
    time.sleep(0.25)
    while time.monotonic() < deadline:
        try:
            remaining = max(0.25, deadline - time.monotonic())
            probe_timeout = max(0.25, min(1.5, remaining / 2))
            if (
                _active_editor_id(caller, response_timeout=probe_timeout) == editor_id
                and not _editor_has_unsaved_changes(caller, response_timeout=probe_timeout)
            ):
                ready_checks += 1
                if ready_checks >= 2:
                    break
            else:
                ready_checks = 0
        except (OSError, TimeoutError, ShizaiCloudError, ValueError) as exc:
            last_error = str(exc)
            ready_checks = 0
        time.sleep(0.25)
    editor_ready = ready_checks >= 2
    if not editor_ready:
        raise ShizaiCloudError(
            "designer reload was acknowledged but the target editor did not become ready"
            + (f": {last_error}" if last_error else "")
        )
    revision_after = flow_revision(project_dir)
    return {
        "kind": "native_rpa_editor_reloaded",
        "flow_ref": str(project_dir),
        "editor_id": editor_id,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "persisted_revision_unchanged": revision_after == revision_before,
        "reload_acknowledged": True,
        "editor_ready": True,
        "ready_checks": ready_checks,
        "canvas_configuration_verified": False,
        "next_action": {
            "tool": "shizai_rpa_run_component",
            "arguments": {
                "project_ref": str(project_dir),
                "expected_revision": revision_after,
                "allow_write": True,
            },
            "missing_arguments": ["step_id"],
            "reason": "精确运行目标组件并核对生成代码/变量缓存，才能证明画布加载了新配置。",
        },
    }


def _top_level_step_ids(context: dict[str, Any]) -> list[str]:
    steps = context.get("steps") if isinstance(context, dict) else None
    if not isinstance(steps, list):
        return []
    return [
        str(step.get("id"))
        for step in steps
        if isinstance(step, dict) and step.get("id") and step.get("top_level")
    ]


def save_rpa_editor(
    paths: ShizaiPaths,
    flow_ref: str,
    *,
    expected_revision: str | None = None,
    dry_run: bool = False,
    client: ShizaiDesktopAgent | None = None,
) -> dict[str, Any]:
    """Save designer canvas edits, then verify the persisted state on disk.

    The designer owns unsaved canvas state. An MCP write or a native run that
    starts while that state is unsaved validates a flow the user cannot see on
    disk, so this calls the designer's own save service and reports success only
    when the dirty flag clears and the task file is re-read afterwards.
    """
    project_dir = resolve_project(paths, flow_ref)
    revision_before = check_revision(project_dir, expected_revision)
    editor_id = _project_editor_id(project_dir)
    caller = client or ShizaiDesktopAgent()
    _require_target_editor(caller, editor_id)
    dirty_before = _editor_has_unsaved_changes(caller)
    context_before = read_edit_context(paths, str(project_dir))
    if dry_run:
        return redact_payload(
            {
                "kind": "native_rpa_editor_save_preview",
                "flow_ref": str(project_dir),
                "editor_id": editor_id,
                "revision": revision_before,
                "dirty_before": dirty_before,
                "steps": _top_level_step_ids(context_before),
                "dry_run": True,
                "saved": False,
                "next_call": {
                    "tool": "shizai_rpa_save_editor",
                    "arguments": {
                        "project_ref": str(project_dir),
                        "expected_revision": revision_before,
                        "dry_run": False,
                        "allow_write": True,
                    },
                },
                "message": "预览只读取 designer 的未保存状态，不写入；提交时携带 expected_revision 与 allow_write=true。",
            }
        )
    _renderer_data(
        call_active_editor_renderer(
            caller, "PackageSaveService", "saveAll", [{"alertOnError": True}]
        )
    )
    dirty_after = _editor_has_unsaved_changes(caller)
    revision_after = flow_revision(project_dir)
    context_after = read_edit_context(paths, str(project_dir))
    result: dict[str, Any] = {
        "kind": "native_rpa_editor_saved",
        "flow_ref": str(project_dir),
        "flow_file": str(context_after.get("flow_file") or ""),
        "editor_id": editor_id,
        "save_requested": True,
        "saved": not dirty_after,
        "dirty_before": dirty_before,
        "dirty_after": dirty_after,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "persisted_change": revision_after != revision_before,
        "steps_before": _top_level_step_ids(context_before),
        "steps": _top_level_step_ids(context_after),
        "truth_source": "native_task",
    }
    if dirty_after:
        result["failure_kind"] = "save_unconfirmed"
        result["retryable"] = True
        result["message"] = (
            "designer 接受了保存请求但仍报告未保存；请在前台处理保存对话框或错误提示后重试，不要继续运行。"
        )
        result["next_action"] = {
            "tool": "shizai_rpa_save_editor",
            "arguments": {
                "project_ref": str(project_dir),
                "expected_revision": revision_after,
                "allow_write": True,
            },
            "reason": "未保存状态下的运行会验证磁盘上看不到的画布配置。",
        }
        return redact_payload(result)
    if dirty_before and not result["persisted_change"]:
        result["note"] = (
            "保存前存在未保存状态，但 revision 未变化：改动可能写在未纳入 revision 的布局文件里，"
            "也可能实际没有改动。继续前用 read_flow 确认目标参数已落盘。"
        )
    result["next_action"] = {
        "tool": "shizai_rpa_reload_editor",
        "arguments": {
            "project_ref": str(project_dir),
            "expected_revision": revision_after,
            "allow_write": True,
        },
        "reason": "保存后刷新 editor，再用 run_component 按 revision 运行目标组件验证画布。",
    }
    return redact_payload(result)


def _component_cache_summary(project_dir: Path, task_id: str, step_id: str) -> dict[str, Any] | None:
    cache_path = project_dir / ".sz" / "variable_cache" / f"{task_id}.json"
    ok, payload, _ = _safe_json(cache_path)
    entry = payload.get(step_id) if ok and isinstance(payload, dict) else None
    if not isinstance(entry, dict):
        return None
    result = entry.get("result")
    table = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else None
    summary: dict[str, Any] = {
        "cache_path": str(cache_path),
        "component_id": str(entry.get("componentId") or step_id),
        "var_names": list(entry.get("varNames") or []),
    }
    if isinstance(table, list) and table and isinstance(table[0], list):
        summary.update(
            {
                "headers": list(table[0]),
                "data_row_count": max(0, len(table) - 1),
                "sample_rows": [list(row) for row in table[1:4] if isinstance(row, list)],
            }
        )
    else:
        summary["result_present"] = result is not None
    return summary


def _runtime_artifact_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "mtime_ns": None, "size": 0, "sha256": None}
    content = path.read_bytes()
    return {
        "exists": True,
        "mtime_ns": path.stat().st_mtime_ns,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _runtime_artifact_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not after.get("exists"):
        return False
    return not before.get("exists") or any(
        before.get(key) != after.get(key) for key in ("mtime_ns", "size", "sha256")
    )


def run_rpa_component(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    *,
    expected_revision: str,
    client: ShizaiDesktopAgent | None = None,
) -> dict[str, Any]:
    """Run exactly one component in the active designer and return compact evidence."""
    project_dir = resolve_project(paths, flow_ref)
    revision = check_revision(project_dir, expected_revision)
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    node = entities.get(str(step_id)) if isinstance(entities, dict) else None
    if not isinstance(node, dict):
        raise ValueError(f"RPA step not found: {step_id}")
    editor_id = _project_editor_id(project_dir)
    caller = client or ShizaiDesktopAgent()
    _require_target_editor(caller, editor_id)
    if _editor_has_unsaved_changes(caller):
        raise ValueError("active designer editor has unsaved changes; save or reload it before component execution")
    package = _native_package_summary(paths, project_dir)
    application_id = str(package.get("application_id") or "").strip()
    if not package.get("matched") or not application_id:
        raise ValueError("flow is not registered in the designer application_package index")

    task_id = task_path.stem
    code_path = project_dir / "projects" / "_temp" / "component_run.py"
    cache_path = project_dir / ".sz" / "variable_cache" / f"{task_id}.json"
    code_before = _runtime_artifact_snapshot(code_path)
    cache_before = _runtime_artifact_snapshot(cache_path)
    request = {
        "projectId": application_id,
        "editorId": editor_id,
        "componentIds": [str(step_id)],
        "isIPA": False,
        "isUseTrigger": False,
        # The designer's generated-code cache also restores non-serializable
        # upstream values such as a live browser object for isolated steps.
        "useCache": True,
        "createCodeParams": {"useCache": True},
    }
    started = time.monotonic()
    raw_run_result = call_active_editor_renderer(caller, "RunService", "runComponentInEditor", [request])
    run_result = _renderer_data(raw_run_result)
    elapsed_seconds = round(time.monotonic() - started, 3)
    state = str(run_result.get("state") or "") if isinstance(run_result, dict) else ""
    code_after = _runtime_artifact_snapshot(code_path)
    cache_after = _runtime_artifact_snapshot(cache_path)
    code_fresh = _runtime_artifact_changed(code_before, code_after)
    cache_fresh = _runtime_artifact_changed(cache_before, cache_after)
    code = code_path.read_text(encoding="utf-8") if code_path.is_file() else ""
    generated_component_ids = list(dict.fromkeys(re.findall(r"#@![^\r\n]*?@!-@!([^@]+)@!-@!", code)))
    cache_summary = _component_cache_summary(project_dir, task_id, str(step_id))
    target_generated = str(step_id) in generated_component_ids
    exact_component = target_generated and set(generated_component_ids) == {str(step_id)}
    runtime_success = state.casefold() == "success"
    collection_verified = (
        node.get("type") == "WebBrowser_DataCollectionV5"
        and cache_fresh
        and isinstance(cache_summary, dict)
        and int(cache_summary.get("data_row_count") or 0) > 0
    )
    runtime_verified = runtime_success and code_fresh and exact_component and (
        collection_verified or node.get("type") != "WebBrowser_DataCollectionV5"
    )
    return {
        "kind": "native_rpa_component_run",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "revision": revision,
        "step_id": str(step_id),
        "component": str(node.get("type") or ""),
        "editor_id": editor_id,
        "run_result": run_result,
        "elapsed_seconds": elapsed_seconds,
        "generated_code_path": str(code_path),
        "generated_component_ids": generated_component_ids,
        "generated_code_fresh": code_fresh,
        "exact_component_verified": exact_component,
        "cache": cache_summary,
        "cache_fresh": cache_fresh,
        "runtime_verified": runtime_verified,
        "page_evidence_required": bool(node.get("mcpElementIds")) and not collection_verified,
        "message": (
            "目标组件已单独运行，并由生成代码和变量缓存验证。"
            if runtime_verified
            else "组件运行请求已返回，但本次生成代码或输出证据不足，不能视为单组件验证完成。"
        ),
    }


def attach_rpa_debug_verification(
    paths: ShizaiPaths,
    flow_ref: str,
    debug_result: dict[str, Any],
) -> dict[str, Any]:
    """Attach confirmed component-boundary evidence to a debug response.

    ``debug_session`` owns DAP transport and reports the component that just
    executed.  This shared project-layer adapter turns that evidence into the
    same gate returned by step writes, keeping CLI and MCP behavior identical.
    Page-level success still requires the caller's screenshot/DOM/OCR check.
    """
    if not isinstance(debug_result, dict):
        return debug_result
    evidence = debug_result.get("runtime_verification")
    if not isinstance(evidence, dict) or not bool(evidence.get("verified")):
        return debug_result
    component_id = str(evidence.get("component_id") or "").strip()
    if not component_id:
        return debug_result
    loaded = read_rpa_flow(paths, flow_ref)
    flow = loaded.get("flow") if isinstance(loaded.get("flow"), dict) else {}
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    step = next(
        (item for item in steps if isinstance(item, dict) and str(item.get("id") or "").strip() == component_id),
        None,
    )
    if step is None:
        return debug_result
    validation = validate_rpa_step(paths, loaded["flow_ref"], component_id)
    gate = _rpa_verification_gate(
        loaded["flow_ref"],
        step,
        validation,
        runtime_evidence=evidence,
    )
    debug_result["verification_gate"] = gate
    debug_result["runtime_verification"]["static_validation_valid"] = bool(validation.get("valid"))
    debug_result["runtime_verification"]["page_evidence_required"] = bool(step.get("element_ids"))
    return debug_result


def _replace_nested_strings(value: Any, old: str, new: str, encoded_old: str, encoded_new: str) -> Any:
    """Replace template text in a JSON-compatible native task payload."""
    if isinstance(value, str):
        return value.replace(old, new).replace(encoded_old, encoded_new)
    if isinstance(value, list):
        return [_replace_nested_strings(item, old, new, encoded_old, encoded_new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_nested_strings(item, old, new, encoded_old, encoded_new) for key, item in value.items()}
    return value


def _staged_step(flow: dict[str, Any] | None, component: str) -> dict[str, Any] | None:
    """Return the first staged step for a native component type."""
    if not isinstance(flow, dict) or not isinstance(flow.get("steps"), list):
        return None
    return next(
        (item for item in flow["steps"] if isinstance(item, dict) and item.get("component") == component),
        None,
    )


def _native_task_file(project_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Return the designer task file and its JSON payload for one application."""
    candidates = sorted(
        (
            item
            for item in project_dir.rglob("*.task")
            if "draft-restore-backup" not in {
                part.casefold() for part in item.relative_to(project_dir).parts[:-1]
            }
        ),
        key=lambda item: (
            len(item.relative_to(project_dir).parts),
            item.name != "io5cd.task",
            str(item).lower(),
        ),
    )
    for task_path in candidates:
        ok, payload, _ = _safe_json(task_path)
        if not ok or not isinstance(payload, dict):
            continue
        entities = payload.get("componentModelEntities")
        if isinstance(entities, dict) and isinstance(entities.get("MAIN_COMPONENT"), dict):
            return task_path, payload
    raise FileNotFoundError(f"no native .task flow found in: {project_dir}")


def _normalize_native_page_scope_js(project_dir: Path) -> tuple[Path, bool]:
    """Make generated link-recovery JS independent of a stale result element."""
    task_path, payload = _native_task_file(project_dir)
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    if not metadata_ok or not isinstance(metadata, dict) or not metadata.get("mcpUseJsLink"):
        return task_path, False
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return task_path, False
    changed = False
    for node in entities.values():
        if not isinstance(node, dict) or node.get("type") != "WebBrowser_RunJsInIeBySelector_v3":
            continue
        arguments = node.get("args0")
        if not isinstance(arguments, list):
            continue
        for argument in arguments:
            if not isinstance(argument, dict):
                continue
            if argument.get("name") == "jsCode":
                current_script = str(argument.get("value") or "")
                rendered_script = (
                    f'"""{DEFAULT_JS_LINK_SCRIPT}"""'
                    if current_script.strip().startswith('"""')
                    else DEFAULT_JS_LINK_SCRIPT
                )
                if current_script != rendered_script:
                    argument["value"] = rendered_script
                    changed = True
                continue
            if argument.get("name") != "uiSelector":
                continue
            # RuntimeHost accepts an empty selector for page-scoped execution.
            # JSON null reaches CommonParseSelector and is rejected before the
            # script can inspect the current page.
            if argument.get("value") != "":
                argument["value"] = ""
                changed = True
    if changed:
        temporary = task_path.with_suffix(task_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(task_path)
    return task_path, changed


def _native_package_summary(paths: ShizaiPaths, project_dir: Path) -> dict[str, Any]:
    """Resolve the runtime identifiers persisted for one local application."""
    row = package_record_for_path(paths, project_dir)
    return {
        "matched": row is not None,
        "application_id": row.get("applicationId") if row else None,
        "package_id": row.get("packageId") if row else None,
        "local_path": row.get("packageLocalFullPath") if row else str(project_dir),
        "runtime_version": row.get("runtimeVersion") if row else None,
        "factory_version": row.get("factoryVersion") if row else None,
        "application_name": row.get("applicationName") if row else None,
    }


def _native_index_snapshot(root: Path, task_path: Path) -> dict[str, Any]:
    """Read the task/module timestamps the designer uses for package freshness."""
    index_path = root / "file_index_700.json"
    module_path = root / "projects" / "flow_modules" / f"{task_path.stem}.py"
    snapshot: dict[str, Any] = {
        "index_file": str(index_path),
        "index_exists": index_path.is_file(),
        "index_valid": False,
        "module_key": f"flow_modules/{task_path.stem}.py",
        "module_file": str(module_path),
        "module_exists": module_path.is_file(),
        "index_task_time": None,
        "index_code_time": None,
        "task_mtime_ms": None,
        "code_mtime_ms": None,
        "task_index_fresh": False,
        "code_index_fresh": False,
    }
    try:
        snapshot["task_mtime_ms"] = int(task_path.stat().st_mtime * 1000)
        if module_path.is_file():
            snapshot["code_mtime_ms"] = int(module_path.stat().st_mtime * 1000)
    except OSError:
        pass
    if not index_path.is_file():
        return snapshot
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return snapshot
    if not isinstance(payload, dict):
        return snapshot
    snapshot["index_valid"] = True
    module = payload.get(snapshot["module_key"])
    if not isinstance(module, dict):
        return snapshot
    snapshot["index_task_time"] = module.get("taskTime")
    snapshot["index_code_time"] = module.get("codeTime")
    task_time = snapshot["task_mtime_ms"]
    code_time = snapshot["code_mtime_ms"]
    index_task_time = snapshot["index_task_time"]
    index_code_time = snapshot["index_code_time"]
    snapshot["task_index_fresh"] = (
        isinstance(task_time, int) and isinstance(index_task_time, (int, float)) and index_task_time >= task_time
    )
    snapshot["code_index_fresh"] = (
        isinstance(code_time, int) and isinstance(index_code_time, (int, float)) and index_code_time >= code_time
    )
    return snapshot


def _native_designer_state_consistency(root: Path, task_path: Path) -> dict[str, Any]:
    """Check the local task graph and derived files before the designer reads them.

    The designer keeps the JSON task and generated runtime files separately.
    A stale index or malformed derived file can make the editor hydrate a
    partial node model and fail later with an opaque ``addChild`` error.
    Missing generated files are reported as materialization work; stale or
    malformed files are reported as a state conflict.
    """
    errors: list[str] = []
    missing: list[str] = []
    try:
        payload = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "conflict": True,
            "errors": [f"native task cannot be read: {exc}"],
            "missing_files": [],
            "task": str(task_path),
        }
    entities = payload.get("componentModelEntities") if isinstance(payload, dict) else None
    if not isinstance(entities, dict):
        errors.append("native task has no componentModelEntities")
        statements: list[str] = []
    else:
        statements = _native_statement_ids(payload)
        if len(statements) != len(set(statements)):
            errors.append("native task statements contain duplicate node ids")
        missing_nodes = [node_id for node_id in statements if not isinstance(entities.get(node_id), dict)]
        if missing_nodes:
            errors.append("native task statements reference missing nodes: " + ", ".join(missing_nodes))

    index = _native_index_snapshot(root, task_path)
    if index["index_exists"] and not index["index_valid"]:
        errors.append("file_index_700.json is malformed")
    if index["index_valid"] and not index["task_index_fresh"]:
        errors.append("file_index_700.json taskTime is older than the native .task file")
    if index["index_valid"] and index["module_exists"] and not index["code_index_fresh"]:
        errors.append("file_index_700.json codeTime is older than the flow module")

    for relative in (
        "file_index_700.json",
        "file_sourcemap.json",
        "fileList.json",
        "edit-state.json",
        "variableInfo",
        "main.sz",
        "projects/__init__.py",
        "projects/global_data.py",
        "projects/rpaRoot.py",
        "projects/flow.py",
        f"projects/flow_modules/{task_path.stem}.py",
    ):
        path = root / relative
        if not path.is_file():
            missing.append(relative)

    variable_info = _native_variable_info_snapshot(root)
    if variable_info["exists"] and not variable_info["valid"]:
        errors.append("variableInfo is malformed")
    materialized_evidence = any(
        (root / relative).is_file()
        for relative in (
            "file_index_700.json",
            "file_sourcemap.json",
            "variableInfo",
            f"projects/flow_modules/{task_path.stem}.py",
        )
    )
    main_snapshot = _native_main_snapshot(root) if (root / "main.sz").is_file() else None
    if main_snapshot is not None and not main_snapshot["decryptable"] and materialized_evidence:
        errors.append("main.sz is not a decryptable native designer payload")

    for relative in ("file_sourcemap.json", "fileList.json", "edit-state.json"):
        path = root / relative
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"{relative} is malformed")
            continue
        if not isinstance(value, dict):
            errors.append(f"{relative} must contain a JSON object")

    return {
        "valid": not errors and not missing,
        "conflict": bool(errors),
        "errors": errors,
        "missing_files": missing,
        "task": str(task_path),
        "statement_ids": statements,
        "index": index,
        "variable_info": variable_info,
        "main": main_snapshot,
    }


def _native_run_state_path(paths: ShizaiPaths, project_dir: Path) -> Path:
    """Return a stable local state file without putting runtime data in the app."""
    digest = hashlib.sha256(str(project_dir).casefold().encode("utf-8")).hexdigest()[:20]
    return paths.data_root / "mcp-rpa-runs" / f"{digest}.json"


def _write_native_run_state(paths: ShizaiPaths, project_dir: Path, state: dict[str, Any]) -> None:
    """Persist one small RPA run marker atomically for later status queries."""
    target = _native_run_state_path(paths, project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(target)


def _read_native_run_state(paths: ShizaiPaths, project_dir: Path) -> dict[str, Any] | None:
    """Read the last local RPA run marker, if one was recorded."""
    path = _native_run_state_path(paths, project_dir)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _native_current_log_lines(lines: list[str], submitted_at: float | None) -> list[str]:
    """Exclude old JSON and timestamped desktop records, including their continuations."""
    if not submitted_at:
        return lines
    result = []
    current = True
    for line in lines:
        timestamp = None
        try:
            payload = json.loads(line)
            if isinstance(payload, dict) and isinstance(payload.get("timestamp"), (int, float)):
                timestamp = payload["timestamp"] / 1000
        except (TypeError, ValueError):
            match = re.match(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)", line)
            if match:
                try:
                    # Desktop log timestamps use the same local clock as the process.
                    timestamp = datetime.fromisoformat(match.group(1).replace(",", ".")).timestamp()
                except ValueError:
                    pass
        if timestamp is not None:
            current = timestamp >= submitted_at - 1
        if current:
            result.append(line)
    return result


def _native_runtime_log_lines(paths: ShizaiPaths, run_state: dict[str, Any] | None) -> list[str]:
    """Collect bounded local runtime evidence for one recorded run."""
    if not run_state:
        return []
    needles = [
        str(run_state.get("uuid") or ""),
        str(run_state.get("workflow_run_id") or ""),
        str(run_state.get("project") or ""),
        str(run_state.get("node_id") or ""),
    ]
    needles = [item.casefold() for item in needles if item]
    log_root = paths.data_root / "logs"
    if not log_root.is_dir():
        return []
    submitted_at = float(run_state.get("submitted_at") or 0)
    submitted_ms = int(submitted_at * 1000) if submitted_at else 0
    candidates: list[tuple[Path, bool]] = []
    patterns = (
        "node-main.log*",
        "node-rpc-common.log*",
        "node-rpc.error.log*",
        "engine_sub.log*",
        "engine_main.log*",
        "II.RPA.RuntimeHost.txt*",
    )
    for pattern in patterns:
        candidates.extend((path, False) for path in log_root.rglob(pattern))
    for path in log_root.rglob("run_*.jsonl"):
        match = re.match(r"run_(\d+)_", path.name)
        run_ms = int(match.group(1)) if match else 0
        if not submitted_ms or (run_ms and abs(run_ms - submitted_ms) <= 30_000):
            candidates.append((path, True))
    if not candidates or not needles:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    unique: dict[str, tuple[Path, bool]] = {}
    for path, force_include in candidates:
        unique.setdefault(str(path.resolve()).casefold(), (path, force_include))
        if force_include:
            unique[str(path.resolve()).casefold()] = (path, True)

    # A run_*.jsonl file can have a coarse filesystem mtime on Windows.  Order
    # the run-scoped file explicitly and process it last so a fresh terminal
    # error/success record is not discarded by the bounded evidence window.
    def evidence_sort_key(item: tuple[Path, bool]) -> tuple[int, float, float]:
        path, force_include = item
        if force_include and submitted_ms:
            match = re.match(r"run_(\d+)_", path.name)
            distance = abs(int(match.group(1)) - submitted_ms) if match else 10**18
            # Reverse sorting makes the nearest run timestamp win among
            # run-scoped files.  Generic logs are deliberately processed
            # first so the final bounded return window retains this record.
            return 0, -float(distance), path.stat().st_mtime if path.exists() else 0
        return 1, 0, path.stat().st_mtime if path.exists() else 0

    ordered_items = sorted(unique.values(), key=evidence_sort_key, reverse=True)
    forced = [item for item in ordered_items if item[1]]
    generic = [item for item in ordered_items if not item[1]]
    # Always retain every run-scoped file, while keeping the generic evidence
    # list bounded.  The current run's terminal record must survive the cap.
    ordered = generic[: max(0, 24 - len(forced))] + forced
    for path, force_include in ordered:
        try:
            with path.open("rb") as stream:
                offset = max(0, path.stat().st_size - 524288)
                stream.seek(offset)
                content = stream.read(524288).decode("utf-8", errors="replace")
            if offset:
                # A tail can begin halfway through an old JSON/desktop record.
                # Its missing timestamp must not turn it into current evidence.
                content = content.partition("\n")[2]
        except OSError:
            continue
        source_lines = _native_current_log_lines(content.splitlines(), submitted_at)
        hit_indexes = []
        if force_include:
            hit_indexes.extend(range(max(0, len(source_lines) - 80), len(source_lines)))
        else:
            hit_indexes.extend(
                index for index, line in enumerate(source_lines) if any(needle in line.casefold() for needle in needles)
            )
            # Engine/RuntimeHost records often omit the request UUID, but keep
            # error and terminal records from the current log tail as evidence.
            if submitted_ms:
                hit_indexes.extend(
                    index
                    for index, line in enumerate(source_lines)
                    if any(
                        marker in line.casefold()
                        for marker in (
                            "运行错误",
                            "run-error",
                            "statechange state: error",
                            'runstatus":"error',
                            "isfinished: true",
                        )
                    )
                )
        for hit_index in hit_indexes:
            # RuntimeHost writes the request UUID once, then emits the actual
            # run-error/run-success records on following lines without repeating
            # that UUID. Keep a small contiguous window for that same request.
            start = max(0, hit_index - 8)
            end = min(len(source_lines), hit_index + 32)
            for line in source_lines[start:end]:
                redacted = str(redact_payload(line))
                if redacted not in seen:
                    seen.add(redacted)
                    lines.append(redacted)
    return lines[-40:]


_NATIVE_OUTPUT_SUFFIXES = {".csv", ".xls", ".xlsx", ".xlsm", ".tsv"}


def _native_configured_output_path(value: Any) -> Path | None:
    """Return an explicitly configured absolute output path, if present."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _native_output_candidates(
    project_dir: Path,
    output_file: str,
    configured_path: Path | None = None,
) -> list[Path]:
    """Find expected and spreadsheet-like outputs in the native project."""
    expected_name = Path(output_file).name or "result.xlsx"
    configured = configured_path.resolve() if configured_path is not None else None
    expected_paths = {
        str((project_dir / "res" / expected_name).resolve()).casefold(),
        str((project_dir / expected_name).resolve()).casefold(),
    }
    if configured is not None:
        expected_paths.add(str(configured).casefold())
    candidates: list[Path] = []
    if configured is not None and configured.is_file():
        candidates.append(configured)
    for candidate in (project_dir / "res" / expected_name, project_dir / expected_name):
        if candidate.is_file():
            candidates.append(candidate)
    roots = [project_dir / "res", project_dir]
    if configured is not None and configured.parent not in roots:
        roots.append(configured.parent)
    for root in roots:
        if not root.is_dir():
            continue
        try:
            items = root.rglob("*")
        except OSError:
            continue
        for candidate in items:
            if not candidate.is_file() or candidate.suffix.casefold() not in _NATIVE_OUTPUT_SUFFIXES:
                continue
            # Excel keeps an open-workbook lock beside the real file. It is
            # never an output from this flow and can otherwise win the
            # evidence scan because it has a newer mtime.
            if candidate.name.startswith("~$"):
                continue
            if any(part.casefold() in {"venv", "__pycache__", "site-packages"} for part in candidate.parts):
                continue
            candidates.append(candidate)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    # Keep the exact configured output first, then expose the remaining
    # spreadsheet artifacts newest-first for diagnostics.  Directory walking
    # order is not stable and previously made an archived/old workbook appear
    # to be the result of the latest run.
    return sorted(
        result,
        key=lambda item: (
            0 if str(item).casefold() in expected_paths else 1,
            -(item.stat().st_mtime if item.exists() else 0),
        ),
    )


def _native_expected_output_path(
    project_dir: Path,
    output_file: str,
    configured_path: Path | None = None,
) -> Path | None:
    """Return only the configured output path, never a previous-run artifact."""
    expected_name = Path(output_file).name or "result.xlsx"
    if configured_path is not None and configured_path.is_file():
        return configured_path.resolve()
    for candidate in (project_dir / "res" / expected_name, project_dir / expected_name):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _archive_existing_native_output(
    project_dir: Path,
    output_file: str,
    configured_path: Path | None = None,
) -> list[dict[str, str]]:
    """Move an existing workbook aside so Excel_Cs_NewV4 can create it again.

    Native Excel components in the installed runtime reject an existing file
    instead of replacing it.  Archiving is recoverable and keeps every retry
    deterministic.  Only the exact configured output is moved; files already
    named ``previous-*`` are left untouched.
    """
    expected_name = Path(output_file).name or "result.xlsx"
    archived: list[dict[str, str]] = []
    seen: set[str] = set()
    candidates = []
    if configured_path is not None:
        candidates.append(configured_path)
    candidates.extend((project_dir / "res" / expected_name, project_dir / expected_name))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        key = str(candidate.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = candidate.stem
        suffix = candidate.suffix
        destination = candidate.with_name(f"{stem}.previous-{timestamp}{suffix}")
        counter = 2
        while destination.exists():
            destination = candidate.with_name(f"{stem}.previous-{timestamp}-{counter}{suffix}")
            counter += 1
        try:
            shutil.move(str(candidate), str(destination))
        except OSError as exc:
            raise ValueError(f"已有输出文件 {candidate} 当前被占用；请关闭正在打开该文件的 Excel 窗口后重试") from exc
        archived.append({"from": str(candidate), "to": str(destination)})
    return archived


def _native_run_output_names(flow: dict[str, Any]) -> list[str]:
    """Return every exact workbook name that a native flow may create.

    A flow using explicit Excel components has two output-related settings:
    ``DataCollectionV5.output_file`` and ``Excel_Cs_NewV4.name``.  The latter
    is the file that blocks a retry, so relying on only the flow-level default
    can leave an existing workbook in place.
    """
    names: dict[str, str] = {}

    def add(value: Any) -> None:
        if value is None:
            return
        name = Path(str(value)).name.strip()
        if not name:
            return
        names.setdefault(name.casefold(), name)

    add(flow.get("output_file"))
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        component = str(step.get("component") or "")
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if component == "Excel_Cs_NewV4":
            add(config.get("name"))
        elif component == "WebBrowser_DataCollectionV5":
            add(config.get("output_file"))
    return list(names.values())


def _xlsx_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    """Read one OOXML cell for output evidence without loading a workbook app."""
    # Shared-string cells store their index in <v>, not in a <t> child.
    # WPS/Excel both commonly emit this compact OOXML form.
    values = [str(item.text or "") for item in cell.iter() if item.tag.rsplit("}", 1)[-1] == "t"]
    value = "".join(values)
    if not value:
        value_node = next(
            (item for item in cell if item.tag.rsplit("}", 1)[-1] == "v"),
            None,
        )
        value = str(value_node.text or "") if value_node is not None else ""
    cell_type = str(cell.attrib.get("t") or "")
    if cell_type == "s":
        try:
            index = int(value)
        except (TypeError, ValueError):
            return value
        return shared_strings[index] if 0 <= index < len(shared_strings) else value
    return value


def _xlsx_output_metrics(
    output_path: Path,
    required_columns: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return structure-only quality metrics for an XLSX output.

    The status surface deliberately reports counts rather than cell values, so
    it can diagnose a broken selector without exposing collected business data.
    """
    requested_columns = [str(item).strip() for item in required_columns or [] if str(item).strip()]
    empty = {
        "columns": [],
        "row_count": 0,
        "data_row_count": 0,
        "column_non_empty_rows": {},
        "missing_required_columns": requested_columns,
        "empty_required_columns": [],
        "complete_required_row_count": 0,
        "valid_link_rows": 0,
        "invalid_link_rows": 0,
    }
    try:
        with zipfile.ZipFile(output_path) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root:
                    if item.tag.rsplit("}", 1)[-1] == "si":
                        shared_strings.append(
                            "".join(str(node.text or "") for node in item.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                        )
            sheet_names = sorted(
                name for name in archive.namelist() if name.startswith("xl/worksheets/") and name.endswith(".xml")
            )
            if not sheet_names:
                return empty
            root = ET.fromstring(archive.read(sheet_names[0]))
            rows: list[list[str]] = []
            for row in root.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                cells = [child for child in row if child.tag.rsplit("}", 1)[-1] == "c"]
                values: list[str] = []
                for cell in cells:
                    reference = str(cell.attrib.get("r") or "")
                    match = re.match(r"([A-Z]+)", reference.upper())
                    column = 0
                    if match:
                        for char in match.group(1):
                            column = column * 26 + (ord(char) - ord("A") + 1)
                        column -= 1
                    while len(values) <= column:
                        values.append("")
                    values[column] = _xlsx_cell_text(cell, shared_strings).strip()
                rows.append(values)
            raw_header = [str(item).strip() for item in (rows[0] if rows else [])]
            columns = [item for item in raw_header if item]
            column_indexes = {
                name: index
                for index, name in enumerate(raw_header)
                if name and name not in {item for item in raw_header[:index] if item}
            }
            data_rows = [values for values in rows[1:] if any(str(item).strip() for item in values)]
            non_empty = {
                name: sum(1 for values in data_rows if index < len(values) and str(values[index]).strip())
                for name, index in column_indexes.items()
            }
            missing = [item for item in requested_columns if item not in column_indexes]
            present_required = [item for item in requested_columns if item in column_indexes]
            complete = sum(
                1
                for values in data_rows
                if all(
                    column_indexes[item] < len(values) and str(values[column_indexes[item]]).strip()
                    for item in present_required
                )
                and not missing
            )
            link_header = next(
                (item for item in raw_header if _canonical_result_field(item) == "link"),
                None,
            )
            link_index = column_indexes.get(link_header) if link_header else None
            valid_links = 0
            invalid_links = 0
            if link_index is not None:
                for values in data_rows:
                    value = str(values[link_index]).strip() if link_index < len(values) else ""
                    try:
                        parsed = urllib.parse.urlparse(value)
                    except ValueError:
                        parsed = urllib.parse.urlparse("")
                    host = parsed.hostname.casefold() if parsed.hostname else ""
                    host_prefix = host.split(".", 1)[0]
                    path_parts = [
                        urllib.parse.unquote(item).casefold() for item in parsed.path.split("/") if item.strip()
                    ]
                    blocked_markers = {
                        "account",
                        "cart",
                        "checkout",
                        "help",
                        "login",
                        "orders",
                        "passport",
                        "search",
                        "signin",
                        "support",
                    }
                    query_keys = {item.casefold() for item in urllib.parse.parse_qs(parsed.query)}
                    has_detail_identity = bool(path_parts) or bool(
                        query_keys & {"id", "item", "itemid", "pid", "product", "productid", "sku"}
                    )
                    is_detail = bool(
                        parsed.scheme in {"http", "https"}
                        and host
                        and host_prefix not in blocked_markers
                        and not any(part in blocked_markers for part in path_parts)
                        and has_detail_identity
                    )
                    if is_detail:
                        valid_links += 1
                    else:
                        invalid_links += 1
            return {
                "columns": columns,
                "row_count": len(rows),
                "data_row_count": len(data_rows),
                "column_non_empty_rows": non_empty,
                "missing_required_columns": missing,
                "empty_required_columns": [item for item in present_required if non_empty.get(item, 0) == 0],
                "complete_required_row_count": complete,
                "valid_link_rows": valid_links,
                "invalid_link_rows": invalid_links,
            }
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        return empty


def _xlsx_output_shape(output_path: Path) -> tuple[list[str], int, int]:
    """Return the legacy header and row-count view for an XLSX file."""
    metrics = _xlsx_output_metrics(output_path)
    return (
        list(metrics["columns"]),
        int(metrics["row_count"]),
        int(metrics["data_row_count"]),
    )


def _native_text_evidence(output_path: Path, *, limit: int = 8192) -> dict[str, Any]:
    """Read a bounded UTF-8 preview; never imply a prefix verifies a whole file."""
    import codecs

    try:
        with output_path.open("rb") as stream:
            raw = stream.read(limit + 1)
        truncated = len(raw) > limit
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        value = decoder.decode(raw[:limit], final=not truncated)
    except (OSError, UnicodeDecodeError) as exc:
        return {"encoding": "utf-8", "decoding_valid": False, "error": str(exc),
                "preview": None, "complete": False}
    preview = redact_payload(value)
    return {"encoding": "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8",
            "decoding_valid": True, "preview": preview, "truncated": truncated,
            "redacted": preview != value, "complete": not truncated and preview == value,
            "bytes_sampled": min(len(raw), limit)}


def _native_output_summary(
    project_dir: Path,
    output_path: Path,
    run_state: dict[str, Any] | None,
    required_columns: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return format and freshness evidence without returning table contents."""
    try:
        stat = output_path.stat()
    except OSError as exc:
        return {"path": str(output_path), "exists": False, "format_valid": False, "error": str(exc)}
    suffix = output_path.suffix.casefold()
    format_valid: bool | None = None
    columns: list[str] = []
    row_count: int | None = None
    data_row_count: int | None = None
    quality: dict[str, Any] | None = None
    text_evidence: dict[str, Any] | None = None
    file_kind = suffix.lstrip(".") or "file"
    if suffix in {".xlsx", ".xlsm"}:
        format_valid = zipfile.is_zipfile(output_path)
        if format_valid:
            try:
                with zipfile.ZipFile(output_path) as archive:
                    format_valid = (
                        "[Content_Types].xml" in archive.namelist() and "xl/workbook.xml" in archive.namelist()
                    )
            except (OSError, zipfile.BadZipFile):
                format_valid = False
        if format_valid:
            quality = _xlsx_output_metrics(output_path, required_columns)
            columns = list(quality["columns"])
            row_count = int(quality["row_count"])
            data_row_count = int(quality["data_row_count"])
    elif suffix == ".xls":
        try:
            magic = output_path.read_bytes()[:8]
        except OSError:
            magic = b""
        # Native Excel output is commonly OLE compound storage; some builds
        # write an OOXML zip with an xls suffix, so accept both signatures.
        format_valid = magic.startswith(b"\xd0\xcf\x11\xe0") or magic.startswith(b"PK\x03\x04")
    elif suffix == ".txt":
        text_evidence = _native_text_evidence(output_path)
        format_valid = text_evidence["decoding_valid"] if not text_evidence.get("truncated") else None
    elif suffix in {".csv", ".tsv"}:
        try:
            text = output_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = output_path.read_text(encoding="gb18030")
            except (OSError, UnicodeDecodeError):
                text = ""
        except OSError:
            text = ""
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            rows = list(csv.reader(io.StringIO(text[:262144]), delimiter=delimiter))
        except csv.Error:
            rows = []
        columns = [str(item) for item in (rows[0] if rows else []) if str(item).strip()]
        row_count = len(rows)
        data_row_count = sum(1 for values in rows[1:] if any(str(item).strip() for item in values))
        format_valid = bool(rows) or stat.st_size == 0
    try:
        relative = str(output_path.relative_to(project_dir))
    except ValueError:
        relative = output_path.name
    submitted_at = float((run_state or {}).get("submitted_at") or 0)
    return {
        "path": str(output_path),
        "relative_path": relative,
        "exists": True,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "fresh_for_run": bool(submitted_at and stat.st_mtime >= submitted_at - 1),
        "extension": suffix,
        "file_kind": file_kind,
        "format_valid": format_valid,
        "columns": columns,
        "row_count": row_count,
        "data_row_count": data_row_count,
        "has_data": bool(stat.st_size and text_evidence.get("decoding_valid")) if text_evidence else bool(data_row_count),
        "quality": quality,
        **({"text_evidence": text_evidence} if text_evidence is not None else {}),
    }


def _native_output_candidate(project_dir: Path, output_file: str) -> Path | None:
    """Find a generated output without recursively scanning bundled virtualenvs."""
    candidates = _native_output_candidates(project_dir, output_file)
    return candidates[0] if candidates else None


def _native_last_known_page(
    paths: ShizaiPaths,
    project_dir: Path,
    flow: dict[str, Any],
) -> dict[str, Any]:
    """Return the strongest page identity available from captured elements."""
    candidates: list[dict[str, str]] = []
    try:
        _, payload = _element_file(paths, str(project_dir))
    except (FileNotFoundError, OSError, ValueError):
        payload = {}
    element_map = payload.get("elementMap") if isinstance(payload, dict) else {}
    if isinstance(element_map, dict):
        for element in element_map.values():
            if not isinstance(element, dict):
                continue
            title, url = _selector_page_identity(element)
            title = _browser_selector_title(title)
            if title or url:
                candidates.append({"title": title, "url": url, "source": "captured_element"})
    # Prefer a captured result page over the initial target page when several
    # elements were imported during the design loop.
    target_url = str(flow.get("target_url") or "").strip()
    result = next(
        (item for item in reversed(candidates) if item.get("url") and item.get("url") != target_url),
        None,
    )
    if result:
        return result
    if candidates:
        return candidates[-1]
    return {
        "title": "",
        "url": target_url,
        "source": "flow_target" if target_url else "unavailable",
    }


def native_rpa_sync_diagnose(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Diagnose native package IDs, task freshness, runtime prerequisites, and flow readiness."""
    project_dir = resolve_project(paths, project_ref)
    package = _native_package_summary(paths, project_dir)
    errors: list[str] = []
    warnings: list[str] = []
    task_path: Path | None = None
    statement_count = 0
    task_error: str | None = None
    try:
        task_path, payload = _native_task_file(project_dir)
        statement_count = len(_native_statement_ids(payload))
    except (FileNotFoundError, OSError, ValueError) as exc:
        task_error = str(exc)
        errors.append(task_error)
    if not package["matched"]:
        errors.append("application_package has no row for this local project path; refresh the designer package index")
    elif not package["application_id"]:
        errors.append("application_package.applicationId is empty; do not use the local directory name as app_id")
    elif not package["package_id"]:
        errors.append("application_package.packageId is empty; refresh the designer package before running")

    validation: dict[str, Any]
    try:
        validation = validate_rpa_project(paths, str(project_dir), strict=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        validation = {"valid": False, "errors": [str(exc)], "warnings": [], "project": str(project_dir)}
    errors.extend(item for item in validation.get("errors", []) if item not in errors)
    warnings.extend(item for item in validation.get("warnings", []) if item not in warnings)

    variable_info = (
        _native_variable_info_snapshot(project_dir)
        if task_path
        else {
            "path": str(project_dir / "variableInfo"),
            "exists": False,
            "valid": False,
            "top_level_type": None,
            "variable_map_type": None,
            "entry_count": 0,
            "error": "native task is missing",
        }
    )
    if not variable_info["exists"]:
        errors.append("variableInfo is missing; materialize the designer flow module before running")
    elif not variable_info["valid"]:
        errors.append("variableInfo.variableMap has the wrong shape; materialize it as [code_key, analysis_info] pairs")

    index = (
        _native_index_snapshot(project_dir, task_path)
        if task_path
        else {
            "index_file": str(project_dir / "file_index_700.json"),
            "index_exists": (project_dir / "file_index_700.json").is_file(),
            "index_valid": False,
            "module_key": None,
            "module_file": None,
            "module_exists": False,
            "index_task_time": None,
            "index_code_time": None,
            "task_mtime_ms": None,
            "code_mtime_ms": None,
            "task_index_fresh": False,
            "code_index_fresh": False,
        }
    )
    if not index["index_exists"]:
        errors.append("file_index_700.json is missing; materialize the designer flow module before running")
    elif not index["index_valid"]:
        errors.append("file_index_700.json is malformed")
    if index["index_valid"] and index["module_key"] and not index["module_exists"]:
        errors.append(f"flow module is missing: {index['module_key']}")
    if index["index_valid"] and not index["task_index_fresh"]:
        errors.append("file_index_700.json taskTime is older than the native .task file")
    if index["index_valid"] and index["module_exists"] and not index["code_index_fresh"]:
        errors.append("file_index_700.json codeTime is older than the flow module")

    consistency = (
        _native_designer_state_consistency(project_dir, task_path)
        if task_path
        else {
            "valid": False,
            "conflict": bool(task_error),
            "errors": [task_error] if task_error else [],
            "missing_files": [],
        }
    )
    for detail in consistency.get("errors", []):
        conflict_detail = f"designer_state_conflict: {detail}"
        if conflict_detail not in errors:
            errors.append(conflict_detail)

    runtime_log_paths = sorted(
        (paths.data_root / "logs").rglob("II.RPA.RuntimeHost.txt*") if (paths.data_root / "logs").is_dir() else [],
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    plugin_state = "unknown"
    plugin_error_code: str | None = None
    plugin_evidence: str | None = None
    if runtime_log_paths:
        try:
            tail = runtime_log_paths[0].read_bytes()[-131072:].decode("utf-8", errors="replace")
            plugin_lines = [
                line
                for line in tail.splitlines()
                if any(
                    marker in line
                    for marker in (
                        "010400100000996",
                        "010400100610996",
                        "010400100000964",
                        "检测到未启用【实在智能 RPA 插件】",
                        "检测到未安装【实在智能 RPA 插件】",
                    )
                )
            ]
            if any("000996" in line or "610996" in line or "未启用" in line for line in plugin_lines):
                plugin_state = "disabled"
                plugin_error_code = "010400100000996"
            elif plugin_lines:
                plugin_state = "not_installed"
                plugin_error_code = "010400100000964"
            if plugin_lines:
                plugin_evidence = str(redact_payload(plugin_lines[-1]))
        except OSError:
            pass
    plugin_unavailable = plugin_state in {"disabled", "not_installed"}
    runtime = {
        "runtime_host_log": str(runtime_log_paths[0]) if runtime_log_paths else None,
        "runtime_host_log_exists": bool(runtime_log_paths),
        "design_capture_status": "separate_channel",
        "runtime_browser_plugin_status": plugin_state,
        "runtime_browser_plugin_ready": False if plugin_unavailable else None,
        "runtime_browser_plugin_error_code": plugin_error_code,
        "runtime_browser_plugin_evidence": plugin_evidence,
        "runtime_verified": False,
        "ready": not plugin_unavailable,
        "message": (
            "Chrome 已安装实在智能 RPA 插件，但运行日志显示插件未启用；请在 Chrome 扩展管理中启用，并保持目标网页打开。"
            if plugin_state == "disabled"
            else "运行日志显示实在智能 RPA 插件未安装；请在设置中心 > 工具插件中安装。"
            if plugin_state == "not_installed"
            else "尚无运行期浏览器插件握手证据；设计期 DeepCapture 正常不代表运行期插件已就绪，首次运行将进行实时探测。"
        ),
    }
    if plugin_unavailable:
        errors.append(f"RuntimeHost 最新日志显示 Chrome RPA 运行插件状态为 {plugin_state}")
    materialization_required = bool(task_path) and bool(
        not variable_info["valid"]
        or not index["index_valid"]
        or not index["module_exists"]
        or not index["task_index_fresh"]
        or not index["code_index_fresh"]
        or consistency.get("conflict")
        or consistency.get("missing_files")
    )
    can_attempt_run = bool(
        validation.get("valid")
        and task_path
        and package["application_id"]
        and package["package_id"]
        and variable_info["valid"]
        and index["index_valid"]
        and index["module_exists"]
        and index["task_index_fresh"]
        and index["code_index_fresh"]
        and not consistency.get("conflict")
        and not consistency.get("missing_files")
    )
    can_run = bool(can_attempt_run and runtime["ready"])
    return {
        "kind": "native_rpa_runtime_diagnosis",
        "project": str(project_dir),
        "package": package,
        "task": {
            "path": str(task_path) if task_path else None,
            "node_id": task_path.stem if task_path else None,
            "statement_count": statement_count,
            "error": task_error,
        },
        "index": index,
        "designer_state": consistency,
        "runtime": runtime,
        "variable_info": variable_info,
        "validation": validation,
        "errors": errors,
        "warnings": warnings,
        "can_run": can_run,
        "can_attempt_run": can_attempt_run,
        "materialization_required": materialization_required,
        "next_tool": (
            "shizai_rpa_run"
            if can_run
            else "shizai_rpa_sync_diagnose"
            if plugin_unavailable
            else "shizai_rpa_materialize"
            if materialization_required
            else "shizai_rpa_validate_project"
        ),
        "next_action": (
            "调用 shizai_rpa_run；运行失败时先查看 shizai_rpa_status 和 runtime.message。"
            if can_run
            else "先在设计器设置中心安装并启用 Chrome RPA 运行插件，再重新诊断。"
            if plugin_unavailable
            else "调用 shizai_rpa_materialize(allow_write=true) 生成 projects/flow_modules 和 file_index_700.json，再重新诊断。"
            if materialization_required
            else "先修复 validation.errors，再重新校验项目。"
        ),
    }


def _native_runtime_log_snapshot(paths: ShizaiPaths) -> dict[str, int]:
    """Capture runtime log sizes before a run for incremental terminal detection."""
    log_root = paths.data_root / "logs"
    if not log_root.is_dir():
        return {}
    snapshot: dict[str, int] = {}
    for pattern in ("II.RPA.RuntimeHost.txt*", "run_*.jsonl"):
        for path in log_root.rglob(pattern):
            try:
                snapshot[str(path.resolve()).casefold()] = path.stat().st_size
            except OSError:
                continue
    return snapshot


def _native_runtime_plugin_blocker(paths: ShizaiPaths, baseline: dict[str, int]) -> dict[str, Any] | None:
    """Detect a plugin failure or run terminal emitted after the current run started."""
    log_root = paths.data_root / "logs"
    if not log_root.is_dir():
        return None
    candidates = [*log_root.rglob("II.RPA.RuntimeHost.txt*"), *log_root.rglob("run_*.jsonl")]
    for path in sorted(
        candidates,
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    ):
        try:
            resolved = str(path.resolve()).casefold()
            start = baseline.get(resolved, 0)
            size = path.stat().st_size
            if size <= start:
                continue
            with path.open("rb") as handle:
                handle.seek(start if start <= size else 0)
                content = handle.read()[-131072:].decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()
        # RuntimeHost logs the complete JavaScript source in a ``scriptCode``
        # field.  Our scripts intentionally contain the interaction markers
        # as error strings, so a source dump must never be treated as a real
        # login/challenge signal.  Only standalone/runtime result lines may
        # trigger the user-interaction branch below.
        interaction_lines = [
            line
            for line in lines
            if '"scriptCode"' not in line and "'scriptCode'" not in line
        ]
        interaction_reason = next(
            (
                reason
                for reason in ("login_required", "page_challenge")
                if any(
                    f"SHIZAI_WAITING_FOR_USER:{reason}" in line
                    for line in reversed(interaction_lines)
                )
            ),
            None,
        )
        if interaction_reason:
            evidence = next(
                line
                for line in reversed(interaction_lines)
                if f"SHIZAI_WAITING_FOR_USER:{interaction_reason}" in line
            )
            user_prompt = (
                "当前网页需要登录。请在现有浏览器窗口完成登录，确认后重新运行流程。"
                if interaction_reason == "login_required"
                else "当前网页需要完成人机验证。请在现有浏览器窗口完成验证，确认后重新运行流程。"
            )
            return {
                "kind": "user_interaction_required",
                "status": "waiting_for_user",
                "terminal_status": "error",
                "reason": interaction_reason,
                "message": user_prompt,
                "interaction": {
                    "required": True,
                    "status": "waiting_for_user",
                    "action": "login" if interaction_reason == "login_required" else "complete_challenge",
                    "pause_automation": True,
                    "automation_may_login": False,
                    "user_prompt": user_prompt,
                },
                "log_path": str(path),
                "evidence": str(redact_payload(evidence)),
            }
        disabled = next(
            (
                line
                for line in reversed(lines)
                if "010400100000996" in line or "010400100610996" in line or "检测到未启用【实在智能 RPA 插件】" in line
            ),
            None,
        )
        missing = next(
            (
                line
                for line in reversed(lines)
                if "010400100000964" in line or "检测到未安装【实在智能 RPA 插件】" in line
            ),
            None,
        )
        evidence = disabled or missing
        if not evidence:
            failure = next(
                (
                    line
                    for line in reversed(lines)
                    if '"engineAction":"error"' in line or '"runStatus":"error"' in line or '"level":"error"' in line
                ),
                None,
            )
            if failure:
                return {
                    "kind": "runtime_execution_failed",
                    "terminal_status": "error",
                    "message": "RuntimeHost 已写入本次运行错误。",
                    "log_path": str(path),
                    "evidence": str(redact_payload(failure)),
                }
            success = next(
                (line for line in reversed(lines) if '"runStatus":"success"' in line),
                None,
            )
            if success:
                return {
                    "kind": "runtime_completed",
                    "terminal_status": "success",
                    "message": "RuntimeHost 已写入本次运行成功终态。",
                    "log_path": str(path),
                    "evidence": str(redact_payload(success)),
                }
            continue
        state = "disabled" if disabled else "not_installed"
        return {
            "kind": "runtime_plugin_unavailable",
            "status": state,
            "terminal_status": "error",
            "error_code": "010400100000996" if disabled else "010400100000964",
            "message": (
                "Chrome 中的实在智能 RPA 插件未启用；请启用插件并保持目标网页打开。"
                if state == "disabled"
                else "实在智能 RPA 插件未安装；请在设置中心 > 工具插件中安装。"
            ),
            "log_path": str(path),
            "evidence": str(redact_payload(evidence)),
        }
    return None


def _native_run_evidence(
    log_lines: list[str],
    *,
    submitted_at: float | None = None,
) -> dict[str, Any]:
    """Extract terminal state and component details from runtime evidence.

    RuntimeHost stores run records in append-only files.  Ignore structured
    records older than the current submission so a previous successful run
    cannot complete the current status query.
    """
    evidence: dict[str, Any] = {
        "is_finished": False,
        "terminal_status": None,
        "error": None,
        "error_code": None,
        "component": None,
        "component_id": None,
        "component_sequence": None,
        "position": None,
    }
    for line in _native_current_log_lines(log_lines, submitted_at):
        lowered = line.casefold()
        if "isfinished: true" in lowered:
            evidence["is_finished"] = True
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            payload = None
        timestamp = payload.get("timestamp") if isinstance(payload, dict) else None
        if submitted_at and isinstance(timestamp, (int, float)) and timestamp < submitted_at * 1000 - 1000:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            run_status = str(data.get("runStatus") or "").strip()
            if run_status:
                normalized_status = run_status.casefold()
                if normalized_status in {
                    "error",
                    "failed",
                    "failure",
                    "success",
                    "succeeded",
                    "finished",
                    "completed",
                    "done",
                    "stopped",
                }:
                    evidence["terminal_status"] = normalized_status
                    evidence["is_finished"] = True
            message = str(data.get("text") or "").strip()
            if data.get("engineAction") == "error" or data.get("level") == "error":
                if message:
                    evidence["error"] = message
                error_code = str(data.get("errorCode") or "").strip()
                if error_code:
                    evidence["error_code"] = error_code
                position = data.get("position")
                if isinstance(position, list):
                    evidence["position"] = position
                error_stack = data.get("errorStack")
                if isinstance(error_stack, list):
                    rows = error_stack[0] if error_stack and isinstance(error_stack[0], list) else error_stack
                    if rows and isinstance(rows[0], dict):
                        row = rows[-1]
                        evidence["component"] = str(row.get("row_name") or "").strip() or None
                        evidence["component_id"] = str(row.get("row_id") or "").strip() or None
                        sequence = row.get("sequence")
                        evidence["component_sequence"] = str(sequence).strip() if sequence is not None else None
        if evidence["error"] is None and "运行错误" in line:
            message = re.search(r"运行错误[：:]\s*(.+)", line)
            if message:
                evidence["error"] = message.group(1).strip().strip("'\"")
        if evidence["error_code"] is None and evidence["error"]:
            code = re.search(r"\[(\d{9,})\]", evidence["error"])
            if code:
                evidence["error_code"] = code.group(1)
    if str(evidence["terminal_status"] or "").casefold() in {
        "error",
        "failed",
        "failure",
    }:
        evidence["is_finished"] = True
    return evidence


def _native_run_status(
    run_state: dict[str, Any] | None,
    output_path: Path | None,
    log_lines: list[str],
) -> str:
    """Infer a conservative status from the local marker, output, and logs."""
    if not run_state:
        return "never_started"
    log_lines = _native_current_log_lines(log_lines, float(run_state.get("submitted_at") or 0))
    if run_state.get("status") == "stopped":
        return "stopped"
    if run_state.get("status") in _NATIVE_CLEANUP_TERMINAL_STATUSES - {"stopped"}:
        return str(run_state.get("status"))
    if run_state.get("status") == "waiting_for_user":
        return "waiting_for_user"
    if run_state.get("status") == "stopping":
        evidence = _native_run_evidence(
            log_lines,
            submitted_at=float(run_state.get("submitted_at") or 0) or None,
        )
        if str(evidence.get("terminal_status") or "").casefold() == "stopped":
            return "stopped"
        cleanup_started_at = float(run_state.get("cleanup_started_at") or 0)
        if cleanup_started_at and time.time() - cleanup_started_at > _native_cleanup_timeout_seconds():
            return "cleanup_timeout"
        return "stopping"
    # A preflight block is terminal for this attempt. Historical RuntimeHost
    # logs from earlier runs must not turn the persisted blocked marker into a
    # misleading failed/succeeded status.
    if run_state.get("status") == "blocked" and run_state.get("failure_kind") in {
        "runtime_not_ready",
        "runtime_plugin_unavailable",
    }:
        return "blocked"
    lowered = "\n".join(log_lines).casefold()
    evidence = _native_run_evidence(
        log_lines,
        submitted_at=float(run_state.get("submitted_at") or 0) or None,
    )
    recorded_terminal = str(run_state.get("terminal_status") or "").casefold()
    observed_terminal = str(evidence.get("terminal_status") or "").casefold()
    if (
        recorded_terminal in {"error", "failed", "failure"}
        or observed_terminal
        in {
            "error",
            "failed",
            "failure",
        }
        or any(
            marker in lowered
            for marker in (
                "run-error",
                "bizexception",
                "流程为空",
                "服务器返回错误",
                "运行错误",
                "statechange state: error",
                "state: error",
                "componentvalidateexception",
                "数据类型错误",
            )
        )
    ):
        return "failed"
    if observed_terminal == "stopped" or recorded_terminal == "stopped":
        return "stopped"
    submitted_at = float(run_state.get("submitted_at") or 0)
    fresh_output = bool(
        submitted_at and output_path and output_path.is_file() and output_path.stat().st_mtime >= submitted_at - 1
    )
    if fresh_output:
        return "succeeded"
    if str(run_state.get("status") or "").casefold() == "succeeded":
        return "failed"
    terminal_status = observed_terminal or recorded_terminal
    if terminal_status in {"success", "succeeded", "finished", "completed", "done"}:
        # A runtime success without a fresh configured output is a failed
        # business run, even though the desktop engine itself stopped cleanly.
        return "failed"
    if any(marker in lowered for marker in ("run-success", "run-end", "run end", "执行完成")):
        return "succeeded" if fresh_output else "failed"
    submitted_at = float(run_state.get("submitted_at") or 0)
    if submitted_at and time.time() - submitted_at > _native_run_timeout_seconds():
        return "timeout"
    return str(run_state.get("status") or "submitted")


def _native_run_timeout_seconds() -> float:
    """Read the local polling timeout without allowing invalid configuration."""
    raw = os.environ.get("SHIZAI_RPA_RUN_TIMEOUT_SECONDS", "900")
    try:
        return max(10.0, float(raw))
    except (TypeError, ValueError):
        return 900.0


def _native_cleanup_timeout_seconds() -> float:
    """Bound the time a persisted stop request may remain unresolved."""
    raw = os.environ.get("SHIZAI_RPA_CLEANUP_TIMEOUT_SECONDS", str(_NATIVE_CLEANUP_TIMEOUT_DEFAULT))
    try:
        return min(120.0, max(1.0, float(raw)))
    except (TypeError, ValueError):
        return _NATIVE_CLEANUP_TIMEOUT_DEFAULT


def _native_stop_is_idempotent(detail: str) -> bool:
    """Recognize the designer responses that mean the runtime is already down."""
    lowered = str(detail).casefold()
    return any(
        marker in lowered
        for marker in (
            "3001",
            "not running",
            "no active",
            "没有正在运行",
            "未运行",
            "当前状态不可停止",
            "没有活动运行",
        )
    )


def _native_close_runtime_mask(caller: Any) -> dict[str, Any]:
    """Best-effort close of the production mask after a run ends.

    Custom test clients and lightweight integrations do not expose the native
    UI channel. They still get correct runtime cleanup; only the real desktop
    client can perform this renderer-to-renderer cleanup.
    """
    if not isinstance(caller, ShizaiDesktopAgent):
        return {"attempted": False, "status": "unsupported_client"}
    from .native_ui import NativeMask

    try:
        result = NativeMask(client=caller).close(force=True)
    except Exception as exc:
        return {"attempted": True, "status": "failed", "error": str(exc)}
    if result.get("visible") is not False or result.get("close_warning"):
        return {
            "attempted": True,
            "status": "failed",
            "error": result.get("close_warning") or "Native mask cleanup was not acknowledged",
            "result": redact_payload(result),
        }
    return {"attempted": True, "status": "complete", "result": redact_payload(result)}


def _native_stop_designer_run(caller: Any) -> dict[str, Any]:
    """Finish the designer-owned run-state transition when possible.

    Native RPA is submitted through CorpBrainService, but the desktop status
    indicator is owned by AppRunService. A designer-created run can therefore
    leave the indicator in a stopping state unless its native stop path is
    attempted too. RuntimeHost cleanup remains authoritative.
    """
    if not isinstance(caller, ShizaiDesktopAgent):
        return {"attempted": False, "status": "unsupported_client"}
    result: Any = None
    app_run_status = "complete"
    try:
        result = caller.call("AppRunService", "stop", [{}])
    except Exception as exc:
        if _native_stop_is_idempotent(str(exc)):
            app_run_status = "already_stopped"
            result = str(exc)
        else:
            app_run_status = "warning"
            result = {"error": str(exc)}
    if isinstance(result, dict) and result.get("isSuccess") is False:
        detail = str(result.get("message") or "AppRunService stop failed")
        code = result.get("code")
        if code not in (None, ""):
            detail = f"{detail} (code={code})"
        if _native_stop_is_idempotent(detail):
            app_run_status = "already_stopped"
        else:
            app_run_status = "warning"
            result = {"result": redact_payload(result), "error": detail}

    # The run float window is updated through the electron-main TrayManager.
    # This final event clears a stale request-stop/stopping label even when
    # AppRunService belongs to a different run mode.
    try:
        tray_result = caller.call("TrayManager", "runStateChange", ["stopped"])
    except Exception as exc:
        return {
            "attempted": True,
            "status": "warning" if app_run_status == "complete" else app_run_status,
            "app_run_status": app_run_status,
            "app_run_result": redact_payload(result),
            "tray_status": "warning",
            "tray_error": str(exc),
        }
    return {
        "attempted": True,
        "status": app_run_status,
        "app_run_status": app_run_status,
        "app_run_result": redact_payload(result),
        "tray_status": "complete",
        "tray_result": redact_payload(tray_result),
    }


def _native_cleanup_runtime(
    paths: ShizaiPaths,
    project_dir: Path,
    *,
    caller: Any,
    reason: str,
    state: dict[str, Any] | None = None,
    close_mask: bool = True,
) -> dict[str, Any]:
    """Stop a native runtime and persist a non-stuck cleanup outcome.

    ``workflowDebugStop`` is the only supported way to stop RuntimeHost. The
    local marker enters ``stopping`` before the RPC, then always leaves that
    state as ``stopped`` or an explicit cleanup failure/timeout. A successful
    stop RPC is recorded as provider confirmation; a matching RuntimeHost log
    record, when available, is recorded as stronger runtime confirmation.
    """
    current = dict(state or _read_native_run_state(paths, project_dir) or {})
    current.setdefault("project", str(project_dir))
    started_at = time.time()
    current.update(
        {
            "status": "stopping",
            "cleanup_status": "requested",
            "cleanup_started_at": started_at,
            "cleanup_reason": reason,
            "cleanup_errors": [],
        }
    )
    _write_native_run_state(paths, project_dir, current)
    _publish_rpa_ui(
        "runtime_stopping",
        "computer_control",
        "正在停止 Native RPA，并清理运行会话。",
        animation="native_rpa_stopping",
        overlay_state="unknown",
        flow_ref=str(project_dir),
        status="stopping",
        run_uuid=current.get("uuid"),
    )

    errors: list[str] = []
    already_stopped = False
    stop_rpc_result: Any = None
    stop_rpc_called = False
    designer_run_cleanup = (
        _native_stop_designer_run(caller)
        if close_mask
        else {"attempted": False, "status": "skipped"}
    )
    try:
        stop_rpc_called = True
        stop_rpc_result = caller.call("CorpBrainService", "workflowDebugStop", [])
        if isinstance(stop_rpc_result, dict) and stop_rpc_result.get("isSuccess") is False:
            detail = str(stop_rpc_result.get("message") or "Native RPA stop RPC failed")
            code = stop_rpc_result.get("code")
            if code not in (None, ""):
                detail = f"{detail} (code={code})"
            if _native_stop_is_idempotent(detail):
                already_stopped = True
            else:
                errors.append(detail)
    except Exception as exc:
        detail = str(exc)
        if _native_stop_is_idempotent(detail):
            already_stopped = True
        else:
            errors.append(detail)

    mask_cleanup = _native_close_runtime_mask(caller) if close_mask else {"attempted": False, "status": "skipped"}
    if mask_cleanup.get("status") == "failed":
        errors.append(f"runtime mask cleanup failed: {mask_cleanup.get('error')}")

    stop_confirmed = bool(stop_rpc_called and not errors) or already_stopped
    log_lines = _native_runtime_log_lines(paths, current)
    evidence = _native_run_evidence(
        log_lines,
        submitted_at=float(current.get("submitted_at") or 0) or None,
    )
    runtime_confirmed = str(evidence.get("terminal_status") or "").casefold() == "stopped"
    cleanup_complete = bool(stop_confirmed and not errors)
    finished_at = time.time()
    final_status = "stopped" if stop_confirmed else "cleanup_timeout"
    cleanup_status = (
        "complete"
        if cleanup_complete
        else "partial"
        if stop_confirmed
        else "failed"
    )
    current.update(
        {
            "status": final_status,
            "cleanup_status": cleanup_status,
            "cleanup_complete": cleanup_complete,
            "cleanup_completed_at": finished_at,
            "stopped_at": finished_at if stop_confirmed else None,
            "cleanup_errors": errors,
            "stop_rpc_result": redact_payload(stop_rpc_result),
            "runtime_confirmation": "runtime_log" if runtime_confirmed else "stop_rpc_ack" if stop_confirmed else None,
            "runtime_evidence": redact_payload(evidence),
            "mask_cleanup": mask_cleanup,
            "designer_run_cleanup": designer_run_cleanup,
        }
    )
    _write_native_run_state(paths, project_dir, current)
    if final_status == "stopped":
        message = (
            "Native RPA 已停止，运行会话已释放。"
            if cleanup_complete
            else "Native RPA 已停止，但运行遮罩清理返回警告。"
        )
        phase = "idle"
        animation = "idle"
        overlay_state = "inactive" if mask_cleanup.get("status") != "failed" else "unknown"
    else:
        message = "Native RPA 停止请求未得到确认；请稍后重试 rpa-stop。"
        phase = "runtime_cleanup_timeout"
        animation = "native_rpa_cleanup_timeout"
        overlay_state = "unknown"
    ui_feedback = _publish_rpa_ui(
        phase,
        "computer_control",
        message,
        animation=animation,
        overlay_state=overlay_state,
        flow_ref=str(project_dir),
        status=final_status,
        cleanup_status=cleanup_status,
        run_uuid=current.get("uuid"),
    )
    return {
        "kind": "native_rpa_cleanup",
        "project": str(project_dir),
        "status": final_status,
        "stopped": bool(stop_confirmed and not already_stopped),
        "already_stopped": already_stopped,
        "cleanup_complete": cleanup_complete,
        "cleanup_status": cleanup_status,
        "cleanup_started_at": started_at,
        "cleanup_completed_at": finished_at,
        "cleanup_errors": errors,
        "stop_rpc_result": redact_payload(stop_rpc_result),
        "runtime_confirmation": current.get("runtime_confirmation"),
        "runtime_evidence": evidence,
        "mask_cleanup": mask_cleanup,
        "designer_run_cleanup": designer_run_cleanup,
        "ui_feedback": ui_feedback,
    }


def _requested_output_row_count(flow: dict[str, Any]) -> int:
    """Return only a row target that came from the user's analyzed request."""
    analysis = flow.get("request_analysis")
    evidence = analysis.get("evidence") if isinstance(analysis, dict) else None
    collection = evidence.get("collection") if isinstance(evidence, dict) else None
    if isinstance(collection, dict) and "max_rows" in collection:
        raw_value = collection.get("max_rows")
        if raw_value in (None, ""):
            return 0
        try:
            return max(0, int(raw_value))
        except (TypeError, ValueError):
            return 0
    try:
        return max(0, int(flow.get("max_rows") or 0))
    except (TypeError, ValueError):
        return 0


def native_rpa_status(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Return local status and evidence for the last native RPA run."""
    project_dir = resolve_project(paths, project_ref)
    diagnosis = native_rpa_sync_diagnose(paths, str(project_dir))
    flow = _native_rpa_state(paths, str(project_dir))
    run_state = _read_native_run_state(paths, project_dir)
    output_file = str(flow.get("output_file") or "result.xlsx")
    configured_output_path = _native_configured_output_path(flow.get("output_path"))
    # The expected output is intentionally resolved separately from the
    # diagnostic artifact list.  A previous-run workbook must never make a
    # newly submitted run look successful.
    output_path = _native_expected_output_path(project_dir, output_file, configured_output_path) or configured_output_path
    output_files = _native_output_candidates(project_dir, output_file, configured_output_path)
    log_lines = _native_runtime_log_lines(paths, run_state)
    status = _native_run_status(run_state, output_path, log_lines)
    runtime_evidence = _native_run_evidence(
        log_lines,
        submitted_at=float((run_state or {}).get("submitted_at") or 0) or None,
    )
    if not diagnosis.get("runtime", {}).get("ready", True) and status in {"submitted", "running", "timeout"}:
        status = "blocked"
    expected_columns = [
        str(item).strip() for item in flow.get("required_columns", DEFAULT_RPA_COLUMNS) if str(item).strip()
    ]
    output_details = [_native_output_summary(project_dir, item, run_state, expected_columns) for item in output_files]
    expected_output = next(
        (item for item in output_details if item["path"] == str(output_path)),
        None,
    )
    # Keep a configured-but-not-yet-created output visible to the caller.  A
    # missing file is normal before the first run (and actionable after a
    # failed run); it must not turn status polling into a filesystem exception.
    if output_path is not None and expected_output is None:
        expected_output = _native_output_summary(project_dir, output_path, run_state, expected_columns)
        output_details.insert(0, expected_output)
    quality_warning: str | None = None
    failure_kind: str | None = (
        str((run_state or {}).get("failure_kind")) if (run_state or {}).get("failure_kind") else None
    )
    actual_columns = [str(item).strip() for item in (expected_output or {}).get("columns", []) if str(item).strip()]
    missing_columns = [item for item in expected_columns if item not in actual_columns]
    extra_columns = [item for item in actual_columns if item not in expected_columns]
    output_quality = (expected_output or {}).get("quality") or {}
    empty_required_columns = list(output_quality.get("empty_required_columns") or [])
    complete_required_row_count = output_quality.get("complete_required_row_count")
    invalid_link_rows = int(output_quality.get("invalid_link_rows") or 0)
    expected_data_row_count = _requested_output_row_count(flow)
    runtime_terminal = str(runtime_evidence.get("terminal_status") or "").casefold()
    fresh_output = bool(expected_output and expected_output.get("fresh_for_run"))
    if status == "failed" and (
        runtime_terminal in {"success", "succeeded", "finished", "completed", "done"}
        or str((run_state or {}).get("status") or "").casefold() == "succeeded"
    ):
        if not fresh_output:
            failure_kind = "missing_fresh_output"
            quality_warning = (
                "RuntimeHost 已报告运行成功，但本次运行没有生成新的配置输出文件；"
                "请检查结果页捕获、采集绑定和 Excel 写入步骤。"
            )
    if status == "succeeded" and expected_output:
        if expected_output.get("format_valid") is False:
            failure_kind = "invalid_output_format"
            quality_warning = "流程运行结束，但输出文件格式校验失败。"
        elif expected_output.get("data_row_count") == 0:
            failure_kind = "empty_output"
            quality_warning = "流程运行结束，但输出文件只有表头或没有有效数据行；请重新校验商品字段绑定。"
        elif expected_data_row_count and int(expected_output.get("data_row_count") or 0) < expected_data_row_count:
            failure_kind = "output_row_count_mismatch"
            quality_warning = (
                f"流程要求至少输出 {expected_data_row_count} 条数据，实际得到 "
                f"{expected_output.get('data_row_count')} 条；当前结果不计为成功。"
            )
        elif empty_required_columns:
            failure_kind = "empty_required_columns"
            quality_warning = (
                "流程运行结束且已写出数据，但必填列全为空："
                + "、".join(empty_required_columns)
                + "；请重新检查对应结果页元素和采集绑定。"
            )
        elif isinstance(complete_required_row_count, int) and complete_required_row_count < int(
            expected_output.get("data_row_count") or 0
        ):
            failure_kind = "incomplete_required_rows"
            quality_warning = (
                "流程运行结束且已写出数据，但部分数据行缺少必填字段："
                f"完整行 {complete_required_row_count}/{expected_output.get('data_row_count')}；"
                "请检查结果页元素和采集绑定。"
            )
        elif invalid_link_rows:
            failure_kind = "invalid_links"
            quality_warning = f"输出中有 {invalid_link_rows} 条链接不是有效详情页地址；当前结果不计为成功。"
    if expected_output and expected_output.get("columns") and expected_columns:
        if expected_columns and actual_columns != expected_columns:
            failure_kind = failure_kind or "output_columns_mismatch"
            quality_warning = (
                (quality_warning + " ") if quality_warning else ""
            ) + f"输出列与流程契约不一致，期望 {expected_columns}，实际 {actual_columns}。"
    if status == "succeeded" and failure_kind:
        status = "failed"
    last_known_page = _native_last_known_page(paths, project_dir, flow)
    collection_step = next(
        (
            item
            for item in flow.get("steps", [])
            if isinstance(item, dict) and item.get("component") == "WebBrowser_DataCollectionV5"
        ),
        None,
    )
    collection_bindings = list(collection_step.get("element_ids", [])) if isinstance(collection_step, dict) else []
    collection_diagnostic: dict[str, Any] | None = None
    if (
        status in {"succeeded", "failed"}
        and expected_output
        and expected_output.get("data_row_count") == 0
        and failure_kind in {None, "empty_output", "output_row_count_mismatch"}
    ):
        collection_diagnostic = {
            "kind": "empty_collection_result",
            "message": "运行状态成功但采集没有产生数据行；这通常表示结果页尚未完成渲染，或采集列的 selector 没有匹配到当前页面。",
            "expected_columns": expected_columns,
            "expected_data_row_count": expected_data_row_count or None,
            "actual_columns": actual_columns,
            "missing_columns": missing_columns,
            "collection_binding_count": len(collection_bindings),
            "collection_binding_ids": collection_bindings,
            "last_known_page": last_known_page,
            "recovery": {
                "next_tool": "shizai_rpa_runtime_preflight",
                "then": ["shizai_rpa_scan_page", "shizai_rpa_capture_elements", "shizai_rpa_set_step_elements"],
                "capture_arguments": {
                    "allow_rebind": True,
                    "reason": "页面身份已变化，明确允许替换当前 step_id 的旧绑定。",
                },
                "rule": "保留当前 Chrome 和已创建流程，先确认当前 URL/title 与结果页一致，再重新截图/扫描并替换过期元素；不要重建 WebBrowser_CreateV3。",
            },
        }
    runtime_terminal = runtime_evidence.get("terminal_status")
    if not runtime_terminal and status in {"succeeded", "failed", "stopped", "timeout"}:
        runtime_evidence["terminal_status"] = {
            "succeeded": "success",
            "failed": "error",
            "stopped": "stopped",
            "timeout": "timeout",
        }.get(status)
    if run_state:
        # Keep the persisted marker and the agent-facing status coherent. The
        # original submitted RPC result remains in ``rpc_result`` for audit.
        if run_state.get("status") != status:
            run_state["status"] = status
        if status == "stopped" and "cleanup_status" not in run_state:
            # Older CLI versions wrote stopped immediately after the RPC and
            # had no cleanup evidence fields. Keep that history visible
            # without upgrading it to a false cleanup confirmation.
            run_state["cleanup_status"] = "legacy_terminal"
            run_state.setdefault("cleanup_complete", None)
        if failure_kind:
            run_state["failure_kind"] = failure_kind
        if status in {"succeeded", "failed", "stopped", "timeout", "cleanup_timeout", "cleanup_failed"}:
            run_state.setdefault("completed_at", time.time())
        if status == "cleanup_timeout":
            run_state["cleanup_status"] = "timeout"
            run_state.setdefault("cleanup_errors", ["停止请求超过清理确认窗口"])
        _write_native_run_state(paths, project_dir, run_state)
    status_explanation = {
        "never_started": "尚未提交运行。",
        "submitted": "RPC 已接受，等待 RuntimeHost 日志或输出文件。",
        "running": "RuntimeHost 仍在执行，继续轮询状态。",
        "succeeded": "检测到完成日志且本次运行生成了新输出文件。",
        "failed": "检测到运行失败证据；先查看 log_evidence、output 和 diagnosis。",
        "stopped": "运行已被停止。",
        "stopping": "已发出停止请求，正在等待 RuntimeHost 释放运行会话。",
        "cleanup_timeout": "停止请求未在清理确认窗口内完成；运行状态未被伪装成已停止，请重试 rpa-stop。",
        "cleanup_failed": "运行已结束，但清理过程失败；请查看 cleanup_errors 后重试 rpa-stop。",
        "timeout": "超过本地轮询超时，未发现完成或错误证据；确认 RuntimeHost 日志后再决定重试。",
        "blocked": "运行前置条件未满足，当前运行没有可用的 RuntimeHost 插件证据。",
        "waiting_for_user": "流程已暂停，等待用户在当前浏览器窗口完成登录或人机验证。",
    }.get(status, "状态来自本地运行标记和日志证据。")
    quality_failure_kinds = {
        "missing_fresh_output",
        "invalid_output_format",
        "empty_output",
        "output_row_count_mismatch",
        "empty_required_columns",
        "incomplete_required_rows",
        "invalid_links",
        "output_columns_mismatch",
    }
    if failure_kind in quality_failure_kinds:
        status_explanation = quality_warning or status_explanation
    if status in {"submitted", "running"}:
        ui_message = "正在操作你的电脑：Native RPA 正在运行，等待 RuntimeHost 和输出文件。"
        ui_phase = "runtime_running"
        ui_animation = "native_rpa_running"
    elif status == "succeeded":
        ui_message = "电脑操作已完成：已检测到本次运行生成的输出文件。"
        ui_phase = "runtime_completed"
        ui_animation = "native_rpa_complete"
    elif failure_kind == "missing_fresh_output":
        ui_message = "电脑操作已结束，但没有发现本次运行生成的新输出文件。"
        ui_phase = "runtime_failed"
        ui_animation = "native_rpa_failed"
    elif failure_kind in quality_failure_kinds:
        ui_message = "电脑操作已结束，但输出数据未达到流程质量契约。"
        ui_phase = "runtime_failed"
        ui_animation = "native_rpa_failed"
    elif status == "failed":
        ui_message = "电脑操作失败：RuntimeHost 返回了运行错误，请检查日志。"
        ui_phase = "runtime_failed"
        ui_animation = "native_rpa_failed"
    elif status == "blocked":
        ui_message = "运行已暂停：运行环境未就绪，暂未继续操作电脑。"
        ui_phase = "runtime_blocked"
        ui_animation = "native_rpa_blocked"
    elif status == "waiting_for_user":
        ui_message = str(
            ((run_state or {}).get("interaction") or {}).get("user_prompt")
            or "流程已暂停，等待用户在当前浏览器窗口完成交互。"
        )
        ui_phase = "runtime_waiting"
        ui_animation = "native_rpa_waiting"
    elif status == "stopping":
        ui_message = "正在停止 Native RPA：等待 RuntimeHost 释放运行会话。"
        ui_phase = "runtime_stopping"
        ui_animation = "native_rpa_stopping"
    elif status in {"cleanup_timeout", "cleanup_failed"}:
        ui_message = status_explanation
        ui_phase = "runtime_cleanup_timeout"
        ui_animation = "native_rpa_cleanup_timeout"
    elif status == "stopped":
        ui_message = "电脑操作已停止，当前 Chrome 会话仍可复用。"
        ui_phase = "idle"
        ui_animation = "idle"
    else:
        ui_message = "当前流程还没有运行记录。"
        ui_phase = "idle"
        ui_animation = "idle"
    ui_feedback = _publish_rpa_ui(
        ui_phase,
        "computer_control",
        ui_message,
        animation=ui_animation,
        overlay_state="unknown" if status in {"stopping", "cleanup_timeout", "cleanup_failed"} else "inactive",
        flow_ref=str(project_dir),
        status=status,
        run_uuid=(run_state or {}).get("uuid"),
        failure_kind=failure_kind,
    )
    next_tool = (
        "shizai_rpa_status"
        if status in {"submitted", "running", "stopping"}
        else "shizai_rpa_stop"
        if status in {"timeout", "cleanup_timeout", "cleanup_failed"}
        else "shizai_rpa_runtime_preflight"
        if failure_kind in quality_failure_kinds or status == "waiting_for_user"
        else "shizai_rpa_sync_diagnose"
        if not diagnosis.get("can_run", False)
        else "shizai_rpa_run"
    )
    return {
        "kind": "native_rpa_status",
        "project": str(project_dir),
        "status": status,
        "run": run_state,
        "output": {
            "path": str(output_path) if output_path else None,
            "exists": bool((expected_output or {}).get("exists")),
            "size": int((expected_output or {}).get("size") or 0),
            "format_valid": expected_output.get("format_valid") if expected_output else None,
            "columns": expected_output.get("columns", []) if expected_output else [],
            "row_count": expected_output.get("row_count") if expected_output else None,
            "data_row_count": expected_output.get("data_row_count") if expected_output else None,
            "expected_data_row_count": expected_data_row_count or None,
            "has_data": bool(expected_output and expected_output.get("has_data")),
            "expected_columns": expected_columns,
            "missing_columns": missing_columns,
            "extra_columns": extra_columns,
            "column_count": len(actual_columns),
            "quality": output_quality or None,
            "quality_warning": quality_warning,
        },
        "output_files": output_details,
        "log_evidence": log_lines,
        "runtime_evidence": runtime_evidence,
        "failure_kind": failure_kind,
        "cleanup_status": (run_state or {}).get("cleanup_status"),
        "cleanup_complete": (run_state or {}).get("cleanup_complete"),
        "cleanup_errors": (run_state or {}).get("cleanup_errors", []),
        "quality_warning": quality_warning,
        "collection_diagnostic": collection_diagnostic,
        "last_known_page": last_known_page,
        "diagnosis": diagnosis,
        "next_tool": next_tool,
        "status_explanation": status_explanation,
        "ui_feedback": ui_feedback,
        "interaction_required": status == "waiting_for_user",
        "interaction": (run_state or {}).get("interaction"),
    }


def _publish_native_run_blocked(paths: ShizaiPaths, project_dir: Path, detail: str) -> None:
    """Persist a preflight block so the next status call remains truthful."""
    _write_native_run_state(
        paths,
        project_dir,
        {
            "project": str(project_dir),
            "status": "blocked",
            "failure_kind": "runtime_not_ready",
            "error": detail,
            "blocked_at": time.time(),
        },
    )
    _publish_rpa_ui(
        "runtime_blocked",
        "computer_control",
        f"运行已暂停：运行环境未就绪，暂未操作电脑。{detail}",
        animation="native_rpa_blocked",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        status="blocked",
        failure_kind="runtime_not_ready",
    )


def _publish_native_editor_block(paths: ShizaiPaths, project_dir: Path, detail: str) -> None:
    """Persist an unsaved-designer block so the next status call stays truthful."""
    _write_native_run_state(
        paths,
        project_dir,
        {
            "project": str(project_dir),
            "status": "blocked",
            "failure_kind": "editor_unsaved_changes",
            "error": detail,
            "blocked_at": time.time(),
        },
    )
    _publish_rpa_ui(
        "runtime_blocked",
        "design_validation",
        f"运行已暂停：designer 有未保存改动，暂未操作电脑。{detail}",
        animation="native_rpa_blocked",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        status="blocked",
        failure_kind="editor_unsaved_changes",
    )


def native_rpa_run(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    input_param: dict[str, Any] | None = None,
    run_uuid: str | None = None,
    workflow_run_id: str | None = None,
    start_take_work: bool = False,
    wait: bool = True,
    wait_seconds: float | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Start a validated native RPA task through the designer debug RPC.

    The default waits for the desktop channel's terminal tray state so the
    client can continue servicing render requests. ``wait=False`` is retained
    for callers that only need submission acknowledgement.
    """
    project_dir = resolve_project(paths, project_ref)
    editor_block = _unsaved_editor_block(project_dir, client)
    if editor_block is not None:
        _publish_native_editor_block(paths, project_dir, str(editor_block["message"]))
        return editor_block
    if input_param is not None and not isinstance(input_param, dict):
        raise ValueError("input_param must be a JSON object")
    # Older MCP-created flows may already contain a complete plan but predate
    # the browser lifecycle finalizer. Repair them immediately before the
    # first runtime diagnosis so this run cannot create another orphan tab.
    browser_cleanup = ensure_native_browser_cleanup_step(
        paths,
        str(project_dir),
        client=client,
    )
    if browser_cleanup.get("error_code") == "editor_unsaved_changes":
        return browser_cleanup
    # Ensure a concrete SQLite target survives a clean checkout or a deleted
    # previous output before RuntimeHost reaches Database_DBConnect.
    try:
        _, payload_for_db = _native_task_file(project_dir)
    except FileNotFoundError:
        # Let the normal sync diagnosis report the actionable application_package
        # or missing-task error.  Database preparation must never mask that
        # preflight result for a malformed/incomplete native project.
        payload_for_db = {}
    entities_for_db = payload_for_db.get("componentModelEntities")
    if isinstance(entities_for_db, dict):
        for node_for_db in entities_for_db.values():
            if not isinstance(node_for_db, dict) or node_for_db.get("type") != "Database_DBConnect":
                continue
            _ensure_sqlite_database_file(
                {
                    "dtype": _native_task_argument(node_for_db, "dtype", ""),
                    "DatabaseConfig": _native_task_argument(node_for_db, "DatabaseConfig", []),
                },
                project_dir=project_dir,
            )
    try:
        task_path, _ = _normalize_native_page_scope_js(project_dir)
        _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    except (FileNotFoundError, OSError, ValueError):
        # The diagnostic below returns the actionable native-task error.
        pass
    diagnosis = native_rpa_sync_diagnose(paths, str(project_dir))
    if not diagnosis.get("can_attempt_run", diagnosis["can_run"]):
        detail = "; ".join(diagnosis["errors"] or diagnosis["warnings"] or ["native RPA project is not ready"])
        _publish_native_run_blocked(paths, project_dir, detail)
        raise ValueError(f"native RPA is not ready to run: {detail}")
    # Design validation and production execution share the native AppRun
    # runtime. A stale DAP session can accept a production request while
    # preventing every component from starting, so release it after the basic
    # project diagnosis succeeds. This does not touch DeepCapture.
    from . import debug_session as debug_core

    debug_capabilities = debug_core.debug_capabilities(paths, str(project_dir))
    design_validation_cleanup: dict[str, Any] = {
        "active_before_run": bool(debug_capabilities.get("active_session")),
        "cleanup_required_before_run": bool(debug_capabilities.get("cleanup_required")),
        "session_status_before_run": debug_capabilities.get("session_status"),
        "deep_capture_touched": False,
        "stopped": False,
    }
    if debug_capabilities.get("active_session") or debug_capabilities.get("cleanup_required"):
        stopped_debug = debug_core.debug_stop(paths, str(project_dir), client=client)
        cleanup_errors = stopped_debug.get("errors") or []
        cleanup_complete = bool(stopped_debug.get("cleanup_complete", not cleanup_errors))
        design_validation_cleanup.update(
            {
                "stopped": bool(stopped_debug.get("stopped")) and cleanup_complete,
                "cleanup_complete": cleanup_complete,
                "errors": cleanup_errors,
            }
        )
        if not cleanup_complete:
            detail = "; ".join(str(item) for item in cleanup_errors) or "native debug cleanup failed"
            raise ValueError(f"unable to stop the active design-validation session before production run: {detail}")
    task_path = Path(str(diagnosis["task"]["path"]))
    task_path, _ = _normalize_native_page_scope_js(project_dir)
    _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    _sync_native_designer_content(paths, project_dir, task_path, client=client)
    diagnosis = native_rpa_sync_diagnose(paths, str(project_dir))
    if not diagnosis.get("can_attempt_run", diagnosis["can_run"]):
        detail = "; ".join(
            diagnosis["errors"] or diagnosis["warnings"] or ["native RPA project is not ready after index sync"]
        )
        _publish_native_run_blocked(paths, project_dir, detail)
        raise ValueError(f"native RPA is not ready to run after index sync: {detail}")
    application_id = diagnosis["package"]["application_id"]
    package_id = diagnosis["package"]["package_id"]
    task_node_id = diagnosis["task"]["node_id"]
    serialized_input_param = json.dumps(input_param, ensure_ascii=False, separators=(",", ":")) if input_param else ""
    request = {
        "app_state": "local",
        "uuid": run_uuid or f"shizai-rpa-{uuid.uuid4().hex}",
        "app_id": application_id,
        "workflow_run_id": workflow_run_id or f"shizai-rpa-run-{uuid.uuid4().hex}",
        # RuntimeHost expects this field to be a JSON string. An empty object
        # sent as a Python object makes its global-variable setup dereference
        # an undefined fileVarList before the flow is loaded.
        "inputParam": serialized_input_param,
        "node_id": task_node_id,
        "applicationId": application_id,
        "start_take_work": bool(start_take_work),
    }
    run_state = {
        "status": "submitted",
        "submitted_at": time.time(),
        "project": str(project_dir),
        "application_id": application_id,
        "package_id": package_id,
        "node_id": task_node_id,
        "uuid": request["uuid"],
        "workflow_run_id": request["workflow_run_id"],
        "request": redact_payload(request),
        "design_validation_cleanup": design_validation_cleanup,
        "browser_cleanup": browser_cleanup,
    }
    # Excel_Cs_NewV4 rejects an existing workbook.  Resolve names from the
    # materialized native flow, including the explicit Excel component's
    # ``name`` argument, then move each exact output aside before submitting.
    # The diagnosis payload intentionally contains readiness data rather than
    # the whole flow, so do not read a non-existent ``diagnosis["flow"]`` key.
    flow_state = _native_rpa_state(paths, str(project_dir))
    configured_output_path = _native_configured_output_path(flow_state.get("output_path"))
    archived_outputs: list[dict[str, str]] = []
    for output_name in _native_run_output_names(flow_state):
        exact_configured_path = (
            configured_output_path
            if configured_output_path is not None
            and configured_output_path.name.casefold() == Path(output_name).name.casefold()
            else None
        )
        archived_outputs.extend(
            _archive_existing_native_output(project_dir, output_name, exact_configured_path)
        )
    if archived_outputs:
        run_state["archived_outputs"] = archived_outputs
    _write_native_run_state(paths, project_dir, run_state)
    _publish_rpa_ui(
        "runtime_starting",
        "computer_control",
        "正在操作你的电脑：正在启动 Native RPA 流程。",
        animation="native_rpa_starting",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        status="submitted",
        run_uuid=request["uuid"],
    )
    caller = client or ShizaiDesktopAgent()
    native_rpc_result: dict[str, Any] | None = None
    runtime_cleanup: dict[str, Any] = {
        "attempted": False,
        "status": "not_needed",
        "reason": "wait_in_progress_or_no_runtime_terminal",
    }
    runtime_log_baseline = _native_runtime_log_snapshot(paths)
    try:
        if wait and callable(getattr(caller, "run_native_rpa", None)):
            run_kwargs: dict[str, Any] = {
                "timeout": _native_run_timeout_seconds() if wait_seconds is None else max(1.0, float(wait_seconds))
            }
            if isinstance(caller, ShizaiDesktopAgent):
                run_kwargs["abort_check"] = lambda: _native_runtime_plugin_blocker(paths, runtime_log_baseline)
            native_rpc_result = caller.run_native_rpa(request, **run_kwargs)
            result = native_rpc_result
        else:
            result = caller.call("CorpBrainService", "workflowDebugRunRPA", [request])
    except Exception as exc:
        runtime_cleanup = _native_cleanup_runtime(
            paths,
            project_dir,
            caller=caller,
            reason="run_exception",
            state=run_state,
            close_mask=True,
        )
        run_state = _read_native_run_state(paths, project_dir) or run_state
        run_state.update({"error": str(exc), "failed_at": time.time()})
        if runtime_cleanup.get("status") not in {"complete", "partial"}:
            run_state["status"] = "failed"
        _write_native_run_state(paths, project_dir, run_state)
        _publish_rpa_ui(
            "runtime_failed",
            "computer_control",
            f"电脑操作失败：Native RPA 启动或运行异常：{exc}",
            animation="native_rpa_failed",
            overlay_state="inactive",
            flow_ref=str(project_dir),
            status="failed",
            run_uuid=request["uuid"],
        )
        raise
    run_state.update({"rpc_result": redact_payload(result), "accepted_at": time.time()})
    if native_rpc_result is not None:
        runtime_blocker = native_rpc_result.get("runtime_blocker")
        run_state.update(
            {
                "accepted": bool(native_rpc_result.get("accepted")),
                "completed": bool(native_rpc_result.get("completed")),
                "terminal_status": native_rpc_result.get("terminal_status"),
                "timed_out": bool(native_rpc_result.get("timed_out")),
                "render_event_count": len(native_rpc_result.get("render_events") or []),
            }
        )
        if isinstance(runtime_blocker, dict) and runtime_blocker.get("kind") == "user_interaction_required":
            run_state.update(
                {
                    "status": "waiting_for_user",
                    "interaction_required": True,
                    "interaction_reason": runtime_blocker.get("reason"),
                    "interaction": redact_payload(runtime_blocker.get("interaction") or {}),
                    "runtime_signal": redact_payload(runtime_blocker),
                    "completed_at": time.time(),
                }
            )
            _publish_rpa_ui(
                "runtime_waiting",
                "computer_control",
                str(runtime_blocker.get("message") or "流程已暂停，等待用户完成网页交互。"),
                animation="native_rpa_waiting",
                overlay_state="inactive",
                flow_ref=str(project_dir),
                status="waiting_for_user",
                interaction_reason=runtime_blocker.get("reason"),
                run_uuid=request["uuid"],
            )
        elif isinstance(runtime_blocker, dict) and runtime_blocker.get("kind") == "runtime_plugin_unavailable":
            run_state.update(
                {
                    "status": "blocked",
                    "failure_kind": "runtime_plugin_unavailable",
                    "runtime_blocker": redact_payload(runtime_blocker),
                    "completed_at": time.time(),
                }
            )
            _publish_rpa_ui(
                "runtime_blocked",
                "computer_control",
                f"运行已暂停：{runtime_blocker.get('message')}",
                animation="native_rpa_blocked",
                overlay_state="inactive",
                flow_ref=str(project_dir),
                status="blocked",
                failure_kind="runtime_plugin_unavailable",
                run_uuid=request["uuid"],
            )
        elif isinstance(runtime_blocker, dict) and runtime_blocker.get("kind") == "runtime_execution_failed":
            run_state.update(
                {
                    "status": "failed",
                    "failure_kind": "runtime_execution_failed",
                    "runtime_signal": redact_payload(runtime_blocker),
                    "completed_at": time.time(),
                }
            )
        elif isinstance(runtime_blocker, dict) and runtime_blocker.get("kind") == "runtime_completed":
            run_state.update(
                {
                    "status": "succeeded",
                    "runtime_signal": redact_payload(runtime_blocker),
                    "completed_at": time.time(),
                }
            )
        if bool(native_rpc_result.get("timed_out")):
            runtime_cleanup = _native_cleanup_runtime(
                paths,
                project_dir,
                caller=caller,
                reason="wait_timeout",
                state=run_state,
                close_mask=True,
            )
            # Cleanup persists a new terminal marker. Do not overwrite it
            # below with the pre-cleanup submitted/succeeded dictionary.
            run_state.update(_read_native_run_state(paths, project_dir) or {})
        elif native_rpc_result.get("terminal_status") in {"success", "error", "stopped"}:
            runtime_cleanup = {
                "attempted": False,
                "status": "complete",
                "reason": "runtime_terminal_event",
                "mask_cleanup": _native_close_runtime_mask(caller),
            }
            if runtime_cleanup["mask_cleanup"].get("status") == "failed":
                runtime_cleanup["status"] = "partial"
                run_state.setdefault("cleanup_errors", []).append(
                    f"runtime mask cleanup failed: {runtime_cleanup['mask_cleanup'].get('error')}"
                )
        run_state["runtime_cleanup"] = redact_payload(runtime_cleanup)
    _write_native_run_state(paths, project_dir, run_state)
    status = native_rpa_status(paths, str(project_dir))
    completed = bool(native_rpc_result and native_rpc_result.get("completed"))
    timed_out = bool(native_rpc_result and native_rpc_result.get("timed_out"))
    return {
        "kind": "native_rpa_run",
        "started": True,
        "accepted": bool(native_rpc_result.get("accepted")) if native_rpc_result else True,
        "completed": completed,
        "timed_out": timed_out,
        "terminal_status": native_rpc_result.get("terminal_status") if native_rpc_result else None,
        "render_events": native_rpc_result.get("render_events", []) if native_rpc_result else [],
        "runtime_mask_suppressions": (
            native_rpc_result.get("runtime_mask_suppressions", []) if native_rpc_result else []
        ),
        "runtime_mask_force_close": (native_rpc_result.get("runtime_mask_force_close") if native_rpc_result else None),
        "design_validation_cleanup": design_validation_cleanup,
        "browser_cleanup": browser_cleanup,
        "runtime_cleanup": runtime_cleanup,
        "project": str(project_dir),
        "application_id": application_id,
        "package_id": package_id,
        "node_id": task_node_id,
        "request": redact_payload(request),
        "rpc_result": redact_payload(result),
        "archived_outputs": archived_outputs,
        "status": status,
        "ui_feedback": status.get("ui_feedback"),
        "interaction_required": status.get("status") == "waiting_for_user",
        "interaction": (status.get("run") or {}).get("interaction"),
        "message": (
            str(
                ((status.get("run") or {}).get("interaction") or {}).get("user_prompt")
                or "流程已暂停，等待用户完成网页交互。"
            )
            if status.get("status") == "waiting_for_user"
            else "Native RPA 通道已结束，但业务输出未闭环；请查看 status、output 和 failure_kind。"
            if status.get("status") == "failed"
            else "Native RPA 已完成通道级运行监听，请查看 status、output 和 ui_feedback。"
            if completed
            else "Native RPA 已提交；监听超时或使用 wait=false 时，请调用 shizai_rpa_status 继续查看运行日志、输出和操作电脑状态。"
        ),
    }


def native_rpa_stop(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Stop the current native RPA run and persist a confirmed cleanup outcome."""
    project_dir = resolve_project(paths, project_ref)
    caller = client or ShizaiDesktopAgent()
    result = _native_cleanup_runtime(
        paths,
        project_dir,
        caller=caller,
        reason="user_stop",
        close_mask=True,
    )
    result["kind"] = "native_rpa_stop"
    result["message"] = (
        "设计器当前没有活动的 Native RPA 运行。"
        if result.get("already_stopped")
        else "Native RPA 已停止。"
        if result.get("cleanup_complete")
        else "Native RPA 停止请求未完成，请稍后重试。"
    )
    return result


def _native_task_argument(node: dict[str, Any], name: str, default: Any = None) -> Any:
    """Read one designer component argument by its native name."""
    for argument in node.get("args0", []):
        if isinstance(argument, dict) and argument.get("name") == name:
            return argument.get("value", default)
    return default


def _native_set_argument(node: dict[str, Any], name: str, value: Any) -> None:
    """Set or add one native component argument without changing other fields."""
    arguments = node.setdefault("args0", [])
    if not isinstance(arguments, list):
        arguments = []
        node["args0"] = arguments
    for argument in arguments:
        if isinstance(argument, dict) and argument.get("name") == name:
            argument["value"] = value
            return
    arguments.append({"name": name, "value": value})


def _native_set_parameter(node: dict[str, Any], name: str, value: Any) -> None:
    """Update an existing argument or option while preserving its native group."""
    for key in ("args0", "options0"):
        entries = node.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("name") == name:
                entry["value"] = value
                return
    raise ValueError(f"component {node.get('type')} has no parameter named {name}")


def _generic_native_value(value: Any, *, output_parameter: bool = False) -> Any:
    """Encode one generic plan value using an explicit literal/expression contract."""
    if output_parameter:
        return str(value).strip()
    if isinstance(value, dict):
        if set(value) == {"variable"}:
            return str(value["variable"]).strip()
        if set(value) == {"expression"}:
            return str(value["expression"]).strip()
        if set(value) == {"literal"}:
            return json.dumps(value["literal"], ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "None"
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _native_list_expression(value: Any, default: str) -> str:
    """Normalize a component value to the designer's list expression syntax."""
    if value is None:
        return json.dumps([default], ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    if not text:
        return json.dumps([default], ensure_ascii=False)
    if text.startswith("[") and text.endswith("]"):
        # Preserve expressions such as [xpath] as well as JSON list literals.
        return text
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        text = text[1:-1]
    return json.dumps([text], ensure_ascii=False)


_NATIVE_CODE_DESCRIPTORS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "WebBrowser_CreateV3": (
        "CreateV3",
        ("type", "url", "isRemote", "ipAddress", "port", "arguments", "openNewTab", "waitingload"),
        "WebBrowser",
    ),
    "WebBrowser_GetTAB_V1": ("GetTAB_V1", ("type", "match_type", "title", "url", "isActive"), "WebBrowser"),
    "Element_SetValue_V6": (
        "SetValue_V6",
        (
            "uiSelector",
            "designated_info",
            "designated_obj",
            "text",
            "input_type",
            "isEnter",
            "operation",
            "input_method",
            "interval",
            "click_data",
            "delaybeforEnter",
            "verifying_input",
            "focus_timeout",
            "force_switch_eng",
        ),
        "Element",
    ),
    "Keyboard_Send_V1": ("Send_V1", ("words", "interval", "fuctype"), "Keyboard"),
    "Keyboard_PressNew": ("PressNew", ("key", "type", "auxiliary_key", "input_us"), "Keyboard"),
    "Basic_Delay_V1": ("Delay_V1", ("second",), "Basic"),
    "WebBrowser_RunJsInIeBySelector_v3": (
        "RunJsInIeBySelector_v3",
        (
            "designated_info",
            "web_page",
            "uiSelector",
            "jsCode",
            "input",
            "executionEnvironment",
            "isAsyncCode",
            "waitResultTimeout",
        ),
        "WebBrowser",
    ),
    "WebBrowser_DataCollectionV5": (
        "DataCollectionV5",
        (
            "config",
            "designated_info",
            "designated_obj",
            "region",
            "collect_pages",
            "collect_items",
            "page_type",
            "page_type_num",
            "selector_page",
            "selector_more",
            "deduplication",
            "operation_num",
            "scroll_config",
            "wait_time_page",
            "showTable",
            "toExcel",
            "filename_suffix",
            "use_name",
            "append_data",
            "use_sheet",
            "sheet_name",
            "filePath",
            "monitor_data",
            "share",
        ),
        "WebBrowser",
    ),
    "WebBrowser_CloseWeb_V2": (
        "CloseWeb_V2",
        ("colse_type", "browser_type", "web", "kill", "is_close_alert"),
        "WebBrowser",
    ),
    "Excel_Cs_NewV4": ("Cs_NewV4", ("path", "name", "software", "show", "maximize", "share"), "Excel"),
    "Excel_Cs_WriteRange": ("Cs_WriteRange", ("workbook", "sheet", "row", "column", "contents", "save"), "Excel"),
}

_NATIVE_OPTION_NAMES: dict[str, tuple[str, ...]] = {
    "WebBrowser_CreateV3": (
        "var_ret",
        "delay_before_new",
        "delay_after_new",
        "timeout_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "WebBrowser_GetTAB_V1": ("var_ret", "skip_err", "delay_before_new", "delay_after_new", "timeout_new"),
    "Element_SetValue_V6": (
        "var_ret",
        "delay_before_new",
        "delay_after_new",
        "timeout_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "Keyboard_Send_V1": (
        "delay_before_new",
        "delay_after_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "Keyboard_PressNew": (
        "delay_before_new",
        "delay_after_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "Basic_Delay_V1": (),
    "WebBrowser_RunJsInIeBySelector_v3": ("var_ret", "delay_before_new", "delay_after_new", "skip_err", "log_exc"),
    "WebBrowser_DataCollectionV5": ("var_ret",),
    "WebBrowser_CloseWeb_V2": (
        "var_ret",
        "delay_before_new",
        "delay_after_new",
        "timeout_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "Excel_Cs_NewV4": (
        "var_ret",
        "delay_before_new",
        "delay_after_new",
        "exc_retry",
        "retry_count",
        "retry_interval",
        "skip_err",
        "log_exc",
    ),
    "Excel_Cs_WriteRange": ("delay_before_new", "delay_after_new", "skip_err", "log_exc"),
}

_NATIVE_DEFAULT_VALUES: dict[str, dict[str, Any]] = {
    "WebBrowser_CreateV3": {
        "isRemote": "0",
        "ipAddress": '""',
        "port": '"61619"',
        "arguments": '""',
        "openNewTab": "1",
        "waitingload": "1",
        "var_ret": "0",
        "delay_before_new": "0.2",
        "delay_after_new": "0.05",
        "timeout_new": "60",
        "exc_retry": "0",
        "retry_count": "1",
        "retry_interval": "1",
        "skip_err": "1",
        "log_exc": "1",
    },
    "WebBrowser_GetTAB_V1": {
        "var_name": "网页对象",
        "type": '"chrome"',
        "match_type": "2",
        "title": '""',
        "url": '""',
        "isActive": "1",
        "var_ret": "0",
        "skip_err": "0",
        "delay_before_new": "0.2",
        "delay_after_new": "0.05",
        "timeout_new": "10",
    },
    "Element_SetValue_V6": {
        "designated_info": "0",
        "designated_obj": "网页对象",
        "input_type": "0",
        "isEnter": "1",
        "operation": "1",
        "input_method": "1",
        "force_switch_eng": "0",
        "interval": "150",
        "click_data": {"cursorPosition": 0, "offset": {"x": 0, "y": 0}, "random": 0},
        "delaybeforEnter": "500",
        "verifying_input": "0",
        "focus_timeout": "100",
        "var_ret": "0",
        "delay_before_new": "0.2",
        "delay_after_new": "0.05",
        "timeout_new": "30",
        "exc_retry": "1",
        "retry_count": "5",
        "retry_interval": "1",
        "skip_err": "0",
        "log_exc": "1",
    },
    "Keyboard_Send_V1": {
        "interval": "10",
        "fuctype": "0",
        "delay_before_new": "0",
        "delay_after_new": "0",
        "exc_retry": "0",
        "retry_count": "1",
        "retry_interval": "1",
        "skip_err": "0",
        "log_exc": "1",
    },
    "Keyboard_PressNew": {
        "key": '"13"',
        "type": '"press"',
        "auxiliary_key": [],
        "input_us": "1",
        "delay_before_new": "0.5",
        "delay_after_new": "0",
        "exc_retry": "0",
        "retry_count": "1",
        "retry_interval": "1",
        "skip_err": "0",
        "log_exc": "1",
    },
    "Basic_Delay_V1": {
        "second": "1",
    },
    "WebBrowser_RunJsInIeBySelector_v3": {
        "designated_info": "1",
        "web_page": "网页对象",
        "uiSelector": "",
        "jsCode": DEFAULT_JS_LINK_SCRIPT,
        "input": '[".plugin_goodsCardWrapper, .gl-item"]',
        "executionEnvironment": '"MAIN"',
        "isAsyncCode": "0",
        "waitResultTimeout": "30",
        "var_ret": "1",
        "delay_before_new": "0.2",
        "delay_after_new": "0.05",
        "skip_err": "0",
        "log_exc": "1",
    },
    "WebBrowser_DataCollectionV5": {
        "designated_info": "1",
        "designated_obj": "网页对象",
        "region": "1",
        "collect_pages": "1",
        "collect_items": "20",
        "page_type": "1",
        "page_type_num": "0",
        "selector_page": "",
        "selector_more": "",
        "deduplication": "0",
        "operation_num": "1",
        "scroll_config": "2",
        "wait_time_page": "2",
        "showTable": "1",
        "toExcel": "1",
        "filename_suffix": ["jd-products", ".xls"],
        "use_name": "0",
        "append_data": "0",
        "use_sheet": "0",
        "sheet_name": '""',
        "filePath": '"res"',
        "monitor_data": "0",
        "share": "0",
        "var_ret": "0",
    },
    "WebBrowser_CloseWeb_V2": {
        "var_name": "操作结果",
        "colse_type": '"select"',
        "browser_type": '"chrome"',
        "web": "网页对象",
        "kill": "0",
        "is_close_alert": "0",
        "var_ret": "0",
        "delay_before_new": "0.2",
        "delay_after_new": "0.2",
        "timeout_new": "10",
        "exc_retry": "0",
        "retry_count": "1",
        "retry_interval": "1",
        "skip_err": "0",
        "log_exc": "1",
    },
    "Excel_Cs_NewV4": {
        "software": '"默认"',
        "show": "1",
        "maximize": "1",
        "share": "0",
        "var_ret": "0",
        "delay_before_new": "0.2",
        "delay_after_new": "0.2",
        "exc_retry": "0",
        "retry_count": "1",
        "retry_interval": "1",
        "skip_err": "0",
        "log_exc": "1",
    },
    "Excel_Cs_WriteRange": {
        "sheet": '"Sheet1"',
        "row": "1",
        "column": '"A"',
        "contents": "数据采集结果",
        "save": "1",
        "delay_before_new": "0.2",
        "delay_after_new": "0.2",
        "skip_err": "0",
        "log_exc": "1",
    },
}


def _native_parse_object_value(value: str) -> Any:
    """Convert the designer's ``@{key=value}`` display value to Python data."""
    text = str(value).strip()
    if not (text.startswith("@{") and text.endswith("}")):
        return value
    body = text[2:-1].strip()
    result: dict[str, Any] = {}
    for item in body.split(";"):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        raw = raw.strip()
        if raw == "":
            parsed: Any = ""
        elif raw.lower() in {"true", "false"}:
            parsed = raw.lower() == "true"
        else:
            try:
                parsed = int(raw)
            except ValueError:
                try:
                    parsed = float(raw)
                except ValueError:
                    parsed = raw
        result[key.strip()] = parsed
    if result.get("offset") == "":
        result["offset"] = {"x": 0, "y": 0}
    return result


def _native_normalize_click_data(value: Any) -> Any:
    """Normalize element click-position data to the RuntimeHost schema."""
    if isinstance(value, str):
        parsed = _native_parse_object_value(value)
        if parsed is value:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
        if parsed is not value:
            value = parsed
    if not isinstance(value, dict):
        return value

    normalized = deepcopy(value)

    def integer(item: Any) -> Any:
        if isinstance(item, bool) or isinstance(item, int):
            return item
        if isinstance(item, str):
            try:
                return int(item.strip())
            except ValueError:
                return item
        return item

    for key in ("cursorPosition", "random"):
        if key in normalized:
            normalized[key] = integer(normalized[key])
    offset = normalized.get("offset")
    if isinstance(offset, dict):
        # clickPositionV2 may persist its UI-only random flag inside offset,
        # but RuntimeHost accepts random only at the click_data root.
        offset.pop("random", None)
        for key in ("x", "y"):
            if key in offset:
                offset[key] = integer(offset[key])
    return normalized


def _native_numeric_expression(value: Any) -> str:
    """Render a static numeric native argument without JSON string quoting."""
    decoded = _decode_selector_value(value)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", decoded):
        return decoded
    return str(value)


def _native_normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """Repair known strict native argument shapes on a copied task node.

    Native numeric inputs are persisted as expression strings, but the
    expression must be ``3`` rather than the JSON string literal ``"3"``.
    Older CLI paths applied the generic JSON encoder to these slots, which
    left a quoted number in the task and made the designer reject the node.
    """
    numeric_native_arguments = {
        "region",
        "collect_pages",
        "collect_items",
        "page_type",
        "page_type_num",
        "deduplication",
        "operation_num",
        "scroll_config",
        "wait_time_page",
        "showTable",
        "toExcel",
        "monitor_data",
        "share",
        "append_data",
        "use_sheet",
        "input_type",
        "isEnter",
        "interval",
        "delaybeforEnter",
        "verifying_input",
        "focus_timeout",
    }
    for key in ("args0", "options0"):
        entries = node.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == "click_data":
                entry["value"] = _native_normalize_click_data(entry.get("value"))
            elif entry.get("name") in numeric_native_arguments:
                entry["value"] = _native_numeric_expression(entry.get("value"))
    return node


def _native_normalize_collection_config(payload: dict[str, Any]) -> bool:
    """Repair quoted static properties in DataCollectionV5 selector paths.

    Action selectors use JSON/Python expressions, while the designer stores
    collection selector properties as raw DOM values. Older MCP builds used
    the action format in ``collectionConfig`` and produced selectors such as
    ``class=\"plugin_goodsCardWrapper\"``. Use the persisted display value
    when it proves that ``value`` is only a JSON string wrapper, leaving
    dynamic expressions and already-correct designer files untouched.
    """
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return False
    changed = False
    for node in entities.values():
        if not isinstance(node, dict) or node.get("type") != "WebBrowser_DataCollectionV5":
            continue
        config_argument = next(
            (
                argument
                for argument in node.get("args0", [])
                if isinstance(argument, dict) and argument.get("name") == "config"
            ),
            None,
        )
        if not isinstance(config_argument, dict):
            continue
        raw_config = config_argument.get("value")
        if isinstance(raw_config, str):
            try:
                config = json.loads(raw_config)
            except json.JSONDecodeError:
                continue
        else:
            config = raw_config
        collection = config.get("collectionConfig") if isinstance(config, dict) else None
        selectors = collection.get("columnSelectorArray") if isinstance(collection, dict) else None
        if not isinstance(selectors, list):
            continue
        node_changed = False
        for selector_path in selectors:
            if not isinstance(selector_path, list):
                continue
            for selector_node in selector_path:
                if not isinstance(selector_node, dict):
                    continue
                for prop in selector_node.get("props", []):
                    if not isinstance(prop, dict):
                        continue
                    display = prop.get("textValue")
                    raw_value = prop.get("value")
                    if display in (None, "") or not isinstance(raw_value, str):
                        continue
                    display_value = _decode_selector_value(display)
                    if not display_value or _decode_selector_value(raw_value) != display_value:
                        continue
                    raw_text = raw_value.strip()
                    if raw_text == display_value or not (
                        len(raw_text) >= 2 and raw_text[0] in {'"', "'"} and raw_text[-1] == raw_text[0]
                    ):
                        continue
                    # Only unwrap a JSON/Python string literal. A value such
                    # as an expression or variable reference must remain
                    # untouched because it is not a static DOM property.
                    prop["value"] = display_value
                    node_changed = True
        if node_changed:
            config_argument["value"] = json.dumps(config, ensure_ascii=False)
            changed = True
    return changed


def _native_code_expression(name: str, value: Any) -> str:
    """Render a task argument as the Python expression expected by RuntimeHost."""
    if value is None:
        return "None"
    if name == "jsCode" and isinstance(value, str):
        # Existing designer tasks store JavaScript in a triple-quoted Python
        # expression. MCP callers may provide plain JavaScript, which must be
        # quoted before the generated flow module is compiled.
        if value.strip().startswith('"""') and value.strip().endswith('"""'):
            return value
        return json.dumps(value, ensure_ascii=False)
    if name == "click_data":
        value = _native_normalize_click_data(value)
    if name == "config" and isinstance(value, str):
        try:
            return repr(json.loads(value))
        except json.JSONDecodeError:
            pass
    if name == "filename_suffix" and isinstance(value, list):
        cleaned = []
        for item in value:
            item_text = str(item)
            try:
                item = json.loads(item_text)
            except (TypeError, json.JSONDecodeError):
                item = item_text.strip('"')
            cleaned.append(item)
        return repr(cleaned)
    if isinstance(value, str):
        parsed = _native_parse_object_value(value)
        if parsed is not value:
            return repr(parsed)
        return value if value.strip() else '""'
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return repr(value)
    return repr(str(value))


def _native_argument_values(node: dict[str, Any], component: str) -> dict[str, Any]:
    """Collect visible args/options and fill hidden defaults from the component contract."""
    values = dict(_NATIVE_DEFAULT_VALUES.get(component, {}))
    for key in ("args0", "options0"):
        entries = node.get(key)
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("name"):
                values[str(entry["name"])] = entry.get("value")
    if component in {"Element_SetValue_V6", "Element_Click_V6"} and "click_data" in values:
        values["click_data"] = _native_normalize_click_data(values["click_data"])
    if component == "WebBrowser_RunJsInIeBySelector_v3":
        values["input"] = _native_list_expression(
            values.get("input"),
            ".plugin_goodsCardWrapper, .gl-item",
        )
    return values


def _native_output_names(node: dict[str, Any], component: str) -> list[str]:
    """Read output variable names in the same order used by generated modules."""
    values = _native_argument_values(node, component)
    if component == "WebBrowser_DataCollectionV5":
        names = [values.get(name) for name in ("var0", "var1", "var2")]
        return [str(item).strip() for item in names if str(item or "").strip()]
    if component in {
        "WebBrowser_CreateV3",
        "WebBrowser_GetTAB_V1",
        "WebBrowser_CloseWeb_V2",
        "WebBrowser_RunJsInIeBySelector_v3",
        "Excel_Cs_NewV4",
    }:
        name = str(values.get("var_name") or "").strip()
        return [name] if name else []
    code_records = node.get("mcpCode") if isinstance(node.get("mcpCode"), list) else []
    names: list[str] = []
    for record in code_records:
        if not isinstance(record, dict) or not record.get("assignTo"):
            continue
        parameters = record["assignTo"] if isinstance(record["assignTo"], list) else str(record["assignTo"]).split(",")
        for parameter in parameters:
            value = str(values.get(str(parameter).strip()) or "").strip()
            if value and value not in names:
                names.append(value)
    if names:
        return names
    return []


def _native_js_table_mode(payload: dict[str, Any], task_path: Path) -> bool:
    """Detect the direct JS-table output mode used by a native project."""
    metadata_path = task_path.parent / "szrpa.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict) and bool(metadata.get("mcpUseJsTable")):
        return True

    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return False
    for node_id in _native_statement_ids(payload):
        node = entities.get(node_id)
        if not isinstance(node, dict) or node.get("type") != "WebBrowser_RunJsInIeBySelector_v3":
            continue
        output = _native_task_argument(node, "var_name", "")
        if str(output or "").strip() == JS_TABLE_OUTPUT_VARIABLE:
            return True
    return False


def _native_js_table_pagination(payload: dict[str, Any], task_path: Path) -> tuple[bool, int, float]:
    """Read the optional CLI JS-table pagination settings from project metadata."""
    metadata_path = task_path.parent / "szrpa.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        max_rows = int(metadata.get("mcpMaxRows") or 0)
    except (TypeError, ValueError):
        max_rows = 0
    try:
        wait_seconds = float(metadata.get("mcpWaitSeconds") or 2.0)
    except (TypeError, ValueError):
        wait_seconds = 2.0
    paginate = bool(metadata.get("mcpJsTablePaginate")) or (
        bool(metadata.get("mcpUseJsTable")) and max_rows > 30
    )
    return paginate, max(0, max_rows), max(0.5, min(wait_seconds, 30.0))


def _native_variable_reference_list(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the variable index consumed by the designer/runtime file service."""
    entities = payload.get("componentModelEntities")
    main = entities.get("MAIN_COMPONENT") if isinstance(entities, dict) else None
    if not isinstance(main, dict):
        return []
    statements = _native_statement_ids(payload)
    refs: list[dict[str, Any]] = []
    known_outputs: list[str] = []
    sequence_number = 1
    for row, node_id in enumerate(statements, start=1):
        node = entities.get(node_id) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        generic_code = node.get("mcpCode") if isinstance(node.get("mcpCode"), list) else []
        descriptor = _NATIVE_CODE_DESCRIPTORS.get(component)
        if descriptor:
            argument_names = descriptor[1] + _NATIVE_OPTION_NAMES.get(component, ())
            output_argument_names = ("var_name", "var0", "var1", "var2")
        elif len(generic_code) == 1 and isinstance(generic_code[0], dict):
            argument_names = tuple(
                str(item).strip() for item in generic_code[0].get("args", []) if str(item).strip()
            ) + tuple(
                str(item.get("name") or "").strip()
                for item in node.get("options0", [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
            assigned = generic_code[0].get("assignTo")
            output_argument_names = tuple(
                str(item).strip()
                for item in (assigned if isinstance(assigned, list) else str(assigned or "").split(","))
                if str(item).strip()
            )
        else:
            continue
        values = _native_argument_values(node, component)
        for argument_name in argument_names:
            value = values.get(argument_name)
            if not isinstance(value, str):
                continue
            if "SHIZAI_ELEMENT_DICT" in value:
                variable_name = "SHIZAI_ELEMENT_DICT"
            else:
                variable_name = next(
                    (
                        item
                        for item in reversed(known_outputs)
                        if item and item in value and not (value.startswith('"') and value.endswith('"'))
                    ),
                    None,
                )
            if not variable_name:
                continue
            refs.append(
                {
                    "id": f"g-io5cd-flowBlock-{sequence_number}",
                    "referenceType": "get",
                    "arrangementId": "io5cd-flowBlock",
                    "componentId": str(node_id),
                    "argumentName": argument_name,
                    "argKeyPath": "",
                    "row": row,
                    "locStart": 1,
                    "locEnd": max(5, len(value)),
                    "callType": "none",
                    "callValue": "",
                    "variableName": variable_name,
                }
            )
            sequence_number += 1
        for argument_name in output_argument_names:
            output_name = str(values.get(argument_name) or "").strip()
            if not output_name:
                continue
            type_names = {
                "WebBrowser_CreateV3": ["rpa_hWeb"],
                "WebBrowser_GetTAB_V1": ["rpa_hWeb"],
                "WebBrowser_DataCollectionV5": ["rpa_list_piliangshuju_v1"] if argument_name == "var0" else ["rpa_str"],
                "Excel_Cs_NewV4": ["rpa_hExcel"],
            }.get(component, ["rpa_any"])
            if component == "WebBrowser_RunJsInIeBySelector_v3" and output_name == JS_TABLE_OUTPUT_VARIABLE:
                type_names = [JS_TABLE_OUTPUT_TYPE]
            refs.append(
                {
                    "id": f"s-io5cd-flowBlock-{sequence_number}",
                    "referenceType": "set",
                    "arrangementId": "io5cd-flowBlock",
                    "componentId": str(node_id),
                    "argumentName": argument_name,
                    "locStart": 0,
                    "variableName": output_name,
                    "variableClass": "local",
                    "variableTypeNames": type_names,
                }
            )
            sequence_number += 1
            known_outputs.append(output_name)
    return refs


def _native_variable_analysis_map(
    payload: dict[str, Any],
    *,
    source_text: str = "",
    date: int | None = None,
) -> list[list[Any]]:
    """Build the pair-list consumed by the designer's code variable analyzer.

    ``file_index_700.json`` stores rich component references, while
    ``variableInfo.variableMap`` is a list of ``[code_key, analysis_info]``
    pairs.  These are two related but different contracts in the native
    designer.  Keeping the conversion here prevents the rich index format
    from being written to the analyzer's pair-list by mistake.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for reference in _native_variable_reference_list(payload):
        arrangement_id = str(reference.get("arrangementId") or "io5cd-flowBlock").strip()
        component_id = str(reference.get("componentId") or "").strip()
        if not component_id:
            continue
        key = f"{arrangement_id}_{component_id}"
        compact = {
            name: reference[name]
            for name in ("referenceType", "variableName", "row", "locStart", "locEnd")
            if name in reference
        }
        if compact.get("referenceType") and compact.get("variableName"):
            grouped.setdefault(key, []).append(compact)
    if not grouped:
        return []
    if date is None:
        date = int(time.time() * 1000)
    md5_value = hashlib.md5(source_text.encode("utf-8")).hexdigest()
    return [
        [
            key,
            {
                "date": date,
                "md5Value": md5_value,
                "variable": variables,
            },
        ]
        for key, variables in grouped.items()
    ]


def _native_variable_info_snapshot(root: Path) -> dict[str, Any]:
    """Inspect the variable analyzer file without confusing it with the rich index."""
    path = root / "variableInfo"
    snapshot: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "valid": False,
        "top_level_type": None,
        "variable_map_type": None,
        "entry_count": 0,
        "error": None,
    }
    if not path.is_file():
        snapshot["error"] = "variableInfo is missing"
        return snapshot
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        snapshot["error"] = str(exc)
        return snapshot
    snapshot["top_level_type"] = type(payload).__name__
    if not isinstance(payload, dict):
        snapshot["error"] = "variableInfo must contain a JSON object"
        return snapshot
    variable_map = payload.get("variableMap")
    snapshot["variable_map_type"] = type(variable_map).__name__
    if not isinstance(variable_map, list):
        snapshot["error"] = "variableInfo.variableMap must be a list of pairs"
        return snapshot
    snapshot["entry_count"] = len(variable_map)
    malformed = next(
        (
            item
            for item in variable_map
            if not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], dict)
            or not isinstance(item[1].get("variable"), list)
        ),
        None,
    )
    if malformed is not None:
        snapshot["error"] = "variableInfo.variableMap entries must be [code_key, analysis_info] pairs"
        return snapshot
    snapshot["valid"] = True
    return snapshot


def _native_module_text(payload: dict[str, Any], task_path: Path) -> str:
    """Generate the executable Python module from the native task entities."""
    entities = payload.get("componentModelEntities")
    statements = _native_statement_ids(payload)
    js_table_mode = _native_js_table_mode(payload, task_path)
    js_table_paginate, js_table_max_rows, js_table_wait_seconds = _native_js_table_pagination(payload, task_path)
    components = {
        str(entities[node_id].get("type"))
        for node_id in statements
        if isinstance(entities, dict)
        and isinstance(entities.get(node_id), dict)
        and str(entities[node_id].get("type")) in _NATIVE_CODE_DESCRIPTORS
    }
    generic_aliases = {
        str(record.get("func") or "").split(".", 1)[0]
        for node_id in statements
        for node in ([entities.get(node_id)] if isinstance(entities, dict) else [])
        for record in (node.get("mcpCode") if isinstance(node, dict) and isinstance(node.get("mcpCode"), list) else [])
        if isinstance(record, dict) and "." in str(record.get("func") or "")
    }
    aliases = {
        alias
        for alias in ("WebBrowser", "Element", "Keyboard", "Basic", "Excel")
        if any(_NATIVE_CODE_DESCRIPTORS[item][2] == alias for item in components)
    } | generic_aliases
    imports = "\n".join(f"import packages.{alias}.py.exports as {alias}" for alias in sorted(aliases))
    lines = [
        "import sys",
        "import os",
        "import json",
        "import time",
        "from common.util.recursive_util import tail_call_optimized",
        "from common.util.decryption_util import decrypt,encryption",
        "from common.util.get_config_util import get_config, get_out_param",
        "from common.util.log_util import trace_log",
        "from common.util.option_util import options",
        "from common.util.elements_util import elementsFormat, elementsFormatNew, collectElementsFormat, child_ele_eval",
        "from common.util.judge_components import *",
        "from common.util.selector_array import get_selector_array,get_selector_array_mobile",
        "from common.util.loop_excel import excel_loop_data",
        "from common.util.custom_code_component_location import custom_code_start, custom_code_end, custom_code_exit",
        "from common.util.loop_code_component_location import *",
        "from common.util.component_public_method import str_encryption",
        "from common.util.pdb_util import process_debug",
        "from common.util.outside_var_util import save_outside_vars, load_outside_vars",
        "from common.util.shared_variables import GetSharedVariable",
        "from common.util.list_util import get_list",
        "from projects.rpaRoot import SZEnv",
        "",
        imports,
        "from ..global_data import globalVar",
        "from ..global_data import TASK_VARIABLE_MAP,MOBILE_DEVICE_MAP,GLOBAL_CONFIG_PASSWORD,SHIZAI_ELEMENT_DICT,SHIZAI_IMAGE_DICT",
        "from ..global_data import run_module, _logError",
        "self = SZEnv['rpa']",
        "",
        "def _reload_shizai_element_dict():",
        "    global SHIZAI_ELEMENT_DICT",
        "    try:",
        "        application_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
        "        with open(os.path.join(application_root, 'elements.json'), encoding='utf-8') as handle:",
        "            payload = json.load(handle)",
        "        if isinstance(payload, dict) and isinstance(payload.get('elementMap'), dict):",
        "            SHIZAI_ELEMENT_DICT = payload['elementMap']",
        "            try:",
        "                from .. import global_data as _global_data",
        "                _global_data.SHIZAI_ELEMENT_DICT = SHIZAI_ELEMENT_DICT",
        "            except Exception:",
        "                pass",
        "    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):",
        "        return",
        "",
        "def main(rpa, **_sz_kw):",
        "    _reload_shizai_element_dict()",
    ]
    for ordinal, node_id in enumerate(statements, start=1):
        node = entities.get(node_id) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        descriptor = _NATIVE_CODE_DESCRIPTORS.get(component)
        generic_code = node.get("mcpCode") if isinstance(node.get("mcpCode"), list) else []
        if not descriptor and generic_code:
            code_record = generic_code[0] if isinstance(generic_code[0], dict) else {}
            function_path = str(code_record.get("func") or "")
            if "." in function_path:
                alias, function_name = function_path.split(".", 1)
                argument_names = tuple(str(item).strip() for item in code_record.get("args", []) if str(item).strip())
                descriptor = (function_name, argument_names, alias)
        if not descriptor:
            continue
        function_name, argument_names, alias = descriptor
        values = _native_argument_values(node, component)
        args = [f"{name}={_native_code_expression(name, values.get(name))}" for name in argument_names]
        option_names = _NATIVE_OPTION_NAMES.get(component, ())
        if generic_code and component not in _NATIVE_CODE_DESCRIPTORS:
            option_names = tuple(
                str(item.get("name") or "").strip()
                for item in node.get("options0", [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
        args.extend(f"{name}={_native_code_expression(name, values.get(name))}" for name in option_names)
        position = (
            json.dumps(task_path.stem, ensure_ascii=False),
            json.dumps(str(node.get("id") or node_id), ensure_ascii=False),
            json.dumps("流程块1", ensure_ascii=False),
            json.dumps(str(node.get("label") or component), ensure_ascii=False),
            json.dumps(str(ordinal), ensure_ascii=False),
            json.dumps(f"{alias}.{function_name}", ensure_ascii=False),
        )
        call = f"{alias}.{function_name}(SZEnv['rpa'], {', '.join(args)}, _position=({', '.join(position)}))"
        outputs = _native_output_names(node, component)
        if (
            component in {"WebBrowser_CreateV3", "WebBrowser_RunJsInIeBySelector_v3", "WebBrowser_DataCollectionV5"}
            and outputs
        ):
            # These two native exports receive their output contract explicitly;
            # RuntimeHost uses it to register the returned values in the local
            # variable table.
            call = call.replace(
                f"_position=({', '.join(position)})",
                f"var_names={json.dumps(outputs, ensure_ascii=False)}, _position=({', '.join(position)})",
            )
        if (
            component == "WebBrowser_RunJsInIeBySelector_v3"
            and js_table_mode
            and js_table_paginate
            and outputs
            and outputs[0] == JS_TABLE_OUTPUT_VARIABLE
        ):
            # A direct JS table is a synchronous native component.  Keep the
            # model-selected extractor, but let the CLI runner repeat it over
            # visible next-page controls and merge rows before Excel receives
            # the final table.  The bounded page budget prevents a bad next
            # button from creating an endless runtime loop.
            nav_values = dict(values)
            nav_values.update({"jsCode": JS_NEXT_PAGE_SCRIPT, "input": "[]", "uiSelector": ""})
            nav_args = [
                f"{name}={_native_code_expression(name, nav_values.get(name))}"
                for name in argument_names
            ]
            nav_args.extend(
                f"{name}={_native_code_expression(name, nav_values.get(name))}"
                for name in option_names
            )
            nav_position = (
                json.dumps(task_path.stem, ensure_ascii=False),
                json.dumps(f"{node.get('id') or node_id}:next", ensure_ascii=False),
                json.dumps("流程块1", ensure_ascii=False),
                json.dumps("翻到下一页", ensure_ascii=False),
                json.dumps(f"{ordinal}.1", ensure_ascii=False),
                json.dumps(f"{alias}.{function_name}", ensure_ascii=False),
            )
            nav_call = (
                f"{alias}.{function_name}(SZEnv['rpa'], {', '.join(nav_args)}, "
                f"var_names=['__shizai_next_page'], _position=({', '.join(nav_position)}))"
            )
            page_budget = max(1, (js_table_max_rows + 19) // 20 + 2) if js_table_max_rows else 8
            lines.extend(
                [
                    "    __shizai_table_rows = []",
                    "    __shizai_table_headers = []",
                    "    __shizai_table_seen = set()",
                    f"    for __shizai_page_index in range({page_budget}):",
                    f"        {JS_TABLE_OUTPUT_VARIABLE} = {call}",
                    f"        if isinstance({JS_TABLE_OUTPUT_VARIABLE}, str):",
                    "            try:",
                    f"                {JS_TABLE_OUTPUT_VARIABLE} = json.loads({JS_TABLE_OUTPUT_VARIABLE})",
                    "            except (TypeError, ValueError) as exc:",
                    "                raise ValueError('JS 表格输出不是有效的 JSON 内容') from exc",
                    f"        if isinstance({JS_TABLE_OUTPUT_VARIABLE}, list) and {JS_TABLE_OUTPUT_VARIABLE}:",
                    f"            if not __shizai_table_headers: __shizai_table_headers = list({JS_TABLE_OUTPUT_VARIABLE}[0])",
                    f"            for __shizai_row in {JS_TABLE_OUTPUT_VARIABLE}[1:]:",
                    "                if not isinstance(__shizai_row, (list, tuple)): continue",
                    "                __shizai_row_key = next((str(__shizai_cell).strip() for __shizai_cell in __shizai_row if isinstance(__shizai_cell, str) and __shizai_cell.strip().lower().startswith(('http://', 'https://'))), repr(list(__shizai_row)))",
                    "                if __shizai_row_key in __shizai_table_seen: continue",
                    "                __shizai_table_seen.add(__shizai_row_key)",
                    "                __shizai_table_rows.append(list(__shizai_row))",
                    f"        if {js_table_max_rows} > 0 and len(__shizai_table_rows) >= {js_table_max_rows}: break",
                    f"        __shizai_next_page = {nav_call}",
                    "        if not __shizai_next_page: break",
                    f"        Basic.Delay_V1(SZEnv['rpa'], second={js_table_wait_seconds!r})",
                    "        网页对象 = WebBrowser.GetTAB_V1(SZEnv['rpa'], type=\"chrome\", match_type=2, title=\"\", url=\"\", isActive=1, var_ret=0, skip_err=0, delay_before_new=0.2, delay_after_new=0.05, timeout_new=10)",
                    f"    {JS_TABLE_OUTPUT_VARIABLE} = ([__shizai_table_headers] if __shizai_table_headers else []) + __shizai_table_rows[:{js_table_max_rows or 0} if {js_table_max_rows} > 0 else None]",
                ]
            )
            lines.extend(
                [
                    '    if (_sz_kw.get("_sz_process_expr_")):',
                    "        try:",
                    '            exec(_sz_kw["_sz_process_expr_"])',
                    "        except NameError as e:",
                    '            _logError(\'流程块"流程块1"的流程分支中使用局部变量出错：流程块中的局部变量"\' + str(e).split("\'")[1] + \'"不存在。\')',
                    "        except Exception as e:",
                    '            _logError(\'流程块"流程块1"的流程分支中所使用的判断条件语法出错:\' + str(e))',
                ]
            )
            continue
        if component == "WebBrowser_DataCollectionV5" and outputs:
            lhs = "[" + ", ".join(outputs) + "]"
            lines.append(f"    {lhs} = {call}")
        elif component in {"WebBrowser_CreateV3", "WebBrowser_GetTAB_V1"} and outputs and outputs[0].isidentifier():
            lines.append(f"    {outputs[0]} = {call}")
        elif component == "Excel_Cs_NewV4" and outputs and outputs[0].isidentifier():
            lines.append(f"    {outputs[0]} = {call}")
        elif component == "WebBrowser_RunJsInIeBySelector_v3" and outputs and outputs[0].isidentifier():
            lines.append(f"    {outputs[0]} = {call}")
        elif outputs and all(item.isidentifier() for item in outputs):
            lhs = outputs[0] if len(outputs) == 1 else "[" + ", ".join(outputs) + "]"
            lines.append(f"    {lhs} = {call}")
        else:
            if component == "Excel_Cs_WriteRange" and js_table_mode:
                # RuntimeHost serializes page-JS results before they reach the
                # generated Python module. Excel expects the table value itself,
                # rather than the JSON string that resembles a file path.
                lines.extend(
                    [
                        f"    if isinstance({JS_TABLE_OUTPUT_VARIABLE}, str):",
                        "        try:",
                        f"            {JS_TABLE_OUTPUT_VARIABLE} = json.loads({JS_TABLE_OUTPUT_VARIABLE})",
                        "        except (TypeError, ValueError) as exc:",
                        "            raise ValueError('JS 表格输出不是有效的 JSON 内容') from exc",
                    ]
                )
            lines.append(f"    {call}")
        lines.extend(
            [
                '    if (_sz_kw.get("_sz_process_expr_")):',
                "        try:",
                '            exec(_sz_kw["_sz_process_expr_"])',
                "        except NameError as e:",
                '            _logError(\'流程块"流程块1"的流程分支中使用局部变量出错：流程块中的局部变量"\' + str(e).split("\'")[1] + \'"不存在。\')',
                "        except Exception as e:",
                "            _logError('流程块\"流程块1\"的流程分支中所使用的判断条件语法出错:' + str(e))",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


_NATIVE_GLOBAL_DATA_FALLBACK = """from common.util.load_func_by_module import run_function_from_module
from common.util.get_config_util import get_config, get_out_param
from projects.rpaRoot import SZEnv
import os
import json

ELEMENT_DICT_PATH_NAME = "elements.json"
IMAGE_DICT_PATH_NAME = "images.json"
globalConfig = {}
outputConfig = {}
g_variable = None
globalVar = {"globalConfig": globalConfig, "outputConfig": outputConfig, "g_variable": g_variable}
SHIZAI_ELEMENT_DICT = {}
SHIZAI_IMAGE_DICT = {}
TASK_VARIABLE_MAP = {}
MOBILE_DEVICE_MAP = {}
GLOBAL_CONFIG_PASSWORD = []

def _read_element_map():
    root = os.path.dirname(os.path.dirname(__file__))
    try:
        with open(os.path.join(root, ELEMENT_DICT_PATH_NAME), encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("elementMap", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}

SHIZAI_ELEMENT_DICT = _read_element_map()

def get_global_var(root):
    global_config = get_config(root, "globalVariable", GLOBAL_CONFIG_PASSWORD)
    if global_config:
        globalVar["globalConfig"] = global_config.get("defaultValue", global_config)
    output_config = get_out_param(root, "globalVariable")
    if output_config:
        globalVar["outputConfig"] = output_config

def run_module(module_info, fn_name="main", *args, **kw):
    return run_function_from_module(f"projects.{module_info['module_path']}", fn_name, *args, **kw)

def _logError(*args):
    try:
        import packages.Log.py.exports as Log
        Log.Error(SZEnv["rpa"], "".join(str(item) for item in args))
    except Exception:
        return None
"""


def _native_flow_text(task_path: Path) -> str:
    """Generate the top-level flow dispatcher expected by the runtime."""
    module_name = task_path.stem
    return f"""import sys
import os
from common.util.recursive_util import tail_call_optimized
from common.util.get_config_util import get_config, get_out_param
from common.util.log_util import trace_log
from projects.rpaRoot import SZEnv
from .global_data import globalVar
from .global_data import TASK_VARIABLE_MAP,MOBILE_DEVICE_MAP,GLOBAL_CONFIG_PASSWORD,SHIZAI_ELEMENT_DICT,SHIZAI_IMAGE_DICT
from .global_data import run_module, _logError
self = SZEnv['rpa']
FLOW_QUEUE = []

def flow_node_{module_name}():
    
    sz_count = 0
    overReTry = False
    while True:
        try:
            run_module({{"module_path": "flow_modules.{module_name}"}}, "main", SZEnv['rpa'], _sz_process_expr_="")
            break
        except Exception as e:
            _e = e
            sz_count += 1
            if sz_count > 0:
                overReTry = True
                _error = SZEnv['rpa'].handling_error_info(e)
                break
    if overReTry:
        raise _e

def main(rpa):
    flow_node_{module_name}()
    while len(FLOW_QUEUE) > 0:
        flowName = FLOW_QUEUE.pop(0)
        eval(flowName + "()")
"""


def _native_main_key() -> bytes:
    """Derive the same AES key used by the installed designer's main.sz service."""
    if Cipher is None or algorithms is None:
        raise RuntimeError("cryptography is required to materialize designer main.sz")
    first_digest = hashlib.sha1(_NATIVE_MAIN_SECRET.encode("utf-8")).hexdigest()
    key_hex = hashlib.sha1(bytes.fromhex(first_digest)).hexdigest()[:32]
    return bytes.fromhex(key_hex)


def _encrypt_native_main(code: str) -> str:
    """Encrypt generated RPAProject source in the designer's main.sz format."""
    if padding is None:
        raise RuntimeError("cryptography is required to materialize designer main.sz")
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(code.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_native_main_key()), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return encrypted.hex().upper()


def _decrypt_native_main(value: bytes | str) -> str:
    """Decode a designer main.sz value for local integrity diagnostics."""
    if padding is None:
        raise RuntimeError("cryptography is required to inspect designer main.sz")
    raw = bytes.fromhex(value.decode("ascii") if isinstance(value, bytes) else value.strip())
    decryptor = Cipher(algorithms.AES(_native_main_key()), modes.ECB()).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _native_main_snapshot(root: Path) -> dict[str, Any]:
    """Report whether main.sz is in the designer's encrypted RPAProject format."""
    path = root / "main.sz"
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "encrypted": False,
        "decryptable": False,
        "class_present": False,
        "error": None,
    }
    if not path.is_file():
        result["error"] = "main.sz is missing"
        return result
    try:
        raw = path.read_text(encoding="ascii").strip()
        result["encrypted"] = bool(raw) and len(raw) % 32 == 0 and bool(re.fullmatch(r"[0-9A-Fa-f]+", raw))
        if not result["encrypted"]:
            result["error"] = "main.sz is not an AES hex payload"
            return result
        source = _decrypt_native_main(raw)
        result["decryptable"] = True
        result["class_present"] = "class RPAProject" in source and "def main(self)" in source
        if not result["class_present"]:
            result["error"] = "decrypted main.sz does not contain the RPAProject entry point"
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    return result


def _native_main_text(root: Path, task_path: Path) -> str:
    """Generate the RPAProject entry point stored in the encrypted main.sz."""
    root_literal = json.dumps(str(root.resolve()), ensure_ascii=False)
    task_name = task_path.stem
    return f"""import sys
import os
from common.util.recursive_util import tail_call_optimized
from common.util.decryption_util import decrypt,encryption
from common.util.get_config_util import get_config, get_out_param
from common.util.log_util import trace_log
from common.util.option_util import options
from common.util.elements_util import elementsFormat, elementsFormatNew, collectElementsFormat, child_ele_eval
from common.util.judge_components import *
from common.util.selector_array import get_selector_array,get_selector_array_mobile
from common.util.loop_excel import excel_loop_data
from common.util.custom_code_component_location import custom_code_start, custom_code_end, custom_code_exit
from common.util.loop_code_component_location import *
from common.util.component_public_method import str_encryption
from common.util.pdb_util import process_debug
from common.util.outside_var_util import save_outside_vars, load_outside_vars
from common.util.shared_variables import GetSharedVariable
from common.util.list_util import get_list
from projects.rpaRoot import SZEnv

from projects.global_data import globalVar
from projects.global_data import TASK_VARIABLE_MAP,MOBILE_DEVICE_MAP,GLOBAL_CONFIG_PASSWORD,SHIZAI_ELEMENT_DICT,SHIZAI_IMAGE_DICT
from projects.global_data import run_module, _logError
from projects.global_data import get_global_var
class RPAProject:
    root = {root_literal}
    def task_{task_name}(self):
        return run_module({{ "module_path": "flow_modules.{task_name}" }}, "main", SZEnv['rpa'])
    
    def main(self):
        SZEnv['rpa'] = self
        get_global_var(self.root)
        run_module({{ "module_path": "flow" }}, "main", SZEnv['rpa'])
"""


def _ensure_native_project_flow(root: Path, task_path: Path) -> None:
    """Keep project.flow aligned with the generated flow-block task."""
    flow_path = root / "project.flow"
    try:
        payload = json.loads(flow_path.read_text(encoding="utf-8")) if flow_path.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    task_node = next((item for item in nodes if isinstance(item, dict) and item.get("shape") == "sz-task"), None)
    if not isinstance(task_node, dict):
        task_node = {
            "id": task_path.stem,
            "label": "流程块1",
            "description": "",
            "shape": "sz-task",
            "x": 245,
            "y": 118,
            "width": 160,
            "height": 44,
            "output": "",
            "input": "",
            "useDoc": "",
            "errorVar": "_error",
            "branches": [],
            "reTryCount": "0",
            "arrangementId": f"{task_path.stem}-flowBlock",
        }
        nodes.insert(0, task_node)
    else:
        task_node["id"] = task_path.stem
        task_node.setdefault("label", "流程块1")
        task_node.setdefault("shape", "sz-task")
        task_node.setdefault("arrangementId", f"{task_path.stem}-flowBlock")
    start_node = next((item for item in nodes if isinstance(item, dict) and item.get("shape") == "sz-start"), None)
    if not isinstance(start_node, dict):
        start_node = {
            "id": "hsxAW",
            "label": "开始",
            "description": "",
            "shape": "sz-start",
            "createTime": int(time.time() * 1000),
            "x": 274,
            "y": 45,
            "width": 98,
            "height": 40,
            "var_name": "globalConfig",
            "config": "[]",
            "configType": "local",
        }
        nodes.append(start_node)
    edge = next(
        (
            item
            for item in edges
            if isinstance(item, dict)
            and item.get("source") == start_node.get("id")
            and item.get("target") == task_node.get("id")
        ),
        None,
    )
    if not isinstance(edge, dict):
        edges.append(
            {
                "id": "defaultEdge",
                "label": "",
                "description": "",
                "shape": "line-arrow",
                "createTime": int(time.time() * 1000),
                "vertices": [],
                "source": start_node.get("id"),
                "target": task_node.get("id"),
                "styleType": "normal",
                "sourceAnchor": 0,
                "targetAnchor": 0,
            }
        )
    payload["nodes"] = nodes
    payload["edges"] = edges
    flow_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _ensure_native_edit_state(root: Path, task_path: Path) -> None:
    """Create the editor arrangement metadata expected by the designer."""
    path = root / "edit-state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    editor_id = f"{task_path.stem}-flowBlock"
    arrangements = payload.get("arrangementInfoList") if isinstance(payload.get("arrangementInfoList"), list) else []
    arrangement = next(
        (item for item in arrangements if isinstance(item, dict) and item.get("sourceId") == task_path.stem), None
    )
    if not isinstance(arrangement, dict):
        arrangements.append(
            {"editorId": editor_id, "sourceId": task_path.stem, "type": "flowBlock", "label": "流程块1"}
        )
    else:
        arrangement.update({"editorId": editor_id, "type": "flowBlock", "label": arrangement.get("label") or "流程块1"})
    payload["arrangementInfoList"] = arrangements
    payload["currentEditorId"] = editor_id
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _update_native_file_list(root: Path, task_path: Path) -> None:
    """Write the package file index shown by the designer's flow navigator."""
    try:
        stat = task_path.stat()
        modified = stat.st_mtime * 1000
        size = stat.st_size
        digest = hashlib.md5(task_path.read_bytes()).hexdigest()
    except OSError:
        modified = time.time() * 1000
        size = 0
        digest = ""
    path = root / "fileList.json"
    payload = {
        "flowBlockList": [
            {
                "id": task_path.stem,
                "name": "流程块1",
                "pathName": "流程块1",
                "lastModifyTime": modified,
                "fileSize": size,
                "fileMd5": digest,
            }
        ],
        "childModuleList": [],
        "codeModuleList": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _sync_native_designer_content(
    paths: ShizaiPaths,
    root: Path,
    task_path: Path,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Persist generated content through the live designer when its package is known."""
    package = _native_package_summary(paths, root)
    result: dict[str, Any] = {
        "mode": "local_encrypted_fallback",
        "package": package,
        "rpc": {},
        "errors": [],
    }
    if not package.get("package_id") or client is None:
        if package.get("package_id") and client is None:
            result["errors"].append("designer RPC client was not supplied; local artifacts were refreshed only")
        return result
    try:
        from . import agent as agent_core

        result["rpc"]["savePackageCode"] = agent_core.save_package_code(
            str(package["package_id"]),
            _native_main_text(root, task_path),
            client=client,
        )
        flow_path = root / "project.flow"
        edit_path = root / "edit-state.json"
        variable_path = root / "variableInfo"
        flow_payload = json.loads(flow_path.read_text(encoding="utf-8"))
        edit_payload = json.loads(edit_path.read_text(encoding="utf-8"))
        variable_payload = json.loads(variable_path.read_text(encoding="utf-8"))
        result["rpc"]["savePackageFlow"] = agent_core.save_package_flow(
            str(package["package_id"]),
            flow_payload,
            client=client,
        )
        result["rpc"]["saveEditState"] = agent_core.save_edit_state(
            str(package["package_id"]),
            edit_payload,
            client=client,
        )
        result["rpc"]["saveVariableInfo"] = agent_core.save_variable_info(
            str(package["package_id"]),
            variable_payload,
            client=client,
        )
        result["mode"] = "designer_rpc_and_local"
    except Exception as exc:
        # The local task and encrypted runtime tree are already durable.  A
        # client build may omit one of the optional package-content RPCs, so
        # keep the native flow usable and expose the sync failure to the
        # caller instead of losing the completed local design.
        result["errors"].append(str(exc))
    return result


def _native_collection_paging_snapshot(payload: dict[str, Any]) -> dict[str, str]:
    """Read paging arguments from the first native collection component."""
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return {}
    collection = next(
        (
            node
            for node in entities.values()
            if isinstance(node, dict)
            and node.get("type") == "WebBrowser_DataCollectionV5"
        ),
        None,
    )
    if not isinstance(collection, dict):
        return {}
    return {
        name: str(_native_task_argument(collection, name, "") or "").strip()
        for name in (
            "region",
            "collect_pages",
            "collect_items",
            "page_type",
            "page_type_num",
            "selector_page",
            "selector_more",
        )
    }


def _restore_native_collection_paging(
    project_dir: Path,
    desired: dict[str, str],
) -> dict[str, Any]:
    """Restore paging fields that an older designer drops during RPC save."""
    if not desired:
        return {"changed": False, "restored": {}}
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    collection = next(
        (
            node
            for node in entities.values()
            if isinstance(node, dict)
            and node.get("type") == "WebBrowser_DataCollectionV5"
        ),
        None,
    ) if isinstance(entities, dict) else None
    if not isinstance(collection, dict):
        return {"changed": False, "restored": {}, "reason": "no collection component"}
    restored: dict[str, str] = {}
    for name, value in desired.items():
        normalized = str(value or "").strip()
        current = str(_native_task_argument(collection, name, "") or "").strip()
        if current == normalized:
            continue
        # Optional paging slots are args0 in the supported native component;
        # use the generic setter so a legacy template missing a hidden slot is
        # repaired into a valid task rather than silently losing the value.
        _native_set_argument(collection, name, normalized)
        restored[name] = normalized
    if not restored:
        return {"changed": False, "restored": {}}
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(task_path)
    _update_native_task_index(project_dir, task_path)
    _update_native_file_list(project_dir, task_path)
    return {"changed": True, "restored": restored, "task": str(task_path)}


def _ensure_native_runtime_support(
    paths: ShizaiPaths,
    root: Path,
    task_path: Path,
    *,
    client: Any | None = None,
) -> list[str]:
    """Create the small generated-code support tree absent from a new application."""
    projects = root / "projects"
    flow_modules = projects / "flow_modules"
    flow_modules.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if _normalize_client_metadata(root):
        created.append(str(root / "szrpa.json"))
    normalized_elements = _normalize_native_element_catalog(root)
    if normalized_elements:
        created.append(str(root / "elements.json"))
    source_global: Path | None = None
    source_root = root.resolve()
    if paths.applications_root.is_dir():
        for candidate in sorted(paths.applications_root.iterdir(), key=lambda item: str(item).lower()):
            if not candidate.is_dir() or candidate.resolve() == source_root:
                continue
            candidate_global = candidate / "projects" / "global_data.py"
            if candidate_global.is_file():
                source_global = candidate_global
                break
    global_target = projects / "global_data.py"
    needs_global_support = True
    if global_target.is_file():
        try:
            needs_global_support = "def get_global_var" not in global_target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            needs_global_support = True
    if needs_global_support:
        if source_global:
            global_target.write_text(source_global.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            global_target.write_text(_NATIVE_GLOBAL_DATA_FALLBACK, encoding="utf-8")
        created.append(str(global_target))
    rpa_root = projects / "rpaRoot.py"
    if not rpa_root.is_file():
        rpa_root.write_text('SZEnv = {"rpa": None}\n', encoding="utf-8")
        created.append(str(rpa_root))
    init_file = projects / "__init__.py"
    if not init_file.is_file():
        init_file.write_text("", encoding="utf-8")
        created.append(str(init_file))
    module_path = flow_modules / f"{task_path.stem}.py"
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    metadata_ok, metadata, _ = _safe_json(root / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    mcp_managed = any(
        metadata.get(key)
        for key in (
            "mcpTargetUrl",
            "mcpRequiredColumns",
            "mcpRefreshBrowserTab",
            "mcpResultElementsReady",
            "mcpUseJsTable",
        )
    )
    component_plan, plan_source = _rpa_component_plan(metadata)
    plan_backed = bool(
        component_plan
        and (
            metadata.get("mcpPlanComplete")
            or metadata.get("mcpPlanId")
            or metadata.get("mcpExecutionPlan")
        )
    )
    mcp_use_js_table = bool(metadata.get("mcpUseJsTable"))
    entities = task_payload.get("componentModelEntities")
    task_changed = False
    if isinstance(entities, dict):
        # Normalize existing nodes as well as template nodes. This repairs
        # tasks written by older CLI versions before the runtime module is
        # regenerated, so the designer and the generated code see the same
        # native argument shapes.
        for node in entities.values():
            if not isinstance(node, dict):
                continue
            before = json.dumps(node, ensure_ascii=False, sort_keys=True)
            _native_normalize_node(node)
            if before != json.dumps(node, ensure_ascii=False, sort_keys=True):
                task_changed = True
    plan_collection_config: dict[str, Any] = {}
    plan_output_contract: dict[str, Any] = {}
    if plan_backed:
        execution_plan = metadata.get("mcpExecutionPlan")
        if isinstance(execution_plan, dict):
            raw_contract = execution_plan.get("output_contract")
            if isinstance(raw_contract, dict):
                plan_output_contract = raw_contract
        for planned in component_plan:
            if (
                isinstance(planned, dict)
                and planned.get("component") == "WebBrowser_DataCollectionV5"
                and isinstance(planned.get("config"), dict)
            ):
                plan_collection_config = dict(planned["config"])
                break
        field_candidates = (
            plan_output_contract.get("fields"),
            plan_output_contract.get("required_fields"),
            plan_output_contract.get("columns"),
            plan_collection_config.get("columns"),
        )
        planned_fields = next(
            (
                candidate
                for candidate in field_candidates
                if isinstance(candidate, list)
                and candidate
                and not any(
                    re.search(r"^[A-Za-z]:[\\/]|[\\/]|(?:写入|保存|导出).*(?:目录|文件)", str(item))
                    for item in candidate
                )
            ),
            None,
        )
        if isinstance(planned_fields, list) and planned_fields:
            plan_collection_config["columns"] = [
                str(item).strip() for item in planned_fields if str(item).strip()
            ]
        planned_output = (
            plan_output_contract.get("file")
            or plan_output_contract.get("filename")
            or plan_output_contract.get("file_path")
        )
        if planned_output:
            plan_collection_config["output_file"] = planned_output
        for source_name in ("destination", "output_path", "filePath"):
            if plan_output_contract.get(source_name) and not plan_collection_config.get("destination"):
                plan_collection_config["destination"] = plan_output_contract[source_name]
                break
        if plan_output_contract.get("format") and not plan_collection_config.get("filename_suffix"):
            plan_collection_config["filename_suffix"] = [
                Path(str(planned_output or "result")).stem,
                "." + str(plan_output_contract["format"]).lstrip("."),
            ]
    if isinstance(entities, dict):
        if _native_normalize_collection_config(task_payload):
            task_changed = True
        for node in entities.values():
            if not isinstance(node, dict) or node.get("type") != "WebBrowser_DataCollectionV5":
                continue
            node_plan_collection_config = plan_collection_config
            if plan_backed:
                planned_for_node = next(
                    (
                        item
                        for item in component_plan
                        if isinstance(item, dict)
                        and str(item.get("id") or "").strip() == str(node.get("id") or "").strip()
                        and item.get("component") == "WebBrowser_DataCollectionV5"
                        and isinstance(item.get("config"), dict)
                    ),
                    None,
                )
                if isinstance(planned_for_node, dict):
                    node_plan_collection_config = dict(planned_for_node["config"])
            # A model-backed plan owns its component parameters. Apply its
            # concrete values when repairing an older task, while leaving
            # deferred selectors empty until live page evidence is captured.
            if mcp_managed:
                collection_source = dict(node_plan_collection_config if plan_backed else metadata)
                if not plan_backed:
                    task_paging = {
                        name: _native_task_argument(node, name, "")
                        for name in (
                            "region",
                            "collect_pages",
                            "collect_items",
                            "page_type",
                            "page_type_num",
                            "selector_page",
                            "selector_more",
                        )
                    }
                    metadata_aliases = {
                        "region": "mcpCollectionRegion",
                        "collect_items": "mcpMaxRows",
                        "page_type": "mcpCollectionPageType",
                        "page_type_num": "mcpCollectionPageTypeNum",
                    }
                    for name, value in task_paging.items():
                        alias = metadata_aliases.get(name)
                        if name not in collection_source and (not alias or alias not in collection_source):
                            collection_source[name] = value
                collection_source = _materialize_collection_paging_selectors(
                    paths,
                    str(root),
                    collection_source,
                )
                collection_paging = _collection_paging_settings(collection_source)
                # Some native designer versions do not persist the optional
                # paging selector in szrpa.json. Keep the selector already
                # captured in the task unless the caller supplied an
                # explicit replacement/clear value.
                if collection_paging.get("region") != "1":
                    for name, aliases in (
                        ("selector_page", ("selector_page", "next_page_selector")),
                        ("selector_more", ("selector_more", "load_more_selector")),
                    ):
                        explicit = any(
                            alias in collection_source
                            for alias in aliases
                        ) if isinstance(collection_source, dict) else False
                        existing = _native_task_argument(node, name, "")
                        if not explicit and isinstance(existing, str) and existing.strip():
                            collection_paging[name] = existing.strip()
                for name in ("page_type_num", "selector_page", "selector_more"):
                    value = collection_paging.get(name)
                    if isinstance(value, str) and value.startswith("$"):
                        collection_paging[name] = "0" if name == "page_type_num" else ""
                for argument in node.get("args0", []):
                    if not isinstance(argument, dict):
                        continue
                    name = argument.get("name")
                    if name in collection_paging and argument.get("value") != collection_paging[name]:
                        argument["value"] = collection_paging[name]
                        task_changed = True
                if plan_backed:
                    output_file = _collection_output_file(
                        node_plan_collection_config,
                        fallback=metadata.get("mcpOutputFile") or "result.xlsx",
                    )
                    output_path = Path(output_file)
                    suffix = [
                        json.dumps(output_path.stem or "result", ensure_ascii=False),
                        json.dumps(
                            output_path.suffix
                            if output_path.suffix.casefold() in _NATIVE_OUTPUT_SUFFIXES
                            else ".xlsx",
                            ensure_ascii=False,
                        ),
                    ]
                    if _native_task_argument(node, "filename_suffix") != suffix:
                        _native_set_argument(node, "filename_suffix", suffix)
                        task_changed = True
                    configured = _native_configured_output_path(output_file)
                    file_path = (
                        str(configured.parent)
                        if configured is not None
                        else str(node_plan_collection_config.get("filePath") or "res")
                    )
                    encoded_path = json.dumps(file_path, ensure_ascii=False)
                    if _native_task_argument(node, "filePath") != encoded_path:
                        try:
                            _native_set_parameter(node, "filePath", encoded_path)
                        except ValueError:
                            _native_set_argument(node, "filePath", encoded_path)
                        task_changed = True
                    metadata_updates = {
                        "mcpRequiredColumns": node_plan_collection_config.get(
                            "columns", metadata.get("mcpRequiredColumns", [])
                        ),
                        "mcpOutputFile": output_path.name or "result.xlsx",
                        "mcpOutputPath": str(configured) if configured else None,
                    }
                    if plan_output_contract.get("max_rows") not in (None, ""):
                        metadata_updates["mcpMaxRows"] = _normalize_max_rows(
                            plan_output_contract["max_rows"],
                            default=metadata.get("mcpMaxRows", 0),
                        )
                    current_paging = _collection_paging_settings(node_plan_collection_config)
                    metadata_updates.update(
                        {
                            "mcpCollectionRegion": current_paging.get("region", "1"),
                            "mcpCollectionPageType": current_paging.get("page_type", "1"),
                            "mcpCollectionPageTypeNum": current_paging.get("page_type_num", "0"),
                        }
                    )
                    _update_native_metadata(root, len(_native_statement_ids(task_payload)), metadata_updates)
        for node in entities.values():
            if not isinstance(node, dict) or node.get("type") != "WebBrowser_RunJsInIeBySelector_v3":
                continue
            for argument in node.get("args0", []):
                if not isinstance(argument, dict) or argument.get("name") != "input":
                    continue
                normalized = _native_list_expression(
                    argument.get("value"),
                    ".plugin_goodsCardWrapper, .gl-item",
                )
                if argument.get("value") != normalized:
                    argument["value"] = normalized
                    task_changed = True
            if mcp_managed and not plan_backed:
                for argument in node.get("args0", []):
                    if not isinstance(argument, dict):
                        continue
                    name = argument.get("name")
                    if name == "var_name":
                        expected = JS_TABLE_OUTPUT_VARIABLE if mcp_use_js_table else "商品链接"
                        if argument.get("value") != expected:
                            argument["value"] = expected
                            task_changed = True
                    elif name == "jsCode":
                        expected_code = (
                            _js_table_script(
                                metadata.get("mcpRequiredColumns", DEFAULT_RPA_COLUMNS),
                                metadata.get("mcpMaxRows"),
                            )
                            if mcp_use_js_table
                            else DEFAULT_JS_LINK_SCRIPT
                        )
                        current = str(argument.get("value") or "")
                        if current.strip().startswith('"""'):
                            expected_code = f'"""{expected_code}"""'
                        if current != expected_code:
                            argument["value"] = expected_code
                            task_changed = True
                    elif name == "web_page" and argument.get("value") != "网页对象":
                        argument["value"] = "网页对象"
                        task_changed = True
                    elif name == "executionEnvironment" and argument.get("value") != '"MAIN"':
                        argument["value"] = '"MAIN"'
                        task_changed = True
    if task_changed:
        temporary = task_path.with_suffix(task_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(task_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(task_path)
        created.append(str(task_path))
    module_text = _native_module_text(task_payload, task_path)
    module_path.write_text(module_text, encoding="utf-8")
    created.append(str(module_path))
    flow_path = projects / "flow.py"
    flow_path.write_text(_native_flow_text(task_path), encoding="utf-8")
    created.append(str(flow_path))
    main_path = root / "main.sz"
    main_path.write_text(_encrypt_native_main(_native_main_text(root, task_path)), encoding="ascii")
    created.append(str(main_path))
    sourcemap = root / "file_sourcemap.json"
    sourcemap.write_text(
        json.dumps(
            {"files": {f"flow_modules/{task_path.stem}.py": f"main|1|{len(module_text.splitlines())}|0"}},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    variable_info = root / "variableInfo"
    try:
        variable_date = int(task_path.stat().st_mtime * 1000)
    except OSError:
        variable_date = int(time.time() * 1000)
    variable_info.write_text(
        json.dumps(
            {
                "sortInfo": {},
                "variableMap": _native_variable_analysis_map(
                    task_payload,
                    source_text=module_text,
                    date=variable_date,
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    created.append(str(variable_info))
    _ensure_native_project_flow(root, task_path)
    _ensure_native_edit_state(root, task_path)
    _update_native_task_index(root, task_path)
    _update_native_file_list(root, task_path)
    return created


def _finalize_native_writeback(
    paths: ShizaiPaths,
    project_dir: Path,
    task_path: Path,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Materialize, sync, and read back one native write as one CLI boundary."""
    generated = _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    designer_sync = _sync_native_designer_content(paths, project_dir, task_path, client=client)
    consistency = _native_designer_state_consistency(project_dir, task_path)
    package = designer_sync.get("package") if isinstance(designer_sync, dict) else {}
    sync_errors = designer_sync.get("errors") if isinstance(designer_sync, dict) else []
    if client is not None and isinstance(package, dict) and package.get("matched") and sync_errors:
        consistency["conflict"] = True
        consistency["valid"] = False
        consistency["errors"] = list(consistency.get("errors") or []) + [
            "designer RPC synchronization failed: " + "; ".join(str(item) for item in sync_errors)
        ]
    if consistency.get("conflict"):
        detail = "; ".join(str(item) for item in consistency.get("errors") or [])
        raise ValueError("designer_state_conflict: " + (detail or "native task and designer artifacts disagree"))
    return {
        "generated_files": sorted(set(generated)),
        "designer_sync": designer_sync,
        "consistency": consistency,
        "readback": _native_rpa_state(paths, str(project_dir)),
    }


def materialize_native_rpa(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    strict: bool = True,
    client: Any | None = None,
) -> dict[str, Any]:
    """Materialize the generated-code tree required by the native RPA runtime.

    The designer persists the task graph first and may leave the generated
    ``projects`` tree and ``file_index_700.json`` for a later client action.
    This public, repeatable operation makes that boundary explicit so an MCP
    client can repair a valid task before asking the runtime to execute it.
    It also backfills editor compatibility metadata for legacy MCP packages.
    """
    project_dir = resolve_project(paths, project_ref)
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    _, plan_source = _rpa_component_plan(metadata)
    _publish_rpa_ui(
        "materializing",
        "runtime_materialize",
        "正在生成流程运行树、变量信息、源码映射和文件索引。",
        animation="materialize_progress",
        flow_ref=str(project_dir),
    )
    task_path, _ = _normalize_native_page_scope_js(project_dir)
    _, task_payload, _ = _safe_json(task_path)
    if not isinstance(task_payload, dict):
        raise ValueError(f"native task is not a JSON object: {task_path}")
    generated = _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    # Snapshot the task after local normalization. The live designer may
    # round-trip hidden paging fields as empty strings during RPC save.
    _, normalized_payload = _native_task_file(project_dir)
    paging_before_sync = _native_collection_paging_snapshot(normalized_payload)
    designer_sync = _sync_native_designer_content(paths, project_dir, task_path, client=client)
    paging_repair = _restore_native_collection_paging(project_dir, paging_before_sync)
    if paging_repair.get("changed"):
        # The task is authoritative after repair; regenerate the derived
        # module, encrypted entrypoint and indexes before validating.
        generated.extend(
            _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
        )
    designer_sync["paging_repair"] = paging_repair
    validation = validate_rpa_project(paths, str(project_dir), strict=strict)
    index = _native_index_snapshot(project_dir, task_path)
    variable_info = _native_variable_info_snapshot(project_dir)
    main_snapshot = _native_main_snapshot(project_dir)
    required_paths = [
        project_dir / "file_index_700.json",
        project_dir / "file_sourcemap.json",
        project_dir / "fileList.json",
        project_dir / "edit-state.json",
        project_dir / "variableInfo",
        project_dir / "projects" / "__init__.py",
        project_dir / "projects" / "global_data.py",
        project_dir / "projects" / "rpaRoot.py",
        project_dir / "projects" / "flow.py",
        project_dir / "projects" / "flow_modules" / f"{task_path.stem}.py",
    ]
    missing = [str(path) for path in required_paths if not path.is_file()]
    ready = (
        bool(validation.get("valid"))
        and main_snapshot["decryptable"]
        and main_snapshot["class_present"]
        and variable_info["valid"]
        and not missing
        and bool(
            index.get("index_valid")
            and index.get("module_exists")
            and index.get("task_index_fresh")
            and index.get("code_index_fresh")
        )
    )
    ui_feedback = _publish_rpa_ui(
        "runtime_ready" if ready else "materialize_incomplete",
        "runtime_materialize",
        "流程运行树已生成，可以进行运行前诊断。"
        if ready
        else "运行树生成未完成，请根据 validation 或 missing_files 修复。",
        animation="materialize_complete" if ready else "materialize_error",
        flow_ref=str(project_dir),
        ready_for_runtime_diagnosis=ready,
        generated_file_count=len(set(generated)),
        next_tool="shizai_rpa_sync_diagnose" if ready else "shizai_rpa_materialize",
    )
    return {
        "kind": "native_rpa_materialized",
        "project": str(project_dir),
        "plan_source": plan_source,
        "task": {
            "path": str(task_path),
            "node_id": task_path.stem,
            "statement_count": len(_native_statement_ids(task_payload)),
        },
        "generated_files": sorted(set(generated)),
        "required_files": [str(path) for path in required_paths],
        "missing_files": missing,
        "main": main_snapshot,
        "variable_info": variable_info,
        "index": index,
        "designer_sync": designer_sync,
        "validation": validation,
        "ready_for_runtime_diagnosis": ready,
        "ui_feedback": ui_feedback,
        "next_tool": "shizai_rpa_sync_diagnose"
        if ready
        else ("shizai_rpa_validate_project" if not validation.get("valid") else "shizai_rpa_materialize"),
        "message": (
            "设计器流程运行树已生成；下一步调用 shizai_rpa_sync_diagnose。"
            if ready
            else "运行树仍有缺失或流程校验未通过，请根据 validation/missing_files 修复后重试。"
        ),
    }


def _native_statement_ids(payload: dict[str, Any]) -> list[str]:
    """Return the ordered statement ids from a native task payload."""
    entities = payload.get("componentModelEntities")
    main = entities.get("MAIN_COMPONENT") if isinstance(entities, dict) else None
    if not isinstance(main, dict):
        return []
    for argument in main.get("args0", []):
        if isinstance(argument, dict) and argument.get("name") == "statements":
            value = argument.get("value")
            if isinstance(value, list):
                return [str(item) for item in value]
    return []


def _accepted_web_page_object_names(
    payload: dict[str, Any] | None,
    step_index: int,
    planned_step: dict[str, Any],
) -> list[str]:
    """Names that may bind a page component to its upstream browser object.

    The designer stores the browser node's own output variable in the consumer's
    ``web_page`` / ``designated_obj`` slot, so a plan that renames the browser
    object must not be rejected against the canonical default alone.
    """
    accepted: list[str] = []
    statements = _native_statement_ids(payload) if isinstance(payload, dict) else []
    if 0 < step_index <= len(statements):
        entities = payload.get("componentModelEntities") if isinstance(payload, dict) else None
        predecessor = entities.get(statements[step_index - 1]) if isinstance(entities, dict) else None
        if isinstance(predecessor, dict) and str(predecessor.get("type") or "") in RPA_BROWSER_OBJECT_COMPONENTS:
            name = _decode_selector_value(_native_task_argument(predecessor, "var_name", ""))
            if name:
                accepted.append(name)
    for item in planned_step.get("outputs") or []:
        name = str(item).strip()
        if name:
            accepted.append(name)
    accepted.append(RPA_DEFAULT_WEB_PAGE_OBJECT)
    return list(dict.fromkeys(accepted))


def _native_component_entities(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return native component entities keyed by their ids."""
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        return {}
    return {
        str(node_id): node
        for node_id, node in entities.items()
        if isinstance(node, dict) and str(node.get("type") or "") in _NATIVE_RPA_COMPONENTS
    }


def _native_element_ids_from_node(node: dict[str, Any]) -> list[str]:
    """Extract element ids embedded in native selector expressions."""
    found: list[str] = []
    for argument in node.get("args0", []):
        if not isinstance(argument, dict):
            continue
        value = argument.get("value")
        if isinstance(value, str):
            for element_id in _NATIVE_ELEMENT_RE.findall(value):
                if element_id not in found:
                    found.append(element_id)
    return found


def _native_collection_element_ids(node: dict[str, Any], elements: dict[str, Any]) -> list[str]:
    """Match DataCollection selector chains back to imported native element ids."""
    config_value = _native_task_argument(node, "config")
    if not isinstance(config_value, str):
        return []
    try:
        config = json.loads(config_value)
    except json.JSONDecodeError:
        return []
    collection = config.get("collectionConfig") if isinstance(config, dict) else None
    selectors = collection.get("columnSelectorArray") if isinstance(collection, dict) else None
    element_map = elements.get("elementMap") if isinstance(elements, dict) else None
    if not isinstance(selectors, list) or not isinstance(element_map, dict):
        return []
    declared_ids = node.get("mcpElementIds")
    if isinstance(declared_ids, list):
        declared = [str(item).strip() for item in declared_ids if str(item).strip()]
        if len(declared) == len(selectors) and len(declared) == len(set(declared)):
            if all(item in element_map for item in declared):
                return declared
    normalized: dict[str, str] = {}
    relaxed: dict[str, list[str]] = {}

    def signature(chain: list[dict[str, Any]], *, ignore_indexes: bool = False) -> str:
        """Build a stable comparison key for a converted web selector chain."""
        value: list[dict[str, Any]] = []
        for item in chain:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            props = []
            for prop in item.get("props", []):
                if not isinstance(prop, dict):
                    continue
                if ignore_indexes and str(prop.get("name") or "").lower() == "@index":
                    continue
                props.append(dict(prop))
            copied["props"] = props
            value.append(copied)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    for element_id, item in element_map.items():
        chain = _collection_selector_chain(item) if isinstance(item, dict) else None
        if chain:
            element_key = str(element_id)
            normalized[signature(chain)] = element_key
            relaxed.setdefault(signature(chain, ignore_indexes=True), []).append(element_key)
    result: list[str] = []
    for selector in selectors:
        if not isinstance(selector, list):
            continue
        key = signature(selector)
        element_id = normalized.get(key)
        if not element_id:
            candidates = relaxed.get(signature(selector, ignore_indexes=True), [])
            element_id = next((item for item in candidates if item not in result), None)
        if element_id and element_id not in result:
            result.append(element_id)
    return result


def _native_output_file(node: dict[str, Any], default: str = "result.xlsx") -> str:
    """Read the output filename encoded by DataCollectionV5."""
    suffix = _native_task_argument(node, "filename_suffix")
    if isinstance(suffix, list) and suffix:
        stem = _decode_selector_value(suffix[0])
        extension = _decode_selector_value(suffix[1]) if len(suffix) > 1 else ".xls"
        if stem:
            return stem + (extension if extension.startswith(".") else "." + extension)
    return default


def _native_component_parameter_names(node: dict[str, Any]) -> set[str]:
    """Return every argument/option exposed by a native component node."""
    names: set[str] = set()
    for key in ("args0", "options0"):
        entries = node.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and str(entry.get("name") or "").strip():
                names.add(str(entry["name"]).strip())
    return names


def _native_config_value(value: Any) -> Any:
    """Decode common native literal forms for the agent-facing flow state."""
    if isinstance(value, list):
        return [_native_config_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _native_config_value(item) for key, item in value.items()}
    if not isinstance(value, str):
        return deepcopy(value)
    text = value.strip()
    if not text:
        return ""
    parsed_object = _native_parse_object_value(text)
    if parsed_object is not value:
        return _native_config_value(parsed_object)
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        return _decode_selector_value(text)
    if text[0] in "[{" and text[-1] in "]}":
        try:
            return _native_config_value(json.loads(text))
        except (TypeError, json.JSONDecodeError):
            pass
    return value


def _native_component_config(node: dict[str, Any]) -> dict[str, Any]:
    """Expose all native arguments/options without a component-specific whitelist."""
    config: dict[str, Any] = {}
    for key in ("args0", "options0"):
        entries = node.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and str(entry.get("name") or "").strip():
                config[str(entry["name"]).strip()] = _native_config_value(entry.get("value"))
    return config


def _collection_output_file(config: dict[str, Any] | None, fallback: Any = "result.xlsx") -> str:
    """Resolve a collection output file from semantic config and destination.

    Client models commonly provide a directory in ``filePath``/``destination``
    and a native ``filename_suffix`` pair. Keep that form intact while giving
    the runtime one concrete file path for output evidence.
    """
    source = config if isinstance(config, dict) else {}
    raw = source.get("output_file") or source.get("file") or source.get("filename")
    suffix = source.get("filename_suffix")
    stem = "result"
    extension = ".xlsx"
    if isinstance(suffix, (list, tuple)) and suffix:
        decoded_stem = _decode_selector_value(suffix[0])
        decoded_extension = _decode_selector_value(suffix[1]) if len(suffix) > 1 else ""
        if decoded_stem:
            stem = decoded_stem
        if decoded_extension:
            extension = decoded_extension if decoded_extension.startswith(".") else f".{decoded_extension}"
    if raw not in (None, ""):
        raw_text = str(raw).strip()
        raw_path = Path(raw_text).expanduser()
        if raw_path.suffix.casefold() in _NATIVE_OUTPUT_SUFFIXES:
            destination = source.get("filePath") or source.get("output_path") or source.get("destination")
            if destination not in (None, "") and not raw_path.is_absolute() and raw_path.parent == Path("."):
                return str(Path(str(destination).strip()).expanduser() / raw_path.name)
            return raw_text
        if raw_path.is_absolute():
            return str(raw_path / f"{stem}{extension}")
        return raw_text
    destination = source.get("filePath") or source.get("output_path") or source.get("destination")
    if destination not in (None, ""):
        destination_text = str(destination).strip()
        destination_path = Path(destination_text).expanduser()
        if destination_path.suffix.casefold() in _NATIVE_OUTPUT_SUFFIXES:
            return destination_text
        return str(destination_path / f"{stem}{extension}")
    fallback_text = str(fallback or "result.xlsx").strip() or "result.xlsx"
    return fallback_text


def _collection_to_excel_setting(
    config: dict[str, Any] | None,
    *,
    excel_mode: bool,
    fallback: Any = 1,
) -> int:
    """Resolve direct file output without changing it during an element rebind."""
    if excel_mode:
        return 0
    source = config if isinstance(config, dict) else {}
    return int(_native_bool(source.get("toExcel", fallback), default=_native_bool(fallback, default=True)))


def _native_rpa_state(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Build the same agent-facing flow state from the real native task."""
    project_dir = resolve_project(paths, project_ref)
    task_path, payload = _native_task_file(project_dir)
    try:
        _, elements = _element_file(paths, str(project_dir))
    except (FileNotFoundError, OSError, ValueError):
        # A debug session can be created before the first browser capture. Keep
        # the flow state readable so DAP setup can report the real next action.
        elements = {"elementMap": {}, "groupMap": {}}
    entities = payload.get("componentModelEntities", {})
    statements = _native_statement_ids(payload)
    steps: list[dict[str, Any]] = []
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    mcp_managed = any(str(key).startswith("mcp") for key in metadata)
    component_plan, plan_source = _rpa_component_plan(metadata)
    plan_complete = bool(
        metadata.get("mcpPlanComplete")
        or metadata.get("mcpPlanId")
        or metadata.get("mcpExecutionPlan")
    )
    # A partial model draft may already carry candidate steps.  It is not an
    # ordering/configuration contract until the validated execution plan is
    # explicitly attached.
    plan_backed = bool(component_plan and plan_complete)
    target_url = str(metadata.get("mcpTargetUrl") or "")
    keyword = str(metadata.get("mcpKeyword") or "")
    output_file = str(metadata.get("mcpOutputFile") or "result.xlsx")
    configured_output_path = str(metadata.get("mcpOutputPath") or "").strip()
    default_columns = [] if plan_backed else DEFAULT_RPA_COLUMNS
    required_columns = [
        str(item).strip() for item in metadata.get("mcpRequiredColumns", default_columns) if str(item).strip()
    ]
    if not required_columns and not plan_backed:
        required_columns = list(DEFAULT_RPA_COLUMNS)
    excel_mode = bool(metadata.get("mcpExcelMode", False))
    use_js_link = bool(metadata.get("mcpUseJsLink", False))
    use_js_table = bool(metadata.get("mcpUseJsTable", False))
    max_rows = _normalize_max_rows(metadata.get("mcpMaxRows"), default=0)
    wait_after_submit = bool(metadata.get("mcpWaitAfterSubmit", False))
    wait_seconds = max(0.0, min(float(metadata.get("mcpWaitSeconds", 1) or 0), 30.0))
    refresh_browser_tab = _native_bool(metadata.get("mcpRefreshBrowserTab", False), default=False)
    result_elements_ready = bool(metadata.get("mcpResultElementsReady", False))
    request_analysis = metadata.get("mcpRequestAnalysis")
    if not isinstance(request_analysis, dict):
        request_analysis = None
    submit_component = str(metadata.get("mcpSubmitComponent") or "").strip()
    open_new_tab = _native_bool(metadata.get("mcpOpenNewTab"), default=False)
    if submit_component not in {"Keyboard_PressNew", "Keyboard_Send_V1"}:
        submit_component = ""
    for node_id in statements:
        node = entities.get(node_id) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        if not plan_backed and component not in _NATIVE_RPA_COMPONENTS:
            # Keep any materializable live-catalog component visible in a
            # model-owned partial flow.  The old compatibility set only
            # covers the browser recipe and must not hide other components.
            if _live_rpa_contract(paths, component) is None:
                continue
        planned_step = (
            component_plan[len(steps)]
            if len(steps) < len(component_plan) and isinstance(component_plan[len(steps)], dict)
            else {}
        )
        if len(steps) == 2 and component in {"Keyboard_PressNew", "Keyboard_Send_V1"}:
            submit_component = component
        if component == "WebBrowser_CreateV3":
            target_url = _decode_selector_value(_native_task_argument(node, "url", target_url)) or target_url
            open_new_tab = _native_bool(
                _native_task_argument(node, "openNewTab", open_new_tab),
                default=open_new_tab,
            )
        if component == "Element_SetValue_V6":
            keyword = _decode_selector_value(_native_task_argument(node, "text", keyword)) or keyword
        if component == "WebBrowser_RunJsInIeBySelector_v3":
            js_output = _decode_selector_value(_native_task_argument(node, "var_name", "商品链接"))
            if plan_backed:
                js_mode = str(metadata.get("mcpJsOutputMode") or "")
                use_js_table = js_mode == "table"
                use_js_link = bool(metadata.get("mcpUseJsLink", False))
            elif use_js_table or js_output == JS_TABLE_OUTPUT_VARIABLE:
                use_js_table = True
                use_js_link = False
            else:
                use_js_link = True
        if component == "WebBrowser_DataCollectionV5" and not excel_mode:
            output_file = _native_output_file(node, output_file)
        element_ids = _native_element_ids_from_node(node)
        if component == "WebBrowser_DataCollectionV5":
            element_ids = _native_collection_element_ids(node, elements)
        step_config: dict[str, Any] = {}
        if component == "WebBrowser_CreateV3":
            open_new_tab = _native_bool(_native_task_argument(node, "openNewTab", 0))
            step_config = {
                "var_name": _decode_selector_value(_native_task_argument(node, "var_name", "网页对象"))
                or "网页对象",
                "url": target_url,
                "type": "chrome",
                "openNewTab": open_new_tab,
            }
        elif component == "Element_SetValue_V6":
            step_config = {
                "text": _decode_selector_value(_native_task_argument(node, "text", keyword)) or keyword,
                "designated_obj": _decode_selector_value(_native_task_argument(node, "designated_obj", "网页对象"))
                or "网页对象",
            }
        elif component == "Keyboard_Send_V1":
            step_config = {
                "words": _decode_selector_value(_native_task_argument(node, "words", "{ENTER}")) or "{ENTER}",
                "interval": _decode_selector_value(_native_task_argument(node, "interval", "10")) or "10",
                "fuctype": _decode_selector_value(_native_task_argument(node, "fuctype", "0")) or "0",
            }
        elif component == "Keyboard_PressNew":
            step_config = {
                "key": _decode_selector_value(_native_task_argument(node, "key", "13")) or "13",
                "type": _decode_selector_value(_native_task_argument(node, "type", "press")) or "press",
                "auxiliary_key": _native_task_argument(node, "auxiliary_key", []),
                "input_us": _decode_selector_value(_native_task_argument(node, "input_us", "1")) or "1",
            }
        elif component == "Basic_Delay_V1":
            wait_after_submit = True
            raw_seconds = _decode_selector_value(_native_task_argument(node, "second", wait_seconds))
            try:
                wait_seconds = max(0.0, min(float(raw_seconds or wait_seconds), 30.0))
            except (TypeError, ValueError):
                wait_seconds = 1.0
            step_config = {"second": wait_seconds}
        elif component == "WebBrowser_GetTAB_V1":
            refresh_browser_tab = True
            raw_match_type = _decode_selector_value(_native_task_argument(node, "match_type", "2"))
            raw_active = _decode_selector_value(_native_task_argument(node, "isActive", "1"))
            try:
                match_type = int(raw_match_type or 2)
            except (TypeError, ValueError):
                match_type = 2
            try:
                is_active = int(raw_active or 1)
            except (TypeError, ValueError):
                is_active = 1
            step_config = {
                "type": _decode_selector_value(_native_task_argument(node, "type", '"chrome"')) or "chrome",
                "match_type": match_type,
                "title": _decode_selector_value(_native_task_argument(node, "title", '""')),
                "url": _decode_selector_value(_native_task_argument(node, "url", '""')),
                "isActive": is_active,
            }
        elif component == "WebBrowser_RunJsInIeBySelector_v3":
            ui_selector = _native_task_argument(node, "uiSelector", "")
            output_variable = _decode_selector_value(_native_task_argument(node, "var_name", "商品链接"))
            step_config = {
                "output_variable": output_variable,
                "designated_info": _decode_selector_value(_native_task_argument(node, "designated_info", "1")),
                "web_page": _native_task_argument(node, "web_page", "网页对象"),
                "uiSelector": ui_selector,
                "page_scope": not bool(_decode_selector_value(ui_selector)),
                "jsCode": _native_task_argument(node, "jsCode", ""),
                "input": _native_task_argument(node, "input", "[]"),
                "executionEnvironment": _decode_selector_value(
                    _native_task_argument(node, "executionEnvironment", '"MAIN"')
                ),
                "isAsyncCode": _decode_selector_value(_native_task_argument(node, "isAsyncCode", "0")),
                "waitResultTimeout": _decode_selector_value(_native_task_argument(node, "waitResultTimeout", "30")),
                "output_mode": str(metadata.get("mcpJsOutputMode") or "")
                or ("table" if output_variable == JS_TABLE_OUTPUT_VARIABLE else "generated_markers"),
            }
            if not plan_backed:
                step_config["generated_element_id"] = JS_LINK_ELEMENT_ID
                step_config["generated_element_ids"] = [JS_MERCHANT_ELEMENT_ID, JS_LINK_ELEMENT_ID]
        elif component == "WebBrowser_DataCollectionV5":
            config_value = _native_task_argument(node, "config")
            try:
                config = json.loads(config_value) if isinstance(config_value, str) else config_value
            except json.JSONDecodeError:
                config = {}
            collection = config.get("collectionConfig") if isinstance(config, dict) else {}
            columns = collection.get("columnConfigArray") or [] if isinstance(collection, dict) else []
            to_excel = _decode_selector_value(_native_task_argument(node, "toExcel", 1))
            paging = _collection_paging_settings(
                {
                    name: _native_task_argument(node, name)
                    for name in (
                        "region",
                        "collect_pages",
                        "collect_items",
                        "page_type",
                        "page_type_num",
                        "selector_page",
                        "selector_more",
                    )
                }
            )
            step_config = {
                "columns": [
                    item.get("columnHeader") for item in columns if isinstance(item, dict) and item.get("columnHeader")
                ],
                "var0": _decode_selector_value(_native_task_argument(node, "var0", "数据采集结果"))
                or "数据采集结果",
                "var1": _decode_selector_value(_native_task_argument(node, "var1", "Excel路径"))
                or "Excel路径",
                "var2": _decode_selector_value(_native_task_argument(node, "var2", "监控空列数据"))
                or "监控空列数据",
                "designated_obj": _decode_selector_value(_native_task_argument(node, "designated_obj", "网页对象"))
                or "网页对象",
                "toExcel": int(to_excel) if to_excel in {"0", "1"} else to_excel,
                "output_file": output_file,
                "filename_suffix": [
                    _decode_selector_value(item)
                    for item in (_native_task_argument(node, "filename_suffix", []) or [])
                ] if isinstance(_native_task_argument(node, "filename_suffix", []), list) else [],
                "filePath": _decode_selector_value(_native_task_argument(node, "filePath", "res")) or "res",
                **paging,
                "pagination_capability": _collection_pagination_capability(paging),
            }
            collection_stage = str(node.get("mcpCollectionStage") or "").strip()
            if not collection_stage and isinstance(planned_step, dict):
                collection_stage = _collection_stage_value(planned_step)
            if collection_stage:
                step_config["collection_stage"] = collection_stage
        elif component in {"WebBrowser_CloseWeb_V2", "WebBrowser_CloseWeb", "WebBrowser_CloseWeb_V1"}:
            step_config = {
                "var_name": _decode_selector_value(_native_task_argument(node, "var_name", "操作结果"))
                or "操作结果",
                "colse_type": _decode_selector_value(_native_task_argument(node, "colse_type", '"select"'))
                or "select",
                "browser_type": _decode_selector_value(_native_task_argument(node, "browser_type", '"chrome"'))
                or "chrome",
                "web": _native_task_argument(node, "web", "网页对象"),
                "kill": _decode_selector_value(_native_task_argument(node, "kill", "0")) or "0",
                "is_close_alert": _decode_selector_value(
                    _native_task_argument(node, "is_close_alert", "0")
                )
                or "0",
            }
        elif component == "Excel_Cs_NewV4":
            step_config = {
                "path": _decode_selector_value(_native_task_argument(node, "path", "res")) or "res",
                "name": _decode_selector_value(_native_task_argument(node, "name", output_file)) or output_file,
                "software": _decode_selector_value(_native_task_argument(node, "software", '"默认"')) or "默认",
                "show": _decode_selector_value(_native_task_argument(node, "show", "1")),
            }
        elif component == "Excel_Cs_WriteRange":
            step_config = {
                "workbook": _native_task_argument(node, "workbook", "Excel文件"),
                "sheet": _decode_selector_value(_native_task_argument(node, "sheet", '"Sheet1"')) or "Sheet1",
                "row": _decode_selector_value(_native_task_argument(node, "row", "1")) or "1",
                "column": _decode_selector_value(_native_task_argument(node, "column", '"A"')) or "A",
                "contents": _native_task_argument(node, "contents", "数据采集结果"),
                "save": _decode_selector_value(_native_task_argument(node, "save", "1")),
            }
        elif _live_rpa_contract(paths, component):
            # Dynamic components are represented by their complete native
            # argument/option surface.  This is the source of truth after a
            # write, including parameters unknown to older MCP releases.
            step_config = _native_component_config(node)
            if planned_step and isinstance(planned_step.get("config"), dict):
                # Preserve semantic planner aliases alongside native values
                # when a complete plan supplied them; native keys win.
                planned_config = deepcopy(planned_step["config"])
                planned_config.update(step_config)
                step_config = planned_config
        elif planned_step:
            step_config = deepcopy(planned_step.get("config") or {})
        contract = _live_rpa_contract(paths, component)
        # Include the complete live args0/options0 surface in the state. The
        # component-specific projections above provide semantic aliases for
        # common steps; native-only parameters remain available to the model.
        if component not in _NATIVE_RPA_COMPONENTS:
            native_config = _native_component_config(node)
            native_config.update(step_config)
            step_config = native_config
        step_record = {
                "id": str(node.get("id") or node_id),
                "component": component,
                "label": str(node.get("label") or ""),
                "element_ids": element_ids,
                "config": step_config,
                "binding_status": "bound"
                if element_ids
                else ("unbound" if bool(contract and contract.get("requires_elements")) else "not_required"),
        }
        if component == "WebBrowser_DataCollectionV5":
            collection_stage = _collection_stage_value(step_record)
            if collection_stage:
                step_record["collection_stage"] = collection_stage
        steps.append(step_record)
    model_partial = _rpa_model_decides({"planning_policy": metadata.get("mcpPlanningPolicy")}) and not plan_complete
    ready_for_validation = bool(steps) and not model_partial and len(steps) >= len(
        _rpa_sequence(
            {
                "component_plan": component_plan,
                "plan_complete": plan_complete,
                "excel_mode": excel_mode,
                "use_js_link": use_js_link,
                "use_js_table": use_js_table,
                "wait_after_submit": wait_after_submit,
                "submit_component": submit_component or _rpa_submit_component({"steps": steps}),
            }
        )
    )
    state = {
        "kind": "native-shizai-rpa",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "name": metadata.get("packageName") or project_dir.name,
        "target_url": target_url,
        "keyword": keyword,
        "output_file": output_file,
        "output_path": configured_output_path or None,
        "required_columns": required_columns,
        "excel_mode": excel_mode,
        "use_js_link": use_js_link,
        "use_js_table": use_js_table,
        "max_rows": max_rows,
        "wait_after_submit": wait_after_submit,
        "wait_seconds": wait_seconds,
        "refresh_browser_tab": refresh_browser_tab,
        "result_elements_ready": result_elements_ready,
        "component_plan": component_plan,
        "plan_source": plan_source,
        "plan_complete": plan_complete,
        "request_analysis": request_analysis,
        "plan_id": metadata.get("mcpPlanId"),
        # A designer-created project has no MCP ordering contract. Once an MCP
        # client edits it, component order remains owned by that client instead
        # of falling back to the legacy browser recipe.
        "planning_policy": metadata.get("mcpPlanningPolicy") or ("model_decides" if not mcp_managed else ""),
        "mcp_managed": mcp_managed,
        "open_new_tab": open_new_tab,
        "submit_component": submit_component or _rpa_submit_component({"steps": steps}),
        "steps": steps,
        "element_count": len(elements.get("elementMap", {})),
        "generated_element_ids": [
            element_id
            for element_id in (JS_MERCHANT_ELEMENT_ID, JS_LINK_ELEMENT_ID)
            if element_id in elements.get("elementMap", {})
        ],
        "status": "ready_for_validation" if ready_for_validation else "components_added",
        "phase": "ready_for_validation"
        if ready_for_validation
        else ("flow_created" if not steps else "browser_capture_required" if len(steps) == 1 else "component_added"),
    }
    next_action = _rpa_next_action(state)
    state["next_tool"] = (
        next_action.get("tool")
        or (next_action.get("tools") or [None])[0]
        or next_action.get("then")
        or "shizai_rpa_validate_project"
    )
    state["next_action"] = next_action
    return state


def _native_template_node(paths: ShizaiPaths, component: str, *, exclude: Path) -> dict[str, Any]:
    """Copy one component's native shape from an installed designer task."""
    for task_path in (
        sorted(paths.applications_root.rglob("*.task"), key=lambda item: str(item).lower())
        if paths.applications_root.is_dir()
        else []
    ):
        if task_path.parent == exclude:
            continue
        ok, payload, _ = _safe_json(task_path)
        if not ok or not isinstance(payload, dict):
            continue
        entities = payload.get("componentModelEntities")
        if not isinstance(entities, dict):
            continue
        for node in entities.values():
            if isinstance(node, dict) and node.get("type") == component:
                return _native_normalize_node(deepcopy(node))

    # A component can be present in the live designer dictionary without an
    # example node in an existing application.  Build the same native shape
    # directly from the dictionary definition so every materializable live
    # component remains usable through MCP/CLI.  This is intentionally
    # contract-driven; no component names or parameter lists are hard-coded.
    try:
        from . import component_catalog as component_catalog_core

        source = component_catalog_core.component_source(paths)
        for record in component_catalog_core.load_component_records(source):
            if str(record.get("type") or "").strip() != component:
                continue
            definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}

            def _native_entries(items: Any) -> list[dict[str, Any]]:
                entries: list[dict[str, Any]] = []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    entry = {"name": item["name"], "value": item.get("defaultValue", "")}
                    for key in ("displayType", "displayParam", "wrapperType", "wrapperParam", "type"):
                        if key in item:
                            entry[key] = deepcopy(item[key])
                    entries.append(entry)
                return entries

            node = {
                "type": component,
                "label": record.get("label") or component,
                "version": str(record.get("version") or "3.3.4.0"),
                "args0": _native_entries(definition.get("args0")),
                "options0": _native_entries(definition.get("options0")),
                "enableComment": False,
                "remark": "",
            }
            return _native_normalize_node(node)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass

    # A freshly created client application may not contain an example node for
    # every built-in component.  The designer's template cache still carries
    # the authoritative argument names and defaults, so use it before the
    # small test-friendly fallback below.
    cache_path = paths.data_root / "tpl-cache.default.json"
    ok, cache, _ = _safe_json(cache_path)
    if ok and isinstance(cache, dict) and isinstance(cache.get("templates"), list):
        record = next(
            (item for item in cache["templates"] if isinstance(item, dict) and item.get("type") == component),
            None,
        )
        if isinstance(record, dict):
            definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}

            def _native_defaults(items: Any) -> list[dict[str, Any]]:
                result: list[dict[str, Any]] = []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    argument = {
                        "name": item["name"],
                        "value": item.get("defaultValue", ""),
                    }
                    for key in ("displayType", "displayParam", "wrapperType", "wrapperParam"):
                        if key in item:
                            argument[key] = deepcopy(item[key])
                    result.append(argument)
                return result

            return _native_normalize_node(
                {
                    "type": component,
                    "label": record.get("label") or component,
                    "version": "3.3.4.0",
                    "args0": _native_defaults(definition.get("args0")),
                    "options0": _native_defaults(definition.get("options0")),
                    "enableComment": False,
                    "remark": "",
                }
            )

    fallback: dict[str, dict[str, Any]] = {
        "WebBrowser_CreateV3": {
            "type": "WebBrowser_CreateV3",
            "label": "打开网页",
            "version": "3.3.4.0",
            "args0": [],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "WebBrowser_GetTAB_V1": {
            "type": "WebBrowser_GetTAB_V1",
            "label": "获取网页 Tab 对象",
            "version": "2.7.6.0",
            "args0": [
                {"name": "var_name", "value": "网页对象"},
                {"name": "type", "value": '"chrome"'},
                {"name": "match_type", "value": "2"},
                {"name": "title", "value": '""'},
                {"name": "url", "value": '""'},
                {"name": "isActive", "value": "1"},
            ],
            "options0": [
                {"name": "var_ret", "value": "0"},
                {"name": "skip_err", "value": "0"},
                {"name": "delay_before_new", "value": "0.2"},
                {"name": "delay_after_new", "value": "0.05"},
                {"name": "timeout_new", "value": "10"},
            ],
            "enableComment": False,
            "remark": "",
        },
        "Element_SetValue_V6": {
            "type": "Element_SetValue_V6",
            "label": "输入文本",
            "version": "3.3.4.0",
            "args0": [],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "Keyboard_Send_V1": {
            "type": "Keyboard_Send_V1",
            "label": "发送键盘输入",
            "version": "3.3.4.0",
            "args0": [],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "Keyboard_PressNew": {
            "type": "Keyboard_PressNew",
            "label": "键盘模拟按键",
            "version": "3.3.4.0",
            "args0": [
                {"name": "key", "value": '"13"'},
                {"name": "type", "value": '"press"'},
                {"name": "auxiliary_key", "value": []},
                {"name": "input_us", "value": "1"},
            ],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "Basic_Delay_V1": {
            "type": "Basic_Delay_V1",
            "label": "等待",
            "version": "3.3.4.0",
            "args0": [{"name": "second", "value": "1"}],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "WebBrowser_RunJsInIeBySelector_v3": {
            "type": "WebBrowser_RunJsInIeBySelector_v3",
            "label": "执行JS",
            "version": "3.2.0.1",
            "args0": [
                {"name": "var_name", "value": "商品链接"},
                {"name": "designated_info", "value": "1"},
                {"name": "web_page", "value": "网页对象"},
                {"name": "uiSelector", "value": ""},
                {"name": "jsCode", "value": DEFAULT_JS_LINK_SCRIPT},
                {"name": "input", "value": '".plugin_goodsCardWrapper, .gl-item"'},
                {"name": "executionEnvironment", "value": '"MAIN"'},
                {"name": "isAsyncCode", "value": "0"},
                {"name": "waitResultTimeout", "value": "30"},
            ],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "WebBrowser_DataCollectionV5": {
            "type": "WebBrowser_DataCollectionV5",
            "label": "数据采集",
            "version": "3.3.4.0",
            "args0": [],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "Excel_Cs_NewV4": {
            "type": "Excel_Cs_NewV4",
            "label": "新建Excel",
            "version": "3.3.4.0",
            "args0": [
                {"name": "var_name", "value": "Excel文件"},
                {"name": "path", "value": '""'},
                {"name": "name", "value": '""'},
                {"name": "software", "value": '"默认"'},
                {"name": "show", "value": "1"},
                {"name": "maximize", "value": "1"},
                {"name": "share", "value": "0"},
            ],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
        "Excel_Cs_WriteRange": {
            "type": "Excel_Cs_WriteRange",
            "label": "写入区域",
            "version": "3.3.4.0",
            "args0": [
                {"name": "workbook", "value": "Excel文件"},
                {"name": "sheet", "value": '"Sheet1"'},
                {"name": "row", "value": "1"},
                {"name": "column", "value": '"A"'},
                {"name": "contents", "value": "数据采集结果"},
                {"name": "save", "value": "1"},
            ],
            "options0": [],
            "enableComment": False,
            "remark": "",
        },
    }
    if component not in fallback:
        raise FileNotFoundError(f"native component template not found: {component}")
    return deepcopy(fallback[component])


def _native_unique_id(entities: dict[str, Any], prefix: str) -> str:
    """Create a compact designer-style component id that is unique in a task."""
    candidate = prefix[:4] + uuid.uuid4().hex[:6]
    while candidate in entities:
        candidate = prefix[:4] + uuid.uuid4().hex[:6]
    return candidate


def _update_native_metadata(
    root: Path,
    component_count: int,
    updates: dict[str, Any] | None = None,
) -> None:
    """Keep the client application's component count in sync with its task."""
    path = root / "szrpa.json"
    ok, metadata, _ = _safe_json(path)
    if not ok or not isinstance(metadata, dict):
        return
    metadata["componentsNum"] = component_count
    if updates:
        metadata.update(updates)
    path.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def attach_rpa_execution_plan(
    paths: ShizaiPaths,
    project_ref: str,
    *,
    analysis: dict[str, Any],
    validated_plan: dict[str, Any],
) -> dict[str, Any]:
    """Persist the request-specific plan after the first native node exists."""
    project_dir = resolve_project(paths, project_ref)
    if validated_plan.get("kind") not in {"validated_rpa_execution_plan", "rpa_plan_preflight"} or not validated_plan.get(
        "valid"
    ):
        raise ValueError("validated_plan must come from validate_rpa_execution_plan")
    plan = validated_plan.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("validated_plan.plan must be an object")
    if (
        not isinstance(analysis, dict)
        or analysis.get("kind") != "rpa_request_analysis"
        or plan.get("analysis_id") != analysis.get("analysis_id")
        or plan.get("query") != analysis.get("query")
    ):
        raise ValueError("analysis and validated plan do not describe the same request")
    _, task = _native_task_file(project_dir)
    component_plan = plan.get("steps")
    if not isinstance(component_plan, list) or not component_plan:
        raise ValueError("validated plan has no component steps")
    statement_ids = _native_statement_ids(task)
    entities = task.get("componentModelEntities") if isinstance(task.get("componentModelEntities"), dict) else {}
    first_planned = component_plan[0]
    first_id = str(first_planned.get("id") or "") if isinstance(first_planned, dict) else ""
    first_node = entities.get(statement_ids[0]) if statement_ids else None
    if (
        not isinstance(first_node, dict)
        or statement_ids[0] != first_id
        or first_node.get("type") != first_planned.get("component")
    ):
        raise ValueError("the existing first native component does not match the validated plan")
    planned_first_config = first_planned.get("config") if isinstance(first_planned.get("config"), dict) else {}
    planned_url = str(planned_first_config.get("url") or "").strip()
    actual_url = _decode_selector_value(_native_task_argument(first_node, "url", ""))
    if planned_url and actual_url != planned_url:
        raise ValueError("the existing first native component URL does not match the validated plan")
    output_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    columns = [str(item).strip() for item in output_contract.get("columns", []) if str(item).strip()]
    output_file = str(
        output_contract.get("file")
        or output_contract.get("filename")
        or output_contract.get("file_path")
        or output_contract.get("output_path")
        or output_contract.get("destination")
        or "result.xlsx"
    ).strip()
    configured_output_path = _native_configured_output_path(output_file)
    plan_target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    request_fingerprint = rpa_request_fingerprint(
        str(analysis.get("query") or ""),
        target_url=planned_url or str(plan_target.get("url") or "").strip(),
        output_contract=output_contract,
    )
    max_rows = output_contract.get("max_rows")
    normalized_max_rows = _normalize_max_rows(max_rows, default=0) if max_rows not in (None, "") else 0
    _update_native_metadata(
        project_dir,
        len(_native_statement_ids(task)),
        {
            "mcpRequestAnalysis": analysis,
            "mcpExecutionPlan": plan,
            "mcpComponentPlan": component_plan,
            "mcpPlanSource": "mcpExecutionPlan.steps",
            "mcpPlanComplete": True,
            "mcpPlanId": plan.get("plan_id"),
            "mcpBrowserCleanupStepIds": [
                str(item.get("id") or "").strip()
                for item in component_plan
                if isinstance(item, dict)
                and item.get("browser_cleanup_for")
                and str(item.get("component") or "") in {
                    "WebBrowser_CloseWeb_V2", "WebBrowser_CloseWeb", "WebBrowser_CloseWeb_V1"
                }
                and str(item.get("id") or "").strip()
            ],
            "mcpOriginalQuery": analysis.get("query"),
            "mcpRequestFingerprint": request_fingerprint,
            "mcpRequiredColumns": columns,
            "mcpOutputFile": Path(output_file).name or "result.xlsx",
            "mcpOutputPath": str(configured_output_path) if configured_output_path else None,
            "mcpMaxRows": normalized_max_rows,
            "mcpExcelMode": any(
                isinstance(item, dict) and str(item.get("component") or "").startswith("Excel_")
                for item in component_plan
            ),
            "mcpUseJsLink": False,
            "mcpUseJsTable": False,
            "mcpWaitAfterSubmit": any(
                isinstance(item, dict) and item.get("component") == "Basic_Delay_V1" for item in component_plan
            ),
            "mcpRefreshBrowserTab": any(
                isinstance(item, dict) and item.get("component") == "WebBrowser_GetTAB_V1" for item in component_plan
            ),
        },
    )
    state = _native_rpa_state(paths, str(project_dir))
    return {
        "kind": "rpa_execution_plan_attached",
        "flow_ref": str(project_dir),
        "plan_id": plan.get("plan_id"),
        "analysis_id": analysis.get("analysis_id"),
        "component_sequence": _rpa_sequence(state),
        "next_action": state.get("next_action"),
        "flow": state,
    }


def _normalize_client_metadata(root: Path) -> bool:
    """Fill metadata fields required by current designer open/import logic.

    Older MCP-created packages were runnable but omitted ``openMode`` and
    compatibility markers.  The application list can display those packages,
    yet the editor has no deterministic mode to open them in.  Keep existing
    values and add only stable defaults so this repair is idempotent.
    """
    path = root / "szrpa.json"
    ok, metadata, _ = _safe_json(path)
    if not ok or not isinstance(metadata, dict):
        return False
    runtime = str(metadata.get("runtimeVersion") or "7.3.7.0").strip()
    factory = str(metadata.get("factoryVersion") or runtime.rsplit(".", 1)[0]).strip()
    defaults = {
        "isNewProject": True,
        "projectMode": "normal",
        "version": "0.0.1",
        "openMode": "code",
        "isAllowNewEdit": True,
        "factoryVersion": factory,
        "runtimeVersion": runtime,
        "supportVersion": factory,
        "supportPatch": "0",
        "arch": "x64",
        "reCreateCode": False,
    }
    changed = False
    for key, value in defaults.items():
        if metadata.get(key) in (None, ""):
            metadata[key] = value
            changed = True
    if changed:
        path.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return changed


def _add_native_rpa_step(
    paths: ShizaiPaths,
    flow_ref: str,
    component: str,
    *,
    step_id: str | None = None,
    before_step_id: str | None = None,
    label: str | None = None,
    element_ids: list[str] | None = None,
    config: dict[str, Any] | None = None,
    client: Any | None = None,
    allow_unbound_predecessors: bool = False,
) -> dict[str, Any]:
    """Append a real designer component directly to a client-created task."""
    project_dir = resolve_project(paths, flow_ref)
    task_path, _ = _native_task_file(project_dir)
    preflight = _native_designer_state_consistency(project_dir, task_path)
    if preflight.get("conflict"):
        detail = "; ".join(str(item) for item in preflight.get("errors") or [])
        raise ValueError("designer_state_conflict: " + (detail or "native task and designer artifacts disagree"))
    state = _native_rpa_state(paths, str(project_dir))
    component_name = str(component).strip()
    # Incremental business steps belong before a managed closing tail. A
    # user-authored close step keeps its explicit position.
    if not before_step_id and component_name != "WebBrowser_CloseWeb_V2":
        _, metadata, _ = _safe_json(project_dir / "szrpa.json")
        cleanup_ids = set((metadata or {}).get("mcpBrowserCleanupStepIds") or [])
        for trailing in reversed(state["steps"]):
            if trailing["id"] not in cleanup_ids:
                break
            before_step_id = trailing["id"]
    _guard_rpa_append_structure(state["steps"], component_name, config)
    step_index = len(state["steps"]) + 1
    _publish_rpa_ui(
        "component_writing",
        "component_write",
        f"正在把第 {step_index} 个组件 {component_name} 写入设计器。",
        animation="component_write",
        flow_ref=str(project_dir),
        component=component_name,
        step_index=step_index,
        step_count=len(state["steps"]),
    )
    contract = _live_rpa_contract(paths, component_name)
    if contract is None:
        raise ValueError(f"RPA component is not installed in the live catalog: {component_name}")
    model_owned = _rpa_model_decides(state)
    expected_sequence = _rpa_sequence(state)
    pending_binding = next(
        (
            item
            for item in state["steps"]
            if isinstance(item, dict) and str(item.get("binding_status") or "") == "unbound"
        ),
        None,
    )
    if pending_binding is not None and not (allow_unbound_predecessors or model_owned):
        raise ValueError(
            f"bind element selector for component {pending_binding.get('id')} before adding another component"
        )
    expected = expected_sequence[len(state["steps"])] if len(state["steps"]) < len(expected_sequence) else None
    if expected is None and not model_owned:
        raise ValueError("all supported RPA components are already added; validate the native flow")
    if expected and component_name != expected and not model_owned:
        raise ValueError(
            f"the next RPA component must be {expected}; follow shizai_rpa_components and add components in order"
        )
    normalized_ids = [str(item).strip() for item in (element_ids or []) if str(item).strip()]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError(f"duplicate element ids in step {step_id or len(state['steps']) + 1}")
    try:
        _, native_elements = _element_file(paths, str(project_dir))
    except (FileNotFoundError, OSError, ValueError):
        native_elements = {"elementMap": {}, "groupMap": {}}
    if contract["requires_elements"]:
        available = set(native_elements["elementMap"])
        missing = [item for item in normalized_ids if item not in available]
        if missing:
            raise ValueError(f"{component_name} references unimported elements: {', '.join(missing)}")
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        raise ValueError(f"native task has no componentModelEntities: {task_path}")
    node = _native_template_node(paths, component_name, exclude=project_dir)
    requested_step_id = str(step_id or "").strip()
    if requested_step_id and requested_step_id in entities:
        raise ValueError(f"duplicate RPA step id: {requested_step_id}")
    if requested_step_id:
        node_id = requested_step_id
    else:
        node_id = _native_unique_id(entities, component_name)
    node["id"] = node_id
    node["label"] = label or contract["designer_label"]
    if component_name == "WebBrowser_DataCollectionV5":
        # The designer's selector schema intentionally omits dynamic text,
        # so several fields can share the same relaxed selector signature.
        # Keep the explicit MCP binding beside the native config for reliable
        # diagnostics after Chrome reorders UIA indexes.
        node["mcpElementIds"] = list(normalized_ids)
    effective_config = dict(config) if isinstance(config, dict) else {}
    workflow_reference = str(effective_config.pop("workflow_reference", "") or "").strip()
    if workflow_reference:
        if component_name != "WebBrowser_RunJsInIeBySelector_v3":
            raise ValueError("workflow_reference is only supported for the referenced component")
        if workflow_reference != JD_PRODUCT_LINK_REFERENCE:
            raise ValueError(f"unknown workflow_reference: {workflow_reference}")
        effective_config.setdefault("jsCode", DEFAULT_JS_LINK_SCRIPT)
        effective_config.setdefault("output_variable", "商品链接")
        effective_config.setdefault("result_mode", "generated_markers")
        effective_config.setdefault("page_scope", True)
    native_keyword = str(state.get("keyword") or "")
    native_output_file = str(state.get("output_file") or "result.xlsx")
    if component_name == "Database_DBConnect":
        # SQLite does not create its parent directory/file when opened by
        # RuntimeHost. Prepare concrete local paths while materializing the
        # component so a newly generated flow is runnable immediately.
        _ensure_sqlite_database_file(effective_config, project_dir=project_dir)
    if component_name == "WebBrowser_CreateV3":
        target_url = str(effective_config.get("url") or state.get("target_url") or "")
        metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
        metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
        is_remote = _native_bool(effective_config.get("isRemote", False))
        ip_address = str(effective_config.get("ipAddress") or "")
        port = str(effective_config.get("port") or "61619")
        browser_arguments = str(effective_config.get("arguments") or "")
        open_new_tab = _native_bool(
            effective_config.get("openNewTab", metadata.get("mcpOpenNewTab")),
            default=False,
        )
        _native_set_argument(node, "var_name", str(effective_config.get("var_name") or "网页对象"))
        _native_set_argument(node, "type", json.dumps(str(effective_config.get("type") or "chrome"), ensure_ascii=False))
        _native_set_argument(node, "url", json.dumps(target_url, ensure_ascii=False))
        _native_set_argument(node, "isRemote", "1" if is_remote else "0")
        _native_set_argument(node, "ipAddress", json.dumps(ip_address, ensure_ascii=False))
        _native_set_argument(node, "port", json.dumps(port, ensure_ascii=False))
        _native_set_argument(node, "arguments", json.dumps(browser_arguments, ensure_ascii=False))
        _native_set_argument(node, "openNewTab", "1" if open_new_tab else "0")
        _native_set_argument(node, "waitingload", "1" if _native_bool(effective_config.get("waitingload", 1)) else "0")
        step_config = {"url": target_url, "type": effective_config.get("type") or "chrome", "openNewTab": open_new_tab}
    elif component_name == "WebBrowser_GetTAB_V1":
        browser_type = str(effective_config.get("type") or "chrome")
        try:
            match_type = int(effective_config.get("match_type", 2))
        except (TypeError, ValueError) as exc:
            raise ValueError("WebBrowser_GetTAB_V1 match_type must be an integer") from exc
        try:
            is_active = int(effective_config.get("isActive", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("WebBrowser_GetTAB_V1 isActive must be 0 or 1") from exc
        if is_active not in {0, 1}:
            raise ValueError("WebBrowser_GetTAB_V1 isActive must be 0 or 1")
        title = str(effective_config.get("title") or "")
        url = str(effective_config.get("url") or "")
        _native_set_argument(node, "var_name", str(effective_config.get("var_name") or "网页对象"))
        _native_set_argument(node, "type", json.dumps(browser_type, ensure_ascii=False))
        _native_set_argument(node, "match_type", str(match_type))
        _native_set_argument(node, "title", json.dumps(title, ensure_ascii=False))
        _native_set_argument(node, "url", json.dumps(url, ensure_ascii=False))
        _native_set_argument(node, "isActive", str(is_active))
        step_config = {
            "type": browser_type,
            "match_type": match_type,
            "title": title,
            "url": url,
            "isActive": is_active,
        }
    elif component_name == "Element_SetValue_V6":
        element_id = normalized_ids[0] if normalized_ids else ""
        native_keyword = str(effective_config.get("text") or native_keyword)
        designated_obj = _native_designated_object(effective_config.get("designated_obj"))
        element_record = native_elements["elementMap"].get(element_id, {})
        _native_set_argument(node, "var_name", str(effective_config.get("var_name") or "校验结果"))
        _native_set_argument(
            node,
            "uiSelector",
            _native_selector_expression(element_id, element_record) if element_id else "",
        )
        # Bind the action to the handle returned by WebBrowser.CreateV3. An
        # explicit ``designated_obj=None`` remains available through the public
        # config update API, but generated browser flows use the proven output
        # binding by default.
        designated_info = _native_bool(
            effective_config.get("designated_info", 1 if effective_config.get("designated_obj") else 0)
        )
        _native_set_argument(node, "designated_info", "1" if designated_info else "0")
        _native_set_argument(node, "designated_obj", designated_obj)
        _native_set_argument(node, "text", json.dumps(native_keyword, ensure_ascii=False))
        # The model may submit here or choose a separate submit component.
        is_enter = _native_bool(effective_config.get("isEnter", False))
        _native_set_argument(node, "isEnter", "1" if is_enter else "0")
        step_config = {
            "text": native_keyword,
            "designated_info": designated_info,
            "designated_obj": designated_obj,
            "isEnter": is_enter,
        }
    elif component_name == "Keyboard_Send_V1":
        # This component is the submit action in the native JD recipe.  The
        # search text is already written by Element.SetValue_V6; sending the
        # keyword a second time makes the input become "手机手机".  Keep an
        # explicit words override for legacy flows, but default new steps to
        # the designer's Enter token.
        submit_words = effective_config.get("words")
        if submit_words is None or not str(submit_words).strip():
            submit_words = "{ENTER}"
        submit_words = str(submit_words)
        _native_set_argument(node, "words", json.dumps(submit_words, ensure_ascii=False))
        _native_set_argument(node, "interval", "10")
        _native_set_argument(node, "fuctype", "0")
        step_config = {"words": submit_words}
    elif component_name == "Keyboard_PressNew":
        key = str(effective_config.get("key") or "13")
        key_type = str(effective_config.get("type") or "press")
        auxiliary_key = effective_config.get("auxiliary_key", [])
        if not isinstance(auxiliary_key, list):
            auxiliary_key = [str(auxiliary_key)]
        input_us = str(effective_config.get("input_us", "1"))
        _native_set_argument(node, "key", json.dumps(key, ensure_ascii=False))
        _native_set_argument(node, "type", json.dumps(key_type, ensure_ascii=False))
        _native_set_argument(node, "auxiliary_key", auxiliary_key)
        _native_set_argument(node, "input_us", input_us)
        step_config = {
            "key": key,
            "type": key_type,
            "auxiliary_key": auxiliary_key,
            "input_us": input_us,
        }
    elif component_name == "Basic_Delay_V1":
        try:
            seconds = max(0.0, min(float(effective_config.get("second", state.get("wait_seconds", 1))), 30.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Basic_Delay_V1 second must be a number between 0 and 30") from exc
        rendered = str(seconds).rstrip("0").rstrip(".") if "." in str(seconds) else str(int(seconds))
        _native_set_argument(node, "second", rendered)
        step_config = {"second": seconds}
    elif component_name == "WebBrowser_RunJsInIeBySelector_v3":
        plan_backed = bool(state.get("component_plan") and state.get("plan_complete"))
        js_table = bool(state.get("use_js_table")) and not plan_backed
        element_id = normalized_ids[0] if normalized_ids else None
        element_record = native_elements["elementMap"].get(element_id, {}) if element_id else {}
        if plan_backed and not str(effective_config.get("jsCode") or "").strip():
            raise ValueError("planned JavaScript step requires explicit config.jsCode")
        js_code = str(effective_config.get("jsCode") or "")
        if not js_code.strip():
            js_code = (
                _js_table_script(state.get("required_columns"), state.get("max_rows"))
                if js_table
                else DEFAULT_JS_LINK_SCRIPT
            )
        js_code_value = js_code if js_code.strip().startswith('"""') else f'"""{js_code}"""'
        input_value = (
            "[]"
            if plan_backed and effective_config.get("input") in (None, "", [])
            else _native_list_expression(
                effective_config.get("input"),
                ".plugin_goodsCardWrapper, .gl-item",
            )
        )
        output_variable = str(effective_config.get("output_variable") or "").strip()
        if plan_backed and not output_variable:
            raise ValueError("planned JavaScript step requires explicit config.output_variable")
        if not output_variable:
            output_variable = JS_TABLE_OUTPUT_VARIABLE if js_table else "商品链接"
        result_mode, mode_alias_used = _normalize_js_result_mode(effective_config.get("result_mode"))
        if mode_alias_used:
            effective_config["result_mode"] = result_mode
        if plan_backed and result_mode not in {"table", "json", "text", "scalar"}:
            raise ValueError("planned JavaScript step requires config.result_mode")
        if not result_mode:
            result_mode = "table" if js_table else "generated_markers"
        _native_set_argument(node, "var_name", output_variable)
        _native_set_argument(node, "designated_info", "1")
        _native_set_argument(node, "web_page", "网页对象")
        page_scope = bool(effective_config.get("page_scope", True))
        # Keep page-scoped execution explicit in the native task.  ``None``
        # is not a valid selector value for RuntimeHost, even though it is a
        # convenient Python sentinel.
        ui_selector = "" if page_scope else _native_selector_expression(element_id, element_record)
        _native_set_argument(node, "uiSelector", ui_selector)
        _native_set_argument(node, "jsCode", js_code_value)
        _native_set_argument(node, "input", input_value)
        _native_set_argument(node, "executionEnvironment", '"MAIN"')
        _native_set_argument(node, "isAsyncCode", "0")
        _native_set_argument(node, "waitResultTimeout", str(effective_config.get("waitResultTimeout", "30")))
        step_config = {
            "output_variable": output_variable,
            "designated_info": "1",
            "web_page": "网页对象",
            "uiSelector": ui_selector,
            "page_scope": page_scope,
            "jsCode": js_code,
            "input": input_value,
            "executionEnvironment": "MAIN",
            "isAsyncCode": "0",
            "waitResultTimeout": str(effective_config.get("waitResultTimeout", "30")),
            "generated_element_id": (
                (
                    _upsert_js_generated_elements(paths, str(project_dir), element_id)
                )
                if element_id and not js_table and (not plan_backed or bool(workflow_reference))
                else None
            ),
            "generated_element_ids": [
                JS_NAME_ELEMENT_ID,
                JS_MERCHANT_ELEMENT_ID,
                JS_PRICE_ELEMENT_ID,
                JS_SALES_ELEMENT_ID,
                JS_LINK_ELEMENT_ID,
            ]
            if workflow_reference == JD_PRODUCT_LINK_REFERENCE and element_id
            else [],
            "output_mode": result_mode,
            "workflow_reference": workflow_reference or None,
        }
    elif component_name == "WebBrowser_DataCollectionV5":
        if str(effective_config.get("collection_output") or "").strip():
            effective_config.setdefault("var0", str(effective_config["collection_output"]).strip())
        # Plans may bind the row target semantically (the model-facing form is
        # ``$output_contract.max_rows``). Resolve it before writing native
        # arguments so RuntimeHost receives the concrete integer it expects.
        if "collect_items" in effective_config:
            effective_config["collect_items"] = _resolve_plan_semantic_value(
                effective_config.get("collect_items"),
                {"max_rows": state.get("max_rows")},
            )
        output_file = _collection_output_file(effective_config, fallback=native_output_file)
        output_path = Path(output_file)
        output_stem = output_path.stem or "result"
        output_extension = output_path.suffix.casefold()
        if output_extension not in _NATIVE_OUTPUT_SUFFIXES:
            output_extension = ".xlsx"
            output_file = str(output_path.with_suffix(output_extension))
            output_path = Path(output_file)
        columns = effective_config.get("columns")
        if not isinstance(columns, list) or not columns:
            columns = list(state.get("required_columns") or DEFAULT_RPA_COLUMNS)
        columns = [str(item).strip() for item in columns if str(item).strip()]
        if not columns:
            columns = list(DEFAULT_RPA_COLUMNS)
        effective_config = _materialize_collection_paging_selectors(
            paths,
            str(project_dir),
            effective_config,
        )
        _, collection_elements = _element_file(paths, str(project_dir))
        collection_element_map = collection_elements.get("elementMap")
        collection_element_map = collection_element_map if isinstance(collection_element_map, dict) else {}
        normalized_ids = _normalize_generated_collection_bindings(
            normalized_ids,
            columns,
            collection_element_map,
        )
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("WebBrowser_DataCollectionV5 has duplicate column element bindings")
        node["mcpElementIds"] = list(normalized_ids)
        collection_config, collection_count = _staged_collection_config(
            _element_file(paths, str(project_dir))[1],
            normalized_ids,
            keyword=native_keyword,
            columns=columns,
            data_attributes=effective_config.get("data_attributes"),
            fallback_page_url=str(effective_config.get("page_url") or state.get("target_url") or ""),
            fallback_page_title=str(effective_config.get("page_title") or ""),
        )
        if normalized_ids and (collection_config is None or collection_count != len(normalized_ids)):
            raise ValueError("WebBrowser_DataCollectionV5 requires reusable imported field selectors")
        collection_paging = _collection_paging_settings(effective_config)
        _native_set_argument(node, "var0", str(effective_config.get("var0") or "数据采集结果"))
        _native_set_argument(node, "var1", str(effective_config.get("var1") or "Excel路径"))
        _native_set_argument(node, "var2", str(effective_config.get("var2") or "监控空列数据"))
        _native_set_argument(node, "config", json.dumps(collection_config or [], ensure_ascii=False))
        _native_set_argument(node, "designated_obj", _native_designated_object(effective_config.get("designated_obj")))
        _apply_collection_paging_settings(node, collection_paging)
        # In explicit Excel mode the collection component hands its table to
        # Excel_Cs_WriteRange instead of creating a second output file.
        to_excel = _collection_to_excel_setting(
            effective_config,
            excel_mode=bool(state.get("excel_mode")),
        )
        _native_set_argument(node, "toExcel", str(to_excel))
        _native_set_argument(
            node,
            "filename_suffix",
            [json.dumps(output_stem, ensure_ascii=False), json.dumps(output_extension, ensure_ascii=False)],
        )
        configured_path = _native_configured_output_path(output_file)
        file_path = (
            str(configured_path.parent)
            if configured_path is not None
            else str(effective_config.get("filePath") or effective_config.get("output_path") or "res")
        )
        try:
            try:
                _native_set_parameter(node, "filePath", json.dumps(file_path, ensure_ascii=False))
            except ValueError:
                _native_set_argument(node, "filePath", json.dumps(file_path, ensure_ascii=False))
        except ValueError:
            # Minimal test/legacy templates may omit optional output-path
            # parameters; adding the argument keeps them materializable while
            # current designer templates continue to update their native slot.
            _native_set_argument(node, "filePath", json.dumps(file_path, ensure_ascii=False))
        step_config = {
            "columns": columns,
            "designated_obj": _native_designated_object(effective_config.get("designated_obj")),
            "data_attributes": {
                header: (
                    str((effective_config.get("data_attributes") or {}).get(header)).strip()
                    if isinstance(effective_config.get("data_attributes"), dict)
                    and (effective_config.get("data_attributes") or {}).get(header)
                    else "href"
                    if header in {"商品链接", "链接", "网址", "URL", "url"}
                    else "innerText"
                )
                for header in columns
            },
            **collection_paging,
            "toExcel": to_excel,
            "output_file": output_file,
            "filePath": file_path,
            "filename_suffix": [output_stem, output_extension],
        }
        collection_stage = _collection_stage_value({"config": effective_config})
        if collection_stage:
            step_config["collection_stage"] = collection_stage
            node["mcpCollectionStage"] = collection_stage
    elif component_name == "WebBrowser_CloseWeb_V2":
        # Close only the browser handle returned by WebBrowser_CreateV3.  The
        # designer spells ``close_type`` as ``colse_type`` in this component;
        # keep the native spelling so the generated task is executable.
        result_variable = str(effective_config.get("var_name") or "操作结果")
        close_type = str(effective_config.get("colse_type") or "select")
        browser_type = str(effective_config.get("browser_type") or "chrome")
        web_object = _native_designated_object(effective_config.get("web"), default="网页对象")
        kill = "1" if _native_bool(effective_config.get("kill", 0)) else "0"
        close_alert = "1" if _native_bool(effective_config.get("is_close_alert", 0)) else "0"
        _native_set_argument(node, "var_name", result_variable)
        _native_set_argument(node, "colse_type", json.dumps(close_type, ensure_ascii=False))
        _native_set_argument(node, "browser_type", json.dumps(browser_type, ensure_ascii=False))
        _native_set_argument(node, "web", web_object)
        _native_set_argument(node, "kill", kill)
        _native_set_argument(node, "is_close_alert", close_alert)
        step_config = {
            "var_name": result_variable,
            "colse_type": close_type,
            "browser_type": browser_type,
            "web": web_object,
            "kill": int(kill),
            "is_close_alert": int(close_alert),
        }
    elif component_name == "Excel_Cs_NewV4":
        output_file = str(effective_config.get("name") or native_output_file)
        output_name = Path(output_file).name or "result.xlsx"
        output_path = str(effective_config.get("path") or "res")
        software = str(effective_config.get("software") or "默认")
        show = str(effective_config.get("show", 1))
        maximize = str(effective_config.get("maximize", 1))
        share = str(effective_config.get("share", 0))
        _native_set_argument(node, "var_name", str(effective_config.get("var_name") or "Excel文件"))
        _native_set_argument(node, "path", json.dumps(output_path, ensure_ascii=False))
        _native_set_argument(node, "name", json.dumps(output_name, ensure_ascii=False))
        _native_set_argument(node, "software", json.dumps(software, ensure_ascii=False))
        _native_set_argument(node, "show", show)
        _native_set_argument(node, "maximize", maximize)
        _native_set_argument(node, "share", share)
        step_config = {
            "path": output_path,
            "name": output_name,
            "software": software,
            "show": show,
            "maximize": maximize,
            "share": share,
        }
        output_file = output_name
    elif component_name == "Excel_Cs_WriteRange":
        workbook = str(effective_config.get("workbook") or "Excel文件")
        sheet = str(effective_config.get("sheet") or "Sheet1")
        row = str(effective_config.get("row", 1))
        column = str(effective_config.get("column") or "A")
        contents = str(effective_config.get("contents") or "数据采集结果")
        save = str(effective_config.get("save", 1))

        def _literal(value: str) -> str:
            text = str(value).strip()
            if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                return text
            return json.dumps(text, ensure_ascii=False)

        _native_set_argument(node, "workbook", workbook)
        _native_set_argument(node, "sheet", _literal(sheet))
        _native_set_argument(node, "row", row)
        _native_set_argument(node, "column", _literal(column))
        _native_set_argument(node, "contents", contents)
        _native_set_argument(node, "save", save)
        step_config = {
            "workbook": workbook,
            "sheet": sheet.strip('"'),
            "row": row,
            "column": column.strip('"'),
            "contents": contents,
            "save": save,
        }
    elif contract.get("catalog_backed"):
        parameter_names = {
            str(item.get("name") or "").strip()
            for key in ("args0", "options0")
            for item in (node.get(key) if isinstance(node.get(key), list) else [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        unknown_parameters = sorted(set(effective_config) - parameter_names)
        if unknown_parameters:
            raise ValueError(f"component {component_name} config has unknown parameters: {unknown_parameters}")
        output_parameters = set(contract.get("output_parameters") or [])
        for parameter, value in effective_config.items():
            _native_set_parameter(
                node,
                parameter,
                _generic_native_value(value, output_parameter=parameter in output_parameters),
            )
        node["mcpCode"] = deepcopy(contract.get("code") or [])
        step_config = deepcopy(effective_config)
    else:
        raise ValueError(f"no native materializer for RPA component: {component_name}")
    # Compatibility templates must also retain explicitly supplied native
    # scalar options and references instead of silently using template values.
    # Structured selectors and compound widget values keep their encoders above.
    native_parameters = {item.get("name") for key in ("args0", "options0")
                         for item in node.get(key, []) if isinstance(item, dict)}
    for parameter, value in effective_config.items():
        explicit_reference = isinstance(value, dict) and set(value) in ({"variable"}, {"expression"})
        extra_scalar = parameter not in step_config and isinstance(value, (bool, int, float))
        if parameter in native_parameters and (explicit_reference or extra_scalar):
            _native_set_parameter(node, parameter, _generic_native_value(value))
            step_config[parameter] = deepcopy(value)
    entities[node_id] = node
    main = entities.get("MAIN_COMPONENT")
    if not isinstance(main, dict):
        raise ValueError("native task has no MAIN_COMPONENT")
    statements = _native_statement_ids(payload)
    normalized_before_step_id = str(before_step_id or "").strip()
    if normalized_before_step_id:
        if normalized_before_step_id not in statements:
            raise ValueError(f"before_step_id is not an existing step: {normalized_before_step_id}")
        statements.insert(statements.index(normalized_before_step_id), node_id)
    else:
        statements.append(node_id)
    for argument in main.setdefault("args0", []):
        if isinstance(argument, dict) and argument.get("name") == "statements":
            argument["value"] = statements
            break
    else:
        main["args0"].insert(0, {"name": "statements", "type": "input_statement", "value": statements})
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
    metadata_updates: dict[str, Any] = {}
    if not state.get("mcp_managed", True):
        metadata_updates["mcpPlanningPolicy"] = "model_decides"
    if component_name == "WebBrowser_CreateV3":
        metadata_updates.update(
            {
                "mcpTargetUrl": str(step_config.get("url") or state.get("target_url") or ""),
                "mcpKeyword": native_keyword,
                "mcpOutputFile": Path(native_output_file).name or "result.xlsx",
                "mcpOpenNewTab": bool(step_config.get("openNewTab", False)),
            }
        )
    if component_name == "Basic_Delay_V1":
        metadata_updates.update(
            {
                "mcpWaitAfterSubmit": True,
                "mcpWaitSeconds": step_config.get("second", 1),
            }
        )
    if component_name == "WebBrowser_GetTAB_V1":
        metadata_updates["mcpRefreshBrowserTab"] = True
    if component_name == "Excel_Cs_NewV4":
        metadata_updates["mcpOutputFile"] = Path(step_config["name"]).name
    if component_name == "WebBrowser_DataCollectionV5":
        collection_paging = step_config if isinstance(step_config, dict) else {}
        output_path_value = str(step_config.get("output_file") or native_output_file)
        configured_output = _native_configured_output_path(output_path_value)
        metadata_updates["mcpCollectionAttributes"] = step_config.get("data_attributes", {})
        metadata_updates.update(
            {
                "mcpOutputFile": Path(output_path_value).name or "result.xlsx",
                "mcpOutputPath": str(configured_output) if configured_output else (
                    str(Path(str(step_config.get("filePath"))).expanduser() / Path(output_path_value).name)
                    if step_config.get("filePath") and Path(str(step_config.get("filePath"))).expanduser().is_absolute()
                    else None
                ),
                "mcpCollectionRegion": _collection_paging_settings(collection_paging).get("region", "1"),
                "mcpCollectionPageType": _collection_paging_settings(collection_paging).get("page_type", "1"),
                "mcpCollectionPageTypeNum": _collection_paging_settings(collection_paging).get("page_type_num", "0"),
                "mcpResultFields": sorted(
                    {
                        _canonical_result_field(item)
                        for item in step_config.get("columns", [])
                        if _canonical_result_field(item) in _RPA_RESULT_FIELDS
                    }
                ),
                "mcpResultFieldElementIds": {
                    _canonical_result_field(header): element_id
                    for header, element_id in zip(step_config.get("columns", []), normalized_ids)
                    if _canonical_result_field(header) in _RPA_RESULT_FIELDS
                },
            }
        )
    if component_name == "WebBrowser_RunJsInIeBySelector_v3":
        metadata_updates.update(
            {
                "mcpUseJsLink": bool(workflow_reference) or bool(step_config.get("output_mode") == "generated_markers"),
                "mcpUseJsTable": bool(step_config.get("output_mode") == "table"),
                "mcpMaxRows": int(state.get("max_rows") or 0),
                "mcpJsOutputMode": step_config.get("output_mode"),
                "mcpJsOutputVariable": step_config.get("output_variable"),
            }
        )
    # Keep the model-facing plan in lockstep with the native task.  The old
    # incremental writer only updated this metadata for insertions and wrote
    # ``outputs=[]`` for every component except JavaScript.  That made native
    # database/JSON steps fail static validation even though their live
    # contracts expose a var_name output.  Derive logical outputs from the
    # same contract used by plan validation and update both append and insert
    # paths.
    metadata_ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
    metadata = metadata if metadata_ok and isinstance(metadata, dict) else {}
    component_plan, _ = _rpa_component_plan(metadata)
    if isinstance(component_plan, list):
        revised_plan = deepcopy(component_plan)
        plan_config = deepcopy(effective_config)
        if workflow_reference:
            plan_config["workflow_reference"] = workflow_reference
        logical_outputs = _logical_plan_outputs(component_name, contract, plan_config)
        new_plan_step = {
            "id": node_id,
            "component": component_name,
            "purpose": label or contract["designer_label"],
            "config": plan_config,
            "outputs": logical_outputs,
        }
        plan_changed = False
        if normalized_before_step_id:
            target_index = next(
                (
                    index
                    for index, item in enumerate(revised_plan)
                    if isinstance(item, dict) and str(item.get("id") or "").strip() == normalized_before_step_id
                ),
                None,
            )
            if target_index is not None:
                target = revised_plan[target_index]
                prior_dependencies = list(target.get("depends_on") or []) if isinstance(target, dict) else []
                new_plan_step["depends_on"] = prior_dependencies
                revised_plan.insert(target_index, new_plan_step)
                plan_changed = True
                if isinstance(target, dict):
                    target["depends_on"] = [node_id]
        elif any(item.get("id") == node_id for item in revised_plan):
            # start_design seeds the complete plan before materializing nodes.
            # Keep its content address stable; the native node is the write
            # source and the plan already contains the model's operation ids,
            # dependencies, and semantic output names.
            pass
        else:
            prior_id = str(revised_plan[-1].get("id") or "").strip() if revised_plan else ""
            new_plan_step["depends_on"] = [prior_id] if prior_id else []
            revised_plan.append(new_plan_step)
            plan_changed = True
        # The native task owns a linear MAIN_COMPONENT statement list.  Repair
        # stale dependencies from deleted nodes before persisting the updated
        # model-facing plan so validation and later edits see the same graph.
        if plan_changed:
            _normalize_linear_plan_dependencies(revised_plan)
            for order, item in enumerate(revised_plan, start=1):
                if isinstance(item, dict):
                    item["order"] = order
            metadata_updates["mcpComponentPlan"] = revised_plan
            execution_plan = metadata.get("mcpExecutionPlan")
            if isinstance(execution_plan, dict):
                revised_execution_plan = deepcopy(execution_plan)
                revised_execution_plan["steps"] = deepcopy(revised_plan)
                revised_execution_plan.pop("plan_id", None)
                revised_execution_plan["plan_id"] = (
                    "plan-"
                    + hashlib.sha256(
                        json.dumps(revised_execution_plan, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()[:16]
                )
                metadata_updates["mcpExecutionPlan"] = revised_execution_plan
                metadata_updates["mcpPlanId"] = revised_execution_plan["plan_id"]
    _update_native_metadata(project_dir, len(statements), metadata_updates)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    updated = _native_rpa_state(paths, str(project_dir))
    new_step = next(item for item in updated["steps"] if item["id"] == node_id)
    step_validation = validate_rpa_step(paths, str(project_dir), node_id)
    verification_gate = _rpa_verification_gate(
        str(project_dir),
        new_step,
        step_validation,
    )
    ui_feedback = _publish_rpa_ui(
        "step_ready" if step_validation["valid"] else "validation_failed",
        "component_validation",
        (
            f"第 {step_index} 个组件 {component_name} 已写入并通过静态校验。"
            if step_validation["valid"]
            else f"第 {step_index} 个组件 {component_name} 已写入，但静态校验未通过，请先修复。"
        ),
        animation="step_complete" if step_validation["valid"] else "validation_error",
        overlay_state="inactive",
        flow_ref=str(project_dir),
        component=component_name,
        step_id=node_id,
        step_index=step_index,
        step_count=len(updated["steps"]),
        valid=bool(step_validation["valid"]),
        next_tool=updated["next_tool"],
    )
    return {
        "kind": "native_rpa_step_added",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "project_ref": str(project_dir),
        "step": {**new_step, "config": step_config},
        "step_count": len(updated["steps"]),
        "phase": updated["phase"],
        "next_tool": updated["next_tool"],
        "next_action": updated["next_action"],
        "step_validation": step_validation,
        "verification_gate": verification_gate,
        "ui_feedback": ui_feedback,
        "designer_owned": True,
        "writeback": writeback,
        "message": "组件已直接写入设计器应用；后续引用此 flow_ref 继续添加组件。",
    }


def _set_native_rpa_step_elements(
    paths: ShizaiPaths,
    flow_ref: str,
    step_id: str,
    element_ids: list[str],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Rebind selectors in a real native task after a better capture."""
    project_dir = resolve_project(paths, flow_ref)
    normalized_ids = [str(item).strip() for item in element_ids if str(item).strip()]
    if not normalized_ids:
        raise ValueError("element_ids must contain at least one id")
    _, native_elements = _element_file(paths, str(project_dir))
    missing = [item for item in normalized_ids if item not in native_elements["elementMap"]]
    if missing:
        raise ValueError("elements are not imported into this project: " + ", ".join(missing))
    task_path, payload = _native_task_file(project_dir)
    entities = payload.get("componentModelEntities")
    if not isinstance(entities, dict):
        raise ValueError(f"native task has no componentModelEntities: {task_path}")
    node = entities.get(step_id)
    if not isinstance(node, dict):
        raise ValueError(f"RPA step not found: {step_id}")
    component = node.get("type")
    _publish_rpa_ui(
        "element_binding",
        "element_rebind",
        f"正在把 {len(normalized_ids)} 个网页元素绑定到组件 {component}。",
        animation="element_bind",
        flow_ref=str(project_dir),
        component=str(component),
        step_id=step_id,
        element_count=len(normalized_ids),
    )
    if component == "Element_SetValue_V6":
        element = native_elements["elementMap"].get(normalized_ids[0], {})
        _native_set_argument(
            node,
            "uiSelector",
            _native_selector_expression(normalized_ids[0], element),
        )
        # Rebinding a selector should restore the browser output binding when
        # an older task stored the ambiguous literal ``None``.
        current_obj = _native_task_argument(node, "designated_obj")
        if str(current_obj or "").strip().casefold() in {"", "none", "null"}:
            current_obj = "网页对象"
        _native_set_argument(
            node,
            "designated_obj",
            _native_designated_object(current_obj),
        )
    elif component == "WebBrowser_RunJsInIeBySelector_v3":
        element = native_elements["elementMap"].get(normalized_ids[0], {})
        _native_set_argument(
            node,
            "uiSelector",
            _native_selector_expression(normalized_ids[0], element),
        )
        _upsert_js_generated_elements(paths, str(project_dir), normalized_ids[0])
    elif component == "WebBrowser_DataCollectionV5":
        output_file = _native_output_file(node)
        ok, metadata, _ = _safe_json(project_dir / "szrpa.json")
        metadata = metadata if ok and isinstance(metadata, dict) else {}
        configured_output = _native_configured_output_path(metadata.get("mcpOutputPath"))
        if configured_output is not None:
            output_file = str(configured_output)
        keyword = str(metadata.get("mcpKeyword") or "手机")
        columns = [
            str(item).strip() for item in metadata.get("mcpRequiredColumns", DEFAULT_RPA_COLUMNS) if str(item).strip()
        ]
        if not columns:
            columns = list(DEFAULT_RPA_COLUMNS)
        current_paging = _collection_paging_settings(
            {
                name: _native_task_argument(node, name, "")
                for name in (
                    "region",
                    "collect_pages",
                    "collect_items",
                    "page_type",
                    "page_type_num",
                    "selector_page",
                    "selector_more",
                )
            }
        )
        normalized_ids = _normalize_generated_collection_bindings(
            normalized_ids,
            columns,
            native_elements.get("elementMap", {}),
        )
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("WebBrowser_DataCollectionV5 has duplicate column element bindings")
        collection_config, synced_columns, synced_attributes = _sync_native_collection_config(
            node,
            native_elements,
            element_ids=normalized_ids,
            columns=columns,
            data_attributes=metadata.get("mcpCollectionAttributes"),
            keyword=keyword,
            page_url=str(metadata.get("mcpCollectionPageUrl") or metadata.get("mcpTargetUrl") or ""),
            page_title=str(metadata.get("mcpCollectionPageTitle") or ""),
        )
        nested_collection = collection_config.get("collectionConfig") if isinstance(collection_config, dict) else None
        selectors = nested_collection.get("columnSelectorArray") if isinstance(nested_collection, dict) else None
        if not isinstance(selectors, list) or len(selectors) != len(normalized_ids):
            raise ValueError("WebBrowser_DataCollectionV5 requires reusable imported field selectors")
        _native_set_argument(node, "config", json.dumps(collection_config, ensure_ascii=False))
        _native_set_argument(node, "designated_obj", "网页对象")
        planned_collection = next(
            (
                item.get("config")
                for item in _rpa_component_plan(metadata)[0]
                if isinstance(item, dict)
                and item.get("component") == "WebBrowser_DataCollectionV5"
                and isinstance(item.get("config"), dict)
            ),
            None,
        )
        planned_paging_source = planned_collection or metadata
        planned_paging = _collection_paging_settings(planned_paging_source)
        raw_planned_region = _decode_selector_value(
            planned_paging_source.get("region", planned_paging_source.get("mcpCollectionRegion", ""))
        )
        planned_selector_values = {
            str(planned_paging.get(name) or "").strip()
            for name in ("selector_page", "selector_more")
        }
        planned_has_concrete_selector = any(
            value and not value.startswith("$")
            for value in planned_selector_values
        )
        # Metadata mirrors the selected pagination mode, but it may be written
        # before the live control is captured. Treating region/page_type_num
        # alone as an override would replace a captured task selector with an
        # empty value during an unrelated column rebind. A complete model plan
        # may still explicitly select single-page mode and clear old paging.
        planned_selects_pagination = planned_has_concrete_selector or (
            planned_collection is not None
            and raw_planned_region == "1"
        )
        # Rebinding collection columns is independent from pagination. Keep a
        # selector already persisted in the native task unless the model's
        # plan explicitly selected a different native pagination mode.
        _apply_collection_paging_settings(
            node,
            planned_paging if planned_selects_pagination else current_paging,
        )
        effective_paging = _collection_paging_settings(
            {
                name: _native_task_argument(node, name, "")
                for name in (
                    "region",
                    "collect_pages",
                    "collect_items",
                    "page_type",
                    "page_type_num",
                    "selector_page",
                    "selector_more",
                )
            }
        )
        to_excel = _collection_to_excel_setting(
            planned_collection,
            excel_mode=bool(metadata.get("mcpExcelMode")),
            fallback=_native_task_argument(node, "toExcel", 1),
        )
        _native_set_argument(node, "toExcel", str(to_excel))
        output_path = Path(output_file)
        stem = output_path.stem or "jd-products"
        extension = output_path.suffix if output_path.suffix.casefold() in _NATIVE_OUTPUT_SUFFIXES else ".xlsx"
        _native_set_argument(node, "filename_suffix", [json.dumps(stem, ensure_ascii=False), json.dumps(extension, ensure_ascii=False)])
        file_path = str(output_path.parent) if output_path.is_absolute() else str(
            (planned_collection or {}).get("filePath") or metadata.get("mcpOutputPath") or "res"
        )
        if not Path(file_path).suffix:
            try:
                _native_set_parameter(node, "filePath", json.dumps(file_path, ensure_ascii=False))
            except ValueError:
                _native_set_argument(node, "filePath", json.dumps(file_path, ensure_ascii=False))
        node["mcpElementIds"] = list(normalized_ids)
        _update_native_metadata(
            project_dir,
            len(_native_statement_ids(payload)),
            {
                "mcpResultFields": sorted(
                    {
                        _canonical_result_field(item)
                        for item in synced_columns
                        if _canonical_result_field(item) in _RPA_RESULT_FIELDS
                    }
                ),
                "mcpResultFieldElementIds": {
                    _canonical_result_field(header): element_id
                    for header, element_id in zip(synced_columns, normalized_ids)
                    if _canonical_result_field(header) in _RPA_RESULT_FIELDS
                },
                "mcpRequiredColumns": synced_columns,
                "mcpCollectionAttributes": synced_attributes,
                "mcpCollectionRegion": effective_paging["region"],
                "mcpCollectionPageType": effective_paging["page_type"],
                "mcpCollectionPageTypeNum": effective_paging["page_type_num"],
            },
        )
    else:
        # The live catalog may expose selector-bearing versions absent from
        # the compatibility list (e.g. Element_SetValue_V5 / Element_Click_V5).
        # Bind their declared primary selector without changing their type,
        # browser object expression or unrelated parameters.
        contract = _live_rpa_contract(paths, str(component)) or {}
        if "uiSelector" not in contract.get("parameter_names", []):
            raise ValueError(f"RPA step does not accept element bindings: {step_id}")
        if len(normalized_ids) != 1:
            raise ValueError(f"{component}.uiSelector requires exactly one element id")
        if step_id not in _native_statement_ids(payload):
            raise ValueError("generic selector binding currently supports main-flow steps only")
        element = native_elements["elementMap"][normalized_ids[0]]
        _native_set_parameter(node, "uiSelector", _native_selector_expression(normalized_ids[0], element))
    node["mcpElementIds"] = list(normalized_ids)
    paging_before_sync = (
        _native_collection_paging_snapshot(payload)
        if component == "WebBrowser_DataCollectionV5"
        else {}
    )
    temporary = task_path.with_suffix(task_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(task_path)
    writeback = _finalize_native_writeback(paths, project_dir, task_path, client=client)
    if paging_before_sync:
        paging_repair = _restore_native_collection_paging(project_dir, paging_before_sync)
        if paging_repair.get("changed"):
            _ensure_native_runtime_support(paths, project_dir, task_path, client=client)
        if isinstance(writeback, dict):
            writeback["paging_repair"] = paging_repair
            _, repaired_payload = _native_task_file(project_dir)
            writeback["readback_paging"] = _native_collection_paging_snapshot(repaired_payload)
    updated = _native_rpa_state(paths, str(project_dir))
    step = next(item for item in updated["steps"] if item["id"] == step_id)
    step_validation = validate_rpa_step(paths, str(project_dir), step_id)
    verification_gate = _rpa_verification_gate(
        str(project_dir),
        step,
        step_validation,
    )
    ui_feedback = _publish_rpa_ui(
        "step_ready" if step_validation["valid"] else "validation_failed",
        "element_rebind",
        (
            f"组件 {component} 的元素绑定已更新并通过校验。"
            if step_validation["valid"]
            else f"组件 {component} 的元素绑定已更新，但校验未通过。"
        ),
        animation="element_ready" if step_validation["valid"] else "validation_error",
        flow_ref=str(project_dir),
        component=str(component),
        step_id=step_id,
        element_count=len(normalized_ids),
        valid=bool(step_validation["valid"]),
    )
    return {
        "kind": "native_rpa_step_rebound",
        "flow_ref": str(project_dir),
        "flow_file": str(task_path),
        "step": step,
        "phase": updated["phase"],
        "next_tool": updated["next_tool"],
        "next_action": updated["next_action"],
        "step_validation": step_validation,
        "verification_gate": verification_gate,
        "ui_feedback": ui_feedback,
        "writeback": writeback,
    }


def _native_selector_chain(element: dict[str, Any]) -> list[dict[str, Any]] | None:
    selector = element.get("selector") if isinstance(element, dict) else None
    chain = selector.get("selector") if isinstance(selector, dict) else None
    return chain if isinstance(chain, list) and chain else None


_UIA_TO_WEB_TAG = {
    "article": "article",
    "group": "div",
    "text": "span",
    "static": "span",
    "label": "span",
    "edit": "input",
    "textarea": "textarea",
    "button": "button",
    "hyperlink": "a",
    "link": "a",
    "image": "img",
    "list": "ul",
    "listitem": "li",
    "table": "table",
    "row": "tr",
    "cell": "td",
    "checkbox": "input",
    "combobox": "select",
}
_COLLECTION_CONTAINER_NAMES = {
    "window",
    "pane",
    "document",
    "application",
    "desktop",
    "browser",
}
_COLLECTION_CARD_CLASS_MARKERS = (
    "plugingoodscardwrapper",
    "goods-card",
    "goodscontainer",
    "gl-item",
    "productcard",
    "product-item",
    "productitem",
    "goodsitem",
    "searchitem",
    "skuitem",
    "card",
)
_COLLECTION_IGNORED_PROPERTIES = {
    "processname",
    "processid",
    "automationid",
    "frameworkid",
    "controltype",
    "handle",
    "hwnd",
    "windowhandle",
}
_DYNAMIC_TEXT_NAMES = {"name", "text", "innertext"}


def _selector_property_value(prop: dict[str, Any]) -> str:
    """Read a selector property without treating numeric zero as missing."""
    raw_value = prop.get("textValue")
    if raw_value is None or raw_value == "":
        raw_value = prop.get("value")
    return _decode_selector_value(raw_value)


def _collection_property(
    name: str,
    value: str,
    source: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Normalize a native selector property to the web-selector shape."""
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return None
    if normalized_name.lower() in _COLLECTION_IGNORED_PROPERTIES:
        return None
    if normalized_name.lower() == "@index":
        try:
            index_value: Any = int(value)
        except (TypeError, ValueError):
            index_value = value
        expression = str(index_value)
        result: dict[str, Any] = {
            "name": "@index",
            # elementsFormatNew evaluates every selector ``value``. Keep the
            # expression string here; a numeric JSON value raises
            # ``eval() arg 1 must be a string`` in RuntimeHost 7.3.7.0.
            "value": expression,
            "pattern": "equal",
            "accurate": "true",
            "textValue": index_value,
            "expressionValue": expression,
        }
    else:
        result = {
            "name": normalized_name,
            # DataCollectionV5 compares this field with the raw DOM
            # attribute. Keep quotes out of the collection selector; action
            # selectors have their own expression serializer.
            "value": str(value),
            "pattern": "equal",
            "accurate": "false",
            "textValue": str(value),
            "expressionValue": "",
        }
    if isinstance(source, dict):
        for key in ("pattern", "accurate"):
            if source.get(key) not in (None, ""):
                result[key] = source[key]
    return result


def _css_selector_properties(css_selector: str) -> list[dict[str, Any]]:
    """Extract stable id/class/attribute hints from a simple CSS selector."""
    css = str(css_selector or "").strip()
    if not css:
        return []
    token = re.split(r"\s*[>+~]\s*|\s+", css)[-1]
    token = re.sub(
        r":(?:nth-[^\(]+|first-child|last-child|only-child)\([^)]*\)",
        "",
        token,
        flags=re.IGNORECASE,
    )
    token = re.sub(r":(?:first|last|only)-child", "", token, flags=re.IGNORECASE)
    properties: list[dict[str, Any]] = []
    id_match = re.search(r"#([A-Za-z_][\w-]*)", token)
    if id_match:
        property_value = _collection_property("id", id_match.group(1))
        if property_value:
            properties.append(property_value)
    classes = re.findall(r"\.([A-Za-z_][\w-]*)", token)
    if classes:
        property_value = _collection_property("class", " ".join(classes))
        if property_value:
            properties.append(property_value)
    for match in re.finditer(
        r"\[\s*([\w:-]+)(?:\s*=\s*[\"']?([^\]\"']+)[\"']?)?\s*\]",
        token,
    ):
        property_name, property_value = match.group(1), match.group(2) or ""
        if property_value:
            normalized = _collection_property(property_name, property_value)
            if normalized:
                properties.append(normalized)
    return properties


def _collection_chain_from_css(css_selector: str) -> list[dict[str, Any]]:
    """Convert a simple descendant CSS selector into DataCollectionV5 nodes."""
    css = str(css_selector or "").strip()
    if not css or any(combinator in css for combinator in ("+", "~", ",")):
        return []
    tokens = [token for token in re.split(r"\s*>\s*|\s+", css) if token]
    chain: list[dict[str, Any]] = []
    for token in tokens:
        tag_match = re.match(r"([A-Za-z][A-Za-z0-9-]*)", token)
        tag = tag_match.group(1) if tag_match else "div"
        chain.append(
            {
                "name": tag,
                "type": "web",
                "accurate": "true",
                "props": _css_selector_properties(token),
            }
        )
    return chain if _has_collection_selector(chain) else []


def _collection_selector_chain(element: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Convert a native element chain into the web-only chain required by V5."""
    # JS recovery fields are repeated values already materialized in each
    # product card.  Returning only the marker makes V5 align each column by
    # marker occurrence; the card repeat scope is supplied separately by the
    # generated collection config.
    generated_marker = str(element.get("mcpGeneratedMarker") or "").strip()
    if generated_marker:
        return [
            {
                "name": "span" if not generated_marker.endswith("link") else "a",
                "type": "web",
                "accurate": "true",
                "props": [
                    {
                        "name": "class",
                        "value": generated_marker,
                        "textValue": generated_marker,
                        "pattern": "equal",
                        "accurate": "true",
                        "expressionValue": "",
                    }
                ],
            }
        ]
    chain = _native_selector_chain(element)
    native_selector = element.get("mcpNativeSelector") if isinstance(element, dict) else None
    native_chain = native_selector.get("selector") if isinstance(native_selector, dict) else None
    if isinstance(native_chain, list) and any(
        isinstance(item, dict) and str(item.get("name") or "").strip().casefold() == "dataitem"
        for item in native_chain
    ):
        chain = native_chain
    if not chain:
        return None
    has_web_entry = any(
        isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "web" for item in chain
    )
    web_entries = [
        item
        for item in chain
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "web"
    ]
    css_selector = _selector_css(element)
    if has_web_entry and css_selector and len(web_entries) >= 4:
        css_chain = _collection_chain_from_css(css_selector)
        if css_chain:
            return css_chain
    converted: list[dict[str, Any]] = []
    table_dataitem_depth = 0
    for item in chain:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        source_name = str(item.get("name") or "div").strip()
        source_name_lower = source_name.lower()
        if has_web_entry:
            if item_type != "web":
                continue
            tag_name = source_name or "div"
        else:
            if source_name_lower in _COLLECTION_CONTAINER_NAMES:
                continue
            if source_name_lower == "table":
                table_dataitem_depth = 0
                tag_name = "table"
            elif source_name_lower == "dataitem" and any(part.get("name") == "table" for part in converted):
                tag_name = "tr" if table_dataitem_depth == 0 else "td"
                table_dataitem_depth += 1
            else:
                tag_name = _UIA_TO_WEB_TAG.get(source_name_lower, "div")
        props: list[dict[str, Any]] = []
        for raw_prop in item.get("props", []):
            prop = _selector_property_dict(raw_prop)
            if prop is None:
                continue
            prop_name = str(prop.get("name") or "").strip()
            if prop_name.lower() in _COLLECTION_IGNORED_PROPERTIES:
                continue
            if (
                not has_web_entry
                and source_name_lower in {"text", "static", "label", "dataitem"}
                and prop_name.lower() in _DYNAMIC_TEXT_NAMES
            ):
                continue
            value = _selector_property_value(prop)
            if not value and prop_name.lower() != "@index":
                continue
            if (
                not has_web_entry
                and prop_name.casefold() == "name"
                and tag_name.casefold() not in {"input", "select", "textarea", "button"}
            ):
                # UIA Name represents accessible text. RuntimeHost's web
                # selector contract exposes that value as lower-case
                # ``innertext``.
                prop_name = "innertext"
            normalized = _collection_property(prop_name, value, prop)
            if normalized:
                props.append(normalized)
        converted.append(
            {
                "name": tag_name,
                "type": "web",
                "accurate": item.get("accurate", "false"),
                "props": props,
            }
        )
    if not converted:
        return None
    # A captured Chrome/UIA chain often starts at several page-level wrappers
    # whose only stable-looking properties are session-local ``@index``
    # values. For DataCollectionV5 the repeat scope must be the product card:
    # keep the stable card class, drop its index, and retain descendant
    # indexes that locate the field inside each repeated card.
    card_start: int | None = None
    for index, part in enumerate(converted):
        class_values = [
            _decode_selector_value(prop.get("textValue") or prop.get("value"))
            for prop in part.get("props", [])
            if isinstance(prop, dict) and str(prop.get("name") or "").casefold() == "class"
        ]
        class_text = " ".join(str(value or "") for value in class_values).casefold().replace("_", "")
        if any(marker in class_text for marker in _COLLECTION_CARD_CLASS_MARKERS):
            card_start = index
            break
    if card_start is not None:
        converted = converted[card_start:]
        root = converted[0]
        root["props"] = [prop for prop in root.get("props", []) if str(prop.get("name") or "").casefold() != "@index"]
    else:
        # JS recovery nodes are appended inside every product card. Their
        # own selector is intentionally page-scoped, so add the stable card
        # family back here to make DataCollectionV5 align each value by row.
        card_context = element.get("card_context") if isinstance(element, dict) else None
        if isinstance(card_context, dict):
            semantic_container = str(card_context.get("semantic_container") or "").strip().casefold()
            if semantic_container == "article":
                first_group = next(
                    (index for index, part in enumerate(converted) if part.get("name") == "div"),
                    None,
                )
                if first_group is not None:
                    converted = converted[first_group:]
                    converted[0]["name"] = "article"
                    converted[0]["props"] = [
                        prop
                        for prop in converted[0].get("props", [])
                        if str(prop.get("name") or "").casefold() != "@index"
                    ]
                else:
                    converted.insert(
                        0,
                        {"name": "article", "type": "web", "accurate": "false", "props": []},
                    )
            family_key = str(
                card_context.get("family_key") or card_context.get("card_family_key") or card_context.get("class") or ""
            ).strip()
            if not semantic_container and family_key and not any(
                any(
                    marker
                    in " ".join(
                        str(_decode_selector_value(prop.get("textValue") or prop.get("value")) or "")
                        for prop in part.get("props", [])
                        if isinstance(prop, dict) and str(prop.get("name") or "").casefold() == "class"
                    )
                    .casefold()
                    .replace("_", "")
                    for marker in _COLLECTION_CARD_CLASS_MARKERS
                )
                for part in converted
            ):
                card_property = _collection_property("class", family_key)
                if card_property:
                    converted.insert(
                        0,
                        {
                            "name": "div",
                            "type": "web",
                            "accurate": "false",
                            "props": [card_property],
                        },
                    )
    if not _has_collection_selector(converted):
        css_properties = _css_selector_properties(_selector_css(element))
        if css_properties:
            converted[-1]["props"].extend(css_properties)
    return converted if _has_collection_selector(converted) else None


def _upsert_page_scope_element(
    paths: ShizaiPaths,
    flow_ref: str,
    root_element_id: str,
    *,
    pending_elements: dict[str, Any] | None = None,
) -> str:
    """Persist a stable page-context element for DataCollectionV5.

    A collection column selector identifies one field inside a repeated card;
    it is not a reliable page handle.  The designer's ``selector_page`` slot
    needs a separate selector that can resolve the current Chrome tab without
    traversing card descendants or captured ``@index`` values.
    """
    element_path, payload = _element_file(paths, flow_ref)
    if pending_elements is not None:
        payload = pending_elements
    element_map = payload.get("elementMap")
    if not isinstance(element_map, dict):
        raise ValueError("native element catalog has no elementMap")
    root = element_map.get(root_element_id)
    if not isinstance(root, dict):
        raise ValueError(f"page scope source element is not imported: {root_element_id}")
    title, url = _selector_page_identity(root)
    title = _browser_selector_title(title)
    if not title and not url:
        raise ValueError(f"page scope source element has no page identity: {root_element_id}")

    tab_props: list[dict[str, Any]] = []
    if title:
        title_expression = json.dumps(title, ensure_ascii=False)
        tab_props.append(
            {
                "name": "title",
                "value": title_expression,
                "pattern": "equal",
                "accurate": "true",
                # elementsFormatNew evaluates the first available value field.
                # Keep every serialized representation expression-safe; a raw
                # title can otherwise be parsed as a Python identifier/string.
                "textValue": title_expression,
                "expressionValue": title_expression,
            }
        )
    if url:
        url_expression = json.dumps(url, ensure_ascii=False)
        tab_props.append(
            {
                "name": "url",
                "value": url_expression,
                "pattern": "equal",
                "accurate": "false",
                "textValue": url_expression,
                "expressionValue": url_expression,
            }
        )

    # DataCollectionV5 accepts a tab selector as page identity, but a
    # tab-only selector cannot provide a DOM context to RuntimeHost.  Build a
    # short, stable web chain from the captured native chain instead.  The
    # native capture includes the browser shell and Document; only the stable
    # containers between Document and the first product card are useful here.
    context_chain, context_source, context_strength = _page_scope_context_chain(root)
    if not context_chain:
        raise ValueError(f"page scope source has no reusable web context: {root_element_id}")
    selector_items = (
        [
            {
                "name": "tab",
                "accurate": "true",
                "props": tab_props,
            }
        ]
        if tab_props
        else []
    )
    selector_items.extend(context_chain)

    generated = {
        "id": MCP_PAGE_SCOPE_ELEMENT_ID,
        "name": "当前 Chrome 结果页",
        "GUIType": "Chrome",
        "categoryName": title or "网页结果页",
        "modelType": "Element",
        "selector": {
            "commonProps": {
                "GUIType": "Chrome",
                "cssSelector": json.dumps("", ensure_ascii=False),
                "processName": "chrome.exe",
            },
            "selector": selector_items,
        },
        "selectorType": "comboSelector",
        "selectorParams": {
            "elementTypes": ["normal", "cv"],
            "comboSelectType": 0,
            "isAutoSimilar": 0,
        },
        "selectorVariableNames": [],
        "parentId": None,
        "page_title": title,
        "page_url": url,
        "process_name": "chrome.exe",
        "mcpSelectorMode": "web",
        "mcpPageScope": True,
        "mcpPageScopeSource": root_element_id,
        "mcpPageScopeContext": {
            "source": context_source,
            "strength": context_strength,
            "stops_before_product_card": True,
            "contains_product_card_index": any(
                any(
                    isinstance(prop, dict) and str(prop.get("name") or "").casefold() == "@index"
                    for prop in item.get("props", [])
                )
                for item in context_chain
            ),
        },
    }
    if element_map.get(MCP_PAGE_SCOPE_ELEMENT_ID) == generated:
        return MCP_PAGE_SCOPE_ELEMENT_ID
    element_map[MCP_PAGE_SCOPE_ELEMENT_ID] = generated
    if pending_elements is not None:
        return MCP_PAGE_SCOPE_ELEMENT_ID
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(element_path)
    return MCP_PAGE_SCOPE_ELEMENT_ID


def _page_scope_context_chain(
    element: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, str]:
    """Return a stable web context for a DataCollectionV5 page selector.

    ``selector`` is the runtime-ready field selector and commonly starts at
    the product card. ``mcpNativeSelector`` retains the original Chrome UIA
    capture, which is the only place where the page containers between
    ``Document`` and that card are still available.  Keep those containers,
    remove browser-shell and dynamic index properties, and stop before the
    repeated card.  A compact CSS/web-chain fallback handles older captures
    that do not include the native selector.
    """
    native_selector = element.get("mcpNativeSelector")
    native_entries = native_selector.get("selector") if isinstance(native_selector, dict) else None
    if isinstance(native_entries, list) and native_entries:
        document_index = next(
            (
                index
                for index, item in enumerate(native_entries)
                if isinstance(item, dict) and str(item.get("name") or "").casefold() == "document"
            ),
            None,
        )
        if document_index is not None:
            converted = _page_scope_web_entries(
                native_entries[document_index + 1 :],
                stop_at_card=True,
                source_is_web=False,
            )
            if converted:
                return converted, "mcpNativeSelector", "strong"

    selector = element.get("selector")
    entries = selector.get("selector") if isinstance(selector, dict) else None
    if isinstance(entries, list) and entries:
        converted = _page_scope_web_entries(
            entries,
            stop_at_card=True,
            source_is_web=True,
        )
        if converted:
            return converted, "selector", "strong" if len(converted) > 1 else "weak"

    css = _selector_css(element)
    css_props = _css_selector_properties(css)
    if css_props:
        for prop in css_props:
            value = _selector_property_value(prop)
            prop["value"] = json.dumps(value, ensure_ascii=False)
            prop["expressionValue"] = json.dumps(value, ensure_ascii=False)
        tag = str(element.get("tag") or "div").strip().lower()
        tag = _NATIVE_SELECTOR_TAGS.get(tag, tag)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", tag):
            tag = "div"
        return (
            [
                {
                    "name": tag,
                    "type": "web",
                    "accurate": "false",
                    "props": css_props,
                }
            ],
            "css_selector",
            "weak",
        )
    return [], "none", "none"


def _page_scope_web_entries(
    entries: list[Any],
    *,
    stop_at_card: bool,
    source_is_web: bool,
) -> list[dict[str, Any]]:
    """Convert page-level capture entries to runtime-expression web nodes.

    This chain is passed to ``elementsFormatNew`` through DataCollectionV5's
    ``selector_page`` argument.  Unlike the collection column selectors,
    which compare raw DOM attributes, ``elementsFormatNew`` evaluates each
    property value as an expression.  Quote the decoded value here so class
    names such as ``_searchCenter_czxc4_9`` remain string literals instead of
    being interpreted as RuntimeHost variables.
    """
    converted: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("name") or "div").strip()
        source_name_lower = source_name.casefold()
        item_type = str(item.get("type") or "").strip().casefold()
        if stop_at_card and _is_collection_card_entry(item):
            break
        if source_name_lower == "tab":
            continue
        if source_name_lower in _NATIVE_SELECTOR_SHELL_NAMES:
            continue
        if source_is_web and item_type != "web":
            continue
        tag = source_name if source_is_web else _UIA_TO_WEB_TAG.get(source_name_lower, "div")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", tag):
            tag = "div"
        props: list[dict[str, Any]] = []
        for raw_prop in item.get("props", []):
            prop = _selector_property_dict(raw_prop)
            if prop is None:
                continue
            name = str(prop.get("name") or "").strip().casefold()
            if name == "@index" or name in _COLLECTION_IGNORED_PROPERTIES:
                continue
            value = _selector_property_value(prop)
            if not value:
                continue
            if not source_is_web and name == "name" and tag.casefold() not in {
                "input",
                "select",
                "textarea",
                "button",
            }:
                # UIA Name is the accessible text for links and ordinary
                # content nodes. In a DOM selector, ``name`` means the HTML
                # name attribute and would miss <a>下一页</a> entirely.
                name = "innertext"
            normalized = _collection_property(name, value, prop)
            if normalized:
                expression = json.dumps(value, ensure_ascii=False)
                normalized["value"] = expression
                # The native formatter prefers textValue in some 7.3.x
                # builds. It must contain the same quoted expression as value
                # or CSS class names are evaluated as variable names.
                normalized["textValue"] = expression
                normalized["expressionValue"] = expression
                props.append(normalized)
        if props:
            converted.append(
                {
                    "name": tag,
                    "type": "web",
                    "accurate": item.get("accurate", "false"),
                    "props": props,
                }
            )
    return converted


def _is_collection_card_entry(item: dict[str, Any]) -> bool:
    """Return whether a captured selector entry is a repeated product card."""
    class_values = []
    for raw_prop in item.get("props", []):
        prop = _selector_property_dict(raw_prop)
        if prop is None or str(prop.get("name") or "").casefold() != "class":
            continue
        class_values.append(_selector_property_value(prop))
    class_text = " ".join(class_values).casefold().replace("_", "")
    return any(marker in class_text for marker in _COLLECTION_CARD_CLASS_MARKERS)


def _upsert_js_link_element(
    paths: ShizaiPaths,
    flow_ref: str,
    root_element_id: str,
) -> str:
    """Register the stable selector created by the JS link extraction step."""
    element_path, payload = _element_file(paths, flow_ref)
    element_map = payload.get("elementMap")
    if not isinstance(element_map, dict):
        raise ValueError("native element catalog has no elementMap")
    root = element_map.get(root_element_id)
    if not isinstance(root, dict):
        raise ValueError(f"JS link scope element is not imported: {root_element_id}")
    title, url = _selector_page_identity(root)
    tab_props: list[dict[str, Any]] = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    marker_prop = _collection_property("class", JS_LINK_MARKER)
    if marker_prop is None:
        raise ValueError("failed to construct JS link marker selector")
    root_card = root.get("card_context") if isinstance(root.get("card_context"), dict) else {}
    family_key = str(
        root_card.get("family_key") or root_card.get("card_family_key") or root_card.get("class") or ""
    ).strip()
    generated = {
        "id": JS_LINK_ELEMENT_ID,
        "name": "商品链接（JS生成）",
        "GUIType": "Chrome",
        "categoryName": title or "京东商品搜索",
        "modelType": "Element",
        "selector": {
            "commonProps": {
                "GUIType": "Chrome",
                "cssSelector": json.dumps(f"a.{JS_LINK_MARKER}", ensure_ascii=False),
                "processName": "chrome.exe",
            },
            "selector": ([{"name": "tab", "accurate": "true", "props": tab_props}] if tab_props else [])
            + [
                {"name": "a", "type": "web", "accurate": "true", "props": [marker_prop]},
            ],
        },
        "selectorType": "comboSelector",
        "selectorParams": {"elementTypes": ["normal", "cv"], "comboSelectType": 0, "isAutoSimilar": 0},
        "selectorVariableNames": [],
        # Selector scope is semantic context, not a native element-group
        # hierarchy.  The designer only accepts groupMap ids in parentId.
        "parentId": None,
        "page_title": title,
        "page_url": url,
        "process_name": "chrome.exe",
        "mcpSelectorMode": "web",
        "mcpGeneratedBy": "WebBrowser_RunJsInIeBySelector_v3",
        "mcpGeneratedMarker": JS_LINK_MARKER,
        "mcpCaptureSource": "deep_capture",
        "mcpVerificationStatus": "verified",
        "card_context": {"family_key": family_key, "generated": True} if family_key else None,
        "link_context": {
            "is_link": True,
            "has_href": True,
            "href": "",
            "service_or_navigation": False,
            "generated": True,
        },
    }
    element_map[JS_LINK_ELEMENT_ID] = generated
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(element_path)
    return JS_LINK_ELEMENT_ID


def _upsert_js_merchant_element(
    paths: ShizaiPaths,
    flow_ref: str,
    root_element_id: str,
) -> str:
    """Register the stable merchant-text selector created by the JS step."""
    element_path, payload = _element_file(paths, flow_ref)
    element_map = payload.get("elementMap")
    if not isinstance(element_map, dict):
        raise ValueError("native element catalog has no elementMap")
    root = element_map.get(root_element_id)
    if not isinstance(root, dict):
        raise ValueError(f"JS merchant scope element is not imported: {root_element_id}")
    title, url = _selector_page_identity(root)
    tab_props: list[dict[str, Any]] = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    marker_prop = _collection_property("class", JS_MERCHANT_MARKER)
    if marker_prop is None:
        raise ValueError("failed to construct JS merchant marker selector")
    root_card = root.get("card_context") if isinstance(root.get("card_context"), dict) else {}
    family_key = str(
        root_card.get("family_key") or root_card.get("card_family_key") or root_card.get("class") or ""
    ).strip()
    generated = {
        "id": JS_MERCHANT_ELEMENT_ID,
        "name": "商家名称（JS生成）",
        "GUIType": "Chrome",
        "categoryName": title or "京东商品搜索",
        "modelType": "Element",
        "selector": {
            "commonProps": {
                "GUIType": "Chrome",
                "cssSelector": json.dumps(f"span.{JS_MERCHANT_MARKER}", ensure_ascii=False),
                "processName": "chrome.exe",
            },
            "selector": ([{"name": "tab", "accurate": "true", "props": tab_props}] if tab_props else [])
            + [
                {"name": "span", "type": "web", "accurate": "true", "props": [marker_prop]},
            ],
        },
        "selectorType": "comboSelector",
        "selectorParams": {"elementTypes": ["normal", "cv"], "comboSelectType": 0, "isAutoSimilar": 0},
        "selectorVariableNames": [],
        "parentId": None,
        "page_title": title,
        "page_url": url,
        "process_name": "chrome.exe",
        "mcpSelectorMode": "web",
        "mcpGeneratedBy": "WebBrowser_RunJsInIeBySelector_v3",
        "mcpGeneratedMarker": JS_MERCHANT_MARKER,
        "mcpGeneratedValue": "merchant_text_or_jd_self_operated",
        "mcpCaptureSource": "deep_capture",
        "mcpVerificationStatus": "verified",
        "card_context": {"family_key": family_key, "generated": True} if family_key else None,
        "merchant_context": {
            "is_merchant": True,
            "generated": True,
            "fallback_value": "京东自营",
        },
    }
    element_map[JS_MERCHANT_ELEMENT_ID] = generated
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(element_path)
    return JS_MERCHANT_ELEMENT_ID


def _upsert_js_field_element(
    paths: ShizaiPaths,
    flow_ref: str,
    root_element_id: str,
    *,
    element_id: str,
    marker: str,
    name: str,
) -> str:
    """Register a stable in-card selector created by the recovery JS step."""
    element_path, payload = _element_file(paths, flow_ref)
    element_map = payload.get("elementMap")
    if not isinstance(element_map, dict):
        raise ValueError("native element catalog has no elementMap")
    root = element_map.get(root_element_id)
    if not isinstance(root, dict):
        raise ValueError(f"JS field scope element is not imported: {root_element_id}")
    title, url = _selector_page_identity(root)
    tab_props: list[dict[str, Any]] = []
    if title:
        tab_props.append(
            {
                "name": "title",
                "value": json.dumps(title, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "true",
                "textValue": title,
                "expressionValue": json.dumps(title, ensure_ascii=False),
            }
        )
    if url:
        tab_props.append(
            {
                "name": "url",
                "value": json.dumps(url, ensure_ascii=False),
                "pattern": "equal",
                "accurate": "false",
                "textValue": url,
                "expressionValue": json.dumps(url, ensure_ascii=False),
            }
        )
    marker_prop = _collection_property("class", marker)
    if marker_prop is None:
        raise ValueError(f"failed to construct JS field marker selector: {marker}")
    card = root.get("card_context") if isinstance(root.get("card_context"), dict) else {}
    family_key = str(card.get("family_key") or card.get("card_family_key") or card.get("class") or "").strip()
    element_map[element_id] = {
        "id": element_id,
        "name": name,
        "GUIType": "Chrome",
        "categoryName": title or "京东商品搜索",
        "modelType": "Element",
        "selector": {
            "commonProps": {
                "GUIType": "Chrome",
                "cssSelector": json.dumps(f'span[{marker}=\\"1\\"]', ensure_ascii=False),
                "processName": "chrome.exe",
            },
            "selector": ([{"name": "tab", "accurate": "true", "props": tab_props}] if tab_props else [])
            + [{"name": "span", "type": "web", "accurate": "true", "props": [marker_prop]}],
        },
        "selectorType": "comboSelector",
        "selectorParams": {"elementTypes": ["normal", "cv"], "comboSelectType": 0},
        "selectorVariableNames": [],
        "parentId": None,
        "page_title": title,
        "page_url": url,
        "process_name": "chrome.exe",
        "mcpSelectorMode": "web",
        "mcpGeneratedBy": "WebBrowser_RunJsInIeBySelector_v3",
        "mcpGeneratedMarker": marker,
        "mcpCaptureSource": "deep_capture",
        "mcpVerificationStatus": "verified",
        "card_context": {"family_key": family_key, "generated": True} if family_key else None,
    }
    temporary = element_path.with_suffix(element_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(element_path)
    return element_id


def _upsert_js_generated_elements(
    paths: ShizaiPaths,
    flow_ref: str,
    root_element_id: str,
) -> str:
    """Register every element produced by the page-recovery JS component."""
    link_id = _upsert_js_link_element(paths, flow_ref, root_element_id)
    _upsert_js_merchant_element(paths, flow_ref, root_element_id)
    _upsert_js_field_element(
        paths,
        flow_ref,
        root_element_id,
        element_id=JS_NAME_ELEMENT_ID,
        marker=JS_NAME_MARKER,
        name="商品名称（JS生成）",
    )
    _upsert_js_field_element(
        paths, flow_ref, root_element_id, element_id=JS_PRICE_ELEMENT_ID, marker=JS_PRICE_MARKER, name="价格（JS生成）"
    )
    _upsert_js_field_element(
        paths, flow_ref, root_element_id, element_id=JS_SALES_ELEMENT_ID, marker=JS_SALES_MARKER, name="销量（JS生成）"
    )
    return link_id


_COLLECTION_LINK_HEADERS = {"商品链接", "链接", "网址", "url", "URL"}
_COLLECTION_LINK_NOISE = (
    "customer_service",
    "客服",
    "help",
    "帮助",
    "privacy",
    "隐私",
    "policy",
    "政策",
    "license",
    "许可证",
    "证照",
    "举报",
    "about",
    "关于我们",
    "销售联盟",
)


def _collection_selector_context(element: dict[str, Any]) -> dict[str, Any]:
    """Extract card and link facts retained by a captured native element."""
    card = element.get("card_context")
    link = element.get("link_context")
    if isinstance(card, dict) or isinstance(link, dict):
        instance_key = str(card.get("instance_key") or card.get("key") or "") if isinstance(card, dict) else ""
        family_key = (
            str(card.get("family_key") or card.get("class") or card.get("marker") or instance_key or "")
            if isinstance(card, dict)
            else ""
        )
        return {
            "card_key": instance_key,
            "card_instance_key": instance_key,
            "card_family_key": family_key,
            "card_context": card if isinstance(card, dict) else None,
            "link_context": link if isinstance(link, dict) else {},
        }
    selector = element.get("selector") if isinstance(element.get("selector"), dict) else {}
    entries = selector.get("selector") if isinstance(selector, dict) else []
    card_key = ""
    card_instance_key = ""
    card_family_key = ""
    link_tag = False
    href = ""
    class_text = ""
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_name = str(entry.get("name") or "").strip().casefold()
            link_tag = link_tag or entry_name in {"a", "link", "hyperlink", "超链接", "链接"}
            values: dict[str, list[str]] = {}
            for raw_prop in entry.get("props", []):
                prop = _selector_property_dict(raw_prop)
                if prop is None:
                    continue
                prop_name = str(prop.get("name") or "").strip().casefold()
                value = _decode_selector_value(prop.get("textValue") or prop.get("value"))
                values.setdefault(prop_name, []).append(value)
            classes = " ".join(values.get("class", []))
            folded_classes = classes.casefold().replace("_", "")
            if any(
                marker in folded_classes
                for marker in (
                    "plugingoodscardwrapper",
                    "goodscontainer",
                    "goodscard",
                    "productcard",
                    "productitem",
                    "goodsitem",
                    "searchitem",
                    "skuitem",
                    "card",
                )
            ):
                index = next((item for item in values.get("@index", []) if item), "unknown")
                card_family_key = " ".join(classes.split()) or "product-card"
                card_instance_key = f"{card_family_key}|@index={index}"
                card_key = card_instance_key
            if values.get("href"):
                href = next((item for item in values["href"] if item), "")
            if classes:
                class_text = f"{class_text} {classes}".strip()
    common = selector.get("commonProps") if isinstance(selector.get("commonProps"), dict) else {}
    css = _decode_selector_value(common.get("cssSelector"))
    haystack = f"{css} {class_text} {href}".casefold()
    service_link = any(marker.casefold() in haystack for marker in _COLLECTION_LINK_NOISE)
    return {
        "card_key": card_key,
        "card_instance_key": card_instance_key,
        "card_family_key": card_family_key or card_key,
        "card_context": None,
        "link_context": {
            "is_link": link_tag or bool(re.match(r"^a(?:[.#\[]|$)", css, re.IGNORECASE)),
            "has_href": bool(href),
            "href": href,
            "service_or_navigation": service_link,
            "css_selector": css,
        },
    }


def _runtime_collection_selector_chain(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize designer selector expressions for DataCollectionV5 runtime.

    The designer stores static selector values as JSON expressions such as
    ``\"goods-card\"``. DataCollectionV5 compares the same field as a raw DOM
    attribute, so its serialized config must contain ``goods-card`` instead.
    Action selectors continue to use the designer expression form.
    """
    normalized = deepcopy(chain)
    for entry in normalized:
        if not isinstance(entry, dict):
            continue
        for prop in entry.get("props", []):
            if not isinstance(prop, dict):
                continue
            if str(prop.get("name") or "").strip().casefold() == "@index":
                continue
            value = _decode_selector_value(prop.get("textValue") or prop.get("value"))
            prop["value"] = value
            prop["textValue"] = value
            prop["expressionValue"] = ""
    return normalized


def _native_table_cell_index(element: dict[str, Any]) -> int | None:
    """Read a zero-based cell position from a captured table cell chain."""
    native_selector = element.get("mcpNativeSelector") if isinstance(element, dict) else None
    entries = native_selector.get("selector") if isinstance(native_selector, dict) else None
    if not isinstance(entries, list):
        return None
    table_seen = False
    table_descendants: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().casefold()
        if name == "table":
            table_seen = True
            table_descendants = []
        elif table_seen and name in {"dataitem", "div", "tr", "td"}:
            # Early MCP builds converted UIA DataItem nodes to web divs and
            # then persisted that converted chain as mcpNativeSelector. The
            # first descendant is the repeated row and the second is its cell.
            table_descendants.append(item)
    if len(table_descendants) < 2:
        return None
    for raw_prop in table_descendants[-1].get("props", []):
        prop = _selector_property_dict(raw_prop)
        if prop is None or str(prop.get("name") or "").strip().casefold() != "@index":
            continue
        try:
            index = int(_selector_property_value(prop))
        except (TypeError, ValueError):
            return None
        return index if index >= 0 else None
    return None


def _collection_css_selector_node(tag: str, css_selector: str) -> dict[str, Any]:
    """Build one native DataCollectionV5 CSS selector node."""
    return {
        "name": tag,
        "type": "web",
        "accurate": "true",
        "props": [
            {
                "name": "cssSelector",
                "value": css_selector,
                "pattern": "equal",
                "accurate": "true",
                "textValue": css_selector,
                "expressionValue": "",
            }
        ],
    }


def _collection_selector_requires_dom_capture(element: dict[str, Any]) -> bool:
    """Return whether a card field only has an inferred UIA-to-DOM path.

    Chrome UIA exposes accessibility roles such as Group and Text. Those
    roles do not identify the backing HTML tag, so converting them to div and
    span can produce a syntactically valid DataCollectionV5 selector that
    always returns zero rows. Native table cells, generated recovery markers,
    and selectors carrying real web/CSS/XPath evidence remain usable.
    """
    if str(element.get("mcpGeneratedMarker") or "").strip():
        return False
    if _native_table_cell_index(element) is not None:
        return False

    selector = element.get("selector") if isinstance(element.get("selector"), dict) else {}
    common = selector.get("commonProps") if isinstance(selector.get("commonProps"), dict) else {}
    if any(_decode_selector_value(common.get(name)) for name in ("cssSelector", "xpathSelector")):
        return False

    entries = selector.get("selector") if isinstance(selector.get("selector"), list) else []
    native_selector = (
        element.get("mcpNativeSelector") if isinstance(element.get("mcpNativeSelector"), dict) else {}
    )
    native_entries = (
        native_selector.get("selector") if isinstance(native_selector.get("selector"), list) else []
    )
    captured_entries = native_entries or entries
    if not captured_entries:
        return False
    if any(
        isinstance(item, dict) and str(item.get("type") or "").strip().casefold() == "web"
        for item in captured_entries
    ):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("name") or "").strip().casefold() in {"group", "text", "static", "label"}
        for item in captured_entries
    )


def _collection_binding_diagnostics(
    elements_payload: dict[str, Any],
    element_ids: list[str],
    columns: list[Any],
) -> list[dict[str, str]]:
    """Reject cross-card and known non-product links before RuntimeHost sees them."""
    element_map = elements_payload.get("elementMap") if isinstance(elements_payload, dict) else {}
    if not isinstance(element_map, dict):
        return []
    facts: list[tuple[str, str, dict[str, Any]]] = []
    for index, element_id in enumerate(element_ids):
        element = element_map.get(element_id)
        if not isinstance(element, dict):
            continue
        header = str(columns[index]).strip() if index < len(columns) else f"字段{index + 1}"
        facts.append((element_id, header, _collection_selector_context(element)))
    issues: list[dict[str, str]] = []
    inferred_fields = [
        header
        for element_id, header, _context in facts
        if isinstance(element_map.get(element_id), dict)
        and _collection_selector_requires_dom_capture(element_map[element_id])
    ]
    if inferred_fields:
        issues.append(
            {
                "check": "native_collection_dom_selector",
                "message": (
                    "以下采集字段只有 Chrome UIA 路径，不能可靠推导网页 DOM 标签："
                    f"{', '.join(inferred_fields)}。请用原生数据采集拾取/预览生成网页 selector，"
                    "或使用带真实 CSS/XPath 证据的通用预处理后重新绑定；禁止把 Group/Text 猜成 div/span。"
                ),
            }
        )
    card_family_keys = {
        facts_item[2].get("card_family_key") or facts_item[2].get("card_key")
        for facts_item in facts
        if facts_item[2].get("card_family_key") or facts_item[2].get("card_key")
    }
    if len(card_family_keys) > 1:
        issues.append(
            {
                "check": "same_product_card",
                "message": "采集字段绑定了多个商品卡片容器族；请重新捕获并选择同一 card_context.family_key 下的字段。",
            }
        )
    if card_family_keys and any(
        not (facts_item[2].get("card_family_key") or facts_item[2].get("card_key")) for facts_item in facts
    ):
        issues.append(
            {
                "check": "same_product_card",
                "message": "采集字段中有元素不在商品卡片容器族上下文内；不能与卡片字段混绑。",
            }
        )
    for element_id, header, context in facts:
        if header not in _COLLECTION_LINK_HEADERS:
            continue
        link_context = context.get("link_context") if isinstance(context.get("link_context"), dict) else {}
        if link_context.get("service_or_navigation"):
            issues.append(
                {
                    "check": "product_link_selector",
                    "message": f"元素 {element_id} 被识别为客服或导航链接，不能绑定到商品链接列；请重新捕获商品详情链接。",
                }
            )
            continue
        if link_context.get("is_link") is False:
            issues.append(
                {
                    "check": "product_link_selector",
                    "message": f"元素 {element_id} 不是原生链接控件；请捕获单元格内的 Hyperlink/链接子节点。",
                }
            )
    return issues


def _collection_runtime_capability_diagnostics(
    collection: dict[str, Any],
    columns: list[Any],
) -> list[dict[str, str]]:
    """Report DataCollectionV5 combinations the installed runtime cannot execute."""
    table_selector = collection.get("tableSelector") if isinstance(collection, dict) else None
    column_configs = collection.get("columnConfigArray") if isinstance(collection, dict) else None
    if not table_selector or not isinstance(column_configs, list):
        return []
    issues: list[dict[str, str]] = []
    for index, header in enumerate(columns):
        if str(header) not in _COLLECTION_LINK_HEADERS or index >= len(column_configs):
            continue
        config = column_configs[index]
        attr = str(config.get("dataAttrName") or "").strip() if isinstance(config, dict) else ""
        if attr.casefold() == "href":
            issues.append(
                {
                    "check": "native_table_nested_link_attribute",
                    "message": (
                        "当前 RuntimeHost 的 DataCollectionV5 表格模式按列读取单元格文本，不能读取单元格内链接的 href；"
                        "不要继续重试列 selector。请依据当前页面捕获证据选择通用网页预处理、非表格采集模式或其他已安装组件，"
                        "并在运行后验证该列为真实 URL。"
                    ),
                }
            )
    return issues


def _staged_collection_config(
    elements_payload: dict[str, Any],
    result_ids: list[str],
    *,
    keyword: str,
    columns: list[str] | None = None,
    data_attributes: dict[str, str] | None = None,
    fallback_page_url: str | None = None,
    fallback_page_title: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Build DataCollectionV5 selectors from the staged native element map."""
    element_map = elements_payload.get("elementMap") if isinstance(elements_payload, dict) else None
    if not isinstance(element_map, dict):
        return None, 0
    selectors: list[list[dict[str, Any]]] = []
    table_cell_indexes: list[int | None] = []
    titles: list[str] = []
    urls: list[str] = []
    for element_id in result_ids:
        element = element_map.get(element_id)
        if not isinstance(element, dict):
            continue
        chain = _collection_selector_chain(element)
        if not chain:
            continue
        selectors.append(_runtime_collection_selector_chain(chain))
        table_cell_indexes.append(_native_table_cell_index(element))
        title, url = _selector_page_identity(element)
        if title:
            titles.append(title)
        if url:
            urls.append(url)
    if not selectors:
        return None, 0
    headers = [str(item).strip() for item in (columns or DEFAULT_RPA_COLUMNS) if str(item).strip()]
    if not headers:
        headers = list(DEFAULT_RPA_COLUMNS)
    table_selector: list[dict[str, Any]] = []
    default_column_indexes = [0] * len(selectors)
    if len(table_cell_indexes) == len(selectors) and all(index is not None for index in table_cell_indexes):
        # DataCollectionV5's table mode is the only native path that reliably
        # repeats rows and paginates. It reads the selected td itself; nested
        # attributes such as an anchor href need a separate preprocessing step.
        table_selector = [{"name": "table", "type": "web", "accurate": "true", "props": []}]
        default_column_indexes = [int(index) for index in table_cell_indexes if index is not None]
        selectors = [
            [
                _collection_css_selector_node(
                    "td",
                    f"td:nth-child({int(cell_index) + 1})",
                )
            ]
            for cell_index in table_cell_indexes
        ]
    explicit_attributes = data_attributes if isinstance(data_attributes, dict) else {}

    def attribute_for(header: str) -> str:
        configured = str(explicit_attributes.get(header) or "").strip()
        if configured:
            return configured
        if header in {"商品链接", "链接", "网址", "URL", "url"}:
            return "href"
        return "innerText"

    columns = [
        {
            "columnHeader": headers[index] if index < len(headers) else f"字段{index + 1}",
            "firstRowTitle": 0,
            "columnHeader_bk": headers[index] if index < len(headers) else f"字段{index + 1}",
            "dateType": "",
            "defaultColumnIndex": default_column_indexes[index],
            "columnAttributes": [],
            "dataAttrName": attribute_for(headers[index] if index < len(headers) else f"字段{index + 1}"),
            "filterConfig": [],
            "dataProcessConfig": [],
        }
        for index in range(len(selectors))
    ]
    result_url = urls[0] if urls else str(fallback_page_url or "").strip()
    result_title = _browser_selector_title(
        titles[0] if titles else str(fallback_page_title or "").strip()
    )
    return {
        "pageInfo": {
            "windowTitle": result_title,
            "pageUrl": result_url,
            "windowType": "web",
            "processName": "",
        },
        "collectionConfig": {
            # RuntimeHost 7.3.7 determines the repeated row from the column
            # selector chains.  Its tableSelector slot is not a generic row
            # scope and supplying a card chain there yields zero rows.
            "tableSelector": table_selector,
            "columnSelectorArray": selectors,
            "columnConfigArray": columns,
            "mergeConfigMap": {},
            "commonSelector": [],
            "scrollVerticalDelay": -1,
            "scrollMaxTotalDelay": 0,
            "selectorVersion": "1.4",
            "scrollType": 0,
        },
    }, len(selectors)


def _update_native_task_index(root: Path, task_path: Path) -> None:
    """Register component dependencies, variables, and current source timestamps."""
    index_path = root / "file_index_700.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        index = {}
    if not isinstance(index, dict):
        index = {}
    try:
        task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    entities = task_payload.get("componentModelEntities")
    main = entities.get("MAIN_COMPONENT") if isinstance(entities, dict) else None
    if not isinstance(main, dict):
        return
    statement_ids: list[str] = []
    for argument in main.get("args0", []):
        if (
            isinstance(argument, dict)
            and argument.get("name") == "statements"
            and isinstance(argument.get("value"), list)
        ):
            statement_ids = [str(item) for item in argument["value"]]
            break
    module_key = f"flow_modules/{task_path.stem}.py"
    module = index.get(module_key)
    if not isinstance(module, dict):
        module = {
            "sourceId": task_path.stem,
            "type": "flowBlock",
            "moduleDefines": [],
        }
        index[module_key] = module
    try:
        task_time = int(task_path.stat().st_mtime * 1000)
    except OSError:
        task_time = int(time.time() * 1000)
    module_path = root / "projects" / "flow_modules" / f"{task_path.stem}.py"
    try:
        code_time = int(module_path.stat().st_mtime * 1000) if module_path.is_file() else task_time
    except OSError:
        code_time = task_time
    module["taskTime"] = task_time
    module["codeTime"] = max(task_time, code_time)
    module["updateTime"] = max(int(module.get("updateTime") or 0), task_time)
    dependency_map: dict[str, list[dict[str, Any]]] = {}
    for node_id in statement_ids:
        node = entities.get(node_id) if isinstance(entities, dict) else None
        if not isinstance(node, dict):
            continue
        component = str(node.get("type") or "")
        descriptor = _NATIVE_CODE_DESCRIPTORS.get(component)
        if not descriptor:
            continue
        plugin_name = descriptor[2]
        dependency_map[node_id] = [
            {
                "name": plugin_name,
                "version": "3.3.4.0" if plugin_name == "Keyboard" else "",
                "dependType": "plugin",
            }
        ]
    module["dependencyMap"] = dependency_map
    module["variableReferenceList"] = _native_variable_reference_list(task_payload)
    module["sourceId"] = module.get("sourceId") or task_path.stem
    module["type"] = module.get("type") or "flowBlock"
    module["moduleDefines"] = module.get("moduleDefines") if isinstance(module.get("moduleDefines"), list) else []
    index_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def validate_project(paths: ShizaiPaths, project_ref: str) -> dict[str, Any]:
    """Validate the native project files without changing them."""
    summary = inspect_project(paths, project_ref)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    project_dir = Path(summary["path"])
    check("project_directory", project_dir.is_dir(), str(project_dir))
    check("szrpa.json", summary["json_files"]["szrpa.json"]["valid"], "metadata JSON")
    check("elements.json", summary["json_files"]["elements.json"]["valid"], "element catalog JSON")
    check("project.flow", (project_dir / "project.flow").is_file(), "native flow file")
    check("main.sz", (project_dir / "main.sz").is_file(), "native main script")
    malformed = [name for name, state in summary["json_files"].items() if state["exists"] and not state["valid"]]
    check("known_json_files", not malformed, ", ".join(malformed) if malformed else "all readable")
    return {
        "project": summary,
        "valid": all(item["passed"] for item in checks),
        "checks": checks,
    }


def export_project(paths: ShizaiPaths, project_ref: str, output: str) -> dict[str, Any]:
    """Create a ZIP copy of a native project directory."""
    project_dir = resolve_project(paths, project_ref)
    output_path = Path(output).expanduser().resolve()
    if output_path.suffix.lower() != ".zip":
        output_path = output_path.with_suffix(output_path.suffix + ".zip" if output_path.suffix else ".zip")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_output = os.path.normcase(str(output_path))
    written = 0
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(project_dir.rglob("*")):
            if not item.is_file() or os.path.normcase(str(item)) == source_output:
                continue
            archive.write(item, item.relative_to(project_dir).as_posix())
            written += 1
    return {
        "project_id": project_dir.name,
        "source": str(project_dir),
        "output": str(output_path),
        "file_count": written,
        "size": output_path.stat().st_size,
    }


def package_records(paths: ShizaiPaths) -> list[dict[str, Any]]:
    """Return package index rows for diagnostics and agent inspection."""
    return query_rows(
        paths,
        "SELECT * FROM application_package ORDER BY id DESC",
    )
