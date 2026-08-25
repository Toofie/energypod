"""The Stage-R amendments to the parking machinery (DESIGN_BATTERY_HEALTH_
WATCH §7.2 step 5, §10 A10, §12 A5 — DESIGN_POD_PARKING §3/§4 as amended).

Two mechanisms, one doctrine: OUR acts stay ours when they fail, and the
ledger never fabricates an origin.

- **A10 — the derived ``automation`` origin**: a park under the composed
  ``energypod:health-adviser`` principal renders ``origin: automation`` on
  the pending row, the completing row, the bus event, ``park_state``, the
  dispatch provenance, and the closing details — byte-identical ``operator``
  rendering for every human principal; a word=1 under an open automation
  lease is OURS (resume needs no takeover); a boot-ADOPTED automation park
  derives ``automation`` from the adopted row's principal.
- **A5 — the resume-side adoption**: a crash between the VERIFIED resume
  write and the lease-closing transaction leaves ``word=0, open lease, a
  pending unit_resumed row`` — the divergence pass (and a fresh RESUME
  landing on the shape) commits the lost closing row as OURS, a store write
  only; outside the bounded recency window the honest ``observed_foreign``
  reading stands with the pending row as the operator's correlation.

The ParkController rig is the standing suite's own (test_parking_controller).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from energypod.application.parking import derive_origin
from energypod.domain.parking import LEASE_CLOSED_OPERATOR, LEASE_OPEN

from .test_parking_controller import Rig

HEALTH_PRINCIPAL = "energypod:health-adviser"


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC)


# --- A10: the derived origin ---------------------------------------------------------


def test_the_origin_derivation_is_the_principal_prefix_and_nothing_else() -> None:
    assert derive_origin("energypod:health-adviser") == "automation"
    assert derive_origin("energypod:parking") == "automation"
    assert derive_origin("person:operator") == "operator"
    assert derive_origin("operator@home") == "operator"
    assert derive_origin("") == "operator"


async def test_an_automation_park_renders_automation_everywhere() -> None:
    """The health principal's park: the lease bound ``hold_s + 60``, the
    rows under the health principal, the bus event, ``park_state``, and the
    dispatch provenance all derive ``automation``."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0, battery_watts=0.0)
    result = await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery (census flag + probe no-response)",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-recovery:mid:2026-08-25",
        lease_s=150,
    )
    lease = result["lease"]
    span = (
        _parse(lease["expires_at"]) - _parse(lease["parked_at"])
    ).total_seconds()
    assert span == 150  # §7.2 step 3: the lease brackets the hold
    parked_rows = rig.audit.of_type("unit_parked")
    assert {row.principal for row in parked_rows} == {HEALTH_PRINCIPAL}
    (event,) = rig.bus.of_type("unit.parked")
    assert event["payload"]["origin"] == "automation"
    assert event["payload"]["principal"] == HEALTH_PRINCIPAL
    states = await rig.controller.park_states()
    state = states["mid"]
    assert state["parked"] is True and state["origin"] == "automation"
    assert state["authorizer"] == HEALTH_PRINCIPAL
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is not None and details["parked_provenance"]["origin"] == "automation"


async def test_an_operator_park_still_renders_operator_everywhere() -> None:
    """The amendment is additive: a human principal's park is byte-identical
    to its standing rendering (``operator`` on every surface)."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=48.0)
    await rig.park()
    (event,) = rig.bus.of_type("unit.parked")
    assert event["payload"]["origin"] == "operator"
    states = await rig.controller.park_states()
    state = states["mid"]
    assert state["origin"] == "operator"


async def test_an_automation_resume_needs_no_takeover_and_closes_automation() -> None:
    """A word=1 under an open ``automation`` lease is OURS — resume by the
    program's own completion or by the operator, no takeover; the closing
    event and lease derive ``automation``."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-park",
    )
    result = await rig.controller.resume(
        "mid", principal_subject=HEALTH_PRINCIPAL, request_id="health-resume"
    )
    assert result["verified"] is True
    assert result["origin"] == "automation"
    (event,) = rig.bus.of_type("unit.resumed")
    assert event["payload"]["origin"] == "automation"
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_CLOSED_OPERATOR
    assert lease.authorizer == HEALTH_PRINCIPAL
    details = rig.controller.dispatch_refusal_details("mid")
    assert details is None or "parked_provenance" not in details


async def test_the_closing_details_derive_the_leases_own_origin() -> None:
    """An automation lease that ends without a resume (the expiry alarm)
    keeps the derived origin on its row's event; the foreign close stays
    ``foreign`` (untouched semantics)."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-park-2",
        lease_s=60,
    )
    rig.clock.advance(120.0)  # the lease expires: the ALARM, never a write
    await rig.controller.supervise()
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == "expired"
    states = await rig.controller.park_states()
    state = states["mid"]
    assert state["parked"] is True and state["origin"] == "automation"


async def test_a_boot_adopted_automation_park_derives_automation() -> None:
    """A10's honest edge: the adopted lease's origin comes from the ADOPTED
    ROW's principal — a crashed health-adviser park adopts as ``automation``,
    and its park_state renders it."""
    rig = Rig()
    rig.store.fail_commits = 1  # the crash: pending row + verified write land, the commit doesn't
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    with contextlib.suppress(OSError):
        await rig.controller.park(
            "mid",
            reason="nightly health-watch recovery",
            principal_subject=HEALTH_PRINCIPAL,
            request_id="health-park-crash",
        )
    rig.actors["mid"].word = 1  # the write DID land before the crash
    rig.observations.serve("mid", debug_mode_w=1)
    rig.clock.advance(5.0)
    await rig.controller.supervise()
    states = await rig.controller.park_states()
    state = states["mid"]
    assert state["parked"] is True
    assert state["origin"] == "automation"
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.authorizer == HEALTH_PRINCIPAL
    adopted_rows = [
        row for row in rig.audit.of_type("unit_parked") if "adopted_pending" in row.reason_codes
    ]
    assert adopted_rows


# --- A5: the resume-side adoption ----------------------------------------------------


async def test_a_crashed_then_verified_resume_is_adopted_as_ours() -> None:
    """The crash window: pending resume row + the write landing (word 0)
    with NO lease commit.  The divergence pass commits the lost closing row
    as OURS — origin derived from the pending row's principal, a store write
    only — and the lease closes ``closed_operator``, never ``observed_foreign``."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-park",
    )
    # The crash: the pending resume row lands, the verified write lands (the
    # actor's own write path sets the word to 0), the closing transaction
    # never does.
    from energypod.domain.audit import AuditEvent

    async def dying_replace(event: AuditEvent, lease: Any, *, expected_epoch: int) -> None:
        raise OSError("the process died between the verified write and the transaction")

    rig.store.replace = dying_replace  # type: ignore[method-assign]
    result = await rig.controller.resume(
        "mid", principal_subject=HEALTH_PRINCIPAL, request_id="health-resume"
    )
    assert "audit_unavailable" in result["degraded"]  # Impl-10: the write stands
    assert rig.actors["mid"].word == 0  # the verified resume write DID land
    # The store healed on restart; the divergence pass adopts OUR resume.
    del rig.store.replace
    rig.observations.serve("mid", debug_mode_w=0)
    rig.clock.advance(2.0)
    await rig.controller.supervise()
    lease = await rig.store.lease("mid")
    assert lease is not None
    assert lease.state == LEASE_CLOSED_OPERATOR, "our completed act, adopted as ours"
    resumed = rig.audit.of_type("unit_resumed")
    adopted = [row for row in resumed if "resume_side_adoption" in row.reason_codes]
    assert adopted
    (event,) = [e for e in rig.bus.of_type("unit.resumed") if e["payload"].get("adopted")]
    assert event["payload"]["origin"] == "automation"
    assert not any(row.result == "observed_foreign" for row in resumed)


async def test_a_genuinely_foreign_resume_still_closes_observed_foreign() -> None:
    """No pending resume row -> the standing honesty is untouched: the
    divergence pass closes the lease ``observed_foreign`` with the alert."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=48.0)
    await rig.park()
    rig.actors["mid"].word = 0  # somebody else resumed it
    rig.observations.serve("mid", debug_mode_w=0)
    rig.clock.advance(1.0)
    await rig.controller.supervise()
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == "closed_foreign"
    resumed = rig.audit.of_type("unit_resumed")
    assert any(row.result == "observed_foreign" for row in resumed)


async def test_a_stale_pending_resume_row_outside_the_window_is_not_adopted() -> None:
    """The bounded recency window: a pending resume row older than the
    commissioned cap can no longer be ours — the honest foreign reading
    stands, with the pending row as the operator's correlation."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=48.0)
    await rig.park()
    # A pending resume row from long ago, never completed.
    from energypod.domain.audit import AuditEvent

    stale = AuditEvent(
        event_id="stale",
        occurred_at=rig.clock.wall
        - timedelta(seconds=rig.controller.commissioning.max_lease_s + 60),
        monotonic_offset_s=0.0,
        process_instance_id="p",
        event_type="unit_resumed",
        unit_id="mid",
        principal="person:operator",
        correlation_id="parking:unit_resumed:old",
        policy_version="parking",
        configuration_version=0,
        observation_sequences={},
        reason_codes=("durable_first",),
        requested_active_w=0,
        authorized_active_w=0,
        request_fingerprint="x",
        response_fingerprint="y",
        result="pending",
        lifecycle=rig.actors["mid"].lifecycle,
        payload={},
    )
    rig.audit.events.append(stale)
    rig.actors["mid"].word = 0
    rig.observations.serve("mid", debug_mode_w=0)
    rig.clock.advance(1.0)
    await rig.controller.supervise()
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == "closed_foreign"


async def test_a_fresh_resume_landing_on_the_crash_shape_adopts_first() -> None:
    """The operator's own RESUME arriving on the crash shape (word already
    0, open lease, our pending row): the idempotent path adopts OUR resume
    before it would close observed-foreign."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-park",
    )
    # A crashed earlier resume: pending row, no completing row, word already 0.
    from energypod.domain.audit import AuditEvent

    crashed = AuditEvent(
        event_id="crashed",
        occurred_at=rig.clock.wall,
        monotonic_offset_s=0.0,
        process_instance_id="p",
        event_type="unit_resumed",
        unit_id="mid",
        principal=HEALTH_PRINCIPAL,
        correlation_id="parking:unit_resumed:health-resume",
        policy_version="parking",
        configuration_version=0,
        observation_sequences={},
        reason_codes=("durable_first",),
        requested_active_w=0,
        authorized_active_w=0,
        request_fingerprint="x",
        response_fingerprint="y",
        result="pending",
        lifecycle=rig.actors["mid"].lifecycle,
        payload={},
    )
    rig.audit.events.append(crashed)
    rig.actors["mid"].word = 0
    result = await rig.controller.resume(
        "mid", principal_subject="person:operator", request_id="operator-resume"
    )
    assert result["origin"] == "none"  # the word was already Normal: the no-op path
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_CLOSED_OPERATOR
    adopted = [
        row
        for row in rig.audit.of_type("unit_resumed")
        if "resume_side_adoption" in row.reason_codes
    ]
    assert adopted
    assert not any(row.result == "observed_foreign" for row in rig.audit.of_type("unit_resumed"))


async def test_the_open_automation_lease_survives_until_the_adopted_close() -> None:
    """End-to-end shape: an automation park's lease is open (``open``, parked)
    until OUR resume closes it — the mirror and the projection agree."""
    rig = Rig()
    rig.observations.serve("mid", authoritative_soc_pct=97.0)
    await rig.controller.park(
        "mid",
        reason="nightly health-watch recovery",
        principal_subject=HEALTH_PRINCIPAL,
        request_id="health-park",
    )
    lease = await rig.store.lease("mid")
    assert lease is not None and lease.state == LEASE_OPEN and lease.parked
    assert rig.controller.parked_unit_ids() == frozenset({"mid"})
