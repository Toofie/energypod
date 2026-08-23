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
//     carry none and are never claimed for a unit; the wire's unit ids are
//     lowercase (mid, rhs, lhs) while the chips are named MID, RHS, LHS, so
//     the unit filter compares canonical ids, never display labels.
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
//     "Reduced to 1,500 W of the 3,000 W requested"; reason power_clamped ->
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
//   - filtered empty: an empty Acknowledgements filter says what will appear
//     there (a latched stop or latched inhibit being acknowledged) and stays
//     honest about unloaded history, while an empty per-unit filter explains
//     that fleet-wide decisions and stops carry no unit id; every other
//     filtered-empty state is the plain "No activity matches these filters"
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
import {
  auditAppended,
  auditEvent,
  auditPage,
  observationPublished,
  resyncRequired,
  unitUnexpectedAutonomy,
  type WireAuditEvent,
} from "../../test/wire";
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
  getUnitDetail: vi.fn(),
  getAudit: vi.fn(),
  postIntent: vi.fn(),
  postIntentCancel: vi.fn(),
  postArm: vi.fn(),
  postDisarm: vi.fn(),
  postEmergencyStop: vi.fn(),
  postStopAcknowledgement: vi.fn(),
  postInhibitAcknowledgement: vi.fn(),
  postExcessCharging: vi.fn(),
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
      entry.getByText("Reduced to 1,500 W of the 3,000 W requested"),
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

  it("renders the audit watt figures at the two-decimal display bound: a raw eight-decimal fixture never reaches the operator", async () => {
    // The audit record's signed active-watt figures can arrive at full float
    // precision; the shared display-precision module (src/lib/format.ts) is
    // the render boundary — magnitudes with the direction in words, at most
    // two decimals, thousands grouped like every other watt figure.
    const preciseDecision = auditEvent({
      sequence: 64,
      event_type: "control_decision",
      unit_id: null,
      occurred_at: minutesAgo(4),
      principal: PRINCIPAL,
      result: "clamped",
      reason_codes: ["power_clamped"],
      requested_active_w: 3000.123456789,
      authorized_active_w: 1500.98765432,
    });
    client.getAudit = vi.fn().mockResolvedValue(auditPage([preciseDecision], null));

    renderView();

    const entry = within((await screen.findAllByRole("listitem"))[0] as HTMLElement);
    expect(
      entry.getByText("Reduced to 1,500.99 W of the 3,000.12 W requested"),
    ).toBeVisible();
    expect(entry.getByText(/Reduced to/).textContent ?? "").not.toMatch(/\d\.\d{3,}/);
  });

  it("renders a stop-held decision cycle honestly, naming the stop — never 'power allowed' at 0 W", async () => {
    // The 2026-08-23 incident shape: while a stop latched, the kernel audited
    // every cycle as authorized-at-0 W with reason stop_authorized, and the
    // timeline read "Power allowed / Allowed 0 W of the 0 W requested".
    const stopHeldDecision = auditEvent({
      sequence: 63,
      event_type: "control_decision",
      unit_id: null,
      occurred_at: minutesAgo(1),
      principal: PRINCIPAL,
      result: "authorized",
      reason_codes: ["stop_authorized"],
      requested_active_w: 0,
      authorized_active_w: 0,
    });
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([stopHeldDecision, emergencyStop], null));

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    const decision = within(items[0] as HTMLElement);

    // Both lines say the stop held the power, with the stop named (the
    // emergency_stop row's own id is the only stop id on the wire).
    expect(
      decision.getAllByText("Emergency stop stop-17 active — all power held at 0 W"),
    ).toHaveLength(2);
    // The dishonest grants never appear.
    expect(decision.queryByText("Power allowed")).toBeNull();
    expect(decision.queryByText(/Allowed \d+ W of the \d+ W requested/)).toBeNull();
    // The raw reason code stays available behind the disclosure.
    const user = userEvent.setup();
    await user.click(decision.getByRole("button", { name: "Show technical detail" }));
    expect(decision.getByText("stop_authorized")).toBeVisible();
  });

  it("renders the stop-held line without inventing an id when no stop row is loaded", async () => {
    const stopHeldDecision = auditEvent({
      sequence: 63,
      event_type: "control_decision",
      unit_id: null,
      occurred_at: minutesAgo(1),
      principal: PRINCIPAL,
      result: "authorized",
      reason_codes: ["stop_authorized"],
      requested_active_w: 0,
      authorized_active_w: 0,
    });
    // The page holds only the decision: an older stop may simply be beyond
    // the loaded page, so the line names no id rather than a guessed one.
    client.getAudit = vi.fn().mockResolvedValue(auditPage([stopHeldDecision], null));

    renderView();

    const decision = within((await screen.findAllByRole("listitem"))[0] as HTMLElement);
    expect(
      decision.getAllByText("Emergency stop active — all power held at 0 W"),
    ).toHaveLength(2);
    expect(decision.queryByText("Power allowed")).toBeNull();
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
    // A per-unit view that matches nothing explains where the fleet-wide
    // rows went instead of looking broken.
    expect(
      screen.getByText(
        "No RHS activity matches these filters. Fleet-wide decisions and stops carry no unit id, so they appear only under All units.",
      ),
    ).toBeVisible();

    // "All units" resets the unit filter: every entry returns and the unit
    // toggles go back to unpressed.
    const allUnitsChip = screen.getByRole("button", { name: "All units" });
    allUnitsChip.focus();
    await user.keyboard("{Enter}");
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
    expect(rhsChip).toHaveAttribute("aria-pressed", "false");
  });

  it("tells an empty Acknowledgements filter what will appear there", async () => {
    // No *_acknowledged entry anywhere in the loaded history: the operator
    // must see what the section is waiting for, not a bare "nothing matches".
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([emergencyStop, clampedDecision], null));

    renderView();
    await screen.findAllByRole("listitem");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Acknowledgements" }));

    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(
      screen.getByText(
        "No acknowledgements yet. One appears here each time a latched emergency stop or a latched unit inhibit is acknowledged.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText("No activity matches these filters"),
    ).not.toBeInTheDocument();
  });

  it("stays honest about unloaded history when the loaded page holds no acknowledgement", async () => {
    // With more history unloaded, an older acknowledgement may simply be
    // beyond the loaded page: the empty message shrinks its claim and points
    // at Load more instead of asserting "none yet".
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([emergencyStop, clampedDecision], 59));

    renderView();
    await screen.findAllByRole("listitem");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Acknowledgements" }));

    expect(
      screen.getByText(
        "No acknowledgements in the activity loaded so far — Load more reaches older entries. One appears here each time a latched emergency stop or a latched unit inhibit is acknowledged.",
      ),
    ).toBeVisible();
  });

  it("does not claim 'no acknowledgements yet' when the unit filter narrowed them away", async () => {
    // Acknowledgements that exist but are excluded by the unit filter are a
    // per-unit empty state, never "nothing yet".
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([inhibitAcknowledgementMID], null));

    renderView();
    await screen.findAllByRole("listitem");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Acknowledgements" }));
    await user.click(screen.getByRole("button", { name: "RHS" }));

    expect(
      screen.getByText("No RHS activity matches these filters."),
    ).toBeVisible();
    expect(
      screen.queryByText(/No acknowledgements/),
    ).not.toBeInTheDocument();
  });

  it("filters units by the wire's canonical lowercase ids while the chips keep their display names", async () => {
    // WIRE TRUTH: the live audit trail's `unit_id` values are lowercase
    // (mid, rhs, lhs — the snapshot and bus frames carry them the same way),
    // while the chips are named MID, RHS, LHS. The filter must bridge the
    // two, not compare a display label against the data.
    const armMidLower = auditEvent({
      sequence: 81,
      event_type: "unit_armed",
      unit_id: "mid",
      occurred_at: minutesAgo(3),
      principal: PRINCIPAL,
      result: "armed",
      reason_codes: ["armed"],
    });
    const disarmLhsLower = auditEvent({
      sequence: 80,
      event_type: "unit_disarmed",
      unit_id: "lhs",
      occurred_at: minutesAgo(4),
      principal: PRINCIPAL,
      result: "disarmed",
      reason_codes: ["disarmed"],
    });
    client.getAudit = vi
      .fn()
      .mockResolvedValue(auditPage([armMidLower, disarmLhsLower, clampedDecision], null));

    renderView();
    expect(await screen.findAllByRole("listitem")).toHaveLength(3);

    const user = userEvent.setup();

    // The display names stay uppercase on the chips and the pressed state
    // tracks the canonical id underneath.
    const lhsChip = screen.getByRole("button", { name: "LHS" });
    await user.click(lhsChip);
    expect(lhsChip).toHaveAttribute("aria-pressed", "true");
    const lhsEntries = screen.getAllByRole("listitem");
    expect(lhsEntries).toHaveLength(1);
    expect((lhsEntries[0] as HTMLElement).textContent).toContain("lhs");

    const midChip = screen.getByRole("button", { name: "MID" });
    await user.click(midChip);
    expect(midChip).toHaveAttribute("aria-pressed", "true");
    expect(lhsChip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("mid")).toBeVisible();

    // A unit with no rows of its own names the fleet-wide absence honestly.
    await user.click(screen.getByRole("button", { name: "RHS" }));
    expect(
      screen.getByText(
        "No RHS activity matches these filters. Fleet-wide decisions and stops carry no unit id, so they appear only under All units.",
      ),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "All units" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
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
      screen.getByText(
        "No MID activity matches these filters. Fleet-wide decisions and stops carry no unit id, so they appear only under All units.",
      ),
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

// --- live-update regression pins (2026-08-23 incident) -----------------------
//
// The operator opened Activity and saw nothing while the fleet was visibly
// publishing: the view was a point-in-time REST read of the audit trail, and
// the audit trail holds no observation entries at all (observations are
// published on the event bus, never audited — wire.ts header). These pin the
// corrected behavior: the view subscribes to the session's shared stream and
// appends what the backend actually emits — audit.appended entries and live
// observation entries — without a second REST read, deduped against a later
// refresh by the audit event's own event_id.

describe("Activity view — live updates from the event stream", () => {
  /** A controllable shared stream: stays open until the test pushes frames. */
  function streamChannel(): {
    openEvents: () => AsyncGenerator<Record<string, unknown>, void, unknown>;
    push(frame: Record<string, unknown>): void;
  } {
    const queue: Record<string, unknown>[] = [];
    let wake: (() => void) | null = null;
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    return {
      openEvents: () =>
        (async function* channel(): AsyncGenerator<Record<string, unknown>, void, unknown> {
          while (true) {
            while (queue.length > 0) {
              const next = queue.shift();
              if (next !== undefined) {
                yield next;
                if (next.type === "resync_required") {
                  return;
                }
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

  it("appends an audit.appended frame to the timeline without another REST read", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();

    // The loaded history is empty, and the bus delivers a real audited fact.
    expect(await screen.findByText("Nothing here yet")).toBeVisible();
    channel.push(
      auditAppended(91, {
        event_id: "facade-91",
        event_type: "unit_armed",
        unit_id: "MID",
        result: "armed",
        reason_codes: ["armed"],
      }) as unknown as Record<string, unknown>,
    );

    // The entry appears immediately, newest first — no second getAudit call.
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]!.textContent).toContain("Arm request");
    expect(items[0]!.textContent).toContain("MID");
    expect(client.getAudit).toHaveBeenCalledTimes(1);
  });

  it("lists live observations from the bus, and the Observations chip selects them", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();
    expect(await screen.findByText("Nothing here yet")).toBeVisible();

    // The only observation signal the backend emits: the bus frame, not the
    // audit trail. It becomes a timeline entry naming the telemetry sequence.
    channel.push(
      observationPublished(92, "MID", { telemetrySequence: 41050 }) as unknown as Record<
        string,
        unknown
      >,
    );
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]!.textContent).toContain("Observation");
    expect(items[0]!.textContent).toContain("MID");
    expect(items[0]!.textContent).toContain("telemetry sequence 41050");

    // The contracted Observations chip finally selects a non-empty set...
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Observations" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    // ...and the other kinds honestly exclude it.
    await user.click(screen.getByRole("button", { name: "Decisions" }));
    expect(await screen.findByText("No activity matches these filters")).toBeVisible();
  });

  it("dedupes a live-appended entry against the same fact arriving later over REST", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();
    expect(await screen.findByText("Nothing here yet")).toBeVisible();

    const frame = auditAppended(93, {
      event_id: "facade-5d",
      event_type: "unit_disarmed",
      unit_id: "LHS",
      result: "disarmed",
      reason_codes: ["disarmed"],
    });
    channel.push(frame as unknown as Record<string, unknown>);
    expect(await screen.findAllByRole("listitem")).toHaveLength(1);

    // The same durable fact, later readable over REST (its store sequence
    // differs from the bus sequence — only the event_id is shared): the
    // resync-triggered refresh must not render it twice.
    client.getAudit = vi.fn().mockResolvedValue(
      auditPage(
        [
          auditEvent({
            event_id: "facade-5d",
            sequence: 12,
            event_type: "unit_disarmed",
            unit_id: "LHS",
            result: "disarmed",
            reason_codes: ["disarmed"],
          }),
        ],
        null,
      ),
    );
    channel.push(resyncRequired("retention_window_exceeded") as unknown as Record<string, unknown>);

    // The discontinuity triggers exactly one quiet refresh on the new mock
    // (the mount read went to the previous one)...
    await waitFor(() => {
      expect(client.getAudit).toHaveBeenCalledTimes(1);
    });
    // ...and the same durable fact still renders exactly once.
    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(1);
    });
  });
});

// ---------------------------------------------------------------------------
// The self-healing awareness layer's QUIET evidence (API_CONTRACTS.md
// "Self-healing awareness layer (recovery detection)" — live on the
// controller since b20058d..7b491b3): a unit.unexpected_autonomy bus frame
// (one per unit per 60 s, carrying the pinned figures) becomes exactly one
// informational timeline entry — never a banner, never a badge, never an
// alarm tier. It exists so the operator can SEE the recorded evidence the
// backend is collecting on mid's standing uncommanded oscillation.
// ---------------------------------------------------------------------------

describe("Activity view — unexpected-autonomy evidence (quiet tier)", () => {
  /** A controllable shared stream, as the live-update suite builds one. */
  function streamChannel(): {
    openEvents: () => AsyncGenerator<Record<string, unknown>, void, unknown>;
    push(frame: Record<string, unknown>): void;
  } {
    const queue: Record<string, unknown>[] = [];
    let wake: (() => void) | null = null;
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    return {
      openEvents: () =>
        (async function* channel(): AsyncGenerator<Record<string, unknown>, void, unknown> {
          while (true) {
            while (queue.length > 0) {
              const next = queue.shift();
              if (next !== undefined) {
                yield next;
                if (next.type === "resync_required") {
                  return;
                }
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

  it("appends one quiet informational entry per evidence frame, never an alarm", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();
    expect(await screen.findByText("Nothing here yet")).toBeVisible();

    channel.push(
      unitUnexpectedAutonomy(93, { unit_id: "mid", measured_watts: 1411.2 }) as unknown as Record<
        string,
        unknown
      >,
    );
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]!.textContent).toContain("Uncommanded activity");
    expect(items[0]!.textContent).toContain("mid");
    expect(items[0]!.textContent).toContain("Measured 1,411.2 W with no request claiming this battery");

    // Quiet by construction: no alert role anywhere on the page, no reason
    // codes to disclose, and a second throttled frame (the backend sends one
    // per unit per 60 s) is its own entry — never an escalation.
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    expect(within(items[0]!).queryByRole("button", { name: /show technical detail/i })).toBeNull();
    channel.push(
      unitUnexpectedAutonomy(94, { unit_id: "mid", measured_watts: -1204.5 }) as unknown as Record<
        string,
        unknown
      >,
    );
    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(2);
    });
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("never fabricates a figure when the frame's measurement is absent", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();
    expect(await screen.findByText("Nothing here yet")).toBeVisible();

    channel.push(
      unitUnexpectedAutonomy(93, { unit_id: "mid", measured_watts: null }) as unknown as Record<
        string,
        unknown
      >,
    );
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]!.textContent).toContain("Measured with no request claiming this battery");
  });

  it("carries the unit id honestly: the unit filter selects it, and no kind chip claims it", async () => {
    client.getAudit = vi.fn().mockResolvedValue(auditPage([], null));
    const channel = streamChannel();
    client.openEvents = vi.fn(channel.openEvents) as unknown as typeof client.openEvents;
    renderView();
    expect(await screen.findByText("Nothing here yet")).toBeVisible();

    channel.push(
      unitUnexpectedAutonomy(93, { unit_id: "mid", measured_watts: 1411.2 }) as unknown as Record<
        string,
        unknown
      >,
    );
    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(1);
    });

    // Evidence is not any of the five contracted kinds: every kind chip that
    // narrows the timeline hides it (it lives in the "other" bucket, quiet).
    const user = userEvent.setup();
    for (const chip of ["Observations", "Decisions", "Arming", "Stops", "Acknowledgements"]) {
      await user.click(screen.getByRole("button", { name: chip }));
      expect(screen.queryAllByRole("listitem")).toHaveLength(0);
      await user.click(screen.getByRole("button", { name: chip }));
    }

    // The unit filter still owns it exactly like any per-unit fact.
    await user.click(screen.getByRole("button", { name: "MID" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "RHS" }));
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
