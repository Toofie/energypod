/**
 * Behavior contract for the "Batteries" view (docs/UI_CONTRACTS.md,
 * "Batteries"): fleet cards per named unit, card fields, latched-inhibit
 * acknowledgement, unit detail tabs, missing telemetry honesty, and keyboard
 * operation.
 *
 * WIRE TRUTH (web/src/test/wire.ts — derived from src/energypod, never
 * invented here):
 *
 * - The API client module is mocked at its exact surface with
 *   `createApiClient` replaced and every other canonical export preserved;
 *   rejections are real `ApiClientError` values carrying the envelope
 *   verbatim plus a status.
 * - The snapshot unit carries `unit_id, lifecycle, telemetry_age_s, quality,
 *   requested_power, authorized_power, measured_watts` plus the amended
 *   nullable `telemetry` summary block (API_CONTRACTS.md "Application service
 *   facade"; wire.ts `telemetrySummary`): the card's charge, pack voltage,
 *   power, cell count+spread, temperature range, and warnings render from it,
 *   and a null block or null field is pinned as named-missing ("No data",
 *   with age) — never an invented value.
 * - `GET /api/v1/units/{unit_id}` (wire.ts `unitDetail`) is the full
 *   latest-observation projection; the Cells tab's distribution, the
 *   temperatures, data completeness, and the Summary tab's identity render
 *   from that on-demand read, with its own loading / error / disconnected
 *   states. `emptyUnitDetail` is the honest every-datum-absent projection.
 * - `quality` is the facade projection vocabulary good/degraded/bad/missing;
 *   "degraded" already means past the service's 30 s good bound.
 * - The only observation event is `observation.published` with the minimal
 *   payload `{unit_id, connection_epoch, sequence}`; the cards' per-unit
 *   observation state is pinned from exactly that frame.
 * - Per-unit event history comes from the audit read model (`event_type`,
 *   `result`, `reason_codes`, integer `sequence`); raw codes are the service's
 *   own reason codes.
 * - The latch fixture widens the snapshot unit with the API_CONTRACTS
 *   "Inhibit acknowledgement" facade exposure (`inhibit`), which the view
 *   reads defensively: a separate test pins the behaviour when the snapshot
 *   does not expose it at all.
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
import {
  auditEvent,
  auditPage,
  emptyUnitDetail,
  observationPublished,
  snapshot,
  snapshotFrame,
  telemetrySummary,
  unitDetail,
  unitSnapshot,
  withInhibit,
  type WireSnapshot,
  type WireUnitDetail,
  type WireUnitSnapshot,
} from "../../test/wire";
import { BatteriesView } from "./BatteriesView";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: vi.fn(),
}));

const CAPTURED_AT = "2026-08-22T10:00:00Z";

interface MockedClient {
  getSnapshot: Mock<() => Promise<WireSnapshot>>;
  getUnitDetail: Mock<(unitId: string) => Promise<WireUnitDetail>>;
  getAudit: Mock<(limit: number, afterSequence?: number) => Promise<AuditPage>>;
  postInhibitAcknowledgement: Mock<(unitId: string) => Promise<Record<string, unknown>>>;
  openEvents: Mock<(afterSequence?: number) => AsyncIterable<StreamEvent>>;
}

function makeClient(): MockedClient {
  const client: MockedClient = {
    getSnapshot: vi.fn<() => Promise<WireSnapshot>>(),
    getUnitDetail: vi.fn<(unitId: string) => Promise<WireUnitDetail>>(),
    getAudit: vi.fn<(limit: number, afterSequence?: number) => Promise<AuditPage>>(),
    postInhibitAcknowledgement: vi.fn<
      (unitId: string) => Promise<Record<string, unknown>>
    >(),
    openEvents: vi.fn<(afterSequence?: number) => AsyncIterable<StreamEvent>>(),
  };
  client.getAudit.mockResolvedValue(auditPage([]));
  // A unit with no observation answers with the honest empty projection.
  client.getUnitDetail.mockImplementation((unitId: string) =>
    Promise.resolve(emptyUnitDetail(unitId)),
  );
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

const HEALTHY_SNAPSHOT: WireSnapshot = snapshot(
  [
    unitSnapshot({ unit_id: "LHS", lifecycle: "observe_only", telemetry_age_s: 4 }),
    unitSnapshot({
      unit_id: "MID",
      lifecycle: "active",
      telemetry_age_s: 2,
      requested_power: { direction: "discharge", watts: 2400 },
      authorized_power: { direction: "discharge", watts: 2400 },
      measured_watts: 2400,
    }),
    unitSnapshot({ unit_id: "RHS", lifecycle: "disarmed", telemetry_age_s: 5 }),
  ],
  { snapshot_sequence: 4100, captured_at: CAPTURED_AT },
);

/**
 * The opening burst the service actually sends: the authoritative snapshot
 * frame, then one `observation.published` event per unit in snapshot order
 * (the composition publishes one event per appended observation).
 */
function fleetEvents(state: WireSnapshot): StreamEvent[] {
  const events: StreamEvent[] = [snapshotFrame(state)];
  let sequence = state.snapshot_sequence;
  for (const unit of state.units) {
    sequence += 1;
    events.push(observationPublished(sequence, unit.unit_id, { occurredAt: CAPTURED_AT }));
  }
  return events;
}

const HEALTHY_EVENTS: StreamEvent[] = fleetEvents(HEALTHY_SNAPSHOT);

function healthyClient(state: WireSnapshot, events: readonly StreamEvent[]): MockedClient {
  const client = makeClient();
  client.getSnapshot.mockResolvedValue(state);
  client.openEvents.mockReturnValue(openStream(events));
  return client;
}

function snapshotWithOrder(order: string[]): WireSnapshot {
  const byId = new Map(HEALTHY_SNAPSHOT.units.map((u) => [u.unit_id, u]));
  return {
    ...HEALTHY_SNAPSHOT,
    units: order.map((id) => byId.get(id)).filter((u): u is WireUnitSnapshot => Boolean(u)),
  };
}

/** MID inhibited with the documented latch exposure attached. */
function latchedSnapshot(): WireSnapshot {
  return {
    ...HEALTHY_SNAPSHOT,
    units: [
      ...HEALTHY_SNAPSHOT.units.filter((u) => u.unit_id !== "MID"),
      withInhibit(
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "inhibited",
          telemetry_age_s: 2,
          measured_watts: 0,
        }),
        { cause_class: "latched", latched: true, reason_code: "identity_mismatch" },
      ),
    ],
  };
}

function latchedEvents(latched: WireSnapshot): StreamEvent[] {
  return fleetEvents(latched);
}

function renderView(client: MockedClient, connection?: "connected" | "disconnected") {
  vi.mocked(createApiClient).mockReturnValue(client as unknown as ApiClient);
  const api = createApiClient("operator-token");
  return connection === undefined ? (
    render(<BatteriesView client={api} />)
  ) : (
    render(<BatteriesView client={api} connection={connection} />)
  );
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
  return document.activeElement instanceof HTMLElement ? document.activeElement : null;
}

beforeEach(() => {
  vi.mocked(createApiClient).mockReset();
});

describe("BatteriesView (UI_CONTRACTS.md - Batteries)", () => {
  it("renders one card per named unit with power, availability, data age, per-unit observation state, and the wire's missing fields named as missing", async () => {
    const client = healthyClient(HEALTHY_SNAPSHOT, HEALTHY_EVENTS);
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    const rhs = await screen.findByRole("group", { name: "RHS" });
    const lhs = await screen.findByRole("group", { name: "LHS" });

    // data age from the snapshot's own telemetry_age_s
    expect(mid).toHaveTextContent(/2\s*(s|seconds)\s*old/);
    // current direction and power come from measured_watts: the sign is
    // carried by the direction word, never rendered as a raw negative number.
    expect(mid).toHaveTextContent(/discharging/i);
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(mid).not.toHaveTextContent(/-2,?400/);
    // availability in plain language
    expect(rhs).toHaveTextContent(/standby|ready|available/i);
    expect(lhs).toHaveTextContent(/observe/i);
    // charge, temperature, and cell detail are on no wire the service sends:
    // each field is named with "no data" and its age, never an invented value
    expect(tightestText(mid, /charge/i, /no data/i)).not.toMatch(/%|\d+\s*W/);
    expect(tightestText(mid, /temperature/i, /no data/i)).not.toMatch(/°|\d+\s*C/i);
    expect(tightestText(mid, /cell/i, /no data/i)).not.toMatch(/mV|\d+\s*V/i);
    expect(mid).not.toHaveTextContent(/%/);
    expect(mid).not.toHaveTextContent(/mV/);
    expect(mid).not.toHaveTextContent(/°/);
    // warnings labelled in words, never colour alone
    expect(mid).toHaveTextContent(/warning/i);
    // the observation event the service actually publishes drives per-unit
    // telemetry state: MID's own observation, at MID's own telemetry sequence
    expect(mid).toHaveTextContent(/last observation:\s*10:00 UTC/i);
    expect(mid).toHaveTextContent(/telemetry sequence 41020/);
    expect(lhs).toHaveTextContent(/telemetry sequence 41010/);
    // live events are followed from the snapshot cursor
    expect(client.openEvents).toHaveBeenCalledWith(4100);
  });

  it("binds every card to its unit id, never to list position", async () => {
    const firstView = renderView(
      healthyClient(snapshotWithOrder(["LHS", "MID", "RHS"]), HEALTHY_EVENTS),
    );
    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(mid).toHaveTextContent(/2\s*(s|seconds)\s*old/);
    expect(screen.getByRole("group", { name: "RHS" })).toHaveTextContent(
      /5\s*(s|seconds)\s*old/,
    );
    firstView.unmount();

    const reordered = snapshotWithOrder(["RHS", "LHS", "MID"]);
    renderView(healthyClient(reordered, fleetEvents(reordered)));
    const midAgain = await screen.findByRole("group", { name: "MID" });
    expect(midAgain).toHaveTextContent(/2,?400\s*W/);
    expect(midAgain).toHaveTextContent(/2\s*(s|seconds)\s*old/);
    const lhsAgain = screen.getByRole("group", { name: "LHS" });
    expect(lhsAgain).toHaveTextContent(/4\s*(s|seconds)\s*old/);
    // RHS never takes LHS's facts with it
    expect(screen.getByRole("group", { name: "RHS" })).not.toHaveTextContent(
      /4\s*(s|seconds)\s*old/,
    );
  });

  it("shows the power actually delivered, never the request, when the safety system clamps the action", async () => {
    // requested 2,400 W, authorized 1,000 W, measured 950 W: a limited
    // action must never be presented as the full request or as what flows.
    const clamped: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((u) =>
        u.unit_id === "MID"
          ? {
              ...u,
              requested_power: { direction: "discharge", watts: 2400 },
              authorized_power: { direction: "discharge", watts: 1000 },
              measured_watts: 950,
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

  // ---------------------------------------------------------------------------
  // Telemetry rendering (API_CONTRACTS.md "Application service facade")
  // ---------------------------------------------------------------------------

  /** The captured fleet, each unit carrying its real decoded telemetry. */
  const TELEMETRY_SNAPSHOT: WireSnapshot = snapshot(
    [
      // MID: the mandate's values (SOC 10 %, 192.4 V, 60 cells 3.205-3.209 V,
      // 23-28 °C) with the evidence-anchored SOH and dynamic limits.
      unitSnapshot({
        unit_id: "MID",
        lifecycle: "disarmed",
        telemetry_age_s: 2,
        measured_watts: 0,
        telemetry: telemetrySummary({
          soh_pct: 100,
          battery_watts: 0,
          pack_current_a: 0,
          dynamic_charge_limit_w: 7692,
          dynamic_discharge_limit_w: 0,
        }),
      }),
      // RHS: discharging per its telemetry block, with measured_watts null —
      // the card's power figure can only come from the telemetry block.
      unitSnapshot({
        unit_id: "RHS",
        lifecycle: "disarmed",
        telemetry_age_s: 3,
        measured_watts: null,
        telemetry: telemetrySummary({
          soc_pct: 68,
          pack_voltage_v: 164.5,
          pack_current_a: -6.9,
          battery_watts: 1132,
          soh_pct: 100,
          dynamic_charge_limit_w: 6532,
          dynamic_discharge_limit_w: 6532,
          cell_count: 50,
          cell_min_v: 3.289,
          cell_max_v: 3.292,
          cell_spread_mv: 3,
          temperature_min_c: 23,
          temperature_max_c: 27,
        }),
      }),
      // LHS: no observation at all — the honest nullable block.
      unitSnapshot({
        unit_id: "LHS",
        lifecycle: "observe_only",
        telemetry_age_s: 4,
        measured_watts: null,
        telemetry: null,
      }),
    ],
    { snapshot_sequence: 4400, captured_at: CAPTURED_AT },
  );

  it("renders card charge, pack voltage, power, cell count+spread, temperature range, and warnings from the telemetry block", async () => {
    renderView(healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/charge level:\s*10%/i);
    expect(mid).toHaveTextContent(/pack voltage:\s*192\.4\s*V/i);
    expect(mid).toHaveTextContent(/cell spread:\s*4\s*mV across 60 cells/i);
    expect(mid).toHaveTextContent(/temperature:\s*23\s*°C to 28\s*°C/i);
    // warnings are the fleet-wide calibration warnings, in words
    expect(mid).toHaveTextContent(/warnings:\s*PCS Warning0 1, DCDC Warning0 1/i);

    const rhs = screen.getByRole("group", { name: "RHS" });
    expect(rhs).toHaveTextContent(/charge level:\s*68%/i);
    expect(rhs).toHaveTextContent(/pack voltage:\s*164\.5\s*V/i);
    expect(rhs).toHaveTextContent(/cell spread:\s*3\s*mV across 50 cells/i);
    expect(rhs).toHaveTextContent(/temperature:\s*23\s*°C to 27\s*°C/i);
    // measured_watts is null on the wire: this figure can only be the
    // telemetry block's own signed battery watts, sign carried by the word.
    expect(rhs).toHaveTextContent(/power:\s*discharging 1,132 W/i);
    expect(rhs).not.toHaveTextContent(/-1,?132/);

    // A unit without an observation keeps every one of those fields honest.
    const lhs = screen.getByRole("group", { name: "LHS" });
    for (const label of [/charge level/i, /pack voltage/i, /temperature/i, /cell spread/i]) {
      expect(tightestText(lhs, label, /no data/i)).not.toMatch(/%|V\b|mV|°/);
    }
    expect(lhs).toHaveTextContent(/warnings:\s*no data/i);
    expect(lhs).not.toHaveTextContent(/%/);
    expect(lhs).not.toHaveTextContent(/°/);
    expect(lhs).not.toHaveTextContent(/\d+\s*W/);
  });

  it("renders an explicit empty warning list as none, distinct from absent warnings", async () => {
    const quiet: WireSnapshot = {
      ...TELEMETRY_SNAPSHOT,
      units: TELEMETRY_SNAPSHOT.units.map((u) =>
        u.unit_id === "MID"
          ? { ...u, telemetry: telemetrySummary({ active_warnings: [] }) }
          : u,
      ),
    };
    renderView(healthyClient(quiet, fleetEvents(quiet)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/warnings:\s*none/i);
    expect(mid).not.toHaveTextContent(/PCS Warning/i);
  });

  it("fetches the unit detail on open and renders the full Cells distribution, temperatures, and completeness", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    client.getUnitDetail.mockResolvedValue(unitDetail("MID"));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    // The on-demand read went to exactly the opened unit's endpoint path.
    await waitFor(() => expect(client.getUnitDetail).toHaveBeenCalledWith("MID"));
    expect(client.getUnitDetail).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("tab", { name: "Cells" }));
    const cells = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.205 V/i);
      expect(cells).toHaveTextContent(/maximum cell voltage:\s*3\.209 V/i);
      expect(cells).toHaveTextContent(/voltage spread:\s*4 mV/i);
      expect(cells).toHaveTextContent(/temperature range:\s*23 °C to 28 °C/i);
    });

    // The complete per-cell distribution is text-readable: every one of the
    // 60 captured cells, first to last.
    const grid = within(cells).getByRole("list", { name: /cell voltages/i });
    const cellItems = within(grid).getAllByRole("listitem");
    expect(cellItems).toHaveLength(60);
    expect(cellItems[0]!.textContent).toBe("Cell 1: 3.205 V");
    expect(cellItems[59]!.textContent).toBe("Cell 60: 3.209 V");

    // The temperature sensors render as values too (BIC x 3 = 18 sensors).
    const sensors = within(cells).getByRole("list", { name: /temperature sensors/i });
    const sensorItems = within(sensors).getAllByRole("listitem");
    expect(sensorItems).toHaveLength(18);
    expect(sensorItems[0]!.textContent).toBe("Sensor 1: 23 °C");
    expect(sensorItems[17]!.textContent).toBe("Sensor 18: 28 °C");

    // Data completeness states the population and the quality map's verdict.
    expect(cells).toHaveTextContent(/60 of 60 cells reporting/i);
    expect(cells).toHaveTextContent(/18 temperature readings/i);
    expect(cells).toHaveTextContent(/all telemetry fields good quality/i);
  });

  it("shows the Cells tab's own loading state until the detail read lands", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    let resolveDetail!: (value: WireUnitDetail) => void;
    client.getUnitDetail.mockReturnValueOnce(
      new Promise<WireUnitDetail>((resolve) => {
        resolveDetail = resolve;
      }),
    );
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Cells" }));
    expect(await screen.findByRole("status", { name: /loading cell detail/i })).toBeInTheDocument();
    // No distribution can render while the read is pending — never a guess.
    const pending = screen.getByRole("tabpanel");
    expect(pending).not.toHaveTextContent(/3\.205/);

    resolveDetail(unitDetail("MID"));
    await waitFor(() => {
      expect(screen.getByRole("tabpanel")).toHaveTextContent(/minimum cell voltage:\s*3\.205 V/i);
    });
  });

  it("surfaces a refused unit-detail read verbatim and recovers on its own retry", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    client.getUnitDetail
      .mockRejectedValueOnce(
        new ApiClientError({
          status: 503,
          code: "unit_detail_unavailable",
          message: "The unit detail could not be read right now.",
          details: null,
          request_id: "req-ud-1",
        }),
      )
      .mockResolvedValueOnce(unitDetail("MID"));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Cells" }));

    // The refusal envelope renders verbatim, with the retry owned by the tab.
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("unit_detail_unavailable")).toBeInTheDocument();
    expect(
      within(alert).getByText("The unit detail could not be read right now."),
    ).toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: /try again/i }));
    const cells = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.205 V/i);
    });
    expect(client.getUnitDetail).toHaveBeenCalledTimes(2);
  });

  it("shows state of charge, state of health, dynamic limits, and identity on the Summary tab", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    client.getUnitDetail.mockResolvedValue(unitDetail("MID"));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    const summary = await screen.findByRole("tabpanel");
    // SOC and SOH come from the snapshot's telemetry block.
    await waitFor(() => {
      expect(summary).toHaveTextContent(/charge level:\s*10%; state of health 100%/i);
      // The device's own dynamic limits, including the SOC-10 % discharge inhibit.
      expect(summary).toHaveTextContent(
        /device limits \(dynamic\): charge up to 7,692 W, discharge up to 0 W/i,
      );
      // Identity comes from the on-demand unit detail: serial, RTU id, profile.
      expect(summary).toHaveTextContent(/identity:\s*BEP0005KXX11B10500151 \(RTU 0x2C225097\), profile iot/i);
    });
  });

  it("names the identity gap while the detail read has not landed", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    client.getUnitDetail.mockReturnValue(new Promise<WireUnitDetail>(() => {}));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    const summary = await screen.findByRole("tabpanel");
    expect(summary).toHaveTextContent(/identity:\s*loading the unit's identity…/i);
    expect(summary).not.toHaveTextContent(/BEP0005/i);
  });

  it("keeps the last cell detail visible with a disconnected notice when the live connection is lost", async () => {
    const user = userEvent.setup();
    const client = healthyClient(TELEMETRY_SNAPSHOT, fleetEvents(TELEMETRY_SNAPSHOT));
    client.getUnitDetail.mockResolvedValue(unitDetail("MID"));
    renderView(client, "disconnected");

    await user.click(await screen.findByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Cells" }));
    const cells = await screen.findByRole("tabpanel");
    await waitFor(() => {
      // the disconnected notice names the state...
      expect(cells).toHaveTextContent(/disconnected from live updates/i);
      expect(cells).toHaveTextContent(/showing the last known cell readings/i);
      // ...while the values stay on screen, dimmed-not-hidden
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.205 V/i);
    });
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
    const empty = snapshot([], { snapshot_sequence: 42, captured_at: CAPTURED_AT });
    renderView(healthyClient(empty, [snapshotFrame(empty)]));

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

  it("marks data past the service's freshness bound stale, with the age next to the value instead of hiding it", async () => {
    // The facade's own projection: telemetry past its 30 s good bound is
    // "degraded", never "good" — the fixture uses the wire vocabulary.
    const stale: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((u) =>
        u.unit_id === "MID" ? { ...u, telemetry_age_s: 95, quality: "degraded" } : u,
      ),
    };
    renderView(healthyClient(stale, fleetEvents(stale)));

    const mid = await screen.findByRole("group", { name: "MID" });
    // the value is kept and carries its age plus the stale mark
    expect(mid).toHaveTextContent(/discharging/i);
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(mid).toHaveTextContent(/95\s*(s|seconds)\s*old/);
    expect(mid).toHaveTextContent(/stale/i);
    expect(mid).toHaveTextContent(/charge level:\s*no data/i);
    expect(mid).not.toHaveTextContent(/%/);
  });

  it("names missing telemetry and its age without inventing values", async () => {
    const partial = snapshot(
      [
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "active",
          telemetry_age_s: 2,
          requested_power: { direction: "discharge", watts: 2400 },
          authorized_power: { direction: "discharge", watts: 2400 },
          measured_watts: 2400,
        }),
        unitSnapshot({
          unit_id: "LHS",
          lifecycle: "disconnected",
          telemetry_age_s: 620,
          quality: "missing",
          authorized_power: null,
          measured_watts: null,
        }),
        unitSnapshot({
          unit_id: "RHS",
          lifecycle: "disarmed",
          telemetry_age_s: 5,
          quality: "bad",
        }),
      ],
      { snapshot_sequence: 4200, captured_at: CAPTURED_AT },
    );
    const events: StreamEvent[] = [
      snapshotFrame(partial),
      observationPublished(4201, "MID", { occurredAt: CAPTURED_AT }),
      observationPublished(4202, "RHS", { occurredAt: CAPTURED_AT }),
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
    expect(lhs).not.toHaveTextContent(/mV/);
    // measured_watts is null: no fabricated power figure on the card at all
    expect(lhs).not.toHaveTextContent(/\d+\s*W/);
    expect(lhs).toHaveTextContent(/no observation received/i);

    // a unit whose data the service judged unusable is marked, not hidden
    const rhs = screen.getByRole("group", { name: "RHS" });
    expect(rhs).toHaveTextContent(/stale/i);

    // the healthy unit still shows the real measurement
    const mid = screen.getByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/2,?400\s*W/);
    expect(mid).toHaveTextContent(/telemetry sequence 42010/);
  });

  it("names a unknown age as unknown instead of guessing one", async () => {
    const noAge = snapshot(
      [
        ...HEALTHY_SNAPSHOT.units.filter((u) => u.unit_id !== "RHS"),
        unitSnapshot({
          unit_id: "RHS",
          lifecycle: "disconnected",
          telemetry_age_s: null,
          quality: "missing",
          measured_watts: null,
        }),
      ],
      { snapshot_sequence: 4300, captured_at: CAPTURED_AT },
    );
    renderView(healthyClient(noAge, fleetEvents(noAge)));

    const rhs = await screen.findByRole("group", { name: "RHS" });
    expect(rhs).toHaveTextContent(/age unknown/);
    expect(tightestText(rhs, /charge/i)).not.toMatch(/last update/);
  });

  it("keeps the last snapshot visible with a disconnected notice when the event stream drops", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    // every attempt delivers the snapshot frame plus the LHS and MID
    // observations (through sequence 4102) and then drops, so the notice
    // persists and the cards' observation state is honestly per-unit.
    client.openEvents.mockImplementation(() =>
      openStream(HEALTHY_EVENTS.slice(0, 3), "fail"),
    );
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/last observation:\s*10:00 UTC/i);
    expect(mid).toHaveTextContent(/telemetry sequence 41020/);

    expect(await screen.findByText(/disconnected/i)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "MID" })).toBeInTheDocument();
    // RHS's observation (sequence 4103) never arrived: named, not invented
    expect(screen.getByRole("group", { name: "RHS" })).toHaveTextContent(
      /no observation received/i,
    );

    // the socket retries automatically, carrying the last delivered sequence
    // (4102, the MID observation) as the cursor — not the snapshot's 4100
    await waitFor(() =>
      expect(client.openEvents.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
    expect(client.openEvents).toHaveBeenLastCalledWith(4102);
  });

  it("shows the disconnected notice from the shell's connection fact while the last snapshot stays rendered", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    client.openEvents.mockReturnValue(openStream(HEALTHY_EVENTS));
    renderView(client, "disconnected");

    expect(await screen.findByText(/disconnected/i)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "MID" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "MID" })).toHaveTextContent(/2,?400\s*W/);
    expect(screen.getByRole("group", { name: "RHS" })).toBeInTheDocument();
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
    // The unit-detail read settles first (the empty projection: every datum
    // absent), then min/max/spread are named fields with their missing-ness
    // stated as text, never colour alone — and never an invented reading.
    await waitFor(() => {
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*no data/i);
      expect(cells).toHaveTextContent(/maximum cell voltage:\s*no data/i);
      expect(cells).toHaveTextContent(/voltage spread:\s*no data/i);
      expect(cells).toHaveTextContent(/temperature range:\s*no data/i);
      expect(cells).not.toHaveTextContent(/mV/);
      // data completeness states what the wire carries
      expect(cells).toHaveTextContent(/complet/i);
      expect(cells).toHaveTextContent(/no cell or temperature readings/i);
    });

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
    const page = auditPage([
      auditEvent({
        sequence: 4080,
        event_type: "control_decision",
        unit_id: "MID",
        result: "rejected",
        reason_codes: ["temperature_high", "telemetry_stale"],
        occurred_at: "2026-08-22T09:58:00Z",
      }),
      auditEvent({
        sequence: 4021,
        event_type: "inhibit_acknowledged",
        unit_id: "MID",
        result: "acknowledged",
        reason_codes: ["latch_cleared"],
        occurred_at: "2026-08-22T09:31:00Z",
      }),
    ]);
    const client = healthyClient(HEALTHY_SNAPSHOT, HEALTHY_EVENTS);
    client.getAudit.mockResolvedValue(page);
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Events" }));

    const panel = await screen.findByRole("tabpanel");
    expect(within(panel).getByText(/temperature/i)).toBeInTheDocument();
    expect(within(panel).getByText(/acknowledg/i)).toBeInTheDocument();
    // raw codes are not shown until asked for
    expect(screen.queryByText(/temperature_high/)).not.toBeInTheDocument();
    expect(screen.queryByText(/latch_cleared/)).not.toBeInTheDocument();

    const items = within(panel).getAllByRole("listitem");
    expect(items.length).toBeGreaterThanOrEqual(2);
    const rejectedItem = items.find((item) => /refused/i.test(item.textContent ?? ""));
    expect(rejectedItem).toBeDefined();
    const rejected = rejectedItem as HTMLElement;
    await user.click(within(rejected).getByRole("button", { name: /technical/i }));
    expect(within(rejected).getByText(/temperature_high/)).toBeInTheDocument();
    expect(within(rejected).getByText(/event_type: control_decision/)).toBeInTheDocument();
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

  it("offers acknowledgement from the inhibited lifecycle while the snapshot does not expose the latch detail, and never invents a latch reason", async () => {
    // Today's snapshot carries no `inhibit` field at all: the control must
    // still be reachable for an inhibited unit, the latch detail must render
    // not-available, and the server alone reports whether anything cleared.
    const inhibited = {
      ...HEALTHY_SNAPSHOT,
      units: [
        ...HEALTHY_SNAPSHOT.units.filter((u) => u.unit_id !== "MID"),
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "inhibited",
          telemetry_age_s: 2,
          measured_watts: 0,
        }),
      ],
    } satisfies WireSnapshot;
    const client = healthyClient(inhibited, fleetEvents(inhibited));
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/latched state not available/i);
    expect(
      within(mid).getByRole("button", { name: /acknowledge/i }),
    ).toBeInTheDocument();

    // no fabricated cause: the dialog names the missing reason, not a code
    const user = userEvent.setup();
    await user.click(within(mid).getByRole("button", { name: /acknowledge/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/not available/i);
    expect(dialog).not.toHaveTextContent(/identity_mismatch/);
    expect(dialog).not.toHaveTextContent(/blocking_fault/);
  });

  it("confirms acknowledgement, explains re-arming is separate, and clears the latch from a refetch", async () => {
    const user = userEvent.setup();
    const latched = latchedSnapshot();
    const cleared: WireSnapshot = {
      ...latched,
      units: latched.units.map((u): WireUnitSnapshot =>
        u.unit_id === "MID" ? { ...u, lifecycle: "disarmed", inhibit: null } : u,
      ),
    };
    const client = makeClient();
    client.getSnapshot.mockResolvedValueOnce(latched).mockResolvedValue(cleared);
    client.openEvents.mockReturnValue(openStream(latchedEvents(latched)));
    client.postInhibitAcknowledgement.mockResolvedValue({
      unit_id: "MID",
      status: "acknowledged",
      latch_cleared: true,
    });
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    await user.click(within(mid).getByRole("button", { name: /acknowledge/i }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/acknowledg/i);
    expect(dialog).toHaveTextContent(/separate/i);
    expect(dialog).toHaveTextContent(/arm/);
    // the reason is the real latched cause in plain words
    expect(dialog).toHaveTextContent(/identity mismatch/i);

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
