/**
 * Behavior contract for Home's parked fleet banner (DESIGN_POD_PARKING §8):
 * the household-facing fact that a battery is standing by under a lease —
 * named per pod with its countdown, the fixed not-isolation sentence always
 * present, the alert promotion at expiry, and the honest nothing while no
 * pod is parked or the parking projection does not speak (the
 * not-commissioned feature detection).
 *
 * The suite mocks the API client module at its exact surface; fixtures come
 * from web/src/test/wire.ts (the PENDING park family's own pinned shapes).
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createApiClient,
  type ApiClient,
  type Health,
  type StreamEvent,
} from "../../api/client";
import {
  parkState,
  snapshot,
  snapshotFrame,
  unitSnapshot,
  withParkState,
  type WireParkState,
  type WireSnapshot,
} from "../../test/wire";
import { HomeView } from "./HomeView";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: vi.fn(),
}));

const HEALTHY: Health = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: false, reasons: ["no_unit_armed"] },
};

/** 20 s of slack so a floored H:MM figure is stable across render delay. */
const RENDER_SLACK_S = 20;

function liveLease(secondsLeft: number): WireParkState {
  return parkState({
    lease_expires_at: new Date(Date.now() + secondsLeft * 1000).toISOString(),
  });
}

function world(rhsPark: WireParkState | null): WireSnapshot {
  const rhs = unitSnapshot({
    unit_id: "rhs",
    lifecycle: "disarmed",
    telemetry_age_s: 2,
  });
  return snapshot(
    [
      unitSnapshot({ unit_id: "lhs", lifecycle: "disarmed", telemetry_age_s: 3 }),
      rhsPark === null ? rhs : withParkState(rhs, rhsPark),
    ],
    { snapshot_sequence: 4100 },
  );
}

function liveStream(state: WireSnapshot): AsyncIterable<StreamEvent> {
  return {
    async *[Symbol.asyncIterator]() {
      yield snapshotFrame(state);
      await new Promise<never>(() => {});
    },
  };
}

function installClient(state: WireSnapshot): ApiClient {
  const client = {
    getSnapshot: vi.fn(() => Promise.resolve(state)),
    getHealth: vi.fn(() => Promise.resolve(HEALTHY)),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    getSchedule: vi.fn(() => Promise.reject(new Error("not used"))),
    openEvents: vi.fn(() => liveStream(state)),
  };
  const typed = client as unknown as ApiClient;
  vi.mocked(createApiClient).mockReturnValue(typed);
  return typed;
}

function renderHome(state: WireSnapshot): void {
  render(<HomeView client={installClient(state)} />);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("HomeView — the parked fleet banner", () => {
  it("renders while any pod is parked: the pod named, its countdown, the fixed sentence", async () => {
    renderHome(world(liveLease(2 * 3600 + 5 * 60 + RENDER_SLACK_S)));

    const banner = await screen.findByRole("region", { name: "Parked batteries" });
    expect(banner.textContent).toContain("Parked — not isolation: rhs (lease expires in 2:05)");
    expect(banner.textContent).toContain("A parked battery stands by at zero watts");
    // The fixed sentence rides the banner verbatim.
    expect(banner.textContent).toContain(
      "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod.",
    );
  });

  it("promotes to the alert family the moment a lease reads expired", async () => {
    renderHome(world(parkState({ expired: true })));

    const banner = await screen.findByRole("region", { name: "Parked batteries" });
    expect(banner.textContent).toContain("rhs — lease expired, Resume required");
    expect(banner.className).toContain("home-parked--expired");
  });

  it("names every parked pod, not just the first", async () => {
    const state = snapshot(
      [
        withParkState(
          unitSnapshot({ unit_id: "lhs", lifecycle: "disarmed", telemetry_age_s: 3 }),
          liveLease(3600 + RENDER_SLACK_S),
        ),
        withParkState(
          unitSnapshot({ unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 2 }),
          parkState({ expired: true }),
        ),
      ],
      { snapshot_sequence: 4101 },
    );
    renderHome(state);

    const banner = await screen.findByRole("region", { name: "Parked batteries" });
    expect(banner.textContent).toContain("lhs (lease expires in 1:00)");
    expect(banner.textContent).toContain("rhs — lease expired, Resume required");
    expect(banner.className).toContain("home-parked--expired");
  });

  it("renders nothing at all while no pod is parked, or the projection is absent", async () => {
    renderHome(world(parkState({ parked: false })));
    // Wait for the view to settle, then the banner must be absent.
    await screen.findByText("Are we safe and connected?");
    expect(screen.queryByRole("region", { name: "Parked batteries" })).toBeNull();
  });

  it("renders nothing while the parking projection is absent (not commissioned)", async () => {
    renderHome(world(null));
    await screen.findByText("Are we safe and connected?");
    expect(screen.queryByRole("region", { name: "Parked batteries" })).toBeNull();
  });
});
