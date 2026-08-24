/**
 * The "PVOutput reporting" Home card — the retiring Docker writer's
 * replacement in one glance, placed beside the night tile (the two halves
 * of the same cutover: the night strategy took the charging, this takes the
 * reporting).
 *
 * States the card owns (each honest, never a blank):
 *
 * - NOT COMMISSIONED: the controller has no `pvoutput` config block — the
 *   status route's structured 409 `pvoutput_not_commissioned` IS the state.
 *   One fixed sentence says commissioning is a config change, never a
 *   console act; there is no toggle to present.
 * - DISABLED (composed, off): the toggle's phrase names the origin — the
 *   config's own setting, or the operator's console act "kept across
 *   restarts" (the durable-toggle honesty; the night tile's "until restart"
 *   is the deliberate mirror this feature inverts).
 * - POSTING (enabled, healthy): the last post's age and slot, PVOutput's
 *   own remaining-post budget, and the stale-gap count (non-zero gaps say
 *   so — honest gaps on the dashboard, never zero-filled).
 * - FAILING (enabled, error word): the standing causes are named in plain
 *   words (a credential PVOutput refuses, absent credentials) and every
 *   other failure carries the wire's own words — PVOutput's refusal text
 *   rides VERBATIM in `last_error`.
 *
 * The toggle is the night tile's guarded shape with this feature's literal:
 * BOTH actions type PVOUTPUT; enabling starts an external write (the
 * boundary demands an interactive operator), disabling stops one. Refusals
 * render their envelope inline and keep the dialog open.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  PVOUTPUT_CONFIRMATION,
  PVOUTPUT_NOT_COMMISSIONED_TEXT,
  pvoutputGapsText,
  pvoutputHealthText,
  pvoutputLastPostText,
  pvoutputToggleStateText,
  toPvOutputStatus,
  type PvOutputStatus,
} from "../../app/pvoutput";

/** The health re-read cadence: the reporter's world moves once per slot. */
export const PVOUTPUT_POLL_MS = 15000;

export interface PvOutputCardProps {
  /** The session's client: the status read and the guarded toggle ride it. */
  client: ApiClient;
}

interface ToggleRefusal {
  code: string;
  message: string;
}

function asRefusal(error: unknown): ToggleRefusal | null {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message };
  }
  return null;
}

/** The plain sentence for a refused toggle; the envelope always rides beside. */
function refusalSentence(refusal: ToggleRefusal): string {
  if (refusal.code === "pvoutput_not_commissioned") {
    return PVOUTPUT_NOT_COMMISSIONED_TEXT;
  }
  if (refusal.code === "pvoutput_toggle_failed") {
    return "The choice could not be recorded in the controller's durable store — nothing changed. Try again.";
  }
  return "";
}

// --- the typed-confirmation dialog ---------------------------------------------

function PvOutputToggleDialog({
  action,
  refusal,
  pending,
  onCancel,
  onConfirm,
}: {
  action: "enable" | "disable";
  refusal: ToggleRefusal | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [typed, setTyped] = useState("");
  const titleId = "pvoutput-toggle-title";
  const inputId = "pvoutput-toggle-confirmation";
  const typedOk = typed.trim() === PVOUTPUT_CONFIRMATION;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    (dialog.querySelector<HTMLElement>("#" + inputId) ?? dialog).focus();
  }, []);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
    }
  };

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="home-pvoutput-dialog"
      onKeyDown={handleKeyDown}
    >
      <h2 id={titleId}>
        {action === "enable" ? "Turn on PVOutput reporting" : "Turn off PVOutput reporting"}
      </h2>
      <p>
        {action === "enable"
          ? "Enabling posts each battery's charge level and power to pvoutput.org once every 5 minutes, on the dashboard slots the old container used. The choice is kept across controller restarts — stand the old Docker container down beside this."
          : "Disabling stops the posts from this controller; the choice is kept across controller restarts."}
      </p>
      <p className="home-pvoutput-typed">
        <label htmlFor={inputId}>
          Type {PVOUTPUT_CONFIRMATION} to confirm{" "}
          {action === "enable" ? "turning it on" : "turning it off"}
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
        <div role="alert" className="home-pvoutput-refusal">
          {refusalSentence(refusal) !== "" && <p>{refusalSentence(refusal)}</p>}
          <p>
            <code>{refusal.code}</code> — <span>{refusal.message}</span>
          </p>
        </div>
      )}
      <div className="home-pvoutput-dialog-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button
          type="button"
          className="home-pvoutput-confirm"
          disabled={!typedOk || pending}
          onClick={onConfirm}
        >
          {action === "enable" ? "Turn on" : "Turn off"}
        </button>
      </div>
    </div>
  );
}

// --- the card -------------------------------------------------------------------

export function PvOutputCard({ client }: PvOutputCardProps): JSX.Element | null {
  const headingId = useId();
  // A client from an earlier build without this surface renders nothing at
  // all (the feature detection the other cards key on the snapshot for).
  const supported = typeof client.getPvOutputStatus === "function";
  const [status, setStatus] = useState<PvOutputStatus | null>(null);
  const [notCommissioned, setNotCommissioned] = useState(false);
  const [unreadable, setUnreadable] = useState(false);
  const [dialog, setDialog] = useState<null | { action: "enable" | "disable" }>(null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState<ToggleRefusal | null>(null);
  const switchRef = useRef<HTMLButtonElement | null>(null);
  const wasOpen = useRef(false);

  const readStatus = useCallback((): void => {
    if (!supported) {
      return;
    }
    client
      .getPvOutputStatus()
      .then(
        (body) => {
          const parsed = toPvOutputStatus(body);
          if (parsed === null) {
            setUnreadable(true);
            return;
          }
          setUnreadable(false);
          setNotCommissioned(false);
          setStatus(parsed);
        },
        (error: unknown) => {
          if (error instanceof ApiClientError && error.code === "pvoutput_not_commissioned") {
            // The structured 409 IS the state: commissioned nowhere, nothing
            // to toggle, one honest sentence.
            setNotCommissioned(true);
            setUnreadable(false);
            setStatus(null);
            return;
          }
          setUnreadable(true);
        },
      );
  }, [client, supported]);

  useEffect(() => {
    if (!supported) {
      return undefined;
    }
    readStatus();
    // The reporter's world moves once per slot; the re-read keeps the last
    // post's age and the failure word current without a stream of its own.
    const timer = window.setInterval(readStatus, PVOUTPUT_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [readStatus, supported]);

  // Closing the dialog (either way) hands focus back to the switch.
  useEffect(() => {
    if (wasOpen.current && dialog === null) {
      switchRef.current?.focus();
    }
    wasOpen.current = dialog !== null;
  }, [dialog]);

  const confirmToggle = (): void => {
    if (dialog === null || pending) {
      return;
    }
    const action = dialog.action;
    const base = status;
    setPending(true);
    setRefusal(null);
    client
      .postPvOutput(action)
      .then(
        (body) => {
          // Optimistic adoption (the night tile's pattern): the 200's own
          // post-toggle snapshot becomes the card's state now; the next
          // status poll confirms it.
          const adopted = toPvOutputStatus(
            (body as Record<string, unknown>).pvoutput_state ?? null,
            base,
          );
          if (adopted !== null) {
            setStatus(adopted);
            setRefusal(null);
            setDialog(null);
          }
        },
        (error: unknown) => {
          const envelope = asRefusal(error);
          setRefusal(
            envelope ?? {
              code: "unexpected_error",
              message: "The PVOutput toggle did not complete.",
            },
          );
        },
      )
      .finally(() => {
        setPending(false);
      });
  };

  if (!supported) {
    return null;
  }
  if (notCommissioned) {
    return (
      <section className="home-card home-card--pvoutput" aria-labelledby={headingId}>
        <h2 id={headingId}>PVOutput reporting</h2>
        <p className="home-pvoutput-status" data-state="not_commissioned">
          {PVOUTPUT_NOT_COMMISSIONED_TEXT}
        </p>
      </section>
    );
  }
  if (status === null) {
    return (
      <section className="home-card home-card--pvoutput" aria-labelledby={headingId}>
        <h2 id={headingId}>PVOutput reporting</h2>
        <p role="status" className="home-pvoutput-status" data-state={unreadable ? "error" : "reading"}>
          {unreadable
            ? "The PVOutput reporter's state could not be read — the next retry is automatic."
            : "Reading the PVOutput reporter's state…"}
        </p>
      </section>
    );
  }
  const gaps = pvoutputGapsText(status);
  return (
    <section className="home-card home-card--pvoutput" aria-labelledby={headingId}>
      <h2 id={headingId}>PVOutput reporting</h2>
      <p
        className="home-pvoutput-status"
        data-enabled={status.enabled ? "true" : "false"}
        data-disabled-reason={status.disabledReason ?? undefined}
      >
        {pvoutputHealthText(status)}
      </p>
      <p className="home-pvoutput-lastpost">{pvoutputLastPostText(status)}</p>
      {gaps !== null && <p className="home-pvoutput-gaps">{gaps}</p>}
      {status.credentialsNote !== null && (
        <p role="note" className="home-pvoutput-credentials">
          {status.credentialsNote}.
        </p>
      )}
      <div className="home-pvoutput-toggle">
        <p className="home-pvoutput-toggle-state">
          PVOutput reporting: <strong>{pvoutputToggleStateText(status)}</strong>
        </p>
        <button
          ref={switchRef}
          type="button"
          role="switch"
          aria-checked={status.enabled ? "true" : "false"}
          className={status.enabled ? "home-pvoutput-switch is-on" : "home-pvoutput-switch"}
          onClick={() => {
            setRefusal(null);
            setDialog({ action: status.enabled ? "disable" : "enable" });
          }}
        >
          PVOutput reporting
        </button>
      </div>
      {dialog !== null && (
        <PvOutputToggleDialog
          action={dialog.action}
          refusal={refusal}
          pending={pending}
          onCancel={() => {
            setRefusal(null);
            setDialog(null);
          }}
          onConfirm={confirmToggle}
        />
      )}
    </section>
  );
}
