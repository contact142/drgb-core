"""The braid: shared knowledge with provenance, and no laundering by claim."""

from __future__ import annotations

import json

import pytest

from drgb.braid import Braid, BraidError


def _doc(agent, nodes, published_at=1000.0, producer="drgb"):
    return {"producer": producer, "namespace": "drgb", "agent": agent,
            "published_at": published_at, "envelope_digest": "d",
            "nodes": nodes}


def test_merges_and_attributes_every_fact():
    braid = Braid()
    braid.contribute(_doc("agent_a", [{"id": "drgb:lane:trade", "source": "kraken"}]))
    braid.contribute(_doc("agent_b", [{"id": "drgb:lane:trade", "region": "eu"}]))

    node = braid.nodes["drgb:lane:trade"]
    assert node.attributes == {"source": "kraken", "region": "eu"}
    assert {a.agent for a in node.asserted_by} == {"agent_a", "agent_b"}
    assert braid.agents() == ("agent_a", "agent_b")


def test_disagreement_is_surfaced_not_overwritten():
    braid = Braid()
    braid.contribute(_doc("agent_a", [{"id": "drgb:lane:x", "status": "healthy"}]))
    braid.contribute(_doc("agent_b", [{"id": "drgb:lane:x", "status": "degraded"}]))

    node = braid.nodes["drgb:lane:x"]
    assert node.attributes["status"] == "healthy"      # first assertion held
    assert len(node.conflicts) == 1
    conflict = braid.conflicts()[0]
    assert conflict["held"] == "healthy" and conflict["asserted"] == "degraded"
    assert conflict["by"] == "agent_b"


def test_an_agent_cannot_assert_another_agents_trust():
    """Laundering expressed as knowledge: agent_b claiming agent_a is trusted.
    Refused — trust is self-only and must come from a ledger, not a claim."""
    braid = Braid()
    result = braid.contribute(_doc("agent_b", [
        {"id": "drgb:trust:trade@agent_a", "agent": "agent_a",
         "win_rate": 1.0, "successes": 999}]))
    assert result["rejected"] == 1
    assert "drgb:trust:trade@agent_a" not in braid.nodes
    assert "asserted_" in braid.rejected[0]["reason"]


def test_an_agent_may_assert_its_own_trust():
    braid = Braid()
    result = braid.contribute(_doc("agent_a", [
        {"id": "drgb:trust:trade@agent_a", "agent": "agent_a", "win_rate": 0.9}]))
    assert result["merged"] == 1 and result["rejected"] == 0


def test_nodes_outside_the_namespace_are_refused():
    braid = Braid()
    result = braid.contribute(_doc("agent_a", [{"id": "host:secret", "v": 1}]))
    assert result["rejected"] == 1 and not braid.nodes
    assert braid.rejected[0]["reason"] == "outside_namespace"


def test_foreign_and_anonymous_documents_are_refused():
    braid = Braid()
    with pytest.raises(BraidError):
        braid.contribute(_doc("a", [], producer="someone_else"))
    with pytest.raises(BraidError):
        braid.contribute(_doc("", []))
    with pytest.raises(BraidError):
        braid.contribute("not a document")


def test_one_bad_file_never_blocks_the_others(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps(
        _doc("agent_a", [{"id": "drgb:lane:a"}])))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "foreign.json").write_text(json.dumps(
        _doc("agent_c", [], producer="other")))

    braid = Braid()
    results = braid.contribute_all(tmp_path)
    assert len(results) == 3
    assert "drgb:lane:a" in braid.nodes                 # the good one merged
    assert len(braid.rejected) == 2                      # both bad ones recorded


def test_briefing_leads_with_conflicts_and_silence():
    braid = Braid()
    braid.contribute(_doc("chatty", [{"id": "drgb:lane:x", "status": "ok"}],
                          published_at=1000.0))
    braid.contribute(_doc("quiet", [{"id": "drgb:lane:x", "status": "bad"}],
                          published_at=100.0))

    briefing = braid.briefing(now=2000.0)
    assert briefing["conflict_count"] == 1
    assert briefing["silence_seconds"]["quiet"] == 1900.0
    assert briefing["silence_seconds"]["chatty"] == 1000.0
    assert briefing["by_kind"] == {"lane": 1}
    assert "attributed" in briefing["note"]


def test_export_refuses_to_write_into_a_host_graph_dir(tmp_path):
    braid = Braid()
    braid.contribute(_doc("a", [{"id": "drgb:lane:x"}]))
    target = tmp_path / "repo" / "graphify-out" / "braid.json"
    target.parent.mkdir(parents=True)
    with pytest.raises(BraidError):
        braid.export(target)
    assert not target.exists()


def test_export_round_trips_with_provenance(tmp_path):
    braid = Braid()
    braid.contribute(_doc("agent_a", [{"id": "drgb:lane:x", "v": 1}]))
    out = braid.export(tmp_path / "braid.json", now=5000.0)
    doc = json.loads(out.read_text())
    assert doc["producer"] == "drgb" and doc["kind"] == "braid"
    node = doc["nodes"][0]
    assert node["asserted_by"][0]["agent"] == "agent_a"
    assert doc["briefing"]["nodes"] == 1
