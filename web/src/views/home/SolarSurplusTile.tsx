/**
 * The "Solar surplus" Home tile — the excess-solar activation feature's whole
 * story in one glance (DESIGN_EXCESS_ACTIVATION.md §5 W-A, W-B).
 *
 * Feature-detected, exactly like the snapshot's `intent` block: the tile
 * renders ONLY when the snapshot carries an `adviser_state` projection. An
 * absent projection (today's backend; a deployment with no `excess_charging`
 * config block) renders NOTHING — no heading, no rows, no toggle, no dialog.
 *
 * Honesty rules pinned here:
 *
 * - ACTIVE names its three facts: the target battery, the commanded watts,
 *   and the fleet's export reading ("Charging mid at 1,400 W from 1,800 W
 *   export"). A null `fleet_export_w` (any unit's grid evidence failed) says
 *   "not available" — one unreadable phase is never treated as zero export.
 * - INACTIVE states are honest one-sentence answers rendered from the FIRST
 *   reason code in the projection's pinned vocabulary (§1's table; the codes
 *   are the wire, the sentences are the console's). An unknown code is named
 *   verbatim, never silently dropped.
 * - The cap line renders the composed `charge_cap_w` as the bare fact it is
 *   ("cap 500 W" for the trial — no invented label).
 * - Per-phase figures come from the snapshot's per-unit telemetry readthrough
 *   (`grid_power_w` / `load_power_w`): one row per unit, negative grid =
 *   import, positive = export, absent = "not available" — the sentence that
 *   makes the feature legible ("that export is what charged rhs").
 * - The toggle is the contract's guarded confirmation (§3): BOTH actions type
 *   EXCESS; the FIRST enable ever additionally captures the net-billing
 *   acknowledgement (the exact assertion, a required checkbox, sent as
 *   "economics": "NET_BILLED" — once, never re-prompted). The 200's
 *   `adviser_state` is adopted optimistically; the next snapshot or
 *   `excess_adviser.state_changed` frame confirms (the releaseStopLatch
 *   pattern). Refusals render their envelope messages inline — the refused
 *   enable names the units or the stop holding it.
 * - The state display distinguishes the origins: "On (config)" /
 *   "On — until restart" — a runtime enable is operational only until the
 *   controller restarts, and the console says so (P1's honest transience).
 */
import { useEffect, useId, useRef, useState } from "react";
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { gridLoadText, toAdviserState, type AdviserState } from "../../app/fleet";
import { formatWatts } from "../../lib/format";

/** One unit's readthrough figures, already narrowed by the view's own parse. */
export interface SolarSurplusUnitReadings {
  unitId: string;
  gridPowerW: number | null;
  loadPowerW: number | null;
}

export interface SolarSurplusTileProps {
  /** The snapshot's adviser projection; null hides the whole tile. */
  adviser: AdviserState | null;
  /** Every fleet unit's grid/load readthrough (one row each, any order). */
  units: readonly SolarSurplusUnitReadings[];
  /** The session's client: the guarded toggle mutation goes through it. */
  client: ApiClient;
  /**
   * Optimistic adoption: a toggle 200's `adviser_state` becomes the tile's
   * state NOW; the next snapshot or state_changed frame confirms it.
   */
  onAdopt: (state: AdviserState) => void;
}

/**
 * The typed confirmation the contract requires for BOTH actions (§3).  The
 * operator's own phrasing of the feature is "type 'excess'" — the console
 * gate therefore matches CASE-INSENSITIVELY (the label still teaches the
 * contract's word); the client always sends the literal "EXCESS" the wire
 * pins.
 */
export const EXCESS_CONFIRMATION = "EXCESS";

/** The acknowledgement value the contract pins for the first enable (P3). */
const NET_BILLED = "NET_BILLED";

/**
 * The plain-language sentence for one adviser reason code — the design
 * contract §5 W-A's own wording, keyed on the pinned §1 vocabulary. The
 * builder takes the state so the sentence can carry its own figures.
 */
function reasonSentence(code: string, state: AdviserState): string {
  switch (code) {
    case "disabled_by_config":
      return "Charging from solar surplus is off (config).";
    case "disabled_by_runtime":
      // The projection carries no boot-value flag, so the console states the
      // fact it CAN see: this off came from the runtime toggle, and a restart
      // re-reads the commissioned config.
      return "Charging from solar surplus is off until the controller restarts — the config's own setting takes over again at boot.";
    case "economics_acknowledgement_required":
      return "Waiting on the one-time net-billing confirmation before solar-surplus charging can start.";
    case "export_evidence_missing":
    case "export_evidence_bad":
    case "export_evidence_stale":
      // fleet_export_w is null by contract under every one of these codes.
      return "Export reading unavailable on the fleet — standing down (fail-closed). Export figure: not available.";
    case "no_export_headroom":
      return `Exporting ${
        state.fleetExportW === null ? "an unavailable figure" : formatWatts(state.fleetExportW)
      } — below the headroom margin, nothing to charge from.`;
    case "no_acceleration_over_autonomy":
      return "Surplus too small — taking over would charge slower than the pod does by itself.";
    case "below_exit_hysteresis":
      return "Surplus is falling — handing back to the pod's own charging.";
    case "no_eligible_target":
      return "Solar surplus available, but no battery needs charging (full, inhibited, or not armed).";
    case "yielding_to_higher_priority":
      return `Standing down — a manual request has ${state.targetUnitId ?? "the target battery"}.`;
    case "export_headroom_available":
      // The commanding code; an inactive projection carrying it still speaks
      // the participation truth.
      return "Charging from solar surplus.";
    default:
      // The vocabulary is pinned and one list (ADVISER_REASON_CODES); an
      // unknown code is rendered honestly instead of guessed at.
      return `Standing down from solar surplus (${code}).`;
  }
}

/**
 * The tile's one-line story: active names its figures; inactive names its
 * first reason code's sentence (an enabled adviser always says why it did
 * what it did — an empty list is never the wire's answer).
 */
export function adviserStatusText(state: AdviserState): string {
  if (state.active) {
    const target = state.targetUnitId ?? "the neediest battery";
    const exportFigure =
      state.fleetExportW === null
        ? "an export figure that is not available"
        : `${formatWatts(state.fleetExportW)} export`;
    return `Charging ${target} at ${formatWatts(state.commandedChargeW)} from ${exportFigure}.`;
  }
  const first = state.reasonCodes[0];
  return first === undefined
    ? "Charging from solar surplus is standing by."
    : reasonSentence(first, state);
}

/**
 * The secondary figure line: the composed cap, rendered as the bare fact (the
 * trial shows "cap 500 W" with no invented label). Null when the adviser is
 * not commanding — the cap belongs to the active sentence.
 */
export function adviserCapText(state: AdviserState): string | null {
  if (!state.active) {
    return null;
  }
  return `cap ${formatWatts(state.chargeCapW)}`;
}

/**
 * The toggle's current-state phrase from `enabled` + `enabled_origin` (§5 W-B,
 * the four states verbatim): the runtime origin is the honest "until restart"
 * marker — a runtime enable or disable is operational only until the
 * controller restarts and the config takes over again (P1).
 */
export function excessToggleStateText(state: AdviserState): string {
  if (state.enabled) {
    return state.enabledOrigin === "config" ? "On (config)" : "On — until restart";
  }
  return state.enabledOrigin === "config" ? "Off (config)" : "Off — until restart";
}

// --- the guarded toggle's refusal envelopes (§3), rendered inline ----------

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
  if (refusal.code === "excess_charging_not_commissioned") {
    return "Excess charging is not commissioned in this deployment's config — there is nothing to turn on.";
  }
  if (refusal.code === "economics_acknowledgement_required") {
    return "The one-time net-billing confirmation is required before the first enable — confirm it below.";
  }
  if (refusal.code === "excess_enable_refused") {
    const details = refusal.details ?? {};
    const unitIds = stringList(details.unit_ids);
    const stopIds = stringList(details.stop_ids);
    if (unitIds.length > 0) {
      return `Cannot enable while a request is still active on ${unitIds.join(", ")} — finish or cancel the request on ${unitIds.join(", ")} first.`;
    }
    if (stopIds.length > 0) {
      return `Cannot enable while an emergency stop holds the fleet (${stopIds.join(", ")}) — acknowledge the stop first.`;
    }
    return "Cannot enable excess charging right now — the fleet is not in a state where it can start.";
  }
  return "";
}

// --- the typed-confirmation dialog ------------------------------------------

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

interface ExcessToggleDialogProps {
  action: "enable" | "disable";
  /**
   * The first enable ever on this site: the net-billing acknowledgement
   * block renders (and a server-side economics_acknowledgement_required
   * refusal routes the dialog back into it — the refusal IS the routing).
   */
  needsAcknowledgement: boolean;
  refusal: ToggleRefusal | null;
  pending: boolean;
  onCancel: () => void;
  /** `acknowledgeEconomics` is true only when the operator checked the box. */
  onConfirm: (acknowledgeEconomics: boolean) => void;
}

function ExcessToggleDialog({
  action,
  needsAcknowledgement,
  refusal,
  pending,
  onCancel,
  onConfirm,
}: ExcessToggleDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [typed, setTyped] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const titleId = "excess-toggle-title";
  const inputId = "excess-toggle-confirmation";
  const ackId = "excess-toggle-acknowledgement";
  // A server refusal of class economics_acknowledgement_required routes the
  // dialog into the acknowledgement step even when the console's state was
  // stale (another console captured it, or the local flag lagged a restart).
  const showAcknowledgement =
    needsAcknowledgement || refusal?.code === "economics_acknowledgement_required";
  // Case-insensitive: the operator's spec says "simply type 'excess'" and
  // the lowercase spelling must confirm exactly like the contract's own word.
  const typedOk = typed.trim().toUpperCase() === EXCESS_CONFIRMATION;
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
      className="home-solar-dialog"
      onKeyDown={handleKeyDown}
    >
      <h2 id={titleId}>
        {action === "enable"
          ? "Turn on charging from solar surplus"
          : "Turn off charging from solar surplus"}
      </h2>
      <p>
        {action === "enable"
          ? "Enabling lets the system charge the neediest battery from the site's solar surplus, inside the commissioned cap. It stands down by itself whenever another request or a safety stop needs a battery."
          : "Disabling stops solar-surplus charging now; a controller restart re-reads the config's own setting either way."}
      </p>
      {showAcknowledgement && (
        <div className="home-solar-ack">
          <p>
            One-time confirmation — the net-billing assumption: this site&apos;s billing nets
            across phases, so import on one phase offsets export credit on another.
            Confirming captures this once for excess-solar charging and is never asked
            again.
          </p>
          <p className="home-solar-ack-check">
            <input
              id={ackId}
              type="checkbox"
              checked={acknowledged}
              disabled={pending}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />{" "}
            <label htmlFor={ackId}>This site&apos;s billing nets across phases</label>
          </p>
        </div>
      )}
      <p className="home-solar-typed">
        <label htmlFor={inputId}>
          Type {EXCESS_CONFIRMATION} to confirm {action === "enable" ? "turning it on" : "turning it off"}
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
        <div role="alert" className="home-solar-refusal">
          {refusalSentence(refusal) !== "" && <p>{refusalSentence(refusal)}</p>}
          <p>
            <code>{refusal.code}</code> — <span>{refusal.message}</span>
          </p>
        </div>
      )}
      <div className="home-solar-dialog-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button
          type="button"
          className="home-solar-confirm"
          disabled={!ready}
          onClick={() => onConfirm(showAcknowledgement && acknowledged)}
        >
          {action === "enable" ? "Turn on" : "Turn off"}
        </button>
      </div>
    </div>
  );
}

// --- the tile ----------------------------------------------------------------

export function SolarSurplusTile({
  adviser,
  units,
  client,
  onAdopt,
}: SolarSurplusTileProps): JSX.Element | null {
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

  const confirmToggle = (acknowledgeEconomics: boolean): void => {
    if (dialog === null || pending) {
      return;
    }
    setPending(true);
    setRefusal(null);
    client
      .postExcessCharging(
        dialog.action,
        // The acknowledgement rides the call exactly when the operator
        // confirmed it (the first enable ever); otherwise the body carries
        // only the action and the typed confirmation.
        acknowledgeEconomics ? { economics: NET_BILLED } : {},
      )
      .then(
        (response) => {
          setPending(false);
          // Optimistic adoption (the releaseStopLatch pattern): the 200's own
          // post-toggle projection becomes the tile's state now; the next
          // snapshot or state_changed frame confirms it.
          const adopted = toAdviserState(response.adviser_state);
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
              message: "The excess-charging toggle did not complete.",
              details: null,
            });
            return;
          }
          setRefusal(envelope);
          // Every refusal keeps the dialog open: the envelope renders inline,
          // and economics_acknowledgement_required routes into the
          // acknowledgement step inside the same dialog.
        },
      );
  };

  if (adviser === null) {
    // Feature detection: no adviser_state anywhere means the feature is not
    // composed on this deployment — nothing renders at all.
    return null;
  }
  return (
    <section className="home-card home-card--solar" aria-labelledby={headingId}>
      <h2 id={headingId}>Solar surplus</h2>
      <p className="home-solar-status" data-active={adviser.active ? "true" : "false"}>
        {adviserStatusText(adviser)}
      </p>
      {adviserCapText(adviser) !== null && (
        <p className="home-solar-cap">{adviserCapText(adviser)}</p>
      )}
      {units.length > 0 && (
        <ul className="home-solar-units" aria-label="Per-unit grid and load">
          {units.map((unit) => (
            <li key={unit.unitId} aria-label={`${unit.unitId} grid and load`}>
              {unit.unitId}: {gridLoadText(unit.gridPowerW, unit.loadPowerW)}
            </li>
          ))}
        </ul>
      )}
      <div className="home-solar-toggle">
        <p className="home-solar-toggle-state">
          Excess charging: <strong>{excessToggleStateText(adviser)}</strong>
          {adviser.enabledOrigin === "runtime"
            ? " — the config's own setting takes over at restart"
            : ""}
        </p>
        <button
          ref={switchRef}
          type="button"
          role="switch"
          aria-checked={adviser.enabled ? "true" : "false"}
          className={adviser.enabled ? "home-solar-switch is-on" : "home-solar-switch"}
          onClick={() => {
            setRefusal(null);
            setDialog({ action: adviser.enabled ? "disable" : "enable" });
          }}
        >
          Charging from solar surplus
        </button>
      </div>
      {dialog !== null && (
        <ExcessToggleDialog
          action={dialog.action}
          // The acknowledgement step is the FIRST-enable gate: only an enable
          // on a site that has never captured the fact (P3).
          needsAcknowledgement={dialog.action === "enable" && !adviser.acknowledgedEconomics}
          refusal={refusal}
          pending={pending}
          onCancel={closeDialog}
          onConfirm={confirmToggle}
        />
      )}
    </section>
  );
}
