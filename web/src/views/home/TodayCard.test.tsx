/**
 * Behavior contract for Home's "Today" card (DESIGN_ENERGY_SCORECARD.md §1 +
 * §8 W-A): the day's energy account so far, honest by construction.
 *
 * The pins this suite owns (the design's own §1 rules, made testable):
 *
 * - Feature detection: an absent `energy_today` renders NOTHING — no heading,
 *   no footnote, no empty state.
 * - The §1 sentence block: bought/sold, charged/discharged/house-load, and
 *   the surplus line; null figures read "not available", never 0.
 * - The surplus line ties into the live adviser projection: a figure renders
 *   the design's sentence (a real 0 included), an absent figure is explained
 *   by the adviser's actual state (enabled-quiet / switched off / absent),
 *   never fabricated.
 * - The day marker always carries the day kind: "so far today — 87%
 *   coverage" for the live day, the threshold breach named for a partial day.
 * - Provenance is named per metric family, and the grid line carries the A/B
 *   unpinned note whenever the CT integration is the source — with no
 *   bought/sold role ever assigned to either counter. After promotion to the
 *   device counters, the line says the roles are confirmed instead.
 * - SOLAR IS NEVER A MEASUREMENT: the card carries no solar-production line,
 *   and the footnote says exactly why, once.
 * - The per-unit disclosure lists each battery's own figures, coverage, and
 *   any counter-reset flag.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { toAdviserState } from "../../app/fleet";
import { toEnergyToday } from "../../app/energy";
import { adviserState, energyToday } from "../../test/wire";
import { TodayCard } from "./TodayCard";

function todayOf(spec: Parameters<typeof energyToday>[0]): ReturnType<typeof toEnergyToday> {
  return toEnergyToday(energyToday(spec));
}

/** The tile's adviser prop is the app model — parsed from the wire fixture. */
function adviserOf(spec: Parameters<typeof adviserState>[0] = {}) {
  return toAdviserState(adviserState(spec));
}

describe("TodayCard — feature detection", () => {
  it("renders nothing at all while the snapshot carries no energy_today", () => {
    const { container } = render(<TodayCard today={null} adviser={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the card for a present block, even one whose figures are absent", () => {
    render(
      <TodayCard
        // An empty units map yields an all-null fleet block (no unit
        // reported anything) — every figure names its gap.
        today={todayOf({ units: {} })}
        adviser={null}
      />,
    );
    expect(screen.getByRole("heading", { name: /today \(so far\)/i })).toBeVisible();
    // Every fleet figure names its gap — none is zero-filled.
    const card = screen.getByRole("heading", { name: /today \(so far\)/i }).closest("section")!;
    expect(card.textContent).toContain("Bought from grid not available");
    expect(card.textContent).toContain("Sold to grid not available");
    expect(card.textContent).toContain("Charged not available");
    expect(card.textContent).toContain("House load not available");
  });
});

describe("TodayCard — the §1 sentence block", () => {
  it("renders the day's figures with the design's own wording", () => {
    render(<TodayCard today={todayOf({})} adviser={adviserOf()} />);
    const card = screen.getByRole("heading", { name: /today \(so far\)/i }).closest("section")!;
    expect(card.textContent).toContain("Bought from grid 8.4 kWh");
    expect(card.textContent).toContain("Sold to grid 12.9 kWh");
    expect(card.textContent).toContain("Charged 6.2 kWh");
    expect(card.textContent).toContain("Discharged 4.1 kWh");
    expect(card.textContent).toContain("House load 14.7 kWh");
    expect(card.textContent).toContain(
      "Solar-surplus charging moved 3.1 kWh that would have been exported.",
    );
  });

  it("renders an honest zero surplus — a real measured 0 is not an absent source", () => {
    render(
      <TodayCard
        today={todayOf({
          units: {
            mid: {
              grid_import_kwh: 1,
              grid_export_kwh: 2,
              battery_charged_kwh: 0.5,
              battery_discharged_kwh: 0,
              load_kwh: 3,
              charged_from_surplus_kwh: 0,
              coverage_pct: 100,
              metric_flags: [],
            },
          },
        })}
        adviser={adviserOf({ active: true })}
      />,
    );
    const card = screen.getByRole("heading", { name: /today \(so far\)/i }).closest("section")!;
    expect(card.textContent).toContain("Solar-surplus charging moved 0 kWh");
  });

  it("explains an absent surplus by the adviser's actual state, never a fabricated figure", () => {
    const absentToday = todayOf({
      units: {
        mid: {
          grid_import_kwh: 1,
          grid_export_kwh: 2,
          battery_charged_kwh: 1,
          battery_discharged_kwh: 1,
          load_kwh: 3,
          charged_from_surplus_kwh: null,
          coverage_pct: 100,
          metric_flags: [],
        },
      },
    });
    const { unmount } = render(<TodayCard today={absentToday} adviser={adviserOf({ enabled: true })} />);
    expect(screen.getByText(/enabled but has not captured energy yet today/i)).toBeVisible();
    unmount();

    render(<TodayCard today={absentToday} adviser={adviserOf({ enabled: false })} />);
    expect(screen.getByText(/switched off, so no surplus energy was captured/i)).toBeVisible();
  });
});

describe("TodayCard — the day marker and provenance", () => {
  it("marks the live day as so-far with its coverage fraction and the as-of clock", () => {
    render(<TodayCard today={todayOf({})} adviser={null} />);
    const marker = screen.getByText(/so far today/i);
    expect(marker.textContent).toContain("98.7% coverage");
    expect(marker.textContent).toContain("figures at 14:03");
    expect(marker.textContent).toContain("Australia/Brisbane");
  });

  it("names a partial day as a partial day, never a quiet full number", () => {
    render(
      <TodayCard
        today={todayOf({
          kind: "partial",
          units: {
            mid: {
              grid_import_kwh: 1,
              grid_export_kwh: 2,
              battery_charged_kwh: 1,
              battery_discharged_kwh: 1,
              load_kwh: 3,
              charged_from_surplus_kwh: null,
              coverage_pct: 61.2,
              metric_flags: [],
            },
          },
        })}
        adviser={null}
      />,
    );
    const marker = screen.getByText(/partial day/i);
    expect(marker.textContent).toContain("61.2% coverage");
    expect(marker.textContent).toContain("below the commissioned coverage threshold");
  });

  it("carries the A/B unpinned note with the integrated source, and never assigns bought/sold to a counter", () => {
    render(<TodayCard today={todayOf({})} adviser={null} />);
    const provenance = screen.getByText(/grid figures measured by the controller/i);
    expect(provenance.textContent).toContain("counter A and counter B");
    expect(provenance.textContent).toContain("until their roles are pinned");
    // THE PIN: no bought/sold role is ever assigned to counter A or B.
    expect(provenance.textContent).not.toMatch(/[AB]\s*(is|=)\s*(bought|sold)/i);
    // Battery and load provenance is named too (the device-counter class).
    expect(provenance.textContent).toContain("recorded by the pods' own energy counters");
  });

  it("says the roles are confirmed once the device counters are the grid source", () => {
    render(
      <TodayCard
        today={todayOf({
          sources: {
            grid: "device_counter",
            battery: "device_counter",
            load: "device_counter",
            surplus: "attributed_adviser",
          },
        })}
        adviser={null}
      />,
    );
    expect(screen.getByText(/come from the pods' own grid counters/i).textContent).toContain(
      "counter roles confirmed",
    );
  });

  it("carries the solar footnote exactly once and no solar-production line", () => {
    render(<TodayCard today={todayOf({})} adviser={null} />);
    const card = screen.getByRole("heading", { name: /today \(so far\)/i }).closest("section")!;
    const footnote = screen.getAllByText(/solar panels are not measured by the pods/i);
    expect(footnote).toHaveLength(1);
    // No solar production figure anywhere — the scorecard never measures it.
    expect(card.textContent).not.toMatch(/solar production/i);
  });
});

describe("TodayCard — the per-unit disclosure", () => {
  it("lists each battery's own figures, coverage, and counter-reset marker on demand", async () => {
    const user = userEvent.setup();
    render(
      <TodayCard
        today={todayOf({
          units: {
            mid: {
              grid_import_kwh: 1.2,
              grid_export_kwh: 6.8,
              battery_charged_kwh: 3.4,
              battery_discharged_kwh: 0.7,
              load_kwh: 5.1,
              charged_from_surplus_kwh: 3.1,
              coverage_pct: 99.4,
              metric_flags: [],
            },
            rhs: {
              grid_import_kwh: 2.1,
              grid_export_kwh: 4.2,
              battery_charged_kwh: null,
              battery_discharged_kwh: 1.6,
              load_kwh: 4.4,
              charged_from_surplus_kwh: null,
              coverage_pct: null,
              metric_flags: ["counter_reset_observed"],
            },
          },
        })}
        adviser={null}
      />,
    );
    await user.click(screen.getByText(/per-battery figures for today/i));
    const list = screen.getByRole("list", { name: /per-battery figures for today/i });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toContain(
      "mid — bought 1.2 kWh · sold 6.8 kWh · charged 3.4 kWh · discharged 0.7 kWh",
    );
    expect(rows[0]!.textContent).toContain("charged from surplus 3.1 kWh");
    expect(rows[0]!.textContent).toContain("99.4% coverage");
    // A null figure names the gap; the reset flag renders its own marker.
    expect(rows[1]!.textContent).toContain("charged not available");
    expect(rows[1]!.textContent).toContain("a counter was reset");
  });

  it("hides the disclosure entirely when the record carries no units", () => {
    render(
      <TodayCard today={todayOf({ units: {} })} adviser={null} />,
    );
    expect(screen.queryByText(/per-battery figures for today/i)).toBeNull();
  });
});
