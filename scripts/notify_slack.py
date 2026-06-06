#!/usr/bin/env python3
"""Slack notification helper for J model tests."""
import sys, json
sys.path.insert(0, '/opt/projects/server/scripts')
from code_mod_pipeline import _notify_slack

if __name__ == '__main__':
    msg = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    _notify_slack(msg)
    print('Sent')
