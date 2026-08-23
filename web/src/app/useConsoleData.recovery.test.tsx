/**
 * Behavior contract for the shell's consumption of the self-healing
 * awareness layer's bus events (API_CONTRACTS.md "Self-healing awareness
 * layer (recovery detection)"; the layer is live on the controller since
 * b20058d..7b491b3):
 *
 * - `unit.health_changed` moves the unit's derived health state-locally —
 *   the badge updates WITHOUT waiting for a poll and WITHOUT a snapshot
 *   refetch (the periodic snapshot remains the reconciler, so a chattery
 *   classifier can never start a read storm).
 * - `actuation.incoherent` produces exactly ONE polite-region announcement
 *   per EPISODE (the backend publishes the detection once and one
 *   echo-classified follow-up of the same type; the shell's own bookkeeping
 *   is robust against a replayed frame), names the echo discriminator
 *   plainly when it classifies, and moves the badge state now.
 * - `unit.unexpected_autonomy` is QUIET-TIER EVIDENCE: no polite line, no
 *   assertive line, no badge — the Activity timeline owns its rendering.
 *
 * These run through the same Probe harness as useConsoleData.test.tsx (one
 * plane per session, mocked client at its exact surface); the badge's own
 * per-state rendering is pinned in unitHealth.test.tsx, the views' adoption
 * in their own suites.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import {
  actuationIncoherent,
  unitHealthChanged,
  unitUnexpectedAutonomy,
} from "../test/wire";
import { SharedDataPlane } from "./SharedDataPlane";
import { useConsoleData } from "./useConsoleData";

const SNAPSHOT: Snapshot = {
  site_id: "site-1",
  snapshot_sequence: 41,
  captured_at: "2026-08-22T10:00:00Z",
  units: [
    {
      // mid today: healthy, its oscillation producing only quiet evidence.
      unit_id: "mid",
      lifecycle: "active",
      telemetry_age_s: 2,
      quality: "good",
      requested_power: { direction: "IDLE", watts: 0 },
      authorized_power: null,
      measured_watts: 0,
    },
    {
      // rhs today: quietly self-healing (solar self-charge).
      unit_id: "rhs",
      lifecycle: "disarmed",
      telemetry_age_s: 3,
      quality: "good",
      requested_power: { direction: "IDLE", watts: 0 },
      authorized_power: null,
      measured_watts: -260,
      health_state: "self_healing",
      health_reasons: ["autonomous_self_charge"],
      remediation_hint: null,
    },
  ],
};

/** The connection-time snapshot frame, then the given frames, then quiet. */
function streamOf(frames: readonly StreamEvent[]): () => AsyncGenerator<StreamEvent, void, unknown> {
  return () =>
    (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
      yield { type: "snapshot", sequence: SNAPSHOT.snapshot_sequence, data: SNAPSHOT };
      for (const frame of frames) {
        yield frame;
      }
      await new Promise(() => undefined);
    })();
}

function mockClient(openEvents: () => AsyncGenerator<StreamEvent, void, unknown>): ApiClient {
  const refused = new Error("not used by this test");
  return {
    getSnapshot: vi.fn(() => Promise.resolve(SNAPSHOT)),
    getHealth: vi.fn(() =>
      Promise.resolve({
        liveness: { ok: true },
        service_readiness: { ready: true, reasons: [] },
        control_readiness: { ready: true, reasons: [] },
      }),
    ),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    postIntent: vi.fn(() => Promise.reject(refused)),
    postArm: vi.fn(() => Promise.reject(refused)),
    postDisarm: vi.fn(() => Promise.reject(refused)),
    postEmergencyStop: vi.fn(() => Promise.reject(refused)),
    postStopAcknowledgement: vi.fn(() => Promise.reject(refused)),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(refused)),
    openEvents: vi.fn(openEvents),
  } as unknown as ApiClient;
}

function Probe({ client }: { client: ApiClient }): React.ReactElement {
  const [plane] = useState(() => new SharedDataPlane(client));
  const data = useConsoleData(plane, vi.fn(), {
    retryDelaysMs: [60_000],
    // The live-cadence poll is disabled: any snapshot read counted below is
    // an event-driven refetch, which is exactly what these pins forbid.
    livePollMs: 600_000,
  });
  const units = data.snapshot?.units ?? [];
  const rhsHealth = units.find((unit) => unit.unitId === "rhs")?.health ?? null;
  const midHealth = units.find((unit) => unit.unitId === "mid")?.health ?? null;
  return (
    <div>
      <ul data-testid="polite" aria-label="Announcements">
        {data.polite.map((text, index) => (
          <li key={index}>{text}</li>
        ))}
      </ul>
      <output data-testid="assertive">{data.assertive.join(" | ") || "none"}</output>
      <output data-testid="rhs-health">
        {rhsHealth === null ? "absent" : `${rhsHealth.state}[${rhsHealth.reasons.join(",")}]`}
      </output>
      <output data-testid="mid-health">
        {midHealth === null ? "absent" : `${midHealth.state}[${midHealth.reasons.join(",")}]`}
      </output>
    </div>
  );
}

/** The polite region's current lines (the announcements the operator hears). */
function politeTexts(): string[] {
  return Array.from(screen.getByTestId("polite").querySelectorAll("li")).map(
    (line) => line.textContent ?? "",
  );
}

describe("useConsoleData — the awareness layer's events", () => {
  it("adopts the snapshot's per-unit health fields (rhs self_healing rides the cold picture)", async () => {
    render(<Probe client={mockClient(streamOf([]))} />);
    await waitFor(() => {
      expect(screen.getByTestId("rhs-health").textContent).toBe(
        "self_healing[autonomous_self_charge]",
      );
    });
    // mid's snapshot carries no health fields: absent, never "healthy".
    expect(screen.getByTestId("mid-health").textContent).toBe("absent");
  });

  it("applies a live health_changed transition without waiting for a poll and without a refetch", async () => {
    const client = mockClient(
      streamOf([
        unitHealthChanged(42, { unit_id: "mid", from: "healthy", to: "self_healing", reasons: ["autonomous_self_charge"] }),
      ]),
    );
    render(<Probe client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("mid-health").textContent).toBe(
        "self_healing[autonomous_self_charge]",
      );
    });
    // The state moved from the frame alone: exactly the mount-time snapshot
    // reads the plane made (its coalesced snapshot() + health()), and the
    // event triggered no authority refetch of its own.
    const reads = (client.getSnapshot as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(reads).toBeLessThanOrEqual(1);
  });

  it("announces the incoherence alarm exactly once per episode, then names the discriminator once", async () => {
    render(
      <Probe
        client={mockClient(
          streamOf([
            // The episode's opening detection frame...
            actuationIncoherent(42, { unit_id: "mid", authorized_watts: 1000 }),
            // ...a replayed opener (a resumed cursor can redeliver it)...
            actuationIncoherent(43, { unit_id: "mid", authorized_watts: 1000 }),
            // ...and the echo-classified follow-up frame of the same episode.
            actuationIncoherent(44, {
              unit_id: "mid",
              authorized_watts: 1000,
              echo: { classification: "echo_matches_write", served_active_w: 1000 },
            }),
            actuationIncoherent(45, {
              unit_id: "mid",
              authorized_watts: 1000,
              echo: { classification: "echo_matches_write", served_active_w: 1000 },
            }),
          ]),
        )}
      />,
    );

    await waitFor(() => {
      expect(politeTexts().filter((line) => line.includes("was commanded 1,000 W"))).toHaveLength(1);
    });
    // The badge state moved from the detection frame, with the backend's own
    // reason vocabulary.
    await waitFor(() => {
      expect(screen.getByTestId("mid-health").textContent).toBe(
        "actuation_incoherent[authorized_not_actuating,echo_matches_write]",
      );
    });
    // The discriminator is named exactly once — politely, never assertively.
    expect(politeTexts().filter((line) => line.includes("confirms receiving our command"))).toHaveLength(1);
    expect(politeTexts().filter((line) => line.includes("check the battery"))).toHaveLength(1);
    expect(screen.getByTestId("assertive").textContent).toBe("none");
  });

  it("re-arms the announcement once the unit leaves the incoherent state (a NEW episode speaks again)", async () => {
    render(
      <Probe
        client={mockClient(
          streamOf([
            actuationIncoherent(42, { unit_id: "mid", authorized_watts: 1000 }),
            // The episode ends (a coherent cycle or the authorization ending).
            unitHealthChanged(43, { unit_id: "mid", from: "actuation_incoherent", to: "healthy", reasons: [] }),
            // A later, genuinely new episode.
            actuationIncoherent(44, { unit_id: "mid", authorized_watts: 800 }),
          ]),
        )}
      />,
    );
    await waitFor(() => {
      expect(politeTexts().filter((line) => line.includes("check the battery"))).toHaveLength(2);
    });
    // And the healthy transition took the badge back down with it.
    await waitFor(() => {
      expect(screen.getByTestId("mid-health").textContent).toBe(
        "actuation_incoherent[authorized_not_actuating]",
      );
    });
  });

  it("keeps unexpected_autonomy QUIET: no polite line, no assertive line, no state change", async () => {
    render(
      <Probe
        client={mockClient(
          streamOf([
            unitUnexpectedAutonomy(42, { unit_id: "mid", measured_watts: 1411.2 }),
            unitUnexpectedAutonomy(43, { unit_id: "mid", measured_watts: -1204.5 }),
          ]),
        )}
      />,
    );
    // The stream connected and its frames were consumed (the connect-time
    // greeting may sit in the region) before the quietness is judged.
    await waitFor(() => {
      expect(screen.getByTestId("mid-health").textContent).toBe("absent");
    });
    // The evidence frames exist only for the Activity timeline's quiet entry:
    // the shell announces nothing beyond the connect-time greeting, asserts
    // nothing, and invents no state.
    const lines = politeTexts().filter(
      (line) => line !== "" && !line.includes("Fleet picture loaded"),
    );
    expect(lines).toHaveLength(0);
    expect(screen.getByTestId("assertive").textContent).toBe("none");
    expect(screen.getByTestId("mid-health").textContent).toBe("absent");
  });
});
