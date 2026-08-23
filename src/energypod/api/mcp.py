"""FastMCP adapter exposing a deliberately narrow application facade."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator

from .idempotency import IdempotencyConflictError, IdempotencyCoordinator, StoredResult


class Principal(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def scopes(self) -> frozenset[str]: ...

    @property
    def interactive(self) -> bool: ...

    @property
    def site_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """Validated identity snapshot bound to one MCP server instance."""

    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


class EnergyService(Protocol):
    async def snapshot(self, *, principal: Principal) -> dict[str, Any]: ...
    async def health(self, *, principal: Principal) -> dict[str, Any]: ...
    async def recent_audit(self, *, principal: Principal, limit: int) -> dict[str, Any]: ...
    async def get_energy_days(self, **kwargs: Any) -> dict[str, Any]: ...
    async def get_plant_history(self, **kwargs: Any) -> dict[str, Any]: ...
    async def submit_intent(self, **kwargs: Any) -> dict[str, Any]: ...


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    unit_ids: tuple[str, ...] = Field(min_length=1)
    direction: Literal["charge", "discharge"]
    watts: StrictInt = Field(gt=0)
    ttl_s: StrictFloat | StrictInt = Field(gt=0)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("unit_ids")
    @classmethod
    def units_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        pattern = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
        invalid = any(re.fullmatch(pattern, item) is None for item in value)
        if len(set(value)) != len(value) or invalid:
            raise ValueError("unit identifiers must be unique and canonical")
        return value

    @field_validator("ttl_s")
    @classmethod
    def ttl_is_finite(cls, value: float | int) -> float | int:
        if not math.isfinite(value):
            raise ValueError("ttl_s must be finite")
        return value

    @field_validator("reason")
    @classmethod
    def reason_is_canonical(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.strip() != value):
            raise ValueError("reason must be non-blank and canonical")
        return value


def create_mcp_server(
    *,
    service: EnergyService,
    principal: Principal,
    mutations_enabled: bool = False,
    max_dispatch_ttl_s: float = 30.0,
    idempotency_capacity: int = 4096,
) -> FastMCP:
    """Create an MCP server bound to an already authenticated session identity."""
    if type(mutations_enabled) is not bool:
        raise ValueError("mutations_enabled must be a boolean")
    if (
        isinstance(max_dispatch_ttl_s, bool)
        or not math.isfinite(max_dispatch_ttl_s)
        or not 0 < max_dispatch_ttl_s <= 300
    ):
        raise ValueError("max_dispatch_ttl_s must be finite and between 0 and 300 seconds")
    if isinstance(idempotency_capacity, bool) or idempotency_capacity < 1:
        raise ValueError("idempotency_capacity must be positive")
    session_principal = _snapshot_principal(principal)

    server = FastMCP("EnergyPod")
    idempotency = IdempotencyCoordinator(max_completed_entries=idempotency_capacity)

    @server.tool
    async def get_snapshot() -> dict[str, Any]:
        """Return the current authoritative fleet snapshot."""
        _require(session_principal, "observe")
        return await service.snapshot(principal=session_principal)

    @server.tool
    async def get_health() -> dict[str, Any]:
        """Return liveness, service readiness, and control readiness."""
        _require(session_principal, "observe")
        return await service.health(principal=session_principal)

    @server.tool
    async def get_energy_days(limit: StrictInt = 8) -> dict[str, Any]:
        """Return the rolled daily energy records, newest last (read-only)."""
        # API_CONTRACTS "Energy scorecard": a read-only ride-along tool -- no
        # MCP surface can touch sources or roles.
        _require(session_principal, "observe")
        if isinstance(limit, bool) or not 1 <= limit <= 31:
            raise ValueError("limit must be between 1 and 31")
        return await service.get_energy_days(principal=session_principal, limit=limit)

    @server.tool
    async def get_plant_history(
        from_: Annotated[str, Field(alias="from")],
        to: str,
        unit_ids: tuple[str, ...] | None = None,
        fields: tuple[str, ...] | None = None,
        points: StrictInt = 600,
    ) -> dict[str, Any]:
        """Return windowed, server-downsampled plant history (read-only).

        ``from``/``to`` are ISO-8601 timestamps with explicit offsets;
        ``unit_ids``/``fields`` narrow the response; ``points`` (50..2000) is
        the per-series downsample target.  Answers 409-shaped errors when the
        site did not commission the ``plant_history`` block.
        """
        # API_CONTRACTS "Plant history": a read-only ride-along tool -- the
        # historian is observability only and no MCP surface mutates it.
        _require(session_principal, "observe")
        if isinstance(points, bool) or not 50 <= points <= 2000:
            raise ValueError("points must be between 50 and 2000")
        return await service.get_plant_history(
            principal=session_principal,
            range_from=from_,
            range_to=to,
            unit_ids=None if unit_ids is None else list(unit_ids),
            fields=None if fields is None else list(fields),
            points=points,
        )

    @server.tool
    async def get_recent_audit(limit: StrictInt = 100) -> dict[str, Any]:
        """Return a bounded recent audit view."""
        # Audit reads require the baseline observe scope in addition to audit:read.
        _require(session_principal, "observe")
        _require(session_principal, "audit:read")
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return await service.recent_audit(principal=session_principal, limit=limit)

    if mutations_enabled and "dispatch" in session_principal.scopes:

        @server.tool
        async def dispatch_intent(
            unit_ids: tuple[str, ...],
            direction: Literal["charge", "discharge"],
            watts: StrictInt,
            ttl_s: StrictFloat | StrictInt,
            idempotency_key: str,
            request_id: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """Submit one bounded, expiring power intent through normal safety controls."""
            request = DispatchRequest(
                unit_ids=unit_ids,
                direction=direction,
                watts=watts,
                ttl_s=ttl_s,
                idempotency_key=idempotency_key,
                request_id=request_id,
                reason=reason,
            )
            if request.ttl_s > max_dispatch_ttl_s:
                raise ValueError("ttl_s exceeds the configured MCP dispatch limit")
            payload = request.model_dump(mode="json")

            async def invoke() -> StoredResult:
                result = await service.submit_intent(
                    unit_ids=payload["unit_ids"],
                    direction=payload["direction"],
                    watts=payload["watts"],
                    ttl_s=payload["ttl_s"],
                    reason=payload["reason"],
                    principal=session_principal,
                    idempotency_key=request.idempotency_key,
                    request_id=request.request_id,
                )
                return StoredResult(status_code=202, body=result)

            try:
                operation_payload = {
                    key: value for key, value in payload.items() if key != "request_id"
                }
                stored = await idempotency.execute(
                    principal_subject=session_principal.subject,
                    key=request.idempotency_key,
                    payload={"operation": "submit_intent", **operation_payload},
                    operation=invoke,
                )
            except IdempotencyConflictError as exc:
                raise ValueError("idempotency key conflicts with another request") from exc
            return dict(stored.body)

    return server


def _require(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise PermissionError("the MCP session lacks the required scope")


def _snapshot_principal(principal: Principal) -> SessionPrincipal:
    subject = getattr(principal, "subject", None)
    scopes = getattr(principal, "scopes", None)
    site_id = getattr(principal, "site_id", None)
    interactive = getattr(principal, "interactive", None)
    pattern = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    if (
        not isinstance(subject, str)
        or re.fullmatch(pattern, subject) is None
        or not isinstance(site_id, str)
        or re.fullmatch(pattern, site_id) is None
        or not isinstance(scopes, frozenset)
        or any(
            not isinstance(scope, str) or re.fullmatch(pattern, scope) is None for scope in scopes
        )
        or type(interactive) is not bool
    ):
        raise ValueError("principal is not a valid authenticated MCP session identity")
    return SessionPrincipal(subject, scopes, interactive, site_id)
