"""Bootstrap preflight — catch the evolution blockers on day one."""

from __future__ import annotations

import pytest

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.ledger import Ledger
from drgb.preflight import (BLOCKING, FATAL, OK, UNKNOWN, run_preflight)

ENV = """
lanes:
  active:
    scenarios_preapproved: [s]
    ceiling: 1.0
    min_crossings: 10
  observe_only:
    scenarios_preapproved: [observe]
    ceiling: 0.0
"""

STUCK = """
lanes:
  impossible:
    scenarios_preapproved: []
    ceiling: 1.0
    min_crossings: 10
"""


def _env(tmp_path, text=ENV, name="e.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return load_envelope(p, violations_log=tmp_path / "v.jsonl")


def _pf(tmp_path, envelope=None, ledger=None, **kw):
    return run_preflight(envelope=envelope or _env(tmp_path),
                         ledger=ledger if ledger is not None else Ledger(tmp_path / "l.jsonl"),
                         agent="a", bridge_fn=request_crossing, **kw)


def test_exit_path_is_checked_before_anything_else(tmp_path):
    report = _pf(tmp_path)
    assert report["findings"][0]["check"] == "exit_path"
    assert report["findings"][0]["status"] == OK


def test_a_lane_with_no_approved_scenario_is_flagged_blocking(tmp_path):
    """A non-zero ceiling with nothing approved can never promote — that is a
    day-one configuration bug, not a slow start."""
    report = _pf(tmp_path, envelope=_env(tmp_path, STUCK, "s.yaml"))
    stuck = [f for f in report["findings"] if f["check"] == "lane_scenarios"]
    assert stuck and stuck[0]["status"] == BLOCKING
    assert report["can_evolve"] is False


def test_a_never_invoked_dependency_is_blocking(tmp_path):
    """The canary problem: waiting on something that may never happen."""
    report = _pf(tmp_path, dependencies={"profile_describer": 0,
                                         "shadow_log": 42})
    by_name = {f["data"].get("name"): f for f in report["findings"]
               if f["check"] == "dependency"}
    assert by_name["profile_describer"]["status"] == BLOCKING
    assert "may never end" in by_name["profile_describer"]["detail"]
    assert by_name["shadow_log"]["status"] == OK
    assert report["can_evolve"] is False


def test_self_writable_evidence_store_is_blocking(tmp_path):
    report = _pf(tmp_path, observed_host_can_write=True)
    store = [f for f in report["findings"] if f["check"] == "evidence_store"][0]
    assert store["status"] == BLOCKING
    assert "off-host" in store["remedy"] or "replicate" in store["remedy"]


def test_unstated_evidence_store_ownership_is_unknown_not_ok(tmp_path):
    report = _pf(tmp_path)
    store = [f for f in report["findings"] if f["check"] == "evidence_store"][0]
    assert store["status"] == UNKNOWN


def test_promotion_eta_is_estimated_from_observed_cadence(tmp_path):
    cadence = {"profiles": [{"key": "lane:active", "interval_s": 3600.0}]}
    report = _pf(tmp_path, observed_host_can_write=False,
                 cadence_report=cadence)
    eta = [f for f in report["findings"] if f["check"] == "lane_promotion_eta"]
    assert eta and eta[0]["data"]["eta_hours"] == pytest.approx(10.0)


def test_absurdly_slow_promotion_is_degraded_not_silent(tmp_path):
    cadence = {"profiles": [{"key": "lane:active", "interval_s": 86400.0 * 40}]}
    report = _pf(tmp_path, observed_host_can_write=False, cadence_report=cadence)
    eta = [f for f in report["findings"] if f["check"] == "lane_promotion_eta"][0]
    assert eta["status"] == "degraded"


def test_broken_ledger_is_blocking(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.record_crossing(lane="active", agent="x", scenario="s",
                           crossing_id="c", granted_multiplier=1.0,
                           originating_agent="a", originating_envelope_digest="d")
    ledger.path.write_text(ledger.path.read_text().replace('"active"', '"other"', 1))
    report = _pf(tmp_path, ledger=ledger)
    store = [f for f in report["findings"] if f["check"] == "evidence_store"][0]
    assert store["status"] == BLOCKING


def test_missing_ledger_is_fatal(tmp_path):
    report = run_preflight(envelope=_env(tmp_path), ledger=None, agent="a",
                           bridge_fn=request_crossing)
    assert report["verdict"] == FATAL and report["can_evolve"] is False


def test_a_clean_bootstrap_can_evolve(tmp_path):
    report = _pf(tmp_path, observed_host_can_write=False,
                 dependencies={"heartbeat": 12},
                 cadence_report={"profiles": [{"key": "lane:active",
                                               "interval_s": 600.0}]})
    assert report["can_evolve"] is True
    assert report["counts"][FATAL] == 0 and report["counts"][BLOCKING] == 0
