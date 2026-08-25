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
  toHealthWatchState,
} from "./healthWatch";

function unit(spec: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    unit_id: "rhs",
    census: { verdict: "nominal", nights: 0, predicates: null, tier: null },
    probe: {
      verdict: null,
      probe_w: null,
      qualifying_samples: null,
      core_samples: null,
      echo: null,
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
                no_ct_view: true,
                modes_normal: true,
              },
              tier: "alert",
            },
            probe: {
              verdict: "fail_no_response",
              probe_w: 300,
              qualifying_samples: 3,
              core_samples: 20,
              echo: "echo_matches_write",
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
      no_ct_view: true,
      modes_normal: true,
    });
    expect(only.probe.verdict).toBe("fail_no_response");
    expect(only.probe.probeW).toBe(300);
    expect(only.probe.qualifyingSamples).toBe(3);
    expect(only.probe.coreSamples).toBe(20);
    expect(only.probe.echo).toBe("echo_matches_write");
    expect(only.recovery.mode).toBe("uncommissioned");
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
            },
          }),
        ],
      }),
    )!.units[0]!;
    expect(probeVerdictText(failing)).toBe(
      "fail — no response — 3/20 samples moved · echo followed the write, the battery stayed still",
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
    expect(healthWatchAlertText(failed)).toContain("no automated recovery exists");
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
