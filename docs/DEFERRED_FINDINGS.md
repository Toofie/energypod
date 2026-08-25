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

## Ledger conventions (2026-08-23 pass)

Each entry below now carries a Status line: FIXED <short-sha> (fix + regression
test landed on this branch), RESOLVED-BY <short-sha> (landed in the main tree
since the finding was filed), DEFERRED-CONFLICT (proper fix overlaps the
excess-charging / publish-fence rework in flight in the main tree), or SKIPPED
(reason). SKIPPED-for-time entries stay queued and keep their priority.

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
   Status: FIXED 08fa5fc — `tests/unit/test_facade_audit_content.py` pins
   the full AuditEvent projection (fingerprints re-derived independently
   from the pinned fact sets) and the exact published body for
   submit_intent, arm, disarm, emergency_stop, acknowledge_emergency_stop,
   and acknowledge_inhibit. submit_advisory_intent deliberately not pinned
   here: it is excess-charging surface under active rework.
2. **Simulator literal register image (~90 survivors):** served words are
   pinned by range/coherence, never literal word values (status/SOC/SOH
   placement, limit words, energy word pairing, extrema triples + index
   words, +40 temperature offsets, RTU-ID value at 0x8106, reserved words
   zero, one-past-block reads raising). One golden full-plan snapshot per
   scenario step computed with literal word arithmetic.
   Status: FIXED d8e6e66 — tests/simulator/test_register_image.py pins the
   EXACT words of every served block (357 words per snapshot: 12 IoT + 5
   common) at two scripted moments (objective live under lease; post-watchdog
   with cell refresh), recomputed from the seed and the documented layout
   (never production code), plus block-edge reads (last word serves, one past
   refuses).
3. **Golden energy/SOC scenario (~28):** a load long enough to cross the
   0.1 kWh integration resolution, asserting exact energy deltas and SOC
   movement; watchdog expiry exactly at the deadline; a second poll after
   expiry (latent empty-lease crash); post-expiry split-interval accounting;
   energy-count ceiling path.
   Status: SKIPPED (time).
4. **Validation matrices (~30):** SimulatedEnergyPod constructor validation
   (19: identity/bic/seed/watchdog/interval shapes + inject hook boundaries);
   facade input shapes (string unit_ids, non-int watts/ttl, limit/cursor
   boundaries, reason length 500/501, Direction enum dispatch); event-bus
   bound=1, bool bound, missing type, missing payload, NaN fail-closed,
   broken clock.
   Status: SKIPPED (time) — partially covered as a side effect of the
   event-bus hardening commits (45eb20b bool/non-int cursor rejection and
   payload-shape rejection; b2030f1 nested fail-closed at publish). The
   simulator-constructor and facade-shape matrices remain open.
5. **Behavioral pins (~40):** unknown-unit-FIRST emergency stop ordering;
   partial-stop acknowledgement liveness (the fleet-wide short-circuit leaves
   one branch untested); exact degraded-list reason codes; health
   per-dependency reason strings and control-readiness reason content; arm
   interactivity requirement; fenced_generation/stop_id response fields;
   post-aclose bus terminality (two terminal reads); cursor-ahead-of-live
   duplicate suppression; connection-epoch assertions; cell cadence across
   multiple refreshes; extrema tie-breaking; constructor defaults.
   Status: SKIPPED (time). Note cursor-ahead-of-live is now pinned by
   45eb20b (future-cursor marker test).
6. **Equivalent-mutant hygiene:** accept the message-text/StrEnum clusters;
   consider mutant exclusions if a mutation-score gate is ever added.
   Status: ACCEPTED, no action — exclusion list only if a gate is added.

## Queue: implementation-review P2 notes (2026-08-22, 30 items)

From the 32-agent Milestone A implementation review (all P0/P1 were fixed in
`858caf9`; these P2s remain):

1. EventBus: JSON enforcement bypassable via payload aliasing/mutation after
   publish (retention/subscribers can later hold non-serializable envelopes).
   Status: FIXED c3ead77 — `_build_envelope` re-materializes the envelope
   from its own JSON encoding, so the retained/delivered copy is exactly the
   wire shape and post-publish caller mutation cannot reach it; nested
   non-serializable values fail the publish.
2. EventBus: future/negative cursors produce a silently dead iterator with no
   resync marker.
   Status: FIXED 45eb20b — negative and non-int cursors are rejected
   input; a cursor ahead of the live edge (desynchronized client, e.g.
   carried from another process instance) gets an explicit `future_cursor`
   resync marker naming the live snapshot and then continues from the live
   edge.
3. EventBus: abandoned subscriptions are never reclaimed (O(capacity) drain
   per publish forever).
   Status: FIXED 96edc09 — the bus holds only weak references to live
   subscriptions; an abandoned iterator is garbage-collected and its entry
   reclaimed at the next publish, while aclose() still detaches eagerly.
4. EventBus: envelope accepts non-string keys / non-mapping payloads; in-memory
   vs wire shape can diverge.
   Status: FIXED b2030f1 — non-string body keys and non-mapping payloads
   are rejected at publish (TypeError, no sequence consumed); any Mapping
   payload is canonicalized to a plain dict so the retained envelope is
   exactly the wire shape.
5. EventBus: `resync_required` control type shares the publisher vocabulary; a
   publisher event of that type closes every client stream.
   Status: FIXED ffa9ca8 — the WebSocket consumer only treats frames typed
   `resync_required` WITHOUT a `sequence` as its own terminal control frame;
   a published event of that type (always carrying the bus-assigned
   sequence) is delivered and the stream stays open.
6. Simulator: blanket GOOD quality lets a malformed-injected limit word report
   62,535 W headroom unflagged.
   Status: FIXED e99c009 — the production decoder was ALREADY fail-closed
   (signed limit decode, negative domain => BAD; now also pinned for the
   exact 0xFFFF word). The real gap was the composed SIMULATE decode:
   unsigned limit decode + blanket GOOD quality. It now derives every
   quality field with the decoder's own fail-closed helpers (a sentinel
   limit is BAD with the value absent, never 62,535 W GOOD; a garbage
   percentage fails its field closed instead of crashing the observation)
   and the pod gained script_quality/clear_scripted_quality (per-field
   scripted degradation, served words untouched).
7. Simulator: system-block battery current served/decoded signed against the
   evidence matrix's unsigned decode.
   Status: DEFERRED-CONFLICT — proper fix is in
   `src/energypod/adapters/modbus/decode.py`, the same module as the in-flight
   PCS-decode rework for excess charging.
8. Simulator: PCS status incoherent between 0x0100+48 and 0x1000+1.
   Status: DEFERRED-CONFLICT — PCS decode/blocks are the center of the
   in-flight excess-charging rework (advisory CT words, PCS live block).
9. Facade: audit pagination cursor has no REST query parameter exposed yet
   (store side works; adapter parameter is the gap).
   Status: RESOLVED-BY 89f483a — `GET /audit?limit&after_sequence` is
   exposed in `src/energypod/api/rest.py` and contracted in
   tests/api/test_rest_contract.py.
10. Facade: acknowledge/stop/ack-inhibit/arm/disarm still commit-then-raise on
    audit failure (submit_intent is atomic; extend the same pattern).
    Status: FIXED bdbc5a0 — one doctrine, pinned red-first per mutation:
    inverse-and-enables-power is atomic with its audit (arm compensates:
    failed append stops the request, disarms every unit it armed, one
    unit_armed/rolled_back row per compensation, original error surfaces);
    no-inverse ops (acknowledge_inhibit, acknowledge_emergency_stop) append
    durably BEFORE touching state, append failure refuses with the latch
    intact; safety-positive ops (disarm, cancel_intent) stand and name the
    failed record in the response degraded list. submit_intent/
    submit_advisory_intent were already atomic (verified); set_excess_
    charging verified (P3 append-first landed with the toggle; the
    participation flip's commit-then-audit row is documented doctrine).
11. REST: known_stops degraded registration now handled; consider surfacing
    degraded-stop reason codes in the response body.
    Status: FIXED bdbc5a0 — emergency_stop error paths attach the uniform
    DegradedReport (stop_id + degraded codes); REST surfaces them as 503
    emergency_stop_degraded with details, keeping the exact-id
    acknowledgement usable.
12. Composition: `_UnresolvedCredentialAuthenticator` is fail-closed; a real
    credential store is a Milestone C concern.
    Status: RESOLVED-BY 6e2db92 — live run mode shipped a credential store
    (src/energypod/runtime/credentials.py, tests/unit/test_credential_store.py).
13. Composition: derive apparent-power/temperature-spread policy envelopes
    from explicit config fields when the policy contract gains them.
    Status: SKIPPED — blocked on the policy contract gaining those fields
    (feature work, not a repair).
14. Composition: consider construction-time rejection of legacy-profile units
    in simulate mode (currently fail-closed at decode time only).
    Status: SKIPPED (time).
15. Actor: `request_bounded_zero` raises RuntimeError when the mailbox owner is
    dead; facade handles it — consider a uniform degraded-report type.
    Status: FIXED bdbc5a0 — DegradedReport (energypod.application.service)
    is the one shape: attached to errors whose safety work landed, and the
    per-mutation response degraded lists (disarm/cancel/emergency_stop and
    both acknowledgements) complete the uniform reporting.
16. main: serving host/port are module constants until a listener config
    contract exists.
    Status: SKIPPED — verified still true (DEFAULT_SERVE_HOST/PORT in
    src/energypod/main.py, no serving fields in ControllerConfig); the
    finding itself defers to a listener config contract.
17. main: types-PyYAML stub not pinned in dev deps (targeted type-ignore used).
    Status: FIXED (this pass) — types-PyYAML==6.0.12.20250822 pinned in
    pyproject dev deps; the `import-untyped` ignore removed from
    src/energypod/main.py.
18. Golden: add a latched-inhibit scenario (inject blocking fault through the
    simulator, drive latch -> acknowledge -> recovery end to end).
    Status: SKIPPED (time) — pairs with mutation item 3's golden program.
19. SQLite: `_SequencedSQLiteAuditRepository` lives in composition; move the
    sequence projection into the shipped persistence class when owned.
    Status: SKIPPED (time).
20. Ledger-known gaps (tracked in CONTINUITY.md): run-mode telemetry decode
    unwired until identity evidence commissioned; repeated-timing-failure and
    external-writer latched causes deferred; exact Waveshare framing and
    watchdog timing are commissioning captures.
    Status: TRACKED — no action in this pass (CONTINUITY remains the owner).

(The remaining P2s from the review were stylistic/defensibility duplicates of
the above classes; the full list lives in the 2026-08-22 review output
referenced from CONTINUITY.md.)

## Queue: Milestone B residuals (2026-08-22, non-blocking)

1. web/src/api/client.ts openEvents parks at an internal await while idle, so
   a queued iterator.return() cannot close the OS socket until a frame arrives
   on the abandoned stream — consumption provably stops (pinned), but the
   socket lingers. One-line abort parameter on openEvents closes it promptly.
   Status: SKIPPED (time).
2. Home/Batteries/Now derive liveness from receiving snapshot frames rather
   than the plane's exported stale flag; during outages each per-view
   resubscribe can transiently re-assert "this picture is current" until the
   shell's failed retry ends it (wording oscillation; data adoption is
   correctly guarded).
   Status: SKIPPED (time).
3. main.tsx mounts on import, so composed.test.tsx replicates the view
   registry verbatim; exporting the registry (or guarding the mount behind an
   entry function) would make composition drift between main.tsx and the
   composed suite a compile error.
   Status: SKIPPED (time).
4. web/src/api/client.ts AuditEvent type annotates a `type` field the wire
   never sends (wire key is `event_type`) and UnitSnapshot.telemetry_age_s is
   non-nullable while the wire sends null — the documented cast lives in
   wire.ts auditPage().
   Status: SKIPPED (time) — small; do together with item 1 in a web pass.
5. Server-side surface gaps the console renders honestly as "No data":
   snapshot units carry no SOC/temperature/cell/inhibit-cause fields; audit
   pagination exists but the console's queue item 9 (REST surface) remains
   the tracking entry.
   Status: RESOLVED-BY 89f483a (and the facade telemetry-summary work) —
   snapshot units now carry the full nullable telemetry summary
   (`telemetry.soc_pct`, cells, temperatures, faults/warnings) and the REST
   audit cursor parameter is exposed (see queue 2 item 9).
6. Visual inspection of all console states requires a display: run
   `corepack pnpm dev` in web/ against `energypod simulate` for manual review;
   the automated state/a11y coverage lives in the vitest suites.
   Status: TRACKED — manual review item; no code action.

## Queue: Milestone A repair-agent notes (2026-08-22)

- Simulator transport exposes `.pod` for scenario handles; composition maps
  `runtime.simulators[unit_id]` to the pod — consider a narrower scenario-handle
  facade so tests cannot reach device internals.
  Status: SKIPPED (time) — API-narrowing refactor; no behavior defect.
- `_SystemClock` is process-relative with origin 1000.0; persisted absolute
  monotonic windows from prior processes must never be reused (observe-only
  boot already guarantees this; document in operator guide).
  Status: SKIPPED (time) — documentation-only follow-up.

## Queue: live-hardware observations (2026-08-24, rhs commissioning)

1. Systematic discharge-power overshoot, +15–16% of command. Source: the
   live single-unit rhs test 2026-08-24 10:38–10:50 local (standby/normal
   register cycle evidence in `var/r3_standby_cycle_20260824.log`; the
   discharge power figures are the live telemetry/audit stream over the two
   legs). Both 1000 W discharge legs settled at a mean of ~1160 W against
   the 1000 W command; both legs cleanly cancelled. Pre-existing and
   independent of the register cycle (identical on both legs, cycle stages
   read back clean). Severity-if-fixed-later: P1 — accuracy of the
   delivered-power contract; charge direction is UNMEASURED, so the bound
   does not yet transfer to the night adviser's 2500 W cap. Concrete
   improvement: controlled single-unit legs at several setpoints
   (500/1000/2000 W, both directions), commanded vs settled mean; if the
   multiplicative bias is confirmed, calibrate the PQ write path (scaling,
   sign-magnitude encoding, or PCS response bias) or surface a
   delivered-vs-commanded correction in the console and audit until then.
   Status: OPEN (filed 2026-08-24; promoted if a charge leg shows the same
   bias at the policy cap).

## Queue: web fixture hygiene (2026-08-25)

1. SolarOutlook test fixture is a time bomb — it pinned an absolute
   instant (2026-08-25T06:00Z) and aged out of range, failing the
   suite on a clean tree (the single 1053/1054 web failure verified
   pre-existing at e82bb26). Improvement: compute RELATIVE instants
   from the test clock in that fixture, and sweep other fixtures for
   absolute-date pins while there. Status: OPEN (queued for the next
   web wave).

## Queue: night-V2 console wire gaps (2026-08-24, flagged by the console wave)

1. Fallback forecast frames carry no age figure — the console cannot say
   how old the fallback data is. Improvement: carry `fetched_at`/age on
   the fallback frame shape.
2. No wire surface for the Insights morning-landing attribution split —
   A1's missed-vs-refused distinction has no read route; the console
   cannot render it. Improvement: a read route (or snapshot key) serving
   the attribution split.
3. The 95 % completion ceiling is a design-pinned fact, not on the wire
   — the console's 95-vs-100 clause is hard-coded wording. Improvement:
   surface the ceiling (and its provenance) in the night projection.
4. The tariff wire carries defaults only — not the operator's
   commissioned 7.27 c night-window rate, so no night-cost figure can be
   computed console-side. Improvement: serve the commissioned rates (or
   the derived night cost) on the tariff wire.
   Status: OPEN (all four filed 2026-08-24 from the cca356a console
   wave; none blocks suggest-mode; revisit when act-mode promotion is
   considered).

## Queue: CONTINUITY.md deferred P2 inventory (cross-checked 2026-08-23)

Deduplicated against the queues above; statuses tracked here so the
CONTINUITY paragraph can shrink to a pointer.

1. ControlPolicy accepts zero `max_soc_jump_pct`/`max_soc_disagreement_pct`.
   Status: FIXED 2f1c01c — the model rejects a zero SOC jump or
   disagreement tolerance (degenerate: every live observation would
   violate it and the fleet could never re-arm).
2. `allocate_fleet_power` signature drift vs API_CONTRACTS.md wording.
   Status: DEFERRED-CONFLICT — the allocator's export-cap term is under
   active excess-charging rework in the main tree
   (src/energypod/domain/allocation.py).
3. In-memory authorization repository discards revoke reason.
   Status: DEFERRED-CONFLICT — the same repository now carries the permanent
   revoked-through fence (fd39ae7) being reworked for the publish-fence
   generation fix.
4. SQLite `_json_value` non-determinism for set/frozenset.
   Status: FIXED d061122 — set/frozenset items encode in deterministic
   (repr-sorted) order; tuple/list keep caller order.
5. Policy-timing cross-validation skipped when timing validation fails
   first. Status: SKIPPED — pydantic field validation is fail-first by
   design; collecting both errors needs a model_validator restructure for
   little operator value (both surfaces name the offending field).
6. SQLite `open` creates parent directories without path validation.
   Status: SKIPPED (time).
7. O(n^2) schedule overlap detection. Status: SKIPPED (time) —
   constructor-time cost with tiny n today.
8. Untyped Any in in-memory repositories. Status: SKIPPED — typing
   hygiene only, no behavior value.
9. `_revoke_after_failure` can mask the original exception.
   Status: DEFERRED-CONFLICT — src/energypod/application/control_kernel.py
   is under active publish-fence rework (5e11c70 fixed the kernel side).
10. No audit event when no intent is selected. Status: DEFERRED-CONFLICT —
    control_kernel.py, same rework.
11. FaultTracker prefix type guard. Status: SKIPPED (time).
12. Waveshare `close` failure handling. Status: SKIPPED (time).
13. Shutdown transport leak if the owner dies mid-check.
    Status: SKIPPED (time).
14. Stale read-preemption start time. Status: SKIPPED (time).
15. Redundant `_used_cycles.clear()` in `fence()`. Status: SKIPPED
    (time) — actor-side fence cleanup, not the generation publish fence.
16. Duplicated idempotency-key parsing in rest mutation lambdas.
    Status: SKIPPED (time).
17. Missing ttl_s<=0 negative cases in REST/MCP tests.
    Status: SKIPPED (time).
18. One tautological prompt-injection assertion.
    Status: SKIPPED (time).
19. `energypod.main` entry point absent though declared in pyproject.
    Status: RESOLVED-BY f99bda1 (Milestone C packaging; tests in
    tests/unit/test_main_entry.py).

## Queue: pod-parking mutation round (2026-08-24, isolated worktree run)

Run by the mutation-testing agent in a dedicated worktree at a084920 (the
parking-round HEAD at run start), removed afterwards; the main tree never ran
mutants. mutmut 2.4.5, per-target runners (the parking test set, the facade
test set, the transport contract tests). Kill rates: waveshare.py 48.4% (217
mutants: 105/112/0 killed/survived/timeout — but inside the write_debug_mode
target region every SEMANTIC mutant died: the {0,1} bound, the FC16 shape,
the echo validation, the resync discipline; the in-region survivors were six
message strings and the _is_error defensive branches); parking.py 56.7% (891:
505/386/0); service.py scoped to the parking facade surface 42.7% (96: 41/55/
0 — the facade methods were extracted verbatim into a worktree-only scope
module rebound at import, so the 3770-line module stayed out of the run).
The service.py facade finding that matters most: renew_park_lease's real body
was executed by no test at all (REST/MCP contract suites fake the service;
the composition suite pins only the route string).

Killing tests landed in the main tree after per-cluster mutant verification
in the worktree (39 tests): tests/unit/test_parking_mutation_kills.py (the
REAL facade: interactive principal per method, confirmation/takeover
literals, block-absent and unknown-unit per method, lease bounds, correlation
keys, renew sliding + anti-rollover boundary, the exact dispatch refusal
message), tests/unit/test_parking_controller_kills.py (wire-code literals,
commissioning/lease-domain/wall-clock validation, exact park/expiry row
shapes with independently recomputed fingerprints, inherited-park semantics,
expiry and boot boundaries, parked_facts postures, conflict classes, the
unmapped vendor word > 6, takeover observed_since, degraded resume,
checklist clamp/latch, foreign_mode details, the provenance window boundary,
closing-detail origins), tests/integration/test_waveshare_mutation_kills.py
(the exact domain-refusal/echo-validation messages, the ModbusException
mapping, _is_error fail-closed). Residue, by class:

1. Audit-payload KEY clusters (~150 survivors): "XXkeyXX"-class mutants over
   the park/resume/renew row payloads and event bodies. Narrative rows carry
   their payload only as a fingerprint (AuditEvent has no payload field), so
   keys are observable only through recomputed fingerprints.
   Concrete improvement: a row-pinning pass recomputing fingerprints from
   expected payloads per mutation (the test_facade_audit_content pattern),
   or asserting the lease store's committed payload projections.
   Status: QUEUED (highest-value residue).
2. Message-text clusters (~90): refusal messages, vendor-name fallbacks, the
   faults retention note, _map_debug_mode_error texts. The load-bearing ones
   (domain refusal, hints, degraded codes, expiry row) are now pinned;
   the rest is the standing accepted noise class.
   Status: ACCEPTED (message vocabulary), with the pinned exceptions above.
3. Equivalent mutants (~35): _FOREIGN_MODE_WORDS set elements (the `> 1`
   disjunct dominates), the empty `elif LEASE_EXPIRED: pass` inversion,
   _is_error's None branch (the function-code check catches None), the
   vendor-word guard `> 1` -> `> 2` (the 2-6 set covers the delta),
   supervise's post-expiry in-flight lease copy (table truth governs the
   projection), the LEASE_OPEN closing-details entry (unreachable through
   renew refusals), annotation-only `|`->`&` edits, slots/frozen flags.
   Status: ACCEPTED as noise.
4. Out-of-round transport surface (~100 survivors in waveshare.py outside
   write_debug_mode): WaveshareTransportConfig's constructor validation is
   entirely untested (~36), the generic write_registers gate boundary
   arithmetic (~20), close/ensure state mutants, the FC03 read-validation
   message cluster. Severity-if-fixed-later: P2 (the config block guards
   retries==0 and the address domains; all are fail-closed raises).
   Status: QUEUED — a compact config-validation parametrize plus gate
   boundary cases kills the cluster.
5. Controller residue (~70): divergence-memory partial branches (the
   word=1 first-look vs transition split), the faults-window scan boundaries
   (continue-vs-break on unit mismatch and pre-park rows, the
   exactly-at-parked_at edge), checklist rounding digits, _optional_word
   boundaries, the boot scan's continue past unknown units (mutant 525),
   _enum_text/_actor_lifecycle fallbacks behind present actors.
   Status: QUEUED (SKIPPED for time this round).
6. Never-mutated new code: the boot-adoption feature (1116ded,
   _adopt_pending_park and its ~180 lines plus tests) landed after the
   a084920 snapshot — it has had no mutation round.
   Status: QUEUED for the next round's parking target list.

Run mechanics worth recording: a mutmut 2.4.5 pony cache bug ("Mutant.line
is required") fires on interrupted registrations; it was worked around with a
reversible local venv patch (restored afterwards — the shared .venv is clean
again), and interrupted runs resumed from the sqlite cache.
