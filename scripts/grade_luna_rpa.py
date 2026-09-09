"""Independently inspect preserved artifacts; protocol completion is not success."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.luna_rpa_eval import tool_payload  # noqa: E402

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [s.attrib["name"] for s in workbook.findall("s:sheets/s:sheet", NS)]
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in s.findall(".//s:t", NS)) for s in strings]
        cells = {}
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for c in sheet.findall(".//s:sheetData/s:row/s:c", NS):
            value = c.findtext("s:v", default="", namespaces=NS)
            if c.get("t") == "s":
                value = shared[int(value)]
            elif c.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in c.findall(".//s:t", NS))
            cells[c.attrib["r"]] = {"value": value, "formula": c.findtext("s:f", namespaces=NS)}
        return {"sheets": names, "cells": cells}


def inspect_artifact(record: dict, case: str, turn: int) -> dict:
    path = Path(record["snapshot"])
    result = {"path": str(path), "fresh_for_turn": record.get("fresh_for_turn"), "matches_expected": False}
    try:
        if path.suffix.casefold() == ".txt":
            value = path.read_text(encoding="utf-8-sig")
            result["text"] = value
            if case == "file-transform":
                replacement = "已完成" if turn == 1 else "已归档"
                result["matches_expected"] = value == f"订单A：{replacement}\n订单B：已取消\n订单C：{replacement}\n"
            elif case == "native-control-flow":
                result["matches_expected"] = value.splitlines() == (["2", "4", "6"] if turn == 1 else ["1", "3", "5", "7"])
            elif case == "structural-edit":
                result["matches_expected"] = value == "结构编辑测试"
        elif path.suffix.casefold() == ".csv":
            rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            result["rows"] = rows
            if case == "csv-summary":
                expected = {"甲公司": 56, "乙公司": 16} if turn == 1 else {"乙公司": 8, "丙公司": 8}
                result["matches_expected"] = (rows[0] == ["客户", "金额"] and len(rows[1:]) == len(expected)
                    and [r[0] for r in rows[1:]] == sorted(expected)
                    and {r[0]: float(r[1]) for r in rows[1:]} == expected)
            elif case in {"browser-search", "browser-pagination"}:
                if case == "browser-pagination":
                    prefix, city, amounts = "HZ", "杭州", [12, 24, 36, 48, 60, 72, 84]
                elif turn == 1:
                    prefix, city, amounts = "SH", "上海", [30, 60, 90]
                else:
                    prefix, city, amounts = "BJ", "北京", [41, 82]
                swapped = case == "browser-pagination" and turn == 2
                header = ["订单号", "金额", "城市"] if swapped else ["订单号", "城市", "金额"]
                expected_rows = [[f"{prefix}-{i:03d}", city, amount] for i, amount in enumerate(amounts, 1)]
                normalized = [[r[0], r[2] if swapped else r[1], float(r[1] if swapped else r[2])] for r in rows[1:]]
                result["matches_expected"] = rows[0] == header and normalized == expected_rows
        elif path.suffix.casefold() == ".xlsx":
            book = read_xlsx(path)
            result["workbook"] = book
            cells = book["cells"]
            expected = {"A1": "商品", "B1": "数量", "C1": "单价", "D1": "金额",
                        "A2": "铅笔", "B2": "3", "C2": "2", "A3": "本子", "B3": "2" if turn == 1 else "5",
                        "C3": "8", "A4": "书包", "B4": "1", "C4": "50"}
            formulas = {"D2": "B2*C2", "D3": "B3*C3", "D4": "B4*C4", "D5": "SUM(D2:D4)"}
            result["matches_expected"] = (book["sheets"] == ["订单" if turn == 1 else "采购订单"]
                and all(cells.get(k, {}).get("value") == v for k, v in expected.items())
                and all(str(cells.get(k, {}).get("formula", "")).upper().replace("$", "").replace(" ", "").lstrip("=") == v
                        for k, v in formulas.items())
                and float(cells.get("D5", {}).get("value") or "nan") == (72 if turn == 1 else 96))
    except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError) as exc:
        result["inspection_error"] = f"{type(exc).__name__}: {exc}"
    return result


def summarize(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    folder = report_path.parent.name
    case = "excel-formulas" if folder.startswith("excel-") else folder.rsplit("-", 1)[0]
    result = {"case": case, "attempt": folder, "thread_id": report.get("thread_id"),
              "model": report.get("model_resolved"), "profile": report.get("profile"), "turns": []}
    for turn in report.get("turns", []):
        calls = [i for i in turn.get("items", []) if i.get("type") == "mcpToolCall"]
        created, proposed, runtime, issues = [], set(), [], []
        for item in calls:
            tool = item.get("tool", "")
            payload = tool_payload(item)
            arguments = item.get("arguments") or {}
            for step in (arguments.get("plan") or {}).get("steps", []):
                if step.get("component"):
                    proposed.add(step["component"])
            if tool == "shizai_rpa_start_design" and (payload.get("application") or {}).get("created"):
                created.append(payload["application"]["local_path"])
            if tool in {"shizai_rpa_run", "shizai_rpa_status", "shizai_rpa_stop"}:
                status = payload.get("status")
                runtime.append({"tool": tool, "project": arguments.get("project_ref"),
                                "status": status.get("status") if isinstance(status, dict) else status,
                                "terminal_status": payload.get("terminal_status"),
                                "timed_out": payload.get("timed_out")})
            if item.get("status") == "failed" or payload.get("error"):
                issues.append({"tool": tool, "error": str(payload.get("error") or item.get("result"))[:1000]})
        artifacts = [inspect_artifact(a, case, turn["index"]) for a in turn.get("artifacts", []) if a.get("snapshot")]
        flows = []
        for flow in turn.get("flows", []):
            data = json.loads(Path(flow["snapshot"]).read_text(encoding="utf-8"))
            nodes = data.get("componentModelEntities", {})
            main = nodes.get("MAIN_COMPONENT", {})
            order = next((a["value"] for a in main.get("args0", []) if a.get("name") == "statements"), [])
            flows.append({"project": flow["project"], "snapshot": flow["snapshot"], "order": order,
                          "nodes": [{"id": k, "type": v.get("type")} for k, v in nodes.items() if k != "MAIN_COMPONENT"]})
        result["turns"].append({"index": turn["index"], "protocol_status": turn["status"],
            "elapsed_seconds": turn.get("elapsed_seconds"), "metrics": turn.get("metrics"),
            "created_projects": created, "proposed_components": sorted(proposed), "flows": flows,
            "runtime": runtime, "tool_errors": issues, "artifacts": artifacts,
            "all_observed_artifacts_match": bool(artifacts) and all(a["matches_expected"] and a["fresh_for_turn"] for a in artifacts),
            "business_grade": "requires_review_of_runtime_structure_and_missing_artifacts",
            "final_message": next((i.get("text") for i in reversed(turn.get("items", [])) if i.get("type") == "agentMessage"), None)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "_tmp/luna-eval/expanded")
    args = parser.parse_args()
    result = {"note": "Artifact checks are independent of model claims; workflow structure and runtime still require review.",
              "cases": [summarize(p) for p in sorted(args.root.glob("*/report.json"))]}
    target = args.root / "artifact-verification.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
