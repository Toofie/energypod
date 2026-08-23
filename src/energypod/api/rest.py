"""Guarded REST and WebSocket inbound adapter."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator
from starlette.websockets import WebSocketDisconnect

from .idempotency import IdempotencyConflictError, IdempotencyCoordinator, StoredResult

API_PREFIX = "/api/v1"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

EVENTS_SUBPROTOCOL = "energypod-events"
# API_CONTRACTS: the browser event-stream ticket is single-use with a short
# TTL (at most 30 s) bound to one principal and to the events stream only.
_MAX_EVENT_TICKET_TTL_S: Final[float] = 30.0
_DEFAULT_EVENT_TICKET_TTL_S: Final[float] = 15.0


class Principal(Protocol):
    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


class Authenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> Principal | None: ...


class EnergyService(Protocol):
    async def snapshot(self, *, principal: Principal) -> dict[str, Any]: ...
    async def health(self, *, principal: Principal) -> dict[str, Any]: ...
    async def unit_detail(self, *, principal: Principal, unit_id: str) -> dict[str, Any]: ...
    async def recent_audit(
        self, *, principal: Principal, limit: int, cursor: int | None = None
    ) -> dict[str, Any]: ...
    async def submit_intent(self, **kwargs: Any) -> dict[str, Any]: ...
    async def cancel_intent(self, **kwargs: Any) -> dict[str, Any]: ...
    async def arm(self, **kwargs: Any) -> dict[str, Any]: ...
    async def disarm(self, **kwargs: Any) -> dict[str, Any]: ...
    async def emergency_stop(self, **kwargs: Any) -> dict[str, Any]: ...
    async def acknowledge_emergency_stop(self, **kwargs: Any) -> dict[str, Any]: ...
    async def acknowledge_inhibit(self, **kwargs: Any) -> dict[str, Any]: ...


class EventSource(Protocol):
    def subscribe(self, *, after_sequence: int | None) -> AsyncIterator[dict[str, Any]]: ...


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class IntentRequest(StrictRequest):
    unit_ids: list[str] = Field(min_length=1)
    direction: Literal["charge", "discharge"]
    watts: StrictInt = Field(gt=0)
    ttl_s: StrictFloat | StrictInt = Field(gt=0, le=300)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("unit_ids")
    @classmethod
    def validate_units(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not _valid_id(item) for item in value):
            raise ValueError("unit identifiers must be unique and canonical")
        return value

    @field_validator("ttl_s")
    @classmethod
    def finite_ttl(cls, value: float | int) -> float | int:
        if not math.isfinite(value):
            raise ValueError("ttl_s must be finite")
        return value

    @field_validator("reason")
    @classmethod
    def canonical_reason(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.strip() != value):
            raise ValueError("reason must be non-blank and canonical")
        return value


class ArmRequest(StrictRequest):
    unit_ids: list[str] = Field(min_length=1)
    confirmation: Literal["ARM"]

    @field_validator("unit_ids")
    @classmethod
    def validate_units(cls, value: list[str]) -> list[str]:
        return IntentRequest.validate_units(value)


class CancelIntentRequest(StrictRequest):
    # The exact intent id or the literal "current" for the newest active one.
    intent_id: str = Field(min_length=1, max_length=128)

    @field_validator("intent_id")
    @classmethod
    def canonical_intent_id(cls, value: str) -> str:
        if value != "current" and not _valid_id(value):
            raise ValueError("intent_id must be canonical or the literal 'current'")
        return value


class DisarmRequest(StrictRequest):
    # Disarming is safety-positive: no confirmation literal is demanded, so
    # automation may always drive selected units back to the safe state.
    unit_ids: list[str] = Field(min_length=1)

    @field_validator("unit_ids")
    @classmethod
    def validate_units(cls, value: list[str]) -> list[str]:
        return IntentRequest.validate_units(value)


class StopRequest(StrictRequest):
    unit_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("unit_ids")
    @classmethod
    def validate_units(cls, value: list[str]) -> list[str]:
        return IntentRequest.validate_units(value)

    @field_validator("reason")
    @classmethod
    def canonical_reason(cls, value: str) -> str:
        validated = IntentRequest.canonical_reason(value)
        assert validated is not None
        return validated


class AcknowledgeRequest(StrictRequest):
    confirmation: Literal["ACKNOWLEDGE"]


class BoundaryError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ):
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _valid_id(value: str) -> bool:
    return bool(re.fullmatch(_ID_PATTERN, value))


def _request_id(raw: str | None) -> str:
    if raw and _valid_id(raw):
        return raw
    return str(uuid.uuid4())


def _header_values(scope: Mapping[str, Any], name: bytes) -> list[str]:
    values: list[str] = []
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != name:
            continue
        try:
            values.append(raw_value.decode("latin-1"))
        except UnicodeDecodeError:
            values.append("")
    return values


def _single_header(scope: Mapping[str, Any], name: bytes) -> str | None:
    values = _header_values(scope, name)
    if not values:
        return None
    if len(values) != 1 or any(char in values[0] for char in "\r\n\x00"):
        raise BoundaryError(400, "malformed_header", "A security-sensitive header is malformed")
    return values[0]


def _validated_principal(identity: Principal | None) -> Principal:
    if identity is None:
        raise BoundaryError(401, "authentication_required", "Bearer authentication is required")
    subject = getattr(identity, "subject", None)
    site_id = getattr(identity, "site_id", None)
    scopes = getattr(identity, "scopes", None)
    interactive = getattr(identity, "interactive", None)
    if (
        not isinstance(subject, str)
        or not _valid_id(subject)
        or not isinstance(site_id, str)
        or not _valid_id(site_id)
        or not isinstance(scopes, frozenset)
        or any(not isinstance(scope, str) or not _valid_id(scope) for scope in scopes)
        or type(interactive) is not bool
    ):
        raise BoundaryError(401, "authentication_required", "Bearer authentication is required")
    return identity


def _error_body(error: BoundaryError, request_id: str) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "details": error.details,
        "request_id": request_id,
    }


def _error_response(error: BoundaryError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content=_error_body(error, request_id),
        headers={"X-Request-ID": request_id},
    )


@dataclass(frozen=True)
class _EventTicket:
    """One single-use handshake credential bound to a principal."""

    principal: Principal
    expires_at_mono: float


def _issue_event_ticket(
    tickets: OrderedDict[str, _EventTicket],
    lock: Lock,
    clock: Callable[[], float],
    ttl_s: float,
    principal: Principal,
) -> str:
    """Mint one opaque ticket; expired leftovers are purged so the store stays bounded."""
    now = float(clock())
    ticket = secrets.token_urlsafe(32)
    with lock:
        for stale in [key for key, record in tickets.items() if record.expires_at_mono <= now]:
            del tickets[stale]
        tickets[ticket] = _EventTicket(principal=principal, expires_at_mono=now + ttl_s)
    return ticket


def _consume_event_ticket(
    ticket: str,
    tickets: dict[str, _EventTicket],
    lock: Lock,
    clock: Callable[[], float],
) -> Principal:
    """Redeem a ticket exactly once; an unknown, consumed, or expired ticket is no credential."""
    now = float(clock())
    with lock:
        record = tickets.pop(ticket, None)
    if record is None or now >= record.expires_at_mono:
        raise BoundaryError(
            401, "authentication_required", "A valid event-stream ticket is required"
        )
    return _validated_principal(record.principal)


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    offered = websocket.scope.get("subprotocols") or ()
    if not isinstance(offered, list | tuple):
        return []
    return [item for item in offered if isinstance(item, str)]


def _event_ticket_candidate(offered: list[str]) -> str | None:
    """Extract the ticket from the documented ``energypod-events, <ticket>`` offer.

    Only the exact two-token convention is the ticket channel: stray protocols
    from non-browser clients never masquerade as credentials, and an offer
    carrying more than one candidate is malformed rather than ambiguous.
    """
    if EVENTS_SUBPROTOCOL not in offered:
        return None
    candidates = [item for item in offered if item != EVENTS_SUBPROTOCOL]
    if len(candidates) > 1:
        raise BoundaryError(400, "malformed_subprotocol", "The event-stream handshake is malformed")
    return candidates[0] if candidates else None


async def _authenticate_events_handshake(
    websocket: WebSocket,
    authenticator: Authenticator,
    tickets: OrderedDict[str, _EventTicket],
    ticket_lock: Lock,
    ticket_clock: Callable[[], float],
) -> tuple[Principal, bool]:
    """Authenticate by single-use ticket or Authorization header.

    Returns the principal plus whether the ``energypod-events`` subprotocol
    was offered and must be negotiated at accept.
    """
    offered = _offered_subprotocols(websocket)
    ticket = _event_ticket_candidate(offered)
    if ticket is not None:
        return _consume_event_ticket(ticket, tickets, ticket_lock, ticket_clock), True
    identity = await _authenticate_header(
        _single_header(websocket.scope, b"authorization"), authenticator
    )
    return identity, EVENTS_SUBPROTOCOL in offered


def create_api_app(
    *,
    service: EnergyService,
    authenticator: Authenticator,
    event_source: EventSource,
    auth_required: bool = True,
    websocket_queue_capacity: int = 128,
    trusted_websocket_origins: frozenset[str] | None = None,
    idempotency_capacity: int = 4096,
    event_ticket_ttl_s: float = _DEFAULT_EVENT_TICKET_TTL_S,
    event_ticket_clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Create an isolated API adapter with no global mutable state."""
    if not auth_required:
        raise ValueError("the service API cannot be created without authentication")
    if websocket_queue_capacity < 1:
        raise ValueError("websocket_queue_capacity must be positive")
    if isinstance(idempotency_capacity, bool) or idempotency_capacity < 1:
        raise ValueError("idempotency_capacity must be positive")
    if trusted_websocket_origins is not None:
        _validate_configured_origins(trusted_websocket_origins)
    if (
        isinstance(event_ticket_ttl_s, bool)
        or not isinstance(event_ticket_ttl_s, int | float)
        or not math.isfinite(float(event_ticket_ttl_s))
        or not 0.0 < float(event_ticket_ttl_s) <= _MAX_EVENT_TICKET_TTL_S
    ):
        raise ValueError("event_ticket_ttl_s must be positive and at most 30 seconds")

    app = FastAPI(title="EnergyPod guarded API", version="1.0.0")
    idempotency = IdempotencyCoordinator(max_completed_entries=idempotency_capacity)
    known_stops: OrderedDict[str, None] = OrderedDict()
    claimed_stops: set[str] = set()
    stop_lock = asyncio.Lock()
    ticket_clock: Callable[[], float] = (
        event_ticket_clock if event_ticket_clock is not None else time.monotonic
    )
    event_tickets: OrderedDict[str, _EventTicket] = OrderedDict()
    ticket_lock = Lock()

    @app.middleware("http")
    async def correlate(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        try:
            supplied_request_id = _single_header(request.scope, b"x-request-id")
        except BoundaryError:
            supplied_request_id = None
        request.state.request_id = _request_id(supplied_request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(BoundaryError)
    async def boundary_error(request: Request, exc: BoundaryError) -> JSONResponse:
        return _error_response(exc, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return _error_response(
            BoundaryError(
                422,
                "validation_error",
                "Request validation failed",
                {"errors": details},
            ),
            request.state.request_id,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(
            BoundaryError(500, "internal_error", "The request could not be completed"),
            request.state.request_id,
        )

    async def principal(request: Request) -> Principal:
        header = _single_header(request.scope, b"authorization")
        return await _authenticate_header(header, authenticator)

    principal_dependency = Depends(principal)

    def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
        async def dependency(identity: Principal = principal_dependency) -> Principal:
            if scope not in identity.scopes:
                raise BoundaryError(403, "insufficient_scope", "The credential lacks permission")
            return identity

        return dependency

    def require_scopes(*scopes: str) -> Callable[..., Awaitable[Principal]]:
        async def dependency(identity: Principal = principal_dependency) -> Principal:
            if any(scope not in identity.scopes for scope in scopes):
                raise BoundaryError(403, "insufficient_scope", "The credential lacks permission")
            return identity

        return dependency

    observe_dependency = Depends(require_scope("observe"))
    # Audit reads require the baseline observe scope in addition to audit:read.
    audit_dependency = Depends(require_scopes("observe", "audit:read"))
    dispatch_dependency = Depends(require_scope("dispatch"))
    arm_dependency = Depends(require_scope("arm"))
    stop_dependency = Depends(require_scope("stop"))
    stop_ack_dependency = Depends(require_scope("stop:acknowledge"))

    async def mutation(
        *,
        request: Request,
        identity: Principal,
        operation_name: str,
        payload: Mapping[str, Any],
        status_code: int,
        invoke: Callable[[], Awaitable[dict[str, Any]]],
    ) -> StoredResult:
        key = _single_header(request.scope, b"idempotency-key")
        if not key or not _valid_id(key):
            raise BoundaryError(
                400,
                "idempotency_key_required",
                "A valid Idempotency-Key is required",
            )
        canonical = {"operation": operation_name, **payload}
        try:
            return await idempotency.execute(
                principal_subject=identity.subject,
                key=key,
                payload=canonical,
                operation=lambda: _invoke(status_code, invoke),
            )
        except IdempotencyConflictError as exc:
            raise BoundaryError(
                409,
                "idempotency_conflict",
                "The key is bound to another request",
            ) from exc

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        """Liveness only (API_CONTRACTS "Operations surface").

        The one unauthenticated endpoint: the process answers, nothing more —
        no readiness, no data, no credential consultation — so container
        orchestration can probe process-up while every operational view stays
        behind bearer authentication at ``/api/v1/health``.
        """
        return {"ok": True}

    @app.get(f"{API_PREFIX}/snapshot")
    async def get_snapshot(identity: Principal = observe_dependency) -> Any:
        return await service.snapshot(principal=identity)

    @app.get(f"{API_PREFIX}/health")
    async def get_health(identity: Principal = observe_dependency) -> Any:
        return await service.health(principal=identity)

    @app.get(f"{API_PREFIX}/audit")
    async def get_audit(
        limit: int = Query(default=100, ge=1, le=500),
        after_sequence: int | None = Query(default=None, ge=0),
        identity: Principal = audit_dependency,
    ) -> Any:
        # ``after_sequence`` is the previous page's oldest-delivered cursor; the
        # facade derives the next cursor from what the store returned.
        return await service.recent_audit(principal=identity, limit=limit, cursor=after_sequence)

    @app.get(f"{API_PREFIX}/units/{{unit_id}}")
    async def get_unit_detail(unit_id: str, identity: Principal = observe_dependency) -> Any:
        """Full latest-observation projection for one unit (observe scope)."""
        if not _valid_id(unit_id):
            raise BoundaryError(422, "validation_error", "Invalid unit identifier")
        try:
            return await service.unit_detail(principal=identity, unit_id=unit_id)
        except LookupError as exc:
            raise BoundaryError(404, "unit_not_found", "The unit identifier is not known") from exc

    @app.post(f"{API_PREFIX}/intents", status_code=202)
    async def submit_intent(
        body: IntentRequest,
        request: Request,
        identity: Principal = dispatch_dependency,
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")
        result = await mutation(
            request=request,
            identity=identity,
            operation_name="submit_intent",
            payload=payload,
            status_code=202,
            invoke=lambda: service.submit_intent(
                **payload,
                principal=identity,
                idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                request_id=request.state.request_id,
            ),
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/intents/cancel")
    async def cancel_intent(
        body: CancelIntentRequest,
        request: Request,
        identity: Principal = dispatch_dependency,
    ) -> JSONResponse:
        # Cancelling an intent stops power: like disarm, the safety-positive
        # direction needs the dispatch scope but NO interactive human
        # principal, so automation may always reach the safe state.
        payload = body.model_dump(mode="json")

        async def invoke() -> dict[str, Any]:
            try:
                return await service.cancel_intent(
                    intent_id=payload["intent_id"],
                    principal=identity,
                    idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                    request_id=request.state.request_id,
                )
            except LookupError as exc:
                raise BoundaryError(
                    404, "intent_not_found", "The intent identifier is not active"
                ) from exc
            except ValueError as exc:
                raise BoundaryError(409, "intent_not_cancelable", str(exc)) from exc

        result = await mutation(
            request=request,
            identity=identity,
            operation_name="cancel_intent",
            payload=payload,
            status_code=200,
            invoke=invoke,
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/arm")
    async def arm(
        body: ArmRequest,
        request: Request,
        identity: Principal = arm_dependency,
    ) -> JSONResponse:
        if not identity.interactive:
            raise BoundaryError(
                403,
                "interactive_operator_required",
                "Interactive operator required",
            )
        payload = body.model_dump(mode="json")
        result = await mutation(
            request=request,
            identity=identity,
            operation_name="arm",
            payload=payload,
            status_code=200,
            invoke=lambda: service.arm(
                unit_ids=payload["unit_ids"],
                principal=identity,
                idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                request_id=request.state.request_id,
            ),
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/disarm")
    async def disarm(
        body: DisarmRequest,
        request: Request,
        identity: Principal = arm_dependency,
    ) -> JSONResponse:
        # Disarming is safety-positive: the arm scope applies, but unlike
        # arming no interactive human principal is required to reach the
        # safe state, so automation may always disarm.
        payload = body.model_dump(mode="json")
        result = await mutation(
            request=request,
            identity=identity,
            operation_name="disarm",
            payload=payload,
            status_code=200,
            invoke=lambda: service.disarm(
                unit_ids=payload["unit_ids"],
                principal=identity,
                idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                request_id=request.state.request_id,
            ),
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/emergency-stop", status_code=202)
    async def emergency_stop(
        body: StopRequest,
        request: Request,
        identity: Principal = stop_dependency,
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")

        async def invoke() -> dict[str, Any]:
            try:
                result = await service.emergency_stop(
                    **payload,
                    principal=identity,
                    idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                    request_id=request.state.request_id,
                )
            except BaseException as exc:
                # A degraded stop (store refused, unknown units) may still have
                # latched known units; register its stop id so the exact-id
                # acknowledgement endpoint stays usable for recovery.
                degraded_stop_id = getattr(exc, "stop_id", None)
                if isinstance(degraded_stop_id, str) and _valid_id(degraded_stop_id):
                    async with stop_lock:
                        known_stops[degraded_stop_id] = None
                raise
            stop_id = result.get("stop_id")
            if isinstance(stop_id, str) and _valid_id(stop_id):
                # Latched stops are safety-critical state, not a replay cache:
                # they must stay acknowledgeable until the domain removes them.
                # The set is bounded by concurrently latched stops because
                # acknowledgement pops the entry.
                async with stop_lock:
                    known_stops[stop_id] = None
            else:
                raise BoundaryError(502, "invalid_service_response", "Invalid stop response")
            return result

        result = await mutation(
            request=request,
            identity=identity,
            operation_name="emergency_stop",
            payload=payload,
            status_code=202,
            invoke=invoke,
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/emergency-stop/{{stop_id}}/acknowledge")
    async def acknowledge_stop(
        stop_id: str,
        body: AcknowledgeRequest,
        request: Request,
        identity: Principal = stop_ack_dependency,
    ) -> JSONResponse:
        if not _valid_id(stop_id):
            raise BoundaryError(422, "validation_error", "Invalid stop identifier")
        payload = {"stop_id": stop_id, **body.model_dump(mode="json")}

        async def invoke() -> dict[str, Any]:
            # This claim belongs inside the idempotency-owned operation. An
            # identical request then joins or replays that operation without
            # revalidating state that the leader has intentionally consumed.
            async with stop_lock:
                if stop_id not in known_stops or stop_id in claimed_stops:
                    raise BoundaryError(
                        409,
                        "stop_id_mismatch",
                        "The stop identifier is not active",
                    )
                claimed_stops.add(stop_id)
            try:
                result = await service.acknowledge_emergency_stop(
                    stop_id=stop_id,
                    principal=identity,
                    idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                    request_id=request.state.request_id,
                )
            except BaseException:
                async with stop_lock:
                    claimed_stops.discard(stop_id)
                raise
            async with stop_lock:
                claimed_stops.discard(stop_id)
                known_stops.pop(stop_id, None)
            return result

        result = await mutation(
            request=request,
            identity=identity,
            operation_name="acknowledge_emergency_stop",
            payload=payload,
            status_code=200,
            invoke=invoke,
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/units/{{unit_id}}/inhibit/acknowledge")
    async def acknowledge_inhibit(
        unit_id: str,
        body: AcknowledgeRequest,
        request: Request,
        identity: Principal = arm_dependency,
    ) -> JSONResponse:
        if not identity.interactive:
            raise BoundaryError(
                403,
                "interactive_operator_required",
                "Interactive operator required",
            )
        if not _valid_id(unit_id):
            raise BoundaryError(422, "validation_error", "Invalid unit identifier")
        payload = {"unit_id": unit_id, **body.model_dump(mode="json")}

        async def invoke() -> dict[str, Any]:
            try:
                return await service.acknowledge_inhibit(
                    unit_id=unit_id,
                    principal=identity,
                    idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                    request_id=request.state.request_id,
                )
            except LookupError as exc:
                raise BoundaryError(
                    404, "unit_not_found", "The unit identifier is not known"
                ) from exc

        result = await mutation(
            request=request,
            identity=identity,
            operation_name="acknowledge_inhibit",
            payload=payload,
            status_code=200,
            invoke=invoke,
        )
        return JSONResponse(status_code=result.status_code, content=dict(result.body))

    @app.post(f"{API_PREFIX}/events/session")
    async def create_events_session(
        identity: Principal = observe_dependency,
    ) -> dict[str, Any]:
        # Browsers cannot set an Authorization header on a WebSocket, so this
        # mints one single-use, short-lived ticket bound to this principal and
        # to the events stream only; it is a credential for nothing else.
        ticket = _issue_event_ticket(
            event_tickets, ticket_lock, ticket_clock, event_ticket_ttl_s, identity
        )
        return {"ticket": ticket, "expires_in_s": event_ticket_ttl_s}

    @app.websocket(f"{API_PREFIX}/events")
    async def events(websocket: WebSocket, after: int | None = Query(default=None, ge=0)) -> None:
        try:
            _reject_websocket_query_credentials(websocket)
            identity, offered_events_subprotocol = await _authenticate_events_handshake(
                websocket, authenticator, event_tickets, ticket_lock, ticket_clock
            )
            if "observe" not in identity.scopes:
                raise BoundaryError(403, "insufficient_scope", "The credential lacks permission")
            _validate_websocket_origin(websocket, trusted_websocket_origins)
        except BoundaryError as exc:
            raise WebSocketException(code=4401 if exc.status == 401 else 1008) from exc

        await websocket.accept(
            subprotocol=EVENTS_SUBPROTOCOL if offered_events_subprotocol else None
        )
        request_id = _request_id(_single_header(websocket.scope, b"x-request-id"))
        try:
            snapshot = await service.snapshot(principal=identity)
            sequence = snapshot.get("snapshot_sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                await websocket.send_json({"type": "resync_required", "reason": "invalid_snapshot"})
                await websocket.close(code=1011)
                return
            envelope: dict[str, Any] = {"type": "snapshot", "sequence": sequence, "data": snapshot}
            if after is not None and after != sequence:
                envelope.update(recovery="cursor_gap", requested_after=after)
            await websocket.send_json(envelope)
        except WebSocketDisconnect:
            return
        except Exception:
            # A failure after accept must still honor the error contract and
            # give the client a clean, informative close instead of a dead socket.
            with suppress(Exception):
                await websocket.send_json(
                    {
                        "type": "error",
                        **_error_body(
                            BoundaryError(500, "internal_error", "The event stream failed"),
                            request_id,
                        ),
                    }
                )
                await websocket.close(code=1011)
            return
        await _stream_events(websocket, event_source, sequence, websocket_queue_capacity)

    return app


async def _authenticate_header(header: str | None, authenticator: Authenticator) -> Principal:
    if not header or not header.startswith("Bearer ") or header.count(" ") != 1:
        raise BoundaryError(401, "authentication_required", "Bearer authentication is required")
    token = header[7:]
    if not token or token.strip() != token:
        raise BoundaryError(401, "authentication_required", "Bearer authentication is required")
    return _validated_principal(await authenticator.authenticate(token))


async def _invoke(
    status_code: int, invoke: Callable[[], Awaitable[dict[str, Any]]]
) -> StoredResult:
    return StoredResult(status_code=status_code, body=await invoke())


def _validate_websocket_origin(websocket: WebSocket, configured: frozenset[str] | None) -> None:
    origin = _single_header(websocket.scope, b"origin")
    if origin is None:
        return
    if configured is not None:
        trusted = origin in configured
    else:
        origin_parts = urlsplit(origin)
        request_host = _single_header(websocket.scope, b"host") or ""
        trusted = (
            origin_parts.scheme in {"http", "https"}
            and origin_parts.netloc == request_host
            and not origin_parts.path
            and not origin_parts.query
            and not origin_parts.fragment
        )
    if not trusted:
        raise BoundaryError(403, "untrusted_origin", "WebSocket origin is not trusted")


def _validate_configured_origins(origins: frozenset[str]) -> None:
    for origin in origins:
        parts = urlsplit(origin)
        if (
            not isinstance(origin, str)
            or parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or origin != f"{parts.scheme}://{parts.netloc}"
        ):
            raise ValueError("trusted WebSocket origins must be canonical HTTP origins")


def _reject_websocket_query_credentials(websocket: WebSocket) -> None:
    forbidden = {
        "access_token",
        "authorization",
        "bearer",
        "credential",
        "id_token",
        "token",
    }
    if any(key.casefold() in forbidden for key in websocket.query_params):
        raise BoundaryError(401, "authentication_required", "Bearer authentication is required")


def _is_sequence_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_discontinuity_marker(event: Mapping[str, Any]) -> bool:
    """A source-side discontinuity marker (the bus's resync) carries no sequence."""
    if "sequence" in event:
        return False
    return event.get("resync") is True or event.get("type") == "resync"


async def _stream_events(
    websocket: WebSocket, source: EventSource, sequence: int, capacity: int
) -> None:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=capacity)

    async def produce() -> None:
        expected = sequence + 1
        subscription = source.subscribe(after_sequence=sequence)

        async def terminate(reason: str, snapshot_sequence: int | None = None) -> None:
            # Preserve already accepted contiguous events, then make the
            # discontinuity explicit. The bounded queue applies backpressure
            # while the terminal marker waits for one slot.
            marker: dict[str, Any] = {"type": "resync_required", "reason": reason}
            if snapshot_sequence is not None:
                marker["snapshot_sequence"] = snapshot_sequence
            await queue.put(marker)

        try:
            async for event in subscription:
                if _is_discontinuity_marker(event):
                    # A source marker is first-class: its reason is the
                    # operator's diagnosis (a slow consumer is not stream
                    # corruption) and its snapshot point is the client's
                    # recovery cursor, so both must arrive unmangled rather
                    # than being relabeled as a source-integrity violation.
                    reason = event.get("reason")
                    snapshot = event.get("snapshot_sequence")
                    await terminate(
                        reason if isinstance(reason, str) and reason else "non_monotonic_event",
                        snapshot if _is_sequence_int(snapshot) else None,
                    )
                    return
                current = event.get("sequence")
                if (
                    not isinstance(current, int)
                    or isinstance(current, bool)
                    or current <= expected - 1
                ):
                    await terminate("non_monotonic_event")
                    return
                if current != expected:
                    await terminate("sequence_gap")
                    return
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    await terminate("backpressure")
                    return
                expected += 1
        finally:
            close = getattr(subscription, "aclose", None)
            if close is not None:
                await close()
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
            else:
                await queue.put(None)

    producer = asyncio.create_task(produce())
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            await websocket.send_json(item)
            if item.get("type") == "resync_required":
                return
    except WebSocketDisconnect:
        return
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
