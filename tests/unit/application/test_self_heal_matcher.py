#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/application/
"""Tests for SelfHealMatcher — pre-approved pattern matching."""

from __future__ import annotations

from devforge.application.self_heal_matcher import SelfHealMatcher


def test_should_match_known_pattern() -> None:
    matcher = SelfHealMatcher()
    plan = matcher.match("svc:ebook-watcher", "down")
    assert plan is not None
    assert plan.component == "svc:ebook-watcher"
    assert plan.approved is True
    assert len(plan.steps) == 3


def test_should_return_none_for_unknown_pattern() -> None:
    matcher = SelfHealMatcher()
    plan = matcher.match("svc:unknown-service", "down")
    assert plan is None


def test_should_return_none_for_unknown_event() -> None:
    matcher = SelfHealMatcher()
    plan = matcher.match("svc:ebook-watcher", "unknown_event")
    assert plan is None


def test_should_match_backup_oneshot() -> None:
    matcher = SelfHealMatcher()
    plan = matcher.match("oneshot:devforge-backup.service", "failed")
    assert plan is not None
    assert plan.approved is True
