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
  actuationIncoherent,
  auditAppended,
  auditEvent,
  auditPage,
  emptyUnitDetail,
  observationPublished,
  snapshot,
  snapshotFrame,
  telemetrySummary,
  unitDetail,
  unitHealthChanged,
  unitSnapshot,
  unitUnexpectedAutonomy,
  withHealth,
  withInhibit,
  withSnapshotIntent,
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
  // Per-unit request truth (the 2026-08-23 false-Limited defect family)
  // ---------------------------------------------------------------------------
  //
  // The snapshot's per-unit `requested_power` repeats the intent's FLEET TOTAL
  // once per covered unit, so a shared figure is never this battery's own
  // request: without per-unit figures it is labelled AS the fleet total, and
  // with them (the shared tracker's maps, seeded live by the decision
  // summaries or cold by the snapshot `intent` block) the exact per-battery
  // figure renders. The allowance is genuinely per-unit on the wire either way.

  /** Three active batteries repeating one intent's 3,000 W total. */
  function sharedTotalWorld(): WireSnapshot {
    return snapshot(
      [
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "active",
          telemetry_age_s: 2,
          requested_power: { direction: "discharge", watts: 3000 },
          authorized_power: { direction: "discharge", watts: 1000 },
          measured_watts: 990,
        }),
        unitSnapshot({
          unit_id: "RHS",
          lifecycle: "active",
          telemetry_age_s: 3,
          requested_power: { direction: "discharge", watts: 3000 },
          authorized_power: { direction: "discharge", watts: 1000 },
          measured_watts: 980,
        }),
        unitSnapshot({
          unit_id: "LHS",
          lifecycle: "active",
          telemetry_age_s: 4,
          requested_power: { direction: "discharge", watts: 3000 },
          authorized_power: { direction: "discharge", watts: 1000 },
          measured_watts: 970,
        }),
      ],
      { snapshot_sequence: 4100, captured_at: CAPTURED_AT },
    );
  }

  it("labels the snapshot's repeated figure as the fleet total — never this battery's own request", async () => {
    const user = userEvent.setup();
    const state = sharedTotalWorld();
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(state);
    client.openEvents.mockReturnValue(openStream(fleetEvents(state)));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    const summary = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(summary).toHaveTextContent(
        /requested discharging — fleet total 3,000 W across 3 batteries/i,
      );
    });
    // The allowance IS per-unit on the wire: 1,000 W for this battery.
    expect(summary).toHaveTextContent(/allowed discharging 1,000 W/i);
    expect(summary.textContent ?? "").not.toMatch(/requested discharging 3,000 W/);
  });

  it("renders the exact per-battery figure once the snapshot carries its intent block (cold load)", async () => {
    const user = userEvent.setup();
    const state = withSnapshotIntent(sharedTotalWorld(), {
      requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
      authorized_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
      directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
    });
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(state);
    client.openEvents.mockReturnValue(openStream(fleetEvents(state)));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    const summary = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(summary).toHaveTextContent(/requested discharging 1,000 W/i);
    });
    expect(summary.textContent ?? "").not.toMatch(/fleet total/);
    expect(summary).toHaveTextContent(/allowed discharging 1,000 W/i);
  });

  it("picks the exact per-battery figure up from the kernel's decision summaries on the stream", async () => {
    const user = userEvent.setup();
    const state = sharedTotalWorld();
    const events = [
      ...fleetEvents(state),
      // The cycle's own maps: each unit's winning target, authorized in full.
      auditAppended(4104, {
        event_type: "control_decision",
        result: "authorized",
        requested_active_w: 3000,
        authorized_active_w: 3000,
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
      }),
    ];
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(state);
    client.openEvents.mockReturnValue(openStream(events));
    renderView(client);

    await user.click(await screen.findByRole("button", { name: "MID" }));
    const summary = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(summary).toHaveTextContent(/requested discharging 1,000 W/i);
    });
    expect(summary.textContent ?? "").not.toMatch(/fleet total/);
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

  it("carries the advisory grid/load readthrough per unit — the same import/export wording as the Home tile, with honest gaps", async () => {
    // The excess-solar package's W-D: `grid_power_w` / `load_power_w` are live
    // on today's wire (service.py `_telemetry_summary`), signed negative =
    // import, positive = export, and null whenever the poll served no PCS
    // live block — that gap is named, never zero-filled.
    const world: WireSnapshot = snapshot([
      unitSnapshot({
        unit_id: "MID",
        lifecycle: "disarmed",
        telemetry: telemetrySummary({ grid_power_w: 620, load_power_w: 340 }),
      }),
      unitSnapshot({
        unit_id: "RHS",
        lifecycle: "disarmed",
        telemetry: telemetrySummary({ grid_power_w: -800, load_power_w: 210 }),
      }),
      // No observation at all: the row still renders, both figures absent.
      unitSnapshot({ unit_id: "LHS", lifecycle: "disarmed", telemetry: null }),
    ]);
    renderView(healthyClient(world, fleetEvents(world)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/grid and load:\s*grid \+620 W export · load 340 W/i);
    const rhs = screen.getByRole("group", { name: "RHS" });
    expect(rhs).toHaveTextContent(/grid and load:\s*grid -800 W import · load 210 W/i);
    const lhs = screen.getByRole("group", { name: "LHS" });
    expect(lhs).toHaveTextContent(/grid and load:\s*grid not available · load not available/i);
  });

  it("warns per-unit when the cell spread crosses the 50 mV early-warning line, and stays silent under it or without a reading", async () => {
    // The 2026-08-23 policy relaxation: imbalance no longer blocks dispatch on
    // its own, so the console keeps the OLD 50 mV gate visible as a warning —
    // exactly the units whose spread crosses it, with the measured figure
    // (LHS 53.999... mV is the live figure that vetoed the fleet that day).
    const spread: WireSnapshot = snapshot(
      [
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "disarmed",
          telemetry_age_s: 2,
          measured_watts: 0,
          telemetry: telemetrySummary({ cell_spread_mv: 22 }),
        }),
        unitSnapshot({
          unit_id: "RHS",
          lifecycle: "disarmed",
          telemetry_age_s: 3,
          measured_watts: 0,
          telemetry: telemetrySummary({ cell_spread_mv: 53.99999999999983 }),
        }),
        // No observation at all: a missing spread never fabricates a warning.
        unitSnapshot({
          unit_id: "LHS",
          lifecycle: "observe_only",
          telemetry_age_s: 4,
          measured_watts: null,
          telemetry: null,
        }),
      ],
      { snapshot_sequence: 4600, captured_at: CAPTURED_AT },
    );
    renderView(healthyClient(spread, fleetEvents(spread)));

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/cell spread:\s*22\s*mV across 60 cells/i);
    expect(mid).not.toHaveTextContent(/cell imbalance warning/i);

    const rhs = screen.getByRole("group", { name: "RHS" });
    expect(rhs).toHaveTextContent(
      /cell imbalance warning:\s*54\s*mV spread \(above the 50 mV early-warning line; not blocking dispatch on its own\)/i,
    );

    const lhs = screen.getByRole("group", { name: "LHS" });
    expect(lhs).not.toHaveTextContent(/cell imbalance warning/i);
  });

  it("renders every telemetry figure at the two-decimal display bound: a raw eight-decimal fixture never reaches the operator", async () => {
    const user = userEvent.setup();
    // The wire can carry float-decoded telemetry at full precision; the
    // shared display-precision module (src/lib/format.ts) is the render
    // boundary — at most two decimals, integers as integers, trailing zeros
    // trimmed — for the card rows and the Cells tab alike. The negative
    // battery watts ride the direction word (charging), magnitude bounded.
    const precise: WireSnapshot = snapshot(
      [
        unitSnapshot({
          unit_id: "MID",
          lifecycle: "disarmed",
          telemetry_age_s: 2.987654321,
          measured_watts: null,
          telemetry: telemetrySummary({
            soc_pct: 46.55555555,
            pack_voltage_v: 192.45678912,
            battery_watts: -1132.56789012,
            cell_spread_mv: 12.345678,
            temperature_min_c: 23.456789,
            temperature_max_c: 27.654321,
          }),
        }),
      ],
      { snapshot_sequence: 4500, captured_at: CAPTURED_AT },
    );
    const client = healthyClient(precise, fleetEvents(precise));
    // The on-demand detail read carries the same full-precision floats.
    client.getUnitDetail.mockResolvedValue(
      unitDetail("MID", {
        cell_min_v: 3.20512345678,
        cell_max_v: 3.20987654321,
        cell_spread_mv: 12.345678,
        temperature_min_c: 23.456789,
        temperature_max_c: 27.654321,
        cell_voltages_v: [3.20512345678, 3.20987654321],
        temperatures_c: [23.456789, 27.654321],
      }),
    );
    renderView(client);

    const mid = await screen.findByRole("group", { name: "MID" });
    expect(mid).toHaveTextContent(/charge level:\s*46\.56%/i);
    expect(mid).toHaveTextContent(/pack voltage:\s*192\.46 V/i);
    expect(mid).toHaveTextContent(/power:\s*charging 1,132\.57 W/i);
    expect(mid).toHaveTextContent(/cell spread:\s*12\.35 mV across 60 cells/i);
    expect(mid).toHaveTextContent(/temperature:\s*23\.46 °C to 27\.65 °C/i);
    // The fractional age lands on whole seconds, like every age on screen.
    expect(mid).toHaveTextContent(/3 seconds old/);
    expect(mid.textContent ?? "").not.toMatch(/\d\.\d{3,}/);

    // The Cells tab's per-cell distribution holds the same bound.
    await user.click(screen.getByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Cells" }));
    const cells = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.21 V/i);
      expect(cells).toHaveTextContent(/maximum cell voltage:\s*3\.21 V/i);
      expect(cells).toHaveTextContent(/voltage spread:\s*12\.35 mV/i);
    });
    const grid = within(cells).getByRole("list", { name: /cell voltages/i });
    const cellItems = within(grid).getAllByRole("listitem");
    expect(cellItems[0]!.textContent).toBe("Cell 1: 3.21 V");
    expect(cellItems[1]!.textContent).toBe("Cell 2: 3.21 V");
    expect(cells.textContent ?? "").not.toMatch(/\d\.\d{3,}/);
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
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.21 V/i);
      expect(cells).toHaveTextContent(/maximum cell voltage:\s*3\.21 V/i);
      expect(cells).toHaveTextContent(/voltage spread:\s*4 mV/i);
      expect(cells).toHaveTextContent(/temperature range:\s*23 °C to 28 °C/i);
    });

    // The complete per-cell distribution is text-readable: every one of the
    // 60 captured cells, first to last.
    const grid = within(cells).getByRole("list", { name: /cell voltages/i });
    const cellItems = within(grid).getAllByRole("listitem");
    expect(cellItems).toHaveLength(60);
    expect(cellItems[0]!.textContent).toBe("Cell 1: 3.21 V");
    expect(cellItems[59]!.textContent).toBe("Cell 60: 3.21 V");

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
    expect(pending).not.toHaveTextContent(/3\.21\s*V/);

    resolveDetail(unitDetail("MID"));
    await waitFor(() => {
      expect(screen.getByRole("tabpanel")).toHaveTextContent(/minimum cell voltage:\s*3\.21 V/i);
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
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.21 V/i);
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
      expect(cells).toHaveTextContent(/minimum cell voltage:\s*3\.21 V/i);
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

// --- live surfaces that must not freeze at their open-time picture -----------
//
// The 2026-08-23 audit's defect class: a panel whose data was fetched ONCE at
// open while its age lines kept ticking — the illusion of updating. The unit
// detail (Cells/Summary tabs) re-reads whenever the world advances while it is
// open, and the per-unit Events tab appends what the bus actually emits.

describe("BatteriesView — open panels stay live", () => {
  /** A pushable stream: the initial frames, then whatever the test pushes. */
  function liveStream(initial: readonly StreamEvent[]): {
    stream: AsyncIterable<StreamEvent>;
    push(frame: StreamEvent): void;
  } {
    const queue: StreamEvent[] = [...initial];
    let wake: (() => void) | null = null;
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    const stream = {
      async *[Symbol.asyncIterator](): AsyncGenerator<StreamEvent, void, unknown> {
        while (true) {
          while (queue.length > 0) {
            const next = queue.shift();
            if (next !== undefined) {
              yield next;
            }
          }
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
        }
      },
    };
    return {
      stream,
      push: (frame) => {
        queue.push(frame);
        notify();
      },
    };
  }

  it("re-reads the open unit detail when the world advances, without flashing loading over the data", async () => {
    const first = unitDetail("MID", { battery_watts: 1000, pack_voltage_v: 190.1 });
    const second = unitDetail("MID", { battery_watts: 640, pack_voltage_v: 191.7 });
    let reads = 0;
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    client.getUnitDetail.mockImplementation(() => {
      reads += 1;
      return Promise.resolve(reads === 1 ? first : second);
    });
    const channel = liveStream(fleetEvents(HEALTHY_SNAPSHOT));
    client.openEvents.mockReturnValue(channel.stream);

    render(<BatteriesView client={client as unknown as ApiClient} />);
    const midCard = await screen.findByRole("group", { name: "MID" });
    await waitFor(() => {
      expect(midCard).toHaveTextContent(/discharging 2,400 W/i);
    });

    // Open the unit: the Cells tab renders the first full projection.
    await userEvent.click(within(midCard).getByRole("button", { name: "MID" }));
    const cellsTab = await screen.findByRole("tab", { name: "Cells" });
    await userEvent.click(cellsTab);
    await waitFor(() => {
      const line = screen.getByText(/data completeness/i).closest("p");
      expect(line?.textContent ?? "").toContain("60 of 60");
    });
    expect(client.getUnitDetail).toHaveBeenCalledTimes(1);

    // The world advances (a republished snapshot with a higher sequence): the
    // open detail re-reads silently — no loading flash, new figures.
    const advanced: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      snapshot_sequence: HEALTHY_SNAPSHOT.snapshot_sequence + 10,
    };
    channel.push(snapshotFrame(advanced));
    await waitFor(() => {
      expect(client.getUnitDetail).toHaveBeenCalledTimes(2);
    });
    // The re-read is SILENT: the ready detail never regressed to loading.
    expect(screen.queryByText(/loading the cell/i)).toBeNull();
    expect(screen.queryByText(/loading the unit/i)).toBeNull();
  });

  it("appends audit.appended facts to the per-unit Events tab as they happen, not at the next mount", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    const channel = liveStream(fleetEvents(HEALTHY_SNAPSHOT));
    client.openEvents.mockReturnValue(channel.stream);

    render(<BatteriesView client={client as unknown as ApiClient} />);
    const midCard = await screen.findByRole("group", { name: "MID" });
    await userEvent.click(within(midCard).getByRole("button", { name: "MID" }));
    await userEvent.click(await screen.findByRole("tab", { name: "Events" }));

    // The loaded history is empty; the bus delivers a decision for MID now.
    expect(await screen.findByText(/no audit events recorded for this unit yet/i)).toBeVisible();

    channel.push({
      type: "audit.appended",
      sequence: HEALTHY_SNAPSHOT.snapshot_sequence + 7,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        event_id: "event-live-1",
        event_type: "control_decision",
        unit_id: "MID",
        generation: 2,
        result: "authorized",
        reason_codes: ["safety_checks_passed"],
      },
    } as unknown as StreamEvent);

    await waitFor(() => {
      const list = document.querySelector(".event-list");
      expect(list?.textContent ?? "").toContain("Power decision");
      expect(list?.textContent ?? "").toContain("Allowed");
      expect(list?.textContent ?? "").toMatch(/safety checks passed/i);
    });
    // A second fact for ANOTHER unit does not leak into this unit's tab.
    channel.push({
      type: "audit.appended",
      sequence: HEALTHY_SNAPSHOT.snapshot_sequence + 8,
      occurred_at: "2026-08-22T12:00:08+10:00",
      payload: {
        event_id: "event-live-2",
        event_type: "control_decision",
        unit_id: "RHS",
        generation: 2,
        result: "rejected",
        reason_codes: ["no_setpoints"],
      },
    } as unknown as StreamEvent);
    await waitFor(() => {
      const list = document.querySelector(".event-list");
      expect(list?.textContent ?? "").not.toContain("event-live-2");
      expect(list?.textContent ?? "").not.toMatch(/refused/i);
    });
  });
});

// ---------------------------------------------------------------------------
// The self-healing awareness layer (API_CONTRACTS.md "Self-healing awareness
// layer (recovery detection)" — live on the controller since b20058d..7b491b3).
//
// WIRE TRUTH (service.py `_health_projection` + recovery.py, fixtures from
// wire.ts — `withHealth`, `unitHealthChanged`, `actuationIncoherent`,
// `unitUnexpectedAutonomy`, never hand-built here): every snapshot unit
// carries `health_state` / `health_reasons` / `remediation_hint` once the
// layer is composed (nulls when its projection is absent), the keys are
// ABSENT on the older wire (the feature detection), and the bus carries the
// layer's own three events. The badge's per-state wording is pinned in
// web/src/app/unitHealth.test.tsx; this suite pins the CARDS' adoption.
// ---------------------------------------------------------------------------

describe("BatteriesView — the per-unit recovery health badge", () => {
  /** A snapshot whose MID carries the given derived health (and optionally
   * its own authorized figure, for the incoherence story's commanded watts). */
  function healthSnapshot(
    health: {
      state?: string | null;
      reasons?: readonly string[] | null;
      remediation_hint?: string | null;
    },
    authorized?: { direction: string; watts: number },
  ): WireSnapshot {
    return {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((unit) =>
        unit.unit_id === "MID"
          ? withHealth(
              {
                ...unit,
                ...(authorized === undefined
                  ? {}
                  : { authorized_power: authorized, requested_power: authorized }),
              },
              health,
            )
          : unit,
      ),
    };
  }

  function cardFor(unitId: string): HTMLElement {
    return screen.getByRole("group", { name: unitId });
  }

  /** A pushable stream: the snapshot's opening burst, then test-pushed frames. */
  function liveChannel(initial: readonly StreamEvent[]): {
    stream: AsyncIterable<StreamEvent>;
    push(frame: StreamEvent): void;
  } {
    const queue: StreamEvent[] = [...initial];
    let wake: (() => void) | null = null;
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    return {
      stream: {
        async *[Symbol.asyncIterator](): AsyncGenerator<StreamEvent, void, unknown> {
          while (true) {
            while (queue.length > 0) {
              const next = queue.shift();
              if (next !== undefined) {
                yield next;
              }
            }
            await new Promise<void>((resolve) => {
              wake = resolve;
            });
          }
        },
      },
      push: (frame) => {
        queue.push(frame);
        notify();
      },
    };
  }

  it.each([
    [
      "self_healing on solar self-charge",
      { state: "self_healing", reasons: ["autonomous_self_charge"] },
      "Managing itself — solar self-charge",
    ],
    [
      "self_healing re-qualifying after an inhibit",
      { state: "self_healing", reasons: ["requalifying_after_inhibit"] },
      "Re-qualifying",
    ],
    [
      "self_healing while cell balancing",
      { state: "self_healing", reasons: ["cell_balancing"] },
      "Cell balancing",
    ],
    [
      "an unreachable gateway path",
      { state: "unreachable", reasons: ["gateway_unreachable"] },
      "Gateway unreachable",
    ],
  ] as const)("renders the badge on the card for %s", async (_name, health, words) => {
    const state = healthSnapshot(health);
    renderView(healthyClient(state, fleetEvents(state)));
    const midCard = await screen.findByRole("group", { name: "MID" });
    expect(within(midCard).getByRole("note").textContent).toBe(words);
    // The other cards, whose units carry no health fields, stay silent.
    const rhsCard = await screen.findByRole("group", { name: "RHS" });
    expect(within(rhsCard).queryByRole("note")).toBeNull();
  });

  it("tells the plain incoherence story with the battery's own commanded figure", async () => {
    const state = healthSnapshot(
      { state: "actuation_incoherent", reasons: ["authorized_not_actuating"] },
      { direction: "discharge", watts: 1000 },
    );
    renderView(healthyClient(state, fleetEvents(state)));
    const midCard = await screen.findByRole("group", { name: "MID" });
    expect(within(midCard).getByRole("note").textContent).toBe(
      "Commanded 1,000 W but the battery isn't moving — investigating",
    );
  });

  it("renders the prominent honest terminal for not_responding with the hint VERBATIM", async () => {
    const hint =
      "pod not responding — remote recovery exhausted; physical restart required (power-cycle the pod, then verify telemetry resumes, Debug Mode reads Normal Mode and SysControlMode reads Remote in the vendor MiniES app — see docs/POD_RECOVERY_RESEARCH.md R5)";
    const state = healthSnapshot({
      state: "not_responding",
      reasons: ["reads_timing_out"],
      remediation_hint: hint,
    });
    const { container } = renderView(healthyClient(state, fleetEvents(state)));
    const midCard = await screen.findByRole("group", { name: "MID" });
    expect(within(midCard).getByRole("note").textContent).toContain(
      "Not responding — remote recovery exhausted",
    );
    // The backend's own remediation words, whole and un-reworded.
    expect(container.querySelector(".unit-health-hint")?.textContent).toBe(hint);
  });

  it.each([
    ["no health fields at all (the older wire)", (unit: WireUnitSnapshot) => unit],
    [
      "null fields (the layer composed, the projection absent)",
      (unit: WireUnitSnapshot) =>
        withHealth(unit, { state: null, reasons: null, remediation_hint: null }),
    ],
    [
      "a healthy state (silence is the design)",
      (unit: WireUnitSnapshot) => withHealth(unit, { state: "healthy", reasons: [] }),
    ],
    [
      "a foreign writer (the inhibit latch surface already tells it)",
      (unit: WireUnitSnapshot) =>
        withHealth(unit, { state: "foreign_writer", reasons: ["external_writer_latched"] }),
    ],
    [
      "an inhibited unit (the same existing surface)",
      (unit: WireUnitSnapshot) => withHealth(unit, { state: "inhibited", reasons: ["inhibited"] }),
    ],
  ] as const)("renders NO health badge for %s", async (_name, attach) => {
    const state: WireSnapshot = {
      ...HEALTHY_SNAPSHOT,
      units: HEALTHY_SNAPSHOT.units.map((unit) => (unit.unit_id === "MID" ? attach(unit) : unit)),
    };
    const { container } = renderView(healthyClient(state, fleetEvents(state)));
    const midCard = await screen.findByRole("group", { name: "MID" });
    // The card still renders every ordinary field; only the badge is absent.
    expect(within(midCard).getByText(/Availability:/i)).toBeInTheDocument();
    expect(container.querySelectorAll(".unit-health")).toHaveLength(0);
  });

  it("applies a live unit.health_changed transition without waiting for a snapshot re-read", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    const channel = liveChannel(fleetEvents(HEALTHY_SNAPSHOT));
    client.openEvents.mockReturnValue(channel.stream);
    renderView(client);
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "RHS" })).toBeInTheDocument();
    });
    expect(client.getSnapshot).toHaveBeenCalledTimes(1);

    // rhs quietly starts managing itself (today's live rhs/lhs story): the
    // badge moves from the frame alone — no refetch, no reconnect.
    channel.push(
      unitHealthChanged(4201, {
        unit_id: "RHS",
        from: "healthy",
        to: "self_healing",
        reasons: ["autonomous_self_charge"],
      }),
    );
    await waitFor(() => {
      expect(within(cardFor("RHS")).getByRole("note").textContent).toBe(
        "Managing itself — solar self-charge",
      );
    });
    expect(client.getSnapshot).toHaveBeenCalledTimes(1);
  });

  it("moves the badge to the warning on an actuation.incoherent detection and its echo follow-up", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    const channel = liveChannel(fleetEvents(HEALTHY_SNAPSHOT));
    client.openEvents.mockReturnValue(channel.stream);
    renderView(client);
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "MID" })).toBeInTheDocument();
    });

    channel.push(
      actuationIncoherent(4201, {
        unit_id: "MID",
        authorized_watts: 1000,
        cycles: 4,
        measured_watts: 12,
        baseline_watts: 8,
        movement_watts: 4,
      }),
    );
    // MID's own snapshot authorization is 2,400 W: the story names THAT
    // battery's own figure, never the event's fleet-level number alone.
    await waitFor(() => {
      expect(within(cardFor("MID")).getByRole("note").textContent).toBe(
        "Commanded 2,400 W but the battery isn't moving — investigating",
      );
    });

    // The echo-classified follow-up frame of the same episode keeps the badge
    // (the discriminator is the shell's announcement, not a second badge).
    channel.push(
      actuationIncoherent(4202, {
        unit_id: "MID",
        authorized_watts: 1000,
        echo: { classification: "echo_matches_write", served_active_w: 1000 },
      }),
    );
    await waitFor(() => {
      expect(within(cardFor("MID")).getAllByRole("note")).toHaveLength(1);
    });
  });

  it("keeps unexpected_autonomy evidence off the cards entirely (Activity owns its quiet story)", async () => {
    const client = makeClient();
    client.getSnapshot.mockResolvedValue(HEALTHY_SNAPSHOT);
    const channel = liveChannel(fleetEvents(HEALTHY_SNAPSHOT));
    client.openEvents.mockReturnValue(channel.stream);
    const { container } = renderView(client);
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "MID" })).toBeInTheDocument();
    });

    channel.push(unitUnexpectedAutonomy(4201, { unit_id: "MID", measured_watts: 1411.2 }));
    await waitFor(() => {
      expect(client.openEvents).toHaveBeenCalled();
    });
    expect(container.querySelectorAll(".unit-health")).toHaveLength(0);
    expect(container.textContent).not.toContain("Uncommanded activity");
  });
});
