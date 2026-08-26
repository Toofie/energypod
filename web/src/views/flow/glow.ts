/**
 * glow.ts — the flow view's Canvas2D LIGHT ENGINE (zero dependencies).
 *
 * ROUND 2's grammar: magnitude is not a ribbon's thickness any more — it is
 * DENSITY + SPEED + GLOW of a living particle stream inside its conduit, and
 * this module is the engine that renders it. One FlowGlowRenderer attaches to
 * each bus canvas (≤4 per view); ALL of them are driven by ONE module-level
 * requestAnimationFrame ticker, so four columns cost one loop total.
 *
 * THE MOTION LAWS (share = |watts| / scaleMax, 0..1) — round-3a DRAMA values,
 * cranked from round 2's shy ones ("alive but shy" — the owner wants rivers,
 * not trickles):
 *   spawn rate   = 3 + 14·share particles/second (a full-scale stream is a RIVER)
 *   speed        = 30 + 80·share units/second along the path (±10% jitter)
 *   core radius  = 1.3 + 1.9·share units (beads read at a glance)
 *   sprite alpha = 0.55 + 0.45·share
 *   streaks      = a motion-blur line 1.5× the previous frame's travel long,
 *                  alpha ×1.3 over round 2 — trails clearly visible
 *   filament     = above share ≈ 0.5 each tone crossfades to a HOT sprite
 *                  whose near-white core reaches further out — a hot filament
 *                  center at high magnitude. Same two validated tints per
 *                  family, only the gradient stops differ: NO new hues.
 *   wander       = lateral sine sway, amplitude ≤1.2 units, pinned to zero at
 *                  both ends by the fade envelope
 *   fades        = fade-in over the first 8% of the path, fade-out over the
 *                  last 8% — streams are born and die inside their conduit
 *   terminals    = bloom pools at BOTH path ends, radius 6→16 units ∝ share,
 *                  alpha envelope ×1.25 over round 2, breathing ±7% on a
 *                  2400 ms period (per-stub phase offset). Each blit is
 *                  edge-capped (`terminalBlitDiameter`) so light fills the
 *                  viewBox's headroom (the bus box is `0 -8 240 128` since
 *                  round 3b) but is never clipped by the canvas edge.
 *   rail         = when through-flow crosses a phase (into-bus AND to-node
 *                  live at once), the rail segment between them energizes at
 *                  alpha 0.14→0.60 ∝ min(Σinto, Σout shares). ROUND 3B: one
 *                  CONTINUOUS filament — a stretched-sprite halo plus a
 *                  hairline core stroke — replacing the chained sprite walk
 *                  whose 9-unit stepping read as a dotted bead-chain.
 *   smoothing    = displayed shares approach their targets with τ=400 ms, so
 *                  each ~2.5 s snapshot breathes instead of snapping
 *
 * COLOR LAW: sprites are pre-rendered radial glows blitted with
 * globalCompositeOperation "lighter" (never ctx.filter). Every tint is an
 * EXISTING validated family color — cores are round-1's march tints
 * (#bfdcff / #ffdf9e / #dfe6ee), falloffs the validated bodies
 * (#4595ec / #c78202 / #476aa4); the neutral rail glow uses the arrowhead
 * white #e8eef6 over fleet-blue #85c2ff. No new tints introduced.
 *
 * DISCIPLINE:
 *   - FULL STOP: no frames at all when document.hidden, connection ≠ "live",
 *     or zero active stubs — the canvas paints nothing and the ticker stops.
 *   - REDUCED MOTION: the ticker never starts; exactly ONE static frame is
 *     painted — a soft gradient stamp along each path (a glowing conduit),
 *     terminal pools at final intensity, ~5 deterministic particles
 *     (t = .12/.32/.52/.72/.9) — and never while document.hidden (a hidden
 *     tab gets no repaints of any kind). Premium stillness, never frozen
 *     motion.
 *   - ALLOCATION DISCIPLINE: the animation loop allocates nothing per frame —
 *     renderers iterate the shared Set directly (a renderer only ever evicts
 *     ITSELF mid-frame, which Set iteration tolerates by specification),
 *     particle positions evaluate through pointAtInto() into one reused
 *     scratch point per renderer, and terminal blooms draw through one shared
 *     helper called explicitly per path end (no array literals, no result
 *     objects anywhere in the frame path).
 *   - dt clamped at 50 ms; DPR capped at 2; resize via ResizeObserver with
 *     unit scale = cssWidth/240 (all drawing happens in viewBox units).
 *   - jsdom guard: getContext('2d') null → permanent no-op (vitest-safe).
 *   - StrictMode double-mount safe: destroy() fully detaches from the shared
 *     ticker and drops every pool reference.
 */
import { BUS_VIEWBOX, RAIL_Y, approxLength, pointAt, pointAtInto, tangentAt, type FlowPath, type FlowPoint } from "./flowGeometry";

/** Which family a stream belongs to (the tone picks its sprite pair). */
export type GlowTone = "grid" | "battery" | "home";

/** One live stub as the bus feeds it: path oriented ALONG THE FLOW. */
export interface GlowStubSpec {
  tone: GlowTone;
  aim: "into-bus" | "to-node";
  path: FlowPath;
  /** |watts| / scaleMax (0..1) — drives density, speed, radius, glow. */
  share: number;
}

/** The rail's through-flow energization segment, in viewBox units. */
export interface RailThroughSpec {
  x1: number;
  x2: number;
  /** min(Σ into-shares, Σ out-shares), clamped 0..1. */
  intensity: number;
}

export interface GlowEnvironment {
  reducedMotion: boolean;
  /** False while document.hidden (the bus also re-syncs on visibilitychange). */
  running: boolean;
  connection: "connecting" | "live" | "lost";
}

// --- tuning constants ---------------------------------------------------------
// Every DRAMA dial from round 3a lives here or in the pure laws below; the
// hard ceilings (no hue changes, no strobing, halos ≤ the prescribed alphas,
// reduced-motion frame still legible) are enforced by those laws' shapes.

const DT_CLAMP_S = 0.05;
const DPR_CAP = 2;
const SHARE_TAU_S = 0.4; // intensity smoothing τ = 400 ms
const FADE_BAND = 0.08; // fade-in/out over the first/last 8% of the path
const WANDER_MAX_UNITS = 1.2;
const BREATH_PERIOD_MS = 2400;
const BREATH_AMP = 0.07; // ±7% terminal breathing
// Hard particle ceiling per stub. Worst case under the round-3a river rate:
// ~17 spawns/s ÷ 110 units/s over a ~62-unit conduit ≈ 10 alive at steady
// state; even a fleet split's five streams hold ~50 — the cap never binds in
// lawful operation (a dt-clamped stall can request <1 spawn/frame), it only
// bounds pathology.
const POOL_CAP = 72;
const SPRITE_PX = 96;
/** Sprite draw diameter = radius × this (the gradient's bright core ≈ 45%). */
const BLIT_FACTOR = 4.5;
// ROUND 3B rail filament: the halo is ONE radial sprite stretched across the
// whole energized segment (soft elliptical falloff, hottest mid-span), and
// the core is a single hairline stroke — no per-step chain, no beading, and
// no per-frame gradient allocation (a cached CanvasGradient would go stale
// as the τ-smoothed intensity glides every frame).
const RAIL_HALO_HEIGHT = 16;
const RAIL_HALO_ALPHA = 0.5;
const RAIL_HALO_EXTEND = 12; // sprite radius carried past each end for a soft entry
const RAIL_CORE_WIDTH = 2.4;
const RAIL_CORE_GAIN = 1.15;
const RAIL_CORE_STYLE = "rgba(232, 238, 246, 0.9)"; // the arrowheads' neutral white
/** Deterministic static-frame particle positions (reduced motion). */
const STATIC_TS = [0.12, 0.32, 0.52, 0.72, 0.9] as const;
// Round-3a drama dials consumed by the pure laws above.
const TERMINAL_R_MIN = 6;
const TERMINAL_R_MAX = 16; // was 14
const TERMINAL_ALPHA_GAIN = 1.25; // round-2 envelope ×1.25
const STREAK_ALPHA_BASE = 0.18; // round-2 trail law …
const STREAK_ALPHA_PER_SHARE = 0.3;
const STREAK_ALPHA_GAIN = 1.3; // … ×1.3
const STREAK_LENGTH_FACTOR = 1.5; // light-paint length ×1.5
const HOT_BLEND_START = 0.5; // filament crossfade window
const HOT_BLEND_END = 0.85;
const RAIL_ALPHA_MIN = 0.14; // rail energization range 0.14→0.60 (was ~0.03→0.12)
const RAIL_ALPHA_SPAN = 0.46;

// --- THE MOTION LAWS (pure, exported, unit-pinned in glow.test.ts) --------------
// Hostile inputs are clamped, never propagated: a NaN share reads as calm,
// an out-of-range share reads as its nearest bound — engine arithmetic can
// never poison a frame.

function clampShare(share: number): number {
  if (Number.isNaN(share)) {
    return 0;
  }
  return share < 0 ? 0 : share > 1 ? 1 : share;
}

/** Spawn rate: 3 + 14·share particles/second — round-3a RIVER dial (was 2+10). */
export function spawnRate(share: number): number {
  const s = clampShare(share);
  return 3 + 14 * s;
}

/** Along-path speed: 30 + 80·share units/second (±10% jitter applied at spawn). */
export function streamSpeed(share: number): number {
  const s = clampShare(share);
  return 30 + 80 * s;
}

/** Bead core radius: 1.3 + 1.9·share units — must read at a glance (was 0.8+1.2). */
export function particleRadius(share: number): number {
  const s = clampShare(share);
  return 1.3 + 1.9 * s;
}

/** Bead sprite alpha: 0.55 + 0.45·share — exactly 1 at full share, never above. */
export function particleCoreAlpha(share: number): number {
  const s = clampShare(share);
  return 0.55 + 0.45 * s;
}

/** Motion-blur streak alpha: round-2's trail law ×1.3 (clearly visible trails):
 *  1.3 × (0.18 + 0.30·share) → 0.234 .. 0.624, always < 1. */
export function particleStreakAlpha(share: number): number {
  const s = clampShare(share);
  return STREAK_ALPHA_GAIN * (STREAK_ALPHA_BASE + STREAK_ALPHA_PER_SHARE * s);
}

/** Terminal bloom radius: 6 → 16 units ∝ share (round-3a cap raise from 14). */
export function terminalRadius(share: number): number {
  const s = clampShare(share);
  return TERMINAL_R_MIN + (TERMINAL_R_MAX - TERMINAL_R_MIN) * s;
}

/** Terminal bloom alpha envelope: round-2's (0.22 + 0.30·share) × 1.25. */
export function terminalAlpha(share: number): number {
  const s = clampShare(share);
  return TERMINAL_ALPHA_GAIN * (0.22 + 0.3 * s);
}

/**
 * THE BLOOM EDGE CAP: a blit centered at anchorY may never leave the canvas,
 * because a clipped additive glow ends in a hard chopped line — the exact
 * artifact the owner flagged in round 2. The blit is scaled down until it
 * fits: min(raw diameter, 2·distance-to-nearest-horizontal-edge), measured
 * against the REAL box edges (BUS_VIEWBOX.minY .. minY + unitsH) — the bus
 * box starts at y=−8, so the junction anchor's true top headroom is 24, not
 * 16. Non-finite inputs yield 0 (draw nothing).
 */
export function terminalBlitDiameter(radiusUnits: number, anchorY: number, unitsH: number): number {
  if (!Number.isFinite(radiusUnits) || !Number.isFinite(anchorY) || !Number.isFinite(unitsH)) {
    return 0;
  }
  const raw = Math.max(0, radiusUnits) * BLIT_FACTOR;
  const top = BUS_VIEWBOX.minY;
  const headroom = Math.max(0, Math.min(anchorY - top, top + unitsH - anchorY));
  return Math.min(raw, 2 * headroom);
}

/** Rail energization alpha: 0.14 → 0.60 across intensity 0..1 (round-3a range,
 *  was 0.03 + 0.09·intensity). */
export function railAlpha(intensity: number): number {
  const i = clampShare(intensity);
  return RAIL_ALPHA_MIN + RAIL_ALPHA_SPAN * i;
}

/**
 * The hot-filament crossfade: 0 below HOT_BLEND_START, 1 at/above
 * HOT_BLEND_END, linear between. At blend > 0 a second, hotter-gradient
 * sprite of the SAME two validated tints joins the base blit (weights sum to
 * 1), so high-share streams grow a near-white filament core without any new
 * hue entering the picture.
 */
export function hotBlend(share: number): number {
  const s = clampShare(share);
  if (s <= HOT_BLEND_START) {
    return 0;
  }
  if (s >= HOT_BLEND_END) {
    return 1;
  }
  return (s - HOT_BLEND_START) / (HOT_BLEND_END - HOT_BLEND_START);
}

/**
 * Prior-runtime identity: tone + aim alone. Tone IS the slot (grid/battery/
 * home own slots 40/120/200 exclusively) and a slot contributes at most one
 * stub per aim, so this pair uniquely names a stream — and deliberately
 * EXCLUDES share and path endpoints, so a topology change (split ↔ single,
 * which moves landings but not identities) matches its prior runtime and the
 * surviving stream GLIDES instead of snapping.
 */
export function stubKey(spec: { readonly tone: string; readonly aim: string }): string {
  return `${spec.tone}:${spec.aim}`;
}

/** Family sprite tints: bright core → body falloff (all previously validated). */
const TONE_CORE: Record<GlowTone, string> = { grid: "#bfdcff", battery: "#ffdf9e", home: "#dfe6ee" };
const TONE_BODY: Record<GlowTone, string> = { grid: "#4595ec", battery: "#c78202", home: "#476aa4" };
const RAIL_CORE = "#e8eef6"; // the arrowheads' neutral near-white
const RAIL_BODY = "#85c2ff"; // the fleet family's text blue

interface Particle {
  t: number;
  ageS: number;
  speedUnitsS: number;
  radiusUnits: number;
  wanderPhase: number;
  wanderRateHz: number;
  prevX: number;
  prevY: number;
}

interface StubRuntime {
  spec: GlowStubSpec;
  lengthUnits: number;
  smoothed: number;
  spawnAcc: number;
  breathPhase: number;
  pool: Particle[];
}

// --- module-level single ticker -------------------------------------------------

const renderers = new Set<FlowGlowRenderer>();
let rafId: number | null = null;
let lastFrameMs = 0;

function hasRaf(): boolean {
  return typeof requestAnimationFrame === "function";
}

function requestTick(): void {
  if (rafId !== null || !hasRaf()) {
    return;
  }
  lastFrameMs =
    typeof performance === "object" && typeof performance.now === "function" ? performance.now() : 0;
  rafId = requestAnimationFrame(onTick);
}

function onTick(nowMs: number): void {
  rafId = null;
  const previous = lastFrameMs;
  lastFrameMs = nowMs;
  const dtS = Math.min(DT_CLAMP_S, Math.max(0, (nowMs - previous) / 1000));
  // Direct Set iteration — no per-frame copy. ECMA-262 §24.2.1: entries
  // deleted BEFORE the iterator reaches them are skipped; a renderer's
  // frame() only ever deletes ITSELF (self-eviction), never a sibling, so
  // the loop is deletion-safe by construction and allocates nothing.
  for (const renderer of renderers) {
    renderer.frame(nowMs, dtS);
  }
  if (renderers.size > 0) {
    requestTick();
  }
}

/**
 * jsdom probe: taken ONCE per module load so vitest environments without a
 * 2D context never even attempt a getContext call on a live canvas.
 */
const CANVAS_2D_OK: boolean = (() => {
  try {
    if (typeof document === "undefined") {
      return false;
    }
    const probe = document.createElement("canvas");
    return typeof probe.getContext === "function" && probe.getContext("2d") !== null;
  } catch {
    return false;
  }
})();

// --- sprites ---------------------------------------------------------------------

const spriteCache = new Map<string, HTMLCanvasElement>();

function rgba(hex: string, alpha: number): string {
  const value = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

/** One radial-gradient stop: [offset, hex tint, alpha]. */
type SpriteStop = readonly [number, string, number];

/**
 * A radial glow sprite pre-rendered once, then blitted additively. Two
 * profiles per family, BOTH built from the same validated core/body tints
 * (the dial said "hotter center, NOT new hues"):
 *   base — round-2's profile: a soft core falling early into the body.
 *   hot  — the filament: the near-white core holds further out (0.34 at
 *          88%) before the body falloff, so a high-share stream reads as a
 *          white-hot wire inside its colored glow.
 */
const BASE_STOPS = (core: string, body: string): readonly SpriteStop[] => [
  [0, core, 1],
  [0.2, core, 0.75],
  [0.45, body, 0.25],
  [1, body, 0],
];
const HOT_STOPS = (core: string, body: string): readonly SpriteStop[] => [
  [0, core, 1],
  [0.34, core, 0.88],
  [0.58, body, 0.3],
  [1, body, 0],
];

function sprite(key: string, stops: readonly SpriteStop[]): HTMLCanvasElement | null {
  const cached = spriteCache.get(key);
  if (cached !== undefined) {
    return cached;
  }
  if (!CANVAS_2D_OK) {
    return null;
  }
  const canvas = document.createElement("canvas");
  canvas.width = SPRITE_PX;
  canvas.height = SPRITE_PX;
  const ctx = canvas.getContext("2d");
  if (ctx === null) {
    return null;
  }
  const r = SPRITE_PX / 2;
  const gradient = ctx.createRadialGradient(r, r, 0, r, r, r);
  for (const [offset, color, alpha] of stops) {
    gradient.addColorStop(offset, rgba(color, alpha));
  }
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, SPRITE_PX, SPRITE_PX);
  spriteCache.set(key, canvas);
  return canvas;
}

function toneSprite(tone: GlowTone, hot: boolean): HTMLCanvasElement | null {
  return sprite(
    hot ? `tone:${tone}:hot` : `tone:${tone}`,
    hot ? HOT_STOPS(TONE_CORE[tone], TONE_BODY[tone]) : BASE_STOPS(TONE_CORE[tone], TONE_BODY[tone]),
  );
}

function railSprite(): HTMLCanvasElement | null {
  return sprite("rail", BASE_STOPS(RAIL_CORE, RAIL_BODY));
}

// --- the renderer ----------------------------------------------------------------

export class FlowGlowRenderer {
  private canvas: HTMLCanvasElement | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private observer: ResizeObserver | null = null;
  /** Fallback resize hook where ResizeObserver is missing (old embedded webviews). */
  private onWindowResize: (() => void) | null = null;
  private dead = false; // jsdom / no-2D: permanent no-op
  private cleared = true;
  private unitsW = 240;
  // Annotated number: BUS_VIEWBOX.height is a const literal (128) and resize()
  // rewrites this field from measured pixels.
  private unitsH: number = BUS_VIEWBOX.height; // the bus viewBox (bloom headroom)
  private unitScale = 1;
  private dpr = 1;
  private runtimes: StubRuntime[] = [];
  private rail: RailThroughSpec | null = null;
  private railSmoothed = 0;
  private env: GlowEnvironment = { reducedMotion: false, running: true, connection: "live" };
  /** One reused de Casteljau evaluation point — the animation loop's
   *  particle placements write here instead of allocating (see pointAtInto). */
  private readonly scratch: FlowPoint = { x: 0, y: 0 };

  /** True while this renderer needs animation frames from the shared ticker. */
  private get wantsFrames(): boolean {
    return (
      !this.dead &&
      this.ctx !== null &&
      this.env.reducedMotion === false &&
      this.env.running &&
      this.env.connection === "live" &&
      this.runtimes.length > 0 &&
      !this.hiddenNow
    );
  }

  /** Live visibility probe (not the last-known env flag): a repaint of any
   *  kind is skipped while the document is hidden — including the
   *  reduced-motion static frame, which used to repaint behind the tab. */
  private get hiddenNow(): boolean {
    return typeof document === "object" && document.hidden === true;
  }

  /** Attach one canvas. A null 2D context marks the renderer permanently dead. */
  attach(canvas: HTMLCanvasElement): void {
    if (this.dead) {
      return;
    }
    this.canvas = canvas;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = CANVAS_2D_OK ? canvas.getContext("2d") : null;
    } catch {
      ctx = null;
    }
    if (ctx === null) {
      this.dead = true;
      this.canvas = null;
      return;
    }
    this.ctx = ctx;
    this.observeResize();
    this.resize();
  }

  /** Replace the active-stub set (+ rail segment). Shares glide via τ, and
   *  runtimes are matched to their predecessors by STABLE KEY (tone+aim — see
   *  stubKey), so a topology change like fleet split→single keeps the
   *  surviving stream's pool and glide instead of snapping to fresh. */
  setSpec(stubs: readonly GlowStubSpec[], railThrough: RailThroughSpec | null): void {
    if (this.dead) {
      return;
    }
    const priors = new Map<string, StubRuntime>();
    for (const runtime of this.runtimes) {
      priors.set(stubKey(runtime.spec), runtime);
    }
    const next: StubRuntime[] = [];
    for (let index = 0; index < stubs.length; index += 1) {
      const spec = stubs[index]!;
      const key = stubKey(spec);
      const prior = priors.get(key);
      priors.delete(key); // first match wins; duplicates are impossible today
      next.push({
        spec,
        lengthUnits: Math.max(1, approxLength(spec.path)),
        smoothed: prior?.smoothed ?? spec.share,
        spawnAcc: prior?.spawnAcc ?? 0,
        breathPhase: index * 0.7, // deterministic phase spread across stubs
        pool: prior?.pool ?? [],
      });
    }
    this.runtimes = next;
    this.rail = railThrough;
    this.sync();
  }

  setEnvironment(env: GlowEnvironment): void {
    if (this.dead) {
      return;
    }
    this.env = env;
    this.sync();
  }

  destroy(): void {
    this.dead = true;
    renderers.delete(this);
    this.observer?.disconnect();
    this.observer = null;
    if (this.onWindowResize !== null && typeof window === "object") {
      window.removeEventListener("resize", this.onWindowResize);
      this.onWindowResize = null;
    }
    this.runtimes = [];
    this.canvas = null;
    this.ctx = null;
  }

  // -- internals ------------------------------------------------------------------

  private observeResize(): void {
    const canvas = this.canvas;
    if (canvas === null) {
      return;
    }
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(() => {
        this.resize();
        if (this.env.reducedMotion && !this.hiddenNow) {
          this.paintStaticFrame(); // stay crisp at the new size
        }
      });
      observer.observe(canvas.parentElement ?? canvas);
      this.observer = observer;
      return;
    }
    // No ResizeObserver: fall back to the window's resize event.
    this.onWindowResize = () => {
      this.resize();
      if (this.env.reducedMotion && !this.hiddenNow) {
        this.paintStaticFrame();
      }
    };
    if (typeof window === "object") {
      window.addEventListener("resize", this.onWindowResize);
    }
  }

  /** Size the backing store to the CSS box (parent-driven), DPR-capped, and
   *  map all future drawing into viewBox units (unit scale = cssW/240). */
  private resize(): void {
    const canvas = this.canvas;
    const ctx = this.ctx;
    if (canvas === null || ctx === null) {
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const cssW = rect.width > 0 ? rect.width : (canvas.parentElement?.clientWidth ?? 240);
    const cssH = rect.height > 0 ? rect.height : cssW * (BUS_VIEWBOX.height / BUS_VIEWBOX.width);
    this.dpr = Math.min(DPR_CAP, typeof window === "object" ? window.devicePixelRatio || 1 : 1);
    const widthPx = Math.max(1, Math.round(cssW * this.dpr));
    const heightPx = Math.max(1, Math.round(cssH * this.dpr));
    if (canvas.width !== widthPx || canvas.height !== heightPx) {
      canvas.width = widthPx;
      canvas.height = heightPx;
    }
    this.unitScale = cssW / 240;
    this.unitsW = widthPx / (this.dpr * this.unitScale);
    this.unitsH = heightPx / (this.dpr * this.unitScale);
    ctx.setTransform(this.dpr * this.unitScale, 0, 0, this.dpr * this.unitScale, 0, 0);
    this.cleared = false;
  }

  /** Re-evaluate participation after any state change. */
  private sync(): void {
    if (this.dead) {
      return;
    }
    if (this.wantsFrames) {
      this.cleared = false;
      renderers.add(this);
      requestTick();
      return;
    }
    renderers.delete(this);
    if (this.env.reducedMotion) {
      // Premium stillness — exactly one static frame, only for a live world
      // that is actually watching: a hidden tab gets no repaints at all
      // (the frame waits for visibilitychange to re-sync).
      if (this.env.connection === "live" && this.env.running && !this.hiddenNow) {
        this.paintStaticFrame();
      } else {
        this.clear();
      }
      return;
    }
    // Full stop: hidden, disconnected, or nothing alive — paint nothing.
    this.clear();
  }

  private clear(): void {
    const ctx = this.ctx;
    if (ctx === null || this.cleared) {
      return;
    }
    ctx.clearRect(0, 0, this.unitsW, this.unitsH);
    this.cleared = true;
  }

  /** Called by the shared ticker. Self-evicts when it no longer wants frames. */
  frame(nowMs: number, dtS: number): void {
    if (!this.wantsFrames) {
      renderers.delete(this);
      this.clear();
      return;
    }
    const ctx = this.ctx;
    if (ctx === null) {
      return;
    }
    const glide = 1 - Math.exp(-dtS / SHARE_TAU_S);
    ctx.clearRect(0, 0, this.unitsW, this.unitsH);
    this.cleared = false;
    ctx.globalCompositeOperation = "lighter";
    this.drawRail(glide, ctx);
    for (const runtime of this.runtimes) {
      runtime.smoothed += (runtime.spec.share - runtime.smoothed) * glide;
      this.updatePool(runtime, dtS);
      this.drawTerminals(runtime, nowMs, ctx);
      this.drawParticles(runtime, ctx);
    }
    ctx.globalCompositeOperation = "source-over";
  }

  private updatePool(runtime: StubRuntime, dtS: number): void {
    const share = runtime.smoothed;
    runtime.spawnAcc += spawnRate(share) * dtS;
    while (runtime.spawnAcc >= 1 && runtime.pool.length < POOL_CAP) {
      runtime.spawnAcc -= 1;
      const jitter = 0.9 + Math.random() * 0.2; // ±10% speed jitter
      const startJitter = Math.random() * 0.05; // de-phase births off t=0 exactly
      runtime.pool.push({
        t: -startJitter,
        ageS: 0,
        speedUnitsS: streamSpeed(share) * jitter,
        radiusUnits: particleRadius(share),
        wanderPhase: Math.random() * Math.PI * 2,
        wanderRateHz: 1.4 + Math.random() * 1.2,
        prevX: Number.NaN,
        prevY: Number.NaN,
      });
    }
    if (runtime.spawnAcc >= 1) {
      runtime.spawnAcc = 0; // pool saturated: drop the backlog, don't burst
    }
    for (let index = runtime.pool.length - 1; index >= 0; index -= 1) {
      const particle = runtime.pool[index]!;
      particle.ageS += dtS;
      particle.t += (particle.speedUnitsS * dtS) / runtime.lengthUnits;
      if (particle.t >= 1) {
        runtime.pool.splice(index, 1);
      }
    }
  }

  private drawTerminals(runtime: StubRuntime, nowMs: number, ctx: CanvasRenderingContext2D): void {
    const base = toneSprite(runtime.spec.tone, false);
    if (base === null) {
      return;
    }
    const share = runtime.smoothed;
    const blend = hotBlend(share);
    const hot = blend > 0 ? toneSprite(runtime.spec.tone, true) : null;
    const breath =
      1 +
      BREATH_AMP *
        Math.sin((2 * Math.PI * ((nowMs % BREATH_PERIOD_MS) / BREATH_PERIOD_MS)) + runtime.breathPhase);
    const radius = terminalRadius(share) * breath;
    const alpha = terminalAlpha(share);
    // Each end is edge-capped against ITS OWN distance to the canvas edge —
    // light grows to fill the viewBox's headroom, never clips past it. Two
    // explicit calls into one reused helper: no array literals, no closures,
    // nothing allocated per frame (the allocation-discipline claim stays TRUE).
    const start = runtime.spec.path.p0;
    const startDiameter = terminalBlitDiameter(radius, start.y, this.unitsH);
    if (startDiameter > 0) {
      this.blitBloom(ctx, base, hot, blend, alpha, start.x, start.y, startDiameter);
    }
    const end = runtime.spec.path.p3;
    const endDiameter = terminalBlitDiameter(radius, end.y, this.unitsH);
    if (endDiameter > 0) {
      this.blitBloom(ctx, base, hot, blend, alpha, end.x, end.y, endDiameter);
    }
    ctx.globalAlpha = 1;
  }

  /** One terminal bloom blit with the hot-filament crossfade (weights sum to
   *  1 — same light budget, hotter center). Pure draw call, zero allocation;
   *  shared by the live frame and the reduced-motion static frame. */
  private blitBloom(
    ctx: CanvasRenderingContext2D,
    base: HTMLCanvasElement,
    hot: HTMLCanvasElement | null,
    blend: number,
    alpha: number,
    x: number,
    y: number,
    diameter: number,
  ): void {
    if (hot === null || blend >= 1) {
      ctx.globalAlpha = alpha;
      ctx.drawImage(hot ?? base, x - diameter / 2, y - diameter / 2, diameter, diameter);
    } else if (blend <= 0) {
      ctx.globalAlpha = alpha;
      ctx.drawImage(base, x - diameter / 2, y - diameter / 2, diameter, diameter);
    } else {
      ctx.globalAlpha = alpha * (1 - blend);
      ctx.drawImage(base, x - diameter / 2, y - diameter / 2, diameter, diameter);
      ctx.globalAlpha = alpha * blend;
      ctx.drawImage(hot, x - diameter / 2, y - diameter / 2, diameter, diameter);
    }
  }

  private drawParticles(runtime: StubRuntime, ctx: CanvasRenderingContext2D): void {
    const base = toneSprite(runtime.spec.tone, false);
    if (base === null) {
      return;
    }
    const share = runtime.smoothed;
    const blend = hotBlend(share);
    const hot = blend > 0 ? toneSprite(runtime.spec.tone, true) : null;
    const coreAlpha = particleCoreAlpha(share);
    const streakAlpha = particleStreakAlpha(share);
    const strokeColor = TONE_BODY[runtime.spec.tone];
    const path = runtime.spec.path;
    ctx.lineCap = "round";
    for (const particle of runtime.pool) {
      if (particle.t < 0) {
        continue; // pre-birth jitter window
      }
      const envelope = fadeEnvelope(particle.t);
      if (envelope <= 0) {
        continue;
      }
      const diameter = particle.radiusUnits * BLIT_FACTOR;
      const position = pointAtInto(path, particle.t, this.scratch);
      const angle = tangentAt(path, particle.t);
      // Lateral sine wander along the perpendicular, clamped ≤1.2 units and
      // pinned to zero at both ends by the same envelope that fades alpha.
      const lateral =
        Math.sin(particle.wanderPhase + 2 * Math.PI * particle.wanderRateHz * particle.ageS) *
        WANDER_MAX_UNITS *
        envelope;
      const x = position.x + Math.cos(angle + Math.PI / 2) * lateral;
      const y = position.y + Math.sin(angle + Math.PI / 2) * lateral;
      // Motion-blur streak: from just BEFORE the previous frame's position to
      // now — round-3a stretches the light-paint to STREAK_LENGTH_FACTOR × a
      // frame's travel by extrapolating half a segment further back under the
      // constant-velocity assumption (skip the birth frame — no "previous").
      if (!Number.isNaN(particle.prevX)) {
        const tailX = particle.prevX - (x - particle.prevX) * (STREAK_LENGTH_FACTOR - 1);
        const tailY = particle.prevY - (y - particle.prevY) * (STREAK_LENGTH_FACTOR - 1);
        ctx.globalAlpha = envelope * streakAlpha;
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = diameter / 3;
        ctx.beginPath();
        ctx.moveTo(tailX, tailY);
        ctx.lineTo(x, y);
        ctx.stroke();
      }
      // The bead itself: crossfade base → hot filament sprite as share climbs.
      if (hot === null || blend >= 1) {
        ctx.globalAlpha = envelope * coreAlpha;
        ctx.drawImage(hot ?? base, x - diameter / 2, y - diameter / 2, diameter, diameter);
      } else if (blend <= 0) {
        ctx.globalAlpha = envelope * coreAlpha;
        ctx.drawImage(base, x - diameter / 2, y - diameter / 2, diameter, diameter);
      } else {
        ctx.globalAlpha = envelope * coreAlpha * (1 - blend);
        ctx.drawImage(base, x - diameter / 2, y - diameter / 2, diameter, diameter);
        ctx.globalAlpha = envelope * coreAlpha * blend;
        ctx.drawImage(hot, x - diameter / 2, y - diameter / 2, diameter, diameter);
      }
      particle.prevX = x;
      particle.prevY = y;
    }
    ctx.globalAlpha = 1;
  }

  private drawRail(glide: number, ctx: CanvasRenderingContext2D): void {
    const rail = this.rail;
    const targetIntensity = rail?.intensity ?? 0;
    this.railSmoothed += (targetIntensity - this.railSmoothed) * glide;
    if (rail === null || this.railSmoothed <= 0.001) {
      return;
    }
    this.strokeRailSegment(
      ctx,
      Math.min(rail.x1, rail.x2),
      Math.max(rail.x1, rail.x2),
      railAlpha(this.railSmoothed),
    );
  }

  /**
   * THE ENERGIZED RAIL (round 3b): one continuous filament across the
   * through-flow segment — a soft halo (the radial sprite stretched into an
   * elliptical band, brightest mid-span, carried a sprite-radius past each
   * end so energy enters and leaves without a hard step) under a single
   * hairline core stroke in the neutral near-white. Round 3a's chained
   * sprite walk stepped every 9 units and read as a dotted bead-chain; the
   * gradient-stroke alternative was rejected because a CanvasGradient would
   * have to be rebuilt every frame while the τ-smoothed intensity glides
   * (breaking the engine's zero-allocation frame law). Two draw calls, zero
   * allocations. Painted additively like everything else in the engine.
   */
  private strokeRailSegment(
    ctx: CanvasRenderingContext2D,
    from: number,
    to: number,
    alpha: number,
  ): void {
    const sprite = railSprite();
    if (sprite === null) {
      return;
    }
    // The halo: one stretched blit spanning the whole segment.
    ctx.globalAlpha = alpha * RAIL_HALO_ALPHA;
    ctx.drawImage(
      sprite,
      from - RAIL_HALO_EXTEND,
      RAIL_Y - RAIL_HALO_HEIGHT / 2,
      to - from + RAIL_HALO_EXTEND * 2,
      RAIL_HALO_HEIGHT,
    );
    // The core: one continuous hairline with round caps.
    ctx.globalAlpha = Math.min(1, alpha * RAIL_CORE_GAIN);
    ctx.strokeStyle = RAIL_CORE_STYLE;
    ctx.lineWidth = RAIL_CORE_WIDTH;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(from, RAIL_Y);
    ctx.lineTo(to, RAIL_Y);
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  /**
   * THE STATIC FRAME (reduced motion): soft gradient stamps chained along
   * each active path — the conduit reads as glowing without moving — plus
   * terminal pools at final intensity and five deterministic particle
   * capsules. Painted once per state change (never while hidden); the ticker
   * NEVER starts. Same laws as the live engine so stillness stays truthful
   * to what motion looks like.
   */
  private paintStaticFrame(): void {
    const ctx = this.ctx;
    if (ctx === null) {
      return;
    }
    ctx.clearRect(0, 0, this.unitsW, this.unitsH);
    this.cleared = false;
    ctx.globalCompositeOperation = "lighter";
    const rail = this.rail;
    if (rail !== null && rail.intensity > 0) {
      // Same continuous filament as the live frame — stillness stays truthful
      // to what motion looks like.
      this.strokeRailSegment(
        ctx,
        Math.min(rail.x1, rail.x2),
        Math.max(rail.x1, rail.x2),
        railAlpha(rail.intensity),
      );
    }
    for (const runtime of this.runtimes) {
      const base = toneSprite(runtime.spec.tone, false);
      if (base === null) {
        continue;
      }
      const share = runtime.spec.share;
      runtime.smoothed = share;
      const blend = hotBlend(share);
      const hot = blend > 0 ? toneSprite(runtime.spec.tone, true) : null;
      // The glowing conduit itself: overlapping soft stamps every ~5 units,
      // lifted from round 2 (0.08+0.18·share) so the reduced-motion frame
      // keeps its legibility under the brighter moving picture it stands in
      // for.
      const steps = Math.max(2, Math.ceil(runtime.lengthUnits / 5));
      const chainDiameter = (1.6 + 2.6 * share) * BLIT_FACTOR * 0.55;
      ctx.globalAlpha = 0.1 + 0.22 * share;
      for (let step = 0; step <= steps; step += 1) {
        const p = pointAt(runtime.spec.path, step / steps);
        ctx.drawImage(base, p.x - chainDiameter / 2, p.y - chainDiameter / 2, chainDiameter, chainDiameter);
      }
      // Terminal pools at final intensity (no breathing in stillness), edge-
      // capped per anchor exactly like the live blooms — same helper.
      const radius = terminalRadius(share);
      const poolAlpha = terminalAlpha(share);
      const start = runtime.spec.path.p0;
      const startPool = terminalBlitDiameter(radius, start.y, this.unitsH);
      if (startPool > 0) {
        this.blitBloom(ctx, base, hot, blend, poolAlpha, start.x, start.y, startPool);
      }
      const end = runtime.spec.path.p3;
      const endPool = terminalBlitDiameter(radius, end.y, this.unitsH);
      if (endPool > 0) {
        this.blitBloom(ctx, base, hot, blend, poolAlpha, end.x, end.y, endPool);
      }
      // Five deterministic capsules mid-path — presence without motion.
      const dotDiameter = particleRadius(share) * BLIT_FACTOR;
      for (const t of STATIC_TS) {
        const p = pointAt(runtime.spec.path, t);
        ctx.globalAlpha = fadeEnvelope(t) * particleCoreAlpha(share);
        ctx.drawImage(base, p.x - dotDiameter / 2, p.y - dotDiameter / 2, dotDiameter, dotDiameter);
      }
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }
}

/**
 * Fade-in over the first 8% of the path, fade-out over the last 8%. Hostile
 * parameters are clamped: NaN and anything below 0 read as invisible (0),
 * anything above 1 reads as the path's end envelope.
 */
export function fadeEnvelope(t: number): number {
  if (Number.isNaN(t)) {
    return 0;
  }
  if (t < FADE_BAND) {
    return Math.max(0, t / FADE_BAND);
  }
  if (t > 1 - FADE_BAND) {
    return Math.max(0, (1 - t) / FADE_BAND);
  }
  return 1;
}
