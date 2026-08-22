# Deferred findings queue

Non-blocking findings from mutation analysis, adversarial reviews, and audit
passes. Items here are queued, not forgotten: they are triaged at every
milestone boundary and promoted only when they touch safety, correctness of a
contracted behavior, or block the next milestone. Anything that would violate a
safety invariant is NEVER deferred — it is fixed before the milestone closes.

## Triage rules

- P0/P1 safety or contract-correctness findings are fixed in-milestone; they do
  not belong here.
- Entries carry: source, date, severity-if-fixed-later, file/line when known,
  and the concrete improvement. Mutation entries additionally carry the
  survivor class (equivalent vs genuine test gap).
- An entry leaves this file by being fixed (with its regression test) or by
  being explicitly rejected with a reason.

## Queue: mutation-analysis findings (2026-08-22, isolated worktree run)

Pending: the isolated mutmut run over events.py, service.py, and
simulator/pod.py reports its per-module survivor classification here when it
completes. Historical safety-module analysis (2026-08-21) already reduced
safety.py survivors 60 -> 23 with the remaining classified as non-vocabulary
equivalents (and/or operator swaps, getattr defaults, limit arithmetic
boundary mutants, dataclass slots).

## Queue: implementation-review P2 notes (2026-08-22, 30 items)

From the 32-agent Milestone A implementation review (all P0/P1 were fixed in
`858caf9`; these P2s remain):

1. EventBus: JSON enforcement bypassable via payload aliasing/mutation after
   publish (retention/subscribers can later hold non-serializable envelopes).
2. EventBus: future/negative cursors produce a silently dead iterator with no
   resync marker.
3. EventBus: abandoned subscriptions are never reclaimed (O(capacity) drain
   per publish forever).
4. EventBus: envelope accepts non-string keys / non-mapping payloads; in-memory
   vs wire shape can diverge.
5. EventBus: `resync_required` control type shares the publisher vocabulary; a
   publisher event of that type closes every client stream.
6. Simulator: blanket GOOD quality lets a malformed-injected limit word report
   62,535 W headroom unflagged.
7. Simulator: system-block battery current served/decoded signed against the
   evidence matrix's unsigned decode.
8. Simulator: PCS status incoherent between 0x0100+48 and 0x1000+1.
9. Facade: audit pagination cursor has no REST query parameter exposed yet
   (store side works; adapter parameter is the gap).
10. Facade: acknowledge/stop/ack-inhibit/arm/disarm still commit-then-raise on
    audit failure (submit_intent is atomic; extend the same pattern).
11. REST: known_stops degraded registration now handled; consider surfacing
    degraded-stop reason codes in the response body.
12. Composition: `_UnresolvedCredentialAuthenticator` is fail-closed; a real
    credential store is a Milestone C concern.
13. Composition: derive apparent-power/temperature-spread policy envelopes
    from explicit config fields when the policy contract gains them.
14. Composition: consider construction-time rejection of legacy-profile units
    in simulate mode (currently fail-closed at decode time only).
15. Actor: `request_bounded_zero` raises RuntimeError when the mailbox owner is
    dead; facade handles it — consider a uniform degraded-report type.
16. main: serving host/port are module constants until a listener config
    contract exists.
17. main: types-PyYAML stub not pinned in dev deps (targeted type-ignore used).
18. Golden: add a latched-inhibit scenario (inject blocking fault through the
    simulator, drive latch -> acknowledge -> recovery end to end).
19. SQLite: `_SequencedSQLiteAuditRepository` lives in composition; move the
    sequence projection into the shipped persistence class when owned.
20. Ledger-known gaps (tracked in CONTINUITY.md): run-mode telemetry decode
    unwired until identity evidence commissioned; repeated-timing-failure and
    external-writer latched causes deferred; exact Waveshare framing and
    watchdog timing are commissioning captures.

(The remaining P2s from the review were stylistic/defensibility duplicates of
the above classes; the full list lives in the 2026-08-22 review output
referenced from CONTINUITY.md.)

## Queue: Milestone A repair-agent notes (2026-08-22)

- Simulator transport exposes `.pod` for scenario handles; composition maps
  `runtime.simulators[unit_id]` to the pod — consider a narrower scenario-handle
  facade so tests cannot reach device internals.
- `_SystemClock` is process-relative with origin 1000.0; persisted absolute
  monotonic windows from prior processes must never be reused (observe-only
  boot already guarantees this; document in operator guide).
