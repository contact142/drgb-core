"""Inter-agent mesh: agents exchange EVIDENCE, never permissions.

Two jobs:

1. **Publish** a compact trust record per lane — what this agent has earned,
   under which envelope — so peers can reason about it. Peers consume it as
   *information*; it can never become the consumer's own authority (invariant
   6, enforced in :mod:`drgb.bridge` and tested in the laundering suite).

2. **Replicate ledger heads.** An agent that controls its own evidence store
   can forge a shorter history (the documented limit). Peers keep the heads
   they have seen, so a rewound or shrunken history *disagrees with the mesh*
   and is detectable by someone other than the agent that rewound it. This is
   the mitigation the VPS-Hermes reach inventory concluded was mandatory.

Records are inert data: no verbs, no requests, no instructions. There is
deliberately no "please act for me" message type — the mesh cannot carry one.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "drgb.mesh/1"


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class LaneTrust:
    """One lane's earned record, as published to peers."""

    lane: str
    n_graded: int
    successes: int
    failures: int
    win_rate: float | None
    status: str
    in_cooldown: bool
    last_graded_ts: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshDocument:
    """What one agent publishes. Inert evidence — never an instruction."""

    schema: str
    agent: str
    envelope_digest: str
    published_at: float
    ledger_head: dict[str, Any]
    lanes: list[LaneTrust] = field(default_factory=list)
    document_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["lanes"] = [lane.to_dict() if not isinstance(lane, dict) else lane
                        for lane in self.lanes]
        return doc


class MeshError(RuntimeError):
    """Malformed or untrustworthy mesh input."""


def publish(ledger, envelope, agent: str, lanes: Iterable[str],
            now: float | None = None) -> MeshDocument:
    """Build this agent's mesh document from its own ledger and envelope."""
    clock = float(now if now is not None else time.time())
    records: list[LaneTrust] = []
    for lane in lanes:
        trust = ledger.trust(lane, originating_agent=agent, now=clock)
        records.append(LaneTrust(
            lane=str(lane), n_graded=int(trust.get("n_graded") or 0),
            successes=int(trust.get("successes") or 0),
            failures=int(trust.get("failures") or 0),
            win_rate=trust.get("win_rate"), status=str(trust.get("status")),
            in_cooldown=bool(trust.get("in_cooldown")),
            last_graded_ts=trust.get("last_graded_ts")))

    head = ledger._read_head() or {}
    ledger_head = {"count": int(head.get("count") or 0),
                   "last_hash": str(head.get("last_hash") or ""),
                   "chain_verified": bool(ledger.verify_chain())}

    doc = MeshDocument(
        schema=SCHEMA, agent=str(agent),
        envelope_digest=str(getattr(envelope, "digest", "unavailable")),
        published_at=clock, ledger_head=ledger_head, lanes=records)
    doc.document_digest = _digest({k: v for k, v in doc.to_dict().items()
                                   if k != "document_digest"})
    return doc


class MeshView:
    """What this agent has seen from its peers, and what disagrees."""

    def __init__(self, store: str | Path | None = None):
        self.store = Path(store) if store else None
        self._latest: dict[str, dict[str, Any]] = {}
        self._head_history: dict[str, list[dict[str, Any]]] = {}
        self.alerts: list[dict[str, Any]] = []
        if self.store and self.store.exists():
            try:
                saved = json.loads(self.store.read_text())
                self._latest = saved.get("latest", {})
                self._head_history = saved.get("head_history", {})
                self.alerts = saved.get("alerts", [])
            except (OSError, json.JSONDecodeError):
                pass  # unreadable peer state is unknown, not fatal

    # ---- ingest ---------------------------------------------------------
    def ingest(self, document: MeshDocument | dict[str, Any]) -> dict[str, Any]:
        """Accept a peer document as EVIDENCE. Returns any alert raised."""
        doc = document.to_dict() if isinstance(document, MeshDocument) else dict(document)
        if doc.get("schema") != SCHEMA:
            raise MeshError(f"unknown mesh schema: {doc.get('schema')!r}")
        agent = str(doc.get("agent") or "")
        if not agent:
            raise MeshError("mesh document has no agent")
        expected = _digest({k: v for k, v in doc.items() if k != "document_digest"})
        if doc.get("document_digest") != expected:
            raise MeshError(f"mesh document digest mismatch for {agent}")

        alert = self._check_head(agent, doc.get("ledger_head") or {},
                                 float(doc.get("published_at") or 0.0))
        self._latest[agent] = doc
        self._head_history.setdefault(agent, []).append(
            {**(doc.get("ledger_head") or {}),
             "published_at": doc.get("published_at")})
        self._persist()
        return alert

    def _check_head(self, agent: str, head: dict[str, Any],
                    published_at: float) -> dict[str, Any]:
        """Peer-replicated head check: a history may only grow."""
        # A peer reporting a broken chain must alert on its FIRST document —
        # that is precisely when a compromised peer would join the mesh.
        if head.get("chain_verified") is False:
            alert = {"agent": agent, "kind": "peer_reports_broken_chain",
                     "published_at": published_at}
            self.alerts.append(alert)
            return alert

        history = self._head_history.get(agent) or []
        if not history:
            return {}
        previous = history[-1]
        prev_count = int(previous.get("count") or 0)
        new_count = int(head.get("count") or 0)
        alert: dict[str, Any] = {}
        if new_count < prev_count:
            alert = {"agent": agent, "kind": "ledger_rewound",
                     "previous_count": prev_count, "new_count": new_count,
                     "published_at": published_at}
        elif (new_count == prev_count
              and str(head.get("last_hash") or "") != str(previous.get("last_hash") or "")):
            alert = {"agent": agent, "kind": "head_diverged_at_same_count",
                     "count": new_count, "published_at": published_at}
        if alert:
            self.alerts.append(alert)
        return alert

    # ---- read -----------------------------------------------------------
    def peers(self) -> tuple[str, ...]:
        return tuple(sorted(self._latest))

    def evidence_for(self, lane: str) -> list[dict[str, Any]]:
        """Peer records for a lane. INFORMATION ONLY — never authority."""
        out: list[dict[str, Any]] = []
        for agent, doc in sorted(self._latest.items()):
            for row in doc.get("lanes", []):
                if row.get("lane") == lane:
                    out.append({"agent": agent, "envelope_digest":
                                doc.get("envelope_digest"), **row,
                                "usable_as_own_authority": False})
        return out

    def report(self) -> dict[str, Any]:
        return {
            "peers": len(self._latest),
            "alerts": self.alerts[-20:],
            "alert_count": len(self.alerts),
            "agents": {a: {"lanes": len(d.get("lanes", [])),
                           "head_count": (d.get("ledger_head") or {}).get("count"),
                           "published_at": d.get("published_at")}
                       for a, d in sorted(self._latest.items())},
        }

    def _persist(self) -> None:
        if not self.store:
            return
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(
            {"latest": self._latest, "head_history": self._head_history,
             "alerts": self.alerts}, indent=2, sort_keys=True))
