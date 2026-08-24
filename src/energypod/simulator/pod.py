"""Deterministic single-unit EnergyPod device model over the evidenced IoT layout.

`SimulatedEnergyPod` serves an in-memory holding-register bank built from the
production register catalog (identity, telemetry blocks, cell blocks, BMS and
BECU status words) plus a device model driven exclusively by the injected
monotonic clock:

- applied ``0x0200`` ``[1, P, Q]`` objectives latch until the watchdog lease
  expires to idle, and every accepted write renews the lease;
- the vendor debug-mode word (``0x8100`` readback, ``0x8000`` write) is a
  device field: ``apply_debug_mode`` accepts only ``{0, 1}`` (DESIGN_POD_PARKING
  section 6), and while parked the pod ACKs objective writes but delivers no
  power, leaves the served objective words unchanged by an ignored write, and
  touches no watchdog lease -- "ignored means ignored";
- telemetry and cell sequences advance once per explicit :meth:`poll` step,
  with cell data refreshing on its own slower cadence;
- every derived register value is a deterministic function of the injected
  seed, the applied setpoint, and scripted time (ADR-0003 decision D4).

The device model reads no wall clock, opens no socket, and uses no randomness
beyond the injected seed, so identical scenario scripts produce identical
register values and sequences.
"""

from __future__ import annotations

import math
import random
import zlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from energypod.adapters.modbus import faults, protocol_codec, register_layout
from energypod.domain.observations import DataQuality, Observation

# Served holding-register block bases (PROTOCOL_EVIDENCE section 5).
_PQ_HEADER_WORD = 1  # first word of the evidenced [1, P, Q] objective frame
_SYSTEM_BASE = 0x0100
_PCS_LIVE_BASE = 0x1000
_PCS_DETAIL_BASE = 0x1060
# Advisory per-pod CT words inside the PCS live block, PROTOCOL_EVIDENCE 4c
# (SysControl.cs:500/:503): external grid power at +17 and load power at +20,
# int16 unscaled watts, negative grid = import / positive = export.  The
# simulator serves scripted scenario values (default 0 W = an idle CT) so the
# excess-solar export bound is exercisable end to end without hardware.
_PCS_GRID_POWER_OFFSET = 17
_PCS_LOAD_POWER_OFFSET = 20
_DCDC_LIVE_BASE = 0x2000
_DCDC_DETAIL_BASE = 0x2060
_TOTALS_BASE = 0x4101
_BMS_BASE = 0x5000
_CELL_VOLTAGE_BASE = 0x5200
_CELL_TEMPERATURE_BASE = 0x523C
_CELL_BALANCE_BASE = 0x524E
_DEBUG_MODE_BASE = 0x8100
_PARAMETERS_BASE = 0x8102
_RTU_ID_BASE = 0x8106
_NETWORK_STATUS_BASE = 0x8139

# Fault/warning status blocks served by the IoT plan.
_FAULT_BLOCK_BASES: tuple[tuple[faults.FaultBlock, int], ...] = (
    (faults.FaultBlock.IOT_PCS, 0x1040),
    (faults.FaultBlock.IOT_DCDC, 0x2040),
    (faults.FaultBlock.IOT_BMS, 0x5040),
)

# MUTATION-3/6 (scriptable quality): the scenario hook may distrust exactly
# the observation's own quality-map fields -- the ten safety-critical fields
# plus the two advisory CT words -- and nothing else.
_SCRIPTABLE_QUALITY_FIELDS: frozenset[str] = frozenset(
    Observation.QUALITY_FIELDS | Observation.ADVISORY_QUALITY_FIELDS
)

_IOT_FAULT_PREFIXES: frozenset[str] = frozenset(
    prefix
    for block, _base in _FAULT_BLOCK_BASES
    for prefix in (
        *faults.FAULT_BLOCK_LAYOUTS[block].warning_offsets,
        *faults.FAULT_BLOCK_LAYOUTS[block].fault_offsets,
    )
)

# Commissioning-plausible static device limits.  The exact firmware limits are
# a commissioning item, so the simulator serves generous fixed headroom: safety
# decisions must come from the kernel and policy, not synthetic scarcity.
_CHARGE_CURRENT_LIMIT_COUNTS = 1500  # x0.1 A -> 150.0 A
_DISCHARGE_CURRENT_LIMIT_COUNTS = 1500  # x0.1 A -> 150.0 A
_CHARGE_POWER_LIMIT_W = 3000
_DISCHARGE_POWER_LIMIT_W = 3000
_REACTIVE_LIMIT_VAR = 1000
_APPARENT_LIMIT_W = 3000

# Seeded-dynamics bounds.  Energy counters are 0.1 kWh counts; the coherence
# contract pins them below one million counts, and cell values must stay
# inside the commissioning voltage window at all times.
_CAPACITY_WH = 50_000.0
_ENERGY_COUNT_CEILING = 1_000_000
_ENERGY_BASE_CEILING = 200_000
_ENERGY_BASE_FLOOR = 10_000
_WATT_SECONDS_PER_COUNT = 360_000.0
_CELL_MV_FLOOR = 2800
_CELL_MV_CEILING = 3650
_CELL_DRIFT_PERIOD_S = 21
_CELL_DRIFT_AMPLITUDE_MV = 10
_TEMPERATURE_DRIFT_PERIOD_S = 5

_SOC_FLOOR_PCT = 5.5
_SOC_CEILING_PCT = 94.5


class SimulatedEnergyPod:
    """One simulated unit: an IoT-layout register bank plus a device model.

    All timing comes from the injected ``clock``; the model advances only in
    :meth:`poll` and :meth:`apply_pq_frame`, never on reads.
    """

    def __init__(
        self,
        *,
        clock: Any,
        identity: str = "SIM-POD-0000",
        bic_count: int = 6,
        seed: int = 0,
        watchdog_timeout_s: float = 2.0,
        cell_poll_interval_s: float = 5.0,
    ) -> None:
        if not callable(getattr(clock, "monotonic", None)):
            raise ValueError("clock must provide monotonic()")
        if type(identity) is not str or not identity.strip():
            raise ValueError("identity must be a non-empty string")
        if type(bic_count) is not int or not 1 <= bic_count <= 6:
            raise ValueError("bic_count must support the non-overlapping IoT cell map (1..6)")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if not _positive_finite(watchdog_timeout_s):
            raise ValueError("watchdog_timeout_s must be a positive finite number")
        if not _positive_finite(cell_poll_interval_s):
            raise ValueError("cell_poll_interval_s must be a positive finite number")

        self._clock = clock
        self._identity = identity
        self._bic_count = bic_count
        self._watchdog_timeout_s = float(watchdog_timeout_s)
        self._cell_poll_interval_s = float(cell_poll_interval_s)

        # Seeded dynamics: one generator, consumed in a fixed construction
        # order, so a seed fully determines the initial device image.  The
        # values model telemetry, not secrets; cryptographic quality is not a
        # simulator requirement.
        rng = random.Random(seed)  # noqa: S311
        self._soc_pct = rng.uniform(60.0, 90.0)
        self._soh_pct = rng.randrange(98, 101)
        self._cell_base_millivolts = [3200 + rng.randrange(0, 201) for _ in range(bic_count * 10)]
        self._cell_base_temperature_words = [64 + rng.randrange(0, 7) for _ in range(bic_count * 3)]
        self._grid_buy_counts_base = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._grid_sell_counts_base = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._load_counts_base = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._pv_counts = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._charge_base_counts = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._discharge_base_counts = rng.randrange(_ENERGY_BASE_FLOOR, _ENERGY_BASE_CEILING)
        self._charge_watt_seconds = 0.0
        self._discharge_watt_seconds = 0.0
        # Energy scorecard (DESIGN_ENERGY_SCORECARD section 9 family 8): the
        # GRID and LOAD counter pairs accumulate from the scripted CT words
        # exactly the way charge/discharge accumulate from the applied
        # objective -- watt-seconds over the injected clock, deterministic.
        self._grid_buy_watt_seconds = 0.0
        self._grid_sell_watt_seconds = 0.0
        self._load_watt_seconds = 0.0

        # Identity through evidenced registers only: the RTU ID is one
        # low-word-first uint32, derived deterministically from the identity.
        self._rtu_id = zlib.crc32(identity.encode("utf-8"))

        now = float(self._clock.monotonic())
        self._origin_mono = now
        self._last_poll_mono = now
        self._telemetry_sequence = 0
        self._telemetry_captured_at_mono = now
        self._cell_sequence = 0
        self._cell_captured_at_mono = now
        self._cell_millivolts = list(self._cell_base_millivolts)
        self._cell_temperature_words = list(self._cell_base_temperature_words)

        self._applied_active_w = 0
        self._applied_reactive_var = 0
        self._lease_deadline_mono: float | None = None
        # The vendor debug-mode word (DESIGN_POD_PARKING section 6): 0 Normal,
        # 1 Standby, nothing else ever.  ``_scripted_debug_mode`` is the
        # scenario hook's pending (value, at_s) transition, landed by the next
        # poll that reaches ``at_s``; ``_parked_readback_words`` is the
        # flip-hook echo of the last ignored write (absent by default -- the
        # pinned-conservative readback leaves the served words unchanged).
        self._debug_mode = 0
        self._scripted_debug_mode: tuple[int, float] | None = None
        self._parked_readback_shows_write = False
        self._parked_readback_words: tuple[int, int] | None = None
        # Night-writer scenario hook (API_CONTRACTS "Night-writer detector"):
        # a FOREIGN served objective -- another writer's words on the detail
        # block, never latched through our own apply path.
        self._scripted_objective: tuple[int, int, bool] | None = None
        # Scripted per-pod CT scenario values (PROTOCOL_EVIDENCE 4c words):
        # the deterministic export scenario the excess-solar bound reads.
        self._scripted_grid_power_w = 0
        self._scripted_load_power_w = 0
        self._fault_words: dict[str, int] = {}
        self._malformed_addresses: set[int] = set()
        # Scripted decode-trust overrides (MUTATION-3/6): field -> quality.
        # The served register bank is untouched by these; see script_quality.
        self._scripted_quality: dict[str, DataQuality] = {}
        self._link_up = True
        self._connection_epoch = 1

        catalog = register_layout.RegisterCatalog()
        blocks = (*catalog.iot_reads(bic_count=bic_count), *catalog.common_reads)
        self._blocks: dict[int, list[int]] = {block.address: [0] * block.count for block in blocks}
        self._rebuild()

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def bic_count(self) -> int:
        return self._bic_count

    @property
    def telemetry_sequence(self) -> int:
        return self._telemetry_sequence

    @property
    def telemetry_captured_at_mono(self) -> float:
        return self._telemetry_captured_at_mono

    @property
    def cell_sequence(self) -> int:
        return self._cell_sequence

    @property
    def cell_captured_at_mono(self) -> float:
        return self._cell_captured_at_mono

    @property
    def link_up(self) -> bool:
        return self._link_up

    @property
    def connection_epoch(self) -> int:
        """Bumped by every simulated link restore; observations carry it."""
        return self._connection_epoch

    def poll(self) -> None:
        """Advance the device model exactly one telemetry sample.

        Time moves only through the injected clock; an explicit poll is the
        single device-model step (ADR-0003 D4).
        """
        now = float(self._clock.monotonic())
        if now < self._last_poll_mono:
            raise ValueError("injected monotonic time moved backwards")
        self._land_scripted_debug_mode(now)
        self._advance_device_state(now)
        self._telemetry_sequence += 1
        self._telemetry_captured_at_mono = now
        if now - self._cell_captured_at_mono >= self._cell_poll_interval_s:
            self._refresh_cells(now)
        self._rebuild()

    def apply_pq_frame(self, frame: tuple[int, ...]) -> None:
        """Latch one gated ``[1, P, Q]`` write and renew the watchdog lease.

        The transport enforces the write gate, but the device model refuses a
        malformed frame independently: no caller can half-latch an objective.
        Shape, header, and both signed-16 payload words are validated and
        decoded into locals before any instance state changes, so a frame that
        fails on its last word leaves the latched objective, the watchdog
        lease, and every served register bit-for-bit unchanged.

        While parked (DESIGN_POD_PARKING section 6, live-proven ACK-then-
        ignore) a well-formed frame is still ACKed -- it returns normally --
        but nothing latches: delivered power stays 0 through the rebuild, the
        served objective words are UNCHANGED by the ignored write (unless the
        ``script_parked_readback_shows_write`` flip hook is staging the other
        fork side), and the watchdog lease is untouched.  Ignored means
        ignored: mode changes what the PCS serves, never the wire protocol.
        """
        if len(frame) != 3 or type(frame[0]) is not int or frame[0] != _PQ_HEADER_WORD:
            raise ValueError("only the evidenced three-register PQ objective is applicable")
        active_w = protocol_codec.decode_signed16(frame[1])
        reactive_var = protocol_codec.decode_signed16(frame[2])
        if self._debug_mode != 0:
            # The ACK: the write is accepted on the wire and changes nothing.
            if self._parked_readback_shows_write:
                self._parked_readback_words = (
                    protocol_codec.encode_signed16(active_w),
                    protocol_codec.encode_signed16(reactive_var),
                )
                self._rebuild()
            return
        lease_deadline_mono = float(self._clock.monotonic()) + self._watchdog_timeout_s
        self._applied_active_w = active_w
        self._applied_reactive_var = reactive_var
        self._lease_deadline_mono = lease_deadline_mono
        self._rebuild()

    def apply_debug_mode(self, value: int) -> None:
        """Apply one vendor debug-mode write: 0 (Normal) or 1 (Standby).

        The transport-layer analog's refusal, at the device boundary: the
        whitelist is ``{0, 1}`` and nothing else, ever (DESIGN_POD_PARKING
        section 0 -- vendor values 2-6 are permanently unexposed).  A direct
        application supersedes any pending scheduled transition and retires
        the parked-echo words: the device word is what was just written.
        """
        self._debug_mode = _validated_debug_mode(value)
        self._scripted_debug_mode = None
        self._parked_readback_words = None
        self._rebuild()

    def script_debug_mode(self, value: int, at_s: float) -> None:
        """Scenario hook: schedule the debug-mode word to change at ``at_s``.

        The ``script_objective`` style, scheduled: the (validated) transition
        is pending until the injected clock reaches ``at_s`` and lands at the
        next :meth:`poll` -- the device model advances only through explicit
        steps -- so a scenario parks (or resumes) mid-flight without
        interleaving a direct call between the drive's steps.  A later call
        replaces the pending transition.
        """
        mode = _validated_debug_mode(value)
        if (
            isinstance(at_s, bool)
            or not isinstance(at_s, int | float)
            or not math.isfinite(float(at_s))
            or float(at_s) < 0.0
        ):
            raise ValueError("at_s must be a non-negative finite scripted time")
        self._scripted_debug_mode = (mode, float(at_s))

    def script_parked_readback_shows_write(self, enabled: bool = True) -> None:
        """Scenario hook: stage the OTHER fork side of the parked readback.

        The pinned-conservative default (DESIGN_POD_PARKING section 6) leaves
        the served objective words UNCHANGED by an ignored write, because no
        live evidence has shown what a parked pod serves at 0x1060+17/+18
        after ACKing.  This hook flips exactly that readback half -- the
        ignored write's pair is served -- while the delivery half (power stays
        0) and the lease half (nothing latches, nothing renews) hold on both
        sides.  Re-stage with ``enabled=False`` to return to the conservative
        readback; the words fall back to the pod's own served pair.
        """
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._parked_readback_shows_write = enabled
        if not enabled:
            self._parked_readback_words = None
        self._rebuild()

    def _land_scripted_debug_mode(self, now: float) -> None:
        scheduled = self._scripted_debug_mode
        if scheduled is not None and now >= scheduled[1]:
            self._debug_mode = scheduled[0]
            self._scripted_debug_mode = None
            self._parked_readback_words = None

    def read(self, address: int, count: int) -> tuple[int, ...]:
        """Serve one register window; reads never advance device state."""
        window = self._served_window(address, count)
        return tuple(
            # Malformed-register injection corrupts exactly the targeted word;
            # complementing guarantees the served word differs from the clean
            # image whatever the underlying value is.
            word ^ 0xFFFF if address + index in self._malformed_addresses else word
            for index, word in enumerate(window)
        )

    def inject_fault(self, prefix: str, bit: int) -> None:
        """Raise one bit of one served IoT fault/warning word."""
        self._require_fault_word(prefix, bit)
        self._fault_words[prefix] = self._fault_words.get(prefix, 0) | (1 << bit)
        self._rebuild()

    def clear_fault(self, prefix: str, bit: int) -> None:
        """Clear one bit of one served IoT fault/warning word."""
        self._require_fault_word(prefix, bit)
        self._fault_words[prefix] = self._fault_words.get(prefix, 0) & ~(1 << bit)
        self._rebuild()

    def inject_malformed_register(self, address: int) -> None:
        """Serve a corrupted word at exactly one register address."""
        self._require_served_address(address)
        self._malformed_addresses.add(address)

    def clear_malformed_register(self, address: int) -> None:
        """Stop corrupting one register address."""
        self._require_served_address(address)
        self._malformed_addresses.discard(address)

    def drop_link(self) -> None:
        """Scenario hook: the device link goes down until restored."""
        self._link_up = False

    def restore_link(self) -> None:
        """Scenario hook: bring the link back as a new connection epoch."""
        if self._link_up:
            return
        self._link_up = True
        self._connection_epoch += 1

    def script_grid_power_w(self, watts: int) -> None:
        """Scenario hook: script the per-pod CT external-grid power word.

        The value is served verbatim at PCS ``0x1000+17`` (int16 unscaled
        watts; negative = import, positive = export — the live-pinned sign,
        PROTOCOL_EVIDENCE 4b/4c) so the excess-solar export bound can be
        driven end to end against a deterministic fleet export scenario.
        """
        self._scripted_grid_power_w = self._ct_word(watts, "grid power")

    def script_objective(
        self, active_w: int, reactive_var: int, *, remote_mode: bool = True
    ) -> None:
        """Scenario hook: place a FOREIGN served PQ objective on the wire.

        API_CONTRACTS "Night-writer detector": the scenario drives another
        writer's words onto the served detail block (``0x1060+17/+18``, int16
        unscaled) WITHOUT latching anything through our own apply path — the
        pod's applied objective, its watchdog lease, and every derived word
        stay untouched, exactly like an external application writing 0x0200
        behind this controller's back.  While scripted, the PCS live block's
        run-mode word reads 1 ("Remote PQ Power", the vendor's written-objective
        state, GlobalFun.cs:178-188) by default — the discriminator the
        detector's pattern tier consumes; ``remote_mode=False`` serves 0
        ("Matching Load"), the pod's own CT-following autonomy state.  The
        words persist until cleared — a scheduled nightly writer refreshes
        continuously, and the scenario decides when it stands down.
        """
        if type(remote_mode) is not bool:
            raise ValueError("remote_mode must be a boolean")
        self._scripted_objective = (
            self._ct_word(active_w, "active objective"),
            self._ct_word(reactive_var, "reactive objective"),
            remote_mode,
        )
        self._rebuild()

    def clear_scripted_objective(self) -> None:
        """Scenario hook: the foreign writer stands down; the served detail
        words return to the pod's own applied objective."""
        self._scripted_objective = None
        self._rebuild()

    def script_load_power_w(self, watts: int) -> None:
        """Scenario hook: script the per-pod CT load power word (+20)."""
        self._scripted_load_power_w = self._ct_word(watts, "load power")

    def script_quality(self, field: str, quality: DataQuality) -> None:
        """Scenario hook: script one quality-map field's decode judgment.

        MUTATION-3/6 (blanket-GOOD masking): the device model keeps serving
        its register bytes verbatim -- a register-image pin still sees the
        same bank -- and only the composed decode's TRUST moves.  BAD/MISSING
        also withdraw the field's decoded value (a distrusted figure is never
        served as if trusted); SUSPECT/STALE keep the value, mirroring the
        production fail-closed downgrade.  Use this to exercise fail-closed
        consumers (the safety kernel's quality gate, the excess adviser's
        export bound) against degraded telemetry the served words alone
        cannot express.
        """
        if type(field) is not str or field not in _SCRIPTABLE_QUALITY_FIELDS:
            raise ValueError("field must name a decoded quality-map field")
        if type(quality) is not DataQuality:
            raise ValueError("quality must be a DataQuality member")
        self._scripted_quality[field] = quality

    def clear_scripted_quality(self, field: str) -> None:
        """Restore the derived judgment for one scripted quality field."""
        if type(field) is not str or field not in _SCRIPTABLE_QUALITY_FIELDS:
            raise ValueError("field must name a decoded quality-map field")
        self._scripted_quality.pop(field, None)

    def scripted_quality(self) -> Mapping[str, DataQuality]:
        """The scripted decode-trust overrides, read-only."""
        return MappingProxyType(dict(self._scripted_quality))

    @staticmethod
    def _ct_word(watts: int, label: str) -> int:
        if isinstance(watts, bool) or type(watts) is not int:
            raise ValueError(f"scripted {label} must be an integer watt value")
        if not -0x8000 <= watts <= 0x7FFF:
            raise ValueError(f"scripted {label} must fit the int16 CT word")
        return watts

    def _advance_device_state(self, now: float) -> None:
        previous = self._last_poll_mono
        deadline = self._lease_deadline_mono
        delivered_w = self._delivered_active_w()
        if deadline is not None and now >= deadline:
            # The load applied only until the lease expired; split the interval
            # so energy and SOC follow the actual applied-power history.
            live_seconds = max(0.0, min(now, deadline) - previous)
            self._accumulate(delivered_w, live_seconds)
            self._applied_active_w = 0
            self._applied_reactive_var = 0
            self._lease_deadline_mono = None
            self._accumulate(0, now - previous - live_seconds)
        else:
            self._accumulate(delivered_w, now - previous)
        # The CT accumulators follow the SCRIPTED site conditions over the
        # whole interval, independent of any latched objective.
        self._accumulate_ct(now - previous)
        self._last_poll_mono = now

    def _delivered_active_w(self) -> int:
        """The power the PCS actually delivers: the applied objective, or 0
        while parked -- a parked PCS control path moves no power regardless of
        what any writer keeps writing (the live 2026-08-24 standby cycle)."""
        return 0 if self._debug_mode != 0 else self._applied_active_w

    def _accumulate_ct(self, seconds: float) -> None:
        """Accumulate the scripted per-pod CT words into the grid/load pairs.

        The sign contract is the live-proven one (negative grid = import,
        positive = export): imports accrue the vendor's FIRST grid pair,
        exports the SECOND, and load accrues only positive load power.  This
        is the scorecard's reference model -- the pod's own counters carry
        the site conditions whether or not any controller is watching.
        """
        if seconds <= 0.0:
            return
        grid_w = self._scripted_grid_power_w
        if grid_w < 0:
            self._grid_buy_watt_seconds += -grid_w * seconds
        elif grid_w > 0:
            self._grid_sell_watt_seconds += grid_w * seconds
        load_w = self._scripted_load_power_w
        if load_w > 0:
            self._load_watt_seconds += load_w * seconds

    def _accumulate(self, power_w: int, seconds: float) -> None:
        if seconds <= 0.0:
            return
        if power_w < 0:
            # Negative P is charging on the evidenced wire convention.
            self._charge_watt_seconds += -power_w * seconds
        else:
            self._discharge_watt_seconds += power_w * seconds
        # Charging raises SOC, discharging lowers it.
        drift_pct = -power_w * seconds / (36.0 * _CAPACITY_WH)
        self._soc_pct = min(_SOC_CEILING_PCT, max(_SOC_FLOOR_PCT, self._soc_pct + drift_pct))

    def _refresh_cells(self, now: float) -> None:
        """Refresh the cell image on its own slower cadence (frozen between)."""
        elapsed = int(now - self._origin_mono)
        drift_mv = elapsed % _CELL_DRIFT_PERIOD_S - _CELL_DRIFT_AMPLITUDE_MV
        drift_counts = elapsed % _TEMPERATURE_DRIFT_PERIOD_S
        self._cell_millivolts = [
            min(_CELL_MV_CEILING, max(_CELL_MV_FLOOR, base + drift_mv))
            for base in self._cell_base_millivolts
        ]
        self._cell_temperature_words = [
            max(0, base + drift_counts) for base in self._cell_base_temperature_words
        ]
        self._cell_sequence += 1
        self._cell_captured_at_mono = now

    def _rebuild(self) -> None:
        """Recompute every served word from the current device state."""
        pack_voltage_counts = self._pack_voltage_counts()
        applied_active_word = protocol_codec.encode_signed16(self._applied_active_w)
        applied_reactive_word = protocol_codec.encode_signed16(self._applied_reactive_var)
        # Measured/delivered words carry the DELIVERED power (0 while parked),
        # never the latched objective a parked PCS is ignoring.
        measured_word = protocol_codec.encode_signed16(self._delivered_active_w())
        pack_current_word = protocol_codec.encode_signed16(
            self._pack_current_counts(pack_voltage_counts, self._delivered_active_w())
        )
        self._rebuild_system_block(pack_voltage_counts, pack_current_word, measured_word)
        self._rebuild_pcs_live_block(pack_voltage_counts, measured_word)
        self._rebuild_fault_blocks()
        self._rebuild_pcs_detail_block(applied_active_word, applied_reactive_word)
        self._rebuild_dcdc_live_block(pack_voltage_counts, measured_word)
        self._rebuild_dcdc_detail_block()
        self._rebuild_totals_block()
        self._rebuild_bms_block(pack_voltage_counts, pack_current_word, measured_word)
        self._rebuild_cell_blocks()
        self._rebuild_identity_blocks()

    def _rebuild_system_block(
        self, pack_voltage_counts: int, pack_current_word: int, measured_word: int
    ) -> None:
        words = [0] * 61
        words[0] = 3  # system status: on grid
        words[1] = 1  # control mode: remote
        words[2] = 6  # work mode: remote dispatch
        words[16] = 3  # battery status: running
        words[17] = int(self._soc_pct)
        words[18] = pack_voltage_counts  # battery voltage x0.1 V
        words[19] = pack_current_word  # battery current x0.1 A
        words[20] = measured_word  # battery power, int16 W
        words[22] = self._soh_pct
        self._blocks[_SYSTEM_BASE] = words

    def _rebuild_pcs_live_block(self, pack_voltage_counts: int, measured_word: int) -> None:
        words = [0] * 21
        words[0] = 0x0101  # packed PCS software version
        words[1] = 3  # PCS status: on grid
        # Run mode: 1 "Remote PQ Power" while ANY writer's objective holds the
        # lease -- ours, or the night-writer scenario's foreign words when the
        # scenario says the PCS reports the written-objective state.
        scripted_remote = bool(self._scripted_objective and self._scripted_objective[2])
        words[2] = 1 if self._lease_deadline_mono is not None or scripted_remote else 0
        words[3] = pack_voltage_counts  # DC voltage x0.1 V
        words[13] = measured_word  # PCS active power, int16 W
        # Advisory per-pod CT words (PROTOCOL_EVIDENCE 4c), scripted scenario
        # values served verbatim: grid export/import at +17, load at +20.
        words[_PCS_GRID_POWER_OFFSET] = self._scripted_grid_power_w & 0xFFFF
        words[_PCS_LOAD_POWER_OFFSET] = self._scripted_load_power_w & 0xFFFF
        self._blocks[_PCS_LIVE_BASE] = words

    def _rebuild_fault_blocks(self) -> None:
        for block, base in _FAULT_BLOCK_BASES:
            spec = faults.FAULT_BLOCK_LAYOUTS[block]
            words = [0] * spec.register_count
            for prefix, offset in spec.warning_offsets.items():
                words[offset] = self._fault_words.get(prefix, 0)
            for prefix, offset in spec.fault_offsets.items():
                words[offset] = self._fault_words.get(prefix, 0)
            self._blocks[base] = words

    def _rebuild_pcs_detail_block(
        self, applied_active_word: int, applied_reactive_word: int
    ) -> None:
        words = [0] * 32
        words[0] = 0  # debug status readback: normal mode
        # The served objective words: the night-writer scenario's FOREIGN pair
        # overrides the applied pair while scripted (the external writer's
        # words are what the wire serves); then, while parked, the flip hook's
        # echo of the last ignored write (DESIGN_POD_PARKING section 6 -- the
        # conservative default leaves these words unchanged); otherwise the
        # pod's own.
        scripted = self._scripted_objective
        parked_echo = self._parked_readback_words if self._debug_mode != 0 else None
        active_word, reactive_word = (
            (scripted[0], scripted[1])
            if scripted is not None
            else parked_echo
            if parked_echo is not None
            else (applied_active_word, applied_reactive_word)
        )
        words[17] = active_word & 0xFFFF  # active power objective, int16 W
        words[18] = reactive_word & 0xFFFF  # reactive power objective, int16 var
        words[24] = _APPARENT_LIMIT_W
        words[25] = _DISCHARGE_POWER_LIMIT_W
        words[26] = _CHARGE_POWER_LIMIT_W
        words[27] = _REACTIVE_LIMIT_VAR
        self._blocks[_PCS_DETAIL_BASE] = words

    def _rebuild_dcdc_live_block(self, pack_voltage_counts: int, measured_word: int) -> None:
        words = [0] * 13
        words[0] = 0x0101  # packed DCDC software version
        words[3] = pack_voltage_counts  # battery voltage == BMS pack voltage
        words[9] = measured_word  # battery power == measured battery power
        self._blocks[_DCDC_LIVE_BASE] = words

    def _rebuild_dcdc_detail_block(self) -> None:
        self._blocks[_DCDC_DETAIL_BASE] = [0] * 19

    def _grid_buy_counts(self) -> int:
        counts = self._grid_buy_counts_base + int(
            self._grid_buy_watt_seconds / _WATT_SECONDS_PER_COUNT
        )
        return min(_ENERGY_COUNT_CEILING - 1, counts)

    def _grid_sell_counts(self) -> int:
        counts = self._grid_sell_counts_base + int(
            self._grid_sell_watt_seconds / _WATT_SECONDS_PER_COUNT
        )
        return min(_ENERGY_COUNT_CEILING - 1, counts)

    def _load_counts(self) -> int:
        counts = self._load_counts_base + int(self._load_watt_seconds / _WATT_SECONDS_PER_COUNT)
        return min(_ENERGY_COUNT_CEILING - 1, counts)

    def _rebuild_totals_block(self) -> None:
        words: list[int] = []
        for counts in (
            self._grid_buy_counts(),
            self._grid_sell_counts(),
            self._load_counts(),
            self._pv_counts,
            self._charge_energy_counts(),
            self._discharge_energy_counts(),
        ):
            # The entire IoT cumulative block is low-word-first uint32 x0.1.
            words.extend((counts & 0xFFFF, (counts >> 16) & 0xFFFF))
        self._blocks[_TOTALS_BASE] = words

    def _rebuild_bms_block(
        self, pack_voltage_counts: int, pack_current_word: int, measured_word: int
    ) -> None:
        words = [0] * 31
        words[0] = 0x0101  # packed BMS version; > 10 selects the IoT layout
        words[1] = 3  # BMS status: running
        words[4] = 0x01  # string enable mask (one stack)
        words[5] = self._bic_count  # int16 BIC count
        words[6] = pack_voltage_counts  # pack voltage x0.1 V
        words[7] = pack_current_word  # pack current x0.1 A
        words[8] = measured_word  # measured battery power, int16 W
        words[9] = int(self._soc_pct)  # SOC, raw percent
        words[10] = self._soh_pct  # SOH, raw percent
        words[11] = _CHARGE_CURRENT_LIMIT_COUNTS
        words[12] = _DISCHARGE_CURRENT_LIMIT_COUNTS
        words[13] = _CHARGE_POWER_LIMIT_W
        words[14] = _DISCHARGE_POWER_LIMIT_W
        words[15], words[16] = self._low_first_words(self._charge_energy_counts())
        words[17], words[18] = self._low_first_words(self._discharge_energy_counts())
        cells = self._cell_millivolts
        temperatures = self._cell_temperature_words
        highest_voltage = _index_of_max(cells)
        lowest_voltage = _index_of_min(cells)
        hottest = _index_of_max(temperatures)
        coldest = _index_of_min(temperatures)
        # Extrema words cohere with the cell blocks they summarize.  The
        # temperature block holds BIC*3 sensors (not one per cell), so a cell
        # reports its own BIC's first sensor.  The pinned coherence contract
        # (T-SIM-POD-002) serves the BMS extrema temperature words one raw-40
        # offset above the temperature-block words.
        words[19], words[20], words[21] = (
            highest_voltage,
            cells[highest_voltage],
            self._temperature_word_for_cell(highest_voltage) + 40,
        )
        words[22], words[23], words[24] = (
            lowest_voltage,
            cells[lowest_voltage],
            self._temperature_word_for_cell(lowest_voltage) + 40,
        )
        words[25], words[26], words[27] = (
            hottest,
            temperatures[hottest] + 40,
            cells[hottest],
        )
        words[28], words[29], words[30] = (
            coldest,
            temperatures[coldest] + 40,
            cells[coldest],
        )
        self._blocks[_BMS_BASE] = words

    def _rebuild_cell_blocks(self) -> None:
        self._blocks[_CELL_VOLTAGE_BASE] = list(self._cell_millivolts)
        self._blocks[_CELL_TEMPERATURE_BASE] = list(self._cell_temperature_words)
        self._blocks[_CELL_BALANCE_BASE] = [0] * self._bic_count

    def _rebuild_identity_blocks(self) -> None:
        id_low, id_high = self._low_first_words(self._rtu_id)
        # The debug-mode readback serves the device's own mode word (written
        # at the vendor's asymmetric 0x8000 address; DESIGN_POD_PARKING 6).
        self._blocks[_DEBUG_MODE_BASE] = [self._debug_mode]
        self._blocks[_NETWORK_STATUS_BASE] = [0]
        self._blocks[_RTU_ID_BASE] = [id_low, id_high]
        parameters = [0] * 56
        parameters[4], parameters[5] = id_low, id_high  # RTU ID at 0x8102+4..5
        self._blocks[_PARAMETERS_BASE] = parameters

    def _served_window(self, address: int, count: int) -> list[int]:
        # A device serves no window at all for a non-positive or non-integer
        # register count (an FC03 quantity of 0 is a protocol violation), so
        # the pod refuses it exactly like an unmapped window instead of
        # serving a truncated or empty slice.
        if type(count) is not int or count < 1:
            raise ValueError("count must define a non-empty register window")
        for base, words in self._blocks.items():
            if base <= address and address + count <= base + len(words):
                return words[address - base : address + count - base]
        raise ValueError("register window is not served by the evidenced IoT layout")

    def _require_served_address(self, address: int) -> None:
        if type(address) is not int or not 0 <= address <= 0xFFFF:
            raise ValueError("address must be an unsigned 16-bit integer")
        self._served_window(address, 1)

    def _require_fault_word(self, prefix: str, bit: int) -> None:
        if type(prefix) is not str or prefix not in _IOT_FAULT_PREFIXES:
            raise ValueError("prefix must name a served IoT fault word")
        if type(bit) is not int or not 0 <= bit <= 15:
            raise ValueError("fault bit must be between 0 and 15")

    def _pack_voltage_counts(self) -> int:
        # Pack voltage is x0.1 V counts derived from the series cell sum.
        return max(1, round(sum(self._cell_millivolts) / 100))

    def _pack_current_counts(self, pack_voltage_counts: int, delivered_w: int) -> int:
        volts = pack_voltage_counts / 10.0
        if volts <= 0.0:
            return 0
        return round(delivered_w / volts * 10.0)

    def _charge_energy_counts(self) -> int:
        counts = self._charge_base_counts + int(self._charge_watt_seconds / _WATT_SECONDS_PER_COUNT)
        return min(_ENERGY_COUNT_CEILING - 1, counts)

    def _discharge_energy_counts(self) -> int:
        counts = self._discharge_base_counts + int(
            self._discharge_watt_seconds / _WATT_SECONDS_PER_COUNT
        )
        return min(_ENERGY_COUNT_CEILING - 1, counts)

    @staticmethod
    def _low_first_words(counts: int) -> tuple[int, int]:
        return (counts & 0xFFFF, (counts >> 16) & 0xFFFF)

    def _temperature_word_for_cell(self, cell_index: int) -> int:
        # The temperature block carries three sensors per BIC, so cell N
        # reports the first sensor of the BIC that owns it.
        sensor = (cell_index // 10) * 3
        return self._cell_temperature_words[min(sensor, len(self._cell_temperature_words) - 1)]


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    number = float(value)
    return math.isfinite(number) and number > 0.0


def _validated_debug_mode(value: object) -> int:
    """The debug-mode whitelist, shared by the device model and its transport.

    ``{0, 1}`` and nothing else, ever (DESIGN_POD_PARKING section 0): 0 is
    Normal and 1 is Standby -- the live-proven pair of the 2026-08-24 rhs
    standby cycle -- while the vendor values 2-6 (Charge, Discharge,
    Circulation, Fixing SOC, Verify Capacity) are permanently unexposed and
    every other shape is refused before any device state is consulted.
    """
    if type(value) is not int or value not in (0, 1):
        raise ValueError("debug mode accepts only 0 (Normal) or 1 (Standby)")
    return value


def _index_of_max(values: list[int]) -> int:
    best = 0
    for index in range(1, len(values)):
        if values[index] > values[best]:
            best = index
    return best


def _index_of_min(values: list[int]) -> int:
    best = 0
    for index in range(1, len(values)):
        if values[index] < values[best]:
            best = index
    return best
