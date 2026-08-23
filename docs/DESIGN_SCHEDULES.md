# Schedules surface — the operator-facing contract

Status: DESIGN ACCEPTED PENDING IMPLEMENTATION (2026-08-23). Owner of the wire
contract: `docs/API_CONTRACTS.md` "Schedule" section (amended by this package:
the two REST endpoints, the `schedule_state` projection, the bus/audit
vocabulary, the config keys). Built feature: the domain, evaluator, and
repository (the original milestones; `src/energypod/domain/schedule.py`,
`src/energypod/application/scheduling.py`, `SQLiteScheduleRepository`).
Accepted scope: `docs/PRODUCT_NEXT.md` §3 "Next 2". Console surface plan: §6
here. Night-writer environment facts: `docs/CONTINUITY.md` 2026-08-23 (final)
and 2026-08-23 (census).

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file. The backend agent follows §7's ordered plan; the web agent follows
§6. No live-hardware interaction is authorized by this document — publishing
the first night schedule requires the operator decisions in §8, verbatim as
written there.

## 0. What exists, what this adds

The schedule domain is finished and validated: immutable civil-time entries
(cross-midnight, DST via IANA zone, overlap rejection at equal priority,
per-entry enabled, effective date bounds), a `ScheduleEvaluator` that turns a
`SchedulePlan` plus a wall instant into at most one short-TTL `SCHEDULE`
`ScheduleIntent`, and a `ScheduleRepository.get()/replace(expected_version,
replacement)` port with CAS versioning (`replacement.version` must equal
`expected_version + 1`) backed by a durable SQLite singleton. What does NOT
exist: any REST endpoint, any facade method, any evaluation in the fleet
cycle, any UI. The plan is ~40% a product.

This package adds exactly the missing 60%, and nothing to the domain except
one extension the operator's own doctrine already demands (per-battery watts,
§1). The additions:

1. **A facade + REST surface** — `GET/PUT /api/v1/schedule` with CAS replace,
   validation, the allowed-windows posture gate, and the one-time night
   acknowledgement (§3, §5).
2. **An evaluation loop** — a `ScheduleRunner` ticking in the fleet cycle
   beside the excess adviser, maintaining exactly one live `SCHEDULE`
   `PowerIntent` through the ordinary intent repository (§2).
3. **The night-writer posture mechanism** — `allowed_windows_local` in config,
   the shipped day-only default, the refusal that names the posture, and the
   PARTITION acknowledgement (§3).
4. **The adviser interaction** — `yield_to_schedule` (default true), per unit
   (§4).
5. **Observability** — the `schedule_state` snapshot projection and the
   `schedule.replaced` / `schedule_window.opened` / `schedule_window.closing`
   bus events (§5).
6. **The console** — the Schedule list-editor view and Home's next-action card
   (§6, for the web agent).

Nothing in this package changes the arbiter's priority order, the allocator,
the safety kernel, the actor, the arm-time sole-writer preflight, or any other
safety path. A schedule-derived intent is an ordinary `PowerIntent` at the
lowest source priority, judged by everything exactly as a manual request is.

## 1. The operator model (plain language first)

**What a schedule IS here.** A schedule is a published list of named, weekly
recurring windows — "charge the batteries named X at Y watts, from 00:01 to
05:59, on these days, between these dates." Each window (an *entry*) says:
its **name** (the entry id — the operator writes "Night Charge" or
"morning-topup"; there is deliberately no separate name field), the **days**
of the week it runs, the **start and end** local times (a window may cross
midnight — 22:30→06:00 is one entry, not two), the **direction** (charge or
discharge), the **watts**, the **batteries** it commands, an optional **date
range** it is effective within, and an **enabled** switch. The whole list is
one **plan** with one **version** number and one IANA **timezone**. Editing is
whole-plan: the console loads the plan, the operator edits a local draft, and
publishing sends the complete list; the version number makes two concurrent
publishes collide honestly instead of silently merging.

**Per-battery watts are the native v1 form.** The 2026-08-23 operator ruling
("each setting is that battery's own request") is the house doctrine for every
other dispatch surface; schedules follow it. The domain's entry today carries
one scalar `watts` (a fleet total the allocator then distributes
capacity-weighted — a split the operator does not choose and cannot see in the
entry). This package extends `ScheduleEntry` with the optional
`watts_by_unit` mapping, exactly the `PowerIntent` dual-form rules: an entry
carries **either** scalar `watts` (fleet total; the existing behavior, wire-
supported) **or** `watts_by_unit` (one positive integer per selected unit;
key set exactly `unit_ids`; the fleet total is the sum); never both, never
neither; `idle` entries carry scalar `watts: 0` and no mapping. The v1 editor
defaults to the per-battery form (one field per selected battery, with a
"same for all" quick fill) and hides the scalar form in the advanced
disclosure. This is the one domain change in the package; it is an addition,
not a redesign — every existing scalar entry decodes unchanged.

**What the console shows.** On Home: a "Next scheduled action" card — the
next entry's name, direction, per-battery watts, and a starts-in countdown
(or "running now" with an ends-in countdown while a window holds). On the
Schedule view: the list editor — one card per entry with its name, days,
times, direction, per-battery watts, batteries, and enabled toggle; publish
publishes the whole list; validation errors render inline on the offending
row; the allowed-window guard (§3) renders on the form and refuses before the
request when the operator's time pickers leave the allowed range. In
Activity: one row per publish with a plain diff ("Schedule v2→v3 — added
Night Charge, removed old-evening"). Pause/skip is the entry's own enabled
toggle; "pause everything" is one publish that disables every entry (the diff
summary says so — there is deliberately no second master switch, §3).

**The honest limits, said in the product.** A schedule is the *lowest*-
priority intent source: any manual request, any agent request, the emergency
stop, and the solar-surplus adviser (unless it yields, §4) outrank it, per
unit. A window that opens while something else commands a battery *waits* —
cleanly, silently taking over the moment the higher request lapses (§2). A
schedule never fights the site's other applications: at night, by default, it
cannot even be published (§3).

## 2. The evaluation loop

**Where.** `src/energypod/application/scheduling.py` grows a `ScheduleRunner`
beside the existing `ScheduleEvaluator`. It is composed into the fleet cycle
(`composition.py::_run_fleet`) as a sibling of the adviser step, in the SAME
bounded-tick pattern: after the polls and the recovery pass, **before** the
adviser step and the kernel tick, wrapped in `wait_for(interval)` with
`suppress(Exception)` per cycle (never `CancelledError`), never able to delay
renewal or control. Ordering rationale, pinned: the schedule's claim is a
*published fact* and the adviser is the *opportunist* — evaluating the
schedule first means the adviser's same-tick claim check already sees the
schedule's intent, so a yield (§4) resolves within one cycle with no
double-claim noise. A runner failure is survivable per cycle exactly like an
advisory failure; the held intent's TTL lapse plus the firmware watchdog
(~3.5–4.0 s) are the designed hand-back, and the runner never halts the fleet.

**Per tick.** Read the plan through the repository port (one singleton row;
the repository is the single source of truth, so a publish lands within one
cycle and there is no cache to invalidate; a read failure is survivable per
cycle). Evaluate at `clock.wall_now()` in the plan's timezone. The evaluator
returns at most ONE matching enabled entry — `max(matches, key=(priority,
entry_id))` — so at most one schedule intent is ever live, fleet-wide. The
runner maintains exactly one held `PowerIntent`, keyed by
`(plan.version, entry_id)`:

- **No plan / no match, nothing held** — idle (`no_plan` / `no_window_open`).
- **No match, something held** — remove the held intent (the window ended,
  the entry was disabled, or the plan changed): publish
  `schedule_window.closing`, then idle.
- **Match, nothing held** — submit (`schedule_window.opened`).
- **Match, held, same key** — renew: remove the previous, submit fresh
  (the adviser's exact remove-then-submit discipline).
- **Match, held, different key** (a publish landed mid-window, or a
  higher-priority entry's window opened over a running one) — remove, submit
  the new key. The version inside the key means a same-entry watts edit takes
  effect the same tick.

**The submitted intent.** An ordinary `PowerIntent`, `source: SCHEDULE`,
direction and units and watt form carried from the entry verbatim (per-unit
targets stay per-unit caps at allocation — the existing doctrine), TTL
`schedule.intent_ttl_s` (default 10.0 s; the same bounds as the adviser's:
> `control_period_s`, ≤ 300 s, validated at config time). It enters through a
new composition-internal facade twin `submit_schedule_intent` — the exact
`submit_advisory_intent` pattern: same validation, audit
(`intent_accepted`), idempotency/correlation contract, and publication, with
the mintage source pinned to `SCHEDULE` and its own intent-id prefix
(`schedule-`), never routed on REST or MCP. The composed principal is
`energypod:schedule-runner` (scopes `observe` + `dispatch`, non-interactive,
site-bound) — audit attribution separates it from every console, agent, and
adviser row by principal plus the `schedule` source tag.

**Source precedence — confirmed and pinned, unchanged.** The arbiter's order
is `emergency_stop > manual > agent > optimizer > schedule` (equal priority
by acceptance revision then stable id), per unit. The pins that matter:

- **A window opens while a manual/agent intent runs: the schedule WAITS.**
  The runner does not check claims and does not withdraw — it keeps renewing
  its short-TTL intent every tick, the arbiter simply does not represent it
  on the units the higher intent claims, and it takes those units over the
  cycle after the claimer lapses (≤ one control period later). There is no
  hysteresis to protect and no retaliation; waiting is free and clean. This
  is the same "returns the moment a claimer lapses" rule every other intent
  source lives by.
- **A latched emergency stop** claims exactly its units and dominates the
  whole cycle while it holds; the schedule keeps renewing and is represented
  the cycle after acknowledgement.
- **The excess adviser** (OPTIMIZER) outranks SCHEDULE by arbiter today —
  verified in code: the adviser's own yield check
  (`_HIGHER_PRIORITY_SOURCES`) contains only `EMERGENCY_STOP`, `MANUAL`,
  `AGENT`, so without §4's flag the adviser would keep renewing and WIN any
  per-unit contest against a schedule, starving it invisibly. §4 closes that.

**Window end = non-renewal, the same watchdog hand-back as everything
else.** The held intent's TTL is shorter than one control period of neglect;
when the window ends the runner removes it, the kernel stops minting, the
actor stops writing, and the firmware watchdog returns the pod to its own
CT-following autonomy. If the runner dies mid-window the same lapse happens
by TTL alone — the designed fail-safe, identical to the adviser's.

## 3. The night-writer postures, as config

The environment fact (operator-confirmed, CONTINUITY 2026-08-23): the site's
other applications write these batteries **at night**, exactly when a Night
Charge schedule would run, and the arm-time sole-writer preflight latches
`external_writer` by design — "coordinate, don't fight." The three postures
from PRODUCT_NEXT, as mechanism:

- **YIELD (the shipped default): schedules are a day-window feature.** The
  controller may schedule only inside the allowed windows; the night stays
  with the existing writers. Implemented as the `allowed_windows_local`
  policy below; nothing else needed.
- **PARTITION (the operator-granted upgrade): the controller owns a granted
  window.** The external writer applications stand down for it. Implemented
  as an explicit, commissioned config revision widening the allowed windows
  PLUS the one-time durable acknowledgement on the first night publish. The
  acknowledgement is the captured human fact; the preflight remains the
  structural enforcement — if a writer does NOT stand down, the arm latches
  `external_writer` and the console says so honestly (that is the partition
  being enforced, not the controller fighting).
- **CONTESTED — not implementable, and not offered.** There is no setting
  that says "run at night anyway and fight them." The preflight latching is
  a safety property, not a preference; a posture that arms into a known
  foreign writer would produce a nightly inhibit-and-audit loop with zero
  energy moved. Rejected; the refusal below says so in one sentence.

**The config.** A `schedule:` block in `runtime/config.py`, the exact
`excess_charging` composition semantics (P6 doctrine):

```yaml
schedule:
  # The allowed command windows, local civil time ("HH:MM" pairs;
  # cross-midnight pairs allowed). The union of the windows is what a
  # schedule may command. OMITTED = the day-only YIELD default.
  allowed_windows_local: [["06:00", "20:00"]]
  intent_ttl_s: 10.0
```

- **A PRESENT block composes** the surface: both REST routes live, the
  `ScheduleRunner` in the fleet cycle, `schedule_state` in the snapshot, the
  posture on GET. **An ABSENT block composes nothing** — byte-identical to
  today: no runner, no projection key, and both routes answer
  409 `schedule_not_commissioned`. Symmetric with the adviser; a schedule
  can never act on a site that did not commission scheduling.
- **`allowed_windows_local`**, default `[["06:00", "20:00"]]` when the block
  is present without the key — the YIELD posture. Each pair is
  `["HH:MM", "HH:MM"]` civil times in the plan's sense (no zone of their
  own; they are policy walls). The **union** of the pairs is the allowed
  set. The shipped default is also the named constant `DAY_DEFAULT`
  (`06:00`–`20:00`): **night means "any civil minute outside DAY_DEFAULT"**
  — independent of how the operator narrows or widens the policy, so
  "night" always means the same thing in the acknowledgement and the
  console copy.
- **`posture` is derived, read-only, never stored**: `partition` when the
  allowed set covers any minute outside DAY_DEFAULT (night granted by
  config), else `yield`. GET and the projection carry it.
- **There is deliberately no `enabled` key.** The plan IS the state: an
  empty plan is off, an entry's `enabled` flag is its own pause, and "pause
  everything" is one publish that disables every entry. A second master
  switch would be a second way to be silently off — the invisible-starvation
  failure class this whole surface exists to remove. Runtime participation
  toggles (the excess pattern) are not imported here: publishing is already
  an explicit, audited, guarded operator act, and the fail-safe direction is
  structural (no plan, or all entries disabled ⇒ no intent ever submitted).

**Enforcement — the REST layer refuses, with the posture named.** Every
enabled entry's window — split across midnight when it crosses — must lie
ENTIRELY inside the union of `allowed_windows_local`, or the PUT is refused
before anything is stored:

```
409 schedule_window_not_allowed
message: "Night Charge (00:01–05:59) falls outside the allowed windows
          06:00–20:00 — the night window belongs to the site's other
          writer applications (day-only posture). Trim the entry to the
          allowed windows, or make the partition choice: stand the
          external writers down and widen allowed_windows_local in
          config, then acknowledge once in the console."
details: {"posture": "yield",
          "allowed_windows_local": [["06:00", "20:00"]],
          "offending": [{"entry_id": "Night Charge",
                         "start_local": "00:01", "end_local": "05:59"}]}
```

The console CANNOT widen the allowed windows — that is a config revision
plus a restart, exactly like the excess feature's cap graduation. The
operator-facing sequence for granting night is therefore: edit config →
restart → publish the night schedule → acknowledge once (below). Every step
is deliberate and durably recorded.

**The one-time night acknowledgement (the NET_BILLED pattern, durable
once).** After the config grants night windows, the FIRST publish ever on
the site whose enabled entries include any night minute must carry
`"night_posture": "PARTITION_ACKNOWLEDGED"`, unless the site has already
captured the durable fact. The fact is the audit event
`schedule_night_windows_acknowledged` (subject, site, wall time, the
assertion text of §8 item 2), durable in the audit store, boot-loaded via
one keyed existence check, never re-prompted — the exact mechanics of
`excess_charging_economics_acknowledged`, including durable-append-FIRST:
an audit failure refuses the publish (no night plan without the durable
fact). Refusal shape:

```
409 night_posture_acknowledgement_required
details: {"acknowledgement": "PARTITION_ACKNOWLEDGED"}
```

Day-only publishes never see either refusal, on any site, ever.

## 4. The adviser interaction (`yield_to_schedule`)

**The key (config, default true).** `excess_charging.yield_to_schedule:
bool = true` — a new key on the existing `ExcessChargingConfig`, present
whenever the excess block is. It is the adviser's own setting because it is
the adviser's own behavior: the one extra check in the existing withdraw
path.

**What true does.** A live `SCHEDULE` intent claiming the adviser's target
unit becomes a yield trigger, exactly the MANUAL/AGENT path: the adviser
withdraws its own intent by repository removal (never a stop triple) and
does not re-post until that claim has expired AND the entry hysteresis
re-qualifies. Implementation shape: the claim check generalizes from the
hard-coded `_HIGHER_PRIORITY_SOURCES` frozenset to
`{EMERGENCY_STOP, MANUAL, AGENT} ∪ ({SCHEDULE} if yield_to_schedule else
∅)`. Per unit, always — the adviser's scope is exactly one unit, so the
yield is per unit by construction.

**The reverse question, answered and pinned: a schedule NEVER suppresses
the adviser fleet-wide.** Suppression is per unit, by claim, like
everything else in the per-unit arbiter. While a schedule claims `mid`,
the adviser (yielding) stands down on `mid` and remains free to charge
`lhs` from the same fleet export in the same cycle — net-across-phases
arbitrage is the adviser's whole premise. Only a live emergency stop
(which claims every unit) suppresses fleet-wide, and that is the existing
rule, not a schedule's.

**What false does (and why the default is true).** With
`yield_to_schedule: false` the behavior is today's: the arbiter ranks
OPTIMIZER above SCHEDULE, the adviser keeps renewing, and a deliberately
published daytime schedule is starved invisibly — the schedule's window
opens, nothing happens, and nothing says why. That is exactly the failure
class the operator has already had to explain once; the flag exists so the
choice is explicit and auditable, not so it can be forgotten.

**Physics, for honesty.** Export exists only in daylight; night windows
exist only in darkness; at dusk the adviser's own bound collapses to zero
as export collapses. The overlap this key governs is the unusual daytime
schedule (an off-peak daytime tariff window). The two features are
complementary by physics; the key covers the corner.

**One documented side effect (existing behavior, pinned as intended).**
The excess activation toggle's enable refusal (`excess_enable_refused`,
`unit_active_under_intent`) already treats a live `schedule`-source intent
as a blocking request — so the adviser cannot be enabled while a schedule
window runs. Enabling happens by day when no window holds, which physics
makes the normal case. No change; documented here so the refusal is not a
surprise.

## 5. The REST surface, facade, audit, and events

### `GET /api/v1/schedule` (observe scope)

```json
{
  "plan": null,
  "policy": {"posture": "yield",
             "allowed_windows_local": [["06:00", "20:00"]],
             "intent_ttl_s": 10.0},
  "acknowledged_night_windows": false,
  "next_action": null
}
```

- `plan` is `null` before the first publish, else
  `{"version": 4, "timezone": "Australia/Brisbane", "entries": [ ... ]}` with
  each entry in the wire shape of §5's PUT body (below) — the editor renders
  straight from this.
- `next_action` is computed fresh from the stored plan by a pure function
  (§5.4): the next occurrence of any enabled entry within its effective
  bounds, `{"entry_id", "days", "start_local", "end_local", "action",
  "watts" | "watts_by_unit", "unit_ids", "starts_at", "starts_in_s"}` —
  `starts_at` an ISO-8601 local instant carrying the plan's zone offset, so
  the countdown is server-computed once and client-recomputed per frame.
  `null` when no enabled entry ever occurs again (all disabled, or past
  every `effective_until`).
- 409 `schedule_not_commissioned` when the config block is absent.

### `PUT /api/v1/schedule` (dispatch scope + interactive principal + Idempotency-Key)

```json
{
  "expected_version": 3,
  "timezone": "Australia/Brisbane",
  "entries": [
    {"entry_id": "Night Charge",
     "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
     "start_local": "00:01", "end_local": "05:59",
     "action": "charge",
     "watts_by_unit": {"lhs": 2500, "mid": 2500, "rhs": 2500},
     "unit_ids": ["lhs", "mid", "rhs"],
     "effective_from": "2026-08-25", "effective_until": "2035-12-31",
     "priority": 0, "enabled": true}
  ],
  "night_posture": "PARTITION_ACKNOWLEDGED"
}
```

- `expected_version` is the plan the editor loaded (`null` asserts "no plan
  exists" — the first publish). `night_posture` is optional and consulted
  only on the first night publish ever (§3). An entry carries exactly one
  watt form: `watts` (scalar fleet total) **or** `watts_by_unit` (key set
  exactly `unit_ids`, one positive integer per unit); `action: "idle"`
  requires scalar `watts: 0` and no mapping. `days` are lowercase
  three-letter names; times `"HH:MM"`; dates ISO. `priority` is an integer
  (higher wins among overlapping windows; equal-priority overlap is the
  domain's rejection).
- **Validation order, pinned:** (1) 422 `validation_error` — shape and
  every domain rule, per-entry field errors (the `ScheduleValidationError`
  messages mapped one-to-one; the wire names the offending `entry_id`);
  (2) 409 `schedule_window_not_allowed` — §3's containment rule;
  (3) 409 `night_posture_acknowledgement_required` — §3's one-time gate;
  (4) 409 `schedule_version_conflict` — `expected_version` ≠ the stored
  plan's version (or a non-null expected against no plan), `details:
  {"current_version": <int | null>}` — `ScheduleVersionConflict` mapped,
  and the console's answer is "the plan changed elsewhere — reload and
  re-apply", the honest CAS outcome.
- **The mutation.** Facade `replace_schedule(principal, *, expected_version,
  timezone, entries, night_posture, idempotency_key, request_id)`: build the
  new `SchedulePlan` at version `current + 1`, capture the acknowledgement
  first when it is being captured (durable append; an audit failure refuses
  — no night plan without the fact), commit through
  `ScheduleRepository.replace` (the CAS the port already enforces), then
  audit and publish on the Impl-10 commit-then-audit discipline with
  `submit_intent`'s compensating shape: an audit/publication failure after
  the commit restores the prior plan (the same CAS path) and surfaces the
  error — a publish is either stored-and-recorded or rolled back, never
  stored-and-silent.
- **200 response:**

```json
{"version": 4,
 "plan": { ... the stored plan ... },
 "diff": {"added": ["Night Charge"], "removed": ["old-evening"],
          "changed": ["morning-topup"], "timezone_changed": false},
 "acknowledged_night_windows": true,
 "next_action": { ... }}
```

  The diff summary is computed entry-by-entry on `entry_id` (same id and
  equal shape = unchanged; same id, different shape = changed; no id on one
  side = added/removed) and is exactly what the audit row and the Activity
  sentence carry.

### Facade methods (the complete list)

- `get_schedule(principal)` — observe; the GET view above; repository and
  pure-function reads only, never triggers control.
- `replace_schedule(...)` — dispatch + interactive; §5 above.
- `submit_schedule_intent(...)` — composition-internal, source pinned to
  `SCHEDULE`, prefix `schedule-`, never routed on REST or MCP; the
  `submit_advisory_intent` twin including its audit/publication contract.

### Audit events

- `schedule_replaced` — result `replaced`; payload: version from → to, the
  diff summary, timezone change flag, principal, request id. One row per
  publish.
- `schedule_night_windows_acknowledged` — the durable-once fact (§3); the
  keyed-existence check loads it at boot; never re-prompted.
- (Unchanged, and load-bearing here: every runner renewal already writes
  its `intent_accepted` row through the internal submit path — the same
  per-tick cadence the adviser produces today, so no new audit growth
  class; the two are complementary by physics — nights versus daylight.)

### Bus events

- `schedule.replaced` — `{principal, version, diff}`; the console refetches
  the plan and re-renders the editor and Home card.
- `schedule_window.opened` — `{entry_id, version, action, watts |
  watts_by_unit, unit_ids, ends_at}`; published on the runner's first
  submit for a window key.
- `schedule_window.closing` — `{entry_id, version, unit_ids, reason:
  "window_ended" | "plan_replaced" | "no_plan"}`; published on the removal
  tick. (The name is "closing" — the moment the window's command ends —
  because a "closed" past window is not a state anything holds.)
- The countdowns are snapshot-derived, not event-derived: `starts_in_s` /
  `ends_in_s` refresh on the console's existing snapshot cadence; the
  events are the transition moments (toasts, card swaps), not a clock.

### The `schedule_state` snapshot projection

Feature-detected on the snapshot top level beside `intent` and
`adviser_state`: **absent when the config block is absent**, present
whenever it is composed. One writer — the runner's post-tick update
(the `adviser_state` single-writer doctrine; `active` derives from
`held_intent_id`, never a lifecycle guess, so it can never claim inactive
while a schedule intent is live):

```json
"schedule_state": {
  "version": 4,
  "active": true,
  "entry_id": "Night Charge",
  "held_intent_id": "schedule-3-881.234117",
  "ends_at": "2026-08-24T05:59:00+10:00",
  "ends_in_s": 2743,
  "next": { ... the next_action object AFTER the running window ... },
  "posture": "partition",
  "last_action": "renew",
  "last_tick_at": "2026-08-24T03:13:41+10:00",
  "reason_codes": ["window_open"]
}
```

**The reason vocabulary — ONE vocabulary, pinned** (the projection codes;
there is no pre-existing tick vocabulary to preserve here): `no_plan`,
`no_window_open`, `window_open` (submitting/renewing),
`waiting_for_higher_priority` (a window holds and every one of its units is
claimed by a higher-priority live intent — the honest "waiting" sentence on
Home), `window_ended`, `plan_changed`. `last_action` is the runner's action
vocabulary: `idle | submit | renew | remove`.

### 5.4 The pure next-occurrence helpers

Two pure functions join `ScheduleEvaluator` (both unit-testable without
composition, both DST-honest through the plan's zone):
`next_start(plan, at) -> (entry, datetime) | None` — the earliest future
local start among enabled entries inside their effective bounds, searched
over the next 8 days (7 weekdays + the boundary), ties by `(priority,
entry_id)`; and `window_end(entry, at) -> datetime` — the local end instant
of the currently matching window (midnight-crossing aware). `next_action`
on GET, `next`/`ends_at` in the projection, and the Home card are all these
two functions' outputs; no client reimplements civil-time arithmetic.

## 6. Console plan (for the follow-up web agent — NO implementation here)

All additions are FEATURE-DETECTED: an absent `schedule_state` (or the
409 `schedule_not_commissioned`) keeps the nav placeholder exactly as today
("Schedule — not available yet", the honest not-yet entry in
`PLANNED_VIEWS`) and hides the Home card; nothing else moves. Patterns to
ride: the typed-confirmation dialog (`SolarSurplusTile`'s EXCESS flow —
the refusal IS the routing), the request card's per-battery figures
(`NowView`'s `watts_by_unit` rendering), the shared data plane's 2.5 s
snapshot cadence (countdowns), `applyEventFrame`'s switch (the new event
cases), and optimistic-then-confirm adoption.

- **W-A. The Schedule view** (nav id `schedule`, promoted out of
  `PLANNED_VIEWS`): the v1 list editor. One card per entry — name, day
  toggles (7), start/end time pickers, direction (charge / discharge),
  per-battery watts (one field per selected battery, "same for all" fill;
  the Now dispatch form's per-battery pattern), battery multi-select,
  enabled toggle; advanced disclosure per entry (`priority`, effective
  dates, scalar fleet-total watts). Add/remove entries; "pause all" = one
  publish disabling every entry (the diff says so). Local draft, "Publish
  changes" sends the whole list with `expected_version`; a 409
  `schedule_version_conflict` renders "the plan changed elsewhere — reload
  and re-apply" (never a silent merge). Domain validation errors map
  one-to-one onto inline row errors (duplicate id, zero-length window,
  equal-priority overlap naming the pair, idle-with-watts, unknown
  timezone). **The allowed-window guard renders on the form**: the policy
  line ("Schedules may command 06:00–20:00 — day-only posture; the night
  window belongs to the site's other applications") plus a client-side
  pre-check mirroring the containment rule, so a night-time picker refusal
  happens before any request; the server's 409 renders the same sentence
  with the offending rows.
- **W-B. The night-posture dialog** (first run only, and only the
  acknowledgement half): triggered strictly by the 409
  `night_posture_acknowledgement_required` refusal — the excess
  acknowledgement pattern: the exact assertion of §8 item 2, a required
  checkbox, resend with `"night_posture": "PARTITION_ACKNOWLEDGED"`,
  captured once, never asked again. The OTHER night refusal
  (`schedule_window_not_allowed`) never opens a dialog — the console cannot
  widen the policy; it renders the refusal sentence and the config path
  ("stand the external writers down and widen `allowed_windows_local` in
  config, then restart, then publish again").
- **W-C. Home's "Next scheduled action" card** (the slot that today says
  "nothing is scheduled"): running-now state from `schedule_state` — name,
  direction, per-battery watts, "ends in H:MM"; `waiting_for_higher_priority`
  renders "Night Charge is waiting — a manual request holds its batteries";
  else the next entry with "starts in H:MM" from `next.starts_in_s`
  (client-recomputed between snapshots); no plan / nothing coming renders
  the honest empty sentence plus the posture line. The countdown is
  plain-language and reduced-motion-safe (no flip animation; a stable
  "H:MM" string refreshed on the snapshot cadence).
- **W-D. Event + Activity wiring**: `schedule.replaced` refetches the plan
  (editor + Home); `schedule_window.opened` / `schedule_window.closing`
  swap the Home card state and toast; Activity renders `schedule_replaced`
  rows as "Schedule v2→v3 published — added Night Charge, removed
  old-evening" from the diff payload, and the night acknowledgement row
  once, verbatim.

## 7. Implementation plan (ordered)

Backend agent, in order (each slot lands its own red→green cycle; run ONLY
the named files while the live controller runs):

1. **B1 — Domain watt form + pure helpers** (1 slot): the `watts_by_unit`
   extension on `ScheduleEntry` (the `PowerIntent` dual-form rules, frozen
   mapping, key set == `unit_ids`); the evaluator passes the entry's form
   through; `next_start` / `window_end`. Red family:
   `tests/unit/test_schedule.py` (dual-form validation, sum derivation,
   idle rules, next-occurrence across midnight / DST / effective bounds /
   disabled entries).
2. **B2 — The runner** (1 slot): `ScheduleRunner` in
   `src/energypod/application/scheduling.py` — the §2 tick state machine
   against injected ports (repository, evaluator, clock, submit/remove),
   reason codes, one-held-intent invariant, remove-then-submit renewal,
   survivable per-cycle failure. Red family: `tests/unit/test_schedule.py`
   (the runner block: open/renew/re-key/end/plan-change/waiting, TTL
   lapse on a dead runner).
3. **B3 — Facade + internal submit twin** (1–2 slots):
   `submit_schedule_intent` (source pinned, prefix, audit/publication
   contract); `get_schedule`; `replace_schedule` — domain validation
   mapping, allowed-windows containment, the night acknowledgement
   (durable-append-first, keyed boot load), CAS with
   `ScheduleVersionConflict` mapping, the diff summary, Impl-10
   commit-then-audit with compensating restore, idempotency, scopes.
   Red families: `tests/unit/test_service_facade.py`,
   `tests/unit/test_facade_audit_content.py`,
   `tests/unit/test_repositories.py` (CAS conflict already pinned — extend
   to the facade path).
4. **B4 — REST** (1 slot): both routes, the §5 envelopes (422 /
   `schedule_window_not_allowed` / `night_posture_acknowledgement_required`
   / `schedule_version_conflict` / `schedule_not_commissioned`), dispatch +
   interactive on PUT, Idempotency-Key, `schedule.replaced` publication.
   Red families: `tests/api/test_rest_contract.py`,
   `tests/api/test_boundary_hardening.py` (scopes/interactive/idempotency),
   `tests/api/test_event_contract.py` (`schedule.replaced`).
5. **B5 — Composition, projection, events, adviser yield** (1–2 slots):
   `ScheduleConfig` (block-present semantics, `allowed_windows_local`
   default + validation, `intent_ttl_s` bounds) and
   `excess_charging.yield_to_schedule`; the runner wired into `_run_fleet`
   (bounded, suppressed, BEFORE the adviser step); `schedule_state`
   single-writer projection; `schedule_window.opened` / `closing`
   publication; the adviser's generalized claim check. Red families:
   `tests/unit/test_config.py`, `tests/unit/test_composition.py`
   (composition, ordering, projection writer, absent-block byte-identity),
   `tests/unit/test_excess_charge.py` (the `yield_to_schedule` family:
   yield-per-unit, re-entry after claim expiry + hysteresis, false =
   today's behavior), `tests/api/test_event_contract.py` (window events,
   throttling to transitions).
6. **B6 — Config example + docs + suite** (0.5–1 slot): the commented
   `schedule:` block in `config/config.live-write-example.yaml`; the
   `docs/CONTINUITY.md` entry; full-suite green.

Web agent, in order: **W1 the editor view** (1–2 slots; the largest single
piece — draft state, validation mapping, the guard) → **W2 Home card +
event wiring** (1 slot) → **W3 night dialog + conflict + Activity** (1
slot). Red families: `web/src/views/schedule/ScheduleView.test.tsx` (new),
`web/src/views/home/HomeView.test.tsx` (card states),
`web/src/app/useConsoleData.test.tsx` + `web/src/app/SharedDataPlane.test.ts`
(event cases, feature detection),
`web/src/views/activity/ActivityView.test.tsx`.

**Risk notes (pinned for both agents):**

- The runner is the LOWEST-priority source and must never special-case
  arbitration: no claim checks, no withdrawal against higher sources, no
  idle intents to "hold" units. Waiting is the arbiter's job.
- Exactly one live `SCHEDULE` intent, ever — the remove-then-submit renewal
  must never leave two (the held-id invariant is a named test).
- The projection must never claim `active: false` while a schedule intent
  is live: derive from `held_intent_id`, one writer.
- The allowed-windows check is a REST/facade gate, NOT an evaluator gate:
  a plan already in the store before a config NARROWING still evaluates
  (honesty: the config revision is the operator's act; the next publish is
  refused). Say this in the config comment.
- The night acknowledgement is durable-append-FIRST; an audit failure
  refuses the publish. Fail closed, always.
- Absent-block behavior is byte-identical to today (no runner, no
  projection key, 409 on both routes) — the same test discipline as the
  adviser's absent-block cases.
- The console must never widen `allowed_windows_local` — there is no
  endpoint, and the UI copy for `schedule_window_not_allowed` must point
  at the config path, not offer a toggle.

## 8. Operator decisions this package needs (verbatim-ready)

1. **Night posture — the default (blocks nothing, confirms the shipped
   state):** "Schedules start day-only: a schedule may command within
   06:00–20:00 local, and the night window stays with the site's existing
   writer applications. Confirm this default posture."
2. **Night partition — the grant, when night charging is actually wanted:**
   "Stand the external writer applications down for the night window and
   grant it to the controller: widen `allowed_windows_local` in the
   controller config (a config revision and restart), then publish the
   night schedule and acknowledge once — 'the external writer applications
   stand down for the granted window; the controller owns it.' Captured
   once as a durable fact, never asked again."
3. **Adviser-vs-schedule default:** "Confirm `yield_to_schedule: true`:
   while a published schedule claims a battery, the solar-surplus adviser
   stands down on that battery only — per unit, never fleet-wide — and
   returns when the window ends. Without it the adviser outranks the
   schedule and the schedule is starved invisibly."
4. **v1 scope trims:** "The v1 editor offers charge and discharge windows
   with per-battery watts; 'hold to zero' (idle) windows and entry
   priorities stay wire-supported (the domain's shape) but out of the v1
   picker — an idle schedule window cannot overrule the adviser, because
   schedules rank lowest. Confirm the trim."
5. **Schedule timezone:** "Every plan carries one IANA timezone and window
   boundaries follow it through DST. Confirm the site zone for the first
   publish (e.g. Australia/Brisbane)."
