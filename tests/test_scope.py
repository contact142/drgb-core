import json

from drgb.scope import build_scope, write_scope
from observe.graph_discovery import DiscoveryReport, GraphSource


GRAPH = {
    "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
    "links": [{"source": "a", "target": "b"},
              {"source": "b", "target": "c"}],
}


def _mkgraph(root, name, doc=GRAPH):
    directory = root / name / "graphify-out"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "graph.json"
    path.write_text(json.dumps(doc))
    return path


def test_mapped_graph_is_ceiling_eligible(tmp_path):
    _mkgraph(tmp_path, "mapped")
    scope = build_scope(roots=[tmp_path])

    assert len(scope.lanes) == 1
    lane = scope.lanes[0]
    assert lane.name == "mapped"
    assert lane.graph_root == str(tmp_path / "mapped")
    assert (lane.node_count, lane.edge_count, lane.external_edges) == (3, 2, 0)
    assert lane.blast_radius == "mapped"
    assert lane.ceiling_eligible is True
    assert lane.reasons == []
    assert scope.unmapped_lanes == []


def test_dangling_endpoint_is_partial_and_not_eligible(tmp_path):
    _mkgraph(tmp_path, "partial", {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "links": [{"source": "a", "target": "missing"},
                  {"source": "a", "target": "b"}],
    })
    scope = build_scope(roots=[tmp_path])

    lane = scope.lanes[0]
    assert lane.blast_radius == "partial"
    assert lane.ceiling_eligible is False
    assert lane.external_edges == 1
    assert lane.reasons
    assert lane.name in scope.unmapped_lanes


def test_malformed_graph_is_unmapped_and_not_eligible(tmp_path):
    path = _mkgraph(tmp_path, "broken")
    path.write_text("{not json")
    source = GraphSource(
        path=str(path), producer="node-link", nodes=2, edges=1,
        hyperedges=None, built_at_commit=None, mtime=None,
        age_seconds=None, manifest_entries=None, status="ok",
        graph_root=str(tmp_path / "broken"),
    )
    report = DiscoveryReport(
        generated_at=123.0, roots=[str(tmp_path)], sources=[source], found=True,
    )

    scope = build_scope(report)

    lane = scope.lanes[0]
    assert lane.blast_radius == "unmapped"
    assert lane.ceiling_eligible is False
    assert lane.reasons and "malformed" in lane.reasons[0]


def test_empty_discovery_requires_live_fallback(tmp_path):
    scope = build_scope(roots=[tmp_path])

    assert scope.lanes == []
    assert scope.unmapped_lanes == []
    assert scope.sources_adopted == 0
    assert "live-discovery fallback required" in scope.discovery_note.lower()


def test_index_style_links_resolve_to_node_ids(tmp_path):
    _mkgraph(tmp_path, "indexed", {
        "nodes": [{"id": "first"}, {"id": "second"}],
        "links": [{"source": 0, "target": 1}],
    })
    scope = build_scope(roots=[tmp_path])

    lane = scope.lanes[0]
    assert lane.blast_radius == "mapped"
    assert lane.external_edges == 0
    assert lane.ceiling_eligible is True


def test_write_scope_round_trips(tmp_path):
    _mkgraph(tmp_path, "mapped")
    scope = build_scope(roots=[tmp_path])
    output = write_scope(scope, tmp_path / "out" / "scope.json")

    assert output == tmp_path / "out" / "scope.json"
    document = json.loads(output.read_text())
    assert document == scope.to_dict()
    assert output.read_text().endswith("\n")
    assert list(document) == sorted(document)


def test_degenerate_graphs_never_confer_eligibility(tmp_path):
    """A 1-node/0-edge graph resolves trivially; it must not be 'mapped'.

    Regression guard for a permissive hole found by adversarial probe:
    without a minimum-topology rule, an empty-ish graph would make a lane
    ceiling-eligible without proving anything about its blast radius.
    """
    _mkgraph(tmp_path, "solo", {"nodes": [{"id": "a"}], "links": []})
    _mkgraph(tmp_path, "pair_no_edges",
             {"nodes": [{"id": "a"}, {"id": "b"}], "links": []})
    _mkgraph(tmp_path, "one_node_self_edge",
             {"nodes": [{"id": "a"}], "links": [{"source": "a", "target": "a"}]})

    scope = build_scope(roots=[tmp_path])
    by_name = {lane.name: lane for lane in scope.lanes}

    for name in ("solo", "pair_no_edges", "one_node_self_edge"):
        lane = by_name[name]
        assert lane.blast_radius == "unmapped", name
        assert lane.ceiling_eligible is False, name
        assert lane.reasons, name
        assert name in scope.unmapped_lanes
