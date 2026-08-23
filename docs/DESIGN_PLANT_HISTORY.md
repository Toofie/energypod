# Plant history — the telemetry historian (design)

Prepared: 2026-08-26 (post-`939b0f9`; the night-charge console wave closed, the
operator's plant-history request). Accepted design for implementation —
contracts, sizing math, red-test families, and the ordered plan. No code, API,
hardware, or test interaction was performed to produce this document; the
src/ facts behind the "what exists" claims are cited inline.

Sources: `docs/PRODUCT_ROADMAP.md` ("Energy Flow ... power and energy charts
... Missing or estimated intervals are visibly different from measured data";
Batteries "recent trend"), `docs/API_CONTRACTS.md` (block-presence config,
observe-scope reads, the energy days route's limit-only paging, the
scorecard's never-interpolate doctrine), `docs/DESIGN_ENERGY_SCORECARD.md`
(the design/plan/red-test precedent this follows),
`src/energypod/runtime/composition.py` (`_MAX_OBSERVATION_HISTORY_PER_UNIT =
64`; the fleet-cycle ordering; `_peek_authorizations`), 
`src/energypod/adapters/persistence/sqlite.py` + `src/energypod/db/schema.py`
(schema_version 2 landed with the energy ledger; this design is version 3),
`src/energypod/application/service.py` (`_telemetry_summary` — the field set
that exists to record), and `web/package.json` (react/react-dom only — no
chart library; the only hand-rolled SVG anywhere in the console is the Energy
Flow view's small static site-bus illustration, and no time-series chart
exists in `web/src` today).

Operator framing this answers: "show me what the plant actually did" — pick a
battery, pick a window (hours/days/weeks), and see SOC, power, grid exchange,
and temperatures as they happened; gaps show as gaps; degraded periods are
marked as degraded. Today the console can only show NOW: the observation
repository keeps ~64 samples per unit IN MEMORY (bounded, gone at every
restart), the scorecard persists daily kWh aggregates only, and the audit
trail persists decisions, not telemetry. Nothing time-series is durable.

---

## 0. What exists, what this adds

| Fact today | Where | Consequence |
|---|---|---|
| Telemetry is volatile: ~64 observations per unit in memory, reset every boot | `composition.py` `_MAX_OBSERVATION_HISTORY_PER_UNIT = 64`; `memory.py` `InMemoryObservationRepository` (bounded deque) | There is no history to query; a restart erases even the last hour |
| The observation stream is rich and control-rate: every fleet cycle (~1.5 s) each actor appends a full `Observation` (SOCs, watts, grid/load CT, pack V/I, cells, temps, limits, mode words, quality map) | `composition.py` `_run_fleet` poll step; `observations.py` | The data already flows — nothing samples it durably |
| The fleet loop already hosts read-only consumers beside control: the recovery pass, the foreign-objective pass, the schedule runner, the adviser step, the energy accountant — each bounded and suppressed per cycle, all after the polls and before the kernel tick | `composition.py` `_run_fleet` | A historian sampler is one more member of that family, not a new task |
| The fleet loop already peeks per-unit authority each cycle (`(watts, direction)` per unit) and the intent repository holds the per-unit winner set | `composition.py` `_peek_authorizations`; the facade's snapshot intent view | The "commanded figures when active" columns need no new authority path |
| SQLite carries `schema_version` with transactional migrations; the energy ledger (v2) established the projection-table doctrine (day records are projections, not acts — the audit store is never overloaded) | `db/schema.py`; `sqlite.py` `SQLiteEnergyLedgerRepository` | A telemetry historian is the same doctrine: append-only sample rows, schema v3 |
| The scorecard persists daily kWh aggregates only | `energy_day` / `energy_baseline` (v2) | The kWh authority exists; the raw trend view does not |
| The console has no chart of any kind; Insights is HTML/CSS rows; `web/package.json` carries react + react-dom only, and the sole SVG in the tree is the Energy Flow view's static site-bus illustration | `web/package.json`; `web/src` | The History view is the console's FIRST chart surface; the chart-library question must be answered deliberately (§4.2) |

What this adds: a `TelemetryHistorian` application component in the fleet
loop that records a compact per-unit telemetry row at a configured cadence
(default 30 s) into the durable store (schema v3); hourly rollups plus a
bounded full-resolution retention window (§2.3); one read REST route
`GET /api/v1/history` with a PINNED server-side downsampling algorithm (§3.2);
a read-only MCP tool; a feature-detected snapshot `history_state`; and the
console History view (§4, the follow-up web agent's plan). Observability
only — no control path reads the historian, no authority derives from it,
nothing is restored from it at boot (§6).

## 1. The operator model (plain language first)

The view the operator asks for, in their language:

> **History — mid, last 24 h**
> SOC fell 97 % → 31 % through the evening, floor at 21:40, night charge lifts
> it from 00:02. Grid: exporting 14:10–16:30 (peak 2.9 kW), importing through
> the charge. Temps 21–29 °C. No samples 02:10–03:40 (controller down).
> 30 s samples · 98 % of the hour covered.

Rules that make it honest (the operator's own standing doctrine, applied):

1. A sample is a periodic photograph of the observation stream — one row per
   unit per `sample_interval_s` (default 30 s). Between samples nothing is
   claimed; the row is the latest observation at that tick, not an average.
2. **Gaps are absent rows, never interpolated.** Controller down, unit
   unreachable, or a stale latest-observation all produce NO row; the API
   reports the gap intervals and the charts render them as gaps (the
   scorecard's `integration_max_gap_s` doctrine and the roadmap's "missing or
   estimated intervals are visibly different from measured data", made
   structural).
3. Every row carries the observation's quality rollup; degraded periods
   render dimmed/marked, never silently clean (§2.2).
4. Nothing is zero-filled: an absent datum is `null` in the row and in the
   response (the `_telemetry_summary` doctrine verbatim).
5. History is never safety-authoritative. No gate, kernel check, adviser, or
   toggle reads it (§6). It is an observability surface with the same standing
   as the scorecard: a projection, not an act.

## 2. The historian

### 2.1 The tick, cadence, and the staleness guard

`energypod.application.history.TelemetryHistorian` composes into the fleet
cycle exactly like the energy accountant: one bounded, fully suppressed tick
per fleet cycle, AFTER the polls and the energy-accountant step and BEFORE
the kernel tick (the pinned ordering; observability can never delay renewal
or control). A failure anywhere inside the tick — repository busy, decode of
the projection, anything — is suppressed and survivable per cycle; the next
due sample is the retry, so the worst honest outcome is a gap of one
interval.

- **Cadence.** A unit is sampled when `sample_interval_s` has elapsed since
  that unit's last RECORDED row (per-unit last-sample clock, seeded at boot so
  the first tick after boot samples immediately — the restart gap is bounded
  by downtime plus one interval).
- **Timestamp.** `sampled_at` is the historian tick's own wall clock
  (`clock.wall_now()`), truncated to whole seconds, ONE timestamp shared by
  every unit sampled in that tick. This is deliberate: it makes per-timestamp
  fleet summation exact (§3.5) and keeps stored timestamps fixed-width and
  lexicographically sortable. The underlying evidence is at most one control
  period older than the tick (the row samples the actor's latest appended
  observation); that bound is covered by the staleness guard below.
- **Staleness guard.** The historian samples a unit's LATEST observation only
  if it is no older than `max(3 × timing.control_period_s,
  sample_interval_s)` (a module constant, not a config key). A unit whose
  polls keep failing keeps a stale "latest" in the repository — recording its
  frozen figures under a fresh timestamp would fabricate continuity. A stale
  latest contributes NO row: an honest gap, exactly like the scorecard's
  excluded windows.
- **No new frames.** The historian reads the observation port's
  `all_latest()` and the cycle's peeked authority — it issues no reads of its
  own and touches no transport (the accountant's pattern).
- **Batch append.** One transaction per tick: `BEGIN IMMEDIATE`,
  `executemany` of every due unit's row, `COMMIT` (the baseline-repository
  pattern). `PersistenceBusyError` is suppressed → a gap, never a crash, and
  never a delay to the kernel tick.

### 2.2 The row (schema v3)

```sql
CREATE TABLE IF NOT EXISTS telemetry_sample (
    unit_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,            -- UTC ISO-8601, second precision
    -- the 15 numeric observables (all NULL when absent, never zero-filled)
    system_soc_pct REAL, bms_soc_pct REAL, soh_pct REAL,
    battery_watts REAL, grid_power_w REAL, load_power_w REAL,
    pack_voltage_v REAL, pack_current_a REAL,
    cell_min_v REAL, cell_max_v REAL, cell_spread_mv REAL,
    temperature_min_c REAL, temperature_max_c REAL,
    dynamic_charge_limit_w REAL, dynamic_discharge_limit_w REAL,
    -- per-row metadata
    lifecycle TEXT NOT NULL,             -- UnitLifecycle value
    health_state TEXT,                   -- recovery monitor's derived state | NULL
    quality TEXT NOT NULL,               -- the row's quality rollup (below)
    commanded_source TEXT,               -- manual|agent|schedule|excess_adviser|night_adviser|optimizer | NULL
    commanded_direction TEXT,            -- charge|discharge|idle | NULL
    commanded_w INTEGER,                 -- that unit's authorized watts | NULL
    -- mode words (the night-writer archaeology set)
    debug_mode_w INTEGER, ctrl_mode_w INTEGER, work_mode_w INTEGER, run_mode_w INTEGER,
    PRIMARY KEY (unit_id, sampled_at)
) WITHOUT ROWID;
```

`WITHOUT ROWID` clusters the table on `(unit_id, sampled_at)` — the exact
access pattern of both append (right edge) and windowed query (range scan) —
so there is no secondary index to maintain.

**Field provenance.** Every numeric column projects from the observation by
the SAME derivations `_telemetry_summary` uses (`cell_min/max/spread` and
`temperature_min/max` are computed from the arrays; everything else is the
observation's own field). `health_state` is the recovery monitor's latest
per-unit projection (an injected read, nullable when no monitor is wired).
The mode words ride verbatim (`debug_mode_w`/`ctrl_mode_w`/`work_mode_w`/
`run_mode_w`) — `run_mode_w` is the night-writer discriminator (Remote PQ
under ANY writer), which is what makes a history window double as the
archaeology view of a cutover night (§6).

**The quality rollup, pinned precisely.** `quality` is the worst per-field
`DataQuality` over the row's CONTROL-RATE fields — exactly
`Observation.REQUIRED_SAFETY_QUALITY_FIELDS` (the nine: BMS SOC, battery
watts, pack V/I, both dynamic limits, cells, temperatures) plus
`grid_power_w`/`load_power_w` when the CT block is composed — with the
precedence `missing > bad > stale > suspect > good` (the scorecard's
`export_evidence` ordering). `system_soc_pct` and `soh_pct` are deliberately
EXCLUDED from the rollup: they ride the cold ring by design (the B1 advisory
demotion) and their staleness is documented behavior, not a degraded period —
marking every run-mode row "stale" would be noise that hides real
degradation. Their VALUES still record.

**The commanded triple.** `commanded_source`/`commanded_direction`/
`commanded_w` are the per-unit control context at sample time, wired from the
facts the fleet loop already holds: the per-unit winner set (intent
repository + arbiter ordering) for source/direction, and the cycle's peeked
authority for watts — the same figures the snapshot's `intent` view composes,
injected into the historian, never routed through the facade. `source` is
`manual|agent|schedule` for those intents; an OPTIMIZER intent is attributed
`excess_adviser` or `night_adviser` when the corresponding projection
(`adviser_state` / `night_charge_state`) claims the unit (both are
single-writer fleet-loop state), else the bare `optimizer`. All three columns
are NULL when no intent claims the unit — "we commanded nothing" is itself
the recorded fact (the foreign-objective detector's eligibility doctrine).
These columns are the SAMPLED PROJECTION at the cadence, never the record of
authority: the audit row is (§6).

**Not recorded, stated honestly.** Per-cell voltages and the full temperature
array (§3.6); per-field quality maps (the rollup plus the live view's own map
cover both timescales); cumulative energy counters and any kWh figure (the
scorecard owns those — §6); audit facts and decisions; intent or authorization
lifecycle beyond the sampled triple.

### 2.3 Retention: full-resolution window + hourly rollups (the math)

Honest sizing at the defaults (3 units, 30 s cadence):

| Quantity | Value | Derivation |
|---|---|---|
| Rows per unit per day | 2,880 | 86 400 s / 30 s |
| Rows per day (fleet) | 8 640 | × 3 units |
| Bytes per row | ≈ 200 B | 15 REAL (120 B) + 4 small ints + 5–6 short TEXT + header; `WITHOUT ROWID`, no secondary index; ~10–15 % page overhead |
| Full-resolution growth | ≈ 1.7 MB/day | 8 640 × 200 B |
| Full-resolution window (14 d default) | ≈ 24 MB | the retention bound |
| A YEAR at full resolution | ≈ 620 MB | why full-res is windowed, not kept |
| Hourly rollup row | ≈ 400 B | 15 fields × min/max/mean (45 REAL) + `sample_count` + `worst_quality` |
| Rollup growth | ≈ 29 KB/day (72 rows) | ≈ 10.5 MB/YEAR |
| Steady state, defaults | **≈ 25–40 MB** | 14 d full-res + rollups forever |

Write load is trivial by construction: one `executemany` of ≤ 3 rows every
30 s against the existing WAL database.

**The rollup table** (`telemetry_rollup_hourly`, `PRIMARY KEY (unit_id,
hour_start)`, `hour_start` in UTC — no DST ambiguity in storage; the console
renders site-local) carries, per numeric field, the hour's `min`/`max`/
`mean`, plus `sample_count` and `worst_quality` (the same precedence over the
hour's rows). An hour with zero samples writes NO rollup row — an absent hour
is a gap, never a zeroed hour. At the 30 s default a fully-covered hour holds
`sample_count` 120; the count IS the coverage honesty marker.

**Config keys** (§2.5) govern both tiers: the full-resolution window and the
rollup retention. Defaults: full-resolution 14 days; hourly rollups forever.

### 2.4 The maintenance pass (rollup + prune)

Rollup and prune run together, in ONE transaction, as a bounded suppressed
pass at two moments: once at boot (after the store opens) and on the first
historian tick after each site-local midnight (the accountant's rollover
moment, independently — no coupling to the accountant). The pass:

1. Aggregates every full-resolution hour OLDER than the retention horizon
   into `telemetry_rollup_hourly` (`INSERT ... ON CONFLICT DO NOTHING` —
   full-res rows are immutable, so the rollup is idempotent and a missed day
   is caught up at the next pass).
2. Deletes full-resolution rows strictly older than the horizon AND older
   than the newest rolled hour — prune happens only inside the same
   transaction that landed the rollups, so a failed rollup can never delete
   the only copy.

The repository port exposes this as `maintain(now)`; the historian owns the
cadence, the repository owns the transaction.

### 2.5 Config keys and composition

```yaml
plant_history:
  sample_interval_s: 30.0              # > timing.control_period_s, <= 3600
  retention_full_resolution_days: 14   # 1..3650
  retention_rollup_days: 0             # 0 = keep hourly rollups forever
```

Validation (config load, before composition): the numeric bounds;
`sample_interval_s > timing.control_period_s` cross-validated on
`ControllerConfig` where the timing block lives (the scorecard precedent). A
PRESENT block additionally REQUIRES the `storage` block — durable history is
the entire point, and a historian silently keeping its rows in memory of a
database-less deployment is exactly the invisible-off class this project
refuses (the config error says so). `energypod simulate` composes the
in-memory adapter regardless (explicitly non-durable, for scenario tests).

Composition (the block-presence doctrine verbatim): a PRESENT block composes
the historian into the fleet loop (the §2.1 slot), the query route, the MCP
tool, and the snapshot's feature-detected `history_state`; an ABSENT block
composes nothing — byte-identical snapshot, 409 on the route, no samples
taken. There is deliberately NO `enabled` key (the schedules/scorecard
doctrine: a second master switch is a second way to be silently off;
decommissioning removes the block).

Storage rides the EXISTING database path (the `storage` block's
`database_path`): no new file, no new volume; the schema v3 migration applies
on every open exactly as v2 did.

**Snapshot `history_state`** (feature-detected, absent when the block is
absent; composed on demand from the repository, one indexed read per unit):
`{"sample_interval_s": 30.0, "retention_full_resolution_days": 14,
"last_sample_at": {"mid": "2026-08-26T14:03:00+00:00", ...}}` — the live
"history is recording" hint the console keys on, with `null` for a unit not
yet sampled.

## 3. The query API

### 3.1 Route, parameters, errors

`GET /api/v1/history` — observe scope, read-only (no mutation exists on this
surface; nothing to confirm, nothing to idempotency-key). Query parameters:

| Parameter | Form | Default | Rules |
|---|---|---|---|
| `from`, `to` | ISO-8601 WITH explicit offset (`Z` or `±HH:MM`; naive = 422 — an implicit local zone is a silent lie) | required | `from < to`; window ≤ 31 days |
| `unit_ids` | comma-separated configured unit ids | all configured units | an unknown id is 422 `validation_error` (details name it) |
| `fields` | comma-separated from the §3.2 vocabulary | `bms_soc_pct,battery_watts,grid_power_w,temperature_min_c,temperature_max_c` | an unknown field is 422 (details name it) |
| `points` | integer | 600 | 50..2000 — the per-series downsample target |

Errors: the structured envelope throughout. 401/403 auth and scope as every
observe route; 422 `validation_error` for every parameter rule above (the
house shape — this surface deliberately uses NO bare 400; the schedules
route's 422 is the precedent); 409 `plant_history_not_commissioned` when the
config block is absent (the schedules/scorecard precedent verbatim). A window
entirely before the first recorded sample is a 200 with empty series and
`first_sample_at: null` — absence is data, not an error.

**Resolution selection (one resolution per response).** The repository names
its oldest retained full-resolution sample; `from` at or after that instant
answers `resolution: "full"` from `telemetry_sample`, otherwise the WHOLE
window answers `resolution: "hourly"` from the rollups. The boundary is
data-driven (real availability), not config-coupled, so a missed maintenance
pass changes nothing the rows cannot support. The cost is stated honestly: a
window opening before the horizon serves hourly for its entirety even where
newer full-res rows exist — a mixed-resolution series would be a chart that
changes shape mid-window, which is worse. The console labels the resolution
(§4.1).

### 3.2 Downsample: the pinned algorithm

**LTTB** (Largest-Triangle-Three-Buckets, Steinarsson's algorithm) per series
over the window's rows ordered by time, `N = points`, with these pins:

- Every emitted point is a REAL stored sample — its exact stored timestamp
  and value, never a synthesized mean or a re-bucketed figure. (This is why
  LTTB over bucket-mean aggregation: bucket min/max/mean fabricates values
  that never existed as samples, and a mean flattens exactly the charge
  bursts and SOC steps the operator will be looking for.)
- The first and last samples of the window are always retained (no edge
  erosion).
- Deterministic: no clock or hash-order dependence; ties break toward the
  EARLIER sample.
- Because visual fidelity is not extremum preservation, each series
  additionally carries `window_min`/`window_min_at`/`window_max`/
  `window_max_at` and the window's `sample_count`, computed over EVERY row in
  the window (the scan that feeds LTTB collects them for free). The console
  can annotate "peak 2,510 W at 01:23" even when the peak sample did not
  survive downsampling.

Hourly resolution does not downsample further (a 31-day window is ≤ 744
hourly points — inside the `points` cap naturally); each hourly point carries
its own `min`/`max`/`n` so the console draws an honest band rather than a
false line.

**The field vocabulary** (one word per series; the §2.2 columns):
`soc_pct` (the advisory system figure), `bms_soc_pct`, `soh_pct`,
`battery_watts`, `grid_power_w`, `load_power_w`, `pack_voltage_v`,
`pack_current_a`, `cell_min_v`, `cell_max_v`, `cell_spread_mv`,
`temperature_min_c`, `temperature_max_c`, `dynamic_charge_limit_w`,
`dynamic_discharge_limit_w` — plus three meta-fields that select compact
step encodings instead of point series: `lifecycle`, `health_state`,
`commanded`.

### 3.3 The response shape

```json
{
  "from": "2026-08-25T06:00:00+00:00",
  "to": "2026-08-26T06:00:00+00:00",
  "resolution": "full",
  "points": 720,
  "fields": ["bms_soc_pct", "battery_watts", "grid_power_w"],
  "units": {
    "mid": {
      "first_sample_at": "2026-08-25T06:00:30+00:00",
      "last_sample_at": "2026-08-26T05:59:30+00:00",
      "sample_count": 2871,
      "quality_worst": "stale",
      "gaps": [{"from": "2026-08-26T02:10:00+00:00",
                "to":   "2026-08-26T03:40:30+00:00"}],
      "series": {
        "battery_watts": {
          "window_min": -2503.0, "window_min_at": "2026-08-26T00:41:00+00:00",
          "window_max": 914.0,   "window_max_at": "2026-08-25T19:12:30+00:00",
          "points": [{"t": "2026-08-25T06:00:30+00:00", "v": -521.0}, "..."]
        }
      },
      "lifecycle_changes":  [{"t": "...", "v": "disarmed"}],
      "health_state_changes": [{"t": "...", "v": "healthy"}],
      "commanded_changes": [
        {"t": "...", "source": "night_adviser", "direction": "charge", "watts": 2500}
      ]
    },
    "rhs": {"...": "..."}, "lhs": {"...": "..."}
  },
  "fleet": {
    "series": {
      "grid_power_w": {"window_min": -2901.0, "...": "...",
                       "points": [{"t": "...", "v": -2870.0}]}
    },
    "gaps": [{"from": "...", "to": "..."}]
  }
}
```

- `series.<field>.points` at `resolution: "full"`: `{t, v}` pairs (LTTB).
- At `resolution: "hourly"`: `{t, v (mean), min, max, n}` per hour; a missing
  hour is simply absent from the array (a gap, never a zero).
- The step-encoded meta-fields arrive as CHANGE-POINT arrays (an entry only
  where the value differs from the previous sample; the first sample always
  present) — compact and exactly renderable as steps. `commanded_changes`
  entries carry the triple; the row set is "no intent claims the unit" when
  the first entry is absent, and the console renders that explicitly.
- Nulls propagate: a series whose field was absent in a sample skips that
  sample's point (a null-valued point is never emitted — an absent SOC is not
  SOC 0).
- Bounded by construction: `units ≤ fleet size`, `fields ≤ 18`, `points ≤
  2000` — the worst legal response is a few MB of JSON; the defaults (5
  fields, 600 points, 3 units) are ~15 k points ≈ ~1 MB.

### 3.4 Gaps

Server-computed, per unit. At full resolution: any interval between
consecutive rows longer than `3 × sample_interval_s` is reported as a gap
`{from, to}` (row-to-row; a missed single sample at 2× the cadence is a
missed sample, not an outage — the 3× line keeps noise out of the gap list).
At hourly resolution: any missing hour strictly between the first and last
rollup hours of the window is a gap. Leading and trailing emptiness (before
the first sample / after the last) is NOT a gap list entry — the
`first_sample_at`/`last_sample_at` bounds already say it, and the console
renders empty space beyond them rather than implying an outage.

### 3.5 Fleet sums

The `fleet` block carries summed series for the three flow fields
(`grid_power_w`, `load_power_w`, `battery_watts`), computed server-side over
the request's effective unit set (all configured units when `unit_ids` is
absent). Summing downsampled series would lie (sum-after-downsample ≠
downsample-of-sum), so the sum runs over the RAW rows first, then LTTB
downsamples the summed series. A fleet point exists only where EVERY unit in
the set has a row at that timestamp (hour, at hourly resolution); otherwise
the point is absent — one unreadable phase is never treated as zero (the
export bound's fail-closed doctrine, applied to display). The fleet block's
`gaps` follow the same rule over the intersection.

### 3.6 What is excluded

- **Per-cell voltages.** 59–60 cells × 2 880 rows/day × 3 units ≈ half a
  million values per day — one field alone would dwarf everything else in
  this design. The cell-level history question is answered honestly, not
  dodged: min/max/spread ARE recorded (the imbalance trend is the operator's
  early-warning line, the roadmap's "imbalance trend"), and when a specific
  cell goes bad the evidence lives where it already lives — the live
  unit-detail cell array and the audit trail's decision-time facts. If
  per-cell history is ever wanted, it is a separate design with its own
  sampling tier and retention; it does not ride this one.
- **The full temperature array** — same arithmetic, same answer; min/max
  carry the thermal trend.
- **Any kWh or money figure** — the scorecard is the kWh authority (§6);
  history never duplicates it.
- **Safety authority of any kind** — no control path reads these tables; the
  historian is not consulted by the kernel, the advisers, any gate, or any
  toggle; nothing restores from it at boot. It is excluded from every
  completeness set and every fail-closed judgment in the system.

## 4. The console (plan for the follow-up web agent — NO implementation here)

### 4.1 The History view

A new top-level view (`history`), nav label "History", ALWAYS offered with an
honest not-commissioned state on the route's 409 (the pinned Schedule-view /
Insights / Objectives precedent in `web/src/app/views.ts`). No new controls
anywhere — the view is read-only by construction. All content
feature-detected on the route's 200.

- **Charts.** Per battery: SOC lines (`bms_soc_pct`, 0–100 axis, the
  authoritative figure; `soc_pct` overlayable), battery power as a filled
  area ± (charge negative, the house sign convention), grid exchange
  (import/export around zero), temperatures (min–max band). Fleet: the three
  summed flow series (§3.5). Cell spread available as an optional series.
- **Range pickers: 6 h / 24 h / 7 d / 30 d** (the pinned four; each maps to a
  `from/to` request with `to = now`, the window sliding live or refreshed on
  re-fetch — the web agent's call, stated in its own plan).
- **Gap rendering.** Gap intervals render as visibly empty/hatched regions
  with an accessible note ("no samples 02:10–03:40 — controller down or unit
  unreachable"); absent data is NEVER drawn as zero, and the space beyond
  `first/last_sample_at` stays empty rather than implying coverage.
- **Honesty markers.** A resolution badge ("30 s samples" vs "hourly
  rollup"); hourly points draw their min–max band with the sample count
  (`n`) surfaced on demand; degraded periods (rows whose `quality` rollup is
  non-good) render dimmed with the word `stale`/`bad`/`missing` in the
  accessible alternative; window extremes annotate peaks the downsample may
  not show; every chart carries a text/table alternative per UI_CONTRACTS
  (no color-only encoding, keyboard-operable, reduced-motion honored).
- **The step series** (`lifecycle`, `health_state`, `commanded`) render as
  labeled bands/step lines under the charts — the archaeology strip.

### 4.2 The chart-library call (pinned recommendation)

The console has never drawn a chart. The precedent usually cited for
"hand-roll SVG like the flow view" is thinner than it sounds: the Energy Flow
view is still a PLANNED view (`PLANNED_VIEWS`) and `web/src` contains no SVG
at all today — Insights renders HTML/CSS rows. History would therefore be a
FIRST chart implementation either way, which is precisely where a library
earns its dependency: multi-series time axes, four zoom ranges, filled areas,
min–max bands, gap regions, and hover readouts are weeks of edge cases the
project does not need to own (and every future chart — Energy Flow, imbalance
trends — inherits the slot).

**The hand-rolled precedent, weighed honestly.** The console's hand-rolled
SVG to date is the Energy Flow view's illustration — a small static
`viewBox` diagram of the site bus, drawn once, not a data chart. (At this
writing it sits uncommitted in `web/src/views/flow/`; whenever it lands, it
is still an illustration.) A hand-rolled TIME-SERIES chart is a different
class of problem: scales and ticks across four zoom ranges, min–max bands,
gap regions aligned to the time axis, hover crosshairs with readouts, and
resize — each one an edge case the console would then own forever. That is
exactly where a library earns its dependency, and every future chart
(Energy Flow's over-time half, imbalance trends) inherits the slot.

**Recommendation: add uPlot** (the current 1.6 line; MIT; ~50 KB minified,
zero dependencies). The decisive rationale for THIS project: uPlot is
framework-agnostic DOM, not a React-ecosystem component — there is no React
peer dependency to lag or break React 19 (the risk that has bitten
Recharts-class libraries during framework transitions), because the console
owns a thin `useEffect`-lifetimes wrapper (~100 lines) exactly like any other
imperative leaf. It is purpose-built for dense time-series (comfortably past
the 2 000-point cap here), and the server's pinned LTTB means the client
renders, never resamples — aliasing responsibility stays where the algorithm
is pinned.

**Fallback, pinned as a slot:** the design fixes the component boundary, not
the library — a `<TimeSeriesChart points gaps bands aria …>` slot in the
History view. If the operator prefers zero new dependencies, hand-rolled SVG
lands in the same slot (the honest cost stated above: the console then owns
axes, scales, zoom, bands, gaps, and hover itself). Recharts-class
declarative React chart libraries were considered and are NOT recommended:
roughly an order of magnitude larger, slower at thousands of points, and
their React-19 peer story is theirs, not ours.

## 5. Implementation plan (ordered; each slot its own red→green cycle)

Backend:

1. **H1 — Schema v3 + repository port** (1 slot): the migration (both tables)
   and `TelemetryHistoryRepository` — `append_samples(rows)`,
   `samples(unit_ids, from, to)` / `rollup_hours(unit_ids, from, to)`,
   `oldest_full_res_at()`, `last_sample_at()`, `maintain(now)`; memory and
   SQLite adapters. Red: the `tests/unit/test_repositories.py` history family
   — v2→v3 migration in place; append/query round-trip with nulls verbatim;
   `maintain` idempotence; rollup min/max/mean/count/worst-quality arithmetic
   (including the empty-hour-absent rule); prune-only-after-rollup (a forced
   rollup failure deletes nothing).
2. **H2 — The historian component** (1 slot): `TelemetryHistorian.tick` —
   cadence gate, staleness guard, the row projection (fields, the quality
   rollup's REQUIRED-set scoping and precedence, the commanded triple),
   first-sample-at-boot, batch append, per-cycle suppression. Red: NEW
   `tests/unit/test_history_historian.py` — cadence and boot-baseline
   vectors; stale-latest → no row; rollup precedence and the system-SOC/soh
   exclusion; nulls never zero; the commanded triple's source attribution
   (manual/schedule/excess/night/optimizer/none); a busy store is a survived
   gap; the tick never raises into the loop.
3. **H3 — Config + composition** (1 slot): the `plant_history:` block, the
   storage requirement, the fleet-loop slot (post-accountant, pre-kernel,
   bounded, suppressed), the boot + midnight maintenance wiring, the simulate
   memory adapter, the `history_state` snapshot key. Red: the
   `test_config.py` plant_history matrix (bounds, the control-period
   cross-validation, present-without-storage refused) and the
   `test_composition.py` plant_history family (present composes historian +
   route + key; absent byte-identical; tick ordering; maintenance moments).
4. **H4 — The query service** (1 slot): facade `get_plant_history` — bounds,
   resolution selection, LTTB (+window extremes), gap computation, fleet
   intersection sums, step encodings. Red: NEW
   `tests/unit/test_history_query.py` — LTTB pinned vectors (membership:
   every point exists in the input; endpoint retention; spike-preservation;
   tie-to-earlier determinism); resolution boundary at the data horizon; gap
   math at both resolutions; the fleet all-present rule; window bounds.
5. **H5 — REST + MCP** (1 slot): the route and the read-only
   `get_plant_history` MCP tool. Red: `tests/api/test_rest_contract.py`
   history family (shape, the 422 matrix, 409 not-commissioned, observe
   scope, empty-window 200) + `tests/api/test_mcp_contract.py`.
6. **H6 — Simulator scenario + golden + docs** (1 slot): a 48 h compressed
   scripted scenario (a night-charge-shaped charge, a controller-down gap, a
   degraded-quality stretch) walked end to end — rows → maintenance rollups →
   query — against EXACT expected series; the archaeology shape pinned
   (commanded triple beside measured watts through the charge window).
   CONTINUITY entry; the live-write example gains the commented block. Red:
   the `tests/simulator/` + `tests/golden/` history families.

Web (after H5), each 1 slot with named vitest families:

7. **W1 — Wire model + view skeleton**: the history client method, the
   view-model parsers, the History view's loading/empty/not-commissioned/
   error/disconnected states (the UI_CONTRACTS state set), range pickers.
8. **W2 — `<TimeSeriesChart>` + uPlot**: the slot, the wrapper, SOC and
   power charts at 6 h/24 h; the accessible table alternative; reduced
   motion.
9. **W3 — Honesty surfaces**: grid/temperature/fleet charts; 7 d/30 d and
   the hourly-rollup badge + bands; gap regions; degraded-period dimming;
   the lifecycle/health/commanded step strip.

## 6. Interplay: the scorecard, the advisers, the audit trail

- **The energy scorecard stays the kWh authority.** Daily bought/sold/
  charged/discharged/load/coverage figures come from `energy_day` — history
  never stores or serves an energy aggregate. The two surfaces cross-link
  and never duplicate: the console's History view points at Insights for the
  day's kWh ("the day's account lives in Insights"), and Insights never
  grows a trend chart that would re-derive what history serves. Different
  questions, different authorities: "how much moved" (scorecard, finalized
  once per site-day) vs "what it looked like" (history, sampled and
  windowed).
- **The night-charge and excess projections appear in the commanded columns
  when active** — which is what makes history the archaeology view of a
  cutover, later: on the last Docker night, rows read `commanded_* = NULL`
  with measured `battery_watts ≈ −2500` and `run_mode_w = 1` (a writer on the
  wire we did not command); from the first night-charge night, rows read
  `source = night_adviser, direction = charge, watts = 2500` beside the
  measured watts converging on them. The before/after evidence of
  DESIGN_NIGHT_CHARGE §3.4's stand-down, in one chart, with the
  night-writer detector's classifications as the corroborating strip.
- **The audit trail owns decisions.** A history row's commanded figures are
  the 30 s sampled projection of control context — where the two ever
  disagree, the audit row is the record and history is the rendering. The
  historian therefore writes no audit facts of its own (samples are
  projections, not acts — the ledger doctrine), and no bus event exists for
  sampling (the console re-queries; a 2 880-row/day heartbeat would be
  noise).
- **Never safety-authoritative, never restored.** No control path, gate,
  adviser, or toggle reads the historian; boot is observe-only and restores
  nothing from these tables; the historian cannot delay the kernel tick
  (bounded and suppressed, the accountant's slot discipline); and its
  failure modes are gaps — the honest degradation — never blocks, refusals,
  or crashes.

## 7. Operator decisions this package needs (verbatim-ready)

1. **Commission the block:** "Add the `plant_history` block to the controller
   config (it needs the existing database path) and restart — the History
   view appears, recording starts immediately, and the schema migrates on
   boot."
2. **Retention defaults:** "Keep 30 s samples for 14 days plus hourly rollups
   forever — about 25–40 MB steady state. Say the word to widen the full-
   resolution window (each extra week ≈ 12 MB) or shorten it."
3. **Cadence:** "30 s (2,880 rows per battery per day) is the default. 15 s
   doubles the storage and catches shorter transients; 60 s halves both."
4. **The chart-library call:** "The console gets its first chart. The design
   recommends uPlot — a ~50 KB dependency-free time-series library wrapped
   in ~100 lines of our own React, chosen because it cannot break with React
   versions. If you would rather have zero new dependencies, the same view
   ships with hand-drawn SVG charts instead; the design's component slot
   takes either."
5. **Not needed from you:** no acknowledgement, no partition interaction, no
   arming — history is passive. It will simply be watching (and recording)
   the night-charge cutover when you run it.
