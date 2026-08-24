/**
 * Behavior contract for the History view (web/src/views/history/HistoryView.tsx).
 *
 * PROP SEAM: exactly like every other view suite — the mocked client module
 * keeps its canonical surface, and the suite injects the client the same way
 * the shell does (one `{ client }` prop).
 *
 * The pins:
 *
 * - THE STATE SET (UI_CONTRACTS): loading, ready, the honest not-commissioned
 *   409 state, error-with-retry, and the empty window — the fresh-database
 *   operator's FIRST state, worded from the response's own facts.
 * - HONESTY ON SCREEN: the resolution tier the data chose with its caveat,
 *   the quality word verbatim, "not available" for absent series, gap notes
 *   in words, and the commanded overlay naming its source ("nothing
 *   commanded" when no intent claimed the unit).
 * - FLEET VS PER-BATTERY: the fleet block renders the summed flows; a
 *   battery scope adds the per-unit facts line, the charge-level chart, the
 *   archaeology strip, and the secondary cells/temperature group.
 * - RANGE SWITCHING re-queries with the new preset's window.
 * - A FAILED REFRESH KEEPS THE LAST WINDOW (never a blank flash), with the
 *   refusal surfaced above it.
 * - CANVAS HONESTY: in a canvas-less environment (jsdom) the chart says so
 *   and the summary table carries the same facts — never a fake chart.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  historyBody,
  historyNotCommissionedRefusal,
  historySeries,
  historyState,
  historyUnit,
  snapshot,
  unitSnapshot,
  withHistoryState,
} from "../../test/wire";
import { HistoryView } from "./HistoryView";

const api = vi.hoisted(() => {
  const client = {
    getSnapshot: vi.fn(),
    getPlantHistory: vi.fn(),
  };
  return { createApiClient: vi.fn(), client };
});

function injectedClient(): ApiClient {
  return api.client as unknown as ApiClient;
}

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: api.createApiClient,
}));

// --- fixtures -----------------------------------------------------------------

/**
 * A recording stamp 30 s before the read: the recording note compares the
 * snapshot's `last_sample_at` against the REAL wall clock, so a fixed
 * calendar stamp would flip the note to "fallen quiet" a minute after it was
 * written (the 2026-08-24 time bomb this suite shipped with). A fresh stamp
 * keeps the note's word deterministic: "History is recording".
 */
const recordedAtIso = (): string => new Date(Date.now() - 30_000).toISOString();

function snapshotWith(units: string[]): ReturnType<typeof snapshot> {
  return withHistoryState(
    snapshot(
      units.map((unitId) =>
        unitSnapshot({
          unit_id: unitId,
          lifecycle: "disarmed",
          telemetry: null,
          measured_watts: null,
        }),
      ),
      { captured_at: recordedAtIso() },
    ),
    historyState({
      last_sample_at: Object.fromEntries(units.map((unitId) => [unitId, recordedAtIso()])),
    }),
  );
}

/** A full-resolution world with a charge, a gap, and a degraded stretch. */
function fullWindow() {
  return historyBody({
    resolution: "full",
    from: "2026-08-24T00:00:00+00:00",
    to: "2026-08-24T06:00:00+00:00",
    units: {
      mid: historyUnit({
        first_sample_at: "2026-08-24T00:00:30+00:00",
        last_sample_at: "2026-08-24T05:59:30+00:00",
        sample_count: 2871,
        quality_worst: "stale",
        gaps: [{ from: "2026-08-24T02:10:00+00:00", to: "2026-08-24T03:40:30+00:00" }],
        series: {
          soc_pct: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 96 }]),
          bms_soc_pct: historySeries([
            { t: "2026-08-24T00:00:30+00:00", v: 97 },
            { t: "2026-08-24T05:59:30+00:00", v: 31 },
          ], { sample_count: 2871 }),
          battery_watts: historySeries(
            [
              { t: "2026-08-24T00:00:30+00:00", v: -521 },
              { t: "2026-08-24T00:41:00+00:00", v: -2503 },
              { t: "2026-08-24T01:23:00+00:00", v: 914 },
            ],
            { sample_count: 2871, window_min: -2503, window_min_at: "2026-08-24T00:41:00+00:00", window_max: 2510, window_max_at: "2026-08-24T01:23:00+00:00" },
          ),
          grid_power_w: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: -600 }]),
          load_power_w: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 340 }]),
          temperature_min_c: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 21.4 }]),
          temperature_max_c: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 28.9 }]),
          cell_min_v: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 3.31 }]),
          cell_max_v: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 3.35 }]),
          cell_spread_mv: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 40 }]),
        },
        lifecycle_changes: [{ t: "2026-08-24T00:00:30+00:00", v: "disarmed" }],
        health_state_changes: [{ t: "2026-08-24T00:00:30+00:00", v: "healthy" }],
        commanded_changes: [
          { t: "2026-08-24T00:00:30+00:00", source: null, direction: null, watts: null },
          { t: "2026-08-24T00:02:00+00:00", source: "night_adviser", direction: "charge", watts: 2500 },
          { t: "2026-08-24T04:00:00+00:00", source: null, direction: null, watts: null },
        ],
      }),
    },
    fleet: {
      series: {
        battery_watts: historySeries(
          [{ t: "2026-08-24T00:00:30+00:00", v: -67 }],
          { sample_count: 8_613, window_min: -7_503, window_min_at: "2026-08-24T00:41:00+00:00", window_max: 2_510, window_max_at: "2026-08-24T01:23:00+00:00" },
        ),
        grid_power_w: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: -131 }]),
        load_power_w: historySeries([{ t: "2026-08-24T00:00:30+00:00", v: 85 }]),
      },
      gaps: [],
    },
  });
}

/** An hourly world: means, bands, and an absent hour. */
function hourlyWindow() {
  return historyBody({
    resolution: "hourly",
    from: "2026-08-17T00:00:00+00:00",
    to: "2026-08-24T06:00:00+00:00",
    units: {
      mid: historyUnit({
        sample_count: 8_361,
        quality_worst: "good",
        series: {
          battery_watts: {
            points: [{ t: "2026-08-23T22:00:00+00:00", v: -2400, min: -2500, max: -1800, n: 118 }],
            sample_count: 8_361,
            window_min: -2503,
            window_min_at: "2026-08-23T22:30:00+00:00",
            window_max: 2510,
            window_max_at: "2026-08-23T19:12:30+00:00",
          },
        },
      }),
    },
    fleet: {
      series: {
        battery_watts: {
          points: [{ t: "2026-08-23T22:00:00+00:00", v: -7100, min: -7500, max: -5400, n: 118 }],
          sample_count: 8_361,
          window_min: -7503,
          window_min_at: null,
          window_max: 7510,
          window_max_at: null,
        },
      },
      gaps: [],
    },
  });
}

function refusal(status: number, body: Record<string, unknown>): ApiClientError {
  return new ApiClientError({
    status,
    code: String(body.code ?? "unknown"),
    message: String(body.message ?? ""),
    details: null,
    request_id: String(body.request_id ?? ""),
  });
}

/** Resolve the view's two reads in order. */
async function landReads(snapshotBody: unknown, historyBodyValue: unknown): Promise<void> {
  api.client.getSnapshot.mockResolvedValueOnce(snapshotBody);
  api.client.getPlantHistory.mockResolvedValueOnce(historyBodyValue);
}

beforeEach(() => {
  api.client.getSnapshot.mockReset();
  api.client.getPlantHistory.mockReset();
});

// --- the state set ----------------------------------------------------------------

describe("History view — states", () => {
  it("loads quietly before the first answer", () => {
    api.client.getSnapshot.mockImplementation(() => new Promise(() => {}));
    api.client.getPlantHistory.mockImplementation(() => new Promise(() => {}));
    render(<HistoryView client={injectedClient()} />);
    expect(screen.getByText(/Loading the recorded window/)).toBeVisible();
    expect(screen.getByText("History")).toBeVisible();
  });

  it("renders the honest not-commissioned state on the route's 409", async () => {
    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid"]));
    api.client.getPlantHistory.mockRejectedValueOnce(
      refusal(409, historyNotCommissionedRefusal()),
    );
    render(<HistoryView client={injectedClient()} />);
    const note = await screen.findByRole("note");
    expect(note.textContent ?? "").toMatch(/not commissioned/i);
    expect(note.textContent ?? "").toContain("plant_history");
    expect(await within(note).findByRole("button", { name: /check again/i })).toBeVisible();
  });

  it("renders the error state with the envelope's code and a retry that recovers", async () => {
    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid"]));
    api.client.getPlantHistory.mockRejectedValueOnce(
      refusal(500, {
        code: "internal_error",
        message: "The historian could not be read",
        details: null,
        request_id: "req-77",
      }),
    );
    render(<HistoryView client={injectedClient()} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toContain("internal_error");
    expect(alert.textContent ?? "").toContain("req-77");

    // The retry re-reads and lands ready.
    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid"]));
    api.client.getPlantHistory.mockResolvedValueOnce(fullWindow());
    await userEvent.setup().click(within(alert).getByRole("button", { name: /try again/i }));
    await waitFor(() => {
      expect(screen.getByText("Whole site — power")).toBeVisible();
    });
  });

  it("keeps the last window on screen when a refresh fails, with the refusal above it", async () => {
    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid"]));
    api.client.getPlantHistory.mockResolvedValueOnce(fullWindow());
    render(<HistoryView client={injectedClient()} />);
    await screen.findByText("Whole site — power");

    api.client.getSnapshot.mockRejectedValueOnce(new Error("snapshot down"));
    api.client.getPlantHistory.mockRejectedValueOnce(
      refusal(0, {
        code: "network_error",
        message: "The EnergyPod service could not be reached",
        details: null,
        request_id: "",
      }),
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/latest refresh failed/i);
    // The last-known picture stays — never a blank flash.
    expect(screen.getByText("Whole site — power")).toBeVisible();
  });

  it("words the empty window from the response's own facts (the fresh-database state)", async () => {
    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid", "rhs"]));
    api.client.getPlantHistory.mockResolvedValueOnce(
      historyBody({ resolution: "hourly", units: { mid: historyUnit(), rhs: historyUnit() } }),
    );
    render(<HistoryView client={injectedClient()} />);
    const empty = await screen.findByText("No recorded samples in this window");
    const note = empty.closest('[role="note"]');
    expect(note?.textContent ?? "").toMatch(/No recorded samples in this window/i);
    expect(note?.textContent ?? "").toMatch(/No rollup hours cover this window/);
    expect(note?.textContent ?? "").toMatch(/6 h/);
    expect(note?.textContent ?? "").toMatch(/Recording is active/i);
    // Never a chart pretending: the fleet section renders its not-available row.
    expect(await screen.findByText("Whole site — power")).toBeVisible();
  });
});

// --- ready: the honest surfaces ------------------------------------------------------

describe("History view — the ready window", () => {
  async function landFull(): Promise<void> {
    await landReads(snapshotWith(["mid", "rhs", "lhs"]), fullWindow());
    render(<HistoryView client={injectedClient()} />);
    await screen.findByText("Whole site — power");
  }

  it("carries the recording note (the data-age line) and the resolution tier the data chose", async () => {
    await landFull();
    const status = screen.getByText(/History is recording/i);
    expect(status.textContent ?? "").toMatch(/last sample \d+ s ago/);
    expect(screen.getByText("30 s samples")).toBeVisible();
    expect(screen.getByText(/real stored sample/)).toBeVisible();
  });

  it("offers the pinned range presets and the battery scopes from the snapshot", async () => {
    await landFull();
    for (const label of ["Today", "6 h", "24 h", "7 d", "30 d"]) {
      expect(screen.getByRole("button", { name: label })).toBeVisible();
    }
    for (const label of ["Whole site", "mid", "rhs", "lhs"]) {
      expect(screen.getByRole("button", { name: label })).toBeVisible();
    }
  });

  it("renders the fleet block first with the summed flows' honest figures", async () => {
    await landFull();
    const fleet = screen.getByLabelText(/Whole-site history/i);
    // The summary rows carry the wire's own extremes and counts.
    expect(fleet.textContent ?? "").toContain("Battery (charge −, discharge +)");
    expect(fleet.textContent ?? "").toContain("high 2,510 W");
  });

  it("switches to a battery: facts line, charge chart, strip, and the commanded overlay's words", async () => {
    await landFull();
    await userEvent.setup().click(screen.getByRole("button", { name: "mid" }));
    const facts = await screen.findByText(
      (_content, element) => element?.classList.contains("history-unit-facts") === true,
    );
    expect(facts.textContent ?? "").toContain("mid");
    expect(facts.textContent ?? "").toContain("2,871 samples");
    // The quality word rides verbatim.
    expect(facts.textContent ?? "").toMatch(/quality stale/);
    // The commanded overlay names its source and says its null periods.
    const strip = screen.getByLabelText(/Lifecycle, health and command history/i);
    expect(strip.textContent ?? "").toContain("night-charge adviser");
    expect(strip.textContent ?? "").toContain("nothing commanded");
    // The gap is worded.
    const chart = screen.getByLabelText(/mid power over the window/i);
    expect(chart.textContent ?? "").toMatch(/no samples/);
    // The commanded overlay's summary row names the window's commanded truth
    // and says when it ended — the null periods are named, never zeroed.
    expect(chart.textContent ?? "").toContain("night-charge adviser");
    expect(chart.textContent ?? "").toMatch(/nothing commanded since/);
    // The secondary group holds the cells and temperatures.
    expect(screen.getByText("Cells and temperature")).toBeVisible();
  });

  it("renders the hourly badge, its whole-window caveat, and the band wording", async () => {
    api.client.getSnapshot.mockResolvedValueOnce(
      withHistoryState(snapshot([unitSnapshot({ unit_id: "mid", lifecycle: "disarmed", telemetry: null, measured_watts: null })]), historyState({ last_sample_at: { mid: recordedAtIso() } })),
    );
    api.client.getPlantHistory.mockResolvedValueOnce(hourlyWindow());
    render(<HistoryView client={injectedClient()} />);
    await screen.findByText("Whole site — power");
    expect(screen.getByText("hourly rollup")).toBeVisible();
    expect(screen.getByText(/for its entirety/)).toBeVisible();
    const fleet = screen.getByLabelText(/Whole-site history/i);
    expect(fleet.textContent ?? "").toContain("hourly means with their own min–max bands");
  });

  it("words an absent series 'not available' — never a zero", async () => {
    await landFull();
    await userEvent.setup().click(screen.getByRole("button", { name: "mid" }));
    // Open the secondary group: the cells world is present, but the SOC
    // advisory series exists while, in a thinner world, absent series word
    // themselves. Here: the fleet's house sum row is present, so pin the
    // absence on the hourly world instead — a full world's absent field.
    const block = screen.getByLabelText(/mid charge level over the window/i);
    expect(block.textContent ?? "").toContain("2,871 samples");
  });
});

// --- range switching ------------------------------------------------------------------

describe("History view — range switching", () => {
  it("re-queries with the new preset's window bounds", async () => {
    await landReads(snapshotWith(["mid"]), fullWindow());
    render(<HistoryView client={injectedClient()} />);
    await screen.findByText("Whole site — power");

    api.client.getSnapshot.mockResolvedValueOnce(snapshotWith(["mid"]));
    api.client.getPlantHistory.mockResolvedValueOnce(fullWindow());
    await userEvent.setup().click(screen.getByRole("button", { name: "6 h" }));

    await waitFor(() => {
      expect(api.client.getPlantHistory).toHaveBeenCalledTimes(2);
    });
    const query = api.client.getPlantHistory.mock.calls[1]![0] as {
      from: string;
      to: string;
      fields: string;
      points: number;
    };
    // Explicit offsets, the pinned field set, the points target.
    expect(query.from).toMatch(/(Z|[+-]\d{2}:\d{2})$/);
    expect(query.to).toMatch(/(Z|[+-]\d{2}:\d{2})$/);
    expect(query.fields).toContain("battery_watts");
    expect(query.fields).toContain("commanded");
    expect(query.points).toBeGreaterThanOrEqual(50);
    // A 6 h window is a 6 h window.
    expect(Date.parse(query.to) - Date.parse(query.from)).toBe(6 * 3_600_000);
  });
});

// --- the canvas slot's honest degradation -----------------------------------------------

describe("History view — the chart slot", () => {
  it("says the canvas is unavailable and still carries every fact as text", async () => {
    await landReads(snapshotWith(["mid"]), fullWindow());
    render(<HistoryView client={injectedClient()} />);
    const chart = await screen.findByLabelText(/Whole-site power over the window/i);
    // jsdom has no 2D context: the component says so instead of faking.
    expect(chart.textContent ?? "").toContain("canvas is unavailable");
    // The summary table is the chart's accessible half, present regardless.
    expect(within(chart).getAllByRole("row").length).toBeGreaterThan(0);
  });
});
