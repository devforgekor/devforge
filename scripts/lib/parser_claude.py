#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Backward-compat shim — delegates to lib.parsers.claude."""
from lib.parsers.claude import parse
parse_claude = parse

