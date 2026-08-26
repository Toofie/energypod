# Site meter — the authoritative whole-site eye (Fronius Solar API v1)

Design date 2026-08-27, operator-directed ("yes wire it in" — the operator
handed the inverter's LAN address and authorized its use after the joint
reconciliation of 2026-08-27 08:40 showed the pod CT words structurally
blind to ~2 kW of household load). Status: CONTRACT v1.0, implementable.
Parents: `docs/DESIGN_EVENING_LOAD_SHARING.md` §3.2/§A1–A6 (the consumer
whose defect this provider cures), the provider precedents
(`src/energypod/adapters/providers/solcast.py`, `pvoutput/client.py`),
and the live probe record in `docs/CONTINUITY.md` 2026-08-27.

This document is DESIGN ONLY: no live-hardware authorization beyond the
read-only GETs the operator already sanctioned against their own inverter,
no new write primitive anywhere, no application-layer adapter imports.

## 1. What it is

ONE read-only HTTP GET per fleet cycle to the site's Fronius inverter:

    GET http://<host>[:port]/solar_api/v1/GetPowerFlowRealtimeData.fcgi

parsed into an immutable reading:

    SiteMeterReading:
      net_exchange_w: float | None   # IMPORT-positive (− P_Grid)
      load_w: float | None           # positive = served load     (− P_Load)
      pv_w: float | None             # production                  (P_PV)
      served_at_mono: float          # capture clock (the client's)
      wall_now: datetime
      quality: "good" | "unavailable"

Fronius sign conventions are translated AT THE ADAPTER (P_Grid negative =
export; P_Load negative-ish = load served). `Head.Status.Code != 0`,
missing/non-finite numbers, transport errors, or timeout produce
`quality: "unavailable"` with every watt `None` — NEVER zero-filled, the
forecast-provider rule. `P_Akku: null` is legitimate (no battery attached)
and is not an error.

## 2. Configuration — block-presence doctrine

    site_meter:
      provider: fronius          # the only value today
      host: <LAN address>        # REQUIRED, operator-provided
      port: 80                   # default
      request_timeout_s: 2.0     # bounds [0.5, 10]
      stale_after_s: 5.0         # must exceed timing.control_period_s

ABSENT block ⇒ nothing composes: byte-identical behavior everywhere, no
port constructed, both advisers keep the exact current logic. Cross-
validations on ControllerConfig name their rule: provider literal;
`stale_after_s > control_period_s`; timeouts positive. No credentials
exist on this surface (LAN read); the host string lives in config like any
endpoint fact.

## 3. Layering — adapter-blind application

`adapters/providers/fronius.py` owns HTTP+JSON+sign translation only.
The APPLICATION layer sees a protocol port injected at composition:

    class _SiteMeterPort(Protocol):
        def latest(self) -> SiteMeterReading | None: ...

polled ONCE per fleet cycle inside a bounded suppressed pass (a failed poll
is one unavailable word and one log line — never a delay to control), the
latest reading cached for the tick. The evening adviser consults it FIRST
each tick under the freshness gate; everything else about composition is
unchanged.

## 4. Freshness and precedence

A reading governs while `now − served_at ≤ stale_after_s`. Stale or
absent ⇒ the consuming program falls back per ITS contract
(`DESIGN_EVENING_LOAD_SHARING.md` A1/A6) — never to guesses, never to
zeros. `excess_charge` MAY consume the same reading later (graduation
path); it does NOT change this wave.

## 5. Projection

The snapshot gains feature-detected `site_meter_state` when composed:
{available, fresh, last_reading_at, last_error} — additive, absent when the
block is absent. Event vocabulary: none added (degradation surfaces as
codes of the consuming program).

## 6. Verification matrix (the wave's tests)

parse round-trips on captured real payloads incl. BOTH P_Grid polarities;
status≠0 refusal; non-finite/null handling; staleness gate; absence = byte-
identical compositions; block validation quartet; consumer precedence +
fallback; divergence-hold integration with the sharing loop.
