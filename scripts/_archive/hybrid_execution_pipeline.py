# Status: production
#!/usr/bin/env python3
import sys
import os

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.hybrid import utils, debate, executor
from lib.common import log

def main():
    log("Starting Hybrid Pipeline v2")
    models = utils.detect_models()
    plan = debate.run_debate_plan(models, "example.py", "Fix bug")
    if plan:
        results = executor.execute_steps(plan, {"timeout": 30}, models)
        log(f"Execution results: {results}")

if __name__ == "__main__":
    main()
