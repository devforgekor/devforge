# Status: production
# Path: imported by scripts/ modules
"""Tracking — dependency tracking, agent name normalization.

phase_tracker retired 2026-09-23 (target docs/phases.md was archived → silent no-op;
blueprint.yaml is now AI-maintained, refactoring SSOT is REFACTORING_STATUS.yaml).
"""
from lib.tracking.dependency_tracker import collect_references
from lib.tracking.agent_names import normalize
