/**
 * Behavior contract for the forecast wire model (web/src/app/forecast.ts) —
 * `GET /api/v1/forecast`'s body narrowed, and the plain-language maps the
 * Insights view's solar section renders from.
 *
 * Wire fixtures come from web/src/test/wire.ts. The pinned honesty: nulls are
 * nulls (no PV, no score, no scoreboard are each real states), the band
 * exists only where the source claims one, the bias word carries the
 * operator's direction, the basis is disclosed beside every accuracy figure,
 * and the scoreboard's small-n wording declines the verdict.
 */
import { describe, expect, it } from "vitest";
import {
  BASIS_DISCLOSURE_TEXT,
  basisInputsText,
  biasText,
  coverageText,
  evidenceText,
  fetchedAgeText,
  horizonText,
  sourceText,
  toForecastOutlook,
  type ProviderStalenessView,
} from "./forecast";
import {
  forecastInterval,
  forecastNotCommissionedEnvelope,
  forecastOutlook,
  forecastPv,
  forecastProvider,
  forecastScore,
  forecastScoreboard,
} from "../test/wire";

describe("toForecastOutlook — the body narrows, absence stays absence", () => {
  it("narrows the full shape: quantiled PV, staleness, score with basis, scoreboard", () => {
    const view = toForecastOutlook(
      forecastOutlook({
        score: forecastScore(),
        scoreboard: forecastScoreboard(),
      }),
    );
    expect(view).not.toBeNull();
    expect(view!.historyComposed).toBe(true);
    expect(view!.notes).toEqual([]);
    expect(view!.pv).not.toBeNull();
    expect(view!.pv!.source).toBe("solcast");
    expect(view!.pv!.quantiled).toBe(true);
    expect(view!.pv!.fetchedAt).toBe(Date.parse("2026-08-25T10:00:00+00:00"));
    expect(view!.pv!.horizonTo).toBe(Date.parse("2026-08-27T12:00:00+00:00"));
    expect(view!.pv!.intervals).toHaveLength(4);
    expect(view!.pv!.intervals[0]).toEqual({
      start: Date.parse("2026-08-25T10:00:00+00:00"),
      end: Date.parse("2026-08-27T12:00:00+00:00"),
      w: 600,
      q10: 270,
      q90: 930,
    });
    expect(view!.provider).not.toBeNull();
    expect(view!.provider!.ageS).toBe(900);
    expect(view!.provider!.stale).toBe(false);
    expect(view!.provider!.lastError).toBeNull();
    expect(view!.score).not.toBeNull();
    expect(view!.score!.samples).toBe(30);
    expect(view!.score!.biasW).toBe(312);
    expect(view!.score!.insideBand).toBe(0.75);
    expect(view!.score!.basis).not.toBeNull();
    expect(view!.score!.basis!.meanPreBatterySurplusW).toBe(900);
    expect(view!.scoreboard).not.toBeNull();
    expect(view!.scoreboard!.records).toBe(1);
    expect(view!.scoreboard!.durable).toBe(false);
  });

  it("keeps every honest null: no family, no score, no scoreboard, no provider", () => {
    const view = toForecastOutlook(
      forecastOutlook({ pv: null, provider: null, notes: ["solcast: the key is not set"] }),
    );
    expect(view!.pv).toBeNull();
    expect(view!.provider).toBeNull();
    expect(view!.score).toBeNull();
    expect(view!.scoreboard).toBeNull();
    expect(view!.notes).toEqual(["solcast: the key is not set"]);
  });

  it("drops intervals that are not real slots (reversed, non-numeric, non-object)", () => {
    const raw = forecastOutlook({
      pv: forecastPv({
        intervals: [
          forecastInterval({
            start: "2026-08-25T11:00:00+00:00",
            end: "2026-08-25T10:00:00+00:00",
          }),
          { start: "2026-08-25T10:00:00+00:00", end: "2026-08-25T10:30:00+00:00", w: 900 },
          { start: "2026-08-25T10:30:00+00:00", end: "2026-08-25T11:00:00+00:00", w: "many" },
        ] as unknown as ReturnType<typeof forecastInterval>[],
      }),
    });
    const view = toForecastOutlook(raw);
    expect(view!.pv!.intervals).toHaveLength(1);
    expect(view!.pv!.intervals[0]!.w).toBe(900);
    // A slot without deciles carries explicit nulls, never fabricated edges.
    expect(view!.pv!.intervals[0]!.q10).toBeNull();
    expect(view!.pv!.intervals[0]!.q90).toBeNull();
  });

  it("treats a non-object body as unreadable (null), and thin bodies keep honest defaults", () => {
    expect(toForecastOutlook("nope")).toBeNull();
    expect(toForecastOutlook(null)).toBeNull();
    const thin = toForecastOutlook({ as_of: "2026-08-25T10:15:00+00:00" });
    expect(thin).not.toBeNull();
    expect(thin!.pv).toBeNull();
    expect(thin!.score).toBeNull();
    expect(thin!.scoreboard).toBeNull();
    expect(thin!.provider).toBeNull();
    expect(thin!.historyComposed).toBe(false);
  });

  it("carries the provider's failure words even when the fetch itself failed", () => {
    const view = toForecastOutlook(
      forecastOutlook({
        pv: null,
        provider: forecastProvider({
          staleness: {
            fetched_at: null,
            age_s: null,
            stale: true,
            last_error: "request timed out after 10s",
            fetch_count: 0,
            error_count: 2,
          },
        }),
      }),
    );
    expect(view!.pv).toBeNull();
    expect(view!.provider!.lastError).toBe("request timed out after 10s");
    expect(view!.provider!.stale).toBe(true);
    expect(view!.provider!.errorCount).toBe(2);
  });
});

describe("the wording maps", () => {
  it("names the composed sources in the operator's words", () => {
    expect(sourceText("solcast")).toContain("Solcast");
    expect(sourceText("open-meteo")).toContain("Open-Meteo");
    expect(sourceText("unknown-source")).toBe("unknown-source");
  });

  it("words the bias with the operator's direction — optimistic over-forecasts", () => {
    expect(biasText(312)).toContain("over-forecast");
    expect(biasText(312)).toContain("optimistic");
    expect(biasText(312)).toContain("under-charges");
    expect(biasText(-150)).toContain("under-forecast");
    expect(biasText(-150)).toContain("pessimistic");
    expect(biasText(0)).toContain("even");
  });

  it("words coverage honestly — a null band is the no-band answer, never 0%", () => {
    expect(coverageText(0.75)).toContain("75%");
    expect(coverageText(null)).toContain("claims no uncertainty band");
  });

  it("words the fetched age; a never-fetched provider says so", () => {
    expect(fetchedAgeText(null)).toBe("never fetched");
    const fetched = Date.parse("2026-08-25T10:00:00Z");
    const report: ProviderStalenessView = {
      source: "solcast",
      fetchedAt: fetched,
      ageS: 900,
      stale: false,
      lastError: null,
      fetchCount: 1,
      errorCount: 0,
    };
    // With a wall clock the age ticks from the fetch instant itself.
    expect(fetchedAgeText(report, fetched + 4 * 60_000)).toContain("fetched 240 s");
    // A wall clock that disagrees with the stamp falls back to the provider's
    // own reported age — never a clamped "0 s ago" pretending to be data.
    expect(fetchedAgeText(report, fetched - 60_000)).toContain("fetched 900 s");
    // Without one the provider's own reported age rides verbatim.
    const reported: ProviderStalenessView = { ...report, ageS: 120 };
    expect(fetchedAgeText(reported)).toContain("fetched 120 s");
  });

  it("words the horizon from the wire's own bound", () => {
    const view = toForecastOutlook(forecastOutlook());
    expect(horizonText(view!.pv!)).toContain("reaches");
  });

  it("declines the verdict at small n and names the accumulation start", () => {
    const narrow = (
      records: number,
    ): NonNullable<NonNullable<ReturnType<typeof toForecastOutlook>>["scoreboard"]> => {
      const view = toForecastOutlook(forecastOutlook({ scoreboard: forecastScoreboard({ records }) }));
      expect(view!.scoreboard).not.toBeNull();
      return view!.scoreboard!;
    };
    const one = evidenceText(narrow(1));
    expect(one).toContain("evidence accumulating since");
    expect(one).toContain("1 fetch");
    expect(one).toContain("too few for a verdict");
    const grown = evidenceText(narrow(9));
    expect(grown).toContain("9 fetches");
    expect(grown).not.toContain("too few");
    const empty = evidenceText({ ...narrow(1), records: 0 });
    expect(empty).toContain("no forecast fetch has been checked");
  });

  it("discloses the post-battery basis beside every accuracy figure", () => {
    expect(BASIS_DISCLOSURE_TEXT).toContain("AFTER the pods absorb");
    expect(BASIS_DISCLOSURE_TEXT).toContain("pre-battery");
  });

  it("words the corrected basis's inputs — inputs, never a second verdict", () => {
    const scored = toForecastOutlook(forecastOutlook({ score: forecastScore() }));
    expect(scored!.score!.basis).not.toBeNull();
    const text = basisInputsText(scored!.score!.basis!);
    expect(text).toContain("promised");
    expect(text).toContain("recorded export");
    expect(text).toContain("pre-battery surplus");
    expect(text).toContain("reconstructed");
    expect(text).toContain("paired");
    expect(basisInputsText(null)).toBe("");
    expect(
      basisInputsText({
        pairedSamples: 4,
        meanForecastW: null,
        meanExportW: null,
        meanPreBatterySurplusW: null,
        meanChargingW: null,
      }),
    ).toBe("");
  });
});

describe("the wire fixtures", () => {
  it("serve the not-commissioned envelope with the pinned code", () => {
    const envelope = forecastNotCommissionedEnvelope();
    expect(envelope.code).toBe("forecast_providers_not_commissioned");
    expect(envelope.status).toBe(409);
  });
});
