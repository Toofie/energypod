"""In-memory actor transport over one simulated register bank.

`SimulatorTransport` implements the actor transport port
(``connect``/``read_holding``/``write_registers``/``close``) against a
`SimulatedEnergyPod` and enforces the production write gate: only the
evidenced three-register ``[1, P, Q]`` objective at ``0x0200`` is writable and
every other write is refused before the device is touched (ADR-0003 decision
D4).  The sanctioned vendor debug-mode word is the one named addition
(DESIGN_POD_PARKING section 5): ``write_debug_mode(value)`` mirrors the
production transport's separately named method -- the generic
``write_registers`` gate stays byte-identical and can never reach ``0x8000``.
It never opens a socket: exact Waveshare on-wire framing is an
unverified-evidence commissioning capture item and must not be simulated as
if known.
"""

from __future__ import annotations

from collections.abc import Sequence

from .pod import SimulatedEnergyPod, _validated_debug_mode

_PQ_WRITE_ADDRESS = 0x0200
_PQ_FRAME_LENGTH = 3
_PQ_HEADER_WORD = 1
_MAX_READ_COUNT = 125  # FC03 protocol limit


class SimulatorTransport:
    """One-pod transport with device-like boundaries and no socket I/O."""

    def __init__(self, *, pod: SimulatedEnergyPod) -> None:
        self._pod = pod
        self._connected = False
        self._closed = False

    @property
    def pod(self) -> SimulatedEnergyPod:
        return self._pod

    async def connect(self) -> None:
        self._ensure_not_closed()
        if self._connected:
            return
        if not self._pod.link_up:
            raise ConnectionError("simulated device link is down")
        self._connected = True

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        _validate_window(address, count)
        self._ensure_connected()
        return self._pod.read(address, count)

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        frame = _validated_pq_frame(address, values)
        self._ensure_connected()
        self._pod.apply_pq_frame(frame)

    async def write_debug_mode(self, value: int) -> None:
        """Write the sanctioned vendor debug-mode word (0x8000 <- value).

        The named method the production transport grew for pod parking
        (DESIGN_POD_PARKING section 5, item 1): the value is validated against
        the ``{0, 1}`` whitelist BEFORE any connection or device state is
        consulted -- the ``_validated_pq_frame`` ordering -- and the generic
        ``write_registers`` gate stays byte-identical, so only this method can
        ever reach the debug register.
        """
        mode = _validated_debug_mode(value)
        self._ensure_connected()
        self._pod.apply_debug_mode(mode)

    async def close(self) -> None:
        self._closed = True
        self._connected = False

    def _ensure_not_closed(self) -> None:
        if self._closed:
            raise ConnectionError("simulator transport is closed")

    def _ensure_connected(self) -> None:
        self._ensure_not_closed()
        if not self._connected:
            raise ConnectionError("simulator transport is not connected")
        if not self._pod.link_up:
            raise ConnectionError("simulated device link is down")


def _validate_window(address: int, count: int) -> None:
    if type(address) is not int or not 0 <= address <= 0xFFFF:
        raise ValueError("address must be an unsigned 16-bit integer")
    if type(count) is not int or not 1 <= count <= _MAX_READ_COUNT:
        raise ValueError("count must define a non-empty FC03-sized register block")
    if address + count - 1 > 0xFFFF:
        raise ValueError("register window exceeds the Modbus address space")


def _validated_pq_frame(address: int, values: Sequence[int]) -> tuple[int, ...]:
    """Validate one write against the production write gate.

    Malformed frames raise ``ValueError`` before any connection or device
    state is consulted, so a refused write can never half-latch an objective.
    """
    if type(address) is not int or not 0 <= address <= 0xFFFF:
        raise ValueError("address must be an unsigned 16-bit integer")
    if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
        raise ValueError("values must be a register sequence")
    frame = tuple(_register(value) for value in values)
    if not frame or address + len(frame) - 1 > 0xFFFF:
        raise ValueError("write range must fit the Modbus address space")
    if (
        address != _PQ_WRITE_ADDRESS
        or len(frame) != _PQ_FRAME_LENGTH
        or frame[0] != _PQ_HEADER_WORD
    ):
        raise ValueError("only the evidenced three-register PQ objective is writable")
    return frame


def _register(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise ValueError("register values must be unsigned 16-bit integers")
    return value
