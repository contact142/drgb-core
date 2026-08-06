"""DRGB's write-isolated, namespaced graphlet.

The host may ingest an exported graphlet on its own terms.  DRGB never writes
into a host graph, and every node and edge is validated before it can be
exported.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drgb.ledger import Ledger
    from drgb.scope import ScopeMap


NAMESPACE = "drgb"
HOST_GRAPH_DIRNAME = "graphify-out"
VALID_RELATIONS = frozenset({
    "observed_by",
    "graded_by",
    "originates_from",
    "diverges_from",
})


class SubgraphError(RuntimeError):
    """Structural or write-isolation problem with a DRGB subgraph."""


def is_namespaced(node_id: Any) -> bool:
    """Return whether ``node_id`` belongs to DRGB's namespace."""
    return isinstance(node_id, str) and node_id.startswith(f"{NAMESPACE}:")


def _node_id(kind: str, value: Any) -> str:
    return f"{NAMESPACE}:{kind}:{value}"


def _row_attrs(row: dict[str, Any]) -> dict[str, Any]:
    """Keep ledger evidence as node attributes without duplicating identity."""
    return {
        key: value
        for key, value in row.items()
        if key not in {"id", "crossing_id", "lane", "originating_agent"}
    }


@dataclass
class Subgraph:
    """An in-memory DRGB graphlet with no dangling links."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    _node_index: dict[str, dict[str, Any]] = field(default_factory=dict,
                                                  init=False, repr=False)
    _edge_keys: set[tuple[str, str, str]] = field(default_factory=set,
                                                   init=False, repr=False)

    def __post_init__(self) -> None:
        initial_nodes = list(self.nodes)
        initial_edges = list(self.edges)
        self.nodes = []
        self.edges = []
        self._node_index = {}
        self._edge_keys = set()

        for node in initial_nodes:
            if not isinstance(node, dict) or "id" not in node:
                raise SubgraphError("node must be an object with an 'id'")
            node_id = node["id"]
            if not is_namespaced(node_id):
                raise SubgraphError(f"node id is outside DRGB namespace: {node_id!r}")
            if node_id in self._node_index:
                raise SubgraphError(f"duplicate node id: {node_id!r}")
            self._insert_node(node_id, {
                key: value for key, value in node.items() if key != "id"
            })

        for edge in initial_edges:
            if not isinstance(edge, dict):
                raise SubgraphError("edge must be an object")
            try:
                source = edge["source"]
                target = edge["target"]
                relation = edge["relation"]
            except KeyError as exc:
                raise SubgraphError(f"edge is missing {exc.args[0]!r}") from None
            self.link(source, target, relation)

    @property
    def links(self) -> list[dict[str, str]]:
        """Alias matching the node-link JSON vocabulary."""
        return self.edges

    def _insert_node(self, node_id: str, attrs: dict[str, Any]) -> str:
        if not is_namespaced(node_id):
            raise SubgraphError(f"node id is outside DRGB namespace: {node_id!r}")
        if node_id in self._node_index:
            raise SubgraphError(f"duplicate node id: {node_id!r}")
        node = {"id": node_id, **attrs}
        node["id"] = node_id
        self.nodes.append(node)
        self._node_index[node_id] = node
        return node_id

    def _add_node(self, node_id: str, attrs: dict[str, Any]) -> str:
        if not is_namespaced(node_id):
            raise SubgraphError(f"node id is outside DRGB namespace: {node_id!r}")
        existing = self._node_index.get(node_id)
        if existing is not None:
            existing.update({key: value for key, value in attrs.items()
                             if key != "id"})
            existing["id"] = node_id
            return node_id
        return self._insert_node(node_id, attrs)

    def add_lane(self, name: str, **attrs: Any) -> str:
        """Add or update a namespaced lane node and return its id."""
        node_attrs = dict(attrs)
        node_attrs["name"] = name
        return self._add_node(_node_id("lane", name), node_attrs)

    def add_crossing(self, crossing_id: str, lane: str,
                     originating_agent: str, **attrs: Any) -> str:
        """Add or update a crossing node and return its id."""
        node_attrs = dict(attrs)
        node_attrs.update({
            "crossing_id": crossing_id,
            "lane": lane,
            "originating_agent": originating_agent,
        })
        return self._add_node(_node_id("crossing", crossing_id), node_attrs)

    def add_trust(self, lane: str, originating_agent: str | None = None,
                  **attrs: Any) -> str:
        """Add or update a per-lane, per-originator trust node."""
        suffix = f"@{originating_agent}" if originating_agent is not None else ""
        node_attrs = dict(attrs)
        node_attrs.update({"lane": lane, "originating_agent": originating_agent})
        return self._add_node(_node_id("trust", f"{lane}{suffix}"), node_attrs)

    def add_divergence(self, crossing_id: str, **attrs: Any) -> str:
        """Add or update a divergence node for a crossing and return its id."""
        node_attrs = dict(attrs)
        node_attrs["crossing_id"] = crossing_id
        return self._add_node(_node_id("divergence", crossing_id), node_attrs)

    def link(self, src_id: str, dst_id: str, relation: str) -> dict[str, str]:
        """Add a validated directed relation between existing nodes."""
        try:
            valid_relation = relation in VALID_RELATIONS
        except TypeError:
            valid_relation = False
        if not valid_relation:
            raise SubgraphError(f"unsupported subgraph relation: {relation!r}")
        if not is_namespaced(src_id) or not is_namespaced(dst_id):
            raise SubgraphError("subgraph links require namespaced endpoints")
        if src_id not in self._node_index or dst_id not in self._node_index:
            raise SubgraphError(
                f"cannot link missing node(s): {src_id!r} -> {dst_id!r}"
            )
        edge = {"source": src_id, "target": dst_id, "relation": relation}
        self.edges.append(edge)
        self._edge_keys.add((src_id, dst_id, relation))
        return edge

    def _link_once(self, src_id: str, dst_id: str, relation: str) -> None:
        key = (src_id, dst_id, relation)
        if key not in self._edge_keys:
            self.link(src_id, dst_id, relation)

    def _validated_parts(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        for node in self.nodes:
            if not isinstance(node, dict) or "id" not in node:
                raise SubgraphError("node must be an object with an 'id'")
            node_id = node["id"]
            if not is_namespaced(node_id):
                raise SubgraphError(f"node id is outside DRGB namespace: {node_id!r}")
            if node_id in node_ids:
                raise SubgraphError(f"duplicate node id: {node_id!r}")
            node_ids.add(node_id)
            normalized = dict(node)
            normalized["id"] = node_id
            nodes.append(normalized)

        links: list[dict[str, str]] = []
        for edge in self.edges:
            if not isinstance(edge, dict):
                raise SubgraphError("edge must be an object")
            source = edge.get("source")
            target = edge.get("target")
            relation = edge.get("relation")
            try:
                valid_relation = relation in VALID_RELATIONS
            except TypeError:
                valid_relation = False
            if not valid_relation:
                raise SubgraphError(f"unsupported subgraph relation: {relation!r}")
            if not is_namespaced(source) or not is_namespaced(target):
                raise SubgraphError("subgraph links require namespaced endpoints")
            if source not in node_ids or target not in node_ids:
                raise SubgraphError(
                    f"cannot export dangling link: {source!r} -> {target!r}"
                )
            links.append({"source": source, "target": target,
                          "relation": relation})
        return nodes, links

    def to_dict(self) -> dict[str, Any]:
        """Return the graphify-compatible node-link document."""
        nodes, links = self._validated_parts()
        return {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": nodes,
            "links": links,
            "producer": NAMESPACE,
            "namespace": NAMESPACE,
        }

    @classmethod
    def from_ledger(cls, ledger: Ledger,
                    scope: ScopeMap | None = None) -> "Subgraph":
        """Build a DRGB graphlet from crossing and grade ledger rows."""
        graph = cls()

        if scope is not None:
            for candidate in getattr(scope, "lanes", []):
                if isinstance(candidate, dict):
                    lane_name = candidate.get("name")
                    lane_attrs = dict(candidate)
                else:
                    lane_name = getattr(candidate, "name", None)
                    if hasattr(candidate, "to_dict"):
                        lane_attrs = dict(candidate.to_dict())
                    else:
                        lane_attrs = {}
                if lane_name is None:
                    raise SubgraphError("scope lane is missing a name")
                lane_attrs.pop("name", None)
                graph.add_lane(lane_name, **lane_attrs)

        rows = list(ledger.rows())
        crossing_rows = [row for row in rows if row.get("kind") == "crossing"]
        grade_rows = [row for row in rows if row.get("kind") == "grade"]
        unsupported = [row.get("kind") for row in rows
                       if row.get("kind") not in {"crossing", "grade"}]
        if unsupported:
            raise SubgraphError(f"unsupported ledger row kind: {unsupported[0]!r}")

        crossing_lanes: dict[str, str] = {}

        for row in crossing_rows:
            if "crossing_id" not in row or "lane" not in row:
                raise SubgraphError("crossing row requires crossing_id and lane")
            crossing_id = row["crossing_id"]
            lane = row["lane"]
            originating_agent = row.get("originating_agent")
            crossing_node = graph.add_crossing(
                crossing_id, lane, originating_agent, **_row_attrs(row)
            )
            lane_node = graph.add_lane(lane)
            crossing_lanes[crossing_node] = lane_node
            graph._link_once(crossing_node, lane_node, "originates_from")

        for row in grade_rows:
            if "crossing_id" not in row or "lane" not in row:
                raise SubgraphError("grade row requires crossing_id and lane")
            crossing_id = row["crossing_id"]
            lane = row["lane"]
            originating_agent = row.get("originating_agent")
            crossing_node = _node_id("crossing", crossing_id)
            if crossing_node not in graph._node_index:
                crossing_node = graph.add_crossing(
                    crossing_id, lane, originating_agent, **_row_attrs(row)
                )
                crossing_lanes[crossing_node] = graph.add_lane(lane)
                graph._link_once(crossing_node, crossing_lanes[crossing_node],
                                 "originates_from")
            lane_node = graph.add_lane(lane)
            trust_node = graph.add_trust(
                lane, originating_agent, **_row_attrs(row)
            )
            graph._link_once(trust_node, lane_node, "observed_by")
            graph.link(crossing_node, trust_node, "graded_by")

        return graph

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "Subgraph":
        """Validate and construct a subgraph from a DRGB document."""
        if not isinstance(document, dict):
            raise SubgraphError("subgraph document must be an object")
        if document.get("producer") != NAMESPACE:
            raise SubgraphError("refusing to load a non-DRGB graph")
        if document.get("namespace", NAMESPACE) != NAMESPACE:
            raise SubgraphError("subgraph document has a foreign namespace")

        raw_nodes = document.get("nodes")
        raw_links = document.get("links", document.get("edges"))
        if not isinstance(raw_nodes, list) or not isinstance(raw_links, list):
            raise SubgraphError("subgraph document requires node and link lists")

        graph = cls()
        for node in raw_nodes:
            if not isinstance(node, dict) or "id" not in node:
                raise SubgraphError("node must be an object with an 'id'")
            node_id = node["id"]
            if not is_namespaced(node_id):
                raise SubgraphError(f"node id is outside DRGB namespace: {node_id!r}")
            graph._insert_node(node_id, {
                key: value for key, value in node.items() if key != "id"
            })
        for edge in raw_links:
            if not isinstance(edge, dict):
                raise SubgraphError("edge must be an object")
            try:
                graph.link(edge["source"], edge["target"], edge["relation"])
            except KeyError as exc:
                raise SubgraphError(f"edge is missing {exc.args[0]!r}") from None
        return graph

    def export(self, path: str | Path) -> Path:
        """Atomically export this graphlet outside host graph locations."""
        destination = Path(path)
        try:
            resolved = destination.resolve()
        except OSError as exc:
            raise SubgraphError(f"cannot resolve export path {destination}: {exc}") \
                from None

        # Case-insensitive: DRGB installs on case-insensitive filesystems too
        # (macOS, Windows), where "GRAPHIFY-OUT" and "graphify-out" are the
        # same directory. A case-sensitive check is a real bypass there.
        host_dir = HOST_GRAPH_DIRNAME.casefold()
        if any(parent.name.casefold() == host_dir for parent in resolved.parents):
            raise SubgraphError(
                f"refusing to write inside host graph directory: {resolved}"
            )

        if destination.name.casefold() == "graph.json" and destination.exists():
            if not destination.is_file():
                raise SubgraphError(f"refusing to overwrite non-file graph path: {destination}")
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SubgraphError(
                    f"refusing to overwrite foreign graph.json: {destination} ({exc})"
                ) from None
            if not isinstance(existing, dict) or existing.get("producer") != NAMESPACE:
                raise SubgraphError(
                    f"refusing to overwrite foreign graph.json: {destination}"
                )

        try:
            payload = json.dumps(self.to_dict(), indent=2) + "\n"
        except (TypeError, ValueError) as exc:
            raise SubgraphError(f"subgraph is not JSON serializable: {exc}") from None

        destination.parent.mkdir(parents=True, exist_ok=True)
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
        except OSError as exc:
            raise SubgraphError(f"cannot export subgraph to {destination}: {exc}") \
                from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return destination

    @classmethod
    def load(cls, path: str | Path) -> "Subgraph":
        """Load a DRGB-owned graphlet and reject foreign graph documents."""
        source = Path(path)
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SubgraphError(f"cannot read subgraph {source}: {exc}") from None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SubgraphError(f"subgraph is malformed: {exc}") from None
        return cls.from_dict(document)


def from_ledger(ledger: Ledger, scope: ScopeMap | None = None) -> Subgraph:
    """Module-level convenience wrapper for :meth:`Subgraph.from_ledger`."""
    return Subgraph.from_ledger(ledger, scope)


def load(path: str | Path) -> Subgraph:
    """Module-level convenience wrapper for :meth:`Subgraph.load`."""
    return Subgraph.load(path)
