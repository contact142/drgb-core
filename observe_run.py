#!/usr/bin/env python3
"""Phase 4.2 — attach DRGB to this host in OBSERVE-ONLY mode.

Runs one observation cycle: poll adapters, learn cadence, request crossings
under the observe-only Hermes envelope, and record everything to a ledger and
subgraph. Grants nothing (every ceiling is 0.0) and changes nothing — its
purpose is to prove zero behaviour change while evidence accumulates.

State lives entirely under ~/.drgb/observe/. Nothing outside is written.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from drgb.bridge import request_crossing
from drgb.cadence import CadenceLearner
from drgb.envelope import load_envelope
from drgb.ledger import Ledger
from drgb.subgraph import Subgraph
from observe.adapters import JsonlLogAdapter, SystemdTimerAdapter, poll_all

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".drgb" / "observe"
ENVELOPE = ROOT / "envelopes" / "hermes_local.yaml"
AGENT = "hermes_local"

# Real event sources on this host, mapped to their envelope lane.
SOURCES: dict[str, tuple[str, Path | None]] = {
    "monitoring": ("systemd", None),
    "shadow_proposals": ("jsonl", Path("/home/derek/projects/AVARA 2.0/data/"
                                       "analysis/xmr_ceiling_sell_shadow.jsonl")),
    "paper_trading": ("jsonl", Path.home() / "projects" / "avara-xmr-swing-lab" /
                      "avara_core" / "data" / "paper_ledger.jsonl"),
    "research_readonly": ("jsonl", Path.home() / "projects" / "timesfm-lab" /
                          "artifacts" / "shadow_log.jsonl"),
}


def _adapter_for(kind: str, path: Path | None):
    if kind == "systemd":
        return SystemdTimerAdapter()
    return JsonlLogAdapter(path)


def main() -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    envelope = load_envelope(ENVELOPE, violations_log=STATE / "violations.jsonl")
    ledger = Ledger(STATE / "ledger.jsonl")
    learner = CadenceLearner(min_samples=3)

    # replay prior observations so cadence survives restarts
    history = STATE / "cadence_history.json"
    if history.exists():
        try:
            for key, stamps in json.loads(history.read_text()).items():
                for ts in stamps:
                    learner.record(key, float(ts))
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # unavailable history is unknown, not fatal

    now = time.time()
    summary: dict = {"ts": now, "lanes": {}, "granted": 0, "denied": 0}
    sub = Subgraph()

    for lane, (kind, path) in SOURCES.items():
        adapter = _adapter_for(kind, path)
        report = poll_all([adapter], now=now)["reports"][0]
        observations = report["observations"]
        for obs in observations:
            learner.record(f"{lane}:{obs['key']}", obs["ts"])

        scenario = envelope.lane(lane).scenarios_preapproved[0]
        decision = request_crossing(
            lane=lane, action="observe", scenario=scenario,
            originating_agent=AGENT, envelope=envelope, ledger=ledger,
            requested_multiplier=0.0, now=now, record=True)
        # an observe-only cycle always grades itself neutral: it took no action
        ledger.grade(crossing_id=decision.crossing_id, lane=lane, agent="drgb",
                     outcome="neutral", originating_agent=AGENT,
                     originating_envelope_digest=envelope.digest)

        sub.add_lane(lane, source=report["source"], available=report["available"])
        summary["lanes"][lane] = {
            "source": report["source"], "available": report["available"],
            "observations": len(observations), "note": report["note"][:70],
            "granted": decision.granted, "reasons": decision.reasons,
        }
        summary["granted" if decision.granted else "denied"] += 1

    cadence = learner.report(now=now)
    summary["cadence"] = {"keys": cadence["keys"], "healthy": cadence["healthy"],
                          "overdue": cadence["overdue"][:5],
                          "unknown": len(cadence["unknown"])}
    summary["ledger_intact"] = ledger.verify_chain()
    summary["envelope_intact"] = envelope.verify_integrity()

    history.write_text(json.dumps(
        {k: learner._history[k][-50:] for k in learner.keys()}))
    sub.export(STATE / "subgraph.json")
    (STATE / "last_cycle.json").write_text(json.dumps(summary, indent=2))
    with (STATE / "cycles.jsonl").open("a") as handle:
        handle.write(json.dumps(summary) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "lanes"},
                     indent=2))
    for lane, row in summary["lanes"].items():
        print(f"  {lane:20} avail={str(row['available']):5} "
              f"obs={row['observations']:4} granted={row['granted']} "
              f"{row['reasons']}")
    return summary


if __name__ == "__main__":
    main()
