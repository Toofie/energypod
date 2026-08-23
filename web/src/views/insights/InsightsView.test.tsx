/**
 * Behavior contract for the Insights view — the energy scorecard's daily
 * ledger (DESIGN_ENERGY_SCORECARD.md §8 W-B): the per-day rows, last-N paging
 * through the days route's own limit, the honest not-commissioned state, the
 * counter-roles note, and the `energy.day_rolled` append.
 *
 * The suite mocks the API client at its surface only; wire fixtures come from
 * web/src/test/wire.ts (the PENDING-BACKEND energy family). Rejections are
 * `ApiClientError` instances carrying the contract's envelopes verbatim — the
 * view renders them verbatim back.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient, StreamEvent } from "../../api/client";
import {
  energyDayRecord,
  energyDayRolled,
  energyNotCommissionedEnvelope,
  getEnergyDaysOk,
  type WireEnergyDayRecord,
} from "../../test/wire";
import { InsightsView } from "./InsightsView";

function notCommissionedError(): ApiClientError {
  return new ApiClientError(energyNotCommissionedEnvelope());
}

/** A stream that parks forever: no frames, no end, no reconnect spin. */
function parkedStream(): AsyncIterable<StreamEvent> {
  return (async function* parked(): AsyncGenerator<StreamEvent, void, unknown> {
    await new Promise(() => undefined);
  })();
}

/** A controllable stream: the given frames first, then pushed frames. */
function liveChannel(initial: readonly StreamEvent[]): {
  openEvents: () => AsyncIterable<StreamEvent>;
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
    openEvents: () =>
      (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
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
      })(),
    push: (frame) => {
      queue.push(frame);
      notify();
    },
  };
}

interface Harness {
  client: ApiClient & { getEnergyDays: ReturnType<typeof vi.fn> };
}

function installHarness(options: {
  getEnergyDays?: ReturnType<typeof vi.fn>;
  openEvents?: () => AsyncIterable<StreamEvent>;
} = {}): Harness {
  const client = {
    getEnergyDays:
      options.getEnergyDays ?? vi.fn(() => Promise.resolve(getEnergyDaysOk({ days: [] }))),
    getSnapshot: vi.fn(),
    getHealth: vi.fn(),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    openEvents: options.openEvents ?? vi.fn(() => parkedStream()),
  };
  return { client: client as unknown as Harness["client"] };
}

function renderView(harness: Harness) {
  return render(<InsightsView client={harness.client} />);
}

/** The ledger's own rows — direct children only (each row nests a unit list). */
function ledgerRows(): HTMLElement[] {
  const ledger = screen.getByRole("list", { name: /daily energy ledger/i });
  return Array.from(ledger.querySelectorAll<HTMLElement>(":scope > li"));
}

/** A rolled day with distinct, assertable figures. */
function rolledDay(date: string, kind: WireEnergyDayRecord["kind"] = "complete"): WireEnergyDayRecord {
  return energyDayRecord({
    date,
    kind,
    units: {
      mid: {
        grid_import_kwh: 1.25,
        grid_export_kwh: 6.75,
        battery_charged_kwh: 3.35,
        battery_discharged_kwh: 0.65,
        load_kwh: 5.05,
        charged_from_surplus_kwh: 3.05,
        coverage_pct: 99.5,
        metric_flags: [],
      },
    },
  });
}

describe("InsightsView — loading, not-commissioned, and errors", () => {
  it("shows the honest not-commissioned state when the route refuses 409 energy_scorecard_not_commissioned", async () => {
    const harness = installHarness({
      getEnergyDays: vi.fn(() => Promise.reject(notCommissionedError())),
    });
    renderView(harness);

    const heading = await screen.findByRole("heading", { name: /not commissioned/i });
    expect(heading).toBeVisible();
    const note = heading.closest("div")!;
    expect(note.textContent).toContain(
      "not commissioned in this deployment's config — there is nothing to show here yet",
    );
    expect(note.textContent).toContain("energy_scorecard_not_commissioned");
    // The commissioning hint is the design's own operator decision 1.
    expect(note.textContent).toContain("add the energy_scorecard block and restart");
    expect(note.textContent).toContain("site-timezone midnight");
  });

  it("renders the error state with the envelope verbatim and recovers on Check-again style retry", async () => {
    let failed = false;
    const harness = installHarness({
      getEnergyDays: vi.fn(() => {
        if (!failed) {
          failed = true;
          return Promise.reject(
            new ApiClientError({
              code: "internal_error",
              message: "The request could not be completed",
              details: null,
              request_id: "req-1",
              status: 500,
            }),
          );
        }
        return Promise.resolve(getEnergyDaysOk({ days: [rolledDay("2026-08-25")] }));
      }),
    });
    renderView(harness);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("internal_error");
    expect(alert.textContent).toContain("The request could not be completed");
    expect(alert.textContent).toContain("req-1");

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    await waitFor(() => {
      expect(screen.getByRole("list", { name: /daily energy ledger/i })).toBeVisible();
    });
  });

  it("says what will appear when the ledger is empty — never a broken-looking blank", async () => {
    const harness = installHarness({
      getEnergyDays: vi.fn(() => Promise.resolve(getEnergyDaysOk({ days: [] }))),
    });
    renderView(harness);
    await waitFor(() => {
      expect(screen.getByText(/no days recorded yet/i)).toBeVisible();
    });
    expect(screen.getByText(/first row appears after the next midnight rollover/i)).toBeVisible();
  });
});

describe("InsightsView — the daily ledger rows", () => {
  it("renders one row per completed day with figures, per-battery breakdown, coverage, and provenance badge", async () => {
    const harness = installHarness({
      getEnergyDays: vi.fn(() =>
        Promise.resolve(
          getEnergyDaysOk({
            days: [rolledDay("2026-08-25"), rolledDay("2026-08-26", "partial")],
            grid_counter_roles: "vendor_labels",
          }),
        ),
      ),
    });
    renderView(harness);

    const ledger = await screen.findByRole("list", { name: /daily energy ledger/i });
    const rows = Array.from(ledger.querySelectorAll<HTMLElement>(":scope > li"));
    expect(rows).toHaveLength(2);
    // Newest-LAST, the route's own order.
    expect(within(rows[0]!).getByText("2026-08-25")).toBeVisible();
    expect(within(rows[1]!).getByText("2026-08-26")).toBeVisible();

    const first = rows[0]!;
    expect(first.textContent).toContain("Bought 1.25 kWh · Sold 6.75 kWh");
    expect(first.textContent).toContain("Charged 3.35 kWh · Discharged 0.65 kWh");
    expect(first.textContent).toContain("House load 5.05 kWh");
    expect(first.textContent).toContain("Charged from surplus 3.05 kWh");
    // Per-battery row: charged/discharged (and the surplus attribution).
    expect(first.textContent).toContain("mid: charged 3.35 kWh, discharged 0.65 kWh");
    expect(first.textContent).toContain("surplus 3.05 kWh");
    expect(first.textContent).toContain("99.5% coverage");
    // The provenance badge names the grid source (the integrated default).
    expect(within(first).getByText("measured by the controller")).toBeVisible();
    // The partial day names the threshold breach, never a quiet full number.
    expect(within(rows[1]!).getByText(/partial day/i).textContent).toContain(
      "below the commissioned coverage threshold",
    );
  });

  it("names a null figure 'not available' and carries the counter-reset marker verbatim", async () => {
    const day = energyDayRecord({
      date: "2026-08-24",
      units: {
        mid: {
          grid_import_kwh: null,
          grid_export_kwh: 2,
          battery_charged_kwh: 1,
          battery_discharged_kwh: null,
          load_kwh: 3,
          charged_from_surplus_kwh: null,
          coverage_pct: null,
          metric_flags: ["counter_reset_observed"],
        },
      },
    });
    const harness = installHarness({
      getEnergyDays: vi.fn(() => Promise.resolve(getEnergyDaysOk({ days: [day] }))),
    });
    renderView(harness);

    const row = (await screen.findByRole("list", { name: /daily energy ledger/i }))
      .querySelectorAll<HTMLElement>(":scope > li")
      .item(0)!;
    expect(row.textContent).toContain("Bought not available");
    expect(row.textContent).toContain("Discharged not available");
    expect(row.textContent).toContain("Coverage not available");
    expect(row.textContent).toContain("a counter was reset during this day");
  });

  it("carries the counter-roles note for the route's answer, and the solar footnote exactly once", async () => {
    const harness = installHarness({
      getEnergyDays: vi.fn(() =>
        Promise.resolve(getEnergyDaysOk({ days: [rolledDay("2026-08-25")], grid_counter_roles: "unpinned" })),
      ),
    });
    renderView(harness);

    const note = await screen.findByRole("note");
    expect(note.textContent).toContain("counter A and counter B");
    expect(note.textContent).toContain("not confirmed yet");
    expect(note.textContent).toContain("measured by the controller");
    // The never-solar pin, once.
    expect(screen.getAllByText(/solar panels are not measured by the pods/i)).toHaveLength(1);
    // No solar-production column exists anywhere in the ledger.
    expect(document.body.textContent ?? "").not.toMatch(/solar production/i);
  });

  it("discloses the A/B cross-check deltas as evidence — never as the bought/sold figures", async () => {
    const harness = installHarness({
      getEnergyDays: vi.fn(() =>
        Promise.resolve(getEnergyDaysOk({ days: [rolledDay("2026-08-25")] })),
      ),
    });
    renderView(harness);
    const row = (await screen.findByRole("list", { name: /daily energy ledger/i }))
      .querySelectorAll<HTMLElement>(":scope > li")
      .item(0)!;
    await userEvent.click(within(row).getByText(/grid counter cross-check/i));
    const detail = within(row).getByText(/Counter A/i);
    expect(detail.textContent).toContain("Counter A 8.3 kWh");
    expect(detail.textContent).toContain("Counter B 12.8 kWh");
    expect(detail.textContent).toContain("consistent with the vendor's labels");
    expect(detail.textContent).toContain("never come from these counters");
  });

  it("renders the honest not-yet-discriminating line when the day's verdict is null", async () => {
    // The wire pin (2026-08-26): the backend emits consistent_with null on
    // non-discriminating days — a first-class answer, never a missing field.
    const day = energyDayRecord({
      date: "2026-08-25",
      counter_cross_check: {
        grid_a_delta_kwh: 1.1,
        grid_b_delta_kwh: 6.9,
        consistent_with: null,
        discriminating: false,
      },
    });
    const harness = installHarness({
      getEnergyDays: vi.fn(() => Promise.resolve(getEnergyDaysOk({ days: [day] }))),
    });
    renderView(harness);
    const row = (await screen.findByRole("list", { name: /daily energy ledger/i }))
      .querySelectorAll<HTMLElement>(":scope > li")
      .item(0)!;
    await userEvent.click(within(row).getByText(/grid counter cross-check/i));
    const detail = within(row).getByText(/Counter A/i);
    expect(detail.textContent).toContain("Counter A 1.1 kWh");
    expect(detail.textContent).toContain("not yet discriminating");
    expect(detail.textContent).toContain("(not discriminating)");
    // No verdict is invented for a day that did not carry one.
    expect(detail.textContent).not.toContain("consistent with");
  });
});

describe("InsightsView — last-N paging", () => {
  it("pages older days through the route's own limit widening", async () => {
    const eightDays = Array.from({ length: 8 }, (_unused, index) =>
      rolledDay(`2026-08-1${index + 1}`),
    );
    const sixteenDays = [
      ...Array.from({ length: 8 }, (_unused, index) => rolledDay(`2026-08-0${index + 1}`)),
      ...eightDays,
    ];
    const get = vi
      .fn()
      .mockResolvedValueOnce(getEnergyDaysOk({ days: eightDays }))
      .mockResolvedValueOnce(getEnergyDaysOk({ days: sixteenDays }));
    const harness = installHarness({ getEnergyDays: get });
    renderView(harness);

    await screen.findByRole("list", { name: /daily energy ledger/i });
    expect(get).toHaveBeenNthCalledWith(1, 8);

    await userEvent.click(screen.getByRole("button", { name: /load more days/i }));
    await waitFor(() => {
      expect(ledgerRows()).toHaveLength(16);
    });
    expect(get).toHaveBeenNthCalledWith(2, 16);
  });

  it("stops paging when the route answered fewer days than asked — no older days exist", async () => {
    const threeDays = Array.from({ length: 3 }, (_unused, index) => rolledDay(`2026-08-2${index + 1}`));
    const harness = installHarness({
      getEnergyDays: vi.fn(() => Promise.resolve(getEnergyDaysOk({ days: threeDays }))),
    });
    renderView(harness);

    await screen.findByRole("list", { name: /daily energy ledger/i });
    expect(screen.getByText(/no older days are recorded/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /load more days/i })).toBeNull();
  });

  it("shows a load-more error without dropping the loaded days", async () => {
    const eightDays = Array.from({ length: 8 }, (_unused, index) => rolledDay(`2026-08-1${index + 1}`));
    const get = vi
      .fn()
      .mockResolvedValueOnce(getEnergyDaysOk({ days: eightDays }))
      .mockRejectedValueOnce(
        new ApiClientError({
          code: "internal_error",
          message: "The request could not be completed",
          details: null,
          request_id: "req-2",
          status: 500,
        }),
      );
    const harness = installHarness({ getEnergyDays: get });
    renderView(harness);

    await screen.findByRole("list", { name: /daily energy ledger/i });
    await userEvent.click(screen.getByRole("button", { name: /load more days/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("We couldn't load more days");
    expect(alert.textContent).toContain("internal_error");
    // The loaded days are still on screen.
    expect(screen.getByText("2026-08-18")).toBeVisible();
    expect(ledgerRows()).toHaveLength(8);
  });
});

describe("InsightsView — the day_rolled transition", () => {
  it("appends the rolled day to the loaded ledger, newest-last, deduped by date", async () => {
    const channel = liveChannel([]);
    const harness = installHarness({
      getEnergyDays: vi.fn(() =>
        Promise.resolve(getEnergyDaysOk({ days: [rolledDay("2026-08-25")] })),
      ),
      openEvents: channel.openEvents,
    });
    renderView(harness);

    await screen.findByRole("list", { name: /daily energy ledger/i });
    channel.push(energyDayRolled(4102, rolledDay("2026-08-26")));
    await waitFor(() => {
      expect(ledgerRows()).toHaveLength(2);
    });
    // Newest-last: the rolled day is the second row.
    expect(within(ledgerRows()[1]!).getByText("2026-08-26")).toBeVisible();
    expect(screen.getByRole("status").textContent).toContain("2026-08-26 was added");

    // A replayed frame of the same rollover never duplicates the row.
    channel.push(energyDayRolled(4103, rolledDay("2026-08-26")));
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(ledgerRows()).toHaveLength(2);
  });
});
