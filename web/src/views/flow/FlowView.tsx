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
 * conductor) with three stub slots. Every active stub is a LIVING STREAM:
 * a curved conduit carries particles whose density, speed, and glow are
 * proportional to watts (the Canvas2D light engine in glow.ts), traveling
 * in the arrowhead's direction; magnitude lives in motion and in the words,
 * never again in a ribbon's thickness (that grammar retired in round 2).
 * Color only names the node family. The worded figure sits beside every
 * node ("Importing 412 W", never a raw signed number). Idle states are
 * honest ("Idle" and a hollow ring); absent data says "not available" and is
 * never zero-filled. The SVG+canvas pair is decorative duplication: every
 * fact it draws also exists as text, and each column carries its whole state
 * as an accessible label.
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
 * THE NODE INSTRUMENTS (round 3b): every node tile carries a physical
 * instrument, each an aria-hidden duplicate of text that already exists —
 * the battery tile grows a vertical MOLTEN CELL whose fill level IS the SoC
 * figure (charging shimmers the meniscus IN PLACE; a discharge shimmer is
 * forbidden as a fake level change; unknown SoC renders an empty vessel and
 * words "not available", never a zero fill), the grid tile a machined FEED
 * PORT whose inner tick rotates with import/export (the port itself never
 * claims direction — words and arrowheads own that), and the home tile a
 * warm HEARTH ambience breathing with the load watts (zero when idle or
 * unknown). Geometry moved with the instruments: the bus box grew to
 * `0 -8 240 128` and the landings to y=96 (flowGeometry.ts), lengthening
 * every conduit into the reclaimed page.
 *
 * LIVE DATA: the view rides the session's existing cadence — the shell's
 * SharedDataPlane republishes every REST snapshot read (its ~2.5 s measured-
 * data heartbeat included) to this view's stream subscription, and the event
 * frames patch the adviser projections and trigger re-reads when authority
 * changes. The view never mints its own client (one socket per session) and
 * never invents a figure the wire did not carry.
 */
import { Fragment, useCallback, useEffect, useId, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, StreamEvent } from "../../api/client";
import { formatPercent, formatWatts } from "../../lib/format";
import { isPlaneSnapshot } from "../../app/SharedDataPlane";
import { usePrefersReducedMotion } from "../../app/usePrefersReducedMotion";
import { useUnitIntentFigures } from "../../app/useUnitIntentFigures";
import {
  arrowScaleW,
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
import { FlowBus, IDLE_STUB, UNKNOWN_STUB, activeStub, single } from "./FlowBus";
import type { SlotSpec } from "./FlowBus";
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
 * THE TRUNCATION FIX AT THE CAUSE: a figure word whose widest UNBREAKABLE
 * token reaches this many characters ("Discharging" → 11, "Exporting" → 9,
 * the "available" inside "not available" → 9) wears the `--compact` label
 * register — one deliberate size step-down (flow.css) that lets the longest
 * real string fit its tile's measure at every breakpoint. A single word can
 * never be wrapped (`overflow-wrap` mid-word breaks are banned for these),
 * so fitting is done by typography, and the overflow probe
 * (scripts/check-flow-overflow.mjs) fails the round if any string still
 * escapes its tile.
 */
const COMPACT_WORD_TOKEN_MIN = 8;

/** The longest single unbreakable token of one figure word ("not available" → 9). */
function widestToken(word: string): number {
  return Math.max(...word.split(" ").map((token) => token.length));
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
 * registers (flow.css ≥81rem): the word a small label line, the figure the
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
              <span
                className={`flow-fig-word${
                  widestToken(part.word) >= COMPACT_WORD_TOKEN_MIN ? " flow-fig-word--compact" : ""
                }`}
              >
                {part.word}
              </span>
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
 * family's gold, the "charged" word quiet beside it, and beneath them THE
 * MOLTEN CELL (round 3b): a vertical glass vessel whose fill level IS the
 * SoC figure — the validated gold ramp stood upright so the light pools at
 * the surface, a bright meniscus line on that boundary, faint segment ticks,
 * a glass specular strip, and a soft end-glow ∝ the reading. While the cell
 * CHARGES, its meniscus shimmers IN PLACE (a surface glint; the fill level
 * itself only ever moves to the real SoC datum — a discharge shimmer is
 * forbidden as a faked level change). The end-glow rides the same reading and
 * floors at exactly 0: a measured-zero vessel renders dark — no halo, no
 * fill layer, no fake surface at the floor. The vessel is decorative
 * aria-hidden duplication; the worded figure above is the primary carrier.
 */
function SocBand({ socPct, charging = false }: { socPct: number; charging?: boolean }): ReactNode {
  const level = Math.min(100, Math.max(0, socPct));
  // Round-3c honesty floor: a MEASURED zero is a dark, empty vessel. The old
  // formula (0.18 + 0.5·level/100) paid an end-glow even at 0% — a faint halo
  // implying charge that was never measured — so the ramp now floors at
  // exactly 0, and the fill layer (with its meniscus line, which would
  // otherwise anchor a glowing "surface" at the vessel floor) does not render
  // at all — the same no-fill-layer rule the unknown-SoC vessel follows.
  return (
    <span className="flow-node-extra flow-node-extra--soc">
      <span className="flow-soc">
        <b className="flow-soc-pct">{formatPercent(socPct)}</b>{" "}
        <span className="flow-soc-word">charged</span>
      </span>
      {/* The level and the end-glow ride in as custom properties from the
          REAL reading; every layer inside is inert glass over that datum. */}
      <span
        className={`flow-soc-cell${charging ? " flow-soc-cell--charging" : ""}`}
        aria-hidden="true"
        style={
          {
            "--cell-level": `${level}%`,
            "--cell-glow": level <= 0 ? "0" : (0.18 + 0.5 * (level / 100)).toFixed(3),
          } as CSSProperties
        }
      >
        <span className="flow-soc-cell-glow" />
        {level > 0 ? (
          <span className="flow-soc-cell-fill">
            <span className="flow-soc-cell-meniscus" />
          </span>
        ) : null}
        <span className="flow-soc-cell-ticks" />
        <span className="flow-soc-cell-gloss" />
      </span>
    </span>
  );
}

/**
 * The battery card's extra when the SoC ITSELF is absent telemetry: the same
 * slot holds an EMPTY vessel — no fill, never a zero-fill lie — and the
 * worded "not available" where the percentage would sit. An unmeasured cell
 * shows nothing it cannot vouch for.
 */
function SocBandUnavailable(): ReactNode {
  return (
    <span className="flow-node-extra flow-node-extra--soc flow-node-extra--soc-unknown">
      <span className="flow-soc">
        <span className="flow-soc-word">not available</span>
      </span>
      <span className="flow-soc-cell" aria-hidden="true" />
    </span>
  );
}

/** Every state the feed port can name. Each one is ALSO a word beside the
 *  node ("Importing", "Exporting", "Idle", "not available") and an arrowhead
 *  on the conduit — the port never carries direction alone. */
type PortDirection = "import" | "export" | "split" | "idle" | "unknown";

/**
 * The grid card's FEED PORT (round 3b): a machined socket ringed in the grid
 * family's blue, docked in the reserved extra slot. When power moves, a twin-
 * chevron terminal mark inside rotates to the flow's heading (up = import,
 * down = export) — restyled in round 3c from a lone bar, which read as an
 * info icon, into gauge-face tooling that cannot be misread as a UI glyph;
 * the fleet's legal both-ways split and idle show no mark at all, and absent
 * telemetry dims the whole socket to presence-without-a-claim. Purely
 * decorative (aria-hidden): the words own every fact.
 */
function FeedPort({ direction }: { direction: PortDirection }): ReactNode {
  return (
    <span className="flow-node-extra flow-node-extra--port">
      <span className="flow-port" aria-hidden="true" data-flow={direction}>
        <span className="flow-port-recess" />
        <span className="flow-port-tick" />
      </span>
    </span>
  );
}

/** The fleet port's direction from the SUMMED picture: importing and
 *  exporting at once is legal there, so the tick stands down ("split") and
 *  the two-sided words carry the exact picture. */
function fleetGridPortDirection(fleet: ReturnType<typeof fleetFlow>): PortDirection {
  if (fleet.gridReporting === 0) {
    return "unknown";
  }
  const importing = (fleet.importW ?? 0) > 0;
  const exporting = (fleet.exportW ?? 0) > 0;
  if (importing && exporting) {
    return "split";
  }
  if (importing) {
    return "import";
  }
  return exporting ? "export" : "idle";
}

/**
 * The home card's HEARTH level (round 3b): load watts against the view's
 * shared scale — exactly 0 when idle or unknown (a dark hearth is the honest
 * hearth), approaching 1 as the house draws its largest measured share.
 * Drives only the ambience's opacity via CSS; the figure stays the carrier.
 */
function hearthLevel(loadW: number | null, scaleMaxW: number): number {
  if (loadW === null || loadW <= 0 || !(scaleMaxW > 0)) {
    return 0;
  }
  return Math.min(1, loadW / scaleMaxW);
}

/**
 * One node card: the node's name, its worded figure, its INSTRUMENT (the
 * round-3b physical layer — battery molten cell / grid feed port), and the
 * home hearth's ambience behind everything.
 */
function FlowNode({
  name,
  parts,
  suffix = "",
  extra = null,
  ambience = 0,
  tone,
}: {
  name: string;
  parts: readonly FlowFigurePart[];
  suffix?: string;
  extra?: ReactNode;
  /** Hearth intensity 0..1 (∝ load watts); >0 renders the ambience layer. */
  ambience?: number;
  tone: "grid" | "battery" | "home";
}): ReactNode {
  return (
    <div className={`flow-node flow-node--${tone}`} data-tone={tone} tabIndex={0}>
      {ambience > 0 ? (
        <span
          className="flow-node-hearth"
          aria-hidden="true"
          style={{ "--hearth-i": ambience.toFixed(3) } as CSSProperties}
        />
      ) : null}
      <span className="flow-node-name">{name}</span>
      <span className="flow-node-figure">
        <FlowFigure parts={parts} suffix={suffix} />
      </span>
      {/* The instrument slot is RESERVED in every card — empty for Home and
          for any battery without a reading — so the same rows land at the
          same y across all columns whether or not a card carries a second
          datum or an instrument (the aligned visual grid). */}
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
            Try again
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
  // The status word up front (the anchor), the explanation after the dash.
  const connectionDash = connectionText.indexOf(" — ");
  const connectionWord = connectionDash === -1 ? connectionText : connectionText.slice(0, connectionDash);
  const connectionRest = connectionDash === -1 ? "" : connectionText.slice(connectionDash);

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
          {/* The status word is the anchor ("Live", "Connection lost") — set it
              forward so the glance lands on the state, not the explanation. */}
          <b className="flow-connection-word">{connectionWord}</b>
          {connectionRest}
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
          {/* THE BAY — every column lives inside one recessed dark-glass
              viewport (.flow-stage): machined bezel, inset vignette, hairline-
              divided zones. Purely presentational (role="presentation"): it
              adds no semantics, so reading order and each column's own
              accessible label are untouched. */}
          <div className="flow-stage" role="presentation">
          {/* THE FLEET COLUMN — FIRST in DOM and reading order: the glance
              path is story → whole site → phase detail, which is also the
              order a screen reader speaks, and the only sane order when the
              grid has collapsed to one column. */}
          <section className="flow-phase flow-phase--fleet" aria-label="Whole site">
            <h3 className="flow-phase-name">Whole site</h3>
            <div className="flow-dock">
              <FlowBus
                grid={fleetGridSlot}
                battery={fleetBatterySlot}
                house={fleetHouseSlot}
                connection={connection}
              />
              <div className="flow-nodes">
                <FlowNode
                  name="Grid"
                  parts={fleetGridFigureParts(fleet)}
                  tone="grid"
                  extra={<FeedPort direction={fleetGridPortDirection(fleet)} />}
                />
                {/* The fleet battery carries no summed SoC — its slot stays
                    reserved (an honest absence, never a guessed vessel). */}
                <FlowNode name="Battery" parts={fleetBatteryFigureParts(fleet)} tone="battery" />
                <FlowNode
                  name="Home"
                  parts={fleetHouseFigureParts(fleet)}
                  suffix={fleetHouseScope(fleet)}
                  tone="home"
                  ambience={hearthLevel(fleet.houseW, scale)}
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
              connection={connection}
            />
          ))}
          </div>
        </div>
      )}

      {/* THE COMMAND OVERLAY — commanded vs measured per phase, only while a
          request is live (the shared per-unit figures machinery). */}
      {commands.length > 0 ? (
        <section className="flow-commands" aria-labelledby="flow-commands-heading">
          <h3 id="flow-commands-heading">Commanded vs delivering</h3>
          <ul>
            {commands.map((row) => (
              // The unit id is the row's anchor — the same scan the diagram's
              // columns train (the wording itself is the pinned row text).
              <li key={row.unitId}>
                <b className="flow-cmd-unit">{row.unitId}</b>
                {row.text.slice(row.unitId.length)}
              </li>
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
  connection,
}: {
  unit: FlowUnit;
  scale: number;
  batteryPulse: boolean;
  /** The view's live-connection state — the light engine full-stops off "live". */
  connection: "connecting" | "live" | "lost";
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
  // The port dims to "unknown" whenever the phase is down or the grid datum
  // is absent — a disconnected socket never rotates a tick it cannot vouch
  // for (the words keep their last-known values, dimmed, per the down rules).
  const gridPortDirection: PortDirection =
    down || gridDirection === "unknown" ? "unknown" : gridDirection;
  return (
    <section
      className={`flow-phase${down ? " flow-phase--down" : ""}`}
      aria-label={`Phase ${unit.unitId} — grid: ${gridText}; battery: ${batteryText}${
        soc === "" ? "" : ` (${soc})`
      }; house: ${houseText}`}
    >
      <h3 className="flow-phase-name">{unit.unitId}</h3>
      <div className="flow-dock">
        <FlowBus
          grid={gridSlot}
          battery={batterySlot}
          house={houseSlot}
          batteryPulse={batteryPulse}
          connection={connection}
        />
        <div className="flow-nodes">
          <FlowNode
            name="Grid"
            parts={gridFigureParts(gridW)}
            tone="grid"
            extra={<FeedPort direction={gridPortDirection} />}
          />
          <FlowNode
            name="Battery"
            parts={batteryFigureParts(batteryW)}
            extra={
              unit.socPct === null ? (
                <SocBandUnavailable />
              ) : (
                // The meniscus may shimmer only while the phase is BOTH
                // charging AND in contact — a downed cell's glint would claim
                // a liveness the wire no longer carries.
                <SocBand socPct={unit.socPct} charging={batteryDirection === "charge" && !down} />
              )
            }
            tone="battery"
          />
          <FlowNode
            name="Home"
            parts={houseFigureParts(loadW)}
            tone="home"
            ambience={hearthLevel(loadW, scale)}
          />
        </div>
      </div>
      {/* The lifecycle note anchors the column's foot — under the figures it
          vouches for — so its presence never shifts any other column's rows. */}
      {note !== null ? <p className="flow-phase-note">{note}</p> : null}
    </section>
  );
}
