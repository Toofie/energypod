# What to build next — delta analysis and recommendation

Prepared: 2026-08-23 (post-`b6b5bcf`). Research and documentation only — no code,
API, hardware, or test interaction was performed to produce this document.

Sources: `docs/PRODUCT_ROADMAP.md`, `docs/CONTINUITY.md` (state of record through
the 2026-08-24 backend-polish entry), `docs/CONTROL_SURFACE_GAP_ANALYSIS.md`
(R1–R10), `docs/DESIGN_EXCESS_CHARGING.md`, `config/config.live-write-example.yaml`
(`excess_charging` block), `docs/DEFERRED_FINDINGS.md`,
`docs/SYNC_RESILIENCE_AUDIT.md`, `docs/POD_RECOVERY_RESEARCH.md`, plus a code
verification pass (`src/energypod/application/excess_charge.py`,
`scheduling.py`, `arbiter.py`, `service.py`, `src/energypod/api/rest.py`,
`web/src`).

Operator framing this answers: the core is baseline; review the docs; name the
next item. Specific flag: excess-solar charging exists with **no UI to enable
it** — "will we simply rely on scheduling?"

---

## 1. Delivered baseline vs remaining (roadmap intent → status)

| Roadmap intent | Status |
|---|---|
| **Phase 1 — Observe-only validated telemetry** | **DELIVERED and exceeded.** Live telemetry from all three units since 2026-08-22; identity-pinned units, tiered read plan, honest quality/staleness, full console, packaging/ops (Milestone C), 1473-test clean full suite. Docker image build still environment-blocked (no Docker on this host). |
| **Phase 2 — Manually armed, guarded dispatch** | **DELIVERED and far exceeded.** Live write-enabled control commissioned 2026-08-22 and hardened through the 2026-08-23/24 waves: per-unit watt targets, concurrent per-battery operation (live-verified mid charge + rhs discharge simultaneously, 11/11 combination matrix), per-intent cancel, mode-word gating with fresh re-read, beat-autonomy arm classification, BMS-authoritative SOC, publish-fence and allocator fixes, console at parity (one card per request, live cancel, per-battery truth everywhere). |
| **Phase 3 — Deterministic tariffs and schedules** | **~40% delivered — domain only.** `ScheduleEntry`/`SchedulePlan` validation (cross-midnight, overlap, timezone, DST), `ScheduleEvaluator` emitting short-TTL `SCHEDULE` intents, and the `ScheduleRepository.get()/replace(version, entries)` port all exist. **No REST endpoint, no facade exposure, no evaluation loop in composition, no UI** (nav placeholder "Schedule — not available yet"). The night-writer coordination constraint (below) is unaddressed. |
| **Phase 4 — Weather/PV/Amber providers** | **NOT STARTED** (by design — never scheduled). One adjacent piece exists: per-unit grid/load CT words (`grid_power_w`/`load_power_w`) decoded and projected through the facade as advisory telemetry (the excess-solar work), though the console does not render them yet. |
| **Phase 5 — Forecasting, optimization, MCP** | **PARTIALLY PREEMPTED by the excess-solar adviser.** A bounded, advisory-only optimizer actor (`ExcessChargeAdviser`) is **fully implemented, simulator-proven, and dormant** — config-gated OFF (`excess_charging` absent block), with the deterministic export bound in the allocator, kernel export-evidence defense, beat-autonomy hysteresis, and per-unit yield under concurrent arbitration. MCP is read-only by default with an optional capped `dispatch_intent`. No forecasting, no plan optimizer, no MCP control surface. |
| **Beyond roadmap (operator-driven)** | Desync-resilience wave (B1–B6 + S1) implemented and live-verified; pod remote-recovery researched and staged (detection buildable, any `0x8000` write commissioning-gated); S1 EE-calibration warning bits documented, enabled by operator decision only. |

The delta in one sentence: the project skipped past Phase 3 and delivered the
safety-relevant heart of Phase 5 ahead of it, because the operator's live
priorities (direct per-battery control, honest display, autonomy preservation)
pulled it there. The consequence is a gap shaped exactly like the operator's
flag: a finished, dormant automation feature with no activation surface, and a
finished scheduling domain with no product surface.

## 2. Candidate list (scored against the operator's demonstrated priorities)

Operator priorities observed this week, in order of evidence: direct per-battery
control (done), honesty of display (done), **autonomy preservation** (doctrine
settled, live-proven), **solar utilization** (built, dormant), **minimal
physical intervention** (researched, staged). Effort/Value/Risk are S/M/L.

| # | Candidate (source) | Effort | Value | Risk | Score and note |
|---|---|---|---|---|---|
| C1 | **Excess-solar activation package** — net-billing confirmation flow, console switch + live state display, grid/export display (R6 UI half), 500 W first-trial protocol (R9 + design §Phase 1; CONTINUITY "REMAINING NEXT") | S–M (state projection + one guarded toggle + UI; all engineering done) | **H** — the operator's #1 dormant value; solar utilization is one of two remaining themes | L — advisory-only, every gate built; one operator decision gates it | **Build first.** Highest value-per-effort on the board. |
| C2 | **Schedules surface** (R5; Phase 3 domain exists) | M (2 REST endpoints + facade + evaluation loop + one view + night-writer design) | **H** — the operator's historical use case (Night Charge 00:01–05:59 @2500 W); answers their literal question | M — night-writer conflict must be designed around, not coded around | **Build second.** The domain is done; the risk is coordination, not code. |
| C3 | **Promoted-P1 hygiene wave** — Impl-10 commit-then-raise facade atomicity, simulator literal register image (~90 mutation survivors), simulator blanket-GOOD quality masking (CONTINUITY 2026-08-23 P2 pass) | M | M — protects the verification oracle exactly as more automation (C1 trial rehearsal, C2 evaluator) rides the simulator; Impl-10 is load-bearing for C1's toggle endpoint | L | **Build third / fold partially into C1.** The toggle and schedule-PUT are new facade mutations; land them on the atomic pattern (submit_intent) and extend Impl-10 to the remaining ops in the same pass. |
| C4 | **Pod-recovery detection layer (R4)** — actuation-coherence watchdog, objective echo readback, bus-answer classifier, console recovery states (POD_RECOVERY_RESEARCH §3 R4; queue items iii/vi) | M | M — serves "minimal physical intervention"; buildable now, no authorization | L | Runner-up. No Class-2 wedge observed in the live record; value is contingent on recurrence. Natural follow-on after C1/C2. |
| C5 | **One-tap dispatch presets + visible holds** (R3) | S | M — daily-use polish the operator would feel immediately | L | Runner-up; UI-only; can ride any web-agent slot. |
| C6 | **Small backend niceties** — `intent.accepted` carries `expires_in_s`; `audit.appended` carries correlation id; distinct arm-refusal reason for already-armed (CONTINUITY console-complete + matrix) | S | S — polish | L | Fold into C1's contract cycle (same files, same bus payloads). |
| C7 | **Safety-bounds read view (R7 i)** | S | M — explains `soc_above_charge_ceiling` refusals the operator has hit live | L | Ride-along for any contract cycle. |
| C8 | **Energy totals view (R8, 0x4101)** | S–M | M — money legibility; sells the excess feature's benefit | M — energy role labels need evidence pinning first | After C1 (it is how the adviser's value becomes visible in dollars). |
| C9 | **Foreign-objective detector, overnight observe run, day/night partition, VLAN** (census queue) | S (detector) / operator (rest) | M — closes the night blind spot | L | The detector rides C2's design; the rest are operator agreements. |
| C10 | **Docker image build** (Milestone C residual) | S | M — packaging completeness | L | Environment-gated (Docker absent here); opportunistic. |

Not candidates: MCP `get_unit_detail` (trivial, ride-along), the mid ±1.2 kW
oscillation (a live-diagnosis item needing the operator's window, not a build
item — fold its mode-word snapshot into C4's classifier capture), any `0x8000`
write (commissioning-grade, separately authorized, per POD_RECOVERY_RESEARCH).

## 3. The ranked next three

### Next 1 — Excess-solar activation package

The engineering is finished and proven: the adviser composes when enabled,
ticks inside the fleet loop, submits short-TTL `OPTIMIZER` intents under
`energypod:excess-adviser`, yields per-unit under concurrent arbitration, and
hands back by non-renewal. What is missing is everything the operator touches.
Two things genuinely gate it, both cheap: one operator decision (net billing)
and one visible surface. Scope:

- **Contract** (`docs/API_CONTRACTS.md` advisory section + a small toggle
  contract): an `adviser_state` projection — enabled, last decision
  (`idle`/`propose`/`renew`/`withdraw`), reason codes (`no_export_headroom`,
  `no_acceleration_over_autonomy`, `below_exit_hysteresis`,
  `no_eligible_target`, `yielding_to_higher_priority`), target unit, current
  export bound, proposed watts, held intent id — on snapshot/health; one
  guarded mutation `POST /api/v1/settings/excess-charging` (arm + interactive
  principal, typed confirmation, idempotency key, audited, mirroring
  inhibit-acknowledgement's shape) flipping the composed adviser at runtime;
  boot composes from the config file value (a runtime toggle does not silently
  persist — decide persistence explicitly). Build it on the atomic
  commit-then-audit pattern (the Impl-10 discipline applied to this one
  operation). Fold C6 (expires_in_s, correlation ids) into the same contract.
- **Backend**: facade projection of the adviser's state (the tick decision is
  computed every cycle and currently discarded beyond its intent); the toggle
  mutation; grid/load figures are already in the snapshot telemetry summary —
  verify the wire model and expose a fleet net-export figure.
- **UI**: Home tile ("Solar surplus charging mid from 1.4 kW export — adviser
  active" / honest inactive states with the reason), a Settings on/off switch
  with confirmation, and the per-phase grid/load figures on Home/Batteries
  (R6's UI half — this is what makes the feature legible: "that export is what
  charged rhs"). Activity attribution already works (principal + source).
- **Protocol**: the operator's net-billing confirmation, then the design's
  Phase 1 trial — 500 W cap (`max_charge_from_export_w: 500`), one neediest
  unit, daytime surplus window, other writer apps quiescent, abort criteria as
  written in `DESIGN_EXCESS_CHARGING.md`.

### Next 2 — Schedules surface (Phase 3 made real)

This answers "will we simply rely on scheduling?" concretely (see §4: no) by
making scheduling a first-class product feature on the existing domain. Scope:

- **Contract**: `GET /api/v1/schedule` (versioned plan + next evaluated action)
  and `PUT /api/v1/schedule` (compare-and-swap on version —
  `ScheduleVersionConflict` already models it); audit events for publication;
  bus events for schedule transitions; the evaluation loop's position in the
  fleet cycle.
- **Backend**: facade methods over `ScheduleRepository`; an evaluation loop in
  `_run_fleet` (evaluate the active plan each cycle, maintain exactly one live
  `SCHEDULE` intent per plan version through the ordinary intent repository —
  the evaluator and TTL discipline already exist); SQLite persistence of the
  plan (repository classes already delivered).
- **UI**: the Schedule view (v1: a readable list editor with validation
  feedback, not a full timeline painter), next-action on Home (the slot exists
  and says "nothing is scheduled"), pause/skip, publication audit in Activity.
- **The night-writer constraint (must be designed, not coded around)**: the
  operator's environment fact is pinned in CONTINUITY — other applications
  write these batteries **only at night**, exactly when a Night Charge schedule
  would run, and our arm-time sole-writer preflight latches `external_writer`
  by design ("coordinate, don't fight"). Three honest postures, requiring an
  explicit operator choice: (a) **partition** — the controller owns the night
  window and the external night writers are stood down (the clean path; make
  the schedule's first-night activation an operator-acknowledged takeover);
  (b) **yield** — the schedule is a daytime/off-peak feature and the night
  window stays with the existing writers (schedule windows validated against a
  configured allowed window); (c) **contested** — the schedule runs, arms fail
  against the preflight, and the console says so honestly every night (worst;
  rejected). Recommend (a) with the partition recorded in config, plus the
  between-cycles foreign-objective detector (C9) so any residual writer is
  surfaced rather than fought.
- **Excess × schedule interaction** (design now, cheap later): source priority
  is already `emergency_stop > manual > agent > optimizer > schedule`, and the
  adviser yields only to sources **above** optimizer — so an active schedule
  does **not** displace the adviser today. Physics mostly arbitrates: export
  exists only in daylight, night-charge windows exist only in darkness, and at
  dusk the adviser's own bound collapses to zero as export collapses. Where
  both could claim one unit (an operator's unusual daytime schedule), decide
  the default: recommend a per-unit adviser yield-to-schedule (one extra check
  in the existing withdraw path, config-gated `yield_to_schedule: true`) so a
  deliberately published plan always outranks opportunism; without it, the
  adviser wins and the schedule is starved invisibly — exactly the failure
  class the operator has already had to explain once.

### Next 3 — Verification-and-atomicity hygiene wave (the promoted P1s)

Not glamorous, but it is the slot that keeps C1 and C2 honest as they land:
the simulator is now the reference model for both features, and both add facade
mutations. Scope: Impl-10 (extend `submit_intent`'s atomic commit-then-audit
pattern to arm/disarm/acknowledge — and to C1's toggle and C2's schedule-PUT
as they are built); the simulator literal register-image golden program
(~90 mutation survivors — "a misplaced word would pass silently"); simulator
blanket-GOOD quality masking (the 62,535 W headroom artifact). If the operator
prefers resilience over quality debt, C4 (the recovery detection layer) is the
alternate for this slot — it is buildable now with no authorization and serves
the minimal-physical-intervention theme; nothing in the docs outranks C1 and
C2 either way.

## 4. Should excess-solar rely on scheduling?

No — and it deliberately does not. The adviser is **reactive by design**: it
acts whenever fleet export exists and nobody of higher priority is commanding,
it re-decides every tick (default 10 s TTL), and it hands back by non-renewal
so the pod's own CT-following autonomy resumes within the ~3.5–4 s watchdog
window. Scheduling is the wrong mechanism for a solar-surplus follower because
sunlight is not a timetable: a fixed window would either miss surplus (charge
at 2 pm while exporting, stop at 3 while still exporting) or chase it badly.
Scheduling instead **composes** with it: a schedule is a separate,
priority-lower, window-scoped intent source (`schedule` ranks below
`optimizer` in the arbiter), best expressed as the operator's night-charge
windows — disjoint from the adviser's daylight export by physics, with the
adviser optionally yielding per-unit to a published schedule where they could
overlap. Use the schedule for what is predictable (tariff windows, overnight
top-ups) and the adviser for what is reactive (spare solar); neither needs the
other to be safe, and the safety kernel governs both identically.

## 5. Operator decisions required

1. **Net-billing confirmation** (gates C1 production enablement): is site
   billing netted across phases? Per-phase billing changes the economics,
   never the safety. Pending since the 2026-08-23 design.
2. **First-trial authorization** (C1): authorize the single-unit, 500 W-capped,
   daytime-surplus trial window with other writer apps quiescent, per the
   design's Phase 1 protocol and abort criteria.
3. **Toggle policy** (C1): who may flip it (recommend arm + interactive
   principal) and whether a runtime toggle persists across restart (recommend:
   boot composes from the config file; persistence is an explicit, audited
   write-back if wanted).
4. **Night-window partition** (C2, deciding posture): stand down the external
   night writers and let the controller own the night, or constrain schedules
   to the day window, before any Night Charge schedule is published.
5. **Adviser-vs-schedule precedence default** (C2): confirm the recommended
   `yield_to_schedule: true` (a published plan outranks opportunism per unit).
6. **Standing, non-blocking**: whether the EE-calibration warning bits
   (`PCS_Warning0_1`/`DCDC_Warning0_1`, standing-active on all three pods)
   should ever be blocking — documented, disabled, awaiting a ruling; and a
   window for the mid ±1.2 kW oscillation observation if C4 is chosen.
