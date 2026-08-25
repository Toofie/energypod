"""T-CAL-SIMULATOR — the calibration program's scripted device legs.

DESIGN_CALIBRATION_CYCLING §11/T-CAL-SIMULATOR: the deterministic pods the
program's own stories run against — a PINNED-SoC pod that traverses and
re-anchors (the happy measurement), the FROZEN-SoC word (the
measurement-first failure), the LATE-STEP twin (C4), the CCL-taper script
(the top anchor lands or refuses), spread-before/after scripting, the
SENSING-BAND meter (a commanded 200 W reads nonzero, a 100 W reads zero —
the min-rate justification leg), and the restart-in-mid-traverse harness
shape the C2 reconstruction composes over (the reconstruction itself is
unit-pinned in test_calibration.py).

The pod is exposed directly as the scenario handle (the house simulator
pattern): the device model advances only through the injected clock, so a
traverse's hours are a handful of large clock advances.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.adapters.modbus import protocol_codec


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

    def bms(self, index: int) -> int:
        return self.pod.read(0x5000, 31)[index]

    @property
    def soc_pct(self) -> int:
        return self.bms(9)

    @property
    def charge_limit_w(self) -> int:
        return self.bms(13)

    def cell_spread_mv(self) -> int:
        cells = self.pod.read(0x5000, 31)
        # The extrema words carry the highest/lowest cell voltages.
        return cells[20] - cells[23]


def harness(simulator: Any, seed: int = 7) -> PodHarness:
    clock = FakeMonotonicClock()
    pod = simulator.SimulatedEnergyPod(
        clock=clock,
        identity="SIM-CAL-0001",
        bic_count=6,
        seed=seed,
        watchdog_timeout_s=3600.0,
    )
    return PodHarness(pod=pod, clock=clock)


def test_pinned_soc_pod_traverses_to_the_floor(simulator: Any) -> None:
    """The happy measurement's plant: a healthy pod's SoC word follows the
    pack down across the traverse until the floor member stops it — the
    delivered discharge energy is measurable and monotone."""
    rig = harness(simulator)
    start = rig.soc_pct
    # 800 W for ~4,000 s moves 0.89 kWh -> ~1.8 pct on the 50 kWh model.
    for _ in range(3):
        rig.drive(800, 4_000.0)
    assert rig.soc_pct < start
    assert rig.charge_limit_w > 0  # not full: no taper collapse
    # The full anchor walk: drive until the model's own floor (5.5) holds.
    while rig.soc_pct > 6:
        rig.drive(800, 20_000.0)
    assert rig.soc_pct <= 6


def test_frozen_soc_word_never_moves_while_watts_flow(simulator: Any) -> None:
    """The measurement-first failure's plant: the SoC word never moves while
    watts flow — exactly the mid pathology the energy bound exists for."""
    rig = harness(simulator)
    rig.pod.script_soc_word("frozen")
    start = rig.soc_pct
    for _ in range(5):
        rig.drive(800, 4_000.0)
    assert rig.soc_pct == start
    rig.pod.clear_scripted_soc_word()
    rig.drive(800, 4_000.0)
    assert rig.soc_pct < start  # the word follows the pack again


def test_late_step_twin_holds_then_steps_at_the_bound(simulator: Any) -> None:
    """C4's companion plant: the word holds frozen until the cumulative
    discharge reaches the scripted Wh, then STEPS once to the target — the
    anchor's two-witness proof on a pod whose word lags its pack."""
    rig = harness(simulator)
    rig.pod.script_soc_word("late_step", at_wh=500.0, to_pct=9.0)
    before = rig.soc_pct
    rig.drive(800, 1_000.0)  # ~222 Wh: still frozen
    assert rig.soc_pct == before
    rig.drive(800, 2_000.0)  # crosses 500 Wh: the step lands
    assert rig.soc_pct == 9
    with pytest.raises(ValueError):
        rig.pod.script_soc_word("bogus")  # the vocabulary is closed


def test_ccl_taper_script_collapses_at_the_configured_soc(simulator: Any) -> None:
    """The top-anchor script: at or above the scripted SoC the served
    dynamic charge limit collapses to 0 W — the true-full signature the
    §5.2 observation keys on; below it the limit stands open."""
    rig = harness(simulator)
    soc = rig.soc_pct
    rig.pod.script_ccl_taper(float(soc))
    rig.pod.poll()
    assert rig.charge_limit_w == 0
    rig.pod.script_ccl_taper(None)
    rig.pod.poll()
    assert rig.charge_limit_w > 0


def test_sensing_band_meter_200_nonzero_100_zero(simulator: Any) -> None:
    """The min-rate justification leg: Victron's per-module ~50 W sensing
    threshold — below 150 W (three modules) a commanded current meters as
    0 W, so the traverse's commanded floor must clear the band."""
    rig = harness(simulator)
    rig.pod.script_sensing_floor_w(150)
    rig.drive(100, 10.0)
    words = rig.pod.read(0x5000, 31)
    assert words[8] == 0  # measured battery power reads zero
    rig.drive(200, 10.0)
    words = rig.pod.read(0x5000, 31)
    assert words[8] == 200  # the sensing-clear commanded rate meters nonzero


def test_cell_spread_scripting_pins_the_measurement_figures(simulator: Any) -> None:
    """Spread before/after scripting: the cell image's extrema sit exactly
    the scripted spread apart — the measurement record's own figures."""
    rig = harness(simulator)
    rig.pod.script_cell_spread_mv(40)
    rig.clock.advance(6.0)  # the cell image refreshes on its own cadence
    rig.pod.poll()
    assert rig.cell_spread_mv() == 40
    rig.pod.script_cell_spread_mv(200)
    rig.clock.advance(6.0)
    rig.pod.poll()
    assert rig.cell_spread_mv() == 200
    rig.pod.script_cell_spread_mv(None)
    with pytest.raises(ValueError):
        rig.pod.script_cell_spread_mv(900)


def test_restart_harness_shape_the_boot_reconstruction_composes_over(simulator: Any) -> None:
    """The C2 harness shape: a traverse driven partway and then simply
    abandoned (the process dies) leaves a pod at a measurable depth — the
    figures the boot reconstruction's ``inconclusive_interrupted`` row and
    the historian's own min-SOC read carry. The reconstruction itself is
    unit-pinned; this leg pins the PLANT it reads."""
    rig = harness(simulator)
    start = rig.soc_pct
    for _ in range(4):
        rig.drive(800, 4_000.0)
    depth = rig.soc_pct
    assert depth < start  # the interrupted night left the pod partway down
    # A fresh process over the SAME pod (the restart): the words it reads
    # are the ones the reconstruction's figures derive from.
    words = rig.pod.read(0x5000, 31)
    assert words[9] == depth
