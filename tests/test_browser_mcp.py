from __future__ import annotations

import base64

import pytest

from shizai_agent.core import mcp_server, project
from shizai_agent.core.browser_mcp import (
    BrowserBridgeManager,
    collection_elements,
    point_elements,
)
from shizai_agent.core.workflow_contracts import RPA_AUTHORING_TOOLS


def _point_result() -> dict:
    return {
        "tabId": 7,
        "screenshotId": "shot-1",
        "pageRevision": "12",
        "url": "https://example.test/search?q=phone",
        "title": "Search",
        "points": [
            {
                "found": True,
                "rejected": False,
                "point_is_match": True,
                "visible": True,
                "tag": "input",
                "text": "",
                "attributes": {"name": "q", "placeholder": "Search"},
                "point": {"x": 320, "y": 80, "role": "search-input"},
                "selector_candidates": [
                    {"selector": "input[name=\"q\"]", "matches": 1, "visible_matches": 1}
                ],
            }
        ],
    }


def test_point_evidence_becomes_verified_native_selector() -> None:
    result = _point_result()
    records = point_elements(result, page=result, prefix="search")
    native = project._native_element(records[0], records[0]["id"], capture_source="browser_bridge")

    assert native["mcpCaptureSource"] == "browser_bridge"
    assert native["mcpVerificationStatus"] == "verified"
    assert native["mcpBrowserEvidence"]["screenshotId"] == "shot-1"
    assert native["selector"]["commonProps"]["cssSelector"] == '"input[name=\\"q\\"]"'
    web_parts = [part for part in native["selector"]["selector"] if part.get("type") == "web"]
    assert web_parts
    assert all(
        prop["accurate"] == "true"
        for part in web_parts
        for prop in part.get("props", [])
    )


def test_collection_schema_becomes_repeated_verified_fields() -> None:
    page = {
        "tabId": 7,
        "screenshotId": "shot-2",
        "pageRevision": "20",
        "url": "https://example.test/phones",
        "title": "Phones",
    }
    inferred = {
        "commonSelector": "article.product-card",
        "rowCount": 18,
        "verified": True,
        "verification": {"method": "field-identity-per-row", "candidateRowCount": 18, "verifiedRowCount": 18},
        "columns": [
            {"name": "title", "sourceSelector": "a.title", "fieldSelector": "a.title", "relativeSelector": "a.title"},
            {
                "name": "price",
                "sourceSelector": "span.price:nth-of-type(1)",
                "fieldSelector": 'span[data-testid="price"]',
                "relativeSelector": 'span[data-testid="price"]',
            },
        ],
    }
    records = collection_elements(inferred, page=page, prefix="collect")
    assert [record["css_selector"] for record in records] == [
        "a.title",
        'span[data-testid="price"]',
    ]
    assert records[1]["mcpBrowserEvidence"]["sourceSelector"] == "span.price:nth-of-type(1)"
    assert records[1]["mcpBrowserEvidence"]["fieldSelector"] == 'span[data-testid="price"]'
    assert all(record["card_context"]["row_count"] == 18 for record in records)
    native = project._native_element(records[0], records[0]["id"], capture_source="browser_bridge")
    assert native["mcpVerificationStatus"] == "verified"
    assert native["mcpBrowserEvidence"]["commonSelector"] == "article.product-card"


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"verified": False},
        {"verified": True, "verification": "untrusted"},
        {"verified": True, "verification": {"method": "positional"}},
        {
            "verified": True,
            "verification": {"method": "field-identity-per-row", "candidateRowCount": 3, "verifiedRowCount": 2},
        },
    ],
)
def test_collection_binding_requires_complete_field_identity_evidence(evidence: dict) -> None:
    inferred = {
        "commonSelector": "article.card",
        "rowCount": 2,
        "columns": [{"relativeSelector": "span.title"}, {"relativeSelector": "span.price"}],
        **evidence,
    }
    with pytest.raises(ValueError, match="field identity was not verified"):
        collection_elements(inferred, page={}, prefix="collect")


def test_collection_binding_never_falls_back_to_unverified_positional_paths() -> None:
    inferred = {
        "commonSelector": "article.card",
        "rowCount": 2,
        "verified": True,
        "verification": {"method": "field-identity-per-row", "candidateRowCount": 2, "verifiedRowCount": 2},
        "columns": [
            {"sourceSelector": "span.title", "relativeSelector": "div:nth-of-type(1) > span"},
            {"sourceSelector": "span.price", "relativeSelector": "div:nth-of-type(3) > span"},
        ],
    }
    with pytest.raises(ValueError, match="no verified field selector"):
        collection_elements(inferred, page={}, prefix="collect")


def test_complete_browser_binding_is_not_replaced_by_older_generated_markers() -> None:
    element_ids = ["bridge-name", "bridge-price", "bridge-sales", "bridge-merchant"]
    element_map = {
        **{element_id: {} for element_id in element_ids},
        project.JS_NAME_ELEMENT_ID: {},
        project.JS_PRICE_ELEMENT_ID: {},
        project.JS_SALES_ELEMENT_ID: {},
        project.JS_MERCHANT_ELEMENT_ID: {},
    }

    assert project._normalize_generated_collection_bindings(
        element_ids,
        ["商品名称", "价格", "热度原文", "商家"],
        element_map,
    ) == element_ids


def test_designer_output_collision_suffixes_preserve_output_contract() -> None:
    expected = ["数据采集结果", "Excel路径", "监控空列数据"]
    assert project._output_names_match(
        expected,
        ["数据采集结果_1", "Excel路径_1", "监控空列数据_1"],
    )
    assert not project._output_names_match(
        expected,
        ["错误结果_1", "Excel路径_1", "监控空列数据_1"],
    )


def test_missing_extension_has_compact_install_guidance(tmp_path) -> None:
    status = BrowserBridgeManager(config_path=tmp_path / "missing.json").status()
    assert status["state"] == "not_installed"
    assert status["requires_user_action"] is True
    assert status["install_command"].endswith("install --with-browser-extension")


def test_browser_tools_are_in_authoring_profile_and_capture_is_image_content() -> None:
    expected = {
        "shizai_browser_status",
        "shizai_browser_tabs",
        "shizai_browser_activate_tab",
        "shizai_browser_capture_viewport",
        "shizai_browser_inspect_points",
        "shizai_browser_infer_collection",
        "shizai_browser_action",
    }
    schemas = {tool.name: tool for tool in mcp_server._schemas("rpa-authoring")}
    assert expected <= RPA_AUTHORING_TOOLS
    assert expected <= schemas.keys()

    png = base64.b64encode(b"png-bytes").decode("ascii")
    content = mcp_server._mcp_tool_content(
        "shizai_browser_capture_viewport",
        {"tabId": 7, "screenshotId": "shot-1", "dataUrl": f"data:image/png;base64,{png}"},
    )
    assert len(content) == 2
    assert content[0].type == "text" and "dataUrl" not in content[0].text
    assert content[1].type == "image" and content[1].data == png


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def status(self, *, connect_wait: float = 0.0) -> dict:
        return {"state": "ready", "connect_wait": connect_wait}

    def call(self, method: str, params: dict | None = None, **_kwargs) -> dict:
        self.calls.append((method, params or {}))
        if method == "inspect_points":
            return _point_result()
        return {"ok": True}


def test_browser_dispatch_maps_protocol_and_requires_write_for_action(monkeypatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(mcp_server, "_BROWSER_BRIDGE_MANAGER", manager)
    result = mcp_server._dispatch(
        "shizai_browser_inspect_points",
        {"tab_id": 7, "screenshot_id": "shot-1", "page_revision": "12", "points": [{"x": 3, "y": 4}]},
    )
    assert result["points"]
    assert manager.calls[-1] == (
        "inspect_points",
        {"tabId": 7, "screenshotId": "shot-1", "pageRevision": "12", "points": [{"x": 3, "y": 4}]},
    )
    with pytest.raises(PermissionError):
        mcp_server._dispatch(
            "shizai_browser_activate_tab",
            {"tab_id": 7, "allow_write": False},
        )
    mcp_server._dispatch(
        "shizai_browser_activate_tab",
        {"tab_id": 7, "allow_write": True},
    )
    assert manager.calls[-1] == ("activate_tab", {"tabId": 7})

    with pytest.raises(PermissionError):
        mcp_server._dispatch(
            "shizai_browser_action",
            {"tab_id": 7, "action": "click", "target": {"selector": "button"}, "allow_write": False},
        )

    mcp_server._dispatch(
        "shizai_browser_action",
        {
            "tab_id": 7,
            "action": "scroll",
            "target": {"selector": "body"},
            "delta_x": 0,
            "delta_y": 480,
            "page_revision": "12",
            "allow_write": True,
        },
    )
    assert manager.calls[-1] == (
        "performAction",
        {
            "tabId": 7,
            "action": "scroll",
            "target": {"selector": "body"},
            "deltaX": 0,
            "deltaY": 480,
            "pageRevision": "12",
        },
    )

    action_schema = {
        tool.name: tool.inputSchema for tool in mcp_server._schemas("rpa-authoring")
    }["shizai_browser_action"]
    assert "scroll" in action_schema["properties"]["action"]["enum"]
