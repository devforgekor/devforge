#!/usr/bin/env python3.11
# Status: production
"""Core engine: keys, API calls, tool implementations — split from gemini_core.py."""

from gemini_core.keys import load_keys, pick_key  # noqa: F401
from gemini_core.api import call_gemini, API_BASE, DEFAULT_MODEL  # noqa: F401
from gemini_core.tools_def import TOOLS                              # noqa: F401
from gemini_core.execute import execute_tool, _safe_path, _strip_html  # noqa: F401
