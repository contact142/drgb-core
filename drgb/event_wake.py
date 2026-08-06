"""Event-wake: replace polling with pings.

A timer wakes an agent on a schedule whether or not anything happened. An
event wake fires only when a source actually changed — so the expensive path
(model reasoning, planning) runs on *news* rather than on the clock.

The check itself must be far cheaper than the work it gates: this uses
``stat`` (size + mtime) rather than reading or parsing, so a no-change poll
costs microseconds.

Invariant 3 applies: a source that cannot be stat'ed is ``unknown`` and
DOES wake the agent — never assume "nothing happened" because you could not
look. A heartbeat also guarantees a wake even in total silence.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_HEARTBEAT_S = 6 * 3600.0


@dataclass
class WakeDecision:
    """Why (or why not) the expensive path should run right now."""

    wake: bool
    reason: str
    changed_sources: list[str] = field(default_factory=list)
    unknown_sources: list[str] = field(default_factory=list)
    checked: int = 0
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventWaker:
    """Decides whether an agent should wake, from cheap source fingerprints."""

    def __init__(self, sources: Iterable[str | Path],
                 heartbeat_s: float | None = DEFAULT_HEARTBEAT_S):
        self.sources = [Path(s) for s in sources]
        self.heartbeat_s = heartbeat_s
        self._fingerprints: dict[str, tuple[int, float] | None] = {}
        self._last_wake: float | None = None
        self.timer_equivalent_wakes = 0     # for the polling comparison
        self.event_wakes = 0

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, float] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime)

    def check(self, now: float | None = None) -> WakeDecision:
        """One cheap poll. Wakes only on change, unknown source, or heartbeat."""
        clock = float(now if now is not None else time.time())
        self.timer_equivalent_wakes += 1     # a timer would have woken here

        changed: list[str] = []
        unknown: list[str] = []
        for path in self.sources:
            key = str(path)
            current = self._fingerprint(path)
            previous = self._fingerprints.get(key, "unseen")
            self._fingerprints[key] = current
            if current is None:
                # cannot look => cannot claim nothing happened
                unknown.append(key)
            elif previous == "unseen":
                changed.append(key)          # first sight is news
            elif current != previous:
                changed.append(key)

        heartbeat_due = (
            self.heartbeat_s is not None
            and (self._last_wake is None
                 or (clock - self._last_wake) >= self.heartbeat_s))

        if changed:
            reason = "source_changed"
        elif unknown:
            reason = "source_unknown"
        elif heartbeat_due:
            reason = "heartbeat"
        else:
            return WakeDecision(False, "no_change", [], unknown,
                                len(self.sources), clock)

        self._last_wake = clock
        self.event_wakes += 1
        return WakeDecision(True, reason, changed, unknown,
                            len(self.sources), clock)

    def savings(self) -> dict[str, Any]:
        """How much work the event gate avoided versus a timer."""
        ratio = (self.timer_equivalent_wakes / self.event_wakes
                 if self.event_wakes else None)
        return {
            "timer_equivalent_wakes": self.timer_equivalent_wakes,
            "event_wakes": self.event_wakes,
            "avoided": self.timer_equivalent_wakes - self.event_wakes,
            "reduction_ratio": ratio,
        }
