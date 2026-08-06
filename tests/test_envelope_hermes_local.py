"""Phase 4.1 — the shipped local-Hermes envelope must be safe by construction.

These assert properties of the actual file that will govern a real fleet, not
of a fixture: every ceiling observe-only to start, irreversible actions never
earnable, no lane able to grant authority to a peer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import Ledger

ENVELOPE = Path(__file__).resolve().parents[1] / "envelopes" / "hermes_local.yaml"

FLEET_IRREVERSIBLE = {
    "order_placement", "withdrawal", "key_rotation", "envelope_edit",
    "service_enable", "service_disable",
}


@pytest.fixture
def envelope(tmp_path):
    return load_envelope(ENVELOPE, violations_log=tmp_path / "violations.jsonl")


def test_the_shipped_envelope_loads_and_declares_domain_lanes(envelope):
    lanes = envelope.lanes()
    assert {"research_readonly", "paper_trading", "monitoring",
            "shadow_proposals", "repo_write", "agent_orchestration"} <= set(lanes)


def test_every_lane_starts_observe_only(envelope):
    """Phase 4 begins at zero authority everywhere. A non-zero ceiling in this
    file is a change Derek must make deliberately — this test is the tripwire."""
    for name in envelope.lanes():
        assert envelope.ceiling_for(name) == 0.0, f"{name} is not observe-only"


def test_every_lane_requires_real_evidence_before_any_promotion(envelope):
    for name in envelope.lanes():
        lane = envelope.lane(name)
        assert lane.min_crossings >= 20, f"{name} threshold too low"


def test_irreversible_actions_are_declared_on_every_lane(envelope):
    for name in envelope.lanes():
        lane = envelope.lane(name)
        assert lane.irreversible_actions, f"{name} declares none"
        assert "envelope_edit" in lane.irreversible_actions, name


def test_no_lane_can_grant_authority_to_a_peer(envelope):
    """Invariant 6 at the configuration layer."""
    orchestration = envelope.lane("agent_orchestration")
    assert "grant_authority" in orchestration.irreversible_actions
    assert orchestration.limits["may_grant_authority_to_peers"] is False


def test_shadow_lane_cannot_place_or_amend_orders(envelope):
    shadow = envelope.lane("shadow_proposals")
    assert shadow.limits["may_place_orders"] is False
    assert "order_placement" in shadow.irreversible_actions
    assert "order_amend" in shadow.irreversible_actions


def test_bridge_refuses_everything_under_this_envelope_today(tmp_path, envelope):
    """End-to-end: with observe-only ceilings, no evidence can buy authority —
    but exits still pass, on every lane."""
    ledger = Ledger(tmp_path / "l.jsonl")
    for name in envelope.lanes():
        lane = envelope.lane(name)
        scenario = lane.scenarios_preapproved[0]
        for i in range(40):     # more than any lane's threshold
            ledger.record_crossing(lane=name, agent="hermes", scenario=scenario,
                                   crossing_id=f"{name}{i}", granted_multiplier=0.0,
                                   originating_agent="hermes_local",
                                   originating_envelope_digest=envelope.digest)
            ledger.grade(crossing_id=f"{name}{i}", lane=name, agent="hermes",
                         outcome="success", originating_agent="hermes_local",
                         originating_envelope_digest=envelope.digest)

        denied = request_crossing(lane=name, action="act", scenario=scenario,
                                  originating_agent="hermes_local",
                                  envelope=envelope, ledger=ledger, record=False)
        assert denied.granted is False, name
        assert denied.reasons == ["ceiling_is_zero_observe_only"], name

        allowed = request_crossing(lane=name, action="abort", scenario=scenario,
                                   originating_agent="hermes_local",
                                   envelope=envelope, ledger=ledger, record=False)
        assert allowed.granted is True and allowed.multiplier == 1.0, name


def test_irreversible_actions_stay_refused_even_with_a_perfect_record(
        tmp_path, envelope):
    ledger = Ledger(tmp_path / "l.jsonl")
    for i in range(50):
        ledger.record_crossing(lane="repo_write", agent="hermes", scenario="read",
                               crossing_id=f"c{i}", granted_multiplier=0.0,
                               originating_agent="hermes_local",
                               originating_envelope_digest=envelope.digest)
        ledger.grade(crossing_id=f"c{i}", lane="repo_write", agent="hermes",
                     outcome="success", originating_agent="hermes_local",
                     originating_envelope_digest=envelope.digest)
    for action in ("force_push", "history_rewrite", "credential_write"):
        decision = request_crossing(lane="repo_write", action=action,
                                    scenario="read",
                                    originating_agent="hermes_local",
                                    envelope=envelope, ledger=ledger, record=False)
        assert decision.granted is False, action
