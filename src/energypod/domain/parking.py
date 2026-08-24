"""The pod-parking lease record and the vendor debug-mode vocabulary.

DESIGN_POD_PARKING (2026-08-24, CONTRACT v2): parking writes the vendor
debug-mode register ``0x8000 <- 1`` (Standby) through the named
``write_debug_mode`` transport path; resuming writes ``0x8000 <- 0`` (Normal).
The lease is the expiry ALARM, not the expiry actor: a lease ends only by a
verified resume write, an observed foreign resume, or expiry (alarm-only, no
write, ever -- including at boot).  Under any other ending the lease persists
in the terminal sub-state ``write_unverified`` (our acts stay ours when they
fail; a failed write never reclassifies a controller-minted lease as foreign).

The durable row is the machine truth (DESIGN section 4); the audit row is the
human-correlated narrative.  This module owns only the pure record -- no I/O,
no clock, no transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

# The vendor's own names for the debug-mode word (GlobalFun.cs:152-165, read
# through the decompiled MiniES app -- VENDOR_RE code is reference truth).
# Values 2-6 (Charge, Discharge, Circulation, Fixing SOC, Verify Capacity) are
# PERMANENTLY UNEXPOSED: unreachable in code (transport-layer {0, 1}
# validation), refused on approach (``park_mode_out_of_scope`` carries the
# vendor name so the operator knows WHICH mode they are looking at), and never
# normalized by any surface.
VENDOR_DEBUG_MODE_NAMES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0: "Normal Mode",
        1: "Standby",
        2: "Charge",
        3: "Discharge",
        4: "Circulation",
        5: "Fixing SOC",
        6: "Verify Capacity",
    }
)


def vendor_debug_mode_name(word: int) -> str | None:
    """The vendor's own name for a debug-mode word, or ``None`` off the map.

    The whitelist for WRITES is ``{0, 1}`` and nothing else, ever; the name map
    is read-side vocabulary only, so an off-map word (a register we did not
    decode, or a vendor value beyond 6) names itself honestly instead of being
    coerced onto the map.
    """
    return VENDOR_DEBUG_MODE_NAMES.get(int(word))


# The lease lifecycle states the durable row carries (DESIGN sections 1/3/4).
# ``open`` and ``expired`` are both PARKED (expiry is the alarm state, still
# parked, still operator-resumable); ``closed_operator`` and ``closed_foreign``
# are terminal; ``write_unverified`` is the terminal sub-state a lease takes
# when OUR resume write could not be verified -- parked stays true and the
# health reason ``park_write_unverified`` names the operator resume.
LEASE_OPEN: Final[str] = "open"
LEASE_EXPIRED: Final[str] = "expired"
LEASE_CLOSED_OPERATOR: Final[str] = "closed_operator"
LEASE_CLOSED_FOREIGN: Final[str] = "closed_foreign"
LEASE_WRITE_UNVERIFIED: Final[str] = "write_unverified"

_PARKED_LEASE_STATES: Final[frozenset[str]] = frozenset(
    {LEASE_OPEN, LEASE_EXPIRED, LEASE_WRITE_UNVERIFIED}
)


def lease_is_parked(state: str) -> bool:
    """Whether a lease state still holds the unit parked (dispatch-refused)."""
    return state in _PARKED_LEASE_STATES


def lease_is_open_for_renewal(state: str) -> bool:
    """Whether a lease state may still be renewed (anti-rollover's domain).

    An expired lease is NOT renewable -- after expiry a NEW park requires
    fresh confirmation, an ordinary ``PARK`` keyed to a new lease (the
    anti-rollover ruling, DESIGN section 1).  ``write_unverified`` holds the
    pod parked but its lease is terminal: the operator resumes, not renews.
    """
    return state == LEASE_OPEN


@dataclass(frozen=True, slots=True)
class ParkLease:
    """One unit's durable parking lease (the ``park_leases`` row).

    ``parked_at`` is the wire's ``parked_at`` (the design's table column
    ``opened_at`` -- the same instant, both spellings appear in the contracts).
    ``expires_at`` is wall-clock UTC because downtime spans restarts and the
    boot reconstruction must judge "expired while we were down" from the row
    alone; the monotonic clock is process-local and never persisted.
    ``max_total_s`` is the anti-rollover cap frozen at park time: renewal may
    never extend past ``parked_at + max_total_s``.
    """

    unit_id: str
    epoch: int
    parked_at: datetime
    expires_at: datetime
    max_total_s: int
    reason: str
    authorizer: str
    soc_pct_at_park: float | None
    state: str = LEASE_OPEN
    closed_at: datetime | None = None
    # DESIGN section 3: a foreign park observed OVER our unchanged lease --
    # named on the row, never fought, never a write.
    foreign_rewrite: bool = False
    # DESIGN section 3: the terminal sub-state flag -- the resume write could
    # not be verified, so the lease persists with ``parked: true`` and the
    # health reason ``park_write_unverified``.
    write_unverified: bool = False

    def __post_init__(self) -> None:
        if not self.unit_id or self.unit_id != self.unit_id.strip():
            raise ValueError("lease unit_id must be normalized")
        if type(self.epoch) is not int or self.epoch < 1:
            raise ValueError("lease epoch must be a positive integer")
        if not self.reason or self.reason != self.reason.strip():
            raise ValueError("lease reason must be normalized")
        if not self.authorizer or self.authorizer != self.authorizer.strip():
            raise ValueError("lease authorizer must be normalized")
        if type(self.max_total_s) is not int or self.max_total_s < 1:
            raise ValueError("lease max_total_s must be a positive integer")
        for name in ("parked_at", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"lease {name} must be a timezone-aware datetime")
        if self.expires_at < self.parked_at:
            raise ValueError("lease expires_at precedes parked_at")
        if self.state not in _LEASE_STATES:
            raise ValueError(f"unknown lease state {self.state!r}")
        if self.closed_at is not None and (
            not isinstance(self.closed_at, datetime) or self.closed_at.tzinfo is None
        ):
            raise ValueError("lease closed_at must be a timezone-aware datetime or None")
        if self.state in {LEASE_OPEN, LEASE_EXPIRED} and self.closed_at is not None:
            # ``expired`` is the ALARM state, not a close: the lease still
            # parks the unit and the operator's RESUME remains the exit.
            raise ValueError("an open or expired lease carries no closed_at")
        if self.state not in {LEASE_OPEN, LEASE_EXPIRED} and self.closed_at is None:
            raise ValueError("a terminal lease carries its closed_at")
        if self.write_unverified and self.state != LEASE_WRITE_UNVERIFIED:
            raise ValueError("write_unverified is the write_unverified state's own flag")

    @property
    def parked(self) -> bool:
        """Whether this lease still holds the unit parked."""
        return lease_is_parked(self.state)

    def rollover_cap_at(self) -> datetime:
        """The anti-rollover ceiling: ``parked_at + max_total_s``, never past."""
        from datetime import timedelta

        return self.parked_at + timedelta(seconds=self.max_total_s)

    def to_payload(self) -> dict[str, object]:
        """The wire ``lease`` object (API_CONTRACTS park/renew responses)."""
        return {
            "parked_at": self.parked_at.astimezone(UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "max_total_s": self.max_total_s,
            "reason": self.reason,
            "authorizer": self.authorizer,
            "epoch": self.epoch,
        }


_LEASE_STATES: Final[frozenset[str]] = frozenset(
    {
        LEASE_OPEN,
        LEASE_EXPIRED,
        LEASE_CLOSED_OPERATOR,
        LEASE_CLOSED_FOREIGN,
        LEASE_WRITE_UNVERIFIED,
    }
)


class ParkLeaseEpochConflict(RuntimeError):
    """The lease row moved under the caller's stale epoch; nothing landed.

    DESIGN_POD_PARKING section 4 (single-flight per unit): a mutation on a
    closed epoch refuses (the ``park_lease_absent`` family) instead of acting
    on a stale view -- the CAS guard raises this before any row changes, and
    the surrounding transaction (the parking audit row included) rolls back
    whole.  Shared by the durable and in-memory twins: both stores raise the
    ONE error so the controller maps one refusal shape.
    """

    def __init__(self, unit_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"park lease for {unit_id!r} moved: expected epoch {expected}, found {actual}"
        )
        self.unit_id = unit_id
        self.expected_epoch = expected
        self.actual_epoch = actual


__all__ = [
    "LEASE_CLOSED_FOREIGN",
    "LEASE_CLOSED_OPERATOR",
    "LEASE_EXPIRED",
    "LEASE_OPEN",
    "LEASE_WRITE_UNVERIFIED",
    "VENDOR_DEBUG_MODE_NAMES",
    "ParkLease",
    "ParkLeaseEpochConflict",
    "lease_is_open_for_renewal",
    "lease_is_parked",
    "vendor_debug_mode_name",
]
