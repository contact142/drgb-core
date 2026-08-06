"""Guardian ledger: append-only, hash-chained evidence of live-vs-shadow.

Trust is *accumulated*, never asserted. Every crossing request and every
graded outcome is a row; rows are chained by sha256 so truncation, reordering,
or forgery is detectable. Grading never mutates a prior row — it appends.

Invariants enforced here:
  * 3 — unavailable != zero: stale or insufficient evidence yields
        ``status="unknown"``, which callers must treat as *not permissive*.
  * 5 — failure imposes cooldown: a graded failure sets a cooldown deadline.
  * 6 — no authority laundering: every row records ``originating_agent`` and
        ``originating_envelope_digest``; trust is computed per originator.
  * E3 — clock skew: future-dated rows are rejected, never counted.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64
FUTURE_TOLERANCE_S = 120.0
DEFAULT_MAX_AGE_S = 7 * 24 * 3600.0
DEFAULT_COOLDOWN_S = 3600.0

KIND_CROSSING = "crossing"
KIND_GRADE = "grade"
VALID_KINDS = {KIND_CROSSING, KIND_GRADE}
VALID_OUTCOMES = {"success", "failure", "neutral"}


class LedgerError(RuntimeError):
    """Structural problem with the ledger (tamper, malformed row, skew)."""


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _hash_row(row: dict[str, Any], prev_hash: str) -> str:
    body = {k: v for k, v in row.items() if k != "row_hash"}
    return hashlib.sha256((_canonical(body) + prev_hash).encode("utf-8")).hexdigest()


class Ledger:
    """Append-only hash-chained ledger backed by a JSONL file.

    A hash chain alone does NOT detect tail truncation: dropping the most
    recent rows leaves a shorter but internally valid chain, which would let
    an actor delete its own failures and recover trust. A sidecar head file
    (``<ledger>.head``) records the row count and last hash after every
    append, so a shortened or rewound ledger is detectable. A missing head
    file is itself unknown-not-permissive.
    """

    def __init__(self, path: str | Path, cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.path = Path(path)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")
        self.cooldown_s = float(cooldown_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ---- head checkpoint ------------------------------------------------
    def _read_head(self) -> dict[str, Any] | None:
        try:
            doc = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def _write_head(self, count: int, last_hash: str) -> None:
        tmp = self.head_path.with_suffix(self.head_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"count": int(count), "last_hash": last_hash},
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.head_path)

    def _row_count(self) -> int:
        try:
            return sum(1 for line in self.path.open("r", encoding="utf-8")
                       if line.strip())
        except OSError:
            return 0

    # ---- reading --------------------------------------------------------
    def rows(self) -> Iterator[dict[str, Any]]:
        """Yield well-formed rows. Malformed lines raise — never silently skip."""
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"malformed ledger row at line {lineno}: {exc}")
                if not isinstance(row, dict):
                    raise LedgerError(f"ledger row {lineno} is not an object")
                yield row

    def last_hash(self) -> str:
        prev = GENESIS
        for row in self.rows():
            prev = str(row.get("row_hash") or prev)
        return prev

    def verify_chain(self) -> bool:
        """True when the chain is internally valid AND matches its head.

        Three failure modes are covered: mutated rows (hash mismatch),
        reordered/removed interior rows (chain break), and TAIL TRUNCATION or
        rewind (head mismatch). A head file that exists but disagrees, or is
        absent for a non-empty ledger, fails closed.
        """
        prev = GENESIS
        count = 0
        try:
            for row in self.rows():
                expected = _hash_row(row, prev)
                if row.get("row_hash") != expected:
                    return False
                prev = str(row["row_hash"])
                count += 1
        except LedgerError:
            return False

        head = self._read_head()
        if head is None:
            # No checkpoint: only an empty ledger can be trusted as intact.
            return count == 0
        try:
            head_count = int(head.get("count"))
        except (TypeError, ValueError):
            return False
        if count != head_count or str(head.get("last_hash") or "") != prev:
            return False
        return True

    # ---- writing --------------------------------------------------------
    def append(self, *, kind: str, lane: str, agent: str,
               originating_agent: str, originating_envelope_digest: str,
               ts: float | None = None, **extra: Any) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise LedgerError(f"unknown row kind: {kind!r}")
        now = time.time()
        stamp = float(ts if ts is not None else now)
        if stamp > now + FUTURE_TOLERANCE_S:
            raise LedgerError(
                f"future-dated row rejected (ts={stamp:.0f}, now={now:.0f}); "
                "clock skew or forged evidence"
            )
        if kind == KIND_GRADE:
            outcome = extra.get("outcome")
            if outcome not in VALID_OUTCOMES:
                raise LedgerError(f"grade requires outcome in {sorted(VALID_OUTCOMES)}")
            if not extra.get("crossing_id"):
                raise LedgerError("grade requires crossing_id")
        row: dict[str, Any] = {
            "kind": kind, "lane": lane, "agent": agent,
            "originating_agent": originating_agent,
            "originating_envelope_digest": originating_envelope_digest,
            "ts": stamp, **extra,
        }
        prev = self.last_hash()
        row["prev_hash"] = prev
        row["row_hash"] = _hash_row(row, prev)
        line = _canonical(row) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._write_head(self._row_count(), str(row["row_hash"]))
        return row

    def record_crossing(self, *, lane: str, agent: str, scenario: str,
                        originating_agent: str, originating_envelope_digest: str,
                        crossing_id: str, granted_multiplier: float,
                        ts: float | None = None, **extra: Any) -> dict[str, Any]:
        return self.append(kind=KIND_CROSSING, lane=lane, agent=agent,
                           originating_agent=originating_agent,
                           originating_envelope_digest=originating_envelope_digest,
                           scenario=scenario, crossing_id=crossing_id,
                           granted_multiplier=float(granted_multiplier),
                           ts=ts, **extra)

    def grade(self, *, crossing_id: str, lane: str, agent: str, outcome: str,
              originating_agent: str, originating_envelope_digest: str,
              ts: float | None = None, **extra: Any) -> dict[str, Any]:
        return self.append(kind=KIND_GRADE, lane=lane, agent=agent,
                           originating_agent=originating_agent,
                           originating_envelope_digest=originating_envelope_digest,
                           crossing_id=crossing_id, outcome=outcome,
                           ts=ts, **extra)

    # ---- trust ----------------------------------------------------------
    def trust(self, lane: str, *, originating_agent: str | None = None,
              now: float | None = None,
              max_age_s: float = DEFAULT_MAX_AGE_S) -> dict[str, Any]:
        """Graded evidence for a lane.

        Returns ``status`` in {"ok", "unknown"}. ``unknown`` means the caller
        has NO basis to grant authority — it is never a permissive default.
        A structurally broken chain is also ``unknown``.
        """
        clock = float(now if now is not None else time.time())
        base = {"lane": lane, "originating_agent": originating_agent,
                "n_graded": 0, "successes": 0, "failures": 0,
                "win_rate": None, "last_graded_ts": None,
                "cooldown_until": None, "in_cooldown": False,
                "status": "unknown", "reason": "no_graded_evidence"}
        if not self.verify_chain():
            return {**base, "reason": "chain_verification_failed"}
        graded: list[dict[str, Any]] = []
        try:
            for row in self.rows():
                if row.get("kind") != KIND_GRADE or row.get("lane") != lane:
                    continue
                if originating_agent is not None and \
                        row.get("originating_agent") != originating_agent:
                    continue  # invariant 6: trust is per originator
                if float(row.get("ts", 0.0)) > clock + FUTURE_TOLERANCE_S:
                    continue  # never count future-dated evidence
                graded.append(row)
        except LedgerError:
            return {**base, "reason": "chain_verification_failed"}
        if not graded:
            return base
        graded.sort(key=lambda r: float(r["ts"]))
        last_ts = float(graded[-1]["ts"])

        # Collapse grades to ONE outcome per crossing. Two attacks otherwise:
        #   * inflation  — re-grading a single crossing N times satisfies a
        #     min_crossings threshold without doing the work;
        #   * whitewash  — re-grading a failure as success to erase it.
        # The kept outcome is the WORST ever recorded for that crossing
        # (failure > neutral > success): history is append-only, so a later
        # row is a correction that may add doubt, never remove it.
        severity = {"failure": 2, "neutral": 1, "success": 0}
        per_crossing: dict[str, dict[str, Any]] = {}
        for row in graded:
            cid = str(row.get("crossing_id") or f"__row:{row.get('row_hash')}")
            outcome = str(row.get("outcome") or "neutral")
            current = per_crossing.get(cid)
            if current is None or severity.get(outcome, 1) > severity.get(
                    str(current.get("outcome") or "neutral"), 1):
                per_crossing[cid] = row
        distinct = list(per_crossing.values())
        distinct.sort(key=lambda r: float(r["ts"]))

        successes = sum(1 for r in distinct if r.get("outcome") == "success")
        failures = sum(1 for r in distinct if r.get("outcome") == "failure")
        last_failure = next((float(r["ts"]) for r in reversed(graded)
                             if r.get("outcome") == "failure"), None)
        cooldown_until = (last_failure + self.cooldown_s) if last_failure else None
        in_cooldown = bool(cooldown_until and clock < cooldown_until)
        stale = (clock - last_ts) > max_age_s
        decided = successes + failures
        out = {
            "lane": lane, "originating_agent": originating_agent,
            "n_graded": len(distinct), "grade_rows": len(graded),
            "successes": successes, "failures": failures,
            "win_rate": (successes / decided) if decided else None,
            "last_graded_ts": last_ts,
            "cooldown_until": cooldown_until, "in_cooldown": in_cooldown,
            "status": "unknown" if stale else "ok",
            "reason": "evidence_stale" if stale else "ok",
        }
        return out
