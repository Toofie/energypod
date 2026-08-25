"""Pod parking over the deterministic simulator (DESIGN_POD_PARKING section 6).

The T-PARK-SIMULATOR family: the device model grows the vendor debug-mode
word (``0x8100`` readback, ``{0, 1}`` whitelist, ``apply_debug_mode`` as the
transport-layer analog's refusal), the pinned-conservative ACK-then-ignore
semantics (a parked pod still ACKs ``[1, P, Q]`` writes while delivered power
stays 0, the served objective words stay UNCHANGED by an ignored write, and
the device watchdog lease is untouched -- "ignored means ignored"), the
``script_parked_readback_shows_write`` fork hook (both sides tested), the
``script_debug_mode(value, at_s)`` scenario hook, and the wedge signature end
to end: a dispatch accepted on a stale word, a park landing mid-flight, and
the actuation-coherence trip at the configured cycles -- the detected-and-
alarming dispatch/park race of section 4, enumerated by design.

Evidence authority: docs/evidence/standby-cycle-2026-08-24.md (the live rhs
standby cycle -- FC16 0x8000 writes, 0x8100 readback, ACK-while-parked,
power to 0, clean exit) and DESIGN_POD_PARKING sections 0/5/6.

SAFETY: no test here may ever contact hardware or open a socket.  The wedge
scenario composes the in-memory simulate mode only.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energypod.adapters.modbus import protocol_codec
from energypod.application.recovery import ECHO_MATCHES_WRITE
from energypod.simulator.pod import SimulatedEnergyPod
from energypod.simulator.transport import SimulatorTransport

# The live-proven wire facts (standby-cycle-2026-08-24.md): the debug-mode
# word is WRITTEN at 0x8000 and READ BACK at 0x8100 -- deliberately asymmetric
# vendor addresses nobody may normalize (DESIGN_POD_PARKING section 5 item 4).
DEBUG_MODE_WRITE_ADDRESS = 0x8000
DEBUG_MODE_READBACK_ADDRESS = 0x8100

# The pinned generic-gate refusal, byte-identical in both transports.
PQ_GATE_MESSAGE = "only the evidenced three-register PQ objective is writable"


class FakeMonotonicClock:
    """Injected monotonic time; the device model may read no other clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_parked_unit(
    *,
    clock: FakeMonotonicClock | None = None,
    watchdog_timeout_s: float = 2.0,
) -> tuple[SimulatedEnergyPod, SimulatorTransport, FakeMonotonicClock]:
    injected = clock if clock is not None else FakeMonotonicClock()
    pod = SimulatedEnergyPod(
        clock=injected,
        identity="SIM-PARK-0001",
        bic_count=3,
        seed=20260824,
        watchdog_timeout_s=watchdog_timeout_s,
    )
    transport = SimulatorTransport(pod=pod)
    return pod, transport, injected


async def debug_readback(transport: SimulatorTransport) -> int:
    return (await transport.read_holding(DEBUG_MODE_READBACK_ADDRESS, 1))[0]


async def measured_watts(pod: SimulatedEnergyPod, transport: SimulatorTransport) -> int:
    """Advance one explicit poll, then read the delivered battery watts."""
    pod.poll()
    system = await transport.read_holding(0x0100, 61)
    return protocol_codec.decode_signed16(system[20])


async def served_objectives(transport: SimulatorTransport) -> tuple[int, int]:
    detail = await transport.read_holding(0x1060, 32)
    return (
        protocol_codec.decode_signed16(detail[17]),
        protocol_codec.decode_signed16(detail[18]),
    )


def device_state(pod: SimulatedEnergyPod) -> dict[str, Any]:
    """Every device-model attribute except the injected clock (the bit-for-bit
    "device left unchanged" pin, the test_simulated_pod precedent)."""
    return {name: copy.deepcopy(value) for name, value in vars(pod).items() if name != "_clock"}


# --- the debug-mode word and its whitelist --------------------------------------


async def test_the_debug_mode_readback_serves_the_device_mode_word() -> None:
    """Section 6: the 0x8100 block serves ``_debug_mode`` (the hard-pinned
    ``[0]`` is gone) -- 0 at boot, tracking every sanctioned transition."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()

    assert await debug_readback(transport) == 0, "a fresh pod boots in Normal"

    pod.apply_debug_mode(1)
    assert await debug_readback(transport) == 1

    pod.apply_debug_mode(0)
    assert await debug_readback(transport) == 0
    await transport.close()


@pytest.mark.parametrize(
    "value",
    [2, 3, 4, 5, 6, -1, 7, 0x10000, True, 1.0, "1", None],
)
async def test_apply_debug_mode_accepts_only_the_sanctioned_values(value: Any) -> None:
    """Section 6 + section 0: ``apply_debug_mode`` validates ``value in {0,1}``
    at the device boundary -- the transport-layer analog's refusal.  Vendor
    values 2-6 (Charge, Discharge, Circulation, Fixing SOC, Verify Capacity)
    and every other shape are refused with the device bit-for-bit unchanged."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()
    pod.apply_debug_mode(1)
    before_state = device_state(pod)

    with pytest.raises((TypeError, ValueError), match="0 \\(Normal\\) or 1 \\(Standby\\)"):
        pod.apply_debug_mode(value)

    assert device_state(pod) == before_state, "a refused mode write must not touch device state"
    assert await debug_readback(transport) == 1
    await transport.close()


async def test_the_transport_write_debug_mode_routes_to_the_pod() -> None:
    """Section 5 item 1, simulator mirror: a separately NAMED
    ``write_debug_mode`` method -- the generic ``write_registers`` gate stays
    byte-identical and can never reach 0x8000."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()

    await transport.write_debug_mode(1)
    assert await debug_readback(transport) == 1

    await transport.write_debug_mode(0)
    assert await debug_readback(transport) == 0

    with pytest.raises(ValueError, match="0 \\(Normal\\) or 1 \\(Standby\\)"):
        await transport.write_debug_mode(2)
    assert await debug_readback(transport) == 0
    await transport.close()


async def test_write_debug_mode_accepts_and_ignores_the_timeout_override() -> None:
    """The production signature's per-op budget parameter (2026-08-24 live
    smoke: timing.mode_write_timeout_s): the simulator mirror ACCEPTS it and
    IGNORES it -- there is no wire here, so the one-shot write's commissioned
    budget has nothing to bound."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()

    await transport.write_debug_mode(1, timeout_s=2.0)
    assert await debug_readback(transport) == 1

    await transport.write_debug_mode(0, timeout_s=2.0)
    assert await debug_readback(transport) == 0
    await transport.close()


async def test_write_debug_mode_validates_before_the_connection_is_consulted() -> None:
    """The PQ gate's ordering, mirrored: a malformed write is refused before
    any connection state is consulted, exactly like ``_validated_pq_frame``."""
    pod, transport, _clock = build_parked_unit()
    # Deliberately NOT connected: a refused value must surface as the mode
    # refusal, never as a ConnectionError.
    with pytest.raises(ValueError, match="0 \\(Normal\\) or 1 \\(Standby\\)"):
        await transport.write_debug_mode(2)
    await transport.close()


@pytest.mark.parametrize(
    ("address", "values"),
    [
        (DEBUG_MODE_WRITE_ADDRESS, (0,)),
        (DEBUG_MODE_WRITE_ADDRESS, (1,)),
        (DEBUG_MODE_WRITE_ADDRESS, (2,)),
        (0x8001, (0xFF00,)),
        (0x0201, (1, 0, 0)),
        (0x0200, (2, 0, 0)),
    ],
)
async def test_the_generic_write_gate_is_byte_identical_and_never_reaches_0x8000(
    address: int, values: tuple[int, ...]
) -> None:
    """T-PARK-SIMULATOR / architecture-fitness: the generic ``write_registers``
    path can NEVER reach the debug register -- the pinned refusal message is
    byte-identical to the production transport's, for the mode writes and for
    every other non-PQ shape alike.  Only the named method can park."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(100, 0))
    before_state = device_state(pod)
    before_readback = await debug_readback(transport)

    with pytest.raises(ValueError, match=f"^{PQ_GATE_MESSAGE}$"):
        await transport.write_registers(address, values)

    assert device_state(pod) == before_state, "a refused generic write changes nothing"
    assert await debug_readback(transport) == before_readback
    await transport.close()


# --- ACK-then-ignore: delivery, served words, watchdog --------------------------


async def test_a_parked_pod_acks_writes_but_delivers_no_power() -> None:
    """Section 6, the live-proven half: while parked, ``apply_pq_frame`` still
    ACKs (no refusal) but delivered power stays 0 on every measured view --
    and resume returns the pod to Normal control with the next write."""
    pod, transport, clock = build_parked_unit()
    await transport.connect()
    pod.poll()

    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-1500, 0))
    assert await measured_watts(pod, transport) == -1500, "scenario guard: driving pre-park"

    pod.apply_debug_mode(1)

    # Renewal keeps flowing while parked: every write ACKs (no raise), the
    # measured words stay 0 across the delivered views (system +20, BMS +8,
    # PCS live +13, DCDC +9), and the pack current word is 0.
    for _ in range(3):
        await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-1500, 0))
        clock.advance(0.4)
        assert await measured_watts(pod, transport) == 0
        bms = await transport.read_holding(0x5000, 31)
        assert protocol_codec.decode_signed16(bms[8]) == 0
        pcs_live = await transport.read_holding(0x1000, 21)
        assert protocol_codec.decode_signed16(pcs_live[13]) == 0
        dcdc = await transport.read_holding(0x2000, 13)
        assert protocol_codec.decode_signed16(dcdc[9]) == 0
        assert protocol_codec.decode_signed16(bms[7]) == 0, "pack current is 0 while parked"

    # Resume: the exit write (live-proven ~1 s, no wedge) returns control.
    pod.apply_debug_mode(0)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-1500, 0))
    assert await measured_watts(pod, transport) == -1500
    await transport.close()


async def test_an_ignored_write_leaves_the_served_objective_words_unchanged() -> None:
    """Section 6, the pinned-conservative readback half: while parked, the
    served objective words (0x1060+17/+18) are UNCHANGED by an ignored write
    -- they keep holding whatever was served before the write, and a parked
    idle pod serves (0, 0) no matter how many objectives arrive."""
    pod, transport, clock = build_parked_unit()
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-1200, 300))
    pod.poll()
    assert await served_objectives(transport) == (-1200, 300), "scenario guard: pre-park pair"

    pod.apply_debug_mode(1)

    # The ignored write's pair (700, -50) never reaches the served words.
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(700, -50))
    assert await served_objectives(transport) == (-1200, 300)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(700, -50))
    assert await served_objectives(transport) == (-1200, 300)

    # An idle park: nothing was ever latched, so the served pair stays (0, 0)
    # under ignored writes too.
    pod.apply_debug_mode(0)
    clock.advance(3.0)  # expire the pre-park lease
    pod.poll()
    assert await served_objectives(transport) == (0, 0)
    pod.apply_debug_mode(1)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(999, 0))
    assert await served_objectives(transport) == (0, 0)
    await transport.close()


async def test_an_ignored_write_never_touches_the_device_watchdog_lease() -> None:
    """Section 6: "the device watchdog lease is untouched" -- an ignored write
    renews nothing.  Pinned from both sides: nothing is latched by writes that
    arrive while parked, and a pre-park lease still expires at its ORIGINAL
    deadline despite continuous ignored renewal."""
    pod, transport, clock = build_parked_unit(watchdog_timeout_s=2.0)
    await transport.connect()

    # Side 1: writes arriving while parked from idle latch nothing.
    pod.apply_debug_mode(1)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-800, 0))
    assert pod._applied_active_w == 0
    assert pod._lease_deadline_mono is None

    # Side 2: a live pre-park lease expires on schedule under ignored renewal.
    pod.apply_debug_mode(0)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-800, 0))
    assert pod._lease_deadline_mono is not None
    original_deadline = pod._lease_deadline_mono

    pod.apply_debug_mode(1)
    clock.advance(0.5)  # an ignored renewal here would move the deadline
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-800, 0))
    assert pod._lease_deadline_mono == original_deadline, "an ignored write renews nothing"

    clock.advance(1.6)  # past the ORIGINAL 2.0 s deadline
    pod.poll()
    assert pod._lease_deadline_mono is None, "the pre-park lease expired on its own schedule"
    assert pod._applied_active_w == 0
    await transport.close()


async def test_script_parked_readback_shows_write_flips_the_readback_half() -> None:
    """Section 6's fork, both sides in one scenario: the conservative default
    keeps the served words unchanged under an ignored write; the
    ``script_parked_readback_shows_write`` hook flips exactly the readback
    half (the served pair shows the ignored write) while the DELIVERY half
    (power stays 0) and the LEASE half (nothing latched, nothing renewed) hold
    -- the model is pinned until live evidence lands, and the hook is how the
    scenario answers the question when it does."""
    pod, transport, _clock = build_parked_unit()
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-1200, 0))
    pod.poll()
    pod.apply_debug_mode(1)

    # The conservative side (the default): unchanged.
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(400, 60))
    assert await served_objectives(transport) == (-1200, 0)
    assert await measured_watts(pod, transport) == 0

    # The flipped side: the readback now SHOWS the ignored write...
    pod.script_parked_readback_shows_write()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(400, 60))
    assert await served_objectives(transport) == (400, 60)
    # ...but delivery and the lease are untouched by the flip.
    assert await measured_watts(pod, transport) == 0
    assert pod._applied_active_w == -1200, "the ignored write latched nothing"
    assert await served_objectives(transport) == (400, 60)

    # Flipping back restores the conservative readback, and resume clears the
    # parked echo: the served words return to the pod's own applied pair.
    pod.script_parked_readback_shows_write(False)
    assert await served_objectives(transport) == (-1200, 0)
    pod.apply_debug_mode(0)
    assert await served_objectives(transport) == (-1200, 0)
    await transport.close()


# --- the scenario hook ------------------------------------------------------------


async def test_script_debug_mode_lands_at_the_scripted_time() -> None:
    """Section 6: ``script_debug_mode(value, at_s)`` in the script_objective
    style -- the transition lands when the injected clock reaches ``at_s``
    (advanced only by the explicit poll), so a scenario can park mid-flight
    without interleaving a direct call between the drive's steps."""
    pod, transport, clock = build_parked_unit()
    await transport.connect()
    pod.poll()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(600, 0))

    pod.script_debug_mode(1, at_s=clock.monotonic() + 1.0)

    clock.advance(0.9)
    pod.poll()
    assert await debug_readback(transport) == 0, "before at_s the pod is still Normal"
    assert await measured_watts(pod, transport) == 600

    clock.advance(0.2)  # past at_s
    pod.poll()
    assert await debug_readback(transport) == 1
    assert await measured_watts(pod, transport) == 0, "the park landed mid-flight"

    # A scheduled resume lands the same way.
    pod.script_debug_mode(0, at_s=clock.monotonic() + 1.0)
    clock.advance(1.1)
    pod.poll()
    assert await debug_readback(transport) == 0
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(600, 0))
    assert await measured_watts(pod, transport) == 600
    await transport.close()


async def test_a_direct_mode_write_supersedes_a_pending_scheduled_one() -> None:
    """The device word is what was just written: a direct ``apply_debug_mode``
    (the transport path) clears any pending scheduled transition, so a stale
    scenario schedule cannot re-park a pod the controller just resumed."""
    pod, transport, clock = build_parked_unit()
    await transport.connect()
    pod.poll()

    pod.script_debug_mode(1, at_s=clock.monotonic() + 1.0)
    pod.apply_debug_mode(0)
    clock.advance(2.0)
    pod.poll()
    assert await debug_readback(transport) == 0, "the superseded schedule never lands"
    await transport.close()


@pytest.mark.parametrize(
    ("value", "at_s"),
    [
        (2, 101.0),
        (-1, 101.0),
        (True, 101.0),
        ("1", 101.0),
        (1, -1.0),
        (1, float("nan")),
        (1, float("inf")),
        (1, "101.0"),
        (1, True),
    ],
)
async def test_script_debug_mode_validates_value_and_time(value: Any, at_s: Any) -> None:
    pod, _transport, _clock = build_parked_unit()
    before_state = device_state(pod)

    with pytest.raises((TypeError, ValueError)):
        pod.script_debug_mode(value, at_s)

    assert device_state(pod) == before_state


# --- the wedge signature, end to end over the composed simulator ------------------
#
# DESIGN_POD_PARKING section 4: the dispatch/park race (a dispatch accepted on
# a stale word, then the park lands, the pod ACK-then-ignores, coherence
# alarms ~4 cycles later) is a DETECTED-AND-ALARMING outcome by design -- the
# watchdog is the fence, enumerated as such, not an error.

_CHARGE_W = 250
_CONTROL_PERIOD_S = 0.40


@dataclass
class _ManualClock:
    """The single deterministic time source the composed runtime may read."""

    now: float = 1000.0
    wall: datetime = field(default_factory=lambda: datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC))
    _waiters: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        self._release()
        await future

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)
        self._release()

    def _release(self) -> None:
        pending: list[tuple[float, asyncio.Future[None]]] = []
        for deadline, future in self._waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)
            elif not future.done():
                pending.append((deadline, future))
        self._waiters = pending


def _parking_fleet_payload() -> dict[str, Any]:
    """One commissioned single-unit write-enabled config (no storage: the
    simulate composition is fully in-memory, so nothing opens a file)."""
    from tests.unit.test_composition import (
        _authentication_payload,
        _policy_payload,
        _timing_payload,
        _unit_payload,
    )

    return {
        "schema_version": 1,
        "revision": 11,
        "mode": "write_enabled",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [_unit_payload("mid", "BEP-MID", "192.168.1.11")],
        "timing": _timing_payload(),
        # The seeded simulator image carries a ~0.2 V cell spread (the
        # simulate-suite convention: every composed dispatch scenario widens
        # the imbalance bound to 0.50 so the seeded banks pass the kernel).
        "policy": {**_policy_payload(), "maximum_cell_imbalance_v": 0.50},
        "authentication": _authentication_payload(),
    }


async def _observe_cycle(runtime: Any, clock: _ManualClock) -> tuple[Any, int]:
    """One unit's fleet-cycle recovery pass, the supervision loop's shape.

    Returns the findings and the authority the pass observed.  The trigger-
    time objective echo read-back is deliberately NOT driven here: that window
    is wired for the live wire transport, and the simulator's discriminator is
    the coherence trip itself.
    """
    actor = runtime.actors["mid"]
    capability = await runtime.authorizations.peek("mid")
    authorized_watts = int(getattr(capability, "watts", 0) or 0)
    direction = getattr(capability, "direction", None)
    authorized_direction = None if direction is None else getattr(direction, "value", direction)
    await actor.heartbeat_once()
    await actor.poll_once()
    findings = await runtime.recovery.observe_cycle(
        "mid",
        authorized_watts=authorized_watts,
        authorized_direction=authorized_direction,
        claimed=True,
        lifecycle=actor.lifecycle,
        inhibit_latched=bool(actor.inhibit_latched),
        inhibit_reason=actor.inhibit_reason,
        observation=await runtime.observations.latest("mid"),
        now_mono=clock.monotonic(),
    )
    return findings, authorized_watts


def _recovery_events(runtime: Any, event_type: str) -> tuple[Any, ...]:
    return tuple(
        event for event in runtime.audit.recent(limit=64) if event.event_type == event_type
    )


async def test_a_mid_flight_park_is_the_detected_wedge_signature() -> None:
    """T-PARK-SIMULATOR wedge end-to-end: dispatch accepted on a stale word ->
    ``script_debug_mode(1)`` lands mid-flight -> the writes keep ACKing while
    delivered power stays 0 -> the actuation-coherence watchdog trips at
    EXACTLY the configured cycles -- not before, not after -- with the debug
    word visible on the wire and a fresh dispatch refused
    ``device_debug_mode_active`` (the existing wire-level gate)."""
    from energypod.runtime.composition import build_runtime
    from energypod.runtime.config import ControllerConfig
    from tests.unit.test_composition import OPERATOR

    clock = _ManualClock()
    config = ControllerConfig.model_validate(_parking_fleet_payload())
    runtime = build_runtime(config, clock=clock, simulate=True)
    cycles = config.policy.actuation_coherence_cycles if config.policy else 4
    assert cycles == 4, "scenario guard: the commissioned default trip streak"

    try:
        actor = runtime.actors["mid"]
        pod = runtime.simulators["mid"]
        await actor.start()

        # Idle cycles: qualification plus the pre-command measured baseline.
        for _ in range(runtime.policy.stable_samples_needed_to_rearm + 2):
            clock.advance(0.05)
            await actor.poll_once()
            await _observe_cycle(runtime, clock)
        assert actor.qualified is True

        await runtime.facade.arm(
            unit_ids=["mid"],
            principal=OPERATOR,
            idempotency_key="park-wedge-arm",
            request_id="park-wedge-arm-request",
        )
        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        view = await runtime.facade.submit_intent(
            unit_ids=["mid"],
            direction="charge",
            watts=_CHARGE_W,
            ttl_s=30.0,
            reason="parking wedge scenario",
            principal=OPERATOR,
            idempotency_key="park-wedge-dispatch",
            request_id="park-wedge-dispatch-request",
        )
        assert view["status"] == "accepted", view

        # Healthy driving cycles: the charge lands, nothing alarms.
        for _ in range(2):
            clock.advance(_CONTROL_PERIOD_S)
            await _observe_cycle(runtime, clock)
            await runtime.kernel.tick()
        observation = await runtime.observations.latest("mid")
        assert observation is not None
        assert observation.battery_watts == -float(_CHARGE_W)
        assert observation.debug_mode_w == 0
        assert _recovery_events(runtime, "actuation_incoherent") == ()

        # The park lands mid-flight at a scripted time: the very next poll
        # serves word 1, a fresh dispatch is refused on the wire-level gate,
        # and the standing intent keeps renewing into ACK-then-ignore.
        pod.script_debug_mode(1, at_s=clock.monotonic() + _CONTROL_PERIOD_S / 2)
        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        observation = await runtime.observations.latest("mid")
        assert observation is not None
        assert observation.debug_mode_w == 1
        with pytest.raises(ValueError, match="device_debug_mode_active"):
            await runtime.facade.submit_intent(
                unit_ids=["mid"],
                direction="charge",
                watts=_CHARGE_W,
                ttl_s=30.0,
                reason="parking wedge scenario stale-word dispatch",
                principal=OPERATOR,
                idempotency_key="park-wedge-dispatch-2",
                request_id="park-wedge-dispatch-2-request",
            )

        # The parked cycles: authority keeps flowing (the standing intent
        # keeps renewing -- the race premise), the writes keep ACKing,
        # delivery stays 0, and no alarm lands before the configured streak.
        await runtime.kernel.tick()  # keep every peeked authority fresh per cycle
        for parked_cycle in range(1, cycles + 1):
            clock.advance(_CONTROL_PERIOD_S)
            _findings, authorized_watts = await _observe_cycle(runtime, clock)
            await runtime.kernel.tick()
            assert authorized_watts == _CHARGE_W, (
                "the standing intent's authority must keep flowing while parked"
            )
            observation = await runtime.observations.latest("mid")
            assert observation is not None
            assert observation.battery_watts == 0.0, "an ignored write delivers nothing"
            if parked_cycle < cycles:
                assert _recovery_events(runtime, "actuation_incoherent") == (), (
                    f"no alarm before the configured streak (cycle {parked_cycle})"
                )

        (incoherent,) = _recovery_events(runtime, "actuation_incoherent")
        assert incoherent.unit_id == "mid"
        assert incoherent.authorized_active_w == -_CHARGE_W
        assert "authorized_not_actuating" in incoherent.reason_codes

        # WAVE 0 W0-2b (DESIGN_BATTERY_HEALTH_WATCH §3.2): the trip records
        # evidence; the health STATE opens only on the objective-echo
        # discriminator's verdict.  The parked pod keeps ACKing and its
        # served-objective word keeps mirroring our write (the pinned
        # ACK-then-ignore model), so the echo matches -- the state opens as
        # the live supervision loop (which drives this read itself) would
        # see it.
        await runtime.recovery.record_incoherence_echo(
            "mid",
            classification=ECHO_MATCHES_WRITE,
            served_active_w=-_CHARGE_W,
            served_reactive_var=0,
        )

        # The snapshot renders the wedge signature state.
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = next(view for view in snapshot["units"] if view["unit_id"] == "mid")
        assert unit["health_state"] == "actuation_incoherent"
        assert "authorized_not_actuating" in unit["health_reasons"]
        assert unit["telemetry"]["debug_mode_w"] == 1

        # No second alarm inside the episode while the pod stays parked.
        for _ in range(3):
            clock.advance(_CONTROL_PERIOD_S)
            await _observe_cycle(runtime, clock)
            await runtime.kernel.tick()
        assert len(_recovery_events(runtime, "actuation_incoherent")) == 1
    finally:
        for actor in runtime.actors.values():
            with contextlib.suppress(Exception):
                await actor.shutdown()
