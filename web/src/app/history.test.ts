/**
 * Behavior contract for the plant-history wire model
 * (web/src/app/history.ts) — the pinned shapes of GET /api/v1/history (the
 * live controller's schema-v3 historian) and the snapshot's `history_state`.
 *
 * The pins that matter most:
 *
 * - GAPS AND NULLS, EXACTLY AS SERVED: gaps are carried verbatim as intervals
 *   (never bridged, never re-derived client-side); a null-valued wire point
 *   never becomes a point, a missing series never becomes zeros, and no
 *   absent datum anywhere becomes 0.
 * - ONE RESOLUTION PER RESPONSE: the model reports the tier the DATA chose
 *   (full vs hourly) and shapes the points accordingly — hourly points carry
 *   their own min/max/n band, sample points are bare {t, v}.
 * - THE COMMANDED TRIPLE: change points become held segments, the house sign
 *   convention is applied for the overlay (charge negative), an all-null
 *   triple stays NULL ("no intent claimed the unit" is a fact, not 0), and
 *   the operator wording names the source in plain words.
 * - HONEST COVERAGE: the window-coverage fraction counts SAMPLED time (each
 *   sample vouches for one cadence interval; each hourly point vouches for
 *   n intervals), is null when unknown or empty, and never fabricates.
 * - THE EMPTY WINDOW'S THREE HONEST EMPTIES: hourly-before-rollups, no
 *   samples at full resolution, and the fresh-database recording state.
 */
import { describe, expect, it } from "vitest";
import {
  COMMANDED_SOURCES,
  commandedSegmentText,
  commandedSegments,
  commandedSourceText,
  commandedWattsSigned,
  coverageFraction,
  emptyWindowNote,
  extremeText,
  gapText,
  HISTORY_FIELDS,
  historyQuery,
  localTime,
  qualityIsClean,
  qualityWordText,
  rangeWindow,
  recordingNote,
  resolutionCaveatText,
  resolutionText,
  toHistoryRecordingState,
  toPlantHistoryWindow,
  type CommandedChange,
  type HistoryUnitBlock,
} from "./history";
import {
  historyBody,
  historySeries,
  historyState,
  historyUnit,
} from "../test/wire";

// --- fixtures ---------------------------------------------------------------------

const WINDOW_FROM = "2026-08-24T00:00:00+00:00";
const WINDOW_TO = "2026-08-24T06:00:00+00:00";
const WINDOW_FROM_MS = Date.parse(WINDOW_FROM);
const WINDOW_TO_MS = Date.parse(WINDOW_TO);

/** A parsed unit straight from a wire unit block. */
function parseUnit(body: unknown): HistoryUnitBlock {
  const window = toPlantHistoryWindow(body);
  const units = window?.units ?? {};
  return units.mid ?? {
    unitId: "mid",
    firstSampleAt: null,
    lastSampleAt: null,
    sampleCount: 0,
    qualityWorst: null,
    gaps: [],
    series: {},
    lifecycleChanges: [],
    healthStateChanges: [],
    commandedChanges: [],
  };
}

// --- range presets ------------------------------------------------------------------

describe("history model — range presets", () => {
  it("resolves the pinned spans against now, window ending at now", () => {
    const now = new Date("2026-08-24T06:00:00Z");
    for (const [range, spanMs] of [
      ["6h", 6 * 3_600_000],
      ["24h", 24 * 3_600_000],
      ["7d", 7 * 86_400_000],
      ["30d", 30 * 86_400_000],
    ] as const) {
      const window = rangeWindow(range, now);
      expect(window.to.getTime()).toBe(now.getTime());
      expect(window.from.getTime()).toBe(now.getTime() - spanMs);
    }
  });

  it("resolves Today as the operator's own site-local midnight to now", () => {
    // 04:30 local on the 24th: today-so-far opens at local midnight.
    const now = new Date("2026-08-24T04:30:00Z");
    const window = rangeWindow("today", now);
    expect(window.to.getTime()).toBe(now.getTime());
    const midnight = new Date(now.getTime());
    midnight.setHours(0, 0, 0, 0);
    expect(window.from.getTime()).toBe(midnight.getTime());
  });

  it("keeps the wire's from < to rule at the midnight boundary", () => {
    const justPastMidnight = new Date("2026-08-24T00:00:20Z");
    justPastMidnight.setHours(0, 0, 20, 0);
    const window = rangeWindow("today", justPastMidnight);
    expect(window.to.getTime()).toBeGreaterThan(window.from.getTime());
    expect(window.to.getTime() - window.from.getTime()).toBeGreaterThanOrEqual(60_000);
  });

  it("builds the query with explicit-offset instants and the pinned field set", () => {
    const now = new Date("2026-08-24T06:00:00Z");
    const query = historyQuery("6h", now);
    expect(query.from).toBe("2026-08-24T00:00:00.000Z");
    expect(query.to).toBe("2026-08-24T06:00:00.000Z");
    // Explicit offsets on both ends (Z is one), every field from the
    // vocabulary's own words, one points target.
    expect(query.fields.split(",")).toEqual([...HISTORY_FIELDS]);
    expect(query.points).toBeGreaterThan(0);
  });
});

// --- window parsing ------------------------------------------------------------------

describe("history model — full-resolution parsing", () => {
  it("reads points as epoch instants with values, stats, gaps, and the meta arrays", () => {
    const body = historyBody({
      resolution: "full",
      from: WINDOW_FROM,
      to: WINDOW_TO,
      units: {
        mid: historyUnit({
          first_sample_at: "2026-08-24T00:00:30+00:00",
          last_sample_at: "2026-08-24T05:59:30+00:00",
          sample_count: 2871,
          quality_worst: "stale",
          gaps: [{ from: "2026-08-24T02:10:00+00:00", to: "2026-08-24T03:40:30+00:00" }],
          series: {
            battery_watts: historySeries(
              [
                { t: "2026-08-24T00:00:30+00:00", v: -521 },
                { t: "2026-08-24T00:01:00+00:00", v: -2503 },
                { t: "2026-08-24T00:01:30+00:00", v: 914 },
              ],
              {
                sample_count: 2871,
                window_min: -2503,
                window_min_at: "2026-08-24T00:41:00+00:00",
                window_max: 914,
                window_max_at: "2026-08-23T19:12:30+00:00",
              },
            ),
          },
          lifecycle_changes: [{ t: "2026-08-24T00:00:30+00:00", v: "disarmed" }],
          health_state_changes: [{ t: "2026-08-24T00:00:30+00:00", v: "healthy" }],
          commanded_changes: [
            { t: "2026-08-24T00:02:00+00:00", source: "night_adviser", direction: "charge", watts: 2500 },
          ],
        }),
      },
    });
    const window = toPlantHistoryWindow(body);
    expect(window).not.toBeNull();
    expect(window?.resolution).toBe("full");
    expect(window?.from).toBe(WINDOW_FROM_MS);
    expect(window?.to).toBe(WINDOW_TO_MS);
    const unit = window?.units.mid;
    expect(unit?.firstSampleAt).toBe(Date.parse("2026-08-24T00:00:30+00:00"));
    expect(unit?.qualityWorst).toBe("stale");
    expect(unit?.gaps).toEqual([
      { from: Date.parse("2026-08-24T02:10:00+00:00"), to: Date.parse("2026-08-24T03:40:30+00:00") },
    ]);
    const series = unit?.series.battery_watts;
    expect(series?.kind).toBe("samples");
    if (series?.kind === "samples") {
      expect(series.points).toEqual([
        { t: Date.parse("2026-08-24T00:00:30+00:00"), v: -521 },
        { t: Date.parse("2026-08-24T00:01:00+00:00"), v: -2503 },
        { t: Date.parse("2026-08-24T00:01:30+00:00"), v: 914 },
      ]);
      // The wire's own extremes ride verbatim — a peak the downsample dropped
      // is still a fact.
      expect(series.stats.windowMin).toBe(-2503);
      expect(series.stats.windowMax).toBe(914);
      expect(series.stats.sampleCount).toBe(2871);
    }
    expect(unit?.lifecycleChanges).toEqual([{ t: Date.parse("2026-08-24T00:00:30+00:00"), v: "disarmed" }]);
    expect(unit?.commandedChanges).toEqual([
      {
        t: Date.parse("2026-08-24T00:02:00+00:00"),
        source: "night_adviser",
        direction: "charge",
        watts: 2500,
      },
    ]);
  });

  it("drops a null-valued wire point — an absent datum is not a zero", () => {
    const unit = parseUnit(
      historyBody({
        resolution: "full",
        units: {
          mid: historyUnit({
            series: {
              bms_soc_pct: {
                points: [
                  { t: "2026-08-24T00:00:30+00:00", v: 97 },
                  { t: "2026-08-24T00:01:00+00:00", v: Number.NaN },
                ],
                sample_count: 2,
                window_min: 97,
                window_min_at: "2026-08-24T00:00:30+00:00",
                window_max: 97,
                window_max_at: "2026-08-24T00:00:30+00:00",
              },
            },
          }),
        },
      }),
    );
    const series = unit.series.bms_soc_pct;
    if (series?.kind === "samples") {
      expect(series.points).toHaveLength(1);
      expect(series.points[0]?.v).toBe(97);
    }
  });

  it("nulls a field the response did not carry, and the window keeps its shape", () => {
    const window = toPlantHistoryWindow(
      historyBody({ resolution: "full", units: { mid: historyUnit() } }),
    );
    expect(window?.units.mid?.series.grid_power_w).toBeUndefined();
    expect(window?.units.mid?.sampleCount).toBe(0);
    expect(window?.units.mid?.qualityWorst).toBeNull();
    expect(window?.fleet).toEqual({ series: {}, gaps: [] });
  });

  it("rejects a body that is not an object at all", () => {
    expect(toPlantHistoryWindow(null)).toBeNull();
    expect(toPlantHistoryWindow("nope")).toBeNull();
  });

  it("keeps the fleet block's summed series and intersection gaps", () => {
    const window = toPlantHistoryWindow(
      historyBody({
        resolution: "full",
        units: { mid: historyUnit() },
        fleet: {
          series: {
            battery_watts: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: -67 }]),
          },
          gaps: [{ from: "2026-08-24T02:10:00+00:00", to: "2026-08-24T02:20:00+00:00" }],
        },
      }),
    );
    expect(window?.fleet?.series.battery_watts?.kind).toBe("samples");
    expect(window?.fleet?.gaps).toHaveLength(1);
  });
});

describe("history model — hourly parsing", () => {
  it("reads each hour's mean with its own min/max band and sample count", () => {
    const window = toPlantHistoryWindow(
      historyBody({
        resolution: "hourly",
        units: {
          mid: historyUnit({
            series: {
              battery_watts: {
                points: [
                  { t: "2026-08-23T22:00:00+00:00", v: -2400, min: -2500, max: -1800, n: 118 },
                  { t: "2026-08-23T23:00:00+00:00", v: -60, min: null, max: null, n: 0 },
                ],
                sample_count: 118,
                window_min: -2500,
                window_min_at: "2026-08-23T22:00:00+00:00",
                window_max: -1800,
                window_max_at: "2026-08-23T22:00:00+00:00",
              },
            },
          }),
        },
      }),
    );
    expect(window?.resolution).toBe("hourly");
    const series = window?.units.mid?.series.battery_watts;
    expect(series?.kind).toBe("hourly");
    if (series?.kind === "hourly") {
      expect(series.points[0]).toEqual({
        t: Date.parse("2026-08-23T22:00:00+00:00"),
        v: -2400,
        min: -2500,
        max: -1800,
        n: 118,
      });
      // An empty-but-present hour keeps its row honestly (n = 0), and the
      // null band rides through as nulls, never zeros.
      expect(series.points[1]?.min).toBeNull();
      expect(series.points[1]?.n).toBe(0);
    }
  });
});

// --- the recording state -------------------------------------------------------------

describe("history model — snapshot history_state", () => {
  it("feature-detects the block: absent means the historian is not composed", () => {
    expect(toHistoryRecordingState({ site_id: "s", units: [] })).toBeNull();
    expect(toHistoryRecordingState(null)).toBeNull();
  });

  it("reads the cadence, retention, and per-unit last-sample instants", () => {
    const state = toHistoryRecordingState({
      history_state: historyState({
        last_sample_at: {
          mid: "2026-08-24T06:03:00+00:00",
          rhs: null,
          lhs: "2026-08-24T06:02:30+00:00",
        },
      }),
    });
    expect(state?.sampleIntervalS).toBe(30);
    expect(state?.retentionFullResolutionDays).toBe(14);
    expect(state?.lastSampleAt.rhs).toBeNull();
    expect(state?.lastSampleAt.mid).toBe(Date.parse("2026-08-24T06:03:00+00:00"));
    expect(state?.lastSampleAtMax).toBe(Date.parse("2026-08-24T06:03:00+00:00"));
  });

  it("carries a no-samples-yet fleet as a null newest sample, not an epoch", () => {
    const state = toHistoryRecordingState({
      history_state: historyState({ last_sample_at: { mid: null, rhs: null } }),
    });
    expect(state?.lastSampleAtMax).toBeNull();
  });
});

// --- coverage -------------------------------------------------------------------------

describe("history model — window coverage", () => {
  it("counts sampled time at full resolution: samples × cadence over window", () => {
    const unit = parseUnit(
      historyBody({
        resolution: "full",
        units: { mid: historyUnit({ sample_count: 10 }) },
      }),
    );
    // 10 × 30 s = 300 s of a 600 s window.
    expect(coverageFraction(unit, "full", 30, 600_000)).toBeCloseTo(0.5, 10);
  });

  it("counts hourly coverage as the rollups' own n, never a full hour per point", () => {
    const unit = parseUnit(
      historyBody({
        resolution: "hourly",
        units: {
          mid: historyUnit({
            series: {
              battery_watts: {
                points: [{ t: "2026-08-23T22:00:00+00:00", v: -2400, min: -2500, max: -1800, n: 60 }],
                sample_count: 60,
                window_min: -2500,
                window_min_at: null,
                window_max: -1800,
                window_max_at: null,
              },
            },
          }),
        },
      }),
    );
    // One half-covered hour (60 × 30 s = 1,800 s) of a 3,600 s window.
    expect(coverageFraction(unit, "hourly", 30, 3_600_000)).toBeCloseTo(0.5, 10);
  });

  it("stays null — never 0 — when the cadence is unknown or nothing sampled", () => {
    const empty = parseUnit(historyBody({ resolution: "full", units: { mid: historyUnit() } }));
    expect(coverageFraction(empty, "full", 30, 600_000)).toBeNull();
    expect(coverageFraction(empty, "full", null, 600_000)).toBeNull();
  });
});

// --- the quality word -------------------------------------------------------------------

describe("history model — the quality word", () => {
  it("rides the wire's own words with one clause of context", () => {
    expect(qualityWordText("good")).toContain("good");
    expect(qualityWordText("stale")).toContain("stale");
    expect(qualityWordText("bad")).toContain("bad");
    expect(qualityWordText("missing")).toContain("missing");
    expect(qualityWordText("suspect")).toContain("suspect");
  });

  it("words the no-samples case and admits unknown words are not clean", () => {
    expect(qualityWordText(null)).toBe("no samples in this window");
    expect(qualityIsClean("good")).toBe(true);
    expect(qualityIsClean("stale")).toBe(false);
    expect(qualityIsClean(null)).toBe(false);
    expect(qualityIsClean("mysterious")).toBe(false);
  });
});

// --- the resolution tier ------------------------------------------------------------------

describe("history model — the resolution badge", () => {
  it("names the tier the data chose, with the cadence when the snapshot carries it", () => {
    expect(resolutionText("full", 30)).toBe("30 s samples");
    expect(resolutionText("full", null)).toBe("full-resolution samples");
    expect(resolutionText("hourly", 30)).toBe("hourly rollup");
  });

  it("states the hourly window's whole-window rule as the badge's caveat", () => {
    expect(resolutionCaveatText("hourly")).toContain("entirety");
    expect(resolutionCaveatText("full")).toContain("real stored sample");
  });
});

// --- the commanded triple --------------------------------------------------------------------

describe("history model — the commanded triple", () => {
  const windowTo = Date.parse("2026-08-24T06:00:00+00:00");

  it("holds each change until the next, the last until the window's end", () => {
    const changes: CommandedChange[] = [
      { t: Date.parse("2026-08-24T00:00:30+00:00"), source: null, direction: null, watts: null },
      { t: Date.parse("2026-08-24T00:02:00+00:00"), source: "night_adviser", direction: "charge", watts: 2500 },
      { t: Date.parse("2026-08-24T04:00:00+00:00"), source: "night_adviser", direction: "charge", watts: 2400 },
    ];
    const segments = commandedSegments(changes, windowTo);
    expect(segments).toHaveLength(3);
    expect(segments[0]?.to).toBe(Date.parse("2026-08-24T00:02:00+00:00"));
    expect(segments[2]?.to).toBe(windowTo);
    expect(segments[1]?.watts).toBe(2500);
  });

  it("keeps the all-null triple null — 'no intent claimed the unit' is a fact, not 0 W", () => {
    const segments = commandedSegments(
      [{ t: Date.parse("2026-08-24T00:00:30+00:00"), source: null, direction: null, watts: null }],
      windowTo,
    );
    expect(segments[0]?.watts).toBeNull();
    expect(commandedWattsSigned(segments[0]!)).toBeNull();
    expect(commandedSegmentText(segments[0]!)).toBe("nothing commanded");
  });

  it("applies the house sign convention for the power overlay and words an idle command", () => {
    expect(commandedWattsSigned({ direction: "charge", watts: 2500 })).toBe(-2500);
    expect(commandedWattsSigned({ direction: "discharge", watts: 800 })).toBe(800);
    expect(commandedWattsSigned({ direction: "idle", watts: 0 })).toBe(0);
    // The operator wording keeps the sign-to-words discipline: a direction
    // WORD plus the magnitude, never a raw negative.
    const text = commandedSegmentText({
      from: 0,
      to: 1,
      source: "night_adviser",
      direction: "charge",
      watts: 2500,
    });
    expect(text).toContain("night-charge adviser");
    expect(text).toContain("charging 2,500 W");
    expect(text).not.toMatch(/-\d/);
  });

  it("decodes every source in the wire's vocabulary", () => {
    for (const source of COMMANDED_SOURCES) {
      expect(commandedSourceText(source)).not.toBe("no command");
    }
    expect(commandedSourceText(null)).toBe("no command");
    expect(commandedSourceText("schedule")).toBe("the schedule");
    expect(commandedSourceText("excess_adviser")).toBe("the solar-surplus adviser");
  });
});

// --- the recording note and the empty window ----------------------------------------------------

describe("history model — the recording note (data age)", () => {
  const now = Date.parse("2026-08-24T06:03:40+00:00");

  it("words the absent snapshot state honestly", () => {
    expect(recordingNote(null, now)).toBe("Recording state not available from the snapshot.");
  });

  it("names the fresh-database case: recording, no samples yet", () => {
    const state = toHistoryRecordingState({
      history_state: historyState({ last_sample_at: { mid: null, rhs: null, lhs: null } }),
    });
    expect(recordingNote(state, now)).toContain("no samples have landed yet");
    expect(recordingNote(state, now)).toContain("30 s");
  });

  it("ages the newest sample and names a recording gap past three cadences", () => {
    const fresh = toHistoryRecordingState({
      history_state: historyState({
        last_sample_at: { mid: "2026-08-24T06:03:12+00:00", rhs: "2026-08-24T06:03:00+00:00", lhs: null },
      }),
    });
    expect(recordingNote(fresh, now)).toContain("28 s ago");
    expect(recordingNote(fresh, now)).toContain("History is recording");
    const quiet = toHistoryRecordingState({
      history_state: historyState({
        last_sample_at: { mid: "2026-08-24T05:00:00+00:00", rhs: null, lhs: null },
      }),
    });
    expect(recordingNote(quiet, now)).toContain("fallen quiet");
  });
});

describe("history model — the empty window's note", () => {
  it("explains an hourly window before any rollup hours exist, and offers the shorter range", () => {
    const state = toHistoryRecordingState({ history_state: historyState() });
    const window = toPlantHistoryWindow(
      historyBody({ resolution: "hourly", units: { mid: historyUnit() } }),
    );
    const note = emptyWindowNote(window!, state);
    expect(note).toContain("No rollup hours cover this window");
    expect(note).toContain("6 h");
    expect(note).toContain("Recording is active");
  });

  it("words a full-resolution window with no samples without inventing a cause", () => {
    const state = toHistoryRecordingState({ history_state: historyState() });
    const window = toPlantHistoryWindow(
      historyBody({ resolution: "full", units: { mid: historyUnit() } }),
    );
    expect(emptyWindowNote(window!, state)).toContain("No samples were recorded in this window");
  });

  it("carries the fresh-database variant when no unit has been sampled", () => {
    const state = toHistoryRecordingState({
      history_state: historyState({ last_sample_at: { mid: null, rhs: null, lhs: null } }),
    });
    const window = toPlantHistoryWindow(
      historyBody({ resolution: "hourly", units: { mid: historyUnit() } }),
    );
    expect(emptyWindowNote(window!, state)).toContain("no samples have landed yet");
  });
});

// --- extremes, local time, gap wording -------------------------------------------------------------

describe("history model — extremes and local wording", () => {
  it("words the window's extremes with their instants, wire-sourced", () => {
    const text = extremeText(
      {
        kind: "samples",
        stats: {
          sampleCount: 2871,
          windowMin: -2503,
          windowMinAt: Date.parse("2026-08-24T00:41:00+00:00"),
          windowMax: 2510,
          windowMaxAt: Date.parse("2026-08-24T01:23:00+00:00"),
        },
        points: [],
      },
      (v) => `${v} W`,
    );
    expect(text).toContain("low -2503 W");
    // Site-local rendering, exactly like every other view: the expected clock
    // time derives from the same Date the model renders with.
    expect(text).toContain(`high 2510 W at ${localTime(Date.parse("2026-08-24T01:23:00+00:00"))}`);
  });

  it("renders site-local clock times through the console's own convention", () => {
    const at = Date.parse("2026-08-24T01:23:00+00:00");
    const date = new Date(at);
    expect(localTime(at)).toBe(
      `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`,
    );
  });

  it("words a gap interval as the accessible note the design pins", () => {
    const gap = {
      from: Date.parse("2026-08-24T02:10:00+00:00"),
      to: Date.parse("2026-08-24T03:40:30+00:00"),
    };
    const from = new Date(gap.from);
    const to = new Date(gap.to);
    expect(gapText(gap, false)).toBe(
      `no samples ${String(from.getHours()).padStart(2, "0")}:${String(from.getMinutes()).padStart(2, "0")}–${String(to.getHours()).padStart(2, "0")}:${String(to.getMinutes()).padStart(2, "0")}`,
    );
  });
});
