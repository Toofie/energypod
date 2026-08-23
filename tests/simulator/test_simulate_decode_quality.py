"""Honest per-field quality in the simulate-mode decode (MUTATION-3/6).

The simulator is the reference model (live captures now cross-check it), so
its decode must not paper over sentinel-decode gaps the live wire will have:
the 2026-08-22 mutation run found the composed simulate decode reporting
BLANKET GOOD quality, so a malformed-injected dynamic-limit word served as
its complement (0x0BB8 ^ 0xFFFF = 0xF447 = 62,535 unsigned) decoded as
62,535 W of GOOD headroom — a figure the production wire decoder refuses
(signed decode, negative domain => BAD) and the safety kernel would trust in
every simulator-driven scenario.

Pinned contract (red first):

- Every quality-map field is derived from the served words with the
  PRODUCTION decoder's fail-closed semantics: a dynamic-limit word that
  decodes negative (any high-bit sentinel: 0xFFFF, 0x8000, a complemented
  limit) is BAD with the value absent — never a giant valid limit; a
  percentage word outside 0-100 is BAD with the value absent — never a
  pydantic crash that drops the whole observation.
- The clean default bank is unchanged: every field GOOD and byte-identical
  to the pre-contract values (scenario banks and golden hashes must not
  move).
- ``script_quality(field, quality)`` degrades one quality-map field for a
  scenario WITHOUT touching the served register words (the device model
  keeps serving its bytes; only the decode's trust moves), BAD/MISSING also
  dropping the value, SUSPECT/STALE keeping it; ``clear_scripted_quality``
  restores the derived judgment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from energypod.adapters.modbus import protocol_codec
from energypod.domain.observations import DataQuality
from tests.unit.test_composition import ScriptedClock, _shutdown_actors, compose

_BMS_BASE = 0x5000
_BMS_SOC_OFFSET = 9
_BMS_CHARGE_LIMIT_OFFSET = 13
_BMS_DISCHARGE_LIMIT_OFFSET = 14
_PCS_LIVE_BASE = 0x1000
_PCS_GRID_POWER_OFFSET = 17


async def _poll_one_observation(runtime: Any, unit_id: str = "mid") -> Any:
    """One explicit poll cycle; the freshest observation it delivered."""
    actor = runtime.actors[unit_id]
    await runtime.clock.sleep(0.05)
    await actor.poll_once()
    return await runtime.observations.latest(unit_id)


async def _simulate_runtime(tmp_path: Path) -> Any:
    runtime = compose(tmp_path / "quality.sqlite3", simulate=True, clock=ScriptedClock())
    assert runtime.simulators is not None
    await runtime.actors["mid"].start()
    return runtime


async def test_a_sentinel_limit_word_fails_closed_in_simulate_mode(tmp_path: Path) -> None:
    """A complemented (high-bit) limit word is BAD with the value absent.

    The simulator serves a 3000 W charge limit; malformed-register injection
    complements it to 0xF447.  Unsigned, that is 62,535 W of headroom — the
    exact artifact the blanket-GOOD decode produced.  The simulate decode
    must refuse it exactly as the production decoder does: signed decode to
    -3001 W, outside the non-negative limit domain, BAD with the value
    absent, and the observation can no longer qualify.
    """
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        clean_charge_word = pod.read(_BMS_BASE + _BMS_CHARGE_LIMIT_OFFSET, 1)[0]
        pod.inject_malformed_register(_BMS_BASE + _BMS_CHARGE_LIMIT_OFFSET)
        served = pod.read(_BMS_BASE + _BMS_CHARGE_LIMIT_OFFSET, 1)[0]
        assert served == clean_charge_word ^ 0xFFFF, "the injected word is the complement"
        assert protocol_codec.decode_signed16(served) < 0, "the sentinel decodes negative"

        latest = await _poll_one_observation(runtime)

        assert latest is not None, "a sentinel word must not cost the whole observation"
        assert latest.dynamic_charge_limit_w is None, (
            "a sentinel limit must never decode as a giant valid limit"
        )
        assert latest.quality["dynamic_charge_limit_w"] is DataQuality.BAD
        assert latest.safety_data_complete is False
    finally:
        await _shutdown_actors(runtime)


async def test_a_sentinel_discharge_limit_word_fails_closed(tmp_path: Path) -> None:
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        pod.inject_malformed_register(_BMS_BASE + _BMS_DISCHARGE_LIMIT_OFFSET)
        latest = await _poll_one_observation(runtime)
        assert latest.dynamic_discharge_limit_w is None
        assert latest.quality["dynamic_discharge_limit_w"] is DataQuality.BAD
    finally:
        await _shutdown_actors(runtime)


async def test_an_out_of_range_soc_word_fails_closed_without_losing_the_observation(
    tmp_path: Path,
) -> None:
    """A garbage percentage word is BAD with the value absent.

    The blanket decode read the raw word unsigned straight into the strict
    observation model, so a complemented SOC word crashed the whole decode
    (no observation delivered at all).  The honest decode fails the FIELD
    closed — value absent, quality BAD — and the poll still delivers every
    other field.
    """
    runtime = await _simulate_runtime(tmp_path)
    try:
        runtime.simulators["mid"].inject_malformed_register(_BMS_BASE + _BMS_SOC_OFFSET)
        latest = await _poll_one_observation(runtime)
        assert latest is not None, "the observation must survive a bad percentage word"
        assert latest.bms_soc_pct is None
        assert latest.quality["bms_soc_pct"] is DataQuality.BAD
        assert latest.pack_voltage_v is not None, "unrelated fields still decode"
        assert latest.quality["pack_voltage_v"] is DataQuality.GOOD
    finally:
        await _shutdown_actors(runtime)


async def test_the_clean_default_bank_stays_good_and_byte_identical(tmp_path: Path) -> None:
    """Defaults preserve every served bank byte: clean pods decode all GOOD.

    The honest-quality change must not move a single existing scenario: with
    no injection and no scripted quality, every quality-map field is GOOD and
    the decoded scalars are exactly the words the bank serves.
    """
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        bms = pod.read(_BMS_BASE, 31)
        latest = await _poll_one_observation(runtime)
        assert all(quality is DataQuality.GOOD for quality in latest.quality.values()), dict(
            latest.quality
        )
        assert latest.safety_data_complete is True
        assert latest.bms_soc_pct == float(protocol_codec.decode_signed16(bms[_BMS_SOC_OFFSET]))
        assert latest.dynamic_charge_limit_w == float(
            protocol_codec.decode_signed16(bms[_BMS_CHARGE_LIMIT_OFFSET])
        )
        assert latest.dynamic_discharge_limit_w == float(
            protocol_codec.decode_signed16(bms[_BMS_DISCHARGE_LIMIT_OFFSET])
        )
    finally:
        await _shutdown_actors(runtime)


async def test_scripted_quality_degrades_a_field_without_touching_the_served_words(
    tmp_path: Path,
) -> None:
    """``script_quality`` moves the decode's trust, never the device's bytes.

    BAD drops the value exactly like the fail-closed decode; the served
    register word is unchanged so a register-image pin still sees the same
    bank.
    """
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        grid_address = _PCS_LIVE_BASE + _PCS_GRID_POWER_OFFSET
        pod.script_grid_power_w(900)
        # One poll lands the scripted CT word in the served bank (the device
        # model rebuilds its words on the poll step).
        await _poll_one_observation(runtime)
        served_before = pod.read(grid_address, 1)[0]
        assert served_before == 900

        pod.script_quality("grid_power_w", DataQuality.BAD)
        assert pod.read(grid_address, 1)[0] == served_before, (
            "scripting quality never rewrites the served bank"
        )

        latest = await _poll_one_observation(runtime)
        assert latest.grid_power_w is None
        assert latest.quality["grid_power_w"] is DataQuality.BAD
        assert pod.read(grid_address, 1)[0] == served_before
    finally:
        await _shutdown_actors(runtime)


async def test_scripted_suspect_quality_keeps_the_value(tmp_path: Path) -> None:
    """SUSPECT distrusts a field without erasing it (the production
    fail-closed downgrade keeps values; the scripted override matches)."""
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        pod.script_quality("bms_soc_pct", DataQuality.SUSPECT)
        latest = await _poll_one_observation(runtime)
        assert latest.bms_soc_pct is not None
        assert latest.quality["bms_soc_pct"] is DataQuality.SUSPECT
        assert latest.safety_data_complete is False
    finally:
        await _shutdown_actors(runtime)


async def test_scripted_quality_clears_back_to_the_derived_judgment(tmp_path: Path) -> None:
    runtime = await _simulate_runtime(tmp_path)
    try:
        pod = runtime.simulators["mid"]
        pod.script_quality("dynamic_discharge_limit_w", DataQuality.MISSING)
        degraded = await _poll_one_observation(runtime)
        assert degraded.quality["dynamic_discharge_limit_w"] is DataQuality.MISSING
        assert degraded.dynamic_discharge_limit_w is None

        pod.clear_scripted_quality("dynamic_discharge_limit_w")
        recovered = await _poll_one_observation(runtime)
        assert recovered.quality["dynamic_discharge_limit_w"] is DataQuality.GOOD
        assert recovered.dynamic_discharge_limit_w is not None
    finally:
        await _shutdown_actors(runtime)


def test_script_quality_validates_its_field_and_quality() -> None:
    from energypod.simulator.pod import SimulatedEnergyPod

    pod = SimulatedEnergyPod(clock=ScriptedClock(), identity="SIM-QUALITY-1")
    with pytest.raises(ValueError):
        pod.script_quality("not_a_quality_field", DataQuality.BAD)
    with pytest.raises(ValueError):
        pod.script_quality("bms_soc_pct", "bad")  # type: ignore[arg-type]
