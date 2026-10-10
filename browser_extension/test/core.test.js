const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../src/core.js");

// Small DOM fixture supporting only the CSS forms exercised below. Real browser
// and native-component evidence is recorded separately from these unit tests.
function matches(node, selector) {
  const chain = selector.split(/\s*>\s*/);
  if (chain.length > 1) return matches(node, chain.pop()) && !!node.parentElement && matches(node.parentElement, chain.join(" > "));
  const nth = selector.match(/:nth-of-type\((\d+)\)$/);
  if (nth) {
    selector = selector.slice(0, nth.index);
    if (!node.parentElement || node.parentElement.children.filter(child => child.tagName === node.tagName)[Number(nth[1]) - 1] !== node) return false;
  }
  const attribute = selector.match(/\[([\w-]+)(?:="([^"]*)")?\]$/);
  if (attribute) {
    selector = selector.slice(0, attribute.index);
    if (!(attribute[1] in node._attrs) || (attribute[2] !== undefined && node._attrs[attribute[1]] !== attribute[2])) return false;
  }
  const compound = selector.match(/^([a-zA-Z]+|\*)?((?:[.#][\w-]+)*)$/);
  if (!compound || (!compound[1] && !compound[2] && !attribute)) return false;
  if (compound[1] && compound[1] !== "*" && node.tagName !== compound[1].toUpperCase()) return false;
  return (compound[2].match(/[.#][\w-]+/g) || []).every(part => part[0] === "#" ? node.id === part.slice(1) : node.className.split(/\s+/).includes(part.slice(1)));
}
function descendants(root) {
  return root.children.flatMap(child => [child, ...descendants(child)]);
}
function el(tag, attrs = {}, text = "") {
  const node = { nodeType: 1, tagName: tag.toUpperCase(), id: attrs.id || "", className: attrs.class || "", parentElement: null, children: [], textContent: text, innerText: text, value: attrs.value || "", type: attrs.type || "", style: { display: "", visibility: "", opacity: "" }, _attrs: attrs };
  node.getAttribute = name => node._attrs[name] || "";
  node.getBoundingClientRect = () => ({ x: 0, y: 0, width: 100, height: 20 });
  node.append = child => { child.parentElement = node; node.children.push(child); };
  node.querySelectorAll = selector => descendants(node).filter(candidate => matches(candidate, selector));
  node.querySelector = selector => node.querySelectorAll(selector)[0] || null;
  return node;
}
function fakeDocument(root, pointNode) {
  return { defaultView: { getComputedStyle: n => n.style }, elementFromPoint: () => pointNode,
    querySelectorAll(selector) { return [root, ...descendants(root)].filter(n => matches(n, selector)); }, querySelector(selector) { return this.querySelectorAll(selector)[0] || null; } };
}

test("inspectPoint returns a verified visible locator and no DOM dump", () => {
  const root = el("div"); const input = el("input", { name: "keyword", placeholder: "Search" }); root.append(input);
  const doc = fakeDocument(root, input); const result = core.inspectPoint(doc, { x: 5, y: 6 });
  assert.equal(result.found, true); assert.equal(result.tag, "input"); assert.equal(result.point_is_match, true); assert.equal(result.selector_candidates[0].visible_matches, 1); assert.equal(result.children, undefined);
});

test("inspectPoint can promote a nested hit to its interactive ancestor", () => {
  const root = el("div"); const button = el("button", { id: "submit" }); const label = el("span", {}, "Search");
  button.append(label); root.append(button);
  const result = core.inspectPoint(fakeDocument(root, label), { x: 5, y: 6, preferInteractive: true });
  assert.equal(result.tag, "button");
  assert.equal(result.attributes.id, "submit");
});

test("local link evidence stays inside the nearest repeated container", () => {
  const root = el("main");
  const card1 = el("article", { class: "card" });
  const card2 = el("article", { class: "card" });
  const link1 = el("a", { class: "product-link", href: "https://example.test/p/1" }, "Phone A");
  const link2 = el("a", { class: "product-link", href: "https://example.test/p/2" }, "Phone B");
  link1.href = link1._attrs.href; link2.href = link2._attrs.href;
  card1.append(link1); card2.append(link2); root.append(card1); root.append(card2);
  card1.querySelectorAll = selector => selector === "a[href]" ? [link1] : [];
  card2.querySelectorAll = selector => selector === "a[href]" ? [link2] : [];
  const doc = fakeDocument(root, link1);
  const baseQuery = doc.querySelectorAll.bind(doc);
  doc.querySelectorAll = selector => selector === "article.card" ? [card1, card2] :
    selector === "a.product-link" ? [link1, link2] : baseQuery(selector);
  const result = core.localLinkEvidence(link1, doc);
  assert.equal(result.links.length, 1);
  assert.equal(result.links[0].href, "https://example.test/p/1");
  assert.equal(result.links[0].selector_candidates[0].visible_matches, 2);
  assert.equal(result.container_candidates[0].visible_matches, 2);
});

test("password fields are rejected and values are never exposed", () => {
  const root = el("div"); const input = el("input", { type: "password", value: "secret" }); root.append(input);
  const doc = fakeDocument(root, input); const result = core.inspectPoint(doc, { x: 1, y: 1 });
  assert.equal(result.rejected, true); assert.equal(result.reason, "password_element"); assert.equal(result.value, undefined);
});

test("invalid selectors are reported without throwing", () => {
  const doc = { querySelectorAll: () => { throw new Error("bad"); }, elementFromPoint: () => null };
  assert.equal(core.validateLocator(doc, "[", null).valid, false);
});

test("screenshot references reject stale page revisions", () => {
  assert.throws(() => core.validateScreenshotReference({ tabId: 7, url: "https://example.test", pageRevision: "4" }, { tabId: 7, pageRevision: "3" }, "https://example.test"), /stale_screenshot_revision/);
});

test("repeated collection inference returns rows and preserves verified field selectors", () => {
  const root = el("main"); const card1 = el("article", { class: "card" }); const title1 = el("span", { class: "title" }, "Phone A"); const price1 = el("span", { class: "price" }, "$10"); const card2 = el("article", { class: "card" }); const title2 = el("span", { class: "title" }, "Phone B"); const price2 = el("span", { class: "price" }, "$20");
  card1.append(title1); card1.append(price1); card2.append(title2); card2.append(price2); root.append(card1); root.append(card2);
  const doc = fakeDocument(root, title1); const result = core.inferRepeatedCollection(doc, [{ name: "name", selector: "span.title", element: title1 }, { name: "price", selector: "span.price:nth-of-type(2)", element: price1 }], 2);
  assert.equal(result.rowCount, 2); assert.equal(result.columns.length, 2); assert.equal(result.sampleRows.length, 2);
  assert.equal(result.columns[0].sourceSelector, "span.title");
  assert.equal(result.columns[1].sourceSelector, "span.price:nth-of-type(2)");
  assert.equal(result.columns[1].fieldSelector, "span.price");
  assert.deepEqual(result.sampleRows, [["Phone A", "$10"], ["Phone B", "$20"]]);
});

function mixedCards() {
  const root = el("main");
  const cards = ["A", "B", "C"].map((name, index) => {
    const card = el("article", { class: "card" });
    const title = el("span", { class: "title" }, "Phone " + name);
    const price = el("span", { class: "price", "data-testid": "price" }, "$" + (index + 1) * 10);
    const titleBox = el("div"); titleBox.append(title); card.append(titleBox);
    // An ad moves the price to a different sibling position, and the last card
    // changes the price wrapper depth. Both must retain the field's identity.
    if (index) { const ad = el("div"); ad.append(el("span", {}, "Fast screen")); card.append(ad); }
    const priceBox = el("div");
    if (index === 2) { const inner = el("section"); inner.append(price); priceBox.append(inner); }
    else priceBox.append(price);
    card.append(priceBox); root.append(card);
    return { card, title, price, priceBox };
  });
  const fields = [{ name: "name", selector: "span.title", element: cards[0].title }, { name: "price", selector: "span.price", element: cards[0].price }];
  return { root, cards, fields, doc: fakeDocument(root, cards[0].title) };
}

test("mixed card layouts sample the same field identity instead of sibling positions", () => {
  const { doc, fields } = mixedCards();
  const result = core.inferRepeatedCollection(doc, fields, 3);
  assert.equal(result.rowCount, 3);
  assert.deepEqual(result.sampleRows, [["Phone A", "$10"], ["Phone B", "$20"], ["Phone C", "$30"]]);
  assert.equal(result.columns[1].fieldSelector, 'span[data-testid="price"]');
  assert.deepEqual(result.verification, { method: "field-identity-per-row", candidateRowCount: 3, verifiedRowCount: 3 });
});

test("missing fields do not silently drop a row or substitute its advert", () => {
  const { doc, cards, fields } = mixedCards();
  cards[1].priceBox.children = [];
  assert.throws(() => core.inferRepeatedCollection(doc, fields, 3), /repeated field identity/);
});

test("duplicate fields in a later row are rejected instead of taking the first match", () => {
  const { doc, cards, fields } = mixedCards();
  cards[1].priceBox.append(el("span", { class: "price", "data-testid": "price" }, "$999"));
  assert.throws(() => core.inferRepeatedCollection(doc, fields, 3), /repeated field identity/);
});

test("a visible same-selector field outside the rows cannot enter native page-wide bindings", () => {
  const { root, cards, fields } = mixedCards();
  root.append(el("span", { class: "price", "data-testid": "price" }, "$999"));
  assert.throws(() => core.inferRepeatedCollection(fakeDocument(root, cards[0].title), fields, 3), /repeated field identity/);
});

test("all requested fields must be present and safe, even when two others resolve", () => {
  for (const element of [null, el("input", { type: "password", value: "secret" })]) {
    const { doc, fields } = mixedCards();
    assert.throws(() => core.inferRepeatedCollection(doc, [...fields, { name: "extra", selector: "input", element }], 3), /all requested fields/);
  }
});

test("positional-only fields do not provide repeated identity evidence", () => {
  const root = el("main");
  for (const label of ["A", "B"]) {
    const card = el("article", { class: "card" }); card.append(el("span", {}, label)); card.append(el("span", {}, "$10")); root.append(card);
  }
  const fields = root.children[0].children.map((element, index) => ({ name: String(index), selector: "article.card > span:nth-of-type(" + (index + 1) + ")", element }));
  assert.throws(() => core.inferRepeatedCollection(fakeDocument(root, fields[0].element), fields, 2), /repeated field identity/);
});

test("selectors and selected elements must agree before generalizing", () => {
  const { doc, fields } = mixedCards();
  fields[1].selector = "span.title";
  assert.throws(() => core.inferRepeatedCollection(doc, fields, 3), /source selector does not match/);
});
