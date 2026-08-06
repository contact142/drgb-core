"""5.5 — the mesh carries evidence, and catches a peer that rewinds itself."""

from __future__ import annotations

import json

import pytest

from drgb.envelope import load_envelope
from drgb.ledger import Ledger
from drgb.mesh import MeshError, MeshView, publish

ENV = """
lanes:
  trade:
    scenarios_preapproved: [s]
    ceiling: 1.0
    min_crossings: 3
"""


@pytest.fixture
def envelope(tmp_path):
    p = tmp_path / "env.yaml"
    p.write_text(ENV)
    return load_envelope(p, violations_log=tmp_path / "v.jsonl")


def _ledger(tmp_path, name, wins=5, fails=0, agent="peer_a"):
    led = Ledger(tmp_path / name)
    for i in range(wins + fails):
        led.record_crossing(lane="trade", agent="x", scenario="s",
                            crossing_id=f"c{i}", granted_multiplier=1.0,
                            originating_agent=agent,
                            originating_envelope_digest="d")
        led.grade(crossing_id=f"c{i}", lane="trade", agent="x",
                  outcome="success" if i < wins else "failure",
                  originating_agent=agent, originating_envelope_digest="d")
    return led


def test_published_document_carries_trust_and_head(tmp_path, envelope):
    led = _ledger(tmp_path, "a.jsonl", wins=4, fails=1)
    doc = publish(led, envelope, "peer_a", ["trade"])
    assert doc.agent == "peer_a" and doc.document_digest
    lane = doc.lanes[0]
    assert lane.lane == "trade" and lane.n_graded == 5 and lane.failures == 1
    assert doc.ledger_head["count"] > 0
    assert doc.ledger_head["chain_verified"] is True
    assert doc.envelope_digest == envelope.digest


def test_mesh_documents_contain_no_verbs_only_evidence(tmp_path, envelope):
    """The mesh cannot carry a request — there is no field for one."""
    doc = publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"])
    payload = json.dumps(doc.to_dict()).lower()
    for verb in ("please", "request", "action", "execute", "grant", "approve"):
        assert verb not in payload


def test_ingest_rejects_tampered_documents(tmp_path, envelope):
    view = MeshView()
    doc = publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"]).to_dict()
    doc["lanes"][0]["successes"] = 9999          # inflate after signing
    with pytest.raises(MeshError):
        view.ingest(doc)


def test_ingest_rejects_unknown_schema_and_anonymous_documents(tmp_path, envelope):
    view = MeshView()
    doc = publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"]).to_dict()
    bad_schema = {**doc, "schema": "something/else"}
    with pytest.raises(MeshError):
        view.ingest(bad_schema)
    with pytest.raises(MeshError):
        view.ingest({**doc, "agent": ""})


def test_peer_evidence_is_explicitly_not_usable_as_authority(tmp_path, envelope):
    view = MeshView()
    view.ingest(publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"]))
    evidence = view.evidence_for("trade")
    assert evidence and evidence[0]["agent"] == "peer_a"
    assert evidence[0]["usable_as_own_authority"] is False


def test_a_peer_that_rewinds_its_ledger_is_caught_by_the_mesh(tmp_path, envelope):
    """The documented evidence-store limit, mitigated: an agent may forge a
    consistent shorter history locally, but it disagrees with what peers
    already saw."""
    led = _ledger(tmp_path, "a.jsonl", wins=3, fails=3)
    view = MeshView(store=tmp_path / "mesh.json")
    assert view.ingest(publish(led, envelope, "peer_a", ["trade"])) == {}

    # peer deletes its failures and recomputes its own head — locally valid
    lines = led.path.read_text().splitlines()
    keep = lines[:6]
    led.path.write_text("\n".join(keep) + "\n")
    led._write_head(len(keep), json.loads(keep[-1])["row_hash"])
    assert led.verify_chain() is True          # convincing to itself

    alert = view.ingest(publish(led, envelope, "peer_a", ["trade"]))
    assert alert["kind"] == "ledger_rewound"
    assert alert["new_count"] < alert["previous_count"]


def test_head_divergence_at_the_same_count_is_caught(tmp_path, envelope):
    led = _ledger(tmp_path, "a.jsonl", wins=3)
    view = MeshView()
    view.ingest(publish(led, envelope, "peer_a", ["trade"]))
    doc = publish(led, envelope, "peer_a", ["trade"]).to_dict()
    doc["ledger_head"]["last_hash"] = "f" * 64          # same count, new hash
    doc["document_digest"] = ""                          # re-sign it honestly
    from drgb.mesh import _digest
    doc["document_digest"] = _digest({k: v for k, v in doc.items()
                                      if k != "document_digest"})
    alert = view.ingest(doc)
    assert alert["kind"] == "head_diverged_at_same_count"


def test_a_peer_reporting_a_broken_chain_raises_an_alert(tmp_path, envelope):
    led = _ledger(tmp_path, "a.jsonl", wins=3)
    led.path.write_text(led.path.read_text().replace('"success"', '"failure"', 1))
    view = MeshView()
    alert = view.ingest(publish(led, envelope, "peer_a", ["trade"]))
    assert alert["kind"] == "peer_reports_broken_chain"


def test_view_persists_across_restarts(tmp_path, envelope):
    store = tmp_path / "mesh.json"
    first = MeshView(store=store)
    first.ingest(publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"]))
    reopened = MeshView(store=store)
    assert reopened.peers() == ("peer_a",)
    assert reopened.report()["peers"] == 1


def test_report_summarises_peers_and_alerts(tmp_path, envelope):
    view = MeshView()
    view.ingest(publish(_ledger(tmp_path, "a.jsonl"), envelope, "peer_a", ["trade"]))
    view.ingest(publish(_ledger(tmp_path, "b.jsonl", agent="peer_b"),
                        envelope, "peer_b", ["trade"]))
    report = view.report()
    assert report["peers"] == 2 and report["alert_count"] == 0
    assert set(report["agents"]) == {"peer_a", "peer_b"}


def test_broken_chain_alerts_on_a_peers_very_first_document(tmp_path, envelope):
    """A compromised peer joining the mesh must not get a free first pass."""
    led = _ledger(tmp_path, "a.jsonl", wins=3)
    led.path.write_text(led.path.read_text().replace('"success"', '"failure"', 1))
    view = MeshView()
    assert view.peers() == ()                     # never seen before
    alert = view.ingest(publish(led, envelope, "newcomer", ["trade"]))
    assert alert["kind"] == "peer_reports_broken_chain"
    assert view.report()["alert_count"] == 1
