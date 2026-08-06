import json

from observe.graph_discovery import discover_graphs, write_report

GRAPH = {"directed": False, "multigraph": False, "graph": {},
         "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
         "links": [{"source": "a", "target": "b"}],
         "hyperedges": [{"id": "h1"}], "built_at_commit": "f" * 40}


def _mkgraph(root, name, doc=GRAPH, manifest=None):
    d = root / name / "graphify-out"
    d.mkdir(parents=True, exist_ok=True)
    (d / "graph.json").write_text(json.dumps(doc))
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest))
    return d / "graph.json"


def test_no_graph_found_is_reported_not_assumed_empty(tmp_path):
    (tmp_path / "empty_project").mkdir()
    r = discover_graphs([tmp_path])
    assert r.found is False
    assert r.fallback_required is True
    assert "NO GRAPH FOUND" in r.note
    assert r.total_nodes == 0 and r.sources == []


def test_missing_root_is_an_error_not_silence(tmp_path):
    r = discover_graphs([tmp_path / "does_not_exist"])
    assert r.found is False
    assert any("root does not exist" in e for e in r.errors)


def test_discovers_and_counts_multiple_graphs(tmp_path):
    _mkgraph(tmp_path, "proj_a", manifest={"f1.md": {}, "f2.md": {}})
    _mkgraph(tmp_path, "proj_b")
    r = discover_graphs([tmp_path])
    assert r.found is True and r.fallback_required is False
    assert len(r.sources) == 2
    assert r.total_nodes == 6 and r.total_edges == 2
    src = sorted(r.sources, key=lambda s: s.path)[0]
    assert src.producer == "graphify"
    assert src.hyperedges == 1
    assert src.built_at_commit == "f" * 40
    assert src.age_seconds is not None and src.age_seconds >= 0
    assert src.manifest_entries == 2


def test_malformed_and_unreadable_graphs_are_reported(tmp_path):
    d = tmp_path / "bad" / "graphify-out"
    d.mkdir(parents=True)
    (d / "graph.json").write_text("{not json")
    d2 = tmp_path / "wrongshape" / "graphify-out"
    d2.mkdir(parents=True)
    (d2 / "graph.json").write_text(json.dumps({"nodes": "not-a-list"}))
    r = discover_graphs([tmp_path])
    assert r.found is False          # nothing usable
    statuses = {s.status for s in r.sources}
    assert statuses == {"malformed"}
    assert len(r.errors) == 2        # both surfaced, neither skipped
    assert r.fallback_required is True


def test_usable_and_broken_sources_coexist(tmp_path):
    _mkgraph(tmp_path, "good")
    bad = tmp_path / "bad" / "graphify-out"
    bad.mkdir(parents=True)
    (bad / "graph.json").write_text("{")
    r = discover_graphs([tmp_path])
    assert r.found is True           # the good one is adopted
    assert r.total_nodes == 3
    assert any(s.status == "malformed" for s in r.sources)  # the bad one still reported


def test_depth_limit_respected(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "graphify-out"
    deep.mkdir(parents=True)
    (deep / "graph.json").write_text(json.dumps(GRAPH))
    assert discover_graphs([tmp_path], max_depth=2).found is False
    assert discover_graphs([tmp_path], max_depth=8).found is True


def test_report_round_trips_to_disk(tmp_path):
    _mkgraph(tmp_path, "proj")
    r = discover_graphs([tmp_path])
    out = write_report(r, tmp_path / "out" / "discovery.json")
    doc = json.loads(out.read_text())
    assert doc["found"] is True
    assert doc["sources"][0]["nodes"] == 3
    assert doc["total_edges"] == 1


def test_dated_snapshots_collapse_to_one_current_graph(tmp_path):
    """graphify writes <repo>/graphify-out/<date>/graph.json snapshots; they
    must not be counted as separate environments."""
    import os
    repo = tmp_path / "proj" / "graphify-out"
    (repo).mkdir(parents=True)
    (repo / "graph.json").write_text(json.dumps(GRAPH))
    for i, day in enumerate(("2026-06-01", "2026-06-02")):
        d = repo / day
        d.mkdir()
        (d / "graph.json").write_text(json.dumps(GRAPH))
        os.utime(d / "graph.json", (1000 + i, 1000 + i))  # older than current
    r = discover_graphs([tmp_path])
    assert len(r.sources) == 3            # all three found and reported
    assert r.current_sources == 1         # only the freshest counts
    assert r.superseded_snapshots == 2
    assert r.total_nodes == 3             # NOT 9
    assert "collapsed" in r.note
    roots = {s.graph_root for s in r.sources}
    assert roots == {str(tmp_path / "proj")}


def test_separate_repos_are_not_collapsed_together(tmp_path):
    _mkgraph(tmp_path, "repo_a")
    _mkgraph(tmp_path, "repo_b")
    r = discover_graphs([tmp_path])
    assert r.current_sources == 2 and r.superseded_snapshots == 0
    assert r.total_nodes == 6
