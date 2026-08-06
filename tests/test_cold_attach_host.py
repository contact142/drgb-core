"""Phase 0.4 — cold attach against the REAL host.

Proves the bootstrap works with zero configuration on a machine DRGB has
never been installed on: discover existing graphs -> build a scope map ->
grow and round-trip its own write-isolated subgraph.

Skips (never fails) when run on a host with no graphs — a fresh clone of this
repo on someone else's machine must not report a false failure. That skip is
itself the "unavailable != zero" rule applied to the test suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drgb.ledger import Ledger
from drgb.scope import build_scope, write_scope
from drgb.subgraph import Subgraph, SubgraphError
from observe.graph_discovery import discover_graphs

HOST_ROOTS = [Path.home() / "projects"]


@pytest.fixture(scope="module")
def host_discovery():
    report = discover_graphs(HOST_ROOTS)
    if not report.found:
        pytest.skip(f"no graphs on this host: {report.note}")
    return report


def test_cold_attach_discovers_host_graphs(host_discovery):
    report = host_discovery
    assert report.current_sources >= 1
    assert report.total_nodes > 0 and report.total_edges > 0
    assert report.fallback_required is False
    # dated snapshots must be collapsed, never multiply-counted
    assert report.current_sources <= len(report.sources)
    roots = {s.graph_root for s in report.sources if not s.superseded and s.is_usable()}
    assert len(roots) == report.current_sources, "one current graph per root"


def test_cold_attach_builds_a_scope_map_with_eligibility(tmp_path, host_discovery):
    scope = build_scope(report=host_discovery)
    assert scope.sources_adopted == host_discovery.current_sources
    assert len(scope.lanes) == scope.sources_adopted
    for lane in scope.lanes:
        assert lane.blast_radius in {"mapped", "partial", "unmapped"}
        # eligibility is never granted without a fully mapped blast radius
        assert lane.ceiling_eligible == (lane.blast_radius == "mapped")
        if not lane.ceiling_eligible:
            assert lane.reasons, f"{lane.name} ineligible without a reason"
    out = write_scope(scope, tmp_path / "scope.json")
    assert json.loads(out.read_text())["sources_adopted"] == scope.sources_adopted


def test_cold_attach_grows_and_round_trips_its_own_subgraph(tmp_path, host_discovery):
    scope = build_scope(report=host_discovery)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    for i, lane in enumerate(scope.lanes[:3]):
        ledger.record_crossing(lane=lane.name, agent="drgb", scenario="observe",
                               crossing_id=f"cold{i}", granted_multiplier=0.0,
                               originating_agent="drgb_bootstrap",
                               originating_envelope_digest="cold-attach")
        ledger.grade(crossing_id=f"cold{i}", lane=lane.name, agent="drgb",
                     outcome="neutral", originating_agent="drgb_bootstrap",
                     originating_envelope_digest="cold-attach")
    assert ledger.verify_chain() is True

    sub = Subgraph.from_ledger(ledger, scope=scope)
    assert sub.nodes, "subgraph should have grown from the host scope"
    assert all(str(n["id"]).startswith("drgb:") for n in sub.nodes)

    exported = sub.export(tmp_path / "drgb_state" / "subgraph.json")
    doc = json.loads(Path(exported).read_text())
    assert doc["producer"] == "drgb" and doc["namespace"] == "drgb"

    reloaded = Subgraph.load(exported)
    assert len(reloaded.nodes) == len(sub.nodes)
    assert len(reloaded.edges) == len(sub.edges)


def test_cold_attach_never_writes_into_a_real_host_graph(host_discovery):
    """The strongest isolation check: try to export into an actual graph dir
    on THIS machine. Must refuse, and must not create the file."""
    real_root = next(Path(s.graph_root) for s in host_discovery.sources
                     if s.is_usable())
    target = real_root / "graphify-out" / "drgb_should_never_appear.json"
    sub = Subgraph()
    sub.add_lane("probe")
    with pytest.raises(SubgraphError):
        sub.export(target)
    assert not target.exists(), "DRGB wrote into a host graph directory"
