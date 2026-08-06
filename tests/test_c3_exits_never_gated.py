"""C3 — the load-bearing invariant: NO guardian state may block an exit.

This is a property test, not an example test. It generates randomised
envelopes, ledgers, scope maps, agents, and adversarial conditions, and
asserts that every recognised or declared exit is granted at full authority
in every one of them. Everything else in DRGB rests on this holding.

Pre-registered pass criterion: zero blocked exits across all generated cases.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from drgb.bridge import EXIT_ACTIONS, request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import Ledger

SEED = 20260806
CASES = 400

SCENARIOS = ["observe", "resting_no_loss_sell", "deploy", "unapproved_scenario"]
AGENTS = ["a1", "a2", "root_agent", "sneaky", ""]
LANES = ["trade", "infra", "lane_not_in_envelope", ""]


def _random_envelope(rng: random.Random, tmp_path: Path, i: int):
    """Envelope with random ceilings, approvals, thresholds — including
    hostile shapes (zero ceiling, everything irreversible, no scenarios)."""
    ceiling = rng.choice([0.0, 0.0, 0.25, 1.0, 1.25])
    irreversible = rng.choice([[], ["place"], ["place", "exit", "abort", "cancel"]])
    scenarios = rng.choice([[], ["observe"], ["resting_no_loss_sell"], SCENARIOS])
    text = (
        "lanes:\n"
        "  trade:\n"
        f"    scenarios_preapproved: {scenarios}\n"
        f"    ceiling: {ceiling}\n"
        f"    min_crossings: {rng.choice([0, 3, 99])}\n"
        f"    irreversible_actions: {irreversible}\n"
        "  infra:\n"
        "    scenarios_preapproved: []\n"
        "    ceiling: 0.0\n"
    )
    p = tmp_path / f"env{i}.yaml"
    p.write_text(text)
    return load_envelope(p, violations_log=tmp_path / "v.jsonl")


def _random_ledger(rng: random.Random, tmp_path: Path, i: int) -> Ledger:
    led = Ledger(tmp_path / f"l{i}.jsonl", cooldown_s=rng.choice([1.0, 3600.0]))
    for k in range(rng.randint(0, 6)):
        led.record_crossing(lane=rng.choice(["trade", "infra"]), agent="x",
                            scenario=rng.choice(SCENARIOS), crossing_id=f"c{k}",
                            granted_multiplier=1.0,
                            originating_agent=rng.choice(AGENTS) or "anon",
                            originating_envelope_digest="d")
        led.grade(crossing_id=f"c{k}", lane=rng.choice(["trade", "infra"]),
                  agent="x", outcome=rng.choice(["success", "failure", "neutral"]),
                  originating_agent=rng.choice(AGENTS) or "anon",
                  originating_envelope_digest="d")
    if rng.random() < 0.25 and led.path.stat().st_size:      # corrupt the chain
        led.path.write_text(led.path.read_text().replace('"success"', '"failure"', 1))
    if rng.random() < 0.15:                                   # truncate it
        lines = led.path.read_text().splitlines()
        led.path.write_text("\n".join(lines[: max(0, len(lines) - 1)]) + "\n")
    return led


class _Scope:
    def __init__(self, lanes):
        self.lanes = lanes


class _LaneCandidate:
    def __init__(self, name, eligible):
        self.name, self.ceiling_eligible = name, eligible


def _random_scope(rng: random.Random):
    choice = rng.random()
    if choice < 0.3:
        return None
    if choice < 0.6:
        return _Scope([])                                     # lane absent
    return _Scope([_LaneCandidate("trade", rng.random() < 0.5)])


@pytest.mark.parametrize("exit_action", sorted(EXIT_ACTIONS))
def test_every_known_exit_survives_randomised_guardian_states(exit_action, tmp_path):
    rng = random.Random(f"{SEED}:{exit_action}")
    for i in range(12):
        envelope = _random_envelope(rng, tmp_path, i) if rng.random() < 0.8 else None
        ledger = _random_ledger(rng, tmp_path, i) if rng.random() < 0.8 else None
        decision = request_crossing(
            lane=rng.choice(LANES), action=exit_action,
            scenario=rng.choice(SCENARIOS),
            originating_agent=rng.choice(AGENTS),
            envelope=envelope, ledger=ledger,
            requested_multiplier=rng.choice([0.0, -5.0, 1.0, float("inf")]),
            scope=_random_scope(rng),
            now=rng.choice([None, time.time(), time.time() - 10**7]),
            record=rng.random() < 0.5,
        )
        assert decision.granted is True, (exit_action, decision.reasons)
        assert decision.multiplier == 1.0, (exit_action, decision.multiplier)
        assert decision.is_exit is True


def test_declared_exits_survive_randomised_states(tmp_path):
    """Caller-declared exits (integrator vocabulary) get the same guarantee."""
    rng = random.Random(SEED + 1)
    for i in range(CASES // 4):
        decision = request_crossing(
            lane=rng.choice(LANES), action=f"custom_{rng.randint(0, 999)}",
            scenario=rng.choice(SCENARIOS), originating_agent=rng.choice(AGENTS),
            envelope=_random_envelope(rng, tmp_path, i) if rng.random() < 0.7 else None,
            ledger=_random_ledger(rng, tmp_path, i) if rng.random() < 0.7 else None,
            requested_multiplier=rng.choice([0.0, 1.0, float("nan")]),
            scope=_random_scope(rng), record=rng.random() < 0.5,
            declare_exit=True,
        )
        assert decision.granted is True and decision.multiplier == 1.0


def test_exits_survive_hostile_collaborators(tmp_path):
    """Even when the envelope and ledger actively misbehave."""
    class HostileEnvelope:
        digest = "hostile"
        def lane(self, name): raise RuntimeError("hostile lane lookup")
        def verify_integrity(self): raise RuntimeError("hostile integrity")

    class HostileLedger:
        def trust(self, *a, **k): raise RuntimeError("hostile trust")
        def record_crossing(self, *a, **k): raise RuntimeError("hostile record")
        def grade(self, *a, **k): raise RuntimeError("hostile grade")

    for action in sorted(EXIT_ACTIONS):
        d = request_crossing(lane="anything", action=action, scenario="anything",
                             originating_agent="anyone",
                             envelope=HostileEnvelope(), ledger=HostileLedger(),
                             record=True)
        assert d.granted is True and d.multiplier == 1.0, action


def test_no_non_exit_is_ever_granted_without_full_evidence(tmp_path):
    """The mirror property: randomised states must never leak authority to a
    non-exit that lacks envelope + approval + integrity + trust."""
    rng = random.Random(SEED + 2)
    leaks = []
    for i in range(CASES):
        envelope = _random_envelope(rng, tmp_path, i) if rng.random() < 0.8 else None
        ledger = _random_ledger(rng, tmp_path, i) if rng.random() < 0.8 else None
        d = request_crossing(
            lane=rng.choice(LANES), action="place",
            scenario=rng.choice(SCENARIOS), originating_agent=rng.choice(AGENTS),
            envelope=envelope, ledger=ledger,
            requested_multiplier=rng.choice([1.0, float("inf"), 99.0]),
            scope=_random_scope(rng), record=False,
        )
        if not d.granted:
            continue
        # a grant is only legitimate with a real envelope, ledger and evidence
        if envelope is None or ledger is None:
            leaks.append(("granted without envelope/ledger", d.reasons))
            continue
        lane_env = envelope.lane(d.lane)
        if d.multiplier > lane_env.ceiling:
            leaks.append(("multiplier exceeded ceiling", d.multiplier, lane_env.ceiling))
        if not lane_env.allows_scenario(d.scenario):
            leaks.append(("unapproved scenario granted", d.scenario))
        if lane_env.is_irreversible(d.action):
            leaks.append(("irreversible action granted", d.action))
        if not d.evidence or d.evidence.get("status") != "ok":
            leaks.append(("granted without ok trust", d.evidence))
    assert not leaks, leaks
