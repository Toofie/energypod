/**
 * Behavior contract for the night-charge wire model (DESIGN_NIGHT_CHARGE.md §5
 * + API_CONTRACTS.md "Off-peak night charge"): the `night_charge_state`
 * projection parse, the `night_charge.state_changed` patch semantics, and the
 * plain-word maps the tile and the shell share.
 *
 * PENDING-BACKEND: every fixture mirrors the contract's pinned shapes — the
 * §5 illustrative night is the default — so when the backend lands, these
 * parses are already speaking the wire's words. Absent = feature-absent
 * (nothing renders); a present-but-unusable datum falls back honestly, never
 * to a fabricated figure.
 */
import { describe, expect, it } from "vitest";
import {
  NIGHT_CHARGE_STATE_CHANGED_EVENT,
  NIGHT_PHASES,
  NIGHT_REASON_CODES,
  NIGHT_SUGGEST_BANNER_TEXT,
  NIGHT_UNIT_PHASES,
  fleetTargetSocPct,
  nightDecompositionText,
  nightDemandText,
  nightExplanationText,
  nightForecastFallbackText,
  nightMorningNoticeText,
  nightPhaseAnnouncement,
  nightPhaseText,
  nightReasonText,
  nightStatusText,
  nightTargetLineText,
  nightToggleStateText,
  nightTrustText,
  nightUnitRowText,
  nightWindowLineText,
  nextWindowInText,
  patchNightChargeState,
  toNightChargeState,
  toNightChargeStateChangedEvent,
  windowEndsInText,
  type NightChargeState,
} from "./nightCharge";
import { NIGHT_CHARGE_STATE_CHANGED } from "../test/wire";
import {
  nightChargeState,
  nightChargeStateChanged,
  nightForecastFallback,
  nightForecastOk,
  nightMorningNotice,
  nightTrust,
  nightUnitState,
} from "../test/wire";

// A fixed clock the countdown assertions derive from: the §5 example's own
// tick (2026-08-27T01:31:05+10:00) — the window ends 5341 s later by the
// projection's own figure, and the derived countdown must agree.
const NOW_MS = Date.parse("2026-08-27T01:31:05+10:00");

describe("nightCharge — the projection parse", () => {
  it("parses the §5 illustrative projection field for field", () => {
    const state = toNightChargeState(nightChargeState());
    expect(state).not.toBeNull();
    expect(state!.enabled).toBe(true);
    expect(state!.enabledOrigin).toBe("runtime");
    expect(state!.acknowledgedPartition).toBe(true);
    expect(state!.posture).toBe("partition");
    expect(state!.active).toBe(true);
    expect(state!.phase).toBe("pacing");
    expect(state!.window).toEqual({
      startLocal: "00:00",
      endLocal: "06:00",
      timezone: "Australia/Brisbane",
    });
    expect(state!.windowEndsAt).toBe("2026-08-27T06:00:00+10:00");
    expect(state!.windowEndsInS).toBe(5341);
    expect(state!.nextWindowAt).toBeNull();
    expect(state!.pacing).toBe("cap_first");
    expect(state!.rateCapW).toBe(2500);
    expect(state!.holdRateW).toBe(100);
    expect(state!.demandScope).toBe("fleet");
    expect(state!.demandThresholdW).toBe(1000);
    // ADDITIVE (2026-08-26): the resume gap rides the projection; an absent
    // key (an older frame) parses as 0 = "unknown" and the words fall back.
    expect(state!.demandExitHysteresisW).toBe(0);
    expect(
      toNightChargeState(nightChargeState({ demand_exit_hysteresis_w: 200 }))!
        .demandExitHysteresisW,
    ).toBe(200);
    expect(state!.demandW).toBe(412);
    expect(state!.demandEvidence).toBe("good");
    expect(state!.heldIntentId).toBe("night-881-77123.445101");
    expect(state!.units).toEqual([
      { unitId: "lhs", socPct: 71.4, phase: "pacing", targetW: 2500, reason: "on_plan", targetSocPct: null },
      { unitId: "mid", socPct: 88, phase: "pacing", targetW: 2500, reason: "on_plan", targetSocPct: null },
      { unitId: "rhs", socPct: 98, phase: "skipped_full", targetW: 0, reason: "at_ceiling", targetSocPct: null },
    ]);
    expect(state!.lastAction).toBe("renew");
    expect(state!.lastTickAt).toBe("2026-08-27T01:31:05+10:00");
    expect(state!.reasonCodes).toEqual(["window_open", "on_plan"]);
    // The V2 keys are ABSENT on the v1 wire (the additive law): the honest
    // defaults keep the parse exactly v1 — the posture is `full`, nothing else
    // of the forecast story exists.
    expect(state!.targetPolicy).toBe("full");
    expect(state!.trust).toBeNull();
    expect(state!.forecast).toBeNull();
    expect(state!.explanation).toBeNull();
    expect(state!.morningNotice).toBeNull();
  });

  it("narrows to null for a non-object — a garbage frame is never half-adopted", () => {
    expect(toNightChargeState(null)).toBeNull();
    expect(toNightChargeState("pacing")).toBeNull();
    expect(toNightChargeState([])).toBeNull();
  });

  it("falls back honestly on absent or unusable fields, never to a fabricated figure", () => {
    const state = toNightChargeState({});
    expect(state).not.toBeNull();
    expect(state!.enabled).toBe(false);
    expect(state!.enabledOrigin).toBe("config");
    expect(state!.acknowledgedPartition).toBe(false);
    expect(state!.active).toBe(false);
    expect(state!.phase).toBe("idle");
    expect(state!.window).toEqual({ startLocal: "00:00", endLocal: "06:00", timezone: "" });
    expect(state!.windowEndsAt).toBeNull();
    expect(state!.windowEndsInS).toBeNull();
    expect(state!.nextWindowAt).toBeNull();
    expect(state!.demandExitHysteresisW).toBe(0);
    expect(state!.demandW).toBeNull();
    expect(state!.demandEvidence).toBe("missing");
    expect(state!.heldIntentId).toBeNull();
    expect(state!.units).toEqual([]);
    expect(state!.reasonCodes).toEqual([]);
  });

  it("drops a unit row without a usable unit_id, never the whole projection", () => {
    const state = toNightChargeState({
      units: [nightUnitState(), { no_id: true }, { unit_id: "", phase: "pacing" }],
    });
    expect(state!.units).toEqual([
      { unitId: "lhs", socPct: 71.4, phase: "pacing", targetW: 2500, reason: "on_plan", targetSocPct: null },
    ]);
  });
});

describe("nightCharge — the state_changed patch", () => {
  it("keeps the tick bookkeeping the payload does not carry (§5: subset minus last_action/last_tick_at)", () => {
    const base = toNightChargeState(nightChargeState())!;
    const patched = patchNightChargeState(base, {
      phase: "holding_on_demand",
      demand_w: 2340,
      demand_evidence: "good",
      reason_codes: ["demand_above_threshold"],
      units: [
        nightUnitState({ phase: "holding_on_demand", target_w: 100, reason: "demand_above_threshold" }),
      ],
      heartbeat: true,
    });
    expect(patched!.phase).toBe("holding_on_demand");
    expect(patched!.demandW).toBe(2340);
    // The payload carries neither field: the snapshot's own values survive.
    expect(patched!.lastAction).toBe("renew");
    expect(patched!.lastTickAt).toBe("2026-08-27T01:31:05+10:00");
  });

  it("treats an explicit null demand as the real fail-closed answer, an absent key as inherit", () => {
    const base = toNightChargeState(nightChargeState())!;
    const nulled = patchNightChargeState(base, { demand_w: null, demand_evidence: "stale" });
    expect(nulled!.demandW).toBeNull();
    const inherited = patchNightChargeState(base, { demand_evidence: "stale" });
    expect(inherited!.demandW).toBe(412);
  });

  it("changes nothing for a non-object payload, and the event parse carries the heartbeat flag", () => {
    const base = toNightChargeState(nightChargeState())!;
    expect(patchNightChargeState(base, "nope")).toBe(base);
    const event = toNightChargeStateChangedEvent(base, {
      phase: "complete",
      heartbeat: true,
    });
    expect(event!.heartbeat).toBe(true);
    expect(event!.state.phase).toBe("complete");
    expect(toNightChargeStateChangedEvent(base, 42)).toBeNull();
  });

  it("shares the one bus vocabulary with the wire fixtures (two sources, one word)", () => {
    expect(NIGHT_CHARGE_STATE_CHANGED_EVENT).toBe(NIGHT_CHARGE_STATE_CHANGED);
  });
});

describe("nightCharge — the plain-word maps", () => {
  it("words the pacing story with every battery's own target (the design's own sentence shape)", () => {
    const state = toNightChargeState(
      nightChargeState({
        units: [
          nightUnitState({ unit_id: "mid", soc_pct: 88, target_w: 1900 }),
          nightUnitState({ unit_id: "lhs", soc_pct: 71.4, target_w: 1200 }),
          nightUnitState({ unit_id: "rhs", soc_pct: 98, phase: "skipped_full", target_w: 0, reason: "at_ceiling" }),
        ],
      }),
    )!;
    expect(nightPhaseText(state)).toBe(
      "Charging toward full by 06:00: mid 1,900 W · lhs 1,200 W · rhs full, sitting out.",
    );
  });

  it("words the demand hold as the no-cycling guarantee, with the reading's own figure", () => {
    const held = toNightChargeState(
      nightChargeState({
        phase: "holding_on_demand",
        demand_w: 2340,
        reason_codes: ["demand_above_threshold"],
        units: [nightUnitState({ phase: "holding_on_demand", target_w: 100, reason: "demand_above_threshold" })],
      }),
    )!;
    expect(nightPhaseText(held)).toBe(
      "Holding — house demand 2,340 W: batteries neither drain nor cycle while the grid meets the spike.",
    );
    // The ONE demand code rides both arms; the row's phase names which — the
    // hold never claims a demand figure the evidence did not supply.
    expect(nightUnitRowText(held.units[0]!)).toBe(
      "lhs — 71.4% charged · lhs held at 100 W (the demand reading did not hold)",
    );
    // Under failed evidence the hold is LOUD, never a silent zero-filled figure.
    const blind = toNightChargeState(
      nightChargeState({
        phase: "holding_on_demand",
        demand_w: null,
        demand_evidence: "stale",
        reason_codes: ["demand_evidence_stale"],
      }),
    )!;
    expect(nightPhaseText(blind)).toBe(
      "Holding — the demand reading is stale: batteries neither drain nor cycle while the grid meets the spike.",
    );
  });

  it("words the stand-by story: the grid serves the heavy load, the resume bound named in watts", () => {
    const standing = toNightChargeState(
      nightChargeState({
        phase: "standing_by_on_demand",
        demand_w: 2340,
        demand_exit_hysteresis_w: 200,
        reason_codes: ["demand_above_threshold"],
        units: [nightUnitState({ phase: "standing_by_on_demand", soc_pct: 88, target_w: 100, reason: "demand_above_threshold" })],
      }),
    )!;
    expect(nightPhaseText(standing)).toBe(
      "Standing by — house demand 2,340 W: the grid serves the heavy load and charging resumes below 800 W.",
    );
    // The per-unit row words the stand-down hold at its own rate — zero
    // discharge, never a zero-watt sit-out spelling.
    expect(nightUnitRowText(standing.units[0]!)).toBe(
      "lhs — 88% charged · lhs standing by at 100 W (house demand high)",
    );
    // The resume bound is stated in watts ONLY from wire figures: a frame
    // without the hysteresis key falls back to words, never a guessed number.
    expect(
      nightPhaseText(
        toNightChargeState(
          nightChargeState({
            phase: "standing_by_on_demand",
            demand_w: null,
            demand_evidence: "good",
            reason_codes: ["demand_above_threshold"],
          }),
        )!,
      ),
    ).toBe(
      "Standing by — the demand reading is good: the grid serves the heavy load and charging resumes once it falls back.",
    );
  });

  it("words the true standby: the parked row names its own silence and its own cause", () => {
    const parked = toNightChargeState(
      nightChargeState({
        units: [
          nightUnitState({ unit_id: "mid", soc_pct: 88, phase: "standing_by_parked", target_w: 0, reason: "night_standby_parked" }),
        ],
      }),
    )!;
    // The zero-watt target is real but unsaid: the phrase names what a park
    // IS (no submission, no answer), and the row's reason names why (its OWN
    // circuit, never the fleet reading).
    expect(nightUnitRowText(parked.units[0]!)).toBe(
      "mid — 88% charged · mid standing by (parked) — answers nothing (its own circuit ran heavy)",
    );
  });

  it("words the six true-standby codes, each naming its consequence (and the operator's part)", () => {
    const of = (code: string) =>
      nightReasonText(toNightChargeState(nightChargeState({ phase: "idle", reason_codes: [code] }))!);
    expect(of("night_standby_parked")).toBe(
      "A battery whose own circuit ran heavy is parked in standby — it answers neither charge nor discharge until its load falls back.",
    );
    expect(of("night_standby_park_refused")).toBe(
      "The park was refused — the battery keeps charging at the trickle hold instead of standing by; repeated refusals hold it there for the window.",
    );
    expect(of("night_standby_release_failed")).toBe(
      "Releasing a parked battery failed — it stays parked while the controller retries; the grid still serves its load.",
    );
    expect(of("night_standby_release_unverified")).toBe(
      "The release could not be verified on the battery — it stays treated as parked for the rest of the window, and no further writes are attempted.",
    );
    expect(of("night_standby_rearm_failed")).toBe(
      "The battery left standby but could not be re-armed — arm it by hand; while disarmed it can neither charge nor discharge.",
    );
    expect(of("night_standby_adopted")).toBe(
      "The controller found a standby park it placed before the restart and owns it again — the battery stays parked until its load falls back.",
    );
  });

  it("words completion with the window's own end clock", () => {
    const done = toNightChargeState(
      nightChargeState({
        phase: "complete",
        active: false,
        held_intent_id: null,
        window_ends_at: "2026-08-27T04:12:00+10:00",
        window_ends_in_s: 0,
        reason_codes: ["target_reached"],
        units: [
          nightUnitState({ phase: "complete", target_w: 0, reason: "target_reached" }),
        ],
      }),
    )!;
    expect(nightPhaseText(done)).toBe("Batteries full — window complete at 04:12.");
    // An unreadable end instant omits the clock rather than inventing one.
    expect(
      nightPhaseText(toNightChargeState(nightChargeState({ phase: "complete", window_ends_at: null }))!),
    ).toBe("Batteries full — window complete.");
  });

  it("words the skipped window: nothing needed charging", () => {
    const skipped = toNightChargeState(
      nightChargeState({
        phase: "skipped_full",
        active: false,
        held_intent_id: null,
        reason_codes: ["at_ceiling"],
        units: [nightUnitState({ phase: "skipped_full", target_w: 0, reason: "at_ceiling" })],
      }),
    )!;
    expect(nightPhaseText(skipped)).toBe(
      "Batteries were already full — nothing to charge this window.",
    );
  });

  it("words the inactive states from the FIRST reason code, honestly per code", () => {
    const outside = toNightChargeState(
      nightChargeState({
        enabled: false,
        active: false,
        phase: "idle",
        window_ends_at: null,
        window_ends_in_s: null,
        next_window_at: "2026-08-28T00:00:00+10:00",
        reason_codes: ["outside_window"],
      }),
    )!;
    expect(nightReasonText(outside)).toBe(
      "Outside the charging window (00:00–06:00) — the next window opens at 00:00.",
    );
    expect(
      nightReasonText(
        toNightChargeState(
          nightChargeState({
            enabled: false,
            enabled_origin: "config",
            active: false,
            phase: "idle",
            reason_codes: ["disabled_by_config"],
          }),
        )!,
      ),
    ).toBe("Night charging is off (config).");
    expect(
      nightReasonText(
        toNightChargeState(
          nightChargeState({
            enabled: false,
            enabled_origin: "runtime",
            active: false,
            phase: "idle",
            reason_codes: ["disabled_by_runtime"],
          }),
        )!,
      ),
    ).toBe(
      "Night charging is off until the controller restarts — the config's own setting takes over again at boot.",
    );
    expect(
      nightReasonText(
        toNightChargeState(
          nightChargeState({
            enabled: false,
            active: false,
            phase: "idle",
            reason_codes: ["night_acknowledgement_required"],
          }),
        )!,
      ),
    ).toBe(
      "Waiting on the one-time night-partition acknowledgement before night charging can start.",
    );
  });

  it("renders the units_disarmed sentence as the arm instruction it is", () => {
    const disarmed = toNightChargeState(
      nightChargeState({
        phase: "idle",
        active: false,
        held_intent_id: null,
        reason_codes: ["units_disarmed"],
        units: [nightUnitState({ phase: "sitting_out", target_w: 0, reason: "units_disarmed" })],
      }),
    )!;
    expect(nightReasonText(disarmed)).toMatch(/^The batteries are disarmed — arm them/);
    expect(nightReasonText(disarmed)).toMatch(/cannot arm itself/);
  });

  it("names an unknown reason code verbatim, never silently dropped", () => {
    const odd = toNightChargeState(
      nightChargeState({ phase: "idle", reason_codes: ["some_future_code"] }),
    )!;
    expect(nightReasonText(odd)).toBe("Night charging is standing down (some_future_code).");
  });

  it("an enabled window state speaks its phase; everything else speaks its reason", () => {
    const pacing = toNightChargeState(nightChargeState())!;
    expect(nightStatusText(pacing)).toBe(nightPhaseText(pacing));
    const idle = toNightChargeState(
      nightChargeState({ enabled: false, phase: "idle", reason_codes: ["outside_window"] }),
    )!;
    expect(nightStatusText(idle)).toBe(nightReasonText(idle));
  });

  it("carries the demand reading with its threshold and its evidence word, never zero-filled", () => {
    expect(nightDemandText(toNightChargeState(nightChargeState())!)).toBe(
      "House demand 412 W · holds above 1,000 W · reading good",
    );
    expect(
      nightDemandText(
        toNightChargeState(nightChargeState({ demand_w: null, demand_evidence: "missing" }))!,
      ),
    ).toBe("House demand not available · holds above 1,000 W · reading missing");
  });

  it("words one per-battery plan row: its own charge figure, target, and reason", () => {
    expect(
      nightUnitRowText(
        toNightChargeState(nightChargeState())!.units[0]!,
      ),
    ).toBe("lhs — 71.4% charged · lhs 2,500 W (on plan)");
    expect(
      nightUnitRowText(
        toNightChargeState(nightChargeState())!.units[2]!,
      ),
    ).toBe("rhs — 98% charged · rhs full, sitting out (at the charge ceiling)");
  });

  it("words the window countdowns from the projection's own instants", () => {
    const open = toNightChargeState(nightChargeState())!;
    // From the §5 tick (01:31:05) to the window's end (06:00) is 4 h 28 min —
    // the INSTANT is the derivation (it ticks with the caller's clock); the
    // carried window_ends_in_s figure is only the fallback. (The design's own
    // illustrative 5341 s disagrees with its illustrative last_tick_at; the
    // console derives from the instant, never the stale figure.)
    expect(windowEndsInText(open, NOW_MS)).toBe("4 h 28 min");
    expect(nightWindowLineText(open, NOW_MS)).toBe(
      "Window 00:00–06:00 ends 06:00 (in 4 h 28 min)",
    );
    const outside = toNightChargeState(
      nightChargeState({
        phase: "idle",
        active: false,
        window_ends_at: null,
        window_ends_in_s: null,
        next_window_at: "2026-08-28T00:00:00+10:00",
      }),
    )!;
    // 22 h 28 min 55 s from the §5 tick to the next midnight open.
    expect(nextWindowInText(outside, NOW_MS)).toBe("22 h 28 min");
    expect(nightWindowLineText(outside, NOW_MS)).toBe(
      "Window 00:00–06:00 — next opens 00:00 (in 22 h 28 min)",
    );
    expect(
      nightWindowLineText(
        toNightChargeState(
          nightChargeState({ phase: "idle", window_ends_at: null, next_window_at: null }),
        )!,
        NOW_MS,
      ),
    ).toBeNull();
  });

  it("distinguishes the toggle's four origin states (the honest until-restart marker)", () => {
    const of = (spec: Parameters<typeof nightChargeState>[0]): NightChargeState =>
      toNightChargeState(nightChargeState(spec))!;
    expect(nightToggleStateText(of({ enabled: true, enabled_origin: "config" }))).toBe("On (config)");
    expect(nightToggleStateText(of({ enabled: true, enabled_origin: "runtime" }))).toBe(
      "On — until restart",
    );
    expect(nightToggleStateText(of({ enabled: false, enabled_origin: "config" }))).toBe("Off (config)");
    expect(nightToggleStateText(of({ enabled: false, enabled_origin: "runtime" }))).toBe(
      "Off — until restart",
    );
  });

  it("announces phase transitions, never a same-phase republish", () => {
    expect(nightPhaseAnnouncement("idle", "pacing")).toBe(
      "Night charging started — pacing toward full.",
    );
    expect(nightPhaseAnnouncement("holding_on_demand", "pacing")).toBe(
      "Night charging resumed — pacing toward full.",
    );
    expect(nightPhaseAnnouncement("pacing", "standing_by_on_demand")).toBe(
      "Night charging is standing by — house demand is high; the grid serves the heavy load while the batteries stand down.",
    );
    expect(nightPhaseAnnouncement("standing_by_on_demand", "pacing")).toBe(
      "Night charging resumed — pacing toward full.",
    );
    // A hold is the fail-closed fallback: the announcement names the failed
    // reading, never a demand figure it does not have.
    expect(nightPhaseAnnouncement("pacing", "holding_on_demand")).toBe(
      "Night charging is holding — the demand reading did not hold, so the batteries neither drain nor cycle.",
    );
    expect(nightPhaseAnnouncement("holding_on_demand", "complete")).toBe(
      "Night charging complete — the batteries are full.",
    );
    expect(nightPhaseAnnouncement("pacing", "idle")).toBe(
      "Night charging stood down — the batteries are back on their own.",
    );
    expect(nightPhaseAnnouncement("pacing", "pacing")).toBeNull();
  });

  it("announces the true standby's park and release through the unit rows while the fleet phase holds", () => {
    // A park rides NO fleet phase (the vocabulary is unchanged): with both
    // projections' rows passed, a same-phase frame whose parked set moved is
    // the announcement.
    const pacingRow = {
      unitId: "mid",
      socPct: 88,
      phase: "pacing",
      targetW: 2500,
      reason: "on_plan",
      targetSocPct: null,
    } as const;
    const parkedRow = {
      unitId: "mid",
      socPct: 88,
      phase: "standing_by_parked",
      targetW: 0,
      reason: "night_standby_parked",
      targetSocPct: null,
    } as const;
    expect(nightPhaseAnnouncement("pacing", "pacing", [pacingRow], [parkedRow])).toBe(
      "mid is parked in standby — answers neither charge nor discharge until house demand falls.",
    );
    // The exit names BOTH halves: the charging resumes AND the battery is
    // re-armed (a release without the re-arm is not the story's end).
    expect(nightPhaseAnnouncement("pacing", "pacing", [parkedRow], [pacingRow])).toBe(
      "mid left standby — charging resumes and the battery re-arms.",
    );
    // Two batteries park together: one sentence, plural verbs, named the
    // operator's way.
    expect(
      nightPhaseAnnouncement("holding_on_demand", "holding_on_demand", [], [
        parkedRow,
        { ...parkedRow, unitId: "rhs" },
      ]),
    ).toBe(
      "mid and rhs are parked in standby — answer neither charge nor discharge until house demand falls.",
    );
    // An unmoved parked set (a heartbeat's sibling) stays silent.
    expect(nightPhaseAnnouncement("pacing", "pacing", [parkedRow], [parkedRow])).toBeNull();
  });

  it("keeps the phase and unit-phase vocabularies whole (§5's ONE lists)", () => {
    expect(NIGHT_PHASES).toEqual([
      "idle",
      "pacing",
      "holding_on_demand",
      "standing_by_on_demand",
      "complete",
      "skipped_full",
    ]);
    expect(NIGHT_UNIT_PHASES).toEqual([
      "pacing",
      "holding_on_demand",
      "standing_by_on_demand",
      "standing_by_parked",
      "skipped_full",
      "complete",
      "sitting_out",
    ]);
    // The reason vocabulary is the ONE list (§5's eighteen + V2 §7's five
    // additions + the true standby's six lifecycle words); the wire fixtures
    // agree.
    expect(NIGHT_REASON_CODES).toHaveLength(29);
    for (const code of [
      "night_standby_parked",
      "night_standby_park_refused",
      "night_standby_release_failed",
      "night_standby_release_unverified",
      "night_standby_rearm_failed",
      "night_standby_adopted",
    ]) {
      expect(NIGHT_REASON_CODES).toContain(code);
    }
  });

  it("parses the parked row: an unknown-before phase rides the checklist, never narrowed away", () => {
    const state = toNightChargeState(
      nightChargeState({
        units: [
          nightUnitState({ unit_id: "mid", soc_pct: 88, phase: "standing_by_parked", target_w: 0, reason: "night_standby_parked" }),
        ],
      }),
    )!;
    expect(state.units[0]!.phase).toBe("standing_by_parked");
  });
});

// --- V2 (the forecast-aware target, DESIGN_NIGHT_CHARGE_V2 §7/§8) ---------------

describe("nightCharge — the V2 projection parse", () => {
  it("parses the §7 forecast-posture projection field for field", () => {
    const state = toNightChargeState(
      nightChargeState({
        target_policy: "forecast_act",
        trust: nightTrust(),
        forecast: nightForecastOk(),
        explanation:
          "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 finishes it (solcast p10, issued 2026-08-24T18:03:00+10:00)",
        morning_notice: nightMorningNotice(),
        units: [
          nightUnitState({ unit_id: "lhs", soc_pct: 61.8, target_soc_pct: 65.5, target_w: 2500 }),
          nightUnitState({ unit_id: "rhs", soc_pct: 68.9, phase: "complete", target_soc_pct: 65.5, target_w: 0, reason: "target_reached" }),
        ],
      }),
    )!;
    expect(state.targetPolicy).toBe("forecast_act");
    expect(state.trust).toEqual({
      state: "earned",
      daysScored: 19,
      requiredDays: 14,
      meanAbsErrPct: 21.4,
      biasPct: -4.2,
      lowSurplusDays: 5,
      highSurplusDays: 4,
    });
    expect(state.forecast).toEqual({
      status: "ok",
      source: "solcast",
      quantile: 0.1,
      issuedAt: "2026-08-24T18:03:00+10:00",
      fetchedAt: "2026-08-24T20:00:12+10:00",
      eSurplusKwh: 6,
      eDeficitKwh: 0.5,
      eCreditKwh: 4.9,
      middayLocal: "12:00",
      ceilingBoundBy: null,
    });
    expect(state.explanation).toBe(
      "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 finishes it (solcast p10, issued 2026-08-24T18:03:00+10:00)",
    );
    expect(state.morningNotice).toEqual({
      date: "2026-08-28",
      targetSocPct: 65.5,
      unitsBelowTarget: ["rhs"],
      untilLocal: "12:00",
    });
    // The per-unit target key, whichever posture named it.
    expect(state.units[0]!.targetSocPct).toBe(65.5);
    expect(state.units[1]!.targetSocPct).toBe(65.5);
    expect(fleetTargetSocPct(state)).toBe(65.5);
  });

  it("reads the suggested key under SUGGEST — the posture names the number's authority", () => {
    const state = toNightChargeState(
      nightChargeState({
        target_policy: "forecast_suggest",
        units: [nightUnitState({ suggested_target_soc_pct: 62 })],
      }),
    )!;
    expect(state.targetPolicy).toBe("forecast_suggest");
    expect(state.units[0]!.targetSocPct).toBe(62);
  });

  it("parses a fallback frame: the ladder's word, the quantile, the midday — and NOTHING else", () => {
    for (const status of [
      "forecast_missing",
      "forecast_stale",
      "forecast_no_load_baseline",
      "forecast_below_trust",
    ] as const) {
      const state = toNightChargeState(
        nightChargeState({ target_policy: "forecast_suggest", forecast: nightForecastFallback(status) }),
      )!;
      expect(state.forecast!.status).toBe(status);
      // No figures ride a fallback frame — there is nothing to misread.
      expect(state.forecast!.source).toBeNull();
      expect(state.forecast!.eCreditKwh).toBeNull();
      expect(state.forecast!.middayLocal).toBe("12:00");
    }
  });

  it("falls back honestly on present-but-unusable V2 data, never to a fabricated figure", () => {
    const state = toNightChargeState({
      target_policy: "forecast_act",
      trust: { state: "garbage", days_scored: "many" },
      forecast: { status: "nonsense" },
      explanation: 42,
      morning_notice: "still dark out",
      units: [{ unit_id: "lhs", soc_pct: 71.4, phase: "pacing", target_w: 2500, reason: "on_plan", target_soc_pct: "high" }],
    })!;
    expect(state.trust!.state).toBe("provisioning");
    expect(state.trust!.daysScored).toBe(0);
    // An unreadable status word is never half-adopted as a live computation.
    expect(state.forecast).toBeNull();
    expect(state.explanation).toBeNull();
    expect(state.morningNotice).toBeNull();
    expect(state.units[0]!.targetSocPct).toBeNull();
  });

  it("patches V2 facts by KEY PRESENCE: an absent key inherits, an explicit null wins", () => {
    const base = toNightChargeState(
      nightChargeState({
        target_policy: "forecast_suggest",
        trust: nightTrust({ state: "provisioning", days_scored: 6 }),
        forecast: nightForecastOk(),
        explanation: "lhs to 66% by 06:00",
        morning_notice: nightMorningNotice(),
      }),
    )!;
    // A v1-shaped payload (no V2 keys — the full-posture event) never erases
    // the snapshot's V2 facts.
    const v1Payload = patchNightChargeState(base, { phase: "holding_on_demand" })!;
    expect(v1Payload.targetPolicy).toBe("forecast_suggest");
    expect(v1Payload.forecast!.status).toBe("ok");
    expect(v1Payload.morningNotice!.date).toBe("2026-08-28");
    // The frame's own explicit nulls ARE the answer: the notice cleared at
    // midday, the forecast gone quiet outside the window.
    const cleared = toNightChargeStateChangedEvent(
      base,
      nightChargeStateChanged(44, {
        target_policy: "forecast_suggest",
        forecast: null,
        explanation: null,
        morning_notice: null,
        trust: nightTrust({ state: "earned" }),
        heartbeat: true,
      }).payload,
    )!.state;
    expect(cleared.forecast).toBeNull();
    expect(cleared.explanation).toBeNull();
    expect(cleared.morningNotice).toBeNull();
    expect(cleared.trust!.state).toBe("earned");
  });
});

describe("nightCharge — the V2 plain-word maps", () => {
  /** A SUGGEST frame with a live computation — the §7 example's own numbers. */
  function suggestFrame(over: Parameters<typeof nightChargeState>[0] = {}): NightChargeState {
    return toNightChargeState(
      nightChargeState({
        target_policy: "forecast_suggest",
        trust: nightTrust(),
        forecast: nightForecastOk(),
        explanation: "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 finishes it (solcast p10, issued 2026-08-24T18:03:00+10:00)",
        units: [
          nightUnitState({ unit_id: "lhs", soc_pct: 61.8, suggested_target_soc_pct: 65.5 }),
          nightUnitState({ unit_id: "mid", soc_pct: 64.0, suggested_target_soc_pct: 65.5 }),
        ],
        ...over,
      }),
    )!;
  }

  it("words the suggest line: the number, the arithmetic, and the 95-vs-100 clause", () => {
    expect(nightTargetLineText(suggestFrame())).toBe(
      "Suggested target: 65.5% — 4.9 kWh forecast surplus by 12:00 finishes it. Targets stop at the ceiling — the pods top the last few percent themselves.",
    );
    // Under ACT the same arithmetic names the governing number.
    expect(
      nightTargetLineText(suggestFrame({ target_policy: "forecast_act" })),
    ).toMatch(/^Target: 65\.5% — 4\.9 kWh forecast surplus by 12:00 finishes it\./);
  });

  it("renders no target line without a live computation — full, fallback, or figure-less", () => {
    expect(nightTargetLineText(toNightChargeState(nightChargeState())!)).toBeNull();
    expect(
      nightTargetLineText(
        suggestFrame({ forecast: nightForecastFallback("forecast_stale") }),
      ),
    ).toBeNull();
    expect(
      nightTargetLineText(suggestFrame({ units: [nightUnitState()] })),
    ).toBeNull();
  });

  it("pins the suggest banner verbatim (a number that does not govern says so beside itself)", () => {
    expect(NIGHT_SUGGEST_BANNER_TEXT).toBe(
      "Showing forecast targets — charging to 95% (v1) until trust is earned; promotion is a config revision",
    );
  });

  it("words every fallback rung with the v1 charge it lands on — never mysterious", () => {
    const of = (
      status: "forecast_missing" | "forecast_stale" | "forecast_no_load_baseline" | "forecast_below_trust",
    ) =>
      nightForecastFallbackText(
        toNightChargeState(
          nightChargeState({ target_policy: "forecast_suggest", forecast: nightForecastFallback(status) }),
        )!.forecast!,
      )!;
    expect(of("forecast_missing")).toBe(
      "No forecast covers this morning — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_stale")).toBe(
      "The forecast is too old to steer with — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_no_load_baseline")).toBe(
      "No load baseline for the morning — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_below_trust")).toBe(
      "Forecast trust has not been earned — charging full tonight (the v1 charge).",
    );
    // Every rung names the v1 charge (§3.3's umbrella, made testable).
    for (const sentence of [
      of("forecast_missing"),
      of("forecast_stale"),
      of("forecast_no_load_baseline"),
      of("forecast_below_trust"),
    ]) {
      expect(sentence).toMatch(/charging full tonight/);
    }
    expect(
      nightForecastFallbackText(
        toNightChargeState(
          nightChargeState({ target_policy: "forecast_act", forecast: nightForecastOk() }),
        )!.forecast!,
      ),
    ).toBeNull();
  });

  it("words A10's ceiling-bound decomposition: sky and netting in honest words", () => {
    const frame = (boundBy: "sky" | "netting" | null) =>
      toNightChargeState(
        nightChargeState({
          target_policy: "forecast_act",
          forecast: nightForecastOk({ ceiling_bound_by: boundBy }),
        }),
      )!.forecast!;
    expect(nightDecompositionText(frame("sky"))).toBe(
      "Charging to full — the sky gave no surplus worth leaving room for.",
    );
    expect(nightDecompositionText(frame("netting"))).toBe(
      "Charging to full — the morning deficit bound it, not the sky.",
    );
    expect(nightDecompositionText(frame(null))).toBeNull();
    expect(
      nightDecompositionText(
        toNightChargeState(
          nightChargeState({
            target_policy: "forecast_act",
            forecast: nightForecastFallback("forecast_missing"),
          }),
        )!.forecast!,
      ),
    ).toBeNull();
  });

  it("words the trust line as evidence, never a verdict below the required days", () => {
    const trust = (spec: Parameters<typeof nightTrust>[0]) =>
      toNightChargeState(
        nightChargeState({ target_policy: "forecast_suggest", trust: nightTrust(spec) }),
      )!.trust!;
    expect(nightTrustText(trust({}))).toBe(
      "Forecast trust: earned — 19/14 days scored · mean err 21.4% · bias -4.2% · 5 low / 4 high mornings.",
    );
    expect(
      nightTrustText(trust({ state: "provisioning", days_scored: 6, low_surplus_days: 2, high_surplus_days: 1 })),
    ).toBe(
      "Forecast trust: provisioning — 6/14 days scored · mean err 21.4% · bias -4.2% · 2 low / 1 high mornings (not a verdict until 14 days).",
    );
    expect(nightTrustText(trust({ state: "suspended", mean_abs_err_pct: 34.1, bias_pct: 12.4 }))).toBe(
      "Forecast trust: SUSPENDED — 19/14 days scored · mean err 34.1% · bias +12.4% · 5 low / 4 high mornings; charging full until the rolling window re-earns it.",
    );
    // A young scoreboard carries no mean or bias — the counts stand alone,
    // and the over-forecast direction keeps its plus sign.
    expect(
      nightTrustText(
        trust({ state: "provisioning", days_scored: 0, mean_abs_err_pct: null, bias_pct: null, low_surplus_days: 0, high_surplus_days: 0 }),
      ),
    ).toBe(
      "Forecast trust: provisioning — 0/14 days scored · 0 low / 0 high mornings (not a verdict until 14 days).",
    );
  });

  it("words the A5 morning notice: the honest close, carried until midday", () => {
    const notice = (spec: Parameters<typeof nightMorningNotice>[0]) =>
      toNightChargeState(
        nightChargeState({
          target_policy: "forecast_suggest",
          morning_notice: nightMorningNotice(spec),
        }),
      )!.morningNotice!;
    expect(nightMorningNoticeText(notice({}))).toBe(
      "Ended the night below target (rhs under 65.5%) — solar is finishing what it can; landing visible after midday (until 12:00).",
    );
    // A notice without units or a target still says the one sentence that matters.
    expect(
      nightMorningNoticeText(notice({ units_below_target: [], target_soc_pct: null, until_local: "" })),
    ).toBe("Ended the night below target — solar is finishing what it can; landing visible after midday.");
  });

  it("renders the projection's explanation verbatim with the forecast's age beside it", () => {
    const state = suggestFrame();
    const fetchedAt = Date.parse("2026-08-24T20:00:12+10:00");
    expect(nightExplanationText(state, fetchedAt + 26 * 60 * 1000)).toBe(
      "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 finishes it (solcast p10, issued 2026-08-24T18:03:00+10:00) (forecast fetched 26 min ago)",
    );
    // An unparseable or absent instant keeps the sentence alone, never an
    // invented age; a frame with no sentence renders nothing.
    expect(
      nightExplanationText(suggestFrame({ forecast: nightForecastOk({ fetched_at: null }) }), 0),
    ).toBe(state.explanation);
    expect(nightExplanationText(toNightChargeState(nightChargeState())!, 0)).toBeNull();
  });

  it("carries the target beside the SOC in the per-battery row (§8's target-vs-SOC)", () => {
    expect(
      nightUnitRowText(
        toNightChargeState(
          nightChargeState({
            target_policy: "forecast_act",
            units: [nightUnitState({ soc_pct: 61.8, target_soc_pct: 65.5 })],
          }),
        )!.units[0]!,
      ),
    ).toBe("lhs — 61.8% charged · target 65.5% · lhs 2,500 W (on plan)");
  });

  it("names the governing target in the ACT pacing story — and never in the SUGGEST one", () => {
    expect(nightPhaseText(suggestFrame({ target_policy: "forecast_act", phase: "pacing" }))).toBe(
      "Charging toward 65.5% (the rest by solar) by 06:00: lhs 2,500 W · mid 2,500 W.",
    );
    // Under SUGGEST the submission math is v1's (the ceiling): the story keeps
    // saying "full" and the suggested number says so beside itself instead.
    expect(nightPhaseText(suggestFrame({ phase: "pacing" }))).toBe(
      "Charging toward full by 06:00: lhs 2,500 W · mid 2,500 W.",
    );
  });

  it("words the five new reason codes in plain language (each fallback names the v1 charge)", () => {
    const of = (code: string, fixture: Parameters<typeof nightChargeState>[0] = {}) =>
      nightReasonText(toNightChargeState(nightChargeState({ phase: "idle", reason_codes: [code], ...fixture }))!);
    expect(of("forecast_missing")).toBe(
      "No forecast covers this morning — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_stale")).toBe(
      "The forecast is too old to steer with — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_no_load_baseline")).toBe(
      "No load baseline for the morning — charging full tonight (the v1 charge).",
    );
    expect(of("forecast_below_trust")).toBe(
      "Forecast trust has not been earned — charging full tonight (the v1 charge).",
    );
    expect(of("window_closed_below_target")).toBe(
      "The window closed below target — solar is finishing what it can.",
    );
  });
});
