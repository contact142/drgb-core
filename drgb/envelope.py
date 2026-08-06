"""Authority envelope: the operator-set constitution a process cannot amend.

The envelope declares, per lane, which scenarios are pre-approved, the ceiling
a lane may climb toward, hard limits, the evidence required to earn authority,
and which actions are irreversible (never inside any envelope).

Invariant 2 of DRGB: **the ceiling is not self-raisable.** Every mutation path
on a loaded envelope raises :class:`EnvelopeViolation` and appends to an
append-only violations log. Widening authority requires editing the envelope
file out-of-process and reloading.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VIOLATIONS_LOG_DEFAULT = Path.home() / ".drgb" / "violations.jsonl"


class EnvelopeViolation(RuntimeError):
    """Raised when a process attempts to widen its own authority."""


class EnvelopeError(ValueError):
    """Raised when an envelope file is malformed."""


def _log_violation(kind: str, detail: dict[str, Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "kind": kind, "detail": detail}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


@dataclass(frozen=True)
class LaneEnvelope:
    """One lane's pre-approved authority. Frozen: the process cannot widen it."""

    name: str
    scenarios_preapproved: tuple[str, ...]
    ceiling: float
    limits: dict[str, Any] = field(default_factory=dict)
    min_crossings: int = 0
    out_of_sample_required: bool = True
    irreversible_actions: tuple[str, ...] = ()

    def allows_scenario(self, scenario: str) -> bool:
        return scenario in self.scenarios_preapproved

    def is_irreversible(self, action: str) -> bool:
        """Irreversible actions are outside every envelope, always operator-only."""
        return action in self.irreversible_actions

    def clamp(self, requested: float) -> float:
        """Clamp a requested multiplier to the ceiling. Never raises it."""
        if requested < 0:
            return 0.0
        return min(float(requested), self.ceiling)


class Envelope:
    """Loaded envelope, pinned to the sha256 of the file it came from."""

    def __init__(self, lanes: dict[str, LaneEnvelope], source: Path,
                 digest: str, violations_log: Path | None = None) -> None:
        object.__setattr__(self, "_lanes", dict(lanes))
        object.__setattr__(self, "_source", Path(source))
        object.__setattr__(self, "_digest", digest)
        object.__setattr__(self, "_violations_log",
                           Path(violations_log or VIOLATIONS_LOG_DEFAULT))
        object.__setattr__(self, "_frozen", True)

    # ---- read paths -----------------------------------------------------
    @property
    def source(self) -> Path:
        return self._source  # type: ignore[attr-defined]

    @property
    def digest(self) -> str:
        return self._digest  # type: ignore[attr-defined]

    @property
    def violations_log(self) -> Path:
        return self._violations_log  # type: ignore[attr-defined]

    def lanes(self) -> tuple[str, ...]:
        return tuple(sorted(self._lanes))  # type: ignore[attr-defined]

    def lane(self, name: str) -> LaneEnvelope:
        try:
            return self._lanes[name]  # type: ignore[attr-defined]
        except KeyError:
            raise EnvelopeError(f"lane not in envelope: {name!r}") from None

    def ceiling_for(self, name: str) -> float:
        return self.lane(name).ceiling

    def verify_integrity(self) -> bool:
        """True when the on-disk file still matches the digest loaded.

        A mismatch means the envelope changed under a running process. That is
        legitimate only as an out-of-process operator edit followed by a
        reload; it is logged either way so the change is never silent.
        """
        try:
            current = hashlib.sha256(self.source.read_bytes()).hexdigest()
        except OSError as exc:
            _log_violation("envelope_unreadable",
                           {"source": str(self.source), "error": str(exc)},
                           self.violations_log)
            return False
        if current != self.digest:
            _log_violation("envelope_changed_under_process",
                           {"source": str(self.source),
                            "loaded_digest": self.digest,
                            "current_digest": current},
                           self.violations_log)
            return False
        return True

    # ---- blocked mutation paths (invariant 2) ---------------------------
    def raise_ceiling(self, *args: Any, **kwargs: Any) -> None:
        self._violation("raise_ceiling", {"args": [str(a) for a in args]})

    def widen(self, *args: Any, **kwargs: Any) -> None:
        self._violation("widen", {"args": [str(a) for a in args]})

    def __setattr__(self, name: str, value: Any) -> None:
        self._violation("setattr", {"attribute": name})

    def __delattr__(self, name: str) -> None:
        self._violation("delattr", {"attribute": name})

    def _violation(self, kind: str, detail: dict[str, Any]) -> None:
        _log_violation(f"self_raise_attempt:{kind}",
                       {**detail, "source": str(self.source)},
                       self.violations_log)
        raise EnvelopeViolation(
            f"authority envelope is not self-raisable (attempted {kind}); "
            "edit the envelope file out-of-process and reload"
        )


def load_envelope(path: str | Path, violations_log: Path | None = None) -> Envelope:
    """Load and validate an envelope file. Malformed files are rejected loudly."""
    p = Path(path)
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        raise EnvelopeError(f"cannot read envelope {p}: {exc}") from None
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        doc = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EnvelopeError(f"envelope is not valid YAML: {exc}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        raise EnvelopeError("envelope must contain a top-level 'lanes' mapping")

    lanes: dict[str, LaneEnvelope] = {}
    for name, row in doc["lanes"].items():
        if not isinstance(row, dict):
            raise EnvelopeError(f"lane {name!r} must be a mapping")
        if "ceiling" not in row:
            raise EnvelopeError(f"lane {name!r} must declare a ceiling")
        try:
            ceiling = float(row["ceiling"])
        except (TypeError, ValueError):
            raise EnvelopeError(f"lane {name!r} ceiling must be numeric") from None
        if ceiling < 0:
            raise EnvelopeError(f"lane {name!r} ceiling must be >= 0")
        scenarios = row.get("scenarios_preapproved") or []
        if not isinstance(scenarios, list):
            raise EnvelopeError(f"lane {name!r} scenarios_preapproved must be a list")
        irreversible = row.get("irreversible_actions") or []
        if not isinstance(irreversible, list):
            raise EnvelopeError(f"lane {name!r} irreversible_actions must be a list")
        limits = row.get("limits") or {}
        if not isinstance(limits, dict):
            raise EnvelopeError(f"lane {name!r} limits must be a mapping")
        lanes[str(name)] = LaneEnvelope(
            name=str(name),
            scenarios_preapproved=tuple(str(s) for s in scenarios),
            ceiling=ceiling,
            limits=dict(limits),
            min_crossings=int(row.get("min_crossings", 0)),
            out_of_sample_required=bool(row.get("out_of_sample_required", True)),
            irreversible_actions=tuple(str(a) for a in irreversible),
        )
    if not lanes:
        raise EnvelopeError("envelope declares no lanes")
    return Envelope(lanes, p, digest, violations_log)
