import json

import pytest

from drgb.envelope import (Envelope, EnvelopeError, EnvelopeViolation,
                           load_envelope)

GOOD = """
lanes:
  xmr_ceiling_sell:
    scenarios_preapproved: [resting_no_loss_sell]
    ceiling: 1.0
    limits: {max_qty_xmr: 1.0, reserve_untouchable_xmr: 1.5}
    min_crossings: 10
    out_of_sample_required: true
    irreversible_actions: [withdrawal, key_rotation]
  hermes_observe:
    scenarios_preapproved: [observe]
    ceiling: 0.0
"""


def _write(tmp_path, text=GOOD, name="env.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_loads_lanes_and_ceilings(tmp_path):
    env = load_envelope(_write(tmp_path), violations_log=tmp_path / "v.jsonl")
    assert env.lanes() == ("hermes_observe", "xmr_ceiling_sell")
    assert env.ceiling_for("xmr_ceiling_sell") == 1.0
    assert env.ceiling_for("hermes_observe") == 0.0
    lane = env.lane("xmr_ceiling_sell")
    assert lane.allows_scenario("resting_no_loss_sell")
    assert not lane.allows_scenario("market_buy")
    assert lane.limits["reserve_untouchable_xmr"] == 1.5


def test_clamp_never_exceeds_ceiling(tmp_path):
    env = load_envelope(_write(tmp_path), violations_log=tmp_path / "v.jsonl")
    lane = env.lane("xmr_ceiling_sell")
    assert lane.clamp(1.25) == 1.0      # requested above ceiling -> clamped
    assert lane.clamp(0.6) == 0.6       # below ceiling -> unchanged
    assert lane.clamp(-5) == 0.0        # negative -> zero, never inverted
    assert env.lane("hermes_observe").clamp(1.0) == 0.0  # observe-only stays 0


def test_irreversible_actions_are_outside_every_envelope(tmp_path):
    env = load_envelope(_write(tmp_path), violations_log=tmp_path / "v.jsonl")
    lane = env.lane("xmr_ceiling_sell")
    assert lane.is_irreversible("withdrawal")
    assert lane.is_irreversible("key_rotation")
    assert not lane.is_irreversible("resting_no_loss_sell")


def test_lane_is_frozen(tmp_path):
    env = load_envelope(_write(tmp_path), violations_log=tmp_path / "v.jsonl")
    lane = env.lane("xmr_ceiling_sell")
    with pytest.raises(Exception):
        lane.ceiling = 99.0  # type: ignore[misc]


@pytest.mark.parametrize("attempt", ["raise_ceiling", "widen"])
def test_self_raise_methods_blocked_and_logged(tmp_path, attempt):
    log = tmp_path / "v.jsonl"
    env = load_envelope(_write(tmp_path), violations_log=log)
    with pytest.raises(EnvelopeViolation):
        getattr(env, attempt)("xmr_ceiling_sell", 99.0)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(r["kind"] == f"self_raise_attempt:{attempt}" for r in rows)


def test_setattr_and_delattr_blocked_and_logged(tmp_path):
    log = tmp_path / "v.jsonl"
    env = load_envelope(_write(tmp_path), violations_log=log)
    with pytest.raises(EnvelopeViolation):
        env.anything = 1
    with pytest.raises(EnvelopeViolation):
        del env.source
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    kinds = {r["kind"] for r in rows}
    assert "self_raise_attempt:setattr" in kinds
    assert "self_raise_attempt:delattr" in kinds


def test_integrity_detects_out_of_process_change(tmp_path):
    log = tmp_path / "v.jsonl"
    p = _write(tmp_path)
    env = load_envelope(p, violations_log=log)
    assert env.verify_integrity() is True
    p.write_text(GOOD.replace("ceiling: 1.0", "ceiling: 5.0"))
    assert env.verify_integrity() is False   # change never silent
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(r["kind"] == "envelope_changed_under_process" for r in rows)
    # the loaded object still enforces the ORIGINAL ceiling until reloaded
    assert env.ceiling_for("xmr_ceiling_sell") == 1.0


def test_missing_file_and_malformed_envelopes_rejected(tmp_path):
    with pytest.raises(EnvelopeError):
        load_envelope(tmp_path / "nope.yaml")
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "lanes: [not, a, mapping]", "a.yaml"))
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "lanes: {}", "b.yaml"))
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "lanes:\n  x:\n    scenarios_preapproved: []\n", "c.yaml"))
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "lanes:\n  x:\n    ceiling: -1\n", "d.yaml"))
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "lanes:\n  x:\n    ceiling: abc\n", "e.yaml"))
    with pytest.raises(EnvelopeError):
        load_envelope(_write(tmp_path, "just a string", "f.yaml"))


def test_unknown_lane_is_an_error_not_a_default(tmp_path):
    env = load_envelope(_write(tmp_path), violations_log=tmp_path / "v.jsonl")
    with pytest.raises(EnvelopeError):
        env.lane("lane_that_does_not_exist")
