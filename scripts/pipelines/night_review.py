#!/usr/bin/env python3
# Status: experimental
# Path: systemd:devforge-night-cycle.timer — runs at KST 01:00
"""Night Debate Pipeline — 30B Proposer → 14B Refuter → N14B Judge.

Reads day_verify output (pipeline_verify_*.json) and runs P-R-J chain.
Alias for: night.py --review

Usage:
  python3 scripts/pipelines/night_review.py [--rubric] [--group-category]
"""

import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.infra.preflight import preflight_checks

preflight_checks("night_review.py", required_ports={8080})

# Forward to night.py --review
import night  # noqa: E402 (after chdir and sys.path)
sys.argv = [sys.argv[0], "--review"] + [a for a in sys.argv[1:] if a not in ("--review",)]
night.main()
