# What to build next — delta analysis and recommendation

Prepared: 2026-08-26 (post-`f0b9288`; fourth edition — the 2026-08-23 next-3 is
fully delivered). Research and documentation only — no code, API, hardware, or
test interaction was performed to produce this document.

Sources: `docs/CONTINUITY.md` (state of record through the schedules live-test
entry: the operator published v1/v2 and a window RAN end to end), the delivered
designs `DESIGN_EXCESS_ACTIVATION.md` / `DESIGN_SCHEDULES.md` /
`DESIGN_EXCESS_CHARGING.md`, `docs/CONTROL_SURFACE_GAP_ANALYSIS.md` (R1–R10),
`docs/PROTOCOL_EVIDENCE.md` + `docs/evidence/field-mapping-2026-08-22.md`
(the `0x4101` counter evidence and A-1), `docs/DEFERRED_FINDINGS.md`, plus a
read-only verification pass (`register_layout.py`, `composition.py` read-plan
tiers, `observations.py`, `rest.py` route inventory, `simulator/pod.py`).

Operator framing this answers: the previous next-3 is delivered and, where it
could be, operator-tested live. Name the next item.

---

## 1. Delivered baseline (the 2026-08-23 next-3, closed)

| The 2026-08-23 recommendation | Status |
|---|---|
| **C1 — Excess-solar activation package** | **DELIVERED AND COMMISSIONED PRESENT-BUT-OFF.** Backend (projection, events, guarded toggle, net-billing durable acknowledgement, P6 composition) landed; console W1–W3 landed (the Home tile, the EXCESS toggle, event wiring, per-phase figures); config commissioned with `enabled: false` and the trial shape — the trial-cap/entry-threshold tension RESOLVED (`assumed_autonomous_charge_w: 300` puts entry at 400 W inside the 500 W cap). Everything now awaits two operator acts: the one-time NET_BILLED acknowledgement and the toggle. |
| **C2 — Schedules surface** | **DELIVERED, COMMISSIONED DAY-ONLY (YIELD), AND OPERATOR-TESTED END TO END.** B1–B6 + console W1–W3 landed; a live 422-on-publish was root-caused to a backend deviation from the design and fixed same-day; the operator's own retries published v1 then v2 and the window RAN (pre-arm refusals audit-visible, authorized per tick after arming, clean non-renewal hand-back). FINDING recorded: `schedule_state` cannot say "window open but units disarmed" — the operator discovered the arm requirement by trying (§2 S3). |
| **C3 — Verification-and-atomicity wave** | **DELIVERED.** Impl-10 commit-then-audit extended to every mutation (with compensating restores); simulator literal register-image golden program (MUTATION-2); honest per-field decode quality + scriptable quality (MUTATION-3/6); full suite green throughout. The wave's residue: the golden ENERGY/SOC scenario and half the validation matrices remain SKIPPED (DEFERRED_FINDINGS 3/4) — consumed by the item ranked #1 below. |

Where the standing R-list stands: R1 mode display DONE (device-mode words
decoded, exposed, dispatch-gating, plus the recovery awareness layer's
health_state surface); R2 explanations DONE in substance (per-unit attribution,
requested/authorized/actual as separate facts, plain-language limiting factors);
R3 one-tap presets PARTIALLY OVERTAKEN (per-battery watt dispatch + the
schedule editor cover the daily-use shape; what remains is polish); R4
starvation DONE (capacity-weighted + per-unit caps + concurrent per-unit
operation, live-proven 11/11); R5 schedules DONE; R6 per-phase display DONE
(the tile + Batteries rows); **R7 NOT STARTED** (neither the read view nor the
guarded changes — no policy read endpoint exists); **R8 NOT STARTED** (no
energy decode, no surface); R9 DONE (state surface + toggle live);
R10 partially done (recovery/quiet-evidence rendering landed; instance
identity, server-side audit filters, MCP unit detail outstanding).

The delta in one sentence: every control theme the operator has touched is
delivered; what remains untouched is exactly the family their prior dashboard
led with — the day's energy numbers — plus a short honesty queue and the
slow-burn policy-surface work.

## 2. Candidate list (scored against the operator's demonstrated priorities)

Observed priorities, in order of evidence: per-battery control (done), honesty
of display (done, repeatedly enforced), solar utilization (built, commissioned
present-but-off, awaiting the operator's toggle), scheduling (done,
operator-tested), **money/energy legibility** (demonstrated by their prior
dashboard leading with kWh figures and the vendor home chart noted approvingly
— nothing built for it), **dislike of invisible states** (the driver behind
most incidents; two small invisible states remain open). Effort/Value/Risk are
S/M/L.

| # | Candidate (source) | Effort | Value | Risk | Verdict |
|---|---|---|---|---|---|
| S1 | **DAILY ENERGY SCORECARD (R8)** — bought/sold/charged/discharged/load per day + charged-from-surplus attribution; A-1 buy/sell pinning protocol with our-own-integration fallback (`DESIGN_ENERGY_SCORECARD.md`) | M (decode + accountant + store + 1 route + 1 card/strip; the data already flows — `0x4101` is read at the cold ring and discarded) | **H** — the operator's untouched demonstrated value; makes the excess feature's worth visible in kWh (its graduation criterion 6 has NO evidence surface today); the honest-money surface | L — advisory read path only, no authority, no cadence change | **Build first.** Designed in full; dispatchable. |
| S2 | **Night-writer between-cycles foreign-objective detector** (census queue; CONTINUITY "QUEUED") — fold the already-read served-objective words into telemetry, alert `foreign_objective_observed` when disarmed/idle, characterize the 7.3 h night blind spot at last | S (decode + one classifier + event; zero extra frames — `0x1060` is already in the plan) | **M–H** — closes the last standing invisible STATE; its night evidence feeds the pending night-partition operator decision | L | **Build second — recommended as S1's companion dispatch** (same decode/composition files; separate contract cycle). |
| S3 | **`schedule_state` disarmed-window reason** (the live FINDING from the operator's own publish test) — the vocabulary cannot say "window open but the units are disarmed"; the operator had to discover arming by trying | S (one projection vocabulary addition + console sentence) | M — an invisible state the operator HIT LIVE during acceptance | L | Ride-along for the next contract cycle; honest fix the operator already implicitly requested. |
| S4 | **Adjustable safety bounds (R7)** — (i) read-only Settings>Safety view of the active policy, then (ii) guarded SOC floor/ceiling + cap changes | (i) S / (ii) M | M — real when the operator wants "keep mid above 20% for the fridge"; they have not asked in any recorded session; they hit the ceiling refusal once and it was explained | (i) L / (ii) M — (ii) is a new policy-mutation surface, the heaviest authority class after the excess toggle | **Build third, staged** — (i) after S1/S2; (ii) only after the operator asks. |
| S5 | **Arm-refusal de-conflation** (fix-queue vii; live-observed) — arm-while-armed returns `actor_failure`; also the console niceties residue (correlation ids on audit.appended) | S | S — honesty polish on a path the operator exercises every session | L | Fold into any facade contract cycle. |
| S6 | **One-tap presets remainder (R3)** | S | S — overtaken by per-battery dispatch + schedules; the remaining shape is "remembered preset" polish | L | Queue; ride a web-agent slot. |
| S7 | **Test debt**: golden energy/SOC scenario + simulator/facade validation matrices (DEFERRED_FINDINGS 3/4) | S–M | M as verification; S as product | L | **The golden half is CONSUMED BY S1** (its §9 family 8); the matrices ride S1's simulator edit. |
| S8 | **Console polish (R10)** — health instance identity (`process_instance_id`/`uptime_s` rendering), reconnect banner, server-side audit filters, MCP `get_unit_detail` | S | S–M | L | Opportunistic web slots. |
| S9 | **Docker image build** (Milestone C residual) | S | M — packaging completeness | L | Environment-gated (Docker absent on this host); opportunistic. |

Not candidates now: the excess-solar trial and graduation (engineering done;
the remaining acts are the operator's — the NET_BILLED acknowledgement, the
toggle, then the §4 trial review against graduation criteria, one of which S1
supplies the evidence for); the mid/lhs ±1.2 kW oscillation (standing
live-diagnosis item needing the operator's observation window — the awareness
layer already timestamps it as quiet evidence); the overnight observe run,
day/night partition grant, and VLAN (operator agreements/infrastructure, not
build items — S2 makes the first one informative); any `0x8000` write or other
Part-4 exclusion (standing).

## 3. The ranked next: the DAILY ENERGY SCORECARD (S1)

**Why it wins.** Every other theme the operator has demonstrated is delivered;
the one they have visibly enjoyed elsewhere — the day's kWh/money numbers —
has no surface in our product. The vendor app's home chart and their prior
dashboard led with exactly these figures (CONTROL_SURFACE_GAP_ANALYSIS R8:
"'sell to grid' is why the excess-solar feature exists"). It is also the
missing evidence surface for the arc we just finished: the excess feature sits
commissioned-but-off awaiting the operator's conviction, and its graduation
criterion 6 is literally "the operator reviews and accepts the economics (kWh
shifted vs the autonomy baseline)" — a number no console shows today. And it
is the cheapest high-value item left: the counters are already read every
cold-ring rotation and discarded; the per-phase CT stream is already
control-rate and live-proven; the persistence, event, snapshot, and
feature-detection patterns are all established. Risk is structurally low —
advisory reads only, no authority anywhere in the design.

**The evidence gate, honestly.** The counter DECODE (order, word order, ×0.1
kWh) and the charge/discharge ROLE labels are confirmed; the grid buy/sell
ROLE labels are not (field-mapping A-1). The design does not wait on A-1 and
does not gamble on it: bought/sold come from OUR OWN integration of the
per-pod CT `grid_power_w` (we own the samples at the 1.5 s cadence; sign
contract live-proven; gaps excluded, never interpolated, with a coverage
fraction and partial-day markers — accuracy assessed honestly in the design
§4), while BOTH grid counter pairs are decoded and recorded from day one as
the passive pinning evidence. The active pinning protocol is one half-hour
read-only observation of a known-import evening. Pinning NEVER self-applies —
promotion to the device counters (strictly better coverage: they count
through our downtime) is an operator config revision gated on the recorded
fact. Site PV is not wired to the pod inputs: the scorecard never presents
solar production as measured — its solar story is the surplus the site
exported and the surplus the batteries captured, both measured.

**Headline shapes** (full contracts in `docs/DESIGN_ENERGY_SCORECARD.md`;
wire-facing pins already landed in API_CONTRACTS "Energy scorecard"): six
advisory `energy_*_kwh` observation fields (grid pair under NEUTRAL A/B
names until pinned); an `EnergyAccountant` in the fleet loop integrating the
CT stream, deltaing the counters, rolling days at site-timezone midnight
(one B4-style promoted energy read at the boundary); a frozen
`EnergyDayRecord` with per-unit metrics, coverage, provenance, and the
counter cross-check; `GET /api/v1/energy/days` + snapshot `energy_today` +
`energy.day_rolled` (transition-only); an `energy_scorecard` config block
(block-presence doctrine, `grid_source`/`grid_counter_roles` A-1 gates,
optional tariff keys for money); Home "Today" card + the Insights view's
first real content. The build also consumes the deferred golden energy/SOC
scenario as its reference-model test family.

**Companion (S2)**: dispatch the night-writer foreign-objective detector as a
separate contract cycle in the same window — it touches the same decode and
composition files, costs zero extra frames, and closes the night blind spot
whose characterization the operator's pending night-partition decision needs.
S3 (the disarmed-window reason) is a natural third rider on the same files.

## 4. Operator decisions required (standing + new)

1. **NEW — Commission the scorecard** (S1): add the `energy_scorecard` block
   and restart; choose the A-1 pinning path (passive ~3-day cross-check, or
   name one known-import half-hour evening); decide source promotion after
   pinning (recommend the device counters); supply tariff keys or stay
   kWh-only. All verbatim-ready in DESIGN_ENERGY_SCORECARD §11.
2. **STANDING — Excess trial start** (everything is built): the one-time
   NET_BILLED acknowledgement, then the console toggle for the 500 W trial —
   plus the operator's arming of the neediest unit. Graduation per
   DESIGN_EXCESS_ACTIVATION §4 (criterion 6's economics review is what S1
   surfaces).
3. **STANDING — Night partition** (DESIGN_SCHEDULES §8): the YIELD day-only
   posture is commissioned; any night schedule needs the explicit stand-down
   grant (config widening + the one-time acknowledgement). S2's night
   evidence should inform this decision, not follow it.
4. **STANDING, non-blocking**: whether the EE-calibration warning bits should
   ever block (documented, disabled, awaiting a ruling); an observation
   window for the mid/lhs oscillation; the S4(ii) adjustable-bounds flow —
   build on request.
