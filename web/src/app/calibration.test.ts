/**
 * Behavior contract for the battery calibration program's wire model
 * (web/src/app/calibration.ts): the §8 projection's narrowing (nulls are
 * nulls, never zeros; a garbage frame is never half-adopted) and the
 * plain-word maps the Home card renders — the class chips with their §3.2
 * vocabulary, the due line's C1 horizon-bounded honesty, the §4.4 verdict
 * words, the §5.4 attribution split, the pinned §0 sentence and the C16
 * stop-route line, and the advise posture's `submits: never`.
 */
import { describe, expect, it } from "vitest";
import {
  CALIBRATION_PINNED_SENTENCE,
  CALIBRATION_STOP_ROUTE,
  calibrationAlertText,
  calibrationAttributionText,
  calibrationClassText,
  calibrationDueText,
  calibrationStatusText,
  calibrationVerdictText,
  toCalibrationState,
} from "./calibration";

function unit(spec: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    unit_id: "mid",
    class: "eligible",
    days_since_deep: 65,
    horizon_bounded: true,
    last_deep_date: null,
    horizon_days: 65,
    due: true,
    evidence_short: false,
    kind: "measurement",
    anchored: false,
    standdown: false,
    throughput_wh_mean: 3300,
    ...spec,
  };
}

function projection(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    mode: "act",
    window: { opens_local: "15:00", ends_local: "22:30" },
    phase: "idle",
    as_of: "2026-10-24T14:00:12+00:00",
    units: [
      unit(),
      {
        unit_id: "lhs",
        class: "excluded_cycles_daily",
        days_since_deep: 0,
        horizon_bounded: false,
        due: false,
        sub_floor_dates_14d: 13,
      },
      {
        unit_id: "rhs",
        class: "eligible",
        probe_verdict: "pass",
        probe_at: "2026-10-23T23:04:00+00:00",
      },
    ],
    last_cycle: null,
    request_measurement: null,
    ...spec,
  };
}

describe("toCalibrationState", () => {
  it("narrows the §8 projection with nulls as nulls", () => {
    const state = toCalibrationState(projection());
    expect(state).not.toBeNull();
    expect(state?.mode).toBe("act");
    expect(state?.window).toEqual({ opensLocal: "15:00", endsLocal: "22:30" });
    expect(state?.units).toHaveLength(3);
    const mid = state?.units[0];
    expect(mid?.unitId).toBe("mid");
    expect(mid?.horizonBounded).toBe(true);
    expect(mid?.lastDeepDate).toBeNull();
    expect(mid?.throughputWhMean).toBe(3300);
    expect(state?.lastCycle).toBeNull();
    expect(state?.requestMeasurement).toBeNull();
    expect(state?.traverse).toBeNull();
  });

  it("never half-adopts a garbage frame", () => {
    expect(toCalibrationState(null)).toBeNull();
    expect(toCalibrationState("calibration")).toBeNull();
    expect(toCalibrationState({ mode: "advise" })).not.toBeNull(); // honest defaults
  });

  it("carries the advise posture's submits: never beside the plan", () => {
    const state = toCalibrationState(projection({ mode: "advise", submits: "never" }));
    expect(state?.submits).toBe("never");
    expect(calibrationStatusText(state!)).toContain("submits nothing");
  });

  it("reads the traverse line with the pinned sentence and the stop route", () => {
    const state = toCalibrationState(
      projection({
        phase: "traversing",
        traverse: {
          unit_id: "mid",
          soc_pct: 42.5,
          floor_pct: 10,
          energy_wh: 2871.2,
          energy_bound_wh: 4600,
          rate_w: 800,
          at_risk: false,
        },
      }),
    );
    expect(state?.traverse?.socPct).toBe(42.5);
    expect(CALIBRATION_STOP_ROUTE).toContain("claim the pod");
    expect(CALIBRATION_PINNED_SENTENCE).toContain("anchor is a floor");
  });
});

describe("the plain-word maps", () => {
  it("renders the §3.2 class set verbatim with its figures", () => {
    const state = toCalibrationState(projection())!;
    const classes = Object.fromEntries(
      state.units.map((row) => [row.unitId, calibrationClassText(row)]),
    );
    expect(classes.mid).toContain("first cycle is a measurement");
    expect(classes.lhs).toContain("excluded — cycles daily");
    expect(classes.lhs).toContain("13 of the last 14");
    expect(classes.rhs).toContain("eligible");
  });

  it("names the C1 horizon bound on every due figure", () => {
    const state = toCalibrationState(projection())!;
    expect(calibrationDueText(state.units[0]!)).toContain("at least 65 days");
  });

  it("renders the deferred and stood-down words honestly", () => {
    const deferred = toCalibrationState(
      projection({ units: [unit({ class: "deferred_probe_required" })] }),
    )!;
    expect(calibrationClassText(deferred.units[0]!)).toContain("no passing health-watch probe");
    const stoodDown = toCalibrationState(projection({ units: [unit({ standdown: true })] }))!;
    expect(calibrationClassText(stoodDown.units[0]!)).toContain("stood down");
    expect(calibrationAlertText(stoodDown)).toContain("stood down");
  });

  it("renders the §4.4 verdict vocabulary and the §5.4 attribution split", () => {
    expect(calibrationVerdictText("floor_reached")).toContain("anchor delivered");
    expect(calibrationVerdictText("floor_miss_energy_bound")).toContain("lying-word finding");
    expect(calibrationVerdictText("inconclusive_preempted")).toContain("operator's own act");
    expect(calibrationVerdictText("skipped:optimizer_claim")).toContain("optimizer claim");
    expect(calibrationAttributionText("top_anchor_missed_solar")).toContain("sky's account");
    expect(calibrationAttributionText("taper_never_observed")).toContain("pod's account");
  });

  it("carries the alert tier only for the alert-tier facts", () => {
    const quiet = toCalibrationState(projection())!;
    expect(calibrationAlertText(quiet)).toBeNull();
    const missed = toCalibrationState(
      projection({
        last_cycle: {
          night: "2026-10-23",
          kind: "measurement",
          verdict: "floor_miss_deadline",
          tier: "alert",
          trace_class: "monotone",
          energy_wh: 2100.4,
        },
      }),
    )!;
    expect(calibrationAlertText(missed)).toContain("deadline arrived");
  });

  it("reads the §9 morning-facts entry (the History console's morning states)", () => {
    const state = toCalibrationState(
      projection({
        morning: {
          night: "2026-08-25",
          unit_id: "mid",
          verdict: "floor_reached",
          graduation: "anchored",
          taper_observed: true,
          attribution: null,
          delta_pct: { before: 0.0, after: -3.5, change: 3.5, quality_gated: true },
          spread: { before_mv: 30, after_mv: 18 },
          tier: "resolved",
        },
      }),
    );
    expect(state?.morning).not.toBeNull();
    expect(state?.morning?.unitId).toBe("mid");
    expect(state?.morning?.deltaChange).toBe(3.5);
    expect(state?.morning?.spreadBeforeMv).toBe(30);
    expect(state?.morning?.spreadAfterMv).toBe(18);
    expect(state?.morning?.tier).toBe("resolved");
    expect(toCalibrationState(projection())?.morning).toBeNull();
  });
});
