#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/domain/turn_collection/
"""Tests for turn collection domain rules (pure, no I/O)."""

from __future__ import annotations

from devforge.domain.turn_collection.collection import (
    MAX_TEXT_CHARS,
    MAX_THINKING_CHARS,
    MAX_USER_TURN_CHARS,
    CheckpointEntry,
    SessionAction,
    filter_unseen,
    get_entry,
    merge_checkpoint,
    plan_session,
    truncate_turn,
)


def test_should_recover_count_when_checkpoint_entry_is_legacy_int() -> None:
    assert CheckpointEntry.from_raw(7) == CheckpointEntry(count=7, mtime=0.0)


def test_should_default_to_zero_when_checkpoint_entry_is_empty() -> None:
    assert CheckpointEntry.from_raw({}) == CheckpointEntry(count=0, mtime=0.0)


def test_should_default_to_zero_when_checkpoint_entry_is_junk() -> None:
    assert CheckpointEntry.from_raw("not-a-dict") == CheckpointEntry(count=0, mtime=0.0)


def test_should_read_session_entry_when_present() -> None:
    checkpoint = {"claude": {"abc": {"count": 3, "mtime": 10.5}}}
    assert get_entry(checkpoint, "claude", "abc") == CheckpointEntry(3, 10.5)


def test_should_default_entry_when_session_is_absent() -> None:
    assert get_entry({}, "claude", "abc") == CheckpointEntry(0, 0.0)


def test_should_preserve_metadata_when_merging_foreign_string_keys() -> None:
    existing = {"last_ingested_at": "2026-09-29T00:00:00+00:00", "claude": {}}
    merged = merge_checkpoint(existing, {"claude": {"last_ingested_at": "stale"}})
    assert merged["last_ingested_at"] == "2026-09-29T00:00:00+00:00"


def test_should_preserve_metadata_when_merging_metadata_key() -> None:
    existing = {"claude": {"last_ingested_at": "keep-me"}}
    merged = merge_checkpoint(existing, {"claude": {"last_ingested_at": "overwrite"}})
    assert merged["claude"]["last_ingested_at"] == "keep-me"


def test_should_keep_higher_values_when_merging_overlapping_sessions() -> None:
    existing = {"claude": {"abc": {"count": 5, "mtime": 100.0}}}
    merged = merge_checkpoint(existing, {"claude": {"abc": {"count": 3, "mtime": 90.0}}})
    assert merged["claude"]["abc"] == {"count": 5, "mtime": 100.0}


def test_should_take_new_higher_position_when_incoming_is_ahead() -> None:
    existing = {"claude": {"abc": {"count": 3, "mtime": 90.0}}}
    merged = merge_checkpoint(existing, {"claude": {"abc": {"count": 8, "mtime": 120.0}}})
    assert merged["claude"]["abc"] == {"count": 8, "mtime": 120.0}


def test_should_upgrade_legacy_int_entry_when_merging() -> None:
    existing = {"claude": {"abc": 4}}
    merged = merge_checkpoint(existing, {"claude": {"abc": {"count": 9, "mtime": 12.0}}})
    assert merged["claude"]["abc"] == {"count": 9, "mtime": 12.0}


def test_should_keep_int_entry_when_incoming_is_int() -> None:
    existing = {"claude": {"abc": 4}}
    merged = merge_checkpoint(existing, {"claude": {"abc": 2}})
    assert merged["claude"]["abc"] == 4


def test_should_add_session_when_missing_from_existing() -> None:
    merged = merge_checkpoint({}, {"claude": {"abc": {"count": 2, "mtime": 5.0}}})
    assert merged["claude"]["abc"] == {"count": 2, "mtime": 5.0}


def test_should_skip_parsing_when_mtime_unchanged_and_count_positive() -> None:
    plan = plan_session(CheckpointEntry(4, 100.0), current_mtime=100.0, parsed=[{"text": "x"}] * 9)
    assert plan.action is SessionAction.UNCHANGED
    assert plan.record is None
    assert plan.new_turns == []


def test_should_parse_when_mtime_unchanged_but_nothing_ingested_yet() -> None:
    plan = plan_session(CheckpointEntry(0, 0.0), current_mtime=0.0, parsed=[{"text": "x"}])
    assert plan.action is SessionAction.INGEST
    assert len(plan.new_turns) == 1


def test_should_record_mtime_when_parsing_returned_none() -> None:
    plan = plan_session(CheckpointEntry(4, 100.0), current_mtime=200.0, parsed=None)
    assert plan.action is SessionAction.NO_NEW
    assert plan.record == CheckpointEntry(4, 200.0)
    assert plan.new_turns == []


def test_should_record_mtime_when_session_is_empty() -> None:
    plan = plan_session(CheckpointEntry(4, 100.0), current_mtime=200.0, parsed=[])
    assert plan.action is SessionAction.NO_NEW
    assert plan.record == CheckpointEntry(4, 200.0)


def test_should_record_mtime_when_source_shrank_or_stayed_equal() -> None:
    plan = plan_session(CheckpointEntry(4, 100.0), current_mtime=200.0, parsed=[{"text": "x"}] * 3)
    assert plan.action is SessionAction.NO_NEW
    assert plan.record == CheckpointEntry(4, 200.0)
    assert plan.new_turns == []


def test_should_slice_from_checkpoint_when_source_grew() -> None:
    parsed = [{"text": f"t{i}"} for i in range(5)]
    plan = plan_session(CheckpointEntry(3, 100.0), current_mtime=200.0, parsed=parsed)
    assert plan.action is SessionAction.INGEST
    assert [t["text"] for t in plan.new_turns] == ["t3", "t4"]
    assert plan.record is None


def test_should_keep_original_indexes_when_filtering_unseen() -> None:
    turns = [
        {"source_message_id": "a", "text": "0"},
        {"source_message_id": "b", "text": "1"},
        {"source_message_id": "c", "text": "2"},
    ]
    kept = filter_unseen(turns, existing_ids={"b"})
    assert [(i, t["text"]) for i, t in kept] == [(0, "0"), (2, "2")]


def test_should_drop_repeats_inside_one_batch_when_filtering() -> None:
    turns = [
        {"source_message_id": "a", "text": "0"},
        {"source_message_id": "a", "text": "1"},
    ]
    kept = filter_unseen(turns, existing_ids=set())
    assert len(kept) == 1
    assert kept[0][0] == 0


def test_should_keep_turns_without_source_message_id_when_filtering() -> None:
    turns = [
        {"source_message_id": "", "text": "0"},
        {"source_message_id": None, "text": "1"},
    ]
    kept = filter_unseen(turns, existing_ids={"a"})
    assert len(kept) == 2


def test_should_keep_every_turn_when_nothing_is_known() -> None:
    turns = [{"source_message_id": "a"}, {"source_message_id": "b"}]
    assert len(filter_unseen(turns, existing_ids=set())) == 2


def test_should_truncate_fields_when_over_the_stored_limits() -> None:
    truncated = truncate_turn(
        {
            "user_turn": "u" * (MAX_USER_TURN_CHARS + 1),
            "thinking": "h" * (MAX_THINKING_CHARS + 1),
            "text": "t" * (MAX_TEXT_CHARS + 1),
            "source_message_id": "keep",
        }
    )
    assert len(truncated["user_turn"]) == MAX_USER_TURN_CHARS
    assert len(truncated["thinking"]) == MAX_THINKING_CHARS
    assert len(truncated["text"]) == MAX_TEXT_CHARS


def test_should_keep_fields_intact_when_exactly_at_the_limit() -> None:
    turn = {
        "user_turn": "u" * MAX_USER_TURN_CHARS,
        "thinking": "h" * MAX_THINKING_CHARS,
        "text": "t" * MAX_TEXT_CHARS,
    }
    truncated = truncate_turn(turn)
    assert truncated == turn


def test_should_treat_missing_or_null_fields_as_empty_when_truncating() -> None:
    assert truncate_turn({}) == {"user_turn": "", "thinking": "", "text": ""}
    assert truncate_turn({"thinking": None, "text": None})["thinking"] == ""
