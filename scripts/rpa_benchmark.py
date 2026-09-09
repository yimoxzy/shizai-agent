"""Prepare, run and grade the independent RPA component benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "benchmarks/rpa"
DATA = ROOT / "_tmp/luna-eval/data"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.rpa.data import inputs, oracle  # noqa: E402
from scripts.grade_luna_rpa import read_xlsx  # noqa: E402
from scripts.luna_rpa_eval import tool_payload  # noqa: E402


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def catalog():
    return read(PACK / "cases.json")["cases"]


def references(facts):
    """Match explicit task facts; no URL/brand/keyword router and no execution."""
    return [p for p in read(PACK / "presets.json")["workflows"]
            if all(facts.get(k) in values for k, values in p["match"].items())]


def local_url(base_url, entry, seed, **extra):
    parsed = urlsplit(base_url.rstrip("/") + entry)
    query = {**dict(parse_qsl(parsed.query)), "seed": str(seed), **extra}
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def validate_pack():
    cases = catalog()
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case ids")
    for case in cases:
        if len(case["turns"]) != 2 or not case["capabilities"]:
            raise ValueError(f"Missing coverage/edit turn: {case['id']}")
        for turn in case["turns"]:
            if turn["budget_seconds"] <= 0 or turn["budget_calls"] <= 0:
                raise ValueError("Budgets must be positive")
            oracle(turn["oracle"], 27, "http://127.0.0.1:18766")
        references(case["facts"])
    return {"valid": True, "cases": len(cases), "turns": sum(len(c["turns"]) for c in cases),
            "groups": dict(Counter(c["group"] for c in cases)),
            "capabilities": sorted({cap for c in cases for cap in c["capabilities"]})}


def prepare(case_id, destination, mode="baseline", seed=27, trial=1, base_url="http://127.0.0.1:18766"):
    case = next(c for c in catalog() if c["id"] == case_id)
    destination = Path(destination).resolve()
    if (destination / "manifest.json").exists():
        raise ValueError("Refusing to overwrite a prepared trial; use a new directory")
    destination.mkdir(parents=True, exist_ok=True)
    run_id = hashlib.sha256(str(destination).encode()).hexdigest()[:8]
    case_name = f"Luna评测-组件-{case_id}-{mode}-{run_id}"
    input_dir = DATA / "inputs" / case_name
    input_dir.mkdir(parents=True, exist_ok=True)
    input_hashes = {}
    for name, content in inputs(seed)[0].items():
        target = input_dir / name
        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"Existing input changed: {target}")
        target.write_bytes(content)
        input_hashes[str(target)] = digest(target)
    output_path = DATA / (case_name + case["extension"])
    entry = case["entry"] if case["environment"] == "live" else local_url(base_url, case["entry"] or "/shop", seed)
    replacements = {"entry": entry, "cards_entry": local_url(base_url, "/shop", seed, layout="cards"),
                    "output": str(output_path), "input_dir": str(input_dir), "case_name": case_name}
    selected = references(case["facts"]) if mode == "reference" else []
    context = (
        f"独立 RPA 测评约定：应用名为 {case_name}。通过已挂载 MCP 创建、保存和执行原生流程，"
        "后续回合编辑本会话同一应用。先评估实时原生组件的适用性，合适时优先使用；"
        "短代码只在有必要性证据时采用，不追求组件数量。原生循环/结构要求不能用脚本或重建应用冒充。"
        "不得读取 benchmarks、其他测评产物、manifest、评分答案或项目源码；可以读取指定业务输入和只读核验实际输出。"
        "不得用宿主 shell/Python/HTTP 绕过 RPA 完成业务操作。除非任务只要求静态生成，否则保存、校验后运行并核对产物；"
        "阻塞时如实说明，不能伪造成功。遇到指标口径不明确先以当前页面证据说明，不用另一指标冒充。"
    )
    if selected:
        context += "\n以下是可选参考，不是可直接执行的计划；重新核对适用性，失配就不用：\n" + json.dumps(selected, ensure_ascii=False)
    prompts = [t["request"].format_map(replacements) for t in case["turns"]]
    prompts[0] = context + "\n\n用户请求：" + prompts[0]
    write(destination / "prompts.json", prompts)
    manifest = {"schema": "rpa-benchmark-trial/v1", "case": case, "case_name": case_name, "mode": mode,
                "seed": seed, "trial": trial, "base_url": base_url, "output_path": str(output_path),
                "input_hashes": input_hashes, "reference_ids": [p["id"] for p in selected],
                "reference_selection": "explicit benchmark facts; not production natural-language retrieval",
                "prompts_sha256": digest(destination / "prompts.json"),
                "pack_sha256": {name: digest(PACK / name) for name in ("cases.json", "presets.json", "rubric.json", "data.py", "fixture_site.py")},
                "expected": [oracle(t["oracle"], seed, base_url) for t in case["turns"]]}
    write(destination / "manifest.json", manifest)
    return {"manifest": str(destination / "manifest.json"), "prompts": str(destination / "prompts.json"),
            "case_name": case_name, "references": manifest["reference_ids"], "entry": entry,
            "note": "Only inputs and prompts prepared; no business output or model run produced."}


def inspect_output(path, expected):
    """Only inspect artifacts; never repair or manufacture a business result."""
    try:
        raw = Path(path).read_bytes()
        kind = expected["kind"]
        if kind == "text":
            actual = raw.decode("utf-8-sig").replace("\r\n", "\n")
            return actual == expected["text"] and not (expected.get("forbid_bom") and raw.startswith(b"\xef\xbb\xbf"))
        if kind in {"csv", "manual"}:
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
            if kind == "manual":
                return None if rows else False
            wanted = expected["rows"]
            if len(rows) != len(wanted) or not rows or rows[0] != wanted[0]:
                return False
            for actual, target in zip(rows[1:], wanted[1:]):
                if len(actual) != len(target):
                    return False
                for i, (a, b) in enumerate(zip(actual, target)):
                    if i in expected.get("numeric_columns", []):
                        if float(a) != float(b):
                            return False
                    elif a != b:
                        return False
            return True
        book = read_xlsx(Path(path))
        if book["sheets"] != expected["sheets"]:
            return False
        for cell, value in expected["cells"].items():
            actual = book["cells"].get(cell, {}).get("value", "")
            if isinstance(value, (int, float)):
                if float(actual) != value:
                    return False
            elif actual != value:
                return False
        for cell, value in expected.get("formulas", {}).items():
            formula = book["cells"].get(cell, {}).get("formula") or ""
            if formula.upper().replace(" ", "").replace("$", "").lstrip("=") != value:
                return False
        return True
    except (OSError, ValueError, KeyError, IndexError, UnicodeError, zipfile.BadZipFile, ET.ParseError):
        return False


def flow_evidence(turn, project, report_dir):
    for flow in turn.get("flows", []):
        if flow.get("project") != project:
            continue
        path = Path(flow["snapshot"]).resolve()
        if not path.is_relative_to(report_dir) or not path.is_file() or digest(path) != flow.get("sha256"):
            raise ValueError("Task snapshot escaped the report or its hash changed")
        data = read(path).get("componentModelEntities", {})
        nodes = {k: v for k, v in data.items() if k != "MAIN_COMPONENT"}
        order = next((a["value"] for a in data.get("MAIN_COMPONENT", {}).get("args0", []) if a.get("name") == "statements"), [])
        return nodes, order
    return {}, []


def grade(manifest_path, report_path, review_path=None):
    manifest_path, report_path = Path(manifest_path).resolve(), Path(report_path).resolve()
    manifest, report = read(manifest_path), read(report_path)
    case = manifest["case"]
    binding = report.get("case_name") == manifest["case_name"] and digest(manifest_path.parent / "prompts.json") == manifest["prompts_sha256"]
    prompts = read(manifest_path.parent / "prompts.json")
    records, projects, previous_nodes, previous_order = [], [], {}, []
    for index, spec in enumerate(case["turns"]):
        turn = next((t for t in report.get("turns", []) if t.get("index") == index + 1), {})
        calls = [i for i in turn.get("items", []) if i.get("type") == "mcpToolCall"]
        runs = [i for i in calls if i.get("tool") == "shizai_rpa_run"]
        last_run = runs[-1] if runs else {}
        payload = tool_payload(last_run)
        project = (last_run.get("arguments") or {}).get("project_ref")
        projects.append(project)
        nodes, order = flow_evidence(turn, project, report_path.parent) if project else ({}, [])
        artifact = next((a for a in turn.get("artifacts", []) if Path(a["path"]).resolve() == Path(manifest["output_path"]).resolve()), None)
        valid_file = False
        if artifact and artifact.get("snapshot") and artifact.get("fresh_for_turn") is True:
            snapshot = Path(artifact["snapshot"]).resolve()
            if snapshot.is_relative_to(report_path.parent) and snapshot.is_file() and digest(snapshot) == artifact.get("sha256"):
                valid_file = inspect_output(snapshot, manifest["expected"][index])
        same_graph = index == 0 or (project is not None and project == projects[0])
        if index and case["edit_mode"] == "parameters":
            same_graph = same_graph and set(nodes) == set(previous_nodes) and order == previous_order
            same_graph = same_graph and all(nodes[k].get("type") == previous_nodes[k].get("type") for k in nodes if k in previous_nodes)
        queries = [json.dumps(i.get("arguments"), sort_keys=True, ensure_ascii=False) for i in calls if i.get("tool") == "shizai_rpa_component_catalog"]
        script_nodes = [k for k, n in nodes.items() if re.search(r"runjs|python|script|executecode", n.get("type", ""), re.I)]
        checks = {"bound_prompt": binding and turn.get("prompt") == prompts[index], "turn_completed": turn.get("status") == "completed",
                  "runtime_success": payload.get("terminal_status") == "success", "native_task_present": bool(nodes),
                  "artifact_correct": valid_file, "same_app_and_required_graph": bool(same_graph)}
        records.append({"index": index + 1, "checks": checks, "seconds": turn.get("elapsed_seconds"), "tool_calls": len(calls),
                        "catalog_calls": len(queries), "repeated_catalog_calls": len(queries) - len(set(queries)),
                        "created_apps": sum(bool((tool_payload(i).get("application") or {}).get("created")) for i in calls if i.get("tool") == "shizai_rpa_start_design"),
                        "component_types": sorted({n.get("type", "") for n in nodes.values()}), "native_node_count": len(nodes),
                        "suspected_script_nodes": script_nodes,
                        "within_speed_target": isinstance(turn.get("elapsed_seconds"), (int, float)) and turn["elapsed_seconds"] <= spec["budget_seconds"] and len(calls) <= spec["budget_calls"]})
        previous_nodes, previous_order = nodes, order
    input_unchanged = all(Path(p).is_file() and digest(p) == sha for p, sha in manifest["input_hashes"].items())
    required = read(PACK / "rubric.json")["manual_checks"]
    review = read(review_path) if review_path else {}
    review_bound = review.get("report_sha256") == digest(report_path) and review.get("manifest_sha256") == digest(manifest_path)
    evidence = review.get("evidence", [])
    valid_review = bool(review_bound and review.get("notes") and evidence and
                        all((report_path.parent / p).resolve().is_relative_to(report_path.parent) and
                            (report_path.parent / p).is_file() for p in evidence) and
                        all(review.get("checks", {}).get(k) is True for k in required))
    failed = not input_unchanged or any(v is False for r in records for v in r["checks"].values())
    outcome = "failed" if failed else "passed" if valid_review else "needs_review"
    # Live/semantic correctness (None) is adjudicated only by the bound review.
    versions = sorted({((tool_payload(i).get("application") or {}).get("factory_version", ""),
                        (tool_payload(i).get("application") or {}).get("runtime_version", ""))
                       for t in report.get("turns", []) for i in t.get("items", [])
                       if i.get("tool") == "shizai_rpa_start_design"})
    return {"case_id": case["id"], "mode": manifest["mode"], "seed": manifest["seed"], "trial": manifest["trial"],
            "model": report.get("model_resolved"), "provider": report.get("model_provider"), "profile": report.get("profile"),
            "implementation_sha256": report.get("implementation_sha256"), "pack_sha256": manifest["pack_sha256"],
            "environment": {"base_url": manifest["base_url"], "desktop_versions": versions},
            "outcome": outcome, "input_unchanged": input_unchanged, "manual_review_accepted": valid_review,
            "fast_and_accurate": outcome == "passed" and all(r["within_speed_target"] for r in records), "turns": records,
            "review_template": {"report_sha256": digest(report_path), "manifest_sha256": digest(manifest_path),
                                "checks": dict.fromkeys(required, None), "notes": "", "evidence": []}}


@contextmanager
def desktop_lock():
    """Serialize this harness across processes; the OS releases locks on exit."""
    target = ROOT / "_tmp/rpa-benchmark/native-desktop.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_trial(manifest_path, codex, model, profile, timeout):
    with desktop_lock():
        return _run_trial(manifest_path, codex, model, profile, timeout)


def _run_trial(manifest_path, codex, model, profile, timeout):
    from scripts.luna_rpa_suite import cleanup

    manifest_path = Path(manifest_path).resolve()
    manifest = read(manifest_path)
    folder = manifest_path.parent
    if digest(folder / "prompts.json") != manifest["prompts_sha256"]:
        raise ValueError("Prepared prompts changed")
    if not all(Path(p).is_file() and digest(p) == sha for p, sha in manifest["input_hashes"].items()):
        raise ValueError("Prepared inputs changed")
    output = folder / "run"
    if output.exists():
        raise ValueError("Trial already started; prepare a new trial")
    output.mkdir()
    command = [sys.executable, str(ROOT / "scripts/luna_rpa_eval.py"), "--codex", codex, "--model", model,
               "--profile", profile, "--case-name", manifest["case_name"], "--prompts", str(folder / "prompts.json"),
               "--output", str(output), "--timeout", str(timeout)]
    write(folder / "execution.json", {"manifest_sha256": digest(manifest_path), "model": model, "profile": profile,
                                       "timeout_per_turn": timeout, "command": command})
    try:
        with (output / "runner.stdout.log").open("w", encoding="utf8") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
    finally:
        if (output / "report.json").exists():
            write(folder / "cleanup.json", cleanup(output / "report.json"))
    if result.returncode:
        raise RuntimeError(f"Runner failed; inspect {output}")
    graded = grade(manifest_path, output / "report.json")
    write(folder / "grade.json", graded)
    write(folder / "review.json", graded["review_template"])
    return {"report": str(output / "report.json"), "grade": str(folder / "grade.json"), "outcome": graded["outcome"]}


def compare(paths):
    groups = defaultdict(list)
    for path in paths:
        value = read(path)
        key = (value["case_id"], value.get("model"), value.get("provider"), value.get("profile"),
               json.dumps(value.get("implementation_sha256"), sort_keys=True), json.dumps(value.get("pack_sha256"), sort_keys=True),
               json.dumps(value.get("environment"), sort_keys=True))
        groups[key].append(value)
    results = []
    for key, values in groups.items():
        entry = {"case_id": key[0], "model": key[1], "provider": key[2], "profile": key[3], "modes": {}}
        pairs = defaultdict(dict)
        for v in values:
            pair = pairs[(v["seed"], v["trial"])]
            if v["mode"] in pair:
                raise ValueError("Duplicate case/seed/trial/mode; do not count the same trial twice")
            pair[v["mode"]] = v
        entry["paired_trials"] = sum(set(pair) == {"baseline", "reference"} for pair in pairs.values())
        matched = [pair for pair in pairs.values() if set(pair) == {"baseline", "reference"}
                   and all(v["outcome"] == "passed" for v in pair.values())]
        entry["paired_verified_successes"] = len(matched)
        entry["median_paired_reference_minus_baseline_seconds"] = statistics.median(
            sum(t["seconds"] for t in pair["reference"]["turns"]) - sum(t["seconds"] for t in pair["baseline"]["turns"])
            for pair in matched) if matched else None
        for mode in ("baseline", "reference"):
            trials = [v for v in values if v["mode"] == mode]
            passed = [v for v in trials if v["outcome"] == "passed"]
            entry["modes"][mode] = {"attempts": len(trials), "verified_passes": len(passed),
                "needs_review": sum(v["outcome"] == "needs_review" for v in trials),
                "median_success_seconds": statistics.median(sum(t["seconds"] for t in v["turns"]) for v in passed) if passed else None,
                "median_success_calls": statistics.median(sum(t["tool_calls"] for t in v["turns"]) for v in passed) if passed else None}
        results.append(entry)
    return {"comparisons": results, "note": "Groups separate case/model/provider/profile/source versions. No speed benefit is inferred from failed or unpaired trials."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("validate")
    listing = subs.add_parser("list")
    listing.add_argument("--group")
    prep = subs.add_parser("prepare")
    prep.add_argument("--case", required=True, choices=[c["id"] for c in catalog()])
    prep.add_argument("--out", required=True, type=Path)
    prep.add_argument("--mode", choices=["baseline", "reference"], default="baseline")
    prep.add_argument("--seed", type=int, default=27)
    prep.add_argument("--trial", type=int, default=1)
    prep.add_argument("--base-url", default="http://127.0.0.1:18766")
    run = subs.add_parser("run")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--codex", required=True)
    run.add_argument("--model", default="gpt-5.6-luna")
    run.add_argument("--profile", choices=["full", "rpa-authoring"], default="full")
    run.add_argument("--timeout", type=float, default=600)
    grading = subs.add_parser("grade")
    grading.add_argument("--manifest", required=True, type=Path)
    grading.add_argument("--report", required=True, type=Path)
    grading.add_argument("--review", type=Path)
    grading.add_argument("--out", required=True, type=Path)
    comparison = subs.add_parser("compare")
    comparison.add_argument("grades", nargs="+", type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8")
    if args.command == "validate":
        result = validate_pack()
    elif args.command == "list":
        result = [{"id": c["id"], "title": c["title"], "group": c["group"], "environment": c["environment"]}
                  for c in catalog() if not args.group or c["group"] == args.group]
    elif args.command == "prepare":
        result = prepare(args.case, args.out, args.mode, args.seed, args.trial, args.base_url)
    elif args.command == "run":
        result = run_trial(args.manifest, args.codex, args.model, args.profile, args.timeout)
    elif args.command == "grade":
        result = grade(args.manifest, args.report, args.review)
        write(args.out, result)
    else:
        result = compare(args.grades)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
