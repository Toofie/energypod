/**
 * The pure marshalling between the history view model
 * (web/src/app/history.ts) and an aligned time-series chart — everything the
 * drawing layer needs and nothing it may decide on its own:
 *
 * - ONE SHARED TIME AXIS per chart: the sorted union of every series' own
 *   instants (the wire's series carry per-series timestamps — a null datum
 *   skips a sample, so the timestamps differ) plus the boundary instants of
 *   every GAP. A series that has no sample at an axis instant holds NULL
 *   there, never 0 and never a carried-forward value.
 * - GAPS BREAK THE LINE: for each gap interval {from, to}, two axis instants
 *   land at the gap's edges with null in EVERY column, so a renderer with
 *   spanGaps=false shows an honest break where no rows exist — the pinned
 *   doctrine (absent rows are never interpolated, and the leading/trailing
 *   space beyond first/last sample stays empty via the chart's fixed x-range).
 * - HOURLY BANDS: an hourly series contributes its mean column plus its own
 *   min/max columns (a missing hour is simply absent from the arrays — an
 *   absent hour is a gap, never a zeroed hour).
 * - STEPS: a step spec (the commanded overlay) expands its held segments
 *   stair-wise — the held value at both edges of every segment, null across
 *   the segments where nothing was claimed.
 *
 * Everything here is pure and synchronous: the behavior suite pins the
 * shapes, the drawing component consumes them, and no renderer detail leaks
 * back into the model.
 */
import type { CommandedSegment, HistoryGap, HistoryHourlyPoint, HistorySamplePoint } from "../../app/history";

/** A plain value line at sample resolution ({t, v} pairs, own timestamps). */
export interface LineSpec {
  readonly kind: "line";
  readonly key: string;
  readonly label: string;
  readonly points: readonly HistorySamplePoint[];
}

/** An hourly rollup: a mean line plus its own min/max band columns. */
export interface HourlySpec {
  readonly kind: "hourly";
  readonly key: string;
  readonly label: string;
  readonly points: readonly HistoryHourlyPoint[];
}

/** A held-step overlay (the commanded watts projection, sign applied by the caller). */
export interface StepSpec {
  readonly kind: "step";
  readonly key: string;
  readonly label: string;
  readonly segments: readonly { from: number; to: number; value: number | null }[];
}

export type ChartSpec = LineSpec | HourlySpec | StepSpec;

/** One rendered column: its identity, its legend label, its aligned values. */
export interface ChartColumn {
  readonly key: string;
  readonly label: string;
  readonly values: readonly (number | null)[];
}

export interface BuiltChart {
  /** The shared, ascending, de-duplicated axis instants (epoch ms). */
  readonly xs: readonly number[];
  /** Every column in spec order (hourly specs contribute mean/min/max). */
  readonly columns: readonly ChartColumn[];
}

/** Insert one instant into the ascending axis if absent (binary search). */
function insertAscending(xs: number[], at: number): void {
  let low = 0;
  let high = xs.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (xs[mid]! < at) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  if (xs[low] === at) {
    return;
  }
  xs.splice(low, 0, at);
}

/**
 * Build one chart's aligned data. The window's [from, to] is NOT part of the
 * axis — the renderer fixes the x-range from the window so the space beyond
 * the first/last sample renders empty rather than implying coverage.
 */
export function buildChart(
  specs: readonly ChartSpec[],
  gaps: readonly HistoryGap[] = [],
): BuiltChart {
  const xs: number[] = [];
  const valueAt = new Map<string, Map<number, number | null>>();
  const columns: { key: string; label: string; values: (number | null)[] }[] = [];
  const columnByIndex: { column: (number | null)[]; map: Map<number, number | null> }[] = [];

  const openColumn = (key: string, label: string): { column: (number | null)[]; map: Map<number, number | null> } => {
    const map = new Map<number, number | null>();
    const column: (number | null)[] = [];
    columns.push({ key, label, values: column });
    columnByIndex.push({ column, map });
    valueAt.set(key, map);
    return { column, map };
  };

  for (const spec of specs) {
    if (spec.kind === "line") {
      const mean = openColumn(spec.key, spec.label);
      for (const point of spec.points) {
        insertAscending(xs, point.t);
        mean.map.set(point.t, point.v);
      }
      continue;
    }
    if (spec.kind === "hourly") {
      const mean = openColumn(spec.key, spec.label);
      const minimum = openColumn(`${spec.key}#min`, `${spec.label} (low)`);
      const maximum = openColumn(`${spec.key}#max`, `${spec.label} (high)`);
      for (const point of spec.points) {
        insertAscending(xs, point.t);
        mean.map.set(point.t, point.v);
        // An absent band rides as null — a null min/max is the wire saying
        // "the hour never carried this field", never 0.
        minimum.map.set(point.t, point.min);
        maximum.map.set(point.t, point.max);
      }
      continue;
    }
    // A step spec: ONE held value per segment edge (the value at its `from`
    // holds until the next edge) — the renderer owns the stair shape through
    // its stepped path builder, so no doubled instants are fabricated here.
    // The final segment's `to` closes the stair at the window edge. A null-
    // valued segment holds null across its span — the overlay breaks where
    // nothing was claimed, it never draws 0 W.
    const step = openColumn(spec.key, spec.label);
    for (const segment of spec.segments) {
      insertAscending(xs, segment.from);
      step.map.set(segment.from, segment.value);
    }
    const last = spec.segments[spec.segments.length - 1];
    if (last !== undefined) {
      insertAscending(xs, last.to);
      if (!step.map.has(last.to)) {
        step.map.set(last.to, last.value);
      }
    }
  }

  // The gap edges: null in EVERY column at both edges of every gap, so the
  // renderer breaks every line across the interval (spanGaps=false).
  for (const gap of gaps) {
    insertAscending(xs, gap.from);
    insertAscending(xs, gap.to);
    for (const entry of columnByIndex) {
      if (!entry.map.has(gap.from)) {
        entry.map.set(gap.from, null);
      }
      if (!entry.map.has(gap.to)) {
        entry.map.set(gap.to, null);
      }
    }
  }

  for (const entry of columnByIndex) {
    entry.column.length = 0;
    for (const at of xs) {
      entry.column.push(entry.map.get(at) ?? null);
    }
  }

  return { xs, columns };
}

/**
 * The honest summary rows a chart's accessible alternative carries: the
 * wire's own window extremes with their instants (a peak the downsample
 * dropped is still a fact), the sample count, and — at hourly resolution —
 * the coverage the rollups' own n vouches for.
 */
export interface SeriesSummaryRow {
  readonly key: string;
  readonly label: string;
  readonly text: string;
}

/** The step segments of a commanded overlay as signed chart segments. */
export function commandedStepSegments(
  segments: readonly CommandedSegment[],
  signed: (segment: CommandedSegment) => number | null,
): readonly { from: number; to: number; value: number | null }[] {
  return segments.map((segment) => ({
    from: segment.from,
    to: segment.to,
    value: signed(segment),
  }));
}
