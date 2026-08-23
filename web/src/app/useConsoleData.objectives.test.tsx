/**
 * Behavior contract for the shell's consumption of the night-writer
 * detector's bus event (API_CONTRACTS.md "Night-writer detector"):
 * `foreign_objective.observed` is the ALERT tier — the detector's own
 * evidence class, NOT a console shout. The pinned shell behavior:
 *
 * - The unit's `last_objective_observed` summary moves NOW, state-locally,
 *   from the frame — so the quiet per-unit line (Batteries cards, Home unit
 *   entries) follows the alert the moment it lands, with NO snapshot refetch
 *   (the periodic snapshot read carries the detector's own summary once
 *   composed and is the reconciler).
 * - NO polite announcement, NO assertive announcement: the pinned surfaces
 *   are the quiet line and the Activity timeline's entry. Alert tier names
 *   the backend's evidence class, never a live-region volume.
 * - QUIET-TIER EVIDENCE IS NEVER PUBLISHED (the contract's own pin): in-band
 *   pod-autonomy and handback-grace samples arrive ONLY through the snapshot
 *   summary and the Objectives view — there is no quiet event for the shell
 *   to mishandle, and the snapshot-side quiet classifications change no
 *   announcement either.
 *
 * These run through the same Probe harness as the awareness-layer suite: one
 * plane per session, the live-cadence poll disabled so any counted snapshot
 * read is an event-driven refetch — exactly what the pins forbid.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import {
  foreignObjectiveObserved,
  lastObjectiveObserved,
  withObjective,
} from "../test/wire";
import { SharedDataPlane } from "./SharedDataPlane";
import { useConsoleData } from "./useConsoleData";

const SNAPSHOT: Snapshot = {
  site_id: "site-1",
  snapshot_sequence: 41,
  captured_at: "2026-08-22T10:00:00Z",
  units: [
    {
      unit_id: "mid",
      lifecycle: "disarmed",
      telemetry_age_s: 2,
      quality: "good",
      requested_power: { direction: "IDLE", watts: 0 },
      authorized_power: null,
      measured_watts: 0,
    },
  ],
};

/** The connection-time snapshot frame, then the given frames, then quiet. */
function streamOf(
  frames: readonly StreamEvent[],
  connectSnapshot: Snapshot = SNAPSHOT,
): () => AsyncGenerator<StreamEvent, void, unknown> {
  return () =>
    (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
      yield { type: "snapshot", sequence: connectSnapshot.snapshot_sequence, data: connectSnapshot };
      for (const frame of frames) {
        yield frame;
      }
      await new Promise(() => undefined);
    })();
}

function mockClient(
  openEvents: () => AsyncGenerator<StreamEvent, void, unknown>,
  snapshot: Snapshot = SNAPSHOT,
): ApiClient {
  const refused = new Error("not used by this test");
  return {
    getSnapshot: vi.fn(() => Promise.resolve(snapshot)),
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
  const mid = data.snapshot?.units.find((unit) => unit.unitId === "mid") ?? null;
  const objective = mid?.objective ?? null;
  return (
    <div>
      <ul data-testid="polite" aria-label="Announcements">
        {data.polite.map((text, index) => (
          <li key={index}>{text}</li>
        ))}
      </ul>
      <output data-testid="assertive">{data.assertive.join(" | ") || "none"}</output>
      <output data-testid="mid-objective">
        {objective === null
          ? "absent"
          : `${objective.classification}|${objective.activeW ?? "null"}|${objective.reason ?? "null"}`}
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

describe("useConsoleData — the night-writer detector's alert event", () => {
  it("moves the unit's summary NOW from the frame, with no refetch and no announcement", async () => {
    const client = mockClient(
      streamOf([
        foreignObjectiveObserved(42, {
          unit_id: "mid",
          observed_at: "2026-08-23T23:40:00+10:00",
          active_w: -2400,
          reason: "sustained_charge_without_pv_evidence",
        }),
      ]),
    );
    render(<Probe client={client} />);
    // The alert landed: the summary is the frame's own evidence, classified
    // foreign by the EVENT TYPE (the detector's assertion), never by the
    // payload's optional copy of the word.
    await waitFor(() => {
      expect(screen.getByTestId("mid-objective").textContent).toBe(
        "foreign_objective_observed|-2400|sustained_charge_without_pv_evidence",
      );
    });
    // QUIET presentation is the pin: the polite region holds only the
    // connect-time greeting, the assertive region nothing.
    const lines = politeTexts().filter(
      (line) => line !== "" && !line.includes("Fleet picture loaded"),
    );
    expect(lines).toHaveLength(0);
    expect(screen.getByTestId("assertive").textContent).toBe("none");
    // No event-driven refetch: the connection-time read is the only one.
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(client.getSnapshot).toHaveBeenCalledTimes(1);
  });

  it("ignores an unusable alert frame (no unit id) — nothing moves, nothing announces", async () => {
    render(
      <Probe
        client={mockClient(
          streamOf([
            {
              type: "foreign_objective.observed",
              sequence: 42,
              occurred_at: "2026-08-23T23:40:12Z",
              payload: { active_w: -2400, reason: "outside_autonomy_band" },
            },
          ]),
        )}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("mid-objective").textContent).toBe("absent");
    });
    const lines = politeTexts().filter(
      (line) => line !== "" && !line.includes("Fleet picture loaded"),
    );
    expect(lines).toHaveLength(0);
    expect(screen.getByTestId("assertive").textContent).toBe("none");
  });

  it("adopts the snapshot's own summary verbatim — quiet classifications included, silently", async () => {
    // The connect-time picture already carries an in-band pod-autonomy sample
    // (the quiet tier's ONLY wire path — it is never published as an event):
    // it adopts onto the unit model and announces nothing.
    const quietSnapshot: Snapshot = {
      ...SNAPSHOT,
      units: [
        withObjective(
          SNAPSHOT.units[0] as never,
          lastObjectiveObserved({
            observed_at: "2026-08-23T17:52:00+10:00",
            active_w: -540,
            classification: "pod_autonomy_objective_observed",
            reason: null,
          }),
        ),
      ],
    } as unknown as Snapshot;
    render(<Probe client={mockClient(streamOf([], quietSnapshot), quietSnapshot)} />);
    await waitFor(() => {
      expect(screen.getByTestId("mid-objective").textContent).toBe(
        "pod_autonomy_objective_observed|-540|null",
      );
    });
    const lines = politeTexts().filter(
      (line) => line !== "" && !line.includes("Fleet picture loaded"),
    );
    expect(lines).toHaveLength(0);
    expect(screen.getByTestId("assertive").textContent).toBe("none");
  });
});
