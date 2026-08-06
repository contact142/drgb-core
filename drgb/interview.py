"""First-install interview: the questions only a system that looked can ask.

DRGB studies an environment before it asks anything — graphs, lanes, cadences,
blast radius, blockers. This turns those findings into a SHORT list of
operator questions that could not have been written in advance, each one
carrying the evidence that provoked it.

Rules:
  * every question cites what was observed — no generic questionnaire;
  * every question states the consequence of each answer;
  * questions are ranked, and only ``max_questions`` are asked (an install
    that interrogates you for an hour will be skipped, and a skipped install
    configures nothing);
  * anything unanswered stays at the SAFE default, and the safe default is
    always the restrictive one;
  * blocking findings become questions FIRST — they are why the install
    would otherwise stall silently.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CRITICAL, IMPORTANT, OPTIONAL = "critical", "important", "optional"
RANK = {CRITICAL: 0, IMPORTANT: 1, OPTIONAL: 2}


@dataclass
class Question:
    """One informed question, with the evidence that provoked it."""

    key: str
    priority: str
    question: str
    observed: str                       # what DRGB actually saw
    options: list[str] = field(default_factory=list)
    default: str = ""
    consequence: str = ""
    answer: str | None = None
    multi: bool = False        # comma-separated selection of several options

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _q(key, priority, question, observed, options, default, consequence,
       multi=False):
    return Question(key=key, priority=priority, question=question,
                    observed=observed, options=list(options), default=default,
                    consequence=consequence, multi=multi)


def build_questions(*, scope=None, preflight: dict | None = None,
                    adapters: dict | None = None,
                    cadence: dict | None = None,
                    peers: list[str] | None = None,
                    max_questions: int = 6) -> list[Question]:
    """Derive questions from what the bootstrap actually found."""
    questions: list[Question] = []

    # --- from preflight: blockers become questions first ------------------
    for finding in (preflight or {}).get("findings", []):
        status, check = finding.get("status"), finding.get("check")
        if status not in {"blocking", "fatal"}:
            continue
        if check == "evidence_store":
            questions.append(_q(
                "evidence_store_location", CRITICAL,
                "Where should the guardian ledger live?",
                finding.get("detail", "the observed host can rewrite its own evidence"),
                ["remote_or_append_only", "peer_replicated", "local_accepted_risk"],
                "remote_or_append_only",
                "A ledger the observed system can rewrite proves nothing. "
                "'local_accepted_risk' keeps DRGB useful for observation but "
                "its tamper-evidence is void against that host."))
        elif check == "dependency":
            name = finding.get("data", {}).get("name", "a dependency")
            questions.append(_q(
                f"dependency_{name}", CRITICAL,
                f"'{name}' has never been observed — is it real?",
                finding.get("detail", ""),
                ["trigger_it_now", "remove_the_dependency", "keep_waiting"],
                "remove_the_dependency",
                "Waiting on something that never fires stalls the install "
                "silently. This is the canary trap."))
        elif check == "lane_scenarios":
            lane = finding.get("data", {}).get("lane", "a lane")
            questions.append(_q(
                f"lane_scenarios_{lane}", CRITICAL,
                f"Lane '{lane}' can never promote — approve a scenario or zero it?",
                finding.get("detail", ""),
                ["approve_a_scenario", "set_ceiling_zero"],
                "set_ceiling_zero",
                "A non-zero ceiling with nothing approved looks live but is "
                "permanently stuck."))

    # --- from scope: what is money/irreversible here? ---------------------
    lanes = [c.name for c in getattr(scope, "lanes", [])] if scope else []
    if lanes:
        questions.append(_q(
            "money_lanes", CRITICAL,
            "Which of these lanes can move money or take irreversible action?",
            f"discovered {len(lanes)} lane(s): {', '.join(lanes[:8])}"
            + ("..." if len(lanes) > 8 else ""),
            lanes + ["none"], "all_treated_as_irreversible",
            "Name every lane that touches money or takes irreversible action "
            "(comma-separated). Named lanes get those actions barred at ANY "
            "evidence level, permanently. Unnamed lanes stay restricted until "
            "surveyed — the default treats ALL of them as irreversible, which "
            "is safe but blocks everything.", multi=True))
        ineligible = [c.name for c in scope.lanes if not c.ceiling_eligible]
        if ineligible:
            questions.append(_q(
                "unmapped_lanes", IMPORTANT,
                "Survey these unmapped lanes now, or leave them ceiling-ineligible?",
                f"blast radius unmapped for: {', '.join(ineligible[:6])}",
                ["survey_now", "leave_ineligible"], "leave_ineligible",
                "An unmapped lane can never earn authority. Leaving it that "
                "way is safe; surveying it later is always allowed."))

    # --- from cadence: is promotion reachable in human time? --------------
    slow = [f for f in (preflight or {}).get("findings", [])
            if f.get("check") == "lane_promotion_eta" and f.get("status") == "degraded"]
    if slow:
        worst = max(slow, key=lambda f: f.get("data", {}).get("eta_hours", 0))
        questions.append(_q(
            "promotion_pace", IMPORTANT,
            "Promotion will take months at the observed cadence — adjust?",
            worst.get("detail", ""),
            ["lower_min_crossings", "raise_cadence", "accept_slow"],
            "accept_slow",
            "Accepting is safe but the lane stays at 0.0 for a long time. "
            "Lowering the threshold trades evidence for speed."))

    # --- from adapters: blind spots ---------------------------------------
    unavailable = (adapters or {}).get("unavailable") or []
    if unavailable:
        questions.append(_q(
            "unavailable_sources", IMPORTANT,
            "These event sources did not answer — expected, or misconfigured?",
            f"unavailable: {', '.join(map(str, unavailable[:6]))}",
            ["expected_ignore", "fix_before_continuing"], "fix_before_continuing",
            "An unavailable source is UNKNOWN, never idle. Ignoring one means "
            "accepting a blind spot on purpose."))

    # --- from peers: should knowledge be shared? --------------------------
    if peers:
        questions.append(_q(
            "braid_sharing", OPTIONAL,
            "Share attributed knowledge with the peers found here?",
            f"peers detected: {', '.join(sorted(peers)[:6])}",
            ["share_attributed", "isolate"], "share_attributed",
            "Sharing keeps agents current and lets peers catch a rewound "
            "ledger. Facts stay attributed; trust is never shared as a claim."))

    questions.sort(key=lambda q: RANK.get(q.priority, 9))
    return questions[:max_questions]


class Interview:
    """Holds the questions, the answers, and what is still safe-defaulted."""

    def __init__(self, questions: list[Question], store: str | Path | None = None):
        self.questions = list(questions)
        self.store = Path(store) if store else None
        if self.store and self.store.exists():
            try:
                saved = json.loads(self.store.read_text()).get("answers", {})
                for q in self.questions:
                    if q.key in saved:
                        q.answer = saved[q.key]
            except (OSError, json.JSONDecodeError):
                pass

    def answer(self, key: str, value: str) -> None:
        for q in self.questions:
            if q.key == key:
                if q.options:
                    picks = ([v.strip() for v in value.split(",") if v.strip()]
                             if q.multi else [value])
                    if not picks:
                        raise ValueError(f"{key}: no selection given")
                    bad = [p for p in picks if p not in q.options]
                    if bad:
                        raise ValueError(
                            f"{bad} not an option for {key}: {q.options}")
                q.answer = value
                self._persist()
                return
        raise KeyError(f"no such question: {key}")

    def resolved(self) -> dict[str, str]:
        """Every question's effective value: the answer, or the SAFE default."""
        return {q.key: (q.answer if q.answer is not None else q.default)
                for q in self.questions}

    def unanswered(self) -> list[str]:
        return [q.key for q in self.questions if q.answer is None]

    def blocking_unanswered(self) -> list[str]:
        return [q.key for q in self.questions
                if q.answer is None and q.priority == CRITICAL]

    def can_proceed(self) -> bool:
        """Install may proceed: unanswered criticals fall back to their safe
        default, so it never hangs — but it says what it defaulted."""
        return True

    def render(self) -> str:
        lines = []
        for i, q in enumerate(self.questions, 1):
            mark = "answered" if q.answer is not None else f"default: {q.default}"
            lines.append(
                f"{i}. [{q.priority}] {q.question}\n"
                f"   observed: {q.observed}\n"
                f"   options : {', '.join(q.options)}\n"
                f"   effect  : {q.consequence}\n"
                f"   -> {mark}")
        return "\n".join(lines)

    def _persist(self) -> None:
        if not self.store:
            return
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(
            {"generated_at": time.time(),
             "answers": {q.key: q.answer for q in self.questions
                         if q.answer is not None},
             "questions": [q.to_dict() for q in self.questions]},
            indent=2, sort_keys=True))
