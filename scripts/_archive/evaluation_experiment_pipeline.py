# Status: production
#!/usr/bin/env python3
import json, os, sys, argparse
from typing import Dict, List, Optional
from lib.rubric import utils, core
from lib.infra.preflight import preflight_checks

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENT_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment_rubric")
os.makedirs(EXPERIMENT_DIR, exist_ok=True)

COMBO_B = {
    "name": "B (proposer + reflector + judge)",
    "proposer_model": "proposer",
    "proposer_port": 8080,
    "refuter_model": "reviewer",
    "refuter_mode": "review-r",
    "refuter_port": 8080,
    "judge_model": "judge",
    "judge_mode": "review-j",
    "judge_port": 8081,
}

ALL_COMBOS = [COMBO_B]

def load_input() -> Dict:
    # Mocking input for dry-run if file doesn't exist
    fpath = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
    if not os.path.exists(fpath):
        return {"findings": [], "total_files_merged": 0, "severity_breakdown": {}}
    with open(fpath) as f:
        return json.load(f)

def find_best_combo(round1_prj_results: List[Dict]) -> str:
    best = "B"
    best_score = -1
    for r in round1_prj_results:
        if not r or "error" in r: continue
        j = r.get("judge", {}).get("result", {})
        total = j.get("P_score", 0) + j.get("R_score", 0) + j.get("consensus_score", 50)
        if total > best_score:
            best_score = total
            best = r["combo"]
    return best

def run_experiment(rounds: Optional[List[int]] = None, dry_run: bool = False):
    utils.log("=" * 60)
    utils.log("DevForge Rubric Experiment V2")
    utils.log("=" * 60)
    input_data = load_input()
    rounds = rounds or [1, 2]
    r1_results = []
    if 1 in rounds:
        utils.log("\nROUND 1: No rubric")
        core.run_primary_verify(input_data, EXPERIMENT_DIR, with_rubric=False, output_suffix="_round1_norubric", dry_run=dry_run)
        for combo in ALL_COMBOS:
            r1_results.append(core.run_prj_combo(combo, input_data, EXPERIMENT_DIR, 1, with_rubric=False, dry_run=dry_run))
    if 2 in rounds:
        utils.log("\nROUND 2: With rubric")
        best_name = find_best_combo(r1_results)
        best_combo = next((c for c in ALL_COMBOS if c["name"] == best_name), ALL_COMBOS[0])
        core.run_primary_verify(input_data, EXPERIMENT_DIR, with_rubric=True, output_suffix="_round2", dry_run=dry_run)
        core.run_prj_combo(best_combo, input_data, EXPERIMENT_DIR, 2, with_rubric=True, dry_run=dry_run)
    if 1 in rounds and 2 in rounds:
        core.compare_rounds(EXPERIMENT_DIR)

def main():
    preflight_checks("evaluation_experiment_pipeline.py")
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[1, 2])
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.compare:
        core.compare_rounds(EXPERIMENT_DIR)
    elif args.round:
        run_experiment(rounds=[args.round], dry_run=args.dry_run)
    else:
        run_experiment(dry_run=args.dry_run)

if __name__ == "__main__":
    main()
