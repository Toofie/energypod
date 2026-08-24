"""Mutation-round killing tests: the write_debug_mode surface (2026-08-24).

From the pod-parking mutation round (isolated worktree, HEAD a084920): every
SEMANTIC mutant inside ``write_debug_mode`` -- the {0,1} bound, the FC16
shape at 0x8000, the ACK-echo validation, the resync-after-failure discipline
-- was already killed by the transport suite. What survived in the target
region was residue this file now pins:

- the refusal and echo-validation message TEXTS (the existing suite matches
  by substring, so whole-message mutants survived);
- ``_is_error``'s fail-closed branches on the FC16 path: a response object
  without a callable ``isError`` and one whose ``isError`` raises must both
  refuse, never pass;
- the ModbusException mapping's exact message (the OSError path's twin).

(The ``response is None`` branch of ``_is_error`` is equivalent on this path:
a None fails the function-code check regardless.)

SAFETY: recording-client double only -- no socket, no live system.
"""

from __future__ import annotations

import importlib
import typing
from types import SimpleNamespace

import pytest

from .test_waveshare_transport import StubResponse, _make_transport


@pytest.fixture(scope="module")
def contract() -> typing.Any:
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
        pytest.fail(f"Waveshare transport contract is not implemented: {error}")


_DEBUG_MODE_ADDRESS = 0x8000
_DOMAIN_REFUSAL = (
    "write_debug_mode accepts only 0 (Normal) or 1 (Standby): "
    "the vendor values 2-6 are permanently unexposed"
)


async def test_the_domain_refusal_message_is_pinned_exactly(contract: typing.Any) -> None:
    transport, client = _make_transport(contract)
    await transport.connect()

    with pytest.raises(ValueError) as caught:
        await transport.write_debug_mode(2)
    assert str(caught.value) == _DOMAIN_REFUSAL
    assert client.write_calls == [], "a refused mode value must never reach the bus"


async def test_write_debug_mode_maps_a_modbus_exception_to_the_response_error(
    contract: typing.Any,
) -> None:
    """A malformed/exceptional FC16 response is the response error, and the
    next operation runs on a reopened stream (the late-response offset guard)
    -- the same discipline the OSError path already pins."""
    transport, client = _make_transport(contract)
    await transport.connect()

    client.write_responses.append(contract.pymodbus.exceptions.ModbusException("garbage pdu"))
    with pytest.raises(contract.ModbusResponseError) as caught:
        await transport.write_debug_mode(1)
    assert str(caught.value) == "Modbus write did not produce a valid acknowledgement"
    assert client.write_calls == [(_DEBUG_MODE_ADDRESS, (1,), 4)]
    assert client.closed is True, "the exceptional PDU must have closed the stale stream"


async def test_the_connection_loss_message_is_pinned_on_the_mode_path(
    contract: typing.Any,
) -> None:
    transport, client = _make_transport(contract)
    await transport.connect()

    client.write_responses.append(ConnectionResetError("synthetic reset"))
    with pytest.raises(contract.TransportConnectionError) as caught:
        await transport.write_debug_mode(1)
    assert str(caught.value) == "connection lost during Modbus write"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            StubResponse(
                function_code=16, address=_DEBUG_MODE_ADDRESS, count=1, dev_id=4, error=True
            ),
            "invalid FC16 acknowledgement",
        ),
        (
            StubResponse(function_code=6, address=_DEBUG_MODE_ADDRESS, count=1, dev_id=4),
            "invalid FC16 acknowledgement",
        ),
        (
            StubResponse(function_code=16, address=0x8001, count=1, dev_id=4),
            "FC16 acknowledgement does not match request",
        ),
        (
            StubResponse(function_code=16, address=_DEBUG_MODE_ADDRESS, count=3, dev_id=4),
            "FC16 acknowledgement does not match request",
        ),
        (
            StubResponse(function_code=16, address=_DEBUG_MODE_ADDRESS, count=1, dev_id=5),
            "FC16 acknowledgement device ID mismatch",
        ),
    ],
)
async def test_the_fc16_echo_validation_messages_are_pinned_exactly(
    contract: typing.Any, response: typing.Any, message: str
) -> None:
    """Each mismatch class names itself exactly, and the error-flagged ACK
    refuses with the same first refusal."""
    transport, client = _make_transport(contract)
    await transport.connect()

    client.write_responses.append(response)
    with pytest.raises(contract.ModbusResponseError) as caught:
        await transport.write_debug_mode(1)
    assert str(caught.value) == message


async def test_a_well_formed_ack_without_is_error_fails_closed(contract: typing.Any) -> None:
    """A response object with NO callable ``isError`` is malformed evidence,
    never a passing ACK -- even when every echoed field matches the request."""
    transport, client = _make_transport(contract)
    await transport.connect()

    client.write_responses.append(
        SimpleNamespace(function_code=16, address=_DEBUG_MODE_ADDRESS, count=1, dev_id=4)
    )
    with pytest.raises(contract.ModbusResponseError) as caught:
        await transport.write_debug_mode(1)
    assert str(caught.value) == "invalid FC16 acknowledgement"


class _RaisingIsError:
    function_code = 16
    address = _DEBUG_MODE_ADDRESS
    count = 1
    dev_id = 4

    def isError(self) -> bool:
        raise RuntimeError("broken response object")


async def test_a_raising_is_error_fails_closed_on_the_mode_path(contract: typing.Any) -> None:
    transport, client = _make_transport(contract)
    await transport.connect()

    client.write_responses.append(_RaisingIsError())
    with pytest.raises(contract.ModbusResponseError) as caught:
        await transport.write_debug_mode(1)
    assert str(caught.value) == "invalid FC16 acknowledgement"
