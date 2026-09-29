#!/usr/bin/env python3.12
# Status: experimental
# Path: scripts/turn_watcher.py → tests/unit/domain/turn_collection/
"""Turn collection rules: checkpoint merge, session increment plan, turn dedup.

Pure domain — no filesystem, no SQL, no clock. Mirrors the decision logic that
lives in scripts/turn_watcher.py so the collector can delegate to it without
changing what it writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

MAX_USER_TURN_CHARS = 8000
MAX_THINKING_CHARS = 4000
MAX_TEXT_CHARS = 8000

# [WHY] collect_turns.py shares collect_checkpoint.json and writes string-valued
# metadata alongside per-session dicts; those keys must survive a merge.
METADATA_CHECKPOINT_KEYS = ("last_ingested_at",)


@dataclass(frozen=True)
class CheckpointEntry:
    """Per-session collection position: how many turns are ingested, and the
    source mtime at that point (used to skip unchanged files)."""

    count: int
    mtime: float

    @classmethod
    def from_raw(cls, entry: Any) -> "CheckpointEntry":
        """Normalize historical checkpoint values (int-only, partial dict, junk)."""
        if isinstance(entry, int) and not isinstance(entry, bool):
            return cls(count=entry, mtime=0.0)
        if isinstance(entry, Mapping):
            return cls(count=entry.get("count", 0), mtime=entry.get("mtime", 0))
        # [WHY] an unreadable entry degrades to "start over" — re-insert is safe
        # because rows are guarded by source_message_id pre-filter and
        # ON CONFLICT (conversation_id, seq), so nothing duplicates.
        return cls(count=0, mtime=0.0)


def get_entry(checkpoint: Mapping[str, Any], source: str, session_id: str) -> CheckpointEntry:
    """Read one session's position, tolerating the legacy int-only format."""
    source_entries = checkpoint.get(source, {})
    return CheckpointEntry.from_raw(source_entries.get(session_id, {}))


def merge_checkpoint(
    existing: MutableMapping[str, Any], incoming: Mapping[str, Any]
) -> Dict[str, Any]:
    """Merge incoming session positions into the checkpoint loaded from disk.

    [WHY] two writers share this file and a crash can replay an older snapshot,
    so foreign metadata keys are kept and count/mtime only ever move forward
    (max wins) — otherwise a race would regress ingestion and re-emit turns.
    """
    for source, sessions in incoming.items():
        base = existing.setdefault(source, {})
        for sid, entry in sessions.items():
            if isinstance(entry, str) or sid in METADATA_CHECKPOINT_KEYS:
                continue
            if isinstance(entry, Mapping):
                prev = base.get(sid, {})
                if isinstance(prev, Mapping):
                    base[sid] = {
                        "count": max(prev.get("count", 0), entry["count"]),
                        "mtime": max(prev.get("mtime", 0), entry["mtime"]),
                    }
                else:
                    base[sid] = {
                        "count": max(prev if isinstance(prev, int) else 0, entry["count"]),
                        "mtime": entry["mtime"],
                    }
            else:
                # integer count (backward compat)
                current = base.get(sid, 0)
                base[sid] = max(current if isinstance(current, int) else 0, entry)
    return dict(existing)


class SessionAction(Enum):
    """What one poll cycle should do with a session."""

    UNCHANGED = "unchanged"  # source untouched → skip parsing entirely
    NO_NEW = "no_new"  # parsed, nothing new → refresh mtime only
    INGEST = "ingest"  # parsed turns beyond the checkpoint → insert


@dataclass(frozen=True)
class SessionPlan:
    """Decision for one session. `record` is a checkpoint write to apply right
    away; for INGEST the caller records it after the insert (count depends on
    how many rows the database accepts)."""

    action: SessionAction
    record: Optional[CheckpointEntry]
    new_turns: List[Any]


def plan_session(
    prev: CheckpointEntry,
    current_mtime: float,
    parsed: Optional[Sequence[Any]],
) -> SessionPlan:
    """Decide whether a session needs parsing, and which turns are new."""
    if current_mtime == prev.mtime and prev.count > 0:
        return SessionPlan(SessionAction.UNCHANGED, None, [])

    if not parsed or len(parsed) <= prev.count:
        # Covers None/empty and "file touched but shrank or stayed equal".
        return SessionPlan(SessionAction.NO_NEW, CheckpointEntry(prev.count, current_mtime), [])

    return SessionPlan(SessionAction.INGEST, None, list(parsed[prev.count :]))


def filter_unseen(
    turns: Sequence[Mapping[str, Any]], existing_ids: Iterable[str]
) -> List[Tuple[int, Mapping[str, Any]]]:
    """Drop turns already stored, and repeats inside the same batch.

    Returns (original_index, turn) pairs so seq keeps its position in the
    source file. Turns without a source_message_id are always kept — they
    cannot be identified, and the insert path de-duplicates them by
    (conversation_id, seq).
    """
    seen = set(existing_ids)
    kept: List[Tuple[int, Mapping[str, Any]]] = []
    for index, turn in enumerate(turns):
        smid = turn.get("source_message_id", "")
        if smid and smid in seen:
            continue
        if smid:
            seen.add(smid)
        kept.append((index, turn))
    return kept


def truncate_turn(turn: Mapping[str, Any]) -> Dict[str, str]:
    """Apply the stored field limits. Length caps are domain policy, not SQL."""
    return {
        "user_turn": turn.get("user_turn", "")[:MAX_USER_TURN_CHARS],
        "thinking": (turn.get("thinking") or "")[:MAX_THINKING_CHARS],
        "text": (turn.get("text") or "")[:MAX_TEXT_CHARS],
    }
