#!/usr/bin/env python3
"""Phase 5.3 — observe VPS Hermes from OUTSIDE it, granting nothing.

Runs on the Prometheus host and polls the VPS with read-only SSH probes, one
per domain lane of envelopes/hermes_vps.yaml. Nothing is installed, started,
stopped or written on the VPS.

The evidence store is deliberately LOCAL: VPS Hermes runs as root and can
rewrite its own code, so a ledger on that machine would prove nothing. Held
here, its record is beyond its reach — this is the deployment mitigation for
the documented evidence-store limit.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from drgb.bridge import request_crossing
from drgb.cadence import CadenceLearner
from drgb.envelope import load_envelope
from drgb.ledger import Ledger
from drgb.braid import Braid
from drgb.mesh import MeshView, publish
from drgb.outcomes import OutcomeGrader
from observe.ssh_adapter import RemoteProbe, SshObserveAdapter

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".drgb" / "observe_vps"
ENVELOPE = ROOT / "envelopes" / "hermes_vps.yaml"
HOST = "hostinger"
AGENT = "hermes_vps"

# One read-only probe per domain lane. Every command is allowlist-validated
# before it can be dispatched.
PROBES: dict[str, RemoteProbe] = {
    "host_control": RemoteProbe(
        "host_control", "systemctl list-units --type=service --state=running "
                        "--no-pager --no-legend"),
    "container_control": RemoteProbe(
        "container_control", "docker ps --format '{{.Names}}'"),
    "trading_adjacent": RemoteProbe(
        "trading_adjacent", "systemctl list-timers --all --no-pager --no-legend"),
    "public_web": RemoteProbe("public_web", "ss -ltn"),
    "repo_filesystem": RemoteProbe("repo_filesystem", "ls /root"),
    "fleet_telemetry": RemoteProbe("fleet_telemetry", "uptime"),
}

# A fleet-wide health signal: any failed unit fails EVERY lane's grade this
# cycle, because a degraded host is not a successful orchestration outcome.
HEALTH_PROBE = RemoteProbe("failed_units",
                           "systemctl list-units --failed --no-pager --no-legend")


def main() -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    envelope = load_envelope(ENVELOPE, violations_log=STATE / "violations.jsonl")
    ledger = Ledger(STATE / "ledger.jsonl")
    learner = CadenceLearner(min_samples=3)

    history = STATE / "cadence_history.json"
    if history.exists():
        try:
            for key, stamps in json.loads(history.read_text()).items():
                for ts in stamps:
                    learner.record(key, float(ts))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    baselines_path = STATE / "baselines.json"
    try:
        grader = OutcomeGrader(json.loads(baselines_path.read_text()))
    except (OSError, json.JSONDecodeError):
        grader = OutcomeGrader()

    now = time.time()
    adapter = SshObserveAdapter(HOST, list(PROBES.values()) + [HEALTH_PROBE])
    report = adapter.poll(now=now)
    by_key = {o.key: o for o in report.observations}
    health = by_key.get("failed_units")
    failed_units = health.data["lines"] if health is not None else None

    summary: dict = {"ts": now, "host": HOST, "remote_available": report.available,
                     "note": report.note, "lanes": {}, "granted": 0, "denied": 0}

    for lane, probe in PROBES.items():
        observed = by_key.get(probe.key)
        if observed is not None:
            learner.record(f"vps:{lane}", now)
        decision = request_crossing(
            lane=lane, action="observe", scenario="observe",
            originating_agent=AGENT, envelope=envelope, ledger=ledger,
            requested_multiplier=0.0, now=now, record=True)
        # Grade against a REAL observed outcome, not a placeholder.
        grade = grader.grade(lane, observed=observed is not None,
                             lines=observed.data["lines"] if observed else None,
                             failed_units=failed_units)
        ledger.grade(crossing_id=decision.crossing_id, lane=lane,
                     agent="drgb_remote", outcome=grade.outcome,
                     originating_agent=AGENT,
                     originating_envelope_digest=envelope.digest,
                     grade_reason=grade.reason)
        trust = ledger.trust(lane, originating_agent=AGENT, now=now)
        summary["lanes"][lane] = {
            "observed": observed is not None,
            "lines": observed.data["lines"] if observed else None,
            "outcome": grade.outcome, "grade_reason": grade.reason,
            "trust": {"n": trust["n_graded"], "wins": trust["successes"],
                      "fails": trust["failures"], "status": trust["status"]},
            "granted": decision.granted, "reasons": decision.reasons,
        }
        summary["granted" if decision.granted else "denied"] += 1

    summary["failed_units"] = failed_units
    baselines_path.write_text(json.dumps(grader.baselines, indent=2))
    # --- mesh: publish our evidence, ingest peers', alert on disagreement ---
    mesh_dir = Path.home() / ".drgb" / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    document = publish(ledger, envelope, AGENT, list(PROBES), now=now)
    (mesh_dir / f"{AGENT}.json").write_text(
        json.dumps(document.to_dict(), indent=2, sort_keys=True))

    view = MeshView(store=STATE / "mesh_view.json")
    mesh_alerts = []
    for peer_doc in sorted(mesh_dir.glob("*.json")):
        if peer_doc.stem == AGENT:
            continue                      # a peer cannot vouch for itself
        try:
            alert = view.ingest(json.loads(peer_doc.read_text()))
        except Exception as exc:          # a bad peer document is an alert
            alert = {"agent": peer_doc.stem, "kind": "peer_document_rejected",
                     "detail": str(exc)[:120]}
            view.alerts.append(alert)
        if alert:
            mesh_alerts.append(alert)
    summary["mesh"] = {"published_lanes": len(document.lanes),
                       "peers_seen": len(view.peers()),
                       "alerts": mesh_alerts,
                       "total_alerts": len(view.alerts)}

    # --- braid: contribute our knowledge, read everyone's, brief ---------
    braid_dir = Path.home() / ".drgb" / "braid"
    braid_dir.mkdir(parents=True, exist_ok=True)
    contribution = {
        "producer": "drgb", "namespace": "drgb", "agent": AGENT,
        "published_at": now, "envelope_digest": envelope.digest,
        "nodes": [{"id": f"drgb:lane:{lane}", "agent": AGENT,
                   "observed": bool(row.get("observed", row.get("available"))),
                   "last_seen": now}
                  for lane, row in summary["lanes"].items()],
    }
    (braid_dir / f"{AGENT}.json").write_text(json.dumps(contribution, indent=2,
                                                        sort_keys=True))
    braid = Braid()
    braid.contribute_all(braid_dir)
    brief = braid.briefing(now=now)
    braid.export(STATE / "braid.json", now=now)
    summary["braid"] = {"agents": brief["agents"], "nodes": brief["nodes"],
                        "conflicts": brief["conflict_count"],
                        "rejected": brief["rejected_count"],
                        "silence_seconds": brief["silence_seconds"]}

    summary["ledger_intact"] = ledger.verify_chain()
    summary["envelope_intact"] = envelope.verify_integrity()
    summary["evidence_store"] = "local_to_observer_not_reachable_by_observed_host"

    history.write_text(json.dumps(
        {k: learner._history[k][-50:] for k in learner.keys()}))
    (STATE / "last_cycle.json").write_text(json.dumps(summary, indent=2))
    with (STATE / "cycles.jsonl").open("a") as handle:
        handle.write(json.dumps(summary) + "\n")

    print(json.dumps({k: v for k, v in summary.items() if k != "lanes"}, indent=2))
    for lane, row in summary["lanes"].items():
        t = row["trust"]
        print(f"  {lane:20} lines={str(row['lines']):5} "
              f"grade={row['outcome']:8} trust(n={t['n']},w={t['wins']},f={t['fails']},{t['status']}) "
              f"granted={row['granted']}")
    return summary


if __name__ == "__main__":
    main()
