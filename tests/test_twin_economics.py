"""B1 — twin economics: does event-gating actually pay for itself?

The architecture's central efficiency claim is that a second reality is
affordable because it evaluates on EVENTS rather than continuously. This
measures that claim on replayed real market data (Kraken XMR 4h closes from
the sibling lab), against a continuously-evaluating twin.

Pre-registered pass criteria:
  * >= 10x fewer shadow evaluations than the continuous twin
  * detection of injected divergences is EQUAL OR BETTER (never worse)

Skips when the replay data is absent so a fresh clone elsewhere does not
report a false failure.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

import pytest

from drgb.twin import EventGate, TwinRunner

DATA = (Path.home() / "projects" / "avara-xmr-swing-lab" /
        "avara_core" / "data" / "xmr_4h.csv")
SEED = 20260806
INJECTED = 25
MIN_SAVING = 10.0


@pytest.fixture(scope="module")
def closes() -> list[float]:
    if not DATA.exists():
        pytest.skip(f"replay data unavailable: {DATA}")
    with DATA.open() as handle:
        rows = list(csv.DictReader(handle))
    series = [float(r["close"]) for r in rows]
    if len(series) < 200:
        pytest.skip("replay series too short to measure economics")
    return series


def _series_with_injected_divergences(closes: list[float]):
    """Live values follow a simple shadow model except at injected points."""
    rng = random.Random(SEED)
    # The live world normally MATCHES the shadow model (predict prev close);
    # injected points are where reality departs from the model.
    live = [closes[max(0, i - 1)] for i in range(len(closes))]
    injected = sorted(rng.sample(range(20, len(closes) - 1), INJECTED))
    for i in injected:
        live[i] = closes[i - 1] * 1.25
    return live, set(injected)


def _shadow(obs: dict) -> float:
    """Shadow model: predict this bar's close from the previous close."""
    return obs["prev"]


def _run(gate: EventGate, live: list[float], closes: list[float],
         consolidation_threshold: int, backoff: int):
    runner = TwinRunner(gate, shadow_fn=_shadow,
                        consolidation_threshold=consolidation_threshold,
                        consolidation_backoff=backoff, tolerance=1e-6)
    caught: set[int] = set()
    for i in range(1, len(closes)):
        # 'residual' is a CHEAP prediction-error probe: the live reading vs
        # the trivially-available prior. Gating on it costs almost nothing and
        # is what makes the twin both cheap and safe (see the measured
        # comparison in test_b1_gating_on_the_wrong_signal_trades_away_detection).
        obs = {"event": "bar_close", "prev": closes[i - 1],
               "signal": closes[i], "residual": abs(live[i] - closes[i - 1]),
               "i": i}
        result = runner.observe(obs, live_value=live[i], pattern_key="xmr")
        if result.fired and result.diverged:
            caught.add(i)
    return runner.metrics(), caught


def test_b1_event_gating_cuts_evaluations_by_at_least_10x(closes):
    live, injected = _series_with_injected_divergences(closes)

    # continuous twin: every bar is an "event"
    continuous_gate = EventGate(event_names=["bar_close"], max_interval_s=None)
    continuous, continuous_caught = _run(continuous_gate, live, closes,
                                         consolidation_threshold=10**9, backoff=1)

    # event-gated twin: fires on PREDICTION ERROR, with skill consolidation
    gated_gate = EventGate(event_names=[], signal_key="residual",
                           threshold=0.5, max_interval_s=None)
    gated, gated_caught = _run(gated_gate, live, closes,
                               consolidation_threshold=3, backoff=8)

    saving = continuous["fired"] / max(1, gated["fired"])
    assert continuous["fired"] > 0 and gated["fired"] > 0
    assert saving >= MIN_SAVING, (
        f"only {saving:.1f}x fewer evaluations "
        f"({continuous['fired']} -> {gated['fired']})")

    # detection must not degrade: prediction-error re-engagement is what makes
    # the saving safe rather than merely cheap.
    missed = injected - gated_caught
    assert len(gated_caught) >= len(continuous_caught), (
        f"gated caught {len(gated_caught)} vs continuous {len(continuous_caught)}")
    assert not missed, f"missed {len(missed)} injected divergences"


def test_b1_gating_on_the_wrong_signal_trades_away_detection(closes):
    """The measured caveat behind B1, kept as a test so it is never forgotten.

    Event-gating is only cheap AND safe when the trigger correlates with
    divergence. Gating on the raw price signal instead of prediction error
    buys a similar compute saving but destroys detection — measured here at
    roughly 10-25x saving for 1-3 of 25 divergences caught. The architecture's
    efficiency claim is therefore conditional on WHAT the gate watches.
    """
    live, injected = _series_with_injected_divergences(closes)
    span = max(closes) - min(closes)
    wrong_gate = EventGate(event_names=[], signal_key="signal",
                           threshold=span * 0.03, max_interval_s=None)
    metrics, caught = _run(wrong_gate, live, closes,
                           consolidation_threshold=3, backoff=8)
    assert metrics["fired"] < 100                 # cheap...
    assert len(caught) < INJECTED * 0.5           # ...but blind


def test_b1_consolidation_reduces_cost_over_time(closes):
    """The saving must GROW with mastery, not be a fixed discount."""
    live = list(closes)          # no injected divergences: a masterable stream
    gate = EventGate(event_names=["bar_close"], max_interval_s=None)
    runner = TwinRunner(gate, shadow_fn=_shadow, consolidation_threshold=3,
                        consolidation_backoff=8, tolerance=1e-6)

    half = len(closes) // 2
    early = late = 0
    for i in range(1, len(closes)):
        obs = {"event": "bar_close", "prev": closes[i - 1], "i": i}
        fired = runner.observe(obs, live_value=closes[i - 1],
                               pattern_key="xmr").fired
        if i < half:
            early += bool(fired)
        else:
            late += bool(fired)
    assert late < early, f"cost did not fall with mastery: early={early} late={late}"


def test_b1_a_drifting_stream_keeps_the_twin_engaged(closes):
    """The mirror property: when the world stops being predictable, the
    saving must evaporate rather than silently persist."""
    gate = EventGate(event_names=["bar_close"], max_interval_s=None)
    runner = TwinRunner(gate, shadow_fn=_shadow, consolidation_threshold=3,
                        consolidation_backoff=8, tolerance=1e-6)
    fired = 0
    for i in range(1, 200):
        obs = {"event": "bar_close", "prev": closes[i - 1], "i": i}
        # live value never matches the shadow model -> permanent divergence
        fired += bool(runner.observe(obs, live_value=closes[i - 1] * 1.5,
                                     pattern_key="xmr").fired)
    assert fired >= 190, f"twin disengaged on a diverging stream ({fired}/199)"
