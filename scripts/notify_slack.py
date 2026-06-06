#!/usr/bin/env python3
# Status: production
# Path: imported by pipelines/code_mod.py
"""Slack notification helper for J model tests."""
import sys, json
sys.path.insert(0, '/opt/projects/server/scripts')
from pipelines.code_mod import _notify_slack

if __name__ == '__main__':
    msg = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    _notify_slack(msg)
    print('Sent')
