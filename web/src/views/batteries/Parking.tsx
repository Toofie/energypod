/**
 * The pod-parking surfaces for the Batteries view (DESIGN_POD_PARKING.md §8,
 * API_CONTRACTS.md "Pod parking"): the parked chip and not-isolation banner,
 * the guarded park dialog (fixed sentence, required reason, bounded lease
 * select, type-back the unit id), the resume dialog with the foreign-takeover
 * acknowledgement step, the post-resume checklist rendered inline, the
 * wedge-signature recovery advisory card, and the delivery-bias evidence
 * readout. Presentational only — the view owns the wire, the state, and the
 * refresh; every wire fact arrives narrowed through web/src/app/park.ts.
 *
 * Honesty pins:
 * - The fixed not-isolation sentence renders VERBATIM above the park
 *   dialog's confirm button and in the unit banner — never re-worded, never
 *   truncated, and no countdown ever implies time-bounded safety.
 * - The lease countdown promotes to the alert wording at expiry; expiry is
 *   the alarm, Resume is the operator act.
 * - The takeover acknowledgement is its own deliberate step exactly when the
 *   park wasn't ours (origin foreign/unrecorded) — and the 409 refusal IS the
 *   routing: a `park_foreign_word_acknowledgement_required` envelope grows
 *   the step even when the console's projection did not predict it.
 * - The advisory renders UNAVAILABLE — never a suggestion — when parking is
 *   not commissioned on the site.
 * - Delivery bias is EVIDENCE, never a warning: plain note styling, the
 *   evidence-only label, and the sentence that no control decision reads it.
 */
import {
  useEffect,
  useRef,
  useState,
  type JSX,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import {
  foreignRewriteText,
  isTakeoverRefusal,
  leaseChoices,
  leaseDurationText,
  leaseIsExpired,
  NOT_ISOLATION_SENTENCE,
  parkConfirmHintText,
  parkOriginText,
  parkRefusalText,
  parkedBannerFixedLine,
  parkedBannerLine,
  parkedChipTooltip,
  resumeConfirmHintText,
  takeoverRequired,
  writeUnverifiedText,
  type ParkStateView,
  type ResumeChecklistView,
} from "../../app/park";
import { formatPercent, formatWatts, wholeSeconds } from "../../lib/format";

// --- the shared refusal block (plain sentence + the envelope verbatim) ------

export interface ParkRefusalView {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
}

/**
 * One refusal's honest render: the plain sentence first, then the API's own
 * envelope verbatim below it (code + message) — the sentence explains, the
 * envelope is the record. An unknown code renders the envelope alone.
 */
export function ParkRefusalBlock({ refusal }: { refusal: ParkRefusalView }): JSX.Element {
  const plain = parkRefusalText(refusal);
  return (
    <div role="alert" className="dialog-error">
      {plain !== "" && <p>{plain}</p>}
      <p className="raw-code">
        {refusal.code} — {refusal.message}
      </p>
    </div>
  );
}

// --- the chip and the banner --------------------------------------------------

/**
 * The parked chip: the one-word state plus the honest tooltip — the mode word
 * and the pack voltage (telemetry's own figures), never a status metaphor.
 * The tooltip rides the native title and the aria-description so keyboard and
 * pointer operators read the same honesty.
 */
export function ParkedChip({
  park,
  modeWord,
  packVoltageV,
}: {
  park: ParkStateView;
  modeWord: number | null;
  packVoltageV: number | null;
}): JSX.Element {
  const tooltip = parkedChipTooltip(modeWord, packVoltageV, park);
  return (
    <span className="parked-chip" title={tooltip} aria-description={tooltip}>
      Parked
    </span>
  );
}

/**
 * The unit banner (card and detail): the pinned countdown line — promoting to
 * the alert wording at expiry — with the fixed not-isolation sentence always
 * beside it, and the two honest sub-state words (write_unverified,
 * foreign_rewrite) each in their own line when the wire carries them.
 */
export function ParkedBanner({
  park,
  nowMs,
}: {
  park: ParkStateView;
  nowMs: number;
}): JSX.Element {
  const expired = leaseIsExpired(park, nowMs);
  return (
    <div
      role={expired ? "alert" : "note"}
      className={expired ? "parked-banner parked-banner--expired" : "parked-banner"}
    >
      <p className="parked-banner-line">{parkedBannerLine(park, nowMs)}</p>
      <p className="parked-banner-fixed">{parkedBannerFixedLine()}</p>
      {park.writeUnverified === true && (
        <p className="parked-banner-note">{writeUnverifiedText()}</p>
      )}
      {park.foreignRewrite === true && (
        <p className="parked-banner-note">{foreignRewriteText()}</p>
      )}
      {park.reason !== "" && (
        <p className="parked-banner-meta">
          Parked {parkOriginText(park.origin)}
          {park.authorizer === "" ? "" : ` by ${park.authorizer}`}
          {park.reason === "" ? "" : ` — “${park.reason}”`}
        </p>
      )}
    </div>
  );
}

// --- the guarded dialogs (focus trapped, focus restored by the view) ---------

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function useDialogTrap(
  dialogRef: RefObject<HTMLDivElement | null>,
  onCancel: () => void,
): (event: ReactKeyboardEvent<HTMLDivElement>) => void {
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    (focusables[0] ?? dialog).focus();
  }, [dialogRef]);
  return (event) => {
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
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
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
}

export interface ParkDialogProps {
  unitId: string;
  /** The unit's park projection — the site's lease budget bounds the select. */
  park: ParkStateView | null;
  error: ParkRefusalView | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (reason: string, leaseS: number) => void;
}

/**
 * The park dialog: the fixed not-isolation sentence VERBATIM above the confirm
 * button, a required reason (1..500), the lease duration bounded by the
 * site's own budget, and the type-back guard — confirm stays disabled until
 * the operator types the unit id exactly. The lease line names the policy:
 * the countdown is never safety. The panel is a flex column (see
 * BatteriesView.css): the guarded fields scroll in `.dialog-body`, while the
 * disabled-reason hint, the fixed sentence, and the actions pin in
 * `.dialog-footer` — Cancel/confirm are visible whatever the content or the
 * viewport, and the grayed confirm always names what is missing.
 */
export function ParkDialog({
  unitId,
  park,
  error,
  pending,
  onCancel,
  onConfirm,
}: ParkDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const handleKeyDown = useDialogTrap(dialogRef, onCancel);
  const [reason, setReason] = useState("");
  const choices = leaseChoices(park);
  const [leaseS, setLeaseS] = useState<number | null>(choices.length > 0 ? choices[choices.length - 1]!.seconds : null);
  const [typed, setTyped] = useState("");
  const reasonValid = reason.trim().length >= 1 && reason.trim().length <= 500;
  const typedMatches = typed === unitId;
  const canConfirm = reasonValid && typedMatches && leaseS !== null && !pending;
  // The disabled-reason hint renders only while the confirm is genuinely
  // held by a missing input — never while a write is pending (that is the
  // wire's own state, not the operator's unfinished form).
  const hint = pending
    ? ""
    : parkConfirmHintText(unitId, {
        reasonValid,
        typedMatches,
        leaseChosen: leaseS !== null,
      });
  const cap = park === null ? null : park.remainingCapS > 0 ? Math.min(park.maxTotalS, park.remainingCapS) : park.maxTotalS;
  return (
    <div className="dialog-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="park-dialog-title"
        className="dialog park-dialog"
        onKeyDown={handleKeyDown}
      >
        <div className="dialog-body">
          <h2 id="park-dialog-title">Park {unitId}</h2>
          <p>
            Parking stands the battery down (the vendor&apos;s Standby mode word) while comms and
            telemetry stay alive.
            {cap !== null && cap >= 60
              ? ` Lease up to ${leaseDurationText(cap)} on this site.`
              : ""}
          </p>
          <div className="park-field">
            <label htmlFor={`park-reason-${unitId}`}>Reason (required)</label>
            <textarea
              id={`park-reason-${unitId}`}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={pending}
              rows={2}
            />
          </div>
          {choices.length > 0 ? (
            <div className="park-field">
              <label htmlFor={`park-lease-${unitId}`}>Lease duration</label>
              <select
                id={`park-lease-${unitId}`}
                value={leaseS === null ? "" : String(leaseS)}
                onChange={(event) => {
                  const next = Number(event.target.value);
                  setLeaseS(Number.isFinite(next) ? next : null);
                }}
                disabled={pending}
              >
                {choices.map((choice) => (
                  <option key={choice.seconds} value={String(choice.seconds)}>
                    {choice.label}
                  </option>
                ))}
              </select>
              <p className="park-field-note">
                The lease is the expiry alarm, not a safety bound — at its end the console asks
                for a Resume; nothing writes the battery on its own.
              </p>
            </div>
          ) : (
            <p className="park-field-note">No lease budget is available for this battery right now.</p>
          )}
          <div className="park-field">
            <label htmlFor={`park-typed-${unitId}`}>Type {unitId} to enable park</label>
            <input
              id={`park-typed-${unitId}`}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              disabled={pending}
              autoComplete="off"
              placeholder={unitId}
            />
          </div>
          {error !== null && <ParkRefusalBlock refusal={error} />}
        </div>
        {/* The pinned footer: the hint, the fixed sentence VERBATIM immediately
            above the confirm button, and the actions — never scrolled away. */}
        <div className="dialog-footer">
          {hint !== "" && (
            <p className="dialog-hint" role="status">
              {hint}
            </p>
          )}
          <p role="note" className="park-fixed-sentence">
            {NOT_ISOLATION_SENTENCE}
          </p>
          <div className="dialog-actions">
            <button type="button" onClick={onCancel} disabled={pending}>
              Cancel
            </button>
            <button type="button" onClick={() => onConfirm(reason.trim(), leaseS ?? 60)} disabled={!canConfirm}>
              Park {unitId}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export interface ResumeDialogProps {
  unitId: string;
  park: ParkStateView | null;
  error: ParkRefusalView | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (takeover: boolean) => void;
}

/**
 * The resume dialog: a plain confirm for our own lease (live or expired — the
 * operator's fresh RESUME is the act), and its own acknowledgement step
 * exactly when the park wasn't ours (origin foreign/unrecorded) — the checkbox
 * the 409 refusal also routes into, because the refusal IS the routing. The
 * same pinned-footer pattern as the park dialog: the takeover step and the
 * refusal record scroll in the body; the disabled-reason hint and the actions
 * stay visible, so the grayed confirm always names what is missing.
 */
export function ResumeDialog({
  unitId,
  park,
  error,
  pending,
  onCancel,
  onConfirm,
}: ResumeDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const handleKeyDown = useDialogTrap(dialogRef, onCancel);
  const [acknowledged, setAcknowledged] = useState(false);
  // The refusal routes the takeover step even when the projection did not
  // predict it (the word moved between reads).
  const takeover = takeoverRequired(park) || (error !== null && isTakeoverRefusal(error));
  const canConfirm = !pending && (!takeover || acknowledged);
  const hint = pending ? "" : resumeConfirmHintText(takeover && !acknowledged);
  return (
    <div className="dialog-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="resume-dialog-title"
        className="dialog park-dialog"
        onKeyDown={handleKeyDown}
      >
        <div className="dialog-body">
          <h2 id="resume-dialog-title">Resume {unitId}</h2>
          <p>
            Resuming writes the Normal mode word and brings the battery back under this
            controller&apos;s authority. It takes about a second.
          </p>
          {takeover ? (
            <div className="park-takeover">
              <p>
                This battery was {parkOriginText(park?.origin ?? "foreign")} — the park is not this
                controller&apos;s lease. Resuming is a deliberate takeover:
                {park?.origin === "unrecorded"
                  ? " the lease record is missing, so what parked it cannot be named."
                  : " another writer parked it."}
              </p>
              <label className="park-takeover-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  disabled={pending}
                />{" "}
                I understand I am taking over a park this controller did not make.
              </label>
            </div>
          ) : (
            <p>
              {park === null
                ? "No park projection is available — resuming is still the operator act."
                : park.parked
                  ? `Parked ${parkOriginText(park.origin)}${park.expired ? " — the lease has expired; this fresh Resume is the act." : "."}`
                  : "The battery reads un-parked; a resume on a Normal word is a harmless no-op."}
            </p>
          )}
          {error !== null && <ParkRefusalBlock refusal={error} />}
        </div>
        <div className="dialog-footer">
          {hint !== "" && (
            <p className="dialog-hint" role="status">
              {hint}
            </p>
          )}
          <div className="dialog-actions">
            <button type="button" onClick={onCancel} disabled={pending}>
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onConfirm(takeover)}
              disabled={!canConfirm}
            >
              Resume {unitId}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// --- the post-resume checklist (rendered inline) ------------------------------

/**
 * The resume 200's checklist, rendered inline beside the battery: the honest
 * after-park facts the operator reads before trusting the pod again. Null
 * figures read "not available", never zero; each latched stop gets the pinned
 * "remains stopped by {stop_id} — acknowledge separately" line.
 */
export function ResumeChecklistCard({
  unitId,
  checklist,
  onDismiss,
}: {
  unitId: string;
  checklist: ResumeChecklistView;
  onDismiss: () => void;
}): JSX.Element {
  const comms =
    checklist.commsAgeS === null
      ? "not available"
      : `${wholeSeconds(checklist.commsAgeS)} ${wholeSeconds(checklist.commsAgeS) === 1 ? "second" : "seconds"}`;
  const drift =
    checklist.socDriftPct === null
      ? "not available"
      : `${formatPercent(checklist.socDriftPct)}${
          checklist.socPctAtPark === null ? "" : ` since park (${formatPercent(checklist.socPctAtPark)} at park)`
        }`;
  const faults =
    checklist.faultsWhileParked === null
      ? "not available"
      : checklist.faultsWhileParked.length === 0
        ? "none recorded"
        : checklist.faultsWhileParked.join(", ");
  return (
    <div
      role="status"
      aria-label={`${unitId} resumed — after-park checklist`}
      className="resume-checklist"
    >
      <p className="resume-checklist-title">
        {unitId} resumed — after-park checklist
      </p>
      <ul>
        <li>Communications age: {comms}</li>
        <li>Charge level drift: {drift}</li>
        <li>Power now: {checklist.measuredWattsNow === null ? "not available" : formatWatts(checklist.measuredWattsNow)}</li>
        <li>Faults while parked: {faults}</li>
      </ul>
      {checklist.faultsRetentionNote !== "" && (
        <p className="resume-checklist-note">{checklist.faultsRetentionNote}</p>
      )}
      {checklist.latchedStops.length > 0 ? (
        <ul className="resume-checklist-stops">
          {checklist.latchedStops.map((stopId) => (
            <li key={stopId}>remains stopped by {stopId} — acknowledge separately</li>
          ))}
        </ul>
      ) : (
        <p className="resume-checklist-note">No latched stops hold this battery.</p>
      )}
      {checklist.latchedInhibit && (
        <p className="resume-checklist-note">
          An inhibit latch also holds — acknowledging it and re-qualifying remain separate steps.
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={onDismiss}>
          Dismiss checklist
        </button>
      </div>
    </div>
  );
}

// --- the wedge-signature recovery advisory card --------------------------------

/** The advisory's own wire presence (narrowed by the view; presence = render). */
export interface RecoveryAdvisoryView {
  echoClassifications: string[];
}

/**
 * The wedge-signature advisory (§7): the three-step walkthrough — disarm →
 * park → resume — proven live 2026-08-24, with the operator-at-the-pod rule
 * and the foreign-writer-during-park note. ADVISORY-ONLY, and honest about
 * commissioning: when the site has no `parking:` block the card renders
 * UNAVAILABLE — never a suggestion the site cannot execute.
 */
export function RecoveryAdvisoryCard({
  advisory,
  parkingComposed,
}: {
  advisory: RecoveryAdvisoryView;
  parkingComposed: boolean;
}): JSX.Element {
  const echoes =
    advisory.echoClassifications.length === 0
      ? ""
      : ` The classifier read: ${advisory.echoClassifications.join(", ")}.`;
  if (!parkingComposed) {
    return (
      <div role="note" className="recovery-advisory recovery-advisory--unavailable">
        <p className="recovery-advisory-title">
          Soft recovery available — but parking is not commissioned on this site
        </p>
        <p>
          The park/resume cycle would recover this wedge, and this site has not commissioned the
          parking tool, so it cannot be executed from this console.{echoes}
        </p>
        <p>The physical-restart checklist stands: remote recovery is exhausted.</p>
      </div>
    );
  }
  return (
    <div role="note" className="recovery-advisory">
      <p className="recovery-advisory-title">Soft recovery available: the park/resume cycle</p>
      <p>
        The battery is not serving its command while the command path is fine — the wedge
        signature (proven live 2026-08-24).{echoes}
      </p>
      <ol className="recovery-advisory-steps">
        <li>Disarm the battery.</li>
        <li>Park it — seconds, not minutes.</li>
        <li>Resume it, then re-arm.</li>
      </ol>
      <p>Keep the operator at the pod the first time per unit.</p>
      <p>
        If a foreign writer wrote during the park, re-arm may need the takeover acknowledgement.
      </p>
      <p className="recovery-advisory-note">
        Advisory-only — the abort-to-human response is unchanged.
      </p>
    </div>
  );
}

// --- the delivery-bias evidence readout ----------------------------------------

/** The bias window's own wire figures (narrowed by the view). */
export interface DeliveryBiasView {
  meanBiasPct: number | null;
  maxBiasPct: number | null;
  sampleCount: number;
  windowS: number | null;
}

/**
 * The delivery-bias readout (§7): mean/max bias, sample count, and the window
 * — EVIDENCE, never a warning. Plain note styling, the evidence-only label,
 * and the standing sentence that no control path reads it.
 */
export function DeliveryBiasReadout({ bias }: { bias: DeliveryBiasView }): JSX.Element {
  const mean = bias.meanBiasPct === null ? "not available" : formatPercent(bias.meanBiasPct);
  const max = bias.maxBiasPct === null ? "not available" : formatPercent(bias.maxBiasPct);
  const window =
    bias.windowS === null
      ? "its window not available"
      : `the last ${leaseDurationText(bias.windowS)}`;
  return (
    <div role="note" className="delivery-bias">
      <p>
        <b>Delivery bias (evidence-only):</b> mean {mean}, max {max} — {bias.sampleCount} sample
        {bias.sampleCount === 1 ? "" : "s"} over {window}.
      </p>
      <p className="delivery-bias-note">
        Authorized-versus-delivered evidence for the operator; no control decision reads it.
      </p>
    </div>
  );
}
