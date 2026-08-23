/**
 * `<TimeSeriesChart>` — the History view's one drawing component, and the
 * console's first chart surface. This component is the PINNED SLOT the design
 * fixes (DESIGN_PLANT_HISTORY.md §4.2): a thin imperative leaf that renders
 * aligned data from web/src/views/history/chartData.ts (pure, suite-pinned)
 * and owns nothing about the data's meaning.
 *
 * THE UPLOT DECISION (§4.2's recommendation, taken with evidence): uPlot
 * 1.6.32 — MIT, zero dependencies, 52 KB minified, first-party TypeScript
 * definitions that resolve under this tsconfig — is framework-agnostic DOM,
 * so there is no React peer dependency to break React 19; the console owns
 * exactly this wrapper (~150 lines of useEffect lifetimes, the same shape as
 * any other imperative leaf) and every future chart (Energy Flow's over-time
 * half, imbalance trends) inherits the slot. The server's pinned LTTB means
 * the client renders and never resamples.
 *
 * Honesty rules the slot enforces on every chart:
 *
 * - The x-range is FIXED to the request window, so the space beyond
 *   first/last sample stays empty rather than implying coverage.
 * - spanGaps is false, always: the null markers chartData plants at gap edges
 *   render as breaks — an absent row is never bridged.
 * - The chart is DECORATIVE DUPLICATION: the same facts ride as the summary
 *   table and gap notes under it (UI_CONTRACTS — no color-only encoding, a
 *   text alternative on every chart). The canvas is aria-hidden; the figure's
 *   label and the table carry the content.
 * - No animation exists to honor or suppress: a static drawing is the
 *   reduced-motion-safe drawing (the shell's reduce-motion class needs
 *   nothing special here).
 *
 * WHERE THE CANVAS CANNOT LIVE (jsdom, a blocked 2D context), the component
 * renders the summary table alone with an explicit note — an honest
 * degradation for the behavior suites, never a fake chart.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { BuiltChart } from "./chartData";
import { localDayTime, localTime } from "../../app/history";

/** The axis unit kinds the view asks for; each maps to the display module. */
export type ChartUnit = "watts" | "volts" | "millivolts" | "celsius" | "percent";

/** One drawn column's styling, keyed to the built chart's column key. */
export interface ChartStyle {
  readonly key: string;
  readonly color: string;
  /** Area fill under the line (the battery-watts convention); "" = none. */
  readonly fill?: string;
  readonly dash?: readonly number[];
  readonly stepped?: boolean;
  /** Render as the upper edge of a band toward the named lower column key. */
  readonly bandTo?: string;
  readonly width?: number;
  /** Hidden from the legend (band edges render as one entry via their pair). */
  readonly quiet?: boolean;
  /** The y scale this column plots against (default the first axis's). */
  readonly scale?: string;
}

export interface ChartAxis {
  readonly scale: string;
  readonly unit: ChartUnit;
  /** A pinned domain (SOC's 0–100) instead of the data's own extent. */
  readonly domain?: readonly [number, number];
}

export interface TimeSeriesChartProps {
  /** The figure's accessible name. */
  label: string;
  from: number;
  to: number;
  built: BuiltChart;
  styles: readonly ChartStyle[];
  axes: readonly ChartAxis[];
  height?: number;
  /** >1 day windows tick with day+time labels. */
  multiDay?: boolean;
  /** The honest-figure rows rendered as the chart's text alternative. */
  summary: readonly { key: string; label: string; text: string }[];
  summaryLabel: string;
  /** Accessible gap notes, already worded ("no samples 02:10–03:40 — …"). */
  gapNotes: readonly string[];
  className?: string;
}

/** A step for the x-axis' minute-aligned ticks, picked for ~6 labels. */
function tickStepMs(rangeMs: number): number {
  const minute = 60_000;
  const steps = [
    minute,
    2 * minute,
    5 * minute,
    10 * minute,
    15 * minute,
    30 * minute,
    60 * minute,
    2 * 60 * minute,
    3 * 60 * minute,
    6 * 60 * minute,
    12 * 60 * minute,
    24 * 60 * minute,
    2 * 24 * 60 * minute,
    7 * 24 * 60 * minute,
  ];
  for (const step of steps) {
    if (rangeMs / step <= 7) {
      return step;
    }
  }
  return steps[steps.length - 1]!;
}

function axisValues(unit: ChartUnit): (value: number) => string {
  switch (unit) {
    case "watts":
      return (v) => `${Math.round(v / 100) / 10} kW`;
    case "volts":
      return (v) => `${v.toFixed(1)} V`;
    case "millivolts":
      return (v) => `${Math.round(v)} mV`;
    case "celsius":
      return (v) => `${Math.round(v)} °C`;
    case "percent":
      return (v) => `${Math.round(v)}%`;
  }
}

/** jsdom and blocked contexts answer null here; the component degrades. */
function canvas2dAvailable(): boolean {
  try {
    return document.createElement("canvas").getContext("2d") !== null;
  } catch {
    return false;
  }
}

/**
 * The chart figure: the drawing plus its always-rendered text alternative.
 * The alternative is NOT a fallback for failure only — it is the chart's
 * accessible half on every render (the summary rows and gap notes are facts
 * the canvas only draws).
 */
export function TimeSeriesChart({
  label,
  from,
  to,
  built,
  styles,
  axes,
  height = 240,
  multiDay = false,
  summary,
  summaryLabel,
  gapNotes,
  className = "",
}: TimeSeriesChartProps): ReactNode {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(720);
  const [drawn, setDrawn] = useState(false);
  const canvasOk = useMemo(canvas2dAvailable, []);

  useEffect(() => {
    if (!canvasOk || holderRef.current === null) {
      return undefined;
    }
    const holder = holderRef.current;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0;
      if (next > 0) {
        setWidth(next);
      }
    });
    observer.observe(holder);
    return () => {
      observer.disconnect();
    };
  }, [canvasOk]);

  // The options signature: any change in shape (series, axes, window, size)
  // rebuilds the chart; a data-only change (a re-fetch of the same window
  // shape) streams through setData instead.
  const signature = useMemo(
    () =>
      JSON.stringify({
        columns: built.columns.map((column) => column.key),
        styles: styles.map((style) => [style.key, style.color, style.bandTo ?? "", style.stepped === true]),
        axes: axes.map((axis) => [axis.scale, axis.unit, axis.domain ?? null]),
        from,
        to,
        width,
        height,
        multiDay,
      }),
    [built, styles, axes, from, to, width, height, multiDay],
  );

  const dataSignature = useMemo(
    () => JSON.stringify([built.xs, built.columns.map((column) => column.values)]),
    [built],
  );

  const dataRef = useRef(dataSignature);
  dataRef.current = dataSignature;

  useEffect(() => {
    if (!canvasOk || holderRef.current === null || built.xs.length === 0) {
      return undefined;
    }
    const holder = holderRef.current;
    let disposed = false;
    let chart: { destroy(): void } | null = null;

    void (async () => {
      // uPlot evaluates a matchMedia query at module load; the library is
      // loaded HERE (canvas proven, real browser) so a canvas-less
      // environment never evaluates it at all.
      const { default: UPlot } = await import("uplot");
      if (disposed) {
        return;
      }

    const styleByKey = new Map(styles.map((style) => [style.key, style]));
    const columnKeys = built.columns.map((column) => column.key);
    const indexOfColumn = (key: string): number => columnKeys.indexOf(key);

    // Bands (the hourly min–max envelope) are top-level pairs in this uPlot
    // line: the upper-edge column names its lower partner; both edges stay
    // drawn as lines, the pair adds one soft fill between them.
    const bands: uPlot.Band[] = [];
    for (const style of styles) {
      if (style.bandTo === undefined) {
        continue;
      }
      const lower = indexOfColumn(style.bandTo);
      const upper = indexOfColumn(style.key);
      if (lower >= 0 && upper >= 0) {
        bands.push({
          series: [upper + 1, lower + 1],
          fill: style.fill ?? "rgba(29,95,160,0.12)",
        });
      }
    }

    const series: uPlot.Series[] = [
      { label: "", value: (_u, v) => (v === null ? "" : localTime(v)) },
      ...built.columns.map((column): uPlot.Series => {
        const style = styleByKey.get(column.key);
        const color = style?.color ?? "var(--ink-soft)";
        const stepped =
          style?.stepped === true ? UPlot.paths.stepped?.({ align: 1 }) : undefined;
        const spec: uPlot.Series = {
          label: style?.quiet === true ? "" : column.label,
          stroke: color,
          width: style?.width ?? 1.5,
          spanGaps: false,
          points: { show: false },
        };
        if (style?.scale !== undefined) {
          spec.scale = style.scale;
        }
        if (style?.dash !== undefined) {
          spec.dash = [...style.dash];
        }
        if (style?.fill !== undefined) {
          spec.fill = style.fill;
        }
        if (stepped !== undefined) {
          spec.paths = stepped;
        }
        return spec;
      }),
    ];
    const options: uPlot.Options = {
      width,
      height,
      title: "",
      id: label,
      cursor: { drag: { x: false, y: false } },
      focus: { alpha: 1 },
      bands,
      legend: { show: true, live: true },
      scales: {
        x: { min: from, max: to, time: false },
        ...Object.fromEntries(
          axes.map((axis) => [
            axis.scale,
            axis.domain === undefined
              ? { range: (_u: uPlot, min: number, max: number) => [min, max] as [number, number] }
              : { range: () => [axis.domain![0]!, axis.domain![1]!] as [number, number] },
          ]),
        ),
      },
      axes: [
        {
          // Minute-aligned ticks the instrument way: a clean step from the
          // window's own length, labels in the operator's site-local clock.
          splits: (_u, _axisIdx, scaleMin, scaleMax) => {
            const step = tickStepMs(scaleMax - scaleMin);
            const first = Math.ceil(scaleMin / step) * step;
            const ticks: number[] = [];
            for (let at = first; at <= scaleMax; at += step) {
              ticks.push(at);
            }
            return ticks;
          },
          values: (_u, values) =>
            values.map((value) => (multiDay ? localDayTime(value) : localTime(value))),
          font: "11px 'Segoe UI', system-ui, sans-serif",
          stroke: "#56637a",
          grid: { show: true, stroke: "#d9dee7", width: 1 },
        },
        ...axes.map((axis): uPlot.Axis => {
          const fmt = axisValues(axis.unit);
          return {
            scale: axis.scale,
            values: (_u, values) => values.map((value) => fmt(value)),
            size: 56,
            font: "11px 'Segoe UI', system-ui, sans-serif",
            stroke: "#56637a",
            grid: axis === axes[0] ? { show: true, stroke: "#d9dee7", width: 1 } : { show: false },
          };
        }),
      ],
      series,
    };

      chart = new UPlot(options, [[...built.xs], ...built.columns.map((column) => [...column.values])], holder);
      setDrawn(true);
    })().catch(() => {
      // A failed dynamic load leaves the summary table as the chart's honest
      // half; never a crash into the view.
      setDrawn(false);
    });

    return () => {
      disposed = true;
      setDrawn(false);
      chart?.destroy();
    };
    // The signatures carry every dependency by CONTENT — a parent re-render
    // that hands the same window through fresh arrays rebuilds nothing.
  }, [signature, dataSignature, canvasOk]);

  const figureStyle: CSSProperties = { minHeight: height + 34 };

  return (
    <figure className={`history-chart ${className}`.trim()} style={figureStyle} aria-label={label}>
      <div
        ref={holderRef}
        className="history-chart-canvas"
        data-chart-drawn={drawn ? "true" : "false"}
        aria-hidden="true"
      />
      {!canvasOk || built.xs.length === 0 ? (
        <p className="history-chart-note">
          {canvasOk
            ? "No points to draw in this window."
            : "The chart canvas is unavailable in this environment — the figures below carry the same facts."}
        </p>
      ) : null}
      <figcaption className="history-chart-alt">
        <table className="history-chart-summary">
          <caption className="history-chart-summary-label">{summaryLabel}</caption>
          <tbody>
            {summary.map((row) => (
              <tr key={row.key} data-series={row.key}>
                <th scope="row">{row.label}</th>
                <td>{row.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {gapNotes.length > 0 ? (
          <ul className="history-chart-gaps" aria-label="Recording gaps in this window">
            {gapNotes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        ) : null}
      </figcaption>
    </figure>
  );
}
