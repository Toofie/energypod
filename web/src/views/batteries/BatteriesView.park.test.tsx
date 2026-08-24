/**
 * Behavior contract for the Batteries view's pod-parking surfaces
 * (DESIGN_POD_PARKING.md §7/§8, API_CONTRACTS.md "Pod parking"): the parked
 * chip and not-isolation banner (with the expiry promotion), the guarded park
 * dialog (fixed sentence, required reason, bounded lease select, type-back),
 * the resume dialog with its foreign-takeover acknowledgement step, the
 * post-resume checklist rendered inline, the wedge-signature recovery
 * advisory, and the delivery-bias evidence readout.
 *
 * WIRE TRUTH (web/src/test/wire.ts — the park family is PENDING-BACKEND, so
 * every fixture is the contract's own pinned shape): the snapshot unit
 * carries `park_state` only where the `parking:` block is commissioned (the
 * absent key is the feature detection); the routes' refusals are the typed
 * 409 envelopes `parkRefusalEnvelope` builds; the resume 200 carries the
 * `checklist` object. The client is mocked at its exact surface; rejections
 * are real ApiClientError values carrying the envelope verbatim.
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
  type StreamEvent,
} from "../../api/client";
import {
  auditPage,
  deliveryBias,
  emptyUnitDetail,
  parkOk,
  parkRefusalEnvelope,
  parkState,
  recoveryAdvisory,
  resumeChecklist,
  resumeOk,
  snapshot,
  snapshotFrame,
  telemetrySummary,
  unitDetail,
  unitSnapshot,
  withParkState,
  type WireParkState,
  type WireSnapshot,
  type WireUnitDetail,
  type WireUnitSnapshot,
} from "../../test/wire";
import { BatteriesView } from "./BatteriesView";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: vi.fn(),
}));

interface MockedClient {
  getSnapshot: Mock<() => Promise<WireSnapshot>>;
  getUnitDetail: Mock<(unitId: string) => Promise<WireUnitDetail>>;
  getAudit: Mock<(limit: number, afterSequence?: number) => Promise<AuditPage>>;
  postPark: Mock<
    (unitId: string, body: { reason: string; leaseS?: number }, idempotencyKey?: string) => Promise<Record<string, unknown>>
  >;
  postResume: Mock<
    (unitId: string, options?: { takeover?: boolean; idempotencyKey?: string }) => Promise<Record<string, unknown>>
  >;
  openEvents: Mock<(afterSequence?: number) => AsyncIterable<StreamEvent>>;
}

function makeClient(): MockedClient {
  const client: MockedClient = {
    getSnapshot: vi.fn<() => Promise<WireSnapshot>>(),
    getUnitDetail: vi.fn<(unitId: string) => Promise<WireUnitDetail>>(),
    getAudit: vi.fn<(limit: number, afterSequence?: number) => Promise<AuditPage>>(),
    postPark: vi.fn<
      (
        unitId: string,
        body: { reason: string; leaseS?: number },
        idempotencyKey?: string,
      ) => Promise<Record<string, unknown>>
    >(),
    postResume: vi.fn<
      (unitId: string, options?: { takeover?: boolean; idempotencyKey?: string }) => Promise<Record<string, unknown>>
    >(),
    openEvents: vi.fn<(afterSequence?: number) => AsyncIterable<StreamEvent>>(),
  };
  client.getAudit.mockResolvedValue(auditPage([]));
  client.getUnitDetail.mockImplementation((unitId: string) =>
    Promise.resolve(emptyUnitDetail(unitId)),
  );
  client.postPark.mockResolvedValue(parkOk());
  client.postResume.mockResolvedValue(resumeOk());
  return client;
}

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

/** The commissioned world: every unit carries a park_state (parked or not). */
function fleetWorld(rhsPark: WireParkState): WireSnapshot {
  return snapshot(
    [
      withParkState(
        unitSnapshot({ unit_id: "lhs", lifecycle: "disarmed", telemetry_age_s: 3 }),
        parkState({ parked: false }),
      ),
      withParkState(
        unitSnapshot({
          unit_id: "rhs",
          lifecycle: "disarmed",
          telemetry_age_s: 2,
          telemetry: telemetrySummary({
            soc_pct: 64,
            pack_voltage_v: 166.4,
            battery_watts: 0,
          }),
        }),
        rhsPark,
      ),
    ],
    { snapshot_sequence: 5000, captured_at: "2026-08-24T02:13:00+10:00" },
  );
}

/** A live operator lease with the given seconds left on the wall clock. */
function liveLease(secondsLeft: number): WireParkState {
  return parkState({
    lease_expires_at: new Date(Date.now() + secondsLeft * 1000).toISOString(),
  });
}

/** 20 s of slack so a floored H:MM figure is stable across render delay. */
const RENDER_SLACK_S = 20;

function refusalError(
  code: Parameters<typeof parkRefusalEnvelope>[0],
  overrides: { details?: Record<string, unknown> } = {},
): ApiClientError {
  const envelope = parkRefusalEnvelope(code, overrides);
  return new ApiClientError({
    status: envelope.status,
    code: envelope.code,
    message: envelope.message,
    details: envelope.details,
    request_id: envelope.request_id,
  });
}

function eventsOf(state: WireSnapshot): StreamEvent[] {
  return [snapshotFrame(state)];
}

function renderView(client: MockedClient) {
  vi.mocked(createApiClient).mockReturnValue(client as unknown as ApiClient);
  const api = createApiClient("operator-token");
  return render(<BatteriesView client={api} />);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("BatteriesView — the parked chip and not-isolation banner", () => {
  it("renders the chip and the pinned banner line with the H:MM countdown, the fixed sentence beside it", async () => {
    const client = makeClient();
    const world = fleetWorld(liveLease(3 * 3600 + 47 * 60 + RENDER_SLACK_S));
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);

    const card = await screen.findByRole("group", { name: "rhs" });
    const chip = within(card).getByText("Parked");
    expect(chip).not.toBeNull();
    // The tooltip names the mode word and the pack voltage — never a metaphor.
    expect(chip.getAttribute("title")).toBe(
      "mode word not available · pack 166.4 V — the battery stays connected at full voltage",
    );
    expect(
      within(card).getByText("Parked — not isolation · lease expires in 3:47"),
    ).not.toBeNull();
    expect(
      within(card).getByText(
        "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod. The lease countdown is policy, never safety.",
      ),
    ).not.toBeNull();
    // The commissioned card carries its affordance; the parked one offers Resume.
    expect(within(card).getByRole("button", { name: "Resume rhs" })).not.toBeNull();
    const lhs = screen.getByRole("group", { name: "lhs" });
    expect(within(lhs).queryByText("Parked")).toBeNull();
    expect(within(lhs).getByRole("button", { name: "Park lhs" })).not.toBeNull();
  });

  it("names the mode word in the tooltip when telemetry carries the debug word", async () => {
    const client = makeClient();
    const world = snapshot(
      [
        withParkState(
          unitSnapshot({
            unit_id: "rhs",
            lifecycle: "disarmed",
            telemetry_age_s: 2,
            telemetry: telemetrySummary({
              soc_pct: 64,
              pack_voltage_v: 166.4,
              battery_watts: 0,
              debug_mode_w: 1,
            }),
          }),
          liveLease(600),
        ),
      ],
      { snapshot_sequence: 5001 },
    );
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);

    const chip = await screen.findByText("Parked");
    expect(chip.getAttribute("title")).toBe(
      "mode word 1 (Standby) · pack 166.4 V — the battery stays connected at full voltage",
    );
  });

  it("promotes the banner to the alert styling and the pinned expiry wording when the lease reads expired", async () => {
    const client = makeClient();
    const world = fleetWorld(parkState({ expired: true }));
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);

    const card = await screen.findByRole("group", { name: "rhs" });
    const alert = within(card).getByRole("alert");
    expect(alert.textContent).toContain("Parked — not isolation · lease expired — Resume required");
    // The fixed sentence stays beside the alert wording.
    expect(alert.textContent).toContain(
      "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod.",
    );
  });

  it("gives write_unverified and foreign_rewrite their own honest words", async () => {
    const client = makeClient();
    const world = fleetWorld(
      parkState({ write_unverified: true, foreign_rewrite: true }),
    );
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);

    const card = await screen.findByRole("group", { name: "rhs" });
    expect(
      within(card).getByText(/The last park write could not be verified/i),
    ).not.toBeNull();
    expect(within(card).getByText(/Another writer parked over this lease/i)).not.toBeNull();
  });

  it("renders no parking surface at all while the projection is absent (not commissioned)", async () => {
    const client = makeClient();
    const world = snapshot([unitSnapshot({ unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 2 })], {
      snapshot_sequence: 5002,
    });
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);

    await screen.findByRole("group", { name: "rhs" });
    expect(screen.queryByText("Parked")).toBeNull();
    expect(screen.queryByRole("button", { name: /Park/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Resume/ })).toBeNull();
  });

  it("flips to parked when a LATER snapshot frame carries the park projection — never waits for a remount", async () => {
    const client = makeClient();
    const before = fleetWorld(parkState({ parked: false }));
    client.getSnapshot.mockResolvedValue(before);
    let pushFrame: ((frame: StreamEvent) => void) | null = null;
    client.openEvents.mockImplementation(
      () =>
        ({
          async *[Symbol.asyncIterator]() {
            yield snapshotFrame(before);
            const queued: StreamEvent[] = [];
            pushFrame = (frame: StreamEvent) => queued.push(frame);
            while (true) {
              if (queued.length > 0) {
                yield queued.shift()!;
              } else {
                await new Promise((resolve) => setTimeout(resolve, 5));
              }
            }
          },
        }) as AsyncIterable<StreamEvent>,
    );
    renderView(client);

    const card = await screen.findByRole("group", { name: "rhs" });
    expect(within(card).getByRole("button", { name: "Park rhs" })).not.toBeNull();

    // The world moves — a parked rhs arrives on the stream, the shared plane's
    // own snapshot cadence in production.
    const after = fleetWorld(liveLease(1800 + RENDER_SLACK_S));
    (after as { snapshot_sequence: number }).snapshot_sequence = 5003;
    await waitFor(() => expect(pushFrame).not.toBeNull());
    pushFrame!(snapshotFrame(after));

    await waitFor(() =>
      expect(within(card).getByText("Parked — not isolation · lease expires in 0:30")).not.toBeNull(),
    );
    expect(within(card).queryByRole("button", { name: "Park rhs" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Resume rhs" })).not.toBeNull();
  });
});

describe("BatteriesView — the guarded park dialog", () => {
  async function openParkDialog(client: MockedClient, world: WireSnapshot) {
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);
    const card = await screen.findByRole("group", { name: "rhs" });
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: "Park rhs" }));
    return user;
  }

  it("carries the fixed not-isolation sentence above the confirm and keeps confirm disabled until reason and type-back both land", async () => {
    const user = await openParkDialog(makeClient(), fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Park rhs" });
    expect(confirm).toBeDisabled();

    // The fixed sentence is present verbatim, and above the actions.
    expect(
      within(dialog).getByText(
        "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod.",
      ),
    ).not.toBeNull();
    expect(dialog.innerHTML.indexOf("Never perform physical work")).toBeGreaterThan(-1);

    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rh");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "s");
    expect(confirm).toBeEnabled();
  });

  it("pins the actions in a footer outside the scrollable body — Cancel/confirm never ride the fold", async () => {
    await openParkDialog(makeClient(), fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    const footer = dialog.querySelector(".dialog-footer");
    const body = dialog.querySelector(".dialog-body");
    expect(footer).not.toBeNull();
    expect(body).not.toBeNull();
    // The footer is a sibling of the body — nothing pinned lives inside the scroll.
    expect(footer!.contains(body!)).toBe(false);

    const actions = within(dialog)
      .getByRole("button", { name: "Park rhs" })
      .closest(".dialog-actions");
    expect(actions).not.toBeNull();
    expect(actions!.closest(".dialog-footer")).toBe(footer);
    expect(actions!.closest(".dialog-body")).toBeNull();

    // The fixed sentence rides the pinned footer too, immediately above the
    // actions row (the design contract's pin holds in the new structure).
    const sentence = within(dialog)
      .getByText(/Never perform physical work/)
      .closest(".park-fixed-sentence");
    expect(sentence!.parentElement).toBe(footer);
    expect(sentence!.nextElementSibling).toBe(actions);
    expect(body!.contains(sentence!)).toBe(false);
  });

  it("carries the unit id as the type-back field's placeholder", async () => {
    await openParkDialog(makeClient(), fleetWorld(parkState({ parked: false })));
    expect(
      screen.getByLabelText("Type rhs to enable park").getAttribute("placeholder"),
    ).toBe("rhs");
  });

  it("names exactly what is missing in one hint line pinned above the actions — never while pending", async () => {
    const client = makeClient();
    const user = await openParkDialog(client, fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    const hint = () => dialog.querySelector(".dialog-hint");

    // Nothing entered (the lease is chosen by default): both halves named.
    expect(hint()!.textContent).toBe("Reason required · type rhs to enable park.");
    // The hint rides the pinned footer, directly above the fixed sentence
    // and the actions — visible at any content height.
    expect(hint()!.parentElement).toBe(dialog.querySelector(".dialog-footer"));

    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    expect(hint()!.textContent).toBe("Type rhs to enable park.");

    // A mid-typing type-back is still a mismatch: the reason alone drops out.
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rh");
    expect(hint()!.textContent).toBe("Type rhs to enable park.");
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "s");
    expect(hint()).toBeNull();

    // A held write is the wire's own state, never a missing input: no hint.
    let release: ((value: Record<string, unknown>) => void) | undefined;
    client.postPark.mockImplementation(
      () =>
        new Promise<Record<string, unknown>>((resolve) => {
          release = resolve;
        }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Park rhs" }));
    expect(within(dialog).getByRole("button", { name: "Park rhs" })).toBeDisabled();
    expect(hint()).toBeNull();
    release!(parkOk());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("names the lease too when the site offers no budget at all", async () => {
    await openParkDialog(
      makeClient(),
      fleetWorld(parkState({ parked: false, max_total_s: 0, remaining_cap_s: 0 })),
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector(".dialog-hint")!.textContent).toBe(
      "Reason required · lease duration required · type rhs to enable park.",
    );
    // The body's own honest line stands beside the hint.
    expect(
      within(dialog).getByText("No lease budget is available for this battery right now."),
    ).not.toBeNull();
  });

  it("bounds the lease select to the site's budget and sends the typed confirmation with the chosen lease", async () => {
    const client = makeClient();
    const user = await openParkDialog(client, fleetWorld(parkState({ parked: false, max_total_s: 7200, remaining_cap_s: 7200 })));
    const dialog = screen.getByRole("dialog");
    const select = within(dialog).getByLabelText("Lease duration") as HTMLSelectElement;
    const options = Array.from(select.options).map((option) => option.value);
    expect(options).not.toContain("14400"); // above the site's 2 h cap
    expect(options).toContain("7200");

    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rhs");
    await user.selectOptions(select, "3600");
    await user.click(within(dialog).getByRole("button", { name: "Park rhs" }));

    await waitFor(() => expect(client.postPark).toHaveBeenCalled());
    const [unitId, body] = client.postPark.mock.calls[0]!;
    expect(unitId).toBe("rhs");
    expect(body).toEqual({ reason: "evening standby", leaseS: 3600 });
  });

  it("closes on the 200 and re-reads the world — the chip arrives from the server, never optimistic state", async () => {
    const client = makeClient();
    const user = await openParkDialog(client, fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rhs");
    await user.click(within(dialog).getByRole("button", { name: "Park rhs" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(client.getSnapshot.mock.calls.length).toBeGreaterThan(1));
  });

  it("renders a refusal's plain sentence beside the envelope verbatim, and the dialog stays open", async () => {
    const client = makeClient();
    client.postPark.mockRejectedValue(refusalError("park_conflict_refused"));
    const user = await openParkDialog(client, fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rhs");
    await user.click(within(dialog).getByRole("button", { name: "Park rhs" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("rhs — armed");
    expect(alert.textContent).toContain("park_conflict_refused");
    expect(alert.textContent).toContain(
      "The unit is not in a state where parking is permitted.",
    );
    expect(screen.getByRole("dialog")).not.toBeNull();
  });

  it("names the not-commissioned refusal honestly when the route answers it", async () => {
    const client = makeClient();
    client.postPark.mockRejectedValue(refusalError("park_not_commissioned"));
    const user = await openParkDialog(client, fleetWorld(parkState({ parked: false })));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Reason (required)"), "evening standby");
    await user.type(within(dialog).getByLabelText("Type rhs to enable park"), "rhs");
    await user.click(within(dialog).getByRole("button", { name: "Park rhs" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("the parking config block is absent");
  });
});

describe("BatteriesView — the resume dialog and the after-park checklist", () => {
  async function openResumeDialog(client: MockedClient, world: WireSnapshot) {
    client.getSnapshot.mockResolvedValue(world);
    client.openEvents.mockReturnValue(openStream(eventsOf(world)));
    renderView(client);
    const card = await screen.findByRole("group", { name: "rhs" });
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: "Resume rhs" }));
    return user;
  }

  it("resumes our own lease with a plain confirm, then renders the checklist inline", async () => {
    const client = makeClient();
    const user = await openResumeDialog(client, fleetWorld(liveLease(600)));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Resume rhs" }));

    await waitFor(() => expect(client.postResume).toHaveBeenCalled());
    expect(client.postResume.mock.calls[0]![0]).toBe("rhs");
    expect(client.postResume.mock.calls[0]![1]).toEqual({});
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const checklist = screen.getByRole("status", { name: "rhs resumed — after-park checklist" });
    expect(checklist.textContent).toContain("Communications age: 2 seconds");
    expect(checklist.textContent).toContain("Charge level drift: -0.6% since park (64% at park)");
    expect(checklist.textContent).toContain("Power now: 0 W");
    expect(checklist.textContent).toContain("Faults while parked: none recorded");
    expect(checklist.textContent).toContain(
      "faults observed while parked were retained; the fault registers are the record",
    );
    expect(checklist.textContent).toContain("No latched stops hold this battery.");
  });

  it("keeps null checklist figures honest — not available, never zero", async () => {
    const client = makeClient();
    client.postResume.mockResolvedValue(
      resumeOk({ checklist: resumeChecklist({ comms_age_s: null, soc_drift_pct: null, measured_watts_now: null, faults_while_parked: null }) }),
    );
    const user = await openResumeDialog(client, fleetWorld(liveLease(600)));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume rhs" }));
    const checklist = await screen.findByRole("status", { name: "rhs resumed — after-park checklist" });
    expect(checklist.textContent).toContain("Communications age: not available");
    expect(checklist.textContent).toContain("Charge level drift: not available");
    expect(checklist.textContent).toContain("Power now: not available");
    expect(checklist.textContent).toContain("Faults while parked: not available");
  });

  it("renders the pinned latched-stops line per stop and the inhibit note", async () => {
    const client = makeClient();
    client.postResume.mockResolvedValue(
      resumeOk({
        checklist: resumeChecklist({
          latched_stops: ["stop-7"],
          latched_inhibit: true,
        }),
      }),
    );
    const user = await openResumeDialog(client, fleetWorld(liveLease(600)));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume rhs" }));
    const checklist = await screen.findByRole("status", { name: "rhs resumed — after-park checklist" });
    expect(
      within(checklist).getByText("remains stopped by stop-7 — acknowledge separately"),
    ).not.toBeNull();
    expect(checklist.textContent).toContain("An inhibit latch also holds");
  });

  it("grows the takeover acknowledgement step for a foreign park and sends takeover only once confirmed", async () => {
    const client = makeClient();
    const user = await openResumeDialog(
      client,
      fleetWorld(parkState({ origin: "foreign" })),
    );
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Resume rhs" });
    expect(confirm).toBeDisabled();
    expect(
      within(dialog).getByText(/parked by another writer|parked, with no recorded lease/i),
    ).not.toBeNull();
    await user.click(within(dialog).getByRole("checkbox"));
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(client.postResume).toHaveBeenCalled());
    expect(client.postResume.mock.calls[0]![1]).toEqual({ takeover: true });
  });

  it("pins its actions beside the takeover hint — the one enable condition named until it lands", async () => {
    const client = makeClient();
    const user = await openResumeDialog(client, fleetWorld(parkState({ origin: "foreign" })));
    const dialog = screen.getByRole("dialog");
    const footer = dialog.querySelector(".dialog-footer");
    expect(footer).not.toBeNull();

    // The actions row lives in the pinned footer, never the scrollable body.
    const actions = within(dialog)
      .getByRole("button", { name: "Resume rhs" })
      .closest(".dialog-actions");
    expect(actions!.closest(".dialog-footer")).toBe(footer);
    expect(actions!.closest(".dialog-body")).toBeNull();

    // The grayed confirm names exactly what is missing, in the footer.
    const hint = dialog.querySelector(".dialog-hint");
    expect(hint!.textContent).toBe("Acknowledge the takeover to enable resume.");
    expect(hint!.parentElement).toBe(footer);

    await user.click(within(dialog).getByRole("checkbox"));
    expect(dialog.querySelector(".dialog-hint")).toBeNull();
  });

  it("routes the takeover step from the 409 itself when the projection did not predict it", async () => {
    const client = makeClient();
    client.postResume
      .mockRejectedValueOnce(refusalError("park_foreign_word_acknowledgement_required"))
      .mockResolvedValue(resumeOk());
    const user = await openResumeDialog(client, fleetWorld(liveLease(600)));
    const dialog = screen.getByRole("dialog");
    // Our own lease: no acknowledgement step on open.
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Resume rhs" }));

    // The refusal IS the routing: the step grows, gated by its checkbox.
    await screen.findByText(/no lease from this controller/i);
    const checkbox = screen.getByRole("checkbox");
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume rhs" })).toBeDisabled();
    await user.click(checkbox);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume rhs" }));
    await waitFor(() => expect(client.postResume).toHaveBeenCalledTimes(2));
    expect(client.postResume.mock.calls[1]![1]).toEqual({ takeover: true });
  });

  it("renders the resume_stop_latched refusal with its stop ids and stays open", async () => {
    const client = makeClient();
    client.postResume.mockRejectedValue(refusalError("resume_stop_latched"));
    const user = await openResumeDialog(client, fleetWorld(liveLease(600)));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume rhs" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("stop-7");
    expect(alert.textContent).toContain("Acknowledge the stop first");
    expect(screen.getByRole("dialog")).not.toBeNull();
  });
});

describe("BatteriesView — the recovery advisory and the delivery-bias evidence", () => {
  /**
   * The summary-tab world: the detail read carries the advisory and/or the
   * bias window; `snapshotPark` controls whether the site's park projection
   * speaks on the snapshot (the commissioning detection the advisory keys on).
   */
  function advisoryWorld(options: {
    advisory: boolean;
    bias: boolean;
    snapshotPark: boolean;
  }): { world: WireSnapshot; detail: WireUnitDetail } {
    const commissioned = fleetWorld(parkState({ parked: false }));
    const world: WireSnapshot = options.snapshotPark
      ? commissioned
      : {
          // A not-commissioned site carries the key on NO unit — the honest
          // world the advisory's unavailable state answers.
          ...commissioned,
          units: commissioned.units.map((unit) => {
            const { park_state: _park, ...rest } = unit;
            return rest as WireUnitSnapshot;
          }),
        };
    const detail: WireUnitDetail = unitDetail("rhs", {
      ...(options.advisory ? { recovery_advisory: recoveryAdvisory() } : {}),
      ...(options.bias ? { delivery_bias: deliveryBias() } : {}),
    });
    return { world, detail };
  }

  async function openSummary(client: MockedClient, seeded: { world: WireSnapshot; detail: WireUnitDetail }) {
    client.getSnapshot.mockResolvedValue(seeded.world);
    client.getUnitDetail.mockImplementation((unitId: string) =>
      unitId === "rhs" ? Promise.resolve(seeded.detail) : Promise.resolve(emptyUnitDetail(unitId)),
    );
    client.openEvents.mockReturnValue(openStream(eventsOf(seeded.world)));
    renderView(client);
    const card = await screen.findByRole("group", { name: "rhs" });
    await userEvent.click(within(card).getByRole("button", { name: "rhs" }));
    await screen.findByRole("tablist");
  }

  it("renders the three-step walkthrough with the operator-at-the-pod rule where parking is commissioned", async () => {
    const client = makeClient();
    await openSummary(client, advisoryWorld({ advisory: true, bias: false, snapshotPark: true }));
    const advisory = screen.getByRole("note");
    expect(advisory.textContent).toContain("Soft recovery available: the park/resume cycle");
    expect(advisory.textContent).toContain("Disarm the battery.");
    expect(advisory.textContent).toContain("Park it");
    expect(advisory.textContent).toContain("Resume it");
    expect(advisory.textContent).toContain("Keep the operator at the pod the first time per unit.");
    expect(advisory.textContent).toContain("foreign writer wrote during the park");
    expect(advisory.textContent).toContain("Advisory-only");
  });

  it("renders the advisory UNAVAILABLE — never a suggestion — while parking is not commissioned", async () => {
    const client = makeClient();
    await openSummary(client, advisoryWorld({ advisory: true, bias: false, snapshotPark: false }));
    const advisory = screen.getByRole("note");
    expect(advisory.textContent).toContain("parking is not commissioned on this site");
    expect(advisory.textContent).toContain("cannot be executed from this console");
    expect(advisory.textContent).toContain("physical-restart checklist stands");
    // The walkthrough steps never render in the unavailable state.
    expect(advisory.textContent).not.toContain("Disarm the battery.");
  });

  it("renders the delivery-bias evidence with the evidence-only label, never a warning", async () => {
    const client = makeClient();
    await openSummary(client, advisoryWorld({ advisory: false, bias: true, snapshotPark: true }));
    const readout = screen.getByRole("note");
    expect(readout.textContent).toContain("Delivery bias (evidence-only)");
    expect(readout.textContent).toContain("mean 15.4%, max 16.1%");
    expect(readout.textContent).toContain("212 samples over the last 15 min");
    expect(readout.textContent).toContain("no control decision reads it");
    expect(readout.getAttribute("role")).toBe("note");
  });

  it("renders neither when the detail read carries neither", async () => {
    const client = makeClient();
    await openSummary(client, advisoryWorld({ advisory: false, bias: false, snapshotPark: true }));
    expect(screen.queryByText(/Delivery bias/)).toBeNull();
    expect(screen.queryByText(/Soft recovery/)).toBeNull();
  });
});
