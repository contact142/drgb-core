"""Bootstrap preflight: find what would stop this system from evolving.

Derek's rule (2026-08-06): *handle this during the bootstrap phase, so it can
evolve the moment we let it off the leash.* The session that produced DRGB
lost hours to blockers that were discoverable on day one and only surfaced
later — a canary role that is never naturally invoked, a review process hung
on stdin, an agent whose evidence store it can itself rewrite, a proxy hop
that nothing reads.

So the check runs at bootstrap, before any authority exists, and answers one
question: **if we released this system right now, what would silently stop it
from making progress?**

Blockers are graded, never hidden:
  * ``fatal``   — evolution is impossible (no evidence store, unreadable envelope)
  * ``blocking``— a lane can never earn its ceiling as configured
  * ``degraded``— it will work but slowly or blindly
  * ``ok``
Anything unmeasurable is reported ``unknown``, never assumed fine.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

FATAL, BLOCKING, DEGRADED, OK, UNKNOWN = (
    "fatal", "blocking", "degraded", "ok", "unknown")
SEVERITY_ORDER = {FATAL: 0, BLOCKING: 1, DEGRADED: 2, UNKNOWN: 3, OK: 4}


@dataclass
class Finding:
    check: str
    status: str
    detail: str
    remedy: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(check, status, detail, remedy="", **data) -> Finding:
    return Finding(check, status, detail, remedy, data)


def check_evidence_store(ledger, *, observed_host_can_write: bool | None = None
                         ) -> Finding:
    """An agent that can rewrite its own ledger can never be bound by it."""
    if ledger is None:
        return _f("evidence_store", FATAL, "no ledger provided",
                  "construct a Ledger before requesting any crossing")
    try:
        intact = ledger.verify_chain()
    except Exception as exc:
        return _f("evidence_store", FATAL, f"ledger unverifiable: {exc}",
                  "repair or re-seed the ledger before granting anything")
    if not intact:
        return _f("evidence_store", BLOCKING, "ledger chain does not verify",
                  "investigate tampering/truncation; trust reads as unknown")
    if observed_host_can_write is None:
        return _f("evidence_store", UNKNOWN,
                  "cannot tell whether the observed host can rewrite this store",
                  "state it explicitly at bootstrap")
    if observed_host_can_write:
        return _f("evidence_store", BLOCKING,
                  "the observed host can rewrite its own evidence store",
                  "move the ledger off-host, sign heads, or replicate to peers")
    return _f("evidence_store", OK, "evidence store is outside the observed host's reach")


def check_lanes_can_ever_earn(envelope, ledger, agent: str,
                              cadence_report: dict | None = None) -> list[Finding]:
    """A lane whose threshold cannot be reached at its observed rate is not
    'slow to promote' — it is permanently stuck, and should say so on day one."""
    findings: list[Finding] = []
    for lane in envelope.lanes():
        cfg = envelope.lane(lane)
        if cfg.ceiling <= 0:
            findings.append(_f("lane_ceiling", OK,
                               f"{lane}: observe-only by design", lane=lane))
            continue
        if not cfg.scenarios_preapproved:
            findings.append(_f("lane_scenarios", BLOCKING,
                               f"{lane}: non-zero ceiling but NO approved scenario",
                               "approve a scenario or set the ceiling to 0.0",
                               lane=lane))
        try:
            trust = ledger.trust(lane, originating_agent=agent)
        except Exception as exc:
            findings.append(_f("lane_trust", UNKNOWN,
                               f"{lane}: trust unreadable: {exc}", lane=lane))
            continue
        needed = max(0, cfg.min_crossings - int(trust.get("n_graded") or 0))
        if needed == 0:
            continue
        rate = None
        if cadence_report:
            for profile in cadence_report.get("profiles", []):
                if str(profile.get("key", "")).endswith(lane) and profile.get("interval_s"):
                    rate = float(profile["interval_s"])
                    break
        if rate is None:
            findings.append(_f("lane_promotion_eta", UNKNOWN,
                               f"{lane}: {needed} more crossings needed, cadence unknown",
                               "let cadence learn, then re-run preflight", lane=lane))
        else:
            eta_h = needed * rate / 3600.0
            status = DEGRADED if eta_h > 24 * 30 else OK
            findings.append(_f("lane_promotion_eta", status,
                               f"{lane}: {needed} crossings ~= {eta_h:.1f}h at observed cadence",
                               "lower min_crossings or raise cadence if this is too slow",
                               lane=lane, eta_hours=eta_h))
    return findings


def check_never_invoked_dependencies(dependencies: dict[str, int]) -> list[Finding]:
    """Canary problem: something the plan waits on may never happen by itself.

    ``dependencies`` maps a name to how many times it has been observed.
    """
    findings = []
    for name, count in sorted(dependencies.items()):
        if count > 0:
            findings.append(_f("dependency", OK, f"{name}: observed {count}x",
                               name=name, count=count))
        else:
            findings.append(_f("dependency", BLOCKING,
                               f"{name}: NEVER observed — waiting on it may never end",
                               "trigger it deliberately or remove the dependency",
                               name=name, count=0))
    return findings


def check_exit_path(bridge_fn, envelope, ledger, agent: str) -> Finding:
    """Invariant 1 must hold at bootstrap, before anything else is trusted."""
    try:
        decision = bridge_fn(lane=next(iter(envelope.lanes())), action="abort",
                             scenario="anything", originating_agent=agent,
                             envelope=envelope, ledger=ledger, record=False)
    except Exception as exc:
        return _f("exit_path", FATAL, f"exit request raised: {exc}",
                  "exits must never raise; fix before deployment")
    if not decision.granted or decision.multiplier != 1.0:
        return _f("exit_path", FATAL, "an exit was NOT granted at full authority",
                  "invariant 1 is violated; do not deploy")
    return _f("exit_path", OK, "exits are ungated")


def run_preflight(*, envelope, ledger, agent: str, bridge_fn,
                  dependencies: dict[str, int] | None = None,
                  cadence_report: dict | None = None,
                  observed_host_can_write: bool | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """Everything that could silently stop evolution, graded, at bootstrap."""
    findings: list[Finding] = [
        check_exit_path(bridge_fn, envelope, ledger, agent),
        check_evidence_store(ledger,
                             observed_host_can_write=observed_host_can_write),
    ]
    findings += check_lanes_can_ever_earn(envelope, ledger, agent, cadence_report)
    findings += check_never_invoked_dependencies(dependencies or {})

    worst = min((SEVERITY_ORDER[f.status] for f in findings), default=4)
    verdict = next(k for k, v in SEVERITY_ORDER.items() if v == worst)
    return {
        "generated_at": float(now if now is not None else time.time()),
        "agent": agent,
        "verdict": verdict,
        "can_evolve": verdict not in {FATAL, BLOCKING},
        "counts": {level: sum(1 for f in findings if f.status == level)
                   for level in (FATAL, BLOCKING, DEGRADED, UNKNOWN, OK)},
        "findings": [f.to_dict() for f in findings],
    }
