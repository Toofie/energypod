/**
 * The flow bus's shared GEOMETRY — pure math, DOM-free, unit-tested.
 *
 * ROUND 2 (the streams): the proportional-width ribbon grammar is retired.
 * What replaces it is a light engine (glow.ts) driving living particle
 * streams along CURVED conduits, and this module is the single source of the
 * shapes both consumers agree on: the SVG draws each conduit as one cubic
 * Bézier path (its markerEnd arrowhead orients along the curve tangent), and
 * the canvas engine spawns particles along the very same path object. One
 * builder, two renderers, zero drift.
 *
 * Everything here is closed-form: de Casteljau evaluation and the Bézier
 * derivative — no DOM measurement anywhere (no getPointAtLength), so the
 * module runs unchanged in jsdom, in the engine, and in the SVG.
 *
 * ROUND 3a (the drama pass): two contracts joined this module. First, the
 * canvas gained vertical HEADROOM for terminal blooms — the bus SVG's viewBox
 * starts at y=−8 (24 units above the rail junctions' y=16), and glow.ts caps
 * every bloom blit to fit that box exactly (see `terminalBlitDiameter`), so
 * light may grow but never clip. Second, the node-card grid's inter-card GAP
 * displaces the outer cards' centers exactly `NODES_GAP_UNITS / 3` outside
 * their third-points — `cardCenterX` is the derived correction, and conduit
 * LANDINGS use it so arrowheads and arriving streams meet the true card
 * centers while the rail-side collars stay pinned to the thirds.
 *
 * ROUND 3b (the node instruments): the bay reclaims its page. The bus box
 * grew again — `0 -8 240 128` (y −8..120) — and the landings moved DOWN into
 * what was spare bloom room: STUB_BOTTOM 78 → 96. Every conduit is ~29%
 * longer (drop 62 → 80 units), so the light engine's rivers travel further
 * and the whole bus reads taller; the landing blooms keep ≥24 units of
 * bottom headroom (cap diameter 48) exactly as the junction blooms do. The
 * x geometry — slots, split offsets, gap correction — is untouched.
 *
 * ORIENTATION CONTRACT: `conduitPath` returns the path oriented ALONG THE
 * FLOW — a "to-node" stub runs rail junction → card landing (the direction
 * the particles travel and the arrowhead points); an "into-bus" stub runs
 * card landing → rail junction (reversed). Both consumers get draw order =
 * energy order for free.
 */

/** The bus geometry, in viewBox units of the `0 -8 240 128` bus SVG — the
 *  round-3b box: 24 units of bloom headroom above y=16 AND below the y=96
 *  landings, so terminal light grows without clipping at either edge. The
 *  x axis still spans exactly 0..240. */
export const RAIL_Y = 12;
export const STUB_TOP = 14;
export const STUB_BOTTOM = 96;
/** The idle ring's center — just below the rail, centered in the slot. */
export const RING_Y = 21;
/** The fleet's two-sided slots render two half-slot stubs, offset ±14 px. */
export const SPLIT_OFFSET = 14;
/**
 * Head-size selection keeps its magnitude meaning after the width channel's
 * retirement: a stub wears the large head when its RETIRED ribbon width
 * would have reached 4 px — i.e. share ≥ 0.25 on the old 2→10 px linear map
 * (`FlowBus.activeStub` still computes widthPx for exactly this purpose).
 */
export const LARGE_HEAD_AT_PX = 4;
/** The stub slots, pinned to the thirds of the 240-unit x axis. The rail-side
 *  hardware (collars, junctions) sits exactly here; the CARD-SIDE landings
 *  correct these points by `cardCenterX` (below), because the node-card
 *  grid's column gap pushes the outer cards' centers off the pure thirds. */
export const SLOT_X = { grid: 40, battery: 120, home: 200 } as const;

/**
 * The node-card grid's inter-card gap as a constant number of viewBox units.
 *
 * DERIVATION (round 3a): `.flow-nodes` (flow.css) lays three equal cards over
 * the same width as this SVG with `column-gap: 2%`. A percentage gap resolves
 * against the dock's width — the very width the viewBox maps to — so the gap
 * is a CONSTANT 2% × 240 = 4.8 units at every viewport, by construction.
 * Column k then spans k·(c + g) .. k·(c + g) + c with c = (240 − 2g)/3, and
 * its center is k·(c+g) + c/2:
 *
 *   k=0:  c/2        = 40 − g/3   = 38.4   (grid card sits g/3 LEFT of 40)
 *   k=1:  1.5c + g   = 120                 (battery is exact at any gap)
 *   k=2:  2.5c + 2g  = 200 + g/3  = 201.6  (home card sits g/3 RIGHT of 200)
 */
export const NODES_GAP_UNITS = 4.8;

/**
 * THE BUS BOX (round 3a, grown in round 3b): the viewBox of the bus SVG and
 * of the light engine's canvas beneath it — one source consumed by FlowBus's
 * `<svg>`, by glow.ts's bloom edge-cap math (`terminalBlitDiameter` measures
 * distances to THESE edges, not to an assumed y=0), and by the tests. The
 * y range −8..120 buys the blooms their headroom: 24 units above the
 * junctions' y=16 AND 24 below the landings' y=96 — the round-3b landing
 * depth that lengthens every conduit into the reclaimed page. The x span
 * stays exactly 0..240.
 */
export const BUS_VIEWBOX = { minX: 0, minY: -8, width: 240, height: 128 } as const;

/**
 * A slot's true card-center x: the third-point corrected by the grid-gap
 * displacement derived above. Conduit landings use this so arrowheads and
 * arriving particles meet the card's real center; rail junctions deliberately
 * do NOT (the collar hardware stays on the pinned thirds).
 */
export function cardCenterX(slotX: number): number {
  if (slotX === SLOT_X.grid) {
    return slotX - NODES_GAP_UNITS / 3;
  }
  if (slotX === SLOT_X.home) {
    return slotX + NODES_GAP_UNITS / 3;
  }
  return slotX; // the middle card is exact for any gap
}

/** The conduit leaves the rail this far below STUB_TOP (a breath of clearance). */
export const CONDUIT_START_Y = STUB_TOP + 2;
/** How far (in units) the curve's control points reach toward each end —
 *  the S-bend's tension: 0 = a corner, 0.5 = maximum ease. */
const CONTROL_REACH = 0.42;
/** Single stubs bias their rail junction ±10 toward the bay's outer edge. */
const JUNCTION_BIAS = 10;

/** One point in bus coordinates. */
export interface FlowPoint {
  x: number;
  y: number;
}

/** A cubic Bézier segment: anchor p0 → anchor p3 with controls c1, c2. */
export interface FlowPath {
  readonly p0: FlowPoint;
  readonly c1: FlowPoint;
  readonly c2: FlowPoint;
  readonly p3: FlowPoint;
}

/** The two aims a live conduit can carry (idle/unknown never build paths). */
export type ConduitAim = "into-bus" | "to-node";

/**
 * Which twin of a split slot a path serves: the into-bus twin takes the LEFT
 * half-slot (landing slotX − SPLIT_OFFSET) with its junction pushed left,
 * the to-node twin the right — junction and landing separate together, so
 * the twins can never touch (see the ≥4-unit clearance pin in
 * flowGeometry.test.ts; the analytic gap is 20 units).
 */
export type SplitSide = ConduitAim;

function singleJunctionBias(slotX: number): number {
  if (slotX === SLOT_X.grid) {
    return -JUNCTION_BIAS; // the grid slot hugs the bay's left edge
  }
  if (slotX === SLOT_X.home) {
    return JUNCTION_BIAS; // the home slot hugs the right
  }
  return 0; // battery rides straight unless it splits
}

/**
 * Build one conduit's path. Rail junction → card landing as a gentle S-curve:
 * the junction sits at CONDUIT_START_Y offset ±10 from the slot's third-point
 * (outer slots bias outward; battery stays straight unless split), the
 * landing sits at STUB_BOTTOM exactly on the card's true center —
 * `cardCenterX(slotX)`, offset by ∓SPLIT_OFFSET for a split's twins — and
 * the controls are pinned vertically so the curve LEAVES the rail plumb and
 * ARRIVES at its card plumb — bending only in between.
 *
 * The returned path is oriented along the flow (see the module contract):
 * `to-node` runs junction → landing, `into-bus` landing → junction.
 */
export function conduitPath(slotX: number, aim: ConduitAim, splitSide?: SplitSide): FlowPath {
  const center = cardCenterX(slotX);
  const landX =
    splitSide === undefined ? center : center + (splitSide === "into-bus" ? -SPLIT_OFFSET : SPLIT_OFFSET);
  const junctionX =
    splitSide === undefined
      ? slotX + singleJunctionBias(slotX)
      : slotX + (splitSide === "into-bus" ? -JUNCTION_BIAS : JUNCTION_BIAS);
  const drop = STUB_BOTTOM - CONDUIT_START_Y;
  const railToLanding: FlowPath = {
    p0: { x: junctionX, y: CONDUIT_START_Y },
    c1: { x: junctionX, y: CONDUIT_START_Y + drop * CONTROL_REACH },
    c2: { x: landX, y: STUB_BOTTOM - drop * CONTROL_REACH },
    p3: { x: landX, y: STUB_BOTTOM },
  };
  return aim === "to-node" ? railToLanding : reversePath(railToLanding);
}

/** The same conduit traversed the other way (anchors swap, controls swap). */
export function reversePath(path: FlowPath): FlowPath {
  return { p0: path.p3, c1: path.c2, c2: path.c1, p3: path.p0 };
}

/** The SVG `d` attribute for one conduit (an absolute M + C cubic). */
export function pathToD(path: FlowPath): string {
  return `M ${path.p0.x} ${path.p0.y} C ${path.c1.x} ${path.c1.y}, ${path.c2.x} ${path.c2.y}, ${path.p3.x} ${path.p3.y}`;
}

function clamp01(t: number): number {
  if (Number.isNaN(t)) {
    return 0;
  }
  return t < 0 ? 0 : t > 1 ? 1 : t;
}

/**
 * The point at parameter t ∈ [0,1] by de Casteljau (out-of-range t clamps,
 * NaN reads as 0 — the engine's jitter arithmetic can never poison a frame).
 */
export function pointAt(path: FlowPath, t: number): FlowPoint {
  return pointAtInto(path, t, { x: 0, y: 0 });
}

/**
 * The same evaluation written into a CALLER-OWNED point — the engine's
 * per-frame hot path reuses one scratch object per renderer instead of
 * allocating a fresh {x,y} for every particle of every frame. Overwrites
 * both fields and returns `out` for chaining; safe to reuse immediately.
 */
export function pointAtInto(path: FlowPath, t: number, out: FlowPoint): FlowPoint {
  const s = clamp01(t);
  const u = 1 - s;
  const a = u ** 3;
  const b = 3 * u * u * s;
  const c = 3 * u * s * s;
  const d = s ** 3;
  out.x = a * path.p0.x + b * path.c1.x + c * path.c2.x + d * path.p3.x;
  out.y = a * path.p0.y + b * path.c1.y + c * path.c2.y + d * path.p3.y;
  return out;
}

/**
 * The flow direction at parameter t, in radians (atan2 of the Bézier
 * derivative, normalized). For our conduits dy/dt dominates: to-node paths
 * point down (angle ≈ +π/2), into-bus paths point up (≈ −π/2), banking by
 * exactly the local slope of the S-bend.
 */
export function tangentAt(path: FlowPath, t: number): number {
  const s = clamp01(t);
  const u = 1 - s;
  const dx = 3 * u * u * (path.c1.x - path.p0.x) + 6 * u * s * (path.c2.x - path.c1.x) + 3 * s * s * (path.p3.x - path.c2.x);
  const dy = 3 * u * u * (path.c1.y - path.p0.y) + 6 * u * s * (path.c2.y - path.c1.y) + 3 * s * s * (path.p3.y - path.c2.y);
  if (dx === 0 && dy === 0) {
    return 0; // degenerate by construction; never happens for built conduits
  }
  return Math.atan2(dy, dx);
}

/** The path's flow end — where the arrowhead docks and particles arrive. */
export function terminalPoint(path: FlowPath): FlowPoint {
  return path.p3;
}

/** The path length in units, approximated as the chord/polygon mean
 *  (within ~0.5% for these shallow curves — plenty for speed mapping). */
export function approxLength(path: FlowPath): number {
  const chord = Math.hypot(path.p3.x - path.p0.x, path.p3.y - path.p0.y);
  const polygon =
    Math.hypot(path.c1.x - path.p0.x, path.c1.y - path.p0.y) +
    Math.hypot(path.c2.x - path.c1.x, path.c2.y - path.c1.y) +
    Math.hypot(path.p3.x - path.c2.x, path.p3.y - path.c2.y);
  return (chord + polygon) / 2;
}
