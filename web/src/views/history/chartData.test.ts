/**
 * Behavior contract for the chart marshalling layer
 * (web/src/views/history/chartData.ts) — the aligned-data shapes the drawing
 * component consumes.
 *
 * The pins:
 *
 * - ONE SHARED AXIS: per-series timestamps (the wire's own per-series skips)
 *   union into one ascending axis; a series holds NULL where it had no
 *   sample — never 0, never carried forward.
 * - GAPS BREAK EVERY LINE: each gap contributes null at BOTH edges in EVERY
 *   column, so a spanGaps=false renderer shows the break the wire reported.
 * - HOURLY BANDS: mean/min/max columns aligned to the same instants; a null
 *   band rides as null.
 * - STEPS: held values at both edges of every segment; a null-valued segment
 *   (nothing commanded) draws nothing, never 0 W.
 */
import { describe, expect, it } from "vitest";
import { buildChart, commandedStepSegments } from "./chartData";
import type { CommandedSegment } from "../../app/history";

const T0 = Date.parse("2026-08-24T00:00:00+00:00");
const MIN = 60_000;
const at = (minutes: number): number => T0 + minutes * MIN;

describe("chart data — the shared axis", () => {
  it("unions per-series timestamps into one ascending axis, nulls where a series was absent", () => {
    const built = buildChart([
      {
        kind: "line",
        key: "battery_watts",
        label: "Battery",
        points: [
          { t: at(0), v: -500 },
          { t: at(2), v: -900 },
        ],
      },
      {
        kind: "line",
        key: "grid_power_w",
        label: "Grid",
        points: [
          { t: at(1), v: -412 },
          { t: at(2), v: -950 },
        ],
      },
    ]);
    expect(built.xs).toEqual([at(0), at(1), at(2)]);
    const battery = built.columns.find((column) => column.key === "battery_watts");
    const grid = built.columns.find((column) => column.key === "grid_power_w");
    // battery had no sample at minute 1: NULL, never 0, never carried forward.
    expect(battery?.values).toEqual([-500, null, -900]);
    expect(grid?.values).toEqual([null, -412, -950]);
  });

  it("sorts and de-duplicates unsorted, duplicated instants", () => {
    const built = buildChart([
      {
        kind: "line",
        key: "soc",
        label: "SOC",
        points: [
          { t: at(5), v: 90 },
          { t: at(1), v: 96 },
          { t: at(5), v: 90 },
        ],
      },
    ]);
    expect(built.xs).toEqual([at(1), at(5)]);
    expect(built.columns[0]?.values).toEqual([96, 90]);
  });
});

describe("chart data — gap breaks", () => {
  it("contributes null at both gap edges in every column", () => {
    const built = buildChart(
      [
        {
          kind: "line",
          key: "battery_watts",
          label: "Battery",
          points: [
            { t: at(0), v: -500 },
            { t: at(10), v: -400 },
          ],
        },
        {
          kind: "line",
          key: "grid_power_w",
          label: "Grid",
          points: [
            { t: at(0), v: -520 },
            { t: at(9), v: -420 },
          ],
        },
      ],
      [{ from: at(2), to: at(8) }],
    );
    expect(built.xs).toEqual([at(0), at(2), at(8), at(9), at(10)]);
    expect(built.columns[0]?.values).toEqual([-500, null, null, null, -400]);
    expect(built.columns[1]?.values).toEqual([-520, null, null, -420, null]);
  });

  it("never overwrites a real sample that already sits on a gap edge", () => {
    const built = buildChart(
      [
        {
          kind: "line",
          key: "battery_watts",
          label: "Battery",
          points: [
            { t: at(0), v: -500 },
            { t: at(2), v: -480 },
          ],
        },
      ],
      [{ from: at(2), to: at(8) }],
    );
    expect(built.columns[0]?.values).toEqual([-500, -480, null]);
  });
});

describe("chart data — hourly bands", () => {
  it("contributes mean plus min/max columns on the same instants", () => {
    const built = buildChart([
      {
        kind: "hourly",
        key: "temperature_min_c",
        label: "Temperature",
        points: [
          { t: at(0), v: 21.5, min: 20.9, max: 22.1, n: 120 },
          { t: at(60), v: 23, min: 22.4, max: 24, n: 118 },
        ],
      },
    ]);
    expect(built.xs).toEqual([at(0), at(60)]);
    expect(built.columns.map((column) => column.key)).toEqual([
      "temperature_min_c",
      "temperature_min_c#min",
      "temperature_min_c#max",
    ]);
    expect(built.columns[0]?.values).toEqual([21.5, 23]);
    expect(built.columns[1]?.values).toEqual([20.9, 22.4]);
    expect(built.columns[2]?.values).toEqual([22.1, 24]);
  });

  it("rides a null band as null, never 0", () => {
    const built = buildChart([
      {
        kind: "hourly",
        key: "battery_watts",
        label: "Battery",
        points: [{ t: at(0), v: -60, min: null, max: null, n: 0 }],
      },
    ]);
    expect(built.columns[1]?.values).toEqual([null]);
    expect(built.columns[2]?.values).toEqual([null]);
  });
});

describe("chart data — steps", () => {
  it("emits one held value per segment edge — the renderer owns the stair", () => {
    const built = buildChart([
      {
        kind: "step",
        key: "commanded_w",
        label: "Commanded",
        segments: [
          { from: at(0), to: at(30), value: -2500 },
          { from: at(30), to: at(60), value: -2400 },
        ],
      },
    ]);
    expect(built.xs).toEqual([at(0), at(30), at(60)]);
    // The shared edge carries the NEW segment's value; the final to-edge
    // closes the stair at the window end.
    expect(built.columns[0]?.values).toEqual([-2500, -2400, -2400]);
  });

  it("breaks across a null-valued segment — nothing commanded draws nothing", () => {
    const built = buildChart([
      {
        kind: "step",
        key: "commanded_w",
        label: "Commanded",
        segments: [
          { from: at(0), to: at(10), value: null },
          { from: at(10), to: at(30), value: -2500 },
          { from: at(30), to: at(60), value: null },
        ],
      },
    ]);
    expect(built.columns[0]?.values).toEqual([null, -2500, null, null]);
  });

  it("maps commanded segments through the sign convention the caller pins", () => {
    const segments: CommandedSegment[] = [
      { from: at(0), to: at(1), source: "night_adviser", direction: "charge", watts: 2500 },
      { from: at(1), to: at(2), source: null, direction: null, watts: null },
    ];
    const stepped = commandedStepSegments(segments, (segment) =>
      segment.direction === "charge" ? -(segment.watts ?? 0) : null,
    );
    expect(stepped[0]?.value).toBe(-2500);
    expect(stepped[1]?.value).toBeNull();
  });
});

describe("chart data — empty and degenerate inputs", () => {
  it("builds an empty chart from no specs", () => {
    const built = buildChart([]);
    expect(built.xs).toEqual([]);
    expect(built.columns).toEqual([]);
  });

  it("keeps a gap-only axis with all-null columns", () => {
    const built = buildChart(
      [{ kind: "line", key: "a", label: "A", points: [] }],
      [{ from: at(0), to: at(5) }],
    );
    expect(built.xs).toEqual([at(0), at(5)]);
    expect(built.columns[0]?.values).toEqual([null, null]);
  });
});
