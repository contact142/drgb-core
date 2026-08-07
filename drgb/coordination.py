"""Fail-closed, read-only coordination status derived from DRGB observers.

This is a *reader* of the two observer artifacts.  It does not poll, issue
requests, publish mesh data, or grant authority.  The point is to make the
work already being observed visible to people and model/agent lanes without
turning an absent or stale heartbeat into invented progress.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = {"local": 30 * 60, "vps": 45 * 60}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return None, "evidence_missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"evidence_unreadable:{type(exc).__name__}"
    if not isinstance(document, dict) or not isinstance(document.get("ts"), (int, float)):
        return None, "evidence_invalid"
    return document, None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _source_status(
    name: str, document: dict[str, Any] | None, error: str | None, now: float,
    max_age_seconds: float,
) -> dict[str, Any]:
    if document is None:
        return {"source": name, "status": "BLOCKED", "fresh": False,
                "reason": error, "observed_at": None, "age_seconds": None,
                "lanes": []}
    timestamp = float(document["ts"])
    age = max(0.0, now - timestamp)
    fresh = age <= max_age_seconds
    integrity = bool(document.get("ledger_intact")) and bool(document.get("envelope_intact"))
    conflicts = int((document.get("braid") or {}).get("conflicts", 0))
    rejected = int((document.get("braid") or {}).get("rejected", 0))
    remote_ok = document.get("remote_available", True)
    reasons: list[str] = []
    if not fresh:
        reasons.append("evidence_stale")
    if not integrity:
        reasons.append("integrity_check_failed")
    if not remote_ok:
        reasons.append("remote_evidence_unavailable")
    if conflicts:
        reasons.append("braid_conflict")
    if rejected:
        reasons.append("braid_rejection")
    status = "READY" if not reasons else "BLOCKED"
    lanes = []
    for lane, row in sorted((document.get("lanes") or {}).items()):
        row = row if isinstance(row, dict) else {}
        observed = row.get("observed", row.get("available", False))
        lane_status = "OBSERVED" if fresh and integrity and observed else "BLOCKED"
        lane_reason = "fresh_observation" if lane_status == "OBSERVED" else (
            "not_observed" if not observed else ",".join(reasons) or "unknown")
        lanes.append({
            "id": f"{name}:{lane}", "lane": lane,
            "owner": "hermes_local" if name == "local" else "hermes_vps",
            "status": lane_status, "reason": lane_reason,
            "observations": row.get("observations", row.get("lines")),
            "outcome": row.get("outcome"),
            "granted": bool(row.get("granted", False)),
            "next_checkpoint": "next DRGB observer cycle",
        })
    return {
        "source": name, "status": status, "fresh": fresh,
        "reason": ",".join(reasons) if reasons else "fresh_integrity_checked",
        "observed_at": _iso(timestamp), "age_seconds": round(age, 1),
        "lanes": lanes,
    }


def build_status(
    local_path: Path, vps_path: Path, *, now: float | None = None,
    max_age_seconds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a provenance-preserving status without treating it as authority."""
    clock = float(time.time() if now is None else now)
    limits = {**DEFAULT_MAX_AGE_SECONDS, **(max_age_seconds or {})}
    local_doc, local_error = _load(local_path)
    vps_doc, vps_error = _load(vps_path)
    sources = [
        _source_status("local", local_doc, local_error, clock, limits["local"]),
        _source_status("vps", vps_doc, vps_error, clock, limits["vps"]),
    ]
    lanes = [lane for source in sources for lane in source["lanes"]]
    blockers = [
        {"source": source["source"], "reason": source["reason"]}
        for source in sources if source["status"] == "BLOCKED"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "drgb-core",
        "contract_version": "drgb-core.v1",
        "generated_at": _iso(clock),
        "live_authority": False,
        "mode": "observe_only_coordination",
        "status": "READY" if not blockers else "BLOCKED",
        "mission": {
            "id": "drgb-observer-coordination",
            "lead": "drgb deterministic status writer",
            "outcome": "publish verified cross-agent observer progress",
            "checkpoint": "next observer cycle",
        },
        "evidence": {
            "class": "LIVE_RUNTIME",
            "sources": [str(local_path), str(vps_path)],
            "freshness_computed": True,
            "provenance": "observer-owned hash-chain and envelope integrity fields",
        },
        "sources": sources,
        "lanes": lanes,
        "blockers": blockers,
        "uncertainty": (
            "This reflects only lanes published by the two DRGB observers; an agent or "
            "mission without current observer evidence is intentionally absent, not assumed idle or complete."
        ),
    }


def to_markdown(status: dict[str, Any]) -> str:
    """Human-readable mirror of the JSON status; no facts beyond the JSON."""
    lines = [
        "# DRGB coordination status", "",
        f"- Status: **{status['status']}**", f"- Generated: {status['generated_at']}",
        "- Authority: `false` (read-only evidence)",
        f"- Mission: {status['mission']['outcome']}", "",
        "## Lanes", "", "| Lane | Owner | Status | Evidence | Checkpoint |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lane in status["lanes"]:
        detail = lane["reason"]
        if lane["observations"] is not None:
            detail += f"; observations={lane['observations']}"
        lines.append(f"| {lane['lane']} | {lane['owner']} | {lane['status']} | {detail} | {lane['next_checkpoint']} |")
    if status["blockers"]:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {row['source']}: {row['reason']}" for row in status["blockers"])
    lines.extend(["", "## Boundary", "", status["uncertainty"], ""])
    return "\n".join(lines)


def write_status(
    destination: Path, local_path: Path, vps_path: Path, *, now: float | None = None,
    max_age_seconds: dict[str, float] | None = None,
) -> dict[str, Any]:
    status = build_status(local_path, vps_path, now=now, max_age_seconds=max_age_seconds)
    _atomic_write(destination / "status.json", json.dumps(status, indent=2, sort_keys=True) + "\n")
    _atomic_write(destination / "status.md", to_markdown(status))
    return status
