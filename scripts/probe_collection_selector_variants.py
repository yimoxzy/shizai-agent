"""Compare DataCollectionV5 selector encodings against the current page."""

from __future__ import annotations

import argparse
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from shizai_agent.core.cloud_api import ShizaiDesktopAgent

DEFAULT_STEP_ID = "collect-jd-phones"
MARKERS = (
    "data-shizai-agent-product-name",
    "data-shizai-agent-product-price",
    "data-shizai-agent-product-sales",
)
BROWSER_EXTENSION_FIELDS = (
    ("span", "_newStyle_1k2fi_39"),
    ("span", "_price_9y3st_31"),
    ("span", "_goods_volume_1d8up_1"),
)


def _prop(name: str, value: str, *, quoted: bool = False, pattern: str = "equal") -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False) if quoted else value
    return {
        "name": name,
        "value": encoded,
        "textValue": value,
        "pattern": pattern,
        "accurate": "true" if pattern == "equal" else "false",
        "expressionValue": encoded if quoted else "",
    }


def _node(tag: str, props: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": tag, "type": "web", "accurate": "true", "props": props}


def _variants() -> dict[str, list[list[dict[str, Any]]]]:
    raw_class = [[_node("span", [_prop("class", marker)])] for marker in MARKERS]
    quoted_class = [[_node("span", [_prop("class", marker, quoted=True)])] for marker in MARKERS]
    raw_attr = [[_node("span", [_prop(marker, "1")])] for marker in MARKERS]
    quoted_attr = [[_node("span", [_prop(marker, "1", quoted=True)])] for marker in MARKERS]
    css = [[_node("span", [_prop("cssSelector", f"span[{marker}='1']")])] for marker in MARKERS]
    card_attr = [
        [
            _node("div", [_prop("class", "plugin_goodsCardWrapper", pattern="contain")]),
            _node("span", [_prop(marker, "1")]),
        ]
        for marker in MARKERS
    ]
    card_css = [
        [
            _node("div", [_prop("cssSelector", "div.plugin_goodsCardWrapper")]),
            _node("span", [_prop("cssSelector", f"span[{marker}='1']")]),
        ]
        for marker in MARKERS
    ]
    visible_css = [
        [_node("div", [_prop("cssSelector", "div.plugin_goodsCardWrapper")]), _node("span", [_prop("cssSelector", selector)])]
        for selector in (
            "div._newStyle_1k2fi_39 span",
            "div._newStyle_9y3st_27._container_9y3st_2 span",
            "div._common-wrap_9uih3_1._text-list_9uih3_33 span",
        )
    ]
    browser_extension_raw_class = [
        [_node(tag, [_prop("class", class_name)])]
        for tag, class_name in BROWSER_EXTENSION_FIELDS
    ]
    return {
        "raw_class": raw_class,
        "quoted_class": quoted_class,
        "raw_attribute": raw_attr,
        "quoted_attribute": quoted_attr,
        "css_selector": css,
        "card_then_attribute": card_attr,
        "card_then_css": card_css,
        "card_then_visible_css": visible_css,
        "browser_extension_raw_class": browser_extension_raw_class,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref", type=Path, help="client application directory holding the task file")
    parser.add_argument("--task-file", default="io5cd.task")
    parser.add_argument("--step-id", default=DEFAULT_STEP_ID)
    parser.add_argument("--output", type=Path, default=Path("collection-selector-variants.json"))
    args = parser.parse_args()
    payload = json.loads((args.flow_ref / args.task_file).read_text(encoding="utf-8"))
    node = payload["componentModelEntities"][args.step_id]
    raw_config = next(item["value"] for item in node["args0"] if item.get("name") == "config")
    base_config = json.loads(raw_config)
    client = ShizaiDesktopAgent()
    results: dict[str, Any] = {}
    for name, selectors in _variants().items():
        config = deepcopy(base_config)
        config["collectionConfig"]["columnSelectorArray"] = selectors
        params = {
            "dataCollectConfig": {
                "turningPageConfig": {},
                "pageInfo": config["pageInfo"],
                "collectionConfig": config["collectionConfig"],
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
        try:
            response = client.call(
                "DataCollectService",
                "dataCollectCapturePreview",
                [{"jsonParams": json.dumps(params, ensure_ascii=False, separators=(",", ":"))}],
            )
            rows = response.get("data") if isinstance(response, dict) else response
            rows = rows if isinstance(rows, list) else []
            results[name] = {"row_count": max(0, len(rows) - 1), "sample": rows[:5]}
        except Exception as exc:
            results[name] = {"error": str(exc)}
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
