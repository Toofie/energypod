"""Pod parking: the ParkController above the transport, below the facade.

DESIGN_POD_PARKING (2026-08-24, CONTRACT v2).  Parking writes the vendor
debug-mode word ``0x8000 <- 1`` through the transport's named
``write_debug_mode``; resuming writes ``0x8000 <- 0``.  The doctrine this
controller implements, pinned:

- **The lease is the expiry ALARM, not the expiry ACTOR.**  At expiry: no
  write (ever, including boot), one ``unit_park_expired`` row, one alert-tier
  ``unit.park_expired`` event, the projection moves to ``{parked: true,
  expired: true}``, and the operator's RESUME stays the only exit.
- **Durable-first, no inverse** (the ``acknowledge_inhibit`` pattern): the
  audit row lands BEFORE the register write with ``result: pending``, then a
  completing row (``parked`` / ``resumed`` / ``refused``) and the lease
  change land in ONE store transaction.  A failed pending append refuses the
  mutation, retryable.  A failed resume write is never rolled back by
  bookkeeping: ``degraded: [audit_unavailable]`` rides the response instead.
- **Divergence is alarmed, never fought**: the observed debug word is
  compared against the ledger every supervision pass.  ``word=1, no lease``
  projects ``origin: foreign``/``unrecorded``; ``word=0, lease open`` closes
  the lease as ``observed_foreign`` (no write) and arms the
  ``resume_provenance`` window; a vendor word 2-6 over our unchanged lease
  sets ``foreign_rewrite`` and renders ``foreign_mode`` -- named, never a
  write, never a re-park.  One extension (DESIGN_BATTERY_HEALTH_WATCH §12,
  A5): a ``word=0, lease open`` that carries a pending ``unit_resumed`` row
  with no completing row inside the bounded recency window is OUR
  crashed-then-verified resume -- adopted as ours, a store write only, never
  misread as foreign.
- **The lease origin vocabulary carries ``automation``, DERIVED (A10)**:
  a row whose principal carries the ``energypod:`` prefix renders
  ``automation``, anything else ``operator`` (:func:`derive_origin`); the
  foreign/unrecorded/none semantics are untouched, and a word=1 under an
  open ``automation`` lease is OURS -- resume needs no takeover.
- **Single-flight per unit**: one asyncio critical section per unit plus the
  monotonic ``lease_epoch`` CAS in the store; the write->readback->verify
  sequence is ONE actor-mailbox operation, so a heartbeat PQ write can never
  interleave between our write and its readback.
- **Boot reconstructs and alarms; boot never writes.**

Parking is NOT electrical isolation -- the battery stays connected at full
voltage.  No countdown here may ever imply time-bounded safety; the lease
countdown is policy, never safety.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from energypod.domain.audit import AuditEvent
from energypod.domain.observations import UnitLifecycle
from energypod.domain.parking import (
    LEASE_CLOSED_FOREIGN,
    LEASE_CLOSED_OPERATOR,
    LEASE_EXPIRED,
    LEASE_OPEN,
    LEASE_WRITE_UNVERIFIED,
    ParkLease,
    lease_is_open_for_renewal,
    vendor_debug_mode_name,
)

from .actor import DebugModeChangeError

# --- the wire vocabulary (API_CONTRACTS "Pod parking") ----------------------------

PARK_NOT_COMMISSIONED: Final[str] = "park_not_commissioned"
PARK_CONFLICT_REFUSED: Final[str] = "park_conflict_refused"
PARK_ALREADY_PARKED: Final[str] = "park_already_parked"
PARK_MODE_OUT_OF_SCOPE: Final[str] = "park_mode_out_of_scope"
PARK_WRITE_FAILED: Final[str] = "park_write_failed"
PARK_READBACK_UNVERIFIED: Final[str] = "park_readback_unverified"
PARK_LEASE_CAP_REACHED: Final[str] = "park_lease_cap_reached"
PARK_LEASE_ABSENT: Final[str] = "park_lease_absent"
PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED: Final[str] = (
    "park_foreign_word_acknowledgement_required"
)
RESUME_STOP_LATCHED: Final[str] = "resume_stop_latched"

# The minimum lease a PARK may carry (a lease shorter than an operator's
# glance is theater) -- the config block's ``lease_s in [60, max_lease_s]``.
MIN_LEASE_S: Final[int] = 60

# DESIGN section 3: the resume-provenance window after an observed foreign
# resume.  Bounded so a long-dead transition cannot render forever; the
# window is honesty about WHERE a resume came from, not a latch.
RESUME_PROVENANCE_WINDOW_S: Final[float] = 900.0

# The parked-state hint at expiry (the pinned sentence, API_CONTRACTS).
EXPIRY_HINT: Final[str] = "lease expired — Resume is an operator act"
# The terminal write-unverified posture's hint: our failed act stays ours.
WRITE_UNVERIFIED_HINT: Final[str] = (
    "resume write unverified — the pod may still be parked; "
    "Repeat RESUME and watch the readback, then verify the mode word in the "
    "vendor app before any physical work (parking is not electrical isolation)"
)

# Fault-class audit events the resume checklist's ``faults_while_parked``
# collects for the park window (the audit trail owns incidents; observations
# are ephemeral).  Bounded by the audit store's own recent-window read.
_FAULT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "heartbeat_failed",
        "actuation_incoherent",
        "unexpected_autonomy",
        "objective_echo",
    }
)
_FAULT_AUDIT_SCAN_LIMIT: Final[int] = 200

# DESIGN section 4 (boot adoption): the pending-row scan reads the same
# bounded recent window as the fault scan.  An evicted pending row is honestly
# NOT FOUND -- the adoption degrades to today's ``unrecorded`` path, never a
# fabricated lease.
_ADOPTION_AUDIT_SCAN_LIMIT: Final[int] = 200

# The lease origin's ``automation`` word (DESIGN_BATTERY_HEALTH_WATCH §10,
# A10): DERIVED, never stored -- ``ParkLease`` and the ``park_leases`` table
# carry no origin column.  A row whose principal carries the ``energypod:``
# prefix renders ``automation``; anything else renders ``operator``.  The
# foreign/unrecorded/none semantics are untouched, and a word=1 under an open
# ``automation`` lease is OURS -- resume by the health watch's own completion
# or by the operator, never a takeover.
ORIGIN_AUTOMATION: Final[str] = "automation"
ORIGIN_OPERATOR: Final[str] = "operator"


def derive_origin(principal: str) -> str:
    """A10's derivation: the acting principal's own class, nothing else.

    The composed automation principals (``energypod:health-adviser`` and the
    controller's own bookkeeping subjects) render ``automation``; every human
    principal renders ``operator``.  Pure and total so every row, event, and
    projection derives the SAME word for the same principal -- the boot-adopted
    automation park derives ``automation`` from the adopted row's principal,
    and no surface can ever fabricate an origin in either direction.
    """
    return ORIGIN_AUTOMATION if principal.startswith("energypod:") else ORIGIN_OPERATOR
# The honest reason an adopted lease carries: the operator's own reason text
# lived in the pending row's payload, which the audit row keeps only as a
# fingerprint -- unknowable after the crash, never invented.
ADOPTED_PENDING_REASON: Final[str] = "adopted pending park (crash-recovered)"

# The words that render ``foreign_mode`` (DESIGN section 0/3): the vendor
# values 2-6 are permanently unexposed -- read-side vocabulary only.
_FOREIGN_MODE_WORDS: Final[frozenset[int]] = frozenset({2, 3, 4, 5, 6})

_PARK_PRINCIPAL_POLICY_VERSION: Final[str] = "parking"


class ParkingRefusal(Exception):
    """A guarded park/resume refusal carrying its wire code and details.

    The facade lets exactly this shape propagate; the guarded boundary maps
    ``code`` onto the 409 envelope with ``details`` verbatim (the pinned
    details shapes live with each raise below).
    """

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class ParkLeaseStore(Protocol):
    """The lease store port both persistence twins implement (async wrapper)."""

    async def commit(self, event: AuditEvent, lease: ParkLease) -> None: ...

    async def replace(
        self, event: AuditEvent, lease: ParkLease, *, expected_epoch: int
    ) -> None: ...

    async def lease(self, unit_id: str) -> ParkLease | None: ...

    async def all_leases(self) -> Mapping[str, ParkLease]: ...


class ParkAuditPort(Protocol):
    """The event-only audit append (pending/refusal/expiry narrative rows)."""

    async def append(self, event: AuditEvent) -> None: ...

    async def recent(
        self, *, limit: int, after_sequence: int | None = None
    ) -> tuple[Any, ...]: ...


class ParkEventBus(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class ParkObservationsPort(Protocol):
    async def latest(self, unit_id: str) -> Any | None: ...


class ParkIntentsPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...


class ParkActorHandle(Protocol):
    """The actor surface parking drives: the ONE named transport operation."""

    @property
    def unit_id(self) -> str: ...

    @property
    def lifecycle(self) -> Any: ...

    @property
    def inhibit_latched(self) -> bool: ...

    async def request_debug_mode_change(self, value: int) -> Mapping[str, Any]: ...

    async def read_debug_word(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ParkCommissioning:
    """The ``parking:`` block's composed envelope (DESIGN section 5.1)."""

    max_lease_s: int
    default_lease_s: int
    mode_write_enabled: bool

    def __post_init__(self) -> None:
        if type(self.max_lease_s) is not int or self.max_lease_s < MIN_LEASE_S:
            raise ValueError("max_lease_s must be at least the minimum lease")
        if type(self.default_lease_s) is not int or not (
            MIN_LEASE_S <= self.default_lease_s <= self.max_lease_s
        ):
            raise ValueError("default_lease_s must sit inside the lease bounds")
        if type(self.mode_write_enabled) is not bool:
            raise ValueError("mode_write_enabled must be boolean")


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_text(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _finite_number(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


def _optional_word(raw: Any) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 0xFFFF:
        return int(raw)
    return None


@dataclass(slots=True)
class _DivergenceMemory:
    """In-run divergence evidence the projection renders (never durable).

    ``foreign_standby`` tracks word=1 with no controller lease: the observed
    0->1 transition mid-run is the vendor-app/foreign-flip class (``origin:
    foreign``, with the observation time); a word that was ALREADY 1 at this
    process's first look is crash-after-write residue (``origin:
    unrecorded``) -- honest unknown, never a fabricated transition.
    """

    foreign_standby_since: datetime | None = None
    foreign_standby_origin: str | None = None
    last_word: int | None = None
    foreign_mode_word: int | None = None
    foreign_mode_first_seen: datetime | None = None
    resume_observed_at: datetime | None = None
    resume_origin: str | None = None


class ParkController:
    """Per-unit critical sections, the lease ledger, divergence, and expiry.

    Mutations (park/renew/resume) run guards AND state transition inside one
    per-unit asyncio lock; the store's epoch CAS is the durable backstop.  The
    transport sequence is delegated to the owning actor's ONE named mailbox
    operation (``request_debug_mode_change``) so a heartbeat PQ write can
    never interleave between the mode write and its readback.
    """

    def __init__(
        self,
        *,
        unit_ids: frozenset[str],
        commissioning: ParkCommissioning,
        clock: Clock,
        store: ParkLeaseStore,
        audit: ParkAuditPort,
        bus: ParkEventBus,
        actors: Mapping[str, ParkActorHandle],
        observations: ParkObservationsPort,
        intents: ParkIntentsPort,
        process_instance_id: str,
        process_origin_mono: float,
        latched_stop_units: Any = None,
    ) -> None:
        if not unit_ids:
            raise ValueError("unit_ids must not be empty")
        self._commissioning = commissioning
        self._clock = clock
        self._store = store
        self._audit = audit
        self._bus = bus
        self._actors = dict(actors)
        self._observations = observations
        self._intents = intents
        self._process_instance_id = process_instance_id
        self._process_origin_mono = float(process_origin_mono)
        # The latched-stop view is bound post-construction (the facade owns
        # the latch registry and is composed after this controller).
        self._latched_stop_units = latched_stop_units
        self._locks: dict[str, asyncio.Lock] = {unit_id: asyncio.Lock() for unit_id in unit_ids}
        self._divergence: dict[str, _DivergenceMemory] = {
            unit_id: _DivergenceMemory() for unit_id in unit_ids
        }
        # The sync parked-unit view advisers and readiness read (mirrored on
        # every mutation and supervision pass; the durable row stays truth).
        self._parked_mirror: set[str] = set()
        self._lease_mirror: dict[str, ParkLease] = {}

    # --- binding ----------------------------------------------------------------

    @property
    def commissioning(self) -> ParkCommissioning:
        """The composed ``parking:`` envelope (the facade's lease bounds)."""
        return self._commissioning

    def bind_latched_stop_units(self, view: Any) -> None:
        """Bind the facade's latched-stop view (composition wiring).

        The view answers ``unit_ids()`` (every unit a latched stop names,
        fleet-wide stops included) and ``stop_ids_for(unit_id)`` (the exact
        ids the resume refusal and checklist name).
        """
        self._latched_stop_units = view

    # --- the mutations -------------------------------------------------------------

    async def park(
        self,
        unit_id: str,
        *,
        reason: str,
        principal_subject: str,
        request_id: str,
        lease_s: int | None = None,
    ) -> dict[str, Any]:
        """One guarded PARK: pending row, ONE actor write->readback, lease.

        The pinned guard order (API_CONTRACTS): commissioning, conflict
        (armed / under intent / latched stop -- re-checked INSIDE the
        critical section), already-parked, then the device sequence's own
        refusals (mode out of scope / write failed / readback unverified).
        """
        self._require_commissioned()
        handle = self._require_unit(unit_id)
        resolved_lease_s = self._resolve_lease_s(lease_s)
        async with self._locks[unit_id]:
            conflicts = await self._unit_conflicts(unit_id)
            if conflicts:
                raise ParkingRefusal(
                    PARK_CONFLICT_REFUSED,
                    "parking refuses while the unit is armed, under an active request, "
                    "or held by a latched emergency stop — disarm and finish first "
                    "(disarm → park → resume is the recovery walkthrough)",
                    {"units": conflicts},
                )
            existing = await self._store.lease(unit_id)
            if existing is not None and existing.parked and existing.state != LEASE_EXPIRED:
                # An EXPIRED lease is still parked but a NEW park is the
                # sanctioned exit (fresh confirmation, new lease, new epoch --
                # anti-rollover); ``write_unverified`` stays refused: the
                # operator resume is its named exit.
                raise ParkingRefusal(
                    PARK_ALREADY_PARKED,
                    f"{unit_id} is already parked — renew the lease or resume instead",
                    {"lease": existing.to_payload()},
                )
            epoch = 1 if existing is None else existing.epoch + 1
            observation = await self._latest_observation(unit_id)
            soc_pct = _finite_number(getattr(observation, "authoritative_soc_pct", None))
            prior_watts = _finite_number(getattr(observation, "battery_watts", None))
            prior_lifecycle = UnitLifecycle(
                _enum_text(getattr(handle, "lifecycle", UnitLifecycle.DISARMED))
            )
            now = self._wall_now()
            lease = ParkLease(
                unit_id=unit_id,
                epoch=epoch,
                parked_at=now,
                expires_at=now + timedelta(seconds=resolved_lease_s),
                max_total_s=self._commissioning.max_lease_s,
                reason=reason,
                authorizer=principal_subject,
                soc_pct_at_park=soc_pct,
            )
            # Durable-first: the pending row precedes the register write; a
            # failed append refuses the park, retryable, nothing consumed.
            await self._audit.append(
                self._row(
                    event_type="unit_parked",
                    unit_id=unit_id,
                    principal=principal_subject,
                    request_id=request_id,
                    result="pending",
                    reason_codes=("durable_first",),
                    lifecycle=prior_lifecycle,
                    payload={
                        "prior_word": None,
                        "written_value": None,
                        "readback_word": None,
                        "verified": False,
                        "origin": derive_origin(principal_subject),
                        "authorizer": principal_subject,
                        "reason": reason,
                        "epoch": epoch,
                        "lease_s": resolved_lease_s,
                    },
                )
            )
            try:
                outcome = await handle.request_debug_mode_change(1)
            except DebugModeChangeError as error:
                await self._audit.append(
                    self._row(
                        event_type="unit_parked",
                        unit_id=unit_id,
                        principal=principal_subject,
                        request_id=request_id,
                        result="refused",
                        reason_codes=(error.reason,),
                        lifecycle=prior_lifecycle,
                        payload=dict(error.details),
                    )
                )
                raise self._map_debug_mode_error(error) from error
            prior_word = int(outcome["prior_word"])
            # A park over a foreign word=1 records the origin transition: the
            # ledger never claims we initiated a park we inherited.  A word=1
            # under our OWN lease (re-park over an expired lease, whose
            # standby is ours) is not an inheritance.
            inherited = prior_word == 1 and (existing is None or not existing.parked)
            codes = ["readback_verified"]
            if inherited:
                codes.append("adopted_foreign_park")
            await self._store.commit(
                self._row(
                    event_type="unit_parked",
                    unit_id=unit_id,
                    principal=principal_subject,
                    request_id=request_id,
                    result="parked",
                    reason_codes=tuple(codes),
                    lifecycle=prior_lifecycle,
                    payload={
                        "prior_word": prior_word,
                        "written_value": int(outcome["written_value"]),
                        "readback_word": int(outcome["readback_word"]),
                        "verified": bool(outcome["verified"]),
                        "origin": derive_origin(principal_subject),
                        "authorizer": principal_subject,
                        "reason": reason,
                        **lease.to_payload(),
                    },
                ),
                lease,
            )
            self._mirror_parked(unit_id, parked=True, lease=lease)
            self._clear_divergence(unit_id)
            await self._publish(
                "unit.parked",
                {
                    "unit_id": unit_id,
                    "principal": principal_subject,
                    "reason": reason,
                    "origin": derive_origin(principal_subject),
                    **lease.to_payload(),
                    "prior_word": prior_word,
                    "readback_word": int(outcome["readback_word"]),
                },
            )
            return {
                "unit_id": unit_id,
                "action": "park",
                "prior_word": prior_word,
                "written_value": int(outcome["written_value"]),
                "readback_word": int(outcome["readback_word"]),
                "verified": bool(outcome["verified"]),
                "as_of": self._wall_now().isoformat(),
                "lease": lease.to_payload(),
                "prior_state": {"lifecycle": prior_lifecycle, "measured_watts": prior_watts},
            }

    async def renew(
        self,
        unit_id: str,
        *,
        lease_s: int,
        principal_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        """One sliding renewal, never past ``parked_at + max_lease_s``."""
        self._require_commissioned()
        self._require_unit(unit_id)
        resolved_lease_s = self._resolve_lease_s(lease_s)
        async with self._locks[unit_id]:
            lease = await self._store.lease(unit_id)
            if lease is None or not lease_is_open_for_renewal(lease.state):
                raise ParkingRefusal(
                    PARK_LEASE_ABSENT,
                    f"{unit_id} has no open lease to renew — a new park needs a fresh "
                    "PARK confirmation (anti-rollover: an expired lease never renews)",
                    self._closing_details(lease),
                )
            now = self._wall_now()
            requested_expires_at = now + timedelta(seconds=resolved_lease_s)
            cap_at = lease.rollover_cap_at()
            if requested_expires_at > cap_at:
                raise ParkingRefusal(
                    PARK_LEASE_CAP_REACHED,
                    f"the renewal would extend {unit_id}'s lease past the anti-rollover "
                    "cap (parked_at + max_lease_s)",
                    {
                        "parked_at": lease.parked_at.astimezone(UTC).isoformat(),
                        "max_total_s": lease.max_total_s,
                        "requested_expires_at": requested_expires_at.astimezone(UTC).isoformat(),
                    },
                )
            renewed = dataclass_replace(lease, expires_at=requested_expires_at)
            await self._store.replace(
                self._row(
                    event_type="unit_park_renewed",
                    unit_id=unit_id,
                    principal=principal_subject,
                    request_id=request_id,
                    result="renewed",
                    reason_codes=("renewed",),
                    lifecycle=self._actor_lifecycle(unit_id),
                    payload={
                        "authorizer": principal_subject,
                        "origin": derive_origin(principal_subject),
                        **renewed.to_payload(),
                    },
                ),
                renewed,
                expected_epoch=lease.epoch,
            )
            await self._publish(
                "unit.park_renewed",
                {
                    "unit_id": unit_id,
                    "principal": principal_subject,
                    "origin": derive_origin(principal_subject),
                    **renewed.to_payload(),
                },
            )
            return {
                "unit_id": unit_id,
                "action": "renew",
                "as_of": self._wall_now().isoformat(),
                "lease": renewed.to_payload(),
            }

    async def resume(
        self,
        unit_id: str,
        *,
        principal_subject: str,
        request_id: str,
        takeover: str | None = None,
    ) -> dict[str, Any]:
        """One guarded RESUME: consent where the park wasn't ours, then write 0.

        Resume on a Normal word is a no-op 200 (``origin: "none"``) -- honest
        idempotence, no error theater.  Resuming our own expired lease needs
        no takeover (the fresh RESUME IS the act).  A word in 2-6 is its own
        acknowledged vendor act, never a resume alias.  A latched emergency
        stop naming the unit refuses: resume would re-enable autonomy under a
        standing stop instruction.
        """
        self._require_commissioned()
        handle = self._require_unit(unit_id)
        if takeover is not None and takeover != "FOREIGN":
            raise ValueError("takeover acknowledgement must be the literal 'FOREIGN'")
        async with self._locks[unit_id]:
            stop_ids = self._latched_stops_for(unit_id)
            if stop_ids:
                raise ParkingRefusal(
                    RESUME_STOP_LATCHED,
                    f"resume would re-enable {unit_id}'s autonomy under a standing "
                    "emergency stop — acknowledge the stop first",
                    {
                        "stop_ids": stop_ids,
                        "acknowledgement_endpoint": (
                            "/api/v1/emergency-stop/{stop_id}/acknowledge"
                        ),
                    },
                )
            lease = await self._store.lease(unit_id)
            try:
                prior_word = int(await handle.read_debug_word()) & 0xFFFF
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise ParkingRefusal(
                    PARK_WRITE_FAILED,
                    "the debug-mode word could not be read back for resume",
                    {"error_class": type(error).__name__},
                ) from error
            if prior_word in _FOREIGN_MODE_WORDS or prior_word > 1:
                raise ParkingRefusal(
                    PARK_MODE_OUT_OF_SCOPE,
                    f"{unit_id} is in the vendor mode word {prior_word} "
                    f"({vendor_debug_mode_name(prior_word) or 'unmapped'}) — normalizing a "
                    "vendor-directed mode is its own acknowledged act, never a resume alias",
                    {
                        "prior_word": prior_word,
                        "vendor_name": vendor_debug_mode_name(prior_word),
                    },
                )
            if prior_word == 0:
                # Idempotent honesty: the pod is already Normal.  An open
                # lease over a Normal word is the observed-foreign-resume
                # divergence (section 1) -- UNLESS our own earlier resume
                # crashed between its verified write and the lease commit, the
                # A5 shape a fresh RESUME lands on first: adopt it as ours
                # (a store write only), else close it the way the supervision
                # pass would, written_value null -- no write either way.
                if (
                    lease is not None
                    and lease.parked
                    and not await self._adopt_pending_resume(unit_id, lease, self._wall_now())
                ):
                    await self._close_observed_foreign(lease, request_id=request_id)
                return {
                    "unit_id": unit_id,
                    "action": "resume",
                    "prior_word": 0,
                    "written_value": 0,
                    "readback_word": 0,
                    "verified": True,
                    "as_of": self._wall_now().isoformat(),
                    "origin": "none",
                    "checklist": await self._resume_checklist(unit_id, lease),
                    "degraded": [],
                }
            # prior_word == 1: whose standby is it?
            origin = derive_origin(principal_subject)
            codes = ["readback_verified"]
            if lease is None or not lease.parked:
                if takeover != "FOREIGN":
                    since = self._divergence[unit_id].foreign_standby_since
                    raise ParkingRefusal(
                        PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED,
                        f"{unit_id} is in Standby with no controller lease — resuming a "
                        "foreign park needs the takeover acknowledgement",
                        {
                            "acknowledgement": "FOREIGN",
                            "prior_word": prior_word,
                            "observed_since": (
                                None if since is None else since.astimezone(UTC).isoformat()
                            ),
                        },
                    )
                origin = "foreign"
                codes.append("foreign_takeover_acknowledged")
            elif lease.state == LEASE_EXPIRED:
                # The operator's fresh RESUME is the act; no takeover needed.
                pass
            lifecycle = self._actor_lifecycle(unit_id)
            await self._audit.append(
                self._row(
                    event_type="unit_resumed",
                    unit_id=unit_id,
                    principal=principal_subject,
                    request_id=request_id,
                    result="pending",
                    reason_codes=("durable_first",),
                    lifecycle=lifecycle,
                    payload={
                        "prior_word": prior_word,
                        "written_value": None,
                        "readback_word": None,
                        "verified": False,
                        "origin": origin,
                        "authorizer": principal_subject,
                        **({} if lease is None else {"epoch": lease.epoch}),
                    },
                )
            )
            try:
                outcome = await handle.request_debug_mode_change(0)
            except DebugModeChangeError as error:
                if error.reason == "readback_unverified" and lease is not None and lease.parked:
                    # ACKed but unverified: our act stays ours -- the lease
                    # persists in the terminal write_unverified posture
                    # (parked stays true), never reclassified foreign.
                    unverified = dataclass_replace(
                        lease,
                        state=LEASE_WRITE_UNVERIFIED,
                        closed_at=self._wall_now(),
                        write_unverified=True,
                    )
                    with_retries = dict(error.details)
                    with_retries["origin"] = origin
                    with_retries["authorizer"] = principal_subject
                    with_retries["epoch"] = lease.epoch
                    # Never roll a resume back by bookkeeping: the posture
                    # is named degraded instead of failed.
                    with contextlib.suppress(Exception):
                        await self._store.replace(
                            self._row(
                                event_type="unit_resumed",
                                unit_id=unit_id,
                                principal=principal_subject,
                                request_id=request_id,
                                result="refused",
                                reason_codes=("readback_mismatch", "write_unverified"),
                                lifecycle=lifecycle,
                                payload=with_retries,
                            ),
                            unverified,
                            expected_epoch=lease.epoch,
                        )
                    self._mirror_parked(unit_id, parked=True, lease=unverified)
                else:
                    await self._audit.append(
                        self._row(
                            event_type="unit_resumed",
                            unit_id=unit_id,
                            principal=principal_subject,
                            request_id=request_id,
                            result="refused",
                            reason_codes=(error.reason,),
                            lifecycle=lifecycle,
                            payload=dict(error.details),
                        )
                    )
                raise self._map_debug_mode_error(error) from error
            checklist = await self._resume_checklist(unit_id, lease)
            degraded: list[str] = []
            if lease is not None and lease.parked:
                closed = dataclass_replace(
                    lease,
                    state=LEASE_CLOSED_OPERATOR,
                    closed_at=self._wall_now(),
                )
                try:
                    await self._store.replace(
                        self._row(
                            event_type="unit_resumed",
                            unit_id=unit_id,
                            principal=principal_subject,
                            request_id=request_id,
                            result="resumed",
                            reason_codes=tuple(codes),
                            lifecycle=lifecycle,
                            payload={
                                "prior_word": prior_word,
                                "written_value": int(outcome["written_value"]),
                                "readback_word": int(outcome["readback_word"]),
                                "verified": bool(outcome["verified"]),
                                "origin": origin,
                                "authorizer": principal_subject,
                                "checklist": checklist,
                                "epoch": lease.epoch,
                            },
                        ),
                        closed,
                        expected_epoch=lease.epoch,
                    )
                except Exception:
                    # Impl-10 (no inverse): the resume stands whatever the
                    # record does; degraded names the bookkeeping.
                    degraded.append("audit_unavailable")
                self._mirror_parked(unit_id, parked=False)
            else:
                # A foreign takeover resume holds no controller lease: the
                # narrative row only, plus the word's own evidence.
                try:
                    await self._audit.append(
                        self._row(
                            event_type="unit_resumed",
                            unit_id=unit_id,
                            principal=principal_subject,
                            request_id=request_id,
                            result="resumed",
                            reason_codes=tuple(codes),
                            lifecycle=lifecycle,
                            payload={
                                "prior_word": prior_word,
                                "written_value": int(outcome["written_value"]),
                                "readback_word": int(outcome["readback_word"]),
                                "verified": bool(outcome["verified"]),
                                "origin": origin,
                                "authorizer": principal_subject,
                                "checklist": checklist,
                            },
                        )
                    )
                except Exception:
                    degraded.append("audit_unavailable")
            self._clear_divergence(unit_id)
            await self._publish(
                "unit.resumed",
                {
                    "unit_id": unit_id,
                    "principal": principal_subject,
                    "origin": origin,
                    "prior_word": prior_word,
                    "readback_word": int(outcome["readback_word"]),
                    "verified": bool(outcome["verified"]),
                    "checklist": checklist,
                },
            )
            return {
                "unit_id": unit_id,
                "action": "resume",
                "prior_word": prior_word,
                "written_value": int(outcome["written_value"]),
                "readback_word": int(outcome["readback_word"]),
                "verified": bool(outcome["verified"]),
                "as_of": self._wall_now().isoformat(),
                "origin": origin,
                "checklist": checklist,
                "degraded": degraded,
            }

    # --- supervision: expiry + divergence (rides the existing fleet pass) ----------

    async def supervise(self) -> None:
        """One bounded pass: the expiry ALARM and the word-vs-ledger truth.

        Called once per fleet cycle by the supervision loop (no new task
        class).  Every step is per-unit suppressed-by-caller; a failing store
        or bus never gates the fleet.  NEVER a write under any path.
        """
        try:
            leases = dict(await self._store.all_leases())
        except Exception:
            return
        now = self._wall_now()
        for unit_id in self._locks:
            lease = leases.get(unit_id)
            if lease is not None and lease.state == LEASE_OPEN and now >= lease.expires_at:
                await self._expire_lease(lease, now)
                lease = dataclass_replace(lease, state=LEASE_EXPIRED)
            # Divergence reconciles EVERY unit every poll -- a word=1 with no
            # lease is exactly the foreign/unrecorded evidence (section 1).
            await self._reconcile_divergence(unit_id, lease)
        try:
            # The mirror re-reads the store AFTER the pass: a lease this pass
            # itself closed must not resurrect from the pre-pass snapshot.
            self._mirror_from(dict(await self._store.all_leases()))
        except Exception:
            return

    async def reconstruct_at_boot(self) -> None:
        """Boot reconstruction from the lease table: alarm, never a write.

        Expired-during-downtime raises the expiry alarm here (table truth
        alone).  A crashed-then-verified park is ADOPTED here when a survived
        observation already serves word=1 (section 4: pending row + word=1 is
        the durable-first park completed); a fresh boot without a served word
        adopts on the first supervision pass, when the first poll has served
        it.  The rest of the word-vs-ledger reconciliation (foreign resume,
        the unrecorded standby) joins on that same first pass.  Boot never
        parks and never un-parks: adoption mints the lost lease row and never
        touches the device register.
        """
        try:
            leases = dict(await self._store.all_leases())
        except Exception:
            return
        now = self._wall_now()
        for unit_id, lease in leases.items():
            if unit_id not in self._locks:
                continue
            if lease.state == LEASE_OPEN and now >= lease.expires_at:
                await self._expire_lease(lease, now, reason_codes=("expired_in_downtime",))
        for unit_id in self._locks:
            standing = leases.get(unit_id)
            if standing is not None and standing.parked:
                continue
            observation = await self._latest_observation(unit_id)
            word = (
                _optional_word(getattr(observation, "debug_mode_w", None))
                if observation is not None
                else None
            )
            if word != 1:
                continue
            adopted = await self._adopt_pending_park(unit_id, now)
            if adopted is not None:
                leases[unit_id] = adopted
        self._mirror_from(leases)

    async def _adopt_pending_park(self, unit_id: str, now: datetime) -> ParkLease | None:
        """Section 4: adopt a crashed-then-verified park as ours.

        The durable-first sequence is pending row -> write -> verifying
        commit; a crash between the verified write and that transaction
        leaves exactly ``word=1, no lease row, a pending row``.  This scans
        the audit store's bounded recent window for the unit's most recent
        ``unit_parked`` pending row with no later completing row and, when
        the row is recent (within ``max_lease_s`` of its timestamp), mints
        the lease the crash lost: origin operator, the row's authorizer (its
        principal) and instant, the commissioning cap as the TTL -- the
        operator's requested ``lease_s`` lived only in the payload the audit
        row keeps as a fingerprint, so the cap is the honest bound, never a
        fabricated figure -- and the standard expiry alarm when that TTL
        already passed.

        Adoption is a STORE write only; the device register is never touched
        (boot never writes).  No row found, the window evicted, a completing
        row present, or the store failing all degrade to ``None`` -- the
        caller keeps today's honest path.
        """
        pending = await self._latest_uncompleted_pending(unit_id)
        if pending is None:
            return None
        occurred = getattr(pending, "occurred_at", None)
        if not isinstance(occurred, datetime) or occurred.tzinfo is None:
            return None
        cap = self._commissioning.max_lease_s
        if (now - occurred).total_seconds() > cap:
            # Stale evidence: whatever lease the operator took can no longer
            # be ours under the anti-rollover cap -- the honest unknown.
            return None
        authorizer = getattr(pending, "principal", None)
        if not isinstance(authorizer, str) or not authorizer.strip():
            return None
        async with self._locks[unit_id]:
            existing = await self._store.lease(unit_id)
            if existing is not None and existing.parked:
                return None
            lease = ParkLease(
                unit_id=unit_id,
                epoch=1 if existing is None else existing.epoch + 1,
                parked_at=occurred,
                expires_at=occurred + timedelta(seconds=cap),
                max_total_s=cap,
                reason=ADOPTED_PENDING_REASON,
                authorizer=authorizer,
                soc_pct_at_park=None,
            )
            correlation = getattr(pending, "correlation_id", None)
            request_id = (
                correlation.removeprefix("parking:unit_parked:")
                if isinstance(correlation, str)
                and correlation.startswith("parking:unit_parked:")
                and len(correlation) > len("parking:unit_parked:")
                else f"adoption:{unit_id}"
            )
            # The completing row the crash lost (the adopted_foreign_park
            # origin-transition vocabulary): OUR controller mints it, no
            # write was performed, the ledger never claims more than that.
            try:
                await self._store.commit(
                    self._row(
                        event_type="unit_parked",
                        unit_id=unit_id,
                        principal="energypod:parking",
                        request_id=request_id,
                        result="parked",
                        reason_codes=("adopted_pending",),
                        lifecycle=self._actor_lifecycle(unit_id),
                        payload={
                            "prior_word": 1,
                            "written_value": None,
                            "readback_word": 1,
                            "verified": None,
                            "origin": derive_origin(authorizer),
                            "authorizer": authorizer,
                            "reason": ADOPTED_PENDING_REASON,
                            "adopted": True,
                            **lease.to_payload(),
                        },
                    ),
                    lease,
                )
            except Exception:
                return None
            adopted = lease
            if now >= lease.expires_at:
                await self._expire_lease(lease, now, reason_codes=("expired_in_downtime",))
                adopted = dataclass_replace(lease, state=LEASE_EXPIRED)
            self._mirror_parked(unit_id, parked=True, lease=adopted)
            return adopted

    async def _latest_uncompleted_pending(
        self,
        unit_id: str,
        *,
        event_type: str = "unit_parked",
        completing_results: tuple[str, ...] = ("parked", "refused"),
    ) -> Any | None:
        """The unit's most recent pending row of one type no later row
        completes -- the crash-after-write shape.

        Row order never matters: a completing row AT-OR-AFTER the pending's
        instant closes the sequence (the commit lands within the write's own
        second), so both store orderings answer identically.
        """
        try:
            events = await self._audit.recent(limit=_ADOPTION_AUDIT_SCAN_LIMIT)
        except Exception:
            return None
        pending_rows: dict[datetime, Any] = {}
        completed_at: list[datetime] = []
        for event in events:
            if getattr(event, "unit_id", None) != unit_id:
                continue
            if getattr(event, "event_type", None) != event_type:
                continue
            occurred = getattr(event, "occurred_at", None)
            if not isinstance(occurred, datetime) or occurred.tzinfo is None:
                continue
            result = getattr(event, "result", None)
            if result == "pending":
                pending_rows.setdefault(occurred, event)
            elif result in completing_results:
                completed_at.append(occurred)
        if not pending_rows:
            return None
        latest = max(pending_rows)
        if any(at >= latest for at in completed_at):
            return None
        return pending_rows[latest]

    async def _adopt_pending_resume(
        self, unit_id: str, lease: ParkLease, now: datetime
    ) -> bool:
        """A5 (DESIGN_BATTERY_HEALTH_WATCH §7.2 step 5): adopt a
        crashed-then-verified RESUME as ours.

        The resume twin of section 4's park-side adoption.  The durable-first
        resume sequence is pending row -> write 0 -> verifying lease-closing
        transaction; a crash between the VERIFIED write and that transaction
        leaves exactly ``word=0, open lease, a pending ``unit_resumed`` row``
        -- the shape the divergence pass would otherwise close as
        ``observed_foreign``, reading OUR completed act as somebody else's
        (and a nightly program multiplies that window).  When the unit's most
        recent pending resume row has no completing row, sits inside the
        commissioned anti-rollover horizon, and belongs to THIS lease (its
        instant at-or-after ``parked_at``), the lost closing row is committed
        as OURS: origin derived from the pending row's principal (A10), a
        STORE write only -- the device register is never touched.

        No row, an evicted window, a completing row, a foreign word's lease,
        or a failing store all return ``False``: the caller keeps the honest
        ``observed_foreign`` reading with the pending row as the operator's
        correlation.
        """
        pending = await self._latest_uncompleted_pending(
            unit_id,
            event_type="unit_resumed",
            completing_results=("resumed", "refused", "observed_foreign"),
        )
        if pending is None:
            return False
        occurred = getattr(pending, "occurred_at", None)
        if not isinstance(occurred, datetime) or occurred.tzinfo is None:
            return False
        if (now - occurred).total_seconds() > self._commissioning.max_lease_s:
            # Outside the bounded recency window: whatever resume happened can
            # no longer be ours under the anti-rollover cap.
            return False
        authorizer = getattr(pending, "principal", None)
        if not isinstance(authorizer, str) or not authorizer.strip():
            return False
        # Deliberately NOT under the unit lock: ``resume()`` itself calls
        # here from INSIDE its own critical section (the idempotent word=0
        # path), and asyncio locks are not reentrant.  The single-flight
        # guarantee is the store's epoch CAS below -- a concurrent mutation
        # bumps the epoch and this replace fails onto the honest path.
        current = await self._store.lease(unit_id)
        if (
            current is None
            or current.epoch != lease.epoch
            or not current.parked
            or current.state == LEASE_WRITE_UNVERIFIED
            or occurred < current.parked_at
        ):
            return False
        closed = dataclass_replace(
            current,
            state=LEASE_CLOSED_OPERATOR,
            closed_at=occurred,
        )
        origin = derive_origin(authorizer)
        correlation = getattr(pending, "correlation_id", None)
        request_id = (
            correlation.removeprefix("parking:unit_resumed:")
            if isinstance(correlation, str)
            and correlation.startswith("parking:unit_resumed:")
            and len(correlation) > len("parking:unit_resumed:")
            else f"adoption:{unit_id}"
        )
        try:
            await self._store.replace(
                self._row(
                    event_type="unit_resumed",
                    unit_id=unit_id,
                    principal="energypod:parking",
                    request_id=request_id,
                    result="resumed",
                    reason_codes=("adopted_pending", "resume_side_adoption"),
                    lifecycle=self._actor_lifecycle(unit_id),
                    payload={
                        "prior_word": 1,
                        "written_value": None,
                        "readback_word": 0,
                        "verified": None,
                        "origin": origin,
                        "authorizer": authorizer,
                        "adopted": True,
                        "epoch": current.epoch,
                    },
                ),
                closed,
                expected_epoch=current.epoch,
            )
        except Exception:
            return False
        self._mirror_parked(unit_id, parked=False)
        memory = self._divergence.get(unit_id)
        if memory is not None:
            # OUR resume, adopted: no foreign-resume provenance window.
            memory.resume_origin = origin
            memory.resume_observed_at = closed.closed_at
        await self._publish(
            "unit.resumed",
            {
                "unit_id": unit_id,
                "origin": origin,
                "adopted": True,
                "epoch": current.epoch,
            },
        )
        return True

    async def _expire_lease(
        self, lease: ParkLease, now: datetime, *, reason_codes: tuple[str, ...] = ()
    ) -> None:
        """The expiry ALARM: one row, one alert-tier event, no write, ever."""
        expired = dataclass_replace(lease, state=LEASE_EXPIRED)
        try:
            await self._store.replace(
                self._row(
                    event_type="unit_park_expired",
                    unit_id=lease.unit_id,
                    principal="energypod:parking",
                    request_id=f"expiry:{lease.unit_id}:{lease.epoch}",
                    result="expired",
                    reason_codes=("expired", *reason_codes),
                    lifecycle=self._actor_lifecycle(lease.unit_id),
                    payload={
                        "written_value": None,
                        "origin": derive_origin(lease.authorizer),
                        "epoch": lease.epoch,
                        "expires_at": lease.expires_at.astimezone(UTC).isoformat(),
                        "hint": EXPIRY_HINT,
                    },
                ),
                expired,
                expected_epoch=lease.epoch,
            )
        except Exception:
            return
        await self._publish(
            "unit.park_expired",
            {
                "unit_id": lease.unit_id,
                "tier": "alert",
                "epoch": lease.epoch,
                "expires_at": lease.expires_at.astimezone(UTC).isoformat(),
                "hint": EXPIRY_HINT,
            },
        )

    async def _reconcile_divergence(self, unit_id: str, lease: ParkLease | None) -> None:
        """Word vs ledger, every poll: alarmed, never fought (section 1/§3)."""
        memory = self._divergence[unit_id]
        observation = await self._latest_observation(unit_id)
        word = _optional_word(getattr(observation, "debug_mode_w", None)) if observation else None
        if word is None:
            return
        try:
            if word in _FOREIGN_MODE_WORDS or word > 1:
                if memory.foreign_mode_word != word:
                    memory.foreign_mode_word = word
                    memory.foreign_mode_first_seen = self._wall_now()
                if (
                    lease is not None
                    and lease.parked
                    and not lease.foreign_rewrite
                ):
                    # A foreign park OVER our unchanged lease: named, no write.
                    flagged = dataclass_replace(lease, foreign_rewrite=True)
                    await self._store.replace(
                        self._row(
                            event_type="unit_parked",
                            unit_id=unit_id,
                            principal="energypod:parking",
                            request_id=f"divergence:{unit_id}:{lease.epoch}",
                            result="parked",
                            reason_codes=("foreign_rewrite",),
                            lifecycle=self._actor_lifecycle(unit_id),
                            payload={
                                "prior_word": word,
                                "written_value": None,
                                "readback_word": word,
                                "verified": None,
                                "origin": derive_origin(lease.authorizer),
                                "epoch": lease.epoch,
                                "foreign_mode": {
                                    "word": word,
                                    "name": vendor_debug_mode_name(word),
                                },
                            },
                        ),
                        flagged,
                        expected_epoch=lease.epoch,
                    )
            elif word == 0:
                # A5: before reading OUR completed act as foreign, try the
                # resume-side adoption (a pending unit_resumed row plus the
                # already-0 word closes the lease as OURS inside the bounded
                # recency window -- a store write only).  Only the genuinely-
                # foreign resume falls through to the alarm.
                if (
                    lease is not None
                    and lease.parked
                    and lease.state != LEASE_WRITE_UNVERIFIED
                    and not await self._adopt_pending_resume(unit_id, lease, self._wall_now())
                ):
                    # The observed foreign resume: close the lease, no write.
                    await self._close_observed_foreign(
                        lease, request_id=f"divergence:{unit_id}:{lease.epoch}"
                    )
                if memory.resume_origin is None:
                    memory.resume_origin = "foreign"
                    memory.resume_observed_at = self._wall_now()
            elif word == 1:
                if lease is None or not lease.parked:
                    if memory.last_word is not None and memory.last_word != 1:
                        # The transition was OBSERVED in-run: the vendor-app /
                        # foreign-flip class.
                        memory.foreign_standby_origin = "foreign"
                        if memory.foreign_standby_since is None:
                            memory.foreign_standby_since = self._wall_now()
                    elif memory.foreign_standby_origin is None:
                        # Already 1 at this process's first look.  Section 4:
                        # before calling it the honest unknown, try adopting
                        # OUR crashed-then-verified park (pending row + word=1,
                        # no lease) -- a store write only, never a device
                        # write.  No row / an evicted window / a store failure
                        # degrades to the unrecorded classification.
                        if await self._adopt_pending_park(unit_id, self._wall_now()) is None:
                            memory.foreign_standby_origin = "unrecorded"
                            if memory.foreign_standby_since is None:
                                memory.foreign_standby_since = self._wall_now()
            memory.last_word = word
        except Exception:
            return

    async def _close_observed_foreign(self, lease: ParkLease, *, request_id: str) -> None:
        """Close a parked lease on the observed word=0 -- no write, one alarm."""
        closed = dataclass_replace(
            lease, state=LEASE_CLOSED_FOREIGN, closed_at=self._wall_now()
        )
        await self._store.replace(
            self._row(
                event_type="unit_resumed",
                unit_id=lease.unit_id,
                principal="energypod:parking",
                request_id=request_id,
                result="observed_foreign",
                reason_codes=("observed_foreign",),
                lifecycle=self._actor_lifecycle(lease.unit_id),
                payload={
                    "prior_word": 1,
                    "written_value": None,
                    "readback_word": 0,
                    "verified": None,
                    "origin": "foreign",
                    "epoch": lease.epoch,
                    "observed_at": closed.closed_at.astimezone(UTC).isoformat()
                    if closed.closed_at is not None
                    else None,
                },
            ),
            closed,
            expected_epoch=lease.epoch,
        )
        self._mirror_parked(lease.unit_id, parked=False)
        memory = self._divergence.get(lease.unit_id)
        if memory is not None:
            memory.resume_origin = "foreign"
            memory.resume_observed_at = closed.closed_at
        await self._publish(
            "unit.resumed",
            {
                "unit_id": lease.unit_id,
                "tier": "alert",
                "origin": "foreign",
                "written_value": None,
                "observed_at": closed.closed_at.astimezone(UTC).isoformat()
                if closed.closed_at is not None
                else None,
                "epoch": lease.epoch,
            },
        )

    # --- projections ------------------------------------------------------------------

    async def park_states(self) -> dict[str, dict[str, Any]]:
        """The ``park_state`` projection per unit (snapshot + unit detail)."""
        try:
            leases = dict(await self._store.all_leases())
        except Exception:
            leases = {}
        now = self._wall_now()
        states: dict[str, dict[str, Any]] = {}
        for unit_id in self._locks:
            lease = leases.get(unit_id)
            memory = self._divergence[unit_id]
            state = self._park_state(unit_id, lease, memory, now)
            if state is not None:
                states[unit_id] = state
        return states

    def _park_state(
        self,
        unit_id: str,
        lease: ParkLease | None,
        memory: _DivergenceMemory,
        now: datetime,
    ) -> dict[str, Any]:
        """One unit's projection, semantics split per field.

        ``max_total_s`` is the SITE's commissioned lease cap and rides every
        composed projection, parked or not (DESIGN section 3: the console's
        lease ladder reads it the moment an operator opens the park dialog --
        with NO lease open there is nothing else to bound a new park against).
        ``remaining_cap_s`` is lease-relative -- the anti-rollover figure an
        open lease still has -- and is ``None`` without one.
        """
        foreign_mode: dict[str, Any] | None = None
        if memory.foreign_mode_word is not None:
            foreign_mode = {
                "word": memory.foreign_mode_word,
                "name": vendor_debug_mode_name(memory.foreign_mode_word),
                "first_observed_at": (
                    None
                    if memory.foreign_mode_first_seen is None
                    else memory.foreign_mode_first_seen.astimezone(UTC).isoformat()
                ),
            }
        if lease is not None and lease.parked:
            remaining_cap_s = max(
                0, int((lease.rollover_cap_at() - now).total_seconds())
            )
            state: dict[str, Any] = {
                "parked": True,
                "origin": derive_origin(lease.authorizer),
                "parked_at": lease.parked_at.astimezone(UTC).isoformat(),
                "lease_expires_at": lease.expires_at.astimezone(UTC).isoformat(),
                "max_total_s": lease.max_total_s,
                "remaining_cap_s": remaining_cap_s,
                "expired": lease.state == LEASE_EXPIRED or now >= lease.expires_at,
                "reason": lease.reason,
                "authorizer": lease.authorizer,
            }
            if lease.foreign_rewrite:
                state["foreign_rewrite"] = True
            if lease.write_unverified:
                state["write_unverified"] = True
            if lease.state == LEASE_EXPIRED:
                state["hint"] = EXPIRY_HINT
            elif lease.write_unverified:
                state["hint"] = WRITE_UNVERIFIED_HINT
            if foreign_mode is not None:
                state["foreign_mode"] = foreign_mode
            return state
        if memory.last_word == 1 or (
            memory.foreign_standby_origin is not None and memory.last_word is None
        ):
            # word=1 with no controller lease: the honest origin split.  The
            # commissioned cap still rides (a NEW park over this word is the
            # sanctioned exit); no lease, no anti-rollover figure.
            return {
                "parked": True,
                "origin": memory.foreign_standby_origin or "unrecorded",
                "parked_at": (
                    None
                    if memory.foreign_standby_since is None
                    else memory.foreign_standby_since.astimezone(UTC).isoformat()
                ),
                "lease_expires_at": None,
                "max_total_s": self._commissioning.max_lease_s,
                "remaining_cap_s": None,
                "expired": False,
                "reason": None,
                "authorizer": None,
                **({"foreign_mode": foreign_mode} if foreign_mode is not None else {}),
            }
        return {
            "parked": False,
            "origin": "none",
            "parked_at": None,
            "lease_expires_at": None,
            "max_total_s": self._commissioning.max_lease_s,
            "remaining_cap_s": None,
            "expired": False,
            "reason": None,
            "authorizer": None,
            **({"foreign_mode": foreign_mode} if foreign_mode is not None else {}),
        }

    def parked_unit_ids(self) -> frozenset[str]:
        """The sync parked view advisers and readiness reasons read."""
        return frozenset(self._parked_mirror)

    def parked_facts(self, unit_id: str) -> dict[str, bool]:
        """The health classifier's parked inputs, from the lease mirror.

        ``{parked, expired, write_unverified}`` -- the composable-state
        wiring (DESIGN section 3): the expiry hint and the write_unverified
        posture name their own reasons beside ``parked``.
        """
        lease = self._lease_mirror.get(unit_id)
        if lease is None or not lease.parked:
            return {"parked": False, "expired": False, "write_unverified": False}
        return {
            "parked": True,
            "expired": lease.state == LEASE_EXPIRED,
            "write_unverified": lease.write_unverified,
        }

    def unit_is_parked(self, unit_id: str) -> bool:
        return unit_id in self._parked_mirror

    def dispatch_refusal_details(self, unit_id: str) -> dict[str, Any] | None:
        """The ``device_debug_mode_active`` provenance details for one unit.

        ``parked_provenance`` when the ledger names the unit;
        ``resume_provenance`` for the window after an observed foreign
        resume; ``foreign_mode`` for a word in 2-6 (never ``parked: true``).
        ``None`` when the controller holds no evidence for the unit.
        """
        memory = self._divergence.get(unit_id)
        details: dict[str, Any] = {}
        lease = self._lease_mirror.get(unit_id)
        if lease is not None and lease.parked:
            details["parked_provenance"] = {
                "parked_at": lease.parked_at.astimezone(UTC).isoformat(),
                "origin": derive_origin(lease.authorizer),
                "authorizer": lease.authorizer,
                "reason": lease.reason,
                "lease_expires_at": lease.expires_at.astimezone(UTC).isoformat(),
            }
        if memory is not None:
            if memory.foreign_mode_word is not None:
                # A word in 2-6 renders foreign_mode, never parked: the pod
                # is in an unexposed vendor mode, not our standby.
                details.pop("parked_provenance", None)
                details["foreign_mode"] = {
                    "word": memory.foreign_mode_word,
                    "name": vendor_debug_mode_name(memory.foreign_mode_word),
                    "note": "device in an unexposed vendor mode",
                }
            if (
                memory.resume_origin == "foreign"
                and memory.resume_observed_at is not None
                and (self._wall_now() - memory.resume_observed_at).total_seconds()
                <= RESUME_PROVENANCE_WINDOW_S
            ):
                details["resume_provenance"] = {
                    "observed_at": memory.resume_observed_at.astimezone(UTC).isoformat(),
                    "origin": "foreign",
                }
        return details or None

    # --- internals ---------------------------------------------------------------------

    def _require_commissioned(self) -> None:
        if not self._commissioning.mode_write_enabled:
            raise ParkingRefusal(
                PARK_NOT_COMMISSIONED,
                "the parking feature is commissioned but this composition is not "
                "write_enabled — the mode register can never be written here",
                {"cause": "mode_not_write_enabled"},
            )

    def _require_unit(self, unit_id: str) -> ParkActorHandle:
        handle = self._actors.get(unit_id)
        if handle is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        return handle

    def _resolve_lease_s(self, lease_s: int | None) -> int:
        max_lease_s = self._commissioning.max_lease_s
        if lease_s is None:
            return min(self._commissioning.default_lease_s, max_lease_s)
        if isinstance(lease_s, bool) or type(lease_s) is not int:
            raise ValueError("lease_s must be an integer")
        if not MIN_LEASE_S <= lease_s <= max_lease_s:
            raise ValueError(
                f"lease_s must be between {MIN_LEASE_S} and {max_lease_s} seconds"
            )
        return lease_s

    async def _unit_conflicts(self, unit_id: str) -> list[dict[str, str]]:
        """The arm-outcomes-shaped conflict causes, judged inside the lock."""
        causes: list[dict[str, str]] = []
        handle = self._actors.get(unit_id)
        lifecycle = (
            _enum_text(getattr(handle, "lifecycle", None)) if handle is not None else None
        )
        if lifecycle in ("armed_idle", "active"):
            causes.append({"unit_id": unit_id, "cause": "unit_armed"})
        try:
            active = await self._intents.active(float(self._clock.monotonic()))
        except Exception:
            active = ()
            causes.append({"unit_id": unit_id, "cause": "intent_store_unavailable"})
        for intent in active:
            source = _enum_text(getattr(intent, "source", None))
            selected = getattr(intent, "selected_unit_ids", None) or ()
            if source == "emergency_stop" or unit_id in selected:
                # A latched stop claims the unit outright (it dominates every
                # cycle); any other live request is an ordinary conflict.
                causes.append(
                    {
                        "unit_id": unit_id,
                        "cause": (
                            "latched_stop" if source == "emergency_stop" else "under_intent"
                        ),
                    }
                )
                break
        stop_units = self._latched_stop_unit_ids()
        if unit_id in stop_units and not any(
            item["cause"] == "latched_stop" for item in causes
        ):
            causes.append({"unit_id": unit_id, "cause": "latched_stop"})
        return causes

    def _latched_stop_unit_ids(self) -> frozenset[str]:
        view = self._latched_stop_units
        if view is None:
            return frozenset()
        try:
            units = view.unit_ids()
        except Exception:
            return frozenset()
        if not isinstance(units, frozenset | set):  # pragma: no cover - wiring guard
            return frozenset()
        return frozenset(units)

    def _latched_stops_for(self, unit_id: str) -> list[str]:
        """The latched stop IDS naming the unit (resume's pre-write guard)."""
        view = self._latched_stop_units
        if view is None:
            return []
        resolver = getattr(view, "stop_ids_for", None)
        if not callable(resolver):  # pragma: no cover - wiring guard
            return []
        try:
            ids = resolver(unit_id)
        except Exception:
            return []
        return sorted(str(stop_id) for stop_id in ids)

    def _closing_details(self, lease: ParkLease | None) -> dict[str, Any]:
        """``park_lease_absent`` details: the closing row's origin and time.

        The pinned shape (the wave C decoder contract):
        ``{"origin": <string>, "closed_at": <ISO timestamp or null>}`` -- the
        origin vocabulary is the projection's own (operator / foreign /
        none); a never-parked unit carries ``origin: "none"`` and a null
        ``closed_at``.
        """
        if lease is None:
            return {"origin": "none", "closed_at": None}
        if lease.state == LEASE_CLOSED_FOREIGN:
            origin = "foreign"
        else:
            # A10: the lease's own origin derives from the principal that
            # took the park -- an automation park closes ``automation`` every
            # way it can end except a foreign resume.
            origin = derive_origin(lease.authorizer)
        return {
            "origin": origin,
            "closed_at": (
                None
                if lease.closed_at is None
                else lease.closed_at.astimezone(UTC).isoformat()
            ),
        }

    async def _resume_checklist(self, unit_id: str, lease: ParkLease | None) -> dict[str, Any]:
        """The honest post-park checklist (API_CONTRACTS resume response)."""
        now_mono = float(self._clock.monotonic())
        observation = await self._latest_observation(unit_id)
        comms_age_s: float | None = None
        measured_now: float | None = None
        current_soc: float | None = None
        if observation is not None:
            captured = _finite_number(getattr(observation, "captured_at_mono", None))
            if captured is not None:
                comms_age_s = max(0.0, now_mono - captured)
            measured_now = _finite_number(getattr(observation, "battery_watts", None))
            current_soc = _finite_number(getattr(observation, "authoritative_soc_pct", None))
        soc_at_park = None if lease is None else lease.soc_pct_at_park
        drift = (
            None
            if soc_at_park is None or current_soc is None
            else round(current_soc - soc_at_park, 3)
        )
        handle = self._actors.get(unit_id)
        faults: list[str] | None
        retention_note: str | None
        faults, retention_note = await self._faults_while_parked(unit_id, lease)
        return {
            "comms_age_s": None if comms_age_s is None else round(comms_age_s, 3),
            "soc_drift_pct": drift,
            "soc_pct_at_park": soc_at_park,
            "measured_watts_now": measured_now,
            "faults_while_parked": faults,
            "faults_retention_note": retention_note,
            "latched_stops": self._latched_stops_for(unit_id),
            "latched_inhibit": bool(getattr(handle, "inhibit_latched", False)) if handle else False,
        }

    async def _faults_while_parked(
        self, unit_id: str, lease: ParkLease | None
    ) -> tuple[list[str] | None, str | None]:
        """Fault-class audit facts inside the park window; null-degrade.

        The audit trail owns incidents; the window is ``[parked_at, now]``.
        When the store's bounded recent window no longer reaches back to the
        park's start, the counts null-degrade and the retention note says so
        -- an outlived window is the honest unknown, never a fabricated empty.
        """
        if lease is None:
            return None, None
        try:
            events = await self._audit.recent(limit=_FAULT_AUDIT_SCAN_LIMIT)
        except Exception:
            return None, "audit_unavailable"
        faults: list[str] = []
        oldest_seen: datetime | None = None
        for event in events:
            if getattr(event, "unit_id", None) != unit_id:
                continue
            occurred = getattr(event, "occurred_at", None)
            if not isinstance(occurred, datetime):
                continue
            if oldest_seen is None or occurred < oldest_seen:
                oldest_seen = occurred
            if occurred < lease.parked_at:
                continue
            if getattr(event, "event_type", None) not in _FAULT_EVENT_TYPES:
                continue
            for code in getattr(event, "reason_codes", ()) or ():
                faults.append(f"{event.event_type}:{code}")
        if oldest_seen is None or oldest_seen > lease.parked_at:
            return (
                None,
                "the audit window no longer reaches back to parked_at — "
                "faults while parked are unknowable from the retained trail",
            )
        return faults, None

    def _map_debug_mode_error(self, error: DebugModeChangeError) -> ParkingRefusal:
        if error.reason == "mode_out_of_scope":
            details = dict(error.details)
            return ParkingRefusal(
                PARK_MODE_OUT_OF_SCOPE,
                "the device holds a vendor-directed mode word the controller never set "
                "(values 2-6 are permanently unexposed) — the vendor app must clear it",
                {
                    "prior_word": details.get("prior_word"),
                    "vendor_name": details.get("vendor_name"),
                },
            )
        if error.reason == "readback_unverified":
            details = dict(error.details)
            return ParkingRefusal(
                PARK_READBACK_UNVERIFIED,
                "the mode write was acknowledged but the readback did not confirm it "
                "after one retry — no lease stands on an unverified write",
                {
                    "prior_word": details.get("prior_word"),
                    "written_value": details.get("written_value"),
                    "readback_word": details.get("readback_word"),
                    "retries": details.get("retries"),
                },
            )
        details = dict(error.details)
        return ParkingRefusal(
            PARK_WRITE_FAILED,
            "the transport refused or timed out on the mode write after one retry",
            {"error_class": details.get("error_class")},
        )

    async def _latest_observation(self, unit_id: str) -> Any | None:
        try:
            return await self._observations.latest(unit_id)
        except Exception:
            return None

    def _actor_lifecycle(self, unit_id: str) -> UnitLifecycle:
        handle = self._actors.get(unit_id)
        if handle is None:
            return UnitLifecycle.DISCONNECTED
        return UnitLifecycle(_enum_text(getattr(handle, "lifecycle", UnitLifecycle.DISARMED)))

    def _mirror_parked(self, unit_id: str, *, parked: bool, lease: ParkLease | None = None) -> None:
        if parked:
            self._parked_mirror.add(unit_id)
            if lease is not None:
                self._lease_mirror[unit_id] = lease
        else:
            self._parked_mirror.discard(unit_id)
            self._lease_mirror.pop(unit_id, None)

    def _mirror_from(self, leases: Mapping[str, ParkLease]) -> None:
        for unit_id in self._locks:
            lease = leases.get(unit_id)
            if lease is not None and lease.parked:
                self._parked_mirror.add(unit_id)
                self._lease_mirror[unit_id] = lease
            else:
                self._parked_mirror.discard(unit_id)
                self._lease_mirror.pop(unit_id, None)

    def _clear_divergence(self, unit_id: str) -> None:
        self._divergence[unit_id] = _DivergenceMemory()

    def _wall_now(self) -> datetime:
        wall = self._clock.wall_now()
        if not isinstance(wall, datetime) or wall.tzinfo is None:
            raise ValueError("clock wall time must be timezone-aware")
        return wall.astimezone(UTC)

    def _row(
        self,
        *,
        event_type: str,
        unit_id: str,
        principal: str,
        request_id: str,
        result: str,
        reason_codes: tuple[str, ...],
        lifecycle: UnitLifecycle,
        payload: Mapping[str, Any],
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        return AuditEvent(
            event_id=f"parking-{uuid.uuid4().hex}",
            occurred_at=self._wall_now(),
            monotonic_offset_s=now_mono - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type=event_type,
            unit_id=unit_id,
            principal=principal,
            correlation_id=f"parking:{event_type}:{request_id}",
            policy_version=_PARK_PRINCIPAL_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"event_type": event_type, **dict(payload)}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=lifecycle,
        )

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        try:
            await self._bus.publish({"type": event_type, "payload": dict(payload)})
        except Exception:
            return


__all__ = [
    "ADOPTED_PENDING_REASON",
    "EXPIRY_HINT",
    "MIN_LEASE_S",
    "ORIGIN_AUTOMATION",
    "ORIGIN_OPERATOR",
    "PARK_ALREADY_PARKED",
    "PARK_CONFLICT_REFUSED",
    "PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED",
    "PARK_LEASE_ABSENT",
    "PARK_LEASE_CAP_REACHED",
    "PARK_MODE_OUT_OF_SCOPE",
    "PARK_NOT_COMMISSIONED",
    "PARK_READBACK_UNVERIFIED",
    "PARK_WRITE_FAILED",
    "RESUME_STOP_LATCHED",
    "WRITE_UNVERIFIED_HINT",
    "ParkCommissioning",
    "ParkController",
    "ParkingRefusal",
    "derive_origin",
]
