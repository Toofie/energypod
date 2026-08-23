/**
 * Behavior contract for the operator-console app shell.
 *
 * Pins UI_CONTRACTS.md "Authentication model (console side)" and "Global shell
 * behavior" (plus the roadmap's "Non-technical UI information architecture" and
 * "Interaction and visual standards"): token entry, the always-visible fleet
 * banner, the four separate connection facts, ARIA live-region announcements,
 * emergency-stop reachability with a focus-trapping confirmation, keyboard
 * navigation with visible focus, honest not-available navigation entries, the
 * disconnected operator state, and reduced motion.
 *
 * The API client module (web/src/api/client.ts) is mocked at its exact surface
 * (only `createApiClient` is replaced; `ApiClientError` keeps the real class so
 * rejection shapes are exact). Wire fixtures mirror what the service actually
 * sends:
 *
 * - Casing follows UI_CONTRACTS.md "Wire casing": the service serializes enums
 *   as lowercase StrEnum values (src/energypod/domain/observations.py,
 *   intents.py via service.py `_enum_value`) — lifecycle "boot" /
 *   "observe_only" / "disarmed" / "armed_idle" / "active" / "inhibited" /
 *   "stopping" / "disconnected", direction "charge" / "discharge" / "idle",
 *   snapshot quality "good" / "degraded" / "bad" / "missing" (the facade's
 *   `_quality_projection` never emits "stale" or "suspect" on the wire — stale
 *   data arrives as quality "degraded" with `telemetry_age_s` past the
 *   freshness bound). Badge labels shown to humans are capitalized; every wire
 *   literal in a fixture is lowercase.
 * - WS first frame `{type: "snapshot", sequence, data}` (rest.py sends exactly
 *   this), then ordered event frames `{type, payload, sequence, occurred_at}`
 *   from the vocabulary the service actually publishes: unit.armed,
 *   unit.disarmed, intent.accepted, emergency_stop.latched,
 *   emergency_stop.acknowledged, inhibit.acknowledged, authorization.revoked,
 *   observation.published, audit.appended. Unit facts live under `payload`.
 *   Discontinuity frames are `{type: "resync_required", reason,
 *   snapshot_sequence?}` with NO `sequence` (rest.py's terminal marker); the
 *   pinned client ends iteration right after one (and after an unexpected
 *   close), and these channels mirror that so reconnect paths are exercised.
 *
 * Reconciliation pins for the primary:
 * (1) client.ts stub's `Health` interface types three flat strings, but the
 *     real service returns nested `{liveness:{ok}, service_readiness:{ready,
 *     reasons}, control_readiness:{ready,reasons}}` (service.py `health`,
 *     rest.py). Fixtures here use the real nested shape and are injected
 *     through one cast until client.ts's owner corrects the interface.
 * (2) No dedicated inhibit-latched event exists in the published vocabulary.
 *     The actor's latched-inhibit path surfaces on the bus as
 *     `authorization.revoked` with payload `{reason: <latched cause>,
 *     unit_ids}` (composition.py); this suite pins that frame as the shell's
 *     assertive inhibit signal. If the API grows a dedicated event, reconcile.
 * (3) Accessible names — banner region /fleet status/i; connection group
 *     /connection/i with four listitems /page loaded/i, /API reachable/i,
 *     /event stream/i, /control/i; polite live region role=status named
 *     /announcements/i; refusal notices are role=alert carrying the envelope's
 *     code and message together; badge labels "Observe only/Disarmed/Armed/
 *     Active/Limited/Inhibited" each with a role=img whose accessible name
 *     matches the badge (never color alone).
 * (4) Conservatism order: inhibited over disarmed/armed_idle, observe_only over
 *     armed_idle (banner most-conservative; unit list disambiguates); units
 *     the snapshot marks disconnected/booting are never presented with an
 *     armed/active badge and the disconnection is named.
 * (5) Paste-only is pinned behaviorally (typed chars never populate; paste
 *     populates) via a readonly field with a paste handler.
 * (6) 401 on any call returns to entry with createApiClient called exactly
 *     once; sign-out name /sign out|lock\b/i; in-memory-only pin asserts
 *     storage lengths 0, clean URL, token never echoed in the DOM.
 * (7) matchMedia is stubbed (jsdom lacks it); the reduced-motion test asserts
 *     the implementation actually queries prefers-reduced-motion.
 * (8) The shell refetches the snapshot whenever a frame changes authority
 *     (emergency_stop.latched, authorization.revoked, inhibit.acknowledged,
 *     emergency_stop.acknowledged, unit.armed, unit.disarmed) because the
 *     server sends its snapshot frame exactly once per connection and the bus
 *     has no lifecycle event for active/inhibited/stopping. Fixtures model
 *     that: `snapshots` lists the worlds successive reads return, so a
 *     post-event read never silently resurrects the pre-event one. Arm/disarm
 *     outcomes patch state only for rows the service accepted (`status`), and
 *     only latched causes (`blocking_fault_active`, `identity_mismatch`) are
 *     announced assertively as inhibit — routine revocations stay polite.
 * (9) The emergency-stop latch banner (2026-08-23 incident) is shell chrome
 *     rendered from the snapshot's PENDING `active_stops` field
 *     ({stop_id, latched_at, principal, reason_codes, unit_ids|null}), never
 *     from session state or a bus event: a console opened mid-latch sees the
 *     stop and can release it. Absent field (today's backend) = no banner and
 *     nothing else changes. The inline release gates on input === stop_id
 *     (whitespace tolerated) and calls client.postStopAcknowledgement, which
 *     always sends the Idempotency-Key header; a 200 clears the banner
 *     optimistically, the next snapshot confirms.
 *
 * TokenEntry, NavBanner, ConnectionIndicator, and EventStreamProvider are
 * internal to AppShell: this suite never imports them, so no stubs are created
 * for them (shared-stub list entry: "AppShell internals — none imported").
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../api/client";
import type { ApiClient } from "../api/client";
import { AppShell } from "./AppShell";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, createApiClient: vi.fn() };
});

const createClientMock = vi.mocked(createApiClient);

type UserSession = ReturnType<typeof userEvent.setup>;

// --- wire fixtures ----------------------------------------------------------
// Lowercase wire values exactly as the service serializes them (see header).

const OPERATOR_TOKEN = "operator-session-token-7f3a";

interface PowerFigure {
  direction: "charge" | "discharge" | "idle";
  watts: number;
}

interface UnitView {
  unit_id: string;
  lifecycle:
    | "boot"
    | "observe_only"
    | "disarmed"
    | "armed_idle"
    | "active"
    | "inhibited"
    | "stopping"
    | "disconnected";
  telemetry_age_s: number | null;
  quality: "good" | "degraded" | "bad" | "missing";
  requested_power: PowerFigure;
  authorized_power: PowerFigure | null;
  measured_watts: number | null;
}

interface FleetView {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: UnitView[];
  /**
   * The amended snapshot contract's engaged stops (PENDING backend field).
   * Absent = today's wire: the latch banner must stay hidden and nothing else
   * may change (feature detection).
   */
  active_stops?: ActiveStopView[];
  /**
   * The snapshot-level intent block (PENDING backend field): the live
   * request's own per-unit figures for cold-load exactness. Absent = today's
   * wire (feature detection); null = no active request.
   */
  intent?: SnapshotIntentView | null;
}

interface SnapshotIntentView {
  requested_watts_by_unit: Record<string, number> | null;
  authorized_watts_by_unit: Record<string, number> | null;
  directions_by_unit: Record<string, string> | null;
}

interface ActiveStopView {
  stop_id: string;
  latched_at: string;
  principal: string;
  reason_codes: string[];
  unit_ids: string[] | null;
}

interface HealthView {
  liveness: { ok: boolean };
  service_readiness: { ready: boolean; reasons: string[] };
  control_readiness: { ready: boolean; reasons: string[] };
}

interface StreamFrame {
  type: string;
  sequence?: number;
  [field: string]: unknown;
}

const SEQUENCE = 41;

function unit(partial: Partial<UnitView> & Pick<UnitView, "unit_id">): UnitView {
  return {
    lifecycle: "armed_idle",
    telemetry_age_s: 2,
    quality: "good",
    requested_power: { direction: "idle", watts: 0 },
    authorized_power: null,
    measured_watts: 0,
    ...partial,
  };
}

function fleet(units: UnitView[], snapshotSequence: number = SEQUENCE): FleetView {
  return {
    site_id: "site-1",
    snapshot_sequence: snapshotSequence,
    captured_at: "2026-08-22T10:00:00Z",
    units,
  };
}

function allUnits(lifecycle: UnitView["lifecycle"]): UnitView[] {
  return [
    unit({ unit_id: "MID", lifecycle }),
    unit({ unit_id: "RHS", lifecycle }),
    unit({ unit_id: "LHS", lifecycle }),
  ];
}

const HEALTHY: HealthView = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: true, reasons: [] },
};

function snapshotFrame(snapshot: FleetView): StreamFrame {
  return { type: "snapshot", sequence: snapshot.snapshot_sequence, data: snapshot };
}

/** A controllable event stream: yields initial frames, then whatever is pushed. */
interface StreamChannel {
  open(): AsyncGenerator<StreamFrame, void, unknown>;
  push(frame: StreamFrame): void;
  fail(error: unknown): void;
}

function streamChannel(initial: StreamFrame[]): StreamChannel {
  const queue: StreamFrame[] = [...initial];
  let failure: unknown = null;
  let wake: (() => void) | null = null;
  const notify = (): void => {
    const release = wake;
    wake = null;
    release?.();
  };
  const stream = (): AsyncGenerator<StreamFrame, void, unknown> =>
    (async function* channelStream(): AsyncGenerator<StreamFrame, void, unknown> {
      while (true) {
        while (queue.length > 0) {
          const next = queue.shift();
          if (next !== undefined) {
            yield next;
            // The pinned client ends iteration right after the discontinuity
            // marker; mirror that so reconnect behavior is exercised.
            if (next.type === "resync_required") {
              return;
            }
          }
        }
        if (failure !== null) {
          const thrown = failure;
          failure = null;
          throw thrown;
        }
        // Stays open until more frames arrive: a live connection.
        await new Promise<void>((resolve) => {
          wake = resolve;
        });
      }
    })();
  return {
    open: () => stream(),
    push: (frame) => {
      queue.push(frame);
      notify();
    },
    fail: (error) => {
      failure = error;
      notify();
    },
  };
}

/** A stream that stays down: every (re)connection dies after a short delay. */
function unreachableStream(): AsyncGenerator<StreamFrame, void, unknown> {
  return (async function* unreachable(): AsyncGenerator<StreamFrame, void, unknown> {
    await new Promise((resolve) => {
      setTimeout(resolve, 5);
    });
    throw networkError("The EnergyPod service could not be reached");
  })();
}

/**
 * A channel that also records its own teardown and consumption, so a test can
 * prove the shell closes the connection itself — at sign-out — instead of
 * waiting for the next server frame to notice it should stop.
 * `returnRequested` counts return() calls the consumer made on the iterator
 * (observable immediately); `returns` counts the returns that actually settled.
 */
function instrumentedChannel(initial: StreamFrame[]): {
  open(): AsyncIterable<StreamFrame>;
  push(frame: StreamFrame): void;
  fail(error: unknown): void;
  state: { returnRequested: number; returns: number; frames: number };
} {
  const base = streamChannel(initial);
  const state = { returnRequested: 0, returns: 0, frames: 0 };
  const open = (): AsyncIterable<StreamFrame> => {
    const inner = base.open()[Symbol.asyncIterator]();
    const iterator = {
      next: async (): Promise<IteratorResult<StreamFrame, void>> => {
        const result = await inner.next();
        if (!result.done) {
          state.frames += 1;
        }
        return result;
      },
      return: async (): Promise<IteratorResult<StreamFrame, void>> => {
        state.returnRequested += 1;
        try {
          return (await inner.return(undefined)) as IteratorResult<StreamFrame, void>;
        } finally {
          state.returns += 1;
        }
      },
    };
    return { [Symbol.asyncIterator]: () => iterator } as unknown as AsyncIterable<StreamFrame>;
  };
  return { open, push: base.push, fail: base.fail, state };
}

/** A network-level failure, exactly as the pinned client reports it. */
function networkError(message: string): ApiClientError {
  return new ApiClientError({
    status: 0,
    code: "network_error",
    message,
    details: null,
    request_id: "",
  });
}

/** A 401 rejection carrying the API error envelope verbatim. */
function unauthorizedError(): ApiClientError {
  return new ApiClientError({
    status: 401,
    code: "invalid_token",
    message: "That access token is not valid.",
    details: null,
    request_id: "req-shell-1",
  });
}

interface ShellSetup {
  snapshot?: FleetView;
  /**
   * The worlds successive snapshot reads return, in order (the last repeats).
   * The shell refetches the snapshot whenever a frame changes authority — the
   * service sends its snapshot frame once per connection — so a fixture whose
   * later reads still return the pre-event world would be lying about the wire.
   */
  snapshots?: FleetView[];
  getSnapshot?: () => Promise<FleetView>;
  getHealth?: () => Promise<HealthView>;
  openEvents?: (afterSequence?: number) => AsyncIterable<StreamFrame>;
  postEmergencyStop?: (
    unitIds: string[],
    reason: string,
  ) => Promise<Record<string, unknown>>;
  postStopAcknowledgement?: (
    stopId: string,
    idempotencyKey?: string,
  ) => Promise<Record<string, unknown>>;
}

function installClient(setup: ShellSetup = {}): { getSnapshot: ReturnType<typeof vi.fn> } {
  const snapshot = setup.snapshot ?? setup.snapshots?.[0] ?? fleet(allUnits("armed_idle"));
  const channel = streamChannel([snapshotFrame(snapshot)]);
  let served = 0;
  const readSnapshot = (): Promise<FleetView> => {
    if (setup.snapshots === undefined) {
      return Promise.resolve(snapshot);
    }
    const worlds = setup.snapshots;
    const offered = worlds[Math.min(served, worlds.length - 1)]!;
    served += 1;
    return Promise.resolve(offered);
  };
  const client = {
    getSnapshot: vi.fn(setup.getSnapshot ?? readSnapshot),
    getHealth: vi.fn(setup.getHealth ?? (() => Promise.resolve(HEALTHY))),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    postIntent: vi.fn(() => Promise.reject(new Error("not used by AppShell"))),
    postArm: vi.fn(() => Promise.reject(new Error("not used by AppShell"))),
    postDisarm: vi.fn(() => Promise.reject(new Error("not used by AppShell"))),
    postEmergencyStop: vi.fn(
      setup.postEmergencyStop ?? (() => Promise.reject(new Error("not used by AppShell"))),
    ),
    postStopAcknowledgement: vi.fn(
      setup.postStopAcknowledgement ??
        (() => Promise.reject(new Error("not used by AppShell"))),
    ),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(new Error("not used by AppShell"))),
    postExcessCharging: vi.fn(() => Promise.reject(new Error("not used by AppShell"))),
    openEvents: vi.fn(
      setup.openEvents ??
        (() => {
          const generator = channel.open();
          return (async function* wrapped(): AsyncGenerator<StreamFrame, void, unknown> {
            yield* generator;
          })();
        }),
    ),
  };
  createClientMock.mockReturnValue(client as unknown as ApiClient);
  return { getSnapshot: client.getSnapshot };
}

let matchMediaStub: ReturnType<typeof vi.fn> | null = null;

function installMatchMedia(reducedMotion = false): void {
  matchMediaStub = vi.fn((query: string): MediaQueryList =>
    ({
      matches: reducedMotion && query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }) as unknown as MediaQueryList,
  );
  vi.stubGlobal("matchMedia", matchMediaStub);
}

// --- query helpers ---------------------------------------------------------

const BANNER_NAME = /fleet status/i;
const CONNECTION_NAME = /connection/i;
const ANNOUNCEMENTS_NAME = /announcements/i;
const TOKEN_FIELD = /token|access/i;

/** State words an implementation must use for a connection fact's value. */
const FACT_YES = /\byes\b|\bok\b/i;
const FACT_NO = /\bnot\b|\bno\b|unreachable|offline|lost|unavailable|blocked|cannot/i;

function banner(): HTMLElement {
  return screen.getByRole("region", { name: BANNER_NAME });
}

function connectionFacts(): HTMLElement {
  return screen.getByRole("group", { name: CONNECTION_NAME });
}

function factItem(pattern: RegExp): HTMLElement {
  return within(connectionFacts()).getByRole("listitem", { name: pattern });
}

/** jsdom-detectable visibility: a hidden ancestor hides the match too. */
function isShown(element: HTMLElement): boolean {
  for (let node: HTMLElement | null = element; node !== null; node = node.parentElement) {
    if (node.getAttribute("aria-hidden") === "true" || node.hasAttribute("hidden")) {
      return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
  }
  return true;
}

/** Text presence where at least one match (not only the first) is visible. */
function expectVisibleText(container: HTMLElement, pattern: RegExp | string) {
  const matches = within(container).getAllByText(pattern);
  expect(matches.some((match) => isShown(match))).toBe(true);
}

/** The shell-level emergency stop control on the current view, if any. */
function stopControls(): HTMLElement[] {
  const controls = screen.getAllByRole("button", { name: /emergency stop|\bstop\b/i });
  expect(controls.length).toBeGreaterThan(0);
  return controls;
}

async function tabTo(user: UserSession, element: HTMLElement): Promise<void> {
  for (let attempt = 0; attempt < 40 && document.activeElement !== element; attempt += 1) {
    await user.tab();
  }
  expect(document.activeElement).toBe(element);
}

/** Unlock with pointer events; the keyboard-only variant lives in its own test. */
async function unlock(user: UserSession, token: string = OPERATOR_TOKEN): Promise<void> {
  const field = screen.getByRole("textbox", { name: TOKEN_FIELD });
  await user.click(field);
  await user.paste(token);
  await user.click(screen.getByRole("button", { name: "Unlock" }));
}

async function dataLanded(): Promise<void> {
  await screen.findAllByText(/MID/);
}

async function unlockAndLand(user: UserSession, token: string = OPERATOR_TOKEN): Promise<void> {
  await unlock(user, token);
  await dataLanded();
}

beforeEach(() => {
  vi.resetAllMocks();
  installMatchMedia();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- token entry ------------------------------------------------------------

describe("AppShell — token entry", () => {
  it("offers exactly one field, paste-only, with the submit labeled Unlock", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);

    // A single field on the entry screen.
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    const field = screen.getByRole("textbox", { name: TOKEN_FIELD });

    // Paste-only: typed characters never land in the field.
    await user.click(field);
    await user.type(field, "abc123");
    expect(field).toHaveValue("");

    // Pasting populates it (the implementation owns the paste handling).
    await user.paste(OPERATOR_TOKEN);
    expect(field).toHaveValue(OPERATOR_TOKEN);

    expect(screen.getByRole("button", { name: "Unlock" })).toBeVisible();
  });

  it("keeps the token in memory only: never storage, never the URL, never the page", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.location.href).not.toContain(OPERATOR_TOKEN);
    expect(document.location.search).not.toContain(OPERATOR_TOKEN);
    expect(document.location.hash).not.toContain(OPERATOR_TOKEN);
    // The token itself is never echoed where an operator or a mirror could read it.
    expect(screen.queryByText(OPERATOR_TOKEN)).toBeNull();
  });

  it("surfaces the wrong-token error envelope verbatim and recovers on retry", async () => {
    const refusal = unauthorizedError();
    installClient({
      getSnapshot: () => Promise.reject(refusal),
      getHealth: () => Promise.reject(refusal),
    });
    const user = userEvent.setup();
    render(<AppShell />);

    await unlock(user, "not-the-right-token");

    // The refused attempt handed the shell that exact token.
    expect(createClientMock).toHaveBeenCalledWith("not-the-right-token");

    // The refusal is announced, and the envelope's code and message surface
    // together inside that one notice — never scattered across the page.
    const notice = await screen.findByRole("alert");
    expect(within(notice).getByText(/invalid_token/)).toBeVisible();
    expect(within(notice).getByText(/That access token is not valid\./)).toBeVisible();

    // No partial data renders alongside a refused unlock.
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText(/MID/)).toBeNull();

    // Retry with the correct token succeeds and clears the refusal message.
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    const client = {
      getSnapshot: vi.fn(() => Promise.resolve(snapshot)),
      getHealth: vi.fn(() => Promise.resolve(HEALTHY)),
      getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
      postIntent: vi.fn(),
      postArm: vi.fn(),
      postDisarm: vi.fn(),
      postEmergencyStop: vi.fn(),
      postStopAcknowledgement: vi.fn(),
      postInhibitAcknowledgement: vi.fn(),
      openEvents: vi.fn(() => channel.open()),
    };
    createClientMock.mockReturnValue(client as unknown as ApiClient);

    await unlock(user);
    await dataLanded();
    expect(createClientMock).toHaveBeenCalledWith(OPERATOR_TOKEN);
    expect(screen.queryByText(/That access token is not valid\./)).toBeNull();
  });

  it("sign-out clears the token and cached data and returns to the entry screen", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    await user.click(screen.getByRole("button", { name: /sign out|lock\b/i }));

    // Back on the entry screen, field empty, no cached fleet data.
    expect(screen.getByRole("button", { name: "Unlock" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: TOKEN_FIELD })).toHaveValue("");
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText(/MID/)).toBeNull();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("returns to the entry screen when any later call answers 401", async () => {
    const expired = new ApiClientError({
      status: 401,
      code: "token_expired",
      message: "The session token has expired.",
      details: null,
      request_id: "req-shell-2",
    });
    let snapshotCalls = 0;
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    installClient({
      getSnapshot: () => {
        snapshotCalls += 1;
        // The unlock-time read succeeds; a later refetch is refused with 401.
        return snapshotCalls === 1
          ? Promise.resolve(snapshot)
          : Promise.reject(expired);
      },
      openEvents: () => channel.open(),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    // A resync discontinuity forces the shell to refetch — that read 401s.
    channel.push({
      type: "resync_required",
      reason: "retention_window_exceeded",
      snapshot_sequence: 45,
    });

    // The session is dropped: entry screen again, no fleet data, no re-unlock.
    expect(await screen.findByRole("button", { name: "Unlock" })).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByText(/MID/)).toBeNull();
    });
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(createClientMock).toHaveBeenCalledTimes(1);
  });

  it("refetches the snapshot and reconnects with the recovery cursor after resync_required", async () => {
    const first = fleet(allUnits("disarmed"));
    const second = fleet(
      allUnits("active").map((entry) => ({
        ...entry,
        requested_power: { direction: "discharge" as const, watts: 900 },
        authorized_power: { direction: "discharge" as const, watts: 900 },
        measured_watts: 880,
      })),
      47,
    );
    let snapshotCalls = 0;
    const getSnapshot = vi.fn(() => {
      snapshotCalls += 1;
      return Promise.resolve(snapshotCalls === 1 ? first : second);
    });
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      if (connections === 1) {
        // The pinned client ends iteration right after the marker.
        return (async function* initial(): AsyncGenerator<StreamFrame, void, unknown> {
          yield snapshotFrame(first);
          yield {
            type: "resync_required",
            reason: "retention_window_exceeded",
            snapshot_sequence: 47,
          };
        })();
      }
      return (async function* reconnected(): AsyncGenerator<StreamFrame, void, unknown> {
        yield snapshotFrame(second);
        await new Promise<void>(() => {});
      })();
    });
    installClient({ snapshot: first, getSnapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expectVisibleText(banner(), "Disarmed");

    // A discontinuity triggers a snapshot refetch...
    await waitFor(() => {
      expect(getSnapshot).toHaveBeenCalledTimes(2);
    });
    // ...and a reconnect carrying the recovery cursor, never a replay from zero.
    await waitFor(() => {
      expect(openEvents.mock.calls.some((call) => call.at(0) === 47)).toBe(true);
    });
    // The refetched snapshot's world is what renders now; the stream continues.
    await waitFor(() => {
      expectVisibleText(banner(), "Active");
    });
  });

  it("shows a loading status while the first data is pending after unlock", async () => {
    installClient({
      getSnapshot: () => new Promise<FleetView>(() => {}),
      getHealth: () => new Promise<HealthView>(() => {}),
      openEvents: () => streamChannel([]).open(),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlock(user);

    const statuses = await screen.findAllByRole("status");
    expect(
      statuses.some((status) =>
        /loading|connecting|preparing|getting (your |the )?(fleet|status)/i.test(
          status.textContent ?? "",
        ),
      ),
    ).toBe(true);
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });
});

// --- fleet banner -----------------------------------------------------------

describe("AppShell — fleet banner", () => {
  it.each([
    { description: "observe only", units: allUnits("observe_only"), badge: "Observe only" },
    { description: "disarmed", units: allUnits("disarmed"), badge: "Disarmed" },
    { description: "armed", units: allUnits("armed_idle"), badge: "Armed" },
    {
      description: "active",
      units: allUnits("active").map((entry) => ({
        ...entry,
        requested_power: { direction: "discharge" as const, watts: 900 },
        authorized_power: { direction: "discharge" as const, watts: 900 },
        measured_watts: 880,
      })),
      badge: "Active",
    },
    {
      // One battery carrying a request no other battery shares: the 3,000 W
      // figure IS that battery's own target (a lone battery's fleet total is
      // its per-battery figure), and its 1,200 W allowance genuinely limits it.
      description: "limited",
      units: [
        unit({
          unit_id: "MID",
          lifecycle: "active",
          requested_power: { direction: "discharge", watts: 3000 },
          authorized_power: { direction: "discharge", watts: 1200 },
          measured_watts: 1150,
        }),
        unit({ unit_id: "RHS", lifecycle: "armed_idle" }),
        unit({ unit_id: "LHS", lifecycle: "armed_idle" }),
      ],
      badge: "Limited",
    },
    { description: "inhibited", units: allUnits("inhibited"), badge: "Inhibited" },
  ])(
    "shows the banner with the $description badge as label plus icon",
    async ({ units, badge }) => {
      installClient({ snapshot: fleet(units) });
      const user = userEvent.setup();
      render(<AppShell />);
      await unlockAndLand(user);

      const shellBanner = banner();
      expectVisibleText(shellBanner, new RegExp(badge, "i"));
      // Never color alone: the icon carries the state's accessible name too.
      expect(within(shellBanner).getByRole("img", { name: new RegExp(badge, "i") })).toBeVisible();
    },
  );

  it("shows the most conservative badge when units disagree", async () => {
    installClient({
      snapshot: fleet([
        unit({ unit_id: "MID", lifecycle: "inhibited" }),
        unit({ unit_id: "RHS", lifecycle: "disarmed" }),
        unit({ unit_id: "LHS", lifecycle: "armed_idle" }),
      ]),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expectVisibleText(banner(), "Inhibited");
  });

  it("prefers observe-only over an armed unit while units disagree", async () => {
    installClient({
      snapshot: fleet([
        unit({ unit_id: "MID", lifecycle: "observe_only" }),
        unit({ unit_id: "RHS", lifecycle: "armed_idle" }),
        unit({ unit_id: "LHS", lifecycle: "armed_idle" }),
      ]),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expectVisibleText(banner(), "Observe only");
    // The unit list disambiguates: another unit really is armed.
    expectVisibleText(document.body, /Armed/);
  });

  it("never presents an optimistic badge when the snapshot marks units disconnected or booting", async () => {
    installClient({
      snapshot: fleet([
        unit({ unit_id: "MID", lifecycle: "disconnected", telemetry_age_s: 37 }),
        unit({ unit_id: "RHS", lifecycle: "boot" }),
        unit({ unit_id: "LHS", lifecycle: "disarmed" }),
      ]),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const shellBanner = banner();
    // A unit with no contact is named, never swallowed by a calmer state word.
    expect(shellBanner.textContent ?? "").toMatch(/disconnect|unreachable|no contact|offline/i);
    // And the fleet is never presented as armed or active while a unit's
    // actual state is unknown ("Disarmed" stays legal; "Armed" does not).
    expect(shellBanner.textContent ?? "").not.toMatch(/\b(armed|active)\b/i);
    // The unit list disambiguates: another unit really is disarmed.
    expectVisibleText(document.body, "Disarmed");
  });

  // --- per-unit truth: the 2026-08-23 false-Limited defect family -------------
  //
  // The snapshot's per-unit `requested_power` repeats the intent's FLEET TOTAL
  // once per covered unit while `authorized_power` is genuinely per-unit, so a
  // banner that compares the two per battery stamps "Limited" on a dispatch
  // the safety system authorized in full. "Limited" may only ever be a
  // battery's OWN target versus its own allowance — from the shared per-unit
  // maps (bus-fed or, once it lands, the snapshot's `intent` block).

  /** Three active batteries repeating one intent's 3,000 W total, each
   * authorized its own 1,000 W: authorized in full, NOT limited. */
  function fleetTotalWorld(): FleetView {
    return fleet([
      unit({
        unit_id: "MID",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1000 },
        measured_watts: 990,
      }),
      unit({
        unit_id: "RHS",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1000 },
        measured_watts: 980,
      }),
      unit({
        unit_id: "LHS",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1000 },
        measured_watts: 970,
      }),
    ]);
  }

  it("never derives Limited from the snapshot's repeated fleet total: a 3 × 1,000 W dispatch authorized in full is Active", async () => {
    installClient({ snapshot: fleetTotalWorld() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    // The shared figure is the intent's total, not any battery's request: no
    // per-unit comparison exists, so the banner stays at the honest state.
    expectVisibleText(banner(), "Active");
    expect(within(banner()).queryByText("Limited")).toBeNull();
  });

  it("derives Limited from the decision's per-unit maps on the bus — the battery's own target versus its own allowance", async () => {
    const world = fleetTotalWorld();
    const channel = streamChannel([snapshotFrame(world)]);
    const client = {
      getSnapshot: vi.fn(() => Promise.resolve(world)),
      getHealth: vi.fn(() => Promise.resolve(HEALTHY)),
      getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
      postIntent: vi.fn(),
      postArm: vi.fn(),
      postDisarm: vi.fn(),
      postEmergencyStop: vi.fn(),
      postStopAcknowledgement: vi.fn(),
      postInhibitAcknowledgement: vi.fn(),
      openEvents: vi.fn(() => channel.open()),
    };
    createClientMock.mockReturnValue(client as unknown as ApiClient);
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expectVisibleText(banner(), "Active");

    // The kernel's cycle summary: each battery's own 1,000 W target, with the
    // site headroom clamping RHS to 400 W. The banner follows the bus-fed maps.
    channel.push({
      type: "audit.appended",
      sequence: 42,
      payload: {
        event_id: "facade-42",
        event_type: "control_decision",
        unit_id: null,
        generation: 9,
        result: "clamped",
        reason_codes: ["power_clamped"],
        requested_active_w: 3000,
        authorized_active_w: 2400,
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 400, LHS: 1000 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
      },
    });

    await waitFor(() => {
      expectVisibleText(banner(), "Limited");
    });
  });

  it("feature-detects the snapshot intent block: exact per-unit figures at cold load, Limited only from a genuine per-unit clamp", async () => {
    // The block's own figures are the truth at cold load: an authorized-in-full
    // request stays Active even though the snapshot's repeated total reads 3,000 W.
    const authorizedInFull: FleetView = {
      ...fleetTotalWorld(),
      intent: {
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
      },
    };
    installClient({ snapshot: authorizedInFull });
    const user = userEvent.setup();
    const first = render(<AppShell />);
    await unlockAndLand(user);
    expectVisibleText(banner(), "Active");
    first.unmount();

    // A genuine clamp named per battery by the block: RHS's own 1,000 W target
    // against its own 400 W allowance — that IS Limited, from per-unit truth.
    const clamped: FleetView = {
      ...authorizedInFull,
      units: authorizedInFull.units.map((entry) =>
        entry.unit_id === "RHS"
          ? { ...entry, authorized_power: { direction: "discharge", watts: 400 } }
          : entry,
      ),
      intent: {
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 400, LHS: 1000 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
      },
    };
    const channel = streamChannel([snapshotFrame(clamped)]);
    const client = {
      getSnapshot: vi.fn(() => Promise.resolve(clamped)),
      getHealth: vi.fn(() => Promise.resolve(HEALTHY)),
      getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
      postIntent: vi.fn(),
      postArm: vi.fn(),
      postDisarm: vi.fn(),
      postEmergencyStop: vi.fn(),
      postStopAcknowledgement: vi.fn(),
      postInhibitAcknowledgement: vi.fn(),
      openEvents: vi.fn(() => channel.open()),
    };
    createClientMock.mockReturnValue(client as unknown as ApiClient);
    const second = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(second);
    expectVisibleText(banner(), "Limited");
  });
});

// --- connection facts -------------------------------------------------------

describe("AppShell — connection facts", () => {
  it("reports page loaded, API reachable, event stream, and control as four separate facts", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const facts = [
      factItem(/page loaded/i),
      factItem(/API reachable/i),
      factItem(/event stream/i),
      factItem(/control/i),
    ];
    // Four distinct elements: the facts are never collapsed into one light.
    expect(new Set(facts).size).toBe(4);
    for (const fact of facts) {
      expect(fact).toBeVisible();
      expect(fact.textContent ?? "").toMatch(FACT_YES);
    }
  });

  it("keeps page loaded affirmative while the API is unreachable", async () => {
    installClient({
      getHealth: () => Promise.reject(networkError("The EnergyPod service could not be reached")),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expect(factItem(/API reachable/i).textContent ?? "").toMatch(FACT_NO);
    expect(factItem(/page loaded/i).textContent ?? "").toMatch(FACT_YES);
  });

  it("marks the event stream not live when the socket dies, and retries with the last sequence as cursor", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      // The first connection is healthy; every retry finds the server down,
      // so the operator's world stays verifiably disconnected.
      return connections === 1 ? channel.open() : unreachableStream();
    });
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.fail(networkError("The event stream connection was lost"));

    await waitFor(() => {
      expect(factItem(/event stream/i).textContent ?? "").toMatch(FACT_NO);
    });
    expect(factItem(/API reachable/i).textContent ?? "").toMatch(FACT_YES);
    // The socket retries automatically, carrying the last seen sequence.
    await waitFor(
      () => {
        const retries = openEvents.mock.calls.filter((_, index) => index > 0);
        expect(retries.some((call) => call.at(0) === SEQUENCE)).toBe(true);
      },
      { timeout: 4000 },
    );
  });

  it("surfaces the stream's own error envelope instead of swallowing it behind the retry loop", async () => {
    const snapshot = fleet(allUnits("disarmed"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    const streamFailure = new ApiClientError({
      status: 1011,
      code: "event_stream_error",
      message: "The event stream failed",
      details: null,
      request_id: "req-shell-9",
    });
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      // The first connection is healthy; every retry fails with the same
      // envelope the service sends in an `error` frame before closing.
      if (connections === 1) {
        return channel.open();
      }
      return (async function* failing(): AsyncGenerator<StreamFrame, void, unknown> {
        throw streamFailure;
      })();
    });
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.fail(streamFailure);

    // The envelope's code and message surface together, inside the one notice
    // the operator reads about live updates — never scattered, never silent.
    const notice = await screen.findByRole("region", { name: /live updates/i });
    expect(within(notice).getByText(/event_stream_error/)).toBeVisible();
    expect(within(notice).getByText(/The event stream failed/)).toBeVisible();
  });

  it("closes the authenticated event stream at sign-out instead of waiting for the next frame", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = instrumentedChannel([snapshotFrame(snapshot)]);
    const openEvents = vi.fn(() => channel.open());
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    const consumed = channel.state.frames;
    expect(consumed).toBeGreaterThan(0);
    const connectionsAtSignOut = openEvents.mock.calls.length;

    await user.click(screen.getByRole("button", { name: /sign out|lock\b/i }));
    const openEventsAfterSignOut = openEvents.mock.calls.length - connectionsAtSignOut;

    // The connection is returned now — the bus being quiet must not keep an
    // authenticated socket open past sign-out.
    await waitFor(() => {
      expect(channel.state.returnRequested).toBeGreaterThan(0);
    });
    // A frame pushed afterwards may settle the one read already in flight,
    // but nothing further is ever consumed from the retired connection.
    channel.push({ type: "audit.appended", sequence: 98, payload: { event_type: "x" } });
    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    const settled = channel.state.frames;
    channel.push({ type: "audit.appended", sequence: 99, payload: { event_type: "y" } });
    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    expect(channel.state.frames).toBe(settled);
    expect(openEventsAfterSignOut).toBe(0);
  });

  it("reports control not ready when the API says control is blocked, without optimism", async () => {
    const blocked: HealthView = {
      liveness: { ok: true },
      service_readiness: { ready: true, reasons: [] },
      control_readiness: { ready: false, reasons: ["MID:not_qualified"] },
    };
    installClient({ getHealth: () => Promise.resolve(blocked) });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expect(factItem(/control/i).textContent ?? "").toMatch(FACT_NO);
    // The other facts are unaffected: readiness is per-fact, not one light.
    expect(factItem(/page loaded/i).textContent ?? "").toMatch(FACT_YES);
    expect(factItem(/API reachable/i).textContent ?? "").toMatch(FACT_YES);
  });

  it("keeps the last fleet data visible with its age while the stream is down, and says so", async () => {
    const snapshot = fleet([
      unit({ unit_id: "MID", lifecycle: "disarmed", telemetry_age_s: 37 }),
      unit({ unit_id: "RHS", lifecycle: "disarmed" }),
      unit({ unit_id: "LHS", lifecycle: "disarmed" }),
    ]);
    const channel = streamChannel([snapshotFrame(snapshot)]);
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      return connections === 1 ? channel.open() : unreachableStream();
    });
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.fail(networkError("The event stream connection was lost"));

    // A designed, operator-visible disconnected notice exists apart from the
    // four-fact indicator: none of these words appear in the facts' own
    // wording, and the thrown envelope's text says nothing about retrying.
    await waitFor(() => {
      expectVisibleText(document.body, /disconnect|connection lost|offline/i);
    });
    await waitFor(() => {
      expectVisibleText(document.body, /reconnect|retry|trying again/i);
    });
    const notices = screen.getAllByText(/disconnect|connection lost|offline|reconnect/i);
    expect(notices.some((notice) => !connectionFacts().contains(notice))).toBe(true);

    // The last-known unit data is dimmed, never discarded — and its age shows.
    expectVisibleText(document.body, /MID/);
    expectVisibleText(document.body, /37/);
    await waitFor(() => {
      expectVisibleText(document.body, /\bage\b|\bold\b|\bago\b/i);
    });
  });
});

// --- announcements ----------------------------------------------------------

describe("AppShell — announcements", () => {
  it("announces a fleet status change politely through the live region", async () => {
    const before = fleet(allUnits("disarmed"));
    // The service's picture once MID is armed: the shell refetches the
    // snapshot on an authority change, so the next read carries the new world.
    const after = fleet(allUnits("armed_idle"), 43);
    const channel = streamChannel([snapshotFrame(before)]);
    installClient({ snapshots: [before, after], openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expectVisibleText(banner(), "Disarmed");

    // A real arming event from the service's published vocabulary.
    channel.push({
      type: "unit.armed",
      sequence: 42,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator-7",
        units: [{ unit_id: "MID", status: "armed", reason: "armed" }],
      },
    });

    await waitFor(() => {
      expectVisibleText(banner(), "Armed");
    });
    // The polite live region carries the change for non-visual operators. The
    // word-boundary means the stale "Disarmed" wording can never satisfy it.
    const announcements = screen.getByRole("status", { name: ANNOUNCEMENTS_NAME });
    expect(announcements.textContent ?? "").toMatch(/MID/);
    expect(announcements.textContent ?? "").toMatch(/\barmed\b/);
  });

  it("refetches the snapshot when a stop latches mid-session: the banner follows the fleet, not the connect-time picture", async () => {
    const before = fleet(allUnits("armed_idle"));
    // Wire truth after an emergency stop: the units are fenced into INHIBITED
    // and the snapshot sequence has moved on. Without the refetch the banner
    // would keep showing "Armed" for the rest of the session, because the
    // server sends its snapshot frame exactly once per connection.
    const after = fleet(allUnits("inhibited"), 46);
    const channel = streamChannel([snapshotFrame(before)]);
    const { getSnapshot } = installClient({
      snapshots: [before, after],
      openEvents: () => channel.open(),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expectVisibleText(banner(), "Armed");

    channel.push({
      type: "emergency_stop.latched",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:15Z",
      payload: {
        principal: "operator-7",
        stop_id: "stop-11",
        unit_ids: ["MID", "RHS", "LHS"],
        reason: "operator requested",
      },
    });

    // The assertive announcement still fires...
    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/stop/i);
    // ...and the picture itself moves: one refetch, then the real state.
    await waitFor(() => {
      expect(getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => {
      expectVisibleText(banner(), "Inhibited");
    });
  });

  it("never presents a unit the API refused to arm as armed", async () => {
    const before = fleet([
      unit({ unit_id: "MID", lifecycle: "disarmed" }),
      unit({ unit_id: "RHS", lifecycle: "disarmed" }),
    ]);
    // The facade publishes refused rows in unit.armed; the world the next
    // snapshot read returns still has RHS disarmed.
    const after = fleet(
      [
        unit({ unit_id: "MID", lifecycle: "armed_idle" }),
        unit({ unit_id: "RHS", lifecycle: "disarmed" }),
      ],
      43,
    );
    const channel = streamChannel([snapshotFrame(before)]);
    installClient({ snapshots: [before, after], openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.push({
      type: "unit.armed",
      sequence: 42,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator-7",
        units: [
          { unit_id: "MID", status: "armed", reason: "armed" },
          { unit_id: "RHS", status: "refused", reason: "not_qualified" },
        ],
      },
    });

    // MID's arming lands, RHS's refusal does not become an arming claim...
    await waitFor(() => {
      expect(banner().textContent ?? "").toMatch(/MID\s+—\s+Armed/);
    });
    expect(banner().textContent ?? "").toMatch(/RHS\s+—\s+Disarmed/);
    expect(banner().textContent ?? "").not.toMatch(/RHS\s+—\s+Armed/);
    // ...and the refusal itself is announced, politely and by reason.
    const announcements = screen.getByRole("status", { name: ANNOUNCEMENTS_NAME });
    await waitFor(() => {
      expect(announcements.textContent ?? "").toMatch(/RHS/);
    });
    expect(announcements.textContent ?? "").toMatch(/not armed/);
    expect(announcements.textContent ?? "").toMatch(/not_qualified/);
  });

  it("keeps routine authorization endings polite: only latched causes announce as inhibit", async () => {
    const before = fleet(allUnits("active"));
    const channel = streamChannel([snapshotFrame(before)]);
    installClient({ snapshots: [before], openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    // Intent expiry revokes authority on the same event type a latched inhibit
    // uses; the reason is the only discriminator the wire offers.
    channel.push({
      type: "authorization.revoked",
      sequence: 42,
      occurred_at: "2026-08-22T10:00:10Z",
      payload: { reason: "no_active_intent", unit_ids: ["MID"] },
    });

    const announcements = screen.getByRole("status", { name: ANNOUNCEMENTS_NAME });
    await waitFor(() => {
      expect(announcements.textContent ?? "").toMatch(/no_active_intent/);
    });
    // A routine revocation is never an emergency: nothing is announced as a
    // latch, so the assertive region keeps its meaning for real ones.
    expect(announcements.textContent ?? "").not.toMatch(/inhibit|latch|held/i);
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("announces a latched inhibit assertively", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    // The latched-inhibit path surfaces on the bus as a revoked authorization
    // whose reason is the latched cause (see header reconciliation pin 2).
    channel.push({
      type: "authorization.revoked",
      sequence: 43,
      occurred_at: "2026-08-22T10:00:10Z",
      payload: { reason: "blocking_fault_active", unit_ids: ["MID"] },
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/MID/);
    expect(alert.textContent ?? "").toMatch(/inhibit|latch|held|block|fault/i);
  });

  it("announces a latched stop assertively", async () => {
    const snapshot = fleet(allUnits("active"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.push({
      type: "emergency_stop.latched",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:15Z",
      payload: {
        principal: "operator-7",
        stop_id: "stop-7",
        unit_ids: ["RHS"],
        reason: "operator requested",
      },
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/RHS/);
    expect(alert.textContent ?? "").toMatch(/stop/i);
  });

  it("announces assertively when the stream is lost during active control", async () => {
    const snapshot = fleet([
      unit({
        unit_id: "MID",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 1500 },
        authorized_power: { direction: "discharge", watts: 1500 },
        measured_watts: 1480,
      }),
      unit({ unit_id: "RHS", lifecycle: "armed_idle" }),
      unit({ unit_id: "LHS", lifecycle: "armed_idle" }),
    ]);
    const channel = streamChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.fail(networkError("The event stream connection was lost"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/lost|disconnect|unavailable/i);
    expect(alert.textContent ?? "").toMatch(/MID|active/i);
  });
});

// --- emergency stop reachability ---------------------------------------------

describe("AppShell — emergency stop", () => {
  it("is reachable from views that display active control, with a focus-trapping confirmation", async () => {
    const stopMock = vi.fn((_unitIds: string[], _reason: string) =>
      Promise.resolve({ stop_id: "stop-9", units: [{ unit_id: "MID", status: "stopped" }] }),
    );
    installClient({
      snapshot: fleet([
        unit({
          unit_id: "MID",
          lifecycle: "active",
          requested_power: { direction: "discharge", watts: 1500 },
          authorized_power: { direction: "discharge", watts: 1500 },
          measured_watts: 1480,
        }),
        unit({ unit_id: "RHS", lifecycle: "armed_idle" }),
        unit({ unit_id: "LHS", lifecycle: "armed_idle" }),
      ]),
      postEmergencyStop: stopMock,
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    // Home displays active control, so a stop control is keyboard-reachable.
    const stop = stopControls().at(0)!;
    await tabTo(user, stop);
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog");
    // The confirmation names the affected units before anything happens.
    expect(dialog.textContent ?? "").toMatch(/MID/);
    // While the dialog is open, focus is trapped inside it.
    expect(dialog.contains(document.activeElement)).toBe(true);
    for (let cycle = 0; cycle < 3; cycle += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }

    // Escape declines: the dialog closes, focus returns to the stop control,
    // and no stop was requested.
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(stop).toHaveFocus();
    expect(stopMock).not.toHaveBeenCalled();

    // Confirming performs the stop for the affected units.
    await user.keyboard("{Enter}");
    const confirming = await screen.findByRole("dialog");
    await user.click(within(confirming).getByRole("button", { name: /confirm|stop/i }));
    await waitFor(() => {
      expect(stopMock).toHaveBeenCalledTimes(1);
    });
    const [affectedUnits] = stopMock.mock.calls.at(0)!;
    expect(affectedUnits).toContain("MID");

    // The same guarantee holds from the Now view, which initiates control.
    await user.click(screen.getByRole("link", { name: "Now" }));
    const nowStop = stopControls().at(0)!;
    await tabTo(user, nowStop);
    expect(nowStop).toBeVisible();
  });
});

// --- navigation -------------------------------------------------------------

describe("AppShell — navigation", () => {
  it("renders the available views as links and the unavailable ones as explicit not-available entries", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const nav = screen.getByRole("navigation");
    // Home is the default view.
    expect(within(nav).getByRole("link", { name: "Home", current: "page" })).toBeVisible();

    for (const name of ["Batteries", "Now", "Schedule", "Activity", "Insights"]) {
      expect(within(nav).getByRole("link", { name })).toBeVisible();
    }

    // Every not-yet-built view says so and is never a dead link. Schedule is
    // deliberately NOT here anymore: its nav link is always offered and the
    // view itself answers a not-commissioned deployment honestly (the pinned
    // decision in views.ts). Insights left this list the same way when the
    // energy scorecard's ledger landed — its link is always offered and the
    // view answers a not-commissioned deployment honestly.
    for (const name of ["Energy Flow", "Plan history"]) {
      expect(within(nav).queryByRole("link", { name: new RegExp(name, "i") })).toBeNull();
      const entry = within(nav).getByRole("listitem", { name: new RegExp(name, "i") });
      expectVisibleText(entry, /not available|coming soon|not yet/i);
    }

    expect(screen.getByRole("main")).toBeVisible();
  });

  it("navigates the shell by keyboard only, with visible focus and a current-view marker", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);

    // Keyboard-only unlock: Tab to the field, paste, Tab to Unlock, Enter.
    const field = screen.getByRole("textbox", { name: TOKEN_FIELD });
    await tabTo(user, field);
    await user.paste(OPERATOR_TOKEN);
    await tabTo(user, screen.getByRole("button", { name: "Unlock" }));
    await user.keyboard("{Enter}");
    await dataLanded();

    // The nav links are one Tab sequence, in reading order.
    const nav = screen.getByRole("navigation");
    const home = within(nav).getByRole("link", { name: "Home" });
    await tabTo(user, home);
    expect(home).toBeVisible();
    expect(home).toHaveFocus();

    await user.tab();
    const batteries = within(nav).getByRole("link", { name: "Batteries" });
    expect(batteries).toHaveFocus();
    expect(batteries).toBeVisible();

    await user.tab();
    const nowLink = within(nav).getByRole("link", { name: "Now" });
    expect(nowLink).toHaveFocus();
    expect(nowLink).toBeVisible();

    await user.tab();
    const scheduleLink = within(nav).getByRole("link", { name: "Schedule" });
    expect(scheduleLink).toHaveFocus();
    expect(scheduleLink).toBeVisible();

    await user.tab();
    const activity = within(nav).getByRole("link", { name: "Activity" });
    expect(activity).toHaveFocus();
    expect(activity).toBeVisible();

    // Enter activates the focused entry; the current view is marked.
    await user.keyboard("{Enter}");
    expect(screen.getByRole("link", { name: "Activity", current: "page" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Home", current: "page" })).toBeNull();
  });

  it("honors reduced motion: status changes still land in the live region, keyboard flows unaffected", async () => {
    installMatchMedia(true);
    const before = fleet(allUnits("disarmed"));
    const after = fleet(allUnits("armed_idle"), 43);
    const channel = streamChannel([snapshotFrame(before)]);
    installClient({ snapshots: [before, after], openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);

    // Keyboard flow works identically with reduced motion requested.
    const field = screen.getByRole("textbox", { name: TOKEN_FIELD });
    await tabTo(user, field);
    await user.paste(OPERATOR_TOKEN);
    await tabTo(user, screen.getByRole("button", { name: "Unlock" }));
    await user.keyboard("{Enter}");
    await dataLanded();
    expectVisibleText(banner(), "Disarmed");

    // The implementation must actually consult the operator's preference:
    // an implementation that ignores the media query cannot pass.
    const queries = matchMediaStub?.mock.calls ?? [];
    expect(queries.some(([query]) => /prefers-reduced-motion/.test(query))).toBe(true);

    channel.push({
      type: "unit.armed",
      sequence: 42,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator-7",
        units: [{ unit_id: "MID", status: "armed", reason: "armed" }],
      },
    });

    // No animated flow: the change is conveyed through the polite live region.
    await waitFor(() => {
      expectVisibleText(banner(), "Armed");
    });
    const announcements = screen.getByRole("status", { name: ANNOUNCEMENTS_NAME });
    expect(announcements.textContent ?? "").toMatch(/MID/);
    expect(announcements.textContent ?? "").toMatch(/\barmed\b/);
  });
});

// --- the live-data badge (self-diagnosis surface) -----------------------------

describe("AppShell — live-data badge", () => {
  /** The always-visible one-glance badge on the shell header. */
  function badge(): HTMLElement {
    return screen.getByLabelText("Live data");
  }

  it("shows Live once the connection delivers, on every view", async () => {
    installClient();
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expect(badge().textContent).toBe("Live");

    // The badge stays put when the operator moves to another view: it is
    // chrome, not part of any single view.
    await user.click(screen.getByRole("link", { name: "Batteries" }));
    expect(badge().textContent).toBe("Live");
  });

  it("moves Live → Reconnecting → Live through a drop and an automatic recovery", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    // Every connection drinks from the same channel: the first delivers the
    // picture, the failure ends it, the retry picks up the pushed frame.
    const openEvents = vi.fn(() => channel.open());
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expect(badge().textContent).toBe("Live");

    // The server drops the connection without warning.
    channel.fail(networkError("The event stream connection was lost"));
    await waitFor(() => {
      expect(badge().textContent).toBe("Reconnecting");
    });

    // The automatic retry reconnects; the reconnected stream delivers a fresh
    // authoritative snapshot frame and the badge returns to Live.
    channel.push(snapshotFrame(fleet(allUnits("disarmed"), SEQUENCE + 10)));
    await waitFor(
      () => {
        expect(badge().textContent).toBe("Live");
      },
      { timeout: 4000 },
    );
  });

  it("says Offline when the service itself cannot be reached", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      return connections === 1 ? channel.open() : unreachableStream();
    });
    installClient({
      snapshot,
      openEvents,
      getHealth: () => Promise.reject(networkError("The EnergyPod service could not be reached")),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.fail(networkError("The event stream connection was lost"));

    // The reconnects die at the transport and the health poll cannot reach
    // the service either: the badge names the service unreachable, never a
    // calm "live" over data that can no longer refresh.
    await waitFor(
      () => {
        expect(badge().textContent).toContain("Offline");
      },
      { timeout: 4000 },
    );
  });

  it("shows the calm controller-restart notice when a lost connection resumes", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      return connections === 1 ? channel.open() : channel.open();
    });
    installClient({ snapshot, openEvents });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);
    expect(screen.queryByText(/Connection restored after controller restart/)).toBeNull();

    channel.fail(networkError("The event stream connection was lost"));
    // Wait for the loss to be observed before arming the recovery frame: the
    // channel's queue drains ahead of its failure, so a frame pushed while the
    // dying generator is still parked would be consumed by the dead connection.
    await waitFor(() => {
      expect(badge().textContent).toBe("Reconnecting");
    });
    channel.push(snapshotFrame(fleet(allUnits("disarmed"), SEQUENCE + 10)));

    // The resume-reconnect is surfaced as a small non-blocking notice: the
    // operator can finally tell a restart from a stall.
    const notice = await screen.findByText(/Connection restored after controller restart/, undefined, {
      timeout: 4000,
    });
    expect(notice).toBeVisible();
    expect(badge().textContent).toBe("Live");
  });
});

// --- announcements are structural: the latch banner clears when it clears -----

describe("AppShell " + "—" + " latch announcements clear", () => {
  it("shows the assertive latch announcement and removes it when the stop is acknowledged", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = streamChannel([snapshotFrame(snapshot)]);
    installClient({ snapshots: [snapshot], openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    channel.push({
      type: "emergency_stop.latched",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:15Z",
      payload: {
        principal: "operator-7",
        stop_id: "stop-12",
        unit_ids: ["MID", "RHS", "LHS"],
        reason: "operator requested",
      },
    });

    // The announcement appears...
    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/stop/i);

    // ...and disappears when the latch is acknowledged: an acknowledged stop
    // must not linger in the alert region as a ghost.
    channel.push({
      type: "emergency_stop.acknowledged",
      sequence: 45,
      occurred_at: "2026-08-22T10:01:15Z",
      payload: { principal: "operator-7", stop_id: "stop-12" },
    });
    await waitFor(() => {
      expect(screen.queryAllByRole("alert")).toHaveLength(0);
    });
  });
});

// --- the emergency-stop latch banner (2026-08-23 incident fix) ---------------
//
// The defect: the release control rendered only in the session that pressed
// the stop, and the latch notice only if the console was connected at the
// latch moment — so a console opened mid-latch showed no way out. The banner
// is shell chrome rendered from the SNAPSHOT's active_stops (the initial read,
// every republished refresh, and the live-cadence poll), on every view, with
// the type-it-back release inline.

describe("AppShell — emergency-stop latch banner", () => {
  const STOP_ID = "stop-5-3753.297000";

  function stoppedFleet(snapshotSequence = 46): FleetView {
    return {
      ...fleet(allUnits("inhibited"), snapshotSequence),
      active_stops: [
        {
          stop_id: STOP_ID,
          latched_at: "2026-08-22T23:14:24Z",
          principal: "operator:home",
          reason_codes: ["operator_requested"],
          unit_ids: null,
        },
      ],
    };
  }

  /** The banner region: role=alert, named by its held-power statement. */
  function latchBanner(): HTMLElement {
    return screen.getByRole("alert", { name: /emergency stop active/i });
  }

  it("renders from the snapshot on load — no event, no session state — naming the stop, who, and when", async () => {
    // THE incident: the console was opened AFTER the stop was engaged. No
    // emergency_stop.latched frame ever arrives on this stream.
    const world = stoppedFleet();
    const channel = streamChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const banner = latchBanner();
    expectVisibleText(banner, /Emergency stop active — all battery power is held at 0 W/);
    expectVisibleText(banner, new RegExp(STOP_ID));
    expectVisibleText(banner, /operator:home/);
    expectVisibleText(banner, /23:14 UTC/);
    expectVisibleText(banner, /whole fleet/);

    // The release control is inline on the banner itself, not buried in Now.
    expect(within(banner).getByRole("textbox", { name: /stop id/i })).toBeVisible();
    expect(within(banner).getByRole("button", { name: /acknowledge/i })).toBeVisible();
  });

  it("unlocks the acknowledge button only when the typed id matches the stop id exactly", async () => {
    const world = stoppedFleet();
    const channel = streamChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const banner = latchBanner();
    const field = within(banner).getByRole("textbox", { name: /stop id/i });
    const acknowledge = within(banner).getByRole("button", { name: /acknowledge/i });
    expect(acknowledge).toBeDisabled();

    // Near misses — the deliberate friction stays. Case and content must
    // match; surrounding whitespace is tolerated (a pasted id often carries
    // a trailing newline).
    await user.type(field, STOP_ID.toUpperCase());
    expect(acknowledge).toBeDisabled();
    await user.clear(field);
    await user.type(field, `${STOP_ID}0`);
    expect(acknowledge).toBeDisabled();
    await user.clear(field);
    await user.type(field, ` ${STOP_ID} `);
    expect(acknowledge).toBeEnabled();

    await user.clear(field);
    await user.type(field, STOP_ID);
    expect(acknowledge).toBeEnabled();
  });

  it("acknowledges through the session client and clears the banner after the 200", async () => {
    const stopped = stoppedFleet();
    const released = { ...fleet(allUnits("disarmed"), 47), active_stops: [] };
    const acknowledge = vi.fn(() =>
      Promise.resolve({ stop_id: STOP_ID, status: "acknowledged" }),
    );
    const channel = streamChannel([snapshotFrame(stopped)]);
    installClient({
      snapshots: [stopped, released],
      openEvents: () => channel.open(),
      postStopAcknowledgement: acknowledge,
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    await user.type(within(latchBanner()).getByRole("textbox", { name: /stop id/i }), STOP_ID);
    await user.click(within(latchBanner()).getByRole("button", { name: /acknowledge/i }));

    // The exact stop id, through the shell's client.
    await waitFor(() => {
      expect(acknowledge).toHaveBeenCalledWith(STOP_ID);
    });

    // The banner comes down (optimistically after the 200, confirmed by the
    // refreshed snapshot whose active_stops no longer name the stop)...
    await waitFor(() => {
      expect(screen.queryByRole("alert", { name: /emergency stop active/i })).toBeNull();
    });
    // ...and the acknowledgement is announced politely.
    const announcements = screen.getByRole("status", { name: ANNOUNCEMENTS_NAME });
    expect(announcements.textContent ?? "").toMatch(
      new RegExp(`Emergency stop ${STOP_ID} acknowledged`),
    );
  });

  it("keeps the banner and surfaces the refusal envelope when the acknowledge fails", async () => {
    const world = stoppedFleet();
    const refusal = new ApiClientError({
      status: 409,
      code: "stop_not_latched",
      message: "That stop is no longer latched.",
      details: null,
      request_id: "req-ack-1",
    });
    const channel = streamChannel([snapshotFrame(world)]);
    installClient({
      snapshot: world,
      openEvents: () => channel.open(),
      postStopAcknowledgement: () => Promise.reject(refusal),
    });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    const banner = latchBanner();
    await user.type(within(banner).getByRole("textbox", { name: /stop id/i }), STOP_ID);
    await user.click(within(banner).getByRole("button", { name: /acknowledge/i }));

    // The envelope verbatim, inside the banner — and the banner stays: a
    // failed acknowledgement must never look like a release.
    await waitFor(() => {
      expect(within(banner).getByText(/stop_not_latched/)).toBeVisible();
    });
    expect(within(banner).getByText(/That stop is no longer latched\./)).toBeVisible();
    expect(latchBanner()).toBeVisible();
  });

  it("stays hidden and harmless while the snapshot carries no active_stops (today's backend)", async () => {
    // Feature detection: the field is absent, so the latch banner must not
    // render and the shell behaves exactly as before.
    const world = fleet(allUnits("inhibited"));
    expect(world.active_stops).toBeUndefined();
    const channel = streamChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: () => channel.open() });
    const user = userEvent.setup();
    render(<AppShell />);
    await unlockAndLand(user);

    expect(screen.queryByRole("alert", { name: /emergency stop active/i })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /stop id/i })).toBeNull();
    expect(screen.queryByText(/all battery power is held at 0 W/i)).toBeNull();
    // The rest of the shell is unaffected.
    expectVisibleText(banner(), "Inhibited");
    expect(factItem(/event stream/i).textContent ?? "").toMatch(FACT_YES);
  });
});
