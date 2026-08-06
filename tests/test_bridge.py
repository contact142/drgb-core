import pytest

from drgb.bridge import (CrossingDecision, EXIT_ACTIONS, is_exit_action,
                         request_crossing)
from drgb.envelope import load_envelope
from drgb.ledger import Ledger

ENV = """
lanes:
  trade:
    scenarios_preapproved: [resting_no_loss_sell]
    ceiling: 1.0
    min_crossings: 3
    irreversible_actions: [withdrawal]
  observe_only:
    scenarios_preapproved: [observe]
    ceiling: 0.0
"""


@pytest.fixture
def env(tmp_path):
    p = tmp_path / "env.yaml"
    p.write_text(ENV)
    return load_envelope(p, violations_log=tmp_path / "v.jsonl")


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "l.jsonl")


def _earn(led, lane="trade", agent="a1", n=4, outcome="success"):
    for i in range(n):
        led.record_crossing(lane=lane, agent="x", scenario="resting_no_loss_sell",
                            crossing_id=f"e{i}", granted_multiplier=1.0,
                            originating_agent=agent,
                            originating_envelope_digest="d")
        led.grade(crossing_id=f"e{i}", lane=lane, agent="x", outcome=outcome,
                  originating_agent=agent, originating_envelope_digest="d")


def _req(**kw):
    base = dict(lane="trade", action="place", scenario="resting_no_loss_sell",
                originating_agent="a1")
    base.update(kw)
    return request_crossing(**base)


# ---- invariant 1: exits are never gated --------------------------------

@pytest.mark.parametrize("action", sorted(EXIT_ACTIONS))
def test_every_exit_action_is_granted_with_no_envelope_or_ledger(action):
    d = _req(action=action, envelope=None, ledger=None, record=False)
    assert d.granted is True and d.multiplier == 1.0 and d.is_exit is True


def test_exit_granted_even_for_unknown_lane_and_unapproved_scenario(env, led):
    d = _req(lane="lane_that_does_not_exist", action="abort",
             scenario="never_approved", envelope=env, ledger=led)
    assert d.granted is True and d.multiplier == 1.0


def test_exit_granted_even_when_irreversible_and_in_cooldown(env, led):
    _earn(led, n=3, outcome="failure")
    d = _req(action="emergency_stop", envelope=env, ledger=led)
    assert d.granted is True and d.is_exit is True


def test_exit_granted_when_ledger_is_broken(env, tmp_path):
    led = Ledger(tmp_path / "broken.jsonl")
    _earn(led)
    led.path.write_text(led.path.read_text().replace('"outcome":"success"',
                                                     '"outcome":"failure"', 1))
    assert led.verify_chain() is False
    d = _req(action="rollback", envelope=env, ledger=led)
    assert d.granted is True and d.multiplier == 1.0


def test_compound_exit_names_recognised():
    assert is_exit_action("emergency_stop") and is_exit_action("position-close")
    assert is_exit_action("KILL") and not is_exit_action("place_order")


# ---- everything else is conservative ------------------------------------

def test_missing_envelope_or_ledger_denies_non_exit(env, led):
    assert _req(envelope=None, ledger=led, record=False).granted is False
    assert _req(envelope=env, ledger=None).granted is False


def test_irreversible_action_never_earnable(env, led):
    _earn(led, n=10)
    d = _req(action="withdrawal", envelope=env, ledger=led)
    assert d.granted is False and d.reasons == ["action_irreversible_operator_only"]


def test_unapproved_scenario_denied(env, led):
    _earn(led, n=10)
    d = _req(scenario="market_buy", envelope=env, ledger=led)
    assert d.granted is False and d.reasons == ["scenario_not_preapproved"]


def test_zero_ceiling_lane_is_observe_only(env, led):
    d = _req(lane="observe_only", scenario="observe", envelope=env, ledger=led)
    assert d.granted is False and d.reasons == ["ceiling_is_zero_observe_only"]


def test_no_evidence_denies(env, led):
    d = _req(envelope=env, ledger=led)
    assert d.granted is False and d.reasons[0].startswith("trust_")


def test_insufficient_evidence_denies(env, led):
    _earn(led, n=2)  # min_crossings is 3
    d = _req(envelope=env, ledger=led)
    assert d.granted is False and "insufficient_evidence" in d.reasons[0]


def test_cooldown_denies_non_exit(env, led):
    _earn(led, n=3)
    led.record_crossing(lane="trade", agent="x", scenario="resting_no_loss_sell",
                        crossing_id="f1", granted_multiplier=1.0,
                        originating_agent="a1", originating_envelope_digest="d")
    led.grade(crossing_id="f1", lane="trade", agent="x", outcome="failure",
              originating_agent="a1", originating_envelope_digest="d")
    d = _req(envelope=env, ledger=led)
    assert d.granted is False and d.reasons == ["in_cooldown_after_failure"]


def test_envelope_changed_under_process_denies(env, led, tmp_path):
    _earn(led, n=5)
    (tmp_path / "env.yaml").write_text(ENV.replace("ceiling: 1.0", "ceiling: 9.0"))
    d = _req(envelope=env, ledger=led)
    assert d.granted is False and d.reasons == ["envelope_integrity_failed"]


def test_trust_is_not_launderable_across_agents(env, led):
    _earn(led, agent="trusted", n=5)
    d = _req(originating_agent="sneaky", envelope=env, ledger=led)
    assert d.granted is False and d.reasons[0].startswith("trust_")


def test_earned_authority_is_graduated_and_clamped(env, led):
    _earn(led, n=5)                       # 100% win rate -> top tier
    d = _req(envelope=env, ledger=led, requested_multiplier=99.0)
    assert d.granted is True
    assert d.multiplier == 1.0            # clamped to ceiling, never 99
    assert d.evidence["n_graded"] == 5


def test_neutral_outcomes_do_not_dilute_win_rate(env, led):
    """win_rate is measured over DECIDED outcomes; neutrals count toward the
    evidence threshold but neither reward nor punish."""
    for i in range(10):
        led.record_crossing(lane="trade", agent="x", scenario="resting_no_loss_sell",
                            crossing_id=f"m{i}", granted_multiplier=1.0,
                            originating_agent="a1", originating_envelope_digest="d")
        led.grade(crossing_id=f"m{i}", lane="trade", agent="x",
                  outcome="success" if i < 7 else "neutral",
                  originating_agent="a1", originating_envelope_digest="d")
    d = _req(envelope=env, ledger=led)
    assert d.granted is True and d.multiplier == 1.0
    assert d.evidence["n_graded"] == 10 and d.evidence["win_rate"] == 1.0


def test_mixed_record_earns_a_lower_tier(env, led, tmp_path):
    """7 wins / 3 losses = 0.70 -> middle tier (0.60 of ceiling), and the
    old failure must be outside the cooldown window to be judged on record."""
    from drgb.ledger import Ledger
    led = Ledger(tmp_path / "mixed.jsonl", cooldown_s=1.0)
    for i in range(10):
        led.record_crossing(lane="trade", agent="x", scenario="resting_no_loss_sell",
                            crossing_id=f"m{i}", granted_multiplier=1.0,
                            originating_agent="a1", originating_envelope_digest="d")
        led.grade(crossing_id=f"m{i}", lane="trade", agent="x",
                  outcome="success" if i < 7 else "failure",
                  originating_agent="a1", originating_envelope_digest="d")
    import time
    d = _req(envelope=env, ledger=led, now=time.time() + 10)
    assert d.granted is True
    assert 0 < d.multiplier < 1.0
    assert abs(d.evidence["win_rate"] - 0.7) < 1e-9


def test_requested_multiplier_caps_the_grant(env, led):
    _earn(led, n=5)
    d = _req(envelope=env, ledger=led, requested_multiplier=0.25)
    assert d.granted is True and d.multiplier == 0.25


def test_scope_ineligible_lane_denied(env, led):
    _earn(led, n=5)

    class _Lane:
        name, ceiling_eligible = "trade", False

    class _Scope:
        lanes = [_Lane()]

    d = _req(envelope=env, ledger=led, scope=_Scope())
    assert d.granted is False and d.reasons == ["blast_radius_not_mapped"]


def test_decisions_are_recorded_to_the_ledger(env, led):
    _earn(led, n=5)
    before = sum(1 for _ in led.rows())
    d = _req(envelope=env, ledger=led)
    after = [r for r in led.rows()]
    assert len(after) == before + 1
    assert after[-1]["crossing_id"] == d.crossing_id
    assert after[-1]["granted"] is True
    assert led.verify_chain() is True


# ---- regression: exit classification must not be a bypass ---------------

@pytest.mark.parametrize("action", [
    "cancel_and_replace",       # PLACES a new order
    "close_position_open_new",  # opens exposure
    "stop_loss_add",            # adds an order
    "non_exit",                 # contains 'exit' as a component
    "restop", "stopper", "killer_feature", "closing_costs",
])
def test_non_exits_are_never_misclassified_as_exits(action, env, led):
    """Regression: component/substring matching let these bypass every gate."""
    assert is_exit_action(action) is False
    d = _req(action=action, envelope=env, ledger=led)
    assert d.is_exit is False
    assert d.granted is False   # no evidence -> denied, not bypassed


@pytest.mark.parametrize("action", [
    "KILL ORDER", "Emergency Stop", " abort ", "position-close",
    "ROLLBACK", "wind down", "kill_switch",
])
def test_real_exits_are_recognised_despite_formatting(action):
    assert is_exit_action(action) is True


def test_declare_exit_supports_caller_vocabulary(env, led):
    """An integrator's own exit name is honoured when declared explicitly."""
    assert is_exit_action("derisk_now") is False
    d = _req(action="derisk_now", envelope=env, ledger=led, declare_exit=True)
    assert d.granted is True and d.is_exit is True and d.multiplier == 1.0


def test_exception_safety_denies_rather_than_raising(env, led):
    class BoomLedger:
        def trust(self, *a, **k): raise RuntimeError("boom")
        def record_crossing(self, *a, **k): raise RuntimeError("boom")

    class BoomEnvelope:
        digest = "x"
        def lane(self, name): raise RuntimeError("boom")
        def verify_integrity(self): raise RuntimeError("boom")

    d = _req(envelope=env, ledger=BoomLedger())
    assert d.granted is False and "trust_lookup_error" in d.reasons[0]
    d2 = _req(envelope=BoomEnvelope(), ledger=led)
    assert d2.granted is False and "envelope_error" in d2.reasons[0]
    # and exits still sail through both
    assert _req(action="abort", envelope=BoomEnvelope(), ledger=BoomLedger()).granted


@pytest.mark.parametrize("requested", [float("inf"), float("nan"), -1.0, 1e308])
def test_pathological_requested_multipliers_never_exceed_ceiling(requested, env, led):
    _earn(led, n=5)
    d = _req(envelope=env, ledger=led, requested_multiplier=requested)
    assert 0.0 <= d.multiplier <= 1.0
    assert d.multiplier == d.multiplier  # not NaN
