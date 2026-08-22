/**
 * The token gate (UI_CONTRACTS.md "Authentication model, console side"):
 * one field, paste-only, submit labeled "Unlock". The token lives in memory
 * only — never storage, never the URL — so the field is readonly and accepts
 * input only through an explicit paste.
 */
import type { ReactElement } from "react";
import type { RefusalEnvelope } from "./useConsoleData";

export interface TokenEntryProps {
  token: string;
  onPasteToken: (token: string) => void;
  onSubmit: () => void;
  refusal: RefusalEnvelope | null;
}

export function TokenEntry({ token, onPasteToken, onSubmit, refusal }: TokenEntryProps): ReactElement {
  return (
    <div className="token-entry">
      <h1>EnergyPod</h1>
      <p id="token-heading">Unlock the console with your operator access token.</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label htmlFor="token-field">Access token</label>
        <input
          id="token-field"
          name="token"
          type="text"
          autoComplete="off"
          spellCheck={false}
          readOnly
          value={token}
          placeholder="Paste the token here — typing is disabled"
          aria-describedby="token-help"
          onPaste={(event) => {
            event.preventDefault();
            const pasted = event.clipboardData.getData("text/plain").trim();
            if (pasted !== "") {
              onPasteToken(pasted);
            }
          }}
        />
        <p id="token-help">
          The token stays in this tab's memory only. It is never stored and never
          written into the address bar.
        </p>
        <button type="submit">Unlock</button>
      </form>
      {refusal !== null && (
        <p role="alert" className="refusal">
          <span>{refusal.code}</span> — <span>{refusal.message}</span>
        </p>
      )}
    </div>
  );
}
