/**
 * Unit contract for the light engine's PURE seams (glow.ts) — the motion
 * laws and helpers the Canvas2D renderer consumes, pinned WITHOUT any canvas
 * (jsdom-safe: importing the module probes for a 2D context and no-ops when
 * it is missing, so these tests exercise the math, never pixels).
 *
 * The pins (round-3a DRAMA values):
 * - THE LAWS ARE MONOTONE IN SHARE: more watts means more particles, faster,
 *   fatter, brighter — never the reverse, for ANY input including hostile
 *   ones. Out-of-range shares read as their nearest bound; NaN reads as calm
 *   (0), because engine arithmetic may never poison a frame.
 * - CEILINGS HOLD: core alpha tops out at exactly 1, streak alpha below it,
 *   the terminal envelope at its prescribed ×1.25 gain, every bloom blit
 *   inside the canvas box (no clipped additive light anywhere).
 * - THE HOT FILAMENT IS A CROSSFADE, not a jump: 0 below the window, 1 at
 *   the top, linear between.
 * - PRIOR MATCHING GLIDES: a runtime's key is tone+aim alone — deliberately
 *   blind to share and path endpoints, so split↔single topology changes
 *   keep their stream alive instead of snapping.
 */
import { describe, expect, it } from "vitest";
import {
  fadeEnvelope,
  hotBlend,
  particleCoreAlpha,
  particleRadius,
  particleStreakAlpha,
  railAlpha,
  spawnRate,
  streamSpeed,
  stubKey,
  terminalAlpha,
  terminalBlitDiameter,
  terminalRadius,
} from "./glow";

/** Every finite sample of the law across the lawful AND hostile domain. */
const SHARES = [-1, -0.25, 0, 0.05, 0.25, 0.5, 0.75, 0.9, 1, 1.5, 7];

describe("glow laws — density and motion", () => {
  it("spawns a RIVER at full share: 3 + 14·share, bounded 3..17", () => {
    expect(spawnRate(0)).toBe(3);
    expect(spawnRate(0.5)).toBeCloseTo(10, 12);
    expect(spawnRate(1)).toBeCloseTo(17, 12);
    expect(spawnRate(3)).toBeCloseTo(17, 12); // clamped, never super-saturated
    expect(spawnRate(-2)).toBe(3);
    expect(spawnRate(Number.NaN)).toBe(3); // calm, not chaos
  });

  it("moves beads 30 + 80·share units/s, monotone over the whole hostile domain", () => {
    expect(streamSpeed(0)).toBe(30);
    expect(streamSpeed(1)).toBeCloseTo(110, 12);
    let previous = -Infinity;
    for (const share of SHARES) {
      const speed = streamSpeed(share);
      expect(speed).toBeGreaterThanOrEqual(previous);
      expect(speed).toBeGreaterThanOrEqual(30);
      expect(speed).toBeLessThanOrEqual(110);
      previous = speed;
    }
  });
});

describe("glow laws — bead size and light", () => {
  it("grows cores 1.3 + 1.9·share units so beads read at a glance", () => {
    expect(particleRadius(0)).toBeCloseTo(1.3, 12);
    expect(particleRadius(1)).toBeCloseTo(3.2, 12);
    let previous = -Infinity;
    for (const share of SHARES) {
      const radius = particleRadius(share);
      expect(radius).toBeGreaterThanOrEqual(previous);
      previous = radius;
    }
    expect(particleRadius(Number.NaN)).toBeCloseTo(1.3, 12);
  });

  it("raises sprite alpha to EXACTLY 1 at full share and never past it", () => {
    expect(particleCoreAlpha(0)).toBeCloseTo(0.55, 12);
    expect(particleCoreAlpha(1)).toBeCloseTo(1, 12);
    for (const share of [...SHARES, Number.NaN]) {
      const alpha = particleCoreAlpha(share);
      expect(alpha).toBeGreaterThan(0);
      expect(alpha).toBeLessThanOrEqual(1); // a globalAlpha above 1 throws
    }
  });

  it("stretches trail alpha ×1.3 over round-2's law: 0.234..0.624", () => {
    expect(particleStreakAlpha(0)).toBeCloseTo(1.3 * 0.18, 12);
    expect(particleStreakAlpha(1)).toBeCloseTo(1.3 * 0.48, 12);
    expect(particleStreakAlpha(0.5)).toBeCloseTo(1.3 * 0.33, 12);
    for (const share of [...SHARES, Number.NaN]) {
      const alpha = particleStreakAlpha(share);
      expect(alpha).toBeGreaterThan(0);
      expect(alpha).toBeLessThan(1); // trails support the bead, never replace it
    }
  });

  it("fades streams in over the first 8% and out over the last 8%, hostile-safe", () => {
    expect(fadeEnvelope(0)).toBe(0);
    expect(fadeEnvelope(1)).toBe(0);
    expect(fadeEnvelope(0.5)).toBe(1);
    expect(fadeEnvelope(0.04)).toBeCloseTo(0.5, 12); // linear ramp up
    expect(fadeEnvelope(0.96)).toBeCloseTo(0.5, 12); // …and down
    for (let t = 0; t <= 1.0001; t += 0.01) {
      const envelope = fadeEnvelope(t);
      expect(envelope).toBeGreaterThanOrEqual(0);
      expect(envelope).toBeLessThanOrEqual(1);
    }
    // Round-3a hardening: the old private version returned 1 for NaN and
    // NEGATIVE amplitudes below the path — both read as invisible now.
    expect(fadeEnvelope(Number.NaN)).toBe(0);
    expect(fadeEnvelope(-0.5)).toBe(0);
    expect(fadeEnvelope(1.5)).toBe(0);
  });
});

describe("glow laws — terminal blooms", () => {
  it("grows pools 6 → 16 units ∝ share (the round-3a cap raise)", () => {
    expect(terminalRadius(0)).toBeCloseTo(6, 12);
    expect(terminalRadius(0.5)).toBeCloseTo(11, 12);
    expect(terminalRadius(1)).toBeCloseTo(16, 12);
    expect(terminalRadius(Number.NaN)).toBeCloseTo(6, 12);
  });

  it("applies the ×1.25 envelope: 0.275 .. 0.65, never above", () => {
    expect(terminalAlpha(0)).toBeCloseTo(0.275, 12);
    expect(terminalAlpha(0.5)).toBeCloseTo(0.4625, 12);
    expect(terminalAlpha(1)).toBeCloseTo(0.65, 12);
    for (const share of [...SHARES, Number.NaN]) {
      expect(terminalAlpha(share)).toBeLessThanOrEqual(0.65 + 1e-12);
      expect(terminalAlpha(share)).toBeGreaterThanOrEqual(0.275 - 1e-12);
    }
  });

  it("caps bloom blits to the canvas so additive light NEVER clips", () => {
    const unitsH = 128; // the round-3b bus box `0 -8 240 128`
    // Mid-box anchors are uncapped: raw diameter = radius × 4.5.
    expect(terminalBlitDiameter(10, 56, unitsH)).toBeCloseTo(45, 12);
    // Junction anchor (y=16): its TRUE top headroom is 24 (the box starts at
    // −8), so the hottest breathing bloom (raw ≈ 77) caps at 48.
    expect(terminalBlitDiameter(16, 16, unitsH)).toBe(48);
    // Landing anchor (y=96, the round-3b depth): true bottom headroom is
    // 120−96 = 24 → capped at 48 — MORE room than round 3a's 44 at y=78,
    // because the landings moved down INTO the box's spare lower headroom.
    expect(terminalBlitDiameter(16, 96, unitsH)).toBe(48);
    // The exact-fit property holds everywhere in the box: half the blit
    // never crosses either horizontal edge, for any anchor and any radius.
    const top = -8;
    for (let y = top; y <= top + unitsH; y += 3) {
      for (const radius of [0, 6, 11, 16, 16.99, 40]) {
        const diameter = terminalBlitDiameter(radius, y, unitsH);
        expect(diameter).toBeGreaterThanOrEqual(0);
        expect(diameter / 2).toBeLessThanOrEqual(Math.min(y - top, top + unitsH - y) + 1e-9);
      }
    }
  });

  it("refuses to draw blooms with impossible geometry", () => {
    expect(terminalBlitDiameter(0, 56, 128)).toBe(0);
    expect(terminalBlitDiameter(-5, 56, 128)).toBe(0);
    expect(terminalBlitDiameter(Number.NaN, 56, 128)).toBe(0);
    expect(terminalBlitDiameter(10, Number.NaN, 128)).toBe(0);
    expect(terminalBlitDiameter(10, 56, Number.NaN)).toBe(0);
    expect(terminalBlitDiameter(10, 200, 128)).toBe(0); // below the box
    expect(terminalBlitDiameter(10, -30, 128)).toBe(0); // above the box
  });
});

describe("glow laws — rail and filament", () => {
  it("energizes the rail across 0.14 → 0.60, monotone, clamped", () => {
    expect(railAlpha(0)).toBeCloseTo(0.14, 12);
    expect(railAlpha(0.5)).toBeCloseTo(0.37, 12);
    expect(railAlpha(1)).toBeCloseTo(0.6, 12);
    expect(railAlpha(4)).toBeCloseTo(0.6, 12);
    expect(railAlpha(-1)).toBeCloseTo(0.14, 12);
    expect(railAlpha(Number.NaN)).toBeCloseTo(0.14, 12);
    let previous = -Infinity;
    for (const intensity of SHARES) {
      const alpha = railAlpha(intensity);
      expect(alpha).toBeGreaterThanOrEqual(previous);
      previous = alpha;
    }
  });

  it("crossfades the hot filament linearly over its window, never before it", () => {
    expect(hotBlend(0)).toBe(0);
    expect(hotBlend(0.49)).toBe(0);
    expect(hotBlend(0.5)).toBe(0);
    expect(hotBlend(0.675)).toBeCloseTo(0.5, 12); // window midpoint
    expect(hotBlend(0.85)).toBe(1);
    expect(hotBlend(1)).toBe(1);
    expect(hotBlend(Number.NaN)).toBe(0);
    let previous = -Infinity;
    for (const share of SHARES) {
      const blend = hotBlend(share);
      expect(blend).toBeGreaterThanOrEqual(previous);
      expect(blend).toBeGreaterThanOrEqual(0);
      expect(blend).toBeLessThanOrEqual(1);
      previous = blend;
    }
  });
});

describe("glow — prior-runtime identity", () => {
  it("keys a stream by tone+aim ALONE, so topology changes glide", () => {
    const single = { tone: "grid", aim: "into-bus" };
    const splitTwin = { tone: "grid", aim: "into-bus" };
    // A split twin and a re-formed single share EVERYTHING that matters:
    // same slot (tone), same direction — the key must NOT distinguish them,
    // even though their paths' landings differ by SPLIT_OFFSET.
    expect(stubKey(splitTwin)).toBe(stubKey(single));
    // Share changes never remount a stream either.
    expect(stubKey({ tone: "grid", aim: "into-bus" })).toBe(stubKey(single));
  });

  it("separates every real stream: aim and tone each flip the key", () => {
    const keys = new Set(
      (["grid", "battery", "home"] as const).flatMap((tone) =>
        (["into-bus", "to-node"] as const).map((aim) => stubKey({ tone, aim })),
      ),
    );
    expect(keys.size).toBe(6); // three slots × two directions, all distinct
    expect(stubKey({ tone: "battery", aim: "to-node" })).not.toBe(stubKey({ tone: "home", aim: "to-node" }));
    expect(stubKey({ tone: "grid", aim: "into-bus" })).not.toBe(stubKey({ tone: "grid", aim: "to-node" }));
  });
});
