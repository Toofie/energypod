/**
 * Emergency-stop reachability (UI_CONTRACTS.md "Global shell behavior"): the
 * control is reachable from every screen that can display or initiate active
 * control, and a focus-trapping confirmation names the affected units before
 * anything is requested. Escape declines and restores focus; confirming calls
 * client.postEmergencyStop with the affected unit ids.
 */
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactElement, RefObject } from "react";
import { ApiClientError, isUnauthorizedError } from "../api/client";
import type { ApiClient } from "../api/client";
import { stoppableUnits, type UnitModel } from "./fleet";
import type { RefusalEnvelope } from "./useConsoleData";

export interface StopControlProps {
  client: ApiClient;
  units: UnitModel[];
  onUnauthorized: (error: ApiClientError) => void;
}

const STOP_REASON = "Emergency stop confirmed at the console";

export function StopControl({ client, units, onUnauthorized }: StopControlProps): ReactElement {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<RefusalEnvelope | null>(null);
  const openerRef: RefObject<HTMLButtonElement | null> = useRef(null);
  const dialogRef: RefObject<HTMLDivElement | null> = useRef(null);
  const wasOpen = useRef(false);

  // Closing the dialog (either way) hands focus back to the stop control.
  useEffect(() => {
    if (wasOpen.current && !open) {
      openerRef.current?.focus();
    }
    wasOpen.current = open;
  }, [open]);

  const affected = stoppableUnits(units).map((unit) => unit.unitId);

  const close = (): void => {
    setFailure(null);
    setOpen(false);
  };

  const confirm = (): void => {
    if (affected.length === 0) {
      close();
      return;
    }
    setBusy(true);
    setFailure(null);
    client
      .postEmergencyStop(affected, STOP_REASON)
      .then(() => {
        setBusy(false);
        close();
      })
      .catch((error: unknown) => {
        setBusy(false);
        if (isUnauthorizedError(error)) {
          onUnauthorized(error);
          return;
        }
        if (error instanceof ApiClientError) {
          setFailure({ code: error.code, message: error.message });
          return;
        }
        setFailure({ code: "unexpected_error", message: "The stop request did not complete." });
      });
  };

  const trapFocus = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    if (focusable === undefined || focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    const active = document.activeElement;
    if (event.shiftKey && active !== first) {
      return;
    }
    if (!event.shiftKey && active !== last) {
      return;
    }
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  };

  return (
    <div className="stop-control">
      <button ref={openerRef} type="button" className="stop-button" onClick={() => setOpen(true)}>
        Emergency stop
      </button>
      {open && (
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="stop-dialog-title"
          className="stop-dialog"
          onKeyDown={trapFocus}
        >
          <h2 id="stop-dialog-title">Confirm emergency stop</h2>
          <p>
            Emergency stop asks every affected unit to stop immediately. Affected
            units: {affected.length === 0 ? "none" : affected.join(", ")}.
          </p>
          {failure !== null && (
            <p role="alert" className="refusal">
              <span>{failure.code}</span> — <span>{failure.message}</span>
            </p>
          )}
          <div className="dialog-actions">
            <button type="button" autoFocus onClick={close}>
              Cancel
            </button>
            <button type="button" disabled={busy} onClick={confirm}>
              Confirm stop
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
