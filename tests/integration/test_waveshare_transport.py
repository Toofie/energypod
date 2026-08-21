"""Focused PyModbus/Waveshare adapter contract tests.

A recording client double verifies the exact PyModbus 3.15 API.  A one-exchange TCP capture peer
separately verifies independently calculated reference RTU bytes.  These are E1 reference vectors,
not captured golden vectors and not evidence that the deployed Waveshare uses this framing.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest

# RV-WAVE-001, E1: independently calculated FC03, device 4, zero-based 0x5000, count 7.
LAYOUT_READ_REQUEST = bytes.fromhex("04 03 50 00 00 07 15 5d")
LAYOUT_READ_RESPONSE = bytes.fromhex("04 03 0e 01 01 00 03 00 00 00 00 00 01 00 06 00 00 2c 81")
LAYOUT_REGISTERS = (0x0101, 0x0003, 0, 0, 1, 6, 0)

# RV-WAVE-002, E1: independently calculated FC16 at 0x0200. No direction meaning is assigned.
SIGNED_PQ_WRITE_REQUEST = bytes.fromhex("04 10 02 00 00 03 06 00 01 fa 24 00 00 ac 2e")
PQ_WRITE_RESPONSE = bytes.fromhex("04 10 02 00 00 03 81 e5")
MODBUS_EXCEPTION_RESPONSE = bytes.fromhex("04 83 02 d0 f0")
WRONG_DEVICE_RESPONSE = bytes.fromhex("05 03 0e 01 01 00 03 00 00 00 00 00 01 00 06 00 00 7d 11")
SHORT_READ_RESPONSE = bytes.fromhex("04 03 0c 01 01 00 03 00 00 00 00 00 01 00 06 6a 7e")


class _MissingContract:
    def __init__(self, message: str) -> None:
        self.message = message

    def __getattr__(self, name: str) -> Any:
        pytest.fail(self.message, pytrace=False)


@pytest.fixture(scope="module")
def contract() -> Any:
    try:
        pymodbus = importlib.import_module("pymodbus")
        client_module = importlib.import_module("pymodbus.client")
        transport = importlib.import_module("energypod.adapters.modbus.waveshare")
        return type(
            "TransportContract",
            (),
            {
                "pymodbus": pymodbus,
                "AsyncModbusTcpClient": client_module.AsyncModbusTcpClient,
                "FramerType": pymodbus.FramerType,
                "WaveshareTransportConfig": transport.WaveshareTransportConfig,
                "WaveshareTransport": transport.WaveshareTransport,
                "TransportConnectionError": transport.TransportConnectionError,
                "ModbusResponseError": transport.ModbusResponseError,
            },
        )
    except (ImportError, AttributeError) as error:
        return _MissingContract(f"Waveshare transport contract is not implemented: {error}")


@dataclass
class StubResponse:
    function_code: int
    registers: list[int] = field(default_factory=list)
    address: int | None = None
    count: int | None = None
    dev_id: int = 4
    error: bool = False

    def isError(self) -> bool:
        return self.error


class RecordingClient:
    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host = host
        self.constructor_kwargs = kwargs
        self.connect_result = True
        self.connected = False
        self.closed = False
        self.read_calls: list[tuple[int, int, int]] = []
        self.write_calls: list[tuple[int, tuple[int, ...], int]] = []
        self.read_responses: list[Any] = []
        self.write_responses: list[Any] = []
        self.read_entered = asyncio.Event()
        self.read_release: asyncio.Event | None = None
        self.write_entered = asyncio.Event()
        self.write_release: asyncio.Event | None = None

    async def connect(self) -> bool:
        self.connected = self.connect_result
        return self.connect_result

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> StubResponse:
        self.read_calls.append((address, count, device_id))
        self.read_entered.set()
        if self.read_release is not None:
            await self.read_release.wait()
        if self.read_responses:
            response = self.read_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return StubResponse(function_code=3, registers=[0] * count, dev_id=device_id)

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> StubResponse:
        self.write_calls.append((address, tuple(values), device_id))
        self.write_entered.set()
        if self.write_release is not None:
            await self.write_release.wait()
        if self.write_responses:
            response = self.write_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return StubResponse(
            function_code=16,
            address=address,
            count=len(values),
            dev_id=device_id,
        )

    def close(self) -> None:
        self.closed = True
        self.connected = False


class RecordingFactory:
    def __init__(self) -> None:
        self.instances: list[RecordingClient] = []

    def __call__(self, host: str, **kwargs: Any) -> RecordingClient:
        client = RecordingClient(host, **kwargs)
        self.instances.append(client)
        return client


class ReferenceRtuPeer:
    """Capture one exact request and send one fixed, independently authored response."""

    def __init__(self, *, expected_request: bytes, response: bytes) -> None:
        self.expected_request = expected_request
        self.response = response
        self.received: bytes | None = None
        self.server: asyncio.AbstractServer | None = None
        self.exchange: asyncio.Future[bytes] | None = None

    @property
    def port(self) -> int:
        assert self.server is not None
        assert self.server.sockets
        return int(self.server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self.exchange = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert self.exchange is not None
        try:
            request = await reader.readexactly(len(self.expected_request))
            self.received = request
            if request != self.expected_request:
                raise AssertionError(
                    f"expected {self.expected_request.hex(' ')}, got {request.hex(' ')}"
                )
            writer.write(self.response)
            await writer.drain()
            if not self.exchange.done():
                self.exchange.set_result(request)
        except Exception as error:
            if not self.exchange.done():
                self.exchange.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    async def wait(self) -> bytes:
        assert self.exchange is not None
        return await asyncio.wait_for(self.exchange, timeout=2.0)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


def _make_transport(contract: Any) -> tuple[Any, RecordingClient]:
    factory = RecordingFactory()
    config = contract.WaveshareTransportConfig(
        host="192.168.1.11",
        port=4196,
        device_id=4,
        timeout_s=0.75,
        retries=0,
        reconnect_delay_s=0,
    )
    transport = contract.WaveshareTransport(config=config, client_factory=factory)
    assert len(factory.instances) == 1
    return transport, factory.instances[0]


def _make_real_transport(contract: Any, *, port: int) -> Any:
    config = contract.WaveshareTransportConfig(
        host="127.0.0.1",
        port=port,
        device_id=4,
        timeout_s=1.0,
        retries=0,
        reconnect_delay_s=0,
    )
    return contract.WaveshareTransport(config=config)


def test_pinned_pymodbus_api_uses_device_id_keyword(contract: Any) -> None:
    """T-INT-TRANSPORT-001 / PYMODBUS-3.15 / S1."""
    assert contract.pymodbus.__version__ == "3.15.0"
    read_parameters = inspect.signature(
        contract.AsyncModbusTcpClient.read_holding_registers
    ).parameters
    write_parameters = inspect.signature(contract.AsyncModbusTcpClient.write_registers).parameters

    assert read_parameters["count"].kind is inspect.Parameter.KEYWORD_ONLY
    assert read_parameters["device_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert write_parameters["device_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "slave" not in read_parameters and "unit" not in read_parameters
    assert "slave" not in write_parameters and "unit" not in write_parameters


def test_transport_constructs_tcp_client_with_explicit_rtu_framer(contract: Any) -> None:
    """T-INT-TRANSPORT-002 / P-TRANSPORT / S0."""
    _, client = _make_transport(contract)

    assert client.host == "192.168.1.11"
    assert client.constructor_kwargs == {
        "port": 4196,
        "framer": contract.FramerType.RTU,
        "timeout": 0.75,
        "retries": 0,
        "reconnect_delay": 0,
    }
    assert client.constructor_kwargs["framer"] is not contract.FramerType.SOCKET


async def test_layout_probe_uses_zero_based_fc03_arguments_and_device_id(contract: Any) -> None:
    """T-INT-TRANSPORT-003 / V-LAYOUT / S0."""
    transport, client = _make_transport(contract)
    client.read_responses.append(StubResponse(function_code=3, registers=[11, 0, 0, 0, 1, 6, 0]))
    await transport.connect()

    result = await transport.read_holding(0x5000, 7)

    assert result == (11, 0, 0, 0, 1, 6, 0)
    assert client.read_calls == [(0x5000, 7, 4)]


async def test_full_pq_write_uses_zero_based_fc16_arguments_and_device_id(contract: Any) -> None:
    """T-INT-TRANSPORT-004 / V-WRITE / S0."""
    transport, client = _make_transport(contract)
    client.write_responses.append(StubResponse(function_code=16, address=0x0200, count=3, dev_id=4))
    await transport.connect()

    await transport.write_registers(0x0200, (1, 0xF448, 0x02EE))

    assert client.write_calls == [(0x0200, (1, 0xF448, 0x02EE), 4)]


async def test_connection_failure_is_explicit(contract: Any) -> None:
    """T-INT-TRANSPORT-005 / INV-TRANSPORT-001 / S1."""
    transport, client = _make_transport(contract)
    client.connect_result = False

    with pytest.raises(contract.TransportConnectionError):
        await transport.connect()


@pytest.mark.parametrize(
    "response",
    [
        StubResponse(function_code=3, registers=[0] * 7, error=True),
        StubResponse(function_code=4, registers=[0] * 7),
        StubResponse(function_code=3, registers=[0] * 6),
        StubResponse(function_code=3, registers=[0] * 8),
        StubResponse(function_code=3, registers=[0] * 7, dev_id=5),
    ],
)
async def test_read_rejects_error_function_length_or_device_mismatch(
    contract: Any,
    response: StubResponse,
) -> None:
    """T-INT-TRANSPORT-006 / INV-RESPONSE-001 / S0."""
    transport, client = _make_transport(contract)
    client.read_responses.append(response)
    await transport.connect()

    with pytest.raises(contract.ModbusResponseError):
        await transport.read_holding(0x5000, 7)


@pytest.mark.parametrize(
    "response",
    [
        None,
        object(),
        StubResponse(function_code=3, registers=[0, 0, 0, 0, 0, 0, True]),
        StubResponse(function_code=3, registers=[0, 0, 0, 0, 0, 0, -1]),
        StubResponse(function_code=3, registers=[0, 0, 0, 0, 0, 0, 65536]),
    ],
)
async def test_read_rejects_malformed_response_objects_and_register_values(
    contract: Any, response: Any
) -> None:
    """T-INT-TRANSPORT-014 / INV-RESPONSE-002 / S0."""
    transport, client = _make_transport(contract)
    client.read_responses.append(response)
    await transport.connect()

    with pytest.raises(contract.ModbusResponseError):
        await transport.read_holding(0x5000, 7)


@pytest.mark.parametrize(
    "response",
    [
        StubResponse(function_code=16, address=0x0200, count=3, error=True),
        StubResponse(function_code=6, address=0x0200, count=3),
        StubResponse(function_code=16, address=0x0201, count=3),
        StubResponse(function_code=16, address=0x0200, count=2),
        StubResponse(function_code=16, address=0x0200, count=3, dev_id=5),
    ],
)
async def test_write_rejects_error_or_acknowledgement_mismatch(
    contract: Any,
    response: StubResponse,
) -> None:
    """T-INT-TRANSPORT-007 / INV-ACK-001 / S0."""
    transport, client = _make_transport(contract)
    client.write_responses.append(response)
    await transport.connect()

    with pytest.raises(contract.ModbusResponseError):
        await transport.write_registers(0x0200, (1, 0, 0))


async def test_transport_serializes_reads_and_writes_on_one_client(contract: Any) -> None:
    """T-INT-TRANSPORT-008 / INV-ACTOR-001 / S0."""
    transport, client = _make_transport(contract)
    client.read_release = asyncio.Event()
    await transport.connect()

    read = asyncio.create_task(transport.read_holding(0x5000, 7))
    await client.read_entered.wait()
    write = asyncio.create_task(transport.write_registers(0x0200, (1, 0, 0)))
    await asyncio.sleep(0)

    assert client.write_calls == []
    client.read_release.set()
    await asyncio.gather(read, write)
    assert client.write_calls == [(0x0200, (1, 0, 0), 4)]


async def test_cancelled_read_releases_serialization_and_propagates_cancellation(
    contract: Any,
) -> None:
    """T-INT-TRANSPORT-012 / INV-TRANSPORT-002 / S0."""
    transport, client = _make_transport(contract)
    client.read_release = asyncio.Event()
    await transport.connect()

    read = asyncio.create_task(transport.read_holding(0x5000, 7))
    await asyncio.wait_for(client.read_entered.wait(), timeout=1.0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    await asyncio.wait_for(transport.write_registers(0x0200, (1, 0, 0)), timeout=1.0)
    assert client.write_calls == [(0x0200, (1, 0, 0), 4)]


async def test_cancelled_write_is_not_retried_or_reported_as_acknowledged(contract: Any) -> None:
    """T-INT-TRANSPORT-013 / INV-TRANSPORT-003 / S0."""
    transport, client = _make_transport(contract)
    client.write_release = asyncio.Event()
    await transport.connect()

    write = asyncio.create_task(transport.write_registers(0x0200, (1, 0, 0)))
    await asyncio.wait_for(client.write_entered.wait(), timeout=1.0)
    write.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write

    await asyncio.sleep(0)
    assert client.write_calls == [(0x0200, (1, 0, 0), 4)]


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_request_failure_is_not_retried_inside_transport(
    contract: Any, operation: str
) -> None:
    """T-INT-TRANSPORT-015 / INV-TRANSPORT-004 / S0.

    A retry could duplicate FC16 or consume command-deadline budget. Reconnect and retry decisions
    belong to the generation-fenced actor and require fresh authorization.
    """
    transport, client = _make_transport(contract)
    await transport.connect()

    if operation == "read":
        client.read_responses.append(ConnectionResetError("synthetic reset"))
        with pytest.raises(contract.TransportConnectionError):
            await transport.read_holding(0x5000, 7)
        assert client.read_calls == [(0x5000, 7, 4)]
        assert client.write_calls == []
    else:
        client.write_responses.append(ConnectionResetError("synthetic reset"))
        with pytest.raises(contract.TransportConnectionError):
            await transport.write_registers(0x0200, (1, 0, 0))
        assert client.write_calls == [(0x0200, (1, 0, 0), 4)]


async def test_close_delegates_to_pymodbus_sync_close(contract: Any) -> None:
    """T-INT-TRANSPORT-009 / PYMODBUS-3.15 / S2."""
    transport, client = _make_transport(contract)
    await transport.connect()

    await transport.close()

    assert client.closed is True


async def test_real_pymodbus_fc03_emits_literal_rtu_over_tcp_frame(contract: Any) -> None:
    """T-INT-TRANSPORT-010 / RV-WAVE-001 / S0."""
    peer = ReferenceRtuPeer(
        expected_request=LAYOUT_READ_REQUEST,
        response=LAYOUT_READ_RESPONSE,
    )
    await peer.start()
    transport = _make_real_transport(contract, port=peer.port)

    try:
        await transport.connect()
        registers = await transport.read_holding(0x5000, 7)
        assert registers == LAYOUT_REGISTERS
        assert await peer.wait() == LAYOUT_READ_REQUEST
    finally:
        await transport.close()
        await peer.close()


async def test_real_pymodbus_fc16_emits_literal_signed_pq_rtu_frame(contract: Any) -> None:
    """T-INT-TRANSPORT-011 / RV-WAVE-002 / S0."""
    peer = ReferenceRtuPeer(
        expected_request=SIGNED_PQ_WRITE_REQUEST,
        response=PQ_WRITE_RESPONSE,
    )
    await peer.start()
    transport = _make_real_transport(contract, port=peer.port)

    try:
        await transport.connect()
        await transport.write_registers(0x0200, (1, 0xFA24, 0))
        assert await peer.wait() == SIGNED_PQ_WRITE_REQUEST
    finally:
        await transport.close()
        await peer.close()


@pytest.mark.parametrize(
    "response",
    [
        LAYOUT_READ_RESPONSE[:-1] + bytes([LAYOUT_READ_RESPONSE[-1] ^ 0x01]),
        LAYOUT_READ_RESPONSE[:-3],
        MODBUS_EXCEPTION_RESPONSE,
        WRONG_DEVICE_RESPONSE,
        SHORT_READ_RESPONSE,
    ],
    ids=["bad-crc", "partial-frame", "exception", "wrong-device", "short-read"],
)
async def test_real_pymodbus_rejects_malformed_or_mismatched_rtu_responses(
    contract: Any, response: bytes
) -> None:
    """T-INT-TRANSPORT-016 / RV-WAVE-001 / INV-RESPONSE-003 / S0."""
    peer = ReferenceRtuPeer(expected_request=LAYOUT_READ_REQUEST, response=response)
    await peer.start()
    transport = _make_real_transport(contract, port=peer.port)

    try:
        await transport.connect()
        with pytest.raises(contract.ModbusResponseError):
            await transport.read_holding(0x5000, 7)
        assert await peer.wait() == LAYOUT_READ_REQUEST
    finally:
        await transport.close()
        await peer.close()
