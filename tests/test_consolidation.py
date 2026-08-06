"""4.4 — cognitive consolidation: what the cheap path has earned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drgb.consolidation import ConsolidationTracker


def test_a_class_needs_evidence_before_it_can_go_cheap():
    tracker = ConsolidationTracker(min_samples=20)
    for _ in range(19):
        tracker.record("route", "hold", "hold")
    assert tracker.profile("route").status == "unknown"
    tracker.record("route", "hold", "hold")
    assert tracker.profile("route").status == "consolidatable"


def test_disagreements_keep_a_class_expensive():
    tracker = ConsolidationTracker(min_samples=10, agreement_threshold=0.95)
    for i in range(20):
        tracker.record("risky", "a", "a" if i % 5 else "b")
    profile = tracker.profile("risky")
    assert profile.status == "not_yet"
    assert profile.agreement_rate < 0.95
    assert profile.disagreements == 4


def test_a_barred_class_never_consolidates_however_perfect():
    tracker = ConsolidationTracker(min_samples=5, barred_classes=["withdrawal"])
    for _ in range(500):
        tracker.record("withdrawal", "approve", "approve")
    profile = tracker.profile("withdrawal")
    assert profile.status == "barred"
    assert profile.agreement_rate == 1.0     # perfect record, still barred


def test_consecutive_streak_resets_on_a_single_disagreement():
    tracker = ConsolidationTracker(min_samples=5)
    for _ in range(10):
        tracker.record("c", 1, 1)
    assert tracker.profile("c").consecutive_agreements == 10
    tracker.record("c", 1, 2)
    assert tracker.profile("c").consecutive_agreements == 0


def test_custom_agreement_predicate_is_respected():
    tracker = ConsolidationTracker(min_samples=3)
    tracker.record("numeric", 1.0001, 1.0, agree=True)    # within tolerance
    tracker.record("numeric", 1.0001, 1.0, agree=True)
    tracker.record("numeric", 5.0, 1.0, agree=False)
    profile = tracker.profile("numeric")
    assert profile.agreements == 2 and profile.disagreements == 1


def test_uncomparable_values_count_as_disagreement_not_agreement():
    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("no comparison for you")

    tracker = ConsolidationTracker(min_samples=1)
    assert tracker.record("weird", Hostile(), "x") is False


def test_report_gives_the_growth_metric():
    tracker = ConsolidationTracker(min_samples=10, barred_classes=["danger"])
    for _ in range(30):
        tracker.record("easy", "x", "x")
    for i in range(30):
        tracker.record("hard", "x", "x" if i % 3 else "y")
    for _ in range(30):
        tracker.record("danger", "x", "x")
    for _ in range(3):
        tracker.record("new", "x", "x")

    report = tracker.report()
    assert report["classes"] == 4 and report["decisions"] == 93
    assert report["consolidatable_classes"] == ["easy"]
    assert report["consolidatable_share"] == pytest.approx(30 / 93)
    assert "new" in report["unknown_classes"]
    assert report["barred_classes"] == ["danger"]


def test_empty_tracker_reports_nothing_rather_than_zero_percent():
    report = ConsolidationTracker().report()
    assert report["classes"] == 0 and report["decisions"] == 0
    assert report["consolidatable_share"] is None      # unknown, not 0%
    assert report["median_agreement"] is None


# ---- real host --------------------------------------------------------------

ZS = Path.home() / "projects" / "timesfm-lab" / "artifacts" / "shadow_log.jsonl"
FT = Path.home() / "projects" / "timesfm-lab" / "artifacts" / "shadow_log_ft.jsonl"


def _load(path: Path) -> dict[tuple, dict]:
    rows = {}
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("asset") and row.get("bar_ts"):
            rows[(row["asset"], row["bar_ts"])] = row
    return rows


def test_real_cheap_vs_expensive_model_agreement():
    """Measures the stock (cheap) vs LoRA fine-tuned (expensive) TimesFM
    shadows on the SAME bars — a genuine paired cheap/expensive comparison.
    Skips when the logs are absent."""
    zs, ft = _load(ZS), _load(FT)
    shared = sorted(set(zs) & set(ft))
    if len(shared) < 5:
        pytest.skip(f"insufficient paired shadow decisions ({len(shared)})")

    tracker = ConsolidationTracker(min_samples=5, agreement_threshold=0.95)
    for key in shared:
        asset = key[0]
        cheap = bool(zs[key].get("events"))       # did the cheap model act?
        expensive = bool(ft[key].get("events"))
        tracker.record(f"timesfm:{asset}", cheap, expensive)

    report = tracker.report()
    assert report["decisions"] == len(shared)
    for profile in report["profiles"]:
        assert profile["status"] in {"consolidatable", "not_yet", "unknown",
                                     "barred"}
        if profile["agreement_rate"] is not None:
            assert 0.0 <= profile["agreement_rate"] <= 1.0
