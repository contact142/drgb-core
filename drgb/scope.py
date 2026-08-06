"""Build a scope map from the graph sources discovered on a host.

Scope understanding is a prerequisite for authority.  A lane can only be
ceiling-eligible when every endpoint in its graph resolves inside that graph;
missing or partial topology therefore remains explicitly non-permissive.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from observe.graph_discovery import DiscoveryReport, GraphSource, discover_graphs


@dataclass
class LaneCandidate:
    """One graph-backed lane and the evidence for its blast-radius status."""

    name: str
    graph_root: str
    node_count: int
    edge_count: int
    external_edges: int
    blast_radius: str
    ceiling_eligible: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScopeMap:
    """The adopted graph scope used as a precondition for authority."""

    generated_at: float
    sources_adopted: int
    lanes: list[LaneCandidate] = field(default_factory=list)
    unmapped_lanes: list[str] = field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0
    discovery_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _GraphAssessment:
    node_count: int
    edge_count: int
    external_edges: int
    blast_radius: str
    reasons: list[str]


_UNRESOLVED = object()

# Minimum topology for a graph to establish blast radius at all. A single
# isolated node resolves trivially and must never confer ceiling eligibility.
MIN_TOPOLOGY_NODES = 2


def _malformed(node_count: int, edge_count: int, detail: str) -> _GraphAssessment:
    return _GraphAssessment(node_count, edge_count, 0, "unmapped",
                            [f"graph is malformed: {detail}"])


def _read_document(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"graph is unreadable: {exc}"
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return None, f"graph is malformed: invalid json: {exc}"
    if not isinstance(document, dict):
        return None, "graph is malformed: top level is not an object"
    return document, None


def _resolve_endpoint(endpoint: Any, node_ids: list[Any], by_id: dict[Any, int]) -> int | object:
    """Resolve a node-link endpoint as an id first, then as an index."""
    try:
        if endpoint in by_id:
            return by_id[endpoint]
    except TypeError:
        # Unhashable endpoint values cannot be node ids or indices.
        return _UNRESOLVED

    if isinstance(endpoint, int) and not isinstance(endpoint, bool):
        return endpoint if 0 <= endpoint < len(node_ids) else _UNRESOLVED

    # JSON producers sometimes serialize numeric indices as strings.  A
    # direct string id wins above, so this does not steal ids such as "01".
    if isinstance(endpoint, str):
        try:
            index = int(endpoint)
        except ValueError:
            return _UNRESOLVED
        if str(index) != endpoint:
            return _UNRESOLVED
        return index if 0 <= index < len(node_ids) else _UNRESOLVED

    return _UNRESOLVED


def _assess_graph(source: GraphSource) -> _GraphAssessment:
    path = Path(source.path)
    graph_root = str(source.graph_root)
    document, error = _read_document(path)
    if error:
        node_count = int(source.nodes or 0)
        edge_count = int(source.edges or 0)
        return _GraphAssessment(node_count, edge_count, 0, "unmapped", [error])
    assert document is not None

    nodes = document.get("nodes")
    links = document.get("links", document.get("edges"))
    if not isinstance(nodes, list) or not isinstance(links, list):
        return _malformed(
            len(nodes) if isinstance(nodes, list) else int(source.nodes or 0),
            len(links) if isinstance(links, list) else int(source.edges or 0),
            "expected list-valued 'nodes' and 'links'/'edges'",
        )

    node_ids: list[Any] = []
    by_id: dict[Any, int] = {}
    node_roots: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or "id" not in node:
            return _malformed(len(nodes), len(links),
                              f"node {index} is missing an 'id'")
        node_id = node["id"]
        try:
            if node_id in by_id:
                return _malformed(len(nodes), len(links),
                                  f"duplicate node id: {node_id!r}")
            by_id[node_id] = index
        except TypeError:
            return _malformed(len(nodes), len(links),
                              f"node {index} has an unhashable id")
        node_ids.append(node_id)
        node_roots.append(str(node.get("graph_root", graph_root)))

    external_edges = 0
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            return _malformed(len(nodes), len(links),
                              f"link {index} is not an object")
        source_index = _resolve_endpoint(link.get("source"), node_ids, by_id)
        target_index = _resolve_endpoint(link.get("target"), node_ids, by_id)
        if source_index is _UNRESOLVED or target_index is _UNRESOLVED:
            external_edges += 1
            continue
        if (node_roots[source_index] != graph_root
                or node_roots[target_index] != graph_root):
            external_edges += 1

    if not nodes:
        return _GraphAssessment(len(nodes), len(links), external_edges,
                                "unmapped", ["graph has no nodes"])
    # A degenerate graph proves nothing about blast radius. Requiring real
    # topology closes a permissive hole: without this, a 1-node/0-edge graph
    # is trivially "fully resolved" and would confer ceiling eligibility.
    if not links:
        return _GraphAssessment(
            len(nodes), len(links), external_edges, "unmapped",
            ["graph has zero edges; blast radius is unmapped"],
        )
    if len(nodes) < MIN_TOPOLOGY_NODES:
        return _GraphAssessment(
            len(nodes), len(links), external_edges, "unmapped",
            [f"graph has fewer than {MIN_TOPOLOGY_NODES} nodes; "
             "too degenerate to establish blast radius"],
        )
    if external_edges:
        return _GraphAssessment(
            len(nodes), len(links), external_edges, "partial",
            [f"{external_edges} edge(s) have unresolved or foreign endpoint(s)"],
        )
    return _GraphAssessment(len(nodes), len(links), 0, "mapped", [])


def _lane_name(graph_root: str) -> str:
    name = Path(graph_root).name
    return name or graph_root


def _current_usable_sources(report: DiscoveryReport) -> list[GraphSource]:
    return [source for source in report.sources
            if source.is_usable() and not source.superseded]


def _fallback_note(report: DiscoveryReport) -> str:
    note = report.note.strip()
    if "live-discovery fallback required" in note.lower():
        return note
    suffix = "live-discovery fallback required before any lane may earn authority"
    return f"{note}; {suffix}" if note else suffix


def build_scope(report: DiscoveryReport | None = None,
                roots: list[str | Path] | None = None) -> ScopeMap:
    """Adopt current usable graph sources into a non-permissive scope map."""
    discovery = report if report is not None else discover_graphs(roots)
    if not discovery.found:
        return ScopeMap(
            generated_at=discovery.generated_at,
            sources_adopted=0,
            lanes=[],
            unmapped_lanes=[],
            total_nodes=0,
            total_edges=0,
            discovery_note=_fallback_note(discovery),
        )

    lanes: list[LaneCandidate] = []
    for source in _current_usable_sources(discovery):
        assessment = _assess_graph(source)
        eligible = assessment.blast_radius == "mapped"
        reasons = list(assessment.reasons)
        if not eligible and not reasons:
            reasons.append(f"blast radius is {assessment.blast_radius}")
        lanes.append(LaneCandidate(
            name=_lane_name(source.graph_root),
            graph_root=source.graph_root,
            node_count=assessment.node_count,
            edge_count=assessment.edge_count,
            external_edges=assessment.external_edges,
            blast_radius=assessment.blast_radius,
            ceiling_eligible=eligible,
            reasons=reasons,
        ))

    return ScopeMap(
        generated_at=discovery.generated_at,
        sources_adopted=len(lanes),
        lanes=lanes,
        unmapped_lanes=[lane.name for lane in lanes if not lane.ceiling_eligible],
        total_nodes=sum(lane.node_count for lane in lanes),
        total_edges=sum(lane.edge_count for lane in lanes),
        discovery_note=discovery.note,
    )


def write_scope(scope: ScopeMap, path: str | Path) -> Path:
    """Atomically write a deterministic JSON scope map."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(scope.to_dict(), indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination.parent,
                prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination
