"""Characterization: day_cycle.sh batch scheduling (Week 2, test 1).

day_cycle.sh is a shell orchestrator, so this captures its scheduling contract
by parsing the script text (no DB/execution):

  - the documented pipeline_state flow
  - batch reservation pulls OLDEST pending turns and caps at 50
    (NOTE: header comments still say "10" — stale; the actual query uses
    LIMIT 50, which this test locks in as the real behavior)

If any of these strings change, the scheduling semantics changed and the
refactor must be reviewed.
"""

import pytest

pytestmark = pytest.mark.characterization

STATE_FLOW = "pending → batching → cleaned → scanned → extracted+verified → enriched → embedded"


def _day_cycle_text(project_root) -> str:
    return (project_root / "scripts" / "day_cycle.sh").read_text(encoding="utf-8")


def test_documented_state_flow(project_root):
    assert STATE_FLOW in _day_cycle_text(project_root)


def test_batch_reservation_selects_oldest_pending(project_root):
    text = _day_cycle_text(project_root)
    assert "SELECT id FROM turns WHERE pipeline_state = 'pending'" in text
    assert "ORDER BY created_at ASC LIMIT 50" in text


def test_batch_reservation_transitions_to_batching(project_root):
    text = _day_cycle_text(project_root)
    assert "UPDATE turns SET pipeline_state = 'batching'" in text


def test_in_flight_resume_guard(project_root):
    text = _day_cycle_text(project_root)
    # When turns are already in-flight, the cycle resumes instead of reserving.
    assert '"$IN_FLIGHT" -gt 0' in text


def test_shadow_reprojection_gated_and_failure_ignored(project_root):
    """Phase 3 gate: shadow reprojection runs inside the embed phase but can
    never break the prod cycle (timeout-bounded, failures ignored)."""
    text = _day_cycle_text(project_root)
    assert '--shadow-since "$SHADOW_SINCE"' in text
    assert 'shadow_diff.py" --mode embed' in text
    assert '|| LOG "  Shadow reprojection failed (ignored)"' in text
    assert '|| LOG "  Shadow diff failed (ignored)"' in text
