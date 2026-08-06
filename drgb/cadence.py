"""Cadence learning: infer what "normal" looks like, then notice silence.

A fleet's most dangerous failure is not a loud error — it is a lane that
quietly stops. (Real case, 2026-07-27: an account's sleeve evaluator was
dropped from its tick and nobody noticed for nine days, because nothing
compared expected cadence against actual.)

This module learns each key's interval from its own observation history and
reports which keys are overdue. It never assumes a schedule it has not seen:
a key with too few samples is ``unknown``, never ``healthy``.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

MIN_SAMPLES = 3          # below this, cadence is unknown — not "fine"
OVERDUE_FACTOR = 3.0     # overdue when silence exceeds factor x learned interval
MAX_HISTORY = 200
FUTURE_TOLERANCE_S = 120.0   # matches the ledger's clock-skew tolerance


@dataclass
class CadenceProfile:
    """What a key's rhythm looks like, and whether it is currently keeping it."""

    key: str
    samples: int
    interval_s: float | None          # learned median interval
    jitter_s: float | None            # median absolute deviation
    last_seen: float | None
    silence_s: float | None
    status: str                       # "healthy" | "overdue" | "unknown"
    reason: str
    overdue_factor: float = OVERDUE_FACTOR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CadenceLearner:
    """Learns per-key intervals from observation timestamps."""

    def __init__(self, min_samples: int = MIN_SAMPLES,
                 overdue_factor: float = OVERDUE_FACTOR,
                 max_history: int = MAX_HISTORY):
        if min_samples < 2:
            raise ValueError("min_samples must be >= 2 to infer an interval")
        self.min_samples = int(min_samples)
        self.overdue_factor = float(overdue_factor)
        self.max_history = int(max_history)
        self._history: dict[str, list[float]] = {}

    # ---- ingest ---------------------------------------------------------
    def record(self, key: str, ts: float | None = None) -> None:
        """Record that ``key`` was seen at ``ts`` (defaults to now)."""
        stamp = float(ts if ts is not None else time.time())
        series = self._history.setdefault(str(key), [])
        series.append(stamp)
        series.sort()
        if len(series) > self.max_history:
            del series[: len(series) - self.max_history]

    def observe_all(self, observations: Iterable[Any]) -> int:
        """Record a batch of adapter Observations (or (key, ts) pairs)."""
        count = 0
        for obs in observations:
            key = getattr(obs, "key", None)
            ts = getattr(obs, "ts", None)
            if key is None and isinstance(obs, (tuple, list)) and len(obs) == 2:
                key, ts = obs
            if key is None:
                continue
            self.record(str(key), ts)
            count += 1
        return count

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._history))

    # ---- learn ----------------------------------------------------------
    def profile(self, key: str, now: float | None = None) -> CadenceProfile:
        clock = float(now if now is not None else time.time())
        series = self._history.get(str(key)) or []
        last_seen = series[-1] if series else None
        silence = (clock - last_seen) if last_seen is not None else None

        if len(series) < self.min_samples:
            return CadenceProfile(
                key=key, samples=len(series), interval_s=None, jitter_s=None,
                last_seen=last_seen, silence_s=silence, status="unknown",
                reason=f"insufficient_samples:{len(series)}<{self.min_samples}",
                overdue_factor=self.overdue_factor)

        gaps = [b - a for a, b in zip(series, series[1:]) if b > a]
        if not gaps:
            return CadenceProfile(
                key=key, samples=len(series), interval_s=None, jitter_s=None,
                last_seen=last_seen, silence_s=silence, status="unknown",
                reason="no_positive_gaps", overdue_factor=self.overdue_factor)

        interval = statistics.median(gaps)
        jitter = statistics.median([abs(g - interval) for g in gaps])
        if interval <= 0:
            return CadenceProfile(
                key=key, samples=len(series), interval_s=interval, jitter_s=jitter,
                last_seen=last_seen, silence_s=silence, status="unknown",
                reason="degenerate_interval", overdue_factor=self.overdue_factor)

        # A last-seen in the future means a skewed clock or mis-parsed
        # timestamps. Reporting that as "healthy" would let a genuinely dead
        # lane hide behind bad time, so it is unknown (invariant 3, E3).
        if silence is not None and silence < -FUTURE_TOLERANCE_S:
            return CadenceProfile(
                key=key, samples=len(series), interval_s=interval,
                jitter_s=jitter, last_seen=last_seen, silence_s=silence,
                status="unknown", reason="last_seen_in_future_clock_skew",
                overdue_factor=self.overdue_factor)

        overdue = silence is not None and silence > interval * self.overdue_factor
        return CadenceProfile(
            key=key, samples=len(series), interval_s=interval, jitter_s=jitter,
            last_seen=last_seen, silence_s=silence,
            status="overdue" if overdue else "healthy",
            reason=(f"silent_for_{silence:.0f}s_vs_interval_{interval:.0f}s"
                    if overdue else "within_expected_cadence"),
            overdue_factor=self.overdue_factor)

    def report(self, now: float | None = None) -> dict[str, Any]:
        """Profile every known key and surface the divergences."""
        clock = float(now if now is not None else time.time())
        profiles = [self.profile(k, now=clock) for k in self.keys()]
        return {
            "generated_at": clock,
            "keys": len(profiles),
            "healthy": sum(1 for p in profiles if p.status == "healthy"),
            "overdue": [p.key for p in profiles if p.status == "overdue"],
            "unknown": [p.key for p in profiles if p.status == "unknown"],
            "profiles": [p.to_dict() for p in profiles],
        }
