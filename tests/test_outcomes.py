"""5.4 — grading orchestration decisions against observable outcomes."""

from __future__ import annotations

import pytest

from drgb.outcomes import FAILURE, NEUTRAL, SUCCESS, OutcomeGrader


def test_first_sight_is_neutral_not_success():
    """A lane cannot earn credit for a cycle that merely established what
    normal looks like."""
    grader = OutcomeGrader()
    grade = grader.grade("lane", observed=True, lines=40)
    assert grade.outcome == NEUTRAL
    assert grade.reason == "first_sight_establishing_baseline"


def test_steady_state_grades_success():
    grader = OutcomeGrader()
    grader.grade("lane", observed=True, lines=40)          # baseline
    grade = grader.grade("lane", observed=True, lines=41)
    assert grade.outcome == SUCCESS


def test_work_vanishing_grades_failure():
    """The failure this system exists to notice: a lane's output disappearing."""
    grader = OutcomeGrader(tolerance=0.25)
    for _ in range(5):
        grader.grade("lane", observed=True, lines=40)
    grade = grader.grade("lane", observed=True, lines=10)
    assert grade.outcome == FAILURE
    assert "work_vanished" in grade.reason


def test_a_degraded_reading_does_not_become_the_new_normal():
    grader = OutcomeGrader(tolerance=0.25)
    for _ in range(5):
        grader.grade("lane", observed=True, lines=40)
    before = grader.baseline_for("lane")["lines"]
    grader.grade("lane", observed=True, lines=5)           # failure
    after = grader.baseline_for("lane")["lines"]
    assert after == before, "a failure must not redefine normal"


def test_failed_units_grade_failure_regardless_of_volume():
    grader = OutcomeGrader()
    grade = grader.grade("lane", observed=True, lines=1000, failed_units=2)
    assert grade.outcome == FAILURE and "failed_units" in grade.reason


def test_unobserved_is_neutral_never_success():
    """Invariant 3 at the grading layer: not looking is not a good outcome."""
    grader = OutcomeGrader()
    assert grader.grade("lane", observed=False, lines=None).outcome == NEUTRAL
    assert grader.grade("lane", observed=True, lines=None).outcome == NEUTRAL


def test_growth_is_not_penalised():
    grader = OutcomeGrader(tolerance=0.25)
    grader.grade("lane", observed=True, lines=40)
    assert grader.grade("lane", observed=True, lines=400).outcome == SUCCESS


def test_baseline_moves_slowly_so_one_cycle_cannot_redefine_normal():
    grader = OutcomeGrader()
    grader.grade("lane", observed=True, lines=100)
    for _ in range(3):
        grader.grade("lane", observed=True, lines=100)
    baseline_before = grader.baseline_for("lane")["lines"]
    grader.grade("lane", observed=True, lines=200)
    moved = grader.baseline_for("lane")["lines"] - baseline_before
    assert 0 < moved < 100, "baseline must not jump to a single new reading"


def test_repeated_unobservability_cannot_earn_authority():
    """Neutrals count as evidence that something was checked, but never as
    wins — so a lane that is always unobservable never builds a win rate."""
    from drgb.ledger import Ledger
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    ledger = Ledger(tmp / "l.jsonl")
    grader = OutcomeGrader()
    for i in range(30):
        grade = grader.grade("dark_lane", observed=False, lines=None)
        ledger.record_crossing(lane="dark_lane", agent="obs", scenario="observe",
                               crossing_id=f"c{i}", granted_multiplier=0.0,
                               originating_agent="a",
                               originating_envelope_digest="d")
        ledger.grade(crossing_id=f"c{i}", lane="dark_lane", agent="obs",
                     outcome=grade.outcome, originating_agent="a",
                     originating_envelope_digest="d")
    trust = ledger.trust("dark_lane", originating_agent="a")
    assert trust["n_graded"] == 30
    assert trust["successes"] == 0
    assert trust["win_rate"] is None      # nothing decided -> no rate at all


def test_invalid_tolerance_rejected():
    with pytest.raises(ValueError):
        OutcomeGrader(tolerance=1.0)
    with pytest.raises(ValueError):
        OutcomeGrader(tolerance=-0.1)
