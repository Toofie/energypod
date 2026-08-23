/**
 * Behavior contract for the Schedule view (DESIGN_SCHEDULES.md §6 W-A/W-B):
 * the whole-plan list editor, its validation, the allowed-window guard, the
 * CAS publish, and the refusal-routed night acknowledgement.
 *
 * The suite mocks the API client at its surface only; wire fixtures come from
 * web/src/test/wire.ts (the PENDING-BACKEND schedule family). Rejections are
 * `ApiClientError` instances carrying the contract's refusal envelopes
 * verbatim — the view renders them verbatim back.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient, Snapshot, StreamEvent } from "../../api/client";
import {
  getScheduleOk,
  putScheduleOk,
  scheduleEntry,
  schedulePlan,
  schedulePolicy,
  scheduleRefusalEnvelope,
  scheduleReplaced,
  type WireScheduleEntry,
  type WireSchedulePlan,
} from "../../test/wire";
import { ScheduleView } from "./ScheduleView";

function refusalError(
  code: Parameters<typeof scheduleRefusalEnvelope>[0],
  options: Parameters<typeof scheduleRefusalEnvelope>[1] = {},
): ApiClientError {
  return new ApiClientError(scheduleRefusalEnvelope(code, options));
}

/** The body of the Nth publish call (mock.calls entries are argument tuples). */
function publishBody(put: ReturnType<typeof vi.fn>, index = 0): Record<string, unknown> {
  const args = put.mock.calls[index] as unknown as unknown[];
  return args[0] as Record<string, unknown>;
}

const SNAPSHOT: Snapshot = {
  site_id: "home-1",
  snapshot_sequence: 4100,
  captured_at: "2026-08-22T10:00:00Z",
  units: [
    { unit_id: "lhs", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good", requested_power: { direction: "IDLE", watts: 0 }, authorized_power: null, measured_watts: 0 },
    { unit_id: "mid", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good", requested_power: { direction: "IDLE", watts: 0 }, authorized_power: null, measured_watts: 0 },
    { unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good", requested_power: { direction: "IDLE", watts: 0 }, authorized_power: null, measured_watts: 0 },
  ],
} as unknown as Snapshot;

/** A stream that parks forever: no frames, no end, no reconnect spin. */
function parkedStream(): AsyncIterable<StreamEvent> {
  return (async function* parked(): AsyncGenerator<StreamEvent, void, unknown> {
    await new Promise(() => undefined);
  })();
}

interface Harness {
  client: ApiClient & {
    getSchedule: ReturnType<typeof vi.fn>;
    putSchedule: ReturnType<typeof vi.fn>;
    getSnapshot: ReturnType<typeof vi.fn>;
    openEvents: ReturnType<typeof vi.fn>;
  };
}

function installHarness(options: {
  get?: ReturnType<typeof vi.fn>;
  put?: ReturnType<typeof vi.fn>;
  snapshot?: Snapshot;
  openEvents?: (afterSequence?: number) => AsyncIterable<StreamEvent>;
} = {}): Harness {
  const client = {
    getSchedule: options.get ?? vi.fn(() => Promise.resolve(getScheduleOk({}))),
    putSchedule: options.put ?? vi.fn(() => Promise.resolve(putScheduleOk({ version: 5, plan: schedulePlan([]) }))),
    getSnapshot: vi.fn(() => Promise.resolve(options.snapshot ?? SNAPSHOT)),
    getHealth: vi.fn(),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    openEvents: options.openEvents ?? vi.fn(() => parkedStream()),
  };
  return { client: client as unknown as Harness["client"] };
}

function renderView(harness: Harness) {
  return render(<ScheduleView client={harness.client} />);
}

/** The entry cards' shared list. */
function entryCards(): HTMLElement[] {
  return screen.queryAllByRole("listitem").filter((node) => node.className.includes("schedule-entry"));
}

const DAY_CHARGE_ENTRY = scheduleEntry();

function planWith(entries: WireScheduleEntry[], version = 4): WireSchedulePlan {
  return schedulePlan(entries, { version });
}

describe("ScheduleView — loading, not-commissioned, and errors", () => {
  it("renders the honest not-commissioned state when GET refuses schedule_not_commissioned", async () => {
    const harness = installHarness({
      get: vi.fn(() => Promise.reject(refusalError("schedule_not_commissioned"))),
    });
    renderView(harness);

    const card = await screen.findByRole("heading", { name: /not commissioned/i });
    expect(card).toBeVisible();
    expect(card.parentElement).toHaveTextContent(
      /not commissioned in this deployment's config — there is nothing to edit here/i,
    );
    expect(screen.getByText("schedule_not_commissioned")).toBeVisible();
    // The refusal message renders verbatim beside the code.
    expect(screen.getByText(/Scheduling is not commissioned in this deployment's config/i)).toBeVisible();
  });

  it("renders the error state with the envelope verbatim and recovers on Retry", async () => {
    let failed = false;
    const harness = installHarness({
      get: vi.fn(() => {
        if (!failed) {
          failed = true;
          return Promise.reject(refusalError("validation_error"));
        }
        return Promise.resolve(getScheduleOk({}));
      }),
    });
    renderView(harness);

    expect(await screen.findByText("validation_error")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(screen.getByText(/No schedule published yet/i)).toBeVisible();
    });
  });

  it("shows the loading line before the first answer lands", () => {
    const harness = installHarness({
      get: vi.fn(() => new Promise(() => undefined)),
    });
    renderView(harness);
    expect(screen.getByRole("status").textContent).toMatch(/Loading the schedule/i);
  });
});

describe("ScheduleView — the commissioned windows and the plan line", () => {
  it("states the day-only policy, the published version, and the next occurrence", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            plan: planWith([DAY_CHARGE_ENTRY]),
            policy: schedulePolicy(),
            next_action: null,
          }),
        ),
      ),
    });
    renderView(harness);

    await screen.findByText(/No schedule published yet — the first publish creates the plan/i).catch(() => {});
    await waitFor(() => {
      expect(screen.getByText(/Published plan v4/i)).toBeVisible();
    });
    expect(screen.getByText(/day-only posture; the night window belongs to the site's other applications/i)).toBeVisible();
    expect(screen.getByText(/Schedules may command 06:00–20:00/i)).toBeVisible();
    expect(screen.getByText(/times follow Australia\/Brisbane/i)).toBeVisible();
    expect(screen.getByText(/Nothing is coming up — every entry is paused or past its date range/i)).toBeVisible();
  });

  it("states the partition policy when the config granted night windows", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
          }),
        ),
      ),
    });
    renderView(harness);

    await waitFor(() => {
      expect(
        screen.getByText(/partition posture; the night window was granted to the controller by a config revision/i),
      ).toBeVisible();
    });
    expect(screen.getByText(/Schedules may command 20:00–06:00/i)).toBeVisible();
  });
});

describe("ScheduleView — the entry list", () => {
  it("renders one card per entry with its name, days, window, direction, and per-battery watts", async () => {
    const harness = installHarness({
      get: vi.fn(() => Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))),
    });
    renderView(harness);

    await waitFor(() => {
      expect(entryCards()).toHaveLength(1);
    });
    const card = entryCards()[0]!;
    expect(within(card).getByDisplayValue("Day charge")).toBeVisible();
    expect(card).toHaveTextContent("every day");
    expect(card).toHaveTextContent("06:30 to 18:00");
    expect(card).toHaveTextContent("Charge");
    expect(card).toHaveTextContent("2,500 W per battery (lhs, mid, rhs)");
    // The per-battery form is the default input: one field per selected battery.
    expect(within(card).getByLabelText("Watts for lhs")).toHaveValue(2500);
    expect(within(card).getByLabelText("Watts for mid")).toHaveValue(2500);
    expect(within(card).getByLabelText("Watts for rhs")).toHaveValue(2500);
    expect(within(card).getByRole("switch", { name: /enabled/i })).toHaveAttribute("aria-checked", "true");
  });

  it("adds an entry with day-legal defaults and removes one", async () => {
    const harness = installHarness({ get: vi.fn(() => Promise.resolve(getScheduleOk({}))) });
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);

    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    expect(entryCards()).toHaveLength(1);
    const card = entryCards()[0]!;
    expect(within(card).getByText(/every day/i)).toBeVisible();
    expect(within(card).getAllByRole("checkbox", { name: /mon|tue|wed|thu|fri|sat|sun/i })).toHaveLength(7);

    await userEvent.click(within(card).getByRole("button", { name: "Remove" }));
    expect(entryCards()).toHaveLength(0);
  });

  it("toggles an entry between enabled and paused", async () => {
    const harness = installHarness({
      get: vi.fn(() => Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))),
    });
    renderView(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));

    const toggle = within(entryCards()[0]!).getByRole("switch");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(toggle).toHaveTextContent(/paused/i);
  });

  it("keeps a wire-carried idle entry idle — the v1 picker never flips it silently", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({ plan: planWith([scheduleEntry({ entry_id: "Quiet hours", action: "idle", watts: 0 })]) }),
        ),
      ),
    });
    renderView(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));

    const card = entryCards()[0]!;
    expect(card).toHaveTextContent("Hold to zero");
    expect(within(card).getByText(/holds to zero \(idle\)/i)).toBeVisible();
  });
});

describe("ScheduleView — inline validation", () => {
  async function renderFreshEntry(): Promise<HTMLElement> {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await userEvent.type(within(card).getByLabelText("Name"), "Night Charge");
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.type(within(card).getByLabelText(`Watts for ${unitId}`), "2500");
    }
    return card;
  }

  it("requires a name", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));

    expect(await within(entryCards()[0]!).findByText("Enter a name for this schedule.")).toBeVisible();
  });

  it("refuses a duplicate name", async () => {
    const harness = installHarness({
      get: vi.fn(() => Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))),
    });
    renderView(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));

    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const fresh = entryCards()[1]!;
    await userEvent.type(within(fresh).getByLabelText("Name"), "day charge");
    expect(
      await within(fresh).findByText(/A schedule named .day charge. already exists — names must be unique./i),
    ).toBeVisible();
  });

  it("requires at least one day", async () => {
    const card = await renderFreshEntry();
    for (const day of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
      await userEvent.click(within(card).getByRole("checkbox", { name: day }));
    }
    expect(await within(card).findByText("Select at least one day.")).toBeVisible();
  });

  it("requires valid HH:MM times that differ", async () => {
    const card = await renderFreshEntry();
    const times = within(card).getAllByLabelText(/^(Starts|Ends)$/i);
    fireEvent.change(times[0]!, { target: { value: "07:00" } });
    fireEvent.change(times[1]!, { target: { value: "07:00" } });
    expect(
      await within(card).findByText("The start and end times must differ — a window needs length."),
    ).toBeVisible();
  });

  it("requires at least one battery", async () => {
    const card = await renderFreshEntry();
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.click(within(card).getByRole("checkbox", { name: new RegExp(`^${unitId}$`) }));
    }
    expect(await within(card).findByText("Select at least one battery.")).toBeVisible();
  });

  it("requires positive watts per battery, bounded by the per-battery cap", async () => {
    const card = await renderFreshEntry();
    const lhs = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(lhs);
    expect(await within(card).findByText(/Enter positive watts for every selected battery/i)).toBeVisible();

    await userEvent.type(lhs, "3000");
    expect(
      await within(card).findByText(/too high: the bound is 2,500 W per battery/i),
    ).toBeVisible();
  });

  it("validates the scalar fleet-total form under the advanced disclosure", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await userEvent.type(within(card).getByLabelText("Name"), "Off-peak");
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.type(within(card).getByLabelText(`Watts for ${unitId}`), "2500");
    }
    await userEvent.click(within(card).getByRole("button", { name: /^Advanced$/i }));
    await userEvent.click(within(card).getByRole("radio", { name: /Fleet total \(scalar\)/i }));
    const scalar = await within(card).findByLabelText(/Fleet-total watts/i);
    await userEvent.clear(scalar);
    expect(
      await within(card).findByText("Enter a positive number of watts (the fleet total)."),
    ).toBeVisible();
    await userEvent.type(scalar, "5000");
    // The per-battery fields are gone from the active form (one form at a time).
    expect(within(card).queryByLabelText("Watts for lhs")).toBeNull();
  });

  it("validates priority and the effective dates", async () => {
    const card = await renderFreshEntry();
    await userEvent.click(within(card).getByRole("button", { name: /^Advanced$/i }));
    await userEvent.type(await within(card).findByLabelText(/Priority \(advanced\)/i), "high");
    expect(
      await within(card).findByText(/Priority is a whole number/i),
    ).toBeVisible();

    const from = within(card).getByLabelText(/Effective from \(optional\)/i);
    const until = within(card).getByLabelText(/Effective until \(optional\)/i);
    fireEvent.change(from, { target: { value: "2026-09-01" } });
    fireEvent.change(until, { target: { value: "2026-08-01" } });
    expect(
      await within(card).findByText(/effective end date must not come before the start date/i),
    ).toBeVisible();
  });

  it("blocks publishing while any row is invalid, and says so", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));

    const publish = screen.getByRole("button", { name: /Publish changes/i });
    expect(publish).toBeDisabled();
    expect(screen.getByText(/Fix the highlighted entries before publishing/i)).toBeVisible();
    expect(harness.client.putSchedule).not.toHaveBeenCalled();
  });
});

describe("ScheduleView — per-battery watts (the default form)", () => {
  it("fills every selected battery from the same-for-all field and shows the live total", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await userEvent.type(within(card).getByLabelText("Name"), "Day charge");

    expect(within(card).getByText(/Enter the watts for each selected battery to see the total/i)).toBeVisible();

    await userEvent.type(within(card).getByLabelText(/Watts to apply to every selected battery/i), "1800");
    expect(within(card).getByLabelText("Watts for lhs")).toHaveValue(1800);
    expect(within(card).getByLabelText("Watts for mid")).toHaveValue(1800);
    expect(within(card).getByLabelText("Watts for rhs")).toHaveValue(1800);
    expect(
      within(card).getByText(/lhs 1,800 W \+ mid 1,800 W \+ rhs 1,800 W = 5,400 W in total/i),
    ).toBeVisible();
    expect(within(card).getByText(/1,800 W per battery \(lhs, mid, rhs\)/i)).toBeVisible();
  });

  it("carries each battery's own figure separately when they differ", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            plan: planWith([
              scheduleEntry({ watts_by_unit: { lhs: 2000, mid: 2500, rhs: 2500 } }),
            ]),
          }),
        ),
      ),
    });
    renderView(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    expect(entryCards()[0]!).toHaveTextContent("lhs 2,000 W, mid 2,500 W, rhs 2,500 W");
  });
});

describe("ScheduleView — the allowed-window guard", () => {
  function nightEntryCard(): Promise<HTMLElement> {
    return waitFor(() => {
      const cards = entryCards();
      expect(cards).toHaveLength(1);
      return cards[0]!;
    });
  }

  it("warns live and holds publishing when an enabled entry leaves the commissioned windows", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await userEvent.type(within(card).getByLabelText("Name"), "Night Charge");
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.type(within(card).getByLabelText(`Watts for ${unitId}`), "2500");
    }
    const times = within(card).getAllByLabelText(/^(Starts|Ends)$/i);
    fireEvent.change(times[0]!, { target: { value: "00:01" } });
    fireEvent.change(times[1]!, { target: { value: "05:59" } });

    expect(
      await within(card).findByText(
        /outside the commissioned 06:00–20:00 windows — publishing will be refused; night windows need the one-time acknowledgement/i,
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /Publish changes/i })).toBeDisabled();
    expect(screen.getByText(/Publishing is held: Night Charge falls outside the commissioned 06:00–20:00 windows/i)).toBeVisible();
    expect(harness.client.putSchedule).not.toHaveBeenCalled();
  });

  it("still judges a cross-midnight window by every minute of it", async () => {
    const harness = installHarness({});
    renderView(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await userEvent.type(within(card).getByLabelText("Name"), "Late top-up");
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.type(within(card).getByLabelText(`Watts for ${unitId}`), "2500");
    }
    const times = within(card).getAllByLabelText(/^(Starts|Ends)$/i);
    fireEvent.change(times[0]!, { target: { value: "22:30" } });
    fireEvent.change(times[1]!, { target: { value: "06:00" } });

    expect(await within(card).findByText(/one window, not two/i)).toBeVisible();
    expect(
      await within(card).findByText(/outside the commissioned 06:00–20:00 windows/i),
    ).toBeVisible();
  });

  it("warns on a paused night entry but does not hold publishing (the server checks enabled entries)", async () => {
    const put = vi.fn(() =>
      Promise.resolve(
        putScheduleOk({
          version: 5,
          plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "00:01", end_local: "05:59", enabled: false })]),
        }),
      ),
    );
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "00:01", end_local: "05:59", enabled: false })]),
          }),
        ),
      ),
      put,
    });
    renderView(harness);
    const card = await nightEntryCard();

    // The warning names the commissioned windows, but a PAUSED entry does not
    // hold the publish: the server judges enabled entries only.
    expect(
      await within(card).findByText(/outside the commissioned 06:00–20:00 windows/i),
    ).toBeVisible();
    expect(screen.getByText(/No unsent changes/i)).toBeVisible();

    const watts = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(watts);
    await userEvent.type(watts, "2100");
    expect(screen.getByRole("button", { name: /Publish changes/i })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("names the paused-night acknowledgement when partition granted the window", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
            plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "22:30", end_local: "05:59", enabled: false })]),
          }),
        ),
      ),
    });
    renderView(harness);
    const card = await nightEntryCard();

    expect(
      await within(card).findByText(/This paused window runs at night — enabling it needs the one-time acknowledgement/i),
    ).toBeVisible();
  });

  it("names the night acknowledgement (not a refusal) when partition granted the window", async () => {
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
            plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "22:30", end_local: "05:59" })]),
          }),
        ),
      ),
    });
    renderView(harness);
    const card = await nightEntryCard();

    expect(
      await within(card).findByText(/This window runs at night — the first night publish needs the one-time acknowledgement/i),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: /Publish changes/i })).toBeDisabled();
    expect(screen.getByText(/No unsent changes/i)).toBeVisible();
  });
});

describe("ScheduleView — publishing", () => {
  function renderEditable(harness: Harness): void {
    renderView(harness);
  }

  async function makeDirty(card: HTMLElement): Promise<void> {
    await userEvent.type(within(card).getByLabelText("Name"), "Day charge");
    for (const unitId of ["lhs", "mid", "rhs"]) {
      await userEvent.type(within(card).getByLabelText(`Watts for ${unitId}`), "2500");
    }
  }

  it("publishes the whole list with the loaded version, in the per-battery form only", async () => {
    const put = vi.fn(() =>
      Promise.resolve(
        putScheduleOk({ version: 5, plan: planWith([scheduleEntry({ entry_id: "Day charge" })], 5), added: ["Day charge"] }),
      ),
    );
    const harness = installHarness({ put });
    renderEditable(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    const card = entryCards()[0]!;
    await makeDirty(card);

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    const body = publishBody(put);
    expect(body.expected_version).toBeNull(); // no plan exists: the first publish
    expect(body.timezone).toBe("Australia/Brisbane");
    const entries = body.entries as Record<string, unknown>[];
    expect(entries).toHaveLength(1);
    expect(entries[0]!.watts_by_unit).toEqual({ lhs: 2500, mid: 2500, rhs: 2500 });
    expect(entries[0]!.watts).toBeUndefined();
    expect(entries[0]!.entry_id).toBe("Day charge");
    expect(entries[0]!.action).toBe("charge");
    expect(entries[0]!.enabled).toBe(true);

    // Optimistic adoption of the 200: the stored plan becomes the draft base.
    await waitFor(() => {
      expect(screen.getByText(/Published plan v5/i)).toBeVisible();
    });
    expect(screen.getByText(/Published — Schedule v5: added Day charge/i)).toBeVisible();
    expect(screen.getByText(/No unsent changes/i)).toBeVisible();
  });

  it("publishes against the loaded plan's version (the CAS base)", async () => {
    const put = vi.fn(() =>
      Promise.resolve(putScheduleOk({ version: 5, plan: planWith([DAY_CHARGE_ENTRY]), changed: ["Day charge"] })),
    );
    const harness = installHarness({
      get: vi.fn(() => Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))),
      put,
    });
    renderEditable(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    const card = entryCards()[0]!;
    const lhs = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(lhs);
    await userEvent.type(lhs, "2000");

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const body = publishBody(put);
    expect(body.expected_version).toBe(4);
    const entries = body.entries as Record<string, unknown>[];
    expect(entries[0]!.watts_by_unit).toEqual({ lhs: 2000, mid: 2500, rhs: 2500 });
  });

  it("keeps the dialog closed until a refusal routes it (never pre-emptively)", async () => {
    const harness = installHarness({});
    renderEditable(harness);
    await screen.findByText(/No schedule published yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add a schedule/i }));
    await makeDirty(entryCards()[0]!);

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("routes the night acknowledgement refusal into the dialog and resends with the typed value", async () => {
    const put = vi
      .fn()
      .mockRejectedValueOnce(refusalError("night_posture_acknowledgement_required"))
      .mockResolvedValueOnce(
        Promise.resolve(
          putScheduleOk({ version: 5, plan: planWith([DAY_CHARGE_ENTRY]), acknowledged_night_windows: true }),
        ),
      );
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
            plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "22:30", end_local: "05:59" })]),
          }),
        ),
      ),
      put,
    });
    renderEditable(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    const card = entryCards()[0]!;
    const mid = within(card).getByLabelText("Watts for mid");
    await userEvent.clear(mid);
    await userEvent.type(mid, "2400");

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    const dialog = await screen.findByRole("dialog", { name: /Acknowledge the night window once/i });
    expect(within(dialog).getByText(/The external writer applications stand down for the granted window; the controller owns it/i)).toBeVisible();
    expect(within(dialog).getByText(/captured once and never asked again/i)).toBeVisible();
    expect(within(dialog).getByText("night_posture_acknowledgement_required")).toBeVisible();
    // Nothing resends until the operator types the exact acknowledgement.
    expect(within(dialog).getByRole("button", { name: /Publish with the acknowledgement/i })).toBeDisabled();

    await userEvent.type(within(dialog).getByLabelText(/Type PARTITION_ACKNOWLEDGED to publish/i), "PARTITION_ACKNOWLEDGED");
    await userEvent.click(within(dialog).getByRole("button", { name: /Publish with the acknowledgement/i }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    const second = publishBody(put, 1);
    expect(second.night_posture).toBe("PARTITION_ACKNOWLEDGED");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(screen.getByText(/Published — Schedule v5 — no entry changes/i)).toBeVisible();
  });

  it("answers a CAS conflict with reload-and-re-apply, never a silent merge", async () => {
    const put = vi.fn(() => Promise.reject(refusalError("schedule_version_conflict")));
    let reads = 0;
    const harness = installHarness({
      get: vi.fn(() => {
        reads += 1;
        return Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY], reads === 1 ? 4 : 5) }));
      }),
      put,
    });
    renderEditable(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    const card = entryCards()[0]!;
    const lhs = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(lhs);
    await userEvent.type(lhs, "2100");

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    expect(await screen.findByText(/The plan changed elsewhere — reload and re-apply/i)).toBeVisible();
    expect(screen.getByText("schedule_version_conflict")).toBeVisible();
    // The draft is never merged away: the edit is still on screen.
    expect(within(entryCards()[0]!).getByLabelText("Watts for lhs")).toHaveValue(2100);

    await userEvent.click(screen.getByRole("button", { name: /Reload the published plan/i }));
    await waitFor(() => {
      expect(screen.getByText(/Published plan v5/i)).toBeVisible();
    });
    // The operator's unsent edit survived the reload, exactly as promised.
    expect(within(entryCards()[0]!).getByLabelText("Watts for lhs")).toHaveValue(2100);
  });

  it("renders the window refusal verbatim with the offending entries", async () => {
    const put = vi.fn(() => Promise.reject(refusalError("schedule_window_not_allowed")));
    const harness = installHarness({
      get: vi.fn(() =>
        Promise.resolve(
          getScheduleOk({
            policy: schedulePolicy({ posture: "partition", allowed_windows_local: [["20:00", "06:00"]] }),
            plan: planWith([scheduleEntry({ entry_id: "Night Charge", start_local: "00:01", end_local: "05:59" })]),
          }),
        ),
      ),
      put,
    });
    renderEditable(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    const card = entryCards()[0]!;
    const lhs = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(lhs);
    await userEvent.type(lhs, "2400");

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    expect(await screen.findByText("schedule_window_not_allowed")).toBeVisible();
    expect(screen.getByText(/falls outside the allowed windows 06:00–20:00/i)).toBeVisible();
    expect(
      screen.getAllByText(/Night Charge \(00:01–05:59\) falls outside the allowed windows/i).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/trim the offending entries to the allowed windows, or make the partition choice in config/i)).toBeVisible();
    // The window refusal never opens the acknowledgement dialog.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("maps a 422's per-entry errors onto the offending row", async () => {
    const put = vi.fn(() =>
      Promise.reject(
        refusalError("validation_error", {
          // The shape the landed REST layer pins (rest.py replace_schedule).
          details: {
            errors: [
              { entry_id: "Day charge", field: "end_local", message: "window must outlast its start" },
            ],
          },
        }),
      ),
    );
    const harness = installHarness({
      get: vi.fn(() => Promise.resolve(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))),
      put,
    });
    renderEditable(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    const card = entryCards()[0]!;
    const lhs = within(card).getByLabelText("Watts for lhs");
    await userEvent.clear(lhs);
    await userEvent.type(lhs, "2400");

    await userEvent.click(screen.getByRole("button", { name: /Publish changes/i }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    expect(await within(entryCards()[0]!).findByText(/Server: end_local: window must outlast its start/i)).toBeVisible();
    expect(screen.getByText("validation_error")).toBeVisible();
  });
});

describe("ScheduleView — the live bus", () => {
  it("follows a schedule.replaced frame when the draft is clean, and keeps a dirty draft", async () => {
    const channel: { push: ((frame: StreamEvent) => void) | null } = { push: null };
    const openEvents = vi.fn(
      () =>
        (async function* live(): AsyncGenerator<StreamEvent, void, unknown> {
          let frame: StreamEvent | null = null;
          let wake: (() => void) | null = null;
          channel.push = (next: StreamEvent): void => {
            frame = next;
            wake?.();
          };
          while (true) {
            if (frame !== null) {
              const outgoing = frame;
              frame = null;
              yield outgoing;
            }
            await new Promise<void>((resolve) => {
              wake = resolve;
            });
            wake = null;
          }
        })(),
    );
    const remotePlan = planWith([scheduleEntry({ entry_id: "Remote edit", start_local: "08:00" })], 6);
    const get = vi
      .fn()
      .mockResolvedValueOnce(getScheduleOk({ plan: planWith([DAY_CHARGE_ENTRY]) }))
      .mockResolvedValue(getScheduleOk({ plan: remotePlan }));
    const harness = installHarness({ get, openEvents });
    renderView(harness);
    await waitFor(() => expect(entryCards()).toHaveLength(1));
    expect(within(entryCards()[0]!).getByDisplayValue("Day charge")).toBeVisible();

    // A clean draft follows the remote publish wholesale.
    channel.push?.(scheduleReplaced(43, { version: 6 }) as unknown as StreamEvent);
    await waitFor(() => {
      expect(within(entryCards()[0]!).getByDisplayValue("Remote edit")).toBeVisible();
    });

    // A dirty draft survives the next remote publish, with the base moved.
    const watts = within(entryCards()[0]!).getByLabelText("Watts for lhs");
    await userEvent.clear(watts);
    await userEvent.type(watts, "1900");
    channel.push?.(scheduleReplaced(44, { version: 7 }) as unknown as StreamEvent);
    await waitFor(() => {
      expect(screen.getByText(/The published plan changed elsewhere \(now v6\) — your unsent changes are still here/i)).toBeVisible();
    });
    expect(within(entryCards()[0]!).getByLabelText("Watts for lhs")).toHaveValue(1900);
  });
});
