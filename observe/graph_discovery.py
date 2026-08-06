"""Discover pre-existing graph / second-brain artifacts on a host.

DRGB's first bootstrap step (Phase 0): adopt what the environment already
knows instead of re-deriving it. Supports the node-link graph shape emitted by
graphify (`graph.json` with nodes/links, optional `manifest.json`), and is
written so other producers can be added as recognisers.

Invariant 3 applies to discovery itself: "no graph found" is a *reported*
state, never a silent assumption that the environment is empty. Unreadable or
malformed graphs are reported as errors, not skipped.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SEARCH_ROOTS = (Path.home() / "projects",)
GRAPH_FILENAMES = ("graph.json",)
MANIFEST_FILENAMES = ("manifest.json",)
MAX_DEPTH = 4


@dataclass
class GraphSource:
    """One discovered graph artifact."""

    path: str
    producer: str
    nodes: int | None
    edges: int | None
    hyperedges: int | None
    built_at_commit: str | None
    mtime: float | None
    age_seconds: float | None
    manifest_entries: int | None
    status: str            # "ok" | "unreadable" | "malformed"
    detail: str | None = None
    graph_root: str = ""   # owning repo/project dir (snapshots collapse to this)
    superseded: bool = False  # an older snapshot of the same graph_root

    def is_usable(self) -> bool:
        return self.status == "ok" and bool(self.nodes)


@dataclass
class DiscoveryReport:
    """What DRGB found. ``found=False`` is a finding, not an empty default."""

    generated_at: float
    roots: list[str]
    sources: list[GraphSource] = field(default_factory=list)
    found: bool = False
    total_nodes: int = 0
    total_edges: int = 0
    errors: list[str] = field(default_factory=list)
    fallback_required: bool = True
    current_sources: int = 0
    superseded_snapshots: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sources"] = [asdict(s) if not isinstance(s, dict) else s
                        for s in self.sources]
        return d


def _graph_root(path: Path) -> str:
    """Owning project dir for a graph file.

    graphify writes both `<repo>/graphify-out/graph.json` (current) and dated
    snapshots `<repo>/graphify-out/<YYYY-MM-DD>/graph.json`. Both belong to
    `<repo>`; treating each snapshot as a separate graph would multiply-count
    the same environment.
    """
    parts = path.parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "graphify-out":
            return str(Path(*parts[:i])) if i else str(path.parent)
    return str(path.parent)


def _read_graph(path: Path, now: float) -> GraphSource:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return GraphSource(str(path), "unknown", None, None, None, None, None,
                           None, None, "unreadable", str(exc),
                           graph_root=_graph_root(path))
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return GraphSource(str(path), "unknown", None, None, None, None, None,
                           None, None, "malformed", f"invalid json: {exc}",
                           graph_root=_graph_root(path))
    if not isinstance(doc, dict):
        return GraphSource(str(path), "unknown", None, None, None, None, None,
                           None, None, "malformed", "top level is not an object",
                           graph_root=_graph_root(path))

    nodes = doc.get("nodes")
    links = doc.get("links", doc.get("edges"))
    if not isinstance(nodes, list) or not isinstance(links, list):
        return GraphSource(str(path), "unknown", None, None, None, None, None,
                           None, None, "malformed",
                           "expected list-valued 'nodes' and 'links'/'edges'",
                           graph_root=_graph_root(path))
    producer = "graphify" if "built_at_commit" in doc or "hyperedges" in doc \
        else "node-link"
    hyper = doc.get("hyperedges")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    manifest_entries = None
    for name in MANIFEST_FILENAMES:
        mpath = path.parent / name
        if mpath.exists():
            try:
                mdoc = json.loads(mpath.read_text(encoding="utf-8"))
                if isinstance(mdoc, dict):
                    manifest_entries = len(mdoc)
            except (OSError, json.JSONDecodeError):
                manifest_entries = None
            break
    return GraphSource(
        path=str(path), producer=producer, nodes=len(nodes), edges=len(links),
        hyperedges=len(hyper) if isinstance(hyper, list) else None,
        built_at_commit=(str(doc["built_at_commit"])
                         if isinstance(doc.get("built_at_commit"), str) else None),
        mtime=mtime, age_seconds=(now - mtime) if mtime else None,
        manifest_entries=manifest_entries, status="ok",
        graph_root=_graph_root(path),
    )


def _collapse_snapshots(sources: list[GraphSource]) -> None:
    """Mark all but the freshest usable graph per ``graph_root`` superseded.

    Without this, dated snapshots of one repo are counted as separate
    environments and the scope map inflates by an order of magnitude.
    """
    by_root: dict[str, list[GraphSource]] = {}
    for s in sources:
        if s.is_usable():
            by_root.setdefault(s.graph_root, []).append(s)
    for group in by_root.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (s.mtime or 0.0), reverse=True)
        for older in group[1:]:
            older.superseded = True


def discover_graphs(roots: list[str | Path] | None = None,
                    max_depth: int = MAX_DEPTH,
                    now: float | None = None) -> DiscoveryReport:
    """Scan ``roots`` for graph artifacts and report exactly what exists."""
    clock = float(now if now is not None else time.time())
    search_roots = [Path(r) for r in (roots or DEFAULT_SEARCH_ROOTS)]
    report = DiscoveryReport(generated_at=clock,
                             roots=[str(r) for r in search_roots])
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            report.errors.append(f"root does not exist: {root}")
            continue
        for name in GRAPH_FILENAMES:
            for depth in range(0, max_depth + 1):
                pattern = "/".join(["*"] * depth + [name])
                for candidate in root.glob(pattern):
                    if not candidate.is_file():
                        continue
                    resolved = candidate.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    report.sources.append(_read_graph(candidate, clock))
    _collapse_snapshots(report.sources)
    usable = [s for s in report.sources if s.is_usable() and not s.superseded]
    report.found = bool(usable)
    report.total_nodes = sum(s.nodes or 0 for s in usable)
    report.total_edges = sum(s.edges or 0 for s in usable)
    report.current_sources = len(usable)
    report.superseded_snapshots = sum(1 for s in report.sources if s.superseded)
    report.fallback_required = not report.found
    for s in report.sources:
        if s.status != "ok":
            report.errors.append(f"{s.status}: {s.path} ({s.detail})")
    report.note = (
        f"adopted {len(usable)} current graph source(s); "
        f"{report.superseded_snapshots} older snapshot(s) collapsed"
        if report.found else
        "NO GRAPH FOUND — this is a reported state, not an empty environment; "
        "live discovery fallback required before any lane may earn authority"
    )
    return report


def write_report(report: DiscoveryReport, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    return p
