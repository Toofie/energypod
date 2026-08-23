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
 * ("Limited" derives ONLY from per-unit figures — the authorized map below
 * the battery's own requested target; a snapshot whose per-unit requested
 * figures repeat the fleet total never yields "Limited" — see the per-unit
 * watt figures describe below; disagreement case: one inhibited among
 * disarmed -> banner "Inhibited" + per-unit labels disambiguate);
 * requested/allowed/actual are three labeled
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
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { ADVISER_REASON_CODES } from "../../app/fleet";
import { NIGHT_REASON_CODES } from "../../app/nightCharge";
import { toScheduleState } from "../../app/schedule";
import { NextScheduleCard } from "./NextScheduleCard";
import {
  actuationIncoherent,
  adviserState,
  energyDayRecord,
  energyDayRolled,
  energyToday,
  excessAdviserStateChanged,
  excessChargingToggleOk,
  getScheduleOk,
  nightChargeState,
  nightChargeStateChanged,
  nightChargingToggleOk,
  nightUnitState,
  scheduleEntry,
  schedulePlan,
  schedulePolicy,
  scheduleReplaced,
  scheduleState as wireScheduleState,
  scheduleWindowClosing,
  scheduleWindowOpened,
  telemetrySummary,
  unitHealthChanged,
  unitUnexpectedAutonomy,
  foreignObjectiveObserved,
  lastObjectiveObserved,
  type WireAdviserState,
  type WireLastObjective,
  type WireEnergyToday,
  type WireNightChargeState,
  type WireScheduleState,
  type WireTelemetrySummary,
} from "../../test/wire";
import { HomeView, HEALTH_POLL_MS } from "./HomeView";

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
  /** The amended snapshot contract's nullable telemetry block (wire.ts). */
  telemetry?: WireTelemetrySummary | null;
  /**
   * The self-healing awareness layer's derived per-unit fields (wire.ts
   * `withHealth`): absent from the older wire — the feature detection the
   * unit line's health badge keys on.
   */
  health_state?: string | null;
  health_reasons?: readonly string[] | null;
  remediation_hint?: string | null;
  /**
   * The night-writer detector's per-unit summary (wire.ts `withObjective`):
   * absent from the older wire — the feature detection the unit line's quiet
   * foreign-objective note keys on.
   */
  last_objective_observed?: WireLastObjective | null;
}

interface FleetView {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: UnitView[];
  /**
   * The excess-solar adviser projection (PENDING-BACKEND, feature-detected):
   * absent from today's wire — the default fixture omits it exactly like the
   * backend does; solar-surplus tests attach it explicitly.
   */
  adviser_state?: WireAdviserState;
  /**
   * The schedules projection (PENDING-BACKEND, feature-detected): absent from
   * today's wire by default; schedule-card tests attach it explicitly.
   */
  schedule_state?: WireScheduleState;
  /**
   * The night-charge projection (PENDING-BACKEND, feature-detected): absent
   * from today's wire by default; night-tile tests attach it explicitly.
   */
  night_charge_state?: WireNightChargeState;
  /**
   * The energy scorecard's live-day block (PENDING-BACKEND, feature-detected):
   * absent from today's wire by default; Today-card tests attach it explicitly.
   */
  energy_today?: WireEnergyToday;
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
  postExcessCharging?: ApiClient["postExcessCharging"];
  postNightCharging?: ApiClient["postNightCharging"];
  getSchedule?: ApiClient["getSchedule"];
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
    postExcessCharging: vi.fn(
      setup.postExcessCharging ??
        (() => Promise.reject(new Error("not used by HomeView"))),
    ),
    postNightCharging: vi.fn(
      setup.postNightCharging ??
        (() => Promise.reject(new Error("not used by HomeView"))),
    ),
    // The schedules facts read: answered only when the card is composed (the
    // view never reads it otherwise); the default carries the day-only policy
    // and no plan, and tests override it per state.
    getSchedule: setup.getSchedule ?? vi.fn(() => Promise.resolve(getScheduleOk({}))),
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
    // The gap itself is named in plain language: the note says "Limited",
    // names the battery, and binds BOTH magnitudes to it. This fixture's lone
    // active battery carries its own figure (a lone battery's fleet total IS
    // its per-battery figure), so the comparison is per-unit here.
    expect(entry.textContent ?? "").toMatch(
      /limited — pod-mid requested 3,?000 W but was allowed 1,?200 W/i,
    );
    expect(entry.textContent ?? "").toMatch(/holding back part of the request/i);
  });

  it("derives the fleet reserve from per-unit state of charge: the pinned average over reporting pods", async () => {
    // The 2026-08-22 capture decode: MID 10 %, RHS 68 %, LHS 48 % — the fleet
    // figure is their average (42 %), each row naming its own pod's reading.
    const snapshot = fleet([
      unit({ unit_id: "pod-mid", telemetry: telemetrySummary({ soc_pct: 10 }) }),
      unit({ unit_id: "pod-rhs", telemetry: telemetrySummary({ soc_pct: 68 }) }),
      unit({ unit_id: "pod-lhs", telemetry: telemetrySummary({ soc_pct: 48 }) }),
    ]);
    installClient({ snapshot });
    renderHome();

    const reserveRegion = await screen.findByRole("region", { name: RESERVE_REGION });
    expectVisibleText(reserveRegion, /Fleet charge level: 42% on average across all pods/i);

    const breakdown = within(reserveRegion).getByRole("list", {
      name: /per.?unit|breakdown/i,
    });
    expect(
      within(breakdown).getByRole("listitem", { name: /pod-mid charge level/i }),
    ).toHaveTextContent(/10% charged/);
    expect(
      within(breakdown).getByRole("listitem", { name: /pod-rhs charge level/i }),
    ).toHaveTextContent(/68% charged/);
    expect(
      within(breakdown).getByRole("listitem", { name: /pod-lhs charge level/i }),
    ).toHaveTextContent(/48% charged/);
  });

  it("averages only the pods reporting a charge reading and names the silent pod's gap", async () => {
    const snapshot = fleet([
      unit({ unit_id: "pod-mid", telemetry: telemetrySummary({ soc_pct: 10 }) }),
      unit({ unit_id: "pod-rhs", telemetry: telemetrySummary({ soc_pct: 68 }) }),
      // No observation: a silent pod is not an empty battery — it contributes
      // nothing to the average and its row stays honestly not-available.
      unit({ unit_id: "pod-lhs", telemetry: null, measured_watts: null }),
    ]);
    installClient({ snapshot });
    renderHome();

    const reserveRegion = await screen.findByRole("region", { name: RESERVE_REGION });
    expectVisibleText(
      reserveRegion,
      /Fleet charge level: 39% on average across the 2 pods reporting a charge reading/i,
    );
    const breakdown = within(reserveRegion).getByRole("list", {
      name: /per.?unit|breakdown/i,
    });
    const silent = within(breakdown).getByRole("listitem", { name: /pod-lhs charge level/i });
    expect(silent).toHaveTextContent(/charge level not available/i);
    expect(silent.textContent ?? "").not.toMatch(/%/);
  });

  it("renders every figure at the two-decimal display bound: a raw eight-decimal fixture never reaches the operator", async () => {
    // The wire can carry float-decoded telemetry at full precision; the
    // display bound (src/lib/format.ts) is the render boundary — at most two
    // decimals, integers as integers, trailing zeros trimmed — so the view's
    // three watt figures and the charge percentages never show more.
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 1234.56789012 },
        authorized_power: { direction: "discharge", watts: 1000.98765432 },
        measured_watts: 980.12345678,
        telemetry: telemetrySummary({ soc_pct: 46.55555555 }),
      }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expect(labeledFigure(entry, /requested/i).textContent ?? "").toContain("1,234.57 W");
    expect(labeledFigure(entry, /allowed/i).textContent ?? "").toContain("1,000.99 W");
    expect(labeledFigure(entry, /actual/i).textContent ?? "").toContain("980.12 W");
    expect(entry.textContent ?? "").not.toMatch(/\d\.\d{3,}/);

    // The reserve rows hold the same bound: the pod's own reading and the
    // fleet average over reporting pods, both trimmed at two decimals.
    const reserveRegion = await screen.findByRole("region", { name: RESERVE_REGION });
    expectVisibleText(reserveRegion, /46\.56%/);
    expect(reserveRegion.textContent ?? "").not.toMatch(/55555555/);
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

  it("lists a per-unit cell imbalance warning factor when a pod's spread crosses the 50 mV early-warning line", async () => {
    // The 2026-08-23 policy relaxation: imbalance no longer blocks dispatch on
    // its own, so the limiting card keeps the OLD 50 mV line visible as a
    // per-unit warning with the measured figure — the operator always sees
    // the condition that used to block (pod-lhs 54 mV is the live figure
    // that vetoed the fleet that day). A pod under the line stays silent.
    const snapshot = fleet([
      unit({ unit_id: "pod-mid", telemetry: telemetrySummary({ cell_spread_mv: 22 }) }),
      unit({ unit_id: "pod-rhs", telemetry: telemetrySummary({ cell_spread_mv: 32 }) }),
      unit({
        unit_id: "pod-lhs",
        telemetry: telemetrySummary({ cell_spread_mv: 53.99999999999983 }),
      }),
    ]);
    installClient({ snapshot });
    renderHome();

    const region = await screen.findByRole("region", { name: LIMITING_REGION });
    expectVisibleText(
      region,
      /pod-lhs cell imbalance warning: 54 mV spread is above the 50 mV early-warning line/i,
    );
    expect(region.textContent ?? "").not.toMatch(/pod-(mid|rhs) cell imbalance/);
  });

  it("renders a cell_imbalance reason code from readiness in plain language, not as a raw code", async () => {
    const health: HealthReport = {
      liveness: { ok: true },
      service_readiness: { ready: true, reasons: [] },
      control_readiness: {
        ready: false,
        reasons: ["pod-lhs:cell_imbalance"],
      },
    };
    const snapshot = fleet([
      unit({ unit_id: "pod-mid" }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs", lifecycle: "inhibited" }),
    ]);
    installClient({ snapshot, getHealth: () => Promise.resolve(health) });
    renderHome();

    const region = await screen.findByRole("region", { name: LIMITING_REGION });
    expectVisibleText(region, /pod-lhs/i);
    expectVisibleText(region, /cell imbalance/i);
    expect(within(region).queryByText("pod-lhs:cell_imbalance")).toBeNull();
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
        // 2026-08-23 defect pin: two batteries share the snapshot's repeated
        // fleet total (3,000 W each) against per-unit allowances of 1,200 W.
        // With no per-unit request figures on the wire (a fresh load or a
        // scalar intent), the repeated total is never compared per-unit, so
        // the honest badge is Active — never a false "Limited". The genuine
        // per-unit clamp, maps on the stream, is pinned in "HomeView —
        // per-unit watt figures from the wire" below.
        description: "active under a repeated scalar total (limited is not derivable)",
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
        badge: "Active",
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

// --- composed-app regressions ---------------------------------------------------
//
// The isolated pins above held while the composed console still froze every
// data age at its captured value, armed a pod whose arm was refused, and left
// a latched fleet looking armed. These pin the composed behavior directly.

describe("HomeView — live outcomes and ages in the composed app", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders a refused arm arriving on the socket as a refusal and never arms the pod", async () => {
    const snapshot = fleet(allUnits("disarmed"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    const openEvents = vi.fn(channel.openEvents);
    installClient({ snapshot, openEvents });
    renderHome();
    await dataLanded();

    channel.push({
      type: "unit.armed",
      sequence: 43,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator-7",
        units: [{ unit_id: "pod-mid", status: "refused", reason: "inhibit_latched" }],
      },
    });

    // The refusal is announced with its reason, through a live region.
    await waitFor(() => {
      const live = [...screen.getAllByRole("status"), ...screen.getAllByRole("alert")];
      expect(
        live.some(
          (region) =>
            /pod-mid/i.test(region.textContent ?? "") &&
            /refused/i.test(region.textContent ?? "") &&
            /inhibit_latched/.test(region.textContent ?? ""),
        ),
      ).toBe(true);
    });

    // A refused row is never a lifecycle change: the pod and the fleet stay
    // disarmed, exactly as before the refused request.
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, "Disarmed");
    expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Disarmed");
  });

  it("shows the fleet as inhibited, never armed, when an emergency stop latches over the socket, and refetches", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    let snapshotCalls = 0;
    const openEvents = vi.fn(channel.openEvents);
    installClient({
      snapshot,
      getSnapshot: () => {
        snapshotCalls += 1;
        // The refetch answers the pre-latch world again (a cached shared read):
        // only a sequence that advances may replace the latched picture.
        return Promise.resolve(snapshot);
      },
      openEvents,
    });
    renderHome();
    await dataLanded();
    expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Armed");

    channel.push({
      type: "emergency_stop.latched",
      sequence: 43,
      occurred_at: "2026-08-22T10:00:08Z",
      payload: {
        principal: "operator-7",
        stop_id: "stop-5-800.500000",
        unit_ids: ["pod-mid", "pod-rhs", "pod-lhs"],
        reason: "operator requested from the console",
        generation: 3,
        degraded: [],
      },
    });

    // A latched stop announces assertively, naming the pods it holds.
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(
        alerts.some(
          (alert) =>
            /emergency stop latched/i.test(alert.textContent ?? "") &&
            /pod-mid/i.test(alert.textContent ?? ""),
        ),
      ).toBe(true);
    });

    // The latched fleet no longer presents as armed or dispatchable.
    await waitFor(() => {
      expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Inhibited");
    });
    const inhibited = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(inhibited, "Inhibited");

    // Both halves of the contract: the event drove the picture immediately,
    // and a snapshot refetch followed it.
    await waitFor(() => {
      expect(snapshotCalls).toBeGreaterThanOrEqual(2);
    });
  });

  it("ignores a snapshot whose sequence does not advance the picture on screen", async () => {
    const newer = fleet(
      [
        unit({
          unit_id: "pod-mid",
          lifecycle: "active",
          requested_power: { direction: "discharge", watts: 1500 },
          authorized_power: { direction: "discharge", watts: 1500 },
          measured_watts: 1490,
        }),
        unit({ unit_id: "pod-rhs", lifecycle: "armed_idle" }),
        unit({ unit_id: "pod-lhs", lifecycle: "armed_idle" }),
      ],
      45,
    );
    const older = fleet(allUnits("disarmed"), 42);
    const channel = liveChannel([{ type: "snapshot", sequence: 45, data: newer }]);
    installClient({ snapshot: older, openEvents: channel.openEvents });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, "Active");

    // A republished stale frame (the shared plane hands every newly mounted
    // view its latest snapshot frame) must not rewind the newer picture...
    channel.push({ type: "snapshot", sequence: 42, data: older });
    // ...and this event, newer than both, proves the stream kept flowing: it
    // applies to the newer world (pod-mid disarmed) while pod-rhs keeps the
    // newer world's Armed badge — adopting the stale snapshot would have
    // shown pod-rhs as Disarmed.
    channel.push({
      type: "unit.disarmed",
      sequence: 46,
      occurred_at: "2026-08-22T10:00:11Z",
      payload: {
        principal: "operator-7",
        units: [{ unit_id: "pod-mid", status: "disarmed", reason: "disarmed" }],
      },
    });

    await waitFor(() => {
      expectVisibleText(entry, "Disarmed");
    });
    const rhs = await findUnitEntry(POWER_REGION, "pod-rhs");
    expectVisibleText(rhs, "Armed");
  });

  it("advances the data age from a captured marker instead of freezing it at the captured value", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const snapshot = fleet([
      unit({ unit_id: "pod-mid", telemetry_age_s: 20 }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    installClient({ snapshot });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /20 s/);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    await waitFor(() => {
      const aged = screen
        .getAllByText(/Data age:/)
        .map((node) => node.textContent ?? "")
        .join(" ");
      // The age ADVANCED (>= 24 s) rather than freezing at the captured 20 s.
      // Under parallel suite load the fake clock can bleed a little real time
      // (shouldAdvanceTime), so the bound is "advanced past 5 s", never an
      // exact second.
      const seconds = aged.match(/Data age: (\d+) s/);
      expect(seconds).not.toBeNull();
      expect(Number(seconds?.[1] ?? 0)).toBeGreaterThanOrEqual(24);
    }, { timeout: 4000 });
  });
});

// --- live-update regression pins (2026-08-23 incident) -----------------------
//
// The service sends its snapshot frame exactly once per connection (rest.py)
// and one observation.published frame per telemetry append (composition.py).
// Home once bound every displayed age to the connect-time snapshot, so the
// number climbed past minutes while the fleet was publishing fine. These pin
// the corrected binding: ages sawtooth from each unit's own observations, a
// quiet connection is named stale instead of masquerading as current, and a
// resumed connection after a controller restart adopts the renumbered world.

/** A stream that yields its frames and then ends cleanly: the connection is
 * lost without an error, exactly like a controller process being swapped. */
function endingStream(frames: StreamFrame[]): AsyncIterable<StreamFrame> {
  return (async function* ending(): AsyncGenerator<StreamFrame, void, unknown> {
    for (const frame of frames) {
      yield frame;
    }
  })();
}

describe("HomeView — live observations and staleness", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resets a unit's data age when its observation arrives over the stream", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const snapshot = fleet([unit({ unit_id: "pod-mid", telemetry_age_s: 20 })]);
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /20 s/);

    // The per-cycle liveness frame: the pod just published, so its reading is
    // fresh NOW — the age restarts from zero instead of climbing to 21, 22…
    channel.push({
      type: "observation.published",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:02Z",
      payload: { unit_id: "pod-mid", connection_epoch: 3, sequence: 440 },
    });
    await waitFor(() => {
      expectVisibleText(entry, /Data age: [0-2] s/);
    });

    // And it keeps ticking from the observation — the healthy sawtooth.
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    await waitFor(() => {
      expectVisibleText(entry, /Data age: [3-9] s/);
    });
  });

  it("names the readings stale when no fresh data has arrived while the connection claims live", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const snapshot = fleet([unit({ unit_id: "pod-mid", telemetry_age_s: 2 })]);
    // A live channel that then goes quiet: the connection stays up, but no
    // observation and no new snapshot ever arrive again.
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();
    await dataLanded();

    act(() => {
      vi.advanceTimersByTime(12000);
    });

    // The climbing age is unmistakable from a healthy sawtooth: the safe region
    // carries the paused-updates line and the age line is marked stale.
    const safeRegion = await screen.findByRole("region", { name: SAFE_REGION });
    await waitFor(() => {
      expectVisibleText(safeRegion, /No fresh readings for 1[0-9] s|2[0-9] s/);
    });
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /updates have paused/);
  });

  it("adopts the renumbered snapshot after a controller restart instead of freezing the pre-restart picture", async () => {
    const before = fleet([unit({ unit_id: "pod-mid", lifecycle: "disarmed" })], 42);
    // The restarted controller renumbers the sequence space from zero and the
    // pod came back active.
    const after = fleet(
      [
        unit({
          unit_id: "pod-mid",
          lifecycle: "active",
          requested_power: { direction: "discharge", watts: 1500 },
          authorized_power: { direction: "discharge", watts: 1500 },
          measured_watts: 1480,
        }),
      ],
      5,
    );
    let connections = 0;
    const openEvents = vi.fn((afterSequence?: number) => {
      connections += 1;
      if (connections === 1) {
        // Healthy, then the controller process is swapped: the socket dies.
        return endingStream([snapshotFrame(before)]);
      }
      // The resume carries the pre-restart cursor; the restarted service
      // answers with its renumbered world (sequence 5 < 42).
      expect(afterSequence).toBe(42);
      return liveChannel([snapshotFrame(after)]).openEvents();
    });
    installClient({ snapshot: before, openEvents });
    renderHome();
    await dataLanded();
    expectVisibleText(await screen.findByRole("region", { name: SAFE_REGION }), "Disarmed");

    // The reconnect lands the new world: never a frozen pre-restart picture.
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(
      () => {
        expectVisibleText(entry, /1,?480/);
      },
      { timeout: 5000 },
    );
    expectVisibleText(entry, "Active");
  });


  it("renders the request figures the moment an accepted intent lands on the stream", async () => {
    const snapshot = fleet(allUnits("armed_idle"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    let snapshotCalls = 0;
    installClient({
      snapshot,
      // The next read carries the granted world (the kernel grants on its
      // next tick after acceptance).
      getSnapshot: () => {
        snapshotCalls += 1;
        return Promise.resolve(
          snapshotCalls === 1
            ? snapshot
            : fleet(
                [
                  unit({
                    unit_id: "pod-mid",
                    lifecycle: "active",
                    requested_power: { direction: "discharge", watts: 1500 },
                    authorized_power: { direction: "discharge", watts: 1500 },
                    measured_watts: 1480,
                  }),
                  unit({ unit_id: "pod-rhs" }),
                  unit({ unit_id: "pod-lhs" }),
                ],
                45,
              ),
        );
      },
      openEvents: vi.fn(channel.openEvents),
    });
    renderHome();
    await dataLanded();

    // The accepted request arrives: the request figures render immediately
    // from the frame's own payload (direction, watts, units).
    channel.push({
      type: "intent.accepted",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator:home",
        intent_id: "intent-44",
        direction: "discharge",
        watts: 1500,
        unit_ids: ["pod-mid"],
      },
    });

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, /1,?500/);
    });
    // The forced fresh read (not the cached connect-time picture) lands the
    // authorized and measured figures next.
    await waitFor(() => {
      expect(snapshotCalls).toBeGreaterThanOrEqual(2);
    });
  });

  it("recomputes the displayed age the moment a throttled tab becomes visible", async () => {
    // Only the clocks are faked; the 1 s age interval stays real, standing in
    // for a browser-throttled background tab whose ticks barely run.
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["Date", "performance"],
    });
    const snapshot = fleet([unit({ unit_id: "pod-mid", telemetry_age_s: 20 })]);
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();

    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expectVisibleText(entry, /20 s/);

    // Half a minute passes with the tab hidden: the throttled interval has
    // not fired, so the number on screen is still the stale "20 s".
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expectVisibleText(entry, /20 s/);

    // Coming back to the tab recomputes the age from the clock BEFORE the
    // operator can read the frozen number — no interval tick required.
    act(() => {
      fireEvent(document, new Event("visibilitychange"));
    });
    expectVisibleText(entry, /Data age: 5[0-9] s/);
  });
});

// --- per-unit watt figures from the wire --------------------------------------
//
// The 2026-08-23 live defect: the operator dispatched 1,000 W per battery over
// 3 batteries and the audit authorized 3,000/3,000 with safety_checks_passed
// every cycle — nothing was clamped — yet every Home card read "Requested
// 3,000 W / Allowed 1,000 W / Limited". The snapshot's per-unit requested
// figure repeats the intent's FLEET TOTAL once per covered unit (service.py
// `_requested_power`), and the cards compared that total against the per-unit
// allowance. The exact per-unit figures come from the shared tracker
// (web/src/app/useUnitIntentFigures.ts — the same implementation Now's request
// card consumes): the intent's `watts_by_unit` and the decision's
// `authorized_watts_by_unit`, live on the stream. Without them, the repeated
// total is labelled AS the fleet total and no "Limited" state is derived.

describe("HomeView — per-unit watt figures from the wire", () => {
  /**
   * The live-incident world: a 3 × 1,000 W dispatch the safety system
   * authorized in full. The snapshot repeats the 3,000 W total per covered
   * unit and carries the true per-unit allowance; `authorizedRhs` bends one
   * battery's allowance for the genuine-clamp test.
   */
  function incidentWorld(authorizedRhs = 1000): FleetView {
    return fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1000 },
        measured_watts: 990,
      }),
      unit({
        unit_id: "pod-rhs",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: authorizedRhs },
        measured_watts: authorizedRhs === 1000 ? 990 : 390,
      }),
      unit({
        unit_id: "pod-lhs",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 3000 },
        authorized_power: { direction: "discharge", watts: 1000 },
        measured_watts: 990,
      }),
    ]);
  }

  /** The per-unit acceptance frame the facade publishes for a 3 × 1,000 W dispatch. */
  function intentAcceptedFrame(): StreamFrame {
    return {
      type: "intent.accepted",
      sequence: 43,
      occurred_at: "2026-08-22T10:00:05Z",
      payload: {
        principal: "operator:home",
        intent_id: "intent-43-1.000000",
        direction: "discharge",
        watts: 3000,
        watts_by_unit: { "pod-mid": 1000, "pod-rhs": 1000, "pod-lhs": 1000 },
        unit_ids: ["pod-mid", "pod-rhs", "pod-lhs"],
      },
    };
  }

  /** The kernel's per-tick decision summary riding the audit bus. */
  function decisionFrame(
    authorizedByUnit: Record<string, number>,
    result = "authorized",
    reasonCodes: string[] = ["safety_checks_passed"],
  ): StreamFrame {
    const total = Object.values(authorizedByUnit).reduce((sum, watts) => sum + watts, 0);
    return {
      type: "audit.appended",
      sequence: 44,
      occurred_at: "2026-08-22T10:00:07Z",
      payload: {
        event_id: "facade-44",
        event_type: "control_decision",
        unit_id: null,
        generation: 9,
        result,
        reason_codes: reasonCodes,
        requested_active_w: 3000,
        authorized_active_w: total,
        requested_watts_by_unit: { "pod-mid": 1000, "pod-rhs": 1000, "pod-lhs": 1000 },
        authorized_watts_by_unit: authorizedByUnit,
      },
    };
  }

  it("renders the exact per-battery request (1,000 W, never the 3,000 W fleet total) and no false Limited", async () => {
    const snapshot = incidentWorld();
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();
    await dataLanded();

    channel.push(intentAcceptedFrame());
    channel.push(decisionFrame({ "pod-mid": 1000, "pod-rhs": 1000, "pod-lhs": 1000 }));

    for (const unitId of ["pod-mid", "pod-rhs", "pod-lhs"]) {
      const entry = await findUnitEntry(POWER_REGION, unitId);
      const requested = labeledFigure(entry, /requested/i);
      await waitFor(() => {
        expect(requested.textContent ?? "").toContain("Discharging 1,000 W");
      });
      // The fleet total never rides a per-battery card again.
      expect(requested.textContent ?? "").not.toMatch(/3,?000/);
      expect(labeledFigure(entry, /allowed/i).textContent ?? "").toContain("1,000 W");
      // Authorized equals requested FOR THIS BATTERY: not limited, and the
      // fleet badge stays Active — the audit authorized 3,000/3,000 in full.
      expect(entry.textContent ?? "").not.toMatch(/limited/i);
      expectVisibleText(entry, "Active");
    }
    expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Active");
  });

  it("still shows Limited for a genuine per-unit clamp — naming the battery and its own numbers", async () => {
    // The site headroom only stretched to 400 W for pod-rhs.
    const snapshot = incidentWorld(400);
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();
    await dataLanded();

    channel.push(intentAcceptedFrame());
    channel.push(
      decisionFrame({ "pod-mid": 1000, "pod-rhs": 400, "pod-lhs": 1000 }, "clamped", [
        "power_clamped",
      ]),
    );

    const clamped = await findUnitEntry(POWER_REGION, "pod-rhs");
    await waitFor(() => {
      expect(clamped.textContent ?? "").toMatch(
        /limited — pod-rhs requested 1,?000 W but was allowed 400 W/i,
      );
    });
    expectVisibleText(clamped, "Limited");
    expect(clamped.textContent ?? "").toMatch(/holding back part of the request/i);

    // The unclamped batteries stay plain: their allowance equals their target.
    const plain = await findUnitEntry(POWER_REGION, "pod-mid");
    expect(plain.textContent ?? "").not.toMatch(/limited/i);
    expectVisibleText(plain, "Active");

    // The conservative fleet badge still names the clamp.
    expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Limited");
  });

  it("labels the snapshot's repeated figure as the fleet total when no per-unit figures exist — and derives no Limited from it", async () => {
    // A fresh page load mid-intent (or a scalar intent): only the snapshot has
    // landed, so its repeated total is all there is.
    const snapshot = incidentWorld();
    installClient({ snapshot });
    renderHome();

    for (const unitId of ["pod-mid", "pod-rhs", "pod-lhs"]) {
      const entry = await findUnitEntry(POWER_REGION, unitId);
      const requested = labeledFigure(entry, /requested/i);
      // The fleet total is labelled AS the fleet total — never stamped on a
      // per-battery card as this battery's request.
      expect(requested.textContent ?? "").toMatch(/fleet total 3,?000 W/i);
      expect(requested.textContent ?? "").not.toMatch(/per batter/i);
      // The allowance IS per-unit on the wire and renders as such...
      expect(labeledFigure(entry, /allowed/i).textContent ?? "").toContain("1,000 W");
      // ...but the fleet total is never compared against it: no Limited badge,
      // no note — the audit authorized the request in full.
      expect(entry.textContent ?? "").not.toMatch(/limited/i);
      expectVisibleText(entry, "Active");
    }
    expectVisibleText(screen.getByRole("region", { name: SAFE_REGION }), "Active");

    // The next-action line states the total across the covered batteries,
    // never "per pod".
    const nextRegion = await screen.findByRole("region", { name: NEXT_REGION });
    expectVisibleText(nextRegion, /3,?000 W requested in total across 3 batteries/i);
    expect(nextRegion.textContent ?? "").not.toMatch(/per (pod|battery)/i);
  });

  it("picks the per-unit figures up from the next control_decision after a cold load mid-intent, and drops them when the request ends", async () => {
    const snapshot = incidentWorld();
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: vi.fn(channel.openEvents) });
    renderHome();
    await dataLanded();

    // No intent.accepted was seen (the page opened after acceptance): the
    // kernel's next per-tick decision carries the maps on its own.
    channel.push(decisionFrame({ "pod-mid": 1000, "pod-rhs": 1000, "pod-lhs": 1000 }));
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expect(labeledFigure(entry, /requested/i).textContent ?? "").toContain("1,000 W");
    });

    // The request ends (routine revocation — expiry, disarm, fence): the maps
    // must not linger as a ghost of an intent that no longer exists, so the
    // card falls back to the honest fleet-total labelling.
    channel.push({
      type: "authorization.revoked",
      sequence: 45,
      occurred_at: "2026-08-22T10:00:20Z",
      payload: { reason: "intent_expired", unit_ids: ["pod-mid", "pod-rhs", "pod-lhs"] },
    });
    await waitFor(() => {
      expect(labeledFigure(entry, /requested/i).textContent ?? "").toMatch(
        /fleet total 3,?000 W/i,
      );
    });
  });
});


// --- health is a live question, not a mount-time one --------------------------
//
// The limiting-factors card names the system's CURRENT readiness reasons;
// those change as units arm, act, and stand down. A mount-time-only health
// read froze the card at the unlock-time answer for the whole session — the
// same defect class as a figure bound to a mount-time fetch.

describe("HomeView — the limiting factors follow the live health envelope", () => {
  it("re-reads health on its poll cadence: a reason that appears mid-session shows up without a reload", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const snapshot = fleet([unit({ unit_id: "pod-mid" }), unit({ unit_id: "pod-rhs" })]);
    let healthy = true;
    const client = installClient({
      snapshot,
      getHealth: () =>
        Promise.resolve(
          healthy
            ? healthyHealth
            : {
                ...healthyHealth,
                control_readiness: { ready: false, reasons: ["pod-mid:inhibit_latched"] },
              },
        ),
    });
    void client;
    renderHome();

    // At unlock nothing is limiting: the calm line renders.
    const calm = await screen.findByText(/nothing is limiting operation/i);
    expect(calm).toBeInTheDocument();

    // Mid-session the health envelope changes (a latch appears). The view's
    // poll re-reads it and the card names the new limiting reason — no
    // reload, no remount.
    healthy = false;
    act(() => {
      vi.advanceTimersByTime(HEALTH_POLL_MS + 1000);
    });
    await waitFor(() => {
      // The plain-language factor line (the raw code sits behind "Show
      // detail"); the unit the latch holds is named.
      expect(screen.getByText(/held by a safety latch/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/nothing is limiting operation/i)).toBeNull();

    // And it clears again when the system recovers.
    healthy = true;
    act(() => {
      vi.advanceTimersByTime(HEALTH_POLL_MS + 1000);
    });
    await waitFor(() => {
      expect(screen.getByText(/nothing is limiting operation/i)).toBeInTheDocument();
    });
  });
});

// --- the solar-surplus tile (excess-solar activation, feature-detected) -------
//
// The tile is the excess-solar feature's whole story in one glance
// (DESIGN_EXCESS_ACTIVATION.md §5 W-A): ACTIVE names target, commanded watts,
// and the fleet's export reading; INACTIVE renders the FIRST reason code's
// plain sentence from the pinned vocabulary; per-unit rows carry the advisory
// grid/load readthrough (negative grid = import, positive = export, absent =
// "not available"). Everything is feature-detected: no adviser_state in the
// snapshot renders NOTHING — today's backend, and any deployment without the
// excess_charging block, sees exactly the console it had before.

const SOLAR_REGION = /solar surplus/i;

/** Units carrying the advisory readthrough: export, import, and absent. */
function solarUnits(): UnitView[] {
  return [
    unit({
      unit_id: "pod-mid",
      telemetry: telemetrySummary({ grid_power_w: 620, load_power_w: 340 }),
    }),
    unit({
      unit_id: "pod-rhs",
      telemetry: telemetrySummary({ grid_power_w: -800, load_power_w: 210 }),
    }),
    unit({ unit_id: "pod-lhs", telemetry: null }),
  ];
}

function solarWorld(adviser: WireAdviserState): FleetView {
  return { ...fleet(solarUnits()), adviser_state: adviser };
}

describe("HomeView — the solar-surplus tile", () => {
  it("renders nothing at all while the snapshot carries no adviser_state (feature detection)", async () => {
    // Today's backend sends no adviser_state: no tile, no heading, no rows —
    // the Home view is exactly what it was before the feature existed.
    installClient();
    renderHome();
    await dataLanded();
    expect(screen.queryByRole("region", { name: SOLAR_REGION })).toBeNull();
    expect(screen.queryByRole("heading", { name: SOLAR_REGION })).toBeNull();
  });

  it("names the active story's three facts and the composed cap", async () => {
    installClient({
      snapshot: solarWorld(
        adviserState({
          active: true,
          hysteresis_state: "holding",
          target_unit_id: "pod-mid",
          commanded_charge_w: 1400,
          fleet_export_w: 1800,
          charge_cap_w: 2500,
          reason_codes: ["export_headroom_available"],
        }),
      ),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    expect(region).toHaveTextContent(/Charging pod-mid at 1,400 W from 1,800 W export\./);
    // The cap is the bare fact it is — the trial's 500 W renders "cap 500 W"
    // with no invented label.
    expect(region).toHaveTextContent(/cap 2,500 W/);
    // The tile sits between "What is powering the home?" and the reserve card.
    const power = screen.getByRole("region", { name: POWER_REGION });
    const reserve = screen.getByRole("region", { name: RESERVE_REGION });
    expect(
      Boolean(power.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(
      Boolean(region.compareDocumentPosition(reserve) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
  });

  it("carries the per-unit grid/load readthrough with import/export wording and honest gaps", async () => {
    installClient({ snapshot: solarWorld(adviserState()) });
    renderHome();

    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    const rows = within(region).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]!).toHaveTextContent(/pod-mid: grid \+620 W export · load 340 W/);
    expect(rows[1]!).toHaveTextContent(/pod-rhs: grid -800 W import · load 210 W/);
    // An absent datum reads "not available" — never zero-filled.
    expect(rows[2]!).toHaveTextContent(/pod-lhs: grid not available · load not available/);
  });

  // One plain sentence per reason code in the ONE pinned vocabulary — the
  // parametrization IS the completeness pin: a code added to the wire
  // vocabulary without a row here fails the completeness test below.
  const INACTIVE_CASES: { code: string; sentence: RegExp; fixture?: Partial<WireAdviserState> }[] = [
    {
      code: "disabled_by_config",
      sentence: /Charging from solar surplus is off \(config\)\./,
      fixture: { enabled: false, enabled_origin: "config" },
    },
    {
      code: "disabled_by_runtime",
      sentence: /off until the controller restarts/,
      fixture: { enabled: false, enabled_origin: "runtime" },
    },
    {
      code: "economics_acknowledgement_required",
      sentence:
        /Waiting on the one-time net-billing confirmation before solar-surplus charging can start\./,
      fixture: { enabled: true, acknowledged_economics: false },
    },
    {
      code: "export_evidence_missing",
      sentence:
        /Export reading unavailable on the fleet — standing down \(fail-closed\)\. Export figure: not available\./,
      fixture: { export_evidence: "missing", fleet_export_w: null },
    },
    {
      code: "export_evidence_bad",
      sentence: /Export reading unavailable on the fleet/,
      fixture: { export_evidence: "bad", fleet_export_w: null },
    },
    {
      code: "export_evidence_stale",
      sentence: /Export reading unavailable on the fleet/,
      fixture: { export_evidence: "stale", fleet_export_w: null },
    },
    {
      code: "no_export_headroom",
      sentence: /Exporting 1,800 W — below the headroom margin, nothing to charge from\./,
      fixture: { fleet_export_w: 1800 },
    },
    {
      code: "no_acceleration_over_autonomy",
      sentence: /Surplus too small — taking over would charge slower than the pod does by itself\./,
    },
    {
      code: "below_exit_hysteresis",
      sentence: /Surplus is falling — handing back to the pod's own charging\./,
    },
    {
      code: "no_eligible_target",
      sentence: /Solar surplus available, but no battery needs charging \(full, inhibited, or not armed\)\./,
      fixture: { target_unit_id: null },
    },
    {
      code: "yielding_to_higher_priority",
      sentence: /Standing down — a manual request has pod-mid\./,
      fixture: { target_unit_id: "pod-mid" },
    },
  ];

  it("maps every code in the pinned vocabulary to a plain sentence (the table is complete)", () => {
    // A vocabulary code with no row here would render a raw code to an
    // operator; a table row with no vocabulary code is dead weight.
    const tabled = INACTIVE_CASES.map((entry) => entry.code).sort();
    expect(tabled).toEqual(
      [...ADVISER_REASON_CODES].filter((code) => code !== "export_headroom_available").sort(),
    );
  });

  it.each(INACTIVE_CASES)(
    "renders the honest inactive sentence for $code",
    async ({ code, sentence, fixture }) => {
      installClient({
        snapshot: solarWorld(
          adviserState({
            active: false,
            hysteresis_state: "inactive",
            reason_codes: [code],
            ...fixture,
          }),
        ),
      });
      renderHome();
      const region = await screen.findByRole("region", { name: SOLAR_REGION });
      expect(region).toHaveTextContent(sentence);
      // The cap line belongs to the active sentence only.
      expect(region.textContent ?? "").not.toMatch(/cap \d/);
    },
  );

  it("never zero-fills the fleet export: a null figure under failed evidence says not available", async () => {
    installClient({
      snapshot: solarWorld(
        adviserState({
          active: false,
          export_evidence: "stale",
          fleet_export_w: null,
          reason_codes: ["export_evidence_stale"],
        }),
      ),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    const status = within(region).getByText(/Export reading unavailable/);
    expect(status).toHaveTextContent(/Export figure: not available\./);
    expect(status.textContent ?? "").not.toMatch(/0 W/);
  });

  it("updates the tile live from state_changed frames: an active-flip announces, a heartbeat re-prices figures silently", async () => {
    // The trial's shape: composed and enabled, cap 500, evaluating entry.
    const world = solarWorld(
      adviserState({
        enabled: true,
        acknowledged_economics: true,
        active: false,
        hysteresis_state: "entering",
        commanded_charge_w: 0,
        fleet_export_w: 800,
        charge_cap_w: 500,
        held_intent_id: null,
        reason_codes: ["no_export_headroom"],
      }),
    );
    const channel = liveChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: channel.openEvents });
    renderHome();
    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    expect(region).toHaveTextContent(/Exporting 800 W — below the headroom margin/);

    // The adviser starts charging: the frame alone moves the tile — no
    // snapshot refetch, no reload — and the flip reaches the live region.
    channel.push(
      excessAdviserStateChanged(43, {
        active: true,
        hysteresis_state: "holding",
        target_unit_id: "pod-mid",
        commanded_charge_w: 400,
        fleet_export_w: 1800,
        held_intent_id: "opt-3f9c21",
        reason_codes: ["export_headroom_available"],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(region).toHaveTextContent(/Charging pod-mid at 400 W from 1,800 W export\./);
    });
    // The event payload carries no charge_cap_w (§2): the composed cap the
    // snapshot carried survives the patch.
    expect(region).toHaveTextContent(/cap 500 W/);
    expect(await screen.findByText("Solar-surplus charging started.")).toBeInTheDocument();

    // A heartbeat republish: same state tuple, fresher figures — the tile
    // re-prices with NO new announcement.
    channel.push(
      excessAdviserStateChanged(44, {
        heartbeat: true,
        active: true,
        hysteresis_state: "holding",
        target_unit_id: "pod-mid",
        commanded_charge_w: 500,
        fleet_export_w: 1900,
        held_intent_id: "opt-3f9c21",
        reason_codes: ["export_headroom_available"],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(region).toHaveTextContent(/Charging pod-mid at 500 W from 1,900 W export\./);
    });
    expect(screen.queryAllByText(/Solar-surplus charging/)).toHaveLength(1);
  });

  it("announces the stand-down when the adviser hands back over the stream", async () => {
    const world = solarWorld(adviserState({ charge_cap_w: 500 }));
    const channel = liveChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: channel.openEvents });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    channel.push(
      excessAdviserStateChanged(43, {
        active: false,
        hysteresis_state: "exiting",
        commanded_charge_w: 0,
        held_intent_id: null,
        fleet_export_w: 700,
        reason_codes: ["below_exit_hysteresis"],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(screen.getByText(/Surplus is falling — handing back/)).toBeInTheDocument();
    });
    expect(await screen.findByText("Solar-surplus charging stood down.")).toBeInTheDocument();
  });
});

// --- the excess-charging toggle (§3's guarded confirmation) -------------------
//
// The tile's footer is the feature's front door: the current state and its
// origin (the honest "until restart" marker), a switch that never flips
// directly — it opens the typed-confirmation dialog — and the FIRST enable's
// one-time net-billing acknowledgement (the exact assertion, a required
// checkbox, sent as "economics": "NET_BILLED", never asked again). Refusals
// render their envelopes inline; the 200's adviser_state is adopted
// optimistically. Disable asks for the EXCESS confirmation only.

function refuse(
  status: number,
  code: string,
  message: string,
  details: Record<string, unknown> | null = null,
): ApiClientError {
  return new ApiClientError({ status, code, message, details, request_id: "req-excess-1" });
}

describe("HomeView — the excess-charging toggle", () => {
  it.each([
    [{ enabled: true, enabled_origin: "config" } as Partial<WireAdviserState>, "On (config)"],
    [{ enabled: true, enabled_origin: "runtime" } as Partial<WireAdviserState>, "On — until restart"],
    [{ enabled: false, enabled_origin: "config" } as Partial<WireAdviserState>, "Off (config)"],
    [{ enabled: false, enabled_origin: "runtime" } as Partial<WireAdviserState>, "Off — until restart"],
  ])("shows the current state and origin ($enabled_origin, enabled $enabled)", async (fixture, expected) => {
    installClient({
      snapshot: { ...fleet([unit({ unit_id: "pod-mid" })]), adviser_state: adviserState(fixture) },
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    expect(region).toHaveTextContent(`Excess charging: ${expected}`);
    // The runtime origin is the honest transience marker: a restart re-reads
    // the commissioned config, and the console says so.
    if (fixture.enabled_origin === "runtime") {
      expect(region).toHaveTextContent(/the config's own setting takes over at restart/);
    }
  });

  it("first enable: EXCESS and the net-billing acknowledgement are both required, and the acknowledgement is sent", async () => {
    const user = userEvent.setup();
    // The trial's boot state: composed but suspended, never acknowledged.
    const offUnacked = adviserState({
      enabled: false,
      enabled_origin: "config",
      acknowledged_economics: false,
      active: false,
      hysteresis_state: "inactive",
      commanded_charge_w: 0,
      held_intent_id: null,
      reason_codes: ["economics_acknowledgement_required"],
    });
    const enabledByToggle = adviserState({
      enabled: true,
      enabled_origin: "runtime",
      acknowledged_economics: true,
    });
    const postExcessCharging = vi.fn(() => Promise.resolve(excessChargingToggleOk(enabledByToggle)));
    installClient({
      snapshot: { ...fleet(solarUnits()), adviser_state: offUnacked },
      postExcessCharging,
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SOLAR_REGION });

    // The switch opens the dialog; it never flips directly.
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on charging from solar surplus/i });
    // The exact assertion is stated, with its required checkbox (the label
    // carries the assertion verbatim).
    expect(within(dialog).getByText(/One-time confirmation — the net-billing assumption/i)).toBeVisible();
    expect(within(dialog).getByLabelText(/This site's billing nets across phases/i)).not.toBeChecked();
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();

    // EXCESS alone is not enough while the acknowledgement is unchecked.
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    expect(confirm).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox"));
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    // The acknowledgement rides the first enable ever (P3) — and only it.
    await waitFor(() => {
      expect(postExcessCharging).toHaveBeenCalledWith("enable", { economics: "NET_BILLED" });
    });

    // The 200's adviser_state is adopted optimistically: the dialog closes and
    // the tile speaks the post-toggle state (origin runtime = until restart).
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(region).toHaveTextContent("Excess charging: On — until restart");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("the acknowledgement is captured once: a later enable never asks again and never re-sends economics", async () => {
    const user = userEvent.setup();
    // Disabled at runtime, but the site HAS captured the net-billing fact.
    const offAcked = adviserState({
      enabled: false,
      enabled_origin: "runtime",
      acknowledged_economics: true,
      active: false,
      hysteresis_state: "inactive",
      commanded_charge_w: 0,
      held_intent_id: null,
      reason_codes: ["no_export_headroom"],
      fleet_export_w: 900,
    });
    const postExcessCharging = vi.fn(() =>
      Promise.resolve(excessChargingToggleOk(adviserState({ enabled: true, enabled_origin: "runtime" }))),
    );
    installClient({
      snapshot: { ...fleet(solarUnits()), adviser_state: offAcked },
      postExcessCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on charging from solar surplus/i });
    // No acknowledgement step the second time — captured once, never re-prompted.
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      // No economics key: the site's captured fact already satisfies P3.
      expect(postExcessCharging).toHaveBeenCalledWith("enable", {});
    });
  });

  it("the operator's own spelling confirms: lowercase 'excess' unlocks the toggle", async () => {
    const user = userEvent.setup();
    // An acknowledged, disabled site: the typed word is the only gate left.
    const offAcked = adviserState({
      enabled: false,
      enabled_origin: "runtime",
      acknowledged_economics: true,
      active: false,
      hysteresis_state: "inactive",
      commanded_charge_w: 0,
      held_intent_id: null,
      reason_codes: ["no_export_headroom"],
      fleet_export_w: 900,
    });
    const postExcessCharging = vi.fn(() =>
      Promise.resolve(excessChargingToggleOk(adviserState({ enabled: true, enabled_origin: "runtime" }))),
    );
    installClient({
      snapshot: { ...fleet(solarUnits()), adviser_state: offAcked },
      postExcessCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on charging from solar surplus/i });
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();
    // The operator spec's literal instruction — "simply type 'excess'" —
    // confirms the toggle; the wire still sends the literal "EXCESS".
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "excess");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(postExcessCharging).toHaveBeenCalledWith("enable", {});
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("disable asks only for the typed EXCESS confirmation", async () => {
    const user = userEvent.setup();
    const onByConfig = adviserState({ enabled: true, enabled_origin: "config" });
    const offByToggle = adviserState({
      enabled: false,
      enabled_origin: "runtime",
      acknowledged_economics: true,
      active: false,
      hysteresis_state: "inactive",
      commanded_charge_w: 0,
      held_intent_id: null,
      reason_codes: ["no_export_headroom"],
      fleet_export_w: 700,
    });
    const postExcessCharging = vi.fn(() => Promise.resolve(excessChargingToggleOk(offByToggle)));
    installClient({
      snapshot: { ...fleet(solarUnits()), adviser_state: onByConfig },
      postExcessCharging,
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SOLAR_REGION });
    expect(region).toHaveTextContent("Excess charging: On (config)");

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn off charging from solar surplus/i });
    // Stopping is the safety-positive direction: no acknowledgement step.
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const confirm = within(dialog).getByRole("button", { name: "Turn off" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(postExcessCharging).toHaveBeenCalledWith("disable", {});
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(region).toHaveTextContent("Excess charging: Off — until restart");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("renders the not-commissioned refusal inline and keeps the dialog open", async () => {
    const user = userEvent.setup();
    const postExcessCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "excess_charging_not_commissioned",
          "The excess_charging block is not composed on this deployment",
        ),
      ),
    );
    installClient({
      snapshot: {
        ...fleet(solarUnits()),
        adviser_state: adviserState({
          enabled: true,
          acknowledged_economics: true,
          active: false,
          reason_codes: ["no_export_headroom"],
        }),
      },
      postExcessCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    // A deployment whose block vanished between snapshot and toggle: the
    // refusal names the honest state — nothing to turn off there either.
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn off/i });
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/not commissioned in this deployment's config/);
    expect(alert).toHaveTextContent(/excess_charging_not_commissioned/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("routes the economics-required refusal into the acknowledgement step", async () => {
    const user = userEvent.setup();
    // The console's state says the fact is captured, but the site's durable
    // record says otherwise (a restart, another console): the refusal IS the
    // routing — the acknowledgement step appears inside the same dialog.
    const postExcessCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "economics_acknowledgement_required",
          "The net-billing acknowledgement must be captured before the first enable",
          { acknowledgement: "NET_BILLED" },
        ),
      ),
    );
    installClient({
      snapshot: {
        ...fleet(solarUnits()),
        adviser_state: adviserState({
          enabled: false,
          acknowledged_economics: true,
          active: false,
          reason_codes: ["no_export_headroom"],
        }),
      },
      postExcessCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on/i });
    // The stale local flag showed no acknowledgement step…
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));

    // …the refusal brings it in, with its plain sentence.
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/net-billing confirmation is required before the first enable/);
    expect(within(dialog).getByRole("checkbox")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    {
      holding: "a unit active under another intent",
      details: { reasons: ["unit_active_under_intent"], unit_ids: ["pod-mid"], stop_ids: [] },
      sentence: /Cannot enable while a request is still active on pod-mid — finish or cancel the request on pod-mid first\./,
    },
    {
      holding: "a latched stop",
      details: { reasons: ["latched_stop_holds"], unit_ids: [], stop_ids: ["stop-7"] },
      sentence: /Cannot enable while an emergency stop holds the fleet \(stop-7\) — acknowledge the stop first\./,
    },
  ])("names what holds a refused enable: $holding", async ({ details, sentence }) => {
    const user = userEvent.setup();
    const postExcessCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "excess_enable_refused",
          "The fleet is not in a state where excess charging can start",
          details,
        ),
      ),
    );
    installClient({
      snapshot: {
        ...fleet(solarUnits()),
        adviser_state: adviserState({
          enabled: false,
          acknowledged_economics: true,
          active: false,
          reason_codes: ["no_export_headroom"],
        }),
      },
      postExcessCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: SOLAR_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on/i });
    await user.type(within(dialog).getByLabelText(/Type EXCESS/i), "EXCESS");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(sentence);
    expect(alert).toHaveTextContent(/excess_enable_refused/);
    // The refused enable leaves the toggle exactly where it was.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

// --- the night-charge tile (off-peak night strategy, feature-detected) ---------
//
// The tile is the night strategy's whole story in one glance
// (DESIGN_NIGHT_CHARGE.md §7 W2), the solar tile's nocturnal mirror: ACTIVE
// phases name their facts in the design's own wording (pacing names the
// window's end and every battery's target; holding names the demand rule's
// no-cycling guarantee; complete names the moment), INACTIVE states render
// the FIRST reason code's plain sentence from the ONE pinned vocabulary, the
// demand reading carries its evidence word (a non-good rollup holds
// fail-closed and is loudly visible), and the window countdowns derive from
// the projection's own instants. Everything is feature-detected: no
// night_charge_state in the snapshot renders NOTHING.

const NIGHT_REGION = /night charging/i;

function nightWorld(
  night: WireNightChargeState,
  extra: Partial<FleetView> = {},
): FleetView {
  return {
    ...fleet([unit({ unit_id: "pod-mid" }), unit({ unit_id: "pod-rhs" }), unit({ unit_id: "pod-lhs" })]),
    night_charge_state: night,
    ...extra,
  };
}

describe("HomeView — the night-charge tile", () => {
  it("renders nothing at all while the snapshot carries no night_charge_state (feature detection)", async () => {
    // Today's backend sends no night_charge_state: no tile, no heading, no
    // toggle — the Home view is exactly what it was before the feature existed.
    installClient();
    renderHome();
    await dataLanded();
    expect(screen.queryByRole("region", { name: NIGHT_REGION })).toBeNull();
    expect(screen.queryByRole("heading", { name: NIGHT_REGION })).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("words the pacing story with every battery's own target and sits beside the solar tile", async () => {
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          units: [
            nightUnitState({ unit_id: "mid", soc_pct: 88, target_w: 1900 }),
            nightUnitState({ unit_id: "lhs", soc_pct: 71.4, target_w: 1200 }),
            nightUnitState({ unit_id: "rhs", soc_pct: 98, phase: "skipped_full", target_w: 0, reason: "at_ceiling" }),
          ],
        }),
        // Both features composed: the night tile follows its daytime mirror.
        { adviser_state: adviserState() },
      ),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region).toHaveTextContent(
      /Charging toward full by 06:00: mid 1,900 W · lhs 1,200 W · rhs full, sitting out\./,
    );
    // The tile sits after the solar tile and before the reserve card — the
    // two advisers are complements, never a minute shared.
    const solar = screen.getByRole("region", { name: SOLAR_REGION });
    const reserve = screen.getByRole("region", { name: RESERVE_REGION });
    expect(
      Boolean(solar.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(
      Boolean(region.compareDocumentPosition(reserve) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
  });

  it("carries the per-battery rows: each battery's own charge figure, target, and reason", async () => {
    installClient({
      snapshot: nightWorld(nightChargeState()),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    const rows = within(region).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]!).toHaveTextContent(/lhs — 71\.4% charged · lhs 2,500 W \(on plan\)/);
    expect(rows[1]!).toHaveTextContent(/mid — 88% charged · mid 2,500 W \(on plan\)/);
    expect(rows[2]!).toHaveTextContent(
      /rhs — 98% charged · rhs full, sitting out \(at the charge ceiling\)/,
    );
  });

  it("carries the demand reading with its threshold and evidence word, never zero-filled", async () => {
    installClient({
      snapshot: nightWorld(
        nightChargeState({ demand_w: null, demand_evidence: "stale", phase: "holding_on_demand", reason_codes: ["demand_evidence_stale"] }),
      ),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    const demand = within(region).getByText(/House demand/);
    // The whole line, exact: the figure is "not available" (never a filled 0),
    // the threshold is named, and the evidence word rides with it.
    expect(demand.textContent).toBe(
      "House demand not available · holds above 1,000 W · reading stale",
    );
    // The fail-closed hold is loudly visible in the status sentence, never a
    // silent never-charges.
    expect(region).toHaveTextContent(
      /Holding — the demand reading is stale: batteries neither drain nor cycle while the grid meets the spike\./,
    );
  });

  it("words the window countdowns: the open end while running, the next opening outside", async () => {
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          enabled: false,
          active: false,
          phase: "idle",
          held_intent_id: null,
          window_ends_at: null,
          window_ends_in_s: null,
          next_window_at: "2026-08-28T00:00:00+10:00",
          reason_codes: ["outside_window"],
        }),
      ),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    // The next window's instant is on the projection; the countdown derives
    // from it (the captured_at fixture anchors the clock to render-time, so
    // only the shape is pinned here, not a frozen figure).
    expect(region).toHaveTextContent(/Window 00:00–06:00 — next opens 00:00 \(in \d+ h \d+ min\)\./);
    expect(region).toHaveTextContent(
      /Outside the charging window \(00:00–06:00\) — the next window opens at 00:00\./,
    );
  });

  // The ACTIVE phase sentences, parametrized — the design's own §7 W2 wording.
  it.each([
    {
      phase: "pacing",
      fixture: {} as Partial<WireNightChargeState>,
      sentence: /Charging toward full by 06:00: lhs 2,500 W · mid 2,500 W · rhs full, sitting out\./,
    },
    {
      phase: "holding_on_demand",
      fixture: {
        demand_w: 2340,
        reason_codes: ["demand_above_threshold"],
        units: [nightUnitState({ phase: "holding_on_demand", target_w: 100, reason: "demand_above_threshold" })],
      } as unknown as Partial<WireNightChargeState>,
      sentence:
        /Holding — house demand 2,340 W: batteries neither drain nor cycle while the grid meets the spike\./,
    },
    {
      phase: "complete",
      fixture: {
        active: false,
        held_intent_id: null,
        window_ends_at: "2026-08-27T04:12:00+10:00",
        window_ends_in_s: 0,
        reason_codes: ["target_reached"],
        units: [nightUnitState({ phase: "complete", target_w: 0, reason: "target_reached" })],
      } as unknown as Partial<WireNightChargeState>,
      sentence: /Batteries full — window complete at 04:12\./,
    },
    {
      phase: "skipped_full",
      fixture: {
        active: false,
        held_intent_id: null,
        reason_codes: ["at_ceiling"],
        units: [nightUnitState({ phase: "skipped_full", target_w: 0, reason: "at_ceiling" })],
      } as unknown as Partial<WireNightChargeState>,
      sentence: /Batteries were already full — nothing to charge this window\./,
    },
  ])("words the active $phase story in the design's own terms", async ({ phase, fixture, sentence }) => {
    installClient({
      snapshot: nightWorld(nightChargeState({ phase, ...fixture })),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region).toHaveTextContent(sentence);
  });

  // One plain sentence per reason code in the ONE pinned vocabulary — the
  // parametrization IS the completeness pin: a code added to the wire
  // vocabulary without a row here fails the completeness test below.
  const INACTIVE_CASES: { code: string; sentence: RegExp; fixture?: Partial<WireNightChargeState> }[] = [
    {
      code: "outside_window",
      sentence: /Outside the charging window \(00:00–06:00\)/,
      fixture: {
        enabled: false,
        active: false,
        phase: "idle",
        held_intent_id: null,
        window_ends_at: null,
        window_ends_in_s: null,
        next_window_at: "2026-08-28T00:00:00+10:00",
      },
    },
    {
      code: "window_open",
      sentence: /The charging window \(00:00–06:00\) is open\./,
    },
    {
      code: "on_plan",
      sentence: /Charging to plan\./,
    },
    {
      code: "deadline_at_risk",
      sentence: /Behind the plan — charging at the cap to reach full by the window's end\./,
    },
    {
      code: "demand_above_threshold",
      sentence: /House demand is above the hold line — batteries held, not cycling\./,
    },
    {
      code: "demand_below_exit",
      sentence: /House demand has fallen back below the hold line — charging resumes\./,
    },
    {
      code: "demand_evidence_missing",
      sentence: /Demand reading missing — holding so the batteries neither drain nor cycle\./,
      fixture: { demand_w: null, demand_evidence: "missing" },
    },
    {
      code: "demand_evidence_bad",
      sentence: /Demand reading bad — holding so the batteries neither drain nor cycle\./,
      fixture: { demand_w: null, demand_evidence: "bad" },
    },
    {
      code: "demand_evidence_stale",
      sentence: /Demand reading stale — holding so the batteries neither drain nor cycle\./,
      fixture: { demand_w: null, demand_evidence: "stale" },
    },
    {
      code: "at_ceiling",
      sentence: /At the charge ceiling — nothing to charge\./,
    },
    {
      code: "no_charge_headroom",
      sentence: /The battery's own charge limit says full — nothing it will accept\./,
    },
    {
      code: "target_reached",
      sentence: /Targets reached — nothing left to charge\./,
    },
    {
      code: "no_eligible_units",
      sentence: /Window open, but no battery can charge \(full, held by another request, or no headroom\)\./,
    },
    {
      code: "units_disarmed",
      // Rendered as the arm instruction it is (§7 W2): the runner can never
      // arm itself, and every restart disarms again.
      sentence: /The batteries are disarmed — arm them before the window opens\./,
      fixture: {
        active: false,
        phase: "idle",
        held_intent_id: null,
        units: [nightUnitState({ phase: "sitting_out", target_w: 0, reason: "units_disarmed" })],
      },
    },
    {
      code: "yielding_to_higher_priority",
      sentence: /Standing down — another request has priority on the batteries\./,
    },
    {
      code: "disabled_by_config",
      sentence: /Night charging is off \(config\)\./,
      fixture: { enabled: false, enabled_origin: "config" },
    },
    {
      code: "disabled_by_runtime",
      sentence: /Night charging is off until the controller restarts/,
      fixture: { enabled: false, enabled_origin: "runtime" },
    },
    {
      code: "night_acknowledgement_required",
      sentence:
        /Waiting on the one-time night-partition acknowledgement before night charging can start\./,
      fixture: { acknowledged_partition: false },
    },
  ];

  it("maps every code in the pinned vocabulary to a plain sentence (the table is complete)", () => {
    // A vocabulary code with no row here would render a raw code to an
    // operator; a table row with no vocabulary code is dead weight.
    const tabled = INACTIVE_CASES.map((entry) => entry.code).sort();
    expect(tabled).toEqual([...NIGHT_REASON_CODES].sort());
  });

  it.each(INACTIVE_CASES)(
    "renders the honest inactive sentence for $code",
    async ({ code, sentence, fixture }) => {
      installClient({
        snapshot: nightWorld(
          nightChargeState({
            enabled: false,
            active: false,
            phase: "idle",
            held_intent_id: null,
            reason_codes: [code],
            ...fixture,
          }),
        ),
      });
      renderHome();
      const region = await screen.findByRole("region", { name: NIGHT_REGION });
      expect(region).toHaveTextContent(sentence);
    },
  );

  it.each([
    [{ enabled: true, enabled_origin: "config" } as Partial<WireNightChargeState>, "On (config)"],
    [{ enabled: true, enabled_origin: "runtime" } as Partial<WireNightChargeState>, "On — until restart"],
    [{ enabled: false, enabled_origin: "config" } as Partial<WireNightChargeState>, "Off (config)"],
    [{ enabled: false, enabled_origin: "runtime" } as Partial<WireNightChargeState>, "Off — until restart"],
  ])("shows the current state and origin ($enabled_origin, enabled $enabled)", async (fixture, expected) => {
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          active: false,
          phase: "idle",
          held_intent_id: null,
          reason_codes: ["outside_window"],
          ...fixture,
        }),
      ),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region).toHaveTextContent(`Night charging: ${expected}`);
    // The runtime origin is the honest transience marker: a restart re-reads
    // the commissioned config, and the console says so.
    if (fixture.enabled_origin === "runtime") {
      expect(region).toHaveTextContent(/the config's own setting takes over at restart/);
    }
  });

  it("composes the charged-overnight story with the scorecard's energy_today where the design pins it", async () => {
    installClient({
      snapshot: nightWorld(nightChargeState(), {
        energy_today: energyToday(),
      }),
    });
    renderHome();

    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    // The nightly charge is real grid import; the scorecard measures it from
    // day one (§6), and the money line stays absent until tariff keys exist —
    // named, never invented (no wire shape carries the rate).
    expect(region).toHaveTextContent(
      /Charging at night is real grid import — the energy scorecard measures it \(bought from the grid today so far: 8\.4 kWh\)\. The cost appears once the tariff keys are commissioned\./,
    );
  });

  it("renders no energy line while the scorecard is not composed (the night story stands alone)", async () => {
    installClient({ snapshot: nightWorld(nightChargeState()) });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region.textContent ?? "").not.toMatch(/grid import/);
  });

  it("updates the tile live from state_changed frames: a phase change announces, a heartbeat re-prices silently", async () => {
    const world = nightWorld(nightChargeState({ demand_w: 412 }));
    const channel = liveChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: channel.openEvents });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region).toHaveTextContent(/Charging toward full by 06:00/);
    expect(region).toHaveTextContent(/House demand 412 W/);

    // The demand rule engages: the frame alone moves the tile — no snapshot
    // refetch, no reload — and the phase change reaches the live region.
    channel.push(
      nightChargeStateChanged(43, {
        phase: "holding_on_demand",
        demand_w: 2340,
        reason_codes: ["demand_above_threshold"],
        units: [nightUnitState({ phase: "holding_on_demand", target_w: 100, reason: "demand_above_threshold" })],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(region).toHaveTextContent(
        /Holding — house demand 2,340 W: batteries neither drain nor cycle while the grid meets the spike\./,
      );
    });
    expect(
      await screen.findByText(
        /Night charging is holding — house demand is high; the batteries neither drain nor cycle\./,
      ),
    ).toBeInTheDocument();

    // A heartbeat republish: same state tuple, fresher figures — the tile
    // re-prices with NO new announcement.
    channel.push(
      nightChargeStateChanged(44, {
        heartbeat: true,
        phase: "holding_on_demand",
        demand_w: 2510,
        reason_codes: ["demand_above_threshold"],
        units: [nightUnitState({ phase: "holding_on_demand", target_w: 100, reason: "demand_above_threshold" })],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(region).toHaveTextContent(/Holding — house demand 2,510 W/);
    });
    expect(screen.queryAllByText(/Night charging is holding/)).toHaveLength(1);
  });

  it("announces the stand-down when the window hands the batteries back over the stream", async () => {
    const world = nightWorld(nightChargeState());
    const channel = liveChannel([snapshotFrame(world)]);
    installClient({ snapshot: world, openEvents: channel.openEvents });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    channel.push(
      nightChargeStateChanged(43, {
        enabled: false,
        enabled_origin: "runtime",
        active: false,
        phase: "idle",
        held_intent_id: null,
        window_ends_at: null,
        window_ends_in_s: null,
        next_window_at: "2026-08-28T00:00:00+10:00",
        reason_codes: ["disabled_by_runtime"],
      }) as unknown as StreamFrame,
    );
    await waitFor(() => {
      expect(screen.getByText(/Night charging is off until the controller restarts/)).toBeInTheDocument();
    });
    expect(
      await screen.findByText(/Night charging stood down — the batteries are back on their own\./),
    ).toBeInTheDocument();
  });
});

// --- the night-charging toggle (§3.4/B4's guarded confirmation) -----------------
//
// The tile's footer is the feature's front door: the current state and its
// origin, a switch that never flips directly — it opens the typed-confirmation
// dialog — and the FIRST enable's one-time night-partition acknowledgement
// (the §8 item 2 assertion verbatim, a required checkbox, sent as
// "night_posture": "PARTITION_ACKNOWLEDGED", never asked again). Refusals
// render their envelopes inline; the 200's projection is adopted
// optimistically. Disable asks for the NIGHT confirmation only.

describe("HomeView — the night-charging toggle", () => {
  it("first enable: NIGHT and the partition acknowledgement are both required, and the acknowledgement is sent", async () => {
    const user = userEvent.setup();
    // The ungranted boot state: composed but suspended, never acknowledged.
    const offUnacked = nightChargeState({
      enabled: false,
      enabled_origin: "config",
      acknowledged_partition: false,
      active: false,
      phase: "idle",
      held_intent_id: null,
      window_ends_at: null,
      window_ends_in_s: null,
      next_window_at: "2026-08-28T00:00:00+10:00",
      reason_codes: ["night_acknowledgement_required"],
    });
    const enabledByToggle = nightChargeState({
      enabled: true,
      enabled_origin: "runtime",
      acknowledged_partition: true,
      phase: "idle",
      active: false,
      held_intent_id: null,
      window_ends_at: null,
      window_ends_in_s: null,
      next_window_at: "2026-08-28T00:00:00+10:00",
      reason_codes: ["outside_window"],
    });
    const postNightCharging = vi.fn(() => Promise.resolve(nightChargingToggleOk(enabledByToggle)));
    installClient({
      snapshot: nightWorld(offUnacked),
      postNightCharging,
    });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });

    // The switch opens the dialog; it never flips directly.
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on night charging/i });
    // The §8 item 2 instruction is stated, with the assertion verbatim as the
    // required checkbox label.
    expect(within(dialog).getByText(/One-time night-partition confirmation/i)).toBeVisible();
    expect(within(dialog).getByLabelText(/the external writer applications stand down/i)).not.toBeChecked();
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();

    // NIGHT alone is not enough while the acknowledgement is unchecked.
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    expect(confirm).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox"));
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    // The acknowledgement rides the first enable ever — and only it.
    await waitFor(() => {
      expect(postNightCharging).toHaveBeenCalledWith("enable", {
        nightPosture: "PARTITION_ACKNOWLEDGED",
      });
    });

    // The 200's night_charge_state is adopted optimistically: the dialog
    // closes and the tile speaks the post-toggle state.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(region).toHaveTextContent("Night charging: On — until restart");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("the acknowledgement is captured once: a later enable never asks again and never re-sends night_posture", async () => {
    const user = userEvent.setup();
    // Disabled at runtime, but the site HAS the durable fact (either surface's
    // capture counts — it is one site fact).
    const offAcked = nightChargeState({
      enabled: false,
      enabled_origin: "runtime",
      acknowledged_partition: true,
      active: false,
      phase: "idle",
      held_intent_id: null,
      window_ends_at: null,
      window_ends_in_s: null,
      next_window_at: "2026-08-28T00:00:00+10:00",
      reason_codes: ["outside_window"],
    });
    const postNightCharging = vi.fn(() =>
      Promise.resolve(
        nightChargingToggleOk(
          nightChargeState({
            enabled: true,
            enabled_origin: "runtime",
            acknowledged_partition: true,
            phase: "idle",
            active: false,
            held_intent_id: null,
            window_ends_at: null,
            window_ends_in_s: null,
            next_window_at: "2026-08-28T00:00:00+10:00",
            reason_codes: ["outside_window"],
          }),
        ),
      ),
    );
    installClient({
      snapshot: nightWorld(offAcked),
      postNightCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on night charging/i });
    // No acknowledgement step the second time — captured once, never re-prompted.
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      // No night_posture key: the site's captured fact already satisfies it.
      expect(postNightCharging).toHaveBeenCalledWith("enable", {});
    });
  });

  it("disable asks only for the typed NIGHT confirmation", async () => {
    const user = userEvent.setup();
    const onByConfig = nightChargeState({ enabled: true, enabled_origin: "config" });
    const offByToggle = nightChargeState({
      enabled: false,
      enabled_origin: "runtime",
      active: false,
      phase: "idle",
      held_intent_id: null,
      window_ends_at: null,
      window_ends_in_s: null,
      next_window_at: "2026-08-28T00:00:00+10:00",
      reason_codes: ["outside_window"],
    });
    const postNightCharging = vi.fn(() => Promise.resolve(nightChargingToggleOk(offByToggle)));
    installClient({
      snapshot: nightWorld(onByConfig),
      postNightCharging,
    });
    renderHome();
    const region = await screen.findByRole("region", { name: NIGHT_REGION });
    expect(region).toHaveTextContent("Night charging: On (config)");

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn off night charging/i });
    // Stopping is the safety-positive direction: no acknowledgement step, and
    // disable is never refused.
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const confirm = within(dialog).getByRole("button", { name: "Turn off" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(postNightCharging).toHaveBeenCalledWith("disable", {});
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(region).toHaveTextContent("Night charging: Off — until restart");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("renders the not-commissioned refusal inline and keeps the dialog open", async () => {
    const user = userEvent.setup();
    const postNightCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "night_charging_not_commissioned",
          "Night charging is not commissioned in this deployment's config.",
        ),
      ),
    );
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          enabled: true,
          acknowledged_partition: true,
          active: false,
          phase: "idle",
          held_intent_id: null,
          window_ends_at: null,
          window_ends_in_s: null,
          next_window_at: "2026-08-28T00:00:00+10:00",
          reason_codes: ["outside_window"],
        }),
      ),
      postNightCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    // A deployment whose block vanished between snapshot and toggle: the
    // refusal names the honest state — nothing to turn off there either.
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn off/i });
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/not commissioned in this deployment's config/);
    expect(alert).toHaveTextContent(/night_charging_not_commissioned/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("routes the acknowledgement-required refusal into the acknowledgement step (the 409 IS the routing)", async () => {
    const user = userEvent.setup();
    // The console's state says the fact is captured, but the site's durable
    // record says otherwise (a restart, another console, the schedules surface
    // never having captured it): the refusal routes the dialog into the step.
    const postNightCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "night_acknowledgement_required",
          "The night-partition acknowledgement must be captured before the first enable.",
          { acknowledgement: "PARTITION_ACKNOWLEDGED" },
        ),
      ),
    );
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          enabled: false,
          acknowledged_partition: true,
          active: false,
          phase: "idle",
          held_intent_id: null,
          window_ends_at: null,
          window_ends_in_s: null,
          next_window_at: "2026-08-28T00:00:00+10:00",
          reason_codes: ["outside_window"],
        }),
      ),
      postNightCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on/i });
    // The stale local flag showed no acknowledgement step…
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));

    // …the refusal brings it in, with its plain sentence.
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      /night-partition acknowledgement is required before the first enable/,
    );
    expect(within(dialog).getByRole("checkbox")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    {
      holding: "units active under another request",
      details: { reasons: ["unit_active_under_intent"], unit_ids: ["lhs", "mid"], stop_ids: [] },
      sentence:
        /Cannot enable while a request is still active on lhs, mid — finish or cancel the request on lhs, mid first\./,
    },
    {
      holding: "a latched stop",
      details: { reasons: ["latched_stop_holds"], unit_ids: [], stop_ids: ["stop-7"] },
      sentence:
        /Cannot enable while an emergency stop holds the fleet \(stop-7\) — acknowledge the stop first\./,
    },
  ])("names what holds a refused enable: $holding", async ({ details, sentence }) => {
    const user = userEvent.setup();
    const postNightCharging = vi.fn(() =>
      Promise.reject(
        refuse(
          409,
          "night_enable_refused",
          "The fleet is not in a state where night charging can start.",
          details,
        ),
      ),
    );
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          enabled: false,
          acknowledged_partition: true,
          active: false,
          phase: "idle",
          held_intent_id: null,
          window_ends_at: null,
          window_ends_in_s: null,
          next_window_at: "2026-08-28T00:00:00+10:00",
          reason_codes: ["outside_window"],
        }),
      ),
      postNightCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on/i });
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(sentence);
    expect(alert).toHaveTextContent(/night_enable_refused/);
    // The refused enable leaves the toggle exactly where it was.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renders a validation refusal's envelope verbatim beside its plain sentence", async () => {
    const user = userEvent.setup();
    const postNightCharging = vi.fn(() =>
      Promise.reject(
        refuse(422, "validation_error", "Request validation failed", {
          errors: [{ field: "confirmation", message: "the typed confirmation must be NIGHT" }],
        }),
      ),
    );
    installClient({
      snapshot: nightWorld(
        nightChargeState({
          enabled: false,
          acknowledged_partition: true,
          active: false,
          phase: "idle",
          held_intent_id: null,
          window_ends_at: null,
          window_ends_in_s: null,
          next_window_at: "2026-08-28T00:00:00+10:00",
          reason_codes: ["outside_window"],
        }),
      ),
      postNightCharging,
    });
    renderHome();
    await screen.findByRole("region", { name: NIGHT_REGION });

    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog", { name: /Turn on/i });
    await user.type(within(dialog).getByLabelText(/Type NIGHT/i), "NIGHT");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    const alert = await within(dialog).findByRole("alert");
    // The envelope is the authority: its code and message render verbatim.
    expect(alert).toHaveTextContent(/validation_error/);
    expect(alert).toHaveTextContent(/Request validation failed/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The self-healing awareness layer (API_CONTRACTS.md "Self-healing awareness
// layer (recovery detection)" — live on the controller since b20058d..7b491b3):
// the per-unit health badge on the "What is powering the home?" unit lines.
// Wire truth: the snapshot units carry health_state / health_reasons /
// remediation_hint once the layer is composed (nulls when its projection is
// absent, keys absent on the older wire), and the bus carries
// unit.health_changed / actuation.incoherent / unit.unexpected_autonomy
// (fixtures from wire.ts). The badge's own per-state wording is pinned in
// web/src/app/unitHealth.test.tsx; this suite pins the Home lines' adoption.
// ---------------------------------------------------------------------------

describe("HomeView — the per-unit recovery health badge", () => {
  function healingFleet(
    health: { state: string; reasons?: readonly string[]; remediation_hint?: string | null },
    authorized?: { direction: "charge" | "discharge"; watts: number },
    measuredWatts?: number,
  ): FleetView {
    return fleet([
      {
        ...unit({
          unit_id: "pod-mid",
          lifecycle: "active",
          ...(measuredWatts === undefined ? {} : { measured_watts: measuredWatts }),
          ...(authorized === undefined
            ? {}
            : {
                requested_power: { direction: authorized.direction, watts: authorized.watts },
                authorized_power: { direction: authorized.direction, watts: authorized.watts },
              }),
        }),
        health_state: health.state,
        health_reasons: health.reasons ?? [],
        remediation_hint: health.remediation_hint ?? null,
      },
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
  }

  it.each([
    ["self_healing on solar self-charge", "self_healing", ["autonomous_self_charge"], "Managing itself — solar self-charge"],
    ["self_healing re-qualifying", "self_healing", ["requalifying_after_inhibit"], "Re-qualifying"],
    ["self_healing cell balancing", "self_healing", ["cell_balancing"], "Cell balancing"],
    ["an unreachable gateway path", "unreachable", ["gateway_unreachable"], "Gateway unreachable"],
  ] as const)("shows %s on the unit line", async (_name, state, reasons, words) => {
    installClient({ snapshot: healingFleet({ state, reasons }) });
    renderHome();
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, words);
    });
    // The units without health fields stay silent.
    const quiet = await findUnitEntry(POWER_REGION, "pod-rhs");
    expect(within(quiet).queryByRole("note")).toBeNull();
  });

  it.each([
    ["charging gently (the evening mid/rhs story, negative watts)", -780.5, "Managing itself — solar self-charge"],
    ["load-serving (the evening lhs story, ~+780 W)", 780, "Managing itself — powering the home"],
  ] as const)("words the solar self-charge line by the unit's own measured direction: %s", async (_name, watts, words) => {
    installClient({
      snapshot: healingFleet(
        { state: "self_healing", reasons: ["autonomous_self_charge"] },
        undefined,
        watts,
      ),
    });
    renderHome();
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, words);
    });
  });

  it("tells the plain incoherence story with the battery's own allowed figure", async () => {
    installClient({
      snapshot: healingFleet(
        { state: "actuation_incoherent", reasons: ["authorized_not_actuating"] },
        { direction: "discharge", watts: 1000 },
      ),
    });
    renderHome();
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, "Commanded 1,000 W but the battery isn't moving — investigating");
    });
  });

  it("shows the prominent honest terminal with the hint VERBATIM", async () => {
    const hint =
      "pod not responding — remote recovery exhausted; physical restart required (power-cycle the pod, then verify telemetry resumes, Debug Mode reads Normal Mode and SysControlMode reads Remote in the vendor MiniES app — see docs/POD_RECOVERY_RESEARCH.md R5)";
    installClient({
      snapshot: healingFleet({ state: "not_responding", reasons: ["reads_timing_out"], remediation_hint: hint }),
    });
    renderHome();
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, "Not responding — remote recovery exhausted");
    });
    // The backend's own remediation words, whole and un-reworded.
    const hintNode = within(entry).getAllByText(hint).at(0);
    expect(hintNode).toBeDefined();
  });

  it.each([
    ["a healthy unit (silence is the design)", "healthy"],
    ["a foreign writer (the Inhibited badge and latch surfaces tell it)", "foreign_writer"],
    ["an inhibited unit (same existing surfaces)", "inhibited"],
  ] as const)("renders NO health badge for %s", async (_name, state) => {
    installClient({ snapshot: healingFleet({ state, reasons: [] }) });
    renderHome();
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    expect(within(entry).queryByRole("note")).toBeNull();
    expect(document.querySelectorAll(".unit-health")).toHaveLength(0);
  });

  it("renders NO health badge when the snapshot carries no health fields at all (the older wire)", async () => {
    installClient({ snapshot: fleet(allUnits("armed_idle")) });
    renderHome();
    await dataLanded();
    expect(document.querySelectorAll(".unit-health")).toHaveLength(0);
  });

  it("applies a live unit.health_changed transition on the unit line without a refetch", async () => {
    const snapshot = fleet(allUnits("disarmed"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    const getSnapshot = vi.fn(() => Promise.resolve(snapshot));
    installClient({ snapshot, getSnapshot, openEvents: channel.openEvents });
    renderHome();
    await dataLanded();

    channel.push(
      unitHealthChanged(43, {
        unit_id: "pod-rhs",
        from: "healthy",
        to: "self_healing",
        reasons: ["autonomous_self_charge"],
      }),
    );
    const entry = await findUnitEntry(POWER_REGION, "pod-rhs");
    await waitFor(() => {
      expectVisibleText(entry, "Managing itself — solar self-charge");
    });
    // The frame alone moved the badge — no event-driven snapshot read.
    expect(getSnapshot).toHaveBeenCalledTimes(1);
  });

  it("moves the unit line to the warning on an actuation.incoherent detection", async () => {
    // The snapshot carries NO health fields: the badge appears only because
    // the detection frame itself moved the state.
    const snapshot = fleet([
      unit({
        unit_id: "pod-mid",
        lifecycle: "active",
        requested_power: { direction: "discharge", watts: 2400 },
        authorized_power: { direction: "discharge", watts: 2400 },
      }),
      unit({ unit_id: "pod-rhs" }),
      unit({ unit_id: "pod-lhs" }),
    ]);
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: channel.openEvents });
    renderHome();
    await dataLanded();

    channel.push(
      actuationIncoherent(43, {
        unit_id: "pod-mid",
        authorized_watts: 1000,
        cycles: 4,
      }),
    );
    const entry = await findUnitEntry(POWER_REGION, "pod-mid");
    await waitFor(() => {
      expectVisibleText(entry, "Commanded 2,400 W but the battery isn't moving — investigating");
    });
  });

  it("keeps unexpected_autonomy evidence off the Home lines entirely", async () => {
    const snapshot = fleet(allUnits("disarmed"));
    const channel = liveChannel([snapshotFrame(snapshot)]);
    installClient({ snapshot, openEvents: channel.openEvents });
    renderHome();
    await dataLanded();

    channel.push(unitUnexpectedAutonomy(43, { unit_id: "pod-mid", measured_watts: 1411.2 }));
    await waitFor(() => {
      expect(channel).toBeDefined();
    });
    expect(document.querySelectorAll(".unit-health")).toHaveLength(0);
    expect(screen.queryByText(/uncommanded activity/i)).toBeNull();
    // And no announcement was spent on it: the polite region stays as it was.
    expect(screen.queryByText(/uncommanded/i)).toBeNull();
  });
});

// --- the next-scheduled-action card (§6 W-C) ----------------------------------
//
// Feature-detected exactly like the solar tile: no `schedule_state` in the
// snapshot, no card. The running state does not duplicate the request figures
// (a running window's intent rides the ordinary request paths); the next
// state carries the pure next-occurrence object's own figures; countdowns
// are snapshot-derived and ticked locally; the posture line states the
// commissioned windows from the policy read, never a guessed DAY_DEFAULT.

const SCHEDULE_REGION = /next scheduled action/i;

function scheduleWorld(state: Partial<WireScheduleState>): FleetView {
  return { ...fleet(allUnits("disarmed")), schedule_state: wireScheduleState(state) };
}

describe("HomeView — the next-scheduled-action card", () => {
  it("renders nothing at all while the snapshot carries no schedule_state (feature detection)", async () => {
    const getSchedule = vi.fn(() => Promise.resolve(getScheduleOk({})));
    installClient({ snapshot: fleet(allUnits("disarmed")), getSchedule });
    renderHome();
    await dataLanded();

    expect(screen.queryByRole("region", { name: SCHEDULE_REGION })).toBeNull();
    // The card's facts read never ran: an absent projection never reads.
    expect(getSchedule).not.toHaveBeenCalled();
    // The old empty sentence stands verbatim when the feature is absent.
    expect(screen.getByText(/No planned action — nothing is scheduled/i)).toBeVisible();
  });

  it("shows the next occurrence with its own figures and a starts-in countdown", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: false,
        entry_id: null,
        held_intent_id: null,
        ends_at: null,
        ends_in_s: null,
        reason_codes: ["no_window_open"],
        next: {
          entry_id: "Night Charge",
          days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
          start_local: "00:01",
          end_local: "05:59",
          action: "charge",
          watts_by_unit: { lhs: 2500, mid: 2500, rhs: 2500 },
          unit_ids: ["lhs", "mid", "rhs"],
          starts_at: "2026-08-24T00:01:00+10:00",
          starts_in_s: 3600,
        },
      }),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    expect(region).toHaveTextContent(/Next: Night Charge/);
    expect(region).toHaveTextContent(/2,500 W per battery \(lhs, mid, rhs\)/);
    expect(region).toHaveTextContent(/starts 00:01, in 1 h\./);
    // The posture line states the commissioned windows from the policy read
    // (the read lands just after the card renders).
    await waitFor(() => {
      expect(region).toHaveTextContent(
        /Schedules run 06:00–20:00 local — day-only; the night window stays with the site's other applications\./,
      );
    });
  });

  it("running now: names the window and its end, and points at the request cards instead of duplicating them", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: true,
        entry_id: "Night Charge",
        ends_at: "2026-08-24T05:59:00+10:00",
        ends_in_s: 2743,
        reason_codes: ["window_open"],
        next: null,
      }),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    expect(region).toHaveTextContent(/Night Charge is running now — ends in 45 min \(at 05:59\)/);
    expect(region).toHaveTextContent(/rides the normal request path/i);
    // No per-battery watts are restated as the card's own figures.
    expect(region.textContent ?? "").not.toMatch(/2,500 W/);
  });

  it("waiting: a manual request holding the batteries is said plainly", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: true,
        entry_id: "Night Charge",
        ends_at: "2026-08-24T05:59:00+10:00",
        ends_in_s: 2743,
        reason_codes: ["waiting_for_higher_priority"],
        next: null,
      }),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    expect(region).toHaveTextContent(
      /Night Charge is waiting — a manual request holds its batteries\. It takes over the moment the other request ends\./,
    );
  });

  it.each([
    [
      "no plan published yet",
      getScheduleOk({ plan: null }),
      /No schedules yet — add one in Schedule\./,
    ],
    [
      "a plan whose every entry is paused",
      getScheduleOk({ plan: schedulePlan([scheduleEntry({ enabled: false })]) }),
      /Nothing is coming up — every schedule is paused or past its date range\./,
    ],
  ])("honest empty (%s) plus the posture line", async (_name, body, expected) => {
    installClient({
      snapshot: scheduleWorld({
        active: false,
        entry_id: null,
        held_intent_id: null,
        ends_at: null,
        ends_in_s: null,
        reason_codes: ["no_window_open"],
        next: null,
      }),
      getSchedule: vi.fn(() => Promise.resolve(body as unknown as Record<string, unknown>)),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    await waitFor(() => {
      expect(region).toHaveTextContent(expected);
    });
    expect(region).toHaveTextContent(/Schedules run 06:00–20:00 local/);
  });

  it("states the partition posture when the config granted the night window", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: false,
        entry_id: null,
        held_intent_id: null,
        ends_at: null,
        ends_in_s: null,
        reason_codes: ["no_window_open"],
        next: null,
      }),
      getSchedule: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
          }) as unknown as Record<string, unknown>,
        ),
      ),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    await waitFor(() => {
      expect(region).toHaveTextContent(
        /Schedules run 20:00–06:00 local — night granted to the controller by config\./,
      );
    });
  });

  it("keeps the card honest when the policy read fails: the card stays, the posture line goes", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: false,
        entry_id: null,
        held_intent_id: null,
        ends_at: null,
        ends_in_s: null,
        reason_codes: ["no_window_open"],
        next: null,
      }),
      getSchedule: vi.fn(() => Promise.reject(new Error("not reachable"))),
    });
    renderHome();
    const region = await screen.findByRole("region", { name: SCHEDULE_REGION });

    await waitFor(() => {
      expect(region).toHaveTextContent(/No schedules yet — add one in Schedule\./);
    });
    expect(region.textContent ?? "").not.toMatch(/Schedules run/);
  });

  it("hands the scheduled wording to the card: the requests card says requests only", async () => {
    installClient({
      snapshot: scheduleWorld({
        active: false,
        entry_id: null,
        held_intent_id: null,
        ends_at: null,
        ends_in_s: null,
        reason_codes: ["no_window_open"],
        next: null,
      }),
    });
    renderHome();
    await screen.findByRole("region", { name: SCHEDULE_REGION });

    expect(screen.getByText(/No request is running right now\./)).toBeVisible();
    expect(screen.queryByText(/nothing is scheduled for the pods/i)).toBeNull();
  });

  it("a window_opened frame flips the card to running without a poll, and closing ends it", async () => {
    const snapshot = scheduleWorld({
      active: false,
      entry_id: null,
      held_intent_id: null,
      ends_at: null,
      ends_in_s: null,
      reason_codes: ["no_window_open"],
      next: null,
    });
    const channel = liveChannel([snapshotFrame(snapshot)]);
    const getSnapshot = vi.fn(() => Promise.resolve(snapshot));
    installClient({ snapshot, getSnapshot, openEvents: channel.openEvents });
    renderHome();
    await screen.findByRole("region", { name: SCHEDULE_REGION });
    const readsBefore = getSnapshot.mock.calls.length;

    channel.push(scheduleWindowOpened(43, { entry_id: "Night Charge", version: 4 }) as unknown as StreamFrame);
    const region = screen.getByRole("region", { name: SCHEDULE_REGION });
    await waitFor(() => {
      expect(region).toHaveTextContent(/Night Charge is running now/);
    });
    // The transition moved from the frame alone: no refetch ran for it.
    expect(getSnapshot.mock.calls.length).toBe(readsBefore);
    await waitFor(() => {
      expect(screen.getByText(/Scheduled window Night Charge opened/i)).toBeVisible();
    });

    channel.push(scheduleWindowClosing(44, { entry_id: "Night Charge", version: 4 }) as unknown as StreamFrame);
    await waitFor(() => {
      expect(region).toHaveTextContent(/window just ended/);
    });
    await waitFor(() => {
      expect(screen.getByText(/Scheduled window Night Charge ended\./)).toBeVisible();
    });
  });

  it("a schedule.replaced frame re-reads the world and the plan facts", async () => {
    const snapshot = scheduleWorld({
      active: false,
      entry_id: null,
      held_intent_id: null,
      ends_at: null,
      ends_in_s: null,
      reason_codes: ["no_window_open"],
      next: null,
    });
    const channel = liveChannel([snapshotFrame(snapshot)]);
    const getSnapshot = vi.fn(() => Promise.resolve(snapshot));
    const getSchedule = vi.fn(() => Promise.resolve(getScheduleOk({})));
    installClient({ snapshot, getSnapshot, getSchedule, openEvents: channel.openEvents });
    renderHome();
    await screen.findByRole("region", { name: SCHEDULE_REGION });
    const readsBefore = getSnapshot.mock.calls.length;
    const scheduleReadsBefore = getSchedule.mock.calls.length;

    channel.push(scheduleReplaced(43, { version: 5, added: ["Night Charge"] }) as unknown as StreamFrame);
    await waitFor(() => {
      expect(getSnapshot.mock.calls.length).toBeGreaterThan(readsBefore);
    });
    await waitFor(() => {
      expect(getSchedule.mock.calls.length).toBeGreaterThan(scheduleReadsBefore);
    });
  });

  it("ticks the countdown down between snapshots (snapshot-derived, client-recomputed)", async () => {
    // The card's own countdown arithmetic, pinned directly: the wire delivers
    // 70 s; the parent's ticking clock advances 30 s; the figure runs down
    // and a FRESH wire figure re-captures the marker. (The HomeView-mounted
    // equivalent depends on the shared interval's effect flush timing, which
    // is load-sensitive under jsdom — the "advances the data age" pre-existing
    // flake in this file is the same mechanism, so this pin stays component-
    // level and deterministic.)
    const running = (endsInS: number): WireScheduleState =>
      wireScheduleState({
        active: true,
        entry_id: "Night Charge",
        ends_at: "2026-08-24T05:59:00+10:00",
        ends_in_s: endsInS,
        reason_codes: ["window_open"],
        next: null,
      });
    const t0 = performance.now();
    const view = (nowMs: number, schedule: WireScheduleState) => (
      <NextScheduleCard schedule={toScheduleState(schedule)} facts={null} nowMs={nowMs} />
    );
    const { rerender } = render(view(t0, running(70)));
    const region = screen.getByRole("region", { name: SCHEDULE_REGION });
    expect(region).toHaveTextContent(/ends in 1 min/);

    rerender(view(t0 + 30_000, running(70)));
    expect(region).toHaveTextContent(/ends in (30|3[1-9]|4[0-9]) s \(at 05:59\)/);

    // A fresh wire figure re-captures the marker: the countdown restarts from
    // the new figure and never runs from the stale base.
    rerender(view(t0, running(3600)));
    expect(region).toHaveTextContent(/ends in 1 h/);
  });
});

describe("HomeView — the energy scorecard's Today card", () => {
  const TODAY_REGION = /today \(so far\)/i;

  /** A fleet snapshot carrying the pending energy_today block. */
  function energyWorld(today: WireEnergyToday, snapshotSequence: number = SEQUENCE): FleetView {
    return { ...fleet(allUnits("disarmed"), snapshotSequence), energy_today: today };
  }

  it("renders nothing at all while the snapshot carries no energy_today (feature detection)", async () => {
    // Today's backend sends no energy_today: no card, no heading, no
    // footnote — the Home view is exactly what it was before the feature.
    installClient();
    renderHome();
    await dataLanded();
    expect(screen.queryByRole("region", { name: TODAY_REGION })).toBeNull();
    expect(screen.queryByRole("heading", { name: TODAY_REGION })).toBeNull();
    expect(screen.queryByText(/solar panels are not measured/i)).toBeNull();
  });

  it("renders the day's account directly after the powering question, figures honest", async () => {
    installClient({ snapshot: energyWorld(energyToday()) });
    renderHome();

    const region = await screen.findByRole("region", { name: TODAY_REGION });
    // The §1 sentence block, from the record's own figures (the sentence
    // carries <strong> figures, so the region's text is matched as a whole).
    expect(region).toHaveTextContent(/bought from grid 8\.4 kWh/i);
    expect(region).toHaveTextContent(/sold to grid 12\.9 kWh/i);
    expect(region).toHaveTextContent(/charged 6\.2 kWh · discharged 4\.1 kWh · house load 14\.7 kWh/i);
    expect(region).toHaveTextContent(/so far today — 98\.7% coverage/i);
    // The A/B unpinned note rides the provenance line with the integrated grid
    // source; no bought/sold role is ever assigned to either counter.
    expect(region).toHaveTextContent(/grid figures measured by the controller/i);
    expect(region).toHaveTextContent(/counter a and counter b until their roles are pinned/i);
    expect(region.textContent ?? "").not.toMatch(/[AB]\s*(is|=)\s*(bought|sold)/i);
    // The card sits after "What is powering the home?" (DESIGN §8 W-A) and
    // before the reserve card.
    const power = screen.getByRole("region", { name: POWER_REGION });
    const reserve = screen.getByRole("region", { name: RESERVE_REGION });
    expect(
      Boolean(power.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(
      Boolean(region.compareDocumentPosition(reserve) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
  });

  it("transitions the card on energy.day_rolled: the world re-reads and the new day renders", async () => {
    const first = energyWorld(energyToday({ date: "2026-08-26" }));
    const second = energyWorld(
      energyToday({
        date: "2026-08-27",
        units: {
          mid: {
            grid_import_kwh: 3.3,
            grid_export_kwh: 7.7,
            battery_charged_kwh: 9.9,
            battery_discharged_kwh: 2.2,
            load_kwh: 6.6,
            charged_from_surplus_kwh: 4.4,
            coverage_pct: 100,
            metric_flags: [],
          },
        },
      }),
      44,
    );
    let snapshotCalls = 0;
    const channel = liveChannel([snapshotFrame(first)]);
    installClient({
      snapshot: first,
      getSnapshot: () => {
        snapshotCalls += 1;
        return Promise.resolve(snapshotCalls === 1 ? first : second);
      },
      openEvents: channel.openEvents,
    });
    renderHome();
    const card = await screen.findByRole("region", { name: TODAY_REGION });
    expect(card).toHaveTextContent(/charged 6\.2 kWh/i);

    // The rollover TRANSITION (one publication per site midnight): the card
    // must not keep yesterday's figures as today's. (The bus and snapshot
    // sequence spaces are ONE numbering on the real wire — the fixture keeps
    // them in step: snapshot 42, rollover 43, the refetched world 44.)
    channel.push(
      energyDayRolled(43, energyDayRecord({ date: "2026-08-26", kind: "complete" })),
    );
    await waitFor(() => {
      expect(card).toHaveTextContent(/charged 9\.9 kWh/i);
    });
    expect(snapshotCalls).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/a new day began — today's energy figures have restarted/i)).toBeVisible();
  });
});

describe("HomeView — the night-writer detector's quiet unit-line note", () => {
  /** A fleet whose pod-mid carries the given observed-objective summary. */
  function fleetWithObjective(objective: WireLastObjective | null): FleetView {
    return fleet([
      { ...unit({ unit_id: "pod-mid", lifecycle: "disarmed" }), last_objective_observed: objective },
      unit({ unit_id: "pod-rhs", lifecycle: "disarmed" }),
    ]);
  }

  it("renders the quiet note ONLY where the detector classified an external writer", async () => {
    installClient({ snapshot: fleetWithObjective(lastObjectiveObserved()) });
    renderHome();
    const mid = await findUnitEntry(POWER_REGION, "pod-mid");
    expect(within(mid).getByRole("note").textContent).toBe(
      "Commanded by something else: -2,400 W at 23:40 (an external charge pattern — sustained charging while the site imported, with no solar surplus)",
    );
    const rhs = await findUnitEntry(POWER_REGION, "pod-rhs");
    expect(within(rhs).queryByText(/Commanded by something else/)).toBeNull();
  });

  it.each([
    [
      "in-band pod autonomy (evidence, not a household fact)",
      lastObjectiveObserved({
        observed_at: "2026-08-24T05:58:00+10:00",
        active_w: -540,
        classification: "pod_autonomy_objective_observed",
        reason: null,
      }),
    ],
    [
      "the site's expected nightly charge (the KNOWN writer, quiet by the amendment)",
      lastObjectiveObserved({
        observed_at: "2026-08-24T05:58:00+10:00",
        active_w: -2500,
        classification: "expected_nightly_charge",
        reason: null,
      }),
    ],
    [
      "our own handback grace",
      lastObjectiveObserved({
        observed_at: "2026-08-23T14:02:00+10:00",
        active_w: -900,
        classification: "handback_grace",
        reason: null,
      }),
    ],
    [
      "an unknown future classification word",
      lastObjectiveObserved({ classification: "future_word", active_w: -2400 }),
    ],
  ] as const)("renders NOTHING on Home for %s", async (_name, objective) => {
    installClient({ snapshot: fleetWithObjective(objective) });
    renderHome();
    await findUnitEntry(POWER_REGION, "pod-mid");
    expect(screen.queryByText(/Commanded by something else/)).toBeNull();
  });

  it("renders nothing new when the snapshot carries no field at all (feature-absent)", async () => {
    installClient({ snapshot: fleet([unit({ unit_id: "pod-mid", lifecycle: "disarmed" })]) });
    renderHome();
    await findUnitEntry(POWER_REGION, "pod-mid");
    expect(screen.queryByText(/Commanded by something else/)).toBeNull();
  });

  it("moves the quiet note the moment the alert-tier frame lands", async () => {
    const channel = liveChannel([]);
    installClient({
      snapshot: fleet([unit({ unit_id: "pod-mid", lifecycle: "disarmed" })]),
      openEvents: channel.openEvents,
    });
    renderHome();
    await findUnitEntry(POWER_REGION, "pod-mid");
    expect(screen.queryByText(/Commanded by something else/)).toBeNull();
    const alert = foreignObjectiveObserved(50, { unit_id: "pod-mid" });
    await act(async () => {
      channel.push({
        type: alert.type,
        sequence: alert.sequence,
        occurred_at: alert.occurred_at,
        payload: alert.payload,
      });
    });
    expect(
      await screen.findByText(
        "Commanded by something else: -2,400 W at 23:40 (an external charge pattern — sustained charging while the site imported, with no solar surplus)",
      ),
    ).toBeInTheDocument();
  });
});
