"""Mutation-round killing tests: the ParkController state machine (2026-08-24).

From the pod-parking mutation round (isolated worktree, HEAD a084920): 386 of
891 mutants survived against the controller suite. This file kills the
SEMANTIC survivors -- the ones where a behavioral pin was genuinely missing:

- the wire code/hint literals (pinning the SYMBOL tautologically follows a
  mutated constant; the literals are the contract);
- commissioning and roster validation, the controller's own lease domain,
  and the timezone-aware wall clock guard;
- the exact park row shapes (pending payload, reason-code sets, correlation,
  monotonic offset, zero-watts, independently recomputed fingerprints);
- inherited-park semantics (a re-park over an expired lease is not an
  adoption; a first park over Normal is never "adopted_foreign_park");
- the anti-rollover boundary: renewal to EXACTLY parked_at + max_lease_s is
  allowed;
- expiry exactly at the deadline (alarm, never a write) and the exact expiry
  row/event; the post-expiry lease staying OURS in the projection;
- boot reconstruction at the exact deadline, continuing past unknown units,
  and refreshing the sync parked mirror;
- ``parked_facts``/``unit_is_parked`` across the parked/expired postures
  (the health classifier's inputs);
- conflict causes: the ACTIVE lifecycle, the intent-store failure class, and
  the emergency-stop-sourced intent claiming a unit it does not select;
- resume: an UNMAPPED vendor word (> 6) refuses out of scope; the takeover
  refusal names when the standby was observed; a failed bookkeeping close
  degrades to ``audit_unavailable`` while the resume stands; the no-op
  response is exact;
- the checklist's comms-age clamp, rounding, and latched-inhibit fact;
- ``dispatch_refusal_details``' foreign_mode shape and the
  resume-provenance window at its exact boundary;
- the closing-details origin vocabulary (foreign, expired).

The audit-payload KEY clusters ("XXkeyXX"-class mutants across the park/
resume/renew rows) are queued for a dedicated row-pinning pass, not chased
one key at a time here.

SAFETY: deterministic fakes only -- the shared controller rig.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import energypod.application.parking as parking_module
from energypod.application.parking import (
    EXPIRY_HINT,
    MIN_LEASE_S,
    PARK_CONFLICT_REFUSED,
    PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED,
    PARK_LEASE_ABSENT,
    PARK_MODE_OUT_OF_SCOPE,
    RESUME_PROVENANCE_WINDOW_S,
    WRITE_UNVERIFIED_HINT,
    ParkCommissioning,
    ParkController,
)
from energypod.domain.observations import UnitLifecycle

from .test_parking_controller import WALL, FakeIntent, Rig, expect_refusal


@pytest.fixture
def rig() -> Rig:
    return Rig()


def _fp(facts: dict[str, Any]) -> str:
    encoded = json.dumps(
        facts, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --- the wire vocabulary, pinned as literals ----------------------------------------


def test_the_wire_code_and_hint_literals_are_pinned() -> None:
    assert parking_module.PARK_CONFLICT_REFUSED == "park_conflict_refused"
    assert parking_module.PARK_MODE_OUT_OF_SCOPE == "park_mode_out_of_scope"
    assert parking_module.PARK_WRITE_FAILED == "park_write_failed"
    assert parking_module.PARK_READBACK_UNVERIFIED == "park_readback_unverified"
    assert parking_module.PARK_LEASE_CAP_REACHED == "park_lease_cap_reached"
    assert parking_module.PARK_LEASE_ABSENT == "park_lease_absent"
    assert (
        parking_module.PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED
        == "park_foreign_word_acknowledgement_required"
    )
    assert parking_module.RESUME_STOP_LATCHED == "resume_stop_latched"
    assert EXPIRY_HINT == "lease expired — Resume is an operator act"
    assert WRITE_UNVERIFIED_HINT == (
        "resume write unverified — the pod may still be parked; "
        "Repeat RESUME and watch the readback, then verify the mode word in the "
        "vendor app before any physical work (parking is not electrical isolation)"
    )


def test_commissioning_and_unit_roster_validation() -> None:
    with pytest.raises(ValueError, match="^max_lease_s must be at least the minimum lease$"):
        ParkCommissioning(
            max_lease_s=MIN_LEASE_S - 1, default_lease_s=MIN_LEASE_S, mode_write_enabled=True
        )
    with pytest.raises(ValueError, match="^default_lease_s must sit inside the lease bounds$"):
        ParkCommissioning(max_lease_s=600, default_lease_s=7200, mode_write_enabled=True)
    with pytest.raises(ValueError, match="^mode_write_enabled must be boolean$"):
        ParkCommissioning(max_lease_s=600, default_lease_s=60, mode_write_enabled=1)  # type: ignore[arg-type]
    ParkCommissioning(
        max_lease_s=MIN_LEASE_S, default_lease_s=MIN_LEASE_S, mode_write_enabled=True
    )
    with pytest.raises(ValueError, match="^unit_ids must not be empty$"):
        ParkController(
            unit_ids=frozenset(),
            commissioning=ParkCommissioning(
                max_lease_s=600, default_lease_s=60, mode_write_enabled=True
            ),
            clock=SimpleNamespace(),
            store=SimpleNamespace(),
            audit=SimpleNamespace(),
            bus=SimpleNamespace(),
            actors={},
            observations=SimpleNamespace(),
            intents=SimpleNamespace(),
            process_instance_id="p",
            process_origin_mono=0.0,
        )


# --- the state machine --------------------------------------------------------------


async def test_the_controller_lease_domain_is_enforced(rig: Rig) -> None:
    with pytest.raises(ValueError, match="^lease_s must be an integer$"):
        await rig.park(lease_s=True)
    with pytest.raises(ValueError, match="^lease_s must be an integer$"):
        await rig.park(lease_s=3600.5)
    with pytest.raises(ValueError, match="^lease_s must be between 60 and 14400 seconds$"):
        await rig.park(lease_s=59)
    with pytest.raises(ValueError, match="^lease_s must be between 60 and 14400 seconds$"):
        await rig.park(lease_s=14401)
    assert (await rig.park(lease_s=MIN_LEASE_S))["lease"]["epoch"] == 1


async def test_a_naive_wall_clock_refuses_rather_than_shifting_time(rig: Rig) -> None:
    rig.clock.wall = rig.clock.wall.replace(tzinfo=None)
    with pytest.raises(ValueError, match="^clock wall time must be timezone-aware$"):
        await rig.park()


async def test_a_normal_park_reason_code_set_is_exact_and_rows_carry_the_full_shape(
    rig: Rig,
) -> None:
    rig.actors["mid"].lifecycle = UnitLifecycle.INHIBITED
    await rig.park(reason="inverter work", lease_s=600)
    pending, parked_row = rig.audit.of_type("unit_parked")
    assert pending.result == "pending"
    assert pending.reason_codes == ("durable_first",)
    # AuditEvent carries its payload only as the fingerprint: recomputing the
    # fingerprint from the EXPECTED payload pins every key and value of the
    # durable row's fact set.
    pending_payload = {
        "prior_word": None,
        "written_value": None,
        "readback_word": None,
        "verified": False,
        "origin": "operator",
        "authorizer": "person:operator",
        "reason": "inverter work",
        "epoch": 1,
        "lease_s": 600,
    }
    assert pending.lifecycle == UnitLifecycle.INHIBITED, "rows carry the actor's lifecycle"
    assert pending.event_id.startswith("parking-")
    assert pending.correlation_id == "parking:unit_parked:req-1"
    assert pending.monotonic_offset_s == rig.clock.now - 1000.0
    assert pending.configuration_version == 0
    assert pending.requested_active_w == 0 and pending.authorized_active_w == 0
    assert pending.request_fingerprint == _fp(
        {"event_type": "unit_parked", **pending_payload}
    )
    assert pending.response_fingerprint == _fp({"result": "pending"})
    assert parked_row.result == "parked"
    assert parked_row.reason_codes == (
        "readback_verified",
    ), "a first park over a Normal word is never an adopted foreign park"
    assert parked_row.request_fingerprint == _fp(
        {
            "event_type": "unit_parked",
            "prior_word": 0,
            "written_value": 1,
            "readback_word": 1,
            "verified": True,
            "origin": "operator",
            "authorizer": "person:operator",
            "reason": "inverter work",
            "epoch": 1,
            "parked_at": WALL.isoformat(),
            "expires_at": (WALL + timedelta(seconds=600)).isoformat(),
            "max_total_s": 14400,
        }
    )
    (event,) = rig.bus.of_type("unit.parked")
    assert event["payload"]["prior_word"] == 0
    assert event["payload"]["readback_word"] == 1
    assert event["payload"]["principal"] == "person:operator"


async def test_a_re_park_over_an_expired_lease_is_not_an_inheritance(rig: Rig) -> None:
    """The word is STILL 1 (our own standby under the expired lease): the
    fresh park inherits nothing -- the ``adopted_foreign_park`` code is for a
    foreign word with no controller lease behind it."""
    await rig.park(lease_s=600)
    rig.clock.advance(700.0)
    await rig.controller.supervise()
    assert rig.actors["mid"].word == 1
    result = await rig.park(lease_s=600)  # fresh park over the expired lease
    parked_row = rig.audit.of_type("unit_parked")[-1]
    assert parked_row.reason_codes == ("readback_verified",)
    assert result["lease"]["epoch"] == 2


async def test_renew_to_exactly_the_cap_is_allowed(rig: Rig) -> None:
    await rig.park(lease_s=600)
    renewed = await rig.renew(lease_s=14400)
    assert renewed["action"] == "renew"
    assert renewed["lease"]["epoch"] == 1
    row = rig.audit.of_type("unit_park_renewed")[-1]
    assert row.reason_codes == ("renewed",)
    assert row.request_fingerprint == _fp(
        {
            "event_type": "unit_park_renewed",
            "authorizer": "person:operator",
            "origin": "operator",
            "epoch": 1,
            "parked_at": WALL.isoformat(),
            "expires_at": (WALL + timedelta(seconds=14400)).isoformat(),
            "max_total_s": 14400,
            "reason": "inverter work",
        }
    )


async def test_expiry_fires_exactly_at_the_deadline_with_the_exact_row(rig: Rig) -> None:
    await rig.park(lease_s=600)
    rig.clock.advance(600.0)  # exactly at expires_at
    await rig.controller.supervise()
    (row,) = rig.audit.of_type("unit_park_expired")
    assert row.principal == "energypod:parking"
    assert row.correlation_id == "parking:unit_park_expired:expiry:mid:1"
    assert row.result == "expired"
    assert row.reason_codes == ("expired",)
    assert row.request_fingerprint == _fp(
        {
            "event_type": "unit_park_expired",
            "written_value": None,
            "origin": "operator",
            "epoch": 1,
            "expires_at": rig.clock.wall.isoformat(),
            "hint": EXPIRY_HINT,
        }
    )
    (event,) = rig.bus.of_type("unit.park_expired")
    assert event["payload"]["tier"] == "alert"
    assert event["payload"]["epoch"] == 1
    assert event["payload"]["hint"] == EXPIRY_HINT
    assert rig.actors["mid"].mode_calls == [1], "expiry never writes"


async def test_after_expiry_the_lease_stays_ours_in_the_projection(rig: Rig) -> None:
    """The expired lease is still OUR parked lease: the divergence pass must
    not reclassify a post-expiry standby word as a foreign park."""
    await rig.park(lease_s=600)
    rig.clock.advance(700.0)
    await rig.controller.supervise()
    states = await rig.controller.park_states()
    assert states["mid"]["parked"] is True
    assert states["mid"]["origin"] == "operator"
    assert states["mid"]["expired"] is True
    assert states["mid"]["hint"] == EXPIRY_HINT


async def test_boot_expiry_at_the_boundary_and_the_scan_continues(rig: Rig) -> None:
    await rig.park(unit_id="rhs", lease_s=600)
    rig.clock.advance(600.0)  # exactly at the deadline, during "downtime"
    rebuilt = Rig()
    rebuilt.store.store = rig.store.store  # the same table, a rebuilt controller
    rebuilt.clock.advance(600.0)  # downtime ran the clock to the exact deadline
    await rebuilt.controller.reconstruct_at_boot()
    rows = rig.audit.of_type("unit_park_expired")  # the shared table's sink
    assert [row.unit_id for row in rows] == ["rhs"]
    assert rows[0].reason_codes == ("expired", "expired_in_downtime")


async def test_parked_facts_and_unit_is_parked_across_the_postures(rig: Rig) -> None:
    assert rig.controller.parked_facts("mid") == {
        "parked": False,
        "expired": False,
        "write_unverified": False,
    }
    assert rig.controller.unit_is_parked("mid") is False
    await rig.park(lease_s=600)
    assert rig.controller.parked_facts("mid") == {
        "parked": True,
        "expired": False,
        "write_unverified": False,
    }
    assert rig.controller.unit_is_parked("mid") is True
    assert rig.controller.unit_is_parked("rhs") is False
    rig.clock.advance(700.0)
    await rig.controller.supervise()
    assert rig.controller.parked_facts("mid") == {
        "parked": True,
        "expired": True,
        "write_unverified": False,
    }


async def test_boot_rebuild_refreshes_the_lease_mirror(rig: Rig) -> None:
    await rig.park(lease_s=600)
    rebuilt = Rig()
    rebuilt.store.store = rig.store.store
    await rebuilt.controller.reconstruct_at_boot()
    assert rebuilt.controller.unit_is_parked("mid") is True
    assert rebuilt.controller.parked_facts("mid") == {
        "parked": True,
        "expired": False,
        "write_unverified": False,
    }


async def test_conflict_causes_cover_the_active_intent_failure_and_stop_classes(
    rig: Rig,
) -> None:
    rig.actors["mid"].lifecycle = UnitLifecycle.ACTIVE
    refusal = await expect_refusal(rig.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details == {"units": [{"unit_id": "mid", "cause": "unit_armed"}]}

    failing = Rig()
    original_active = failing.intents.active

    async def broken(_now: float) -> tuple[Any, ...]:
        raise OSError("intent store down")

    failing.intents.active = broken  # type: ignore[method-assign]
    refusal = await expect_refusal(failing.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details == {
        "units": [{"unit_id": "mid", "cause": "intent_store_unavailable"}]
    }
    failing.intents.active = original_active  # type: ignore[method-assign]

    stopped = Rig()
    # An emergency-stop-sourced intent claims the unit even without selecting
    # it; the cause is latched_stop, never under_intent.
    stopped.intents.live.append(
        FakeIntent("stop-1", SimpleNamespace(value="emergency_stop"), frozenset({"rhs"}))
    )
    refusal = await expect_refusal(stopped.park(), PARK_CONFLICT_REFUSED)
    assert refusal.details == {"units": [{"unit_id": "mid", "cause": "latched_stop"}]}


async def test_an_unmapped_vendor_word_refuses_resume_out_of_scope(rig: Rig) -> None:
    """Word 7 is outside the named 2-6 vocabulary: the ``> 1`` backstop must
    refuse it exactly like a named vendor mode, never write over it."""
    rig.actors["mid"].word = 7
    refusal = await expect_refusal(rig.resume(), PARK_MODE_OUT_OF_SCOPE)
    assert refusal.details == {"prior_word": 7, "vendor_name": None}
    assert "unmapped" in refusal.message
    assert rig.actors["mid"].mode_calls == []


async def test_the_takeover_refusal_names_when_the_standby_was_observed(rig: Rig) -> None:
    rig.actors["mid"].word = 1
    rig.observations.serve("mid", debug_mode_w=1)
    await rig.controller.supervise()  # establishes foreign_standby_since
    refusal = await expect_refusal(rig.resume(), PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED)
    assert refusal.details["acknowledgement"] == "FOREIGN"
    assert refusal.details["observed_since"] == rig.clock.wall.isoformat()


async def test_a_failed_bookkeeping_resume_stands_degraded(rig: Rig) -> None:
    await rig.park(lease_s=600)

    async def failing_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("lease table unavailable")

    rig.store.replace = failing_replace  # type: ignore[method-assign]
    result = await rig.resume()
    assert result["verified"] is True
    assert result["degraded"] == ["audit_unavailable"]
    assert rig.actors["mid"].word == 0, "the resume stands whatever the record does"


async def test_the_no_op_resume_response_is_exact(rig: Rig) -> None:
    result = await rig.resume()
    assert result == {
        "unit_id": "mid",
        "action": "resume",
        "prior_word": 0,
        "written_value": 0,
        "readback_word": 0,
        "verified": True,
        "as_of": rig.clock.wall.isoformat(),
        "origin": "none",
        "checklist": result["checklist"],
        "degraded": [],
    }
    assert set(result["checklist"]) == {
        "comms_age_s",
        "soc_drift_pct",
        "soc_pct_at_park",
        "measured_watts_now",
        "faults_while_parked",
        "faults_retention_note",
        "latched_stops",
        "latched_inhibit",
    }


async def test_the_checklist_clamps_and_reports_the_latch(rig: Rig) -> None:
    rig.observations.serve(
        "mid",
        captured_at_mono=rig.clock.now + 5.0,  # clock skew: the age clamps to 0
        battery_watts=-123.25,
        authoritative_soc_pct=55.25,
    )
    await rig.park(lease_s=600)
    rig.actors["mid"].inhibit_latched = True
    checklist = (await rig.resume())["checklist"]
    assert checklist["comms_age_s"] == 0.0
    assert checklist["measured_watts_now"] == -123.25
    assert checklist["latched_inhibit"] is True


async def test_dispatch_refusal_details_render_foreign_mode_and_the_window(rig: Rig) -> None:
    await rig.park(lease_s=600)
    rig.observations.serve("mid", debug_mode_w=4)
    await rig.controller.supervise()
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is not None
    assert details["foreign_mode"] == {
        "word": 4,
        "name": "Circulation",
        "note": "device in an unexposed vendor mode",
    }
    assert "parked_provenance" not in details

    # The resume-provenance window is inclusive at its exact boundary.
    rig2 = Rig()
    await rig2.park(lease_s=600)
    rig2.actors["mid"].word = 0
    rig2.observations.serve("mid", debug_mode_w=0)
    await rig2.controller.supervise()  # the observed foreign resume closes the lease
    details2 = rig2.controller.dispatch_refusal_details("mid")
    assert details2 is not None and "resume_provenance" in details2
    assert details2["resume_provenance"] == {
        "observed_at": rig2.clock.wall.isoformat(),
        "origin": "foreign",
    }
    rig2.clock.advance(RESUME_PROVENANCE_WINDOW_S)  # exactly at the window edge
    assert "resume_provenance" in (rig2.controller.dispatch_refusal_details("mid") or {})


async def test_closing_details_name_the_foreign_and_expired_origins(rig: Rig) -> None:
    fresh = Rig()
    refusal = await expect_refusal(fresh.renew(), PARK_LEASE_ABSENT)
    assert refusal.details == {"origin": "none", "closed_at": None}

    expired = Rig()
    await expired.park(lease_s=600)
    expired.clock.advance(700.0)
    await expired.controller.supervise()
    refusal = await expect_refusal(expired.renew(), PARK_LEASE_ABSENT)
    assert refusal.details["origin"] == "operator"
    assert refusal.details["closed_at"] is None, "expiry never closes the lease row"

    foreign = Rig()
    await foreign.park(lease_s=600)
    foreign.actors["mid"].word = 0
    foreign.observations.serve("mid", debug_mode_w=0)
    await foreign.controller.supervise()
    refusal = await expect_refusal(foreign.renew(), PARK_LEASE_ABSENT)
    assert refusal.details["origin"] == "foreign"

    unverified = Rig()
    await unverified.park(lease_s=600)
    unverified.actors["mid"].unverify_once = True
    from energypod.application.parking import PARK_READBACK_UNVERIFIED

    await expect_refusal(unverified.resume(), PARK_READBACK_UNVERIFIED)
    refusal = await expect_refusal(unverified.renew(), PARK_LEASE_ABSENT)
    assert refusal.details["origin"] == "operator"
    assert refusal.details["closed_at"] is not None
