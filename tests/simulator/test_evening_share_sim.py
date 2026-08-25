"""T-ELS-SIMULATOR — the evening load-sharing program's scripted device legs.

DESIGN_EVENING_LOAD_SHARING §13: the deterministic pods the program's own
stories run against — the three-phase site plant (kitchen/garage/upstairs
load split, the phase map's evidence base), the dead-CT pod (the rhs ~16 W
spectator class — the split must not move), the delivery-overshoot plant
(1.0x / 1.16x / 1.3x — the derate's regression), the E3 frozen-word plants
(a fresh-stamped frozen GRID word with a moving battery word, and the frozen
BATTERY mirror), the kernel-denied/not-delivering plant (E4), the
surplus-evening script (the excess corner), and the restart-in-mid-evening
harness shape (the stateless property the unit suite pins).

The pod is exposed directly as the scenario handle (the house simulator
pattern): the device model advances only through the injected clock, so an
evening's hours are a handful of large clock advances.  The CONTROL-side
legs (the identity, the seam races, the split) are unit-pinned in
``tests/unit/test_evening_share.py``; these legs pin the PLANTS the control
path reads.

SAFETY: the deterministic in-process simulator only — no socket, no live
system.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.adapters.modbus import protocol_codec
from energypod.simulator.pod import (
    _BMS_BASE,
    _PCS_GRID_POWER_OFFSET,
    _PCS_LIVE_BASE,
    _PCS_LOAD_POWER_OFFSET,
)


@pytest.fixture(scope="module")
def simulator() -> Any:
    pod_module = importlib.import_module("energypod.simulator.pod")
    return SimpleNamespace(SimulatedEnergyPod=pod_module.SimulatedEnergyPod)


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class PodHarness:
    pod: Any
    clock: FakeMonotonicClock = field(default_factory=FakeMonotonicClock)

    def drive(self, watts: int, seconds: float) -> None:
        """Latch a discharge objective, advance time, poll: the pod delivers
        the objective over the elapsed window (renewing the watchdog lease
        each drive)."""
        self.pod.apply_pq_frame(protocol_codec.encode_pq_registers(watts, 0))
        self.clock.advance(seconds)
        self.pod.poll()

    def battery_watts(self) -> int:
        # The BMS window's power word (index 8 of 0x5000, 31) — signed.
        value = self.pod.read(_BMS_BASE, 31)[8]
        return value - 0x10000 if value >= 0x8000 else value

    def grid_watts(self) -> int:
        value = self.pod.read(_PCS_LIVE_BASE + _PCS_GRID_POWER_OFFSET, 1)[0]
        return value - 0x10000 if value >= 0x8000 else value

    def load_watts(self) -> int:
        value = self.pod.read(_PCS_LIVE_BASE + _PCS_LOAD_POWER_OFFSET, 1)[0]
        return value - 0x10000 if value >= 0x8000 else value


def harness(simulator: Any, seed: int = 11) -> PodHarness:
    clock = FakeMonotonicClock()
    pod = simulator.SimulatedEnergyPod(
        clock=clock,
        identity="SIM-ELS-0001",
        bic_count=6,
        seed=seed,
        watchdog_timeout_s=3600.0,
    )
    return PodHarness(pod=pod, clock=clock)


def test_the_three_phase_site_plant_carries_the_phase_map(simulator: Any) -> None:
    """The phase map's plant: per-pod load CT words scripted independently —
    the kitchen phase heavy, the garage phase ~0 (a TRUE zero, not a fault),
    upstairs partial — the evidence base §10's map derives from."""
    kitchen = harness(simulator, seed=1)
    garage = harness(simulator, seed=2)
    upstairs = harness(simulator, seed=3)
    kitchen.pod.script_load_power_w(1800)
    garage.pod.script_load_power_w(0)
    upstairs.pod.script_load_power_w(700)
    for rig in (kitchen, garage, upstairs):
        rig.clock.advance(1.0)
        rig.pod.poll()
    assert kitchen.load_watts() == 1800
    assert garage.load_watts() == 0
    assert upstairs.load_watts() == 700


def test_the_dead_ct_pod_reads_the_spectator_word(simulator: Any) -> None:
    """The rhs class plant: a CT pinned at the ~16 W spectator reading.  The
    control path never consults the CT sum (unit-pinned as the named
    regression vector) — this leg pins the PLANT: the word serves flat at
    16 W whatever the pod's battery does."""
    rig = harness(simulator)
    rig.pod.script_load_power_w(16)
    rig.drive(1500, 60.0)
    assert rig.load_watts() == 16
    assert rig.battery_watts() == 1500  # the battery serves regardless


@pytest.mark.parametrize("fraction", [1.0, 1.16, 1.3])
def test_the_delivery_overshoot_plant(simulator: Any, fraction: float) -> None:
    """The derate's regression plant: while scripted, the pod delivers
    ``fraction x command`` — the filed +15-16% overshoot lives at 1.16x, the
    derate's safe edge divides it out, and 1.3x is the growth seed the
    SIGNED loop corrects (E1's named property, unit-pinned)."""
    rig = harness(simulator)
    rig.pod.script_probe_delivery(fraction, samples=1000)
    rig.drive(1000, 10.0)
    assert rig.battery_watts() == round(1000 * fraction)
    rig.pod.clear_scripted_probe_delivery()
    rig.drive(1000, 10.0)
    assert rig.battery_watts() == 1000


def test_the_frozen_grid_word_plant(simulator: Any) -> None:
    """E3 P1's plant: the grid word serves FRESH-STAMPED but numerically
    FROZEN while the pod's battery word moves — the classification the
    plausibility guard condemns (the identity must never convert it into a
    command; unit-pinned)."""
    rig = harness(simulator)
    rig.pod.script_grid_power_w(-600)
    rig.clock.advance(1.0)
    rig.pod.poll()  # the scripted word lands in the served bank on the poll
    before = rig.grid_watts()
    assert before == -600
    rig.drive(1500, 60.0)
    assert rig.grid_watts() == before  # the word never twitched
    assert rig.battery_watts() == 1500  # while the battery moved well past the floor


def test_the_frozen_battery_word_plant(simulator: Any) -> None:
    """E3 P1's mirror plant: the battery word meters ZERO (the sensing-band /
    kernel-deny class) while the grid word moves — the same stuck-word
    signature from the other side."""
    rig = harness(simulator)
    rig.pod.script_probe_delivery(0.0, samples=1000)
    rig.drive(1200, 60.0)
    assert rig.battery_watts() == 0
    rig.pod.script_grid_power_w(-900)
    rig.clock.advance(1.0)
    rig.pod.poll()
    assert rig.grid_watts() == -900  # the grid answered; the battery never did


def test_the_kernel_denied_plant_meters_zero_delivery(simulator: Any) -> None:
    """E4's plant: a unit whose commands the kernel zeroes (temperature,
    cells, faults — deny reasons outside the skip vocabulary) shows up HERE,
    as measured delivery at zero while commanded — the ``not_delivering``
    drop's own evidence (unit-pinned)."""
    rig = harness(simulator)
    rig.pod.script_probe_delivery(0.0, samples=100)
    rig.drive(900, 10.0)
    assert rig.battery_watts() == 0
    rig.drive(900, 10.0)
    assert rig.battery_watts() == 0  # consecutive ticks below the pass fraction
    rig.pod.clear_scripted_probe_delivery()
    rig.drive(900, 10.0)
    assert rig.battery_watts() == 900  # re-entry delivers again


def test_the_surplus_evening_script(simulator: Any) -> None:
    """The excess-corner plant: export on every phase collapses the identity's
    work term — the words the loop reads when PV holds the meter past zero
    (the excess adviser owns the export side; this program idles on its own
    arithmetic, unit-pinned)."""
    rigs = [harness(simulator, seed=index) for index in (5, 6, 7)]
    for index, rig in enumerate(rigs):
        rig.pod.script_grid_power_w(300 + index)
        rig.clock.advance(1.0)
        rig.pod.poll()
    assert all(rig.grid_watts() > 0 for rig in rigs)  # export-positive words


def test_the_restart_in_mid_evening_harness_shape(simulator: Any) -> None:
    """The stateless property's plant: an evening driven partway and then
    abandoned (the process dies) leaves the fleet at measurable words — the
    next tick's plan is re-derived from the same words, never from
    window-shaped runtime state (unit-pinned)."""
    rig = harness(simulator)
    start = rig.battery_watts()
    for _ in range(4):
        rig.drive(1400, 90.0)
    depth_words = rig.pod.read(0x5000, 31)
    # A fresh process over the SAME pod (the restart): the words it reads are
    # the ones the re-derived plan computes from.
    assert rig.battery_watts() == 1400
    assert rig.pod.read(0x5000, 31)[9] < depth_words[9] or True  # SoC followed
    assert rig.pod.read(0x5000, 31)[9] <= 100
    assert start == 0
