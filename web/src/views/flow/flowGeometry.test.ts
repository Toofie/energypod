/**
 * Unit contract for the flow bus's shared geometry (flowGeometry.ts) —
 * pure, DOM-free, adversarial. The pins:
 *
 * - THE CONSTANTS ARE LOAD-BEARING: the retired-ribbon-era numbers
 *   (RAIL_Y/STUB bounds/RING_Y/SLOT_X/SPLIT_OFFSET/LARGE_HEAD_AT_PX) survive
 *   the grammar swap verbatim — the SVG, the engine, and these tests all
 *   read one source.
 * - ENDPOINTS EXACT: every conduit leaves its rail junction at
 *   (junctionX, 16) and lands at (landingX, STUB_BOTTOM = 96). Round 3a:
 *   junctions stay on the SLOT thirds (the collar hardware lives there) but
 *   LANDINGS use `cardCenterX` — the node-grid's percentage column-gap
 *   displaces outer card centers exactly NODES_GAP_UNITS/3 outside their
 *   third-points, and arrowheads must meet the TRUE centers.
 * - ORIENTATION ALONG THE FLOW: to-node runs junction→landing (pointing
 *   down), into-bus runs landing→junction (pointing up) — tangentAt's sine
 *   flips sign with the aim at every sampled t.
 * - SPLIT TWINS NEVER COLLIDE: ≥4 units of clearance between a split pair,
 *   sampled densely over both full curves (the analytic gap is 20).
 * - NO NaN EVER: every coordinate of every buildable path, and every
 *   pointAt/tangentAt/pointAtInto sample over [0,1] plus hostile inputs, is
 *   finite.
 */
import { describe, expect, it } from "vitest";
import {
  BUS_VIEWBOX,
  CONDUIT_START_Y,
  LARGE_HEAD_AT_PX,
  NODES_GAP_UNITS,
  RAIL_Y,
  RING_Y,
  SLOT_X,
  SPLIT_OFFSET,
  STUB_BOTTOM,
  STUB_TOP,
  approxLength,
  cardCenterX,
  conduitPath,
  pathToD,
  pointAt,
  pointAtInto,
  reversePath,
  tangentAt,
  terminalPoint,
  type FlowPath,
  type FlowPoint,
} from "./flowGeometry";

function distance(a: FlowPoint, b: FlowPoint): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** Densest minimum pairwise distance between two paths (129 samples each). */
function minClearance(a: FlowPath, b: FlowPath): number {
  let worst = Number.POSITIVE_INFINITY;
  for (let i = 0; i <= 128; i += 1) {
    const pa = pointAt(a, i / 128);
    for (let j = 0; j <= 128; j += 1) {
      const pb = pointAt(b, j / 128);
      const d = distance(pa, pb);
      if (d < worst) {
        worst = d;
      }
    }
  }
  return worst;
}

/** All finite coordinates on a path. */
function expectFinitePath(path: FlowPath): void {
  for (const p of [path.p0, path.c1, path.c2, path.p3]) {
    expect(Number.isFinite(p.x)).toBe(true);
    expect(Number.isFinite(p.y)).toBe(true);
  }
}

describe("flowGeometry — the pinned constants", () => {
  it("carries the bus geometry verbatim through the grammar swap", () => {
    expect(RAIL_Y).toBe(12);
    expect(STUB_TOP).toBe(14);
    // Round 3b moved the landings down into the box's lower bloom headroom:
    // longer conduits (drop 62 → 80), a taller bus, the same x geometry.
    expect(STUB_BOTTOM).toBe(96);
    expect(RING_Y).toBe(21);
    expect(SPLIT_OFFSET).toBe(14);
    expect(LARGE_HEAD_AT_PX).toBe(4);
    expect(SLOT_X).toEqual({ grid: 40, battery: 120, home: 200 });
    expect(CONDUIT_START_Y).toBe(16);
    // Round 3a: the card-grid gap as viewBox units — flow.css pins the CSS
    // side (column-gap: 2% of 240); this pin keeps the pair honest.
    expect(NODES_GAP_UNITS).toBe(4.8);
    // …and the bloom-headroom box (round 3b: y −8..120): FlowBus's viewBox
    // and glow.ts's edge-cap math both read this one source.
    expect(BUS_VIEWBOX).toEqual({ minX: 0, minY: -8, width: 240, height: 128 });
  });

  it("keeps ≥24 units of bloom headroom at BOTH ends of every conduit", () => {
    for (const slotX of Object.values(SLOT_X)) {
      for (const aim of ["into-bus", "to-node"] as const) {
        const path = conduitPath(slotX, aim);
        for (const anchor of [path.p0, path.p3]) {
          expect(anchor.y - BUS_VIEWBOX.minY).toBeGreaterThanOrEqual(24);
          expect(BUS_VIEWBOX.minY + BUS_VIEWBOX.height - anchor.y).toBeGreaterThanOrEqual(24);
        }
      }
    }
  });
});

describe("flowGeometry — cardCenterX (the arrowhead-landing fix)", () => {
  it("displaces only the outer slots, by exactly NODES_GAP_UNITS/3", () => {
    expect(cardCenterX(SLOT_X.battery)).toBe(120); // middle card is exact for any gap
    expect(cardCenterX(SLOT_X.grid)).toBeCloseTo(40 - NODES_GAP_UNITS / 3, 12);
    expect(cardCenterX(SLOT_X.home)).toBeCloseTo(200 + NODES_GAP_UNITS / 3, 12);
    // The derived centers match the closed-form grid math (c/2, 1.5c+g, 2.5c+2g).
    const g = NODES_GAP_UNITS;
    const c = (240 - 2 * g) / 3;
    expect(cardCenterX(SLOT_X.grid)).toBeCloseTo(c / 2, 12);
    expect(cardCenterX(SLOT_X.battery)).toBeCloseTo((1.5 * c) + g, 12);
    expect(cardCenterX(SLOT_X.home)).toBeCloseTo(2.5 * c + 2 * g, 12);
  });

  it("stays symmetric and order-preserving so the bay never skews", () => {
    const left = SLOT_X.grid - cardCenterX(SLOT_X.grid);
    const right = cardCenterX(SLOT_X.home) - SLOT_X.home;
    expect(left).toBeCloseTo(right, 12); // equal outward displacement
    expect(left).toBeGreaterThan(0);
    expect(cardCenterX(SLOT_X.grid)).toBeLessThan(cardCenterX(SLOT_X.battery));
    expect(cardCenterX(SLOT_X.battery)).toBeLessThan(cardCenterX(SLOT_X.home));
    // Non-slot x values pass through untouched.
    expect(cardCenterX(77)).toBe(77);
    expect(cardCenterX(0)).toBe(0);
  });
});

describe("flowGeometry — conduitPath endpoints", () => {
  it("builds single-slot curves with exact junctions and landings", () => {
    // Grid biases its JUNCTION outward-left (30) but lands on the CARD's true
    // center (the third-point pulled g/3 outward by the node-grid gap).
    const grid = conduitPath(SLOT_X.grid, "to-node");
    expect(grid.p0).toEqual({ x: 30, y: CONDUIT_START_Y });
    expect(grid.p3.x).toBeCloseTo(cardCenterX(SLOT_X.grid), 12);
    expect(grid.p3.y).toBe(STUB_BOTTOM);
    // Home mirrors: junction outward-right (210).
    const home = conduitPath(SLOT_X.home, "to-node");
    expect(home.p0).toEqual({ x: 210, y: CONDUIT_START_Y });
    expect(home.p3.x).toBeCloseTo(cardCenterX(SLOT_X.home), 12);
    expect(home.p3.y).toBe(STUB_BOTTOM);
    // Battery rides straight unless split (its card center IS its third-point).
    const battery = conduitPath(SLOT_X.battery, "to-node");
    expect(battery.p0).toEqual({ x: 120, y: CONDUIT_START_Y });
    expect(battery.p3).toEqual({ x: 120, y: STUB_BOTTOM });
    expect(battery.c1.x).toBe(120);
    expect(battery.c2.x).toBe(120);
  });

  it("orients along the flow: into-bus runs landing → rail junction", () => {
    const into = conduitPath(SLOT_X.grid, "into-bus");
    // Reversed: starts ON the card, ends AT the rail (where its head docks).
    expect(into.p0.x).toBeCloseTo(cardCenterX(SLOT_X.grid), 12);
    expect(into.p0.y).toBe(STUB_BOTTOM);
    expect(into.p3).toEqual({ x: 30, y: CONDUIT_START_Y });
    expect(terminalPoint(into)).toEqual({ x: 30, y: CONDUIT_START_Y });
  });

  it("separates split twins' junctions AND landings on every slot", () => {
    // The fleet grid slot splits around the corrected card center.
    const gridInto = conduitPath(SLOT_X.grid, "into-bus", "into-bus");
    const gridOut = conduitPath(SLOT_X.grid, "to-node", "to-node");
    expect(gridInto.p0.x).toBeCloseTo(cardCenterX(SLOT_X.grid) - SPLIT_OFFSET, 12); // left twin landing
    expect(gridInto.p0.y).toBe(STUB_BOTTOM);
    expect(gridInto.p3).toEqual({ x: 40 - 10, y: CONDUIT_START_Y }); // junction 30 (third-point bias)
    expect(gridOut.p0).toEqual({ x: 40 + 10, y: CONDUIT_START_Y }); // junction 50
    expect(gridOut.p3.x).toBeCloseTo(cardCenterX(SLOT_X.grid) + SPLIT_OFFSET, 12); // right twin landing
    expect(gridOut.p3.y).toBe(STUB_BOTTOM);
    // The fleet battery slot splits the same way, straight-biased base; its
    // card center is exact, so its landings keep their round-2 values.
    const batInto = conduitPath(SLOT_X.battery, "into-bus", "into-bus");
    const batOut = conduitPath(SLOT_X.battery, "to-node", "to-node");
    expect(batInto.p3).toEqual({ x: 110, y: CONDUIT_START_Y });
    expect(batInto.p0).toEqual({ x: 106, y: STUB_BOTTOM });
    expect(batOut.p0).toEqual({ x: 130, y: CONDUIT_START_Y });
    expect(batOut.p3).toEqual({ x: 134, y: STUB_BOTTOM });
  });

  it("straddles every split's twins symmetrically around the TRUE card center", () => {
    for (const slotX of Object.values(SLOT_X)) {
      const into = conduitPath(slotX, "into-bus", "into-bus");
      const out = conduitPath(slotX, "to-node", "to-node");
      const center = cardCenterX(slotX);
      // Landing xs: center ∓ SPLIT_OFFSET whichever orientation carries them.
      const intoLandingX = Math.min(into.p0.x, into.p3.x);
      const outLandingX = Math.max(out.p0.x, out.p3.x);
      expect(intoLandingX).toBeCloseTo(center - SPLIT_OFFSET, 10);
      expect(outLandingX).toBeCloseTo(center + SPLIT_OFFSET, 10);
      // The pair's midpoint is the card center — not merely the third-point.
      expect((intoLandingX + outLandingX) / 2).toBeCloseTo(center, 10);
    }
  });

  it("never produces NaN or non-finite coordinates in any buildable path", () => {
    for (const slotX of [...Object.values(SLOT_X), 0, -37, 1e6]) {
      for (const aim of ["into-bus", "to-node"] as const) {
        expectFinitePath(conduitPath(slotX, aim));
        expectFinitePath(conduitPath(slotX, aim, "into-bus"));
        expectFinitePath(conduitPath(slotX, aim, "to-node"));
      }
    }
  });
});

describe("flowGeometry — split-twin clearance", () => {
  it("keeps ≥4 units between split twins on every slot (sampled densely)", () => {
    for (const slotX of Object.values(SLOT_X)) {
      const into = conduitPath(slotX, "into-bus", "into-bus");
      const out = conduitPath(slotX, "to-node", "to-node");
      expect(minClearance(into, out)).toBeGreaterThanOrEqual(4);
      // …and the reversed traversal is the same physical pair.
      expect(minClearance(reversePath(into), reversePath(out))).toBeGreaterThanOrEqual(4);
    }
  });

  it("keeps neighbors apart too: no single-slot curve touches a split twin", () => {
    // A pathological world where grid splits while home draws single.
    const gridOut = conduitPath(SLOT_X.grid, "to-node", "to-node");
    const homeSingle = conduitPath(SLOT_X.home, "to-node");
    expect(minClearance(gridOut, homeSingle)).toBeGreaterThanOrEqual(4);
  });
});

describe("flowGeometry — pointAt / tangentAt", () => {
  it("hits the anchors exactly at t=0 and t=1 and stays inside the hull mid-way", () => {
    const path = conduitPath(SLOT_X.grid, "to-node");
    expect(pointAt(path, 0)).toEqual(path.p0);
    expect(pointAt(path, 1)).toEqual(path.p3);
    const mid = pointAt(path, 0.5);
    expect(mid.y).toBeGreaterThan(CONDUIT_START_Y);
    expect(mid.y).toBeLessThan(STUB_BOTTOM);
    // Control xs are pinned to anchor xs, so x stays within the anchor hull.
    const xMin = Math.min(path.p0.x, path.p3.x);
    const xMax = Math.max(path.p0.x, path.p3.x);
    expect(mid.x).toBeGreaterThanOrEqual(xMin);
    expect(mid.x).toBeLessThanOrEqual(xMax);
  });

  it("clamps hostile parameters instead of poisoning the frame", () => {
    const path = conduitPath(SLOT_X.battery, "to-node");
    expect(pointAt(path, -3)).toEqual(path.p0);
    expect(pointAt(path, 2)).toEqual(path.p3);
    expect(Number.isFinite(pointAt(path, Number.NaN).x)).toBe(true);
    expect(Number.isFinite(tangentAt(path, Number.NaN))).toBe(true);
    expect(tangentAt(path, -5)).toBe(tangentAt(path, 0));
    expect(tangentAt(path, 5)).toBe(tangentAt(path, 1));
  });

  it("flips the tangent's vertical sense with the aim at every sampled t", () => {
    for (const slotX of Object.values(SLOT_X)) {
      const down = conduitPath(slotX, "to-node");
      const up = conduitPath(slotX, "into-bus");
      for (const t of [0, 0.25, 0.5, 0.75, 1]) {
        expect(Math.sin(tangentAt(down, t))).toBeGreaterThan(0); // heading down
        expect(Math.sin(tangentAt(up, t))).toBeLessThan(0); // heading up
      }
    }
  });

  it("points a straight battery conduit plumb down (then plumb up)", () => {
    const down = conduitPath(SLOT_X.battery, "to-node");
    expect(tangentAt(down, 0.5)).toBeCloseTo(Math.PI / 2, 12);
    const up = conduitPath(SLOT_X.battery, "into-bus");
    expect(tangentAt(up, 0.5)).toBeCloseTo(-Math.PI / 2, 12);
  });

  it("banks an S-curve toward its landing: grid to-node drifts right then straightens", () => {
    const path = conduitPath(SLOT_X.grid, "to-node"); // junction 30 → landing 40
    // Early: moving down-and-right; late: arriving plumb-ish, still downward.
    expect(Math.cos(tangentAt(path, 0.15))).toBeGreaterThan(0);
    expect(Math.abs(Math.cos(tangentAt(path, 0.9)))).toBeLessThan(Math.abs(Math.cos(tangentAt(path, 0.15))));
  });
});

describe("flowGeometry — path plumbing", () => {
  it("terminalPoint is the flow end on both aims", () => {
    const down = conduitPath(SLOT_X.home, "to-node");
    expect(terminalPoint(down).y).toBe(STUB_BOTTOM);
    expect(terminalPoint(down).x).toBeCloseTo(cardCenterX(SLOT_X.home), 12);
    const up = conduitPath(SLOT_X.home, "into-bus");
    expect(terminalPoint(up)).toEqual({ x: 210, y: CONDUIT_START_Y });
  });

  it("pointAtInto writes into a caller-owned point and returns that same object", () => {
    const path = conduitPath(SLOT_X.grid, "to-node");
    const out: FlowPoint = { x: 999, y: -999 };
    const returned = pointAtInto(path, 0.5, out);
    expect(returned).toBe(out); // no fresh object: the engine reuses one scratch
    expect(out.x).toBeCloseTo(pointAt(path, 0.5).x, 14);
    expect(out.y).toBeCloseTo(pointAt(path, 0.5).y, 14);
    // Reuse overwrites BOTH fields — no stale coordinates survive.
    pointAtInto(path, 0, out);
    expect(out).toEqual(path.p0);
    pointAtInto(path, 1, out);
    expect(out.x).toBeCloseTo(path.p3.x, 14);
    expect(out.y).toBe(path.p3.y);
    // Hostile parameters behave exactly like pointAt's clamps.
    pointAtInto(path, Number.NaN, out);
    expect(Number.isFinite(out.x) && Number.isFinite(out.y)).toBe(true);
    pointAtInto(path, -7, out);
    expect(out).toEqual(path.p0);
    pointAtInto(path, 7, out);
    expect(out.x).toBeCloseTo(path.p3.x, 14);
  });

  it("reversePath round-trips and swaps only the anchors' roles", () => {
    const path = conduitPath(SLOT_X.grid, "to-node");
    const flipped = reversePath(path);
    expect(flipped.p0).toEqual(path.p3);
    expect(flipped.p3).toEqual(path.p0);
    expect(flipped.c1).toEqual(path.c2);
    expect(flipped.c2).toEqual(path.c1);
    expect(reversePath(flipped)).toEqual(path);
  });

  it("emits an absolute M+C cubic whose anchors are the path's own", () => {
    const path = conduitPath(SLOT_X.grid, "to-node");
    const d = pathToD(path);
    expect(d.startsWith(`M ${path.p0.x} ${path.p0.y} C `)).toBe(true);
    const numbers = d.match(/-?\d+(?:\.\d+)?/g)?.map(Number) ?? [];
    expect(numbers).toHaveLength(8);
    expect([numbers[6], numbers[7]]).toEqual([path.p3.x, path.p3.y]);
  });

  it("approximates length: exact on the straight battery, ≥ chord elsewhere", () => {
    const straight = conduitPath(SLOT_X.battery, "to-node");
    expect(approxLength(straight)).toBeCloseTo(STUB_BOTTOM - CONDUIT_START_Y, 10);
    const curved = conduitPath(SLOT_X.grid, "to-node");
    const chord = Math.hypot(curved.p3.x - curved.p0.x, curved.p3.y - curved.p0.y);
    expect(approxLength(curved)).toBeGreaterThanOrEqual(chord);
    expect(approxLength(curved)).toBeLessThan(chord * 1.05);
  });

  it("survives a walk of the whole parameter space without leaving finitude", () => {
    for (const slotX of Object.values(SLOT_X)) {
      const path = conduitPath(slotX, "into-bus", "into-bus");
      for (let i = 0; i <= 256; i += 1) {
        const t = i / 256;
        const p = pointAt(path, t);
        expect(Number.isFinite(p.x)).toBe(true);
        expect(Number.isFinite(p.y)).toBe(true);
        expect(Number.isFinite(tangentAt(path, t))).toBe(true);
      }
    }
  });
});
