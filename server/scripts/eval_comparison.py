#!/usr/bin/env python3
"""DeepSeek Pro evaluation of 12-run comparison test results.

Reads pipeline (local32b_task*.json) and debate (debate_sessions/*/final_report.md)
outputs, scores each with DeepSeek Pro, produces comparison matrix.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.llm.client import call_llm

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
RESULT_DIR = Path("/var/tmp/comparison_tests")
OUTPUT_DIR = RESULT_DIR / "evaluation"
PIPELINE_DIR = Path("/var/tmp/code_mod_tests")
DEBATE_DIR = Path("/opt/ai_data/debate_sessions")

EVAL_PROMPT = """You are a code review quality evaluator. Score the following code modification output.

TASK:
{task_description}

OUTPUT TO EVALUATE:
{output}

Score each dimension 0-100:
1. **correctness** — Does the diff actually fix the problem? Are there logic errors?
2. **completeness** — Are all affected code paths covered? Missing edge cases?
3. **minimality** — Is the change minimal and surgical? No unnecessary refactoring or style changes?
4. **clarity** — Is the diff easy to understand? Clear variable names, no dead code?
5. **rationale** — Is the reasoning sound? Are the decision points well-justified?

Output STRICT JSON:
{{"correctness": <0-100>,
  "completeness": <0-100>,
  "minimality": <0-100>,
  "clarity": <0-100>,
  "rationale": <0-100>,
  "overall": <0-100>,
  "summary": "<2-3 sentence overall assessment>",
  "strengths": ["point1", "point2"],
  "weaknesses": ["point1", "point2"]}}"""


def load_task_description(task_id: int) -> str:
    """Load task description from YAML."""
    import yaml
    tasks_file = Path(__file__).resolve().parent.parent / "code_mod_test_tasks.yaml"
    with open(tasks_file) as f:
        config = yaml.safe_load(f)
    for t in config["tasks"]:
        if t["id"] == task_id:
            return f"Task {task_id}: {t['name']}\n{t['description']}"
    return f"Task {task_id}"


def find_pipeline_output(task_id: int, api: bool) -> Optional[dict]:
    """Find the latest pipeline result for a task."""
    suffix = "with_api" if api else "no_api"
    pattern = f"local32b_task{task_id:02d}_*.json"
    files = sorted(PIPELINE_DIR.glob(pattern), reverse=True)
    for f in files:
        data = json.loads(f.read_text())
        pkg = data.get("stage4", {}).get("body", {})
        diff = pkg.get("diff", "")
        if diff:
            return {
                "source_file": str(f.name),
                "diff": str(diff)[:5000],
                "analysis": pkg.get("analysis", ""),
                "rationale": pkg.get("rationale", []),
                "confidence": pkg.get("confidence", "N/A"),
                "api_used": data.get("stage2", {}).get("model", "") == "deepseek-chat",
            }
    return None


def find_debate_output(task_id: int, api: bool) -> Optional[dict]:
    """Find the latest debate output for a task."""
    suffix = "with_api" if api else "no_api"
    # Search debate session dirs for matching reports
    for session_dir in sorted(DEBATE_DIR.glob("*/"), reverse=True):
        state_file = session_dir / "state.jsonl"
        report_file = session_dir / "final_report.md"
        if not state_file.exists() or not report_file.exists():
            continue
        # Check if this session has the right task
        state_text = state_file.read_text()
        if f"task {task_id}" not in state_text.lower() and f"t{task_id:02d}" not in state_text.lower():
            # Try matching by question content
            task_desc = load_task_description(task_id)
            if task_desc.split("\n")[1][:40].lower() not in state_text.lower():
                continue

        # Read the report
        report = report_file.read_text()
        # Extract diff if present
        diff = ""
        in_diff = False
        for line in report.split("\n"):
            if line.startswith("```diff") or line.startswith("```python"):
                in_diff = True
                continue
            if in_diff and line.strip() == "```":
                in_diff = False
                continue
            if in_diff:
                diff += line + "\n"

        # Check if API was used (search_exec with deepseek-api source)
        api_used = "deepseek-api" in state_text

        return {
            "source_file": str(session_dir.name),
            "diff": diff[:5000] if diff else report[:5000],
            "analysis": "",
            "rationale": [],
            "confidence": "N/A",
            "api_used": api_used,
        }
    return None


def evaluate_output(task_desc: str, output: dict, system: str, task_id: int, api: bool) -> dict:
    """Score a single output with DeepSeek Pro."""
    label = f"{system}_t{task_id:02d}_{'api' if api else 'local'}"
    print(f"  Evaluating {label}...")

    output_text = f"DIFF:\n{output.get('diff', 'N/A')}\n\n"
    if output.get("analysis"):
        output_text += f"ANALYSIS:\n{output['analysis']}\n\n"
    if output.get("rationale"):
        output_text += f"RATIONALE:\n{'; '.join(output['rationale'])}\n"

    status, body = call_llm(
        "https://api.deepseek.com/v1/chat/completions",
        [{"role": "user", "content": EVAL_PROMPT.format(
            task_description=task_desc,
            output=output_text,
        )}],
        api_key=DEEPSEEK_KEY,
        model="deepseek-chat",
        timeout=120,
        max_tokens=1024,
    )

    if status != 200:
        return {"label": label, "error": body.get("error", f"HTTP {status}")}

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        # Try JSON parse
        import re
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            parsed["label"] = label
            parsed["system"] = system
            parsed["task_id"] = task_id
            parsed["api_mode"] = api
            return parsed
    except json.JSONDecodeError:
        pass

    return {"label": label, "raw": content, "error": "JSON parse failed"}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not DEEPSEEK_KEY:
        print("ERROR: DEEPSEEK_API_KEY not set")
        sys.exit(1)

    tasks = [1, 5, 11]
    systems = ["pipeline", "debate"]
    api_modes = [False, True]

    all_results = []

    for task_id in tasks:
        task_desc = load_task_description(task_id)
        print(f"\n{'='*60}")
        print(f"Task {task_id}: {task_desc.split(chr(10))[0]}")
        print(f"{'='*60}")

        for system in systems:
            for api in api_modes:
                if system == "pipeline":
                    output = find_pipeline_output(task_id, api)
                else:
                    output = find_debate_output(task_id, api)

                if output is None:
                    print(f"  {system} t{task_id:02d} api={api}: NOT FOUND — skipping")
                    all_results.append({
                        "label": f"{system}_t{task_id:02d}_{'api' if api else 'local'}",
                        "error": "output not found",
                        "system": system,
                        "task_id": task_id,
                        "api_mode": api,
                    })
                    continue

                result = evaluate_output(task_desc, output, system, task_id, api)
                all_results.append(result)

    # Write results
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = OUTPUT_DIR / f"evaluation_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults: {out_path}")

    # Print summary table
    print(f"\n{'='*80}")
    print(f"COMPARISON MATRIX")
    print(f"{'='*80}")
    header = f"{'Run':<30} {'Corr':>5} {'Cmpl':>5} {'Min':>5} {'Clar':>5} {'Rat':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for r in all_results:
        if "error" in r:
            print(f"{r['label']:<30} ERROR: {r['error'][:40]}")
        else:
            print(f"{r['label']:<30} {r.get('correctness',0):>5} {r.get('completeness',0):>5} "
                  f"{r.get('minimality',0):>5} {r.get('clarity',0):>5} "
                  f"{r.get('rationale',0):>5} {r.get('overall',0):>7}")

    # Group by system + api_mode
    print(f"\n{'='*80}")
    print(f"SUMMARY BY MODE (average overall score)")
    print(f"{'='*80}")
    from collections import defaultdict
    groups = defaultdict(list)
    for r in all_results:
        if "error" not in r and "overall" in r:
            key = f"{r.get('system','?')} + {'API' if r.get('api_mode') else 'Local'}"
            groups[key].append(r["overall"])

    for key, scores in sorted(groups.items()):
        avg = sum(scores) / len(scores)
        print(f"  {key:<25} avg={avg:.1f}  ({len(scores)} runs)")

    print(f"\nFull results: {out_path}")


if __name__ == "__main__":
    main()
