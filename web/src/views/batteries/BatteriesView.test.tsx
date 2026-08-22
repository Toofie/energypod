/**
 * Behavior contract for the "Batteries" view (docs/UI_CONTRACTS.md,
 * "Batteries"): fleet cards per named unit, card fields, latched-inhibit
 * acknowledgement, unit detail tabs, missing telemetry honesty, and keyboard
 * operation.
 *
 * The API client module is mocked at its exact surface with `createApiClient`
 * replaced and every other canonical export preserved (the view may narrow
 * rejections with `error instanceof ApiClientError`, so every rejection below
 * is a real `ApiClientError` carrying the envelope verbatim plus a status).
 * Fixtures mirror the wire exactly (src/energypod/application/service.py,
 * src/energypod/api/rest.py, src/energypod/application/events.py):
 *
 * - enums are lowercase StrEnum values ("disarmed", "observe_only", "active",
 *   "inhibited", "charge", "discharge", "idle", "good", "stale", "missing",
 *   "suspect");
 * - the first WS frame is the snapshot envelope {type, sequence, data};
 * - every event frame carries {type, sequence, occurred_at} with the
 *   event-specific fields nested under "payload" (the facade publishes
 *   {"type", "payload"} and the bus adds only sequence/occurred_at, which
 *   rest.py then sends unchanged).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";

import {
  ApiClientError,
  createApiClient,
  type ApiClient,
  type AuditPage,
  type ErrorEnvelope,
  type StreamEvent,
} from "../../api/client";
import { BatteriesView } from "./BatteriesView";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: vi.fn(),
}));

const CAPTURED_AT = "2026-08-22T10:00:00Z";

/** The health envelope exactly as the service serializes it (three facts). */
const HEALTH = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: false, reasons: ["no_unit_armed"] },
};

/**
 * The facade snapshot also exposes each unit's inhibit cause class and
 * latched flag (docs/API_CONTRACTS.md, "Inhibit acknowledgement"); the shared
 * client type keeps the wire-minimal unit, so this suite widens it locally.
 */
interface LatchState {
  cause_class: "transient" | "qualified" | "latched";
  latched: boolean;
  reason_code: string | null;
}

type WirePower = { direction: "charge" | "discharge" | "idle"; watts: number };

/** The unit view exactly as the facade serializes it (lowercase StrEnums). */
interface WireUnit {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: WirePower;
  authorized_power: WirePower | null;
  measured_watts: number | null;
  inhibit?: LatchState | null;
}

interface WireSnapshot {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: WireUnit[];
}

/** Per-unit telemetry detail carried by observation events on the socket. */
interface CellVoltages {
  min_v: number;
  max_v: number;
  spread_mv: number;
}

interface ObservationData {
  soc_percent: number;
  temperature_min_c: number;
  temperature_max_c: number;
  cells: CellVoltages | null;
  cell_data_age_s: number;
  warnings: { code: string }[];
}

/** An observation frame: event-specific fields nested under "payload". */
interface ObservationEvent extends StreamEvent {
  type: "observation";
  occurred_at: string;
  payload: {
    unit_id: string;
    data: ObservationData;
  };
}

interface MockedClient {
  getSnapshot: Mock<() => Promise<WireSnapshot>>;
  getHealth: Mock<() => Promise<typeof HEALTH>>;
  getAudit: Mock<(limit: number, afterSequence?: number) => Promise<AuditPage>>;
  postInhibitAcknowledgement: Mock<
    (unitId: string) => Promise<Record<string, unknown>>
  >;
  openEvents: Mock<(afterSequence?: number) => AsyncIterable<StreamEvent>>;
}

function makeClient(): MockedClient {
  const client: MockedClient = {
    getSnapshot: vi.fn<() => Promise<WireSnapshot>>(),
    getHealth: vi.fn<() => Promise<typeof HEALTH>>(),
    getAudit: vi.fn<
      (limit: number, afterSequence?: number) => Promise<AuditPage>
    >(),
    postInhibitAcknowledgement: vi.fn<
      (unitId: string) => Promise<Record<string, unknown>>
    >(),
    openEvents: vi.fn<(afterSequence?: number) => AsyncIterable<StreamEvent>>(),
  };
  client.getHealth.mockResolvedValue(HEALTH);
  client.getAudit.mockResolvedValue({ events: [], next_cursor: null });
  return client;
}

/**
 * Event stream that stays open after delivering the given events, or fails
 * like a dropped connection when `then` is "fail". Each `[Symbol.asyncIterator]()`
 * call starts a fresh iteration, so one returned object is replayable.
 */
function openStream(
  events: readonly StreamEvent[],
  then: "open" | "fail" = "open",
): AsyncIterable<StreamEvent> {
  return {
    async *[Symbol.asyncIterator]() {
      for (const event of events) {
        yield event;
      }
      if (then === "fail") {
        throw new Error("event stream closed unexpectedly");
      }
      await new Promise<never>(() => {});
    },
  };
}

function unit(spec: Partial<WireUnit> & Pick<WireUnit, "unit_id">): WireUnit {
  return {
    unit_id: spec.unit_id,
    lifecycle: spec.lifecycle ?? "disarmed",
    telemetry_age_s: spec.telemetry_age_s ?? 3,
    quality: spec.quality ?? "good",
    requested_power: spec.requested_power ?? { direction: "idle", watts: 0 },
    authorized_power: spec.authorized_power ?? null,
    measured_watts: spec.measured_watts ?? 0,
    inhibit: spec.inhibit ?? null,
  };
}

type ObservationSpec = Partial<ObservationData>;

function observation(
  sequence: number,
  unitId: string,
  spec: ObservationSpec = {},
): ObservationEvent {
  return {
    type: "observation",
    sequence,
    occurred_at: CAPTURED_AT,
    payload: {
      unit_id: unitId,
      data: {
        soc_percent: spec.soc_percent ?? 62,
        temperature_min_c: spec.temperature_min_c ?? 24.1,
        temperature_max_c: spec.temperature_max_c ?? 27.8,
        cells:
          spec.cells === undefined
            ? { min_v: 3.31, max_v: 3.352, spread_mv: 42 }
            : spec.cells,
        cell_data_age_s: spec.cell_data_age_s ?? 6,
        warnings: spec.warnings ?? [],
      },
    },
  };
}

const FLEET_SPEC: Record<string, ObservationSpec> = {
  MID: { warnings: [{ code: "EE_CALIBRATION_WARNING" }] },
  RHS: {
    soc_percent: 78,
    temperature_min_c: 23.0,
    temperature_max_c: 25.5,
    cells: { min_v: 3.32, max_v: 3.338, spread_mv: 18 },
  },
  LHS: {
    soc_percent: 44,
    temperature_min_c: 25.2,
    temperature_max_c: 28.0,
    cells: { min_v: 3.3, max_v: 3.341, spread_mv: 41 },
  },
};

function fleetEvents(
  snapshot: WireSnapshot,
  spec: Record<string, ObservationSpec> = FLEET_SPEC,
): StreamEvent[] {
  const events: StreamEvent[] = [
    {
      type: "snapshot",
      sequence: snapshot.snapshot_sequence,
      data: snapshot,
    },
  ];
  let sequence = snapshot.snapshot_sequence;
  for (const snapshotUnit of snapshot.units) {
    sequence += 1;
    events.push(
      observation(
        sequence,
        snapshotUnit.unit_id,
        spec[snapshotUnit.unit_id] ?? {},
      ),
    );
  }
  return events;
}

const HEALTHY_SNAPSHOT: WireSnapshot = {
  site_id: "home-1",
  snapshot_sequence: 4100,
  captured_at: CAPTURED_AT,
  units: [
    unit({
      unit_id: "LHS",
      lifecycle: "observe_only",
      telemetry_age_s: 4,
    }),
    unit({
      unit_id: "MID",
      lifecycle: "active",
      telemetry_age_s: 2,
      requested_power: { direction: "discharge", watts: 2400 },
      authorized_power: { direction: "discharge", watts: 2400 },
      measured_watts: -2400,
    }),
    unit({
      unit_id: "RHS",
      lifecycle: "disarmed",
      telemetry_age_s: 5,
    }),
  ],
};

const HEALTHY_EVENTS: StreamEvent[] = fleetEvents(HEALTHY_SNAPSHOT);

function healthyClient(
  snapshot: WireSnapshot,
  events: readonly StreamEvent[],
): MockedClient {
  const client = makeClient();
  client.getSnapshot.mockResolvedValue(snapshot);
  client.openEvents.mockReturnValue(openStream(events));
  return client;
}

function snapshotWithOrder(order: string[]): WireSnapshot {
  const byId = new Map(HEALTHY_SNAPSHOT.units.map((u) => [u.unit_id, u]));
  return {
    ...HEALTHY_SNAPSHOT,
    units: order
      .map((id) => byId.get(id))
      .filter((u): u is WireUnit => Boolean(u)),
  };
}

function latchedSnapshot(): WireSnapshot {
  return {
    ...HEALTHY_SNAPSHOT,
    units: [
      ...HEALTHY_SNAPSHOT.units.filter((u) => u.unit_id !== "MID"),
      unit({
        unit_id: "MID",
        lifecycle: "inhibited",
        telemetry_age_s: 2,
        measured_watts: 0,
        inhibit: {
          cause_class: "latched",
          latched: true,
          reason_code: "CRITICAL_BLOCKING_FAULT",
        },
      }),
    ],
  };
}

function latchedEvents(latched: WireSnapshot): StreamEvent[] {
  return fleetEvents(latched, {
    ...FLEET_SPEC,
    MID: { warnings: [{ code: "CRITICAL_BLOCKING_FAULT" }] },
  });
}

function renderView(client: MockedClient) {
  vi.mocked(createApiClient).mockReturnValue(client as unknown as ApiClient);
  const api = createApiClient("operator-token");
  return render(<BatteriesView client={api} />);
}

/**
 * The tightest text under `scope` matching every pattern: the labelled row
 * itself, never an ancestor that merely contains it.
 */
function tightestText(scope: HTMLElement, ...patterns: RegExp[]): string {
  const matches = within(scope)
    .getAllByText((_: string, element: Element | null) =>
      patterns.every((pattern) => pattern.test(element?.textContent ?? "")),
    )
    .map((element: HTMLElement) => element.textContent ?? "");
  if (matches.length === 0) {
    throw new Error(
      `expected an element matching ${patterns.map(String).join(" + ")} within the scope`,
    );
  }
  return matches.reduce(
    (tightest: string, text: string) => (text.length < tightest.length ? text : tightest),
  );
}

async function tabUntilFocused(
  user: ReturnType<typeof userEvent.setup>,
  target: HTMLElement,
) {
  for (
    let attempt = 0;
    attempt < 40 && document.activeElement !== target;
    attempt += 1
  ) {
    await user.tab();
  }
}

function focusedElement(): HTMLElement | null {
  return document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
}

beforeEach(() => {
  vi.mocked(createApiClient).mockReset();
});

describe("BatteriesView (UI_CONTRACTS.md - Batteries)", () => {
  it("renders one card per named unit with charge, power, availability, temperatures, cell spread, data age and warnings", async () => {
    const client = healthyClient(HEALTHY_SNAPSHOT, HEALTHY_EVENTS);
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    const rhs = await screen.findByRole("group", { name: "RHS" });
    const lhs = await screen.findByRole("group", { name: "LHS" });

    // charge level and data age
    expect(mid).toHaveTextContent(/62\s*%/);
    expect(mid).toHaveTextContent(/2\s*(s|seconds)\s*old/);
    // current direction and power come from measured_watts: the sign is
    // carried by the direction word, never rendered as a raw negative number.
    expect(mid).toHaveTextContent(/discharging/i);
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(mid).not.toHaveTextContent(/-2,?400/);
    // availability in plain language
    expect(rhs).toHaveTextContent(/standby|ready|available/i);
    expect(lhs).toHaveTextContent(/observe/i);
    // temperature range
    expect(mid).toHaveTextContent(/24\.1/);
    expect(mid).toHaveTextContent(/27\.8/);
    // cell spread
    expect(mid).toHaveTextContent(/42\s*mV/);
    // warnings labelled in words, never colour alone
    expect(mid).toHaveTextContent(/warning/i);
    // live events are followed from the snapshot cursor
    expect(client.openEvents).toHaveBeenCalledWith(4100);
  });

  it("binds every card to its unit id, never to list position", async () => {
    const firstView = renderView(
      healthyClient(snapshotWithOrder(["LHS", "MID", "RHS"]), HEALTHY_EVENTS),
    );
    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(screen.getByRole("group", { name: "RHS" })).toHaveTextContent(/78\s*%/);
    firstView.unmount();

    const reordered = snapshotWithOrder(["RHS", "LHS", "MID"]);
    renderView(healthyClient(reordered, fleetEvents(reordered)));
    const midAgain = await screen.findByRole("group", { name: "MID" });
    expect(midAgain).toHaveTextContent(/62\s*%/);
    expect(midAgain).toHaveTextContent(/2,?400\s*W/);
    const rhsAgain = screen.getByRole("group", { name: "RHS" });
    expect(rhsAgain).toHaveTextContent(/78\s*%/);
    expect(rhsAgain).not.toHaveTextContent(/44\s*%/);
  });

  it("shows the power actually delivered, never the request, when the safety system clamps the action", async () => {
    // requested 2,400 W, authorized 1,000 W, measured -950 W: a limited
    // action must never be presented as the full request or as what flows.
    const clamped: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((u) =>
        u.unit_id === "MID"
          ? {
              ...u,
              requested_power: { direction: "discharge", watts: 2400 },
              authorized_power: { direction: "discharge", watts: 1000 },
              measured_watts: -950,
            }
          : u,
      ),
    };
    renderView(healthyClient(clamped, fleetEvents(clamped)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/discharging/i);
    expect(mid).toHaveTextContent(/950\s*W/);
    expect(mid).not.toHaveTextContent(/2,?400/);
    expect(mid).not.toHaveTextContent(/-950/);
  });

  it("shows a loading skeleton and no unit cards until the snapshot lands", async () => {
    const client = makeClient();
    let resolveSnapshot!: (value: WireSnapshot) => void;
    client.getSnapshot.mockReturnValue(
      new Promise<WireSnapshot>((resolve) => {
        resolveSnapshot = resolve;
      }),
    );
    client.openEvents.mockReturnValue(openStream([]));
    renderView(client);

    expect(screen.getByRole("status", { name: /loading/i })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "MID" })).not.toBeInTheDocument();

    resolveSnapshot(HEALTHY_SNAPSHOT);
    expect(
      await screen.findByRole("group", { name: "MID" }),
    ).toBeInTheDocument();
  });

  it("explains what will appear when no batteries are connected", async () => {
    const empty: WireSnapshot = {
      site_id: "home-1",
      snapshot_sequence: 42,
      captured_at: CAPTURED_AT,
      units: [],
    };
    renderView(
      healthyClient(empty, [
        {
          type: "snapshot",
          sequence: 42,
          data: empty,
        },
      ]),
    );

    expect(await screen.findByText(/no batteries/i)).toBeInTheDocument();
    expect(screen.getByText(/appear here/i)).toBeInTheDocument();
    expect(screen.getByText(/connect/i)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "MID" })).not.toBeInTheDocument();
  });

  it("surfaces the snapshot error envelope verbatim and recovers on retry", async () => {
    const user = userEvent.setup();
    const failure: ErrorEnvelope = {
      code: "SNAPSHOT_UNAVAILABLE",
      message: "The battery snapshot could not be loaded right now.",
      details: null,
      request_id: "req-8f21",
    };
    const client = makeClient();
    client.getSnapshot
      .mockRejectedValueOnce(new ApiClientError({ ...failure, status: 503 }))
      .mockResolvedValue(HEALTHY_SNAPSHOT);
    client.openEvents.mockReturnValue(openStream(HEALTHY_EVENTS));
    renderView(client);

    expect(await screen.findByText("SNAPSHOT_UNAVAILABLE")).toBeInTheDocument();
    expect(
      screen.getByText("The battery snapshot could not be loaded right now."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "MID" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(
      await screen.findByRole("group", { name: "MID" }),
    ).toBeInTheDocument();
  });

  it("shows the age of stale data next to the value instead of hiding it", async () => {
    const stale: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((u) =>
        u.unit_id === "MID"
          ? { ...u, telemetry_age_s: 95, quality: "stale" }
          : u,
      ),
    };
    renderView(healthyClient(stale, fleetEvents(stale)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/62\s*%/);
    expect(mid).toHaveTextContent(/95\s*(s|seconds)\s*old/);
  });

  it("names missing telemetry and its age without inventing values", async () => {
    const partial: WireSnapshot = {
      site_id: "home-1",
      snapshot_sequence: 4200,
      captured_at: CAPTURED_AT,
      units: [
        unit({
          unit_id: "MID",
          lifecycle: "active",
          telemetry_age_s: 2,
          requested_power: { direction: "discharge", watts: 2400 },
          authorized_power: { direction: "discharge", watts: 2400 },
          measured_watts: -2400,
        }),
        unit({
          unit_id: "LHS",
          lifecycle: "disconnected",
          telemetry_age_s: 620,
          quality: "missing",
          authorized_power: null,
          measured_watts: null,
        }),
        unit({
          unit_id: "RHS",
          lifecycle: "disarmed",
          telemetry_age_s: 5,
          quality: "suspect",
        }),
      ],
    };
    const events: StreamEvent[] = [
      {
        type: "snapshot",
        sequence: 4200,
        data: partial,
      },
      observation(4201, "MID"),
      observation(4202, "RHS", {
        soc_percent: 78,
        temperature_min_c: 23.0,
        temperature_max_c: 25.5,
        cells: null,
        cell_data_age_s: 402,
      }),
    ];
    renderView(healthyClient(partial, events));

    // a unit with no telemetry at all ties "no data" to each labelled field:
    // no percentage, no watt figure, and no zero-filled stand-in measurement
    // anywhere in those rows (the age next to the gap stays allowed).
    const lhs = await screen.findByRole("group", { name: "LHS" });
    for (const label of [/charge/i, /temperature/i, /cell/i]) {
      const row = tightestText(lhs, label, /no data/i);
      expect(row).not.toMatch(/%|mV|\d+\s*W/);
      expect(row).not.toMatch(/\b0\b/);
    }
    expect(lhs).toHaveTextContent(/620\s*(s|seconds)\s*old/);
    expect(lhs).not.toHaveTextContent(/%/);
    // measured_watts is null: no fabricated power figure on the card at all
    expect(lhs).not.toHaveTextContent(/\d+\s*W/);

    // a unit with partial telemetry names only what is missing, with the
    // cell block's own age
    const rhs = screen.getByRole("group", { name: "RHS" });
    const rhsCellRow = tightestText(rhs, /cell/i, /no data/i);
    expect(rhsCellRow).not.toMatch(/mV|\d+\s*W/);
    expect(rhs).toHaveTextContent(/402\s*(s|seconds)\s*old/);
    expect(rhs).not.toHaveTextContent(/mV/);

    // the healthy unit still shows real measurements
    const mid = screen.getByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/62\s*%/);
    expect(mid).toHaveTextContent(/42\s*mV/);
  });

  it("keeps the last snapshot visible with a disconnected notice when the event stream drops", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    // every attempt delivers the snapshot frame plus the LHS and MID
    // observations (through sequence 4102) and then drops, so the notice
    // persists and MID's charge value honestly comes from its observation.
    client.openEvents.mockImplementation(() =>
      openStream(HEALTHY_EVENTS.slice(0, 3), "fail"),
    );
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/62\s*%/);

    expect(await screen.findByText(/disconnected/i)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "MID" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "RHS" })).toBeInTheDocument();

    // the socket retries automatically, carrying the last delivered sequence
    // (4102, the MID observation) as the cursor — not the snapshot's 4100
    await waitFor(() =>
      expect(client.openEvents.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
    expect(client.openEvents).toHaveBeenLastCalledWith(4102);
  });

  it("opens a unit and reads the detail tabs using only the keyboard", async () => {
    const user = userEvent.setup();
    renderView(healthyClient(HEALTHY_SNAPSHOT, HEALTHY_EVENTS));

    const midButton = await screen.findByRole("button", { name: "MID" });
    await tabUntilFocused(user, midButton);
    expect(midButton).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("heading", { name: /MID/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Summary" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Cells" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Events" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Details" })).toBeInTheDocument();
    // only the active tab's panel is exposed
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);

    // Summary: condition, power, limits, communications
    const summary = screen.getByRole("tabpanel");
    expect(summary).toHaveTextContent(/condition/i);
    expect(summary).toHaveTextContent(/power/i);
    expect(summary).toHaveTextContent(/limit/i);
    expect(summary).toHaveTextContent(/communication/i);

    const summaryTab = screen.getByRole("tab", { name: "Summary" });
    await tabUntilFocused(user, summaryTab);
    expect(summaryTab).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    const cellsTab = screen.getByRole("tab", { name: "Cells" });
    expect(cellsTab).toHaveFocus();
    await user.keyboard("{Enter}");

    const cells = await screen.findByRole("tabpanel");
    // min/max/spread readable as text, never colour alone
    expect(cells).toHaveTextContent(/3\.31/);
    expect(cells).toHaveTextContent(/3\.35/);
    expect(cells).toHaveTextContent(/42\s*mV/);
    expect(cells).toHaveTextContent(/24\.1/);
    expect(cells).toHaveTextContent(/27\.8/);
    // data completeness: the cell block's own age is part of the tab
    expect(cells).toHaveTextContent(/complet|data age/i);
    expect(cells).toHaveTextContent(/\b6\s*(s|sec)/i);

    // Details is reachable by keyboard and carries the data-quality map
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{ArrowRight}");
    const detailsTab = screen.getByRole("tab", { name: "Details" });
    expect(detailsTab).toHaveFocus();
    await user.keyboard("{Enter}");
    const details = await screen.findByRole("tabpanel");
    expect(details).toHaveTextContent(/quality/i);
    expect(details).toHaveTextContent(/data/i);
  });

  it("shows unit events in plain language and reveals the raw code on demand", async () => {
    const user = userEvent.setup();
    const audit: AuditPage = {
      events: [
        {
          type: "warning",
          sequence: 4080,
          occurred_at: "2026-08-22T09:58:00Z",
          unit_id: "MID",
          code: "TEMP_OUT_OF_RANGE",
          payload: null,
        },
        {
          type: "fault",
          sequence: 4021,
          occurred_at: "2026-08-22T09:31:00Z",
          unit_id: "MID",
          code: "BMS_COMM_LOSS",
          payload: null,
        },
      ],
      next_cursor: null,
    };
    const client = healthyClient(HEALTHY_SNAPSHOT, HEALTHY_EVENTS);
    client.getAudit.mockResolvedValue(audit);
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Events" }));

    const panel = await screen.findByRole("tabpanel");
    expect(within(panel).getByText(/temperature/i)).toBeInTheDocument();
    expect(
      within(panel).getByText(/communications|connection/i),
    ).toBeInTheDocument();
    // raw codes are not shown until asked for
    expect(screen.queryByText("TEMP_OUT_OF_RANGE")).not.toBeInTheDocument();
    expect(screen.queryByText("BMS_COMM_LOSS")).not.toBeInTheDocument();

    const items = within(panel).getAllByRole("listitem");
    expect(items.length).toBeGreaterThanOrEqual(2);
    const warningItem = items.find((item) =>
      /temperature/i.test(item.textContent ?? ""),
    );
    expect(warningItem).toBeDefined();
    const warning = warningItem as HTMLElement;
    await user.click(within(warning).getByRole("button", { name: /technical/i }));
    expect(within(warning).getByText("TEMP_OUT_OF_RANGE")).toBeInTheDocument();
  });

  it("shows latch state, announces it assertively, and offers acknowledge only for latched units", async () => {
    const latched = latchedSnapshot();
    renderView(healthyClient(latched, latchedEvents(latched)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(within(mid).getByText(/latched/i)).toBeInTheDocument();
    expect(
      within(mid).getByRole("button", { name: /acknowledge/i }),
    ).toBeInTheDocument();
    // a latched inhibit is an emergency event: it announces assertively
    // (role="alert" is an assertive live region)
    const alerts = screen.getAllByRole("alert");
    expect(
      alerts.some((alert) => /inhibit|latch/i.test(alert.textContent ?? "")),
    ).toBe(true);

    const rhs = screen.getByRole("group", { name: "RHS" });
    expect(
      within(rhs).queryByRole("button", { name: /acknowledge/i }),
    ).not.toBeInTheDocument();
  });

  it("confirms acknowledgement, explains re-arming is separate, and clears the latch from a refetch", async () => {
    const user = userEvent.setup();
    const latched = latchedSnapshot();
    const cleared: WireSnapshot = {
      ...latched,
      units: latched.units.map((u): WireUnit =>
        u.unit_id === "MID"
          ? { ...u, lifecycle: "disarmed", inhibit: null }
          : u,
      ),
    };
    const client = makeClient();
    client.getSnapshot.mockResolvedValueOnce(latched).mockResolvedValue(cleared);
    client.openEvents.mockReturnValue(openStream(latchedEvents(latched)));
    client.postInhibitAcknowledgement.mockResolvedValue({ acknowledged: true });
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    await user.click(within(mid).getByRole("button", { name: /acknowledge/i }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/acknowledg/i);
    expect(dialog).toHaveTextContent(/separate/i);
    expect(dialog).toHaveTextContent(/arm/);

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));
    expect(client.postInhibitAcknowledgement).toHaveBeenCalledWith("MID");
    // post-acknowledge state is server authority (API_CONTRACTS.md, "Inhibit
    // acknowledgement"): the latch clears only through a snapshot refetch,
    // never through optimistic local state
    await waitFor(() => expect(client.getSnapshot).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() => {
      const clearedCard = screen.getByRole("group", { name: "MID" });
      expect(
        within(clearedCard).queryByRole("button", { name: /acknowledge/i }),
      ).not.toBeInTheDocument();
    });
  });

  it("surfaces the acknowledgement refusal envelope verbatim", async () => {
    const user = userEvent.setup();
    const refusal: ErrorEnvelope = {
      code: "PERMISSION_DENIED",
      message: "This account is not allowed to acknowledge inhibits.",
      details: null,
      request_id: "req-311",
    };
    const latched = latchedSnapshot();
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(latched);
    client.openEvents.mockReturnValue(openStream(latchedEvents(latched)));
    client.postInhibitAcknowledgement.mockRejectedValue(
      new ApiClientError({ ...refusal, status: 403 }),
    );
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    await user.click(within(mid).getByRole("button", { name: /acknowledge/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(client.postInhibitAcknowledgement).toHaveBeenCalledWith("MID");
    expect(
      await within(dialog).findByText("PERMISSION_DENIED"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "This account is not allowed to acknowledge inhibits.",
      ),
    ).toBeInTheDocument();
  });

  it("traps focus in the acknowledgement dialog and restores it on cancel", async () => {
    const user = userEvent.setup();
    const latched = latchedSnapshot();
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(latched);
    client.openEvents.mockReturnValue(openStream(latchedEvents(latched)));
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    const acknowledge = within(mid).getByRole("button", {
      name: /acknowledge/i,
    });
    await tabUntilFocused(user, acknowledge);
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toContainElement(focusedElement());
    // Shift+Tab from the first focusable must wrap to the dialog's last
    // focusable instead of escaping backwards out of the dialog
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(dialog).toContainElement(focusedElement());
    // Tabbing through every focusable wraps inside the dialog: focus never
    // leaves it and the first focusable is reached again
    const visited = new Set<HTMLElement>();
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await user.keyboard("{Tab}");
      const current = focusedElement();
      expect(dialog).toContainElement(current);
      if (current === null) {
        continue;
      }
      if (visited.has(current)) {
        break;
      }
      visited.add(current);
    }
    expect(visited.size).toBeGreaterThanOrEqual(2);

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(client.postInhibitAcknowledgement).not.toHaveBeenCalled();
    const acknowledgeAgain = within(
      screen.getByRole("group", { name: "MID" }),
    ).getByRole("button", { name: /acknowledge/i });
    expect(acknowledgeAgain).toHaveFocus();
  });
});
