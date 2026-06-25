#!/usr/bin/env python3
# Status: deprecated
# Path: none — replaced by review_consumer.py (night_cycle.sh Night Verify phase)
"""Night Verify Pipeline — 27B verify → feedback → restore day.

Runs after night_review completes. Alias for: night.py --verify

Usage:
  python3 scripts/pipelines/night_verify.py [--feedback]
"""

import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.infra.preflight import preflight_checks

preflight_checks("night_verify.py", required_ports={8083, 8084})

import night  # noqa: E402
sys.argv = [sys.argv[0], "--verify"] + [a for a in sys.argv[1:] if a not in ("--verify",)]
night.main()
