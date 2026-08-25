/**
 * Unit contract for the evening load-sharing wire model
 * (web/src/app/eveningShare.ts): the projection's narrowing (a garbage frame
 * is never half-adopted), the §8.3 field mapping, E9's within_tolerance
 * display-word definition, the plain-word maps (phase sentences, skip
 * reasons, the live line, the close line), and the alert story (the E6
 * degrade note, the evidence-withdraw fallback wording).
 */
import { describe, expect, it } from "vitest";
import {
  EVENING_PINNED_SENTENCE,
  EVENING_STATE_EVENT,
  EVENING_STOP_ROUTE,
  eveningAlertText,
  eveningCloseText,
  eveningLiveLine,
  eveningReasonText,
  eveningStatusText,
  toEveningShareState,
} from "./eveningShare";

function projection(spec: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    mode: "act",
    window: { opens_local: "16:00", ends_local: "22:30" },
    phase: "sharing",
    as_of: "2026-08-26T08:41:05+00:00",
    engaged: true,
    work_w: 1620,
    net_exchange_w: 90,
    elsewhere_w: 0,
    within_tolerance: true,
    commanded_total_w: 1408,
    derate: 1.16,
    reason_codes: ["on_plan"],
    units: [
      { unit_id: "lhs", soc_pct: 88.2, weight: 3878, share_w: 817, phase: "sharing", reason: "on_plan" },
    ],
    held_intent_id: "els-12-1000.5",
    ...spec,
  };
}

describe("toEveningShareState", () => {
  it("returns null for a garbage frame, never a half-adopted one", () => {
    expect(toEveningShareState(null)).toBeNull();
    expect(toEveningShareState("sharing")).toBeNull();
    expect(toEveningShareState(42)).toBeNull();
  });

  it("narrows the §8.3 shape with defaults that name themselves", () => {
    const state = toEveningShareState(projection())!;
    expect(state.mode).toBe("act");
    expect(state.window).toEqual({ opensLocal: "16:00", endsLocal: "22:30" });
    expect(state.phase).toBe("sharing");
    expect(state.workW).toBe(1620);
    expect(state.netExchangeW).toBe(90);
    expect(state.commandedTotalW).toBe(1408);
    expect(state.derate).toBeCloseTo(1.16);
    expect(state.units).toHaveLength(1);
    expect(state.units[0]).toMatchObject({
      unitId: "lhs",
      shareW: 817,
      phase: "sharing",
      reason: "on_plan",
    });
    // An unknown phase falls back to idle, never a fabricated state.
    expect(toEveningShareState(projection({ phase: "warping" }))!.phase).toBe("idle");
  });

  it("carries the advise posture's submits: never marker", () => {
    const state = toEveningShareState(projection({ mode: "advise", submits: "never" }))!;
    expect(state.submits).toBe("never");
  });

  it("carries the last close's morning facts", () => {
    const state = toEveningShareState(
      projection({
        last_close: {
          night: "2026-08-25",
          served_wh: { lhs: 2140, mid: 1680, rhs: 1210 },
          import_wh: 610,
          spill_wh: 40,
          convergence_delta_pct: { open: 27.1, close: 12.4 },
          money: { import_paid_cents: 187.7, spill_earned_cents: 0.1 },
        },
      }),
    )!;
    expect(state.lastClose).not.toBeNull();
    expect(state.lastClose!.night).toBe("2026-08-25");
    expect(state.lastClose!.convergenceOpen).toBeCloseTo(27.1);
    expect(state.lastClose!.money).toEqual({
      import_paid_cents: 187.7,
      spill_earned_cents: 0.1,
    });
  });
});

describe("eveningStatusText", () => {
  it("words each fleet phase, with advise naming its own truth", () => {
    const base = toEveningShareState(projection())!;
    expect(eveningStatusText(base)).toContain("Sharing the evening load");
    expect(eveningStatusText({ ...base, phase: "capability_limited" })).toContain(
      "fleet is at its caps",
    );
    expect(eveningStatusText({ ...base, phase: "withdrawn" })).toContain("Withdrawn");
    expect(eveningStatusText({ ...base, phase: "idle" })).toContain("Idle until the 16:00 window");
    const advise = { ...base, submits: "never" as const };
    expect(eveningStatusText(advise)).toContain("submits nothing");
  });
});

describe("eveningLiveLine", () => {
  it("words work, the SIGNED exchange, and elsewhere; empty with no work word", () => {
    const state = toEveningShareState(projection())!;
    expect(eveningLiveLine(state)).toContain("work 1620 W");
    expect(eveningLiveLine(state)).toContain("import 90 W");
    const exporting = toEveningShareState(projection({ net_exchange_w: -220 }))!;
    expect(eveningLiveLine(exporting)).toContain("export 220 W");
    const flowing = toEveningShareState(projection({ elsewhere_w: 800 }))!;
    expect(eveningLiveLine(flowing)).toContain("elsewhere 800 W");
    expect(eveningLiveLine(toEveningShareState(projection({ work_w: null }))!)).toBe("");
  });
});

describe("eveningCloseText", () => {
  it("words the morning facts: served, pods, import, money, convergence", () => {
    const state = toEveningShareState(
      projection({
        last_close: {
          night: "2026-08-25",
          served_wh: { lhs: 2140, mid: 1680, rhs: 1210 },
          import_wh: 610,
          spill_wh: 40,
          convergence_delta_pct: { open: 27.1, close: 12.4 },
          money: { import_paid_cents: 187.7, spill_earned_cents: 0.1 },
        },
      }),
    )!;
    const line = eveningCloseText(state.lastClose!);
    expect(line).toContain("5.03 kWh across 3 pods");
    expect(line).toContain("import 0.61 kWh");
    expect(line).toContain("188 c");
    expect(line).toContain("27.1→12.4 pct");
  });
});

describe("eveningReasonText", () => {
  it("words the §7.1 skip vocabulary verbatim, never silence", () => {
    expect(eveningReasonText("optimizer_claim")).toContain("another adviser");
    expect(eveningReasonText("under_intent")).toContain("preempts instantly");
    expect(eveningReasonText("claim_settling")).toContain("20 s debounce");
    expect(eveningReasonText("not_delivering")).toContain("not delivering");
    expect(eveningReasonText("soc_floor")).toContain("participation floor");
    expect(eveningReasonText("unit_disarmed")).toContain("never arms");
    expect(eveningReasonText("some_future_reason")).toBe("some future reason");
  });
});

describe("eveningAlertText", () => {
  it("carries the E6 degrade note first, then the evidence withdraw", () => {
    const degraded = toEveningShareState(
      projection({ degraded_note: "mode degraded to advise at boot (E6)" }),
    )!;
    expect(eveningAlertText(degraded)).toContain("degraded to advise");
    const withdrawn = toEveningShareState(
      projection({ phase: "withdrawn", reason_codes: ["grid_evidence_missing"] }),
    )!;
    expect(eveningAlertText(withdrawn)).toContain("grid evidence missing");
    expect(eveningAlertText(withdrawn)).toContain("own autonomy");
    expect(eveningAlertText(toEveningShareState(projection())!)).toBeNull();
  });
});

describe("the pinned vocabulary", () => {
  it("carries §0's sentence and the C16 stop route VERBATIM", () => {
    expect(EVENING_PINNED_SENTENCE).toMatch(/^The meter nets/);
    expect(EVENING_PINNED_SENTENCE).toContain("no watt is ever exported");
    expect(EVENING_STOP_ROUTE).toContain("claim any pod");
    expect(EVENING_STATE_EVENT).toBe("evening.state_changed");
  });
});
