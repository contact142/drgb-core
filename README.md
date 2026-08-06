# drgb-core

Reference implementation of the **Dual Reality Guardian Bridge (DRGB)**.
Inventor: Derek Keene.

> **PRIVATE — PRE-FILING. DO NOT PUBLISH.**
> No public repository, no open-sourcing, no whitepaper, no external demo
> until a provisional patent application is filed. Trade-secret posture until
> counsel advises otherwise. See
> `operator-brain/docs/superpowers/specs/2026-08-06-drgb-invention-disclosure.md`.

## What it is

A domain-agnostic governance layer that lets any system **earn** authority
from evidence instead of being granted it by assertion.

- **Observe** — adapters attach to arbitrary event sources (timers, services,
  logs, model routes, trading lanes, motion controllers).
- **Twin** — a shadow reality evaluates the same inputs, fired only at
  declared **events** (never continuously). This is what makes an always-
  available second reality affordable.
- **Guardian** — an append-only, hash-chained ledger grades live-vs-shadow
  divergence over time, per lane / skill / agent.
- **Bridge** — converts ledger evidence into a *graduated* authority
  multiplier, bounded by an operator-set envelope.
- **Graph** — expected-vs-actual state plus the trust ledger, densifying over
  time, used to route authority and attention.

## Invariants (enforced in code, not documentation)

1. **Exits are never gated.** No guardian state may block an abort, exit, or
   rollback. This is the single most important property.
2. **The ceiling is not self-raisable.** A process may climb toward its
   operator-set ceiling by earning it; it may never raise the ceiling.
   Attempts raise and are logged as violations.
3. **Unavailable is not zero.** Missing or stale evidence freezes authority;
   it never defaults permissive.
4. **Evidence before authority.** Promotion requires a pre-registered sample
   size and an out-of-sample window.
5. **Failure imposes cooldown.**
6. **No authority laundering.** A request that crosses from one agent to
   another executes under the **requester's** envelope, never the executor's.
   Every ledger row records `originating_agent` and `originating_envelope_digest`,
   so a low-authority agent cannot obtain an outcome by asking a high-authority
   peer to perform it. This is what makes an inter-agent mesh safe: agents
   exchange **evidence** (trust records, divergence reports), not permissions.

## Known limit: evidence storage must outrank the observed process

The guardian ledger is hash-chained and head-checkpointed, so mutation,
reordering, interior deletion, replay, forged rows, and tail truncation are
all detected. **But an actor with write access to BOTH the ledger and its
head file can manufacture a consistent shorter history** — deleting its own
failures and recomputing the checkpoint. This is asserted by
`test_e2_head_forgery_is_the_known_limit_and_is_documented` so it can never
be a surprise.

The mitigation is deployment-level, not code-level, and any serious
installation must choose one:

1. keep the ledger/head on **append-only or remote storage** the observed
   process cannot rewrite (the strongest option);
2. **sign** head checkpoints with a key the observed process cannot read;
3. **replicate** heads to a peer, so a rewound history disagrees with the mesh.

Stated plainly: DRGB can prove tampering to anyone who controls the evidence
store. It cannot bind an actor who controls its own evidence store — the same
reason auditors do not let the audited party hold the only copy of the books.

## First install: study, then ask

    venv/bin/python bootstrap.py

Order is the point: **discover** existing graphs (adopt, never re-derive) ->
**scope** lanes and blast radius -> **observe** which event sources actually
answer -> **preflight** for anything that would silently stop evolution ->
**interview** the operator with the few questions those findings provoked.

Every question cites what was observed, lists its options, and states the
consequence of each answer — questions that could not have been written in
advance. Unanswered questions fall back to the RESTRICTIVE default and are
reported as defaulted, so an install never hangs and never quietly assumes
permission. Answer later with `--answer key=value` and re-run.

## Bootstrap: scope before authority

On install, DRGB's first job is to learn the shape of its environment — and it
looks for **existing graphs first** rather than re-deriving the world:

1. **Discover** second-brain / graph artifacts already on the host
   (`graphify-out/graph.json`, manifests, memory stores).
2. **Adopt** them as the scope map: lanes, hosts, services, blast radius.
   No graph found is *reported*, never assumed empty.
3. **Grow** its own namespaced subgraph (`drgb:lane`, `drgb:crossing`,
   `drgb:trust`, `drgb:divergence`) — write-isolated, never editing the
   host's graph files.
4. **Sync upward** by export, on the host's own terms.

**A lane whose blast radius is unmapped cannot earn a ceiling above 0.0.**
Scope understanding is a prerequisite for authority, not a nicety.

## Status

Phase 2 of `operator-brain/docs/superpowers/specs/DRGB_WORK_QUEUE.md`.
Conformance tests: `2026-08-06-drgb-adaptation-test-plan.md` (C3 first).
