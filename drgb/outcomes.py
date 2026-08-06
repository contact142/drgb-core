"""Grading orchestration decisions against observable outcomes.

A crossing that is never graded is not evidence — it is a log line. This turns
observed remote state into ``success`` / ``failure`` / ``neutral`` per lane.

The grading rule is deliberately conservative and asymmetric:

  * ``failure``  — a definite, observed degradation (failed units, a lane's
    expected work absent, a port that was listening and is not).
  * ``success``  — the lane's health signal is present AND matches the
    baseline this lane has previously established.
  * ``neutral``  — anything else: no signal, first sight, an unparsable
    answer, or a probe that did not run. Never counted as success.

Neutral outcomes still count toward the evidence threshold (something was
observed) but never toward the win rate, so a lane cannot earn authority by
being repeatedly unobservable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SUCCESS = "success"
FAILURE = "failure"
NEUTRAL = "neutral"


@dataclass
class Grade:
    """One lane's graded outcome, with the evidence that produced it."""

    lane: str
    outcome: str
    reason: str
    signal: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OutcomeGrader:
    """Grades lanes against a per-lane baseline learned from observation."""

    def __init__(self, baselines: dict[str, dict[str, Any]] | None = None,
                 tolerance: float = 0.25):
        if not 0 <= tolerance < 1:
            raise ValueError("tolerance must be in [0, 1)")
        self.baselines: dict[str, dict[str, Any]] = dict(baselines or {})
        self.tolerance = float(tolerance)

    # ---- baselines ------------------------------------------------------
    def baseline_for(self, lane: str) -> dict[str, Any] | None:
        return self.baselines.get(lane)

    def update_baseline(self, lane: str, lines: int) -> None:
        """Record what 'normal' looks like. Only healthy observations should
        update a baseline; a degraded reading must not become the new normal."""
        current = self.baselines.setdefault(lane, {})
        seen = int(current.get("observations", 0)) + 1
        previous = float(current.get("lines", lines))
        # slow-moving average so one busy cycle cannot redefine normal
        current["lines"] = previous + (lines - previous) / min(seen, 10)
        current["observations"] = seen

    # ---- grading --------------------------------------------------------
    def grade(self, lane: str, *, observed: bool, lines: int | None,
              failed_units: int | None = None) -> Grade:
        if failed_units is not None and failed_units > 0:
            return Grade(lane, FAILURE, f"failed_units:{failed_units}",
                         {"failed_units": failed_units})
        if not observed or lines is None:
            return Grade(lane, NEUTRAL, "not_observed",
                         {"observed": observed})

        baseline = self.baseline_for(lane)
        if baseline is None or not baseline.get("observations"):
            self.update_baseline(lane, lines)
            return Grade(lane, NEUTRAL, "first_sight_establishing_baseline",
                         {"lines": lines})

        expected = float(baseline["lines"])
        if expected <= 0:
            self.update_baseline(lane, lines)
            return Grade(lane, NEUTRAL, "degenerate_baseline", {"lines": lines})

        drop = (expected - lines) / expected
        if drop > self.tolerance:
            # A lane's work vanishing is the failure this system exists to
            # notice. Do NOT fold it into the baseline.
            return Grade(lane, FAILURE,
                         f"work_vanished:{lines}<{expected:.1f}",
                         {"lines": lines, "expected": expected, "drop": drop})

        self.update_baseline(lane, lines)
        return Grade(lane, SUCCESS, f"within_baseline:{lines}~{expected:.1f}",
                     {"lines": lines, "expected": expected})

    def state(self) -> dict[str, Any]:
        return {"tolerance": self.tolerance, "baselines": self.baselines}
