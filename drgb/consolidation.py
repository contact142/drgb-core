"""Cognitive consolidation: measure what the cheap path can already handle.

Derek's formulation: an agent wakes on an event, a large model reasons, and
over time DRGB routes more of that thinking to a cheaper path — but only for
decision classes where the cheap path has *demonstrably* agreed with the
expensive one. Growth becomes a number: the share of decisions handled below
the big model with no increase in divergence.

This is the same evidence-before-authority rule applied to cognition, so it
inherits the same guards: a class with too few samples is ``unknown`` (never
"cheap is fine"), one disagreement resets consolidation, and a class whose
disagreements are costly can be barred from consolidating at any rate.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

MIN_SAMPLES = 20
AGREEMENT_THRESHOLD = 0.95
MAX_HISTORY = 500


@dataclass
class ClassProfile:
    """One decision class and whether the cheap path has earned it."""

    decision_class: str
    samples: int
    agreements: int
    disagreements: int
    agreement_rate: float | None
    consecutive_agreements: int
    status: str            # "consolidatable" | "not_yet" | "unknown" | "barred"
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConsolidationTracker:
    """Records cheap-vs-expensive decisions and reports what can be routed down."""

    def __init__(self, min_samples: int = MIN_SAMPLES,
                 agreement_threshold: float = AGREEMENT_THRESHOLD,
                 barred_classes: Iterable[str] = (),
                 max_history: int = MAX_HISTORY):
        if not 0.0 < agreement_threshold <= 1.0:
            raise ValueError("agreement_threshold must be in (0, 1]")
        self.min_samples = int(min_samples)
        self.agreement_threshold = float(agreement_threshold)
        self.barred = {str(c) for c in barred_classes}
        self.max_history = int(max_history)
        self._history: dict[str, list[bool]] = {}

    def record(self, decision_class: str, cheap: Any, expensive: Any,
               agree: bool | None = None) -> bool:
        """Record one paired decision. Returns whether they agreed.

        ``agree`` may be supplied when equality is not the right comparison
        (e.g. numeric tolerance); otherwise values are compared directly.
        """
        if agree is None:
            try:
                agree = bool(cheap == expensive)
            except Exception:
                agree = False
        series = self._history.setdefault(str(decision_class), [])
        series.append(bool(agree))
        if len(series) > self.max_history:
            del series[: len(series) - self.max_history]
        return bool(agree)

    def classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._history))

    def profile(self, decision_class: str) -> ClassProfile:
        series = self._history.get(str(decision_class)) or []
        agreements = sum(1 for a in series if a)
        disagreements = len(series) - agreements
        rate = (agreements / len(series)) if series else None

        streak = 0
        for value in reversed(series):
            if not value:
                break
            streak += 1

        if decision_class in self.barred:
            status, reason = "barred", "class_barred_from_consolidation"
        elif len(series) < self.min_samples:
            status = "unknown"
            reason = f"insufficient_samples:{len(series)}<{self.min_samples}"
        elif rate is not None and rate >= self.agreement_threshold:
            status, reason = "consolidatable", f"agreement_{rate:.3f}"
        else:
            status = "not_yet"
            reason = f"agreement_{rate:.3f}<{self.agreement_threshold}"

        return ClassProfile(
            decision_class=str(decision_class), samples=len(series),
            agreements=agreements, disagreements=disagreements,
            agreement_rate=rate, consecutive_agreements=streak,
            status=status, reason=reason)

    def report(self) -> dict[str, Any]:
        """Overall growth metric: what share of decisions can go cheap."""
        profiles = [self.profile(c) for c in self.classes()]
        total = sum(p.samples for p in profiles)
        consolidatable = [p for p in profiles if p.status == "consolidatable"]
        covered = sum(p.samples for p in consolidatable)
        rates = [p.agreement_rate for p in profiles
                 if p.agreement_rate is not None]
        return {
            "classes": len(profiles),
            "decisions": total,
            "consolidatable_classes": [p.decision_class for p in consolidatable],
            "consolidatable_share": (covered / total) if total else None,
            "median_agreement": statistics.median(rates) if rates else None,
            "unknown_classes": [p.decision_class for p in profiles
                                if p.status == "unknown"],
            "barred_classes": sorted(self.barred),
            "profiles": [p.to_dict() for p in profiles],
        }
