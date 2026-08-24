/**
 * Behavior contract for the energy scorecard's shared wire model
 * (DESIGN_ENERGY_SCORECARD.md §5/§6 + API_CONTRACTS.md "Energy scorecard"):
 * the `EnergyDayRecord` parse, the `energy_today` snapshot block, the days
 * route body, and the `energy.day_rolled` event — null-safe and
 * absent-tolerant per field, never zero-filled — plus the plain-language pins
 * (the neutral A/B naming, the provenance labels, the never-solar-as-measured
 * wording).
 *
 * PENDING-BACKEND fixtures come from web/src/test/wire.ts; the pinned shapes
 * are the docs', never a view's preference.
 */
import { describe, expect, it } from "vitest";
import {
  SOLAR_FOOTNOTE,
  counterRolesNote,
  crossCheckVerdictText,
  dayMarkerText,
  gridProvenanceNote,
  kwhText,
  sourceProvenanceText,
  tariffText,
  toEnergyDayRecord,
  toEnergyDayRolledEvent,
  toEnergyDaysView,
  toEnergyToday,
} from "./energy";
import {
  energyDayRecord,
  energyDayRolled,
  energyTariff,
  energyToday,
  energyUnitDay,
  getEnergyDaysOk,
} from "../test/wire";

describe("energy — the day-record parse (null-safe, absent-tolerant)", () => {
  it("parses the design's own §5 record with the fleet sums and worst-unit coverage", () => {
    const record = toEnergyDayRecord(energyDayRecord())!;
    expect(record).not.toBeNull();
    expect(record.date).toBe("2026-08-26");
    expect(record.timezone).toBe("Australia/Brisbane");
    expect(record.utcOffsetMinutes).toBe(600);
    expect(record.kind).toBe("in_progress");
    expect(record.solarProductionMeasured).toBe(false);
    // The fleet sums are the three units' own figures: 1.2 + 2.1 + 5.1 import.
    expect(record.fleet.gridImportKwh).toBeCloseTo(8.4, 10);
    expect(record.fleet.gridExportKwh).toBeCloseTo(12.9, 10);
    expect(record.fleet.batteryChargedKwh).toBeCloseTo(6.2, 10);
    expect(record.fleet.batteryDischargedKwh).toBeCloseTo(4.1, 10);
    // Fleet coverage is the WORST unit's (99.4 / 100 / 98.7 -> 98.7).
    expect(record.fleet.coveragePct).toBeCloseTo(98.7, 10);
    expect(record.sources).toEqual({
      grid: "integrated_ct",
      battery: "device_counter",
      load: "device_counter",
      surplus: "attributed_adviser",
    });
    expect(record.counterCrossCheck).toEqual({
      gridADeltaKwh: 8.3,
      gridBDeltaKwh: 12.8,
      consistentWith: "vendor_labels",
      discriminating: true,
    });
    // Only mid carries a surplus attribution in the default record; the
    // non-target units carry a REAL 0 (the adviser was never pointed at
    // them), and the fleet sum is the honest 3.1.
    expect(record.units.mid!.chargedFromSurplusKwh).toBeCloseTo(3.1, 10);
    expect(record.units.rhs!.chargedFromSurplusKwh).toBe(0);
    expect(record.fleet.chargedFromSurplusKwh).toBeCloseTo(3.1, 10);
    expect(record.units.mid!.metricFlags).toEqual([]);
  });

  it("keeps an absent per-unit figure null and a reset flag verbatim — never zero-filled", () => {
    const record = toEnergyDayRecord(
      energyDayRecord({
        units: {
          mid: {
            grid_import_kwh: null,
            grid_export_kwh: null,
            battery_charged_kwh: 0,
            battery_discharged_kwh: null,
            load_kwh: null,
            charged_from_surplus_kwh: null,
            coverage_pct: null,
            metric_flags: ["counter_reset_observed"],
          },
        },
      }),
    )!;
    const mid = record.units.mid!;
    expect(mid.gridImportKwh).toBeNull();
    expect(mid.batteryDischargedKwh).toBeNull();
    expect(mid.coveragePct).toBeNull();
    // A real 0 is a real 0 — distinct from an absent source.
    expect(mid.batteryChargedKwh).toBe(0);
    expect(mid.metricFlags).toEqual(["counter_reset_observed"]);
  });

  it("returns null only for a non-object; a partial record keeps every figure honestly absent", () => {
    expect(toEnergyDayRecord(null)).toBeNull();
    expect(toEnergyDayRecord("nope")).toBeNull();
    const partial = toEnergyDayRecord({ date: "2026-08-27" })!;
    expect(partial).not.toBeNull();
    expect(partial.date).toBe("2026-08-27");
    expect(partial.kind).toBe("complete");
    expect(partial.fleet.gridImportKwh).toBeNull();
    expect(partial.units).toEqual({});
    expect(partial.solarProductionMeasured).toBeNull();
    expect(partial.counterCrossCheck).toBeNull();
  });

  it("keeps the cross-check verdict's null FIRST-CLASS and never invents a verdict", () => {
    // The wire pin (2026-08-26): consistent_with is
    // "vendor_labels" | "swapped" | "undiscriminating" | null — null is the
    // backend's own could-not-discriminate answer on non-discriminating days,
    // not a missing field.
    const noVerdict = toEnergyDayRecord(
      energyDayRecord({
        counter_cross_check: {
          grid_a_delta_kwh: 1.1,
          grid_b_delta_kwh: null,
          consistent_with: null,
          discriminating: false,
        },
      }),
    )!;
    expect(noVerdict.counterCrossCheck).toEqual({
      gridADeltaKwh: 1.1,
      gridBDeltaKwh: null,
      consistentWith: null,
      discriminating: false,
    });
    // A value outside the pinned vocabulary is unusable, not a guess: the
    // honest no-verdict, never an unknown string rendered as evidence.
    const unknown = toEnergyDayRecord({
      ...energyDayRecord(),
      counter_cross_check: {
        grid_a_delta_kwh: 1.1,
        grid_b_delta_kwh: 6.9,
        consistent_with: "guessed",
        discriminating: true,
      },
    })!;
    expect(unknown.counterCrossCheck!.consistentWith).toBeNull();
  });

  it("rolls the fleet up the PINNED way: a null unit is skipped, never erasing the fleet figure", () => {
    const record = toEnergyDayRecord(
      energyDayRecord({
        units: {
          // mid absent all day (source absent): it contributes nothing — it
          // must not zero or null the fleet's day.
          mid: energyUnitDay({
            grid_import_kwh: null,
            grid_export_kwh: null,
            battery_charged_kwh: null,
            battery_discharged_kwh: null,
            load_kwh: null,
            charged_from_surplus_kwh: null,
            coverage_pct: null,
          }),
          rhs: energyUnitDay({
            grid_import_kwh: 2.1,
            grid_export_kwh: 4.2,
            battery_charged_kwh: 2.0,
            battery_discharged_kwh: 1.6,
            load_kwh: 4.4,
            charged_from_surplus_kwh: 0,
            coverage_pct: 100,
          }),
          lhs: energyUnitDay({
            grid_import_kwh: 5.1,
            grid_export_kwh: 1.9,
            battery_charged_kwh: 0.8,
            battery_discharged_kwh: 1.8,
            load_kwh: 5.2,
            charged_from_surplus_kwh: 0,
            coverage_pct: 98.7,
          }),
        },
      }),
    )!;
    // The sums are over the units that carried a figure (2.1 + 5.1 import).
    expect(record.fleet.gridImportKwh).toBeCloseTo(7.2, 10);
    expect(record.fleet.gridExportKwh).toBeCloseTo(6.1, 10);
    expect(record.fleet.batteryChargedKwh).toBeCloseTo(2.8, 10);
    // Coverage is the worst NON-NULL unit's (100 / 98.7 -> 98.7); the unit
    // with no coverage figure contributes no evidence.
    expect(record.fleet.coveragePct).toBeCloseTo(98.7, 10);
  });

  it("keeps a fleet figure null only when NO unit carried one", () => {
    const record = toEnergyDayRecord(
      energyDayRecord({
        units: {
          mid: energyUnitDay({
            grid_import_kwh: null,
            grid_export_kwh: null,
            battery_charged_kwh: null,
            battery_discharged_kwh: null,
            load_kwh: null,
            charged_from_surplus_kwh: null,
            coverage_pct: null,
          }),
        },
      }),
    )!;
    expect(record.fleet.gridImportKwh).toBeNull();
    expect(record.fleet.batteryDischargedKwh).toBeNull();
    expect(record.fleet.chargedFromSurplusKwh).toBeNull();
    expect(record.fleet.coveragePct).toBeNull();
  });
});

describe("energy — the snapshot block, the route body, and the rollover event", () => {
  it("narrowes energy_today as the record plus as_of", () => {
    const today = toEnergyToday(energyToday({ as_of: "2026-08-26T14:03:00+10:00" }))!;
    expect(today.asOf).toBe("2026-08-26T14:03:00+10:00");
    expect(today.kind).toBe("in_progress");
    expect(toEnergyToday(null)).toBeNull();
    // An absent as_of stays null, never a fabricated stamp.
    expect(toEnergyToday(energyDayRecord())!.asOf).toBeNull();
  });

  it("narrowes the optional tariff block: absent/null = kWh-only, present = the operator's own rates", () => {
    // Absent key and explicit null are both the honest kWh-only answer.
    expect(toEnergyToday(energyToday())!.tariff).toBeNull();
    expect(toEnergyToday(energyToday({ tariff: null }))!.tariff).toBeNull();
    expect(toEnergyToday(energyDayRecord())!.tariff).toBeNull();
    // The commissioned site's own block, field for field.
    const tariff = toEnergyToday(energyToday({ tariff: energyTariff() }))!.tariff!;
    expect(tariff.currency).toBe("AUD");
    expect(tariff.importCentsPerKwh).toBe(30.77);
    expect(tariff.exportCentsPerKwh).toBe(2);
    // A present-but-unusable datum falls back to null, never to 0.
    const broken = toEnergyToday({
      ...energyToday(),
      tariff: { currency: "aud", import_cents_per_kwh: "thirty", export_cents_per_kwh: null },
    })!.tariff!;
    expect(broken.currency).toBe("aud");
    expect(broken.importCentsPerKwh).toBeNull();
    expect(broken.exportCentsPerKwh).toBeNull();
  });

  it("words the tariff as rates only — no cost is computed from the general rate", () => {
    expect(tariffText(toEnergyToday(energyToday({ tariff: energyTariff() }))!.tariff!)).toBe(
      "Tariff keys are commissioned — AUD 30.77 c/kWh import · 2 c/kWh feed-in (the general rates; the night window's own off-peak rate is not carried on this wire, so no cost is computed here).",
    );
    // A block with no usable rate renders nothing — the caller keeps its own
    // "once commissioned" wording rather than an empty sentence.
    expect(tariffText({ currency: "AUD", importCentsPerKwh: null, exportCentsPerKwh: null })).toBeNull();
  });

  it("narrowes the days route body: days newest-last, the roles vocabulary, the solar fact", () => {
    const body = getEnergyDaysOk({
      days: [
        energyDayRecord({ date: "2026-08-25", kind: "complete" }),
        energyDayRecord({ date: "2026-08-26", kind: "partial" }),
      ],
      grid_counter_roles: "vendor_labels",
    });
    const view = toEnergyDaysView(body)!;
    expect(view.days.map((day) => day.date)).toEqual(["2026-08-25", "2026-08-26"]);
    expect(view.days[1]!.kind).toBe("partial");
    expect(view.gridCounterRoles).toBe("vendor_labels");
    expect(view.solarProductionMeasured).toBe(false);
    // An unknown roles answer is never promoted to a pin it does not carry.
    expect(toEnergyDaysView({ days: [], grid_counter_roles: "guessed" })!.gridCounterRoles).toBe(
      "unpinned",
    );
    expect(toEnergyDaysView("not an object")).toBeNull();
  });

  it("narrowes the day_rolled payload as the completed record itself", () => {
    const frame = energyDayRolled(4102, energyDayRecord({ date: "2026-08-26", kind: "complete" }));
    const rolled = toEnergyDayRolledEvent(frame.payload)!;
    expect(rolled.date).toBe("2026-08-26");
    expect(rolled.kind).toBe("complete");
    expect(toEnergyDayRolledEvent(null)).toBeNull();
  });
});

describe("energy — the plain-language pins", () => {
  it("names an absent figure, never a zero standing in for it", () => {
    expect(kwhText(null)).toBe("not available");
    expect(kwhText(8.4)).toBe("8.4 kWh");
    expect(kwhText(0)).toBe("0 kWh");
  });

  it("words the day marker: so-far, partial-day breach, coverage riding either", () => {
    expect(dayMarkerText("in_progress", 87)).toBe("so far today — 87% coverage");
    expect(dayMarkerText("in_progress", null)).toBe("so far today");
    expect(dayMarkerText("partial", 61.2)).toBe(
      "partial day — 61.2% coverage — below the commissioned coverage threshold, so the figures are incomplete",
    );
    expect(dayMarkerText("complete", 100)).toBe("full day — 100% coverage");
  });

  it("labels each metric source in the design's own provenance classes", () => {
    expect(sourceProvenanceText("integrated_ct")).toBe("measured by the controller");
    expect(sourceProvenanceText("device_counter")).toBe(
      "recorded by the pods' own energy counters",
    );
    expect(sourceProvenanceText("attributed_adviser")).toBe(
      "attributed from measured watts while solar-surplus charging was active",
    );
  });

  it("carries the A/B unpinned note wherever the integrated source is in play, and never guesses bought/sold", () => {
    const unpinned = gridProvenanceNote("integrated_ct");
    expect(unpinned).toContain("measured by the controller");
    expect(unpinned).toContain("counter A and counter B");
    // The pin: the note never assigns a bought/sold role to either counter.
    expect(unpinned).not.toMatch(/[AB]\s*=\s*bought|[AB]\s*=\s*sold/i);
    const pinned = gridProvenanceNote("device_counter");
    expect(pinned).toContain("roles confirmed");
  });

  it("words the ledger's counter-roles note for every route answer", () => {
    expect(counterRolesNote("unpinned")).toContain("counter A and counter B");
    expect(counterRolesNote("unpinned")).toContain("not confirmed yet");
    expect(counterRolesNote("unpinned")).toContain("measured by the controller");
    expect(counterRolesNote("vendor_labels")).toContain("confirmed as the vendor's labels");
    expect(counterRolesNote("swapped")).toContain("SWAPPED");
  });

  it("words the cross-check verdict: the two pins, the undiscriminating day, and the honest null", () => {
    expect(crossCheckVerdictText("vendor_labels")).toBe("consistent with the vendor's labels");
    expect(crossCheckVerdictText("swapped")).toBe("consistent with the vendor's labels swapped");
    expect(crossCheckVerdictText("undiscriminating")).toContain(
      "undiscriminating — the day's evidence did not single out one label ordering",
    );
    // NULL is first-class: the honest not-yet-discriminating line, naming why
    // (the wire pin's own causes) and never a verdict the wire did not carry.
    const none = crossCheckVerdictText(null);
    expect(none).toContain("not yet discriminating");
    expect(none).toContain("could not tell the counters apart");
    expect(none).not.toMatch(/vendor|swapped/i);
    // No wording ever assigns a bought/sold role to either counter — the
    // verdict is evidence, never a pin.
    expect(crossCheckVerdictText("vendor_labels")).not.toMatch(/[AB]\s*(is|=)\s*(bought|sold)/i);
    expect(crossCheckVerdictText("swapped")).not.toMatch(/[AB]\s*(is|=)\s*(bought|sold)/i);
  });

  it("pins the solar footnote: sold is the export, never a measured production", () => {
    expect(SOLAR_FOOTNOTE).toBe(
      "Solar panels are not measured by the pods — 'sold' is the surplus the site exported.",
    );
    expect(SOLAR_FOOTNOTE).not.toMatch(/solar production/i);
  });
});
