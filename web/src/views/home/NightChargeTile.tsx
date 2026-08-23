/**
 * The "Night charging" Home tile — the off-peak night strategy's whole story
 * in one glance (DESIGN_NIGHT_CHARGE.md §7 W2), placed right beside the solar
 * tile (its daytime complement: the two advisers never share a minute).
 *
 * Feature-detected exactly like the solar tile: it renders ONLY when the
 * snapshot carries a `night_charge_state` projection. An absent projection
 * (today's backend; a deployment with no `night_charging` config block)
 * renders NOTHING — no heading, no rows, no toggle, no dialog.
 *
 * Honesty rules pinned here (the design's own wording):
 *
 * - ACTIVE phases name their facts in plain words: pacing names the window's
 *   end and every battery's own target ("Charging toward full by 06:00: mid
 *   1,900 W · lhs 1,200 W · rhs full, sitting out"); standing by names the
 *   measured-demand stand-down with its honest trade ("the batteries stand
 *   down at zero watts and the pods answer the house on their own until
 *   demand falls back"); holding names the fail-closed guarantee ("batteries
 *   neither drain nor cycle while the grid meets the spike" — the missing-
 *   evidence fallback at `hold_rate_w`, never a response to measured demand);
 *   complete names the moment ("Batteries full — window complete at 04:12");
 *   skipped_full states the window had nothing to charge.
 * - INACTIVE states are honest one-sentence answers rendered from the FIRST
 *   reason code in the projection's pinned vocabulary (§5; the codes are the
 *   wire, the sentences are the console's). An unknown code is named verbatim,
 *   never silently dropped. `units_disarmed` renders as the arm instruction
 *   it is (the runner can never arm itself; every restart disarms again).
 * - The demand reading is the LOAD-word figure with its evidence word always
 *   named — a non-good rollup holds fail-closed and is loudly visible, never
 *   a silent never-charges. A null figure says "not available", never 0.
 * - The window countdown ticks from the projection's own instants, falling
 *   back to the snapshot-derived seconds; outside the window the NEXT window's
 *   countdown is the answer.
 * - The energy line composes with the scorecard's `energy_today` where the
 *   design pins it (§6): the nightly charge is real grid import, the scorecard
 *   measures it from day one, and the money line appears only when the
 *   operator supplies the tariff keys — which no wire shape carries today, so
 *   its absence is stated, never improvised (the Today card's own gap).
 * - The toggle is the contract's guarded confirmation (§3.4/B4): BOTH actions
 *   type NIGHT; the FIRST enable ever additionally captures the one-time
 *   night-partition acknowledgement (the §8 item 2 assertion verbatim, a
 *   required checkbox, sent as "night_posture": "PARTITION_ACKNOWLEDGED" —
 *   either surface's capture counts, never re-prompted). A
 *   `night_acknowledgement_required` refusal routes the dialog back into the
 *   acknowledgement step (the 409 IS the routing). Refusals render their
 *   envelope messages inline — the refused enable names the units or the stop
 *   holding it.
 * - The state display distinguishes the origins: "On (config)" /
 *   "On — until restart" — a runtime enable is operational only until the
 *   controller restarts, and the console says so (the excess doctrine).
 */
import { useEffect, useId, useRef, useState } from "react";
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { kwhText, type EnergyToday } from "../../app/energy";
import {
  nightDemandText,
  nightStatusText,
  nightToggleStateText,
  nightUnitRowText,
  nightWindowLineText,
  toNightChargeState,
  type NightChargeState,
} from "../../app/nightCharge";

export interface NightChargeTileProps {
  /** The snapshot's night projection; null hides the whole tile. */
  night: NightChargeState | null;
  /**
   * The snapshot's `energy_today` block (itself feature-detected): the energy
   * line renders only when BOTH blocks are present — the "charged overnight"
   * story composes the scorecard's own measured import, never an invented
   * night-window figure.
   */
  today: EnergyToday | null;
  /** The session's client: the guarded toggle mutation goes through it. */
  client: ApiClient;
  /**
   * Optimistic adoption: a toggle 200's `night_charge_state` becomes the
   * tile's state NOW; the next snapshot or state_changed frame confirms it.
   */
  onAdopt: (state: NightChargeState) => void;
  /** Home's ticking clock, for the window countdowns (never an animation). */
  nowMs: number;
}

/** The typed confirmation the contract requires for BOTH actions (B4). */
export const NIGHT_CONFIRMATION = "NIGHT";

/** The acknowledgement value the contract pins for the first enable (§3.2). */
const PARTITION_ACKNOWLEDGED = "PARTITION_ACKNOWLEDGED";

/**
 * The one-time night-partition acknowledgement's assertion, DESIGN_NIGHT_CHARGE
 * §8 item 2 VERBATIM (the quoted sentence inside the operator decision): the
 * checkbox label carries it word for word — captured once as a durable fact,
 * never asked again.
 */
export const PARTITION_ASSERTION =
  "the external writer applications stand down for the granted window; the controller owns it";

/**
 * The energy line, composed where the design pins it (§6): the nightly charge
 * is real grid import at the off-peak rate; the scorecard measures the kWh
 * from day one; the MONEY line appears only when the operator supplies the
 * tariff keys — absent on every pinned wire shape, so the gap is named, never
 * filled with an invented rate. Null when the scorecard is not composed (the
 * night story stands on its own figures).
 */
export function nightEnergyLine(today: EnergyToday): string {
  return `Charging at night is real grid import — the energy scorecard measures it (bought from the grid today so far: ${kwhText(
    today.fleet.gridImportKwh,
  )}). The cost appears once the tariff keys are commissioned.`;
}

// --- the guarded toggle's refusal envelopes (B4), rendered inline -------------

interface ToggleRefusal {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
}

function asRefusal(error: unknown): ToggleRefusal | null {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message, details: error.details };
  }
  return null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry !== "")
    : [];
}

/**
 * The plain sentence for a refused toggle, from the envelope's own details.
 * The envelope's code and message always render verbatim beside it.
 */
function refusalSentence(refusal: ToggleRefusal): string {
  if (refusal.code === "night_charging_not_commissioned") {
    return "Night charging is not commissioned in this deployment's config — there is nothing to turn on. Commissioning is a config change on the controller: add the night_charging block and restart.";
  }
  if (refusal.code === "night_acknowledgement_required") {
    return "The one-time night-partition acknowledgement is required before the first enable — confirm it below.";
  }
  if (refusal.code === "night_enable_refused") {
    const details = refusal.details ?? {};
    const unitIds = stringList(details.unit_ids);
    const stopIds = stringList(details.stop_ids);
    if (unitIds.length > 0) {
      return `Cannot enable while a request is still active on ${unitIds.join(", ")} — finish or cancel the request on ${unitIds.join(", ")} first.`;
    }
    if (stopIds.length > 0) {
      return `Cannot enable while an emergency stop holds the fleet (${stopIds.join(", ")}) — acknowledge the stop first.`;
    }
    return "Cannot enable night charging right now — the fleet is not in a state where it can start.";
  }
  return "";
}

// --- the typed-confirmation dialog ---------------------------------------------

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

interface NightToggleDialogProps {
  action: "enable" | "disable";
  /** The commissioned window's walls, for the dialog's own plain sentence. */
  windowText: string;
  /**
   * The first enable ever on this site: the partition acknowledgement block
   * renders (and a server-side night_acknowledgement_required refusal routes
   * the dialog back into it — the refusal IS the routing).
   */
  needsAcknowledgement: boolean;
  refusal: ToggleRefusal | null;
  pending: boolean;
  onCancel: () => void;
  /** `acknowledgePartition` is true only when the operator checked the box. */
  onConfirm: (acknowledgePartition: boolean) => void;
}

function NightToggleDialog({
  action,
  windowText,
  needsAcknowledgement,
  refusal,
  pending,
  onCancel,
  onConfirm,
}: NightToggleDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [typed, setTyped] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const titleId = "night-toggle-title";
  const inputId = "night-toggle-confirmation";
  const ackId = "night-toggle-acknowledgement";
  // A server refusal of class night_acknowledgement_required routes the dialog
  // into the acknowledgement step even when the console's state was stale
  // (another console captured it via the schedules surface, or the local flag
  // lagged a restart) — the 409 IS the routing.
  const showAcknowledgement =
    needsAcknowledgement || refusal?.code === "night_acknowledgement_required";
  const typedOk = typed.trim() === NIGHT_CONFIRMATION;
  const ready = typedOk && (!showAcknowledgement || acknowledged) && !pending;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const input = dialog.querySelector<HTMLElement>("#" + inputId);
    (input ?? dialog).focus();
  }, []);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const focusables = focusableIn(dialog);
    if (focusables.length === 0) {
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (first === undefined || last === undefined) {
      return;
    }
    const active = document.activeElement;
    const inside = active instanceof Node && dialog.contains(active);
    if (event.shiftKey) {
      if (active === first || !inside) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !inside) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="home-night-dialog"
      onKeyDown={handleKeyDown}
    >
      <h2 id={titleId}>
        {action === "enable" ? "Turn on night charging" : "Turn off night charging"}
      </h2>
      <p>
        {action === "enable"
          ? `Enabling charges the batteries to full inside the off-peak window (${windowText}), standing them down entirely whenever house demand is high — each pod answers the house on its own until demand falls back. Night charging stops by itself at the window's end.`
          : "Disabling stops night charging now; a controller restart re-reads the config's own setting either way."}
      </p>
      {showAcknowledgement && (
        <div className="home-night-ack">
          <p>
            One-time night-partition confirmation — stand the external writer
            applications down for the night window and grant it to the controller: widen{" "}
            <code>allowed_windows_local</code> in the controller config (a config revision
            and restart), then acknowledge once. Captured as a durable fact, never asked
            again.
          </p>
          <p className="home-night-ack-check">
            <input
              id={ackId}
              type="checkbox"
              checked={acknowledged}
              disabled={pending}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />{" "}
            <label htmlFor={ackId}>{PARTITION_ASSERTION}</label>
          </p>
        </div>
      )}
      <p className="home-night-typed">
        <label htmlFor={inputId}>
          Type {NIGHT_CONFIRMATION} to confirm {action === "enable" ? "turning it on" : "turning it off"}
        </label>
        <input
          id={inputId}
          type="text"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          disabled={pending}
          onChange={(event) => setTyped(event.target.value)}
        />
      </p>
      {refusal !== null && (
        <div role="alert" className="home-night-refusal">
          {refusalSentence(refusal) !== "" && <p>{refusalSentence(refusal)}</p>}
          <p>
            <code>{refusal.code}</code> — <span>{refusal.message}</span>
          </p>
        </div>
      )}
      <div className="home-night-dialog-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button
          type="button"
          className="home-night-confirm"
          disabled={!ready}
          onClick={() => onConfirm(showAcknowledgement && acknowledged)}
        >
          {action === "enable" ? "Turn on" : "Turn off"}
        </button>
      </div>
    </div>
  );
}

// --- the tile -------------------------------------------------------------------

export function NightChargeTile({
  night,
  today,
  client,
  onAdopt,
  nowMs,
}: NightChargeTileProps): JSX.Element | null {
  const headingId = useId();
  const [dialog, setDialog] = useState<null | { action: "enable" | "disable" }>(null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState<ToggleRefusal | null>(null);
  const switchRef = useRef<HTMLButtonElement | null>(null);
  const wasOpen = useRef(false);

  // Closing the dialog (either way) hands focus back to the switch.
  useEffect(() => {
    if (wasOpen.current && dialog === null) {
      switchRef.current?.focus();
    }
    wasOpen.current = dialog !== null;
  }, [dialog]);

  const closeDialog = (): void => {
    setRefusal(null);
    setDialog(null);
  };

  const confirmToggle = (acknowledgePartition: boolean): void => {
    if (dialog === null || pending) {
      return;
    }
    setPending(true);
    setRefusal(null);
    client
      .postNightCharging(
        dialog.action,
        // The acknowledgement rides the call exactly when the operator
        // confirmed it (the first enable ever); otherwise the body carries
        // only the action and the typed confirmation.
        acknowledgePartition ? { nightPosture: PARTITION_ACKNOWLEDGED } : {},
      )
      .then(
        (response) => {
          setPending(false);
          // Optimistic adoption (the releaseStopLatch pattern): the 200's own
          // post-toggle projection becomes the tile's state now; the next
          // snapshot or night_charge.state_changed frame confirms it.
          const adopted = toNightChargeState(response.night_charge_state);
          if (adopted !== null) {
            onAdopt(adopted);
          }
          setRefusal(null);
          setDialog(null);
        },
        (error: unknown) => {
          setPending(false);
          const envelope = asRefusal(error);
          if (envelope === null) {
            setRefusal({
              code: "unexpected_error",
              message: "The night-charging toggle did not complete.",
              details: null,
            });
            return;
          }
          setRefusal(envelope);
          // Every refusal keeps the dialog open: the envelope renders inline,
          // and night_acknowledgement_required routes into the acknowledgement
          // step inside the same dialog.
        },
      );
  };

  if (night === null) {
    // Feature detection: no night_charge_state anywhere means the feature is
    // not composed on this deployment — nothing renders at all.
    return null;
  }
  const windowWalls = `${night.window.startLocal}–${night.window.endLocal}`;
  const windowLine = nightWindowLineText(night, nowMs);
  return (
    <section className="home-card home-card--night" aria-labelledby={headingId}>
      <h2 id={headingId}>Night charging</h2>
      <p
        className="home-night-status"
        data-active={night.active ? "true" : "false"}
        data-phase={night.phase}
      >
        {nightStatusText(night)}
      </p>
      {windowLine !== null && <p className="home-night-window">{windowLine}.</p>}
      <p className="home-night-demand" data-evidence={night.demandEvidence}>
        {nightDemandText(night)}
      </p>
      {night.units.length > 0 && (
        <ul className="home-night-units" aria-label="Per-battery night charge plan">
          {night.units.map((unit) => (
            <li key={unit.unitId} aria-label={`${unit.unitId} night charge plan`}>
              {nightUnitRowText(unit)}
            </li>
          ))}
        </ul>
      )}
      {today !== null && (
        <p className="home-night-energy">{nightEnergyLine(today)}</p>
      )}
      <div className="home-night-toggle">
        <p className="home-night-toggle-state">
          Night charging: <strong>{nightToggleStateText(night)}</strong>
          {night.enabledOrigin === "runtime"
            ? " — the config's own setting takes over at restart"
            : ""}
        </p>
        <button
          ref={switchRef}
          type="button"
          role="switch"
          aria-checked={night.enabled ? "true" : "false"}
          className={night.enabled ? "home-night-switch is-on" : "home-night-switch"}
          onClick={() => {
            setRefusal(null);
            setDialog({ action: night.enabled ? "disable" : "enable" });
          }}
        >
          Night charging
        </button>
      </div>
      {dialog !== null && (
        <NightToggleDialog
          action={dialog.action}
          windowText={windowWalls}
          // The acknowledgement step is the FIRST-enable gate: only an enable
          // on a site that has never captured the fact (either surface's
          // capture counts — it is one site fact).
          needsAcknowledgement={dialog.action === "enable" && !night.acknowledgedPartition}
          refusal={refusal}
          pending={pending}
          onCancel={closeDialog}
          onConfirm={confirmToggle}
        />
      )}
    </section>
  );
}
