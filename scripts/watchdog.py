#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-watchdog.service
"""DevForge Watchdog — entry point."""
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from lib.watchdog import main

main()
