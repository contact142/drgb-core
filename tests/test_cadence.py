"""A2 — cadence learning: infer the rhythm, then notice the silence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drgb.cadence import CadenceLearner
from observe.adapters import JsonlLogAdapter, Observation


def test_learns_a_regular_interval():
    learner = CadenceLearner(min_samples=3)
    for i in range(10):
        learner.record("timer_a", ts=1000.0 + i * 240.0)
    profile = learner.profile("timer_a", now=1000.0 + 9 * 240.0 + 10)
    assert profile.status == "healthy"
    assert profile.interval_s == 240.0
    assert profile.jitter_s == 0.0
    assert profile.samples == 10


def test_tolerates_jitter_without_crying_wolf():
    learner = CadenceLearner(min_samples=3)
    stamps = [0, 240, 495, 720, 985, 1200]        # +/- ~5% jitter
    for t in stamps:
        learner.record("jittery", ts=float(t))
    profile = learner.profile("jittery", now=1200 + 300)
    assert profile.status == "healthy"
    assert 200 <= profile.interval_s <= 280


def test_detects_a_lane_that_silently_stopped():
    """The nine-day-stale-sleeve case, in miniature."""
    learner = CadenceLearner(min_samples=3, overdue_factor=3.0)
    for i in range(8):
        learner.record("sleeve_tick", ts=i * 3600.0)     # hourly
    healthy = learner.profile("sleeve_tick", now=7 * 3600.0 + 1800)
    assert healthy.status == "healthy"

    stale = learner.profile("sleeve_tick", now=7 * 3600.0 + 9 * 86400)
    assert stale.status == "overdue"
    assert "silent_for" in stale.reason
    assert stale.silence_s > 9 * 86400 - 1


def test_too_few_samples_is_unknown_never_healthy():
    """Invariant 3 at the cadence layer: no basis to judge means unknown."""
    learner = CadenceLearner(min_samples=4)
    learner.record("new_lane", ts=100.0)
    learner.record("new_lane", ts=200.0)
    profile = learner.profile("new_lane", now=10**9)
    assert profile.status == "unknown"
    assert "insufficient_samples" in profile.reason
    assert profile.interval_s is None


def test_an_unseen_key_is_unknown_not_absent():
    learner = CadenceLearner()
    profile = learner.profile("never_seen", now=1000.0)
    assert profile.status == "unknown" and profile.samples == 0
    assert profile.last_seen is None and profile.silence_s is None


def test_duplicate_and_out_of_order_timestamps_do_not_break_learning():
    learner = CadenceLearner(min_samples=3)
    for t in [500.0, 100.0, 300.0, 300.0, 700.0, 900.0]:
        learner.record("messy", ts=t)
    profile = learner.profile("messy", now=1000.0)
    assert profile.status in {"healthy", "overdue"}
    assert profile.interval_s and profile.interval_s > 0


def test_report_separates_healthy_overdue_and_unknown():
    learner = CadenceLearner(min_samples=3, overdue_factor=2.0)
    for i in range(6):
        learner.record("steady", ts=i * 60.0)
        learner.record("stopped", ts=i * 60.0)
    learner.record("fresh", ts=300.0)

    report = learner.report(now=300.0 + 10 * 60.0)
    assert "stopped" in report["overdue"] and "steady" in report["overdue"]
    assert "fresh" in report["unknown"]
    assert report["keys"] == 3


def test_learns_from_adapter_observations():
    learner = CadenceLearner(min_samples=3)
    observations = [Observation(source="s", event="beat", ts=i * 120.0,
                                key="heartbeat") for i in range(6)]
    assert learner.observe_all(observations) == 6
    profile = learner.profile("heartbeat", now=5 * 120.0 + 60)
    assert profile.status == "healthy" and profile.interval_s == 120.0


# ---- real host --------------------------------------------------------------

REAL_LOGS = [
    Path.home() / "projects" / "timesfm-lab" / "artifacts" / "shadow_log.jsonl",
    Path.home() / ".sgam" / "team" / "conference.jsonl",
]


@pytest.mark.parametrize("log_path", REAL_LOGS, ids=lambda p: p.name)
def test_cadence_learning_on_a_real_host_log(log_path):
    """A2 against real data on this machine. Skips if the log is absent."""
    if not log_path.exists() or log_path.stat().st_size == 0:
        pytest.skip(f"real log unavailable: {log_path}")
    adapter = JsonlLogAdapter(log_path, key_field=None, max_rows=5000)
    report = adapter.poll()
    if not report.observations:
        pytest.skip("no rows to learn from")

    learner = CadenceLearner(min_samples=3)
    # rows carry ISO strings in some logs; keep only numeric-ts observations
    numeric = [o for o in report.observations if isinstance(o.ts, (int, float))]
    learner.observe_all(numeric)
    result = learner.report()
    assert result["keys"] >= 1
    for profile in result["profiles"]:
        assert profile["status"] in {"healthy", "overdue", "unknown"}
        if profile["status"] != "unknown":
            assert profile["interval_s"] and profile["interval_s"] > 0


def test_future_dated_last_seen_is_unknown_not_healthy():
    """Found by the live run: a skewed or mis-parsed timestamp put last_seen
    in the future, and negative silence read as 'healthy' — which would let a
    genuinely dead lane hide behind bad time."""
    learner = CadenceLearner(min_samples=3)
    for i in range(6):
        learner.record("skewed", ts=10_000.0 + i * 60.0)
    profile = learner.profile("skewed", now=5_000.0)     # clock behind the data
    assert profile.status == "unknown"
    assert profile.reason == "last_seen_in_future_clock_skew"
    assert profile.silence_s < 0


def test_small_negative_silence_within_tolerance_still_healthy():
    learner = CadenceLearner(min_samples=3)
    for i in range(6):
        learner.record("edge", ts=1000.0 + i * 300.0)
    last = 1000.0 + 5 * 300.0
    assert learner.profile("edge", now=last - 30).status == "healthy"
