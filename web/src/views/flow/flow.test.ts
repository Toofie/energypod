/**
 * Behavior contract for the Energy Flow view's shared model
 * (web/src/views/flow/flow.ts).
 *
 * The pins that matter most:
 *
 * - SIGN-TO-WORDS DISCIPLINE: the wire's signed figures (grid negative =
 *   import, battery negative = charge) never reach an operator as a sign.
 *   Every wording renders a direction WORD plus the absolute magnitude, and
 *   no wording function may emit "-" before a digit.
 * - HONEST NULLS AND HONEST ZEROS: an absent datum is "not available" (never
 *   zero-filled); a measured 0 W is "Idle" (never import, export, charge, or
 *   discharge).
 * - FLEET ROLLOPS scope: sums cover the phases that report the datum; a
 *   figure no phase reports is null, a figure phases sum to zero is an honest
 *   0 (and words to "Idle"), and import/export as well as charge/discharge
 *   are kept as separate sides — never netted across phases.
 * - THE STORY COMPOSITIONS: the measured sentence families (idle, charging,
 *   discharging, mixed, import+export split, house-only) and the
 * feature-detected adviser stories (night pacing / demand-hold, solar
 * surplus), which compose only while their projections are present and
 * active.
 * - THE COMMAND OVERLAY: commanded-vs-measured rows resolved through the
 *   shared per-unit figures machinery, with an opposite-direction mismatch
 * named — never silently rendered as delivery.
 */
import { describe, expect, it } from "vitest";
import {
  arrowWidthPx,
  ARROW_MAX_PX,
  ARROW_MIN_PX,
  batteryFlowDirection,
  batteryFlowText,
  commandRows,
  fleetBatteryText,
  fleetFlow,
  fleetGridText,
  fleetHouseText,
  excessStory,
  gridFlowDirection,
  gridFlowText,
  hasLiveCommand,
  houseFlowText,
  lifecycleNote,
  measuredStory,
  nightStory,
  phaseListText,
  socText,
  toFlowUnit,
  type FlowUnit,
} from "./flow";
import type { AdviserState } from "../../app/fleet";
import type { NightChargeState } from "../../app/nightCharge";

// --- fixtures -------------------------------------------------------------------

/** A phase's flow facts, directly in the model's shape. */
function phase(
  unitId: string,
  facts: Partial<Pick<FlowUnit, "gridPowerW" | "loadPowerW" | "batteryWatts" | "socPct" | "lifecycle" | "authorized">> = {},
): FlowUnit {
  return {
    unitId,
    lifecycle: facts.lifecycle ?? "active",
    gridPowerW: facts.gridPowerW ?? null,
    loadPowerW: facts.loadPowerW ?? null,
    batteryWatts: facts.batteryWatts ?? null,
    socPct: facts.socPct ?? null,
    authorized: facts.authorized ?? null,
  };
}

const adviser = (over: Partial<AdviserState> = {}): AdviserState => ({
  enabled: true,
  enabledOrigin: "config",
  acknowledgedEconomics: true,
  active: false,
  hysteresisState: "inactive",
  targetUnitId: null,
  commandedChargeW: 0,
  eligibleExportChargeW: 0,
  fleetExportW: null,
  exportEvidence: "good",
  chargeCapW: 0,
  heldIntentId: null,
  lastAction: "idle",
  lastTickAt: "",
  reasonCodes: [],
  ...over,
});

const night = (over: Partial<NightChargeState> = {}): NightChargeState => ({
  enabled: true,
  enabledOrigin: "config",
  acknowledgedPartition: true,
  posture: "partition",
  active: false,
  phase: "idle",
  window: { startLocal: "00:00", endLocal: "06:00", timezone: "Australia/Brisbane" },
  windowEndsAt: null,
  windowEndsInS: null,
  nextWindowAt: null,
  pacing: "cap_first",
  rateCapW: 0,
  holdRateW: 0,
  demandScope: "fleet",
  demandThresholdW: 0,
  demandW: null,
  demandEvidence: "good",
  heldIntentId: null,
  units: [],
  lastAction: "idle",
  lastTickAt: "",
  reasonCodes: [],
  // V2's additive keys, at their v1-identity defaults (a full-posture frame).
  targetPolicy: "full",
  trust: null,
  forecast: null,
  explanation: null,
  morningNotice: null,
  ...over,
});

// --- sign-to-words (the discipline) ----------------------------------------------

describe("flow model — sign-to-words", () => {
  it("words the grid figure from its sign: import, export, idle, or the named gap", () => {
    expect(gridFlowText(-412)).toBe("Importing 412 W");
    expect(gridFlowText(300)).toBe("Exporting 300 W");
    expect(gridFlowText(0)).toBe("Idle");
    expect(gridFlowText(null)).toBe("not available");
    expect(gridFlowText(-412.456)).toBe("Importing 412.46 W");
  });

  it("words the battery figure from its sign: charging, discharging, idle, or the gap", () => {
    expect(batteryFlowText(-1900)).toBe("Charging 1,900 W");
    expect(batteryFlowText(800)).toBe("Discharging 800 W");
    expect(batteryFlowText(0)).toBe("Idle");
    expect(batteryFlowText(null)).toBe("not available");
  });

  it("words the house figure without ever inventing a direction", () => {
    expect(houseFlowText(340)).toBe("Using 340 W");
    expect(houseFlowText(0)).toBe("Using 0 W");
    expect(houseFlowText(null)).toBe("not available");
  });

  it("never emits a raw negative to the operator", () => {
    for (const text of [
      gridFlowText(-412),
      gridFlowText(-0.5),
      batteryFlowText(-1980.25),
      fleetGridText(fleetFlow([phase("mid", { gridPowerW: -800 }), phase("rhs", { gridPowerW: 300 })])),
      fleetBatteryText(fleetFlow([phase("mid", { batteryWatts: -1132.456 })])),
    ]) {
      expect(text).not.toMatch(/-\d/);
    }
  });

  it("maps the signs to the arrow directions the diagram draws", () => {
    expect(gridFlowDirection(-412)).toBe("import");
    expect(gridFlowDirection(300)).toBe("export");
    expect(gridFlowDirection(0)).toBe("idle");
    expect(gridFlowDirection(null)).toBe("unknown");
    expect(batteryFlowDirection(-1900)).toBe("charge");
    expect(batteryFlowDirection(800)).toBe("discharge");
    expect(batteryFlowDirection(0)).toBe("idle");
    expect(batteryFlowDirection(null)).toBe("unknown");
  });

  it("words the charge level beside the battery figure; '' when absent", () => {
    expect(socText(48.5)).toBe("48.5% charged");
    expect(socText(null)).toBe("");
  });
});

// --- parsing ----------------------------------------------------------------------

describe("flow model — snapshot parsing", () => {
  it("reads the telemetry block's flow fields, keeping absent data null", () => {
    const unit = toFlowUnit({
      unit_id: "mid",
      lifecycle: "active",
      telemetry: { soc_pct: 10, grid_power_w: -412, load_power_w: 340, battery_watts: -1900 },
    });
    expect(unit).toEqual({
      unitId: "mid",
      lifecycle: "active",
      gridPowerW: -412,
      loadPowerW: 340,
      batteryWatts: -1900,
      socPct: 10,
      authorized: null,
    });
  });

  it("falls back to the unit-level measured_watts (the same datum, same sign)", () => {
    const unit = toFlowUnit({ unit_id: "mid", lifecycle: "active", measured_watts: 800, telemetry: null });
    expect(unit?.batteryWatts).toBe(800);
  });

  it("clamps an out-of-range charge reading to the named gap, never a figure", () => {
    expect(toFlowUnit({ unit_id: "mid", telemetry: { soc_pct: 140 } })?.socPct).toBeNull();
    expect(toFlowUnit({ unit_id: "mid", telemetry: {} })?.socPct).toBeNull();
  });

  it("rejects a row with no unit id", () => {
    expect(toFlowUnit({ telemetry: {} })).toBeNull();
    expect(toFlowUnit(null)).toBeNull();
  });
});

// --- the fleet rollup ---------------------------------------------------------------

describe("flow model — fleet rollup", () => {
  it("sums each side over the phases that report it", () => {
    const fleet = fleetFlow([
      phase("mid", { gridPowerW: -800, batteryWatts: -1900, loadPowerW: 340 }),
      phase("rhs", { gridPowerW: 300, batteryWatts: -1900, loadPowerW: 270 }),
      phase("lhs", { gridPowerW: 0, batteryWatts: 800, loadPowerW: 100 }),
    ]);
    expect(fleet.importW).toBe(800);
    expect(fleet.exportW).toBe(300);
    expect(fleet.chargingW).toBe(3800);
    expect(fleet.dischargingW).toBe(800);
    expect(fleet.houseW).toBe(710);
    expect(fleet.importPhases).toEqual(["mid"]);
    expect(fleet.exportPhases).toEqual(["rhs"]);
    expect(fleet.chargingPhases).toEqual(["mid", "rhs"]);
    expect(fleet.dischargingPhases).toEqual(["lhs"]);
  });

  it("keeps import and export separate — never a netted figure", () => {
    const fleet = fleetFlow([
      phase("mid", { gridPowerW: -800 }),
      phase("rhs", { gridPowerW: 300 }),
    ]);
    expect(fleet.importW).toBe(800);
    expect(fleet.exportW).toBe(300);
    expect(fleetGridText(fleet)).toBe("Importing 800 W · Exporting 300 W");
  });

  it("nulls a figure no phase reports, and words an all-zero grid as Idle", () => {
    const silent = fleetFlow([phase("mid", { batteryWatts: -500 }), phase("rhs", { batteryWatts: 0 })]);
    expect(silent.importW).toBeNull();
    expect(silent.exportW).toBeNull();
    expect(silent.houseW).toBeNull();
    expect(silent.gridReporting).toBe(0);
    expect(fleetGridText(silent)).toBe("not available");

    const quiet = fleetFlow([phase("mid", { gridPowerW: 0 }), phase("rhs", { gridPowerW: 0 })]);
    expect(quiet.importW).toBe(0);
    expect(quiet.exportW).toBe(0);
    expect(fleetGridText(quiet)).toBe("Idle");
  });

  it("words the fleet battery with both sides when phases disagree", () => {
    const fleet = fleetFlow([
      phase("mid", { batteryWatts: -1900 }),
      phase("lhs", { batteryWatts: 800 }),
    ]);
    expect(fleetBatteryText(fleet)).toBe("Charging 1,900 W · Discharging 800 W");
  });

  it("names the house sum's scope when a phase does not report a load", () => {
    const fleet = fleetFlow([
      phase("mid", { loadPowerW: 340 }),
      phase("rhs", { loadPowerW: 270 }),
      phase("lhs", {}),
    ]);
    expect(fleetHouseText(fleet)).toBe("Using 610 W — across the 2 of 3 phases reporting");
  });
});

// --- the story compositions ----------------------------------------------------------

describe("flow model — the story line", () => {
  it("composes the all-idle site", () => {
    const units = [
      phase("mid", { gridPowerW: 0, batteryWatts: 0, loadPowerW: 0 }),
      phase("rhs", { gridPowerW: 0, batteryWatts: 0, loadPowerW: 0 }),
    ];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "Nothing is flowing — the grid connection, the batteries and the house are all idle.",
    );
  });

  it("composes the charging fleet with its source and the house", () => {
    const units = [
      phase("mid", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 270 }),
      phase("rhs", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 270 }),
      phase("lhs", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 270 }),
    ];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "All the batteries are charging 5,700 W in total from the grid, the house using 810 W.",
    );
  });

  it("composes charging from export without ever calling it solar", () => {
    const units = [
      phase("mid", { gridPowerW: 500, batteryWatts: -300, loadPowerW: 100 }),
      phase("rhs", { gridPowerW: 500, batteryWatts: -300, loadPowerW: 100 }),
    ];
    const story = measuredStory(units, fleetFlow(units));
    expect(story).toBe("All the batteries are charging 600 W in total, while the site exports 1,000 W, the house using 200 W.");
    expect(story).not.toMatch(/solar/i);
  });

  it("composes the mixed fleet: one phase discharging while the others charge", () => {
    const units = [
      phase("lhs", { gridPowerW: 300, batteryWatts: 800, loadPowerW: 100 }),
      phase("mid", { gridPowerW: -1500, batteryWatts: -1900, loadPowerW: 200 }),
      phase("rhs", { gridPowerW: -1200, batteryWatts: -1900, loadPowerW: 110 }),
    ];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "lhs is discharging 800 W while mid and rhs are charging 3,800 W in total, the house using 410 W, importing 2,700 W on mid and rhs while exporting 300 W on lhs.",
    );
  });

  it("composes the import+export split across phases when the batteries are idle", () => {
    const units = [
      phase("mid", { gridPowerW: -800, batteryWatts: 0, loadPowerW: 400 }),
      phase("rhs", { gridPowerW: 300, batteryWatts: 0, loadPowerW: 0 }),
      phase("lhs", { gridPowerW: 0, batteryWatts: 0, loadPowerW: 0 }),
    ];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "The phases are pulling different ways, importing 800 W on mid while exporting 300 W on rhs, the house using 400 W.",
    );
  });

  it("composes the discharging fleet with the grid's contribution", () => {
    const units = [
      phase("mid", { gridPowerW: -100, batteryWatts: 800, loadPowerW: 900 }),
      phase("rhs", { gridPowerW: -100, batteryWatts: 800, loadPowerW: 900 }),
    ];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "The batteries are discharging 1,600 W in total, the house using 1,800 W, the site importing 200 W.",
    );
  });

  it("composes the quiet site: batteries idle, house running", () => {
    const units = [phase("mid", { gridPowerW: -340, batteryWatts: 0, loadPowerW: 340 })];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "The batteries are idle — the house is using 340 W, imported from the grid (340 W).",
    );
  });

  it("says so when no reading has arrived at all", () => {
    const units = [phase("mid"), phase("rhs")];
    expect(measuredStory(units, fleetFlow(units))).toBe(
      "No power readings yet — the pods have not reported a measurement.",
    );
  });

  it("lists phases the operator's way", () => {
    expect(phaseListText(["mid"])).toBe("mid");
    expect(phaseListText(["mid", "rhs"])).toBe("mid and rhs");
    expect(phaseListText(["mid", "rhs", "lhs"])).toBe("mid, rhs and lhs");
  });
});

// --- the adviser stories (feature-detected) ---------------------------------------------

describe("flow model — adviser stories", () => {
  const chargingUnits = [
    phase("mid", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 270 }),
    phase("rhs", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 270 }),
    phase("lhs", { gridPowerW: -2000, batteryWatts: -1900, loadPowerW: 260 }),
  ];

  it("composes the night pacing story while the window runs", () => {
    const fleet = fleetFlow(chargingUnits);
    expect(nightStory(night({ active: true, phase: "pacing" }), fleet)).toBe(
      "Night charging is running — All the batteries are charging 5,700 W in total, the house using 800 W.",
    );
  });

  it("composes the night demand-hold story with the design's own guarantee", () => {
    const fleet = fleetFlow(chargingUnits);
    expect(
      nightStory(night({ active: true, phase: "holding_on_demand", demandW: 1900 }), fleet),
    ).toBe(
      "Night charging is holding — house demand is 1,900 W, so the batteries neither drain nor cycle while the grid meets the house.",
    );
    expect(
      nightStory(night({ active: true, phase: "holding_on_demand", demandW: null }), fleet),
    ).toBe(
      "Night charging is holding — the demand reading is not available, so the batteries neither drain nor cycle while the grid meets the house.",
    );
  });

  it("composes the night stand-by story: measured demand stands the batteries down", () => {
    const fleet = fleetFlow(chargingUnits);
    expect(
      nightStory(night({ active: true, phase: "standing_by_on_demand", demandW: 1900 }), fleet),
    ).toBe(
      "Night charging is standing by — house demand is 1,900 W, so the batteries stand down at zero watts and the pods answer the house on their own until demand falls back.",
    );
  });

  it("composes the night completion stories", () => {
    const fleet = fleetFlow(chargingUnits);
    expect(nightStory(night({ active: true, phase: "complete" }), fleet)).toBe(
      "Night charging is complete — the batteries are full.",
    );
    expect(nightStory(night({ active: true, phase: "skipped_full" }), fleet)).toBe(
      "The night window found the batteries already full — nothing to charge.",
    );
  });

  it("claims nothing while the night projection is absent, disabled, or idle", () => {
    const fleet = fleetFlow(chargingUnits);
    expect(nightStory(night({ enabled: false, phase: "pacing" }), fleet)).toBeNull();
    expect(nightStory(night({ enabled: true, phase: "idle" }), fleet)).toBeNull();
  });

  it("composes the solar-surplus story from the export evidence, never a solar figure", () => {
    const fleet = fleetFlow(chargingUnits);
    const story = excessStory(adviser({ active: true, fleetExportW: 3400 }), fleet);
    expect(story).toBe(
      "Solar-surplus charging is active — All the batteries are charging 5,700 W in total, while the site exports 3,400 W.",
    );
    // The measured sum stands in only when the adviser's own reading is null.
    expect(excessStory(adviser({ active: true, fleetExportW: null }), fleetFlow([
      phase("mid", { batteryWatts: -300, gridPowerW: 500, loadPowerW: 0 }),
    ]))).toBe(
      "Solar-surplus charging is active — All the batteries are charging 300 W in total, while the site exports 500 W.",
    );
  });

  it("claims nothing while the excess projection is absent or inactive", () => {
    expect(excessStory(adviser({ active: false }), fleetFlow(chargingUnits))).toBeNull();
  });
});

// --- the command overlay ------------------------------------------------------------------

describe("flow model — command overlay", () => {
  it("resolves commanded figures through the per-unit map, then the snapshot figure", () => {
    const units = [
      phase("mid", { batteryWatts: -980, authorized: { direction: "charge", watts: 1000 } }),
      phase("rhs", { batteryWatts: 1010, authorized: { direction: "discharge", watts: 0 } }),
    ];
    const rows = commandRows(units, { mid: 1000, rhs: 1200 }, { mid: "charge", rhs: "discharge" });
    // rhs carries no commanded figure from the snapshot, but the live map
    // does (a decision summary landed) — the map is the fresher truth.
    expect(rows.map((row) => row.text)).toEqual([
      "mid — commanded 1,000 W charge, charging at 980 W",
      "rhs — commanded 1,200 W discharge, delivering 1,010 W",
    ]);
  });

  it("falls back to the snapshot's own per-unit authorized figure", () => {
    const units = [phase("mid", { batteryWatts: 980, authorized: { direction: "discharge", watts: 1000 } })];
    expect(commandRows(units, null, null).map((row) => row.text)).toEqual([
      "mid — commanded 1,000 W discharge, delivering 980 W",
    ]);
  });

  it("names an opposite-direction mismatch instead of calling it delivery", () => {
    const units = [phase("mid", { batteryWatts: 500, authorized: { direction: "charge", watts: 1000 } })];
    expect(commandRows(units, null, null)[0]?.text).toBe(
      "mid — commanded 1,000 W charge, but currently discharging 500 W",
    );
  });

  it("words a missing measurement honestly and skips idle commands", () => {
    const units = [
      phase("mid", { batteryWatts: null, authorized: { direction: "charge", watts: 1000 } }),
      phase("rhs", { batteryWatts: 0, authorized: { direction: "idle", watts: 0 } }),
    ];
    const rows = commandRows(units, null, null);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.text).toBe("mid — commanded 1,000 W charge, no measurement yet");
  });

  it("gates the request prefix on any live commanded figure", () => {
    const silent = [phase("mid", { batteryWatts: -500 }), phase("rhs", { batteryWatts: -500 })];
    expect(hasLiveCommand(silent, null)).toBe(false);
    expect(hasLiveCommand(silent, { mid: 1000 })).toBe(true);
    expect(
      hasLiveCommand([phase("mid", { authorized: { direction: "discharge", watts: 900 } })], null),
    ).toBe(true);
  });
});

// --- the arrow geometry ---------------------------------------------------------------------

describe("flow model — arrow geometry", () => {
  it("scales stroke width with watts against the view's largest flow", () => {
    expect(arrowWidthPx(null, 3000)).toBe(0);
    expect(arrowWidthPx(0, 3000)).toBe(0);
    expect(arrowWidthPx(3000, 3000)).toBeCloseTo(ARROW_MAX_PX, 10);
    // The floor is a floor: the smallest positive flow still draws visibly.
    expect(arrowWidthPx(1, 3000)).toBeGreaterThanOrEqual(ARROW_MIN_PX);
    expect(arrowWidthPx(1, 3000)).toBeLessThanOrEqual(ARROW_MIN_PX + 0.01);
    expect(arrowWidthPx(1500, 3000)).toBeCloseTo((ARROW_MIN_PX + ARROW_MAX_PX) / 2, 10);
    // A zero scale never divides.
    expect(arrowWidthPx(1500, 0)).toBe(0);
  });
});

// --- the lifecycle notes ---------------------------------------------------------------------

describe("flow model — lifecycle notes", () => {
  it("words only the states that owe the operator a note", () => {
    expect(lifecycleNote("disconnected")).toBe("No contact — these figures are the last known");
    expect(lifecycleNote("inhibited")).toBe("Held by a safety latch");
    expect(lifecycleNote("active")).toBeNull();
    expect(lifecycleNote("armed_idle")).toBeNull();
    expect(lifecycleNote("disarmed")).toBeNull();
  });
});
