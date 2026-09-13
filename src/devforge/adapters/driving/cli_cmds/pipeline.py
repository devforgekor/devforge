"""CLI subcommands for pipeline operations.

This module is deprecated — use devforge/cli.py directly which handles
layering properly by importing the application layer at the entry point
level rather than from within devforge.adapters.

Usage:
    devforge pipeline orchestrate [--limit 50] [--dry-run]
    devforge pipeline status
    devforge pipeline extract <turn-id>
"""
from __future__ import annotations

# Note: Pipeline commands are now handled directly in devforge/cli.py
# to maintain the hexagonal layer architecture (adapters cannot import
# application layer — that's handled by the CLI entry point which is
# outside the devforge.adapters package).

# This file is kept for backwards compatibility with imports that reference it.
# The actual implementation lives in devforge/cli.py.

__deprecated__ = True
