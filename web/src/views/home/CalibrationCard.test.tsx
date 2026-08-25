/**
 * Behavior contract for the Battery calibration Home card
 * (web/src/views/home/CalibrationCard.tsx): T-CAL-CONSOLE's states — the
 * quiet no-surface feature detection, the advise posture's
 * `submits: never` line, the per-unit class rows, the live traverse line
 * with the pinned §0 sentence AND the C16 stop-route line, the
 * morning-after line with both economics branches, and the stand-down alert.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { toCalibrationState } from "../../app/calibration";
import { CalibrationCard } from "./CalibrationCard";

function projection(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    mode: "advise",
    submits: "never",
    window: { opens_local: "15:00", ends_local: "22:30" },
    phase: "planned",
    target: "mid",
    reason: null,
    as_of: "2026-08-25T14:00:12+00:00",
    units: [
      {
        unit_id: "mid",
        class: "eligible",
        days_since_deep: 65,
        horizon_bounded: true,
        last_deep_date: null,
        horizon_days: 65,
        due: true,
        evidence_short: false,
        kind: "measurement",
        anchored: false,
        standdown: false,
        throughput_wh_mean: 3300,
      },
      {
        unit_id: "lhs",
        class: "excluded_cycles_daily",
        days_since_deep: 0,
        horizon_bounded: false,
        due: false,
        sub_floor_dates_14d: 13,
      },
    ],
    last_cycle: null,
    request_measurement: null,
    ...spec,
  };
}

afterEach(cleanup);

describe("CalibrationCard", () => {
  it("renders nothing at all while the projection is absent (the block-presence doctrine)", () => {
    const { container } = render(<CalibrationCard calibration={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the advise posture's submits: never beside the plan", () => {
    render(<CalibrationCard calibration={toCalibrationState(projection())} />);
    expect(screen.getByText(/Submits: never/iu)).not.toBeNull();
    expect(screen.getByRole("heading", { name: /Battery calibration/iu })).not.toBeNull();
  });

  it("renders one row per battery with its class chip and due line", () => {
    render(<CalibrationCard calibration={toCalibrationState(projection())} />);
    const mid = screen.getByLabelText("mid calibration row");
    expect(mid.textContent).toContain("first cycle is a measurement");
    expect(mid.textContent).toContain("at least 65 days");
    const lhs = screen.getByLabelText("lhs calibration row");
    expect(lhs.textContent).toContain("excluded — cycles daily");
  });

  it("carries the pinned sentence AND the C16 stop route wherever the traverse renders", () => {
    render(
      <CalibrationCard
        calibration={toCalibrationState(
          projection({
            mode: "act",
            submits: null,
            phase: "traversing",
            traverse: {
              unit_id: "mid",
              soc_pct: 42.5,
              floor_pct: 10,
              energy_wh: 2871,
              energy_bound_wh: 4600,
              rate_w: 800,
              at_risk: false,
            },
          }),
        )}
      />,
    );
    expect(
      screen.getByText(/Partial cycles anchor nothing/iu),
    ).not.toBeNull();
    expect(screen.getByText(/claim the pod — any manual command preempts/iu)).not.toBeNull();
    expect(screen.getByText(/Traversing mid/iu)).not.toBeNull();
  });

  it("renders the morning-after line with BOTH economics branches (C7)", () => {
    render(
      <CalibrationCard
        calibration={toCalibrationState(
          projection({
            last_cycle: {
              night: "2026-08-24",
              kind: "measurement",
              verdict: "floor_reached",
              tier: "notice",
              trace_class: "monotone",
              energy_wh: 4350,
              economics_cents: {
                net_house_absorbed_cents: 99.5,
                net_fully_exported_cents: -25.6,
              },
            },
          }),
        )}
      />,
    );
    const line = screen.getByText(/Last cycle/iu);
    expect(line.textContent).toContain("anchor delivered");
    expect(line.textContent).toContain("+100 c");
    expect(line.textContent).toContain("-26 c");
  });

  it("promotes the stood-down unit to the alert styling with the operator exit", () => {
    render(
      <CalibrationCard
        calibration={toCalibrationState(
          projection({
            units: [
              {
                unit_id: "mid",
                class: "eligible",
                days_since_deep: 65,
                horizon_bounded: true,
                last_deep_date: null,
                horizon_days: 65,
                due: true,
                evidence_short: false,
                kind: "measurement",
                anchored: false,
                standdown: true,
              },
            ],
          }),
        )}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("stood down");
  });

  it("names the calibration-event honesty clause beside any cycle record", () => {
    render(
      <CalibrationCard
        calibration={toCalibrationState(
          projection({
            last_cycle: {
              night: "2026-08-24",
              kind: "measurement",
              verdict: "floor_reached",
              tier: "notice",
              trace_class: "monotone",
              energy_wh: 4350,
            },
          }),
        )}
      />,
    );
    expect(screen.getByText(/no SoC-calibration event register/iu)).not.toBeNull();
  });
});
