/**
 * Display-precision pins for the console's one formatter module
 * (src/lib/format.ts): at most two decimal places, integers as integers,
 * trailing zeros trimmed, thousands grouped, never scientific notation —
 * and the rounding rule itself (toFixed at the bound, then trimmed), so a
 * regressions in any helper's precision is caught here rather than in the
 * views that share it.
 *
 * Charging watts are negative on the wire (PROTOCOL_EVIDENCE 4b), so the
 * negative cases are pinned with the same weight as the positive ones: the
 * formatter keeps the sign and rounds symmetrically; a view that carries the
 * sign in a direction word passes Math.abs itself.
 */
import { describe, expect, it } from "vitest";

import {
  formatAmps,
  formatDecimal,
  formatMillivolts,
  formatPercent,
  formatSeconds,
  formatTemp,
  formatVolts,
  formatWatts,
  wholeSeconds,
} from "./format";

describe("formatDecimal — the shared display rule", () => {
  it("renders integers as integers, never with forced decimals", () => {
    expect(formatDecimal(500)).toBe("500");
    expect(formatDecimal(0)).toBe("0");
    expect(formatDecimal(1500)).toBe("1,500");
    expect(formatDecimal(1000000)).toBe("1,000,000");
  });

  it("keeps one decimal as one decimal and trims trailing zeros", () => {
    expect(formatDecimal(46.5)).toBe("46.5");
    expect(formatDecimal(98.1)).toBe("98.1");
    // A value that is already two decimals stays exactly two.
    expect(formatDecimal(27.65)).toBe("27.65");
  });

  it("rounds beyond two decimals at the toFixed bound, then trims", () => {
    expect(formatDecimal(1234.567)).toBe("1,234.57");
    expect(formatDecimal(1234.56)).toBe("1,234.56");
    expect(formatDecimal(3.209)).toBe("3.21");
  });

  it("keeps the sign of charging watts (negative on the wire)", () => {
    expect(formatDecimal(-980)).toBe("-980");
    expect(formatDecimal(-1132.456)).toBe("-1,132.46");
    expect(formatDecimal(-0.5)).toBe("-0.5");
  });

  it("never renders negative zero", () => {
    expect(formatDecimal(-0)).toBe("0");
    expect(formatDecimal(-0.004)).toBe("0");
  });

  it("never renders scientific notation", () => {
    expect(formatDecimal(1e-7)).toBe("0");
    expect(formatDecimal(1.23456789e7)).toBe("12,345,678.9");
    expect(formatDecimal(1234567890.123456)).toBe("1,234,567,890.12");
  });
});

describe("unit helpers", () => {
  it("formatWatts carries the unit and the display bound", () => {
    expect(formatWatts(2400)).toBe("2,400 W");
    expect(formatWatts(950)).toBe("950 W");
    expect(formatWatts(-980.126)).toBe("-980.13 W");
    expect(formatWatts(1500.12345678)).toBe("1,500.12 W");
  });

  it("formatPercent carries the unit and trims", () => {
    expect(formatPercent(10)).toBe("10%");
    expect(formatPercent(46.5)).toBe("46.5%");
    expect(formatPercent(98.099)).toBe("98.1%");
    expect(formatPercent(46.55555555)).toBe("46.56%");
  });

  it("formatVolts carries the unit and the bound (pack and cell alike)", () => {
    expect(formatVolts(192.4)).toBe("192.4 V");
    expect(formatVolts(3.205)).toBe("3.21 V");
    expect(formatVolts(164.504999)).toBe("164.5 V");
  });

  it("formatMillivolts carries the cell spread's own scale", () => {
    expect(formatMillivolts(4)).toBe("4 mV");
    expect(formatMillivolts(12.345678)).toBe("12.35 mV");
  });

  it("formatAmps carries the unit and the sign", () => {
    expect(formatAmps(6.9)).toBe("6.9 A");
    expect(formatAmps(-6.9)).toBe("-6.9 A");
  });

  it("formatTemp carries the unit and the bound", () => {
    expect(formatTemp(23)).toBe("23 °C");
    expect(formatTemp(23.456789)).toBe("23.46 °C");
    expect(formatTemp(-12.345)).toBe("-12.35 °C");
  });
});

describe("ages and countdowns — whole seconds", () => {
  it("wholeSeconds rounds to integers and never yields -0", () => {
    expect(wholeSeconds(2)).toBe(2);
    expect(wholeSeconds(0.4)).toBe(0);
    expect(wholeSeconds(1.5)).toBe(2);
    expect(wholeSeconds(94.9)).toBe(95);
    expect(wholeSeconds(-0.4)).toBe(0);
  });

  it("formatSeconds renders whole seconds with the unit", () => {
    expect(formatSeconds(0.6)).toBe("1 s");
    expect(formatSeconds(20)).toBe("20 s");
    expect(formatSeconds(299.6)).toBe("300 s");
  });
});
