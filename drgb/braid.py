"""The braid: agents keep up with each other's work, with provenance.

A shared knowledge surface branched from the second brain — but built the way
this architecture builds everything:

* each agent writes ONLY its own namespaced subgraph (write-isolated);
* peers IMPORT those exports read-only and merge them into a shared view;
* every merged fact stays **attributed** to the agent that asserted it, so a
  claim can never be laundered into anonymous "known truth";
* when two agents assert different things about the same node, that is a
  **conflict to surface**, not a value to overwrite;
* the merged braid syncs UPWARD into the second brain by export, never by
  writing into the host's graph files.

The reader exists before the producer: :meth:`Braid.briefing` emits the digest
that agents already consume, so this cannot become another write-only log.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

NAMESPACE = "drgb"


@dataclass
class Attribution:
    """Who asserted this, when, and under which envelope."""

    agent: str
    published_at: float
    envelope_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BraidNode:
    """One merged node and every agent that asserted something about it."""

    node_id: str
    kind: str
    attributes: dict[str, Any] = field(default_factory=dict)
    asserted_by: list[Attribution] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "kind": self.kind,
                "attributes": self.attributes,
                "asserted_by": [a.to_dict() if not isinstance(a, dict) else a
                                for a in self.asserted_by],
                "conflicts": self.conflicts}


class BraidError(RuntimeError):
    """Malformed contribution — refused rather than partially merged."""


def _kind_of(node_id: str) -> str:
    parts = str(node_id).split(":")
    return parts[1] if len(parts) > 2 and parts[0] == NAMESPACE else "unknown"


class Braid:
    """Merged, attributed view of what every agent has learned."""

    # Attributes an agent may only assert about ITSELF, never about a peer.
    SELF_ONLY = {"trust", "n_graded", "successes", "failures", "win_rate"}

    def __init__(self) -> None:
        self.nodes: dict[str, BraidNode] = {}
        self.rejected: list[dict[str, Any]] = []

    # ---- ingest ---------------------------------------------------------
    def contribute(self, document: dict[str, Any]) -> dict[str, Any]:
        """Merge one agent's exported subgraph. Returns a merge summary."""
        if not isinstance(document, dict):
            raise BraidError("contribution is not an object")
        if document.get("producer") != NAMESPACE:
            raise BraidError(f"foreign producer: {document.get('producer')!r}")
        agent = str(document.get("agent") or "")
        if not agent:
            raise BraidError("contribution has no agent")

        attribution = Attribution(
            agent=agent,
            published_at=float(document.get("published_at") or time.time()),
            envelope_digest=str(document.get("envelope_digest") or "unknown"))

        merged = conflicts = rejected = 0
        for raw in document.get("nodes") or []:
            if not isinstance(raw, dict) or "id" not in raw:
                rejected += 1
                continue
            node_id = str(raw["id"])
            if not node_id.startswith(f"{NAMESPACE}:"):
                self.rejected.append({"agent": agent, "node": node_id,
                                      "reason": "outside_namespace"})
                rejected += 1
                continue
            attrs = {k: v for k, v in raw.items() if k != "id"}

            # An agent may not assert another agent's trust — that is exactly
            # the laundering shape, expressed as knowledge instead of a request.
            owner = attrs.get("agent") or attrs.get("originating_agent")
            if owner and str(owner) != agent:
                leaking = self.SELF_ONLY & set(attrs)
                if leaking:
                    self.rejected.append({
                        "agent": agent, "node": node_id,
                        "reason": f"asserted_{sorted(leaking)}_about_{owner}"})
                    rejected += 1
                    continue

            node = self.nodes.get(node_id)
            if node is None:
                self.nodes[node_id] = BraidNode(
                    node_id=node_id, kind=_kind_of(node_id),
                    attributes=dict(attrs), asserted_by=[attribution])
                merged += 1
                continue

            for key, value in attrs.items():
                if key in node.attributes and node.attributes[key] != value:
                    node.conflicts.append({
                        "attribute": key, "held": node.attributes[key],
                        "asserted": value, "by": agent,
                        "at": attribution.published_at})
                    conflicts += 1
                else:
                    node.attributes.setdefault(key, value)
            if all(a.agent != agent for a in node.asserted_by):
                node.asserted_by.append(attribution)
            merged += 1

        return {"agent": agent, "merged": merged, "conflicts": conflicts,
                "rejected": rejected}

    def contribute_all(self, directory: str | Path) -> list[dict[str, Any]]:
        """Merge every peer export in a directory. One bad file never blocks
        the rest — it is recorded and skipped."""
        results = []
        for path in sorted(Path(directory).glob("*.json")):
            try:
                results.append(self.contribute(json.loads(path.read_text())))
            except (BraidError, json.JSONDecodeError, OSError) as exc:
                entry = {"file": path.name, "reason": str(exc)[:120]}
                self.rejected.append(entry)
                results.append({"agent": path.stem, "error": entry["reason"]})
        return results

    # ---- read -----------------------------------------------------------
    def agents(self) -> tuple[str, ...]:
        seen = {a.agent for n in self.nodes.values() for a in n.asserted_by}
        return tuple(sorted(seen))

    def conflicts(self) -> list[dict[str, Any]]:
        return [{"node_id": n.node_id, **c}
                for n in self.nodes.values() for c in n.conflicts]

    def briefing(self, now: float | None = None) -> dict[str, Any]:
        """The digest agents actually read. Conflicts and silence lead."""
        clock = float(now if now is not None else time.time())
        by_kind: dict[str, int] = {}
        latest_by_agent: dict[str, float] = {}
        for node in self.nodes.values():
            by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
            for a in node.asserted_by:
                latest_by_agent[a.agent] = max(latest_by_agent.get(a.agent, 0.0),
                                               a.published_at)
        return {
            "generated_at": clock,
            "agents": sorted(latest_by_agent),
            "silence_seconds": {agent: round(clock - ts, 1)
                                for agent, ts in sorted(latest_by_agent.items())},
            "nodes": len(self.nodes),
            "by_kind": dict(sorted(by_kind.items())),
            "conflicts": self.conflicts()[:20],
            "conflict_count": len(self.conflicts()),
            "rejected": self.rejected[-20:],
            "rejected_count": len(self.rejected),
            "note": ("attributed knowledge only — every fact names the agent "
                     "that asserted it; conflicts are surfaced, never merged away"),
        }

    def export(self, path: str | Path, now: float | None = None) -> Path:
        """Write the braid for upward sync into the second brain (by import,
        never by writing the host's own graph files)."""
        p = Path(path)
        if any(part.casefold() == "graphify-out" for part in p.resolve().parents
               for part in [part.name]):
            raise BraidError(f"refusing to write inside a host graph dir: {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "producer": NAMESPACE, "namespace": NAMESPACE, "kind": "braid",
            "generated_at": float(now if now is not None else time.time()),
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "briefing": self.briefing(now=now),
        }
        p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return p
