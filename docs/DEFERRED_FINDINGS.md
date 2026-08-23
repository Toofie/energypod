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

Run by a background agent in a dedicated git worktree against the Milestone A
state; worktree removed afterwards; main tree never mutated. Kill rates:
events.py 66.3% (101 mutants), service.py 57.8% (578), simulator/pod.py 57.8%
(559). Equivalent-mutant counts accepted as noise: ~22 (events), ~75
(service), ~60 (pod) — dominated by error-message text, StrEnum fallbacks,
annotation-only edits, and contract-declined literal pins. Genuine test gaps,
queued by value:

1. **Facade audit/publish content assertions (~86 survivors, highest value):**
   the facade suite never asserts audit-event content (event_type, result,
   reason_codes, payload, correlation_id, policy_version, fingerprints,
   requested/authorized watts) nor published-body payloads beyond type
   presence. One compact exact-dict test per happy-path operation kills the
   cluster.
   Status: FIXED 2026-08-23 — `tests/unit/test_facade_audit_content.py` pins
   the full AuditEvent dict (fingerprints re-derived independently from the
   pinned fact sets) and the exact published body for submit_intent, arm,
   disarm, emergency_stop, acknowledge_emergency_stop, and
   acknowledge_inhibit. submit_advisory_intent deliberately not pinned here:
   it is excess-charging surface under active rework.
2. **Simulator literal register image (~90 survivors):** served words are
   pinned by range/coherence, never literal word values (status/SOC/SOH
   placement, limit words, energy word pairing, extrema triples + index
   words, +40 temperature offsets, RTU-ID value at 0x8106, reserved words
   zero, one-past-block reads raising). One golden full-plan snapshot per
   scenario step computed with literal word arithmetic.
3. **Golden energy/SOC scenario (~28):** a load long enough to cross the
   0.1 kWh integration resolution, asserting exact energy deltas and SOC
   movement; watchdog expiry exactly at the deadline; a second poll after
   expiry (latent empty-lease crash); post-expiry split-interval accounting;
   energy-count ceiling path.
4. **Validation matrices (~30):** SimulatedEnergyPod constructor validation
   (19: identity/bic/seed/watchdog/interval shapes + inject hook boundaries);
   facade input shapes (string unit_ids, non-int watts/ttl, limit/cursor
   boundaries, reason length 500/501, Direction enum dispatch); event-bus
   bound=1, bool bound, missing type, missing payload, NaN fail-closed,
   broken clock.
5. **Behavioral pins (~40):** unknown-unit-FIRST emergency stop ordering;
   partial-stop acknowledgement liveness (the fleet-wide short-circuit leaves
   one branch untested); exact degraded-list reason codes; health
   per-dependency reason strings and control-readiness reason content; arm
   interactivity requirement; fenced_generation/stop_id response fields;
   post-aclose bus terminality (two terminal reads); cursor-ahead-of-live
   duplicate suppression; connection-epoch assertions; cell cadence across
   multiple refreshes; extrema tie-breaking; constructor defaults.
6. **Equivalent-mutant hygiene:** accept the message-text/StrEnum clusters;
   consider mutant exclusions if a mutation-score gate is ever added.

## Queue: implementation-review P2 notes (2026-08-22, 30 items)

From the 32-agent Milestone A implementation review (all P0/P1 were fixed in
`858caf9`; these P2s remain):

1. EventBus: JSON enforcement bypassable via payload aliasing/mutation after
   publish (retention/subscribers can later hold non-serializable envelopes).
   Status: FIXED 2026-08-23 — `_build_envelope` re-materializes the envelope
   from its own JSON encoding, so the retained/delivered copy is exactly the
   wire shape and post-publish caller mutation cannot reach it; nested
   non-serializable values fail the publish.
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

## Queue: Milestone B residuals (2026-08-22, non-blocking)

1. web/src/api/client.ts openEvents parks at an internal await while idle, so
   a queued iterator.return() cannot close the OS socket until a frame arrives
   on the abandoned stream — consumption provably stops (pinned), but the
   socket lingers. One-line abort parameter on openEvents closes it promptly.
2. Home/Batteries/Now derive liveness from receiving snapshot frames rather
   than the plane's exported stale flag; during outages each per-view
   resubscribe can transiently re-assert "this picture is current" until the
   shell's failed retry ends it (wording oscillation; data adoption is
   correctly guarded).
3. main.tsx mounts on import, so composed.test.tsx replicates the view
   registry verbatim; exporting the registry (or guarding the mount behind an
   entry function) would make composition drift between main.tsx and the
   composed suite a compile error.
4. web/src/api/client.ts AuditEvent type annotates a `type` field the wire
   never sends (wire key is `event_type`) and UnitSnapshot.telemetry_age_s is
   non-nullable while the wire sends null — the documented cast lives in
   wire.ts auditPage().
5. Server-side surface gaps the console renders honestly as "No data":
   snapshot units carry no SOC/temperature/cell/inhibit-cause fields; audit
   pagination exists but the console's queue item 9 (REST surface) remains
   the tracking entry.
6. Visual inspection of all console states requires a display: run
   `corepack pnpm dev` in web/ against `energypod simulate` for manual review;
   the automated state/a11y coverage lives in the vitest suites.

## Queue: Milestone A repair-agent notes (2026-08-22)

- Simulator transport exposes `.pod` for scenario handles; composition maps
  `runtime.simulators[unit_id]` to the pod — consider a narrower scenario-handle
  facade so tests cannot reach device internals.
- `_SystemClock` is process-relative with origin 1000.0; persisted absolute
  monotonic windows from prior processes must never be reused (observe-only
  boot already guarantees this; document in operator guide).
