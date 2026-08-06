"""5.6 / Invariant 6 — authority cannot be laundered between agents.

The attack: a low-authority agent cannot do X, so it asks a high-authority
peer to do X for it. If the executor's own envelope and record are what get
checked, the fleet's ceilings become decorative — every restriction is one
polite request away from being bypassed.

The rule: a crossing executes under the **requester's** envelope and the
**requester's** record, never the executor's, and every ledger row carries
``originating_agent`` and ``originating_envelope_digest`` so the provenance
survives audit.

Pre-registered pass criterion: no arrangement of peers, records or envelopes
lets a request obtain more authority than the ORIGINATOR could obtain alone.
"""

from __future__ import annotations

import random

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import Ledger
from drgb.mesh import MeshView, publish

SEED = 20260806

PERMISSIVE = """
lanes:
  trade:
    scenarios_preapproved: [s, risky]
    ceiling: 1.25
    min_crossings: 1
    irreversible_actions: []
"""

RESTRICTIVE = """
lanes:
  trade:
    scenarios_preapproved: [s]
    ceiling: 0.0
    min_crossings: 50
    irreversible_actions: [risky_action]
"""


def _env(tmp_path, text, name):
    p = tmp_path / name
    p.write_text(text)
    return load_envelope(p, violations_log=tmp_path / "violations.jsonl")


def _earn(ledger, agent, n=20, outcome="success"):
    for i in range(n):
        cid = f"{agent}-{i}"
        ledger.record_crossing(lane="trade", agent="executor", scenario="s",
                               crossing_id=cid, granted_multiplier=1.0,
                               originating_agent=agent,
                               originating_envelope_digest="d")
        ledger.grade(crossing_id=cid, lane="trade", agent="executor",
                     outcome=outcome, originating_agent=agent,
                     originating_envelope_digest="d")


def test_a_powerful_executors_record_does_not_serve_a_weak_requester(tmp_path):
    """The core laundering attack: high-trust executor, no-trust requester."""
    envelope = _env(tmp_path, PERMISSIVE, "p.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    _earn(ledger, "powerful_executor", n=50)

    # the powerful agent can act for itself
    own = request_crossing(lane="trade", action="act", scenario="s",
                           originating_agent="powerful_executor",
                           envelope=envelope, ledger=ledger, record=False)
    assert own.granted is True

    # the weak agent asking through it gets nothing
    laundered = request_crossing(lane="trade", action="act", scenario="s",
                                 originating_agent="weak_requester",
                                 envelope=envelope, ledger=ledger, record=False)
    assert laundered.granted is False
    assert laundered.reasons[0].startswith("trust_")


def test_the_requesters_envelope_governs_not_the_executors(tmp_path):
    """Even with a permissive executor envelope available, a request made
    under a restrictive envelope stays restricted."""
    restrictive = _env(tmp_path, RESTRICTIVE, "r.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    _earn(ledger, "requester", n=100)

    decision = request_crossing(lane="trade", action="act", scenario="s",
                                originating_agent="requester",
                                envelope=restrictive, ledger=ledger,
                                record=False)
    assert decision.granted is False
    assert decision.reasons == ["ceiling_is_zero_observe_only"]


def test_an_irreversible_action_cannot_be_delegated_to_a_peer(tmp_path):
    restrictive = _env(tmp_path, RESTRICTIVE, "r.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    _earn(ledger, "requester", n=100)
    _earn(ledger, "powerful_executor", n=100)
    for agent in ("requester", "powerful_executor"):
        decision = request_crossing(lane="trade", action="risky_action",
                                    scenario="s", originating_agent=agent,
                                    envelope=restrictive, ledger=ledger,
                                    record=False)
        assert decision.granted is False
        assert decision.reasons == ["action_irreversible_operator_only"]


def test_provenance_is_recorded_on_every_row(tmp_path):
    """An auditor must be able to see WHO the authority was claimed for."""
    envelope = _env(tmp_path, PERMISSIVE, "p.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    _earn(ledger, "powerful_executor", n=5)
    request_crossing(lane="trade", action="act", scenario="s",
                     originating_agent="weak_requester", envelope=envelope,
                     ledger=ledger, record=True)
    rows = [r for r in ledger.rows()]
    assert rows[-1]["originating_agent"] == "weak_requester"
    assert rows[-1]["originating_envelope_digest"] == envelope.digest
    assert rows[-1]["granted"] is False


def test_peer_mesh_evidence_cannot_be_converted_into_authority(tmp_path):
    """A peer's published trust is information. Consuming it must not raise
    the consumer's own multiplier by any route."""
    envelope = _env(tmp_path, PERMISSIVE, "p.yaml")
    strong_ledger = Ledger(tmp_path / "strong.jsonl")
    _earn(strong_ledger, "strong_peer", n=50)

    view = MeshView()
    view.ingest(publish(strong_ledger, envelope, "strong_peer", ["trade"]))
    evidence = view.evidence_for("trade")
    assert evidence[0]["successes"] == 50
    assert evidence[0]["usable_as_own_authority"] is False

    # the weak agent has its own empty ledger; the mesh changes nothing
    own_ledger = Ledger(tmp_path / "weak.jsonl")
    decision = request_crossing(lane="trade", action="act", scenario="s",
                                originating_agent="weak_agent",
                                envelope=envelope, ledger=own_ledger,
                                record=False)
    assert decision.granted is False


def test_chained_delegation_does_not_accumulate_authority(tmp_path):
    """A -> B -> C: no hop may add authority the originator lacks."""
    envelope = _env(tmp_path, PERMISSIVE, "p.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    for agent in ("agent_b", "agent_c"):
        _earn(ledger, agent, n=50)

    for hop in ("agent_a", "agent_b", "agent_c"):
        decision = request_crossing(lane="trade", action="act", scenario="s",
                                    originating_agent="agent_a",
                                    envelope=envelope, ledger=ledger,
                                    record=False)
        # originator is agent_a throughout, regardless of who executes
        assert decision.originating_agent == "agent_a"
        assert decision.granted is False, hop


def test_randomised_peer_arrangements_never_leak_authority(tmp_path):
    """Property: over many peer/record/envelope arrangements, no originator
    ever obtains authority it could not obtain alone."""
    rng = random.Random(SEED)
    envelope = _env(tmp_path, PERMISSIVE, "p.yaml")
    leaks = []
    for i in range(120):
        ledger = Ledger(tmp_path / f"r{i}.jsonl")
        peers = rng.sample(["p1", "p2", "p3", "p4"], rng.randint(1, 4))
        for peer in peers:
            _earn(ledger, peer, n=rng.randint(10, 60))

        originator = rng.choice(["outsider", "p1", "p2", "p3", "p4"])
        decision = request_crossing(lane="trade", action="act", scenario="s",
                                    originating_agent=originator,
                                    envelope=envelope, ledger=ledger,
                                    record=False)
        alone = ledger.trust("trade", originating_agent=originator)
        if decision.granted and (alone["status"] != "ok"
                                 or (alone["n_graded"] or 0) < 1):
            leaks.append((originator, peers, decision.reasons))
    assert not leaks, leaks


def test_exits_are_still_never_gated_for_any_requester(tmp_path):
    """Anti-laundering must not accidentally block an exit for a weak agent —
    invariant 1 outranks invariant 6."""
    restrictive = _env(tmp_path, RESTRICTIVE, "r.yaml")
    ledger = Ledger(tmp_path / "l.jsonl")
    for agent in ("nobody", "weak_requester", "unknown_agent"):
        decision = request_crossing(lane="trade", action="emergency_stop",
                                    scenario="anything",
                                    originating_agent=agent,
                                    envelope=restrictive, ledger=ledger,
                                    record=False)
        assert decision.granted is True and decision.multiplier == 1.0
