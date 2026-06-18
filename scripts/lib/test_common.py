#!/usr/bin/env python3
# Status: production
# Path: imported by — scripts/tests/*
"""Shared test utilities — setup, heartbeat, cleanup, re-exports.

Usage::

    from lib.test_common import test_setup, test_heartbeat, test_complete, log
    TEST = test_setup("14b_comparison_q8", "14B Q8 model comparison")

    test_heartbeat("batch 3/10 done")
    result = call_llm(messages, model="reviewer")
    test_complete()
"""

import atexit
import os
import signal
import sys
import time as _time
from typing import Any, Dict, List, Optional

from lib.protection import register_protect, unregister_protect, active_contexts
from lib.common import log
from lib.db import psql_json, esc_sql
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import MODEL_REGISTRY, call_llm, call_llm_json
from lib.watchdog.messenger import heartbeat as _heartbeat, resolve_pulse

# Ensure scripts dir is on path for direct execution
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Module-level state
_test_name: str = ""
_test_start: float = 0.0
_test_cleanup_done: bool = False


def test_setup(name: str, description: str = "") -> dict:
    """Register test heartbeat + signal handlers + atexit cleanup.

    Call once at the top of every test script.
    Returns dict with keys: name, description, start_time, scripts_dir, pulse_id.

    The heartbeat pulse ``heartbeat_test_{name}`` is created with IN_PROGRESS.
    Watchdog automatically discovers it and monitors for staleness.
    """
    global _test_name, _test_start, _test_cleanup_done
    _test_name = name
    _test_start = _time.time()
    _test_cleanup_done = False

    pulse_id = f"test_{name}"
    protect_ctx = f"test_{name}"

    # Duplicate guard — reject if same test context is already active
    existing = active_contexts()
    if protect_ctx in existing:
        log(f"[test:{name}] DUPLICATE DETECTED — same test already running, abort")
        sys.exit(1)

    # Register heartbeat
    _heartbeat(pulse_id, detail="started")

    # Register protection — prevents timer/cycle interference
    register_protect(protect_ctx, reason=description)

    def _cleanup(signum=None, frame=None):
        global _test_cleanup_done
        if _test_cleanup_done:
            return
        _test_cleanup_done = True
        elapsed = _time.time() - _test_start
        reason = "atexit"
        if signum == signal.SIGTERM:
            reason = "SIGTERM"
        elif signum == signal.SIGINT:
            reason = "SIGINT"
        resolve_pulse(f"heartbeat_{pulse_id}")
        unregister_protect(protect_ctx)
        log(f"[test:{name}] Cleanup ({reason}, {elapsed:.0f}s)")

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    atexit.register(_cleanup)

    log(f"[test:{name}] {'=' * 50}")
    log(f"[test:{name}] Started — {description or name}")
    log(f"[test:{name}] {'=' * 50}")

    return {
        "name": name,
        "description": description,
        "start_time": _test_start,
        "scripts_dir": _SCRIPTS_DIR,
        "pulse_id": f"heartbeat_{pulse_id}",
    }


def test_heartbeat(detail: str = ""):
    """Update test heartbeat with progress detail.

    Call periodically during long-running tests so the watchdog
    can distinguish "still running" from "hung".
    """
    if not _test_name:
        return
    _heartbeat(f"test_{_test_name}", detail=detail)
    log(f"[test:{_test_name}] {detail}")


def test_complete(detail: str = "completed"):
    """Mark test heartbeat as RESOLVED + log final status + remove protection.

    Watchdog will NOT report this heartbeat as stale since the
    pulse status is RESOLVED (non-running).
    """
    global _test_cleanup_done
    if not _test_name or _test_cleanup_done:
        return
    _test_cleanup_done = True

    elapsed = _time.time() - _test_start
    msg = f"{detail} ({elapsed:.0f}s)"
    _heartbeat(f"test_{_test_name}", detail=msg)
    resolve_pulse(f"heartbeat_{_test_name}")
    unregister_protect(f"test_{_test_name}")
    log(f"[test:{_test_name}] Completed — {msg}")
