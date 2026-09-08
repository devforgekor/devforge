#!/usr/bin/env python3
# Status: production
import builtins
import json
import os
import sys

# Add scripts dir to sys.path
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

# CLI Global Config
builtins.DRY_RUN = "--dry-run" in sys.argv
builtins.RESUME_PRJ = "--resume-prj" in sys.argv
builtins.INPUT_OVERRIDE = None
for _i, _a in enumerate(sys.argv):
    if _a == "--input" and _i + 1 < len(sys.argv):
        builtins.INPUT_OVERRIDE = sys.argv[_i + 1]

from lib.common import log
from lib.infra.preflight import preflight_checks
from lib.pipeline_common import *
from lib.pod_manager import *

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

# Import Split Modules
from lib.prj.core import *
from lib.prj.utils import *


def run_round(round_num, with_rubric, resume_state_path=None):
    log("============================================================")
    log(f"ROUND {round_num}: {'WITH RUBRIC' if with_rubric else 'NO RUBRIC'}")
    log("============================================================")

    if resume_state_path and os.path.exists(resume_state_path):
        with open(resume_state_path) as f:
            # Simplified state restoration for refactor verification
            data = json.load(f)
            state = PipelineState(round_num, with_rubric, data.get("input_data", {}))
        log(f"Resuming from state: {resume_state_path}")
    else:
        data = load_input(builtins.INPUT_OVERRIDE)
        state = PipelineState(round_num, with_rubric, data)

    tag = f"R{round_num}"
    try:
        result = run_propose_review_judge(state, tag, rubric_append="")
        return result
    except Exception as e:
        log(f"UNHANDLED ERROR: {e}")
        import traceback

        log(traceback.format_exc())
        return None


def main():
    preflight_checks()
    if builtins.DRY_RUN:
        log("[DRY RUN] 모드 활성화 — LLM 호출/컨테이너 없이 데이터 흐름만 검증")
    log("P-R-J 고정 역할 실험 시작 (Refactored)")
    run_round(1, with_rubric=False)


if __name__ == "__main__":
    main()
