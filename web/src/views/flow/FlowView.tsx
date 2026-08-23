/**
 * "Flow" (Energy Flow) view — the console's live at-a-glance picture of where
 * power is going right now: per phase and fleet-wide, grid import/export,
 * battery charge/discharge, and house load, composed from the snapshot's own
 * per-unit telemetry (`grid_power_w` / `load_power_w` / `battery_watts` /
 * `soc_pct` — every datum already on the wire; no new backend surface).
 *
 * THE FLOW PICTURE: one column per phase (a unit is a phase on this
 * three-phase site) plus a fleet column. Each column is three nodes — GRID,
 * BATTERY, HOME — joined to a phase bus by pure-SVG arrows whose thickness is
 * proportional to watts and whose head gives the direction, with the worded
 * figure beside every node ("Importing 412 W", never a raw signed number).
 * Idle states are honest ("Idle"); absent data says "not available" and is
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
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, StreamEvent } from "../../api/client";
import { isPlaneSnapshot } from "../../app/SharedDataPlane";
import { useUnitIntentFigures } from "../../app/useUnitIntentFigures";
import {
  arrowScaleW,
  arrowWidthPx,
  batteryFlowDirection,
  batteryFlowText,
  commandRows,
  FLOW_SOLAR_FOOTNOTE,
  fleetBatteryText,
  fleetFlow,
  fleetGridText,
  fleetHouseText,
  flowStory,
  excessStory,
  gridFlowDirection,
  gridFlowText,
  hasLiveCommand,
  houseFlowText,
  lifecycleNote,
  nightStory,
  socText,
  toFlowSnapshot,
  patchFlowAdviser,
  patchFlowNight,
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

/** Which way one stub's arrow points — the arrowhead IS the direction. */
type StubAim = "into-bus" | "to-node" | "both" | "idle" | "unknown";

interface Stub {
  aim: StubAim;
  widthPx: number;
}

function stubFor(direction: "import" | "export" | "idle" | "unknown", watts: number | null, scale: number): Stub {
  switch (direction) {
    case "import":
      return { aim: "into-bus", widthPx: arrowWidthPx(watts, scale) };
    case "export":
      return { aim: "to-node", widthPx: arrowWidthPx(watts, scale) };
    case "idle":
      return { aim: "idle", widthPx: 0 };
    default:
      return { aim: "unknown", widthPx: 0 };
  }
}

/**
 * One column's phase bus: a horizontal rail (the phase) with three stubs —
 * grid, battery, home — whose stroke width is proportional to watts and whose
 * arrowheads give the direction (into the bus = supplying the phase, out of
 * it = drawing). aria-hidden by design: the worded figures carry the same
 * facts as text right below.
 */
function FlowBus({ grid, battery, house }: { grid: Stub; battery: Stub; house: Stub }): ReactNode {
  const uid = useId().replace(/[^a-zA-Z0-9-]/g, "");
  const markerId = `flow-arrow-${uid}`;
  const railY = 14;
  const stubTop = 22;
  const stubBottom = 92;
  const stub = (cx: number, spec: Stub, color: string): ReactNode => {
    const dashed = spec.aim === "idle";
    const dotted = spec.aim === "unknown";
    // The line's own direction carries the head: into-bus draws bottom→top
    // (the head lands on the bus), to-node top→bottom (the head lands on the
    // node), both draws one head at each end (markerStart auto-reverses).
    const intoBus = spec.aim === "into-bus" || spec.aim === "both";
    return (
      <line
        x1={cx}
        y1={intoBus ? stubBottom : stubTop}
        x2={cx}
        y2={intoBus ? stubTop : stubBottom}
        stroke={color}
        strokeWidth={dashed || dotted ? 2 : Math.max(2, spec.widthPx)}
        strokeLinecap="round"
        strokeDasharray={dashed ? "3 6" : dotted ? "1 7" : undefined}
        markerStart={spec.aim === "both" ? `url(#${markerId})` : undefined}
        markerEnd={
          spec.aim === "to-node" || spec.aim === "into-bus" || spec.aim === "both"
            ? `url(#${markerId})`
            : undefined
        }
      />
    );
  };
  return (
    <svg className="flow-bus" viewBox="0 0 240 100" aria-hidden="true" focusable="false">
      <defs>
        {/* orient="auto-start-reverse" flips the head for markerStart, so one
            def serves both aims. markerUnits="userSpaceOnUse" keeps the head a
            constant size while the stroke width carries the magnitude — a
            thicker flow is a thicker line, never a cartoon-scale head. One
            neutral ink for every head: the stub's color names the node
            family, the head names the direction. */}
        <marker
          id={markerId}
          viewBox="0 0 12 12"
          refX="10.5"
          refY="6"
          markerWidth="12"
          markerHeight="12"
          markerUnits="userSpaceOnUse"
          orient="auto-start-reverse"
        >
          <path d="M 1 1.5 L 11 6 L 1 10.5 z" fill="var(--ink)" />
        </marker>
      </defs>
      <line x1="16" y1={railY} x2="224" y2={railY} className="flow-bus-rail" />
      {stub(48, grid, "var(--active)")}
      {stub(120, battery, "var(--armed)")}
      {stub(192, house, "var(--ink-soft)")}
    </svg>
  );
}

/** One node card: the node's name, its worded figure, and any extra line. */
function FlowNode({
  name,
  figure,
  extra = null,
  tone,
}: {
  name: string;
  figure: string;
  extra?: string | null;
  tone: "grid" | "battery" | "home";
}): ReactNode {
  return (
    <div className={`flow-node flow-node--${tone}`} data-tone={tone}>
      <span className="flow-node-name">{name}</span>
      <span className="flow-node-figure">{figure}</span>
      {extra !== null && extra !== "" ? <span className="flow-node-extra">{extra}</span> : null}
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
  const connectionText =
    connection === "live"
      ? "Live — this picture refreshes every few seconds."
      : connection === "lost"
        ? "Connection lost — showing the last known picture; reconnecting automatically."
        : "Connecting to live updates…";

  // The fleet column's stubs: both-sided when the phases pull apart — the
  // worded figures below the bus carry the exact split either way.
  const fleetGridStub: Stub =
    fleet.gridReporting === 0
      ? { aim: "unknown", widthPx: 0 }
      : (fleet.importW ?? 0) > 0 && (fleet.exportW ?? 0) > 0
        ? { aim: "both", widthPx: arrowWidthPx(Math.max(fleet.importW ?? 0, fleet.exportW ?? 0), scale) }
        : (fleet.importW ?? 0) > 0
          ? { aim: "into-bus", widthPx: arrowWidthPx(fleet.importW, scale) }
          : (fleet.exportW ?? 0) > 0
            ? { aim: "to-node", widthPx: arrowWidthPx(fleet.exportW, scale) }
            : { aim: "idle", widthPx: 0 };
  const fleetBatteryStub: Stub =
    fleet.batteryReporting === 0
      ? { aim: "unknown", widthPx: 0 }
      : (fleet.chargingW ?? 0) > 0 && (fleet.dischargingW ?? 0) > 0
        ? { aim: "both", widthPx: arrowWidthPx(Math.max(fleet.chargingW ?? 0, fleet.dischargingW ?? 0), scale) }
        : (fleet.chargingW ?? 0) > 0
          ? { aim: "to-node", widthPx: arrowWidthPx(fleet.chargingW, scale) }
          : (fleet.dischargingW ?? 0) > 0
            ? { aim: "into-bus", widthPx: arrowWidthPx(fleet.dischargingW, scale) }
            : { aim: "idle", widthPx: 0 };
  const fleetHouseStub: Stub =
    fleet.houseW === null
      ? { aim: "unknown", widthPx: 0 }
      : fleet.houseW > 0
        ? { aim: "to-node", widthPx: arrowWidthPx(fleet.houseW, scale) }
        : { aim: "idle", widthPx: 0 };

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
          {units.map((unit) => (
            <PhaseColumn key={unit.unitId} unit={unit} scale={scale} />
          ))}

          {/* THE FLEET COLUMN — the same three nodes, summed across phases. */}
          <section className="flow-phase flow-phase--fleet" aria-label="Whole site">
            <h3 className="flow-phase-name">Whole site</h3>
            <FlowBus grid={fleetGridStub} battery={fleetBatteryStub} house={fleetHouseStub} />
            <div className="flow-nodes">
              <FlowNode name="Grid" figure={fleetGridText(fleet)} tone="grid" />
              <FlowNode name="Battery" figure={fleetBatteryText(fleet)} tone="battery" />
              <FlowNode name="Home" figure={fleetHouseText(fleet)} tone="home" />
            </div>
          </section>
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
function PhaseColumn({ unit, scale }: { unit: FlowUnit; scale: number }): ReactNode {
  const gridDirection = gridFlowDirection(unit.gridPowerW);
  const batteryDirection = batteryFlowDirection(unit.batteryWatts);
  const houseAim: StubAim =
    unit.loadPowerW === null ? "unknown" : unit.loadPowerW > 0 ? "to-node" : "idle";
  const soc = socText(unit.socPct);
  const note = lifecycleNote(unit.lifecycle);
  const gridText = gridFlowText(unit.gridPowerW);
  const batteryText = batteryFlowText(unit.batteryWatts);
  const houseText = houseFlowText(unit.loadPowerW);
  return (
    <section
      className="flow-phase"
      aria-label={`Phase ${unit.unitId} — grid: ${gridText}; battery: ${batteryText}${
        soc === "" ? "" : ` (${soc})`
      }; house: ${houseText}`}
    >
      <h3 className="flow-phase-name">{unit.unitId}</h3>
      {note !== null ? <p className="flow-phase-note">{note}</p> : null}
      <FlowBus
        grid={stubFor(gridDirection, unit.gridPowerW, scale)}
        battery={
          batteryDirection === "charge"
            ? { aim: "to-node", widthPx: arrowWidthPx(unit.batteryWatts, scale) }
            : batteryDirection === "discharge"
              ? { aim: "into-bus", widthPx: arrowWidthPx(unit.batteryWatts, scale) }
              : batteryDirection === "idle"
                ? { aim: "idle", widthPx: 0 }
                : { aim: "unknown", widthPx: 0 }
        }
        house={{ aim: houseAim, widthPx: arrowWidthPx(unit.loadPowerW, scale) }}
      />
      <div className="flow-nodes">
        <FlowNode name="Grid" figure={gridText} tone="grid" />
        <FlowNode name="Battery" figure={batteryText} extra={soc} tone="battery" />
        <FlowNode name="Home" figure={houseText} tone="home" />
      </div>
    </section>
  );
}
