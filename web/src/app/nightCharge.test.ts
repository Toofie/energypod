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
  NIGHT_UNIT_PHASES,
  nightDemandText,
  nightPhaseAnnouncement,
  nightPhaseText,
  nightReasonText,
  nightStatusText,
  nightToggleStateText,
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
import { nightChargeState, nightUnitState } from "../test/wire";

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
    expect(state!.demandW).toBe(412);
    expect(state!.demandEvidence).toBe("good");
    expect(state!.heldIntentId).toBe("night-881-77123.445101");
    expect(state!.units).toEqual([
      { unitId: "lhs", socPct: 71.4, phase: "pacing", targetW: 2500, reason: "on_plan" },
      { unitId: "mid", socPct: 88, phase: "pacing", targetW: 2500, reason: "on_plan" },
      { unitId: "rhs", socPct: 98, phase: "skipped_full", targetW: 0, reason: "at_ceiling" },
    ]);
    expect(state!.lastAction).toBe("renew");
    expect(state!.lastTickAt).toBe("2026-08-27T01:31:05+10:00");
    expect(state!.reasonCodes).toEqual(["window_open", "on_plan"]);
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
      { unitId: "lhs", socPct: 71.4, phase: "pacing", targetW: 2500, reason: "on_plan" },
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
    expect(nightPhaseAnnouncement("pacing", "holding_on_demand")).toBe(
      "Night charging is holding — house demand is high; the batteries neither drain nor cycle.",
    );
    expect(nightPhaseAnnouncement("holding_on_demand", "complete")).toBe(
      "Night charging complete — the batteries are full.",
    );
    expect(nightPhaseAnnouncement("pacing", "idle")).toBe(
      "Night charging stood down — the batteries are back on their own.",
    );
    expect(nightPhaseAnnouncement("pacing", "pacing")).toBeNull();
  });

  it("keeps the phase and unit-phase vocabularies whole (§5's ONE lists)", () => {
    expect(NIGHT_PHASES).toEqual([
      "idle",
      "pacing",
      "holding_on_demand",
      "complete",
      "skipped_full",
    ]);
    expect(NIGHT_UNIT_PHASES).toEqual([
      "pacing",
      "holding_on_demand",
      "skipped_full",
      "complete",
      "sitting_out",
    ]);
    // The reason vocabulary is the ONE list; the wire fixtures agree.
    expect(NIGHT_REASON_CODES).toHaveLength(18);
  });
});
