/**
 * Behavior contract for the schedules wire model (web/src/app/schedule.ts):
 * the PENDING-BACKEND parse layer every schedule surface rides. Pins the
 * null-safe, absent-tolerant narrowing (the active_stops / intent-block /
 * adviser_state feature-detection pattern), the dual watt form, the
 * client-side mirror of the allowed-windows containment rule, the
 * DAY_DEFAULT-night pin, and the two window-transition patch helpers.
 */
import { describe, expect, it } from "vitest";
import { scheduleEntry, scheduleState, scheduleNextAction } from "../test/wire";
import {
  allowedMinuteSet,
  allowedWindowsText,
  applyWindowClosing,
  applyWindowOpened,
  countdownText,
  daysSummaryText,
  isWindowAllowed,
  localTimeOfInstant,
  minutesOfHHMM,
  SCHEDULE_DAYS,
  toScheduleEntry,
  toScheduleNextAction,
  toSchedulePlan,
  toSchedulePolicy,
  toScheduleReplacedEvent,
  toScheduleState,
  toScheduleWindowClosingEvent,
  toScheduleWindowOpenedEvent,
  touchesNightMinutes,
  wattsSummaryText,
} from "./schedule";

const DAY_ALLOWED = allowedMinuteSet([["06:00", "20:00"]]);

describe("schedule wire model — entries", () => {
  it("narrows the per-battery form entry the fixtures build", () => {
    const entry = toScheduleEntry(scheduleEntry());
    expect(entry).not.toBeNull();
    expect(entry!.entryId).toBe("Day charge");
    expect(entry!.days).toEqual([...SCHEDULE_DAYS]);
    expect(entry!.action).toBe("charge");
    // The dual form survives exactly: per-unit map present, scalar null.
    expect(entry!.watts).toBeNull();
    expect(entry!.wattsByUnit).toEqual({ lhs: 2500, mid: 2500, rhs: 2500 });
    expect(entry!.unitIds).toEqual(["lhs", "mid", "rhs"]);
    expect(entry!.enabled).toBe(true);
    expect(entry!.priority).toBe(0);
  });

  it("keeps the scalar fleet-total form scalar, never stamped per battery", () => {
    const entry = toScheduleEntry(scheduleEntry({ watts: 3000 }));
    expect(entry!.watts).toBe(3000);
    expect(entry!.wattsByUnit).toBeNull();
    expect(wattsSummaryText(entry!)).toBe("fleet total 3,000 W");
  });

  it("drops a non-object or an entry with no usable id entirely", () => {
    expect(toScheduleEntry(null)).toBeNull();
    expect(toScheduleEntry("Night Charge")).toBeNull();
    expect(toScheduleEntry({ days: ["mon"] })).toBeNull();
    expect(toScheduleEntry({ entry_id: "" })).toBeNull();
  });

  it("falls back per field on unusable data instead of fabricating", () => {
    const entry = toScheduleEntry({
      entry_id: "Night Charge",
      days: ["mon", "funday", 7],
      start_local: 1,
      action: "sideways",
      enabled: "yes",
      priority: "high",
    });
    expect(entry!.days).toEqual(["mon"]);
    expect(entry!.startLocal).toBe("");
    expect(entry!.action).toBe("charge");
    expect(entry!.enabled).toBe(true);
    expect(entry!.priority).toBe(0);
    expect(entry!.wattsByUnit).toBeNull();
    expect(entry!.watts).toBeNull();
  });
});

describe("schedule wire model — plan, policy, projection", () => {
  it("narrows a plan and keeps null-plan distinct from a parse failure", () => {
    const plan = toSchedulePlan({ version: 4, timezone: "Australia/Brisbane", entries: [scheduleEntry()] });
    expect(plan!.version).toBe(4);
    expect(plan!.entries).toHaveLength(1);
    expect(toSchedulePlan(null)).toBeNull();
    expect(toSchedulePlan({ entries: [] })).toBeNull();
  });

  it("narrows the policy with the day-only default when windows are absent", () => {
    expect(toSchedulePolicy({ posture: "yield" })!.allowedWindowsLocal).toEqual([["06:00", "20:00"]]);
    const partition = toSchedulePolicy({
      posture: "partition",
      allowed_windows_local: [["20:00", "06:00"]],
    });
    expect(partition!.posture).toBe("partition");
    expect(partition!.allowedWindowsLocal).toEqual([["20:00", "06:00"]]);
    expect(toSchedulePolicy(null)).toBeNull();
  });

  it("narrows the schedule_state projection, absent-tolerant per field", () => {
    const state = toScheduleState(scheduleState());
    expect(state!.active).toBe(true);
    expect(state!.entryId).toBe("Night Charge");
    expect(state!.endsInS).toBe(2743);
    expect(state!.posture).toBe("partition");
    expect(state!.reasonCodes).toEqual(["window_open"]);
    expect(state!.next).toBeNull();

    const bare = toScheduleState({});
    expect(bare!.active).toBe(false);
    expect(bare!.entryId).toBeNull();
    expect(bare!.posture).toBe("yield");
    expect(bare!.next).toBeNull();
    expect(toScheduleState("holding")).toBeNull();
  });

  it("narrows the next-occurrence object with its countdown fields", () => {
    const next = toScheduleNextAction(scheduleNextAction({ starts_in_s: 90 }));
    expect(next!.entryId).toBe("Night Charge");
    expect(next!.startsInS).toBe(90);
    expect(next!.startsAt).toBe("2026-08-24T00:01:00+10:00");
    expect(next!.wattsByUnit).toEqual({ lhs: 2500, mid: 2500, rhs: 2500 });
    expect(toScheduleNextAction({})).toBeNull();
  });
});

describe("schedule wire model — bus events", () => {
  it("narrows schedule.replaced with its diff", () => {
    const event = toScheduleReplacedEvent({
      principal: "operator:home",
      version: 5,
      diff: { added: ["Night Charge"], removed: ["old-evening"], changed: ["morning-topup"] },
    });
    expect(event!.diff).toEqual({
      added: ["Night Charge"],
      removed: ["old-evening"],
      changed: ["morning-topup"],
    });
    expect(toScheduleReplacedEvent("replaced")).toBeNull();
  });

  it("narrows schedule_window.opened with the running window's command", () => {
    const event = toScheduleWindowOpenedEvent({
      entry_id: "Night Charge",
      version: 4,
      action: "charge",
      watts_by_unit: { lhs: 2000, mid: 2500, rhs: 2500 },
      unit_ids: ["lhs", "mid", "rhs"],
      ends_at: "2026-08-24T05:59:00+10:00",
    });
    expect(event!.entryId).toBe("Night Charge");
    expect(event!.wattsByUnit).toEqual({ lhs: 2000, mid: 2500, rhs: 2500 });
    expect(event!.watts).toBeNull();
    expect(toScheduleWindowOpenedEvent({ version: 4 })).toBeNull();
  });

  it("narrows schedule_window.closing with its reason", () => {
    const event = toScheduleWindowClosingEvent({ entry_id: "Night Charge", reason: "plan_replaced" });
    expect(event!.reason).toBe("plan_replaced");
    expect(toScheduleWindowClosingEvent(null)).toBeNull();
  });
});

describe("the allowed-window containment rule, client-side mirror", () => {
  it("accepts a window inside the day-only policy", () => {
    expect(isWindowAllowed("06:00", "20:00", DAY_ALLOWED)).toBe(true);
    expect(isWindowAllowed("06:30", "18:00", DAY_ALLOWED)).toBe(true);
  });

  it("refuses a night window under the day-only policy", () => {
    expect(isWindowAllowed("00:01", "05:59", DAY_ALLOWED)).toBe(false);
    expect(isWindowAllowed("19:00", "21:00", DAY_ALLOWED)).toBe(false);
  });

  it("splits a cross-midnight window and judges every minute of it", () => {
    // 22:30→06:00 is one entry; its minutes run 22:30-23:59 and 00:00-05:59,
    // both outside the day-only policy.
    expect(isWindowAllowed("22:30", "06:00", DAY_ALLOWED)).toBe(false);
    // A cross-midnight window fully inside a cross-midnight policy passes.
    const partition = allowedMinuteSet([["20:00", "06:00"]]);
    expect(isWindowAllowed("22:30", "06:00", partition)).toBe(true);
  });

  it("treats unusable times as invalid input, never a containment answer", () => {
    expect(isWindowAllowed("nonsense", "06:00", DAY_ALLOWED)).toBeNull();
    expect(isWindowAllowed("06:00", "", DAY_ALLOWED)).toBeNull();
  });

  it("judges night by DAY_DEFAULT, whatever the policy grants", () => {
    expect(touchesNightMinutes("00:01", "05:59")).toBe(true);
    expect(touchesNightMinutes("22:30", "06:00")).toBe(true);
    expect(touchesNightMinutes("06:00", "20:00")).toBe(false);
    expect(touchesNightMinutes("06:00", "20:01")).toBe(true);
    expect(touchesNightMinutes("06:00", "x")).toBeNull();
  });

  it("reads civil minutes and renders the policy's walls", () => {
    expect(minutesOfHHMM("06:00")).toBe(360);
    expect(minutesOfHHMM("23:59")).toBe(1439);
    expect(minutesOfHHMM("6:00")).toBe(360);
    expect(minutesOfHHMM("24:00")).toBeNull();
    expect(minutesOfHHMM("12:5")).toBeNull();
    expect(minutesOfHHMM("garbage")).toBeNull();
    expect(allowedWindowsText([["06:00", "20:00"]])).toBe("06:00–20:00");
    expect(allowedWindowsText([["06:00", "08:00"], ["18:00", "20:00"]])).toBe(
      "06:00–08:00 and 18:00–20:00",
    );
  });
});

describe("the window-transition patches (state-local card swaps)", () => {
  it("an opened window flips the projection to running, deriving the countdown from ends_at", () => {
    const nowMs = Date.parse("2026-08-24T03:13:41+10:00");
    const next = applyWindowOpened(
      null,
      toScheduleWindowOpenedEvent({
        entry_id: "Night Charge",
        version: 4,
        ends_at: "2026-08-24T05:59:00+10:00",
      })!,
      nowMs,
    );
    expect(next.active).toBe(true);
    expect(next.entryId).toBe("Night Charge");
    expect(next.version).toBe(4);
    // 03:13:41 -> 05:59:00 is 2 h 45 m 19 s.
    expect(next.endsInS).toBe(2 * 3600 + 45 * 60 + 19);
    expect(next.reasonCodes).toEqual(["window_open"]);
  });

  it("a closing window stops claiming a running window without inventing a next", () => {
    const before = toScheduleState(scheduleState())!;
    const after = applyWindowClosing(
      before,
      toScheduleWindowClosingEvent({ entry_id: "Night Charge", version: 4 })!,
    );
    expect(after.active).toBe(false);
    expect(after.heldIntentId).toBeNull();
    expect(after.endsAt).toBeNull();
    expect(after.reasonCodes).toEqual(["window_ended"]);
  });
});

describe("plain-language summaries", () => {
  it("renders the days line in household words", () => {
    expect(daysSummaryText([...SCHEDULE_DAYS])).toBe("every day");
    expect(daysSummaryText(["mon", "tue", "wed", "thu", "fri"])).toBe("weekdays");
    expect(daysSummaryText(["sat", "sun"])).toBe("weekends");
    expect(daysSummaryText(["mon", "wed", "fri"])).toBe("Mon, Wed, Fri");
    expect(daysSummaryText([])).toBe("no days");
  });

  it("renders the per-battery watts line (the house doctrine)", () => {
    expect(
      wattsSummaryText({ action: "charge", watts: null, wattsByUnit: { lhs: 2500, mid: 2500 } }),
    ).toBe("2,500 W per battery (lhs, mid)");
    expect(
      wattsSummaryText({ action: "discharge", watts: null, wattsByUnit: { lhs: 2000, mid: 2500 } }),
    ).toBe("lhs 2,000 W, mid 2,500 W");
    expect(wattsSummaryText({ action: "charge", watts: null, wattsByUnit: { mid: 1800 } })).toBe(
      "1,800 W (mid)",
    );
    expect(wattsSummaryText({ action: "idle", watts: 0, wattsByUnit: null })).toBe("hold to zero");
    expect(wattsSummaryText({ action: "charge", watts: null, wattsByUnit: null })).toBe(
      "watts not available",
    );
  });

  it("renders countdowns in plain words and the site-local clock time", () => {
    expect(countdownText(45)).toBe("45 s");
    expect(countdownText(59)).toBe("59 s");
    expect(countdownText(60)).toBe("1 min");
    expect(countdownText(750)).toBe("12 min");
    expect(countdownText(3600)).toBe("1 h");
    expect(countdownText(4143)).toBe("1 h 09 min");
    expect(countdownText(2743)).toBe("45 min");
    expect(countdownText(0)).toBe("0 s");
    expect(localTimeOfInstant("2026-08-24T05:59:00+10:00")).toBe("05:59");
    expect(localTimeOfInstant(null)).toBe("");
    expect(localTimeOfInstant("not an instant")).toBe("");
  });
});
