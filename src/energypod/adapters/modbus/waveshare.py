"""Serialized PyModbus 3.15 transport for Waveshare RTU-over-TCP gateways.

This adapter performs exactly one operation per call. Reconnect and retry policy
belongs to the generation-fenced unit actor, never to this transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pymodbus import FramerType
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException


class TransportConnectionError(ConnectionError):
    """The TCP transport could not connect or was lost during an operation."""


class ModbusResponseError(RuntimeError):
    """A response was absent, malformed, exceptional, or did not acknowledge the request."""


@dataclass(frozen=True, slots=True)
class WaveshareTransportConfig:
    host: str
    port: int = 4196
    device_id: int = 4
    timeout_s: float = 1.0
    retries: int = 0
    reconnect_delay_s: float = 0.0
    inter_request_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if type(self.host) is not str or not self.host.strip():
            raise ValueError("host must be a non-empty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if type(self.device_id) is not int or not 0 <= self.device_id <= 247:
            raise ValueError("device_id must be between 0 and 247")
        if (
            type(self.timeout_s) not in (int, float)
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")
        if self.retries != 0 or type(self.retries) is not int:
            raise ValueError("transport retries must be exactly zero")
        if (
            type(self.reconnect_delay_s) not in (int, float)
            or not math.isfinite(self.reconnect_delay_s)
            or self.reconnect_delay_s < 0
        ):
            raise ValueError("reconnect_delay_s must be a non-negative finite number")
        if (
            type(self.inter_request_delay_s) not in (int, float)
            or not math.isfinite(self.inter_request_delay_s)
            or self.inter_request_delay_s < 0
        ):
            raise ValueError("inter_request_delay_s must be a non-negative finite number")


ClientFactory = Callable[..., Any]


class WaveshareTransport:
    """One-client, one-lock transport with strict response validation."""

    def __init__(
        self,
        *,
        config: WaveshareTransportConfig,
        client_factory: ClientFactory = AsyncModbusTcpClient,
    ) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._connected = False
        self._closed = False
        self._client_factory = client_factory
        # RTU-over-TCP gateways bridge to a half-duplex RS-485 bus: requests
        # fired back-to-back can make the gateway answer out of order (the
        # live commissioning capture observed stale/mismatched PDUs until the
        # prior integration's proven 0.1 s inter-frame gap was restored). The
        # gap is enforced under the request lock so every request pays it.
        self._last_request_mono: float | None = None
        self._client = client_factory(
            config.host,
            port=config.port,
            framer=FramerType.RTU,
            timeout=config.timeout_s,
            retries=config.retries,
            reconnect_delay=config.reconnect_delay_s,
        )

    async def connect(self) -> None:
        async with self._lock:
            self._ensure_not_closed()
            if self._connected:
                return
            try:
                connected = await self._client.connect()
            except (OSError, ModbusException) as error:
                raise TransportConnectionError("unable to connect to Waveshare gateway") from error
            if connected is not True:
                raise TransportConnectionError("Waveshare gateway declined the connection")
            self._ensure_not_closed()
            self._connected = True

    async def _respect_inter_request_gap(self) -> None:
        """Hold the commissioned inter-frame gap between bus requests."""
        gap = self._config.inter_request_delay_s
        if gap <= 0:
            self._last_request_mono = time.monotonic()
            return
        now = time.monotonic()
        if self._last_request_mono is not None:
            remaining = gap - (now - self._last_request_mono)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_mono = time.monotonic()

    async def _resync_after_failure(self) -> None:
        """Reopen the TCP stream after any operation failure.

        RTU framing carried over TCP carries no transaction identifier, so a
        single late response (the gateway occasionally answers after the
        caller's timeout under load) permanently offsets the response stream:
        every later response answers the PREVIOUS request and all operations
        time out forever. The gateway is stateless per connection, so closing
        and reconnecting clears the offset. Failures still propagate to the
        caller (fail-closed semantics are unchanged); only the next operation
        runs on a fresh stream.
        """
        self._connected = False
        with contextlib.suppress(Exception):
            await self._client.close()
        # A closed pymodbus client with reconnect_delay=0 never re-establishes
        # itself (observed live: the LHS transport stayed connectionless for
        # hours while the gateway happily accepted fresh connections). The
        # resync therefore rebuilds the client from the factory so the next
        # operation connects on a genuinely new socket.
        with contextlib.suppress(Exception):
            self._client = self._client_factory(
                self._config.host,
                port=self._config.port,
                framer=FramerType.RTU,
                timeout=self._config.timeout_s,
                retries=self._config.retries,
                reconnect_delay=self._config.reconnect_delay_s,
            )

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        self._validate_address_count(address, count)
        async with self._lock:
            self._ensure_connected()
            await self._respect_inter_request_gap()
            try:
                response = await self._client.read_holding_registers(
                    address,
                    count=count,
                    device_id=self._config.device_id,
                )
            except ModbusException as error:
                await self._resync_after_failure()
                raise ModbusResponseError("Modbus read did not produce a valid response") from error
            except OSError as error:
                await self._resync_after_failure()
                raise TransportConnectionError("connection lost during Modbus read") from error
            self._ensure_connected()
            return self._validate_read_response(response, count)

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        if type(address) is not int or not 0 <= address <= 0xFFFF:
            raise ValueError("address must be an unsigned 16-bit integer")
        if isinstance(values, str | bytes | bytearray) or not isinstance(values, Sequence):
            raise ValueError("values must be a register sequence")
        registers = tuple(self._validate_register(value) for value in values)
        if not registers or address + len(registers) - 1 > 0xFFFF:
            raise ValueError("write range must fit the Modbus address space")
        if address != 0x0200 or len(registers) != 3 or registers[0] != 1:
            raise ValueError("only the evidenced three-register PQ objective is writable")
        async with self._lock:
            self._ensure_connected()
            await self._respect_inter_request_gap()
            try:
                response = await self._client.write_registers(
                    address,
                    list(registers),
                    device_id=self._config.device_id,
                )
            except ModbusException as error:
                await self._resync_after_failure()
                raise ModbusResponseError(
                    "Modbus write did not produce a valid acknowledgement"
                ) from error
            except OSError as error:
                await self._resync_after_failure()
                raise TransportConnectionError("connection lost during Modbus write") from error
            self._ensure_connected()
            self._validate_write_response(response, address, len(registers))

    async def write_debug_mode(self, value: int) -> None:
        """Write the sanctioned vendor debug-mode word: FC16 ``[value]`` at 0x8000.

        The separately named method pod parking composes (DESIGN_POD_PARKING
        section 5, item 1): the generic ``write_registers`` predicate stays
        byte-identical and can never reach 0x8000 -- only this method can, and
        only when the ``parking:`` config block commissioned it.  The value
        domain is structural here at the transport layer, not caller
        discipline: exactly ``{0, 1}`` (0 Normal / 1 Standby), the pair
        live-proven on rhs 2026-08-24 (docs/evidence/standby-cycle-2026-
        08-24.md -- the FC16 echo-validated ACK, the ~1 s readback
        transitions at 0x8100, and the clean exit); the vendor values 2-6
        (Charge, Discharge, Circulation, Fixing SOC, Verify Capacity) are
        PERMANENTLY UNEXPOSED, and so is every other shape.  The write runs
        under the same lock, inter-frame gap, ACK-echo validation, and
        resync-after-failure discipline as the PQ objective.
        """
        if type(value) is not int or value not in (0, 1):
            raise ValueError(
                "write_debug_mode accepts only 0 (Normal) or 1 (Standby): the vendor "
                "values 2-6 are permanently unexposed"
            )
        async with self._lock:
            self._ensure_connected()
            await self._respect_inter_request_gap()
            try:
                response = await self._client.write_registers(
                    0x8000,
                    [value],
                    device_id=self._config.device_id,
                )
            except ModbusException as error:
                await self._resync_after_failure()
                raise ModbusResponseError(
                    "Modbus write did not produce a valid acknowledgement"
                ) from error
            except OSError as error:
                await self._resync_after_failure()
                raise TransportConnectionError("connection lost during Modbus write") from error
            self._ensure_connected()
            self._validate_write_response(response, 0x8000, 1)

    async def close(self) -> None:
        # Closing must not queue behind a cancellation-resistant socket operation:
        # closing the underlying client is the mechanism that unblocks that I/O.
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._connected = False
            result = self._client.close()
            if inspect.isawaitable(result):
                await result

    def _ensure_not_closed(self) -> None:
        if self._closed:
            raise TransportConnectionError("Waveshare transport is closed")

    def _ensure_connected(self) -> None:
        self._ensure_not_closed()
        if not self._connected:
            raise TransportConnectionError("Waveshare transport is not connected")

    @staticmethod
    def _validate_address_count(address: int, count: int) -> None:
        if type(address) is not int or not 0 <= address <= 0xFFFF:
            raise ValueError("address must be an unsigned 16-bit integer")
        if type(count) is not int or count <= 0 or address + count - 1 > 0xFFFF:
            raise ValueError("count must define a non-empty in-range register block")

    @staticmethod
    def _validate_register(value: object) -> int:
        if type(value) is not int or not 0 <= value <= 0xFFFF:
            raise ValueError("register values must be unsigned 16-bit integers")
        return value

    def _validate_read_response(self, response: object, count: int) -> tuple[int, ...]:
        if self._is_error(response) or getattr(response, "function_code", None) != 3:
            raise ModbusResponseError("invalid FC03 response")
        if getattr(response, "dev_id", None) != self._config.device_id:
            raise ModbusResponseError("FC03 response device ID mismatch")
        registers = getattr(response, "registers", None)
        if not isinstance(registers, list) or len(registers) != count:
            raise ModbusResponseError("FC03 response register count mismatch")
        try:
            return tuple(self._validate_register(value) for value in registers)
        except ValueError as error:
            raise ModbusResponseError("FC03 response contains an invalid register") from error

    def _validate_write_response(self, response: object, address: int, count: int) -> None:
        if self._is_error(response) or getattr(response, "function_code", None) != 16:
            raise ModbusResponseError("invalid FC16 acknowledgement")
        if getattr(response, "dev_id", None) != self._config.device_id:
            raise ModbusResponseError("FC16 acknowledgement device ID mismatch")
        if (
            getattr(response, "address", None) != address
            or getattr(response, "count", None) != count
        ):
            raise ModbusResponseError("FC16 acknowledgement does not match request")

    @staticmethod
    def _is_error(response: object) -> bool:
        if response is None:
            return True
        checker = getattr(response, "isError", None)
        if not callable(checker):
            return True
        try:
            return checker() is not False
        except Exception:
            return True
