/**
 * The pod-parking wire model's behavior contract (web/src/app/park.ts)
 * against DESIGN_POD_PARKING.md §2/§3/§7 and API_CONTRACTS.md "Pod parking".
 *
 * Every fixture is a wire shape from web/src/test/wire.ts (the shared wire
 * truth) or a hand-built variant of one — never a shape invented here. The
 * pins under test:
 *
 * - `park_state` narrows per field; the ABSENT key is the not-commissioned
 *   feature detection (null, never a fabricated "unparked" state).
 * - The countdown is derived from the lease's own expiry instant and reads
 *   H:MM; the promotion to the expired alert answers the wire's `expired`
 *   flag OR the instant having passed between snapshots (never waits for the
 *   next read).
 * - Every pinned 409 refusal shape renders its plain sentence from its
 *   details; an unknown code renders "" so the envelope alone speaks.
 * - The takeover rule is exactly the word-parked-no-lease origins.
 * - The lease choices are bounded by the site's own budget and always offer
 *   the cap, named as the cap.
 */
import { describe, expect, it } from "vitest";
import {
  notParkedParkState,
  parkRefusalEnvelope,
  parkState,
  resumeChecklist,
  withParkState,
} from "../test/wire";
import {
  hmmText,
  isTakeoverRefusal,
  leaseChoices,
  leaseDurationText,
  leaseIsExpired,
  leaseSecondsRemaining,
  NOT_ISOLATION_SENTENCE,
  parkConfirmHintText,
  parkRefusalText,
  parkedBannerFixedLine,
  parkedBannerLine,
  parkedChipTooltip,
  resumeConfirmHintText,
  toParkState,
  toResumeChecklist,
  takeoverRequired,
} from "./park";

/** A fixed "now" inside the fixture lease (parked 02:00, expires 06:00 local). */
const NOW_MS = Date.parse("2026-08-24T02:13:00+10:00");

function refusal(code: Parameters<typeof parkRefusalEnvelope>[0]) {
  const envelope = parkRefusalEnvelope(code);
  return { code: envelope.code, message: envelope.message, details: envelope.details };
}

describe("park_state decoding", () => {
  it("narrows the projection's own fields", () => {
    const state = toParkState(parkState());
    expect(state).not.toBeNull();
    expect(state!.parked).toBe(true);
    expect(state!.origin).toBe("operator");
    expect(state!.maxTotalS).toBe(14400);
    expect(state!.remainingCapS).toBe(14400);
    expect(state!.expired).toBe(false);
    expect(state!.reason).toBe("evening standby");
    expect(state!.authorizer).toBe("operator:home");
    expect(state!.foreignRewrite).toBeNull();
    expect(state!.writeUnverified).toBeNull();
    expect(state!.foreignMode).toBeNull();
  });

  it("treats an absent or unusable value as the not-commissioned feature detection", () => {
    expect(toParkState(undefined)).toBeNull();
    expect(toParkState(null)).toBeNull();
    expect(toParkState("parked")).toBeNull();
    expect(toParkState({})).toBeNull();
    // A frame without the boolean `parked` is never half-adopted.
    expect(toParkState({ origin: "operator" })).toBeNull();
  });

  it("keeps the honest sub-states: foreign_rewrite, write_unverified, foreign_mode", () => {
    const state = toParkState(
      parkState({
        origin: "operator",
        foreign_rewrite: true,
        write_unverified: true,
        foreign_mode: { word: 3, name: "Circulation", first_observed_at: "2026-08-24T01:00:00+10:00" },
      }),
    );
    expect(state!.foreignRewrite).toBe(true);
    expect(state!.writeUnverified).toBe(true);
    expect(state!.foreignMode).toEqual({
      word: 3,
      name: "Circulation",
      firstObservedAt: "2026-08-24T01:00:00+10:00",
    });
  });

  it("narrows an unknown origin to none, never drops the frame", () => {
    const state = toParkState(parkState({ origin: "mystery" }));
    expect(state).not.toBeNull();
    expect(state!.origin).toBe("none");
  });

  it("round-trips through the snapshot fixture attachment", () => {
    const unit = withParkState(
      { unit_id: "rhs", lifecycle: "disarmed", telemetry_age_s: 2, quality: "good", requested_power: { direction: "idle", watts: 0 }, authorized_power: null, measured_watts: 0, telemetry: null },
      parkState(),
    );
    expect(toParkState(unit.park_state)).not.toBeNull();
  });
});

describe("the resume checklist decoding", () => {
  it("narrows every checklist field, nulls preserved", () => {
    const checklist = toResumeChecklist(resumeChecklist());
    expect(checklist).not.toBeNull();
    expect(checklist!.commsAgeS).toBe(1.8);
    expect(checklist!.socDriftPct).toBe(-0.6);
    expect(checklist!.socPctAtPark).toBe(64);
    expect(checklist!.measuredWattsNow).toBe(0);
    expect(checklist!.faultsWhileParked).toEqual([]);
    expect(checklist!.latchedStops).toEqual([]);
    expect(checklist!.latchedInhibit).toBe(false);
    expect(checklist!.faultsRetentionNote).not.toBe("");
  });

  it("keeps explicit nulls null and lists the latched stop ids", () => {
    const checklist = toResumeChecklist(
      resumeChecklist({
        comms_age_s: null,
        soc_drift_pct: null,
        latched_stops: ["stop-7", "stop-9"],
        latched_inhibit: true,
        faults_while_parked: ["PCS_Fault0_0"],
      }),
    );
    expect(checklist!.commsAgeS).toBeNull();
    expect(checklist!.socDriftPct).toBeNull();
    expect(checklist!.faultsWhileParked).toEqual(["PCS_Fault0_0"]);
    expect(checklist!.latchedStops).toEqual(["stop-7", "stop-9"]);
    expect(checklist!.latchedInhibit).toBe(true);
  });

  it("refuses an unusable checklist rather than fabricating one", () => {
    expect(toResumeChecklist(undefined)).toBeNull();
    expect(toResumeChecklist("ok")).toBeNull();
  });
});

describe("the lease countdown (policy, never safety)", () => {
  it("derives the remaining seconds from the lease's own expiry instant", () => {
    const state = toParkState(parkState())!;
    expect(leaseSecondsRemaining(state, NOW_MS)).toBe(
      3 * 3600 + 47 * 60,
    );
  });

  it("formats H:MM with zero-padded minutes", () => {
    expect(hmmText(3 * 3600 + 47 * 60)).toBe("3:47");
    expect(hmmText(12 * 60 + 5)).toBe("0:12");
    expect(hmmText(26 * 3600 + 3 * 60)).toBe("26:03");
    expect(hmmText(0)).toBe("0:00");
  });

  it("never counts negative: a passed instant reads zero", () => {
    const state = toParkState(parkState())!;
    expect(leaseSecondsRemaining(state, Date.parse("2026-08-24T07:00:00+10:00"))).toBe(0);
  });

  it("reads expired from the wire's own flag without waiting for the clock", () => {
    const state = toParkState(parkState({ expired: true, lease_expires_at: "2026-08-24T06:00:00+10:00" }))!;
    expect(leaseIsExpired(state, NOW_MS)).toBe(true);
  });

  it("promotes to expired when the instant passes between snapshots", () => {
    const state = toParkState(parkState({ expired: false }))!;
    const afterLease = Date.parse("2026-08-24T06:00:01+10:00");
    expect(leaseIsExpired(state, NOW_MS)).toBe(false);
    expect(leaseIsExpired(state, afterLease)).toBe(true);
  });
});

describe("the banner and chip wording", () => {
  it("carries the pinned countdown line with the H:MM figure", () => {
    const state = toParkState(parkState())!;
    expect(parkedBannerLine(state, NOW_MS)).toBe(
      "Parked — not isolation · lease expires in 3:47",
    );
  });

  it("promotes to the pinned alert wording at expiry", () => {
    const state = toParkState(parkState({ expired: true }))!;
    expect(parkedBannerLine(state, NOW_MS)).toBe(
      "Parked — not isolation · lease expired — Resume required",
    );
  });

  it("always carries the fixed not-isolation sentence verbatim", () => {
    expect(parkedBannerFixedLine()).toContain(NOT_ISOLATION_SENTENCE);
    expect(NOT_ISOLATION_SENTENCE).toBe(
      "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod.",
    );
  });

  it("names the mode word and pack voltage in the chip tooltip, never a metaphor", () => {
    expect(parkedChipTooltip(1, 166.4, toParkState(parkState())!)).toBe(
      "mode word 1 (Standby) · pack 166.4 V — the battery stays connected at full voltage",
    );
    expect(parkedChipTooltip(0, null, null)).toBe(
      "mode word 0 (Normal) · pack voltage not available — the battery stays connected at full voltage",
    );
    expect(parkedChipTooltip(null, 166.4, null)).toContain("mode word not available");
    // A foreign vendor mode names its own word and name.
    expect(
      parkedChipTooltip(null, 166.4, toParkState(parkState({ foreign_mode: { word: 3, name: "Circulation", first_observed_at: "2026-08-24T01:00:00+10:00" } }))!),
    ).toContain("mode word 3 (Circulation)");
  });
});

describe("the lease-duration choices", () => {
  it("bounds every choice to the site's budget and offers the cap, named as the cap", () => {
    const state = toParkState(parkState({ max_total_s: 14400, remaining_cap_s: 14400 }))!;
    const choices = leaseChoices(state);
    expect(choices.length).toBeGreaterThan(0);
    for (const choice of choices) {
      expect(choice.seconds).toBeGreaterThanOrEqual(60);
      expect(choice.seconds).toBeLessThanOrEqual(14400);
    }
    const cap = choices.find((choice) => choice.seconds === 14400);
    expect(cap).toBeDefined();
    expect(cap!.label).toBe("4 h");
    // The site maximum is named when it is not a ladder step.
    const odd = leaseChoices(toParkState(parkState({ max_total_s: 9000, remaining_cap_s: 9000 }))!);
    expect(odd.some((choice) => choice.label === "150 min (site maximum)")).toBe(true);
  });

  it("bounds renewal-shaped budgets by the remaining cap (anti-rollover)", () => {
    const state = toParkState(
      parkState({ max_total_s: 14400, remaining_cap_s: 1800 }),
    )!;
    const choices = leaseChoices(state);
    for (const choice of choices) {
      expect(choice.seconds).toBeLessThanOrEqual(1800);
    }
    expect(choices.some((choice) => choice.seconds === 1800)).toBe(true);
  });

  it("builds the ladder from the site cap alone when no lease is open (the corrected wire)", () => {
    // The realistic not-parked frame on a commissioned site: max_total_s is
    // the SITE's cap, remaining_cap_s is null (lease-relative, no lease).
    // The ladder must build from the cap alone — this is the exact frame the
    // park dialog opens against, and the empty-ladder bug that blocked every
    // console park.
    const state = toParkState(notParkedParkState({ max_total_s: 7200 }))!;
    expect(state.parked).toBe(false);
    const choices = leaseChoices(state);
    expect(choices.length).toBeGreaterThan(0);
    for (const choice of choices) {
      expect(choice.seconds).toBeGreaterThanOrEqual(60);
      expect(choice.seconds).toBeLessThanOrEqual(7200);
    }
    expect(choices.some((choice) => choice.seconds === 7200)).toBe(true);
    expect(choices.some((choice) => choice.seconds === 14400)).toBe(false);
  });

  it("offers nothing below the wire's 60-second floor and nothing without a budget", () => {
    expect(leaseChoices(null)).toEqual([]);
    expect(
      leaseChoices(toParkState(parkState({ max_total_s: 30, remaining_cap_s: 30 }))!),
    ).toEqual([]);
  });

  it("words durations the operator reads: whole hours, else whole minutes", () => {
    expect(leaseDurationText(3600)).toBe("1 h");
    expect(leaseDurationText(14400)).toBe("4 h");
    expect(leaseDurationText(5400)).toBe("90 min");
    expect(leaseDurationText(60)).toBe("1 min");
  });
});

describe("the takeover rule", () => {
  it("requires the acknowledgement exactly for the no-lease word-parked origins", () => {
    expect(takeoverRequired(toParkState(parkState({ origin: "foreign" }))!)).toBe(true);
    expect(takeoverRequired(toParkState(parkState({ origin: "unrecorded" }))!)).toBe(true);
    expect(takeoverRequired(toParkState(parkState({ origin: "operator" }))!)).toBe(false);
    expect(takeoverRequired(toParkState(parkState({ origin: "operator", expired: true }))!)).toBe(false);
    expect(takeoverRequired(null)).toBe(false);
  });

  it("routes the takeover from the refusal envelope itself (the 409 IS the routing)", () => {
    expect(isTakeoverRefusal(refusal("park_foreign_word_acknowledgement_required"))).toBe(true);
    expect(isTakeoverRefusal(refusal("park_write_failed"))).toBe(false);
  });
});

describe("the guarded dialogs' disabled-reason hints", () => {
  it("names exactly the missing park inputs — one sentence, lowercase after the separator", () => {
    const all = { reasonValid: true, typedMatches: true, leaseChosen: true };
    expect(parkConfirmHintText("rhs", all)).toBe("");
    expect(parkConfirmHintText("rhs", { ...all, reasonValid: false })).toBe("Reason required.");
    expect(parkConfirmHintText("rhs", { ...all, leaseChosen: false })).toBe("Lease duration required.");
    expect(parkConfirmHintText("rhs", { ...all, typedMatches: false })).toBe("Type rhs to enable park.");
    expect(parkConfirmHintText("mid", { ...all, typedMatches: false })).toBe("Type mid to enable park.");
    expect(parkConfirmHintText("rhs", { ...all, reasonValid: false, typedMatches: false })).toBe(
      "Reason required · type rhs to enable park.",
    );
    expect(
      parkConfirmHintText("rhs", { reasonValid: false, typedMatches: false, leaseChosen: false }),
    ).toBe("Reason required · lease duration required · type rhs to enable park.");
  });

  it("names the resume dialog's one enable condition", () => {
    expect(resumeConfirmHintText(true)).toBe("Acknowledge the takeover to enable resume.");
    expect(resumeConfirmHintText(false)).toBe("");
  });
});

describe("every pinned 409 refusal shape renders from its details", () => {
  it("park_not_commissioned: the block-absent and mode splits", () => {
    expect(parkRefusalText(refusal("park_not_commissioned"))).toContain(
      "the parking config block is absent",
    );
    const envelope = parkRefusalEnvelope("park_not_commissioned", {
      details: { cause: "mode_not_write_enabled" },
    });
    expect(
      parkRefusalText({ code: envelope.code, message: envelope.message, details: envelope.details }),
    ).toContain("not in write-enabled mode");
  });

  it("park_conflict_refused: one row per refusing unit, cause named", () => {
    const text = parkRefusalText(refusal("park_conflict_refused"));
    expect(text).toContain("rhs — armed");
    expect(text).toContain("Disarm or acknowledge first");
  });

  it("park_already_parked: renew instead", () => {
    expect(parkRefusalText(refusal("park_already_parked"))).toContain("Already parked");
  });

  it("park_mode_out_of_scope: the vendor's own mode word is never transitioned", () => {
    const text = parkRefusalText(refusal("park_mode_out_of_scope"));
    expect(text).toContain("Circulation");
    expect(text).toContain("mode word 3");
    expect(text).toContain("never transitions a mode it did not set");
  });

  it("park_write_failed: the transport class, nothing changed", () => {
    const text = parkRefusalText(refusal("park_write_failed"));
    expect(text).toContain("gateway_timeout");
    expect(text).toContain("nothing was changed");
  });

  it("park_readback_unverified: wrote/read back/retries, no lease minted", () => {
    const text = parkRefusalText(refusal("park_readback_unverified"));
    expect(text).toContain("wrote 1, read back 0");
    expect(text).toContain("after 1 retry");
    expect(text).toContain("No lease was minted");
  });

  it("park_foreign_word_acknowledgement_required: the takeover is deliberate", () => {
    const text = parkRefusalText(refusal("park_foreign_word_acknowledgement_required"));
    expect(text).toContain("no lease from this controller");
    expect(text).toContain("observed since 01:12");
    expect(text).toContain("confirm the foreign-park acknowledgement");
  });

  it("park_lease_cap_reached: the anti-rollover bound in words", () => {
    const text = parkRefusalText(refusal("park_lease_cap_reached"));
    expect(text).toContain("maximum 4 h");
    expect(text).toContain("parked at 02:00");
  });

  it("park_lease_absent: the closing row's origin and time, read defensively", () => {
    const text = parkRefusalText(refusal("park_lease_absent"));
    expect(text).toContain("closed by operator");
    expect(text).toContain("at 05:31");
  });

  it("resume_stop_latched: the stop ids, acknowledge first", () => {
    const text = parkRefusalText(refusal("resume_stop_latched"));
    expect(text).toContain("stop-7");
    expect(text).toContain("Acknowledge the stop first");
  });

  it("an unknown code renders no sentence: the envelope alone speaks", () => {
    expect(
      parkRefusalText({ code: "park_mystery", message: "?", details: null }),
    ).toBe("");
  });
});
