"""Run independent Luna cases serially against the shared native desktop."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.luna_rpa_eval import tool_payload  # noqa: E402
from shizai_agent.core.debug_session import debug_stop  # noqa: E402
from shizai_agent.core.extension_bridge import ShizaiExtensionBridge  # noqa: E402
from shizai_agent.core.paths import ShizaiPaths  # noqa: E402
from shizai_agent.core.project import native_rpa_stop  # noqa: E402


def prepare_inputs() -> None:
    data = ROOT / "_tmp/luna-eval/data"
    data.mkdir(parents=True, exist_ok=True)
    for fixture in (ROOT / "tests/evals/fixtures").iterdir():
        content = fixture.read_text(encoding="utf-8-sig")
        target = data / fixture.name
        if target.exists():
            if target.read_text(encoding="utf-8-sig") != content:
                raise ValueError(f"Input fixture changed; preserve and inspect it before resetting: {target}")
        else:
            target.write_text(content, encoding="utf-8-sig" if target.suffix == ".csv" else "utf-8", newline="")


def cleanup(report_path: Path) -> dict:
    """Only stop a run associated with an application created by this case."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    projects = set()
    last_run = None
    captured = set()
    debugged = set()
    for turn in report.get("turns", []):
        for item in turn.get("items", []):
            payload = tool_payload(item)
            if item.get("tool") == "shizai_rpa_start_design":
                app = payload.get("application") or {}
                if app.get("created") and app.get("local_path") and str(app.get("name", "")).startswith("Luna评测"):
                    projects.add(app.get("local_path"))
            if item.get("tool") in {"shizai_rpa_run", "shizai_rpa_open_browser", "shizai_rpa_debug_start"}:
                last_run = item.get("arguments", {}).get("project_ref")
            if item.get("tool") in {"shizai_rpa_open_browser", "shizai_rpa_debug_start"}:
                debugged.add(item.get("arguments", {}).get("project_ref"))
            if item.get("tool") in {"shizai_rpa_capture_windows", "shizai_rpa_scan_page",
                                    "shizai_extension_start_capture", "shizai_rpa_browser_session_start"}:
                captured.add(item.get("arguments", {}).get("project_ref"))
    for project in sorted(captured & projects):
        result = ShizaiExtensionBridge().stop_capture(package_path=project)
        (report_path.parent / "suite-capture-cleanup.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not (result.get("result") or {}).get("isSuccess"):
            raise RuntimeError(f"Capture cleanup was not confirmed: {report_path.parent.name}")
    for project in sorted(debugged & projects):
        result = debug_stop(ShizaiPaths.from_environment(), project)
        (report_path.parent / "suite-debug-cleanup.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("cleanup_complete"):
            raise RuntimeError(f"Debug cleanup was not confirmed: {report_path.parent.name}")
    if last_run and last_run in projects:
        result = native_rpa_stop(ShizaiPaths.from_environment(), last_run)
        (report_path.parent / "suite-cleanup.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("cleanup_complete"):
            raise RuntimeError(f"Native runtime cleanup was not confirmed: {report_path.parent.name}")
        return {"project": last_run, "cleanup_complete": True}
    return {"run_observed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--cases", nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "_tmp/luna-eval/expanded")
    parser.add_argument("--suffix", default="A")
    parser.add_argument("--timeout", default=600, type=float)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prepare_inputs()
    for case in args.cases:
        prompt = ROOT / "tests/evals" / f"luna-{case}.json"
        if not prompt.is_file():
            raise ValueError(f"Unknown case: {case}")
        output = args.output.resolve() / f"{case}-{args.suffix.lower()}"
        output.mkdir(parents=True, exist_ok=True)
        if (output / "report.json").exists():
            raise ValueError(f"Refusing to overwrite previous evidence: {output}")
        command = [sys.executable, str(ROOT / "scripts/luna_rpa_eval.py"),
                   "--codex", args.codex, "--prompts", str(prompt),
                   "--case-name", f"Luna评测-扩展-{case}-{args.suffix}",
                   "--output", str(output), "--timeout", str(args.timeout),
                   "--profile", "full" if case == "structural-edit" else "rpa-authoring"]
        print(json.dumps({"case": case, "status": "starting"}), flush=True)
        with (output / "runner.stdout.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise RuntimeError(f"Runner failed; inspect {output}")
        cleaned = cleanup(output / "report.json")
        prepare_inputs()
        print(json.dumps({"case": case, "status": "finished", "cleanup": cleaned}), flush=True)


if __name__ == "__main__":
    main()
