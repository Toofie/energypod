# Pod parking — the sanctioned standby mode

Design date 2026-08-24. Status: CONTRACT v2 (two red-team panels folded in:
doctrine + engineering; verdicts REWORK→amended and SHIP-WITH-AMENDMENTS).
Builds on the live-proven standby cycle of 2026-08-24 (evidence
`docs/evidence/standby-cycle-2026-08-24.md`), vendor-app truth
(MiniESapp.cs decompile), external fleet-API patterns, and the industrial
guarded-action literature (IEC 61511, ISA-TR84 anti-rollover, permit-to-work,
MCP tool/elicitation specs).

## 0. What parking is — and is not

Parking a pod writes the vendor debug-mode register `0x8000 ← 1` (Standby)
through the controller's transport; resuming writes `0x8000 ← 0` (Normal).
Live-proven on rhs 2026-08-24: Standby parks the PCS control path (measured
power → 0) while comms, telemetry, pack voltage, and SOC reporting stay
alive; the exit write returned the pod to Normal in ~1 s with no wedge. The
vendor's own PQ sender refuses to dispatch when the mode word ≠ 0
(`MiniESapp.cs:2180-2184`) — parking manufactures the vendor's own
refused-send precondition.

**Parking is NOT electrical isolation.** The pack stays connected at full
voltage (166 V observed on rhs while parked); contactor state is unknown.
The fixed sentence — *"Parking is not electrical isolation — the battery
stays connected at full voltage. Never perform physical work on a parked
pod."* — appears verbatim in the park dialog, the unit banner, and the
OpenAPI summaries of both routes. No countdown may imply time-bounded
safety; the lease countdown is policy, never safety.

Values 2–6 (Charge, Discharge, Circulation, Fixing SOC, Verify Capacity)
remain **permanently unexposed** — unreachable in code (transport-layer
validation, §5), refused on approach (§2 `park_mode_out_of_scope`), and never
normalized by any surface. The whitelist is `{0, 1}` and nothing else, ever.

## 1. Doctrine decisions (each with its reason)

- **Park is a mode, not a setpoint.** Converges with Enode IDLE, Enphase
  Sleep/Wake, SMA 40236. The existing `device_debug_mode_active` dispatch
  refusal IS the wire-level gate; parking adds provenance, not a new code.
- **We consciously exceed the vendor's guards** (admin login only, no
  confirmation, no interlock, no readback comparison — `MiniESapp.cs:2159-
  2170`): interactive principal both directions, typed confirmation,
  actor-owned atomic write→read→verify, full audit, durable-first semantics.
- **Park refuses while armed or active** (`park_conflict_refused`). Discrete
  auditable acts over cascades (the arming/night-enable doctrine). The
  recovery walkthrough therefore reads disarm → park → resume (§7).
- **The lease is the expiry ALARM, not the expiry ACTOR.** Every park
  carries a TTL. At expiry the controller performs **no write**: it appends
  `unit_park_expired` (result `expired`, no write issued), publishes the
  alert-tier event, moves the projection to `{parked: true, expired: true}`,
  promotes the banner, and holds `health_state: PARKED` with the hint
  "lease expired — Resume is an operator act." Resuming a device is an
  enabling act; in this controller every enabling act (arm, resume, enable,
  publish) is an interactive human act — a timer is not a principal. Boot
  reconstructs and alarms the same way; boot never parks, never un-parks.
  Anti-rollover (ISA-TR84) unchanged: renewal may never extend past
  `parked_at + max_lease_s`; after expiry a NEW park requires fresh
  confirmation (an ordinary `PARK`, keyed to a new lease).
- **Park is a stop-direction act and never compensates by resuming.**
  Park/resume follow the durable-first no-inverse pattern
  (`acknowledge_inhibit`): the audit row is appended BEFORE the register
  write with `result: pending`, then completed (`parked` / `refused`) by the
  verified write→readback. A failed append refuses the park (fail-closed,
  retryable). A failed write leaves `refused`, mints no lease, and — if the
  word actually moved — takes the `write_unverified` posture (§3). A resume
  is never rolled back by bookkeeping; `degraded: [audit_unavailable]` rides
  the response exactly like disarm's.
- **Divergence is alarmed, never fought.** The debug word is compared
  against the lease ledger every poll. `word=1, no lease` → `origin:
  foreign`. `word=0, lease open` → **foreign resume observed**: the lease
  closes as `unit_resumed` / `result: observed_foreign` (`written_value:
  null` — no write), one alert-tier event carries `origin: foreign` + the
  observed transition time, and the dispatch provenance for the following
  window renders `resume_provenance: {observed_at, origin: foreign}`. The
  controller never re-parks in response (the CONTESTED-not-implementable
  posture). A foreign park OVER our unchanged lease renders
  `park_state.foreign_rewrite: true` — named, no write.
- **Resume works on any standby word, with consent where the park wasn't
  ours.** Resuming our own lease: plain `RESUME`. Resuming a word=1 with no
  controller lease: refuses 409 `park_foreign_word_acknowledgement_required`
  until the body carries `takeover: "FOREIGN"` (the arm takeover pattern —
  per-request, audited `foreign_takeover_acknowledged`, never persisted).
  Resuming our own expired lease needs no takeover (the operator's fresh
  RESUME IS the act). Resume on a Normal word is a no-op 200, `origin:
  "none"` — idempotent honesty, no error theater.
- **Synchronous response, not an action object.** Our write→readback is one
  serialized actor operation, bounded well under a second; the response
  carries `prior_word`, `written_value`, `readback_word`, `verified`, and an
  `as_of` timestamp. A replayed Idempotency-Key answer describes the
  ORIGINAL operation, never current state; a retried park after restart or
  entry eviction resolves as `park_already_parked` (correct, and stated).
- **MCP observes and recommends; it does not park.** v1 ships no MCP
  mutation tool; `get_unit_detail`/`get_snapshot` gain the park projection,
  `recovery_advisory`, and `delivery_bias`; the agent-loop contract text
  gains "never dispatch onto a parked unit; recommend the park/resume cycle
  to the human operator for the wedge signature." The API_CONTRACTS MCP
  sentence is amended to name park/resume as the operator-only REST surface
  (§11). Plan/apply + confirmation tokens remain the documented extension
  (§10), deliberately unbuilt.
- **Two-person rule: considered and declined** (single-operator home site;
  NIST four-eyes targets multi-operator command authority). The type-back
  unit id is the v1 guard; console type-back is client-side, the wire always
  carries the literal. Revisit if a second operator ever exists.

## 2. API surface

All three routes: `arm` scope AND interactive principal, Idempotency-Key
required (the shared `mutation()` wrapper), typed refusals as 409 envelopes
with pinned details shapes.

### POST /api/v1/units/{unit_id}/park

Request: `{"confirmation": "PARK", "reason": "<1..500, required>",
"lease_s": <60..max_lease_s, optional, default min(default_lease_s,
max_lease_s)>}`

200: `{unit_id, action: "park", prior_word, written_value: 1, readback_word,
verified: true, as_of, lease: {parked_at, expires_at, max_total_s, reason,
authorizer, epoch}, prior_state: {lifecycle, measured_watts}}`

Refusals (details shapes pinned):
- `park_not_commissioned` — no `parking:` block, or mode not write_enabled
  (split: `{"cause": "block_absent" | "mode_not_write_enabled"}`).
- `park_conflict_refused` — armed / under intent / latched stop:
  `{"units": [{"unit_id", "cause"}]}` (the arm outcomes shape; singular
  unit on a per-unit route).
- `park_already_parked` — `{"lease": {...}}`; renew instead.
- `park_mode_out_of_scope` — `prior_word ∈ {2..6}`:
  `{"prior_word", "vendor_name"}` (from GlobalFun.cs:152-165). We never
  transition a vendor-directed mode we didn't set.
- `park_write_failed` — transport refused/timeout (the gateway classes):
  `{"error_class"}`. One bounded retry, then refuse.
- `park_readback_unverified` — ACKed but 0x8100 ≠ 1 after ONE retry (the B5
  shape; total budget ≤ `write_timeout_s` × 2 + gap):
  `{"prior_word", "written_value", "readback_word", "retries"}`. No lease;
  `write_unverified` posture if the word moved (§3).

### POST /api/v1/units/{unit_id}/park/renew

Request: `{"confirmation": "RENEW", "lease_s": <60..cap, required>}` —
sliding: `new expires_at = now + lease_s`, never past `parked_at +
max_lease_s`. Refuses `park_lease_cap_reached`
(`{"parked_at", "max_total_s", "requested_expires_at"}`) and
`park_lease_absent` (never parked / already closed — details
`{"origin": <closing row's origin>, "closed_at": <ISO timestamp>}`).
200 mirrors park's lease object + `as_of`.

### POST /api/v1/units/{unit_id}/resume

Request: `{"confirmation": "RESUME", "takeover": "FOREIGN"?}` — the
takeover field is required exactly when `prior_word == 1` with no controller
lease.

200: `{unit_id, action: "resume", prior_word, written_value: 0,
readback_word, verified: true, as_of, origin: "operator" | "foreign" |
"none", checklist: {comms_age_s, soc_drift_pct, soc_pct_at_park,
measured_watts_now, faults_while_parked, faults_retention_note,
latched_stops: [ids], latched_inhibit: bool}}`

Refusals: the commissioning/write/readback twins above;
`park_foreign_word_acknowledgement_required`
(`{"acknowledgement": "FOREIGN", "prior_word", "observed_since"}`);
`park_mode_out_of_scope` for `prior_word ∈ {2..6}` — R2b normalization of a
vendor mode is its own acknowledged act, never a resume alias;
`resume_stop_latched` when a latched emergency stop names the unit (resume
would re-enable autonomy under a standing stop instruction — acknowledge
the stop first): `{"stop_ids": [...],
"acknowledgement_endpoint": "/api/v1/emergency-stop/{stop_id}/acknowledge"}`.

## 3. State, projections, and vocabulary

- `park_state` per unit (snapshot + unit detail): `{parked: bool, origin:
  "operator" | "foreign" | "unrecorded" | "none", parked_at,
  lease_expires_at, max_total_s, remaining_cap_s, expired: bool, reason,
  authorizer, foreign_rewrite: bool?, write_unverified: bool?,
  foreign_mode: {word, name, first_observed_at}?}` — absent key when
  uncommissioned. `unrecorded` covers word=1 with no lease AND no foreign
  evidence (crash-after-write residue); `foreign` is reserved for the
  vendor-app/foreign-flip class.
- A lease ends only by (a) verified resume write, (b) observed foreign
  resume, or (c) expiry (alarm-only). Under any OTHER ending the lease
  persists in the terminal sub-state `write_unverified`: `parked: true,
  write_unverified: true`, health reason `park_write_unverified`, hint
  naming the operator resume. **Our acts stay ours when they fail** — no
  failed write ever reclassifies a controller-minted lease as foreign.
- `health_state` gains `PARKED`, ladder position between
  `actuation_incoherent` and `self_healing`, **implemented as a composable
  state**: compute the healing-reason list first (pure), then
  `("parked", *healing)` — so a parked+balancing pod (observed live on lhs)
  keeps its balancing visibility. `unreachable`, `not_responding`,
  `foreign_writer`, `inhibited`, `actuation_incoherent` legitimately outrank
  a park (fault beats operator state — pinned so nobody "fixes" it). Wiring:
  `observe_cycle` gains `parked`/origin inputs fed from the lease state.
- Dispatch: the existing `device_debug_mode_active` refusal fires as today;
  details gain `parked_provenance: {parked_at, authorizer, reason,
  lease_expires_at}` when the ledger names the unit, and
  `resume_provenance: {observed_at, origin: foreign}` for the window after
  an observed foreign resume. A word ∈ {2..6} renders `foreign_mode` in the
  details ("device in an unexposed vendor mode"), never `parked: true`.
  Control-readiness reasons gain `unit:parked`.
- **Adviser vocabulary (pinned, one vocabulary per feature):**
  - Night-charge and schedule projections: `unit_parked` joins additively
    and **outranks `units_disarmed` for a parked unit** — resume, not arm,
    is the true next step (a parked unit cannot be armed usefully anyway).
  - Excess adviser: `unit_parked` renders in place of `no_eligible_target`
    when every otherwise-eligible unit's exclusion cause is park.
  - **The ScheduleRunner does NOT exclude parked units.** It stays dumb per
    DESIGN_SCHEDULES §7 ("no claim checks"): it submits the published fact,
    the facade refuses `device_debug_mode_active` with provenance (loud),
    the per-cycle failure is survivable per its standing doctrine, and
    `schedule_state` carries `unit_parked` as a projection-level code (the
    `units_disarmed` mechanism verbatim).
  - **Night-charge and excess DO exclude parked units at selection** —
    selection is those advisers' designed job (they already exclude
    non-eligible/full units; `unit_parked` is the stated exclusion cause in
    the projection), and an adviser repeatedly submitting into a standing
    refusal would violate the never-retry-a-denied-dispatch-unchanged
    doctrine. Coordinator ruling 2026-08-24, resolving the B1 ambiguity.
  - Foreign PQ objectives observed on a parked unit (the night-writer
    case): the night-writer detector annotates those samples `unit_parked`
    — never silent, never an alert by itself. Consequence chain stated
    honestly: per the pinned conservative simulator model (§6) an ignored
    write leaves the served-objective words unchanged, so a foreign write
    during our park shows at the readback and the next arm classifies it by
    the standing rules (possibly `external_writer` + takeover) — the
    wedge-recovery walkthrough copy says so.
- Audit rows: `unit_parked`, `unit_park_renewed`, `unit_resumed`,
  `unit_park_expired` — per unit; `result` ∈ {pending → parked, renewed,
  resumed, observed_foreign, expired, refused}; reason codes carry
  `readback_verified` / `readback_mismatch` / `foreign_takeover_acknowledged`
  / `adopted_foreign_park` (a park over a foreign word=1 records the origin
  transition — the ledger never claims we initiated a park we inherited);
  payload {prior_word, written_value, readback_word, verified, origin,
  authorizer, reason, lease fields, epoch, checklist on resume}. Bus events:
  `unit.parked`, `unit.park_renewed`, `unit.resumed`, `unit.park_expired`
  (alert tier on expiry), typed payloads mirroring the rows;
  `unit.health_changed` carries transitions both directions.

## 4. Lease durability, epochs, and restart semantics

- **Dedicated durable row** (not audit replay — the in-memory audit store is
  a bounded deque and would evict leases in simulator mode): sqlite
  `park_leases(unit_id PRIMARY KEY, opened_at, expires_at, max_total_s,
  reason, authorizer, epoch, state, soc_pct_at_park)` written in the SAME
  transaction boundary as the audit append. The audit row remains the
  human-correlated narrative; the table is the machine truth. Simulator/
  memory mode gets an unbounded in-memory map with the same interface.
- **Single-flight per unit**: a monotonically increasing `lease_epoch`;
  every mutation (park/renew/resume) performs guards AND state transition
  inside one critical section (park-controller asyncio lock) and carries
  `expected_lease_epoch`; a mutation on a closed epoch refuses
  (`park_lease_absent` family) instead of acting on a stale view. The
  transport sequence (write→readback→verify) is ONE actor-mailbox message
  (the `refresh_mode_words`/`request_bounded_zero` pattern), never three —
  a heartbeat PQ write must not interleave between our write and readback.
- **Arm-while-parked is legal** (a parked unit is disarmed; arm checks
  lifecycle/inhibit/qualification — dispatch remains the gate). Park
  re-checks armed INSIDE the critical section. The dispatch/park race (a
  dispatch accepted on a stale word, then the park lands, the pod
  ACK-then-ignores, coherence alarms ~4 cycles later) is a
  **detected-and-alarming** outcome by design — the watchdog is the fence,
  enumerated as such, not an error.
- **Boot:** reconstruct from the lease table + the device word. Expired-
  during-downtime → raise the expiry alarm (no write — ever). Pending row +
  word=1 → the durable-first park completed; adopt it as ours. Open lease +
  word=0 → the foreign-resume reconciliation of §1. Boot never writes the
  mode register under any path.
- Expiry timing rides the existing bounded supervision pass (a per-unit
  wall-clock check per fleet cycle — no new task class; the composition
  contract starts nothing).

## 5. Transport and the write-scope ADR

The structural write whitelist grows from ONE operation to TWO:
`(0x0200, [1, P, Q])` and `(0x8000, [v]) v ∈ {0,1}`. Doctrine-preserving
conditions (all four required):
1. A separately named `write_debug_mode(value)` method — the PQ predicate at
   `waveshare.py:188-189` stays byte-identical; `write_registers` (the
   generic path) can NEVER reach 0x8000, pinned by an architecture-fitness
   test; only the named method can, and only when the `parking:` block
   composed it.
2. **Transport-layer {0,1} validation** — the bound is structural at the
   lowest level (the same refusal the simulator's `apply_debug_mode`
   makes), not caller discipline; the method's docstring names the live
   evidence and the standing prohibition on 2–6.
3. **Per-write ledger promise**: every call appends exactly one audit row
   (durable-first, §1) carrying prior/written/readback words, actor,
   origin, request_id, times. A write without its row is a build failure;
   mutation-test coverage on the method and both routes is named in the
   test plan.
4. `_WRITE_BLOCKS` gains `{"debug_mode": RegisterBlock(0x8000, 1, 16,
   False)}` — the catalog stays the single admission list, docstring
   amended. The 0x8000-write / 0x8100-readback address asymmetry is called
   out in a comment so no implementer "normalizes" it.

### 5.1 The `parking:` config block (block-PRESENCE commissioning)

```yaml
parking:
  max_lease_s: 14400        # 360..86400; lease_s ∈ [60, max_lease_s]
  default_lease_s: 14400    # >= 60, <= max_lease_s
```

Validated at config load on block-PRESENCE (the night pattern): refused
unless `mode: write_enabled` AND a `policy` block is present — a present
block on an observe-only site is a validation error, never silently
incapable. `policy.debug_modes_enabled` remains the always-false tombstone
it is today, documented in the block comment as superseded-by-nothing: the
`parking:` block is the one and only policy flag that can ever compose this
write.

## 6. Simulator contract

- `_debug_mode` field served at the 0x8100 block (replacing the hard-pinned
  `[0]`); `apply_debug_mode(value)` validates `value ∈ {0,1}` else refuses.
- **ACK-then-ignore, pinned conservative**: while parked, `apply_pq_frame`
  still ACKs (live-proven) but delivered power stays 0; **the served
  objective words (0x1060+17/+18) are UNCHANGED by an ignored write, and
  the device watchdog lease is untouched** — "ignored means ignored." A
  scenario hook `script_parked_readback_shows_write` flips the readback
  half when live evidence lands; both sides tested.
- Scenario hook `script_debug_mode(value, at_s)`; the wedge signature is
  end-to-end simulable (accept dispatch → park mid-flight → 4-cycle
  coherence trip → advisory renders).
- Existing-test inventory: the transport-gate tests survive (named method,
  not widened predicate); tests pinning `_WRITE_BLOCKS` single-entry and
  the simulator's hard-pinned `[0]` change in the same round; nothing that
  parks exists today, so ACK-then-ignore breaks no current test.

## 7. Recovery advisory + delivery-bias estimator (companions)

- `recovery_advisory` on unit detail + MCP (pinned shape: present only
  when the signature holds, `{"commissioned": bool,
  "echo_classifications": [string]}` — `commissioned: false` is the
  uncommissioned-honesty state, absence = no advisory): when the classifier holds
  `actuation_incoherent` with echo `objective_not_served` or
  `echo_matches_write`, render: "Soft recovery available: the park/resume
  cycle (proven live 2026-08-24). Operator act: disarm → park → resume
  (seconds; keep the operator at the pod the first time per unit). If a
  foreign writer wrote during the park, re-arm may need the takeover
  acknowledgement." Advisory-only; abort-to-human unchanged. One honesty
  sentence owned: **the wedge recovery tool requires the `parking:` block
  commissioned** — the advisory renders as unavailable otherwise, never as
  a suggestion the site cannot execute.
- `delivery_bias` on unit detail + MCP (pinned shape:
  `{"mean_bias_pct", "max_bias_pct", "sample_count", "window_s"}`,
  evidence-only): bounded per-unit deque of
  (authorized, measured) samples while ACTIVE (window: last 600 cycles or
  15 min, whichever the memory bound allows; cap ~600 samples), projecting
  mean/max bias, sample count, window — labeled evidence-only. Motivated by
  the filed +15–16% discharge overshoot; charge samples accumulate
  identically. No control path reads it.

## 8. Console

- Park action: dialog with the fixed not-isolation line, reason (required),
  lease duration (bounded select), type-back the unit id; Resume action
  with the checklist rendered inline (including "remains stopped by
  {stop_id} — acknowledge separately" when latched).
- Parked chip + banner: "Parked — not isolation · lease expires in H:MM",
  promoting to the alert styling "lease expired — Resume required" at TTL.
  Chip tooltip names the mode word and pack voltage, not a status metaphor.
  Home carries a fleet banner while any pod is parked.
- Wedge-signature advisory card (§7) with the three-step walkthrough.
- Delivery-bias readout in unit detail (evidence styling, never a warning).
- One rhythm / one vocabulary / live-refresh standards; shot matrix per the
  polish bar (states × viewports, before/after vision-verified).

## 9. The ten challenge flags — resolved

| # | Flag | Ruling |
|---|---|---|
| 1 | Autonomous lease-expiry write | **REVERSED** — alarm-only expiry; no autonomous mode write, ever, including boot |
| 2 | Refuse-while-armed vs auto-fence | KEEP (discrete acts; walkthrough shows disarm) |
| 3 | MCP v1 no-act | KEEP (standing contract requires it; amended naming, §11) |
| 4 | PARKED ladder position | KEEP, composable-reasons implementation (§3) |
| 5 | Resume-on-foreign-word | CHANGED — `takeover: "FOREIGN"` required |
| 6 | Synchronous response | KEEP (bounded sub-second verify; `as_of` honesty) |
| 7 | Audit replay vs dedicated row | CHANGED — dedicated durable `park_leases` row |
| 8 | Simulator ACK-then-ignore | KEEP, pinned conservative + flip hook |
| 9 | Two-person rule | KEEP declined (single-operator site) |
| 10 | Both-directions interactive | KEEP — and now enforced on EVERY resume path (flag 1's reversal is the same principle) |

## 10. Future extensions (documented, deliberately unbuilt)

MCP plan/apply with server-minted pod-bound single-use TTL'd confirmation
tokens (elicitation where supported); one-shot `cycle` action for recovery;
park-aware schedule deferral (v1: runner stays dumb, §3).

## 11. Same-round amendment sweep (mandatory, before implementation)

1. `docs/PROTOCOL_EVIDENCE.md` §8 rows 0/1 → live-observed on this fleet
   (session 8645b050 evidence); §13 item 11 narrowed to values 2–6; §14 row
   rewritten ("values {0,1}: superseded by the authorized live trial + this
   design's guarded surface; values 2–6: unchanged, official documentation
   still required").
2. `docs/API_CONTRACTS.md` write-enabled section: the writable set becomes
   the two named blocks with their value domains; the "controller never
   writes 0x8000/0x0101" sentence amended (0x0101 stays never-written); the
   MCP sentence names park/resume as operator-only REST.
3. `docs/CONTROL_SURFACE_GAP_ANALYSIS.md` Part 4 row → new standing
   exclusion (values 2–6 + all other maintenance writes); Part 2 §2.3 and
   "do BETTER" #8 amended.
4. `docs/CONTINUITY.md` non-negotiable invariants + protocol facts amended
   (ledger entry by the coordinator thread at round close).
5. This feature's own API_CONTRACTS section (from §2/§3 here) and the
   DESIGN_SCHEDULES/API_CONTRACTS vocabulary amendments for `unit_parked`.

## 12. Test matrix (author FIRST, per repo doctrine)

T-PARK-COMMISSIONING (block present/absent; observe-only refusal shape;
scope/403; idempotency; literals; lease bounds) · T-PARK-HAPPY (200 shape,
audit, events, projection, health composition, dispatch provenance) ·
T-PARK-REFUSALS (every code + pinned details shape) · T-PARK-IDEMPOTENCY
(replay, conflict, stale-answer `as_of`, post-restart retry) ·
T-PARK-CONCURRENCY (single-flight epochs; park-vs-arm; dispatch race =
detected incoherence; renew vs expiry) · T-PARK-EXPIRY (alarm-only: row,
event, projection, banner; anti-rollover; new-lease-after-expiry) ·
T-PARK-CRASH/RESTART (pending-row adoption; foreign-resume at boot and
mid-run; expired-in-downtime alarm; simulator-mode lease survival) ·
T-PARK-REPLAY (table truth: renewed rows participate; closing set incl.
observed_foreign) · T-PARK-HEALTH (composition, precedence both ways,
transitions) · T-PARK-FOREIGN (word=1 no lease; word 2–6; takeover
requirement; foreign PQ during park + detector annotation + next-arm
classification) · T-PARK-SIMULATOR (apply validation; ACK-then-ignore both
fork sides via hook; wedge end-to-end; `_WRITE_BLOCKS`; PQ gate
byte-identical; architecture-fitness: generic path can never reach 0x8000) ·
T-PARK-MCP (read fields; NO mutation tool; contract text) · T-PARK-CONSOLE
(chip/countdown/banner/checklist/409-detail rendering; shots).
