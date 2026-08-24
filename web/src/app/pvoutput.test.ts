/**
 * Behavior contract for the PVOutput reporting wire model
 * (web/src/app/pvoutput.ts): the `GET /api/v1/pvoutput/status` snapshot's
 * narrowing (nulls are nulls, never zero; a garbage frame is never
 * half-adopted; absent fields inherit the base) and the plain-word maps the
 * Home card renders (the durable-toggle state phrase, the last-post line,
 * the standing failure words, the honest-gap sentence).
 */
import { describe, expect, it } from "vitest";
import {
  PVOUTPUT_CONFIRMATION,
  PVOUTPUT_NOT_COMMISSIONED_TEXT,
  pvoutputGapsText,
  pvoutputHealthText,
  pvoutputLastPostText,
  pvoutputToggleStateText,
  toPvOutputStatus,
} from "./pvoutput";

function status(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    feature: "pvoutput",
    enabled: false,
    enabled_origin: "config",
    disabled_reason: null,
    credentials_note: null,
    interval_s: 300,
    unit_slots: { lhs: ["v7", "v8"], rhs: ["v9", "v10"], mid: ["v11", "v12"] },
    native_battery_fields: true,
    as_of: "2026-08-27T01:31:00+00:00",
    last_success_at: null,
    last_post_age_s: null,
    last_posted_slot: null,
    last_error: null,
    consecutive_failures: 0,
    rate_remaining: null,
    slots_skipped_stale: 0,
    ...spec,
  };
}

describe("toPvOutputStatus", () => {
  it("narrows the wire snapshot with every nullable field honest", () => {
    const parsed = toPvOutputStatus(status());
    expect(parsed).not.toBeNull();
    expect(parsed!.enabled).toBe(false);
    expect(parsed!.enabledOrigin).toBe("config");
    expect(parsed!.disabledReason).toBeNull();
    expect(parsed!.lastSuccessAt).toBeNull();
    expect(parsed!.lastPostAgeS).toBeNull();
    expect(parsed!.lastPostedSlot).toBeNull();
    expect(parsed!.lastError).toBeNull();
    expect(parsed!.rateRemaining).toBeNull();
    expect(parsed!.slotsSkippedStale).toBe(0);
    expect(parsed!.unitSlots).toEqual({ lhs: ["v7", "v8"], rhs: ["v9", "v10"], mid: ["v11", "v12"] });
  });

  it("never half-adopts a garbage frame", () => {
    expect(toPvOutputStatus(null)).toBeNull();
    expect(toPvOutputStatus("nope")).toBeNull();
    expect(toPvOutputStatus(42)).toBeNull();
  });

  it("keeps unknown enum spellings at their honest defaults", () => {
    const parsed = toPvOutputStatus(status({ enabled_origin: "somewhere_else" }));
    expect(parsed!.enabledOrigin).toBe("config");
    const refused = toPvOutputStatus(status({ disabled_reason: "mystery" }));
    expect(refused!.disabledReason).toBeNull();
  });

  it("inherits absent fields from the base (the toggle's partial frames)", () => {
    const base = toPvOutputStatus(status({ enabled: true, rate_remaining: 40 }))!;
    const patched = toPvOutputStatus({ enabled: false }, base)!;
    expect(patched.enabled).toBe(false);
    expect(patched.rateRemaining).toBe(40);
  });

  it("treats an explicit wire null as the frame's own answer", () => {
    const base = toPvOutputStatus(status({ rate_remaining: 40, last_error: "boom" }))!;
    const patched = toPvOutputStatus(status({ rate_remaining: null, last_error: null }), base)!;
    expect(patched.rateRemaining).toBeNull();
    expect(patched.lastError).toBeNull();
  });
});

describe("pvoutputToggleStateText", () => {
  it("names the origin, and a runtime act is KEPT across restarts", () => {
    // The durable-toggle honesty: the night tile's "until restart" is the
    // deliberate mirror this feature inverts.
    expect(pvoutputToggleStateText(toPvOutputStatus(status())!)).toBe("Off (config)");
    expect(pvoutputToggleStateText(toPvOutputStatus(status({ enabled: true }))!)).toBe("On (config)");
    expect(pvoutputToggleStateText(toPvOutputStatus(status({ enabled_origin: "runtime" }))!)).toBe(
      "Off — kept across restarts",
    );
    expect(
      pvoutputToggleStateText(toPvOutputStatus(status({ enabled: true, enabled_origin: "runtime" }))!),
    ).toBe("On — kept across restarts");
  });
});

describe("pvoutputLastPostText", () => {
  it("says not-yet before the first post, never a zero age", () => {
    expect(pvoutputLastPostText(toPvOutputStatus(status())!)).toBe(
      "No post yet — the first 5-minute slot has not landed.",
    );
  });

  it("carries the server-computed age, the slot, and the post budget", () => {
    const line = pvoutputLastPostText(
      toPvOutputStatus(
        status({
          last_success_at: "2026-08-26T15:30:01+00:00",
          last_post_age_s: 112,
          last_posted_slot: "2026-08-27 01:30",
          rate_remaining: 43,
        }),
      )!,
    );
    expect(line).toBe("Last post 1 min ago · slot 2026-08-27 01:30 · 43 posts left this hour.");
  });

  it("omits the budget when PVOutput did not report one", () => {
    const line = pvoutputLastPostText(
      toPvOutputStatus(
        status({
          last_success_at: "2026-08-26T15:30:01+00:00",
          last_post_age_s: 112,
          last_posted_slot: "2026-08-27 01:30",
        }),
      )!,
    );
    expect(line).not.toContain("posts left");
  });
});

describe("pvoutputHealthText", () => {
  it("names an auth refusal as the standing stop it is, with the wire's words", () => {
    const line = pvoutputHealthText(
      toPvOutputStatus(status({ disabled_reason: "auth_failed", last_error: "Read only key" }))!,
    );
    expect(line).toContain("refused the credentials");
    expect(line).toContain("Read only key");
  });

  it("names absent credentials in plain words", () => {
    const line = pvoutputHealthText(
      toPvOutputStatus(status({ disabled_reason: "missing_credentials" }))!,
    );
    expect(line).toContain("credentials are not set");
  });

  it("carries a transient failure verbatim with its streak", () => {
    const line = pvoutputHealthText(
      toPvOutputStatus(status({ enabled: true, last_error: "Moon Powered", consecutive_failures: 2 }))!,
    );
    expect(line).toContain("Failing (2 in a row)");
    expect(line).toContain("Moon Powered");
    expect(line).toContain("next slot retries");
  });

  it("states the cadence while posting cleanly", () => {
    const line = pvoutputHealthText(toPvOutputStatus(status({ enabled: true }))!);
    expect(line).toContain("Posting every 5 min");
    expect(line).toContain("own dashboard slot");
  });

  it("says standing by while off and healthy", () => {
    expect(pvoutputHealthText(toPvOutputStatus(status())!)).toContain("Standing by");
  });
});

describe("pvoutputGapsText", () => {
  it("is quiet at zero and counts honestly otherwise", () => {
    expect(pvoutputGapsText(toPvOutputStatus(status())!)).toBeNull();
    expect(pvoutputGapsText(toPvOutputStatus(status({ slots_skipped_stale: 1 }))!)).toContain(
      "1 slot skipped",
    );
    expect(pvoutputGapsText(toPvOutputStatus(status({ slots_skipped_stale: 3 }))!)).toContain(
      "3 slots skipped",
    );
    expect(pvoutputGapsText(toPvOutputStatus(status({ slots_skipped_stale: 3 }))!)).toContain(
      "never zero-filled",
    );
  });
});

describe("the pinned literals", () => {
  it("carries the guarded toggle's confirmation and the not-commissioned sentence", () => {
    expect(PVOUTPUT_CONFIRMATION).toBe("PVOUTPUT");
    expect(PVOUTPUT_NOT_COMMISSIONED_TEXT).toContain("not commissioned");
    expect(PVOUTPUT_NOT_COMMISSIONED_TEXT).toContain("controller's config");
  });
});
