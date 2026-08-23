/**
 * Behavior contract for the self-healing awareness layer's console half
 * (API_CONTRACTS.md "Self-healing awareness layer (recovery detection)",
 * energyypod.application.recovery): the wire parsing (null-safe; absent
 * fields = feature absent = render nothing), the plain-language maps, and the
 * one shared per-unit badge's silence rules.
 *
 * WIRE TRUTH (src/energypod/application/recovery.py + service.py
 * `_health_projection`, mirrored in web/src/test/wire.ts):
 * - Snapshot units carry `health_state` / `health_reasons` / `remediation_hint`
 *   — nulls when no monitor is wired or its projection fails; the keys ride
 *   every unit once the layer is composed and are ABSENT on the older wire.
 * - The state vocabulary is the monitor's own StrEnum (lowercase): healthy,
 *   self_healing, actuation_incoherent, not_responding, unreachable,
 *   foreign_writer, inhibited.
 * - `remediation_hint` is non-null ONLY where remote recovery is genuinely
 *   exhausted, and the console renders it VERBATIM.
 *
 * DESIGN PINS (the quietness rules, pinned per state):
 * - healthy renders NOTHING — silence is the design.
 * - A null/absent/unusable health renders NOTHING — that is the feature
 *   detection, never "healthy".
 * - foreign_writer and inhibited render nothing HERE: the existing inhibit
 *   surfaces (the latch line, the acknowledge control, the Inhibited badge)
 *   already tell those stories; duplicating them would be noise.
 * - self_healing is quiet and positive, worded by the backend's own reason
 *   codes; actuation_incoherent carries the plain story with the battery's
 *   OWN authorized figure; not_responding is the prominent honest terminal.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  applyHealthPatch,
  toActuationIncoherentEvent,
  toHealthChangedEvent,
  toUnexpectedAutonomyEvent,
  toUnitHealth,
  type UnitHealth,
} from "./fleet";
import {
  echoDiscriminatorText,
  incoherenceAnnouncement,
  incoherenceStory,
  UnitHealthTag,
} from "./unitHealth";

function health(
  state: UnitHealth["state"],
  reasons: string[] = [],
  remediationHint: string | null = null,
): UnitHealth {
  return { state, reasons, remediationHint };
}

describe("toUnitHealth — snapshot health fields (null-safe, feature-detected)", () => {
  it("accepts every vocabulary state with its reasons and hint", () => {
    for (const state of [
      "healthy",
      "self_healing",
      "actuation_incoherent",
      "not_responding",
      "unreachable",
      "foreign_writer",
      "inhibited",
    ] as const) {
      expect(
        toUnitHealth({
          health_state: state,
          health_reasons: ["reads_timing_out"],
          remediation_hint: "pod not responding — physical restart required",
        }),
      ).toEqual(health(state, ["reads_timing_out"], "pod not responding — physical restart required"));
    }
  });

  it("is null for an absent, null, non-string, or out-of-vocabulary state — never a fabricated one", () => {
    expect(toUnitHealth({})).toBeNull();
    expect(toUnitHealth({ health_state: null, health_reasons: null, remediation_hint: null })).toBeNull();
    expect(toUnitHealth({ health_state: 7 })).toBeNull();
    // A future backend vocabulary word is not rendered as a state we do not know.
    expect(toUnitHealth({ health_state: "quantum_healing" })).toBeNull();
    expect(toUnitHealth(null)).toBeNull();
    expect(toUnitHealth("healthy")).toBeNull();
  });

  it("drops non-string reason entries and blank hints without losing the state", () => {
    expect(
      toUnitHealth({
        health_state: "self_healing",
        health_reasons: ["autonomous_self_charge", 4, "", null],
        remediation_hint: "",
      }),
    ).toEqual(health("self_healing", ["autonomous_self_charge"], null));
  });
});

describe("toHealthChangedEvent — the unit.health_changed payload", () => {
  it("narrows a real transition", () => {
    expect(
      toHealthChangedEvent({ unit_id: "mid", from: "healthy", to: "self_healing", reasons: ["cell_balancing"] }),
    ).toEqual({ unitId: "mid", from: "healthy", to: "self_healing", reasons: ["cell_balancing"] });
  });

  it("is null without a unit id or with an unknown target state", () => {
    expect(toHealthChangedEvent({ from: "healthy", to: "healthy" })).toBeNull();
    expect(toHealthChangedEvent({ unit_id: "", to: "healthy" })).toBeNull();
    expect(toHealthChangedEvent({ unit_id: "mid", to: "quantum_healing" })).toBeNull();
    expect(toHealthChangedEvent("noise")).toBeNull();
  });
});

describe("toActuationIncoherentEvent — the watchdog payload and its echo follow-up", () => {
  it("narrows the episode opener, every figure nullable", () => {
    expect(
      toActuationIncoherentEvent({
        unit_id: "mid",
        cycles: 4,
        authorized_watts: 1000,
        authorized_direction: "discharge",
        measured_watts: 12,
        baseline_watts: 8,
        movement_watts: 4,
      }),
    ).toEqual({
      unitId: "mid",
      cycles: 4,
      authorizedWatts: 1000,
      authorizedDirection: "discharge",
      measuredWatts: 12,
      baselineWatts: 8,
      movementWatts: 4,
      echoClassification: null,
      servedActiveW: null,
      servedReactiveVar: null,
    });
  });

  it("carries the discriminator fields only when the echo frame carries them", () => {
    const parsed = toActuationIncoherentEvent({
      unit_id: "mid",
      echo_classification: "echo_matches_write",
      served_active_w: 1000,
      served_reactive_var: 0,
    });
    expect(parsed?.echoClassification).toBe("echo_matches_write");
    expect(parsed?.servedActiveW).toBe(1000);
    expect(parsed?.servedReactiveVar).toBe(0);
  });

  it("is null without a unit id", () => {
    expect(toActuationIncoherentEvent({ cycles: 4 })).toBeNull();
    expect(toActuationIncoherentEvent(null)).toBeNull();
  });
});

describe("toUnexpectedAutonomyEvent — the quiet evidence payload", () => {
  it("narrows the pinned figures, every one nullable", () => {
    expect(
      toUnexpectedAutonomyEvent({
        unit_id: "mid",
        measured_watts: 1411.2,
        soc_pct: 10,
        debug_mode_w: null,
        ctrl_mode_w: 1,
        work_mode_w: 7,
        run_mode_w: 0,
      }),
    ).toEqual({
      unitId: "mid",
      measuredWatts: 1411.2,
      socPct: 10,
      debugModeW: null,
      ctrlModeW: 1,
      workModeW: 7,
      runModeW: 0,
    });
    expect(toUnexpectedAutonomyEvent({ unit_id: "mid" })?.measuredWatts).toBeNull();
    expect(toUnexpectedAutonomyEvent({ measured_watts: 100 })).toBeNull();
  });
});

describe("applyHealthPatch — event patches never erase the wire's terminal hint", () => {
  const hint = "pod not responding — remote recovery exhausted; physical restart required";
  it("keeps the snapshot's hint while the state is unchanged", () => {
    expect(applyHealthPatch(health("not_responding", ["reads_timing_out"], hint), health("not_responding", ["reads_timing_out"]))).toEqual(
      health("not_responding", ["reads_timing_out"], hint),
    );
  });

  it("resets the hint when the state actually changes (the old hint belongs to the old state)", () => {
    expect(
      applyHealthPatch(health("not_responding", [], hint), health("healthy", [])),
    ).toEqual(health("healthy", [], null));
    expect(applyHealthPatch(null, health("self_healing", ["cell_balancing"]))).toEqual(
      health("self_healing", ["cell_balancing"], null),
    );
  });
});

describe("UnitHealthTag — one badge, per state, with the design's quietness", () => {
  it.each([
    ["autonomous_self_charge", "Managing itself — solar self-charge"],
    ["requalifying_after_inhibit", "Re-qualifying"],
    ["cell_balancing", "Cell balancing"],
  ] as const)("renders the quiet positive tag for self_healing (%s)", (reason, words) => {
    const { container } = render(
      <UnitHealthTag health={health("self_healing", [reason])} />,
    );
    expect(screen.getByRole("note").textContent).toBe(words);
    expect(container.querySelector(".unit-health--healing")).not.toBeNull();
  });

  it("falls back to the honest generic line for an unrecognized self-healing reason", () => {
    render(<UnitHealthTag health={health("self_healing", ["something_new"])} />);
    expect(screen.getByRole("note").textContent).toBe("Managing itself");
  });

  it("tells the plain incoherence story with the battery's own commanded figure", () => {
    render(<UnitHealthTag health={health("actuation_incoherent")} authorizedWatts={1000} />);
    expect(screen.getByRole("note").textContent).toBe(
      "Commanded 1,000 W but the battery isn't moving — investigating",
    );
  });

  it("never fabricates a figure when no authorization is on screen", () => {
    render(<UnitHealthTag health={health("actuation_incoherent")} authorizedWatts={null} />);
    expect(screen.getByRole("note").textContent).toBe(
      "The battery isn't moving as commanded — investigating",
    );
  });

  it("renders the prominent honest terminal for not_responding, hint VERBATIM", () => {
    const hint =
      "pod not responding — remote recovery exhausted; physical restart required (power-cycle the pod, then verify telemetry resumes, Debug Mode reads Normal Mode and SysControlMode reads Remote in the vendor MiniES app — see docs/POD_RECOVERY_RESEARCH.md R5)";
    const { container } = render(
      <UnitHealthTag health={health("not_responding", ["reads_timing_out"], hint)} />,
    );
    expect(screen.getByRole("note").textContent).toContain("Not responding — remote recovery exhausted");
    // Verbatim, whole, un-reworded: the operator reads the backend's own words.
    expect(container.querySelector(".unit-health-hint")?.textContent).toBe(hint);
    expect(container.querySelector(".unit-health--critical")).not.toBeNull();
  });

  it("renders the not_responding terminal without inventing a hint when the wire sends none", () => {
    const { container } = render(<UnitHealthTag health={health("not_responding")} />);
    expect(screen.getByRole("note").textContent).toBe("Not responding — remote recovery exhausted");
    expect(container.querySelector(".unit-health-hint")).toBeNull();
  });

  it("names the gateway class for unreachable", () => {
    render(<UnitHealthTag health={health("unreachable", ["gateway_unreachable"])} />);
    expect(screen.getByRole("note").textContent).toBe("Gateway unreachable");
  });

  it.each([
    ["a feature-absent health (null)", null],
    ["a healthy unit (silence is the design)", health("healthy")],
    [
      "a foreign writer (the inhibit latch surfaces already tell it)",
      health("foreign_writer", ["external_writer_latched"]),
    ],
    ["an inhibited unit (same existing surfaces)", health("inhibited", ["inhibited"])],
  ] as const)("renders NOTHING for %s", (_name, value) => {
    const { container } = render(<UnitHealthTag health={value} authorizedWatts={1000} />);
    expect(container.querySelector(".unit-health")).toBeNull();
    expect(container.textContent).toBe("");
  });
});

describe("the incoherence alarm wording (one per episode, plain language)", () => {
  it("announces the commanded figure through the shared display formatter", () => {
    expect(incoherenceAnnouncement("pod-mid", 1000)).toBe(
      "pod-mid was commanded 1,000 W but isn't responding to control — check the battery.",
    );
  });

  it("announces without a figure rather than inventing one", () => {
    expect(incoherenceAnnouncement("pod-mid", null)).toBe(
      "pod-mid was commanded power but isn't responding to control — check the battery.",
    );
  });

  it.each([
    [
      "echo_matches_write",
      "pod-mid: the battery confirms receiving our command — the transport is fine, the fault is inside the battery; remote recovery is exhausted.",
    ],
    [
      "external_writer",
      "pod-mid: another writer is present — something else is commanding this battery.",
    ],
    [
      "objective_not_served",
      "pod-mid: the battery is not serving any command — its own mode may be blocking dispatch (check Debug Mode and SysControlMode in the vendor app).",
    ],
    [
      "echo_unreadable",
      "pod-mid: the battery's command readback could not be read — still investigating.",
    ],
  ] as const)("names the %s discriminator plainly", (classification, words) => {
    expect(echoDiscriminatorText("pod-mid", classification)).toBe(words);
  });

  it("names an unknown classification honestly, never guessing", () => {
    expect(echoDiscriminatorText("pod-mid", "echo_weekend")).toBe(
      'pod-mid: the command readback reported "echo_weekend" — still investigating.',
    );
  });

  it("keeps the badge story aligned with the announcement wording", () => {
    expect(incoherenceStory(1000)).toBe(
      "Commanded 1,000 W but the battery isn't moving — investigating",
    );
    expect(incoherenceStory(null)).toBe(
      "The battery isn't moving as commanded — investigating",
    );
  });
});
