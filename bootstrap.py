#!/usr/bin/env python3
"""First-install bootstrap: study the environment, then ask what only a
system that looked could ask.

    venv/bin/python bootstrap.py [--answer key=value ...]

Order matters and is the whole point:
  1. DISCOVER  existing graphs / second brain — adopt, never re-derive.
  2. SCOPE     lanes and blast radius; unmapped means ceiling-ineligible.
  3. OBSERVE   what event sources actually answer; unavailable != idle.
  4. PREFLIGHT what would silently stop this system from evolving.
  5. INTERVIEW the few questions the findings provoked, each citing evidence.
  6. WRITE     an install record with the answers and the safe defaults used.

Nothing is granted, nothing outside ~/.drgb/install is written, and the
install never hangs: unanswered questions fall back to the restrictive
default and are reported as defaulted.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from drgb.bridge import request_crossing
from drgb.envelope import load_envelope
from drgb.interview import Interview, build_questions
from drgb.ledger import Ledger
from drgb.preflight import run_preflight
from drgb.scope import build_scope
from observe.adapters import SystemdTimerAdapter, poll_all

STATE = Path.home() / ".drgb" / "install"
DEFAULT_ENVELOPE = Path(__file__).resolve().parent / "envelopes" / "hermes_local.yaml"


def main() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", default=str(DEFAULT_ENVELOPE))
    parser.add_argument("--agent", default="installer")
    parser.add_argument("--answer", action="append", default=[],
                        metavar="key=value")
    parser.add_argument("--max-questions", type=int, default=6)
    args = parser.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    now = time.time()

    print("1/5 discovering existing graphs...")
    scope = build_scope()
    print(f"    adopted {scope.sources_adopted} graph source(s), "
          f"{scope.total_nodes} nodes; "
          f"{sum(1 for l in scope.lanes if not l.ceiling_eligible)} unmapped")

    print("2/5 polling event sources...")
    adapters = poll_all([SystemdTimerAdapter()], now=now)
    print(f"    {adapters['available']}/{adapters['sources']} available, "
          f"{adapters['observations']} observations")

    print("3/5 preflighting for evolution blockers...")
    envelope = load_envelope(args.envelope, violations_log=STATE / "violations.jsonl")
    ledger = Ledger(STATE / "ledger.jsonl")
    dependencies = {o["key"]: 1 for r in adapters["reports"]
                    for o in r.get("observations", [])}
    preflight = run_preflight(envelope=envelope, ledger=ledger,
                              agent=args.agent, bridge_fn=request_crossing,
                              dependencies=dependencies, now=now)
    print(f"    verdict={preflight['verdict']} "
          f"can_evolve={preflight['can_evolve']} counts={preflight['counts']}")

    print("4/5 building questions from what was found...")
    peers = [p.stem for p in (Path.home() / ".drgb" / "braid").glob("*.json")]
    questions = build_questions(scope=scope, preflight=preflight,
                                adapters=adapters, peers=peers,
                                max_questions=args.max_questions)
    interview = Interview(questions, store=STATE / "interview.json")
    for pair in args.answer:
        key, _, value = pair.partition("=")
        interview.answer(key, value)

    print(f"\n=== {len(questions)} question(s) for the operator ===\n")
    print(interview.render() or "  (environment produced no questions)")

    defaulted = interview.unanswered()
    record = {
        "generated_at": now,
        "scope": {"sources": scope.sources_adopted, "nodes": scope.total_nodes,
                  "lanes": [l.name for l in scope.lanes]},
        "adapters": {"available": adapters["available"],
                     "unavailable": adapters["unavailable"]},
        "preflight": {"verdict": preflight["verdict"],
                      "can_evolve": preflight["can_evolve"],
                      "counts": preflight["counts"]},
        "questions": [q.to_dict() for q in questions],
        "resolved": interview.resolved(),
        "defaulted_because_unanswered": defaulted,
        "note": ("install proceeds with safe defaults for anything unanswered; "
                 "answer later with --answer key=value and re-run"),
    }
    (STATE / "install_record.json").write_text(json.dumps(record, indent=2,
                                                          sort_keys=True))
    print(f"\n5/5 install record -> {STATE / 'install_record.json'}")
    if defaulted:
        print(f"    DEFAULTED (unanswered): {', '.join(defaulted)}")
    return record


if __name__ == "__main__":
    main()
