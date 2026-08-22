// Activity view contract suite (docs/UI_CONTRACTS.md, "Activity" and
// "State and error contract").
//
// Pinned surface strings an implementation must render (calm household tone,
// plain language first, raw codes on demand):
//   - heading "Activity"; entries as list items, newest first
//   - pagination button "Load more"; it passes the previous page's
//     next_cursor as the afterSequence argument of client.getAudit and
//     disappears when next_cursor is null
//   - per control entry: principal display_name verbatim (who), the decided /
//     happened / why lines below, and a "Show technical detail" disclosure
//     revealing the raw reason codes; a native <details> is legal - before
//     the disclosure the raw code must be not VISIBLE, not absent from the
//     DOM
//   - decision language: CLAMPED -> "Reduced to 1500 W of the 3000 W
//     requested"; result -> "Delivering 1480 W"; reason SITE_EXPORT_LIMIT ->
//     "Site export limit"; a missing result -> "Result not recorded yet"
//     (never a fabricated measurement)
//   - kind chips "Observations", "Decisions", "Arming", "Stops",
//     "Acknowledgements" with the audit-type mapping observation ->
//     Observations, decision -> Decisions, arm AND disarm -> Arming, stop ->
//     Stops, *_acknowledgement -> Acknowledgements; unit chips "All units",
//     "MID", "RHS", "LHS" where "All units" resets the unit filter; every
//     chip is a toggle button exposing aria-pressed across its whole
//     lifecycle (false before activation, true while active, false again
//     after de-toggle) and keyboard operable
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
// { code, message, details, request_id } (docs/API_CONTRACTS.md). Audit
// fixtures use the service's lowercase wire enum values (decision_status
// "authorized" / "clamped"; docs/UI_CONTRACTS.md "Wire casing"), and
// rejections are thrown as the client's ApiClientError carrying the envelope
// verbatim plus the HTTP status (docs: src/api/client.ts "TypedError pin").
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient } from "../../api/client";
import type { ApiClient, AuditPage } from "../../api/client";
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

const principalSam = { kind: "human", display_name: "Sam (home operator)" };

// Wire fixtures: lowercase StrEnum values exactly as /api/v1/audit
// serializes them. Newest first, exactly as the API returns a page.
const authorizedDecisionMID = {
  type: "decision",
  sequence: 60,
  occurred_at: minutesAgo(2),
  unit_id: "MID",
  principal: principalSam,
  decision_status: "authorized",
  requested_watts: 1000,
  authorized_watts: 1000,
  measured_watts: 990,
  reason_codes: ["WITHIN_LIMITS"],
};

const observationRHS = {
  type: "observation",
  sequence: 59,
  occurred_at: minutesAgo(30),
  unit_id: "RHS",
};

const armLHS = {
  type: "arm",
  sequence: 58,
  occurred_at: minutesAgo(90),
  unit_id: "LHS",
  principal: principalSam,
  decision_status: "authorized",
  reason_codes: ["OPERATOR_REQUEST"],
};

const disarmLHS = {
  type: "disarm",
  sequence: 57,
  occurred_at: minutesAgo(95),
  unit_id: "LHS",
  principal: principalSam,
  decision_status: "authorized",
  reason_codes: ["OPERATOR_REQUEST"],
};

const clampedDecisionMID = {
  type: "decision",
  sequence: 60,
  occurred_at: minutesAgo(5),
  unit_id: "MID",
  principal: principalSam,
  decision_status: "clamped",
  requested_watts: 3000,
  authorized_watts: 1500,
  measured_watts: 1480,
  reason_codes: ["SITE_EXPORT_LIMIT"],
};

const stopRHS = {
  type: "stop",
  sequence: 61,
  occurred_at: minutesAgo(1),
  unit_id: "RHS",
  principal: principalSam,
  decision_status: "authorized",
  stop_id: "stop-17",
  reason_codes: ["OPERATOR_REQUEST"],
};

const stopAcknowledgementRHS = {
  type: "stop_acknowledgement",
  sequence: 62,
  occurred_at: minutesAgo(0.5),
  unit_id: "RHS",
  principal: principalSam,
  decision_status: "authorized",
  stop_id: "stop-17",
  reason_codes: [],
};

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

const unitOrder = () =>
  screen
    .getAllByRole("listitem")
    .map((item) => (item.textContent ?? "").match(/MID|RHS|LHS/)?.[0]);

beforeEach(() => {
  client = makeClient();
  vi.mocked(createApiClient).mockReset();
  vi.mocked(createApiClient).mockReturnValue(client);
});

describe("Activity view", () => {
  it("renders a healthy newest-first timeline with who requested each entry", async () => {
    const page: AuditPage = {
      events: [authorizedDecisionMID, observationRHS, armLHS],
      next_cursor: 58,
    };
    client.getAudit = vi.fn().mockResolvedValue(page);

    renderView();

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeVisible();

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(unitOrder()).toEqual(["MID", "RHS", "LHS"]);

    // Who requested it is shown on the newest entry.
    expect(
      within(items[0] as HTMLElement).getByText("Sam (home operator)"),
    ).toBeVisible();

    // More history is available.
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    expect(client.getAudit).toHaveBeenCalledTimes(1);
    expect(client.getAudit).toHaveBeenCalledWith(PAGE_SIZE);
  });

  it("shows what was decided, what happened, and why, with raw codes on demand", async () => {
    client.getAudit = vi
      .fn()
      .mockResolvedValue({ events: [clampedDecisionMID], next_cursor: null });

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    const entry = within(items[0] as HTMLElement);

    // Who / what was decided / what happened / why, in plain language first.
    expect(entry.getByText("Sam (home operator)")).toBeVisible();
    expect(
      entry.getByText("Reduced to 1500 W of the 3000 W requested"),
    ).toBeVisible();
    expect(entry.getByText("Delivering 1480 W")).toBeVisible();
    expect(entry.getByText("Site export limit")).toBeVisible();

    // Raw codes are not visible until asked for. A closed native <details>
    // satisfies this: text queries match hidden text, so the pin is
    // visibility, never DOM presence.
    const hiddenCode = entry.queryByText("SITE_EXPORT_LIMIT");
    if (hiddenCode) {
      expect(hiddenCode).not.toBeVisible();
    }

    const user = userEvent.setup();
    await user.click(entry.getByRole("button", { name: "Show technical detail" }));

    expect(entry.getByText("SITE_EXPORT_LIMIT")).toBeVisible();
  });

  it("paginates with the audit cursor and stops when the cursor is null", async () => {
    const page1: AuditPage = {
      events: [authorizedDecisionMID, observationRHS],
      next_cursor: 59,
    };
    const page2: AuditPage = {
      events: [armLHS],
      next_cursor: null,
    };
    const getAudit = vi
      .fn()
      .mockResolvedValueOnce(page1)
      .mockResolvedValueOnce(page2);
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
    expect(unitOrder()).toEqual(["MID", "RHS", "LHS"]);

    // A null cursor means the end of the timeline: no further load offered.
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
    expect(getAudit).toHaveBeenCalledTimes(2);
  });

  it("maps every audit kind to its chip and resets the unit filter with All units", async () => {
    client.getAudit = vi.fn().mockResolvedValue({
      events: [
        stopAcknowledgementRHS,
        stopRHS,
        clampedDecisionMID,
        observationRHS,
        armLHS,
        disarmLHS,
      ],
      next_cursor: null,
    });

    renderView();

    expect(await screen.findAllByRole("listitem")).toHaveLength(6);
    expect(unitOrder()).toEqual(["RHS", "RHS", "MID", "RHS", "LHS", "LHS"]);

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
    // kind's entries: arm AND disarm both map to Arming, while a stop
    // acknowledgement is an Acknowledgement, not a Stop.
    const expectedByKind = [
      ["Observations", ["RHS"]],
      ["Decisions", ["MID"]],
      ["Arming", ["LHS", "LHS"]],
      ["Stops", ["RHS"]],
      ["Acknowledgements", ["RHS"]],
    ] as const;
    for (const [name, expectedUnits] of expectedByKind) {
      const chip = screen.getByRole("button", { name });
      chip.focus();
      await user.keyboard("{Enter}");
      expect(chip).toHaveAttribute("aria-pressed", "true");
      expect(unitOrder()).toEqual(expectedUnits);
      chip.focus();
      await user.keyboard("{Enter}");
      expect(chip).toHaveAttribute("aria-pressed", "false");
      expect(screen.getAllByRole("listitem")).toHaveLength(6);
    }

    // Unit chips narrow the timeline to that unit's entries.
    const lhsChip = screen.getByRole("button", { name: "LHS" });
    lhsChip.focus();
    await user.keyboard("{Enter}");
    expect(lhsChip).toHaveAttribute("aria-pressed", "true");
    expect(unitOrder()).toEqual(["LHS", "LHS"]);
    lhsChip.focus();
    await user.keyboard("{Enter}");
    expect(lhsChip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getAllByRole("listitem")).toHaveLength(6);

    const rhsChip = screen.getByRole("button", { name: "RHS" });
    rhsChip.focus();
    await user.keyboard("{Enter}");
    expect(rhsChip).toHaveAttribute("aria-pressed", "true");
    expect(unitOrder()).toEqual(["RHS", "RHS", "RHS"]);

    // "All units" resets the unit filter: every entry returns and the unit
    // toggle goes back to unpressed.
    const allUnitsChip = screen.getByRole("button", { name: "All units" });
    allUnitsChip.focus();
    await user.keyboard("{Enter}");
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
    expect(rhsChip).toHaveAttribute("aria-pressed", "false");
  });

  it("filters by kind and unit using the keyboard only", async () => {
    client.getAudit = vi.fn().mockResolvedValue({
      events: [stopRHS, clampedDecisionMID, { ...armLHS, unit_id: "MID" }],
      next_cursor: null,
    });

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
    expect((onlyStop[0] as HTMLElement).textContent).toContain("RHS");

    // Narrow further by unit, still keyboard only.
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

  it("shows an empty state that explains what will appear here and the first step", async () => {
    client.getAudit = vi.fn().mockResolvedValue({ events: [], next_cursor: null });

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
      .mockResolvedValueOnce({
        events: [authorizedDecisionMID],
        next_cursor: null,
      });
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
    client.getAudit = vi.fn().mockResolvedValue({
      events: [
        { ...authorizedDecisionMID, occurred_at: minutesAgo(125) },
        observationRHS,
      ],
      next_cursor: null,
    });

    renderView("disconnected");

    expect(await screen.findByText("Connection lost")).toBeVisible();
    expect(
      screen.getByText(/showing the activity we have/),
    ).toBeVisible();

    // Last data stays on screen, dimmed but not hidden - and it carries its
    // age: the retained newest entry is stale and says so next to its values.
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(screen.getByText("Sam (home operator)")).toBeVisible();
    expect(
      within(items[0] as HTMLElement).getByText("2 hours ago"),
    ).toBeVisible();

    // REST retry is manual while disconnected.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(client.getAudit).toHaveBeenCalledTimes(2);
  });

  it("shows the age next to a stale entry instead of hiding it", async () => {
    client.getAudit = vi.fn().mockResolvedValue({
      events: [{ ...authorizedDecisionMID, occurred_at: minutesAgo(125) }],
      next_cursor: null,
    });

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);

    // The age belongs to the stale entry itself, not to a page-level banner.
    const stale = within(items[0] as HTMLElement);
    expect(stale.getByText("2 hours ago")).toBeVisible();
    expect((items[0] as HTMLElement).textContent).toContain("MID");
  });

  it("names a missing result explicitly and never fabricates a measurement", async () => {
    const missingResult = {
      type: "decision",
      sequence: 44,
      occurred_at: minutesAgo(10),
      unit_id: "LHS",
      principal: principalSam,
      decision_status: "authorized",
      requested_watts: 1200,
      authorized_watts: 800,
      reason_codes: ["WITHIN_LIMITS"],
    };
    client.getAudit = vi.fn().mockResolvedValue({
      events: [missingResult],
      next_cursor: null,
    });

    renderView();

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(1);
    const entry = within(items[0] as HTMLElement);

    expect(entry.getByText("Result not recorded yet")).toBeVisible();
    expect(entry.queryByText(/delivering/i)).toBeNull();
    expect(screen.queryByText(/^0 W$/)).toBeNull();
  });

  it("surfaces a refused Load more verbatim with the envelope code, message, and request id", async () => {
    const getAudit = vi
      .fn()
      .mockResolvedValueOnce({
        events: [authorizedDecisionMID],
        next_cursor: 60,
      })
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
