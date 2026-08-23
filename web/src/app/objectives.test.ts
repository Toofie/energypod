/**
 * Behavior contract for the night-writer detector's console wire model
 * (API_CONTRACTS.md "Night-writer detector (foreign-objective observation)"):
 * the per-unit `last_objective_observed` summary parsing (null-safe), the
 * alert-tier `foreign_objective.observed` payload parsing, the session view's
 * response shape, the plain-language maps, and the two pinned honesty rules —
 * the quiet line renders ONLY on the detector's foreign classification, and
 * the signature honesty note says exactly what the contract says.
 *
 * WIRE TRUTH (the amended contract, mirrored in web/src/test/wire.ts):
 * - Summary: `{observed_at, active_w, reactive_var, classification, reason}`
 *   — null before any recorded sample; the detector composes ALWAYS once it
 *   lands, so the key is never absent, only null.
 * - Bus event (alert tier, one per episode+reason):
 *   `{unit_id, observed_at, active_w, reactive_var, classification, reason,
 *   lifecycle, claimed, run_mode_w, ctrl_mode_w, work_mode_w, debug_mode_w,
 *   grid_power_w, pv_evidence}`. QUIET-TIER EVIDENCE IS NEVER PUBLISHED —
 *   in-band autonomy and handback grace accumulate behind the read surface.
 * - Session view: `GET /api/v1/objectives/observed?last=24h` answers
 *   `{as_of, last, window_s, units: [...]}` with the per-unit rollup.
 * - Classifications: pod_autonomy_objective_observed | handback_grace |
 *   foreign_objective_observed. Reasons (one word):
 *   reactive_objective_observed | outside_autonomy_band |
 *   sustained_remote_mode_objective | sustained_charge_without_pv_evidence.
 */
import { describe, expect, it } from "vitest";
import { normalizeSnapshot, patchUnitObjective } from "./fleet";
import {
  OBJECTIVE_CLASSIFICATIONS,
  OBJECTIVE_SIGNATURE_HONESTY_NOTE,
  OBJECTIVE_WINDOW_OPTIONS,
  commandedBySomeoneElseText,
  isForeignObjective,
  objectiveClassificationText,
  objectiveFromForeignEvent,
  objectiveReasonText,
  toForeignObjectiveEvent,
  toObservedObjectivesView,
  toUnitObjective,
} from "./objectives";
import {
  FOREIGN_OBJECTIVE_AUDIT_TYPE,
  FOREIGN_OBJECTIVE_OBSERVED_EVENT,
  lastObjectiveObserved,
  observedObjectivesUnit,
  withObjective,
  type WireUnitSnapshot,
} from "../test/wire";

describe("toUnitObjective — the per-unit summary (null-safe, feature-detected)", () => {
  it("narrows the contract's summary shape verbatim", () => {
    expect(
      toUnitObjective({
        observed_at: "2026-08-23T23:40:00+10:00",
        active_w: -2400,
        reactive_var: 0,
        classification: "foreign_objective_observed",
        reason: "sustained_charge_without_pv_evidence",
      }),
    ).toEqual({
      observedAt: "2026-08-23T23:40:00+10:00",
      activeW: -2400,
      reactiveVar: 0,
      classification: "foreign_objective_observed",
      reason: "sustained_charge_without_pv_evidence",
    });
  });

  it("keeps an unknown classification word instead of refusing or rewording it", () => {
    expect(toUnitObjective({ classification: "future_word", active_w: -5 })?.classification).toBe(
      "future_word",
    );
  });

  it("is null for an absent, null, non-object, or classification-less value — never fabricated", () => {
    expect(toUnitObjective({})).toBeNull();
    expect(toUnitObjective({ observed_at: "x", active_w: -5 })).toBeNull();
    expect(toUnitObjective({ classification: null })).toBeNull();
    expect(toUnitObjective(null)).toBeNull();
    expect(toUnitObjective("foreign_objective_observed")).toBeNull();
  });

  it("keeps every figure nullable — an absent datum is never zero", () => {
    const parsed = toUnitObjective({ classification: "handback_grace" });
    expect(parsed).toEqual({
      observedAt: null,
      activeW: null,
      reactiveVar: null,
      classification: "handback_grace",
      reason: null,
    });
  });
});

describe("toForeignObjectiveEvent — the alert-tier payload", () => {
  it("narrows the contract's payload, every figure nullable", () => {
    expect(
      toForeignObjectiveEvent({
        unit_id: "mid",
        observed_at: "2026-08-23T23:40:00+10:00",
        active_w: -2400,
        reactive_var: 0,
        classification: "foreign_objective_observed",
        reason: "sustained_charge_without_pv_evidence",
        lifecycle: "disarmed",
        claimed: false,
        run_mode_w: 1,
        ctrl_mode_w: 1,
        work_mode_w: 6,
        debug_mode_w: 0,
        grid_power_w: -180,
        pv_evidence: false,
      }),
    ).toEqual({
      unitId: "mid",
      observedAt: "2026-08-23T23:40:00+10:00",
      activeW: -2400,
      reactiveVar: 0,
      classification: "foreign_objective_observed",
      reason: "sustained_charge_without_pv_evidence",
      lifecycle: "disarmed",
      claimed: false,
      runModeW: 1,
      ctrlModeW: 1,
      workModeW: 6,
      debugModeW: 0,
      gridPowerW: -180,
      pvEvidence: false,
    });
  });

  it("is null without a unit id", () => {
    expect(toForeignObjectiveEvent({ active_w: -2400 })).toBeNull();
    expect(toForeignObjectiveEvent({ unit_id: "" })).toBeNull();
    expect(toForeignObjectiveEvent(null)).toBeNull();
  });

  it("implies the foreign summary for the state-local patch — the EVENT TYPE is the assertion", () => {
    const event = toForeignObjectiveEvent({
      unit_id: "mid",
      observed_at: "2026-08-23T23:40:00+10:00",
      active_w: -2400,
      classification: "whatever_the_payload_says",
    });
    expect(objectiveFromForeignEvent(event!)).toEqual({
      observedAt: "2026-08-23T23:40:00+10:00",
      activeW: -2400,
      reactiveVar: null,
      classification: FOREIGN_OBJECTIVE_AUDIT_TYPE,
      reason: null,
    });
  });
});

describe("isForeignObjective — the quiet line's one gate", () => {
  it("is true only for the detector's foreign classification", () => {
    expect(isForeignObjective(toUnitObjective(lastObjectiveObserved()))).toBe(true);
    expect(
      isForeignObjective(
        toUnitObjective(lastObjectiveObserved({ classification: "pod_autonomy_objective_observed" })),
      ),
    ).toBe(false);
    // The site's KNOWN nightly writer is expected, never foreign (the
    // amendment's outranking rule — quiet characterization, no quiet line).
    expect(
      isForeignObjective(toUnitObjective(lastObjectiveObserved({ classification: "expected_nightly_charge", active_w: -2500 }))),
    ).toBe(false);
    expect(
      isForeignObjective(toUnitObjective(lastObjectiveObserved({ classification: "handback_grace" }))),
    ).toBe(false);
    // An unknown future word is not the backend's foreign assertion — never
    // treated as one (no accusation the wire did not make).
    expect(
      isForeignObjective(toUnitObjective(lastObjectiveObserved({ classification: "future_word" }))),
    ).toBe(false);
    expect(isForeignObjective(null)).toBe(false);
  });
});

describe("the plain-language maps", () => {
  it("words every pinned reason — the pattern reasons in the operator's words", () => {
    expect(objectiveReasonText("reactive_objective_observed")).toBe(
      "carries reactive power, which the pod's own self-charge never does",
    );
    expect(objectiveReasonText("outside_autonomy_band")).toBe(
      "outside the pod's own operating range",
    );
    expect(objectiveReasonText("sustained_remote_mode_objective")).toBe(
      "held continuously in remote-power mode — a written objective, not the pod's own load-following",
    );
    expect(objectiveReasonText("sustained_charge_without_pv_evidence")).toBe(
      "an external charge pattern — sustained charging while the site imported, with no solar surplus",
    );
  });

  it("words an unknown reason honestly from its own letters, never guessing", () => {
    expect(objectiveReasonText("brand_new_rule")).toBe("brand new rule");
  });

  it("words the classifications — the quiet tiers get quiet words", () => {
    expect(objectiveClassificationText("pod_autonomy_objective_observed")).toBe(
      "the pod's own self-charge",
    );
    expect(objectiveClassificationText("expected_nightly_charge")).toBe(
      "the site's scheduled nightly charge",
    );
    expect(objectiveClassificationText("handback_grace")).toBe("the tail of our own command");
    expect(objectiveClassificationText("foreign_objective_observed")).toBe("an external writer");
    expect(objectiveClassificationText("future_word")).toBe("future word");
  });

  it("carries the classification vocabulary as a runtime checklist", () => {
    expect(OBJECTIVE_CLASSIFICATIONS).toEqual([
      "pod_autonomy_objective_observed",
      "expected_nightly_charge",
      "handback_grace",
      "foreign_objective_observed",
    ]);
  });
});

describe("commandedBySomeoneElseText — THE QUIET PER-UNIT LINE", () => {
  it("composes the pinned shape: figure, the sample's own time, the pattern reason in plain words", () => {
    expect(
      commandedBySomeoneElseText(
        toUnitObjective(
          lastObjectiveObserved({
            observed_at: "2026-08-23T23:40:00+10:00",
            active_w: -2400,
            reason: "sustained_charge_without_pv_evidence",
          }),
        )!,
      ),
    ).toBe(
      "Commanded by something else: -2,400 W at 23:40 (an external charge pattern — sustained charging while the site imported, with no solar surplus)",
    );
  });

  it("names a missing figure honestly and drops the clauses the wire did not carry", () => {
    const parsed = toUnitObjective({ classification: "foreign_objective_observed" });
    expect(parsed !== null && commandedBySomeoneElseText(parsed)).toBe(
      "Commanded by something else: power not available",
    );
  });
});

describe("THE HONESTY LINE — pinned verbatim (the contract's own stated limit)", () => {
  it("says an in-band charge objective cannot be attributed pod-vs-external by signature alone", () => {
    expect(OBJECTIVE_SIGNATURE_HONESTY_NOTE).toBe(
      "A charge objective inside the pod's own self-charge range cannot be attributed to the pod or to an external writer by its signature alone — the figure and sign fit both. In-band objectives are recorded here as evidence without attribution; only a pattern the pod's own behavior never shows (reactive power, an out-of-range magnitude, or a sustained written-objective mode) is attributed as an external writer — and never named to a specific application.",
    );
  });
});

describe("snapshot adoption — normalizeSnapshot carries the per-unit summary", () => {
  const foreignUnit: WireUnitSnapshot = withObjective(
    { unit_id: "mid", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good" } as WireUnitSnapshot,
    lastObjectiveObserved(),
  );
  const quietUnit: WireUnitSnapshot = withObjective(
    { unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 3, quality: "good" } as WireUnitSnapshot,
    lastObjectiveObserved({
      observed_at: "2026-08-24T05:58:00+10:00",
      active_w: -540,
      classification: "pod_autonomy_objective_observed",
      reason: null,
    }),
  );

  it("parses the summary onto the unit model (feature-present)", () => {
    const world = normalizeSnapshot({
      site_id: "home-1",
      snapshot_sequence: 4100,
      captured_at: "2026-08-22T10:00:00Z",
      units: [foreignUnit, quietUnit],
    });
    expect(world.units[0]?.objective).not.toBeNull();
    expect(world.units[0]?.objective?.classification).toBe("foreign_objective_observed");
    expect(world.units[1]?.objective?.classification).toBe("pod_autonomy_objective_observed");
  });

  it("treats an absent field and an explicit null identically — no recorded sample", () => {
    const world = normalizeSnapshot({
      site_id: "home-1",
      snapshot_sequence: 4100,
      captured_at: "2026-08-22T10:00:00Z",
      units: [
        { unit_id: "lhs", lifecycle: "disarmed", telemetry_age_s: 4, quality: "good" },
        withObjective(
          { unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 3, quality: "good" } as WireUnitSnapshot,
          null,
        ),
      ],
    });
    expect(world.units[0]?.objective).toBeNull();
    expect(world.units[1]?.objective).toBeNull();
  });

  it("patchUnitObjective moves one unit's summary state-locally (the alert frame's move)", () => {
    const world = normalizeSnapshot({
      site_id: "home-1",
      snapshot_sequence: 4100,
      captured_at: "2026-08-22T10:00:00Z",
      units: [{ unit_id: "mid", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good" }],
    });
    const patched = patchUnitObjective(
      world,
      "mid",
      toUnitObjective(lastObjectiveObserved({ active_w: -2400 }))!,
    );
    expect(patched?.units[0]?.objective?.activeW).toBe(-2400);
    // An unknown unit changes nothing; a null world stays null.
    expect(patchUnitObjective(world, "ghost", toUnitObjective(lastObjectiveObserved())!)).toBe(
      world,
    );
    expect(patchUnitObjective(null, "mid", toUnitObjective(lastObjectiveObserved())!)).toBeNull();
  });
});

describe("toObservedObjectivesView — the session view's response shape", () => {
  it("narrows the contract's answer, per-unit rollups and all", () => {
    const view = toObservedObjectivesView({
      as_of: "2026-08-24T06:00:00+10:00",
      last: "24h",
      window_s: 86_400,
      units: [
        observedObjectivesUnit(),
        observedObjectivesUnit({
          unit_id: "lhs",
          first_seen_at: null,
          last_seen_at: null,
          sample_count: 0,
          charge_sample_count: 0,
          discharge_sample_count: 0,
          min_active_w: null,
          typical_active_w: null,
          max_active_w: null,
          foreign_episode_count: 0,
          foreign_active: false,
          foreign_reason: null,
          last_objective_observed: null,
        }),
      ],
    });
    expect(view).not.toBeNull();
    expect(view?.last).toBe("24h");
    expect(view?.windowS).toBe(86_400);
    expect(view?.units).toHaveLength(2);
    expect(view?.units[0]?.typicalActiveW).toBe(-2270);
    expect(view?.units[0]?.classificationCounts).toEqual({ foreign_objective_observed: 442 });
    expect(view?.units[0]?.lastObjective?.classification).toBe("foreign_objective_observed");
    expect(view?.units[1]?.lastObjective).toBeNull();
    expect(view?.units[1]?.sampleCount).toBe(0);
  });

  it("is null for a non-object body or one without a units array — never half-adopted", () => {
    expect(toObservedObjectivesView(null)).toBeNull();
    expect(toObservedObjectivesView({ as_of: "x" })).toBeNull();
    expect(toObservedObjectivesView("ok")).toBeNull();
  });

  it("drops unusable unit rows without losing the rest", () => {
    const view = toObservedObjectivesView({ units: [{ no_unit_id: true }, observedObjectivesUnit()] });
    expect(view?.units).toHaveLength(1);
    expect(view?.units[0]?.unitId).toBe("mid");
  });

  it("offers only window spellings inside the route's legal range", () => {
    expect(OBJECTIVE_WINDOW_OPTIONS).toEqual(["24h", "3d", "7d"]);
  });
});

describe("the wire fixtures stay pinned to the contract", () => {
  it("names the bus type with the DOT and the audit fact with the underscore", () => {
    expect(FOREIGN_OBJECTIVE_OBSERVED_EVENT).toBe("foreign_objective.observed");
    expect(FOREIGN_OBJECTIVE_AUDIT_TYPE).toBe("foreign_objective_observed");
  });
});
