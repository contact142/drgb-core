"""Observe adapters: attach DRGB to event sources that already exist.

Two adapters cover most hosts:

* :class:`SystemdTimerAdapter` — scheduled work (the cadence backbone of a
  fleet). Read-only: it runs ``systemctl ... list-timers`` and parses it.
* :class:`JsonlLogAdapter` — append-only event logs (heartbeats, briefings,
  decision logs), read incrementally from a byte offset.

Both obey invariant 3: an unavailable source is *reported* as unavailable,
never rendered as an empty-but-healthy result. Neither adapter mutates the
host, and neither raises into the caller.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

_TIMER_LINE = re.compile(r"\s{2,}")


@dataclass
class Observation:
    """One normalised event handed to the twin/gate."""

    source: str
    event: str
    ts: float
    key: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdapterReport:
    """What an adapter saw. ``available=False`` is a finding, not emptiness."""

    source: str
    available: bool
    observations: list[Observation] = field(default_factory=list)
    error: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self),
                "observations": [o.to_dict() for o in self.observations]}


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


class SystemdTimerAdapter:
    """Read-only view of systemd timers (user scope by default)."""

    def __init__(self, user_scope: bool = True,
                 runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
                 unit_filter: str = "*"):
        self.user_scope = user_scope
        self.unit_filter = unit_filter
        self._runner = runner or (lambda cmd: _run(cmd))
        self.source = f"systemd:{'user' if user_scope else 'system'}"

    def _command(self) -> list[str]:
        cmd = ["systemctl"]
        if self.user_scope:
            cmd.append("--user")
        cmd += ["list-timers", self.unit_filter, "--all", "--no-pager",
                "--no-legend"]
        return cmd

    def poll(self, now: float | None = None) -> AdapterReport:
        clock = float(now if now is not None else time.time())
        try:
            code, out, err = self._runner(self._command())
        except Exception as exc:  # systemd absent, permission denied, timeout
            return AdapterReport(self.source, False,
                                 error=f"{type(exc).__name__}: {exc}",
                                 note="timer source unavailable — not empty")
        if code != 0:
            return AdapterReport(self.source, False,
                                 error=(err or out).strip()[:200],
                                 note="timer source unavailable — not empty")

        observations: list[Observation] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p for p in _TIMER_LINE.split(line) if p]
            if len(parts) < 2:
                continue
            unit = next((p for p in parts if p.endswith(".timer")), None)
            if unit is None:
                continue
            activates = next((p for p in parts if p.endswith(".service")), None)
            idx = parts.index(unit)
            observations.append(Observation(
                source=self.source, event="timer_state", ts=clock, key=unit,
                data={"unit": unit, "activates": activates,
                      "fields": parts[:idx], "raw": line},
            ))
        return AdapterReport(
            self.source, True, observations,
            note=f"{len(observations)} timer(s) observed")


class JsonlLogAdapter:
    """Incremental reader for an append-only JSONL event log."""

    def __init__(self, path: str | Path, source: str | None = None,
                 event_field: str = "kind", ts_field: str = "ts",
                 key_field: str | None = None, max_rows: int = 500):
        self.path = Path(path)
        self.source = source or f"jsonl:{self.path.name}"
        self.event_field = event_field
        self.ts_field = ts_field
        self.key_field = key_field
        self.max_rows = int(max_rows)
        self._offset = 0

    @property
    def offset(self) -> int:
        return self._offset

    def reset(self) -> None:
        self._offset = 0

    def poll(self, now: float | None = None) -> AdapterReport:
        clock = float(now if now is not None else time.time())
        if not self.path.exists():
            return AdapterReport(self.source, False,
                                 error=f"missing: {self.path}",
                                 note="log source unavailable — not empty")
        try:
            size = self.path.stat().st_size
            if size < self._offset:      # rotated/truncated: start over, say so
                self._offset = 0
                rotated = True
            else:
                rotated = False
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                raw_lines = handle.readlines()
                self._offset = handle.tell()
        except OSError as exc:
            return AdapterReport(self.source, False,
                                 error=f"{type(exc).__name__}: {exc}",
                                 note="log source unavailable — not empty")

        observations: list[Observation] = []
        malformed = 0
        for line in raw_lines[: self.max_rows]:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            ts = row.get(self.ts_field)
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = clock
            key = str(row.get(self.key_field) if self.key_field else self.source)
            observations.append(Observation(
                source=self.source, event=str(row.get(self.event_field) or "row"),
                ts=ts, key=key, data=row))
        note = f"{len(observations)} row(s)"
        if malformed:
            note += f"; {malformed} malformed row(s) reported, not skipped silently"
        if rotated:
            note += "; source rotated — offset reset"
        return AdapterReport(self.source, True, observations, note=note)


def poll_all(adapters: Iterable[Any], now: float | None = None) -> dict[str, Any]:
    """Poll every adapter. One failing source never suppresses the others."""
    reports: list[AdapterReport] = []
    for adapter in adapters:
        try:
            reports.append(adapter.poll(now=now))
        except Exception as exc:
            reports.append(AdapterReport(
                getattr(adapter, "source", str(adapter)), False,
                error=f"{type(exc).__name__}: {exc}",
                note="adapter raised — treated as unavailable"))
    return {
        "generated_at": float(now if now is not None else time.time()),
        "sources": len(reports),
        "available": sum(1 for r in reports if r.available),
        "unavailable": [r.source for r in reports if not r.available],
        "observations": sum(len(r.observations) for r in reports),
        "reports": [r.to_dict() for r in reports],
    }
