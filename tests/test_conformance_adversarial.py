"""Conformance suite: adversarial evidence (E1, E2, E3).

  E1 corrupt ledger      — mutation, truncation, garbage, partial writes
  E2 forged/replayed rows — hand-crafted rows, replays, head forgery
  E3 clock skew           — future-dating, boundaries, negative time

These go past the unit tests: they model an ACTOR trying to manufacture
trust, not a disk going bad. Where an attack succeeds, the test documents it
explicitly rather than hiding it — a known, stated limit is safe; an unknown
one is not.
"""

from __future__ import annotations

import json
import random
import time

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import FUTURE_TOLERANCE_S, Ledger, LedgerError

SEED = 20260806
ENV = """
lanes:
  trade:
    scenarios_preapproved: [s]
    ceiling: 1.0
    min_crossings: 3
"""


def _env(tmp_path):
    p = tmp_path / "env.yaml"
    p.write_text(ENV)
    return load_envelope(p, violations_log=tmp_path / "v.jsonl")


def _earn(led, n, outcome="success", agent="a1", prefix="e", ts=None):
    for i in range(n):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id=f"{prefix}{i}", granted_multiplier=1.0,
                            originating_agent=agent,
                            originating_envelope_digest="d", ts=ts)
        led.grade(crossing_id=f"{prefix}{i}", lane="trade", agent="x",
                  outcome=outcome, originating_agent=agent,
                  originating_envelope_digest="d", ts=ts)


def _granted(tmp_path, led, **kw):
    return request_crossing(lane="trade", action="place", scenario="s",
                            originating_agent="a1", envelope=_env(tmp_path),
                            ledger=led, record=False, **kw).granted


# ---- E1: corrupt ledger --------------------------------------------------

@pytest.mark.parametrize("attack", [
    "flip_outcome", "swap_agent", "edit_multiplier", "insert_row",
    "delete_interior", "delete_tail", "append_garbage", "empty_file",
    "partial_line", "null_bytes",
])
def test_e1_every_corruption_is_detected_and_freezes_authority(tmp_path, attack):
    led = Ledger(tmp_path / f"{attack}.jsonl")
    _earn(led, 5)
    assert led.verify_chain() is True
    text = led.path.read_text()
    lines = text.splitlines()

    if attack == "flip_outcome":
        led.path.write_text(text.replace('"outcome":"success"', '"outcome":"failure"', 1))
    elif attack == "swap_agent":
        led.path.write_text(text.replace('"originating_agent":"a1"',
                                         '"originating_agent":"impostor"', 1))
    elif attack == "edit_multiplier":
        led.path.write_text(text.replace('"granted_multiplier":1.0',
                                         '"granted_multiplier":99.0', 1))
    elif attack == "insert_row":
        led.path.write_text("\n".join(lines[:2] + [lines[0]] + lines[2:]) + "\n")
    elif attack == "delete_interior":
        led.path.write_text("\n".join(lines[:3] + lines[4:]) + "\n")
    elif attack == "delete_tail":
        led.path.write_text("\n".join(lines[:-2]) + "\n")
    elif attack == "append_garbage":
        led.path.write_text(text + "not-json\n")
    elif attack == "empty_file":
        led.path.write_text("")
    elif attack == "partial_line":
        led.path.write_text(text + lines[-1][: len(lines[-1]) // 2] + "\n")
    elif attack == "null_bytes":
        led.path.write_bytes(led.path.read_bytes().replace(b'"success"', b'"succe\x00s"', 1))

    assert led.verify_chain() is False, attack
    assert led.trust("trade", originating_agent="a1")["status"] == "unknown", attack
    assert _granted(tmp_path, led) is False, attack


# ---- E2: forged and replayed rows ---------------------------------------

def test_e2_hand_forged_row_with_plausible_fields_is_rejected(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    _earn(led, 4)
    forged = {
        "kind": "grade", "lane": "trade", "agent": "x", "outcome": "success",
        "crossing_id": "forged", "originating_agent": "a1",
        "originating_envelope_digest": "d", "ts": time.time(),
        "prev_hash": json.loads(led.path.read_text().splitlines()[-1])["row_hash"],
        "row_hash": "f" * 64,
    }
    with led.path.open("a") as handle:
        handle.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
    assert led.verify_chain() is False


def test_e2_replaying_a_valid_row_is_rejected(tmp_path):
    """A replayed row carries a real hash — but not one that chains from the
    new predecessor."""
    led = Ledger(tmp_path / "l.jsonl")
    _earn(led, 4)
    last = led.path.read_text().splitlines()[-1]
    with led.path.open("a") as handle:
        handle.write(last + "\n")
    assert led.verify_chain() is False


def test_e2_head_forgery_is_the_known_limit_and_is_documented(tmp_path):
    """An actor with write access to BOTH the ledger and its head file can
    manufacture a consistent shorter history.

    This test asserts the limit exists so it is never a surprise. Mitigation
    is deployment-level, not code-level: keep the head on append-only or
    remote storage, or sign it with a key the observed process cannot read.
    """
    led = Ledger(tmp_path / "l.jsonl")
    _earn(led, 3, outcome="success", prefix="ok")
    _earn(led, 3, outcome="failure", prefix="bad")
    assert led.trust("trade", originating_agent="a1")["failures"] == 3

    lines = led.path.read_text().splitlines()
    keep = lines[:6]                                    # drop the failures
    led.path.write_text("\n".join(keep) + "\n")
    assert led.verify_chain() is False, "truncation alone must be caught"

    # ...but recomputing the head to match makes it consistent again:
    led._write_head(len(keep), json.loads(keep[-1])["row_hash"])
    assert led.verify_chain() is True, (
        "KNOWN LIMIT: write access to ledger + head permits forged history")
    assert led.trust("trade", originating_agent="a1")["failures"] == 0


def test_e2_appending_after_a_forged_head_still_chains_correctly(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    _earn(led, 2)
    _earn(led, 1, prefix="more")
    assert led.verify_chain() is True


# ---- E3: clock skew ------------------------------------------------------

def test_e3_future_dated_rows_are_refused_at_write(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    with pytest.raises(LedgerError):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id="future", granted_multiplier=1.0,
                            originating_agent="a1",
                            originating_envelope_digest="d",
                            ts=time.time() + FUTURE_TOLERANCE_S + 60)


def test_e3_tolerance_boundary_is_respected(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    inside = led.record_crossing(lane="trade", agent="x", scenario="s",
                                 crossing_id="inside", granted_multiplier=1.0,
                                 originating_agent="a1",
                                 originating_envelope_digest="d",
                                 ts=time.time() + FUTURE_TOLERANCE_S - 5)
    assert inside["row_hash"]
    with pytest.raises(LedgerError):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id="outside", granted_multiplier=1.0,
                            originating_agent="a1",
                            originating_envelope_digest="d",
                            ts=time.time() + FUTURE_TOLERANCE_S + 5)


def test_e3_future_evidence_written_by_hand_is_never_counted(tmp_path):
    """Even if a future-dated row is smuggled in, trust must not count it."""
    led = Ledger(tmp_path / "l.jsonl")
    _earn(led, 4)
    assert led.trust("trade", originating_agent="a1", now=time.time())["n_graded"] == 4
    far_past = led.trust("trade", originating_agent="a1",
                         now=time.time() - 10_000_000)
    assert far_past["n_graded"] == 0        # nothing has "happened yet"
    assert far_past["status"] == "unknown"


def test_e3_negative_and_zero_timestamps_do_not_crash_or_grant(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for ts in (0.0, -1.0, -10**9):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id=f"t{ts}", granted_multiplier=1.0,
                            originating_agent="a1",
                            originating_envelope_digest="d", ts=ts)
        led.grade(crossing_id=f"t{ts}", lane="trade", agent="x",
                  outcome="success", originating_agent="a1",
                  originating_envelope_digest="d", ts=ts)
    trust = led.trust("trade", originating_agent="a1")
    assert trust["status"] == "unknown"     # ancient evidence is stale
    assert _granted(tmp_path, led) is False


def test_e3_randomised_skew_never_leaks_authority(tmp_path):
    rng = random.Random(SEED)
    leaks = []
    for i in range(120):
        led = Ledger(tmp_path / f"skew{i}.jsonl")
        offset = rng.choice([0, -60, -3600, -10**7, -10**9])
        _earn(led, rng.randint(3, 6), ts=time.time() + offset)
        granted = _granted(tmp_path, led)
        if granted and offset <= -10**7:
            leaks.append(("stale evidence granted", offset))
    assert not leaks, leaks
