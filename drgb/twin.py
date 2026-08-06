"""Event-gated shadow evaluation for the Dual Reality Guardian Bridge.

The twin is deliberately not a continuous monitor.  :class:`EventGate`
decides when an observation is worth evaluating, and :class:`TwinRunner`
keeps the shadow result entirely on the evidence side of the bridge.  In
particular, ``observe`` contains gate, shadow, comparison, and ledger errors:
the live caller never depends on the twin completing successfully.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from numbers import Number
from typing import Any, Callable, Iterable

from drgb.ledger import Ledger


def _exception_text(prefix: str, exc: Exception) -> str:
    detail = str(exc)
    suffix = f":{detail}" if detail else ""
    return f"{prefix}:{type(exc).__name__}{suffix}"


def _append_error(current: str | None, new: str) -> str:
    if current:
        return f"{current}; {new}"
    return new


def _numeric(value: Any) -> bool:
    """Return whether ``value`` is suitable for numeric divergence math."""
    return isinstance(value, Number) and not isinstance(value, bool)


@dataclass(init=False)
class EventGate:
    """Decide whether an observation is a declared evaluation event.

    ``event_names`` is matched against the observation's ``event`` (or
    ``event_name``) field.  Numeric deviation uses ``signal_key`` and
    ``threshold``.  The first signal is remembered when a real event fires;
    it does not itself create an event.  ``clock`` is injectable so heartbeat
    behavior can be tested without sleeping.

    A few descriptive aliases are accepted for the constructor arguments
    (``allowed_events``, ``deviation_threshold``, and ``signal_field``) so
    adapters can use their existing vocabulary without changing gate
    semantics.
    """

    event_names: frozenset[str]
    signal_key: str | None
    threshold: float | None
    max_interval_s: float | None
    clock: Callable[[], float]
    event_key: str
    _last_evaluated_at: float = field(init=False, repr=False, compare=False)
    _last_signal: float | None = field(default=None, init=False,
                                        repr=False, compare=False)

    def __init__(
        self,
        event_names: Iterable[str] | None = None,
        signal_key: str | None = None,
        threshold: float | None = None,
        max_interval_s: float | None = None,
        clock: Callable[[], float] | None = None,
        *,
        allowed_events: Iterable[str] | None = None,
        event_allowlist: Iterable[str] | None = None,
        events: Iterable[str] | None = None,
        deviation_threshold: float | None = None,
        signal_threshold: float | None = None,
        signal: str | None = None,
        signal_field: str | None = None,
        numeric_signal_key: str | None = None,
        event_key: str | None = None,
        event_field: str | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        declared = event_names
        if declared is None:
            declared = allowed_events
        if declared is None:
            declared = event_allowlist
        if declared is None:
            declared = events

        resolved_signal = signal_key
        if resolved_signal is None:
            resolved_signal = signal
        if resolved_signal is None:
            resolved_signal = signal_field
        if resolved_signal is None:
            resolved_signal = numeric_signal_key

        resolved_threshold = threshold
        if resolved_threshold is None:
            resolved_threshold = deviation_threshold
        if resolved_threshold is None:
            resolved_threshold = signal_threshold

        resolved_clock = clock or now_fn or time.time
        if not callable(resolved_clock):
            raise TypeError("clock must be callable")

        self.event_names = frozenset(str(name) for name in (declared or ()))
        self.signal_key = resolved_signal
        self.threshold = (None if resolved_threshold is None
                          else float(resolved_threshold))
        self.max_interval_s = (None if max_interval_s is None
                               else float(max_interval_s))
        self.clock = resolved_clock
        self.event_key = event_key or event_field or "event"

        if self.threshold is not None and self.threshold < 0:
            raise ValueError("threshold must be >= 0")
        if self.max_interval_s is not None and self.max_interval_s < 0:
            raise ValueError("max_interval_s must be >= 0")

        # A threshold without an explicitly named signal uses the conventional
        # ``signal`` observation field.  Gates without a threshold do not need
        # a signal field at all.
        if self.threshold is not None and self.signal_key is None:
            self.signal_key = "signal"

        self._last_evaluated_at = float(self.clock())
        self._last_signal = None

    @property
    def allowed_events(self) -> frozenset[str]:
        """Alias for the declared event-name allowlist."""
        return self.event_names

    @property
    def deviation_threshold(self) -> float | None:
        """Alias for :attr:`threshold`."""
        return self.threshold

    @property
    def last_evaluated_at(self) -> float:
        return self._last_evaluated_at

    @property
    def last_evaluated_value(self) -> float | None:
        return self._last_signal

    def _event_name(self, obs: dict[str, Any]) -> Any:
        for key in (self.event_key, "event", "event_name"):
            try:
                if key in obs and obs[key] is not None:
                    return obs[key]
            except Exception:
                return None
        return None

    def _signal_value(self, obs: dict[str, Any]) -> float | None:
        if self.signal_key is None:
            return None
        try:
            value = obs.get(self.signal_key)
        except Exception:
            return None
        if not _numeric(value):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    def should_fire(self, obs: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(True, reason)`` only for a declared event.

        The state is advanced only when this method returns ``True``.  Thus a
        quiet observation cannot move the heartbeat or the reference signal.
        Force and heartbeat are checked before ordinary event triggers so a
        consolidated runner can recognize their mandatory bypass semantics.
        """
        now = float(self.clock())
        signal_value = self._signal_value(obs)

        try:
            forced = obs.get("force") is True
        except Exception:
            forced = False

        event_name = self._event_name(obs)
        declared = False
        if event_name is not None:
            try:
                declared = str(event_name) in self.event_names
            except Exception:
                declared = False

        heartbeat = (
            self.max_interval_s is not None
            and (now - self._last_evaluated_at) > self.max_interval_s
        )

        threshold_breach = False
        if (self.threshold is not None and signal_value is not None
                and self._last_signal is not None):
            threshold_breach = (
                abs(signal_value - self._last_signal) > self.threshold
            )

        if forced:
            reason = "force"
        elif heartbeat:
            reason = "heartbeat"
        elif declared:
            reason = "declared_event"
        elif threshold_breach:
            reason = "threshold_breach"
        else:
            return False, "not_event"

        self._last_evaluated_at = now
        if signal_value is not None:
            self._last_signal = signal_value
        return True, reason


@dataclass
class TwinResult:
    """The shadow result; it never grants or blocks live authority."""

    fired: bool
    reason: str
    shadow_value: Any = None
    live_value: Any = None
    diverged: bool | None = None
    delta: float | None = None
    error: str | None = None
    pattern_key: str | None = None
    consolidated: bool = False


@dataclass
class _PatternState:
    consecutive_agreements: int = 0
    consolidated: bool = False
    events_since_fire: int = 0
    last_shadow_value: Any = None   # for prediction-error re-engagement

    def reset(self) -> None:
        self.consecutive_agreements = 0
        self.consolidated = False
        self.events_since_fire = 0
        # last_shadow_value is deliberately retained: it is the reference a
        # drifting live value is measured against after de-consolidation.


class TwinRunner:
    """Run a shadow function only when the event gate permits it.

    Every path through :meth:`observe` is exception-contained.  A broken gate,
    shadow function, comparison, or optional ledger can reduce evidence, but
    it cannot change or interrupt the caller's live value/path.
    """

    def __init__(
        self,
        gate: EventGate,
        shadow_fn: Callable[[dict], Any],
        ledger: Ledger | None = None,
        consolidation_threshold: int = 5,
        consolidation_backoff: int = 4,
        tolerance: float = 1e-9,
    ) -> None:
        if consolidation_threshold < 1:
            raise ValueError("consolidation_threshold must be >= 1")
        if consolidation_backoff < 1:
            raise ValueError("consolidation_backoff must be >= 1")
        if tolerance < 0:
            raise ValueError("tolerance must be >= 0")

        self.gate = gate
        self.shadow_fn = shadow_fn
        self.ledger = ledger
        self.consolidation_threshold = int(consolidation_threshold)
        self.consolidation_backoff = int(consolidation_backoff)
        self.tolerance = float(tolerance)

        self._state: dict[str, _PatternState] = {}
        self._observations = 0
        self._fired = 0
        self._skipped = 0
        self._divergences = 0
        self._errors = 0

    def _state_for(self, pattern_key: str | None,
                   create: bool = False) -> _PatternState | None:
        if pattern_key is None:
            return None
        try:
            if create:
                return self._state.setdefault(pattern_key, _PatternState())
            return self._state.get(pattern_key)
        except Exception:
            # The public contract uses string keys.  Treat an accidental
            # unhashable adapter key as unavailable evidence, never as a live
            # path failure.
            return None

    def _compare(self, shadow_value: Any, live_value: Any,
                 live_provided: bool) -> tuple[bool | None, float | None,
                                               str | None]:
        if not live_provided:
            return None, None, None

        if _numeric(shadow_value) and _numeric(live_value):
            try:
                raw_delta = abs(shadow_value - live_value)
                delta = float(raw_delta)
                if math.isnan(delta):
                    # Equal infinities have zero difference; NaNs and unlike
                    # infinities are divergence rather than agreement.
                    if shadow_value == live_value:
                        return False, 0.0, None
                    return True, None, None
                if not math.isfinite(delta):
                    return True, delta, None
                return delta > self.tolerance, delta, None
            except Exception as exc:
                return None, None, _exception_text("comparison_error", exc)

        try:
            equal = shadow_value == live_value
            return not bool(equal), None, None
        except Exception as exc:
            return None, None, _exception_text("comparison_error", exc)

    def _update_consolidation(self, pattern_key: str | None,
                              diverged: bool | None,
                              shadow_value: Any = None) -> bool:
        state = self._state_for(pattern_key, create=True)
        if state is None:
            return False
        # Remember the latest shadow reading so a later live value can be
        # checked for prediction error while the twin is backing off.
        if shadow_value is not None:
            state.last_shadow_value = shadow_value

        if diverged is False:
            state.consecutive_agreements += 1
            if state.consecutive_agreements >= self.consolidation_threshold:
                state.consolidated = True
            return state.consolidated

        # Unknown outcomes (including a shadow error or no live value) are not
        # agreements.  They therefore break the consecutive streak just as a
        # known divergence does; this keeps compute savings evidence-backed.
        state.reset()
        return False

    def _prediction_error(self, pattern_key: str | None, live_value: Any) -> bool:
        """True when the live value departs from this skill's last shadow value.

        Consolidation lets the twin skip evaluations, which would otherwise
        make a *drifting* skill invisible: divergence can only be measured on
        a firing. Prediction error therefore forces a fire — the same
        principle that gates the twin in the first place (evaluate on
        surprise, coast otherwise).
        """
        if live_value is None:
            return False
        state = self._state_for(pattern_key)
        if state is None or state.last_shadow_value is None:
            return False
        diverged, _delta, _err = self._compare(state.last_shadow_value,
                                              live_value, True)
        return bool(diverged)

    def _should_skip(self, pattern_key: str | None, reason: str,
                     live_value: Any = None) -> bool:
        state = self._state_for(pattern_key)
        if state is None or not state.consolidated:
            return False
        if reason in {"force", "heartbeat"}:
            return False
        if self._prediction_error(pattern_key, live_value):
            state.events_since_fire = 0
            return False  # surprise re-engages the twin immediately

        state.events_since_fire += 1
        if state.events_since_fire < self.consolidation_backoff:
            return True
        state.events_since_fire = 0
        return False

    @staticmethod
    def _ledger_value(value: Any) -> Any:
        """Keep ordinary values intact and make unusual values appendable."""
        try:
            json.dumps(value)
            return value
        except Exception:
            try:
                return repr(value)
            except Exception:
                return f"<{type(value).__name__}>"

    def _append_ledger(self, result: TwinResult) -> str | None:
        if self.ledger is None:
            return None
        lane = result.pattern_key or "twin"
        originating_agent = result.pattern_key or "twin"
        try:
            self.ledger.append(
                kind="crossing",
                lane=lane,
                agent="twin",
                originating_agent=originating_agent,
                originating_envelope_digest="unavailable",
                crossing_id=uuid.uuid4().hex,
                scenario="shadow_evaluation",
                reason=result.reason,
                pattern_key=result.pattern_key,
                shadow_value=self._ledger_value(result.shadow_value),
                live_value=self._ledger_value(result.live_value),
                diverged=result.diverged,
                delta=result.delta,
                error=result.error,
            )
        except Exception as exc:
            return _exception_text("ledger_append_failed", exc)
        return None

    def _state_consolidated(self, pattern_key: str | None) -> bool:
        state = self._state_for(pattern_key)
        return bool(state and state.consolidated)

    def observe(self, obs: dict, live_value: Any = None,
                pattern_key: str | None = None) -> TwinResult:
        """Observe one input and, only when gated, evaluate its shadow.

        This method intentionally has no exception path.  The live caller can
        ignore the returned evidence when it is unavailable or erroneous, and
        continue on its own already-determined live path.
        """
        self._observations += 1

        try:
            fired, reason = self.gate.should_fire(obs)
            reason = str(reason)
            if not fired:
                self._skipped += 1
                return TwinResult(
                    fired=False,
                    reason=reason,
                    live_value=live_value,
                    pattern_key=pattern_key,
                    consolidated=self._state_consolidated(pattern_key),
                )
        except Exception as exc:
            self._skipped += 1
            self._errors += 1
            return TwinResult(
                fired=False,
                reason="gate_error",
                live_value=live_value,
                error=_exception_text("gate_error", exc),
                pattern_key=pattern_key,
                consolidated=self._state_consolidated(pattern_key),
            )

        try:
            if self._should_skip(pattern_key, reason, live_value):
                self._skipped += 1
                return TwinResult(
                    fired=False,
                    reason="consolidated_backoff",
                    live_value=live_value,
                    pattern_key=pattern_key,
                    consolidated=True,
                )
        except Exception as exc:
            # The gate has fired, but a malformed consolidation key/state must
            # still never escape into the live path.
            self._skipped += 1
            self._errors += 1
            return TwinResult(
                fired=False,
                reason="consolidation_error",
                live_value=live_value,
                error=_exception_text("consolidation_error", exc),
                pattern_key=pattern_key,
                consolidated=self._state_consolidated(pattern_key),
            )

        self._fired += 1
        shadow_value = None
        error: str | None = None
        shadow_failed = False
        try:
            shadow_value = self.shadow_fn(obs)
        except Exception as exc:
            shadow_failed = True
            error = _exception_text("shadow_error", exc)

        live_provided = live_value is not None
        if shadow_failed:
            # There is no shadow value to compare.  A shadow exception is
            # explicitly unknown, never a synthetic divergence against None.
            diverged, delta, comparison_error = None, None, None
        else:
            try:
                diverged, delta, comparison_error = self._compare(
                    shadow_value, live_value, live_provided)
            except Exception as exc:
                diverged, delta = None, None
                comparison_error = _exception_text("comparison_error", exc)
        if comparison_error:
            error = _append_error(error, comparison_error)

        if diverged is True:
            self._divergences += 1
        if error:
            self._errors += 1

        try:
            consolidated = self._update_consolidation(pattern_key, diverged,
                                                      shadow_value)
        except Exception as exc:
            self._errors += 1
            error = _append_error(error, _exception_text("consolidation_error", exc))
            consolidated = False

        result = TwinResult(
            fired=True,
            reason=reason,
            shadow_value=shadow_value,
            live_value=live_value,
            diverged=diverged,
            delta=delta,
            error=error,
            pattern_key=pattern_key,
            consolidated=consolidated,
        )

        ledger_error = self._append_ledger(result)
        if ledger_error:
            result.error = _append_error(result.error, ledger_error)
            self._errors += 1
        return result

    def consolidation_state(self) -> dict[str, dict[str, Any]]:
        """Return a detached snapshot of per-pattern consolidation state."""
        return {
            key: {
                "consecutive_agreements": state.consecutive_agreements,
                "consolidated": state.consolidated,
                "events_since_fire": state.events_since_fire,
            }
            for key, state in self._state.items()
        }

    def metrics(self) -> dict[str, int | float]:
        """Return counters for gating, shadow evaluations, and agreement."""
        consolidated_keys = sum(
            1 for state in self._state.values() if state.consolidated
        )
        ratio = (self._fired / self._observations
                 if self._observations else 0.0)
        return {
            "observations": self._observations,
            "fired": self._fired,
            "skipped": self._skipped,
            "divergences": self._divergences,
            "errors": self._errors,
            "consolidated_keys": consolidated_keys,
            "evaluation_ratio": ratio,
        }
