/**
 * Behavior contract for the Home view (UI_CONTRACTS.md, "Home").
 *
 * The suite mocks the API client module at its exact surface (only
 * `createApiClient` is replaced; `ApiClientError` keeps the real class so
 * rejections carry the pinned client's TypedError shape). Wire fixtures mirror
 * what the service actually sends, per UI_CONTRACTS.md "Wire casing":
 *
 * - Enums are lowercase StrEnum values (service.py `_enum_value`): lifecycle
 *   "boot"/"observe_only"/"disarmed"/"armed_idle"/"active"/"inhibited"/
 *   "stopping"/"disconnected", direction "charge"/"discharge"/"idle". Badge
 *   labels shown to humans are capitalized; every wire literal is lowercase.
 * - Snapshot quality is the facade's projection (service.py
 *   `_quality_projection`): "good"/"degraded"/"bad"/"missing" — never "stale"
 *   or "suspect". Stale data arrives on the wire as quality "degraded" with
 *   `telemetry_age_s` past the freshness bound, and the stale-state test
 *   derives its fixture exactly that way.
 * - Health is the real nested shape `{liveness:{ok}, service_readiness:{ready,
 *   reasons}, control_readiness:{ready,reasons}}` (service.py `health`) with
 *   machine reason codes drawn from the strings the service actually emits
 *   ("pod-mid:inhibit_latched", "pod-lhs:not_qualified", "no_unit_armed").
 * - WS first frame `{type: "snapshot", sequence, data}`, then ordered event
 *   frames `{type, payload, sequence, occurred_at}` from the published
 *   vocabulary (unit.armed etc.); discontinuity frames `{type:
 *   "resync_required", reason, snapshot_sequence?}` carry no `sequence`, and
 *   the pinned client ends iteration right after one.
 * - Rejections at this boundary are `ApiClientError` instances (the client
 *   contract's TypedError pin), never bare envelope objects.
 *
 * Reconciliation pins for the primary:
 * (1) client.ts stub's `Health` interface types three flat strings while the
 *     real service returns the nested shape above; fixtures use the real shape
 *     and are injected through one cast until client.ts's owner corrects the
 *     interface.
 * (2) No pinned wire source carries intent expiry: the snapshot unit view
 *     (service.py `_unit_view`) carries lifecycle/telemetry_age_s/quality/
 *     requested_power/authorized_power/measured_watts, and the
 *     `intent.accepted` payload carries only direction/watts/unit_ids (plus
 *     ids). The "with expiry countdown" phrase in UI_CONTRACTS.md Home cannot
 *     be implemented from the pinned wire without fabricating a number, so it
 *     is deliberately NOT asserted here. Either the API grows an expiry field
 *     on the snapshot/intent frames or the phrase is dropped; until then an
 *     honest implementation shows direction and watts only.
 *
 * Contract pins for the implementer: five regions with accessible names /safe
 * and connected/i, /powering the home/i, /battery reserve|how full/i, /what
 * happens next/i, /limiting/i in that DOM order; per-unit entries are
 * listitems whose accessible name contains the unit id (exactly one per
 * region); reserve breakdown list named /per.?unit|breakdown/i; badge labels
 * exactly "Observe only"/"Disarmed"/"Armed"/"Active"/"Limited"/"Inhibited"
 * ("Limited" = active units with requested 3000 vs authorized 1200;
 * disagreement case: one inhibited among disarmed -> banner "Inhibited" +
 * per-unit labels disambiguate); requested/allowed/actual are three labeled
 * figures, each binding its own magnitude inside its own entry; next-action
 * region shows direction and watts while an intent is active and an explicit
 * none-statement when idle; limiting factors from health reasons AND snapshot
 * quality (bad unit named), plain language collapsed, raw code (e.g.
 * "pod-mid:inhibit_latched") revealed only after expanding via a Tab+Enter
 * keyboard-only flow; loading = role=status text, no unit data; empty fleet
 * explains what will appear + first step; disconnected = notice + last values
 * with age visible + automatic socket reconnect calling openEvents(42) (last
 * sequence as cursor); stale = age text next to the value, value stays
 * visible, stale/old named; partial = null telemetry/measured_watts render
 * "not available" naming the missing field, never 0 W; error state renders
 * envelope code+message verbatim ("internal_error"/"The request could not be
 * completed") with a keyboard-operable retry that recovers; refused path
 * renders "insufficient_scope"/"The credential lacks permission" verbatim
 * with no partial data; a unit status change arriving over the socket is
 * announced through a live region.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { HomeView } from "./HomeView";

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, createApiClient: vi.fn() };
});

const createClientMock = vi.mocked(createApiClient);

type UserSession = ReturnType<typeof userEvent.setup>;

// --- wire fixtures ---------------------------------------------------------

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
}

interface HealthReport {
  liveness: { ok: boolean };
  service_readiness: { ready: boolean; reasons: string[] };
  control_readiness: { ready: boolean; reasons: string[] };
}

interface StreamFrame {
  type: string;
  sequence?: number;
  [field: string]: unknown;
}

type StreamFactory = (afterSequence?: number) => AsyncIterable<StreamFrame>;

const SEQUENCE = 42;

function unit(partial: Partial<UnitView> & Pick<UnitView, "unit_id">): UnitView {
  return {
    lifecycle: "disarmed",
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
    site_id: "home",
    snapshot_sequence: snapshotSequence,
    captured_at: "2026-08-22T10:00:00Z",
    units,
  };
}

function allUnits(lifecycle: UnitView["lifecycle"]): UnitView[] {
  return [
    unit({ unit_id: "pod-mid", lifecycle }),
    unit({ unit_id: "pod-rhs", lifecycle }),
    unit({ unit_id: "pod-lhs", lifecycle }),
  ];
}

const healthyHealth: HealthReport = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: true, reasons: [] },
};

function snapshotFrame(snapshot: FleetView): StreamFrame {
  return { type: "snapshot", sequence: snapshot.snapshot_sequence, data: snapshot };
}

function liveStream(snapshot: FleetView): StreamFactory {
  return async function* live(): AsyncIterable<StreamFrame> {
    yield snapshotFrame(snapshot);
    // Stream stays open: no further frames, no error.
    await new Promise<void>(() => {});
  };
}

/** A stream that never delivers anything: used where no data may exist. */
function silentStream(): StreamFactory {
  return () =>
    (async function* silent(): AsyncGenerator<StreamFrame, void, unknown> {
      await new Promise<void>(() => {});
    })();
}

function failingStream(snapshot: FleetView): StreamFactory {
  return async function* failing(): AsyncIterable<StreamFrame> {
    yield snapshotFrame(snapshot);
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    });
    // An opaque transport error: any visible disconnected wording must come
    // from a designed notice, not from leaking this message.
    throw new Error("socket boom");
  };
}

/** A controllable live stream: yields the initial frames, then pushed frames. */
function liveChannel(initial: StreamFrame[]): {
  openEvents: StreamFactory;
  push(frame: StreamFrame): void;
} {
  const queue: StreamFrame[] = [...initial];
  let wake: (() => void) | null = null;
  const notify = (): void => {
    const release = wake;
    wake = null;
    release?.();
  };
  return {
    openEvents: () =>
      (async function* channel(): AsyncIterable<StreamFrame> {
        while (true) {
          while (queue.length > 0) {
            const next = queue.shift();
            if (next !== undefined) {
              yield next;
              // The pinned client ends iteration right after the marker.
              if (next.type === "resync_required") {
                return;
              }
            }
          }
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
        }
      })(),
    push: (frame) => {
      queue.push(frame);
      notify();
    },
  };
}

interface ClientSetup {
  snapshot?: FleetView;
  getSnapshot?: () => Promise<FleetView>;
  getHealth?: () => Promise<HealthReport>;
  openEvents?: StreamFactory;
}

/** Builds the mocked client; the cast only bridges this suite's local wire types. */
function installClient(setup: ClientSetup = {}): ApiClient {
  const snapshot = setup.snapshot ?? fleet(allUnits("armed_idle"));
  const client = {
    getSnapshot: vi.fn(setup.getSnapshot ?? (() => Promise.resolve(snapshot))),
    getHealth: vi.fn(setup.getHealth ?? (() => Promise.resolve(healthyHealth))),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    postIntent: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    postArm: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    postDisarm: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    postEmergencyStop: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    postStopAcknowledgement: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(new Error("not used by HomeView"))),
    openEvents: vi.fn(setup.openEvents ?? liveStream(snapshot)),
  };
  const typedClient = client as unknown as ApiClient;
  createClientMock.mockReturnValue(typedClient);
  return typedClient;
}

function renderHome() {
  render(<HomeView client={createApiClient("operator-token")} />);
}

// --- query helpers ---------------------------------------------------------

const SAFE_REGION = /safe and connected/i;
const POWER_REGION = /powering the home/i;
const RESERVE_REGION = /battery reserve|how full/i;
const NEXT_REGION = /what happens next/i;
const LIMITING_REGION = /limiting/i;

const QUESTION_REGIONS = [SAFE_REGION, POWER_REGION, RESERVE_REGION, NEXT_REGION, LIMITING_REGION];

function questionRegions(): HTMLElement[] {
  return QUESTION_REGIONS.map((name) => screen.getByRole("region", { name }));
}

function expectInDomOrder(elements: HTMLElement[]) {
  for (let index = 1; index < elements.length; index += 1) {
    const earlier = elements.at(index - 1);
    const later = elements.at(index);
    expect(earlier).toBeDefined();
    expect(later).toBeDefined();
    expect(Boolean(earlier!.compareDocumentPosition(later!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(
      true,
    );
  }
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

async function findUnitEntry(regionName: RegExp, unitId: string): Promise<HTMLElement> {
  const region = await screen.findByRole("region", { name: regionName });
  const entries = within(region).getAllByRole("listitem", { name: new RegExp(unitId, "i") });
  expect(entries).toHaveLength(1);
  return entries.at(0)!;
}

/**
 * The one labeled figure (role group or figure) for a power label inside an
 * entry. Requested/allowed/actual each get their own named container so a
 * magnitude can never be presented under a sibling's label.
 */
function labeledFigure(entry: HTMLElement, label: RegExp): HTMLElement {
  const named = [
    ...within(entry).queryAllByRole("group", { name: label }),
    ...within(entry).queryAllByRole("figure", { name: label }),
  ];
  expect(named).toHaveLength(1);
  return named.at(0)!;
}

async function tabTo(user: UserSession, element: HTMLElement): Promise<void> {
  for (let attempt = 0; attempt < 40 && document.activeElement !== element; attempt += 1) {
    await user.tab();
  }
  expect(document.activeElement).toBe(element);
}

async function dataLanded() {
  await screen.findAllByText(/pod-mid/i);
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("HomeView", () => {
  it("answers the five questions in order on a healthy render", async () => {
    installClient();
    renderHome();
    await dataLanded();

    const regions = questionRegions();
    expectInDomOrder(regions);

    // Question 1 carries the fleet badge; armed and healthy.
    expectVisibleText(regions.at(0)!, "Armed");

    // Question 2: per-unit power entries for every named unit.
    const powerRegion = regions.at(1)!;
    for (const unitId of ["pod-mid", "pod-rhs", "pod-lhs"]) {
      const entries = within(powerRegion).getAllByRole("listitem", {
        name: new RegExp(unitId, "i"),
      });
      expect(entries).toHaveLength(1);
    }

    // Question 3: fleet reserve with a per-unit breakdown naming each unit.
    const breakdown = within(regions.at(2)!).getByRole("list", {
      name: /per.?unit|breakdown/i,
    });
    for (const unitId of ["pod-mid", "pod-rhs", "pod-lhs"]) {
      expect(
        within(breakdown).getByRole("listitem", { name: new RegExp(unitId, "i") }),
      ).toBeVisible();
    }

    // Question 4: nothing planned while no intent is active.
    expectVisibleText(
      regions.at(3)!,
      /no (planned )?action|nothing planned|nothing scheduled|no active (intent|action|plan)/i,
    );

    // Question 5: calm "nothing limiting" statement in a healthy fleet.
    expectVisibleText(regions.at(4)!, /nothing|no active limits|operating normally/i);
  });

  it("binds requested, allowed, and actual each to its own magnitude so a limited action is never shown as delivered", async () => {
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1200 },
        measured_watts: 1150,
      }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    // Three separately labeled facts, textual as well as visual.
    expectVisibleText(entry, /requested/i);
    expectVisibleText(entry, /allowed/i);
    expectVisibleText(entry, /actual/i);
    // Each magnitude is bound to its own label inside its own figure:
    // rendering the allowed figure under the requested label cannot pass.
    expect(labeledFigure(entry, /requested/i).textContent ?? "").toMatch(/3,?000/);
    expect(labeledFigure(entry, /allowed/i).textContent ?? "").toMatch(/1,?200/);
    expect(labeledFigure(entry, /actual/i).textContent ?? "").toMatch(/1,?150/);
    // The gap itself is named in plain language, bound to the authorized
    // magnitude — the word "limit" alone is not enough.
    expect(entry.textContent ?? "").toMatch(
      /(?:limit\w*|reduc\w*|holding back|less than requested)[^\d]{0,40}1,?200/i,
    );
  });

  it("shows the next planned action's direction and watts while an intent is active", async () => {
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        requested_power: { direction: "charge", watts: 1500 },
        authorized_power: { direction: "charge", watts: 1500 },
        measured_watts: 1480,
      }),
      unit({
        unit_id: "pod-rhs",
        lifecycle: "active",
        requested_power: { direction: "charge", watts: 1500 },
        authorized_power: { direction: "charge", watts: 1500 },
        measured_watts: 1495,
      }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const nextRegion = await screen.findByRole("region", { name: NEXT_REGION });
    expectVisibleText(nextRegion, /charg/i);
    expectVisibleText(nextRegion, /1,?500/);
    // No pinned wire source carries intent expiry (header reconciliation pin
    // 2), so nothing here demands a countdown an honest view cannot compute.
  });

  it("lists limiting factors in plain language and expands to the raw reason code by keyboard only", async () => {
    const health: HealthReport = {
      liveness: { ok: true },
      service_readiness: { ready: true, reasons: [] },
      control_readiness: {
        ready: false,
        reasons: ["pod-mid:inhibit_latched", "pod-lhs:not_qualified"],
      },
    };
    const snapshot = fleet([
      unit({ unit_id: "pod-mid", lifecycle: "inhibited" }),
      unit({ unit_id: "pod-rhs", quality: "bad" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot, getHealth: () => Promise.resolve(health) });
    renderHome();

    const region = await screen.findByRole("region", { name: LIMITING_REGION });
    const factors = within(region).getAllByRole("listitem");
    expect(factors.length).toBeGreaterThanOrEqual(2);

    const latched = factors.find((factor) => /pod-mid/i.test(factor.textContent ?? ""));
    expect(latched).toBeDefined();
    if (!latched) throw new Error("pod-mid limiting factor missing");

    // Plain language first: the raw code is not visible before expanding.
    expectVisibleText(latched, /latch|hold|block|safety/i);
    expect(within(latched).queryByText("pod-mid:inhibit_latched")).toBeNull();

    // Snapshot quality contributes its own factor, naming the unit.
    expectVisibleText(region, /pod-rhs/);

    // Keyboard-only: reach the control with Tab and activate with Enter.
    const user = userEvent.setup();
    const reveal = within(latched).getByRole("button", {
      name: /detail|more|raw|code|why|explain/i,
    });
    await tabTo(user, reveal);
    await user.keyboard("{Enter}");
    expect(within(latched).getByText("pod-mid:inhibit_latched")).toBeVisible();
  });

  describe("fleet badge", () => {
    it.each([
      {
        description: "observe only",
        units: allUnits("observe_only"),
        badge: "Observe only",
      },
      {
        description: "disarmed",
        units: allUnits("disarmed"),
        badge: "Disarmed",
      },
      {
        description: "armed",
        units: allUnits("armed_idle"),
        badge: "Armed",
      },
      {
        description: "active",
        units: allUnits("active").map((entry): UnitView => ({
          ...entry,
          requested_power: { direction: "discharge", watts: 900 },
          authorized_power: { direction: "discharge", watts: 900 },
          measured_watts: 890,
        })),
        badge: "Active",
      },
      {
        description: "limited",
        units: [
          unit({
            unit_id: "pod-mid",
            lifecycle: "active",
            requested_power: { direction: "discharge", watts: 3000 },
            authorized_power: { direction: "discharge", watts: 1200 },
            measured_watts: 1150,
          }),
          unit({
            unit_id: "pod-rhs",
            lifecycle: "active",
            requested_power: { direction: "discharge", watts: 3000 },
            authorized_power: { direction: "discharge", watts: 1200 },
            measured_watts: 1120,
          }),
          unit({ unit_id: "pod-lhs", lifecycle: "armed_idle" }),
        ],
        badge: "Limited",
      },
    ])("shows the $description badge", async ({ units, badge }) => {
      installClient({ snapshot: fleet(units) });
      renderHome();
      await dataLanded();
      const banner = screen.getByRole("region", { name: SAFE_REGION });
      expectVisibleText(banner, badge);
    });

    it("shows the most conservative badge when units disagree and the unit list disambiguates", async () => {
      const snapshot = fleet([
        unit({ unit_id: "pod-mid", lifecycle: "inhibited" }),
        unit({ unit_id: "pod-rhs", lifecycle: "disarmed" }),
        unit({ unit_id: "pod-lhs", lifecycle: "disarmed" }),
      ]);
      installClient({ snapshot });
      renderHome();
      await dataLanded();

      expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Inhibited");

      const inhibited = await findUnitEntry(POWER_REGION, "pod-mid");
      expectVisibleText(inhibited, "Inhibited");
      const healthy = await findUnitEntry(POWER_REGION, "pod-rhs");
      expectVisibleText(healthy, "Disarmed");
    });
  });

  it("shows a loading status without unit data while the first snapshot is pending", () => {
    // The socket never delivers a frame either: "no unit data" is asserted in
    // a world where no data exists at all.
    installClient({
      getSnapshot: () => new Promise<FleetView>(() => {}),
      getHealth: () => new Promise<HealthReport>(() => {}),
      openEvents: silentStream(),
    });
    renderHome();

    const statuses = screen.getAllByRole("status");
    expect(
      statuses.some((status) => /loading|updating|preparing/i.test(status.textContent ?? "")),
    ).toBe(true);
    expect(screen.queryByText(/pod-mid/i)).toBeNull();
  });

  it("explains what will appear here and what to do first when the fleet has no units", async () => {
    const health: HealthReport = {
      liveness: { ok: true },
      service_readiness: { ready: true, reasons: [] },
      control_readiness: { ready: false, reasons: ["no_unit_armed"] },
    };
    installClient({ snapshot: fleet([]), getHealth: () => Promise.resolve(health) });
    renderHome();

    const reserveRegion = await screen.findByRole("region", { name: RESERVE_REGION });
    expectVisibleText(reserveRegion, /no (batter|unit)|nothing (to show|here)|will appear/i);
    expectVisibleText(reserveRegion, /first|connect|add|enrol|wait/i);

    const powerRegion = await screen.findByRole("region", { name: POWER_REGION });
    expectVisibleText(powerRegion, /no (batter|unit)|nothing (to show|here)|will appear/i);
  });

  it("shows a disconnected notice, keeps the last values with their age, and reconnects from the last sequence", async () => {
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        telemetry_age_s: 45,
        requested_power: { direction: "discharge", watts: 1200 },
        authorized_power: { direction: "discharge", watts: 1200 },
        measured_watts: 1150,
      }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    const openEvents = vi.fn(failingStream(snapshot));
    installClient({ snapshot, openEvents });
    renderHome();
    await dataLanded();

    // The stream's own error message ("socket boom") cannot satisfy this: the
    // wording must come from a designed disconnected notice.
    await screen.findAllByText(/disconnect|connection lost|reconnect/i);

    // Last data is dimmed but not hidden: value and its age stay visible.
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /45/);
    expectVisibleText(entry, /1,?150/);

    // The socket retries automatically with the last seen sequence as cursor.
    await waitFor(
      () => {
        expect(openEvents.mock.calls.some((call) => call.at(0) === SEQUENCE)).toBe(true);
      },
      { timeout: 4000 },
    );
  });

  it("refetches the snapshot and reconnects with the recovery cursor after resync_required", async () => {
    const first = fleet(allUnits("disarmed"));
    const second = fleet(
      [
        unit({
          unit_id: "pod-mid",
          lifecycle: "active",
          requested_power: { direction: "discharge", watts: 1500 },
          authorized_power: { direction: "discharge", watts: 1500 },
          measured_watts: 1490,
        }),
        unit({ unit_id: "pod-rhs" }),
        unit({ unit_id: "pod-lhs" }),
      ],
      45,
    );
    let snapshotCalls = 0;
    const channel = liveChannel([
      snapshotFrame(first),
      { type: "resync_required", reason: "retention_window_exceeded", snapshot_sequence: 45 },
    ]);
    const openEvents = vi.fn(channel.openEvents);
    installClient({
      snapshot: first,
      getSnapshot: () => {
        snapshotCalls += 1;
        return Promise.resolve(snapshotCalls === 1 ? first : second);
      },
      openEvents,
    });
    renderHome();
    await dataLanded();

    // The discontinuity triggers a snapshot refetch...
    await waitFor(() => {
      expect(snapshotCalls).toBe(2);
    });
    // ...and a reconnect carrying the recovery cursor, never a replay from zero.
    await waitFor(() => {
      expect(openEvents.mock.calls.some((call) => call.at(0) === 45)).toBe(true);
    });
    // The refetched snapshot's world is what renders now.
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, /1,?490/);
    });
  });

  it("announces a unit status change arriving over the socket through a live region", async () => {
    const snapshot = fleet(allUnits("disarmed"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    const openEvents = vi.fn(channel.openEvents);
    installClient({ snapshot, openEvents });
    renderHome();
    await dataLanded();

    // A real arming event from the service's published vocabulary.
    channel.push({
      type: "unit.armed",
      sequence: 43,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator-7",
        units: [{ unit_id: "pod-mid", status: "armed", reason: "armed" }],
      },
    });

    // The change reaches non-visual operators: a live region (polite status or
    // assertive alert) names the unit and its new state. The word boundary
    // means the stale "Disarmed" wording can never satisfy it.
    await waitFor(() => {
      const live = [...screen.getAllByRole("status"), ...screen.getAllByRole("alert")];
      expect(
        live.some(
          (region) =>
            /pod-mid/i.test(region.textContent ?? "") && /\barmed\b/i.test(region.textContent ?? ""),
        ),
      ).toBe(true);
    });
  });

  it("shows the age next to stale values and keeps them visible rather than hiding them", async () => {
    // Stale data on the wire: quality stays "degraded" while telemetry_age_s
    // passes the freshness bound (the service never emits a "stale" quality).
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "armed_idle",
        telemetry_age_s: 95,
        quality: "degraded",
        measured_watts: 800,
      }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /95/);
    expectVisibleText(entry, /800/);
    expectVisibleText(entry, /stale|old|aged/i);
  });

  it("names the missing fields for a unit with partial telemetry instead of inventing values", async () => {
    const snapshot = fleet([
      unit({ unit_id: "pod-mid" }),
      unit({ unit_id: "pod-rhs", telemetry_age_s: null, measured_watts: null, quality: "missing" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-rhs");
    expectVisibleText(entry, /actual/i);
    expectVisibleText(entry, /not available|unavailable|no reading|missing/i);
    expectVisibleText(entry, /telemetry|measur|reading/i);

    // The reserve breakdown never invents a charge level the API does not send.
    const reserveRegion = await screen.findByRole("region", { name: RESERVE_REGION });
    expectVisibleText(reserveRegion, /not available|unavailable|no (charge|reading|level)/i);
    const breakdown = within(reserveRegion).getByRole("list", { name: /per.?unit|breakdown/i });
    expect(within(breakdown).queryByText(/%/)).toBeNull();
  });

  it("renders the error envelope verbatim with a keyboard-operable retry and recovers", async () => {
    const failure = new ApiClientError({
      status: 500,
      code: "internal_error",
      message: "The request could not be completed",
      details: null,
      request_id: "req-7",
    });
    let attempts = 0;
    // The socket never delivers a frame: "no unit data" is asserted in a
    // world where no data exists.
    installClient({
      getSnapshot: () => {
        attempts += 1;
        return attempts === 1
          ? Promise.reject(failure)
          : Promise.resolve(fleet(allUnits("armed_idle")));
      },
      openEvents: silentStream(),
    });
    renderHome();

    // The envelope arrives with the rejected promise — one microtask after
    // render, beyond React's synchronous act flush — so the first assertion
    // yields to it (the pre-repair form awaited findAllByText here; the
    // silentStream fixture keeps "no unit data" true while it waits).
    expect(await screen.findByText(/internal_error/)).toBeVisible();
    expect(screen.getByText(/The request could not be completed/)).toBeVisible();
    expect(screen.queryByText(/pod-mid/i)).toBeNull();

    const user = userEvent.setup();
    const retry = screen.getByRole("button", { name: /retry|try again/i });
    await tabTo(user, retry);
    await user.keyboard("{Enter}");
    await dataLanded();
  });

  it("surfaces a refused read's error envelope code and message verbatim", async () => {
    const refused = new ApiClientError({
      status: 403,
      code: "insufficient_scope",
      message: "The credential lacks permission",
      details: null,
      request_id: "req-9",
    });
    installClient({
      getSnapshot: () => Promise.reject(refused),
      getHealth: () => Promise.reject(refused),
      openEvents: silentStream(),
    });
    renderHome();

    // Same timing reconciliation as the internal_error case above: the
    // refusal is only observable after the rejected promise's microtask.
    expect(await screen.findByText(/insufficient_scope/)).toBeVisible();
    expect(screen.getByText(/The credential lacks permission/)).toBeVisible();
    // No partial data renders alongside a refusal.
    expect(screen.queryByText(/pod-mid/i)).toBeNull();
    expect(screen.getByRole("button", { name: /retry|try again/i })).toBeVisible();
  });
});
