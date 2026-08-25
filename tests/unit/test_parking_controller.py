"""The ParkController doctrine contracts (DESIGN_POD_PARKING sections 1-4).

The §12 families this suite owns (the simulator half is B2's, the REST half
lives in tests/api/test_parking_rest.py):

- T-PARK-HAPPY: the 200 shape, durable-first pending->parked rows, events.
- T-PARK-REFUSALS: every code with EXACTLY the pinned details shape.
- T-PARK-IDEMPOTENCY: the post-restart retry resolving park_already_parked.
- T-PARK-CONCURRENCY: the single-flight critical section (park-vs-park).
- T-PARK-EXPIRY: alarm-only -- a row, an alert-tier event, NO write, ever.
- T-PARK-CRASH/RESTART: boot reconstruction (expired-in-downtime alarm,
  never a write); the crashed-then-verified park adopted at boot AND at the
  first pass (operator origin, resume needs no takeover); the evicted audit
  window degrading to ``unrecorded``; the TTL-elapsed row adopting straight
  into the expired alarm; a row older than the cap never adopted.
- T-PARK-REPLAY: the table truth (renewed rows participate; the closing set
  includes observed_foreign).
- T-PARK-FOREIGN: word=1 no lease (unrecorded/foreign + takeover), word 2-6
  (out of scope + foreign_mode + foreign_rewrite), the observed foreign
  resume closing the lease with written_value null.

SAFETY: no hardware, no sockets, no live system contact.  Deterministic
fakes only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.adapters.persistence.memory import InMemoryParkLeaseRepository
from energypod.application.actor import DebugModeChangeError
from energypod.application.parking import (
    ADOPTED_PENDING_REASON,
    EXPIRY_HINT,
    PARK_ALREADY_PARKED,
    PARK_CONFLICT_REFUSED,
    PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED,
    PARK_LEASE_ABSENT,
    PARK_LEASE_CAP_REACHED,
    PARK_MODE_OUT_OF_SCOPE,
    PARK_READBACK_UNVERIFIED,
    PARK_WRITE_FAILED,
    ParkCommissioning,
    ParkController,
    ParkingRefusal,
)
from energypod.domain.audit import AuditEvent
from energypod.domain.observations import UnitLifecycle
from energypod.domain.parking import (
    LEASE_CLOSED_FOREIGN,
    LEASE_CLOSED_OPERATOR,
    LEASE_EXPIRED,
    LEASE_OPEN,
    ParkLease,
)

WALL = datetime(2026, 8, 24, 2, 0, 0, tzinfo=UTC)
UNITS = ("mid", "rhs")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.wall = WALL

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


class FakeActor:
    """The parking-facing actor surface with a scriptable device word."""

    def __init__(
        self,
        unit_id: str,
        *,
        word: int = 0,
        lifecycle: UnitLifecycle = UnitLifecycle.DISARMED,
        inhibit_latched: bool = False,
    ) -> None:
        self.unit_id = unit_id
        self.word = word
        self.lifecycle = lifecycle
        self.inhibit_latched = inhibit_latched
        self.mode_calls: list[int] = []
        self.read_calls = 0
        self.fail_writes = 0        # transport failures left to simulate
        self.unverify_once = False  # one mismatched readback, then verify

    async def request_debug_mode_change(self, value: int) -> dict[str, Any]:
        from energypod.domain.parking import vendor_debug_mode_name

        if value not in (0, 1):  # pragma: no cover - the controller never sends these
            raise ValueError("the write domain is {0, 1}")
        prior = self.word
        if prior not in (0, 1):
            raise DebugModeChangeError(
                "mode_out_of_scope",
                {"prior_word": prior, "vendor_name": vendor_debug_mode_name(prior)},
            )
        for attempt in (1, 2):
            self.mode_calls.append(value)  # one entry per transport write attempt
            if self.fail_writes:
                self.fail_writes -= 1
                if attempt == 2:
                    raise DebugModeChangeError("write_failed", {"error_class": "TransportError"})
                continue
            if self.unverify_once and attempt == 1:
                self.unverify_once = False
                self.word = value
                raise DebugModeChangeError(
                    "readback_unverified",
                    {
                        "prior_word": prior,
                        "written_value": value,
                        "readback_word": 1 - value,
                        "retries": 1,
                    },
                )
            self.word = value
            return {
                "prior_word": prior,
                "written_value": value,
                "readback_word": value,
                "verified": True,
                "retries": 0,
            }
        raise AssertionError("unreachable")  # pragma: no cover

    async def read_debug_word(self) -> int:
        self.read_calls += 1
        return self.word


class FakeObservations:
    def __init__(self) -> None:
        self.latest_by_unit: dict[str, SimpleNamespace] = {}

    async def latest(self, unit_id: str) -> SimpleNamespace | None:
        return self.latest_by_unit.get(unit_id)

    def serve(self, unit_id: str, **facts: Any) -> None:
        current = self.latest_by_unit.get(unit_id)
        base = dict(vars(current)) if current is not None else {}
        base.update(facts)
        self.latest_by_unit[unit_id] = SimpleNamespace(**base)


@dataclass
class FakeIntent:
    id: str
    source: SimpleNamespace
    selected_unit_ids: frozenset[str]


class FakeIntents:
    def __init__(self) -> None:
        self.live: list[FakeIntent] = []

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return tuple(self.live)


class FakeAudit:
    """The event-only audit port; also the fault-window evidence source.

    ``retention_limit`` models the bounded store's eviction: when set, only
    the newest that-many rows answer ``recent`` (the outlived-window case).
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.failing = False
        self.retention_limit: int | None = None

    async def append(self, event: AuditEvent) -> None:
        if self.failing:
            raise OSError("audit store unavailable")
        self.events.append(event)

    async def recent(self, *, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]:
        if self.retention_limit is None:
            retained = self.events
        else:
            retained = self.events[-self.retention_limit :]
        return tuple(retained[-limit:])

    def of_type(self, event_type: str) -> list[AuditEvent]:
        return [event for event in self.events if event.event_type == event_type]

    @property
    def types(self) -> list[str]:
        return [event.event_type for event in self.events]


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, body: dict[str, Any]) -> int:
        self.events.append(body)
        return len(self.events)

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == event_type]


class AsyncLeaseStore:
    """The async wrapper over the real in-memory twin (the composition's).

    The committed audit rows land in the SHARED audit store exactly as the
    SQLite path's single transaction leaves them in ``audit_events``: the
    test rig's audit fake sees pending rows (through the controller's
    event-only port) and completing rows (through this store) alike.
    """

    def __init__(self, audit: FakeAudit | None = None) -> None:
        self.audit = audit
        self.store = InMemoryParkLeaseRepository(audit_sink=self._sync_sink())
        self.commit_calls: list[ParkLease] = []
        self.fail_commits = 0  # commits left to drop (the crash-window simulation)

    def _sync_sink(self) -> Any:
        audit = self.audit

        class _Sink:
            def append(self, event: AuditEvent) -> None:
                if audit is not None:
                    audit.events.append(event)

        return _Sink()

    async def commit(self, event: AuditEvent, lease: ParkLease) -> None:
        if self.fail_commits:
            # The durable-first crash window: the pending row and the verified
            # write landed; the completing transaction never did.
            self.fail_commits -= 1
            raise OSError("the process died between the verified write and the transaction")
        self.store.commit(event, lease)
        self.commit_calls.append(lease)

    async def replace(self, event: AuditEvent, lease: ParkLease, *, expected_epoch: int) -> None:
        self.store.replace(event, lease, expected_epoch=expected_epoch)
        self.commit_calls.append(lease)

    async def lease(self, unit_id: str) -> ParkLease | None:
        return self.store.lease(unit_id)

    async def all_leases(self) -> dict[str, ParkLease]:
        return self.store.all_leases()


@dataclass
class StopView:
    """The facade-side latched-stop view the controller binds."""

    units: frozenset[str] = frozenset()
    ids_by_unit: dict[str, list[str]] = field(default_factory=dict)

    def unit_ids(self) -> frozenset[str]:
        return self.units

    def stop_ids_for(self, unit_id: str) -> list[str]:
        return self.ids_by_unit.get(unit_id, [])


class Rig:
    """One controller with every port faked and scripted."""

    def __init__(self, *, max_lease_s: int = 14400, default_lease_s: int = 14400) -> None:
        self.clock = FakeClock()
        self.audit = FakeAudit()
        self.store = AsyncLeaseStore(audit=self.audit)
        self.bus = FakeBus()
        self.actors = {
            "mid": FakeActor("mid"),
            "rhs": FakeActor("rhs"),
        }
        self.observations = FakeObservations()
        self.intents = FakeIntents()
        self.stop_view = StopView()
        self.controller = ParkController(
            unit_ids=frozenset(UNITS),
            commissioning=ParkCommissioning(
                max_lease_s=max_lease_s,
                default_lease_s=default_lease_s,
                mode_write_enabled=True,
            ),
            clock=self.clock,
            store=self.store,
            audit=self.audit,
            bus=self.bus,
            actors=self.actors,
            observations=self.observations,
            intents=self.intents,
            process_instance_id="process-1",
            process_origin_mono=1000.0,
            latched_stop_units=self.stop_view,
        )

    async def park(self, unit_id: str = "mid", **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "reason": "inverter work",
            "principal_subject": "person:operator",
            "request_id": "req-1",
        }
        kwargs.update(overrides)
        return await self.controller.park(unit_id, **kwargs)

    async def renew(self, unit_id: str = "mid", lease_s: int = 3600) -> dict[str, Any]:
        return await self.controller.renew(
            unit_id,
            lease_s=lease_s,
            principal_subject="person:operator",
            request_id="req-2",
        )

    async def resume(self, unit_id: str = "mid", takeover: str | None = None) -> dict[str, Any]:
        return await self.controller.resume(
            unit_id,
            principal_subject="person:operator",
            request_id="req-3",
            takeover=takeover,
        )


@pytest.fixture
def rig() -> Rig:
    return Rig()


async def expect_refusal(coro: Any, code: str) -> ParkingRefusal:
    with pytest.raises(ParkingRefusal) as caught:
        await coro
    assert caught.value.code == code, f"expected {code}, got {caught.value.code}"
    return caught.value


# --- T-PARK-HAPPY -------------------------------------------------------------------


async def test_park_happy_path_writes_pending_then_parked_and_mints_the_lease(rig: Rig) -> None:
    rig.observations.serve("mid", authoritative_soc_pct=48.0, battery_watts=0.0)
    result = await rig.park(lease_s=7200)

    assert result["unit_id"] == "mid" and result["action"] == "park"
    assert result["prior_word"] == 0 and result["written_value"] == 1
    assert result["readback_word"] == 1 and result["verified"] is True
    assert result["lease"]["max_total_s"] == 14400
    assert result["lease"]["epoch"] == 1
    assert result["prior_state"] == {"lifecycle": "disarmed", "measured_watts": 0.0}
    # Durable-first: the pending row precedes the write; the completing row
    # and the lease land in ONE store transaction.
    rows = rig.audit.of_type("unit_parked")
    assert [row.result for row in rows] == ["pending", "parked"]
    assert rows[0].reason_codes == ("durable_first",)
    assert "readback_verified" in rows[1].reason_codes
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.epoch == 1 and lease.soc_pct_at_park == 48.0
    assert lease.reason == "inverter work" and lease.authorizer == "person:operator"
    # The bus mirrors the row.
    (parked_event,) = rig.bus.of_type("unit.parked")
    assert parked_event["payload"]["origin"] == "operator"
    assert parked_event["payload"]["epoch"] == 1
    # The parked mirror drives dispatch refusal and readiness.
    assert rig.controller.parked_unit_ids() == frozenset({"mid"})


async def test_a_park_over_a_foreign_standby_records_the_origin_transition(rig: Rig) -> None:
    """``adopted_foreign_park``: the ledger never claims we initiated a park
    we inherited -- parking over an existing word=1 still mints OUR lease with
    the transition named on the completing row."""
    rig.actors["mid"].word = 1
    await rig.park()
    rows = rig.audit.of_type("unit_parked")
    assert "adopted_foreign_park" in rows[1].reason_codes
    assert rows[1].result == "parked"


async def test_resume_happy_path_closes_the_lease_and_renders_the_checklist(rig: Rig) -> None:
    rig.observations.serve(
        "mid", authoritative_soc_pct=45.5, battery_watts=0.0, captured_at_mono=1002.0
    )
    await rig.park()
    rig.clock.advance(120.0)
    rig.actors["mid"].word = 1  # still parked
    result = await rig.resume()

    assert result["origin"] == "operator"
    assert result["prior_word"] == 1 and result["written_value"] == 0 and result["verified"]
    checklist = result["checklist"]
    assert set(checklist) == {
        "comms_age_s",
        "soc_drift_pct",
        "soc_pct_at_park",
        "measured_watts_now",
        "faults_while_parked",
        "faults_retention_note",
        "latched_stops",
        "latched_inhibit",
    }
    assert checklist["soc_pct_at_park"] == 45.5
    assert checklist["soc_drift_pct"] == 0.0
    assert checklist["latched_stops"] == [] and checklist["latched_inhibit"] is False
    assert result["degraded"] == []
    # The lease closed as the operator's, in the same transaction as the row.
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_CLOSED_OPERATOR
    assert lease.closed_at is not None
    rows = rig.audit.of_type("unit_resumed")
    assert [row.result for row in rows] == ["pending", "resumed"]
    assert "readback_verified" in rows[1].reason_codes
    assert rig.controller.parked_unit_ids() == frozenset()
    (event,) = rig.bus.of_type("unit.resumed")
    assert event["payload"]["origin"] == "operator"


async def test_renew_slides_inside_the_cap_and_participates_in_the_table_truth(rig: Rig) -> None:
    await rig.park(lease_s=3600)
    rig.clock.advance(600.0)
    result = await rig.renew(lease_s=3600)
    assert result["action"] == "renew"
    lease = await rig.store.lease("mid")
    assert lease is not None
    assert lease.expires_at == WALL + timedelta(seconds=600 + 3600)
    assert lease.epoch == 1, "a renewal keeps the epoch -- same lease, same CAS base"
    (row,) = rig.audit.of_type("unit_park_renewed")
    assert row.result == "renewed"
    (event,) = rig.bus.of_type("unit.park_renewed")
    assert event["payload"]["epoch"] == 1


# --- T-PARK-REFUSALS ------------------------------------------------------------------


async def test_park_refuses_while_armed_or_under_intent_or_latched(rig: Rig) -> None:
    """The arm-outcomes shape, singular unit, cause de-conflated."""
    rig.actors["mid"].lifecycle = UnitLifecycle.ARMED_IDLE
    refusal = await expect_refusal(rig.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details == {"units": [{"unit_id": "mid", "cause": "unit_armed"}]}

    rig.actors["mid"].lifecycle = UnitLifecycle.DISARMED
    rig.intents.live.append(
        FakeIntent("intent-1", SimpleNamespace(value="manual"), frozenset({"mid"}))
    )
    refusal = await expect_refusal(rig.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details["units"] == [{"unit_id": "mid", "cause": "under_intent"}]

    rig.intents.live.clear()
    rig.stop_view.units = frozenset({"mid"})
    rig.stop_view.ids_by_unit["mid"] = ["stop-9"]
    refusal = await expect_refusal(rig.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details["units"] == [{"unit_id": "mid", "cause": "latched_stop"}]
    # Nothing was written and no lease exists under any conflict.
    assert rig.actors["mid"].mode_calls == []
    assert await rig.store.lease("mid") is None


async def test_a_second_park_refuses_already_parked_with_the_lease(rig: Rig) -> None:
    """T-PARK-IDEMPOTENCY's post-restart half: a retried park (a fresh key,
    or the same body after entry eviction) resolves as ``park_already_parked``
    -- correct, and stated."""
    first = await rig.park()
    refusal = await expect_refusal(rig.park(), PARK_ALREADY_PARKED)
    assert refusal.details == {"lease": first["lease"]}


async def test_a_vendor_mode_word_refuses_out_of_scope_with_its_name(rig: Rig) -> None:
    rig.actors["mid"].word = 4  # Circulation
    refusal = await expect_refusal(rig.park(), PARK_MODE_OUT_OF_SCOPE)
    assert refusal.details == {"prior_word": 4, "vendor_name": "Circulation"}
    # The refused row lands; no lease is minted.
    rows = rig.audit.of_type("unit_parked")
    assert [row.result for row in rows] == ["pending", "refused"]
    assert rows[1].reason_codes == ("mode_out_of_scope",)
    assert await rig.store.lease("mid") is None


async def test_a_failed_write_refuses_with_the_error_class_after_one_retry(rig: Rig) -> None:
    rig.actors["mid"].fail_writes = 2  # both attempts fail
    refusal = await expect_refusal(rig.park(), PARK_WRITE_FAILED)
    assert refusal.details == {"error_class": "TransportError"}
    assert rig.actors["mid"].mode_calls == [1, 1], "one bounded retry, then refuse"
    assert await rig.store.lease("mid") is None, "a failed write mints no lease"


async def test_a_transport_blip_retries_once_and_parks(rig: Rig) -> None:
    rig.actors["mid"].fail_writes = 1  # first attempt fails, retry lands
    result = await rig.park()
    assert result["verified"] is True
    assert rig.actors["mid"].mode_calls == [1, 1]


async def test_an_unverified_readback_refuses_with_the_four_facts(rig: Rig) -> None:
    rig.actors["mid"].unverify_once = True
    refusal = await expect_refusal(rig.park(), PARK_READBACK_UNVERIFIED)
    assert refusal.details == {
        "prior_word": 0,
        "written_value": 1,
        "readback_word": 0,
        "retries": 1,
    }
    assert await rig.store.lease("mid") is None, "no lease stands on an unverified write"


async def test_renew_refuses_past_the_anti_rollover_cap(rig: Rig) -> None:
    await rig.park(lease_s=3600)
    rig.clock.advance(4 * 3600.0 - 60.0)
    refusal = await expect_refusal(rig.renew(lease_s=3600), PARK_LEASE_CAP_REACHED)
    assert set(refusal.details) == {"parked_at", "max_total_s", "requested_expires_at"}
    assert refusal.details["max_total_s"] == 14400
    assert refusal.details["parked_at"] == WALL.isoformat()


async def test_renew_refuses_absent_with_the_closing_row_truth(rig: Rig) -> None:
    """The pinned details shape (the wave C decoder contract): the closing
    row's origin string and ISO close time, flat; never-parked is none/null."""
    refusal = await expect_refusal(rig.renew(), PARK_LEASE_ABSENT)
    assert refusal.details == {"origin": "none", "closed_at": None}

    await rig.park(lease_s=3600)
    rig.actors["mid"].word = 1
    await rig.resume()
    refusal = await expect_refusal(rig.renew(), PARK_LEASE_ABSENT)
    assert set(refusal.details) == {"origin", "closed_at"}
    assert refusal.details["origin"] == "operator"
    assert refusal.details["closed_at"] is not None
    assert refusal.details["closed_at"].endswith("+00:00")

    # The foreign close names its own origin.
    await rig.park(lease_s=3600)
    rig.actors["mid"].word = 0
    rig.observations.serve("mid", debug_mode_w=0)
    await rig.controller.supervise()
    refusal = await expect_refusal(rig.renew(), PARK_LEASE_ABSENT)
    assert refusal.details["origin"] == "foreign"


async def test_resume_on_a_foreign_standby_requires_the_takeover_acknowledgement(rig: Rig) -> None:
    rig.actors["mid"].word = 1
    refusal = await expect_refusal(rig.resume(), PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED)
    assert refusal.details["acknowledgement"] == "FOREIGN"
    assert refusal.details["prior_word"] == 1
    assert rig.actors["mid"].mode_calls == [], "no write without the acknowledgement"

    # The acknowledgement resumes it: audited, origin foreign, no lease minted.
    result = await rig.resume(takeover="FOREIGN")
    assert result["origin"] == "foreign"
    rows = rig.audit.of_type("unit_resumed")
    assert "foreign_takeover_acknowledged" in rows[1].reason_codes
    assert await rig.store.lease("mid") is None


async def test_resume_on_a_vendor_mode_word_is_its_own_act_never_an_alias(rig: Rig) -> None:
    rig.actors["mid"].word = 3  # Discharge
    refusal = await expect_refusal(rig.resume(takeover="FOREIGN"), PARK_MODE_OUT_OF_SCOPE)
    assert refusal.details == {"prior_word": 3, "vendor_name": "Discharge"}
    assert rig.actors["mid"].mode_calls == []


async def test_resume_refuses_under_a_latched_stop_and_names_the_acknowledgement(rig: Rig) -> None:
    await rig.park()
    rig.stop_view.units = frozenset({"mid"})
    rig.stop_view.ids_by_unit["mid"] = ["stop-7"]
    refusal = await expect_refusal(rig.resume(), "resume_stop_latched")
    assert refusal.details == {
        "stop_ids": ["stop-7"],
        "acknowledgement_endpoint": "/api/v1/emergency-stop/{stop_id}/acknowledge",
    }
    assert 0 not in rig.actors["mid"].mode_calls, "no enabling write under a standing stop"


async def test_resume_on_a_normal_word_is_an_honest_no_op(rig: Rig) -> None:
    result = await rig.resume()
    assert result["origin"] == "none"
    assert result["prior_word"] == 0 and result["verified"] is True
    assert rig.actors["mid"].mode_calls == []
    # An open lease over a Normal word closes as the observed foreign resume.
    await rig.park()
    rig.actors["mid"].word = 0
    result = await rig.resume()
    assert result["origin"] == "none"
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_CLOSED_FOREIGN
    rows = rig.audit.of_type("unit_resumed")
    assert rows[-1].result == "observed_foreign"


# --- T-PARK-EXPIRY: alarm-only ----------------------------------------------------------


async def test_expiry_alarms_without_any_write(rig: Rig) -> None:
    await rig.park(lease_s=3600)
    rig.clock.advance(3601.0)
    await rig.controller.supervise()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_EXPIRED
    (row,) = rig.audit.of_type("unit_park_expired")
    assert row.result == "expired"
    (event,) = rig.bus.of_type("unit.park_expired")
    assert event["payload"]["tier"] == "alert"
    assert EXPIRY_HINT in event["payload"]["hint"]
    # ALARM-ONLY: the device word was never written by the controller.
    assert rig.actors["mid"].mode_calls == [1]
    # Still parked: the projection says expired, and the exit is RESUME.
    state = (await rig.controller.park_states())["mid"]
    assert state["parked"] is True and state["expired"] is True
    assert state["hint"] == EXPIRY_HINT
    # Expiry is idempotent: a second pass appends nothing.
    await rig.controller.supervise()
    assert len(rig.audit.of_type("unit_park_expired")) == 1


async def test_an_expired_lease_never_renews_and_a_new_park_mints_a_new_epoch(rig: Rig) -> None:
    await rig.park(lease_s=3600)
    rig.clock.advance(3601.0)
    await rig.controller.supervise()
    await expect_refusal(rig.renew(), PARK_LEASE_ABSENT)

    rig.actors["mid"].word = 1
    result = await rig.park(lease_s=3600)
    assert result["lease"]["epoch"] == 2, "fresh confirmation, new lease, monotonic epoch"


# --- T-PARK-CRASH/RESTART ----------------------------------------------------------------


async def test_boot_reconstruction_alarms_expired_in_downtime_without_writing(rig: Rig) -> None:
    await rig.park(lease_s=3600)
    # The downtime: the lease expires while the controller is down.
    rig.clock.advance(6 * 3600.0)
    fresh_audit = FakeAudit()
    fresh_bus = FakeBus()
    revived = ParkController(
        unit_ids=frozenset(UNITS),
        commissioning=ParkCommissioning(
            max_lease_s=14400, default_lease_s=14400, mode_write_enabled=True
        ),
        clock=rig.clock,
        store=rig.store,
        audit=fresh_audit,
        bus=fresh_bus,
        actors=rig.actors,
        observations=rig.observations,
        intents=rig.intents,
        process_instance_id="process-2",
        process_origin_mono=rig.clock.now,
    )
    await revived.reconstruct_at_boot()
    (row,) = rig.audit.of_type("unit_park_expired")
    assert row.result == "expired" and "expired_in_downtime" in row.reason_codes
    (event,) = fresh_bus.of_type("unit.park_expired")
    assert event["payload"]["tier"] == "alert"
    # Boot NEVER writes the mode register under any path.
    assert rig.actors["mid"].mode_calls == [1]
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_EXPIRED


async def test_boot_reconstruction_never_parks_or_unparks_a_live_lease(rig: Rig) -> None:
    await rig.park(lease_s=14400)
    boot = ParkController(
        unit_ids=frozenset(UNITS),
        commissioning=ParkCommissioning(
            max_lease_s=14400, default_lease_s=14400, mode_write_enabled=True
        ),
        clock=rig.clock,
        store=rig.store,
        audit=rig.audit,
        bus=rig.bus,
        actors=rig.actors,
        observations=rig.observations,
        intents=rig.intents,
        process_instance_id="process-2",
        process_origin_mono=rig.clock.now,
    )
    await boot.reconstruct_at_boot()
    assert rig.audit.of_type("unit_park_expired") == []
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_OPEN


# --- T-PARK-CRASH/RESTART: the crashed-then-verified park adopted -------------------


async def _crash_after_verified_write(rig: Rig) -> None:
    """Land the durable-first crash window on ``mid``: the pending row and the
    verified write landed (the word moved to 1); the completing transaction --
    the lease row -- never did."""
    rig.store.fail_commits = 1
    with pytest.raises(OSError):
        await rig.park()
    assert rig.actors["mid"].word == 1
    assert [row.result for row in rig.audit.of_type("unit_parked")] == ["pending"]
    assert await rig.store.lease("mid") is None


def _revived(
    rig: Rig,
    *,
    audit: FakeAudit | None = None,
    bus: FakeBus | None = None,
    observations: FakeObservations | None = None,
) -> ParkController:
    """The next process over the SAME durable truth (the store and the audit
    trail survive the crash; a fresh bus collects this process's events)."""
    return ParkController(
        unit_ids=frozenset(UNITS),
        commissioning=ParkCommissioning(
            max_lease_s=14400, default_lease_s=14400, mode_write_enabled=True
        ),
        clock=rig.clock,
        store=rig.store,
        audit=audit if audit is not None else rig.audit,
        bus=bus if bus is not None else rig.bus,
        actors=rig.actors,
        observations=observations if observations is not None else rig.observations,
        intents=rig.intents,
        process_instance_id="process-2",
        process_origin_mono=rig.clock.now,
    )


async def test_boot_adopts_a_crashed_then_verified_park_as_ours(rig: Rig) -> None:
    """DESIGN section 4: pending row + word=1 is the durable-first park
    COMPLETED.  Boot adopts the lease the crash lost -- operator origin, the
    row's authorizer and instant, the cap as the TTL -- with an honest
    ``adopted_pending`` completing row; the operator's RESUME then needs no
    takeover, because the park is ours."""
    rig.observations.serve("mid", debug_mode_w=1)  # the survived readback word
    await _crash_after_verified_write(rig)
    fresh_bus = FakeBus()
    revived = _revived(rig, bus=fresh_bus)
    await revived.reconstruct_at_boot()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_OPEN
    assert lease.epoch == 1 and lease.authorizer == "person:operator"
    assert lease.parked_at == WALL
    assert lease.expires_at == WALL + timedelta(seconds=14400)
    assert lease.soc_pct_at_park is None, "the crash lost the SOC snapshot -- never invented"
    rows = rig.audit.of_type("unit_parked")
    assert [row.result for row in rows] == ["pending", "parked"]
    assert rows[1].reason_codes == ("adopted_pending",)
    assert rows[1].correlation_id == rows[0].correlation_id, "the adoption completes THE request"
    assert rig.actors["mid"].mode_calls == [1], "adoption is a store write only"
    state = (await revived.park_states())["mid"]
    assert state["parked"] is True and state["origin"] == "operator"
    assert state["reason"] == ADOPTED_PENDING_REASON

    # The resume is the operator's own act: no takeover acknowledgement.
    result = await revived.resume("mid", principal_subject="person:operator", request_id="req-9")
    assert result["origin"] == "operator" and result["verified"] is True
    closed = await rig.store.lease("mid")
    assert closed is not None and closed.state == LEASE_CLOSED_OPERATOR


async def test_the_first_pass_adopts_when_the_word_first_serves(rig: Rig) -> None:
    """A fresh boot holds no served word yet, so boot reconstruction has
    nothing to judge on; the adoption joins on the FIRST supervision pass --
    exactly where the ``unrecorded`` classification used to be the only
    answer."""
    await _crash_after_verified_write(rig)
    fresh_observations = FakeObservations()
    revived = _revived(rig, observations=fresh_observations, bus=FakeBus())
    await revived.reconstruct_at_boot()
    assert await rig.store.lease("mid") is None, "no served word -- nothing to adopt on yet"

    fresh_observations.serve("mid", debug_mode_w=1)
    await revived.supervise()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_OPEN
    state = (await revived.park_states())["mid"]
    assert state["origin"] == "operator", "adopted, never 'unrecorded'"
    assert rig.actors["mid"].mode_calls == [1], "the pass never writes either"


async def test_an_evicted_pending_row_degrades_to_the_honest_unknown(rig: Rig) -> None:
    """The audit store's bounded window no longer reaches the pending row:
    adoption is honestly impossible and today's ``unrecorded`` path stands --
    never a lease minted from evidence we can no longer see."""
    await _crash_after_verified_write(rig)
    rig.audit.events.append(
        _fault_row("heartbeat_failed", ("suppressed_exception",), WALL + timedelta(minutes=30))
    )
    rig.audit.retention_limit = 1  # the pending row is gone from the window
    rig.observations.serve("mid", debug_mode_w=1)
    revived = _revived(rig, bus=FakeBus())
    await revived.reconstruct_at_boot()
    await revived.supervise()

    assert await rig.store.lease("mid") is None
    state = (await revived.park_states())["mid"]
    assert state["parked"] is True and state["origin"] == "unrecorded"


async def test_a_ttl_elapsed_pending_row_adopts_straight_into_the_expired_alarm(
    rig: Rig,
) -> None:
    """The crashed park's own cap passed while the process was down: the
    adoption lands the lease ALREADY in the expiry alarm -- the row, the
    alert-tier event, and never a write under any path."""
    rig.observations.serve("mid", debug_mode_w=1)
    await _crash_after_verified_write(rig)
    rig.clock.advance(14400.0)  # exactly the cap: recent evidence, fully elapsed
    fresh_bus = FakeBus()
    revived = _revived(rig, bus=fresh_bus)
    await revived.reconstruct_at_boot()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_EXPIRED
    (expired_row,) = rig.audit.of_type("unit_park_expired")
    assert expired_row.result == "expired"
    assert "expired_in_downtime" in expired_row.reason_codes
    (event,) = fresh_bus.of_type("unit.park_expired")
    assert event["payload"]["tier"] == "alert"
    assert rig.actors["mid"].mode_calls == [1], "boot never writes, under any path"
    state = (await revived.park_states())["mid"]
    assert state["parked"] is True and state["expired"] is True
    assert state["hint"] == EXPIRY_HINT


async def test_a_pending_row_older_than_the_cap_is_never_adopted(rig: Rig) -> None:
    """Stale evidence is the honest unknown: past the anti-rollover cap no
    lease can still be ours, so the row is not adopted on either path."""
    rig.observations.serve("mid", debug_mode_w=1)
    await _crash_after_verified_write(rig)
    rig.clock.advance(14401.0)
    revived = _revived(rig, bus=FakeBus())
    await revived.reconstruct_at_boot()
    await revived.supervise()

    assert await rig.store.lease("mid") is None
    state = (await revived.park_states())["mid"]
    assert state["origin"] == "unrecorded"


# --- T-PARK-FOREIGN: divergence is alarmed, never fought ------------------------------------


async def test_an_observed_foreign_resume_closes_the_lease_with_no_write(rig: Rig) -> None:
    await rig.park()
    # The vendor app (or a foreign writer) resumes the pod out from under us.
    rig.actors["mid"].word = 0
    rig.observations.serve("mid", debug_mode_w=0)
    await rig.controller.supervise()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_CLOSED_FOREIGN
    rows = rig.audit.of_type("unit_resumed")
    assert rows[-1].result == "observed_foreign"
    assert "observed_foreign" in rows[-1].reason_codes
    events = rig.bus.of_type("unit.resumed")
    assert events[-1]["payload"]["origin"] == "foreign"
    assert events[-1]["payload"]["tier"] == "alert"
    assert events[-1]["payload"]["written_value"] is None
    assert rig.actors["mid"].mode_calls == [1], "the controller never re-parks in response"

    # The dispatch provenance window names the foreign resume.
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is not None and details["resume_provenance"]["origin"] == "foreign"
    # The window is bounded: long after, the provenance fades.
    rig.clock.advance(901.0)
    assert rig.controller.dispatch_refusal_details("mid") is None


async def test_a_word_one_with_no_lease_projects_the_honest_unknown_first(rig: Rig) -> None:
    """``unrecorded`` -- the word was already 1 at this process's first look
    (crash-after-write residue); the honest unknown, never a fabricated
    transition.  The commissioned cap still rides (a NEW park over this word
    is the sanctioned exit); the lease-relative figure stays null."""
    rig.observations.serve("mid", debug_mode_w=1)
    await rig.controller.supervise()
    state = (await rig.controller.park_states())["mid"]
    assert state["parked"] is True and state["origin"] == "unrecorded"
    assert state["lease_expires_at"] is None
    assert state["max_total_s"] == 14400
    assert state["remaining_cap_s"] is None


async def test_the_not_parked_projection_serves_the_commissioned_cap(rig: Rig) -> None:
    """The field-semantics split (DESIGN section 3): ``max_total_s`` is the
    SITE's commissioned lease cap on every composed projection, independent of
    any lease; ``remaining_cap_s`` is lease-relative and ``None`` without an
    open one.  The console's park dialog builds its duration ladder from the
    cap alone the moment it opens -- the not-parked shape is load-bearing."""
    state = (await rig.controller.park_states())["mid"]
    assert state["parked"] is False and state["origin"] == "none"
    assert state["max_total_s"] == 14400
    assert state["remaining_cap_s"] is None


async def test_an_observed_transition_to_standby_is_the_foreign_class(rig: Rig) -> None:
    rig.observations.serve("mid", debug_mode_w=0)
    await rig.controller.supervise()
    rig.actors["mid"].word = 1
    rig.observations.serve("mid", debug_mode_w=1)
    await rig.controller.supervise()
    state = (await rig.controller.park_states())["mid"]
    assert state["origin"] == "foreign", "the 0->1 transition was OBSERVED in-run"


async def test_a_vendor_word_over_our_lease_sets_foreign_rewrite_and_renders_foreign_mode(
    rig: Rig,
) -> None:
    await rig.park()
    rig.actors["mid"].word = 5  # Fixing SOC
    rig.observations.serve("mid", debug_mode_w=5)
    await rig.controller.supervise()

    lease = await rig.store.lease("mid")
    assert lease is not None and lease.foreign_rewrite is True, "named, never fought"
    state = (await rig.controller.park_states())["mid"]
    assert state["foreign_mode"]["word"] == 5
    assert state["foreign_mode"]["name"] == "Fixing SOC"
    assert state["foreign_mode"]["first_observed_at"] is not None
    # The lease stays open -- a vendor mode is not a resume; no write, ever.
    assert lease.state == LEASE_OPEN
    assert rig.actors["mid"].mode_calls == [1]
    # The dispatch details render foreign_mode, never parked.
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is not None and "foreign_mode" in details
    assert "parked_provenance" not in details


# --- T-PARK-CONCURRENCY: single-flight -------------------------------------------------------


async def test_concurrent_parks_serialize_into_one_lease(rig: Rig) -> None:
    """Two PARKs racing on one unit: the critical section admits exactly one
    write sequence; the loser resolves ``park_already_parked``."""
    results = await asyncio.gather(rig.park(), rig.park(), return_exceptions=True)
    outcomes = sorted(
        result["action"] if isinstance(result, dict) else result.code for result in results
    )
    assert outcomes == ["park", "park_already_parked"]
    assert rig.actors["mid"].mode_calls == [1]
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.epoch == 1


async def test_parked_provenance_rides_the_dispatch_refusal(rig: Rig) -> None:
    await rig.park()
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is not None
    assert set(details["parked_provenance"]) == {
        "parked_at",
        "origin",
        "authorizer",
        "reason",
        "lease_expires_at",
    }
    # A10: the derived origin — an operator park renders "operator" everywhere.
    assert details["parked_provenance"]["origin"] == "operator"
    assert details["parked_provenance"]["reason"] == "inverter work"


# --- the unverified-resume posture --------------------------------------------------------------


async def test_an_unverified_resume_leaves_the_lease_in_the_terminal_posture(rig: Rig) -> None:
    """Our acts stay ours when they fail: an ACKed-but-unverified resume moves
    the lease to ``write_unverified`` (parked stays true), never foreign."""
    await rig.park()
    rig.actors["mid"].word = 1
    rig.actors["mid"].unverify_once = True
    await expect_refusal(rig.resume(), PARK_READBACK_UNVERIFIED)

    lease = await rig.store.lease("mid")
    assert lease is not None
    assert lease.state == "write_unverified" and lease.write_unverified is True
    assert rig.controller.parked_unit_ids() == frozenset({"mid"}), "still parked"
    state = (await rig.controller.park_states())["mid"]
    assert state["parked"] is True and state["write_unverified"] is True
    rows = rig.audit.of_type("unit_resumed")
    assert rows[-1].result == "refused"
    assert "write_unverified" in rows[-1].reason_codes


async def test_a_failed_resume_write_leaves_the_lease_retryable(rig: Rig) -> None:
    """A transport-refused resume write (never ACKed) changes nothing: the
    lease stays open, the operator retries."""
    await rig.park()
    rig.actors["mid"].word = 1
    rig.actors["mid"].fail_writes = 2
    await expect_refusal(rig.resume(), PARK_WRITE_FAILED)
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_OPEN

    rig.actors["mid"].fail_writes = 0
    result = await rig.resume()
    assert result["verified"] is True
    assert (await rig.store.lease("mid")).state == LEASE_CLOSED_OPERATOR  # type: ignore[union-attr]


# --- commissioning ------------------------------------------------------------------------------


async def test_a_non_write_enabled_composition_refuses_with_its_cause(rig: Rig) -> None:
    rig.controller._commissioning = ParkCommissioning(  # type: ignore[attr-defined]
        max_lease_s=14400, default_lease_s=14400, mode_write_enabled=False
    )
    refusal = await expect_refusal(rig.park(), "park_not_commissioned")
    assert refusal.details == {"cause": "mode_not_write_enabled"}


async def test_lease_bounds_are_validated(rig: Rig) -> None:
    with pytest.raises(ValueError, match="lease_s must be between 60 and 14400"):
        await rig.park(lease_s=59)
    with pytest.raises(ValueError, match="lease_s must be between 60 and 14400"):
        await rig.park(lease_s=14401)
    # The default lease rides the block's default_lease_s.
    result = await rig.park(lease_s=None)
    assert result["lease"]["max_total_s"] == 14400


# --- the resume checklist's fault window -------------------------------------------


async def test_faults_while_parked_collect_the_audit_window_facts(rig: Rig) -> None:
    rig.observations.serve("mid", authoritative_soc_pct=50.0)
    await rig.park(lease_s=3600)
    # Two fault-class facts inside the park window, one benign row.
    rig.audit.events.append(
        _fault_row("heartbeat_failed", ("suppressed_exception",), WALL + timedelta(minutes=5))
    )
    rig.audit.events.append(
        _fault_row(
            "actuation_incoherent",
            ("authorized_not_actuating",),
            WALL + timedelta(minutes=6),
        )
    )
    rig.audit.events.append(
        _fault_row("unit_parked", ("readback_verified",), WALL + timedelta(minutes=7))
    )
    rig.actors["mid"].word = 1
    result = await rig.resume()
    faults = result["checklist"]["faults_while_parked"]
    assert faults is not None
    assert "heartbeat_failed:suppressed_exception" in faults
    assert "actuation_incoherent:authorized_not_actuating" in faults
    assert all(not fault.startswith("unit_parked") for fault in faults), (
        "the narrative rows are not fault-class facts"
    )
    assert result["checklist"]["faults_retention_note"] is None


async def test_an_outlived_evidence_window_null_degrades_with_the_retention_note(
    rig: Rig,
) -> None:
    """The audit window's bounded recent read no longer reaches parked_at:
    the faults null-degrade and the note says so -- never a fabricated empty
    list that would read as 'nothing happened while parked'."""
    rig.observations.serve("mid", authoritative_soc_pct=50.0)
    await rig.park(lease_s=3600)
    # The retained trail starts AFTER the park (the older rows evicted).
    rig.audit.events.append(
        _fault_row("heartbeat_failed", ("suppressed_exception",), WALL + timedelta(hours=2))
    )
    rig.audit.retention_limit = 2  # the parked-at rows are gone from the window
    rig.clock.advance(3 * 3600.0)  # the resume happens hours later
    rig.actors["mid"].word = 1
    result = await rig.resume()
    assert result["checklist"]["faults_while_parked"] is None
    assert result["checklist"]["faults_retention_note"] is not None
    assert "unknowable" in result["checklist"]["faults_retention_note"]


def _fault_row(event_type: str, codes: tuple[str, ...], occurred: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=f"fault-{event_type}-{occurred.isoformat()}",
        occurred_at=occurred,
        monotonic_offset_s=1.0,
        process_instance_id="process-1",
        event_type=event_type,
        unit_id="mid",
        principal="energypod:runtime",
        correlation_id="fleet-heartbeat",
        policy_version="runtime",
        configuration_version=1,
        observation_sequences={},
        reason_codes=codes,
        requested_active_w=0,
        authorized_active_w=0,
        request_fingerprint="fingerprint",
        response_fingerprint="fingerprint",
        result="suppressed",
        lifecycle=UnitLifecycle.DISARMED,
    )
