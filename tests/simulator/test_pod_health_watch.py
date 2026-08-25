"""The health watch's simulator legs (DESIGN_BATTERY_HEALTH_WATCH section 14).

``script_stuck`` pins the spectator signature with its ECHO CLASS PINNED
(A3): SoC >= 95, dead battery watts, dead load CT, normal mode words, and PQ
writes ACKed-but-ignored — ``matches`` serves the written objective words
while delivering nothing (the ``fail_no_response`` leg), ``not_served``
leaves them unchanged (the advisory-only leg).

``script_probe_delivery`` pins the delivered share of a latched objective
for a bounded poll budget: the fleet-bias band passes, 0.0 serves nothing,
0.3 is the degraded pod, and a short 2.0 share over a quiet rest is the
spike pattern the 80%-of-samples rule must refuse.

SAFETY: no socket, no hardware — the deterministic device model only.
"""

from __future__ import annotations

import pytest

from energypod.adapters.modbus import protocol_codec
from energypod.simulator.pod import SimulatedEnergyPod


class FakeMonotonicClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_pod(*, watchdog_timeout_s: float = 2.0) -> tuple[SimulatedEnergyPod, FakeMonotonicClock]:
    clock = FakeMonotonicClock()
    pod = SimulatedEnergyPod(
        clock=clock, identity="SIM-BHW-0001", seed=11, watchdog_timeout_s=watchdog_timeout_s
    )
    return pod, clock


def objective_frame(active_w: int) -> tuple[int, int, int]:
    return (
        1,
        protocol_codec.encode_signed16(active_w),
        protocol_codec.encode_signed16(0),
    )


def test_script_stuck_validates_its_arguments() -> None:
    pod, _clock = build_pod()
    with pytest.raises(ValueError, match="matches"):
        pod.script_stuck("maybe", 0.0)
    with pytest.raises(ValueError, match="at_s"):
        pod.script_stuck("matches", -1.0)


def test_the_stuck_signature_lands_at_the_scripted_time() -> None:
    """Pending until ``at_s``, landed at the next poll: SoC pinned at the
    top, dead load CT, normal mode words."""
    pod, clock = build_pod()
    pod.script_stuck("matches", at_s=clock.now + 5.0)
    pod.poll()
    assert pod._soc_pct < 95.0  # still the seeded state: nothing landed yet
    clock.advance(6.0)
    pod.poll()
    assert pod._soc_pct == 97.0
    assert pod._debug_mode == 0
    assert pod._scripted_load_power_w == 0


def test_the_matches_echo_class_serves_the_write_and_delivers_nothing() -> None:
    """A3's echoed-and-dead leg: the write is ACKed (no exception), the
    served objective words follow it, and delivered power stays dead."""
    pod, clock = build_pod()
    pod.script_stuck("matches", at_s=0.0)
    pod.poll()
    pod.apply_pq_frame(objective_frame(300))
    words = pod.read(0x1060 + 17, 2)
    assert protocol_codec.decode_signed16(words[0]) == 300
    pod.poll()
    clock.advance(1.0)
    pod.poll()
    assert pod._delivered_active_w() == 0
    # Nothing latched and no lease renewed: ignored means ignored.
    assert pod._lease_deadline_mono is None


def test_the_not_served_echo_class_leaves_the_words_unchanged() -> None:
    """A3's advisory-only leg: the write is ACKed, the served objective
    words stay at the pod's own (nothing), and delivery stays dead."""
    pod, _clock = build_pod()
    pod.script_stuck("not_served", at_s=0.0)
    pod.poll()
    before = pod.read(0x1060 + 17, 2)
    pod.apply_pq_frame(objective_frame(300))
    after = pod.read(0x1060 + 17, 2)
    assert before == after
    assert pod._delivered_active_w() == 0


def test_clear_scripted_stuck_restores_service() -> None:
    pod, clock = build_pod()
    pod.script_stuck("matches", at_s=0.0)
    pod.poll()
    pod.clear_scripted_stuck()
    pod.apply_pq_frame(objective_frame(300))
    clock.advance(0.5)
    assert pod._delivered_active_w() == 300


def test_probe_delivery_serves_the_share_while_the_budget_lasts() -> None:
    # A long watchdog lease: the delivery share is under test, not the
    # unrenewed-objective expiry (the controller renews every fleet cycle).
    pod, clock = build_pod(watchdog_timeout_s=30.0)
    pod.apply_pq_frame(objective_frame(300))
    pod.script_probe_delivery(0.87, samples=3)
    served = []
    for _ in range(5):
        clock.advance(1.0)
        pod.poll()
        served.append(pod._delivered_active_w())
    assert served[:3] == [261, 261, 261]
    # The budget expired: the pod is back to idle (the spike shape's rest).
    assert served[3:] == [0, 0]


def test_probe_delivery_validates_its_arguments() -> None:
    pod, _clock = build_pod()
    with pytest.raises(ValueError, match="fraction"):
        pod.script_probe_delivery(-0.1, 3)
    with pytest.raises(ValueError, match="samples"):
        pod.script_probe_delivery(1.0, -1)


def test_the_dead_share_serves_nothing() -> None:
    pod, clock = build_pod(watchdog_timeout_s=30.0)
    pod.apply_pq_frame(objective_frame(300))
    pod.script_probe_delivery(0.0, samples=4)
    clock.advance(1.0)
    pod.poll()
    assert pod._delivered_active_w() == 0


def test_the_spike_pattern_composes_from_a_short_double_share() -> None:
    """2x command for two polls, quiet for the rest: 90% at zero, 10% at 2x
    — the pattern the probe's share rule must refuse."""
    pod, clock = build_pod(watchdog_timeout_s=60.0)
    pod.apply_pq_frame(objective_frame(300))
    pod.script_probe_delivery(2.0, samples=2)
    delivered = []
    for _ in range(20):
        clock.advance(1.0)
        pod.poll()
        delivered.append(pod._delivered_active_w())
    assert delivered.count(600) == 2
    assert delivered.count(0) == 18
