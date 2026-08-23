/**
 * "Flow" (Energy Flow) view — the console's live at-a-glance picture of where
 * power is going right now: per phase and fleet-wide, grid import/export,
 * battery charge/discharge, and house load, composed from the snapshot's own
 * per-unit telemetry (`grid_power_w` / `load_power_w` / `battery_watts` /
 * `soc_pct` — every datum already on the wire; no new backend surface).
 *
 * THE FLOW PICTURE: the Whole-site fleet column FIRST (the glance path —
 * story, whole site, phase detail), then one column per phase (a unit is a
 * phase on this three-phase site). Each column is three nodes — GRID,
 * BATTERY, HOME — docked under one phase bus: a horizontal rail (the phase
 * conductor) with three stub slots. Every active stub is a ribbon whose
 * thickness is proportional to watts, whose arrowhead gives the direction,
 * and whose dashes march in the arrowhead's direction at a speed set by the
 * magnitude; color only names the node family. The worded figure sits beside
 * every node ("Importing 412 W", never a raw signed number). Idle states are
 * honest ("Idle" and a hollow ring); absent data says "not available" and is
 * never zero-filled. The SVG is decorative duplication: every fact it draws
 * also exists as text, and each column carries its whole state as an
 * accessible label.
 *
 * THE STORY LINE: one plain sentence composing the measured state, with the
 * strategy advisers' stories riding on top when their projections are present
 * (feature-detected — the night window's demand-hold guarantee and the
 * solar-surplus story compose only while the snapshot carries their state).
 *
 * COMMAND OVERLAY: while a request is live, each commanded phase renders
 * "commanded vs delivering" beside the diagram, resolved through the shared
 * per-unit figures machinery (web/src/app/useUnitIntentFigures.ts) exactly
 * like Home's power cards and Now's request cards.
 *
 * SOLAR HONESTY: the site's PV is not wired to the pods' sensors, so no solar
 * node exists anywhere on this view; export is labeled "exporting" (surplus
 * leaving the site) and the one footnote states the fact.
 *
 * LIVE DATA: the view rides the session's existing cadence — the shell's
 * SharedDataPlane republishes every REST snapshot read (its ~2.5 s measured-
 * data heartbeat included) to this view's stream subscription, and the event
 * frames patch the adviser projections and trigger re-reads when authority
 * changes. The view never mints its own client (one socket per session) and
 * never invents a figure the wire did not carry.
 */
import { Fragment, useCallback, useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, StreamEvent } from "../../api/client";
import { formatPercent, formatWatts } from "../../lib/format";
import { isPlaneSnapshot } from "../../app/SharedDataPlane";
import { usePrefersReducedMotion } from "../../app/usePrefersReducedMotion";
import { useUnitIntentFigures } from "../../app/useUnitIntentFigures";
import {
  arrowScaleW,
  arrowWidthPx,
  batteryFigureParts,
  batteryFlowDirection,
  batteryFlowText,
  commandRows,
  FLOW_SOLAR_FOOTNOTE,
  fleetBatteryFigureParts,
  fleetFlow,
  fleetGridFigureParts,
  fleetHouseFigureParts,
  fleetHouseScope,
  flowStory,
  excessStory,
  gridFigureParts,
  gridFlowDirection,
  gridFlowText,
  hasLiveCommand,
  houseFigureParts,
  houseFlowText,
  lifecycleNote,
  nightStory,
  socText,
  toFlowSnapshot,
  patchFlowAdviser,
  patchFlowNight,
  type FlowFigurePart,
  type FlowSnapshot,
  type FlowUnit,
} from "./flow";
import "./flow.css";

/** The one prop the shell hands every mounted view (views.ts ShellViewProps). */
export interface FlowViewProps {
  client: ApiClient;
}

type ConnectionState = "connecting" | "live" | "lost";
type Phase = "loading" | "ready" | "error";

const RECONNECT_BASE_DELAY_MS = 400;
const RECONNECT_MAX_DELAY_MS = 5000;

/** The authority-changing frames that prove the measured world moved. */
const REFETCH_FRAME_TYPES: readonly string[] = [
  "unit.armed",
  "unit.disarmed",
  "emergency_stop.latched",
  "emergency_stop.acknowledged",
  "authorization.revoked",
  "authorization.granted",
  "intent.accepted",
  "intent.expired",
  "intent.cancelled",
  "inhibit.acknowledged",
  "schedule.replaced",
  "energy.day_rolled",
];

// ---------------------------------------------------------------------------
// the decorative bus (pure SVG; every fact it draws also exists as text)
// ---------------------------------------------------------------------------
//
// Geometry (the design brief's pins, round 2's flatter aspect): one horizontal
// rail — the phase conductor, 3 px, --line, rounded caps — across the top of a
// `0 0 240 84` viewBox at y = 12; three stub slots dropping to the node cards
// at the viewBox's thirds (x = 40 / 120 / 200), each landing on the center of
// the node card docked immediately beneath the SVG. Direction is carried by
// THREE redundant channels — the word beside the node, the arrowhead, and the
// proportional width — while color only ever names the NODE FAMILY (grid
// --active, battery --armed, home --ink-soft; heads all --ink).

/** The bus geometry, in viewBox units (the brief's pinned numbers). */
const RAIL_Y = 12;
const STUB_TOP = 14;
const STUB_BOTTOM = 78;
/** The idle ring's center — just below the rail, centered in the slot. */
const RING_Y = 21;
/** The rim stops this far short of either end, so its caps tuck under the ink
 *  head and stop shy of the rail (the edge shades the ribbon, never the tips). */
const RIM_INSET = 4;
/** The fleet's two-sided slots render two half-slot stubs, offset ±14 px. */
const SPLIT_OFFSET = 14;
/** Strokes at 4 px and above wear the large head; thinner wear the small. */
const LARGE_HEAD_AT_PX = 4;
/** The stub slots, pinned to the thirds so each lands on its card's center. */
const SLOT_X = { grid: 40, battery: 120, home: 200 } as const;
/** The march's speed band: the largest flow on screen cycles in 1.6 s, the smallest in 3.2 s. */
const MARCH_SLOW_MS = 3200;
const MARCH_FAST_MS = 1600;

/** Which way one stub's arrow points — the arrowhead IS the direction. */
type StubAim = "into-bus" | "to-node" | "idle" | "unknown";

/** One stub: its aim, its proportional width, and its share of the scale. */
interface Stub {
  aim: StubAim;
  /** Solid stroke width in viewBox units (ARROW_MIN_PX..ARROW_MAX_PX); 0 when no ribbon draws. */
  widthPx: number;
  /** |watts| / scaleMax (0..1) — sets the march speed; 1 = the fastest. */
  share: number;
}

/** One slot's content: a single stub, or the fleet's two-sided split. */
type SlotSpec =
  | { kind: "single"; stub: Stub }
  | { kind: "split"; intoBus: Stub; toNode: Stub };

const IDLE_STUB: Stub = { aim: "idle", widthPx: 0, share: 0 };
const UNKNOWN_STUB: Stub = { aim: "unknown", widthPx: 0, share: 0 };

function single(stub: Stub): SlotSpec {
  return { kind: "single", stub };
}

/** An active stub: width proportional to watts, march speed proportional to share. */
function activeStub(aim: "into-bus" | "to-node", watts: number, scaleMaxW: number): Stub {
  return {
    aim,
    widthPx: arrowWidthPx(watts, scaleMaxW),
    share: scaleMaxW > 0 ? Math.min(1, Math.abs(watts) / scaleMaxW) : 0,
  };
}

/**
 * One stub mark. The line's own draw direction carries both the arrowhead and
 * the march (the invariant that makes one keyframe serve every stub): an
 * into-bus stub is drawn bottom→top so its head lands on the rail and its
 * dashes march upward; a to-node stub is drawn top→bottom so both point down
 * at the node. Idle draws a hollow ring (connected, nothing moving); unknown
 * draws a 1.5 px dotted line (presence without a claim).
 */
function StubMark({
  cx,
  spec,
  tone,
  smallHead,
  largeHead,
  pulse = false,
}: {
  cx: number;
  spec: Stub;
  tone: "grid" | "battery" | "home";
  smallHead: string;
  largeHead: string;
  pulse?: boolean;
}): ReactNode {
  if (spec.aim === "idle") {
    return (
      <g className={`flow-stub flow-stub--${tone}`} key="idle">
        <circle
          className={pulse ? "flow-idle-ring flow-idle-ring--pulse" : "flow-idle-ring"}
          cx={cx}
          cy={RING_Y}
          r={3.5}
        />
      </g>
    );
  }
  if (spec.aim === "unknown") {
    return (
      <g className="flow-stub" key="unknown">
        <line className="flow-stub-unknown" x1={cx} y1={STUB_TOP} x2={cx} y2={STUB_BOTTOM} />
      </g>
    );
  }
  const intoBus = spec.aim === "into-bus";
  const y1 = intoBus ? STUB_BOTTOM : STUB_TOP;
  const y2 = intoBus ? STUB_TOP : STUB_BOTTOM;
  const head = spec.widthPx >= LARGE_HEAD_AT_PX ? largeHead : smallHead;
  return (
    // The key is the aim: a flip remounts the group (the 150 ms fade-in of a
    // fresh stub) while a magnitude change keeps it (the 400 ms stroke-width
    // transition breathes the new width instead).
    <g className={`flow-stub flow-stub--${tone}`} key={spec.aim}>
      {/* The rim: the family's own hue, deepened, full opacity, 2.6 units
          wider than the ribbon and drawn a few units short of either end —
          the darker edge that keeps a thin gold ribbon reading GOLD at a
          glance instead of beige-on-white (the track alone at partial alpha
          was the round-1 failure the review caught). */}
      <line
        className="flow-stub-rim"
        x1={cx}
        y1={intoBus ? STUB_BOTTOM - RIM_INSET : STUB_TOP + RIM_INSET}
        x2={cx}
        y2={intoBus ? STUB_TOP + RIM_INSET : STUB_BOTTOM - RIM_INSET}
        strokeLinecap="round"
        style={{ strokeWidth: spec.widthPx + 2.6 }}
      />
      <line
        className="flow-stub-track"
        x1={cx}
        y1={y1}
        x2={cx}
        y2={y2}
        strokeLinecap="round"
        markerEnd={`url(#${head})`}
        style={{ strokeWidth: spec.widthPx }}
      />
      <line
        className="flow-stub-march"
        x1={cx}
        y1={y1}
        x2={cx}
        y2={y2}
        strokeLinecap="round"
        style={{
          strokeWidth: spec.widthPx * 0.55,
          animationDuration: `${MARCH_SLOW_MS - MARCH_FAST_MS * spec.share}ms`,
        }}
      />
    </g>
  );
}

/** One node slot: a single stub, or the fleet's two-sided split (±14 px). */
function BusSlot({
  x,
  tone,
  spec,
  smallHead,
  largeHead,
  pulse = false,
}: {
  x: number;
  tone: "grid" | "battery" | "home";
  spec: SlotSpec;
  smallHead: string;
  largeHead: string;
  pulse?: boolean;
}): ReactNode {
  if (spec.kind === "split") {
    return (
      <g key="split">
        <StubMark cx={x - SPLIT_OFFSET} spec={spec.intoBus} tone={tone} smallHead={smallHead} largeHead={largeHead} />
        <StubMark cx={x + SPLIT_OFFSET} spec={spec.toNode} tone={tone} smallHead={smallHead} largeHead={largeHead} />
      </g>
    );
  }
  return <StubMark cx={x} spec={spec.stub} tone={tone} smallHead={smallHead} largeHead={largeHead} pulse={pulse} />;
}

/**
 * One column's phase bus: the rail (the phase itself) with three stub slots —
 * grid, battery, home — each a ribbon whose width is proportional to watts
 * and whose head gives the direction. aria-hidden by design: the worded
 * figures carry the same facts as text right below.
 */
function FlowBus({
  grid,
  battery,
  house,
  batteryPulse = false,
}: {
  grid: SlotSpec;
  battery: SlotSpec;
  house: SlotSpec;
  batteryPulse?: boolean;
}): ReactNode {
  const uid = useId().replace(/[^a-zA-Z0-9-]/g, "");
  const smallHead = `flow-head-sm-${uid}`;
  const largeHead = `flow-head-lg-${uid}`;
  return (
    <svg className="flow-bus" viewBox="0 0 240 84" aria-hidden="true" focusable="false">
      <defs>
        {/* Two heads, one shape: small (8×7) under 4 px of stroke, large
            (12×10) at 4 px and above — a constant single size would put a
            cartoon head on a 2 px trickle. markerUnits="userSpaceOnUse" keeps
            both constant while the stroke width carries the magnitude;
            orient="auto-start-reverse" lets one def serve either aim. One
            neutral ink for every head: the stroke names the node family, the
            head names the direction. */}
        <marker
          id={smallHead}
          viewBox="0 0 8 7"
          refX={7}
          refY={3.5}
          markerWidth={8}
          markerHeight={7}
          markerUnits="userSpaceOnUse"
          orient="auto-start-reverse"
        >
          <path d="M 1 1 L 7 3.5 L 1 6 z" fill="var(--ink)" />
        </marker>
        <marker
          id={largeHead}
          viewBox="0 0 12 10"
          refX={10.5}
          refY={5}
          markerWidth={12}
          markerHeight={10}
          markerUnits="userSpaceOnUse"
          orient="auto-start-reverse"
        >
          <path d="M 1 1.5 L 11 5 L 1 8.5 z" fill="var(--ink)" />
        </marker>
      </defs>
      <line className="flow-bus-rail" x1={16} y1={RAIL_Y} x2={224} y2={RAIL_Y} />
      <BusSlot key="grid" x={SLOT_X.grid} tone="grid" spec={grid} smallHead={smallHead} largeHead={largeHead} />
      <BusSlot
        key="battery"
        x={SLOT_X.battery}
        tone="battery"
        spec={battery}
        smallHead={smallHead}
        largeHead={largeHead}
        pulse={batteryPulse}
      />
      <BusSlot key="home" x={SLOT_X.home} tone="home" spec={house} smallHead={smallHead} largeHead={largeHead} />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// the number tick (§c motion #3): the watt figure tweens 300 ms, ease-out, and
// ONLY while the direction word is unchanged — a state change swaps instantly,
// because rolling a counter across a semantic boundary is a lie. Reduced
// motion snaps (usePrefersReducedMotion gates the tween).
// ---------------------------------------------------------------------------

const NUMBER_TICK_MS = 300;

const hasRequestFrame = typeof requestAnimationFrame === "function";
/** rAF where it exists, a 16 ms timer where it does not (jsdom without visuals). */
function requestNextFrame(step: () => void): number {
  return hasRequestFrame
    ? requestAnimationFrame(step)
    : (setTimeout(step, 16) as unknown as number);
}
function cancelNextFrame(handle: number): void {
  if (hasRequestFrame) {
    cancelAnimationFrame(handle);
  } else {
    clearTimeout(handle);
  }
}

/**
 * The figure line's displayed magnitudes: the target values, tweened from the
 * previous display when — and only when — the words did not change.
 */
function useTickedWatts(parts: readonly FlowFigurePart[], animate: boolean): number[] {
  const [display, setDisplay] = useState<number[]>(() => parts.map((part) => part.watts ?? 0));
  const fromRef = useRef<number[]>(display);
  const previousRef = useRef<readonly FlowFigurePart[]>(parts);
  const key = parts.map((part) => `${part.word}:${part.watts ?? "none"}`).join("|");
  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = parts;
    const target = parts.map((part) => part.watts ?? 0);
    const wordsSteady =
      previous.length === parts.length &&
      parts.every(
        (part, index) => part.word === previous[index]?.word && part.watts !== null && previous[index]?.watts !== null,
      );
    const from = fromRef.current;
    if (
      !animate ||
      !wordsSteady ||
      (from.length === target.length && from.every((value, index) => value === target[index]))
    ) {
      fromRef.current = target;
      setDisplay(target);
      return;
    }
    const origin = from.length === target.length ? from : target;
    const startedAt = performance.now();
    let handle = 0;
    let cancelled = false;
    const step = (): void => {
      if (cancelled) {
        return;
      }
      const t = Math.min(1, (performance.now() - startedAt) / NUMBER_TICK_MS);
      const eased = 1 - (1 - t) ** 3;
      const next = target.map((value, index) => {
        const start = origin[index] ?? value;
        return start + (value - start) * eased;
      });
      fromRef.current = next;
      setDisplay(next);
      if (t < 1) {
        handle = requestNextFrame(step);
      }
    };
    handle = requestNextFrame(step);
    return () => {
      cancelled = true;
      cancelNextFrame(handle);
    };
    // The serialized parts are the dependency: a new array of the same words
    // and magnitudes must not restart anything.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, animate]);
  return display.length === parts.length ? display : parts.map((part) => part.watts ?? 0);
}

/**
 * One worded figure line, its magnitudes ticking between snapshots.
 *
 * Rendered as PARTS — the direction word and its magnitude in sibling spans —
 * so the widest thing in the card is always a wrap point, never an unbreakable
 * string: the card's own measure wraps "Importing / 412 W" while the words and
 * figures stay exactly the pinned `figurePartsText` reading ("Importing 412
 * W", "Charging 3,800 W · Discharging 800 W" — the separator rides between the
 * parts). The desktop's four-across band promotes the two into typographic
 * registers (flow.css ≥77rem): the word a small label line, the figure the
 * larger datum line — the number is what the homeowner scans, the word names
 * it, and neither ever reaches past its card's edge again.
 */
function FlowFigure({
  parts,
  suffix = "",
}: {
  parts: readonly FlowFigurePart[];
  suffix?: string;
}): ReactNode {
  const reducedMotion = usePrefersReducedMotion();
  const display = useTickedWatts(parts, !reducedMotion);
  return (
    <>
      {parts.map((part, index) => {
        const watts = part.watts === null ? null : (display[index] ?? part.watts);
        return (
          <Fragment key={`${index}-${part.word}`}>
            {index === 0 ? null : <span className="flow-fig-sep"> · </span>}
            <span className="flow-fig-part">
              <span className="flow-fig-word">{part.word}</span>
              {watts === null ? null : (
                <>
                  {" "}
                  <span className="flow-fig-num">{formatWatts(watts)}</span>
                </>
              )}
            </span>
          </Fragment>
        );
      })}
      {suffix !== "" ? <span className="flow-fig-scope">{suffix}</span> : null}
    </>
  );
}

/**
 * The battery card's SoC band — real hierarchy for the one per-phase figure
 * the homeowner scans: the percentage large and weight-forward in the battery
 * family's gold, the "charged" word quiet beside it, and a slim honest meter
 * of the same datum beneath (drawn once, never animated). The wording is the
 * pinned "…% charged" string, verbatim.
 */
function SocBand({ socPct }: { socPct: number }): ReactNode {
  return (
    <span className="flow-node-extra flow-node-extra--soc">
      <span className="flow-soc">
        <b className="flow-soc-pct">{formatPercent(socPct)}</b>{" "}
        <span className="flow-soc-word">charged</span>
      </span>
      <span className="flow-soc-meter" aria-hidden="true">
        <span className="flow-soc-fill" style={{ width: `${socPct}%` }} />
      </span>
    </span>
  );
}

/** One node card: the node's name, its worded figure, and the reserved SoC band. */
function FlowNode({
  name,
  parts,
  suffix = "",
  extra = null,
  tone,
}: {
  name: string;
  parts: readonly FlowFigurePart[];
  suffix?: string;
  extra?: ReactNode;
  tone: "grid" | "battery" | "home";
}): ReactNode {
  return (
    <div className={`flow-node flow-node--${tone}`} data-tone={tone} tabIndex={0}>
      <span className="flow-node-name">{name}</span>
      <span className="flow-node-figure">
        <FlowFigure parts={parts} suffix={suffix} />
      </span>
      {/* The SoC band is RESERVED in every card — empty for Grid and Home —
          so the same rows land at the same y across all four columns whether
          or not a card carries a second datum (the aligned visual grid). */}
      {extra ?? <span className="flow-node-extra flow-node-extra--reserved" aria-hidden="true" />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// the view
// ---------------------------------------------------------------------------

export function FlowView({ client }: FlowViewProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [failure, setFailure] = useState<ApiClientError | null>(null);
  const [snapshot, setSnapshot] = useState<FlowSnapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [reloadNonce, setReloadNonce] = useState(0);
  /**
   * The live request's per-unit commanded figures — the ONE shared tracker
   * (web/src/app/useUnitIntentFigures.ts) Home's cards and Now's cards
   * consume too, so the overlay's "commanded" can never drift from them.
   */
  const unitFigures = useUnitIntentFigures();
  const adviserRef = useRef<FlowSnapshot["adviserState"]>(null);
  const nightRef = useRef<FlowSnapshot["nightState"]>(null);
  // The picture sequence + the last consumed stream sequence (the resume
  // cursor): a snapshot that does not advance the picture is never adopted,
  // except the sanctioned renumber of a controller restart.
  const pictureSequenceRef = useRef<number | null>(null);
  const lastSequenceRef = useRef<number | undefined>(undefined);

  const retry = useCallback((): void => {
    setFailure(null);
    setSnapshot(null);
    setConnection("connecting");
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectAttempt = 0;

    setConnection("connecting");

    /** The single adoption path, guarded by sequence (see HomeView's pin). */
    const applySnapshot = (value: unknown, sequence: number | null, allowRenumber = false): boolean => {
      const parsed = toFlowSnapshot(value);
      if (parsed === null) {
        return false;
      }
      const incoming = sequence ?? parsed.sequence;
      const current = pictureSequenceRef.current;
      if (current !== null && incoming <= current && !(allowRenumber && incoming < current)) {
        return false;
      }
      pictureSequenceRef.current = incoming;
      adviserRef.current = parsed.adviserState;
      nightRef.current = parsed.nightState;
      lastSequenceRef.current = incoming;
      setSnapshot(parsed);
      setFailure(null);
      setPhase("ready");
      return true;
    };

    /** A fresher read after a world-moving frame; adopted only if it advances. */
    const refetchSnapshot = (): void => {
      const read =
        typeof client.refreshSnapshot === "function" ? client.refreshSnapshot() : client.getSnapshot();
      read
        .then((value) => {
          if (!cancelled) {
            unitFigures.adoptSnapshot(value);
            applySnapshot(value, null);
          }
        })
        .catch(() => {
          // The last known picture stays; the reconnect path retries anyway.
        });
    };

    /** The frames this view reads beyond its snapshot adoption. */
    const applyEventFrame = (frame: StreamEvent): void => {
      // The shared tracker eats every frame: acceptances and control-decision
      // audits feed the commanded maps; request-ending frames clear them.
      unitFigures.consumeEvent(frame);
      const payload = (frame.payload ?? null) as Record<string, unknown> | null;
      if (frame.type === "excess_adviser.state_changed") {
        // The projection patches state-locally — watt figures ride every
        // publication; only an enabled/active flip re-reads the world.
        const previous = adviserRef.current;
        const next = patchFlowAdviser(previous, payload);
        if (next !== null && next !== previous) {
          adviserRef.current = next;
          setSnapshot((prior) => (prior === null ? prior : { ...prior, adviserState: next }));
          const flipped =
            previous === null || previous.enabled !== next.enabled || previous.active !== next.active;
          if (flipped) {
            refetchSnapshot();
          }
        }
        return;
      }
      if (frame.type === "night_charge.state_changed") {
        // The night adviser's own frame, same mechanics as the excess tile's.
        const previous = nightRef.current;
        const next = patchFlowNight(previous, payload);
        if (next !== null && next !== previous) {
          nightRef.current = next;
          setSnapshot((prior) => (prior === null ? prior : { ...prior, nightState: next }));
          const flipped =
            previous === null || previous.enabled !== next.enabled || previous.active !== next.active;
          if (flipped) {
            refetchSnapshot();
          }
        }
        return;
      }
      if (REFETCH_FRAME_TYPES.includes(frame.type)) {
        refetchSnapshot();
      }
    };

    const onStreamLost = (): void => {
      if (cancelled) {
        return;
      }
      reconnectAttempt += 1;
      setConnection("lost");
      const delay = Math.min(
        RECONNECT_BASE_DELAY_MS * 2 ** (reconnectAttempt - 1),
        RECONNECT_MAX_DELAY_MS,
      );
      reconnectTimer = setTimeout(() => {
        void connect(lastSequenceRef.current);
      }, delay);
    };

    async function connect(cursor: number | undefined): Promise<void> {
      if (cancelled) {
        return;
      }
      // A connection opened with a cursor is a resume: its first snapshot may
      // carry a renumbered (lower) sequence after a controller restart and is
      // the new world, never a stale one.
      const resumed = cursor !== undefined;
      let firstSnapshot = true;
      try {
        const stream = client.openEvents(cursor);
        for await (const frame of stream) {
          if (cancelled) {
            return;
          }
          if (typeof frame.sequence === "number") {
            lastSequenceRef.current = frame.sequence;
          }
          if (frame.type === "resync_required") {
            // Refetch once, then reconnect from the recovery cursor — never a
            // replay from zero.
            const recovery =
              typeof frame.snapshot_sequence === "number"
                ? frame.snapshot_sequence
                : lastSequenceRef.current;
            try {
              const fresh = await client.getSnapshot();
              if (cancelled) {
                return;
              }
              unitFigures.adoptSnapshot(fresh);
              applySnapshot(fresh, null);
            } catch {
              // The last known picture stays; reconnect anyway.
            }
            void connect(recovery);
            return;
          }
          if (frame.type === "snapshot") {
            unitFigures.adoptSnapshot(frame.data);
            applySnapshot(
              frame.data,
              typeof frame.sequence === "number" ? frame.sequence : null,
              resumed && firstSnapshot,
            );
            firstSnapshot = false;
            // A plane-republished REST read is data, not liveness: a frame the
            // plane marked stale must never claim "live".
            if (!(isPlaneSnapshot(frame) && frame.stale)) {
              setConnection("live");
            }
            continue;
          }
          setConnection("live");
          if (
            typeof frame.sequence === "number" &&
            pictureSequenceRef.current !== null &&
            frame.sequence <= pictureSequenceRef.current
          ) {
            // Already part of the picture on screen (a republished frame from
            // the shared stream): applying it again could rewind a newer world.
            continue;
          }
          applyEventFrame(frame);
          if (typeof frame.sequence === "number") {
            pictureSequenceRef.current = Math.max(
              pictureSequenceRef.current ?? frame.sequence,
              frame.sequence,
            );
          }
        }
        onStreamLost();
      } catch {
        onStreamLost();
      }
    }

    async function load(): Promise<void> {
      setPhase("loading");
      setConnection("connecting");
      try {
        const value = await client.getSnapshot();
        if (cancelled) {
          return;
        }
        unitFigures.adoptSnapshot(value);
        if (!applySnapshot(value, null)) {
          setFailure(
            new ApiClientError({
              code: "invalid_snapshot",
              message: "The snapshot response was not readable",
              details: null,
              request_id: "",
              status: 0,
            }),
          );
          setPhase("error");
          return;
        }
      } catch (error) {
        if (cancelled) {
          return;
        }
        setFailure(
          error instanceof ApiClientError
            ? error
            : new ApiClientError({
                code: "unexpected_failure",
                message: error instanceof Error ? error.message : "The request failed",
                details: null,
                request_id: "",
                status: 0,
              }),
        );
        setPhase("error");
        return;
      }
      void connect(lastSequenceRef.current);
    }

    void load();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
    };
    // The tracker's callbacks are stable (setState-only inside), so this runs
    // once per client/session — the same mount discipline as every other view.
  }, [client, reloadNonce]);

  const headingId = useId();

  if (phase === "error") {
    return (
      <section className="flow-view" aria-labelledby={headingId}>
        <h2 id={headingId}>Energy flow</h2>
        <div role="alert" className="flow-error">
          <p>
            <code>{failure?.code ?? "unexpected_failure"}</code>
          </p>
          <p>{failure?.message ?? "The flow picture could not be loaded."}</p>
          <button type="button" onClick={retry}>
            Retry
          </button>
        </div>
      </section>
    );
  }

  if (snapshot === null) {
    return (
      <section className="flow-view" aria-labelledby={headingId}>
        <h2 id={headingId}>Energy flow</h2>
        <p role="status" className="flow-loading">
          Loading the live power picture…
        </p>
      </section>
    );
  }

  const units = snapshot.units;
  const fleet = fleetFlow(units);
  const scale = arrowScaleW(units, fleet);

  // The story: the advisers' own words while their projections say a window
  // is running (feature-detected), the measured composition otherwise — with
  // a live request named when it is what the flows are carrying out.
  let story =
    (snapshot.nightState !== null ? nightStory(snapshot.nightState, fleet) : null) ??
    (snapshot.adviserState !== null ? excessStory(snapshot.adviserState, fleet) : null);
  if (story === null) {
    story = flowStory(units, fleet);
    if (hasLiveCommand(units, unitFigures.authorizedByUnit)) {
      story = `Carrying out a power request — ${story}`;
    }
  }

  const commands = commandRows(units, unitFigures.authorizedByUnit, unitFigures.directionsByUnit);
  // The phases a live request has commanded but whose batteries have not
  // moved yet ("not moving yet" in the overlay) — their idle ring pulses.
  const commandedNotMoving = new Set(
    commands.filter((row) => row.measuredW === 0).map((row) => row.unitId),
  );
  const connectionText =
    connection === "live"
      ? "Live — this picture refreshes every few seconds."
      : connection === "lost"
        ? "Connection lost — showing the last known picture; reconnecting automatically."
        : "Connecting to live updates…";

  // The fleet column's slots. The fleet is the SUMMED picture, so its Grid and
  // Battery slots may legally show both directions at once — and when the
  // phases pull apart, that slot SPLITS into two half-slot stubs (±14 px), the
  // import/discharging side head-up with its own width and the export/charging
  // side head-down with its own: two opposite flows are never one thickness.
  // The worded figures below the bus carry the exact split either way.
  const fleetGridSlot: SlotSpec =
    fleet.gridReporting === 0
      ? single(UNKNOWN_STUB)
      : (fleet.importW ?? 0) > 0 && (fleet.exportW ?? 0) > 0
        ? {
            kind: "split",
            intoBus: activeStub("into-bus", fleet.importW ?? 0, scale),
            toNode: activeStub("to-node", fleet.exportW ?? 0, scale),
          }
        : (fleet.importW ?? 0) > 0
          ? single(activeStub("into-bus", fleet.importW ?? 0, scale))
          : (fleet.exportW ?? 0) > 0
            ? single(activeStub("to-node", fleet.exportW ?? 0, scale))
            : single(IDLE_STUB);
  const fleetBatterySlot: SlotSpec =
    fleet.batteryReporting === 0
      ? single(UNKNOWN_STUB)
      : (fleet.chargingW ?? 0) > 0 && (fleet.dischargingW ?? 0) > 0
        ? {
            kind: "split",
            intoBus: activeStub("into-bus", fleet.dischargingW ?? 0, scale),
            toNode: activeStub("to-node", fleet.chargingW ?? 0, scale),
          }
        : (fleet.chargingW ?? 0) > 0
          ? single(activeStub("to-node", fleet.chargingW ?? 0, scale))
          : (fleet.dischargingW ?? 0) > 0
            ? single(activeStub("into-bus", fleet.dischargingW ?? 0, scale))
            : single(IDLE_STUB);
  const fleetHouseSlot: SlotSpec =
    fleet.houseW === null
      ? single(UNKNOWN_STUB)
      : fleet.houseW > 0
        ? single(activeStub("to-node", fleet.houseW, scale))
        : single(IDLE_STUB);

  return (
    <section className="flow-view" aria-labelledby={headingId} data-connection={connection}>
      <div className="flow-heading-row">
        <h2 id={headingId}>Energy flow</h2>
        <p role="status" className={`flow-connection flow-connection--${connection}`}>
          {connectionText}
        </p>
      </div>

      {/* THE STORY LINE — one plain sentence composing the current state. */}
      <p className="flow-story">{story}</p>

      {units.length === 0 ? (
        <p className="flow-empty">
          No batteries are connected yet — the flow picture appears as soon as a pod checks in.
        </p>
      ) : (
        <div className="flow-diagram" role="group" aria-label="Power flow by phase">
          {/* THE FLEET COLUMN — FIRST in DOM and reading order: the glance
              path is story → whole site → phase detail, which is also the
              order a screen reader speaks, and the only sane order when the
              grid has collapsed to one column. */}
          <section className="flow-phase flow-phase--fleet" aria-label="Whole site">
            <h3 className="flow-phase-name">Whole site</h3>
            <div className="flow-dock">
              <FlowBus grid={fleetGridSlot} battery={fleetBatterySlot} house={fleetHouseSlot} />
              <div className="flow-nodes">
                <FlowNode name="Grid" parts={fleetGridFigureParts(fleet)} tone="grid" />
                <FlowNode name="Battery" parts={fleetBatteryFigureParts(fleet)} tone="battery" />
                <FlowNode
                  name="Home"
                  parts={fleetHouseFigureParts(fleet)}
                  suffix={fleetHouseScope(fleet)}
                  tone="home"
                />
              </div>
            </div>
          </section>

          {units.map((unit) => (
            <PhaseColumn
              key={unit.unitId}
              unit={unit}
              scale={scale}
              batteryPulse={commandedNotMoving.has(unit.unitId)}
            />
          ))}
        </div>
      )}

      {/* THE COMMAND OVERLAY — commanded vs measured per phase, only while a
          request is live (the shared per-unit figures machinery). */}
      {commands.length > 0 ? (
        <section className="flow-commands" aria-labelledby="flow-commands-heading">
          <h3 id="flow-commands-heading">Commanded vs delivering</h3>
          <ul>
            {commands.map((row) => (
              <li key={row.unitId}>{row.text}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* SOLAR HONESTY — the one footnote, stated once. */}
      <p role="note" className="flow-footnote">
        {FLOW_SOLAR_FOOTNOTE}
      </p>
    </section>
  );
}

/** One phase's column: the bus plus the three nodes, all worded. */
function PhaseColumn({
  unit,
  scale,
  batteryPulse,
}: {
  unit: FlowUnit;
  scale: number;
  batteryPulse: boolean;
}): ReactNode {
  const gridW = unit.gridPowerW;
  const batteryW = unit.batteryWatts;
  const loadW = unit.loadPowerW;
  // A phase with no contact keeps its last-known worded figures — dimmed,
  // never gone — while its stubs drop to dotted-unknown: the numbers stay,
  // the arrows stop claiming a live flow they cannot vouch for.
  const down = unit.lifecycle === "disconnected";
  const gridDirection = gridFlowDirection(gridW);
  const batteryDirection = batteryFlowDirection(batteryW);
  const soc = socText(unit.socPct);
  const note = lifecycleNote(unit.lifecycle);
  const gridSlot: SlotSpec =
    down || gridDirection === "unknown"
      ? single(UNKNOWN_STUB)
      : gridDirection === "import"
        ? single(activeStub("into-bus", gridW ?? 0, scale))
        : gridDirection === "export"
          ? single(activeStub("to-node", gridW ?? 0, scale))
          : single(IDLE_STUB);
  const batterySlot: SlotSpec =
    down || batteryDirection === "unknown"
      ? single(UNKNOWN_STUB)
      : batteryDirection === "charge"
        ? single(activeStub("to-node", batteryW ?? 0, scale))
        : batteryDirection === "discharge"
          ? single(activeStub("into-bus", batteryW ?? 0, scale))
          : single(IDLE_STUB);
  const houseSlot: SlotSpec =
    down || loadW === null
      ? single(UNKNOWN_STUB)
      : loadW > 0
        ? single(activeStub("to-node", loadW, scale))
        : single(IDLE_STUB);
  const gridText = gridFlowText(gridW);
  const batteryText = batteryFlowText(batteryW);
  const houseText = houseFlowText(loadW);
  return (
    <section
      className={`flow-phase${down ? " flow-phase--down" : ""}`}
      aria-label={`Phase ${unit.unitId} — grid: ${gridText}; battery: ${batteryText}${
        soc === "" ? "" : ` (${soc})`
      }; house: ${houseText}`}
    >
      <h3 className="flow-phase-name">{unit.unitId}</h3>
      <div className="flow-dock">
        <FlowBus grid={gridSlot} battery={batterySlot} house={houseSlot} batteryPulse={batteryPulse} />
        <div className="flow-nodes">
          <FlowNode name="Grid" parts={gridFigureParts(gridW)} tone="grid" />
          <FlowNode
            name="Battery"
            parts={batteryFigureParts(batteryW)}
            extra={unit.socPct === null ? null : <SocBand socPct={unit.socPct} />}
            tone="battery"
          />
          <FlowNode name="Home" parts={houseFigureParts(loadW)} tone="home" />
        </div>
      </div>
      {/* The lifecycle note anchors the column's foot — under the figures it
          vouches for — so its presence never shifts any other column's rows. */}
      {note !== null ? <p className="flow-phase-note">{note}</p> : null}
    </section>
  );
}
