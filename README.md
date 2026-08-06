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

## Status

Phase 2 of `operator-brain/docs/superpowers/specs/DRGB_WORK_QUEUE.md`.
Conformance tests: `2026-08-06-drgb-adaptation-test-plan.md` (C3 first).
