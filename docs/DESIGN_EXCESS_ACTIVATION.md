# Excess-solar activation package — the operator-facing contract

Status: DESIGN ACCEPTED PENDING IMPLEMENTATION (2026-08-25). Owner of the wire
contract: `docs/API_CONTRACTS.md` "Excess-solar accelerated charging (advisory)"
(extended by this package: `adviser_state` projection, `excess_adviser.state_changed`
event, the guarded activation toggle). Built feature: `docs/DESIGN_EXCESS_CHARGING.md`.
Accepted scope: `docs/PRODUCT_NEXT.md` §3 "Next 1". Console surface plan: §5 here.

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or `web/`
file. The backend agent follows §6's ordered plan; the web agent follows §5. No
live-hardware interaction is authorized by this document — the trial in §4
requires the operator decisions in §7, verbatim as written there.

## 0. What exists, what this adds

The adviser is fully implemented and dormant: config-gated OFF, composed only
when `excess_charging.enabled` is true, ticking inside the fleet loop, submitting
short-TTL `OPTIMIZER` intents under `energypod:excess-adviser`, yielding per unit,
handing back by non-renewal. Three things are missing, and they are exactly the
operator-facing three:

1. **Observability** — the tick decision is computed every cycle and discarded
   beyond its intent. The console cannot say whether the feature is on, why it is
   idle, what it would command, or what the fleet is exporting.
2. **A surface** — there is no way to enable or disable the feature except a
   config edit and a restart. The operator's flag ("no UI to enable it") is this.
3. **The protocol's front door** — the net-billing confirmation is a pending
   doc note; the first trial is a paragraph. This package makes the confirmation
   a captured, durable operator fact and the trial a commissioned, capped, and
   evidenced window.

Nothing in this package changes the adviser's authority, the export bound, the
hysteresis, the yield rules, or any safety path. The adviser still yields to
every higher-priority source; the bound stays one min() term; the kernel defense
is untouched.

## 1. The `adviser_state` projection

**Where it lives:** the facade snapshot (`GET /api/v1/snapshot` and the event
stream's first frame), TOP LEVEL beside `intent` — the same feature-detected
addition pattern the `intent` block used. The key is **absent** when the
`excess_charging` config block is absent (nothing composed: no adviser, no
projection, no tile, no toggle). The key is **present** whenever the block is
present and its commissioning gates validated (§3 P6), including while suspended.
`health` is unchanged — this is snapshot state, not a readiness fact.

**Exact JSON shape** (values illustrative; field set and types are the contract):

```json
"adviser_state": {
  "enabled": true,
  "enabled_origin": "runtime",
  "acknowledged_economics": true,
  "active": true,
  "hysteresis_state": "holding",
  "target_unit_id": "mid",
  "commanded_charge_w": 1400,
  "eligible_export_charge_w": 1600,
  "fleet_export_w": 1800,
  "export_evidence": "good",
  "charge_cap_w": 2500,
  "held_intent_id": "opt-3f9c21",
  "last_action": "renew",
  "last_tick_at": "2026-08-25T11:04:31+10:00",
  "reason_codes": ["export_headroom_available"]
}
```

| Field | Type | Meaning |
|---|---|---|
| `enabled` | bool | the adviser is participating this process (config at boot, or the runtime toggle since) |
| `enabled_origin` | `"config" \| "runtime"` | whether the current participation state is the boot-composed config value (`config`) or was last changed by the toggle (`runtime`) — the honest "until restart" marker |
| `acknowledged_economics` | bool | the site has ever captured the net-billing acknowledgement (§3 P3); durable across restarts |
| `active` | bool | the adviser holds a live intent right now (`held_intent_id != null`; equivalently `hysteresis_state == "holding"`) |
| `hysteresis_state` | `"inactive" \| "entering" \| "holding" \| "exiting"` | see below |
| `target_unit_id` | str \| null | this tick's neediest eligible unit; null when no target qualified |
| `commanded_charge_w` | int | the last tick's `proposed_watts` (the achievable min: bound, target headroom, static cap); 0 when not commanding |
| `eligible_export_charge_w` | int | the deterministic export bound from the last tick |
| `fleet_export_w` | int \| null | Σ `grid_power_w` over every fleet unit, positive = export; **null when any unit's grid evidence is missing/bad/stale** — never zero-filled (one unreadable phase is never treated as zero export) |
| `export_evidence` | `"good" \| "missing" \| "bad" \| "stale"` | the fleet rollup under the bound's own fail-closed rules (worst per-unit grid word wins; the kernel spellings) |
| `charge_cap_w` | int | the composed `max_charge_from_export_w` (the trial shows 500; the console may always render the cap) |
| `held_intent_id` | str \| null | the live adviser intent id, null when holding nothing |
| `last_action` | `"idle" \| "propose" \| "renew" \| "withdraw"` | the domain action vocabulary, verbatim |
| `last_tick_at` | iso-8601 str | wall time of the last completed tick |
| `reason_codes` | [str] | the pinned vocabulary below; empty never — an enabled adviser always says why it did what it did |

**Hysteresis states.** `inactive`: not participating (disabled by config, disabled
at runtime, or suspended on the pending acknowledgement). `entering`: enabled and
evaluating entry — includes the post-yield re-qualification wait (the claim has
expired and the entry threshold has not re-cleared). `holding`: intervening; a
live adviser intent exists. `exiting`: the most recent tick withdrew; persists
until the next tick re-evaluates (the projection is tick-granular and says so
through `last_tick_at`).

**The reason vocabulary — ONE vocabulary, pinned.** The implemented decision
codes are kept VERBATIM (they are already plain, tested, and live in the domain);
the projection adds only the states the tick alone cannot see. Two vocabularies
for the same facts is exactly the drift class the EE-calibration incident taught
(codes that can never match are silently inert).

| Code | Source | Meaning |
|---|---|---|
| `disabled_by_config` | projection | the block is present, `enabled: false`, untouched this process |
| `disabled_by_runtime` | projection | the operator disabled at runtime; operational only until restart |
| `economics_acknowledgement_required` | projection | composed but suspended: the site has never captured the net-billing fact |
| `export_evidence_missing` / `export_evidence_bad` / `export_evidence_stale` | projection (kernel spellings) | some fleet unit's grid word is absent / non-GOOD / past `export_telemetry_max_age_s`; the bound is 0 and `fleet_export_w` is null |
| `no_export_headroom` | tick (verbatim) | evidence good but export at or below the headroom margin |
| `no_acceleration_over_autonomy` | tick (verbatim) | achievable < autonomy + `min_acceleration_w`; taking over would slow charging |
| `below_exit_hysteresis` | tick (verbatim) | was intervening; achievable fell to the exit threshold — handing back |
| `no_eligible_target` | tick (verbatim) | no unit is controllable, under the SOC ceiling, and headroom-positive |
| `yielding_to_higher_priority` | tick (verbatim) | a live emergency stop, or a manual/agent intent claims the target |
| `export_headroom_available` | tick (verbatim) | commanding (propose/renew) |

The console's plain sentences map from these codes (§5); the codes are the wire.

**Single writer.** The projection object is owned by the fleet loop (one writer:
the post-tick update; the toggle flips only the participation flag and the next
tick observes it). `active` derives from `held_intent_id`, never from a
lifecycle guess, so the projection can never claim inactive while an adviser
intent is still live (the withdraw-then-tick race).

## 2. Events

One new bus event, same vocabulary as the projection:

```json
{
  "type": "excess_adviser.state_changed",
  "payload": {
    "enabled": true,
    "enabled_origin": "runtime",
    "acknowledged_economics": true,
    "active": true,
    "hysteresis_state": "holding",
    "target_unit_id": "mid",
    "commanded_charge_w": 1400,
    "eligible_export_charge_w": 1600,
    "fleet_export_w": 1800,
    "export_evidence": "good",
    "reason_codes": ["export_headroom_available"],
    "held_intent_id": "opt-3f9c21",
    "heartbeat": false
  }
}
```

**Throttling (pinned).** Publish ONLY when the semantic state tuple changes —
`(enabled, enabled_origin, acknowledged_economics, active, hysteresis_state,
target_unit_id, export_evidence, reason_codes)`. Watt figures
(`commanded_charge_w`, `eligible_export_charge_w`, `fleet_export_w`) are carried
on every publication but are NOT triggers: while holding, the commanded watts
re-price with export every tick (~1.5 s), and publishing that would put one
event per cycle on the bus for figure wander the console already gets from its
2.5 s snapshot cadence and the `control_decision`/`audit.appended` frames.

**Heartbeat (recommended, pinned as the default):** while `enabled` is true,
republish the full payload every 30 s with `"heartbeat": true` (a repeat
publication that does NOT change the state tuple). Bounded (≤ 2/min), it lets a
quiet-but-healthy adviser prove liveness on the stream and refreshes the tile's
watt figures for any consumer that missed the snapshot cadence. While disabled,
no heartbeat — the `state_changed` to disabled is the last event. The cadence is
a constant, not a config key.

The toggle itself needs no dedicated bus event: it flips `enabled`, the next
tick's projection change publishes `state_changed`, and the REST 200 carries the
same projection for optimistic adoption (§3).

## 3. The guarded toggle

`POST /api/v1/excess-charging` — the inhibit-acknowledgement guarded-confirmation
pattern, applied to a feature gate:

```
POST /api/v1/excess-charging
Authorization: Bearer <token>          (arm scope; enable additionally requires
                                        an interactive principal — see P4)
Idempotency-Key: <opaque>

{"action": "enable" | "disable",
 "confirmation": "EXCESS",             // typed confirmation, always required
 "economics": "NET_BILLED"}            // OPTIONAL; consulted only on the first
                                        // enable ever (see P3)
```

**200 response** (either action):

```json
{
  "feature": "excess_charging",
  "enabled": true,
  "enabled_origin": "runtime",
  "persisted": false,
  "acknowledged_economics": true,
  "adviser_state": { ... the §1 projection, post-toggle ... }
}
```

`persisted` is always `false` and is spelled anyway: the contract states the
non-persistence policy on every response.

**Error envelopes** (`{code, message, details, request_id}`):

| Status | Code | When | `details` |
|---|---|---|---|
| 422 | `validation_error` | unknown action; missing/incorrect `confirmation`; `economics` present but not exactly `"NET_BILLED"` | field errors |
| 409 | `excess_charging_not_commissioned` | no `excess_charging` block (or composition refused it): nothing to toggle | — |
| 409 | `economics_acknowledgement_required` | `enable`, no prior captured acknowledgement on this site, no `economics` field | `{"acknowledgement": "NET_BILLED"}` (what to send) |
| 409 | `excess_enable_refused` | `enable` while any unit is ACTIVE under another intent, or while any latched stop holds | `{"reasons": ["unit_active_under_intent" \| "latched_stop_holds"], "unit_ids": [...], "stop_ids": [...]}` |

Every call is audited (result `enabled` / `disabled` / `noop`) and idempotent by
key, exactly like the arm/inhibit-acknowledge mutations. The mutation adopts the
Impl-10 atomic commit-then-audit pattern (the facade discipline the delta
analysis pinned for every new mutation).

### The pinned policy decisions, with rationale

**P1 — Runtime state does NOT persist across restart. Boot composes from config;
the config stays the source of truth.** A restart while enabled-in-config
re-enables; a runtime disable is operational only until reboot. Rationale: (a)
the config file is the commissioned artifact — every commissioning gate
(`write_enabled` mode, policy presence, cap ≤ `max_unit_charge_w`, freshness,
hysteresis, TTL bounds) was validated against it, and a persisted runtime enable
would silently outlive a config revision that disables the feature (a stale
runtime fact resurrecting a decommissioned feature after reboot); (b) no new
durable settings store, migration, or write-back path is needed — the audit
trail is the record, not the state; (c) the failure mode is fail-safe: a reboot
degrades to the commissioned default, never beyond it; (d) the operator is never
surprised: `enabled_origin: "runtime"` plus the console's "until restart" note
(§5) make the transience visible at a glance. A permanent change remains an
explicit, audited config edit — the same path graduation to 2500 W takes (§4).

**P2 — `enable` is REFUSED while any unit is ACTIVE under another intent, or
while a latched stop holds.** Rationale: enabling under an active manual/agent
request is legal (the adviser would simply yield per unit) but opaque — the
operator would see "on" doing nothing, the exact invisibility failure this
package exists to remove. Refusing names the conflict and forces a deliberate
finish-or-cancel first; attribution stays clean. A latched stop fences the whole
fleet — enabling under it is dead state that would misread as "on". Latched
inhibits (e.g. `external_writer`) do NOT refuse: the selector skips those units
honestly (`no_eligible_target`), and the projection says so. `disable` is never
refused — stopping is the safety-positive direction.

**P3 — The net-billing confirmation is captured ONCE, as an explicit acknowledged
fact, and is required before the FIRST enable ever succeeds on a site.** The
audit event is `excess_charging_economics_acknowledged` (subject, site, wall
time, the assertion text). It is durable (the SQLite audit store in run mode),
never expires, never re-prompts, and is loaded at boot into the gate. The first
enable either carries `"economics": "NET_BILLED"` (captured and applied in the
same audited mutation) or is refused with `economics_acknowledgement_required`.
Rationale: the net-billing assumption is the one pending operator decision that
gates the ECONOMICS of production enablement (per-phase billing changes the
arbitrage, never the safety) — the contract's honest representation is to make
the operator's decision part of the durable record instead of a doc note. It
gates enablement, not composition: an unacknowledged site composes suspended
with reason `economics_acknowledgement_required` (fail-closed, visible), so even
a config `enabled: true` cannot silently participate without the captured fact.
Ordering is durable-audit-append FIRST, latch flip second; an audit failure
refuses the enable (no enable without the durable fact).

**P4 — Scopes: `arm` + interactive principal to enable; `arm` alone to disable.**
Exactly the arm/disarm and inhibit-acknowledge precedents: enabling grants
participation (a control-adjacent act requiring a human), disabling is
safety-positive and may be driven by any arm-scoped principal.

**P5 — The toggle can NEVER bypass the commissioning gates.** It flips
participation only. It cannot change `max_charge_from_export_w` or any cap, the
export triple, the read plan, the mode, the hysteresis keys, or the TTL — all of
those are composition facts validated at config load. The bound remains a min()
term that can only lower power; the kernel defense, the arbiter priority, and
the per-unit yield rules are untouched. An enabled-at-runtime adviser commands
under exactly the commissioned envelope, and it still yields to every
higher-priority source, unchanged.

**P6 — Composition semantics (the enabling amendment): a PRESENT block composes
the machinery; `enabled` gates participation.** Block present + gates validated
⇒ export triple armed, PCS block promoted to the core read plan, adviser
constructed, `adviser_state` present. `enabled: true` ⇒ participating at boot
(subject to P3's latch); `enabled: false` (explicit) ⇒ composed but suspended at
boot, enableable at runtime — this is what makes the console's first enable
possible without a config edit. An ABSENT block composes nothing, exactly as
today: no adviser, no triple, no tier promotion, `adviser_state` absent, and the
toggle answers `excess_charging_not_commissioned`. The validation gates
strengthen correspondingly: they apply whenever the block is PRESENT (not only
when `enabled: true`) — an explicit disabled block that could never be enabled
safely is refused at validation time. The PCS promotion while suspended is the
budgeted cost (steady plan ≤ 8 windows + probe, inside the 1.5 s period) that
buys the live per-phase grid/load figures in the console whether or not the
adviser participates.

## 4. First-trial wiring (the 500 W window)

**Config for the trial period** — the ONLY key changed from the commissioned
defaults, everything per `config/config.live-write-example.yaml`'s commented
block plus:

```yaml
excess_charging:
  enabled: false          # suspended at boot; the trial STARTS at the toggle
  max_charge_from_export_w: 500   # the trial cap (graduation restores 2500)
  # ... all other commissioned defaults ...
```

The trial deliberately starts at the RUNTIME toggle, not `enabled: true`: the
window's start, end, and abort are the operator's console acts (exercising the
surface this package commissions), the boot state is off, and a mid-trial reboot
degrades to off — the fail-safe direction of P1. The runtime toggle can never
raise the cap (P5): 500 W is the commissioned envelope for the whole trial.

**Evidence the trial must record** (per `DESIGN_EXCESS_CHARGING.md` Phase 1;
each item already leaves a durable trace — the trial is a review of these, not a
new logging effort):

1. **Pre-flight**: the net-billing acknowledgement — `excess_charging_economics_acknowledged`
   audit row (P3; also satisfies the design's "confirm the NET-BILLING
   assumption" step), and per-pod export visible on the console (the §1
   `fleet_export_w` + per-unit figures).
2. **Start**: `excess_charging_toggled` (result `enabled`, principal) — the
   operator's console act.
3. **Arming the single neediest unit**: the existing `unit_armed` audit row with
   the `ARM` confirmation (unchanged).
4. **Per-cycle while active**: `control_decision` rows (source `optimizer`,
   principal `energypod:excess-adviser`) carry requested/authorized watts per
   unit; the observation stream carries measured battery watts and telemetry
   ages (ages must stay inside `export_telemetry_max_age_s`); the projection
   carries commanded vs export (`commanded_charge_w` / `fleet_export_w`).
5. **Transitions**: `excess_adviser.state_changed` events + snapshots — dusk
   collapse must show a clean `below_exit_hysteresis` withdraw, not a staleness
   collapse; any manual request on the target must show
   `yielding_to_higher_priority`.
6. **Hand-back**: `intent.expired` / `authorization.revoked`, then measured
   watts returning to the pod's own autonomy within the ~3.5–4.0 s watchdog gap.
7. **End/abort**: `excess_charging_toggled` (result `disabled`). The design's
   ABORT criteria stand verbatim — any one: measured charge exceeding eligible
   +10% for more than two cycles; export evidence stale/bad; any unit latching
   INHIBITED (especially `external_writer`); a fleet halt; the target reaching
   the SOC ceiling; operator command.
8. **Record**: results in `docs/CONTINUITY.md`; promote the §4c
   `PROTOCOL_EVIDENCE` classification for the promoted-read-plan timing.

**Graduation criteria to the production 2500 W** — ALL must hold, then the
operator edits `max_charge_from_export_w: 2500` in config and restarts (a config
revision — the commissioned path; never the runtime toggle):

1. At least 3 daytime surplus sessions across at least 2 days with ZERO abort
   criteria hit.
2. Measured ≈ commanded within ±10% steady-state throughout (excluding the first
   ramp second); no sustained exceedance.
3. Site export reduced by approximately the commanded charge while active — the
   arbitrage is physically real, not a CT artifact.
4. At least one live yield: a manual/agent intent claiming the target mid-window,
   clean withdrawal, autonomy resumed inside the watchdog window, re-entry only
   after the claim expired AND entry re-qualified.
5. No export-evidence staleness breach while commanding (a dusk/wane collapse
   must arrive as a legitimate `below_exit_hysteresis`/`no_export_headroom`
   withdraw, never `export_evidence_stale` mid-charge).
6. The operator reviews the audit trail and accepts the economics (kWh shifted
   vs the autonomy baseline) — the assumption of P3, confirmed in fact.

## 5. Console plan (for the follow-up web agent — NO implementation here)

All additions are FEATURE-DETECTED: an absent `adviser_state` in the snapshot
hides the tile, the toggle, and every change below; nothing else moves. The
existing patterns to ride: the shared data plane's 2.5 s live snapshot cadence
(the tile's live watts), `applyEventFrame`'s switch (the new event case), the
typed-confirmation dialogs (arm / emergency stop), and the optimistic-then-
confirm adoption (`releaseStopLatch`).

**W-A. The Home tile** (new card, after "What is powering the home?"): the
feature's whole story in one glance.

- Active: **"Solar surplus: charging mid at 1,400 W from 1,800 W export"** —
  `commanded_charge_w` + `target_unit_id` + `fleet_export_w`; secondary line
  "cap 2,500 W" (`charge_cap_w`; the trial renders "cap 500 W" with no invented
  label — the cap figure is the fact).
- Honest inactive states (one sentence from the FIRST matching reason code):
  - `disabled_by_config` → "Charging from solar surplus is off (config)."
  - `disabled_by_runtime` → "Charging from solar surplus is off until the
    controller restarts — the config re-enables it at boot." (only when the
    config boot value is true; else the plain off sentence)
  - `economics_acknowledgement_required` → "Waiting on the one-time net-billing
    confirmation before solar-surplus charging can start."
  - `export_evidence_missing|bad|stale` → "Export reading unavailable on the
    fleet — standing down (fail-closed)." with `fleet_export_w: null` shown as
    "not available", never 0.
  - `no_export_headroom` → "Exporting {fleet_export_w} — below the headroom
    margin, nothing to charge from."
  - `no_acceleration_over_autonomy` → "Surplus too small — taking over would
    charge slower than the pod does by itself."
  - `below_exit_hysteresis` → "Surplus is falling — handing back to the pod's
    own charging."
  - `no_eligible_target` → "Solar surplus available, but no battery needs
    charging (full, inhibited, or not armed)."
  - `yielding_to_higher_priority` → "Standing down — a manual request has
    {target_unit_id}."
- Per-phase figures in the tile body: one row per unit from the snapshot's
  per-unit telemetry readthrough — "mid: grid +620 W export · load 340 W"
  (`grid_power_w` negative = import, positive = export; `load_power_w` is the
  pod's local load; absent reads "not available"). This is R6's UI half and the
  sentence that makes the feature legible ("that export is what charged rhs").

**W-B. The toggle control** (the tile's footer; no new view — there is no
Settings view and one endpoint does not justify one):

- Current state, always: "On (config)" / "On — until restart" / "Off (config)" /
  "Off — until restart" from `enabled` + `enabled_origin`.
- Enable opens a typed-confirmation dialog: type **EXCESS**; the FIRST enable
  ever on the site additionally surfaces the net-billing acknowledgement — the
  exact assertion "This site's billing nets across phases" with a required
  checkbox, sending `"economics": "NET_BILLED"` (P3; captured once, never asked
  again). Disable asks nothing beyond the EXCESS confirmation.
- The 200 body's `adviser_state` is adopted optimistically; the next
  `excess_adviser.state_changed` / snapshot confirms (the `releaseStopLatch`
  pattern).
- Refusals render their `details` honestly: `unit_active_under_intent` names the
  units ("finish or cancel the request on mid first"); `latched_stop_holds`
  names the stop; `economics_acknowledgement_required` deep-links to the
  acknowledgement in the dialog.

**W-C. Event wiring** (`useConsoleData` / `applyEventFrame`, feature-detected
case): `excess_adviser.state_changed` patches the shell's adviser-state slice
from the payload and triggers the debounced authority refetch when
`active`/`enabled` changed (the adviser's intents already drive
`intent.accepted`/audit frames — this frame is the tile's, not the request
cards'). Unknown-type default behavior stays.

**W-D. Batteries view**: add the two readthrough figures (`grid_power_w`,
`load_power_w`) to each unit's detail rows, same import/export wording as the
tile.

## 6. Implementation plan (ordered)

Backend agent, in order (each slot lands its own red→green cycle; run ONLY the
named files while the live controller runs):

1. **B1 — State projection** (1 slot): the adviser exposes its held/hysteresis
   facts; a frozen `ExcessAdviserState` projection object (single writer: the
   fleet loop post-tick; readers: facade, toggle, event publication); the
   vocabulary constants of §1; the evidence rollup reusing the bound's own
   fail-closed rules. Red family: `tests/unit/test_excess_charge.py`
   ("adviser state projection" block).
2. **B2 — Events** (1 slot): `excess_adviser.state_changed` publication in the
   fleet loop, the §2 throttle tuple, the 30 s heartbeat-while-enabled. Red
   families: `tests/api/test_event_contract.py` (payload/throttle/heartbeat),
   `tests/unit/test_composition.py` (publication wiring).
3. **B3 — Toggle + acknowledgement gate + composition semantics** (2 slots):
   facade `set_excess_charging` (both actions, the P2 refusal set, the P3
   durable gate — audit-store query at boot, durable-append-first latch flip —
   idempotency, `excess_charging_toggled` / `excess_charging_economics_acknowledged`
   audit rows, Impl-10 atomic pattern); the REST endpoint with the P4 scope
   rules and the §3 error envelopes; the P6 composition rescope (block-present
   composes suspended machinery; the participation flag consumed at tick start —
   a disabled tick withdraws-if-held once, then idles with
   `disabled_by_runtime`/`disabled_by_config`). Red families:
   `tests/unit/test_service_facade.py`, `tests/api/test_rest_contract.py`,
   `tests/api/test_boundary_hardening.py` (scopes/interactive), the rescoped
   adviser-composition cases in `tests/unit/test_composition.py`, and the
   T-UNIT-CONFIG rescope in `tests/unit/test_config.py` (gates bind to
   block-PRESENT).
4. **B4 — Config/trial keys + docs** (1 slot): the §4 trial shape validated
   (`max_charge_from_export_w: 500` inside every gate), the
   `config/config.live-write-example.yaml` comment updated for P6 semantics
   (an explicit `enabled: false` block now composes suspended machinery — the
   "absent == enabled:false" comment line changes meaning and must be rewritten),
   `docs/CONTINUITY.md` entry, full-suite green.

Web agent, in order: **W1 tile** (1 slot) → **W2 toggle + acknowledgement flow**
(1 slot) → **W3 event wiring + Batteries figures** (1 slot). Red families:
`web/src/views/home/HomeView.test.tsx` (tile sentences, feature detection),
`web/src/app/useConsoleData.test.tsx` + `web/src/app/SharedDataPlane.test.ts`
(event case, optimistic adoption), `web/src/views/batteries/BatteriesView.test.tsx`.

**Risk notes (pinned for both agents):**

- The toggle must never bypass the config commissioning gates (P5): enabling at
  runtime cannot exceed `max_charge_from_export_w` or relax any gate, bound, or
  mode — participation only.
- The adviser still yields to every higher-priority source, unchanged; P2's
  enable refusal is an operator-clarity guard, not an arbitration change.
- Non-persistence (P1): a reboot during an enabled-at-runtime period reverts to
  the config default — the console must carry the "until restart" wording.
- The projection must never claim `active: false` while an adviser intent is
  live: derive from `held_intent_id`, one writer, tick-granular `last_tick_at`.
- The acknowledgement gate fails closed: no durable audit append ⇒ no latch ⇒
  no enable (an audit-store failure is a refusal, never a silent pass).
- The event throttle excludes watt wander by design; a consumer needing live
  watts reads the snapshot (the shell's 2.5 s cadence already does).
- The config-gate rescope (block-present) must not change absent-block behavior
  in any way — the two adviser-composition tests that pinned "composed only
  when enabled" change meaning deliberately; absent-block cases stay verbatim.
- Simulator persistence is in-memory by design: an acknowledgement captured
  under `simulate` is process-lifetime; the trial runs on the live deployment's
  durable store.

## 7. Operator decisions this package needs (verbatim-ready)

1. **Net billing** (gates the first enable ever; P3): "This site's billing nets
   across phases — import on one phase offsets export credit on another.
   Confirmed for excess-solar charging; capture it once as the acknowledged
   fact." (The first-enable dialog sends `"economics": "NET_BILLED"`.)
2. **First-trial authorization** (§4): "Authorized: one unit — the neediest at
   the time, capped at 500 W, in a daytime surplus window, with the other
   writer apps quiescent for that window, and the abort criteria exactly as
   written in DESIGN_EXCESS_CHARGING.md."
3. **Toggle-holder policy confirmation** (P1/P4): "The excess-charging toggle
   requires the arm scope and an interactive principal to enable, and the arm
   scope alone to disable; it does not persist across restart — the config file
   stays the source of truth and boot recomposes from it; the net-billing
   confirmation is captured once and never re-prompted."
