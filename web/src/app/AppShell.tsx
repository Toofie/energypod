/**
 * The operator-console app shell (UI_CONTRACTS.md "Authentication model" and
 * "Global shell behavior"): the token gate, the always-visible fleet banner,
 * the four-fact connection indicator, ARIA live regions, honest navigation,
 * the emergency-stop control, and view switching by state — no router
 * library, the shell drives views itself.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
import { createApiClient } from "../api/client";
import type { ApiClient, ApiClientError } from "../api/client";
import { SharedDataPlane, sharedClient } from "./SharedDataPlane";
import { ConnectionFacts, DisconnectedNotice, FleetBanner } from "./ShellChrome";
import { StopControl } from "./StopControl";
import { TokenEntry } from "./TokenEntry";
import { useConsoleData } from "./useConsoleData";
import type { RefusalEnvelope } from "./useConsoleData";
import { usePrefersReducedMotion } from "./usePrefersReducedMotion";
import { PLANNED_VIEWS, VIEWS } from "./views";
import type { ShellView, ViewId } from "./views";

type Session =
  | { phase: "entry"; refusal: RefusalEnvelope | null }
  | { phase: "active"; client: ApiClient; plane: SharedDataPlane };

const INITIAL_SESSION: Session = { phase: "entry", refusal: null };

/**
 * Views are injected, not imported: the shell's own contract covers the gate,
 * banner, facts, navigation, announcements, and stop; the product tree (see
 * main.tsx) supplies the real view components through this prop. An id with no
 * injected component renders an honest not-configured state, never a lie.
 */
export interface AppShellProps {
  views?: Partial<Record<ViewId, ShellView>>;
}

export function AppShell({ views = {} }: AppShellProps): ReactElement {
  const reducedMotion = usePrefersReducedMotion();
  const [session, setSession] = useState<Session>(INITIAL_SESSION);
  const [token, setToken] = useState("");
  const [view, setView] = useState<ViewId>("home");
  const headingRef = useRef<HTMLHeadingElement>(null);

  const returnToEntry = useCallback((refusal: RefusalEnvelope | null): void => {
    setSession({ phase: "entry", refusal });
    setToken("");
    setView("home");
  }, []);

  const handleUnauthorized = useCallback(
    (error: ApiClientError): void => {
      // A 401 on any call: the session ends, everything cached is dropped.
      returnToEntry({ code: error.code, message: error.message });
    },
    [returnToEntry],
  );

  const activePlane = session.phase === "active" ? session.plane : null;
  const data = useConsoleData(activePlane, handleUnauthorized);
  // Views receive the session's shared client: coalesced reads, fanned-out stream.
  const viewClient = useMemo(
    () => (session.phase === "active" ? sharedClient(session.plane, session.client) : null),
    [session],
  );

  const unlock = (): void => {
    if (token === "") {
      return;
    }
    const client = createApiClient(token);
    setSession({ phase: "active", client, plane: new SharedDataPlane(client) });
  };

  const switchView = (next: ViewId): void => {
    setView(next);
  };

  // Moving between views moves focus sensibly: to the new view's heading.
  useEffect(() => {
    if (session.phase === "active") {
      headingRef.current?.focus();
    }
  }, [view, session.phase]);

  if (session.phase === "entry") {
    return (
      <TokenEntry
        token={token}
        onPasteToken={setToken}
        onSubmit={unlock}
        refusal={session.refusal}
      />
    );
  }

  const client = session.client;
  const disconnected = data.streamStatus === "down";
  const currentView = VIEWS.find((entry) => entry.id === view) ?? VIEWS[0]!;
  const CurrentComponent = views[currentView.id];

  return (
    <div
      className={[
        "shell",
        reducedMotion ? "reduce-motion" : "",
        disconnected ? "is-disconnected" : "",
      ]
        .filter((part) => part !== "")
        .join(" ")}
    >
      <header className="shell-header">
        <p className="brand">EnergyPod</p>
        <FleetBanner units={data.snapshot?.units ?? []} />
        <ConnectionFacts data={data} />
        <div className="header-actions">
          <StopControl
            client={client}
            units={data.snapshot?.units ?? []}
            onUnauthorized={handleUnauthorized}
          />
          <button
            type="button"
            onClick={() => {
              returnToEntry(null);
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      <nav aria-label="Views" className="shell-nav">
        <ul>
          {VIEWS.map((entry) => (
            <li key={entry.id}>
              <a
                href={`#view-${entry.id}`}
                aria-current={view === entry.id ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  switchView(entry.id);
                }}
              >
                {entry.label}
              </a>
            </li>
          ))}
          {PLANNED_VIEWS.map((label) => (
            <li key={label} className="planned-view" aria-label={`${label} — not available yet`}>
              {label} — <em>not available yet</em>
            </li>
          ))}
        </ul>
      </nav>

      <div role="status" aria-label="Announcements" aria-live="polite" className="announcements">
        {data.polite.map((text, index) => (
          <p key={index}>{text}</p>
        ))}
      </div>
      {data.assertive.map((text, index) => (
        <p key={index} role="alert" className="urgent-announcements">
          {text}
        </p>
      ))}

      {disconnected && <DisconnectedNotice />}

      <main aria-labelledby="current-view-heading" className={disconnected ? "dimmed" : ""}>
        <h1 id="current-view-heading" tabIndex={-1} ref={headingRef}>
          {currentView.label}
        </h1>
        {data.snapshotError !== null && (
          <div role="alert" className="snapshot-error">
            <p>
              <span>{data.snapshotError.code}</span> — <span>{data.snapshotError.message}</span>
            </p>
            <button type="button" onClick={data.retrySnapshot}>
              Retry
            </button>
          </div>
        )}
        {data.snapshot === null && data.snapshotError === null && (
          <div role="status" className="loading">
            <p>Loading the fleet picture…</p>
          </div>
        )}
        {CurrentComponent !== undefined ? (
          <CurrentComponent client={viewClient ?? client} />
        ) : (
          <p className="view-placeholder">
            This view is not configured in this build of the console.
          </p>
        )}
      </main>
    </div>
  );
}
