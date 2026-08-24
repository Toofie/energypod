/**
 * Behavior contract for the shared plain-word maps (web/src/app/fleet.ts):
 * the lifecycle vocabulary's ONE wording and the recovery vocabulary's short
 * words — the maps the shell's banner list, the Batteries rows, and the
 * History strip's bands/listings all render from, so their truths cannot
 * drift apart.
 *
 * The pins:
 *
 * - EVERY WIRE VALUE HAS A WORD: each lifecycle in `LIFECYCLES` and each
 *   state in `HEALTH_STATES` maps to operator words — a stranger on the wire
 *   is humanized ("some_new_state" → "Some new state"), never passed through
 *   as a raw code.
 * - THE UNIFICATION: `armed_idle` is "Armed and idle" everywhere — the idle
 *   half is the fact the state asserts (the pre-unification "Armed" was the
 *   banner-badge word wearing the wrong surface).
 */
import { describe, expect, it } from "vitest";
import {
  HEALTH_STATES,
  healthWord,
  LIFECYCLE_WORDS,
  lifecycleWord,
} from "./fleet";
import { LIFECYCLE_VALUES } from "../test/wire";

describe("fleet maps — the lifecycle words", () => {
  it("words every lifecycle the wire can serve, never a raw code", () => {
    for (const lifecycle of LIFECYCLE_VALUES) {
      const word = lifecycleWord(lifecycle);
      expect(word).not.toBe(lifecycle);
      expect(word).not.toMatch(/_/);
      expect(word.length).toBeGreaterThan(0);
      // The map itself carries the word (the helper only adds the fallback).
      expect(LIFECYCLE_WORDS[lifecycle]).toBe(word);
    }
  });

  it("pins the unification: armed_idle is 'Armed and idle' in the ONE shared map", () => {
    expect(lifecycleWord("armed_idle")).toBe("Armed and idle");
    expect(LIFECYCLE_WORDS.armed_idle).toBe("Armed and idle");
  });

  it("keeps the per-state words the surfaces already render", () => {
    expect(lifecycleWord("boot")).toBe("Starting up");
    expect(lifecycleWord("observe_only")).toBe("Observe only");
    expect(lifecycleWord("disarmed")).toBe("Disarmed");
    expect(lifecycleWord("active")).toBe("Active");
    expect(lifecycleWord("inhibited")).toBe("Inhibited");
    expect(lifecycleWord("stopping")).toBe("Stopping");
    expect(lifecycleWord("disconnected")).toBe("No contact");
  });

  it("humanizes an unknown code instead of showing it raw", () => {
    expect(lifecycleWord("some_new_state")).toBe("Some new state");
    expect(lifecycleWord("mysterious")).toBe("Mysterious");
    expect(lifecycleWord("")).toBe("");
  });
});

describe("fleet maps — the health short words", () => {
  it("words every recovery state the monitor can serve, never a raw code", () => {
    for (const state of HEALTH_STATES) {
      const word = healthWord(state);
      expect(word).not.toBe(state);
      expect(word).not.toMatch(/_/);
      expect(word.length).toBeGreaterThan(0);
    }
  });

  it("keeps the strip's short words", () => {
    expect(healthWord("healthy")).toBe("Healthy");
    expect(healthWord("self_healing")).toBe("Self-healing");
    expect(healthWord("not_responding")).toBe("Not responding");
    expect(healthWord("unreachable")).toBe("Unreachable");
    expect(healthWord("inhibited")).toBe("Inhibited");
    expect(healthWord("parked")).toBe("Parked");
    expect(healthWord("foreign_writer")).toBe("Foreign writer");
    expect(healthWord("actuation_incoherent")).toBe("Actuation incoherent");
  });

  it("humanizes an unknown code instead of showing it raw", () => {
    expect(healthWord("new_monitor_state")).toBe("New monitor state");
  });
});
