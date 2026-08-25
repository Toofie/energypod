/**
 * Behavior contract for the Evening load sharing Home card
 * (web/src/views/home/EveningShareCard.tsx): T-ELS-CONSOLE's states — the
 * quiet no-surface feature detection, the advise posture's `submits: never`
 * line, the engaged live line (work, the netted exchange, the split) with
 * the pinned §0 sentence AND the C16 stop-route line, the capability_limited
 * residual, the per-unit skip reasons verbatim, the E6 degrade alert, the
 * evidence-withdraw alert, and the morning-after close line.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  EVENING_PINNED_SENTENCE,
  EVENING_STOP_ROUTE,
  toEveningShareState,
} from "../../app/eveningShare";
import { EveningShareCard } from "./EveningShareCard";

function projection(spec: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    mode: "act",
    window: { opens_local: "16:00", ends_local: "22:30" },
    phase: "sharing",
    as_of: "2026-08-26T08:41:05+00:00",
    engaged: true,
    work_w: 1620,
    net_exchange_w: 90,
    elsewhere_w: 0,
    within_tolerance: true,
    commanded_total_w: 1408,
    derate: 1.16,
    reason_codes: ["on_plan"],
    units: [
      {
        unit_id: "lhs",
        soc_pct: 88.2,
        weight: 3878,
        share_w: 817,
        phase: "sharing",
        reason: "on_plan",
      },
      {
        unit_id: "mid",
        soc_pct: 74.9,
        weight: 2807,
        share_w: 591,
        phase: "sharing",
        reason: "on_plan",
      },
      {
        unit_id: "rhs",
        soc_pct: 61.3,
        weight: 1577,
        share_w: 0,
        phase: "sitting_out",
        reason: "share_below_floor",
        note: "3-way split would breach min_share_w; the set rule keeps two",
      },
    ],
    held_intent_id: "els-12-1000.5",
    ...spec,
  };
}

afterEach(cleanup);

describe("EveningShareCard", () => {
  it("renders nothing at all while the projection is absent (the block-presence doctrine)", () => {
    const { container } = render(<EveningShareCard evening={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the advise posture's submits: never beside the plan", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(projection({ mode: "advise", submits: "never" }))}
      />,
    );
    expect(screen.getByText(/Submits: never/iu)).not.toBeNull();
    expect(screen.getByRole("heading", { name: /Evening load sharing/iu })).not.toBeNull();
  });

  it("carries the live line, the pinned sentence AND the stop route while engaged", () => {
    render(<EveningShareCard evening={toEveningShareState(projection())} />);
    const live = screen.getByText(/work 1620 W/iu);
    expect(live.textContent).toContain("import 90 W");
    expect(live.textContent).toContain("commanded 1,408 W");
    expect(screen.getByText(EVENING_PINNED_SENTENCE)).not.toBeNull();
    expect(screen.getByText(EVENING_STOP_ROUTE)).not.toBeNull();
  });

  it("renders the per-pod shares with SoC, and skips with their reason verbatim", () => {
    render(<EveningShareCard evening={toEveningShareState(projection())} />);
    const lhs = screen.getByLabelText("lhs evening share row");
    expect(lhs.textContent).toContain("817 W");
    expect(lhs.textContent).toContain("SoC 88%");
    const rhs = screen.getByLabelText("rhs evening share row");
    expect(rhs.textContent).toContain("sitting out");
  });

  it("words the idle phase with its reason and renders no live line", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(
          projection({
            phase: "idle",
            engaged: false,
            work_w: null,
            net_exchange_w: null,
            commanded_total_w: null,
            reason_codes: ["below_one_pod_floor"],
          }),
        )}
      />,
    );
    expect(screen.getByText(/Idle until the 16:00 window/iu)).not.toBeNull();
    expect(screen.queryByText(/work /iu)).toBeNull();
  });

  it("carries the capability_limited residual honestly", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(
          projection({ phase: "capability_limited", residual_import_w: 1200 }),
        )}
      />,
    );
    expect(screen.getByText(/grid covers the remaining/iu).textContent).toContain("1,200 W");
  });

  it("raises the E6 degrade note as the alert when boot degraded the block", () => {
    const degradeNote =
      "mode degraded to advise at boot: the recorded netting evidence " +
      "(docs/evidence/netting.md) does not exist (E6)";
    render(
      <EveningShareCard
        evening={toEveningShareState(
          projection({
            mode: "advise",
            submits: "never",
            degraded_note: degradeNote,
          }),
        )}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("degraded to advise");
    expect(alert.textContent).toContain("E6");
  });

  it("raises the evidence-withdraw alert naming the standing autonomy fallback", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(
          projection({
            phase: "withdrawn",
            engaged: false,
            reason_codes: ["grid_evidence_stale"],
          }),
        )}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("grid evidence stale");
    expect(alert.textContent).toContain("own autonomy");
  });

  it("renders the morning-after close line from the last evening's record", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(
          projection({
            phase: "idle",
            engaged: false,
            last_close: {
              night: "2026-08-25",
              served_wh: { lhs: 2140, mid: 1680, rhs: 1210 },
              import_wh: 610,
              spill_wh: 40,
              convergence_delta_pct: { open: 27.1, close: 12.4 },
              money: { import_paid_cents: 187.7, spill_earned_cents: 0.1 },
            },
          }),
        )}
      />,
    );
    const close = screen.getByText(/Last evening/iu);
    expect(close.textContent).toContain("2026-08-25");
  });

  it("hides the engaged notes (pinned sentence, stop route) while not engaged", () => {
    render(
      <EveningShareCard
        evening={toEveningShareState(projection({ engaged: false, phase: "idle" }))}
      />,
    );
    expect(screen.queryByText(EVENING_PINNED_SENTENCE)).toBeNull();
    expect(screen.queryByText(EVENING_STOP_ROUTE)).toBeNull();
  });
});
