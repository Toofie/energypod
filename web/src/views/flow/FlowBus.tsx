/**
 * The flow bus — one column's phase conductor and its three stub slots,
 * extracted from FlowView.tsx in round 2. THE GRAMMAR SWAP lives here:
 *
 * THE THICKNESS RIBBONS ARE RETIRED. The old rim/track/march <line> trio —
 * the proportional-width channel ("I don't like the lines representing the
 * bars of like thickness") — is gone. What replaces it:
 *
 *   - The SVG keeps the STRUCTURE: the conduit chassis + rail, terminal
 *     collars, idle rings (+ the commanded pulse), unknown dotted stubs, and
 *     the neutral arrowheads (#e8eef6) — now markerEnd on each ACTIVE slot's
 *     curved conduit path, so a head orients along the curve tangent instead
 *     of sitting on a straight pill.
 *   - A <canvas> underlay (flow-bus-glow) is the LIGHT ENGINE's surface
 *     (glow.ts): living particle streams whose density, speed, radius, and
 *     glow ∝ share; terminal bloom pools; rail energization on through-flow.
 *   - Magnitude survives in words (every figure is text below), in motion
 *     (the streams), and at the heads only as two discrete steps: a stub
 *     wears the large head when its retired ribbon width would have reached
 *     LARGE_HEAD_AT_PX (share ≥ 0.25 on the old 2→10 px map).
 *
 * Direction stays tri-redundant: the word beside the node, the arrowhead,
 * and now the streams' own travel. Color still names the NODE FAMILY only.
 * aria-hidden throughout: every fact is duplicated as text.
 */
import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { arrowWidthPx } from "./flow";
import { usePrefersReducedMotion } from "../../app/usePrefersReducedMotion";
import {
  BUS_VIEWBOX,
  LARGE_HEAD_AT_PX,
  RAIL_Y,
  RING_Y,
  SLOT_X,
  STUB_BOTTOM,
  STUB_TOP,
  SPLIT_OFFSET,
  conduitPath,
  pathToD,
} from "./flowGeometry";
import { FlowGlowRenderer } from "./glow";
import type { GlowStubSpec, RailThroughSpec } from "./glow";

/** Which way one stub points — the head IS the direction (idle/unknown draw no stream). */
export type StubAim = "into-bus" | "to-node" | "idle" | "unknown";

/** One stub: its aim, its share of the view's scale, and its retired-width
 *  measure — kept ONLY to pick the small/large head exactly as before. */
export interface Stub {
  aim: StubAim;
  /** What ARROW_MIN_PX..ARROW_MAX_PX would have been; head-size selector only. */
  widthPx: number;
  /** |watts| / scaleMax (0..1) — the engine's density/speed/glow driver. */
  share: number;
}

/** One slot's content: a single stub, or the fleet's two-sided split. */
export type SlotSpec =
  | { kind: "single"; stub: Stub }
  | { kind: "split"; intoBus: Stub; toNode: Stub };

export const IDLE_STUB: Stub = { aim: "idle", widthPx: 0, share: 0 };
export const UNKNOWN_STUB: Stub = { aim: "unknown", widthPx: 0, share: 0 };

export function single(stub: Stub): SlotSpec {
  return { kind: "single", stub };
}

/** An active stub: share drives the streams; widthPx survives for head sizing. */
export function activeStub(aim: "into-bus" | "to-node", watts: number, scaleMaxW: number): Stub {
  return {
    aim,
    widthPx: arrowWidthPx(watts, scaleMaxW),
    share: scaleMaxW > 0 ? Math.min(1, Math.abs(watts) / scaleMaxW) : 0,
  };
}

/**
 * One stub mark. Idle draws a hollow ring (connected, nothing moving);
 * unknown draws the dotted presence line; active draws its curved conduit
 * path with the neutral arrowhead docked at the flow end — the static half
 * of the stream grammar (the canvas paints its living half).
 */
function StubMark({
  cx,
  spec,
  tone,
  smallHead,
  largeHead,
  splitSide,
  pulse = false,
}: {
  cx: number;
  spec: Stub;
  tone: "grid" | "battery" | "home";
  smallHead: string;
  largeHead: string;
  splitSide?: "into-bus" | "to-node";
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
  const head = spec.widthPx >= LARGE_HEAD_AT_PX ? largeHead : smallHead;
  return (
    // The key is the aim: a flip remounts the group (the 150 ms fade-in of a
    // fresh conduit) while a share change keeps it (the engine glides there).
    <g className={`flow-stub flow-stub--${tone}`} key={spec.aim}>
      <path className={`flow-conduit flow-conduit--${tone}`} d={pathToD(conduitPath(cx, spec.aim, splitSide))} markerEnd={`url(#${head})`} />
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
        <StubMark
          cx={x - SPLIT_OFFSET}
          spec={spec.intoBus}
          tone={tone}
          smallHead={smallHead}
          largeHead={largeHead}
          splitSide="into-bus"
        />
        <StubMark
          cx={x + SPLIT_OFFSET}
          spec={spec.toNode}
          tone={tone}
          smallHead={smallHead}
          largeHead={largeHead}
          splitSide="to-node"
        />
      </g>
    );
  }
  return <StubMark cx={x} spec={spec.stub} tone={tone} smallHead={smallHead} largeHead={largeHead} pulse={pulse} />;
}

/** The engine-facing picture of this bus: active streams + rail through-flow. */
interface DerivedGlowSpec {
  stubs: GlowStubSpec[];
  railThrough: RailThroughSpec | null;
}

/** Pure derivation from the three slot specs — rebuilt per render, compared
 *  by key so the engine only hears real changes. */
function deriveGlowSpec(grid: SlotSpec, battery: SlotSpec, house: SlotSpec): DerivedGlowSpec {
  const stubs: GlowStubSpec[] = [];
  const slots: readonly { readonly tone: "grid" | "battery" | "home"; readonly spec: SlotSpec; readonly x: number }[] = [
    { tone: "grid", spec: grid, x: SLOT_X.grid },
    { tone: "battery", spec: battery, x: SLOT_X.battery },
    { tone: "home", spec: house, x: SLOT_X.home },
  ];
  let intoSum = 0;
  let outSum = 0;
  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  const note = (stub: GlowStubSpec): void => {
    stubs.push(stub);
    // One end of every path touches the rail — that end feeds the segment.
    const railX = stub.aim === "into-bus" ? stub.path.p3.x : stub.path.p0.x;
    minX = Math.min(minX, railX);
    maxX = Math.max(maxX, railX);
    if (stub.aim === "into-bus") {
      intoSum += stub.share;
    } else {
      outSum += stub.share;
    }
  };
  for (const slot of slots) {
    if (slot.spec.kind === "split") {
      note({ tone: slot.tone, aim: "into-bus", path: conduitPath(slot.x, "into-bus", "into-bus"), share: slot.spec.intoBus.share });
      note({ tone: slot.tone, aim: "to-node", path: conduitPath(slot.x, "to-node", "to-node"), share: slot.spec.toNode.share });
    } else if (slot.spec.stub.aim === "into-bus") {
      note({ tone: slot.tone, aim: "into-bus", path: conduitPath(slot.x, "into-bus"), share: slot.spec.stub.share });
    } else if (slot.spec.stub.aim === "to-node") {
      note({ tone: slot.tone, aim: "to-node", path: conduitPath(slot.x, "to-node"), share: slot.spec.stub.share });
    }
  }
  const railThrough =
    intoSum > 0 && outSum > 0
      ? {
          x1: Math.max(16, minX - 8),
          x2: Math.min(224, maxX + 8),
          intensity: Math.min(1, Math.min(intoSum, outSum)),
        }
      : null;
  return { stubs, railThrough };
}

function serializeGlowSpec(derived: DerivedGlowSpec): string {
  const stubKeys = derived.stubs.map(
    (stub) =>
      `${stub.tone}:${stub.aim}:${stub.share.toFixed(4)}:${stub.path.p0.x},${stub.path.p0.y}>${stub.path.p3.x},${stub.path.p3.y}`,
  );
  const railKey =
    derived.railThrough === null
      ? "-"
      : `${derived.railThrough.x1.toFixed(2)}~${derived.railThrough.x2.toFixed(2)}@${derived.railThrough.intensity.toFixed(4)}`;
  return `${railKey}|${stubKeys.join("|")}`;
}

/**
 * One column's phase bus: the wrap div stacks the light-engine canvas UNDER
 * the structural SVG — the canvas paints the living streams, the SVG keeps
 * rail, collars, rings, unknown dashes, and the arrowheads riding the curved
 * conduits above them. aria-hidden by design: the worded figures carry the
 * same facts as text right below.
 */
export function FlowBus({
  grid,
  battery,
  house,
  batteryPulse = false,
  connection = "live",
}: {
  grid: SlotSpec;
  battery: SlotSpec;
  house: SlotSpec;
  batteryPulse?: boolean;
  connection?: "connecting" | "live" | "lost";
}): ReactNode {
  const uid = useId().replace(/[^a-zA-Z0-9-]/g, "");
  const smallHead = `flow-head-sm-${uid}`;
  const largeHead = `flow-head-lg-${uid}`;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<FlowGlowRenderer | null>(null);
  const specKeyRef = useRef<string>("");
  const reducedMotion = usePrefersReducedMotion();

  // The renderer lives once per mount; StrictMode's double-mount fully
  // destroys the first instance before the second attaches.
  useEffect(() => {
    const renderer = new FlowGlowRenderer();
    rendererRef.current = renderer;
    if (canvasRef.current !== null) {
      renderer.attach(canvasRef.current);
    }
    return () => {
      renderer.destroy();
      rendererRef.current = null;
      specKeyRef.current = "";
    };
  }, []);

  // Fed after EVERY render; serialized so an unchanged picture never pushes.
  useEffect(() => {
    const renderer = rendererRef.current;
    if (renderer === null) {
      return;
    }
    const derived = deriveGlowSpec(grid, battery, house);
    const key = serializeGlowSpec(derived);
    if (specKeyRef.current !== key) {
      specKeyRef.current = key;
      renderer.setSpec(derived.stubs, derived.railThrough);
    }
  });

  // The environment: reduced-motion preference, liveness, and visibility.
  useEffect(() => {
    const apply = (): void => {
      rendererRef.current?.setEnvironment({
        reducedMotion,
        running: !(typeof document === "object" && document.hidden === true),
        connection,
      });
    };
    apply();
    document.addEventListener("visibilitychange", apply);
    return () => {
      document.removeEventListener("visibilitychange", apply);
    };
  }, [reducedMotion, connection]);

  return (
    <div className="flow-bus-wrap">
      {/* The light engine's surface — decorative light only; a canvas is not
          focusable by nature, so unlike the SVG it needs no focusable={false}. */}
      <canvas ref={canvasRef} className="flow-bus-glow" aria-hidden="true" />
      {/* Round-3a box, grown in round 3b (BUS_VIEWBOX, the single source —
          also read by glow.ts's bloom edge-cap math): y runs -8..120 so the
          engine's terminal blooms have real headroom (24 units above the
          junctions AND 24 below the round-3b landings at y=96, which lengthen
          every conduit into the reclaimed page) and can be sized to FIT the
          canvas instead of chopping at its edge. The x span stays exactly
          0..240; every x geometry constant below is unchanged. */}
      <svg
        className="flow-bus"
        viewBox={`${BUS_VIEWBOX.minX} ${BUS_VIEWBOX.minY} ${BUS_VIEWBOX.width} ${BUS_VIEWBOX.height}`}
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          {/* Two heads, one shape: small (8×7) under the large-head threshold,
              large (12×10) at/above it — a constant single size would put a
              cartoon head on a trickle. markerUnits="userSpaceOnUse" keeps
              both constant; orient follows each curved path's tangent at the
              dock point. One NEUTRAL near-white (#e8eef6) for every head on
              the dark bay: the stream names the family, the head names the
              direction — direction is therefore never color. */}
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
            <path d="M 1 1 L 7 3.5 L 1 6 z" fill="#e8eef6" />
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
            <path d="M 1 1.5 L 11 5 L 1 8.5 z" fill="#e8eef6" />
          </marker>
        </defs>
        {/* High-voltage conduit busbar track and mounting hardware */}
        <rect className="flow-bus-channel" x={12} y={RAIL_Y - 4} width={216} height={8} rx={4} />
        <line className="flow-bus-rail" x1={16} y1={RAIL_Y} x2={224} y2={RAIL_Y} />
        {/* Hardware terminal collars at conduit junction points */}
        <circle className="flow-bus-collar flow-bus-collar--grid" cx={SLOT_X.grid} cy={RAIL_Y} r={4.5} />
        <circle className="flow-bus-collar flow-bus-collar--battery" cx={SLOT_X.battery} cy={RAIL_Y} r={4.5} />
        <circle className="flow-bus-collar flow-bus-collar--home" cx={SLOT_X.home} cy={RAIL_Y} r={4.5} />
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
    </div>
  );
}
