# Status: production
# Path: imported by scripts/ modules
"""Log parsers — Claude, Copilot, OpenCode."""

from lib.parsers.claude import parse as parse_claude
from lib.parsers.copilot import parse as parse_copilot

__all__ = ["parse_claude", "parse_copilot"]
