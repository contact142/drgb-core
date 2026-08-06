"""The bridge: converts evidence into a bounded authority multiplier.

This is where the envelope (what the operator pre-approved) meets the ledger
(what has actually been earned). It is the only place authority is granted,
and it is deliberately the most conservative module in the system.

Order of evaluation is safety-first and must not be reordered:

  1. **Exits bypass everything** (invariant 1). An exit/abort/rollback is
     granted unconditionally — before the envelope is consulted, before the
     ledger is read, and even if both are missing, stale, corrupt, or the lane
     is unknown. Nothing in this module may return a blocked exit.
  2. Irreversible actions are refused always (operator-only, never earnable).
  3. Unknown lane / unapproved scenario -> refused.
  4. Envelope integrity failure, unknown trust, cooldown, insufficient
     evidence, or an ineligible blast radius -> 0.0 (refused, not permissive).
  5. Otherwise a graduated fraction of the ceiling, clamped by the envelope
     and never exceeding what was requested.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from drgb.envelope import Envelope, EnvelopeError
from drgb.ledger import Ledger

# Actions that terminate or reduce exposure. Never gated, ever.
#
# EXACT MATCH ONLY after normalisation. Component/substring matching was tried
# and removed: it made "cancel_and_replace" (which PLACES a new order) and
# "close_position_open_new" classify as exits, bypassing every gate — a
# privilege-escalation vector. It also mis-handled spaced names. Integrators
# with their own vocabulary MUST pass ``declare_exit=True`` rather than rely
# on inference; an undeclared, unrecognised action is gated normally.
EXIT_ACTIONS = frozenset({
    # simple
    "exit", "abort", "rollback", "cancel", "stop", "halt", "kill",
    "unwind", "close", "shutdown", "revert", "disarm", "flatten",
    # explicit compounds seen in real fleets
    "emergency_stop", "emergency_exit", "emergency_close", "panic_stop",
    "position_close", "close_position", "close_all", "cancel_all",
    "kill_switch", "killswitch", "flatten_all", "force_exit", "stop_out",
    "roll_back", "shut_down", "wind_down", "safe_stop", "abort_all",
    # order-level cancels: unambiguously reduce exposure
    "kill_order", "cancel_order", "close_order", "cancel_orders",
    "kill_orders", "cancel_open_orders",
})

# Deliberately NOT listed, because they are ambiguous or add exposure:
#   stop_order        -> may mean PLACING a stop order (new exposure)
#   cancel_and_replace-> cancels then PLACES
#   stop_loss_add     -> adds an order
# Integrators whose vocabulary includes such names must decide per action and
# pass declare_exit=True where the action genuinely reduces exposure.


def _normalise_action(action: str) -> str:
    """lower, trim, and fold separators so 'KILL ORDER' == 'kill_order'."""
    token = str(action or "").strip().lower()
    folded = "".join(ch if ch.isalnum() else "_" for ch in token)
    while "__" in folded:
        folded = folded.replace("__", "_")
    return folded.strip("_")

# Graduated authority tiers: (minimum win rate, fraction of ceiling).
TRUST_TIERS: tuple[tuple[float, float], ...] = (
    (0.80, 1.00),
    (0.65, 0.60),
    (0.50, 0.25),
)


def is_exit_action(action: str) -> bool:
    """True when ``action`` is a recognised exit. EXACT match only.

    Deliberately NOT fuzzy: substring/component matching let non-exits such as
    ``cancel_and_replace`` and ``close_position_open_new`` masquerade as exits
    and skip every gate. Callers whose vocabulary differs must declare exits
    explicitly via ``declare_exit=True``.
    """
    return _normalise_action(action) in EXIT_ACTIONS


@dataclass
class CrossingDecision:
    """The bridge's answer. ``multiplier`` is the authority actually granted."""

    lane: str
    action: str
    scenario: str
    originating_agent: str
    granted: bool
    multiplier: float
    is_exit: bool
    crossing_id: str
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tier_fraction(win_rate: float) -> float:
    for minimum, fraction in TRUST_TIERS:
        if win_rate >= minimum:
            return fraction
    return 0.0


def request_crossing(*, lane: str, action: str, scenario: str,
                     originating_agent: str, envelope: Envelope | None,
                     ledger: Ledger | None, requested_multiplier: float = 1.0,
                     scope=None, now: float | None = None,
                     record: bool = True,
                     declare_exit: bool = False) -> CrossingDecision:
    """Evaluate a crossing request and return the authority granted.

    ``envelope`` and ``ledger`` may be ``None`` (unavailable). That never
    grants authority — but it also never blocks an exit.

    ``declare_exit=True`` marks a caller-defined exit explicitly; it is the
    supported way to name exits outside :data:`EXIT_ACTIONS`.
    """
    crossing_id = uuid.uuid4().hex
    exit_request = bool(declare_exit) or is_exit_action(action)

    def _finish(decision: CrossingDecision) -> CrossingDecision:
        if record and ledger is not None:
            try:
                ledger.record_crossing(
                    lane=lane, agent="bridge", scenario=scenario,
                    crossing_id=decision.crossing_id,
                    granted_multiplier=decision.multiplier,
                    originating_agent=originating_agent,
                    originating_envelope_digest=(
                        envelope.digest if envelope is not None else "unavailable"),
                    action=action, is_exit=decision.is_exit,
                    granted=decision.granted,
                )
            except Exception as exc:  # ledger trouble must never gate an exit
                decision.reasons.append(f"ledger_record_failed:{exc}")
        return decision

    # ---- 1. exits bypass everything (invariant 1) -----------------------
    if exit_request:
        return _finish(CrossingDecision(
            lane=lane, action=action, scenario=scenario,
            originating_agent=originating_agent, granted=True, multiplier=1.0,
            is_exit=True, crossing_id=crossing_id,
            reasons=["exit_action_never_gated"],
        ))

    def deny(reason: str, evidence: dict[str, Any] | None = None) -> CrossingDecision:
        return _finish(CrossingDecision(
            lane=lane, action=action, scenario=scenario,
            originating_agent=originating_agent, granted=False, multiplier=0.0,
            is_exit=False, crossing_id=crossing_id, reasons=[reason],
            evidence=evidence or {},
        ))

    # ---- 2. no envelope means no authority ------------------------------
    if envelope is None:
        return deny("envelope_unavailable")
    try:
        lane_env = envelope.lane(lane)
    except EnvelopeError:
        return deny("lane_not_in_envelope")
    except Exception as exc:  # a broken envelope grants nothing, quietly
        return deny(f"envelope_error:{type(exc).__name__}")

    # ---- 3. irreversible actions are never earnable ---------------------
    if lane_env.is_irreversible(action):
        return deny("action_irreversible_operator_only")
    if not lane_env.allows_scenario(scenario):
        return deny("scenario_not_preapproved")

    # ---- 4. integrity, scope, and evidence gates ------------------------
    try:
        integrity_ok = envelope.verify_integrity()
    except Exception as exc:
        return deny(f"envelope_integrity_error:{type(exc).__name__}")
    if not integrity_ok:
        return deny("envelope_integrity_failed")
    if lane_env.ceiling <= 0:
        return deny("ceiling_is_zero_observe_only")
    if scope is not None:
        candidate = next((c for c in getattr(scope, "lanes", [])
                          if c.name == lane), None)
        if candidate is None:
            return deny("lane_absent_from_scope_map")
        if not candidate.ceiling_eligible:
            return deny("blast_radius_not_mapped")
    if ledger is None:
        return deny("ledger_unavailable")

    try:
        trust = ledger.trust(lane, originating_agent=originating_agent, now=now)
    except Exception as exc:
        return deny(f"trust_lookup_error:{type(exc).__name__}")
    if trust.get("status") != "ok":
        return deny(f"trust_{trust.get('reason', 'unknown')}", trust)
    if trust.get("in_cooldown"):
        return deny("in_cooldown_after_failure", trust)
    n_graded = int(trust.get("n_graded") or 0)
    if n_graded < lane_env.min_crossings:
        return deny(
            f"insufficient_evidence:{n_graded}<{lane_env.min_crossings}", trust)
    win_rate = trust.get("win_rate")
    if win_rate is None:
        return deny("no_decided_outcomes", trust)

    # ---- 5. graduated authority, clamped ---------------------------------
    fraction = _tier_fraction(float(win_rate))
    if fraction <= 0:
        return deny(f"win_rate_below_floor:{win_rate:.2f}", trust)
    earned = lane_env.clamp(lane_env.ceiling * fraction)
    granted_multiplier = min(earned, lane_env.clamp(max(0.0, requested_multiplier)))
    if granted_multiplier <= 0:
        return deny("clamped_to_zero", trust)
    return _finish(CrossingDecision(
        lane=lane, action=action, scenario=scenario,
        originating_agent=originating_agent, granted=True,
        multiplier=granted_multiplier, is_exit=False, crossing_id=crossing_id,
        reasons=[f"earned_tier:{fraction:.2f}_of_ceiling:{lane_env.ceiling}"],
        evidence=trust,
    ))
