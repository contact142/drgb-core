"""First-install interview — informed questions, safe defaults, no stalling."""

from __future__ import annotations

import pytest

from drgb.interview import CRITICAL, Interview, build_questions


class _Lane:
    def __init__(self, name, eligible=True):
        self.name, self.ceiling_eligible = name, eligible


class _Scope:
    def __init__(self, lanes):
        self.lanes = lanes


def _preflight(*findings):
    return {"findings": list(findings)}


def test_no_findings_yields_no_noise():
    assert build_questions() == []


def test_a_self_writable_evidence_store_becomes_a_critical_question():
    questions = build_questions(preflight=_preflight(
        {"check": "evidence_store", "status": "blocking",
         "detail": "the observed host can rewrite its own evidence store"}))
    assert len(questions) == 1
    q = questions[0]
    assert q.priority == CRITICAL and q.key == "evidence_store_location"
    assert "rewrite its own evidence" in q.observed      # cites what it saw
    assert q.default == "remote_or_append_only"          # safe default
    assert "proves nothing" in q.consequence


def test_the_canary_trap_becomes_a_question_instead_of_a_silent_stall():
    questions = build_questions(preflight=_preflight(
        {"check": "dependency", "status": "blocking",
         "detail": "profile_describer: NEVER observed",
         "data": {"name": "profile_describer"}}))
    q = questions[0]
    assert q.key == "dependency_profile_describer"
    assert q.default == "remove_the_dependency"
    assert "canary trap" in q.consequence


def test_questions_are_ranked_and_capped():
    findings = [{"check": "dependency", "status": "blocking",
                 "detail": f"dep{i}", "data": {"name": f"dep{i}"}}
                for i in range(10)]
    questions = build_questions(preflight=_preflight(*findings),
                                max_questions=3)
    assert len(questions) == 3
    assert all(q.priority == CRITICAL for q in questions)


def test_criticals_come_before_optionals():
    questions = build_questions(
        preflight=_preflight({"check": "evidence_store", "status": "blocking",
                              "detail": "d"}),
        peers=["peer_a"])
    assert questions[0].priority == CRITICAL
    assert questions[-1].key == "braid_sharing"


def test_scope_produces_a_money_lane_question_naming_real_lanes():
    scope = _Scope([_Lane("trading"), _Lane("research"), _Lane("dark", False)])
    questions = build_questions(scope=scope)
    money = next(q for q in questions if q.key == "money_lanes")
    assert "trading" in q_observed(money) and "research" in q_observed(money)
    assert money.default == "all_treated_as_irreversible"
    unmapped = next(q for q in questions if q.key == "unmapped_lanes")
    assert "dark" in unmapped.observed
    assert unmapped.default == "leave_ineligible"


def q_observed(question):
    return question.observed


def test_unavailable_sources_are_surfaced_not_ignored():
    questions = build_questions(adapters={"unavailable": ["jsonl:gone.jsonl"]})
    q = questions[0]
    assert q.key == "unavailable_sources"
    assert q.default == "fix_before_continuing"
    assert "never idle" in q.consequence


def test_answers_are_validated_against_options():
    interview = Interview(build_questions(peers=["p"]))
    with pytest.raises(ValueError):
        interview.answer("braid_sharing", "nonsense")
    with pytest.raises(KeyError):
        interview.answer("no_such_key", "share_attributed")
    interview.answer("braid_sharing", "isolate")
    assert interview.resolved()["braid_sharing"] == "isolate"


def test_unanswered_questions_fall_back_to_the_safe_default():
    """An install must never hang waiting for an operator — but it must say
    what it defaulted."""
    interview = Interview(build_questions(preflight=_preflight(
        {"check": "evidence_store", "status": "blocking", "detail": "d"})))
    assert interview.can_proceed() is True
    assert interview.blocking_unanswered() == ["evidence_store_location"]
    assert interview.resolved()["evidence_store_location"] == "remote_or_append_only"


def test_answers_persist_across_restarts(tmp_path):
    store = tmp_path / "interview.json"
    first = Interview(build_questions(peers=["p"]), store=store)
    first.answer("braid_sharing", "isolate")
    second = Interview(build_questions(peers=["p"]), store=store)
    assert second.resolved()["braid_sharing"] == "isolate"
    assert second.unanswered() == []


def test_render_shows_evidence_and_consequence(tmp_path):
    interview = Interview(build_questions(preflight=_preflight(
        {"check": "evidence_store", "status": "blocking",
         "detail": "host can rewrite its own store"})))
    text = interview.render()
    assert "observed:" in text and "effect  :" in text
    assert "host can rewrite its own store" in text


def test_multi_select_questions_accept_several_options():
    """The money-lanes question is inherently multi-answer: a fleet has more
    than one money lane, and forcing a single pick misrepresents it."""
    scope = _Scope([_Lane("trading"), _Lane("haveno"), _Lane("docs")])
    interview = Interview(build_questions(scope=scope))
    interview.answer("money_lanes", "trading, haveno")
    assert interview.resolved()["money_lanes"] == "trading, haveno"
    with pytest.raises(ValueError):
        interview.answer("money_lanes", "trading, not_a_lane")
    with pytest.raises(ValueError):
        interview.answer("money_lanes", "  ")


def test_single_select_questions_still_reject_lists():
    interview = Interview(build_questions(peers=["p"]))
    with pytest.raises(ValueError):
        interview.answer("braid_sharing", "share_attributed, isolate")
