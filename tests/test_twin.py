from drgb.ledger import Ledger
from drgb.twin import EventGate, TwinRunner


class _Clock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_event_gate_fires_on_declared_name_and_not_otherwise():
    gate = EventGate(event_names={"fill"})

    assert gate.should_fire({"event": "noise"}) == (False, "not_event")
    assert gate.should_fire({"event": "fill"}) == (True, "declared_event")


def test_event_gate_fires_on_threshold_breach():
    gate = EventGate(event_names={"seed"}, signal_key="price", threshold=5.0)

    assert gate.should_fire({"event": "seed", "price": 100.0})[0] is True
    assert gate.should_fire({"price": 104.0}) == (False, "not_event")
    assert gate.should_fire({"price": 105.1}) == (True, "threshold_breach")


def test_event_gate_fires_on_heartbeat_and_force():
    clock = _Clock()
    gate = EventGate(max_interval_s=10.0, clock=clock)

    assert gate.should_fire({"event": "noise"})[0] is False
    clock.advance(10.1)
    assert gate.should_fire({"event": "noise"}) == (True, "heartbeat")
    assert gate.should_fire({"event": "noise", "force": True}) == (
        True, "force")


def test_raising_shadow_is_contained_and_recorded():
    gate = EventGate(event_names={"tick"})

    def shadow(_obs):
        raise RuntimeError("shadow unavailable")

    runner = TwinRunner(gate, shadow)
    result = runner.observe({"event": "tick"}, live_value=7)

    assert result.fired is True
    assert result.error is not None and "shadow unavailable" in result.error
    assert result.diverged is None
    assert runner.metrics()["errors"] == 1


def test_numeric_divergence_uses_tolerance_and_delta():
    runner = TwinRunner(
        EventGate(event_names={"tick"}),
        lambda _obs: 10.0,
        tolerance=1e-6,
    )

    agree = runner.observe({"event": "tick"}, live_value=10.0000005)
    diverge = runner.observe({"event": "tick"}, live_value=10.01)

    assert agree.diverged is False and abs(agree.delta - 5e-7) < 1e-15
    assert diverge.diverged is True and abs(diverge.delta - 0.01) < 1e-12
    assert runner.metrics()["divergences"] == 1


def test_non_numeric_divergence_uses_equality():
    runner = TwinRunner(EventGate(event_names={"tick"}), lambda _obs: {"ok": 1})

    agree = runner.observe({"event": "tick"}, live_value={"ok": 1})
    diverge = runner.observe({"event": "tick"}, live_value={"ok": 2})

    assert agree.diverged is False and agree.delta is None
    assert diverge.diverged is True and diverge.delta is None


def test_consolidation_reduces_firing_after_consecutive_agreements():
    calls = []
    runner = TwinRunner(
        EventGate(event_names={"tick"}),
        lambda obs: calls.append(obs) or 1,
        consolidation_threshold=3,
        consolidation_backoff=2,
    )

    first = [runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
             for _ in range(3)]
    skipped = runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    later = runner.observe({"event": "tick"}, live_value=1, pattern_key="p")

    assert all(result.fired for result in first)
    assert first[-1].consolidated is True
    assert skipped.fired is False and skipped.reason == "consolidated_backoff"
    assert later.fired is True and later.consolidated is True
    assert len(calls) == 4
    assert runner.consolidation_state()["p"]["consolidated"] is True


def test_single_divergence_deconsolidates_immediately():
    runner = TwinRunner(
        EventGate(event_names={"tick"}),
        lambda obs: obs.get("shadow", 1),
        consolidation_threshold=2,
        consolidation_backoff=5,
    )

    runner.observe({"event": "tick", "shadow": 1}, live_value=1,
                   pattern_key="p")
    runner.observe({"event": "tick", "shadow": 1}, live_value=1,
                   pattern_key="p")
    divergent = runner.observe({"event": "tick", "shadow": 2, "force": True},
                               live_value=1,
                               pattern_key="p")
    next_result = runner.observe({"event": "tick", "shadow": 1}, live_value=1,
                                 pattern_key="p")

    assert divergent.diverged is True and divergent.consolidated is False
    assert next_result.fired is True
    assert runner.consolidation_state()["p"]["consecutive_agreements"] == 1


def test_force_and_heartbeat_bypass_consolidation_backoff():
    clock = _Clock()
    runner = TwinRunner(
        EventGate(event_names={"tick"}, max_interval_s=10.0, clock=clock),
        lambda _obs: 1,
        consolidation_threshold=1,
        consolidation_backoff=10,
    )

    runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    assert runner.observe({"event": "tick"}, live_value=1,
                          pattern_key="p").fired is False
    assert runner.observe({"event": "tick", "force": True}, live_value=1,
                          pattern_key="p").fired is True
    clock.advance(10.1)
    heartbeat = runner.observe({"event": "noise"}, live_value=1,
                               pattern_key="p")

    assert heartbeat.fired is True and heartbeat.reason == "heartbeat"


def test_metrics_report_skips_ratio_and_consolidated_keys():
    runner = TwinRunner(
        EventGate(event_names={"tick"}),
        lambda _obs: 1,
        consolidation_threshold=1,
        consolidation_backoff=2,
    )

    runner.observe({"event": "noise"}, live_value=1, pattern_key="p")
    runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    metrics = runner.metrics()

    assert metrics == {
        "observations": 4,
        "fired": 2,
        "skipped": 2,
        "divergences": 0,
        "errors": 0,
        "consolidated_keys": 1,
        "evaluation_ratio": 0.5,
    }


def test_ledger_rows_are_appended_for_fired_evaluations(tmp_path):
    ledger = Ledger(tmp_path / "twin.jsonl")
    runner = TwinRunner(EventGate(event_names={"tick"}), lambda _obs: 2,
                        ledger=ledger)

    result = runner.observe({"event": "tick"}, live_value=1, pattern_key="p")
    rows = list(ledger.rows())

    assert result.fired is True and result.diverged is True
    assert len(rows) == 1
    assert rows[0]["kind"] == "crossing"
    assert rows[0]["diverged"] is True
    assert rows[0]["delta"] == 1.0
    assert ledger.verify_chain() is True


def test_ledger_failure_is_contained_and_recorded():
    class _BrokenLedger:
        def append(self, **_kwargs):
            raise RuntimeError("ledger unavailable")

    runner = TwinRunner(EventGate(event_names={"tick"}), lambda _obs: 1,
                        ledger=_BrokenLedger())
    result = runner.observe({"event": "tick"}, live_value=1)

    assert result.fired is True
    assert result.error is not None and "ledger unavailable" in result.error
    assert runner.metrics()["errors"] == 1


def test_prediction_error_re_engages_a_consolidated_skill(monkeypatch):
    """Consolidation lets the twin skip; a drifting live value must pull it
    straight back. Without this, a consolidated skill can drift invisibly
    because divergence is only measurable on a firing.

    Regression guard for a hole found by adversarial probe.
    """
    clock = {"t": 0.0}

    gate = EventGate(event_names=["tick"], max_interval_s=10_000.0,
                     clock=lambda: clock["t"])
    runner = TwinRunner(gate, shadow_fn=lambda obs: 1.0,
                        consolidation_threshold=2, consolidation_backoff=50)

    for _ in range(3):
        clock["t"] += 1
        runner.observe({"event": "tick"}, live_value=1.0, pattern_key="k")
    assert runner.consolidation_state()["k"]["consolidated"] is True

    clock["t"] += 1
    quiet = runner.observe({"event": "tick"}, live_value=1.0, pattern_key="k")
    assert quiet.fired is False and quiet.reason == "consolidated_backoff"

    clock["t"] += 1
    surprised = runner.observe({"event": "tick"}, live_value=99.0, pattern_key="k")
    assert surprised.fired is True, "prediction error must re-engage the twin"
    assert surprised.diverged is True
    assert runner.consolidation_state()["k"]["consolidated"] is False


def test_prediction_error_does_not_fire_without_a_live_value():
    clock = {"t": 0.0}
    gate = EventGate(event_names=["tick"], max_interval_s=10_000.0,
                     clock=lambda: clock["t"])
    runner = TwinRunner(gate, shadow_fn=lambda obs: 1.0,
                        consolidation_threshold=2, consolidation_backoff=50)
    for _ in range(3):
        clock["t"] += 1
        runner.observe({"event": "tick"}, live_value=1.0, pattern_key="k")
    clock["t"] += 1
    blind = runner.observe({"event": "tick"}, pattern_key="k")
    assert blind.fired is False and blind.reason == "consolidated_backoff"
