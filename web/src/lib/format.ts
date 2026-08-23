/**
 * The console's one display-precision module (the operator's standing rule):
 * every numeric metric rendered to an operator carries AT MOST two decimal
 * places, integers render as integers ("500 W", never "500.00 W"), trailing
 * zeros are trimmed ("46.5", never "46.50"; "98.1 V", never "98.10 V"), and
 * no figure ever renders in scientific notation.
 *
 * DISPLAY ONLY. Rounding happens exactly at this render boundary: wire
 * payloads, parsed view models, and every derived value keep full precision,
 * and nothing this module returns feeds back into stored state. Every view
 * formats its numbers through these helpers — no view formats a number by
 * hand (sequence numbers, cell counts, and the other true integers of the
 * wire are rendered as the integers they are, not routed through here).
 *
 * Rounding rule (pinned by format.test.ts): `toFixed` at the display bound,
 * then trailing zeros trimmed — 1234.567 renders "1,234.57", 1500 renders
 * "1,500", -1132.456 renders "-1,132.46" (charging watts are negative and
 * keep their sign; a view that carries the sign in a direction word passes
 * the absolute value). Thousands are grouped with commas — a deterministic
 * replacement, never ICU-dependent locale data. Ages and countdowns are
 * whole seconds (`wholeSeconds` / `formatSeconds`, the same rule at zero
 * decimals).
 */

/** The display bound: at most two decimal places, everywhere. */
const MAX_DISPLAY_DECIMALS = 2;

/**
 * Any number as the operator reads it: at most `maxFractionDigits` decimals
 * (default the display bound), trailing zeros trimmed, thousands grouped,
 * never scientific notation. A value that rounds to zero never renders "-0".
 */
export function formatDecimal(value: number, maxFractionDigits: number = MAX_DISPLAY_DECIMALS): string {
  if (!Number.isFinite(value)) {
    // The parse layers never hand a non-finite figure through; if one ever
    // arrives, its name ("NaN", "Infinity") is still never scientific.
    return String(value);
  }
  const fixed = value.toFixed(Math.max(0, Math.min(maxFractionDigits, 100)));
  const negative = fixed.startsWith("-");
  const bare = negative ? fixed.slice(1) : fixed;
  const [integerPart = "0", fractionPart = ""] = bare.split(".");
  const trimmedFraction = fractionPart.replace(/0+$/, "");
  const grouped = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const composed = trimmedFraction === "" ? grouped : `${grouped}.${trimmedFraction}`;
  if (negative && composed === "0") {
    return "0";
  }
  return negative ? `-${composed}` : composed;
}

/** Watts with their unit ("2,400 W", "-980.13 W"): sign kept for the caller. */
export function formatWatts(watts: number): string {
  return `${formatDecimal(watts)} W`;
}

/** A percentage with its sign-free unit ("48.5%", "98.1%"). */
export function formatPercent(value: number): string {
  return `${formatDecimal(value)}%`;
}

/** Volts with their unit ("192.4 V", "3.21 V") — pack and cell alike. */
export function formatVolts(value: number): string {
  return `${formatDecimal(value)} V`;
}

/** Millivolts with their unit ("4 mV"): the cell-spread figure's own scale. */
export function formatMillivolts(value: number): string {
  return `${formatDecimal(value)} mV`;
}

/** Amperes with their unit ("-6.9 A"): pack current, sign kept for the caller. */
export function formatAmps(value: number): string {
  return `${formatDecimal(value)} A`;
}

/** Celsius with its unit ("23 °C", "27.65 °C"). */
export function formatTemp(value: number): string {
  return `${formatDecimal(value)} °C`;
}

/**
 * An age or countdown as whole seconds (the display rule for time: no
 * decimals at all), normalized so a fraction that rounds to zero never
 * renders as -0.
 */
export function wholeSeconds(seconds: number): number {
  const whole = Number(seconds.toFixed(0));
  return whole === 0 ? 0 : whole;
}

/** Whole seconds with their unit ("12 s"): ages, "…s ago" figures, countdowns. */
export function formatSeconds(seconds: number): string {
  return `${wholeSeconds(seconds)} s`;
}
