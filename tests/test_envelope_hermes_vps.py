"""Phase 5.2 — the fleet-wide VPS envelope must be safe by construction.

VPS Hermes runs as root over money paths, public sites, eight provider keys
and its own self-update timer. These tests assert properties of the SHIPPED
file, so any loosening trips a red suite rather than passing quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import Ledger

ENVELOPE = Path(__file__).resolve().parents[1] / "envelopes" / "hermes_vps.yaml"

EXPECTED_DOMAINS = {
    "host_control", "container_control", "trading_adjacent", "cross_host",
    "model_api_spend", "public_web", "business_ops", "repo_filesystem",
    "agent_orchestration", "memory_knowledge", "self_update", "fleet_telemetry",
}

# Never earnable on ANY lane, at any evidence level.
UNIVERSALLY_BARRED = {"self_update", "credential_read", "envelope_edit",
                      "grant_authority"}


@pytest.fixture
def envelope(tmp_path):
    return load_envelope(ENVELOPE, violations_log=tmp_path / "violations.jsonl")


def test_every_surveyed_domain_has_a_lane(envelope):
    assert set(envelope.lanes()) == EXPECTED_DOMAINS


def test_all_ceilings_are_zero(envelope):
    """The lead orchestrator starts with nothing. A non-zero ceiling here is a
    deliberate act by Derek — this test is the tripwire."""
    for name in envelope.lanes():
        assert envelope.ceiling_for(name) == 0.0, name


def test_blast_radius_justifies_high_thresholds(envelope):
    for name in envelope.lanes():
        assert envelope.lane(name).min_crossings >= 50, name


def test_universally_barred_actions_are_barred_on_every_lane(envelope):
    for name in envelope.lanes():
        actions = set(envelope.lane(name).irreversible_actions)
        missing = UNIVERSALLY_BARRED - actions
        assert not missing, f"{name} fails to bar {sorted(missing)}"


def test_self_update_lane_approves_no_scenario_at_all(envelope):
    lane = envelope.lane("self_update")
    assert lane.scenarios_preapproved == ()
    assert "code_replace" in lane.irreversible_actions
    assert "restart_self" in lane.irreversible_actions


def test_money_lane_bars_every_order_and_withdrawal_verb(envelope):
    lane = envelope.lane("trading_adjacent")
    for action in ("order_placement", "order_amend",
                   "order_cancel_and_replace", "withdrawal",
                   "offer_publish", "settlement_action"):
        assert lane.is_irreversible(action), action
    assert lane.limits["money_touching"] is True


def test_unsurveyed_lane_is_marked_unmapped(envelope):
    """Phase 0 rule: no scope, no authority. business_ops internals were not
    surveyed, so the envelope must say so rather than imply coverage."""
    lane = envelope.lane("business_ops")
    assert lane.limits["blast_radius"] == "unmapped"
    assert lane.limits["survey"] == "pending"


def test_no_lane_may_grant_authority_to_a_peer(envelope):
    for name in envelope.lanes():
        assert envelope.lane(name).is_irreversible("grant_authority"), name
    orchestration = envelope.lane("agent_orchestration")
    assert orchestration.limits["may_grant_authority_to_peers"] is False


def test_memory_lane_forbids_writing_the_host_graph(envelope):
    assert envelope.lane("memory_knowledge").limits["host_graph_write"] == "forbidden"


def test_a_flawless_record_still_buys_nothing_today(tmp_path, envelope):
    """End-to-end over every domain: 120 successful crossings, zero authority —
    and exits still pass everywhere."""
    ledger = Ledger(tmp_path / "l.jsonl")
    for name in sorted(envelope.lanes()):
        lane = envelope.lane(name)
        scenario = lane.scenarios_preapproved[0] if lane.scenarios_preapproved \
            else "observe"
        for i in range(120):
            cid = f"{name}-{i}"
            ledger.record_crossing(lane=name, agent="vps_hermes",
                                   scenario=scenario, crossing_id=cid,
                                   granted_multiplier=0.0,
                                   originating_agent="hermes_vps",
                                   originating_envelope_digest=envelope.digest)
            ledger.grade(crossing_id=cid, lane=name, agent="vps_hermes",
                         outcome="success", originating_agent="hermes_vps",
                         originating_envelope_digest=envelope.digest)

        denied = request_crossing(lane=name, action="act", scenario=scenario,
                                  originating_agent="hermes_vps",
                                  envelope=envelope, ledger=ledger, record=False)
        assert denied.granted is False, name

        allowed = request_crossing(lane=name, action="emergency_stop",
                                   scenario=scenario,
                                   originating_agent="hermes_vps",
                                   envelope=envelope, ledger=ledger, record=False)
        assert allowed.granted is True and allowed.multiplier == 1.0, name


def test_self_update_is_refused_on_every_lane_with_perfect_evidence(
        tmp_path, envelope):
    ledger = Ledger(tmp_path / "l.jsonl")
    for i in range(200):
        ledger.record_crossing(lane="host_control", agent="vps_hermes",
                               scenario="observe", crossing_id=f"c{i}",
                               granted_multiplier=0.0,
                               originating_agent="hermes_vps",
                               originating_envelope_digest=envelope.digest)
        ledger.grade(crossing_id=f"c{i}", lane="host_control",
                     agent="vps_hermes", outcome="success",
                     originating_agent="hermes_vps",
                     originating_envelope_digest=envelope.digest)
    for name in envelope.lanes():
        decision = request_crossing(lane=name, action="self_update",
                                    scenario="observe",
                                    originating_agent="hermes_vps",
                                    envelope=envelope, ledger=ledger,
                                    record=False)
        assert decision.granted is False, name
