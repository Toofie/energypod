/**
 * Behavior contract for the "Flow" (Energy Flow) view
 * (web/src/views/flow/FlowView.tsx).
 *
 * PROP SEAM (web/src/app/views.ts `ShellViewProps`): the composed app mounts
 * this view through the shell's injection point with exactly one prop — the
 * session's shared `{ client }` (a second client would open a second events
 * socket). The suite injects the client the same way.
 *
 * The client module is mocked at its exact surface (`createApiClient`
 * replaced, every other canonical export preserved), exactly like the other
 * view suites; envelope fixtures mirror the real REST/WS wire (lowercase
 * enums, signed telemetry: grid negative = import / positive = export,
 * battery negative = charge / positive = discharge).
 *
 * The pins:
 *
 * - RENDERING PER STATE: the all-idle site, the charging fleet, the mixed
 *   discharging/charging fleet, the import+export split across phases, and
 *   the adviser-active picture with its commanded-vs-measured overlay — each
 *   with its story sentence and per-phase worded figures.
 * - SIGN-TO-WORDS DISCIPLINE: a snapshot full of negative watts never puts a
 *   raw negative on screen; every flow renders as a direction word plus the
 *   absolute magnitude.
 * - LIVE-UPDATE PIN: two successive snapshots change the figures on screen.
 * - ACCESSIBILITY: each phase column carries its whole state as a text label;
 *   the diagram's SVG is decorative duplication, never the only carrier.
 * - FEATURE-ABSENT HONESTY: absent telemetry words "not available" (never
 *   zero-filled); absent adviser projections claim nothing; the solar
 *   footnote states the site fact.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { FlowView } from "./FlowView";

const api = vi.hoisted(() => {
  const client = {
    getSnapshot: vi.fn(),
    getHealth: vi.fn(),
    getAudit: vi.fn(),
    postIntent: vi.fn(),
    postArm: vi.fn(),
    postDisarm: vi.fn(),
    postEmergencyStop: vi.fn(),
    openEvents: vi.fn(),
  };
  return { createApiClient: vi.fn(), client };
});

/** The mocked client as the shell hands it to the view (one bridging cast). */
function injectedClient(): ApiClient {
  return api.client as unknown as ApiClient;
}

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: api.createApiClient,
}));

// --- envelope fixtures (shapes from the real service adapter) -------------------

type PowerView = { direction: string; watts: number };

type TelemetrySpec = {
  soc_pct?: number | null;
  grid_power_w?: number | null;
  load_power_w?: number | null;
  battery_watts?: number | null;
};

type UnitSpec = {
  unit_id: string;
  lifecycle?: string;
  requested_power?: PowerView;
  authorized_power?: PowerView | null;
  measured_watts?: number | null;
  telemetry?: TelemetrySpec | null;
};

type SnapshotEnvelope = {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: UnitSpec[];
  adviser_state?: Record<string, unknown>;
  night_charge_state?: Record<string, unknown>;
};

/** A unit whose telemetry is present with every flow datum explicit. */
function unit(
  unitId: string,
  telemetry: TelemetrySpec = {},
  over: Partial<UnitSpec> = {},
): UnitSpec {
  return {
    unit_id: unitId,
    lifecycle: "active",
    requested_power: { direction: "idle", watts: 0 },
    authorized_power: null,
    measured_watts: null,
    telemetry: {
      soc_pct: null,
      grid_power_w: null,
      load_power_w: null,
      battery_watts: null,
      ...telemetry,
    },
    ...over,
  };
}

function snapshotEnvelope(units: UnitSpec[], sequence = 41): SnapshotEnvelope {
  return {
    site_id: "site-1",
    snapshot_sequence: sequence,
    captured_at: "2026-08-22T12:00:00+10:00",
    units,
  };
}

type Frame = Record<string, unknown> & { type: string };

/** A controllable stream: yields the initial frames, then pushed frames. */
function liveChannel(initial: Frame[]): { push(frame: Frame): void } {
  const queue: Frame[] = [...initial];
  let wake: (() => void) | null = null;
  api.client.openEvents.mockImplementation(() => {
    return (async function* channel(): AsyncGenerator<Frame, void, unknown> {
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
    })();
  });
  return {
    push: (frame) => {
      queue.push(frame);
      const release = wake;
      wake = null;
      release?.();
    },
  };
}

/** A stream that delivers its frames and then ends (connection loss). */
function endingStream(frames: Frame[]): void {
  api.client.openEvents.mockImplementationOnce(() => {
    return (async function* ending(): AsyncGenerator<Frame, void, unknown> {
      for (const frame of frames) {
        yield frame;
      }
    })();
  });
}

function renderFlow() {
  render(<FlowView client={injectedClient()} />);
}

beforeEach(() => {
  vi.resetAllMocks();
});

// --- the states -----------------------------------------------------------------

describe("FlowView — rendering per state", () => {
  it("renders the all-idle site with honest Idle labels and the idle story", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { soc_pct: 100, grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
        unit("rhs", { soc_pct: 98, grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
        unit("lhs", { soc_pct: 99, grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    expect(await screen.findByText(/Nothing is flowing/i)).toBeVisible();
    // Idle says Idle, never a zero dressed up as a flow: grid and battery
    // figures across the three phases plus the fleet column.
    expect((await screen.findAllByText("Idle")).length).toBe(8);
    expect(screen.getAllByText(composedLine("Using 0 W")).length).toBe(4);
    // The SoC wording renders verbatim (the percentage is its own styled run,
    // so the pin matches the composed line, not one text node).
    const socLine = (_: string, element: Element | null): boolean =>
      (element?.textContent ?? "") === "100% charged";
    expect(screen.getAllByText(socLine).length).toBeGreaterThan(0);
  });

  it("renders the charging fleet: per-phase words, fleet sums, and the story", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { soc_pct: 20, grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
        unit("rhs", { soc_pct: 30, grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
        unit("lhs", { soc_pct: 40, grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
      ]),
    );

    renderFlow();

    expect(
      await screen.findByText(
        "All the batteries are charging 5,700 W in total from the grid, the house using 810 W.",
      ),
    ).toBeVisible();
    expect(screen.getAllByText(composedLine("Importing 2,000 W")).length).toBe(3);
    expect(screen.getAllByText(composedLine("Charging 1,900 W")).length).toBe(3);
    expect(screen.getByLabelText(/Phase mid — grid: Importing 2,000 W/)).toBeVisible();
    expect(screen.getByLabelText(/Whole site/i)).toBeVisible();
    // The fleet column words its sums, never a netted import/export figure.
    const fleet = screen.getByLabelText(/Whole site/i);
    expectVisibleText(fleet, /Importing 6,000 W/);
    expectVisibleText(fleet, /Charging 5,700 W/);
    expectVisibleText(fleet, /Using 810 W/);
  });

  it("renders the mixed fleet with no raw negative anywhere on screen", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("lhs", { soc_pct: 60, grid_power_w: 300, load_power_w: 100, battery_watts: 800 }),
        unit("mid", { soc_pct: 25, grid_power_w: -1500, load_power_w: 200, battery_watts: -1900 }),
        unit("rhs", { soc_pct: 35, grid_power_w: -1200, load_power_w: 110, battery_watts: -1900 }),
      ]),
    );

    renderFlow();

    expect(
      await screen.findByText(
        "lhs is discharging 800 W while mid and rhs are charging 3,800 W in total, the house using 410 W, importing 2,700 W on mid and rhs while exporting 300 W on lhs.",
      ),
    ).toBeVisible();
    // The lhs node, and the fleet's own second figure part (the fleet words BOTH
    // sides — its composed line is pinned just below).
    expect(screen.getAllByText(composedLine("Discharging 800 W")).length).toBe(2);
    // The lhs grid node, and the fleet's second figure part (its composed line
    // is pinned just below).
    expect(screen.getAllByText(composedLine("Exporting 300 W")).length).toBe(2);
    // The fleet column words BOTH sides — never a netted figure.
    const fleet = screen.getByLabelText(/Whole site/i);
    expectVisibleText(fleet, /Importing 2,700 W · Exporting 300 W/);
    expectVisibleText(fleet, /Charging 3,800 W · Discharging 800 W/);
    // THE SIGN DISCIPLINE: the wire is full of negatives; the operator sees none.
    const view = document.body.querySelector(".flow-view");
    expect(view?.textContent ?? "").not.toMatch(/-\d/);
  });

  it("renders the import+export split across phases with both fleet sides", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { grid_power_w: -800, load_power_w: 400, battery_watts: 0 }),
        unit("rhs", { grid_power_w: 300, load_power_w: 0, battery_watts: 0 }),
        unit("lhs", { grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    expect(
      await screen.findByText(
        "The phases are pulling different ways, importing 800 W on mid while exporting 300 W on rhs, the house using 400 W.",
      ),
    ).toBeVisible();
    const fleet = screen.getByLabelText(/Whole site/i);
    expectVisibleText(fleet, /Importing 800 W · Exporting 300 W/);
  });
});

// --- the command overlay ------------------------------------------------------------

describe("FlowView — commanded vs measured", () => {
  it("renders the overlay from the snapshot's per-unit authorized figures", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit(
          "mid",
          { soc_pct: 45, grid_power_w: -1980, load_power_w: 340, battery_watts: -980 },
          {
            lifecycle: "active",
            requested_power: { direction: "charge", watts: 1000 },
            authorized_power: { direction: "charge", watts: 1000 },
          },
        ),
        unit("rhs", { soc_pct: 50, grid_power_w: -200, load_power_w: 120, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    expect(
      await screen.findByText(composedLine("mid — commanded 1,000 W charge, charging at 980 W")),
    ).toBeVisible();
    // The request is what the flows are carrying out: the story says so.
    expect(
      screen.getByText(
        "Carrying out a power request — mid is charging 980 W in total from the grid, the house using 460 W.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: /commanded vs delivering/i })).toBeVisible();
  });

  it("renders no overlay while nothing is commanded", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([unit("mid", { grid_power_w: -100, load_power_w: 100, battery_watts: 0 })]),
    );

    renderFlow();

    await screen.findByRole("group", { name: /power flow by phase/i });
    expect(screen.queryByRole("heading", { name: /commanded vs delivering/i })).toBeNull();
  });
});

// --- honesty pins ---------------------------------------------------------------------

describe("FlowView — honesty", () => {
  it("words absent telemetry as not available, never zero-filled, and says so in the story", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([unit("mid", {}, { telemetry: null }), unit("rhs", {}, { telemetry: null })]),
    );

    renderFlow();

    expect(
      await screen.findByText(/No power readings yet — the pods have not reported a measurement/),
    ).toBeVisible();
    expect(screen.getAllByText("not available").length).toBeGreaterThanOrEqual(9);
    // The fleet rollup never sums a silence into a zero.
    const fleet = screen.getByLabelText(/Whole site/i);
    expectVisibleText(fleet, /not available/);
  });

  it("carries the solar footnote on every picture", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([unit("mid", { grid_power_w: 0, load_power_w: 0, battery_watts: 0 })]),
    );

    renderFlow();

    expect(await screen.findByText(/not wired to the pods' sensors/i)).toBeVisible();
  });

  it("renders the measured story — and never a solar claim — while the excess projection is absent", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { grid_power_w: 500, load_power_w: 100, battery_watts: -300 }),
        unit("rhs", { grid_power_w: 500, load_power_w: 100, battery_watts: -300 }),
      ]),
    );

    renderFlow();

    expect(
      await screen.findByText(
        "All the batteries are charging 600 W in total, while the site exports 1,000 W, the house using 200 W.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/Solar-surplus charging is active/i)).toBeNull();
  });

  it("renders the excess adviser's story while its projection says it is commanding", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue({
      site_id: "site-1",
      snapshot_sequence: 41,
      captured_at: "2026-08-22T12:00:00+10:00",
      adviser_state: {
        enabled: true,
        active: true,
        fleet_export_w: 3400,
      },
      units: [
        unit("mid", { grid_power_w: 1200, load_power_w: 100, battery_watts: -950 }),
        unit("rhs", { grid_power_w: 1200, load_power_w: 100, battery_watts: -950 }),
      ],
    } as unknown as SnapshotEnvelope);

    renderFlow();

    expect(
      await screen.findByText(
        "Solar-surplus charging is active — All the batteries are charging 1,900 W in total, while the site exports 3,400 W.",
      ),
    ).toBeVisible();
  });

  it("renders the night strategy's story from its snapshot projection", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue({
      site_id: "site-1",
      snapshot_sequence: 41,
      captured_at: "2026-08-22T12:00:00+10:00",
      night_charge_state: {
        enabled: true,
        active: true,
        phase: "pacing",
        window: { start_local: "00:00", end_local: "06:00", timezone: "Australia/Brisbane" },
      },
      units: [
        unit("mid", { grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
        unit("rhs", { grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
        unit("lhs", { grid_power_w: -2000, load_power_w: 260, battery_watts: -1900 }),
      ],
    } as unknown as SnapshotEnvelope);

    renderFlow();

    expect(
      await screen.findByText(
        "Night charging is running — All the batteries are charging 5,700 W in total, the house using 800 W.",
      ),
    ).toBeVisible();
  });

  it("renders the night strategy's demand-hold story while the house spikes", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue({
      site_id: "site-1",
      snapshot_sequence: 41,
      captured_at: "2026-08-22T12:00:00+10:00",
      night_charge_state: {
        enabled: true,
        active: true,
        phase: "holding_on_demand",
        demand_w: 1900,
      },
      units: [
        unit("mid", { grid_power_w: -1900, load_power_w: 950, battery_watts: 0 }),
        unit("rhs", { grid_power_w: -1900, load_power_w: 950, battery_watts: 0 }),
      ],
    } as unknown as SnapshotEnvelope);

    renderFlow();

    expect(
      await screen.findByText(
        "Night charging is holding — house demand is 1,900 W, so the batteries neither drain nor cycle while the grid meets the house.",
      ),
    ).toBeVisible();
  });

  it("explains an empty fleet", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([]));

    renderFlow();

    expect(await screen.findByText(/No batteries are connected yet/i)).toBeVisible();
  });

  it("renders a refused load verbatim with a retry", async () => {
    api.client.getSnapshot.mockRejectedValue(
      new ApiClientError({
        code: "network_error",
        message: "The EnergyPod service could not be reached",
        details: null,
        request_id: "req-1",
        status: 0,
      }),
    );

    renderFlow();

    const alert = await screen.findByRole("alert");
    expectVisibleText(alert, /network_error/);
    expectVisibleText(alert, /The EnergyPod service could not be reached/);
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
  });
});

// --- the live-update pin ----------------------------------------------------------------

describe("FlowView — live updates", () => {
  it("adopts two successive snapshots: the figures on screen change", async () => {
    const first = snapshotEnvelope(
      [
        unit("mid", { grid_power_w: -412, load_power_w: 340, battery_watts: -1900 }),
        unit("rhs", { grid_power_w: 0, load_power_w: 120, battery_watts: 0 }),
      ],
      41,
    );
    const second = snapshotEnvelope(
      [
        unit("mid", { grid_power_w: -520, load_power_w: 350, battery_watts: -2100 }),
        unit("rhs", { grid_power_w: 300, load_power_w: 130, battery_watts: 800 }),
      ],
      42,
    );
    api.client.getSnapshot.mockResolvedValue(first);
    const channel = liveChannel([]);

    renderFlow();

    expect((await screen.findAllByText(composedLine("Importing 412 W"))).length).toBe(2); // mid + fleet
    expect(screen.getAllByText(composedLine("Charging 1,900 W")).length).toBe(2);

    // The shell's measured-data heartbeat republishes the refreshed snapshot
    // to this view's subscription: sequence 42 advances the picture.
    channel.push({ type: "snapshot", sequence: 42, data: second });

    await waitFor(() => {
      // The 300 ms number tick runs per figure; every changed figure and the
      // old magnitude's disappearance belong to the same wait.
      expect(screen.getAllByText(composedLine("Importing 520 W")).length).toBe(2); // mid + the fleet's part
      expect(screen.getAllByText(composedLine("Charging 2,100 W")).length).toBe(2); // mid + the fleet's part
      expect(screen.getAllByText(composedLine("Exporting 300 W")).length).toBe(2); // rhs + the fleet's part
      expect(screen.queryByText(composedLine("Importing 412 W"))).toBeNull();
    });

    // One client, one stream: the view never minted a second socket.
    expect(api.createApiClient).not.toHaveBeenCalled();
    expect(api.client.openEvents).toHaveBeenCalledTimes(1);
    expect(api.client.openEvents).toHaveBeenCalledWith(41);
  });

  it("does not rewind the picture for a snapshot the picture already advanced past", async () => {
    const first = snapshotEnvelope([unit("mid", { grid_power_w: -412, load_power_w: 340, battery_watts: -1900 })], 41);
    const second = snapshotEnvelope([unit("mid", { grid_power_w: -520, load_power_w: 350, battery_watts: -2100 })], 42);
    const stale = snapshotEnvelope([unit("mid", { grid_power_w: -100, load_power_w: 10, battery_watts: -50 })], 41);
    api.client.getSnapshot.mockResolvedValue(first);
    const channel = liveChannel([]);

    renderFlow();
    await screen.findAllByText(composedLine("Importing 412 W"));

    channel.push({ type: "snapshot", sequence: 42, data: second });
    await waitFor(() => {
      // mid's node and the fleet's grid figure (the only phase importing).
      expect(screen.getAllByText(composedLine("Importing 520 W")).length).toBe(2);
    });

    // A republished frame behind the adopted sequence must not rewind it.
    channel.push({ type: "snapshot", sequence: 41, data: stale });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.getAllByText(composedLine("Importing 520 W")).length).toBe(2);
  });

  it("shows the disconnected state and keeps the last known picture", async () => {
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([unit("mid", { grid_power_w: -412, load_power_w: 340, battery_watts: -1900 })]),
    );
    endingStream([{ type: "snapshot", sequence: 41, data: snapshotEnvelope([unit("mid", { grid_power_w: -412, load_power_w: 340, battery_watts: -1900 })]) }]);

    renderFlow();

    expect(await screen.findByText(composedLine(/Connection lost — showing the last known picture/i))).toBeVisible();
    expect(screen.getAllByText(composedLine("Importing 412 W")).length).toBe(2);
  });
});

// --- the pinned diagram structure (the polish round's visual pins) --------------------

describe("FlowView — the pinned diagram structure", () => {
  it("puts the Whole-site column first in DOM and reading order", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { grid_power_w: -412, load_power_w: 340, battery_watts: -1900 }),
        unit("rhs", { grid_power_w: 0, load_power_w: 120, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    await screen.findByRole("group", { name: /power flow by phase/i });
    const diagram = document.body.querySelector(".flow-diagram");
    expect(diagram).not.toBeNull();
    const columns = diagram?.querySelectorAll(":scope > .flow-phase") ?? [];
    expect(columns.length).toBe(3);
    // The glance path is story → whole site → phase detail; the fleet column
    // leads the DOM (and so the screen reader's speech) before any phase.
    expect(columns[0]?.getAttribute("aria-label")).toBe("Whole site");
    expect(columns[1]?.getAttribute("aria-label")).toMatch(/Phase mid/);
    expect(columns[2]?.getAttribute("aria-label")).toMatch(/Phase rhs/);
  });

  it("renders every idle slot as a hollow ring and every gap as a dotted stub", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("mid", { soc_pct: 100, grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
        unit("rhs", { soc_pct: 100, grid_power_w: 0, load_power_w: 0, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    await screen.findByText(/Nothing is flowing/i);
    // 3 columns × 3 idle slots (grid, battery, and a measured 0 W house) = 9
    // rings, and no ribbon or dotted stub anywhere.
    expect(document.body.querySelectorAll(".flow-idle-ring").length).toBe(9);
    expect(document.body.querySelectorAll(".flow-stub-track").length).toBe(0);
    expect(document.body.querySelectorAll(".flow-stub-unknown").length).toBe(0);
  });

  it("words an absent datum with a dotted stub — never a zero-filled ribbon", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([unit("mid", { grid_power_w: -412, load_power_w: null, battery_watts: null })]),
    );

    renderFlow();

    expect((await screen.findAllByText(composedLine("Importing 412 W"))).length).toBe(2); // mid + the fleet
    // mid's column: one ribbon (the grid import) and two dotted unknowns
    // (battery, house); the fleet column mirrors the same split.
    const columns = document.body.querySelectorAll(".flow-phase");
    expect(columns.length).toBe(2);
    expect(columns[1]?.querySelectorAll(".flow-stub-track").length).toBe(1);
    expect(columns[1]?.querySelectorAll(".flow-stub-unknown").length).toBe(2);
    expect(columns[0]?.querySelectorAll(".flow-stub-unknown").length).toBe(2);
    expect(columns[0]?.querySelectorAll(".flow-stub-track").length).toBe(1);
  });

  it("splits the fleet's both-directions slots into two half-slot stubs", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("lhs", { grid_power_w: 300, load_power_w: 100, battery_watts: 800 }),
        unit("mid", { grid_power_w: -1500, load_power_w: 200, battery_watts: -1900 }),
      ]),
    );

    renderFlow();

    await screen.findByText(/discharging 800 W while mid is charging 1,900 W/i);
    // The fleet column: the grid slot AND the battery slot both split (import
    // beside export, discharge beside charge), the house slot does not — five
    // ribbons where a single column would draw three, and each side carries
    // its own width instead of one max-width both-headed lie.
    const fleet = document.body.querySelector(".flow-phase--fleet");
    expect(fleet).not.toBeNull();
    expect(fleet?.querySelectorAll(".flow-stub-track").length).toBe(5);
  });

  it("dims a disconnected phase, dots its stubs, and keeps its last-known words", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit(
          "mid",
          { soc_pct: 45, grid_power_w: -540, load_power_w: null, battery_watts: -600 },
          { lifecycle: "disconnected" },
        ),
        unit("rhs", { soc_pct: 80, grid_power_w: -330, load_power_w: 330, battery_watts: 0 }),
      ]),
    );

    renderFlow();

    const down = await screen.findByLabelText(/Phase mid — grid: Importing 540 W/);
    expect(down.className).toContain("flow-phase--down");
    expectVisibleText(down, /No contact — these figures are the last known/);
    expectVisibleText(down, /Charging 600 W/);
    // The arrows stop claiming a live flow they cannot vouch for; the words stay.
    expect(down.querySelectorAll(".flow-stub-unknown").length).toBe(3);
    expect(down.querySelectorAll(".flow-stub-track").length).toBe(0);
    // The note anchors the column's FOOT — under the figures it vouches for —
    // so a phase that needs a word never shifts any other column's rows.
    const note = down.querySelector(".flow-phase-note");
    expect(note).not.toBeNull();
    expect(note === down.lastElementChild).toBe(true);
  });

  it("puts every arrowhead at the end the direction names (the head IS the direction)", async () => {
    liveChannel([]);
    // lhs imports and discharges; rhs exports and charges — each family's
    // both directions on screen at once.
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("lhs", { grid_power_w: -800, load_power_w: 300, battery_watts: 900 }),
        unit("rhs", { grid_power_w: 500, load_power_w: 200, battery_watts: -700 }),
      ]),
    );

    renderFlow();

    await screen.findByText(/while rhs is charging 700 W/i);
    const columns = document.body.querySelectorAll(".flow-phase");
    expect(columns.length).toBe(3);
    const stubsOf = (column: Element, tone: string): string[] =>
      [...column.querySelectorAll(`.flow-stub--${tone} .flow-stub-track`)].map(
        (line) => `${line.getAttribute("y1")}->${line.getAttribute("y2")}`,
      );
    // A stub drawn bottom→top (78→14) carries its head INTO the rail; one
    // drawn top→bottom (14→78) carries its head down to the node. Import and
    // discharge feed the phase; export, charge, and the house's draw leave it.
    expect(stubsOf(columns[1]!, "grid")).toEqual(["78->14"]); // lhs importing
    expect(stubsOf(columns[1]!, "battery")).toEqual(["78->14"]); // lhs discharging
    expect(stubsOf(columns[1]!, "home")).toEqual(["14->78"]);
    expect(stubsOf(columns[2]!, "grid")).toEqual(["14->78"]); // rhs exporting
    expect(stubsOf(columns[2]!, "battery")).toEqual(["14->78"]); // rhs charging
    // Every active track wears exactly one head (markerEnd), on its drawn end.
    for (const line of document.body.querySelectorAll(".flow-stub-track")) {
      expect(line.getAttribute("marker-end")).toMatch(/^url\(#flow-head-(sm|lg)-/);
      expect(line.getAttribute("marker-start")).toBeNull();
    }
  });

  it("edges every ribbon with its family's own deepened hue (the gold stays gold)", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("lhs", { soc_pct: 40, grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
      ]),
    );

    renderFlow();

    await screen.findByText(/All the batteries are charging 1,900 W/i);
    // Every track carries a rim beneath it: the same hue deepened, full
    // opacity, 2.6 units wider — the darker edge that keeps a thin battery
    // ribbon reading gold on white instead of beige (round 2's finding 1).
    const tracks = document.body.querySelectorAll(".flow-stub-track");
    expect(tracks.length).toBeGreaterThan(0);
    for (const track of tracks) {
      const rim = track.previousElementSibling;
      expect(rim?.classList.contains("flow-stub-rim")).toBe(true);
      const trackWidth = Number((track as SVGElement).style.strokeWidth);
      const rimWidth = Number((rim as SVGElement).style.strokeWidth);
      expect(rimWidth).toBeCloseTo(trackWidth + 2.6, 5);
    }
  });

  it("reserves the SoC band in every node card and meters the battery's reading", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit("lhs", { soc_pct: 40, grid_power_w: -2000, load_power_w: 270, battery_watts: -1900 }),
      ]),
    );

    renderFlow();

    await screen.findByText(/All the batteries are charging 1,900 W/i);
    // Every card — Grid and Home included — carries exactly one SoC band, so
    // the same rows land at the same y across all four columns; only the
    // battery's carries the reading, worded and metered.
    const nodes = document.body.querySelectorAll(".flow-node");
    expect(nodes.length).toBe(6); // the fleet's three + the phase's three
    for (const node of nodes) {
      const bands = node.querySelectorAll(":scope > .flow-node-extra");
      expect(bands.length).toBe(1);
    }
    const reserved = document.body.querySelectorAll(".flow-node-extra--reserved");
    // Grid + Home in both columns (the fleet's battery has no summed SoC, so
    // its band is reserved too): 5 reserved, 1 worded.
    expect(reserved.length).toBe(5);
    const socBand = document.body.querySelector(".flow-node-extra--soc");
    expect(socBand).not.toBeNull();
    expect(socBand?.textContent).toBe("40% charged");
    const fill = socBand?.querySelector<HTMLElement>(".flow-soc-fill");
    expect(fill?.style.width).toBe("40%");
  });

  it("pulses the battery ring of a phase commanded but not yet moving", async () => {
    liveChannel([]);
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        unit(
          "mid",
          { soc_pct: 45, grid_power_w: -340, load_power_w: 340, battery_watts: 0 },
          {
            lifecycle: "active",
            requested_power: { direction: "charge", watts: 2000 },
            authorized_power: { direction: "charge", watts: 2000 },
          },
        ),
      ]),
    );

    renderFlow();

    expect(await screen.findByText(composedLine("mid — commanded 2,000 W charge, not moving yet"))).toBeVisible();
    // No gold flow is drawn until watts are measured — the battery's idle ring
    // pulses (mid's and the fleet's are both idle; only the commanded phase's
    // pulses), and it is the battery's ring, not any other slot's.
    expect(document.body.querySelectorAll(".flow-idle-ring").length).toBe(2);
    const pulsing = document.body.querySelectorAll(".flow-idle-ring--pulse");
    expect(pulsing.length).toBe(1);
    expect(pulsing[0]?.closest("g")?.classList.contains("flow-stub--battery")).toBe(true);
  });
});

/** A visible line composed of styled runs — the figure (its word and its
 * figure are sibling spans inside one part), a command row (its unit id is
 * its own anchor run), the connection line (its status word leads) — matched
 * leaf-most: the composing element itself, never an ancestor that merely
 * contains it. */
function composedLine(match: string | RegExp): (_: string, element: Element | null) => boolean {
  const normalize = (value: string | null): string => (value ?? "").replace(/\s+/g, " ").trim();
  const fits = (value: string): boolean =>
    typeof match === "string" ? value === match : match.test(value);
  return (_: string, element: Element | null): boolean => {
    if (element === null) return false;
    if (!fits(normalize(element.textContent))) return false;
    return !Array.from(element.children).some((child) => fits(normalize(child.textContent)));
  };
}

// --- helpers ---------------------------------------------------------------------------

/** A visible text match inside a scope (the wording itself, not an ancestor). */
function expectVisibleText(scope: HTMLElement, pattern: RegExp): void {
  const matches = within(scope)
    .getAllByText((_: string, element: Element | null) => pattern.test(element?.textContent ?? ""))
    .map((element: HTMLElement) => element.textContent ?? "");
  if (matches.length === 0) {
    throw new Error(`expected visible text matching ${pattern} within the scope`);
  }
  expect(matches.length).toBeGreaterThan(0);
}
