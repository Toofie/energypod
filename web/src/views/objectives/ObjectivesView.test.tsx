/**
 * Behavior contract for the Objectives view — the night-writer detector's
 * evidence table (API_CONTRACTS.md "Night-writer detector", the read surface
 * `GET /api/v1/objectives/observed`): the per-unit session record of what
 * commanded the batteries while we commanded nothing.
 *
 * WIRE TRUTH (web/src/test/wire.ts `getObservedObjectivesOk`, from the
 * amended contract):
 *
 * - The 200 body is `{as_of, last, window_s, units: [...]}`; each unit row is
 *   the contract's rollup: first/last seen, NONZERO sample counts split by the
 *   active word's sign, min/typical/max active watts (the typical figure is
 *   the backend's LOWER median), the foreign episode count and standing
 *   state, and the same `last_objective_observed` summary the snapshot
 *   carries. Nulls are named, never zero-filled.
 * - The detector composes ALWAYS once its backend half lands (no config
 *   block), so the view has no not-commissioned state — an older backend
 *   without the route answers an error envelope, rendered verbatim.
 * - QUIET-TIER EVIDENCE IS NEVER PUBLISHED on the bus: this view and the
 *   snapshot's per-unit summary are the quiet tier's only windows, and the
 *   pinned honesty note (the signature's attribution limit) heads the table.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../../api/client";
import type { ApiClient, StreamEvent } from "../../api/client";
import { OBJECTIVE_SIGNATURE_HONESTY_NOTE } from "../../app/objectives";
import {
  foreignObjectiveObserved,
  getObservedObjectivesOk,
  lastObjectiveObserved,
  observedObjectivesUnit,
  type WireObservedObjectivesUnit,
} from "../../test/wire";
import { ObjectivesView } from "./ObjectivesView";

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, createApiClient: vi.fn() };
});

const createClientMock = vi.mocked(createApiClient);

function refusal(
  code: string,
  message: string,
  status: number,
  requestId: string,
): ApiClientError {
  return new ApiClientError({ code, message, details: null, request_id: requestId, status });
}

/** An event stream that stays open and delivers nothing (the quiet bus). */
function parkedStream(): AsyncIterable<StreamEvent> {
  return {
    async *[Symbol.asyncIterator]() {
      await new Promise<never>(() => {});
    },
  };
}

function installClient(
  getObjectives: ApiClient["getObservedObjectives"],
  openEvents: ApiClient["openEvents"] = () => parkedStream(),
): ApiClient {
  const client = {
    getObservedObjectives: vi.fn(getObjectives),
    openEvents: vi.fn(openEvents),
  } as unknown as ApiClient;
  createClientMock.mockReturnValue(client);
  return client;
}

function renderView(): void {
  render(<ObjectivesView client={createApiClient("operator-token")} />);
}

const FOREIGN_NOW_ROW: WireObservedObjectivesUnit = observedObjectivesUnit();

/** The site's REAL nightly story: the KNOWN writer, expected and quiet. */
const NIGHTLY_ROW: WireObservedObjectivesUnit = observedObjectivesUnit({
  unit_id: "rhs",
  first_seen_at: "2026-08-24T00:00:00+10:00",
  last_seen_at: "2026-08-24T05:58:00+10:00",
  sample_count: 718,
  charge_sample_count: 718,
  discharge_sample_count: 0,
  min_active_w: -2554,
  typical_active_w: -2500,
  max_active_w: -2441,
  classification_counts: { expected_nightly_charge: 717, handback_grace: 1 },
  foreign_episode_count: 0,
  foreign_active: false,
  foreign_reason: null,
  last_objective_observed: lastObjectiveObserved({
    observed_at: "2026-08-24T05:58:00+10:00",
    active_w: -2500,
    classification: "expected_nightly_charge",
    reason: null,
  }),
});

const EMPTY_ROW: WireObservedObjectivesUnit = observedObjectivesUnit({
  unit_id: "lhs",
  first_seen_at: null,
  last_seen_at: null,
  sample_count: 0,
  charge_sample_count: 0,
  discharge_sample_count: 0,
  min_active_w: null,
  typical_active_w: null,
  max_active_w: null,
  foreign_episode_count: 0,
  foreign_active: false,
  foreign_reason: null,
  last_objective_observed: null,
});

beforeEach(() => {
  createClientMock.mockReset();
});

describe("ObjectivesView — the evidence table (API_CONTRACTS 'Night-writer detector')", () => {
  it("renders the per-unit session record from the endpoint's own rollup", async () => {
    installClient(() =>
      Promise.resolve(
        getObservedObjectivesOk({ units: [FOREIGN_NOW_ROW, NIGHTLY_ROW, EMPTY_ROW] }),
      ),
    );
    renderView();

    // The pinned honesty line heads the table, always.
    expect(await screen.findByText(OBJECTIVE_SIGNATURE_HONESTY_NOTE)).toBeVisible();

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    // header + three unit rows
    expect(rows).toHaveLength(4);

    // The foreign-now row: the detector's standing assertion with its reason
    // in plain words, the window's own figures, and the sign split.
    const midRow = within(table).getByRole("row", { name: /mid/i });
    expect(midRow.textContent).toContain("23:31 – 05:12");
    expect(midRow.textContent).toContain("442 (442 charging, 0 discharging)");
    expect(midRow.textContent).toContain("min -2,400 W · typical -2,270 W · max -1,980 W");
    expect(midRow.textContent).toContain(
      "1 foreign episode; foreign now — an external charge pattern — sustained charging while the site imported, with no solar surplus",
    );
    expect(midRow.textContent).toContain("-2,400 W held by an external writer at 23:40");

    // The quiet rows render beside it with the same honesty — evidence,
    // never an alarm tone difference in words. The nightly row IS the site's
    // known writer: characterized, counted, never foreign.
    const rhsRow = within(table).getByRole("row", { name: /rhs/i });
    expect(rhsRow.textContent).toContain("00:00 – 05:58");
    expect(rhsRow.textContent).toContain("718 (718 charging, 0 discharging)");
    expect(rhsRow.textContent).toContain("nightly charge 717 · handback 1");
    expect(rhsRow.textContent).toContain("0 foreign episodes; none active");
    expect(rhsRow.textContent).toContain(
      "-2,500 W held by the site's scheduled nightly charge at 05:58",
    );

    // A unit with no recorded samples names every gap — never 0, never
    // fabricated.
    const lhsRow = within(table).getByRole("row", { name: /lhs/i });
    expect(lhsRow.textContent).toContain("not available");
    expect(lhsRow.textContent).toContain("no samples recorded");
    expect(lhsRow.textContent).toContain("no recorded sample");
  });

  it("carries the window's own caption — the echoed ask and the answer's stamp, never a local clock", async () => {
    installClient(() =>
      Promise.resolve(
        getObservedObjectivesOk({
          as_of: "2026-08-24T06:00:00+10:00",
          last: "24h",
          units: [FOREIGN_NOW_ROW],
        }),
      ),
    );
    renderView();
    expect(await screen.findByText("Window: 24 hours, as of 06:00 local")).toBeVisible();
  });

  it("switching the window re-reads with the chosen spelling (the route's own Nh/Nd grammar)", async () => {
    const getObjectives = vi.fn((last?: string) =>
      Promise.resolve(getObservedObjectivesOk({ last: last ?? "24h", units: [NIGHTLY_ROW] })),
    );
    installClient(getObjectives as unknown as ApiClient["getObservedObjectives"]);
    renderView();
    await screen.findByRole("table");
    expect(getObjectives).toHaveBeenLastCalledWith("24h");

    await userEvent.click(screen.getByRole("button", { name: "3 days" }));
    await waitFor(() => {
      expect(getObjectives).toHaveBeenLastCalledWith("3d");
    });
    expect(screen.getByRole("button", { name: "3 days" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(screen.getByRole("button", { name: "24 hours" }).getAttribute("aria-pressed")).toBe(
      "false",
    );

    await userEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() => {
      expect(getObjectives).toHaveBeenLastCalledWith("7d");
    });
  });

  it("explains an empty window honestly — what will appear here and when", async () => {
    installClient(() => Promise.resolve(getObservedObjectivesOk({ units: [] })));
    renderView();
    expect(
      await screen.findByText(
        /No batteries are recorded in this window — samples appear once a pod holds an objective while nothing of ours commands it./,
      ),
    ).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("surfaces a refused read verbatim — code, message, request id — and recovers on retry", async () => {
    let refused = true;
    installClient(() =>
      refused
        ? Promise.reject(
            refusal(
              "http_404",
              "The route does not exist on this controller build",
              404,
              "req-objectives-1",
            ),
          )
        : Promise.resolve(getObservedObjectivesOk({ units: [NIGHTLY_ROW] })),
    );
    renderView();
    expect(await screen.findByText(/We couldn't load the observed objectives./i)).toBeVisible();
    expect(await screen.findByText("http_404")).toBeVisible();
    expect(
      await screen.findByText("The route does not exist on this controller build"),
    ).toBeVisible();
    expect(await screen.findByText("req-objectives-1")).toBeVisible();

    refused = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table")).toBeVisible();
  });

  it("refuses to half-adopt an unreadable body — the honest error, never a partial table", async () => {
    installClient(() => Promise.resolve({ units: "no" } as unknown as Record<string, unknown>));
    renderView();
    expect(await screen.findByText("unreadable_observed_objectives")).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("renders the loading state before the first answer lands", async () => {
    installClient(
      () =>
        new Promise(() => {
          /* never settles */
        }),
    );
    renderView();
    expect(await screen.findByRole("status", { name: /loading observed objectives/i }));
    // The honesty note heads the view even while loading — it is the table's
    // standing contract, not data.
    expect(screen.getByText(OBJECTIVE_SIGNATURE_HONESTY_NOTE)).toBeVisible();
  });

  // The live path: the detector's ALERT frame is exactly the moment this
  // table's world changed. Without the subscription a console left open on
  // Objectives showed the mount-time window for the whole session while
  // Home, Batteries and Activity all moved (Insights' day_rolled pattern).
  it("re-reads the window when the detector's alert frame lands on the bus — silently, never blanking the table", async () => {
    const answers = [
      getObservedObjectivesOk({ units: [NIGHTLY_ROW] }),
      getObservedObjectivesOk({ units: [NIGHTLY_ROW, FOREIGN_NOW_ROW] }),
    ];
    const getObjectives = vi.fn(() => Promise.resolve(answers.shift()!));
    const queue: StreamEvent[] = [];
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    let wake: (() => void) | null = null;
    const openEvents = (): AsyncIterable<StreamEvent> => ({
      async *[Symbol.asyncIterator]() {
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
    });
    installClient(
      getObjectives as unknown as ApiClient["getObservedObjectives"],
      openEvents,
    );
    renderView();

    // The mount read answered one quiet battery; the alert tier fires.
    const table = await screen.findByRole("table");
    expect(within(table).queryByRole("row", { name: /^mid/i })).toBeNull();
    queue.push(foreignObjectiveObserved(501) as unknown as StreamEvent);
    notify();

    // One silent re-read lands the new evidence row — no loading flash, no
    // error state over data that was already on screen.
    await waitFor(() => {
      expect(getObjectives).toHaveBeenCalledTimes(2);
    });
    const midRow = await within(await screen.findByRole("table")).findByRole("row", {
      name: /^mid/i,
    });
    expect(midRow.textContent).toContain("foreign now");
  });

  it("keeps the last table when the alert-driven re-read fails — the caption still names its own as-of stamp", async () => {
    const answers: (Promise<Record<string, unknown>> | null)[] = [
      Promise.resolve(getObservedObjectivesOk({ units: [NIGHTLY_ROW] })),
      null,
    ];
    const getObjectives = vi.fn(() => {
      const next = answers.shift();
      return next ?? Promise.reject(refusal("network_error", "unreachable", 0, ""));
    });
    const queue: StreamEvent[] = [];
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    let wake: (() => void) | null = null;
    const openEvents = (): AsyncIterable<StreamEvent> => ({
      async *[Symbol.asyncIterator]() {
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
    });
    installClient(
      getObjectives as unknown as ApiClient["getObservedObjectives"],
      openEvents,
    );
    renderView();

    const table = await screen.findByRole("table");
    expect(table.textContent).toContain("rhs");
    queue.push(foreignObjectiveObserved(502) as unknown as StreamEvent);
    notify();

    await waitFor(() => {
      expect(getObjectives).toHaveBeenCalledTimes(2);
    });
    // The transient failure neither blanks the table nor raises the error
    // state — the silent path keeps the last honest picture.
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByText(/We couldn't load the observed objectives./i)).toBeNull();
  });
});
