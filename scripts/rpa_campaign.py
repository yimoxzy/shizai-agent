"""Run the component benchmark serially, checkpointing each independent trial."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rpa_benchmark import catalog, digest, prepare, read, run_trial, write  # noqa: E402

PAIRS = ["jd-phones-sales", "file-replace", "web-pages", "table-numeric-sort", "excel-formulas"]


def plan():
    # Alternate A/B order across the five exploratory pairs.
    result = []
    for index, case_id in enumerate(PAIRS):
        modes = ["baseline", "reference"] if index % 2 == 0 else ["reference", "baseline"]
        result.extend((case_id, mode) for mode in modes)
    result.extend((c["id"], "baseline") for c in catalog() if c["id"] not in PAIRS)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--timeout", default=600, type=float)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8")
    folder = args.out.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    ledger_path = folder / "campaign.json"
    core = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / "shizai_agent/core").glob("*.py"))}
    if ledger_path.exists():
        ledger = read(ledger_path)
        if ledger["implementation_sha256"] != core:
            raise ValueError("MCP source changed during campaign; create a separate campaign")
    else:
        ledger = {"started_at": time.time(), "model": "gpt-5.6-luna", "effort": "medium",
                  "timeout_per_turn": args.timeout, "seed": 27, "trial": 1,
                  "implementation_sha256": core, "status": "prepared", "trials": []}
        for case_id, mode in plan():
            target = folder / f"{case_id}-{mode}-r1"
            if not (target / "manifest.json").exists():
                prepare(case_id, target, mode, seed=27, trial=1)
            ledger["trials"].append({"case_id": case_id, "mode": mode, "folder": str(target),
                                     "profile": "full" if case_id.startswith(("edit-", "control-")) else "rpa-authoring",
                                     "status": "prepared"})
        write(ledger_path, ledger)
    if ledger["timeout_per_turn"] != args.timeout:
        raise ValueError("Do not change timeout in a started campaign")
    if args.prepare_only:
        print(json.dumps({"prepared_trials": len(ledger["trials"]), "ledger": str(ledger_path)}), flush=True)
        return
    ledger["status"] = "running"
    write(ledger_path, ledger)
    for index, trial in enumerate(ledger["trials"], 1):
        if (folder / "PAUSE").exists():
            ledger["status"] = "paused_at_trial_boundary"
            write(ledger_path, ledger)
            print(json.dumps({"status": ledger["status"], "next_index": index}), flush=True)
            return
        target = Path(trial["folder"])
        if (target / "grade.json").exists() and (target / "cleanup.json").exists():
            cleaned = read(target / "cleanup.json")
            if cleaned.get("cleanup_complete") is not True and cleaned.get("run_observed") is not False:
                raise ValueError(f"Unconfirmed cleanup: {target}")
            trial.update(status="finished", outcome=read(target / "grade.json")["outcome"])
            write(ledger_path, ledger)
            continue
        if (target / "run").exists():
            ledger["status"] = "paused_existing_trial"
            write(ledger_path, ledger)
            raise ValueError(f"Existing incomplete trial requires inspection: {target}")
        current = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / "shizai_agent/core").glob("*.py"))}
        if current != core:
            raise ValueError("MCP source changed during campaign")
        trial.update(status="running", started_at=time.time())
        write(ledger_path, ledger)
        print(json.dumps({"index": index, "total": len(ledger["trials"]), **trial}), flush=True)
        try:
            result = run_trial(target / "manifest.json", args.codex, ledger["model"], trial["profile"], args.timeout)
        except Exception as exc:
            trial.update(status="runner_error", error=f"{type(exc).__name__}: {exc}")
            ledger["status"] = "paused_error"
            write(ledger_path, ledger)
            raise
        trial.update(status="finished", finished_at=time.time(), outcome=result["outcome"])
        write(ledger_path, ledger)
        print(json.dumps({"index": index, "status": "finished", "case_id": trial["case_id"], **result}), flush=True)
    ledger.update(status="measurement_finished", finished_at=time.time())
    write(ledger_path, ledger)
    print(json.dumps({"status": ledger["status"], "trials": len(ledger["trials"])}), flush=True)


if __name__ == "__main__":
    main()
