"""4.3 — event-wake: does waking on news beat waking on the clock?"""

from __future__ import annotations

import json

import pytest

from drgb.event_wake import EventWaker


def test_first_sight_is_news_then_quiet(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text("{}\n")
    waker = EventWaker([src], heartbeat_s=None)

    first = waker.check(now=100.0)
    assert first.wake is True and first.reason == "source_changed"

    quiet = waker.check(now=200.0)
    assert quiet.wake is False and quiet.reason == "no_change"


def test_appending_wakes_the_agent(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text("{}\n")
    waker = EventWaker([src], heartbeat_s=None)
    waker.check(now=100.0)

    with src.open("a") as handle:
        handle.write(json.dumps({"new": True}) + "\n")
    woken = waker.check(now=200.0)
    assert woken.wake is True and str(src) in woken.changed_sources


def test_a_missing_source_wakes_rather_than_assuming_silence(tmp_path):
    """Invariant 3: not being able to look is not the same as nothing
    happening. The agent wakes and finds out."""
    src = tmp_path / "gone.jsonl"
    waker = EventWaker([src], heartbeat_s=None)
    decision = waker.check(now=100.0)
    assert decision.wake is True and decision.reason == "source_unknown"
    assert str(src) in decision.unknown_sources


def test_heartbeat_guarantees_a_wake_in_total_silence(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text("{}\n")
    waker = EventWaker([src], heartbeat_s=3600.0)
    waker.check(now=0.0)                     # first sight
    assert waker.check(now=1800.0).wake is False
    beat = waker.check(now=3600.0)
    assert beat.wake is True and beat.reason == "heartbeat"


def test_multiple_sources_report_which_one_changed(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("{}\n")
    b.write_text("{}\n")
    waker = EventWaker([a, b], heartbeat_s=None)
    waker.check(now=0.0)
    with b.open("a") as handle:
        handle.write("{}\n")
    decision = waker.check(now=10.0)
    assert decision.changed_sources == [str(b)]


def test_savings_reports_the_polling_comparison(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text("{}\n")
    waker = EventWaker([src], heartbeat_s=None)
    waker.check(now=0.0)                      # wake (first sight)
    for i in range(1, 20):
        waker.check(now=float(i))             # quiet
    with src.open("a") as handle:
        handle.write("{}\n")
    waker.check(now=20.0)                     # wake

    savings = waker.savings()
    assert savings["timer_equivalent_wakes"] == 21
    assert savings["event_wakes"] == 2
    assert savings["avoided"] == 19
    assert savings["reduction_ratio"] == pytest.approx(10.5)


def test_truncation_is_a_change_not_silence(tmp_path):
    src = tmp_path / "a.jsonl"
    src.write_text("aaaa\n")
    waker = EventWaker([src], heartbeat_s=None)
    waker.check(now=0.0)
    src.write_text("")                        # rotated/truncated
    assert waker.check(now=10.0).wake is True
