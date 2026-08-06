"""Conformance suite: guardian integrity (C1, C2, C4, C5).

Property-style, matching test_c3_exits_never_gated.py. Each test states its
pre-registered pass criterion in its docstring. These are the properties an
installation must be able to demonstrate before it is trusted anywhere.

  C1 evidence before authority   — no promotion without sample size + record
  C2 ceiling not self-raisable   — and every attempt is logged
  C4 cooldown after failure      — failure demotes and imposes a wait
  C5 unavailable != zero         — missing/stale/broken evidence freezes
"""

from __future__ import annotations

import json
import random
import time

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import EnvelopeViolation, load_envelope
from drgb.ledger import DEFAULT_MAX_AGE_S, Ledger

SEED = 20260806
ENV = """
lanes:
  trade:
    scenarios_preapproved: [s]
    ceiling: 1.0
    min_crossings: {min_crossings}
    irreversible_actions: [withdrawal]
"""


def _env(tmp_path, min_crossings=5, name="env.yaml"):
    p = tmp_path / name
    p.write_text(ENV.format(min_crossings=min_crossings))
    return load_envelope(p, violations_log=tmp_path / "violations.jsonl")


def _led(tmp_path, name="l.jsonl", cooldown_s=3600.0):
    return Ledger(tmp_path / name, cooldown_s=cooldown_s)


def _earn(led, n, outcome="success", agent="a1", ts=None, prefix="e"):
    for i in range(n):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id=f"{prefix}{i}", granted_multiplier=1.0,
                            originating_agent=agent,
                            originating_envelope_digest="d", ts=ts)
        led.grade(crossing_id=f"{prefix}{i}", lane="trade", agent="x",
                  outcome=outcome, originating_agent=agent,
                  originating_envelope_digest="d", ts=ts)


def _ask(env, led, **kw):
    base = dict(lane="trade", action="place", scenario="s",
                originating_agent="a1", envelope=env, ledger=led, record=False)
    base.update(kw)
    return request_crossing(**base)


# ---- C1: evidence before authority --------------------------------------

@pytest.mark.parametrize("required", [1, 3, 5, 20, 99])
def test_c1_authority_requires_the_declared_sample_size(tmp_path, required):
    """PASS: for every threshold, n-1 graded crossings must NOT grant, and the
    threshold met with a good record MUST grant."""
    env = _env(tmp_path, min_crossings=required, name=f"env{required}.yaml")
    short = _led(tmp_path, f"short{required}.jsonl")
    _earn(short, max(0, required - 1))
    assert _ask(env, short).granted is False

    enough = _led(tmp_path, f"enough{required}.jsonl")
    _earn(enough, required)
    assert _ask(enough and env, enough).granted is True


def test_c1_a_bad_record_never_earns_authority_however_large(tmp_path):
    """PASS: volume is not evidence. 200 crossings with a losing record must
    not promote."""
    env = _env(tmp_path, min_crossings=5)
    led = _led(tmp_path, cooldown_s=1.0)
    for i in range(200):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id=f"b{i}", granted_multiplier=1.0,
                            originating_agent="a1", originating_envelope_digest="d")
        led.grade(crossing_id=f"b{i}", lane="trade", agent="x",
                  outcome="success" if i % 5 == 0 else "failure",
                  originating_agent="a1", originating_envelope_digest="d")
    d = _ask(env, led, now=time.time() + 10)
    assert d.granted is False


# ---- C2: the ceiling is not self-raisable -------------------------------

def test_c2_no_mutation_path_raises_the_ceiling_and_all_are_logged(tmp_path):
    """PASS: every attempted self-raise raises AND appends a violation row,
    and the effective ceiling is unchanged afterwards."""
    log = tmp_path / "violations.jsonl"
    env = _env(tmp_path)
    before = env.ceiling_for("trade")

    attempts = [
        lambda: env.raise_ceiling("trade", 99.0),
        lambda: env.widen("trade"),
        lambda: setattr(env, "_lanes", {}),
        lambda: setattr(env, "anything", 1),
        lambda: delattr(env, "source"),
    ]
    for attempt in attempts:
        with pytest.raises(EnvelopeViolation):
            attempt()

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) >= len(attempts)
    assert all(r["kind"].startswith("self_raise_attempt:") for r in rows)
    assert env.ceiling_for("trade") == before


def test_c2_a_raised_ceiling_on_disk_does_not_take_effect_silently(tmp_path):
    """PASS: editing the envelope under a running process is detected, and the
    process keeps enforcing the ceiling it loaded until an explicit reload."""
    env = _env(tmp_path)
    led = _led(tmp_path)
    _earn(led, 10)
    assert _ask(env, led).multiplier == 1.0

    (tmp_path / "env.yaml").write_text(ENV.format(min_crossings=5)
                                       .replace("ceiling: 1.0", "ceiling: 50.0"))
    assert env.verify_integrity() is False
    assert env.ceiling_for("trade") == 1.0
    d = _ask(env, led)
    assert d.granted is False and d.reasons == ["envelope_integrity_failed"]


# ---- C4: cooldown after failure -----------------------------------------

def test_c4_failure_demotes_then_recovers_only_after_the_wait(tmp_path):
    env = _env(tmp_path, min_crossings=3)
    led = _led(tmp_path, cooldown_s=1800.0)
    _earn(led, 10)
    assert _ask(env, led).granted is True

    led.record_crossing(lane="trade", agent="x", scenario="s", crossing_id="fail",
                        granted_multiplier=1.0, originating_agent="a1",
                        originating_envelope_digest="d")
    led.grade(crossing_id="fail", lane="trade", agent="x", outcome="failure",
              originating_agent="a1", originating_envelope_digest="d")

    assert _ask(env, led).granted is False           # demoted immediately
    assert _ask(env, led, now=time.time() + 1799).granted is False
    assert _ask(env, led, now=time.time() + 1801).granted is True  # recovered


def test_c4_cooldown_applies_to_the_failing_originator_only(tmp_path):
    """PASS: one agent's failure must not silently punish an unrelated agent's
    independently earned record (the mirror of anti-laundering)."""
    env = _env(tmp_path, min_crossings=3)
    led = _led(tmp_path, cooldown_s=1800.0)
    _earn(led, 5, agent="clean", prefix="c")
    _earn(led, 5, agent="messy", prefix="m")
    led.record_crossing(lane="trade", agent="x", scenario="s", crossing_id="mf",
                        granted_multiplier=1.0, originating_agent="messy",
                        originating_envelope_digest="d")
    led.grade(crossing_id="mf", lane="trade", agent="x", outcome="failure",
              originating_agent="messy", originating_envelope_digest="d")

    assert _ask(env, led, originating_agent="messy").granted is False
    assert _ask(env, led, originating_agent="clean").granted is True


# ---- C5: unavailable is never zero --------------------------------------

@pytest.mark.parametrize("breakage", ["absent", "stale", "corrupt", "truncated",
                                      "malformed"])
def test_c5_every_form_of_missing_evidence_freezes_authority(tmp_path, breakage):
    """PASS: no form of unavailable evidence may read as permissive."""
    env = _env(tmp_path, min_crossings=3)
    led = _led(tmp_path, f"{breakage}.jsonl")

    if breakage == "stale":
        _earn(led, 10, ts=time.time() - (DEFAULT_MAX_AGE_S + 3600))
    elif breakage != "absent":
        _earn(led, 10)
        text = led.path.read_text()
        if breakage == "corrupt":
            led.path.write_text(text.replace('"success"', '"failure"', 1))
        elif breakage == "truncated":
            lines = text.splitlines()
            led.path.write_text("\n".join(lines[:-3]) + "\n")
        elif breakage == "malformed":
            led.path.write_text(text + "{not json}\n")

    trust = led.trust("trade", originating_agent="a1")
    assert trust["status"] == "unknown", breakage
    d = _ask(env, led)
    assert d.granted is False and d.multiplier == 0.0, breakage


def test_c5_randomised_breakage_never_leaks_authority(tmp_path):
    """PASS over randomised corruption: authority is never granted on evidence
    the guardian cannot verify."""
    rng = random.Random(SEED)
    env = _env(tmp_path, min_crossings=3)
    leaks = []
    for i in range(150):
        led = _led(tmp_path, f"r{i}.jsonl")
        _earn(led, rng.randint(3, 8))
        text = led.path.read_text()
        mode = rng.choice(["corrupt", "truncate", "garbage", "reorder", "clean"])
        if mode == "corrupt":
            led.path.write_text(text.replace('"success"', '"failure"', 1))
        elif mode == "truncate":
            lines = text.splitlines()
            led.path.write_text("\n".join(lines[: max(1, len(lines) // 2)]) + "\n")
        elif mode == "garbage":
            led.path.write_text(text + "\n{oops}\n")
        elif mode == "reorder":
            lines = text.splitlines()
            rng.shuffle(lines)
            led.path.write_text("\n".join(lines) + "\n")
        d = _ask(env, led)
        if d.granted and mode != "clean":
            leaks.append((mode, d.reasons))
    assert not leaks, leaks
