# Status: production
#!/usr/bin/env python3
import json, os, subprocess, sys, time, urllib.request, hashlib, uuid
from datetime import datetime, timezone
from pathlib import Path

# Add scripts dir to sys.path
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.llm.json_parser import save_dlq, validate_schema
from lib.pod_manager import *
from lib.token_budget import TokenBudget
from lib.llm_client import call_llm, resolve_model
from lib.db import psql, psql_ok, escape_sql_string, psql_json
from lib.pipeline_common import *
from pipelines.night_cycle import save_feedback_to_db
from lib.common import log
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

# CLI Global Config
RESUME_PRJ = "--resume-prj" in sys.argv
DRY_RUN = "--dry-run" in sys.argv
INPUT_OVERRIDE = None
for _i, _a in enumerate(sys.argv):
    if _a == "--input" and _i + 1 < len(sys.argv):
        INPUT_OVERRIDE = sys.argv[_i + 1]

# Import Split Modules
from lib.prj.utils import *
from lib.prj.core import *

def run_round(round_num, with_rubric, resume_state_path=None):
    log(f"============================================================")
    log(f"ROUND {round_num}: {'WITH RUBRIC' if with_rubric else 'NO RUBRIC'}")
    log(f"============================================================")
    
    if resume_state_path and os.path.exists(resume_state_path):
        with open(resume_state_path, 'r') as f:
             # Assuming from_dict is a classmethod
             state_dict = json.load(f)
             state = PipelineState(state_dict.get('round_num', round_num), 
                                  state_dict.get('with_rubric', with_rubric),
                                  state_dict.get('input_data', {}))
             # In actual implementation, from_dict would restore full state
        log(f"Resuming from state: {resume_state_path}")
    else:
        data = load_input(INPUT_OVERRIDE)
        state = PipelineState(round_num, with_rubric, data)

    tag = f"R{round_num}"
    try:
        # Pass necessary context if core functions need them
        result = run_propose_review_judge(state, tag, rubric_append="")
        return result
    except Exception as e:
        log(f"UNHANDLED ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        return None

def main():
    preflight_checks()
    log("P-R-J 고정 역할 실험 시작 (Refactored)")
    run_round(1, with_rubric=False)

if __name__ == "__main__":
    main()
