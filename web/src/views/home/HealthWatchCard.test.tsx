/**
 * Behavior contract for the Battery health watch Home card
 * (web/src/views/home/HealthWatchCard.tsx): T-BHW-CONSOLE's states — the
 * quiet no-surface feature detection, the running/complete program states,
 * the notice and alert census tiers, the alert-tier probe failure with the
 * §11 defined-restart advisory (its 10-minute wait and battery-first
 * ordering are load-bearing text), the §6.3 export note, the not-isolation
 * sentence on parked styling, and the uncommissioned-stage honesty.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { toHealthWatchState } from "../../app/healthWatch";
import { HealthWatchCard } from "./HealthWatchCard";

function projection(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    stages: ["census", "probe"],
    window: { opens_local: "23:00", deadline_local: "23:45" },
    phase: "done",
    night: "2026-08-25",
    reason: null,
    as_of: "2026-08-25T23:41:05+00:00",
    units: [
      {
        unit_id: "lhs",
        census: { verdict: "nominal", nights: 0, predicates: null, tier: null },
        probe: {
          verdict: "pass",
          probe_w: 300,
          qualifying_samples: 20,
          core_samples: 20,
          echo: "echo_matches_write",
        },
        recovery: { mode: "uncommissioned" },
      },
      {
        unit_id: "rhs",
        census: { verdict: "stuck_suspected", nights: 2, predicates: null, tier: "alert" },
        probe: {
          verdict: "fail_no_response",
          probe_w: 300,
          qualifying_samples: 3,
          core_samples: 20,
          echo: "echo_matches_write",
        },
        recovery: { mode: "uncommissioned" },
      },
    ],
    ...spec,
  };
}

function renderCard(spec: Partial<Record<string, unknown>>) {
  return render(
    <HealthWatchCard health={toHealthWatchState(projection(spec))} />,
  );
}

afterEach(cleanup);

describe("the feature detection", () => {
  it("renders nothing at all without a health_watch_state", () => {
    const { container } = render(<HealthWatchCard health={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("the program states", () => {
  it("names the opening window before it starts", () => {
    renderCard({ phase: "await_window" });
    expect(screen.getByText(/opens at 23:00/)).toBeInTheDocument();
  });

  it("names the running phases and the completed night", () => {
    renderCard({ phase: "census" });
    expect(
      screen.getByText("Tonight's health watch is reading the batteries (the census — no writes)."),
    ).toBeInTheDocument();
    cleanup();
    renderCard({ phase: "done" });
    expect(screen.getByText("Tonight's health watch is complete.")).toBeInTheDocument();
  });

  it("renders the deferred and interrupted program reasons honestly", () => {
    renderCard({ reason: "window_not_quiet" });
    expect(screen.getByText(/deferred rather than fight/)).toBeInTheDocument();
    cleanup();
    renderCard({ reason: "interrupted" });
    expect(screen.getByText(/health watch was interrupted/)).toBeInTheDocument();
  });

  it("carries the uncommissioned-stage honesty on the stage line", () => {
    renderCard({});
    expect(screen.getByText("census · probe · recovery not commissioned")).toBeInTheDocument();
  });
});

describe("the per-battery rows", () => {
  it("renders the census chip, the probe figures, and the recovery word", () => {
    renderCard({});
    const rhs = screen.getByLabelText("rhs health watch row");
    expect(rhs).toHaveTextContent("flagged 2 nights");
    expect(rhs).toHaveTextContent("3/20 samples moved");
    expect(rhs).toHaveTextContent("recovery not commissioned");
    const lhs = screen.getByLabelText("lhs health watch row");
    expect(lhs).toHaveTextContent("nominal");
    expect(lhs).toHaveTextContent("20/20 samples delivered");
  });

  it("renders a skipped disarmed unit as the arm instruction", () => {
    renderCard({
      units: [
        {
          unit_id: "lhs",
          census: { verdict: "nominal", nights: 0, predicates: null, tier: null },
          probe: { verdict: "skipped:unit_disarmed", probe_w: null },
          recovery: { mode: "uncommissioned" },
        },
      ],
    });
    expect(screen.getByText(/the program never arms/)).toBeInTheDocument();
  });
});

describe("the alert styling and the advisories", () => {
  it("announces an alert-tier probe failure and says no recovery exists", () => {
    renderCard({});
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/FAILED on rhs/);
    expect(alert).toHaveTextContent(/no automated recovery exists/);
  });

  it("carries the defined-restart advisory verbatim beside a failure", () => {
    renderCard({});
    const advisory = screen.getByText(/battery button OFF for 5 s/);
    expect(advisory).toHaveTextContent("WAIT 10 MINUTES");
    expect(advisory).toHaveTextContent("battery ON FIRST, then AC, then DC");
  });

  it("shows no alert story on a quiet healthy night", () => {
    renderCard({
      units: [
        {
          unit_id: "lhs",
          census: { verdict: "nominal", nights: 0, predicates: null, tier: null },
          probe: {
            verdict: "pass",
            probe_w: 300,
            qualifying_samples: 20,
            core_samples: 20,
            echo: "echo_matches_write",
          },
          recovery: { mode: "uncommissioned" },
        },
      ],
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/battery button OFF/)).toBeNull();
  });

  it("names the export honesty note once a probe ran", () => {
    renderCard({});
    expect(
      screen.getByText(/export up to its own magnitude for under a minute/),
    ).toBeInTheDocument();
  });

  it("rides the not-isolation sentence on parked styling", () => {
    renderCard({
      units: [
        {
          unit_id: "lhs",
          census: { verdict: "excluded:parked", nights: 0, predicates: null, tier: null },
          probe: { verdict: "skipped:census_excluded", probe_w: null },
          recovery: { mode: "uncommissioned" },
        },
      ],
    });
    expect(screen.getByText(/not electrical isolation/)).toBeInTheDocument();
  });
});
