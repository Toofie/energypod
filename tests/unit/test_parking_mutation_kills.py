"""Mutation-round killing tests: the REAL parking facade surface (2026-08-24).

From the pod-parking mutation round (isolated worktree, HEAD a084920): the
REST/MCP contract suites pin the park/renew/resume routes through a FAKE
service, and the composition suite drives only happy-path park and resume --
so the facade methods' own guards survived every mutant. This file pins them
against the composed runtime's REAL facade:

- the interactive-principal requirement, per method, both directions;
- the typed confirmation literals and the takeover literal (the valid
  ``FOREIGN`` takeover resume included);
- the block-absent refusal and the unknown-unit error, per method;
- the lease_s domain (type, both bounds) at the facade, by exact message;
- the malformed correlation-key messages, by exact message;
- the renewed lease sliding through the real facade (row + table truth);
- the park audit rows carrying the request correlation;
- the ``device_debug_mode_active`` refusal message pinned EXACTLY (the
  existing suites match by substring, so whole-message mutants survived).

SAFETY: the in-memory simulate composition only -- no socket, no live system.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

import pytest

from energypod.application.parking import (
    PARK_LEASE_CAP_REACHED,
    PARK_NOT_COMMISSIONED,
    ParkingRefusal,
)

from .test_composition import OPERATOR, OperatorPrincipal
from .test_parking_composition import ManualClock, _compose

_SERVICE_PRINCIPAL = OperatorPrincipal(
    scopes=frozenset({"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}),
    interactive=False,
)


def _park_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "unit_id": "mid",
        "confirmation": "PARK",
        "reason": "inverter work",
        "principal": OPERATOR,
        "idempotency_key": "kill-1",
        "request_id": "kill-1-request",
    }
    kwargs.update(overrides)
    return kwargs


def _renew_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "unit_id": "mid",
        "confirmation": "RENEW",
        "lease_s": 3600,
        "principal": OPERATOR,
        "idempotency_key": "kill-2",
        "request_id": "kill-2-request",
    }
    kwargs.update(overrides)
    return kwargs


def _resume_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "unit_id": "mid",
        "confirmation": "RESUME",
        "principal": OPERATOR,
        "idempotency_key": "kill-3",
        "request_id": "kill-3-request",
    }
    kwargs.update(overrides)
    return kwargs


_DEFAULT_PARKING = {"max_lease_s": 7200, "default_lease_s": 3600}


async def _started_runtime(
    clock: ManualClock | None = None, parking: dict[str, Any] | None = _DEFAULT_PARKING
) -> Any:
    runtime = _compose(parking, clock or ManualClock())
    for handle in runtime.actors.values():
        await handle.start()
    return runtime


async def _shutdown(runtime: Any) -> None:
    for handle in runtime.actors.values():
        with contextlib.suppress(Exception):
            await handle.shutdown()


def _lease_seconds(lease: dict[str, Any]) -> float:
    parked = datetime.fromisoformat(lease["parked_at"])
    expires = datetime.fromisoformat(lease["expires_at"])
    return (expires - parked).total_seconds()


# --- the interactive-principal requirement, per method ------------------------------


async def test_park_renew_and_resume_each_require_an_interactive_principal() -> None:
    runtime = await _started_runtime()
    try:
        for call in (
            runtime.facade.park_unit(**_park_kwargs(principal=_SERVICE_PRINCIPAL)),
            runtime.facade.renew_park_lease(**_renew_kwargs(principal=_SERVICE_PRINCIPAL)),
            runtime.facade.resume_unit(**_resume_kwargs(principal=_SERVICE_PRINCIPAL)),
        ):
            with pytest.raises(PermissionError, match="an interactive operator principal"):
                await call
    finally:
        await _shutdown(runtime)


# --- the facade's own validation rows, by exact message ----------------------------


async def test_park_validates_confirmation_reason_and_lease_bounds() -> None:
    runtime = await _started_runtime()
    try:
        with pytest.raises(ValueError, match="^confirmation must be the literal 'PARK'$"):
            await runtime.facade.park_unit(**_park_kwargs(confirmation="park"))
        with pytest.raises(ValueError, match="^a reason is required$"):
            await runtime.facade.park_unit(**_park_kwargs(reason=None))
        with pytest.raises(ValueError, match="^reason must be canonical"):
            await runtime.facade.park_unit(**_park_kwargs(reason=""))
        with pytest.raises(ValueError, match="^lease_s must be between 60 and 7200 seconds$"):
            await runtime.facade.park_unit(**_park_kwargs(lease_s=59))
        with pytest.raises(ValueError, match="^lease_s must be between 60 and 7200 seconds$"):
            await runtime.facade.park_unit(**_park_kwargs(lease_s=7201))
        with pytest.raises(ValueError, match="^lease_s must be between 60 and 7200 seconds$"):
            await runtime.facade.park_unit(**_park_kwargs(lease_s=3600.5))
        with pytest.raises(ValueError, match="^lease_s must be between 60 and 7200 seconds$"):
            await runtime.facade.park_unit(**_park_kwargs(lease_s=True))
        # Both bounds are INCLUSIVE on the real facade.
        for boundary in (60, 7200):
            result = await runtime.facade.park_unit(
                **_park_kwargs(
                    lease_s=boundary,
                    idempotency_key=f"kill-bounds-{boundary}",
                    request_id=f"kill-bounds-{boundary}-request",
                )
            )
            assert _lease_seconds(result["lease"]) == boundary
            await runtime.facade.resume_unit(
                **_resume_kwargs(
                    idempotency_key=f"kill-bounds-resume-{boundary}",
                    request_id=f"kill-bounds-resume-{boundary}-request",
                )
            )
    finally:
        await _shutdown(runtime)


async def test_the_correlation_key_messages_name_their_field_per_method() -> None:
    runtime = await _started_runtime()
    try:
        with pytest.raises(ValueError, match="^idempotency_key must be a canonical identifier$"):
            await runtime.facade.park_unit(**_park_kwargs(idempotency_key="not canonical"))
        with pytest.raises(ValueError, match="^request_id must be a canonical identifier$"):
            await runtime.facade.park_unit(**_park_kwargs(request_id="nope!"))
        with pytest.raises(ValueError, match="^unit_id must be a canonical identifier$"):
            await runtime.facade.park_unit(**_park_kwargs(unit_id="?"))
        with pytest.raises(ValueError, match="^idempotency_key must be a canonical identifier$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(idempotency_key="not canonical"))
        with pytest.raises(ValueError, match="^request_id must be a canonical identifier$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(request_id="nope!"))
        with pytest.raises(ValueError, match="^idempotency_key must be a canonical identifier$"):
            await runtime.facade.resume_unit(**_resume_kwargs(idempotency_key="not canonical"))
        with pytest.raises(ValueError, match="^request_id must be a canonical identifier$"):
            await runtime.facade.resume_unit(**_resume_kwargs(request_id="nope!"))
    finally:
        await _shutdown(runtime)


# --- renew: the whole method was untested against the real facade --------------------


async def test_renew_slides_the_lease_and_writes_the_renewed_row() -> None:
    clock = ManualClock()
    runtime = await _started_runtime(clock, {"max_lease_s": 7200, "default_lease_s": 3600})
    try:
        parked = await runtime.facade.park_unit(**_park_kwargs(lease_s=600))
        renewed = await runtime.facade.renew_park_lease(**_renew_kwargs(lease_s=1200))
        assert renewed["action"] == "renew"
        assert renewed["lease"]["epoch"] == parked["lease"]["epoch"]
        assert renewed["lease"]["parked_at"] == parked["lease"]["parked_at"]
        assert renewed["lease"]["expires_at"] > parked["lease"]["expires_at"]
        assert _lease_seconds(renewed["lease"]) == 1200
        rows = [
            event
            for event in runtime.audit.recent(limit=32)
            if event.event_type == "unit_park_renewed"
        ]
        assert [row.result for row in rows] == ["renewed"]
        assert rows[0].correlation_id == "parking:unit_park_renewed:kill-2-request"
        assert rows[0].unit_id == "mid"

        with pytest.raises(ValueError, match="^confirmation must be the literal 'RENEW'$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(confirmation="renew"))
        with pytest.raises(ValueError, match="^lease_s must be an integer$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(lease_s=3600.5))
        with pytest.raises(ValueError, match="^lease_s must be an integer$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(lease_s=True))
        with pytest.raises(ValueError, match="^lease_s must be between 60 and 7200 seconds$"):
            await runtime.facade.renew_park_lease(**_renew_kwargs(lease_s=59))
        # The renew bounds are inclusive too.
        assert (
            await runtime.facade.renew_park_lease(
                **_renew_kwargs(lease_s=60, idempotency_key="kill-60", request_id="kill-60-req")
            )
        )["lease"]["expires_at"]
        # Anti-rollover: while the lease is still open, a renewal whose
        # expires_at would pass parked_at + max_lease_s is the controller's
        # typed refusal, carried verbatim through the facade.
        clock.advance(300)
        with pytest.raises(ParkingRefusal) as caught:
            await runtime.facade.renew_park_lease(
                **_renew_kwargs(lease_s=7200, idempotency_key="kill-cap", request_id="kill-cap-req")
            )
        assert caught.value.code == PARK_LEASE_CAP_REACHED
    finally:
        await _shutdown(runtime)


async def test_an_absent_parking_block_refuses_renew_and_resume_with_the_cause() -> None:
    runtime = await _started_runtime(parking=None)
    try:
        for call in (
            runtime.facade.renew_park_lease(**_renew_kwargs()),
            runtime.facade.resume_unit(**_resume_kwargs()),
        ):
            with pytest.raises(ParkingRefusal) as caught:
                await call
            assert caught.value.code == PARK_NOT_COMMISSIONED
            assert caught.value.details == {"cause": "block_absent"}
    finally:
        await _shutdown(runtime)


async def test_park_renew_and_resume_name_an_unknown_unit_exactly() -> None:
    runtime = await _started_runtime()
    try:
        for call in (
            runtime.facade.park_unit(**_park_kwargs(unit_id="pod-ghost")),
            runtime.facade.renew_park_lease(**_renew_kwargs(unit_id="pod-ghost")),
            runtime.facade.resume_unit(**_resume_kwargs(unit_id="pod-ghost")),
        ):
            with pytest.raises(LookupError, match="^no unit with id 'pod-ghost'$"):
                await call
    finally:
        await _shutdown(runtime)


# --- resume: the takeover literal, both directions -----------------------------------


async def test_the_takeover_literal_guards_and_clears_on_the_real_facade() -> None:
    runtime = await _started_runtime()
    try:
        with pytest.raises(ValueError, match="^confirmation must be the literal 'RESUME'$"):
            await runtime.facade.resume_unit(**_resume_kwargs(confirmation="resume"))
        with pytest.raises(
            ValueError, match="^takeover acknowledgement must be the literal 'FOREIGN'$"
        ):
            await runtime.facade.resume_unit(**_resume_kwargs(takeover="MINE"))
        # The VALID acknowledged takeover: Standby with no controller lease.
        pod = runtime.simulators["mid"]
        pod.apply_debug_mode(1)
        resumed = await runtime.facade.resume_unit(**_resume_kwargs(takeover="FOREIGN"))
        assert resumed["origin"] == "foreign"
        assert resumed["prior_word"] == 1 and resumed["verified"] is True
        assert pod._debug_mode == 0
    finally:
        await _shutdown(runtime)


async def test_park_rows_carry_the_request_correlation() -> None:
    runtime = await _started_runtime()
    try:
        await runtime.facade.park_unit(**_park_kwargs())
        rows = [
            event for event in runtime.audit.recent(limit=32) if event.event_type == "unit_parked"
        ]
        assert [row.result for row in rows] == ["parked", "pending"]
        assert {row.correlation_id for row in rows} == {
            "parking:unit_parked:kill-1-request"
        }, "every park row names the facade's canonical request id"
    finally:
        await _shutdown(runtime)


# --- the dispatch refusal message, pinned exactly ------------------------------------


async def test_the_debug_mode_refusal_message_is_exact() -> None:
    clock = ManualClock()
    runtime = await _started_runtime(clock)
    try:
        await runtime.facade.park_unit(**_park_kwargs())
        clock.advance(0.5)
        await runtime.actors["mid"].poll_once()
        with pytest.raises(ValueError) as caught:
            await runtime.facade.submit_intent(
                unit_ids=["mid"],
                direction="charge",
                watts=500,
                ttl_s=30.0,
                principal=OPERATOR,
                idempotency_key="kill-dispatch",
                request_id="kill-dispatch-request",
            )
        assert str(caught.value) == "device_debug_mode_active: ['mid']"
        assert set(caught.value.details["mid"]["parked_provenance"]) == {  # type: ignore[attr-defined]
            "parked_at",
            "origin",
            "authorizer",
            "reason",
            "lease_expires_at",
        }
    finally:
        await _shutdown(runtime)
