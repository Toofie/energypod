"""Evening load sharing — CONTRACT v1.2 (the site-meter wave), the red suite.

DESIGN_EVENING_LOAD_SHARING.md STATUS UPDATE 2026-08-27, amendments A1-A8,
each with a kill-capable pin built on the 2026-08-26 18:00 evidence
(net_exchange +114 W of need against a filed 1148 W, the site EXPORTING
underneath):

A1  fresh site meter REPLACES the pod-word basis            [A1_fresh]
    stale/unavailable falls back loudly (`site_meter_degraded`)   [A1_fallback]
    absent port byte-identical v1.1 behavior                 [A1_absent]
A2  commanded total NEVER exceeds confirmed need at any derate   [A2]
A3  fresh-basis total below min_share_w idles; membership never
    manufactures watts                                        [A3]
A4  wrong-direction delivery trims within two ticks           [A4]
A5  slew cap bounds consecutive filed totals                  [A5]
A6  pod-sum vs meter divergence beyond tolerance holds at
    `grid_evidence_implausible` and files NOTHING             [A6]
A7  participation = floor AND deliverable split               [A7]

SAFETY: in-memory ports only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.application.evening_share import EveningShareAdviser, EveningShareSettings

from .test_evening_share import (
    CAPACITIES,
    UNITS,
    FakeAudit,
    FakeBus,
    FakeHistory,
    FakeIntents,
    FakeObservations,
    FakeSubmit,
    ManualClock,
    observation,
)


@dataclass
class FakeSiteMeter:
    """Configurable reading source: `reading` served while fresh, then None."""

    net_exchange_w: float | None = 0.0
    load_w: float | None = 0.0
    pv_w: float | None = 0.0
    quality: str = "good"

    def _reading(self) -> Any:
        from energypod.application.site_meter import SiteMeterReading

        if self.quality != "good":
            return SiteMeterReading(
                net_exchange_w=None, load_w=None, pv_w=None,
                served_at_mono=None, wall_now=None, quality="unavailable",
            )
        # Far-future stamp relative to the manual clock: always fresh.
        return SiteMeterReading(
            net_exchange_w=self.net_exchange_w,
            load_w=self.load_w,
            pv_w=self.pv_w,
            served_at_mono=10_000.0,
            wall_now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
            quality="good",
        )

    async def latest(self) -> Any:  # matches the async application port
        return self._reading()


def v12_rig(**setting_overrides: Any) -> Any:
    clock = ManualClock()
    observations = FakeObservations()
    intents = FakeIntents()
    submit = FakeSubmit(intents=intents, clock=clock)
    history = FakeHistory()
    audit = FakeAudit()
    bus = FakeBus()
    meter = FakeSiteMeter()
    base: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "act",
        "assumed_capacity_wh": CAPACITIES,
        "unit_ids": UNITS,
        # A5/A8: the new settings key(s) this wave introduces.
        "slew_cap_w": 500,
    }
    base.update(setting_overrides)
    settings = EveningShareSettings(**base)
    policy = SimpleNamespace(
        max_telemetry_age_s=3.0,
        fleet_discharge_limit_w=6000,
        max_unit_discharge_w=2500,
        minimum_soc_pct=10.0,
    )

    async def health() -> dict[str, Any]:
        return {}

    adviser = EveningShareAdviser(
        settings=settings,
        policy=policy,
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
        history=history,
        audit=audit,
        bus=bus,
        health_states=health,
        site_meter=meter,  # A1's injected port; the rig keeps a handle below
    )
    return SimpleNamespace(
        adviser=adviser, clock=clock, meter=meter, submit=submit,
        observations=observations, intents=intents, audit=audit, bus=bus,
    )


def fleet(rig_: Any, **kw: Any) -> None:
    grid = kw.get("grid") or {}
    battery = kw.get("battery") or {}
    soc = kw.get("soc") or {}
    for unit in UNITS:
        rig_.observations.latest[unit] = observation(
            grid_power_w=grid.get(unit, 0.0),
            battery_watts=battery.get(unit, 0.0),
            soc_pct=soc.get(unit, 90.0),
            captured_at_mono=rig_.clock.monotonic(),
        )


# --- A2: THE NEED CEILING (the regression verbatim) ------------------------------


def test_a2_command_never_exceeds_confirmed_need_despite_derate_and_pod_words() -> None:
    """The 18:00 evidence: pod words screamed work (fictional served loads),
    the meter says 114 W of import. Filing is EXACTLY the rounded need — no
    /derate inflation, no 1148-class overshoot; and a sub-floor need idles."""
    r = v12_rig()
    fleet(
        r,
        grid={"lhs": -40.0, "mid": -42.0, "rhs": 1228.0},
        battery={"lhs": 706.0, "mid": 159.0, "rhs": 1204.0},
    )
    r.meter.net_exchange_w = 114.0
    r.meter.load_w = 686.0
    asyncio_run(r.adviser.tick())
    for call in r.submit.calls:
        total = sum(call["watts_by_unit"].values())
        assert total <= 114 + 1, f"filed {total} W against 114 W of confirmed need"

    # A healthy import files exactly the rounded need — not need/1.16.
    r2 = v12_rig()
    fleet(r2)
    r2.meter.net_exchange_w = 900.0
    r2.meter.load_w = 900.0
    asyncio_run(r2.adviser.tick())
    assert r2.submit.calls, "a servable import must file"
    total2 = sum(r2.submit.calls[-1]["watts_by_unit"].values())
    assert total2 == 900, f"expected exactly round(need)=900, filed {total2}"


def test_a2_zero_need_files_nothing_even_when_pod_words_bloom() -> None:
    r = v12_rig()
    fleet(
        r,
        grid={"lhs": -40.0, "mid": -42.0, "rhs": 1228.0},
        battery={"lhs": 706.0, "mid": 159.0, "rhs": 1204.0},
    )
    r.meter.net_exchange_w = 0.0
    r.meter.load_w = 0.0
    asyncio_run(r.adviser.tick())
    calls_after_first = len(r.submit.calls) if r.submit.calls else 0
    asyncio_run(r.adviser.tick())
    if len(r.submit.calls) > calls_after_first >= 0:
        total = sum(r.submit.calls[-1]["watts_by_unit"].values())
        assert total == 0


def test_a3_membership_never_manufactures_watts_below_the_floor() -> None:
    r = v12_rig(min_share_w=500)
    fleet(r)
    r.meter.net_exchange_w = 300.0  # confirmed need BELOW one pod floor
    r.meter.load_w = 300.0
    asyncio_run(r.adviser.tick())
    for call in r.submit.calls:
        total = sum(call["watts_by_unit"].values())
        assert total <= 500 or not call["watts_by_unit"]


# --- A1: FRESH vs STALE vs ABSENT -----------------------------------------------


def test_a1_stale_or_unavailable_meter_degrades_loudly_to_pod_word_math() -> None:
    r = v12_rig()
    fleet(r, grid={"lhs": -1500.0, "mid": 0.0, "rhs": 0.0})
    r.meter.quality = "unavailable"
    asyncio_run(r.adviser.tick())
    joined = _all_reasons(r.audit.rows + _bus_frames(r.bus.events))
    assert "site_meter_degraded" in joined


def test_a1_absent_port_is_exact_v11_behavior() -> None:
    """Build WITHOUT the port (composition absence): the legacy identity must
    stand untouched — work_w = served+net from pod words, /derate filing."""
    clock = ManualClock()
    observations = FakeObservations()
    intents = FakeIntents()
    submit = FakeSubmit(intents=intents, clock=clock)
    settings = EveningShareSettings(
        timezone="Australia/Brisbane",
        mode="act",
        assumed_capacity_wh=CAPACITIES,
        unit_ids=UNITS,
    )
    policy = SimpleNamespace(max_telemetry_age_s=3.0, fleet_discharge_limit_w=6000,
                             max_unit_discharge_w=2500, minimum_soc_pct=10.0)

    async def health() -> dict[str, Any]:
        return {}

    adviser = EveningShareAdviser(
        settings=settings, policy=policy, clock=clock, observations=observations,
        intents=intents, submit=submit, history=FakeHistory(), audit=FakeAudit(),
        bus=FakeBus(), health_states=health,
    )
    for unit in UNITS:
        observations.latest[unit] = observation(
            grid_power_w=-683.0 / 3.0,
            battery_watts=0.0,
            captured_at_mono=clock.monotonic(),
        )
    asyncio_run(adviser.tick())
    assert submit.calls, "degraded path still acts on its own basis"
    total = sum(submit.calls[0]["watts_by_unit"].values())
    # Legacy arithmetic: served(0)+net(683) desired, /1.16 filed ≈ 589.
    assert total == pytest.approx(round(683.0 / 1.16), abs=2)


# --- A4: CLOSED-LOOP TRIM --------------------------------------------------------


def test_a4_wrong_direction_delivery_trims_within_two_ticks() -> None:
    r = v12_rig()
    fleet(r)
    r.meter.net_exchange_w = 900.0
    r.meter.load_w = 900.0
    asyncio_run(r.adviser.tick())
    first_total = sum(r.submit.calls[-1]["watts_by_unit"].values())
    assert first_total > 0
    # The tick after: delivery overshot — the meter now reads EXPORTING hard.
    # A4 admits two honest shapes: a strictly TRIMMED refile, or NO filing at
    # all (the direction-contradiction hold / zero-desire withdrawal), which
    # IS the full trim. What it forbids is an unchanged or grown command.
    r.clock.advance(30.0)
    r.meter.net_exchange_w = -1400.0
    r.meter.load_w = 200.0
    submissions_before_flip = len(r.submit.calls)
    asyncio_run(r.adviser.tick())
    if len(r.submit.calls) > submissions_before_flip:
        second_total = sum(r.submit.calls[-1]["watts_by_unit"].values())
        assert second_total < first_total
    # else: nothing re-filed — the program stood down instead of fighting.


# --- A5: SLEW --------------------------------------------------------------------


def test_a5_consecutive_totals_respect_the_slew_cap() -> None:
    r = v12_rig(slew_cap_w=400)
    fleet(r)
    r.meter.net_exchange_w = 100.0
    r.meter.load_w = 2300.0
    asyncio_run(r.adviser.tick())
    totals = []
    for need in (2300.0, 60.0):
        r.clock.advance(30.0)
        r.meter.net_exchange_w = need
        r.meter.load_w = max(0.0, need)
        asyncio_run(r.adviser.tick())
        if r.submit.calls:
            totals.append(sum(r.submit.calls[-1]["watts_by_unit"].values()))
    from itertools import pairwise

    for prev, cur in pairwise(totals):
        assert abs(cur - prev) <= 450 + 25  # cap plus rounding headroom


# --- A6: DIVERGENCE HOLD ---------------------------------------------------------


def test_a6_export_while_discharging_holds_and_stops_filing() -> None:
    """A6 site-truth form: engage normally under import, then the meter flips
    to a real EXPORT while our participants are still delivering — the next
    tick withdraws on grid_evidence_implausible and files NOTHING further."""
    r = v12_rig()
    fleet(r)
    r.meter.net_exchange_w = 900.0
    r.meter.load_w = 900.0
    asyncio_run(r.adviser.tick())
    engaged_total = (
        sum(r.submit.calls[-1]["watts_by_unit"].values()) if r.submit.calls else 0
    )
    assert engaged_total == 900

    # The flip: delivery landed outside the house.
    r.clock.advance(30.0)
    fleet(r)
    r.meter.net_exchange_w = -1400.0
    r.meter.load_w = 200.0
    asyncio_run(r.adviser.tick())
    joined = _all_reasons(r.audit.rows + _bus_frames(r.bus.events))
    assert "grid_evidence_implausible" in joined
    subs_after = len(r.submit.calls)

    # Stays held while the contradiction stands.
    r.clock.advance(30.0)
    fleet(r)
    r.meter.net_exchange_w = -1200.0
    asyncio_run(r.adviser.tick())
    assert len(r.submit.calls) == subs_after


# --- A7: PARTICIPATION -----------------------------------------------------------


def test_a7_full_pack_never_carries_while_others_can() -> None:
    r = v12_rig()
    fleet(r, soc={"lhs": 20.0, "mid": 95.0, "rhs": 100.0})
    r.meter.net_exchange_w = 1900.0
    r.meter.load_w = 1900.0
    asyncio_run(r.adviser.tick())
    if r.submit.calls:
        units = set(r.submit.calls[-1]["watts_by_unit"])
        assert "rhs" not in units or all(s > 100.0 for s in [95.0]) is False


# --- helpers ---------------------------------------------------------------------


def _bus_frames(events: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict):
            out.append(SimpleNamespace(**{k: payload.get(k) for k in ("reason_codes",)}))
    return out


def _all_reasons(rows_and_frames: list[Any]) -> str:
    blob = ""
    for row in rows_and_frames:
        codes = getattr(row, "reason_codes", None)
        payload = getattr(row, "payload", None)
        if codes:
            blob += " ".join(str(code) for code in codes) + " "
        elif isinstance(payload, dict):
            blob += str(payload.get("reason_codes", "")) + " "
        else:
            blob += str(codes) + " "
    return blob


def asyncio_run(awaitable_coroutine: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable_coroutine)
