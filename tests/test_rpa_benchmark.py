"""Check benchmark validity and prevent false accuracy/speed passes."""

import csv
import io
import json
from pathlib import Path

import pytest

from benchmarks.rpa.data import inputs, oracle, products
from benchmarks.rpa.fixture_site import render
from scripts import rpa_benchmark as bench


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf8")


def test_pack_oracles_and_reference_non_matches():
    result = bench.validate_pack()
    assert result["cases"] == 34 and result["turns"] == 68
    facts = {"resource": "product_listing", "operation": "collect"}
    assert bench.references({**facts, "site": "jd.com"}) == bench.references({**facts, "site": "another-shop"})
    assert not bench.references({"resource": "text_file", "operation": "collect", "site": "jd.com"})
    assert not bench.references({"resource": "control_flow", "operation": "loop"})


def test_seed_changes_business_values_and_local_oracles():
    assert inputs(27) == inputs(27)
    assert inputs(27)[0]["orders.csv"] != inputs(28)[0]["orders.csv"]
    assert products(27) != products(28)
    assert oracle("table:paid", 27, "http://local") != oracle("table:paid", 28, "http://local")


def test_sales_missing_and_lower_bounds_cannot_be_replaced_by_exact_counts(tmp_path):
    path = tmp_path / "output.csv"
    for key in ("shop_missing:耳机", "shop_bound:耳机"):
        expected = oracle(key, 27, "http://local")
        path.write_bytes(csv_bytes(expected["rows"]))
        assert bench.inspect_output(path, expected) is True
        wrong = [r[:] for r in expected["rows"]]
        wrong[1][3] = "10000"
        wrong[1][4] = "累计销量"
        path.write_bytes(csv_bytes(wrong))
        assert bench.inspect_output(path, expected) is False


def test_pagination_oracle_rejects_first_page_duplicates_and_wrong_column_order(tmp_path):
    expected = oracle("shop:手机", 27, "http://local")
    path = tmp_path / "output.csv"
    for rows in (expected["rows"][:5], expected["rows"] + expected["rows"][1:2],
                 [list(reversed(r)) for r in expected["rows"]]):
        path.write_bytes(csv_bytes(rows))
        assert bench.inspect_output(path, expected) is False
    path.write_bytes(csv_bytes(expected["rows"]))
    assert bench.inspect_output(path, expected) is True


def test_fixture_handles_overlap_more_layout_frames_and_hides_absent_sales():
    phone_ids = [p["id"] for p in products(27) if p["category"] == "手机"]
    second = render("/shop", {"q": "手机", "seed": "27", "overlap": "1", "page": "2"})
    assert phone_ids[3] in second and phone_ids[0] not in second and "下一页" in second
    more = render("/shop", {"q": "手机", "seed": "27", "more": "1", "page": "2"})
    assert all(p in more for p in phone_ids[:8]) and phone_ids[8] not in more
    absent = render("/shop", {"q": "手机", "seed": "27", "metric": "reviews"})
    assert "条评价" in absent and "已售" not in absent
    assert "<article>" in render("/shop", {"q": "耳机", "layout": "cards"})
    assert "<iframe" in render("/frame", {})
    assert "请登录" in render("/gate", {})


def test_native_flow_contract_checks_required_component_and_collection_mode():
    collection_node = {
        "collect": {
            "type": "WebBrowser_DataCollectionV5",
            "args0": [{
                "name": "config",
                "value": json.dumps({
                    "collectionConfig": {
                        "tableSelector": [{"name": "table"}],
                        "columnConfigArray": [{"columnHeader": "商品链接", "dataAttrName": "href"}],
                    }
                }),
            }],
        }
    }
    spec = {"native_flow": {
        "required_components": ["WebBrowser_DataCollectionV5"],
        "forbid_table_href": True,
    }}

    assert bench.native_flow_contract(collection_node, spec) is False
    collection_node["collect"]["args0"][0]["value"] = json.dumps({
        "collectionConfig": {
            "tableSelector": [],
            "columnConfigArray": [{"columnHeader": "商品链接", "dataAttrName": "href"}],
        }
    })
    assert bench.native_flow_contract(collection_node, spec) is True
    collection_node["collect"]["args0"][0]["value"] = json.dumps({
        "collectionConfig": {
            "tableSelector": [{"name": "table"}],
            "columnConfigArray": [{"columnHeader": "商品名称", "dataAttrName": "innerText"}],
        }
    })
    assert bench.native_flow_contract(collection_node, spec) is True
    assert bench.native_flow_contract({}, spec) is False


@pytest.mark.parametrize("config", ["{broken", None, {}, {"collectionConfig": {}}])
def test_native_flow_contract_rejects_unreadable_collection_config(config):
    nodes = {
        "collect": {
            "type": "WebBrowser_DataCollectionV5",
            "args0": [{"name": "config", "value": config}],
        }
    }
    spec = {"native_flow": {
        "required_components": ["WebBrowser_DataCollectionV5"],
        "forbid_table_href": True,
    }}

    assert bench.native_flow_contract(nodes, spec) is False


def test_preparation_keeps_oracles_out_of_model_prompts_and_cannot_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "DATA", tmp_path / "data")
    base = bench.prepare("jd-phones-sales", tmp_path / "base")
    ref = bench.prepare("jd-phones-sales", tmp_path / "ref", mode="reference")
    assert "可选参考" not in bench.read(base["prompts"])[0]
    assert "可选参考" in bench.read(ref["prompts"])[0]
    assert ref["references"] == ["listing-collection"]
    assert not Path(bench.read(base["manifest"])["output_path"]).exists()
    with pytest.raises(ValueError, match="overwrite"):
        bench.prepare("jd-phones-sales", tmp_path / "base")


def synthetic_report(tmp_path, monkeypatch):
    """Synthetic grading fixture only, never reported as native RPA success."""
    monkeypatch.setattr(bench, "DATA", tmp_path / "data")
    prepared = bench.prepare("file-replace", tmp_path / "prepared")
    manifest_path = Path(prepared["manifest"])
    manifest = bench.read(manifest_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    turns = []
    for index, expected in enumerate(manifest["expected"], 1):
        output = report_dir / f"turn-{index}.txt"
        output.write_text(expected["text"], encoding="utf8")
        task = report_dir / f"turn-{index}.task"
        bench.write(task, {"componentModelEntities": {"MAIN_COMPONENT": {"args0": [{"name": "statements", "value": ["write"]}]},
                                                         "write": {"type": "File_Write_V1", "args0": []}}})
        turns.append({"index": index, "status": "completed", "elapsed_seconds": 1,
                      "prompt": bench.read(prepared["prompts"])[index - 1],
                      "items": [{"type": "mcpToolCall", "tool": "shizai_rpa_run", "arguments": {"project_ref": "owned"},
                                 "result": {"structuredContent": {"terminal_status": "success"}}}],
                      "artifacts": [{"path": manifest["output_path"], "snapshot": str(output), "sha256": bench.digest(output), "fresh_for_turn": True}],
                      "flows": [{"project": "owned", "snapshot": str(task), "sha256": bench.digest(task)}]})
    report_path = report_dir / "report.json"
    bench.write(report_path, {"case_name": manifest["case_name"], "model_resolved": "synthetic-fixture", "turns": turns})
    return manifest_path, report_path


def test_fast_correct_files_still_require_review_and_review_is_bound(tmp_path, monkeypatch):
    manifest, report = synthetic_report(tmp_path, monkeypatch)
    result = bench.grade(manifest, report)
    assert result["outcome"] == "needs_review" and not result["fast_and_accurate"]
    review = result["review_template"]
    review.update(checks=dict.fromkeys(review["checks"], True), notes="Synthetic reviewer fixture", evidence=["turn-1.task"])
    path = tmp_path / "review.json"
    bench.write(path, review)
    assert bench.grade(manifest, report, path)["outcome"] == "passed"
    data = bench.read(report)
    data["turns"][0]["elapsed_seconds"] = 2
    bench.write(report, data)
    assert bench.grade(manifest, report, path)["outcome"] == "needs_review"


@pytest.mark.parametrize("failure", ["missing_edit", "stale_artifact", "runtime_failed", "changed_app", "changed_ids", "wrong_text", "changed_input"])
def test_incomplete_or_wrong_results_never_earn_speed_success(tmp_path, monkeypatch, failure):
    manifest, report = synthetic_report(tmp_path, monkeypatch)
    data = bench.read(report)
    turn = data["turns"][1]
    if failure == "missing_edit":
        data["turns"].pop()
    elif failure == "stale_artifact":
        turn["artifacts"][0]["fresh_for_turn"] = False
    elif failure == "runtime_failed":
        turn["items"][0]["result"]["structuredContent"]["terminal_status"] = "error"
    elif failure == "changed_app":
        turn["items"][0]["arguments"]["project_ref"] = "replacement"
        turn["flows"][0]["project"] = "replacement"
    elif failure == "changed_ids":
        task = Path(turn["flows"][0]["snapshot"])
        bench.write(task, {"componentModelEntities": {"replacement": {"type": "File_Write_V1"}}})
        turn["flows"][0]["sha256"] = bench.digest(task)
    elif failure == "wrong_text":
        output = Path(turn["artifacts"][0]["snapshot"])
        output.write_text("wrong", encoding="utf8")
        turn["artifacts"][0]["sha256"] = bench.digest(output)
    else:
        input_file = next(iter(bench.read(manifest)["input_hashes"]))
        Path(input_file).write_text("mutated", encoding="utf8")
    bench.write(report, data)
    result = bench.grade(manifest, report)
    assert result["outcome"] == "failed" and not result["fast_and_accurate"]


def test_failed_trials_are_excluded_from_speed_comparison(tmp_path):
    record = {"case_id": "sample", "seed": 27, "trial": 1, "mode": "baseline", "outcome": "failed",
              "turns": [{"seconds": 1, "tool_calls": 1}]}
    left, right = tmp_path / "left.json", tmp_path / "right.json"
    bench.write(left, record)
    bench.write(right, {**record, "mode": "reference", "outcome": "passed", "turns": [{"seconds": 50, "tool_calls": 10}]})
    group = bench.compare([left, right])["comparisons"][0]
    assert group["paired_trials"] == 1
    assert group["modes"]["baseline"]["median_success_seconds"] is None
    assert group["modes"]["reference"]["median_success_seconds"] == 50


def test_native_benchmark_lock_rejects_concurrent_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ROOT", tmp_path)
    with bench.desktop_lock():
        with pytest.raises(OSError), bench.desktop_lock():
            pass
    with bench.desktop_lock():
        pass
