import json
import time

import pytest

from drgb.ledger import DEFAULT_MAX_AGE_S, Ledger, LedgerError

AG = dict(originating_agent="hermes_local",
          originating_envelope_digest="deadbeef")


def _cross(led, i, lane="lane_a", **kw):
    return led.record_crossing(lane=lane, agent="executor", scenario="s",
                               crossing_id=f"c{i}", granted_multiplier=1.0,
                               **{**AG, **kw})


def _grade(led, i, outcome, lane="lane_a", **kw):
    return led.grade(crossing_id=f"c{i}", lane=lane, agent="executor",
                     outcome=outcome, **{**AG, **kw})


def test_chain_verifies_and_detects_tampering(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(3):
        _cross(led, i)
        _grade(led, i, "success")
    assert led.verify_chain() is True
    # tamper: flip an outcome in place
    text = led.path.read_text().replace('"outcome":"success"', '"outcome":"failure"', 1)
    led.path.write_text(text)
    assert led.verify_chain() is False


def test_truncation_and_reordering_detected(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(4):
        _cross(led, i)
    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")  # drop a row
    assert led.verify_chain() is False
    led.path.write_text("\n".join([lines[1], lines[0]] + lines[2:]) + "\n")
    assert led.verify_chain() is False


def test_forged_row_appended_by_hand_is_detected(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    _cross(led, 0)
    with led.path.open("a") as fh:
        fh.write('{"kind":"grade","lane":"lane_a","agent":"x","outcome":"success",'
                 '"crossing_id":"c0","ts":1,"prev_hash":"x","row_hash":"x"}\n')
    assert led.verify_chain() is False


def test_future_dated_rows_rejected(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    with pytest.raises(LedgerError):
        _cross(led, 0, ts=time.time() + 3600)


def test_no_evidence_is_unknown_not_permissive(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    t = led.trust("lane_a")
    assert t["status"] == "unknown" and t["reason"] == "no_graded_evidence"
    assert t["win_rate"] is None and t["n_graded"] == 0


def test_stale_evidence_is_unknown(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    old = time.time() - (DEFAULT_MAX_AGE_S + 60)
    _cross(led, 0, ts=old)
    _grade(led, 0, "success", ts=old)
    t = led.trust("lane_a")
    assert t["status"] == "unknown" and t["reason"] == "evidence_stale"
    assert t["n_graded"] == 1  # counted, but not trusted


def test_broken_chain_yields_unknown_not_optimistic_trust(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(3):
        _cross(led, i)
        _grade(led, i, "success")
    led.path.write_text(led.path.read_text().replace('"lane":"lane_a"', '"lane":"lane_b"', 1))
    t = led.trust("lane_a")
    assert t["status"] == "unknown" and t["reason"] == "chain_verification_failed"


def test_trust_accumulates_and_win_rate_is_graded_only(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(5):
        _cross(led, i)
    for i in range(5):
        _grade(led, i, "success" if i < 4 else "failure")
    t = led.trust("lane_a", now=time.time() + 7200)  # past cooldown
    assert t["status"] == "ok"
    assert (t["n_graded"], t["successes"], t["failures"]) == (5, 4, 1)
    assert abs(t["win_rate"] - 0.8) < 1e-9


def test_failure_sets_cooldown(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", cooldown_s=1800)
    _cross(led, 0)
    _grade(led, 0, "failure")
    t = led.trust("lane_a")
    assert t["in_cooldown"] is True and t["cooldown_until"] is not None
    later = led.trust("lane_a", now=time.time() + 1801)
    assert later["in_cooldown"] is False


def test_trust_is_per_originator_no_laundering(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    # high-trust record built by one originator
    for i in range(3):
        led.record_crossing(lane="lane_a", agent="executor", scenario="s",
                            crossing_id=f"h{i}", granted_multiplier=1.0,
                            originating_agent="trusted_agent",
                            originating_envelope_digest="aaa")
        led.grade(crossing_id=f"h{i}", lane="lane_a", agent="executor",
                  outcome="success", originating_agent="trusted_agent",
                  originating_envelope_digest="aaa")
    # a different originator inherits NONE of it
    other = led.trust("lane_a", originating_agent="sneaky_agent")
    assert other["status"] == "unknown" and other["n_graded"] == 0
    owner = led.trust("lane_a", originating_agent="trusted_agent")
    assert owner["n_graded"] == 3


def test_grade_requires_outcome_and_crossing_id(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    with pytest.raises(LedgerError):
        led.append(kind="grade", lane="l", agent="a", crossing_id="c1",
                   outcome="maybe", **AG)
    with pytest.raises(LedgerError):
        led.append(kind="grade", lane="l", agent="a", outcome="success", **AG)


def test_malformed_line_raises_rather_than_being_skipped(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    _cross(led, 0)
    with led.path.open("a") as fh:
        fh.write("{not json}\n")
    with pytest.raises(LedgerError):
        list(led.rows())
    assert led.verify_chain() is False


def test_tail_truncation_is_detected(tmp_path):
    """A hash chain alone leaves tail truncation valid: dropping recent rows
    yields a shorter but self-consistent chain, letting an actor delete its
    own failures and recover trust. The head checkpoint closes that.

    Regression guard for a hole found by the C5 randomised property test.
    """
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(6):
        _cross(led, i)
        _grade(led, i, "failure" if i > 3 else "success")
    assert led.verify_chain() is True

    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join(lines[:-4]) + "\n")   # delete recent failures
    assert led.verify_chain() is False


def test_rewind_and_head_removal_both_fail_closed(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(4):
        _cross(led, i)
    assert led.verify_chain() is True

    # head deleted on a non-empty ledger -> cannot prove intactness
    led.head_path.unlink()
    assert led.verify_chain() is False

    # head present but stale (points at an older count) -> mismatch
    led2 = Ledger(tmp_path / "m.jsonl")
    for i in range(3):
        _cross(led2, i)
    stale = json.loads(led2.head_path.read_text())
    stale["count"] = stale["count"] - 1
    led2.head_path.write_text(json.dumps(stale))
    assert led2.verify_chain() is False


def test_empty_ledger_without_head_is_still_valid(tmp_path):
    led = Ledger(tmp_path / "fresh.jsonl")
    assert led.verify_chain() is True
    assert led.trust("any_lane")["status"] == "unknown"


def test_regrading_one_crossing_cannot_inflate_evidence(tmp_path):
    """Authority-earning bypass: re-grading a single crossing N times must not
    satisfy a min_crossings threshold. Evidence counts CROSSINGS, not rows."""
    led = Ledger(tmp_path / "l.jsonl")
    _cross(led, 0)
    for _ in range(10):
        _grade(led, 0, "success")
    trust = led.trust("lane_a", originating_agent="hermes_local")
    assert trust["n_graded"] == 1, "one crossing must count once"
    assert trust["grade_rows"] == 10   # rows still visible for audit
    assert led.verify_chain() is True


def test_a_failure_cannot_be_whitewashed_by_regrading(tmp_path):
    """A later 'success' row is a correction that may add doubt, never remove
    it: the worst outcome recorded for a crossing is the one that counts."""
    led = Ledger(tmp_path / "l.jsonl")
    _cross(led, 0)
    _grade(led, 0, "failure")
    _grade(led, 0, "success")
    _grade(led, 0, "success")
    trust = led.trust("lane_a", originating_agent="hermes_local")
    assert trust["failures"] == 1 and trust["successes"] == 0
    assert trust["in_cooldown"] is True
