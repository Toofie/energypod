// Activity view contract suite (docs/UI_CONTRACTS.md, "Activity" and
// "State and error contract").
//
// WIRE TRUTH: every audit fixture is built by web/src/test/wire.ts from the
// service's own read model (service.py `recent_audit` over the sequenced
// AuditEvent projection). On the wire:
//   - the kind key is `event_type` — control_decision, intent_accepted,
//     unit_armed, unit_disarmed, emergency_stop, stop_acknowledged,
//     inhibit_acknowledged, authorization_revoked — there is no `type` field
//     and no observation entry (observations are published on the event bus,
//     never audited);
//   - `principal` is the caller's subject string, shown verbatim as "who";
//   - the outcome is `result` (authorized/clamped/rejected/... plus the
//     facade's accepted/armed/disarmed/latched/acknowledged/revoked);
//   - the watt figures are the signed `requested_active_w` and
//     `authorized_active_w` (charge is negative on the wire);
//   - `reason_codes` are the service's own codes (power_clamped,
//     telemetry_stale, latched, latch_cleared, ...);
//   - only the per-unit mutations (unit_armed, unit_disarmed,
//     inhibit_acknowledged) carry a `unit_id`; fleet-wide decisions and stops
//     carry none and are never claimed for a unit.
//
// Pinned surface strings an implementation must render (calm household tone,
// plain language first, raw codes on demand):
//   - heading "Activity"; entries as list items, newest first by `sequence`
//   - pagination button "Load more"; it passes the previous page's
//     next_cursor as the afterSequence argument of client.getAudit and
//     disappears when next_cursor is null
//   - per control entry: the principal subject verbatim (who), the decided /
//     happened / why lines below, and a "Show technical detail" disclosure
//     revealing the raw reason codes; a native <details> is legal - before
//     the disclosure the raw code must be not VISIBLE, not absent from the
//     DOM
//   - decision language: result "clamped" with the wire's watt figures ->
//     "Reduced to 1500 W of the 3000 W requested"; reason power_clamped ->
//     "Power clamped"; a missing result -> "Result not recorded yet" (never
//     a fabricated measurement)
//   - kind chips "Observations", "Decisions", "Arming", "Stops",
//     "Acknowledgements" with the wire mapping control_decision ->
//     Decisions, unit_armed AND unit_disarmed -> Arming, emergency_stop ->
//     Stops, *_acknowledged -> Acknowledgements; the Observations chip is
//     contracted and selects an empty set today (the audit trail holds no
//     observation entries), which pins the filtered-empty state; unit chips
//     "All units", "MID", "RHS", "LHS" where "All units" resets the unit
//     filter; every chip is a toggle button exposing aria-pressed across its
//     whole lifecycle and keyboard operable
//   - states: loading role "status" named "Loading activity" renders skeleton
//     placeholder entries INSIDE the status region (never a spinner-only
//     region) that carry no data; empty "Nothing here yet" + what appears
//     here + first step (Now view); filtered empty "No activity matches
//     these filters"; EVERY rendered error envelope carries its code,
//     message, AND request_id verbatim with a "Try again" retry; disconnected
//     shows "Connection lost" while the already-loaded entries stay visible
//     - now with their age - with "Try again" for the manual REST retry;
//     stale entries show their age ("2 hours ago") next to the entry itself
//     instead of hiding it
//
// The audit REST envelope is { events, next_cursor } and error envelopes are
// { code, message, details, request_id } (docs/API_CONTRACTS.md). Rejections
// are thrown as the client's ApiClientError carrying the envelope verbatim
// plus the HTTP status (docs: src/api/client.ts "TypedError pin").
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { auditEvent, auditPage, type WireAuditEvent } from "../../test/wire";
import { ActivityView } from "./ActivityView";

// Only createApiClient is replaced; the rest of the client module (notably
// ApiClientError itself) passes through, so a conforming view may import the
// client's documented error API and the rejections below are the exact
// TypedError instances production would throw.
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, createApiClient: vi.fn() };
});

const PAGE_SIZE = 50;

const minutesAgo = (m: number) =>
  new Date(Date.now() - m * 60_000).toISOString();

/** A canonical principal subject (rest.py `_ID_PATTERN` grammar). */
const PRINCIPAL = "operator.sam";

// Wire fixtures, newest first exactly as the API returns a page. The facade
// audits fleet-wide decisions and stops without a unit id; only the per-unit
// mutations carry one.
const clampedDecision = auditEvent({
  sequence: 60,
  event_type: "control_decision",
  unit_id: null,
  occurred_at: minutesAgo(5),
  principal: PRINCIPAL,
  result: "clamped",
  reason_codes: ["power_clamped"],
  requested_active_w: 3000,
  authorized_active_w: 1500,
});

const authorizedDecision = auditEvent({
  sequence: 60,
  event_type: "control_decision",
  unit_id: null,
  occurred_at: minutesAgo(2),
  principal: PRINCIPAL,
  result: "authorized",
  reason_codes: ["safety_checks_passed"],
  requested_active_w: 1000,
  authorized_active_w: 1000,
});

const emergencyStop = auditEvent({
  sequence: 61,
  event_type: "emergency_stop",
  unit_id: null,
  occurred_at: minutesAgo(1),
  principal: PRINCIPAL,
  intent_id: "stop-17",
  result: "latched",
  reason_codes: ["latched"],
});

const stopAcknowledgement = auditEvent({
  sequence: 62,
  event_type: "stop_acknowledged",
  unit_id: null,
  occurred_at: minutesAgo(0.5),
  principal: PRINCIPAL,
  intent_id: "stop-17",
  result: "acknowledged",
  reason_codes: ["acknowledged"],
});

const inhibitAcknowledgementMID = auditEvent({
  sequence: 57,
  event_type: "inhibit_acknowledged",
  unit_id: "MID",
  occurred_at: minutesAgo(30),
  principal: PRINCIPAL,
  result: "acknowledged",
  reason_codes: ["latch_cleared"],
});

const armLHS = auditEvent({
  sequence: 58,
  event_type: "unit_armed",
  unit_id: "LHS",
  occurred_at: minutesAgo(90),
  principal: PRINCIPAL,
  result: "armed",
  reason_codes: ["armed"],
});

const disarmLHS = auditEvent({
  sequence: 59,
  event_type: "unit_disarmed",
  unit_id: "LHS",
  occurred_at: minutesAgo(95),
  principal: PRINCIPAL,
  result: "disarmed",
  reason_codes: ["disarmed"],
});

let client: ApiClient;

const makeClient = (): ApiClient => ({
  getSnapshot: vi.fn(),
  getHealth: vi.fn(),
  getAudit: vi.fn(),
  postIntent: vi.fn(),
  postArm: vi.fn(),
  postDisarm: vi.fn(),
  postEmergencyStop: vi.fn(),
  postStopAcknowledgement: vi.fn(),
  postInhibitAcknowledgement: vi.fn(),
  openEvents: vi.fn(async function* () {}),
});

const renderView = (connection?: "connected" | "disconnected") =>
  connection === undefined ? (
    render(<ActivityView client={client} />)
  ) : (
    render(<ActivityView client={client} connection={connection} />)
  );

/** The units named by the visible entries, in order; an entry with no unit
 * (a fleet-wide decision or stop) contributes null. */
const unitOrder = () =>
  screen
    .getAllByRole("listitem")
    .map((item) => (item.textContent ?? "").match(/MID|RHS|LHS/)?.[0] ?? null);

/** Drop one field from a wire entry: the view must name it missing, never
 * invent a value for it. */
function withoutField(entry: WireAuditEvent, field: string): WireAuditEvent {
  const mutable = entry as unknown as Record<string, unknown>;
  delete mutable[field];
  return mutable as unknown as WireAuditEvent;
}

beforeEach(() => {
  client = makeClient();
  vi.mocked(createApiClient).mockReset();
  vi.mocked(createApiClient).mockReturnValue(client);
});

describe("Activity view", () => {
  it("renders a healthy newest-first timeline with who requested each entry", async () => {
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([authorizedDecision, disarmLHS, armLHS], 58));

    renderView();

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeVisible();

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(unitOrder()).toEqual([null, "LHS", "LHS"]);

    // Who requested it is shown on the newest entry: the audit record's own
    // principal subject, verbatim.
    expect(
      within(items[0] as HTMLElement).getByText("operator.sam"),
    ).toBeVisible();

    // More history is available.
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    expect(client.getAudit).toHaveBeenCalledTimes(1);
    expect(client.getAudit).toHaveBeenCalledWith(PAGE_SIZE);
  });

  it("shows what was decided, what happened, and why, with raw codes on demand", async () => {
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([clampedDecision], null));

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    const entry = within(items[0] as HTMLElement);

    // Who / what was decided / what happened / why, in plain language first,
    // mapped from the wire's result and the signed active-watt figures.
    expect(entry.getByText("operator.sam")).toBeVisible();
    expect(
      entry.getByText("Reduced to 1500 W of the 3000 W requested"),
    ).toBeVisible();
    expect(entry.getByText("Power reduced by the safety system")).toBeVisible();
    expect(entry.getByText("Power clamped")).toBeVisible();

    // Raw codes are not visible until asked for. A closed native <details>
    // satisfies this: text queries match hidden text, so the pin is
    // visibility, never DOM presence.
    const hiddenCode = entry.queryByText("power_clamped");
    if (hiddenCode) {
      expect(hiddenCode).not.toBeVisible();
    }

    const user = userEvent.setup();
    await user.click(entry.getByRole("button", { name: "Show technical detail" }));

    expect(entry.getByText("power_clamped")).toBeVisible();
  });

  it("paginates with the audit cursor and stops when the cursor is null", async () => {
    const getAudit = vi
      .fn()
      .mockResolvedValueOnce(auditPage([authorizedDecision, disarmLHS], 59))
      .mockResolvedValueOnce(auditPage([armLHS], null));
    client.getAudit = getAudit;

    renderView();

    expect(await screen.findAllByRole("listitem")).toHaveLength(2);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Load more" }));

    // Load more passes the previous page's next_cursor as the cursor.
    expect(getAudit).toHaveBeenNthCalledWith(1, PAGE_SIZE);
    expect(getAudit).toHaveBeenNthCalledWith(2, PAGE_SIZE, 59);

    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(3);
    });
    // Older entries are appended below, newest-first order preserved.
    expect(unitOrder()).toEqual([null, "LHS", "LHS"]);

    // A null cursor means the end of the timeline: no further load offered.
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
    expect(getAudit).toHaveBeenCalledTimes(2);
  });

  it("maps every audit kind to its chip, keeps unit-less entries out of unit filters, and resets with All units", async () => {
    client.getAudit = vi.fn().mockResolvedValue(
      auditPage(
        [
          stopAcknowledgement,
          emergencyStop,
          clampedDecision,
          disarmLHS,
          armLHS,
          inhibitAcknowledgementMID,
        ],
        null,
      ),
    );

    renderView();

    expect(await screen.findAllByRole("listitem")).toHaveLength(6);
    expect(unitOrder()).toEqual([null, null, null, "LHS", "LHS", "MID"]);

    // Every contracted chip exists as a button and, with no filter active,
    // every toggle starts honestly unpressed.
    for (const name of [
      "Observations",
      "Decisions",
      "Arming",
      "Stops",
      "Acknowledgements",
      "MID",
      "RHS",
      "LHS",
    ]) {
      const chip = screen.getByRole("button", { name });
      expect(chip).toBeVisible();
      expect(chip).toHaveAttribute("aria-pressed", "false");
    }
    expect(screen.getByRole("button", { name: "All units" })).toBeVisible();

    const user = userEvent.setup();

    // Each kind chip, keyboard-activated on its own, keeps exactly that
    // kind's entries: arm AND disarm both map to Arming, a stop
    // acknowledgement is an Acknowledgement (never a Stop), and the
    // Observations chip selects the empty set today because the audit trail
    // holds no observation entries.
    const expectedByKind = [
      ["Observations", 0],
      ["Decisions", 1],
      ["Arming", 2],
      ["Stops", 1],
      ["Acknowledgements", 2],
    ] as const;
    for (const [name, expected] of expectedByKind) {
      const chip = screen.getByRole("button", { name });
      chip.focus();
      await user.keyboard("{Enter}");
      expect(chip).toHaveAttribute("aria-pressed", "true");
      if (expected === 0) {
        expect(screen.queryAllByRole("listitem")).toHaveLength(0);
        expect(screen.getByText("No activity matches these filters")).toBeVisible();
      } else {
        expect(screen.getAllByRole("listitem")).toHaveLength(expected);
      }
      chip.focus();
      await user.keyboard("{Enter}");
      expect(chip).toHaveAttribute("aria-pressed", "false");
      expect(screen.getAllByRole("listitem")).toHaveLength(6);
    }

    // Unit chips narrow the timeline to that unit's own entries; a fleet-wide
    // decision or stop is never claimed for a unit it does not name.
    const lhsChip = screen.getByRole("button", { name: "LHS" });
    lhsChip.focus();
    await user.keyboard("{Enter}");
    expect(lhsChip).toHaveAttribute("aria-pressed", "true");
    expect(unitOrder()).toEqual(["LHS", "LHS"]);

    const midChip = screen.getByRole("button", { name: "MID" });
    midChip.focus();
    await user.keyboard("{Enter}");
    expect(unitOrder()).toEqual(["MID"]);

    const rhsChip = screen.getByRole("button", { name: "RHS" });
    rhsChip.focus();
    await user.keyboard("{Enter}");
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.getByText("No activity matches these filters")).toBeVisible();

    // "All units" resets the unit filter: every entry returns and the unit
    // toggles go back to unpressed.
    const allUnitsChip = screen.getByRole("button", { name: "All units" });
    allUnitsChip.focus();
    await user.keyboard("{Enter}");
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
    expect(rhsChip).toHaveAttribute("aria-pressed", "false");
  });

  it("filters by kind and unit using the keyboard only", async () => {
    client.getAudit = vi.fn().mockResolvedValue(
      auditPage([emergencyStop, clampedDecision, { ...armLHS, unit_id: "MID" }], null),
    );

    renderView();

    expect(await screen.findAllByRole("listitem")).toHaveLength(3);

    const user = userEvent.setup();

    // Keyboard-only activation of the kind filter, unpressed before use.
    const stopsChip = screen.getByRole("button", { name: "Stops" });
    expect(stopsChip).toHaveAttribute("aria-pressed", "false");
    stopsChip.focus();
    await user.keyboard("{Enter}");

    expect(stopsChip).toHaveAttribute("aria-pressed", "true");
    const onlyStop = screen.getAllByRole("listitem");
    expect(onlyStop).toHaveLength(1);
    expect((onlyStop[0] as HTMLElement).textContent).toContain("Emergency stop");

    // Narrow further by unit, still keyboard only: the stop is fleet-wide on
    // the wire (no unit id), so it cannot be claimed for MID.
    const midChip = screen.getByRole("button", { name: "MID" });
    midChip.focus();
    await user.keyboard("{Enter}");

    expect(midChip).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByText("No activity matches these filters"),
    ).toBeVisible();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);

    // Toggling the unit filter off restores the matching entry.
    midChip.focus();
    await user.keyboard("{Enter}");
    expect(midChip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("renders the wire's own vocabulary in words: an unknown event type never crashes and a missing result is named", async () => {
    // A type the service has not taught the console yet, plus an entry whose
    // result never arrived: both render defensively, neither fabricates.
    const unknown = auditEvent({
      sequence: 71,
      event_type: "maintenance_window",
      unit_id: "RHS",
      occurred_at: minutesAgo(3),
      principal: PRINCIPAL,
    });
    const missingResult = withoutField(
      auditEvent({
        sequence: 70,
        event_type: "control_decision",
        unit_id: null,
        occurred_at: minutesAgo(10),
        principal: PRINCIPAL,
        requested_active_w: 1200,
        authorized_active_w: 800,
        reason_codes: ["safety_checks_passed"],
      }),
      "result",
    );
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([unknown, missingResult], null));

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);

    // The newest entry is the unknown-but-named type, in plain words.
    expect(
      within(items[0] as HTMLElement).getByText("Maintenance window"),
    ).toBeVisible();

    // The decision without a result says so and never invents a measurement.
    const decision = within(items[1] as HTMLElement);
    expect(decision.getByText("Result not recorded yet")).toBeVisible();
    expect(decision.queryByText(/delivering/i)).toBeNull();
    expect(screen.queryByText(/^0 W$/)).toBeNull();
  });

  it("shows an empty state that explains what will appear here and the first step", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));

    renderView();

    expect(await screen.findByText("Nothing here yet")).toBeVisible();
    expect(screen.getByText(/will appear here/)).toBeVisible();
    expect(screen.getByText(/Now view/)).toBeVisible();

    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("shows a skeleton of placeholder entries inside the loading status, never a spinner alone", async () => {
    client.getAudit = vi.fn().mockReturnValue(new Promise(() => {}));

    renderView();

    const status = screen.getByRole("status", { name: "Loading activity" });
    expect(status).toBeVisible();

    // The skeleton is structural: placeholder rows stand in for timeline
    // entries inside the status region, bounded by the page size. The
    // placeholders appear once the outstanding fetch outlives the current
    // tick (the standard anti-flash pattern), so the query waits for them.
    const placeholders = await within(status).findAllByRole("listitem");
    expect(placeholders.length).toBeGreaterThanOrEqual(1);
    expect(placeholders.length).toBeLessThanOrEqual(PAGE_SIZE);

    // Placeholders are not data: nothing has loaded, so they never name a unit.
    expect(within(status).queryByText(/MID|RHS|LHS/)).toBeNull();

    // No real entry renders outside the skeleton while loading.
    const outsideSkeleton = screen
      .getAllByRole("listitem")
      .filter((item) => !status.contains(item));
    expect(outsideSkeleton).toHaveLength(0);
  });

  it("renders the error envelope verbatim and recovers via the manual retry", async () => {
    const getAudit = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiClientError({
          status: 503,
          code: "AUDIT_UNAVAILABLE",
          message: "Audit storage is not responding",
          details: null,
          request_id: "req-9z8y",
        }),
      )
      .mockResolvedValueOnce(auditPage([authorizedDecision], null));
    client.getAudit = getAudit;

    renderView();

    // The rendered envelope is verbatim, request_id included: the operator
    // can quote it back for diagnosis.
    expect(await screen.findByText("AUDIT_UNAVAILABLE")).toBeVisible();
    expect(screen.getByText("Audit storage is not responding")).toBeVisible();
    expect(screen.getByText("req-9z8y")).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findAllByRole("listitem")).toHaveLength(1);
    expect(getAudit).toHaveBeenCalledTimes(2);
  });

  it("keeps the loaded timeline visible under a disconnected notice with a manual retry", async () => {
    client.getAudit = vi.fn().mockResolvedValue(
      auditPage(
        [{ ...authorizedDecision, occurred_at: minutesAgo(125) }, disarmLHS],
        null,
      ),
    );

    renderView("disconnected");

    expect(await screen.findByText("Connection lost")).toBeVisible();
    expect(
      screen.getByText(/showing the activity we have/),
    ).toBeVisible();

    // Last data stays on screen, dimmed but not hidden - and it carries its
    // age: the retained newest entry is stale and says so next to its values.
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(
      within(items[0] as HTMLElement).getByText("operator.sam"),
    ).toBeVisible();
    expect(
      within(items[0] as HTMLElement).getByText("2 hours ago"),
    ).toBeVisible();

    // REST retry is manual while disconnected.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(client.getAudit).toHaveBeenCalledTimes(2);
  });

  it("shows the age next to a stale entry instead of hiding it", async () => {
    client.getAudit = vi.fn().mockResolvedValue(
      auditPage([{ ...authorizedDecision, occurred_at: minutesAgo(125) }], null),
    );

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);

    // The age belongs to the stale entry itself, not to a page-level banner.
    const stale = within(items[0] as HTMLElement);
    expect(stale.getByText("2 hours ago")).toBeVisible();
    expect(stale.getByText("Power decision")).toBeVisible();
  });

  it("surfaces a refused Load more verbatim with the envelope code, message, and request id", async () => {
    const getAudit = vi
      .fn()
      .mockResolvedValueOnce(auditPage([authorizedDecision], 60))
      .mockRejectedValueOnce(
        new ApiClientError({
          status: 403,
          code: "FORBIDDEN",
          message: "Missing required scope: audit:read",
          details: { required_scope: "audit:read" },
          request_id: "req-1a2b3c",
        }),
      );
    client.getAudit = getAudit;

    renderView();

    expect(await screen.findAllByRole("listitem")).toHaveLength(1);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("FORBIDDEN")).toBeVisible();
    expect(
      screen.getByText("Missing required scope: audit:read"),
    ).toBeVisible();
    expect(screen.getByText("req-1a2b3c")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeVisible();

    // The already-loaded entries are not dropped by the refusal.
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });
});
