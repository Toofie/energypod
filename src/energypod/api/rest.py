"""Guarded REST and WebSocket inbound adapter."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator
from starlette.websockets import WebSocketDisconnect

from .idempotency import IdempotencyConflictError, IdempotencyCoordinator, StoredResult

API_PREFIX = "/api/v1"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


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
    async def recent_audit(self, *, principal: Principal, limit: int) -> dict[str, Any]: ...
    async def submit_intent(self, **kwargs: Any) -> dict[str, Any]: ...
    async def arm(self, **kwargs: Any) -> dict[str, Any]: ...
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


def create_api_app(
    *,
    service: EnergyService,
    authenticator: Authenticator,
    event_source: EventSource,
    auth_required: bool = True,
    websocket_queue_capacity: int = 128,
    trusted_websocket_origins: frozenset[str] | None = None,
    idempotency_capacity: int = 4096,
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

    app = FastAPI(title="EnergyPod guarded API", version="1.0.0")
    idempotency = IdempotencyCoordinator(max_completed_entries=idempotency_capacity)
    known_stops: OrderedDict[str, None] = OrderedDict()
    claimed_stops: set[str] = set()
    stop_lock = asyncio.Lock()

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

    @app.get(f"{API_PREFIX}/snapshot")
    async def get_snapshot(identity: Principal = observe_dependency) -> Any:
        return await service.snapshot(principal=identity)

    @app.get(f"{API_PREFIX}/health")
    async def get_health(identity: Principal = observe_dependency) -> Any:
        return await service.health(principal=identity)

    @app.get(f"{API_PREFIX}/audit")
    async def get_audit(
        limit: int = Query(default=100, ge=1, le=500),
        identity: Principal = audit_dependency,
    ) -> Any:
        return await service.recent_audit(principal=identity, limit=limit)

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

    @app.post(f"{API_PREFIX}/emergency-stop", status_code=202)
    async def emergency_stop(
        body: StopRequest,
        request: Request,
        identity: Principal = stop_dependency,
    ) -> JSONResponse:
        payload = body.model_dump(mode="json")

        async def invoke() -> dict[str, Any]:
            result = await service.emergency_stop(
                **payload,
                principal=identity,
                idempotency_key=cast(str, _single_header(request.scope, b"idempotency-key")),
                request_id=request.state.request_id,
            )
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

    @app.websocket(f"{API_PREFIX}/events")
    async def events(websocket: WebSocket, after: int | None = Query(default=None, ge=0)) -> None:
        try:
            _reject_websocket_query_credentials(websocket)
            identity = await _authenticate_header(
                _single_header(websocket.scope, b"authorization"), authenticator
            )
            if "observe" not in identity.scopes:
                raise BoundaryError(403, "insufficient_scope", "The credential lacks permission")
            _validate_websocket_origin(websocket, trusted_websocket_origins)
        except BoundaryError as exc:
            raise WebSocketException(code=4401 if exc.status == 401 else 1008) from exc

        await websocket.accept()
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


async def _stream_events(
    websocket: WebSocket, source: EventSource, sequence: int, capacity: int
) -> None:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=capacity)

    async def produce() -> None:
        expected = sequence + 1
        subscription = source.subscribe(after_sequence=sequence)

        async def terminate(reason: str) -> None:
            # Preserve already accepted contiguous events, then make the
            # discontinuity explicit. The bounded queue applies backpressure
            # while the terminal marker waits for one slot.
            await queue.put({"type": "resync_required", "reason": reason})

        try:
            async for event in subscription:
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
