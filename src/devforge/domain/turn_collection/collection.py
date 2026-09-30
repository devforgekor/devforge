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

# [WHY] a transient parse failure (lock contention) resolves on its own, so it
# gets a few attempts before parking. PERMANENT and UNKNOWN park immediately.
MAX_TRANSIENT_FAILURES = 3

# [WHY] sqlite3 reports SQLITE_CORRUPT as a bare DatabaseError — it has no
# OperationalError/IntegrityError subclass and no error-code attribute — so the
# message is the only signal available. Matched last, after class name.
_PERMANENT_MARKERS = ("malformed", "not a database", "disk image")
_TRANSIENT_MARKERS = ("locked", "busy")
_TRANSIENT_CLASS_NAMES = ("OperationalError",)


class FailureKind(Enum):
    """How a session's parse failure should be retried."""

    TRANSIENT = "transient"  # may resolve on its own → bounded retries
    PERMANENT = "permanent"  # deterministic, retrying cannot help → park now
    UNKNOWN = "unknown"  # unrecognized → park, never swallow


def classify_session_error(exc: BaseException) -> FailureKind:
    """Classify a session parse failure. Defaults to UNKNOWN.

    [WHY] retrying a deterministic failure wastes a cycle every 3s forever, but
    guessing wrong on an unrecognized error hides a real regression. UNKNOWN
    parks the session and forces the caller to log it with a traceback.
    """
    message = str(exc).lower()
    if any(marker in message for marker in _PERMANENT_MARKERS):
        return FailureKind.PERMANENT
    if type(exc).__name__ in _TRANSIENT_CLASS_NAMES:
        return FailureKind.TRANSIENT
    if any(marker in message for marker in _TRANSIENT_MARKERS):
        return FailureKind.TRANSIENT
    return FailureKind.UNKNOWN


@dataclass(frozen=True)
class CheckpointEntry:
    """Per-session collection position: how many turns are ingested, and the
    source mtime at that point (used to skip unchanged files).

    `failures`/`fail_kind`/`quarantined_at` describe a parked session. They live
    in the file so a collector restart cannot un-park it — an in-memory flag
    comes back closed and the retry loop resumes immediately.
    """

    count: int
    mtime: float
    failures: int = 0
    fail_kind: str = ""
    quarantined_at: float = 0.0

    @classmethod
    def from_raw(cls, entry: Any) -> "CheckpointEntry":
        """Normalize historical checkpoint values (int-only, partial dict, junk)."""
        if isinstance(entry, int) and not isinstance(entry, bool):
            return cls(count=entry, mtime=0.0)
        if isinstance(entry, Mapping):
            return cls(
                count=entry.get("count", 0),
                mtime=entry.get("mtime", 0),
                failures=entry.get("failures", 0),
                fail_kind=entry.get("fail_kind", ""),
                quarantined_at=entry.get("quarantined_at", 0.0),
            )
        # [WHY] an unreadable entry degrades to "start over" — re-insert is safe
        # because rows are guarded by source_message_id pre-filter and
        # ON CONFLICT (conversation_id, seq), so nothing duplicates.
        return cls(count=0, mtime=0.0)


def get_entry(checkpoint: Mapping[str, Any], source: str, session_id: str) -> CheckpointEntry:
    """Read one session's position, tolerating the legacy int-only format."""
    source_entries = checkpoint.get(source, {})
    return CheckpointEntry.from_raw(source_entries.get(session_id, {}))


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _merge_entry(prev: Any, incoming: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge one session entry.

    [WHY] count/mtime only ever move forward, so a replayed older snapshot
    cannot regress ingestion. Quarantine fields are different: a writer that
    supplies them is stating the session's current state and may clear them on
    recovery, so presence of "failures" makes them authoritative. Writers that
    omit them (collect_turns.py) keep whatever the last write left behind.
    """
    prev_map = prev if isinstance(prev, Mapping) else {}
    prev_count = _num(prev_map.get("count", prev))
    explicit = "failures" in incoming
    if explicit:
        failures = _num(incoming.get("failures"))
        fail_kind = incoming.get("fail_kind", "")
        quarantined_at = _num(incoming.get("quarantined_at", 0))
    else:
        failures = max(_num(prev_map.get("failures", 0)), _num(incoming.get("failures", 0)))
        fail_kind = incoming.get("fail_kind") or prev_map.get("fail_kind", "")
        quarantined_at = max(
            _num(prev_map.get("quarantined_at", 0)), _num(incoming.get("quarantined_at", 0))
        )
    merged = {
        "count": max(prev_count, incoming["count"]),
        "mtime": max(_num(prev_map.get("mtime", 0)), incoming["mtime"]),
        "failures": failures,
        "fail_kind": fail_kind,
        "quarantined_at": quarantined_at,
    }
    # Zero-valued keys are dropped to keep the file compact, but a writer that
    # stated the session's state must keep them so the next merge still sees an
    # authoritative write instead of preserving a stale quarantine.
    keep = (
        ("count", "mtime", "failures", "fail_kind", "quarantined_at")
        if explicit
        else ("count", "mtime")
    )
    return {k: v for k, v in merged.items() if v or k in keep}


def merge_checkpoint(
    existing: MutableMapping[str, Any], incoming: Mapping[str, Any]
) -> Dict[str, Any]:
    """Merge incoming session positions into the checkpoint loaded from disk.

    [WHY] two writers share this file and a crash can replay an older snapshot,
    so foreign metadata keys are kept and positions only ever move forward
    (max wins) — otherwise a race would regress ingestion and re-emit turns.
    """
    for source, sessions in incoming.items():
        base = existing.setdefault(source, {})
        for sid, entry in sessions.items():
            if isinstance(entry, str) or sid in METADATA_CHECKPOINT_KEYS:
                continue
            if isinstance(entry, Mapping):
                base[sid] = _merge_entry(base.get(sid, {}), entry)
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
    QUARANTINE = "quarantine"  # parse failed → park, retry only on source change


@dataclass(frozen=True)
class SessionPlan:
    """Decision for one session. `record` is a checkpoint write to apply right
    away; for INGEST the caller records it after the insert (count depends on
    how many rows the database accepts)."""

    action: SessionAction
    record: Optional[CheckpointEntry]
    new_turns: List[Any]


def is_quarantined(prev: CheckpointEntry) -> bool:
    """True when the session is parked after a parse failure."""
    return prev.quarantined_at > 0


def should_skip_parse(prev: CheckpointEntry, current_mtime: float) -> bool:
    """True when the source is untouched since the last ingest.

    [WHY] most poll cycles see no change — the parse itself is the expensive
    part, so the check runs before any file/sqlite read. A quarantined session
    is parked until its source advances, which is the only signal that whatever
    broke it (a corrupt index, a lock) has been fixed.
    """
    if current_mtime == prev.mtime and prev.count > 0:
        return True
    return is_quarantined(prev) and current_mtime <= prev.mtime


def plan_session(
    prev: CheckpointEntry,
    current_mtime: float,
    parsed: Optional[Sequence[Any]],
) -> SessionPlan:
    """Decide whether a session needs parsing, and which turns are new."""
    if should_skip_parse(prev, current_mtime):
        return SessionPlan(SessionAction.UNCHANGED, None, [])

    if not parsed or len(parsed) <= prev.count:
        # Covers None/empty and "file touched but shrank or stayed equal".
        return SessionPlan(SessionAction.NO_NEW, CheckpointEntry(prev.count, current_mtime), [])

    return SessionPlan(SessionAction.INGEST, None, list(parsed[prev.count :]))


def plan_failure(
    prev: CheckpointEntry,
    current_mtime: float,
    exc: BaseException,
    now: float,
) -> SessionPlan:
    """Decide what to do about a failed parse.

    Deterministic failures park at once. Transient ones get
    MAX_TRANSIENT_FAILURES attempts. Either way the record advances so the
    checkpoint persists the decision and the next cycle sees it.
    """
    kind = classify_session_error(exc)
    failures = prev.failures + 1
    park = kind is not FailureKind.TRANSIENT or failures >= MAX_TRANSIENT_FAILURES
    record = CheckpointEntry(
        count=prev.count,
        mtime=current_mtime,
        failures=failures,
        fail_kind=kind.value,
        quarantined_at=now if park else 0.0,
    )
    return SessionPlan(SessionAction.QUARANTINE if park else SessionAction.NO_NEW, record, [])


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


@dataclass(frozen=True)
class CollectionHealth:
    """Ingest state aggregated across one source's sessions."""

    total: int
    quarantined: int
    kinds: Tuple[str, ...]

    @property
    def completeness(self) -> float:
        """Fraction of known sessions that are still collectible."""
        if self.total == 0:
            return 1.0
        return (self.total - self.quarantined) / self.total


def collection_health(entries: Mapping[str, Any]) -> CollectionHealth:
    """Summarize a source's checkpoint entries.

    [WHY] a parked session is silent — the collector skips it without raising —
    so a source that has quietly stopped ingesting looks healthy to every
    liveness check. This is the symptom-level reading the watchdog needs.
    """
    total = 0
    quarantined = 0
    kinds: List[str] = []
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            continue
        total += 1
        if entry.get("quarantined_at"):
            quarantined += 1
            kind = entry.get("fail_kind") or "unknown"
            if kind not in kinds:
                kinds.append(kind)
    return CollectionHealth(total=total, quarantined=quarantined, kinds=tuple(kinds))


def truncate_turn(turn: Mapping[str, Any]) -> Dict[str, str]:
    """Apply the stored field limits. Length caps are domain policy, not SQL."""
    return {
        "user_turn": turn.get("user_turn", "")[:MAX_USER_TURN_CHARS],
        "thinking": (turn.get("thinking") or "")[:MAX_THINKING_CHARS],
        "text": (turn.get("text") or "")[:MAX_TEXT_CHARS],
    }
