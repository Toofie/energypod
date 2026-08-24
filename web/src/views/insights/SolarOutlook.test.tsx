/**
 * Behavior contract for the Insights view's solar section
 * (web/src/views/insights/SolarOutlook.tsx) — "Solar outlook & forecast
 * accuracy", the provider round's advisory read rendered honestly.
 *
 * PROP SEAM: exactly like every other view suite — the suite injects the
 * client the same way the shell does (one `{ client }` prop); wire fixtures
 * come from web/src/test/wire.ts.
 *
 * The pins:
 *
 * - THE STATE SET: loading, ready, the honest not-commissioned 409 state
 *   (the `forecast_providers` config hint), error-with-retry, and the two
 *   empties (no provider family; nothing scored yet) worded from the
 *   response's own facts — never a blank and never a zero.
 * - THE OUTLOOK: the source named, the fetched-at age with the provider's
 *   own stale verdict carried (word + color-class, never color-only), the
 *   band's existence worded for a deterministic source, and the chart's text
 *   alternative carrying the peak and the horizon (jsdom is canvas-less).
 * - THE SCOREBOARD: the bias worded in the operator's direction, the
 *   coverage note, the basis DISCLOSED beside every accuracy figure (the
 *   scorer's recorded basis is post-battery — absorption is the desired
 *   outcome and must not read as the forecast's miss), the corrected basis's
 *   inputs rendered as INPUTS, and "evidence accumulating since" honesty at
 *   small n — no verdict theater at one fetch.
 * - A FAILED REFRESH KEEPS THE LAST OUTLOOK, refusal surfaced above it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  forecastNotCommissionedEnvelope,
  forecastOutlook,
  forecastPv,
  forecastProvider,
  forecastScore,
  forecastScoreboard,
} from "../../test/wire";
import { SolarOutlookSection } from "./SolarOutlook";

interface Harness {
  client: ApiClient & { getForecast: ReturnType<typeof vi.fn> };
}

function installHarness(getForecast: ReturnType<typeof vi.fn>): Harness {
  const client = {
    getForecast,
    getSnapshot: vi.fn(),
    getHealth: vi.fn(),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    openEvents: vi.fn(),
  };
  return { client: client as unknown as Harness["client"] };
}

function renderSection(harness: Harness) {
  return render(<SolarOutlookSection client={harness.client} />);
}

function notCommissionedError(): ApiClientError {
  return new ApiClientError(forecastNotCommissionedEnvelope());
}

describe("SolarOutlookSection — loading, not-commissioned, and errors", () => {
  it("shows the honest not-commissioned state with the config hint", async () => {
    const harness = installHarness(vi.fn(() => Promise.reject(notCommissionedError())));
    renderSection(harness);

    const heading = await screen.findByRole("heading", {
      name: /the forecast providers are not commissioned/i,
    });
    const note = heading.closest("div")!;
    expect(note.textContent).toContain("forecast_providers");
    expect(note.textContent).toContain("no control path reads them");
    expect(note.textContent).toContain("forecast_providers_not_commissioned");
  });

  it("renders the error state with the envelope verbatim and recovers on retry", async () => {
    let failed = false;
    const harness = installHarness(
      vi.fn(() => {
        if (!failed) {
          failed = true;
          return Promise.reject(
            new ApiClientError({
              code: "internal_error",
              message: "The request could not be completed",
              details: null,
              request_id: "req-f1",
              status: 500,
            }),
          );
        }
        return Promise.resolve(forecastOutlook({ pv: null, provider: null }));
      }),
    );
    renderSection(harness);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("internal_error");
    expect(alert.textContent).toContain("The request could not be completed");
    expect(alert.textContent).toContain("req-f1");

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    await waitFor(() => {
      expect(screen.getByText(/no pv forecast is available right now/i)).toBeVisible();
    });
  });

  it("words the unreadable body as its own failure, never a fabricated outlook", async () => {
    const harness = installHarness(vi.fn(() => Promise.resolve("not-json")));
    renderSection(harness);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("unreadable_forecast");
  });
});

describe("SolarOutlookSection — the outlook", () => {
  it("renders the curve's text alternative: source, peak, horizon, and the claimed band", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: forecastPv(),
            provider: forecastProvider(),
          }),
        ),
      ),
    );
    renderSection(harness);

    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    // The source in the operator's words.
    expect(within(section).getByText(/Solcast/i)).toBeVisible();
    // The chart's text alternative (jsdom is canvas-less): the peak figure
    // and the slot count, plus the band's existence.
    expect(section.textContent).toContain("peak 2,100 W");
    expect(section.textContent).toContain("4 slots");
    expect(section.textContent).toContain("reaches");
    expect(section.textContent).toContain("a q10–q90 band claimed by the source");
    // The fetched-at age line from the provider's own report.
    expect(section.textContent).toContain("fetched 900 s");
    // The scoreboard's honest empty (nothing scored yet).
    expect(section.textContent).toContain("no elapsed minutes of this fetch have recorded surplus");
  });

  it("carries the provider's stale verdict as word AND mark, never color-only", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: forecastPv(),
            provider: forecastProvider({
              staleness: {
                fetched_at: "2026-08-25T06:00:00+00:00",
                age_s: 14_400,
                stale: true,
                last_error: null,
                fetch_count: 2,
                error_count: 1,
              },
            }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    const staleMark = section.querySelector(".insights-solar-fetched--stale");
    expect(staleMark).not.toBeNull();
    expect(staleMark!.getAttribute("data-stale")).toBe("true");
    expect(staleMark!.textContent).toContain("stale by the provider's own threshold");
    // The failure count rides too.
    expect(section.textContent).toContain("1 fetch failure");
  });

  it("words a deterministic source's bandlessness instead of drawing one", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: forecastPv({
              source: "open-meteo",
              quantiled: false,
              intervals: [
                {
                  start: "2026-08-25T10:00:00+00:00",
                  end: "2026-08-25T11:00:00+00:00",
                  w: 900,
                },
              ],
            }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(section.textContent).toContain("none claimed — a deterministic source");
    expect(section.textContent).not.toContain("q10");
  });

  it("renders the absent family with the registry's own notes verbatim", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: null,
            provider: null,
            notes: [
              "solcast: the api key environment variable SOLCAST_API_KEY is not set; the PV provider is absent until the reference resolves",
            ],
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(within(section).getByText(/no pv forecast is available right now/i)).toBeVisible();
    expect(
      within(section).getByText(/the api key environment variable SOLCAST_API_KEY/i),
    ).toBeVisible();
  });

  it("carries a failed first fetch's own words when no cache exists", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: null,
            provider: forecastProvider({
              staleness: {
                fetched_at: null,
                age_s: null,
                stale: true,
                last_error: "provider answered HTTP 429 for the rooftop site",
                fetch_count: 0,
                error_count: 3,
              },
            }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(section.textContent).toContain("provider's last attempt failed");
    expect(section.textContent).toContain("provider answered HTTP 429");
  });
});

describe("SolarOutlookSection — the scoreboard", () => {
  it("renders the latest fetch's check with the bias direction and its basis disclosed", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            score: forecastScore({ samples: 30, bias_w: 312, mae_w: 312, inside_band: 0.75 }),
            scoreboard: forecastScoreboard({ records: 1, mean_bias_w: 312 }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });

    // The latest-fetch line: the count, the window, the bias word, coverage.
    expect(section.textContent).toContain("30 recorded samples");
    expect(section.textContent).toContain("over-forecast by 312 W");
    expect(section.textContent).toContain("optimistic");
    expect(section.textContent).toContain("under-charges");
    expect(section.textContent).toContain("75% of the recorded surplus");

    // The basis disclosure rides wherever an accuracy figure renders: the
    // number never stands as an unqualified verdict of the provider.
    expect(section.textContent).toContain("Basis honesty");
    expect(section.textContent).toContain("AFTER the pods absorb");

    // The corrected basis's inputs: evidence for the night-v2 rebuild,
    // rendered as inputs, never a second verdict.
    expect(section.textContent).toContain("pre-battery surplus");
    expect(section.textContent).toContain("reconstructed");
    expect(section.textContent).toContain("paired");
  });

  it("declines the verdict at n=1 — evidence honesty, no theater", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            scoreboard: forecastScoreboard({ records: 1, since: "2026-08-25T10:00:00+00:00" }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(section.textContent).toContain("evidence accumulating since");
    expect(section.textContent).toContain("1 fetch");
    expect(section.textContent).toContain("too few for a verdict");
    // The not-durable line says what a restart does to the evidence.
    expect(section.textContent).toContain("a restart starts the evidence over");
  });

  it("renders the grown accumulation without the small-n caveat", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            score: forecastScore({ bias_w: -80, mae_w: 190 }),
            scoreboard: forecastScoreboard({
              records: 12,
              total_samples: 6_480,
              mean_bias_w: -80,
              mean_mae_w: 190,
              mean_inside_band: 0.82,
              since: "2026-08-24T09:00:00+00:00",
            }),
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(section.textContent).toContain("12 fetches");
    expect(section.textContent).not.toContain("too few for a verdict");
    expect(section.textContent).toContain("under-forecast by 80 W");
    expect(section.textContent).toContain("82% of the recorded surplus");
    // The accumulated line names its basis too.
    expect(section.textContent).toContain("against recorded export");
  });

  it("words the history-absent state — no scoreboard can ever accumulate", async () => {
    const harness = installHarness(
      vi.fn(() =>
        Promise.resolve(
          forecastOutlook({
            pv: null,
            provider: null,
            history_composed: false,
          }),
        ),
      ),
    );
    renderSection(harness);
    const section = await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(section.textContent).toContain("plant-history block is not composed");
  });

  it("keeps the last outlook when a refresh fails, with the refusal above it", async () => {
    let calls = 0;
    const harness = installHarness(
      vi.fn(() => {
        calls += 1;
        if (calls <= 1) {
          return Promise.resolve(
            forecastOutlook({
              pv: forecastPv(),
              provider: forecastProvider(),
            }),
          );
        }
        return Promise.reject(
          new ApiClientError({
            code: "network_error",
            message: "The EnergyPod service could not be reached",
            details: null,
            request_id: "",
            status: 0,
          }),
        );
      }),
    );
    renderSection(harness);
    await screen.findByRole("region", { name: /solar outlook & forecast accuracy/i });
    expect(screen.getByText(/Solcast/i)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: /refresh/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("network_error");
    // The last-known picture stays on screen.
    expect(screen.getByText(/Solcast/i)).toBeVisible();
  });
});
