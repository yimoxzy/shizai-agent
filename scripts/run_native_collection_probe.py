"""Prepare and execute one native DataCollectionV5 step through MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from shizai_agent.core import project as project_core
from shizai_agent.core.cloud_api import ShizaiDesktopAgent
from shizai_agent.core.native_ui import call_active_editor_renderer
from shizai_agent.core.paths import ShizaiPaths

try:
    from scripts.probe_designer_reload import probe_designer_reload
except ModuleNotFoundError:  # Direct script execution.
    from probe_designer_reload import probe_designer_reload

ROOT = Path(__file__).resolve().parents[1]


def _payload(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", [])
    raw = getattr(content[0], "text", None) if content else None
    if not isinstance(raw, str):
        raise RuntimeError("MCP tool did not return JSON text")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP tool returned non-JSON text: {raw[:1000]}") from exc
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool failed: {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool returned a non-object payload")
    return payload


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    payload = _payload(await session.call_tool(name, arguments))
    payload["probe_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return payload


def _server() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "shizai_agent.core.mcp_server"],
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )


async def prepare(args: argparse.Namespace) -> dict[str, Any]:
    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            before = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": args.flow_ref, "detail": "edit"},
            )
            before_flow = before.get("flow") if isinstance(before.get("flow"), dict) else before
            steps = before_flow.get("steps") if isinstance(before_flow.get("steps"), list) else []
            by_id = {item.get("id"): item for item in steps if isinstance(item, dict)}
            collection_context = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": args.flow_ref, "detail": "edit", "step_id": args.collection_step},
            )
            expected = {
                args.collection_step: "WebBrowser_DataCollectionV5",
                args.input_step: "Element_SetValue_V6",
                args.enter_step: "Keyboard_PressNew",
            }
            for step_id, component in expected.items():
                current = by_id.get(step_id)
                if not current or current.get("component") != component:
                    raise RuntimeError(f"required step {step_id} is not {component}")

            changes: list[dict[str, Any]] = []
            if args.wait_step not in by_id:
                changes.append(
                    await _call(
                        session,
                        "shizai_rpa_add_step",
                        {
                            "flow_ref": args.flow_ref,
                            "component": "Basic_Delay_V1",
                            "step_id": args.wait_step,
                            "before_step_id": args.collection_step,
                            "label": "等待京东结果页加载",
                            "config": {"second": args.wait_seconds},
                            "allow_write": True,
                        },
                    )
                )
            elif by_id[args.wait_step].get("component") != "Basic_Delay_V1":
                raise RuntimeError(f"step id {args.wait_step} is already used")

            if args.refresh_step not in by_id:
                changes.append(
                    await _call(
                        session,
                        "shizai_rpa_add_step",
                        {
                            "flow_ref": args.flow_ref,
                            "component": "WebBrowser_GetTAB_V1",
                            "step_id": args.refresh_step,
                            "before_step_id": args.collection_step,
                            "label": "重新获取京东结果页标签",
                            "config": {
                                "var_name": "网页对象",
                                "type": "chrome",
                                "match_type": 2,
                                "title": "",
                                "url": "",
                                "isActive": 1,
                            },
                            "allow_write": True,
                        },
                    )
                )
            elif by_id[args.refresh_step].get("component") != "WebBrowser_GetTAB_V1":
                raise RuntimeError(f"step id {args.refresh_step} is already used")

            selected_collection = (
                collection_context.get("selected_step")
                if isinstance(collection_context.get("selected_step"), dict)
                else {}
            )
            collection_element_ids = [
                str(item).strip()
                for item in (selected_collection.get("element_ids") or [])
                if str(item).strip()
            ]
            if not collection_element_ids:
                raise RuntimeError(
                    "the collection step has no captured product field to scope the reference workflow; "
                    f"read_keys={sorted(before)}, flow_keys={sorted(before_flow)}, "
                    f"collection_step={selected_collection!r}"
                )
            if args.preprocess_step not in by_id:
                changes.append(
                    await _call(
                        session,
                        "shizai_rpa_add_step",
                        {
                            "flow_ref": args.flow_ref,
                            "component": "WebBrowser_RunJsInIeBySelector_v3",
                            "step_id": args.preprocess_step,
                            "before_step_id": args.collection_step,
                            "label": "生成商品采集字段",
                            "element_ids": [collection_element_ids[0]],
                            "config": {
                                "workflow_reference": "jd_product_card_link_v1",
                                "input": [".plugin_goodsCardWrapper", ".gl-item"],
                                "output_variable": "商品字段预处理结果",
                                "result_mode": "generated_markers",
                                "page_scope": True,
                            },
                            "allow_write": True,
                        },
                    )
                )
            elif by_id[args.preprocess_step].get("component") != "WebBrowser_RunJsInIeBySelector_v3":
                raise RuntimeError(f"step id {args.preprocess_step} is already used")

            changes.append(
                await _call(
                    session,
                    "shizai_rpa_set_step_elements",
                    {
                        "flow_ref": args.flow_ref,
                        "step_id": args.collection_step,
                        "element_ids": [
                            project_core.JS_NAME_ELEMENT_ID,
                            project_core.JS_PRICE_ELEMENT_ID,
                            project_core.JS_SALES_ELEMENT_ID,
                        ],
                        "allow_write": True,
                    },
                )
            )

            collection_config = {
                "var0": "数据采集结果",
                "var1": "Excel路径",
                "var2": "监控空列数据",
                "columns": ["商品名称", "价格", "热度原文"],
                "data_attributes": {
                    "商品名称": "innerText",
                    "价格": "innerText",
                    "热度原文": "innerText",
                },
                "designated_info": 1,
                "designated_obj": "网页对象",
                "region": 1,
                "toExcel": 0,
            }
            preview = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": args.flow_ref,
                    "step_id": args.collection_step,
                    "config": collection_config,
                    "dry_run": True,
                },
            )
            updated = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": args.flow_ref,
                    "step_id": args.collection_step,
                    "config": collection_config,
                    "expected_revision": preview["revision"],
                    "allow_write": True,
                },
            )
            after = await _call(
                session,
                "shizai_rpa_read_flow",
                {"flow_ref": args.flow_ref, "detail": "edit"},
            )
            return {
                "server": {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                },
                "before": before,
                "added": changes,
                "update_preview": preview,
                "updated": updated,
                "after": after,
            }


def run_components(flow_ref: str, step_ids: list[str]) -> dict[str, Any]:
    paths = ShizaiPaths.from_environment()
    project_dir = project_core.resolve_project(paths, flow_ref)
    package = project_core._native_package_summary(paths, project_dir)
    edit_state = json.loads((project_dir / "edit-state.json").read_text(encoding="utf-8"))
    editor_id = str(edit_state.get("currentEditorId") or "").strip()
    application_id = str(package.get("application_id") or "").strip()
    if not editor_id or not application_id:
        raise RuntimeError("active editor identity is incomplete")

    request = {
        "projectId": application_id,
        "editorId": editor_id,
        "componentIds": step_ids,
        "isIPA": False,
        "isUseTrigger": False,
        "useCache": True,
        "createCodeParams": {"useCache": True},
    }
    client = ShizaiDesktopAgent()
    editor_ready = call_active_editor_renderer(client, "WorkbenchService", "isNeedSave", [])
    started = time.monotonic()
    result = call_active_editor_renderer(client, "RunService", "runComponentInEditor", [request])
    elapsed = round(time.monotonic() - started, 3)
    code_path = project_dir / "projects" / "_temp" / "component_run.py"
    variable_cache = project_dir / ".sz" / "variable_cache" / "io5cd.json"
    return {
        "step_ids": step_ids,
        "editor_ready": editor_ready,
        "result": result,
        "elapsed_seconds": elapsed,
        "component_code_path": str(code_path),
        "component_code": code_path.read_text(encoding="utf-8") if code_path.is_file() else "",
        "variable_cache_path": str(variable_cache),
        "variable_cache": json.loads(variable_cache.read_text(encoding="utf-8"))
        if variable_cache.is_file()
        else None,
    }


async def validate_collection(flow_ref: str, step_id: str) -> dict[str, Any]:
    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            return await _call(
                session,
                "shizai_rpa_validate_step",
                {"flow_ref": flow_ref, "step_id": step_id, "strict": False},
            )


def collection_preview_quality(preview: Any) -> dict[str, Any]:
    if not isinstance(preview, dict) or not preview.get("valid"):
        return {"valid": False, "reason": str(preview.get("failure_kind") or "preview_invalid") if isinstance(preview, dict) else "preview_missing"}
    rows = [row for row in preview.get("sample_rows", []) if isinstance(row, list) and len(row) >= 3]
    if not rows:
        return {"valid": False, "reason": "sample_rows_missing"}
    checks = []
    heat_pattern = re.compile(r"(?:评价|好评|人看过|加购|已售|销量|成交|付款)")
    for row in rows:
        name, price, heat = (str(value).strip() for value in row[:3])
        checks.append(
            {
                "name_like_product": len(name) >= 4 and not re.fullmatch(r"[\d.,¥￥\s]+", name),
                "price_like_number": bool(re.search(r"\d", price)) and len(price) <= 40,
                "heat_has_explicit_metric": bool(heat_pattern.search(heat)),
                "columns_distinct": len({name, price, heat}) == 3,
            }
        )
    valid = all(all(item.values()) for item in checks)
    return {"valid": valid, "reason": None if valid else "semantic_sample_mismatch", "checks": checks}


async def sync_collection_page_identity(
    flow_ref: str,
    collection_step: str,
    refresh_step: str,
) -> dict[str, Any]:
    project_dir = project_core.resolve_project(ShizaiPaths.from_environment(), flow_ref)
    cache_path = project_dir / ".sz" / "variable_cache" / "io5cd.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    refresh = cache.get(refresh_step) if isinstance(cache, dict) else None
    result = refresh.get("result") if isinstance(refresh, dict) else None
    page_url = str(result.get("url") or "").strip() if isinstance(result, dict) else ""
    page_title = str(result.get("title") or "").strip() if isinstance(result, dict) else ""
    if not page_url or not page_title:
        raise RuntimeError("GetTAB variable cache does not contain the current page URL and title")

    config = {"page_url": page_url, "page_title": page_title}
    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            preview = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": flow_ref,
                    "step_id": collection_step,
                    "config": config,
                    "dry_run": True,
                },
            )
            updated = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": flow_ref,
                    "step_id": collection_step,
                    "config": config,
                    "expected_revision": preview["revision"],
                    "allow_write": True,
                },
            )
    return {"page_url": page_url, "page_title": page_title, "preview": preview, "updated": updated}


def _select_adjacent_product_fields(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    heat_tokens = ("已售", "销量", "付款", "评价", "人看过", "加购")

    def order(item: dict[str, Any]) -> int:
        return int(item.get("scan_order") or 0)

    heat = next(
        (
            item
            for item in candidates
            if any(token in str(item.get("text") or "") for token in heat_tokens)
            and order(item) >= 100
        ),
        None,
    )
    if heat is None:
        raise RuntimeError("the current result page exposes no sales, review, view, or cart heat text")
    heat_order = order(heat)
    names = [
        item
        for item in candidates
        if "product_name" in (item.get("semantic_fields") or [])
        and not any(token in str(item.get("text") or "") for token in heat_tokens)
        and 0 < heat_order - order(item) <= 30
    ]
    prices = [
        item
        for item in candidates
        if "price" in (item.get("semantic_fields") or [])
        and 0 < order(item) - heat_order <= 30
    ]
    if not names or not prices:
        raise RuntimeError("could not align a product name and price around the selected heat field")
    return {
        "product_name": min(names, key=lambda item: heat_order - order(item)),
        "price": min(prices, key=lambda item: order(item) - heat_order),
        "heat": heat,
    }


async def recapture_live_collection_fields(
    flow_ref: str,
    collection_step: str,
    refresh_step: str,
) -> dict[str, Any]:
    project_dir = project_core.resolve_project(ShizaiPaths.from_environment(), flow_ref)
    cache_path = project_dir / ".sz" / "variable_cache" / "io5cd.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    refresh = cache.get(refresh_step) if isinstance(cache, dict) else None
    browser_result = refresh.get("result") if isinstance(refresh, dict) else None
    page_url = str(browser_result.get("url") or "").strip() if isinstance(browser_result, dict) else ""
    page_title = str(browser_result.get("title") or "").strip() if isinstance(browser_result, dict) else ""

    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            scan = await _call(
                session,
                "shizai_rpa_scan_page",
                {
                    "project_ref": flow_ref,
                    "query": "商品名称 价格 评价 加购 热度",
                    "level": 10,
                    "max_nodes": 300,
                },
            )
            candidates = [item for item in scan.get("candidates", []) if isinstance(item, dict)]
            selected = _select_adjacent_product_fields(candidates)
            selected_ids = [
                str(selected[name].get("element_id") or "").strip()
                for name in ("product_name", "price", "heat")
            ]
            captured = await _call(
                session,
                "shizai_rpa_capture_elements",
                {
                    "element_ids": selected_ids,
                    "scan_token": scan.get("scan_token"),
                    "page_title": page_title,
                    "page_url": page_url,
                    "project_ref": flow_ref,
                    "step_id": collection_step,
                    "allow_rebind": True,
                    "allow_write": True,
                },
            )
            config = {
                "var0": "数据采集结果",
                "var1": "Excel路径",
                "var2": "监控空列数据",
                "columns": ["商品名称", "价格", "热度原文"],
                "data_attributes": {
                    "商品名称": "innerText",
                    "价格": "innerText",
                    "热度原文": "innerText",
                },
                "page_url": page_url,
                "page_title": page_title,
            }
            update_preview = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": flow_ref,
                    "step_id": collection_step,
                    "config": config,
                    "dry_run": True,
                },
            )
            updated = await _call(
                session,
                "shizai_rpa_update_step",
                {
                    "flow_ref": flow_ref,
                    "step_id": collection_step,
                    "config": config,
                    "expected_revision": update_preview["revision"],
                    "allow_write": True,
                },
            )
            validation = await _call(
                session,
                "shizai_rpa_validate_step",
                {"flow_ref": flow_ref, "step_id": collection_step, "strict": False},
            )
            return {
                "selected": {
                    name: {
                        "element_id": item.get("element_id"),
                        "text": item.get("text"),
                        "scan_order": item.get("scan_order"),
                    }
                    for name, item in selected.items()
                },
                "scan_token": scan.get("scan_token"),
                "captured": captured,
                "updated": updated,
                "validation": validation,
            }


async def cleanup(flow_ref: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            for name in ("shizai_rpa_debug_stop", "shizai_rpa_browser_session_stop"):
                try:
                    results[name] = await _call(
                        session,
                        name,
                        {"project_ref": flow_ref, "allow_write": True},
                    )
                except Exception as exc:
                    results[name] = {"error": str(exc)}
    return results


async def release_capture_session(flow_ref: str) -> dict[str, Any]:
    async with stdio_client(_server()) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            return await _call(
                session,
                "shizai_rpa_browser_session_stop",
                {"project_ref": flow_ref, "allow_write": True},
            )


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    prepared = await prepare(args)
    revision = prepared["after"].get("revision")
    reload_result = probe_designer_reload(
        ShizaiPaths.from_environment(),
        args.flow_ref,
        expected_revision=revision,
        allow_reload=True,
        strategy="active-editor-reload",
    )
    time.sleep(args.editor_wait)
    navigation = run_components(
        args.flow_ref,
        [args.open_step, args.input_step, args.enter_step, args.wait_step, args.refresh_step],
    )
    page_identity = await sync_collection_page_identity(
        args.flow_ref,
        args.collection_step,
        args.refresh_step,
    )
    preprocessing_run = run_components(args.flow_ref, [args.preprocess_step])
    capture_session_release = await release_capture_session(args.flow_ref)
    validation_attempts = [await validate_collection(args.flow_ref, args.collection_step)]
    for _ in range(2):
        native_preview = validation_attempts[-1].get("native_collection_preview")
        if isinstance(native_preview, dict) and native_preview.get("valid"):
            break
        if not isinstance(native_preview, dict) or native_preview.get("failure_kind") not in {
            "empty_preview",
            "column_collapse",
        }:
            break
        time.sleep(1.5)
        validation_attempts.append(await validate_collection(args.flow_ref, args.collection_step))
    validation = validation_attempts[-1]
    native_preview = validation.get("native_collection_preview")
    preview_quality = collection_preview_quality(native_preview)
    preview_valid = bool(preview_quality.get("valid"))
    collection_run = run_components(args.flow_ref, [args.collection_step]) if preview_valid else None
    cleaned = await cleanup(args.flow_ref)
    return {
        "kind": "native_collection_component_probe",
        "prepared": prepared,
        "reload": reload_result,
        "navigation_run": navigation,
        "page_identity": page_identity,
        "preprocessing_run": preprocessing_run,
        "capture_session_release": capture_session_release,
        "validation_attempts": validation_attempts,
        "validation": validation,
        "preview_quality": preview_quality,
        "collection_run": collection_run,
        "cleanup": cleaned,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow_ref")
    parser.add_argument("--open-step", default="ybQEw")
    parser.add_argument("--input-step", default="input-jd-phone")
    parser.add_argument("--enter-step", default="press-enter-search")
    parser.add_argument("--wait-step", default="wait-jd-results")
    parser.add_argument("--refresh-step", default="refresh-jd-result-tab")
    parser.add_argument("--preprocess-step", default="prepare-jd-product-fields")
    parser.add_argument("--collection-step", default="collect-jd-phones")
    parser.add_argument("--wait-seconds", type=float, default=3.0)
    parser.add_argument("--editor-wait", type=float, default=4.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(execute(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(args.output or "native collection probe complete")


if __name__ == "__main__":
    main()
