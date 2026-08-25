/**
 * Behavior contract for the nightly health watch's wire model
 * (web/src/app/healthWatch.ts): the §10 projection's narrowing (nulls are
 * nulls, never zeros; a garbage frame is never half-adopted; absent fields
 * inherit the base) and the plain-word maps the Home card renders (the
 * program status sentence, the census chip with its persistence nights, the
 * probe verdict lines with their one-line figures, the skip reasons
 * verbatim, the uncommissioned recovery honesty, and §6.3's export note).
 */
import { describe, expect, it } from "vitest";
import {
  HEALTH_WATCH_EXPORT_NOTE,
  HEALTH_WATCH_RESTART_ADVISORY,
  anyUnitParked,
  censusChipText,
  healthWatchAlertText,
  healthWatchStatusText,
  healthWatchStagesText,
  healthWatchUnitRowText,
  probeSkipText,
  probeVerdictText,
  recoveryMorningText,
  recoveryRouteText,
  recoveryWalkthroughText,
  toHealthWatchState,
} from "./healthWatch";

function unit(spec: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    unit_id: "rhs",
    census: { verdict: "nominal", nights: 0, predicates: null, tier: null, note: null },
    probe: {
      verdict: null,
      probe_w: null,
      qualifying_samples: null,
      core_samples: null,
      echo: null,
      consecutive_fail_nights: null,
    },
    recovery: { mode: "uncommissioned" },
    ...spec,
  };
}

function projection(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    stages: ["census", "probe"],
    window: { opens_local: "23:00", deadline_local: "23:45" },
    phase: "done",
    night: "2026-08-25",
    reason: null,
    as_of: "2026-08-25T23:41:05+00:00",
    units: [unit()],
    ...spec,
  };
}

describe("toHealthWatchState", () => {
  it("narrows the §10 projection with every field honest", () => {
    const parsed = toHealthWatchState(
      projection({
        units: [
          unit({
            census: {
              verdict: "stuck_suspected",
              nights: 2,
              predicates: {
                full: true,
                still: true,
                house_needed: true,
                load_unserved_in_phase: true,
                modes_normal: true,
              },
              tier: "alert",
              note: null,
            },
            probe: {
              verdict: "fail_no_response",
              probe_w: 300,
              qualifying_samples: 3,
              core_samples: 20,
              echo: "echo_matches_write",
              consecutive_fail_nights: 1,
            },
            recovery: { mode: "uncommissioned" },
          }),
        ],
      }),
    );
    expect(parsed).not.toBeNull();
    expect(parsed!.stages).toEqual(["census", "probe"]);
    expect(parsed!.window).toEqual({ opensLocal: "23:00", deadlineLocal: "23:45" });
    expect(parsed!.phase).toBe("done");
    expect(parsed!.night).toBe("2026-08-25");
    const only = parsed!.units[0]!;
    expect(only.unitId).toBe("rhs");
    expect(only.census.verdict).toBe("stuck_suspected");
    expect(only.census.nights).toBe(2);
    expect(only.census.tier).toBe("alert");
    expect(only.census.predicates).toEqual({
      full: true,
      still: true,
      house_needed: true,
      load_unserved_in_phase: true,
      modes_normal: true,
    });
    expect(only.census.note).toBeNull();
    expect(only.probe.verdict).toBe("fail_no_response");
    expect(only.probe.probeW).toBe(300);
    expect(only.probe.qualifyingSamples).toBe(3);
    expect(only.probe.coreSamples).toBe(20);
    expect(only.probe.echo).toBe("echo_matches_write");
    expect(only.probe.consecutiveFailNights).toBe(1);
    expect(only.recovery.mode).toBe("uncommissioned");
  });

  it("narrows A16's soft note, its annotator, and route B's figures", () => {
    const parsed = toHealthWatchState(
      projection({
        units: [
          unit({
            census: {
              verdict: "phase_idle_or_ct_silent",
              nights: 4,
              predicates: {
                full: true,
                still: true,
                house_needed: true,
                load_unserved_in_phase: false,
                modes_normal: true,
              },
              tier: "notice",
              note: "ct_link_suspect",
            },
            probe: {
              verdict: "fail_no_response",
              probe_w: 300,
              qualifying_samples: 3,
              core_samples: 20,
              echo: "echo_matches_write",
              consecutive_fail_nights: 2,
            },
            recovery: {
              mode: "advise",
              verdict: "advised",
              tier: "alert",
              posture: "advise",
              route: "b",
              attempts_total: 0,
              consecutive_fails: 0,
            },
          }),
        ],
      }),
    );
    const only = parsed!.units[0]!;
    expect(only.census.verdict).toBe("phase_idle_or_ct_silent");
    expect(only.census.note).toBe("ct_link_suspect");
    expect(only.census.tier).toBe("notice");
    expect(only.census.predicates!.load_unserved_in_phase).toBe(false);
    expect(only.probe.consecutiveFailNights).toBe(2);
    expect(only.recovery.route).toBe("b");
    // An unknown route word narrows to null, never a half-adopted string.
    const odd = toHealthWatchState(
      projection({ units: [unit({ recovery: { mode: "advise", route: "c" } })] }),
    )!;
    expect(odd.units[0]!.recovery.route).toBeNull();
  });

  it("never half-adopts a garbage frame", () => {
    expect(toHealthWatchState(null)).toBeNull();
    expect(toHealthWatchState("nope")).toBeNull();
    expect(toHealthWatchState([])).toBeNull();
  });

  it("falls back per field on a partial frame and inherits from the base", () => {
    const parsed = toHealthWatchState(projection({ night: null }));
    expect(parsed!.night).toBeNull();
    expect(parsed!.units).toHaveLength(1);
    const patched = toHealthWatchState({ phase: "probe" }, parsed);
    expect(patched!.phase).toBe("probe");
    expect(patched!.window.opensLocal).toBe("23:00");
    expect(patched!.units).toHaveLength(1);
  });

  it("drops unusable unit rows instead of inventing them", () => {
    const parsed = toHealthWatchState(projection({ units: [{ unit_id: "" }, 7, unit()] }));
    expect(parsed!.units.map((entry) => entry.unitId)).toEqual(["rhs"]);
  });
});

describe("healthWatchStatusText", () => {
  it("names the phase and the window in plain words", () => {
    expect(healthWatchStatusText(toHealthWatchState(projection({ phase: "await_window" }))!)).toBe(
      "The nightly health watch opens at 23:00.",
    );
    expect(healthWatchStatusText(toHealthWatchState(projection({ phase: "census" }))!)).toContain(
      "census",
    );
    expect(healthWatchStatusText(toHealthWatchState(projection({ phase: "probe" }))!)).toContain(
      "one-at-a-time",
    );
    expect(healthWatchStatusText(toHealthWatchState(projection())!)).toBe(
      "Tonight's health watch is complete.",
    );
  });

  it("renders the program-level reasons as their honest stories", () => {
    expect(
      healthWatchStatusText(toHealthWatchState(projection({ reason: "window_not_quiet" }))!),
    ).toContain("deferred rather than fight");
    expect(
      healthWatchStatusText(toHealthWatchState(projection({ reason: "interrupted" }))!),
    ).toContain("interrupted");
    expect(
      healthWatchStatusText(toHealthWatchState(projection({ reason: "outside_window" }))!),
    ).toContain("no act starts after the deadline");
  });
});

describe("healthWatchStagesText", () => {
  it("includes the not-commissioned honesty for absent stages", () => {
    expect(healthWatchStagesText(toHealthWatchState(projection())!)).toBe(
      "census · probe · recovery not commissioned",
    );
    expect(
      healthWatchStagesText(
        toHealthWatchState(projection({ stages: ["census", "probe", "recovery"] }))!,
      ),
    ).toBe("census · probe · recovery");
  });
});

describe("censusChipText", () => {
  it("renders nominal, the flag with its nights, degraded, and excluded classes", () => {
    expect(censusChipText(toHealthWatchState(projection())!.units[0]!)).toBe("nominal");
    const flagged = toHealthWatchState(
      projection({
        units: [
          unit({
            census: { verdict: "stuck_suspected", nights: 2, predicates: null, tier: "alert" },
          }),
        ],
      }),
    )!.units[0]!;
    expect(censusChipText(flagged)).toBe("flagged 2 nights");
    const degraded = toHealthWatchState(
      projection({ units: [unit({ census: { verdict: "degraded_evidence", nights: 0 } })] }),
    )!.units[0]!;
    expect(censusChipText(degraded)).toBe("evidence degraded");
    const parked = toHealthWatchState(
      projection({ units: [unit({ census: { verdict: "excluded:parked", nights: 0 } })] }),
    )!.units[0]!;
    expect(censusChipText(parked)).toBe("parked");
    expect(censusChipText(toHealthWatchState(projection({ units: [unit()] }))!.units[0]!)).not
      .toBe("not yet read");
  });

  it("says not-yet-read while the night is still opening", () => {
    const unread = toHealthWatchState(
      projection({ units: [unit({ census: { verdict: null, nights: 0 } })] }),
    )!.units[0]!;
    expect(censusChipText(unread)).toBe("not yet read");
  });

  it("renders A16's soft note as the note it is, with its annotator", () => {
    const noted = toHealthWatchState(
      projection({
        units: [
          unit({
            census: {
              verdict: "phase_idle_or_ct_silent",
              nights: 4,
              predicates: null,
              tier: "notice",
              note: "ct_link_suspect",
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(censusChipText(noted)).toBe("idle phase or CT silent 4 nights · CT link suspect");
    const cleared = toHealthWatchState(
      projection({
        units: [
          unit({
            census: {
              verdict: "phase_idle_or_ct_silent",
              nights: 1,
              predicates: null,
              tier: "notice",
              note: null,
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(censusChipText(cleared)).toBe("idle phase or CT silent");
  });
});

describe("probeVerdictText", () => {
  it("carries the one-line figures on the verdict classes", () => {
    const failing = toHealthWatchState(
      projection({
        units: [
          unit({
            probe: {
              verdict: "fail_no_response",
              probe_w: 300,
              qualifying_samples: 3,
              core_samples: 20,
              echo: "echo_matches_write",
              consecutive_fail_nights: 1,
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(probeVerdictText(failing)).toBe(
      "fail — no response — 3/20 samples moved · echo followed the write, the battery stayed still",
    );
    // Route B's repetition evidence rides the same line once it is a streak.
    const repeated = toHealthWatchState(
      projection({
        units: [
          unit({
            probe: {
              verdict: "fail_no_response",
              probe_w: 300,
              qualifying_samples: 3,
              core_samples: 20,
              echo: "echo_matches_write",
              consecutive_fail_nights: 2,
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(probeVerdictText(repeated)).toBe(
      "fail — no response — 3/20 samples moved · echo followed the write, the battery stayed still · 2 nights in a row",
    );
    const passing = toHealthWatchState(
      projection({
        units: [
          unit({
            probe: {
              verdict: "pass",
              probe_w: 300,
              qualifying_samples: 20,
              core_samples: 20,
              echo: "echo_matches_write",
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(probeVerdictText(passing)).toBe("pass — 20/20 samples delivered");
  });

  it("renders each inconclusive word as its own honest story", () => {
    const words: Array<[string, RegExp]> = [
      ["inconclusive_echo_mismatch", /advisory only/],
      ["inconclusive_baseline_confounded", /demand moved/],
      ["inconclusive_preempted", /higher-priority/],
      ["inconclusive_aborted", /next night is another chance/],
      ["fail_baseline_not_returned", /did not stop cleanly/],
    ];
    for (const [verdict, pattern] of words) {
      const parsed = toHealthWatchState(
        projection({ units: [unit({ probe: { verdict, probe_w: 300 } })] }),
      )!.units[0]!;
      expect(probeVerdictText(parsed)).toMatch(pattern);
    }
  });

  it("renders unknown verdicts verbatim, never silence", () => {
    const parsed = toHealthWatchState(
      projection({ units: [unit({ probe: { verdict: "fail_exotic" } })] }),
    )!.units[0]!;
    expect(probeVerdictText(parsed)).toBe("fail_exotic");
  });

  it("says not-probed-yet while the night is still opening", () => {
    const parsed = toHealthWatchState(projection())!.units[0]!;
    expect(probeVerdictText(parsed)).toBe("not probed yet");
  });
});

describe("probeSkipText", () => {
  it("renders the pinned skip reasons, the disarmed one as the arm instruction", () => {
    expect(probeSkipText("unit_disarmed")).toContain("the program never arms");
    expect(probeSkipText("quiet_evidence_stale")).toContain("quiet-load");
    expect(probeSkipText("census_excluded")).toContain("another surface owns its state");
    expect(probeSkipText("some_future_reason")).toBe("skipped — some_future_reason");
  });
});

describe("the row and the alert story", () => {
  it("composes one unit's whole row", () => {
    const parsed = toHealthWatchState(projection())!.units[0]!;
    expect(healthWatchUnitRowText(parsed)).toBe(
      "rhs — census nominal · probe not probed yet · recovery not commissioned",
    );
  });

  it("names the first alert-tier fact across the fleet", () => {
    const failed = toHealthWatchState(
      projection({
        units: [
          unit({
            census: { verdict: "stuck_suspected", nights: 2, tier: "alert" },
            probe: { verdict: "fail_no_response", probe_w: 300 },
          }),
        ],
      }),
    )!;
    expect(healthWatchAlertText(failed)).toContain("FAILED on rhs");
    // The Stage-R-era truth: the alert names the site's own recovery posture
    // (advise writes nothing; auto hands the night to the recovery stage).
    expect(healthWatchAlertText(failed)).toContain("recovery stage is advise");
    expect(healthWatchAlertText(failed)).toContain("writes nothing");
    const flagged = toHealthWatchState(
      projection({
        units: [
          unit({
            census: { verdict: "stuck_suspected", nights: 3, tier: "alert" },
          }),
        ],
      }),
    )!;
    expect(healthWatchAlertText(flagged)).toContain("persisted on rhs");
    expect(healthWatchAlertText(toHealthWatchState(projection())!)).toBeNull();
  });

  it("pins the export note and the defined-restart advisory verbatim", () => {
    expect(HEALTH_WATCH_EXPORT_NOTE).toContain("export up to its own magnitude");
    expect(HEALTH_WATCH_RESTART_ADVISORY).toContain("WAIT 10 MINUTES");
    expect(HEALTH_WATCH_RESTART_ADVISORY).toContain("battery ON FIRST");
  });

  it("gates the parked styling on the census's parked class", () => {
    expect(anyUnitParked(toHealthWatchState(projection())!)).toBe(false);
    const parked = toHealthWatchState(
      projection({ units: [unit({ census: { verdict: "excluded:parked", nights: 0 } })] }),
    )!;
    expect(anyUnitParked(parked)).toBe(true);
  });
});

describe("the recovery surface (§7/§8/§11/§13, the Stage R wave)", () => {
  const recoveryUnit = (recovery: Record<string, unknown>): Record<string, unknown> =>
    unit({
      census: { verdict: "stuck_suspected", nights: 2, tier: "alert" },
      probe: {
        verdict: "fail_no_response",
        probe_w: 300,
        qualifying_samples: 3,
        core_samples: 20,
        echo: "echo_matches_write",
      },
      recovery,
    });

  it("narrows the recovery half with the A11 morning figures", () => {
    const parsed = toHealthWatchState(
      projection({
        stages: ["census", "probe", "recovery"],
        units: [
          recoveryUnit({
            mode: "auto",
            verdict: "recovered",
            tier: "resolved",
            posture: "auto",
            rung: "verified",
            reason: null,
            left_armed: false,
            attempts_total: 4,
            consecutive_fails: 0,
            consecutive_fail_limit: 3,
            trailing_30_nights: { attempted: 11, recovered: 9, attempt_rate: 0.3667 },
          }),
        ],
      }),
    );
    const recovery = parsed!.units[0]!.recovery;
    expect(recovery.mode).toBe("auto");
    expect(recovery.verdict).toBe("recovered");
    expect(recovery.tier).toBe("resolved");
    expect(recovery.rung).toBe("verified");
    expect(recovery.leftArmed).toBe(false);
    expect(recovery.attemptsTotal).toBe(4);
    expect(recovery.consecutiveFails).toBe(0);
    expect(recovery.consecutiveFailLimit).toBe(3);
    expect(recovery.trailing).toEqual({ attempted: 11, recovered: 9, attemptRate: 0.3667 });
  });

  it("renders each recovery verdict with its figures and cap arithmetic", () => {
    const at = (recovery: Record<string, unknown>): string =>
      healthWatchUnitRowText(
        toHealthWatchState(projection({ units: [recoveryUnit(recovery)] }))!.units[0]!,
      );
    expect(
      at({ mode: "advise", verdict: "advised", tier: "alert", attempts_total: 0 }),
    ).toContain("advisory rendered");
    expect(
      at({
        mode: "auto",
        verdict: "recovered",
        tier: "resolved",
        attempts_total: 2,
        consecutive_fails: 0,
      }),
    ).toContain("recovered — the standby cycle ran");
    expect(
      at({ mode: "auto", verdict: "recovered_unproven", tier: "alert", rung: "rearm_refused" }),
    ).toContain("proof missing");
    expect(at({ mode: "auto", verdict: "failed_no_effect", tier: "alert" })).toContain(
      "still did not follow commands",
    );
    expect(at({ mode: "auto", verdict: "write_unverified", tier: "alert" })).toContain(
      "readback never confirmed",
    );
    expect(
      at({
        mode: "auto",
        verdict: "advisory_only",
        tier: "alert",
        consecutive_fails: 3,
        consecutive_fail_limit: 3,
      }),
    ).toContain("advisory-only");
    expect(at({ mode: "auto", verdict: "skipped:foreign_standby", tier: "alert" })).toContain(
      "Standby and it is not ours",
    );
  });

  it("keeps the uncommissioned honesty: no verdict is ever invented", () => {
    const parsed = toHealthWatchState(projection({ units: [unit()] }));
    expect(parsed!.units[0]!.recovery.mode).toBe("uncommissioned");
    expect(parsed!.units[0]!.recovery.verdict).toBeNull();
    expect(parsed!.units[0]!.recovery.attemptsTotal).toBe(0);
    expect(healthWatchUnitRowText(parsed!.units[0]!)).toContain(
      "recovery not commissioned",
    );
  });

  it("renders the advise walkthrough and the auto morning re-arm (§13)", () => {
    const advise = toHealthWatchState(
      projection({ units: [recoveryUnit({ mode: "advise", verdict: "advised" })] }),
    )!.units[0]!;
    expect(recoveryWalkthroughText(advise)).toContain("disarm → park → resume → re-arm → verify");
    expect(recoveryWalkthroughText(advise)).toContain("writes nothing");
    const auto = toHealthWatchState(
      projection({
        units: [
          recoveryUnit({
            mode: "auto",
            verdict: "recovered",
            tier: "resolved",
            attempts_total: 2,
          }),
        ],
      }),
    )!.units[0]!;
    expect(recoveryWalkthroughText(auto)).toContain("left the battery disarmed");
    expect(recoveryWalkthroughText(auto)).toContain("re-arm it this morning");
    // Not eligible: no advisory card at all.
    expect(recoveryWalkthroughText(toHealthWatchState(projection())!.units[0]!)).toBeNull();
  });

  it("renders the recovered morning line with A11's trailing-30 rate", () => {
    const recovered = toHealthWatchState(
      projection({
        units: [
          recoveryUnit({
            mode: "auto",
            verdict: "recovered",
            tier: "resolved",
            attempts_total: 7,
            trailing_30_nights: { attempted: 6, recovered: 5, attempt_rate: 0.2 },
          }),
        ],
      }),
    )!.units[0]!;
    const morning = recoveryMorningText(recovered);
    expect(morning).toContain("Before: stuck signature + a no-response probe");
    expect(morning).toContain("6 cycle nights in the last 30 (20%)");
    expect(recoveryMorningText(toHealthWatchState(projection())!.units[0]!)).toBeNull();
  });

  it("names route B's own eligibility story where route A stays quiet (A16)", () => {
    const byRoute = (route: string | null): Record<string, unknown> =>
      recoveryUnit({ mode: "advise", verdict: "advised", tier: "alert", route });
    const routeB = toHealthWatchState(projection({ units: [byRoute("b")] }))!.units[0]!;
    expect(recoveryRouteText(routeB)).toContain("route B");
    expect(recoveryRouteText(routeB)).toContain("cannot see this pod's phase");
    expect(recoveryRouteText(routeB)).toContain("two consecutive nightly probe failures");
    const routeA = toHealthWatchState(projection({ units: [byRoute("a")] }))!.units[0]!;
    expect(recoveryRouteText(routeA)).toBeNull();
    expect(recoveryRouteText(toHealthWatchState(projection())!.units[0]!)).toBeNull();
    // The recovered morning line carries the route's own "before" story.
    const recoveredB = toHealthWatchState(
      projection({
        units: [
          recoveryUnit({
            mode: "auto",
            verdict: "recovered",
            tier: "resolved",
            route: "b",
            attempts_total: 1,
          }),
        ],
      }),
    )!.units[0]!;
    expect(recoveryMorningText(recoveredB)).toContain(
      "Before: a phase the census cannot see + repeated no-response probes",
    );
  });

  it("puts a recovery outcome FIRST in the alert story (§11's urgent styling)", () => {
    const alert = healthWatchAlertText(
      toHealthWatchState(
        projection({
          units: [
            recoveryUnit({
              mode: "auto",
              verdict: "write_unverified",
              tier: "alert",
              attempts_total: 1,
            }),
          ],
        }),
      )!,
    );
    expect(alert).toContain("Recovery outcome on rhs");
    expect(alert).toContain("defined-restart advisory is below");
  });
});
