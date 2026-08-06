"""Read-only observation of a REMOTE host over SSH.

Observing a machine from outside it has a property that matters more than
convenience: **the observed host cannot reach the evidence store.** VPS Hermes
runs as root and can rewrite its own code, so a ledger living on that machine
would be worthless (see the README "Known limit"). Polled from here, its
evidence is beyond its reach.

Commands are read-only by construction: an allowlist of verbs, no shell
metacharacters permitted, and a hard timeout. Nothing is installed, started,
stopped or written on the remote host.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from observe.adapters import AdapterReport, Observation

# Read-only verbs only. Anything not listed cannot be dispatched at all.
ALLOWED_BINARIES = frozenset({
    "systemctl", "docker", "ls", "cat", "stat", "wc", "uptime", "df",
    "journalctl", "pgrep", "ss", "date", "echo", "grep", "head", "tail",
})
FORBIDDEN_SUBCOMMANDS = frozenset({
    "start", "stop", "restart", "enable", "disable", "mask", "unmask",
    "kill", "rm", "run", "exec", "pull", "push", "build", "prune",
    "daemon-reload", "reboot", "poweroff", "edit", "set-property",
})
FORBIDDEN_CHARS = set(";|&><`$\n")


class UnsafeCommand(ValueError):
    """Raised when a command is not provably read-only."""


def assert_read_only(command: str) -> list[str]:
    """Validate and tokenise a command, or refuse it."""
    if any(ch in FORBIDDEN_CHARS for ch in command):
        raise UnsafeCommand(f"shell metacharacters are not permitted: {command!r}")
    parts = shlex.split(command)
    if not parts:
        raise UnsafeCommand("empty command")
    binary = parts[0].rsplit("/", 1)[-1]
    if binary not in ALLOWED_BINARIES:
        raise UnsafeCommand(f"binary not in read-only allowlist: {binary!r}")
    for token in parts[1:]:
        if token.lstrip("-").lower() in FORBIDDEN_SUBCOMMANDS:
            raise UnsafeCommand(f"mutating subcommand refused: {token!r}")
    return parts


@dataclass
class RemoteProbe:
    """One read-only command mapped to an observation key."""

    key: str
    command: str
    event: str = "remote_state"


class SshObserveAdapter:
    """Polls a remote host with read-only commands over SSH."""

    def __init__(self, host: str, probes: Iterable[RemoteProbe],
                 timeout_s: float = 45.0,
                 runner: Callable[[list[str]], tuple[int, str, str]] | None = None):
        self.host = host
        self.probes = list(probes)
        self.timeout_s = float(timeout_s)
        self.source = f"ssh:{host}"
        self._runner = runner or self._default_runner

    def _default_runner(self, argv: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=self.timeout_s)
        return proc.returncode, proc.stdout, proc.stderr

    def _ssh_argv(self, command: str) -> list[str]:
        assert_read_only(command)          # refuse before it ever leaves here
        return ["ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(min(self.timeout_s, 20))}",
                self.host, command]

    def poll(self, now: float | None = None) -> AdapterReport:
        clock = float(now if now is not None else time.time())
        observations: list[Observation] = []
        errors: list[str] = []

        for probe in self.probes:
            try:
                argv = self._ssh_argv(probe.command)
            except UnsafeCommand as exc:
                errors.append(f"{probe.key}: refused: {exc}")
                continue
            try:
                code, out, err = self._runner(argv)
            except Exception as exc:
                errors.append(f"{probe.key}: {type(exc).__name__}: {exc}")
                continue
            if code != 0:
                errors.append(f"{probe.key}: exit {code}: {(err or out).strip()[:120]}")
                continue
            lines = [ln for ln in out.splitlines() if ln.strip()]
            observations.append(Observation(
                source=self.source, event=probe.event, ts=clock, key=probe.key,
                data={"lines": len(lines), "sample": lines[:5],
                      "command": probe.command}))

        available = bool(observations)
        note = (f"{len(observations)}/{len(self.probes)} probe(s) answered"
                if available else
                "remote host unreachable or all probes failed — "
                "unavailable, NOT idle")
        if errors:
            note += f"; {len(errors)} probe error(s) reported"
        return AdapterReport(self.source, available, observations,
                             error="; ".join(errors)[:400] or None, note=note)
