import json

import pytest

from drgb.ledger import Ledger
from drgb.scope import LaneCandidate, ScopeMap
from drgb.subgraph import (
    HOST_GRAPH_DIRNAME,
    NAMESPACE,
    Subgraph,
    SubgraphError,
    is_namespaced,
)


AG = dict(originating_agent="hermes_local",
          originating_envelope_digest="deadbeef")


def _cross(ledger, crossing_id, lane="lane_a", **extra):
    return ledger.record_crossing(
        lane=lane, agent="executor", scenario="s", crossing_id=crossing_id,
        granted_multiplier=1.0, **{**AG, **extra}
    )


def _grade(ledger, crossing_id, outcome, lane="lane_a", **extra):
    return ledger.grade(
        crossing_id=crossing_id, lane=lane, agent="executor", outcome=outcome,
        **{**AG, **extra}
    )


def test_node_ids_are_namespaced():
    graph = Subgraph()
    lane = graph.add_lane("lane_a")
    crossing = graph.add_crossing("c1", "lane_a", "hermes_local")
    trust = graph.add_trust("lane_a", "hermes_local")
    divergence = graph.add_divergence("c1")

    assert NAMESPACE == "drgb"
    assert {lane, crossing, trust, divergence} == {
        "drgb:lane:lane_a",
        "drgb:crossing:c1",
        "drgb:trust:lane_a@hermes_local",
        "drgb:divergence:c1",
    }
    assert all(is_namespaced(node["id"]) for node in graph.nodes)
    assert is_namespaced("graphify:node") is False


def test_bad_relation_is_rejected():
    graph = Subgraph()
    lane = graph.add_lane("lane_a")

    with pytest.raises(SubgraphError):
        graph.link(lane, lane, "related_to")


def test_dangling_edge_is_rejected():
    graph = Subgraph()
    lane = graph.add_lane("lane_a")

    with pytest.raises(SubgraphError):
        graph.link(lane, "drgb:lane:missing", "observed_by")


def test_from_ledger_on_populated_ledger(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    _cross(ledger, "c0")
    _grade(ledger, "c0", "success")
    _cross(ledger, "c1")
    _grade(ledger, "c1", "failure")
    scope = ScopeMap(
        generated_at=123.0,
        sources_adopted=1,
        lanes=[LaneCandidate(
            name="lane_a", graph_root="/host/lane_a", node_count=2,
            edge_count=1, external_edges=0, blast_radius="mapped",
            ceiling_eligible=True,
        )],
    )

    graph = Subgraph.from_ledger(ledger, scope)
    node_ids = {node["id"] for node in graph.nodes}
    relations = [edge["relation"] for edge in graph.edges]

    assert len(graph.nodes) == 4
    assert len(graph.edges) == 5
    assert node_ids == {
        "drgb:lane:lane_a",
        "drgb:crossing:c0",
        "drgb:crossing:c1",
        "drgb:trust:lane_a@hermes_local",
    }
    assert relations.count("originates_from") == 2
    assert relations.count("graded_by") == 2
    assert relations.count("observed_by") == 1
    assert all(is_namespaced(node_id) for node_id in node_ids)


def test_from_ledger_on_empty_ledger_returns_valid_empty_subgraph(tmp_path):
    ledger = Ledger(tmp_path / "empty.jsonl")

    graph = Subgraph.from_ledger(ledger)

    assert graph.nodes == []
    assert graph.edges == []
    assert graph.to_dict()["nodes"] == []
    assert graph.to_dict()["links"] == []


def test_export_refuses_a_path_inside_graphify_out(tmp_path):
    graph = Subgraph()
    target = tmp_path / HOST_GRAPH_DIRNAME / "subgraph.json"

    with pytest.raises(SubgraphError):
        graph.export(target)
    assert target.exists() is False


def test_export_refuses_to_overwrite_a_foreign_graph_json(tmp_path):
    target = tmp_path / "graph.json"
    target.write_text(json.dumps({
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [],
        "links": [],
        "producer": "graphify",
    }))

    with pytest.raises(SubgraphError):
        Subgraph().export(target)


def test_export_load_round_trip_preserves_nodes_and_edges(tmp_path):
    graph = Subgraph()
    lane = graph.add_lane("lane_a", graph_root="local")
    crossing = graph.add_crossing("c1", "lane_a", "hermes_local")
    trust = graph.add_trust("lane_a", "hermes_local")
    divergence = graph.add_divergence("c1", reason="shadow_mismatch")
    graph.link(crossing, lane, "originates_from")
    graph.link(crossing, trust, "graded_by")
    graph.link(trust, lane, "observed_by")
    graph.link(divergence, crossing, "diverges_from")

    path = graph.export(tmp_path / "drgb-subgraph.json")
    loaded = Subgraph.load(path)
    document = json.loads(path.read_text())

    assert loaded.nodes == graph.nodes
    assert loaded.edges == graph.edges
    assert document["producer"] == NAMESPACE
    assert document["namespace"] == NAMESPACE
    assert set(document) == {
        "directed", "multigraph", "graph", "nodes", "links",
        "producer", "namespace",
    }


def test_loading_a_foreign_document_raises(tmp_path):
    path = tmp_path / "foreign.json"
    path.write_text(json.dumps({
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [],
        "links": [],
        "producer": "graphify",
    }))

    with pytest.raises(SubgraphError):
        Subgraph.load(path)


def test_write_isolation_is_case_insensitive(tmp_path):
    """DRGB installs on case-insensitive filesystems (macOS/Windows) where
    GRAPHIFY-OUT and graphify-out are the same directory. A case-sensitive
    guard is a real bypass there.

    Regression guard for a leak found by adversarial probe.
    """
    subgraph = Subgraph()
    subgraph.add_lane("l1")

    for variant in ("GRAPHIFY-OUT", "Graphify-Out", "graphify-OUT"):
        target = tmp_path / "repo" / variant / "drgb.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SubgraphError):
            subgraph.export(target)
        assert not target.exists()


def test_foreign_graph_guard_is_case_insensitive(tmp_path):
    subgraph = Subgraph()
    subgraph.add_lane("l1")
    foreign = tmp_path / "plain" / "GRAPH.JSON"
    foreign.parent.mkdir(parents=True)
    foreign.write_text(json.dumps({"nodes": [], "links": []}))
    with pytest.raises(SubgraphError):
        subgraph.export(foreign)
