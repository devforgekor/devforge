#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/domain/turn_collection/
"""Tests for turn collection domain rules (pure, no I/O)."""

from __future__ import annotations

import sqlite3

from devforge.domain.turn_collection.collection import (
    MAX_TEXT_CHARS,
    MAX_THINKING_CHARS,
    MAX_TRANSIENT_FAILURES,
    MAX_USER_TURN_CHARS,
    CheckpointEntry,
    FailureKind,
    SessionAction,
    classify_session_error,
    collection_health,
    filter_unseen,
    get_entry,
    is_quarantined,
    merge_checkpoint,
    plan_failure,
    plan_session,
    should_skip_parse,
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


def test_should_skip_parse_when_source_is_untouched() -> None:
    assert should_skip_parse(CheckpointEntry(4, 100.0), current_mtime=100.0) is True


def test_should_not_skip_parse_when_nothing_was_ingested_yet() -> None:
    assert should_skip_parse(CheckpointEntry(0, 100.0), current_mtime=100.0) is False


def test_should_not_skip_parse_when_source_changed() -> None:
    assert should_skip_parse(CheckpointEntry(4, 100.0), current_mtime=101.0) is False


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


# --- failure classification -------------------------------------------------
# [WHY] sqlite3 raises a bare DatabaseError for SQLITE_CORRUPT (no
# OperationalError/IntegrityError subclass, no error code attribute), so the
# message is the only available signal. Unknown shapes must stay UNKNOWN
# rather than guess — an UNKNOWN is quarantined and logged, never swallowed.


def test_should_classify_corrupt_database_image_as_permanent() -> None:
    exc = sqlite3.DatabaseError("database disk image is malformed")
    assert classify_session_error(exc) is FailureKind.PERMANENT


def test_should_classify_not_a_database_as_permanent() -> None:
    assert classify_session_error(sqlite3.DatabaseError("file is not a database")) is (
        FailureKind.PERMANENT
    )


def test_should_classify_lock_contention_as_transient() -> None:
    assert classify_session_error(sqlite3.OperationalError("database is locked")) is (
        FailureKind.TRANSIENT
    )


def test_should_classify_unrecognized_database_error_as_unknown() -> None:
    assert classify_session_error(sqlite3.DatabaseError("something else entirely")) is (
        FailureKind.UNKNOWN
    )


def test_should_classify_non_database_exception_as_unknown() -> None:
    assert classify_session_error(ValueError("bad json")) is FailureKind.UNKNOWN


# --- quarantine plan --------------------------------------------------------


def test_should_quarantine_immediately_when_failure_is_permanent() -> None:
    prev = CheckpointEntry(count=3, mtime=10.0)
    plan = plan_failure(
        prev, 10.0, sqlite3.DatabaseError("database disk image is malformed"), now=99.0
    )
    assert plan.action is SessionAction.QUARANTINE
    assert plan.record is not None
    assert plan.record.failures == 1
    assert plan.record.fail_kind == FailureKind.PERMANENT.value
    assert plan.record.quarantined_at == 99.0


def test_should_quarantine_immediately_when_failure_is_unknown() -> None:
    prev = CheckpointEntry(count=3, mtime=10.0)
    plan = plan_failure(prev, 10.0, ValueError("bad json"), now=99.0)
    assert plan.action is SessionAction.QUARANTINE
    assert plan.record is not None
    assert plan.record.fail_kind == FailureKind.UNKNOWN.value


def test_should_retry_while_transient_failures_stay_under_the_cap() -> None:
    prev = CheckpointEntry(count=3, mtime=10.0, failures=1)
    plan = plan_failure(prev, 10.0, sqlite3.OperationalError("database is locked"), now=99.0)
    assert plan.action is not SessionAction.QUARANTINE
    assert plan.record is not None
    assert plan.record.failures == 2
    assert plan.record.quarantined_at == 0.0


def test_should_quarantine_when_transient_failures_reach_the_cap() -> None:
    prev = CheckpointEntry(count=3, mtime=10.0, failures=MAX_TRANSIENT_FAILURES - 1)
    plan = plan_failure(prev, 10.0, sqlite3.OperationalError("database is locked"), now=99.0)
    assert plan.action is SessionAction.QUARANTINE
    assert plan.record is not None
    assert plan.record.failures == MAX_TRANSIENT_FAILURES
    assert plan.record.quarantined_at == 99.0


def test_should_preserve_ingested_count_when_recording_a_failure() -> None:
    prev = CheckpointEntry(count=17, mtime=10.0)
    plan = plan_failure(prev, 10.0, ValueError("x"), now=99.0)
    assert plan.record is not None
    assert plan.record.count == 17


# --- skip / recovery --------------------------------------------------------


def test_should_skip_parsing_when_quarantined_and_source_untouched() -> None:
    prev = CheckpointEntry(count=17, mtime=10.0, failures=1, quarantined_at=99.0)
    assert is_quarantined(prev)
    assert should_skip_parse(prev, 10.0)


def test_should_retry_when_quarantined_but_source_advanced() -> None:
    prev = CheckpointEntry(count=17, mtime=10.0, failures=1, quarantined_at=99.0)
    assert not should_skip_parse(prev, 11.0)


def test_should_report_healthy_when_never_failed() -> None:
    assert not is_quarantined(CheckpointEntry(count=17, mtime=10.0))


# --- quarantine state survives merge (regression) -----------------------------
# [WHY] merge_checkpoint rebuilds each entry from scratch. Before the quarantine
# fields existed it silently dropped anything but count/mtime, which would have
# reset a parked session every poll cycle and restored the infinite retry loop.


def test_should_preserve_quarantine_state_when_merging() -> None:
    existing = {"opencode": {"abc": {"count": 3, "mtime": 10.0}}}
    incoming = {
        "opencode": {
            "abc": {
                "count": 3,
                "mtime": 10.0,
                "failures": 2,
                "fail_kind": "permanent",
                "quarantined_at": 99.0,
            }
        }
    }
    merged = merge_checkpoint(existing, incoming)["opencode"]["abc"]
    assert merged["failures"] == 2
    assert merged["fail_kind"] == "permanent"
    assert merged["quarantined_at"] == 99.0


def test_should_keep_quarantine_state_when_merging_writer_that_omits_it() -> None:
    # collect_turns.py only knows count/mtime — it must not clear a parked session.
    existing = {
        "opencode": {"abc": {"count": 3, "mtime": 10.0, "failures": 5, "quarantined_at": 200.0}}
    }
    incoming = {"opencode": {"abc": {"count": 3, "mtime": 10.0}}}
    merged = merge_checkpoint(existing, incoming)["opencode"]["abc"]
    assert merged["failures"] == 5
    assert merged["quarantined_at"] == 200.0


def test_should_clear_quarantine_when_writer_states_recovery() -> None:
    # turn_watcher writes the full triple, so a repaired source can un-park.
    existing = {
        "opencode": {
            "abc": {
                "count": 3,
                "mtime": 10.0,
                "failures": 5,
                "fail_kind": "permanent",
                "quarantined_at": 200.0,
            }
        }
    }
    incoming = {
        "opencode": {
            "abc": {
                "count": 3,
                "mtime": 30.0,
                "failures": 0,
                "fail_kind": "",
                "quarantined_at": 0.0,
            }
        }
    }
    merged = merge_checkpoint(existing, incoming)["opencode"]["abc"]
    assert merged["mtime"] == 30.0
    assert merged["failures"] == 0
    assert merged["fail_kind"] == ""
    assert merged["quarantined_at"] == 0.0


def test_should_take_newer_fail_kind_when_merging() -> None:
    existing = {"opencode": {"abc": {"count": 3, "mtime": 10.0, "fail_kind": "transient"}}}
    incoming = {"opencode": {"abc": {"count": 3, "mtime": 10.0, "fail_kind": "permanent"}}}
    merged = merge_checkpoint(existing, incoming)["opencode"]["abc"]
    assert merged["fail_kind"] == "permanent"


def test_should_keep_quarantine_state_when_merging_legacy_int_entry() -> None:
    existing = {"opencode": {"abc": 3}}
    incoming = {"opencode": {"abc": {"count": 3, "mtime": 10.0, "quarantined_at": 99.0}}}
    merged = merge_checkpoint(existing, incoming)["opencode"]["abc"]
    assert merged["quarantined_at"] == 99.0


def test_should_read_quarantine_state_from_checkpoint_entry() -> None:
    entry = CheckpointEntry.from_raw(
        {"count": 3, "mtime": 10.0, "failures": 2, "fail_kind": "permanent", "quarantined_at": 99.0}
    )
    assert entry.failures == 2
    assert entry.fail_kind == "permanent"
    assert entry.quarantined_at == 99.0


def test_should_default_quarantine_state_when_entry_is_legacy() -> None:
    assert CheckpointEntry.from_raw(7) == CheckpointEntry(count=7, mtime=0.0)


# --- aggregate health (the symptom the watchdog reads) -----------------------
# [WHY] a parked session is skipped silently, so nothing raises. Without an
# aggregate reading a source that stopped ingesting looks perfectly healthy.


def test_should_report_full_completeness_when_nothing_is_parked() -> None:
    health = collection_health({"a": {"count": 3, "mtime": 1.0}, "b": {"count": 1, "mtime": 1.0}})
    assert health.total == 2
    assert health.quarantined == 0
    assert health.completeness == 1.0


def test_should_report_degraded_completeness_when_a_session_is_parked() -> None:
    health = collection_health(
        {
            "a": {"count": 3, "mtime": 1.0},
            "b": {
                "count": 1,
                "mtime": 1.0,
                "failures": 1,
                "fail_kind": "permanent",
                "quarantined_at": 99.0,
            },
        }
    )
    assert health.quarantined == 1
    assert health.completeness == 0.5
    assert health.kinds == ("permanent",)


def test_should_list_each_failure_kind_once() -> None:
    health = collection_health(
        {
            "a": {"quarantined_at": 1.0, "fail_kind": "permanent"},
            "b": {"quarantined_at": 2.0, "fail_kind": "permanent"},
            "c": {"quarantined_at": 3.0, "fail_kind": "transient"},
        }
    )
    assert health.kinds == ("permanent", "transient")


def test_should_name_unknown_kind_when_parked_entry_has_none() -> None:
    health = collection_health({"a": {"quarantined_at": 1.0}})
    assert health.kinds == ("unknown",)


def test_should_count_legacy_int_entries_in_the_total() -> None:
    health = collection_health({"a": 3, "b": {"quarantined_at": 1.0, "fail_kind": "permanent"}})
    assert health.total == 1
    assert health.quarantined == 1


def test_should_treat_empty_source_as_complete() -> None:
    assert collection_health({}).completeness == 1.0
