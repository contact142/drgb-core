"""Coordination status is a fail-closed reader of observer evidence."""

from __future__ import annotations

import json

from drgb.coordination import build_status, write_status


def _cycle(ts: float, **overrides):
    document = {
        "ts": ts, "ledger_intact": True, "envelope_intact": True,
        "lanes": {"monitoring": {"available": True, "observations": 3, "granted": False}},
        "braid": {"conflicts": 0, "rejected": 0},
    }
    document.update(overrides)
    return document


def test_fresh_evidence_is_visible_but_never_live_authority(tmp_path):
    local, vps = tmp_path / "local.json", tmp_path / "vps.json"
    local.write_text(json.dumps(_cycle(100.0)))
    vps.write_text(json.dumps(_cycle(100.0, remote_available=True)))

    status = build_status(local, vps, now=120.0)

    assert status["status"] == "READY"
    assert status["live_authority"] is False
    assert {lane["status"] for lane in status["lanes"]} == {"OBSERVED"}


def test_stale_evidence_fails_closed(tmp_path):
    local, vps = tmp_path / "local.json", tmp_path / "vps.json"
    local.write_text(json.dumps(_cycle(0.0)))
    vps.write_text(json.dumps(_cycle(0.0)))

    status = build_status(local, vps, now=10_000.0)

    assert status["status"] == "BLOCKED"
    assert all(source["reason"] == "evidence_stale" for source in status["sources"])
    assert {lane["status"] for lane in status["lanes"]} == {"BLOCKED"}


def test_missing_or_conflicting_evidence_is_blocked(tmp_path):
    local, vps = tmp_path / "local.json", tmp_path / "vps.json"
    local.write_text(json.dumps(_cycle(100.0, braid={"conflicts": 1, "rejected": 0})))

    status = build_status(local, vps, now=120.0)

    assert status["status"] == "BLOCKED"
    assert {item["reason"] for item in status["blockers"]} == {"braid_conflict", "evidence_missing"}


def test_writer_uses_atomic_json_and_markdown_mirror(tmp_path):
    local, vps, destination = tmp_path / "local.json", tmp_path / "vps.json", tmp_path / "out"
    local.write_text(json.dumps(_cycle(100.0)))
    vps.write_text(json.dumps(_cycle(100.0)))

    written = write_status(destination, local, vps, now=120.0)

    assert json.loads((destination / "status.json").read_text())["status"] == written["status"]
    assert "# DRGB coordination status" in (destination / "status.md").read_text()
