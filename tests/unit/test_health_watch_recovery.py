"""T-BHW-RECOVERY + the recovery-half of T-BHW-RESTART / NEVER-THRASH /
EMERGENCY (DESIGN_BATTERY_HEALTH_WATCH §7/§8/§17).

Against ``energypod.application.health_watch``, with the C/P wave's own
fakes (``test_health_watch``):

- §7.1's eligibility conjunction: flag-without-fail, fail-without-flag, and
  an inconclusive probe all refuse — and NO ladder state (I10:
  ``actuation_incoherent`` included) is ever a trigger.
- §7.2's composite in ``auto``, step by step: re-verify, disarm, park (the
  lease bound ``hold_s + 60``, the health principal, the reason naming the
  watch), hold, resume, the ONE bounded re-arm, the §6 re-run, the closing
  disarm — and the audit/event/projection shapes under the health principal.
- §8's ladder: resync-then-ONE-reissue -> ``failed_write``;
  ACKed-but-unverified -> ``write_unverified`` with NO further write (I3);
  verification FAIL -> ``failed_no_effect`` (counted); verification
  inconclusive or re-arm refused -> ``recovered_unproven`` (NOT counted,
  A2); the cap -> advisory-only.
- §7.3/I1: one cycle per unit per civil night, across restarts, derived
  from durable rows; never a second attempt after any terminal outcome.
- A4: the interrupted composite — health rows with no
  ``health_recovery_outcome`` row raises the alert naming the state the unit
  was actually left in, including the crash-after-re-arm shape (armed,
  normal, no lease).
- I7: a latched stop mid-program ends the sequence with no further write.
- The ``advise`` posture: steps 2-6 do not run — no disarm, no park, no
  re-arm, ever; the advisory row is the product.

SAFETY: no hardware, no sockets, no live system contact.  Deterministic
fakes only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.application import health_watch as hw
from energypod.application.parking import (
    PARK_READBACK_UNVERIFIED,
    PARK_WRITE_FAILED,
    ParkingRefusal,
)

from .test_health_watch import (
    NIGHT,
    POLICY,
    UNIT_IDS,
    ZONE,
    FakeAudit,
    FakeBus,
    FakeClock,
    FakeHistory,
    FakeIntents,
    FakeObservations,
    FakeSubmit,
    evidence_rows,
    fleet_observations,
)

HOLD_S = 60  # the bounded floor keeps the composite inside the test budget


def stuck_history() -> FakeHistory:
    """The exhibiting fingerprint: rhs the spectator, siblings flowing."""
    return FakeHistory(
        rows=evidence_rows(
            socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
            watts={"lhs": 600.0, "mid": 600.0, "rhs": 20.0},
            loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
        )
    )


def recovery_settings(**overrides: Any) -> hw.HealthWatchSettings:
    values: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": time(23, 0),
        "deadline_local": time(23, 45),
        "stages": ("census", "probe", "recovery"),
        "stuck": hw.StuckSettings(),
        "probe": hw.ProbeSettings(
            settle_s=10,
            sustain_s=20,
            baseline_return_s=10,
            inter_unit_gap_s=0,
        ),
        "recovery_mode": "auto",
        "recovery": hw.RecoverySettings(mode="auto", hold_s=HOLD_S, consecutive_fail_limit=3),
        "unit_ids": UNIT_IDS,
        "sample_interval_s": 30.0,
        "intent_ttl_s": 4.0,
    }
    values.update(overrides)
    return hw.HealthWatchSettings(**values)


@dataclass
class FakeParkControl:
    """The §7.2 composer port: records every call, scripts every refusal."""

    park_calls: list[dict[str, Any]] = field(default_factory=list)
    resume_calls: list[dict[str, Any]] = field(default_factory=list)
    park_refusals: list[ParkingRefusal | None] = field(default_factory=list)
    resume_refusals: list[ParkingRefusal | None] = field(default_factory=list)

    async def park(
        self,
        unit_id: str,
        *,
        reason: str,
        principal_subject: str,
        request_id: str,
        lease_s: int | None = None,
    ) -> dict[str, Any]:
        self.park_calls.append(
            {
                "unit_id": unit_id,
                "reason": reason,
                "principal_subject": principal_subject,
                "request_id": request_id,
                "lease_s": lease_s,
            }
        )
        if self.park_refusals:
            refusal = self.park_refusals.pop(0)
            if refusal is not None:
                raise refusal
        return {
            "unit_id": unit_id,
            "action": "park",
            "prior_word": 0,
            "written_value": 1,
            "readback_word": 1,
            "verified": True,
            "lease": {"epoch": 1, "max_total_s": lease_s},
        }

    async def resume(
        self, unit_id: str, *, principal_subject: str, request_id: str
    ) -> dict[str, Any]:
        self.resume_calls.append(
            {"unit_id": unit_id, "principal_subject": principal_subject, "request_id": request_id}
        )
        if self.resume_refusals:
            refusal = self.resume_refusals.pop(0)
            if refusal is not None:
                raise refusal
        return {
            "unit_id": unit_id,
            "action": "resume",
            "prior_word": 1,
            "written_value": 0,
            "readback_word": 0,
            "verified": True,
            "origin": "automation",
            "lease": {"epoch": 1},
            "checklist": {"comms_age_s": 0.5},
        }


@dataclass
class FakeLifecyclePort:
    """One facade twin: the disarm or the ONE bounded re-arm."""

    disarmed: bool = True  # disarm answers "disarmed" while True
    armed: bool = True  # arm answers "armed" while True
    arm_reason: str = "external_writer"
    calls: list[str] = field(default_factory=list)

    async def __call__(self, *, unit_id: str) -> dict[str, str]:
        self.calls.append(unit_id)
        return self._outcome(unit_id)

    def _outcome(self, unit_id: str) -> dict[str, str]:
        raise NotImplementedError


@dataclass
class FakeDisarm(FakeLifecyclePort):
    def _outcome(self, unit_id: str) -> dict[str, str]:
        if self.disarmed:
            return {"unit_id": unit_id, "status": "disarmed", "reason": "disarmed"}
        return {"unit_id": unit_id, "status": "refused", "reason": "actor_failure"}


@dataclass
class FakeArm(FakeLifecyclePort):
    def _outcome(self, unit_id: str) -> dict[str, str]:
        if self.armed:
            return {"unit_id": unit_id, "status": "armed", "reason": "armed"}
        return {"unit_id": unit_id, "status": "refused", "reason": self.arm_reason}


class Rig:
    """A recovery-staged controller with every port faked and scripted."""

    def __init__(
        self,
        *,
        settings: hw.HealthWatchSettings | None = None,
        history: FakeHistory | None = None,
        audit: FakeAudit | None = None,
        echo: str = hw.ECHO_MATCHES_WRITE,
        health_state: str = "healthy",
        mode: str = "auto",
        receipts_missing: tuple[str, ...] = (),
        parked: frozenset[str] = frozenset(),
        stopped: frozenset[str] = frozenset(),
    ) -> None:
        self.clock = FakeClock()
        self.observations = FakeObservations()
        self.observations.auto_stamp_mono = self.clock.monotonic
        self.intents = FakeIntents()
        self.submit = FakeSubmit()
        self.submit.intents = self.intents
        self.submit.clock = self.clock
        self.history = history if history is not None else stuck_history()
        self.audit = audit if audit is not None else FakeAudit()
        self.bus = FakeBus()
        self.park = FakeParkControl()
        self.disarm = FakeDisarm()
        self.arm = FakeArm()
        self.echo_handle = SimpleNamespace(read_objective_echo=self._echo)
        self._echo_class = echo
        self._health_state = health_state
        self._stopped = stopped
        self._parked = set(parked)
        self.observations.latest = fleet_observations(
            rhs__soc_pct=97.0, rhs__battery_watts=20.0
        )

        async def health_states() -> dict[str, Any]:
            return {unit: SimpleNamespace(state=self._health_state) for unit in UNIT_IDS}

        self.controller = hw.HealthWatchController(
            settings=settings
            or recovery_settings(
                recovery_mode=mode, recovery=hw.RecoverySettings(mode=mode, hold_s=HOLD_S)
            ),
            policy=POLICY,
            clock=self.clock,
            observations=self.observations,
            intents=self.intents,
            submit=self.submit,
            history=self.history,
            audit=self.audit,
            bus=self.bus,
            actors={unit: self.echo_handle for unit in UNIT_IDS},
            health_states=health_states,
            parked_units=lambda: frozenset(self._parked),
            latched_stop_units=lambda: frozenset(self._stopped),
            park_control=self.park,
            disarm=self.disarm if mode == "auto" else None,
            arm=self.arm if mode == "auto" else None,
            recovery_receipts_missing=receipts_missing,
        )
        self.handles = {
            "clock": self.clock,
            "controller": self.controller,
            "state": lambda: self.controller.state_payload()["phase"],
        }

    async def _echo(self) -> tuple[str, tuple[int | None, int | None]]:
        return (self._echo_class, (300, 0))

    async def run(
        self,
        *,
        seconds: float = 900.0,
        until: Any = None,
        each_tick: Any = None,
    ) -> None:
        """Drive one tick per second until done (or a predicate holds)."""
        deadline = self.clock.now + seconds
        while self.clock.now < deadline:
            await self.controller.tick()
            if each_tick is not None:
                each_tick()
            if until is not None and until():
                return
            if self.controller.state_payload()["phase"] == "done":
                return
            self.clock.advance(1.0)
        raise AssertionError("the program did not finish inside the budget")

    def outcome_row(self, unit_id: str) -> Any:
        rows = [
            event
            for event in self.audit.appended
            if event.event_type == "health_recovery_outcome" and event.unit_id == unit_id
        ]
        assert rows, f"no health_recovery_outcome row for {unit_id}"
        return rows[-1]

    def recovery_payload(self, unit_id: str) -> dict[str, Any]:
        payload = self.controller.state_payload()
        return next(unit["recovery"] for unit in payload["units"] if unit["unit_id"] == unit_id)


def outcome_row_for(
    unit: str, night: date, verdict: str, *, occurred: time = time(23, 30)
) -> Any:
    """A seeded durable outcome row (the restart/cap derivations read these)."""
    return SimpleNamespace(
        event_type="health_recovery_outcome",
        unit_id=unit,
        occurred_at=datetime.combine(night, occurred, tzinfo=ZONE).astimezone(UTC),
        payload={"night": night.isoformat(), "verdict": verdict},
    )


def probe_row_for(unit: str, night: date, verdict: str) -> Any:
    return SimpleNamespace(
        event_type="health_probe_completed",
        unit_id=unit,
        occurred_at=datetime.combine(night, time(23, 20), tzinfo=ZONE).astimezone(UTC),
        payload={"night": night.isoformat(), "verdict": verdict},
    )


def census_row_for(unit: str, night: date, verdict: str) -> Any:
    return SimpleNamespace(
        event_type="health_census_recorded",
        unit_id=unit,
        occurred_at=datetime.combine(night, time(23, 10), tzinfo=ZONE).astimezone(UTC),
        payload={"night": night.isoformat(), "verdict": verdict},
    )


# --- §7.1: the eligibility conjunction ----------------------------------------------


async def test_a_probe_failure_without_a_census_flag_never_cycles() -> None:
    """Fail-without-flag: one weak night — alert, no write."""
    rig = Rig(
        history=FakeHistory(rows=evidence_rows()),  # healthy fleet, nothing flagged
        echo=hw.ECHO_MATCHES_WRITE,
    )
    # rhs still dead in the LATEST words so its probe fails no-response even
    # though the census (the 6 h window) never flagged it.
    rig.observations.latest = fleet_observations(rhs__soc_pct=97.0, rhs__battery_watts=20.0)
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] is None
    assert row.payload["eligibility"] == {
        "census": hw.CENSUS_NOMINAL,
        "probe": hw.PROBE_FAIL_NO_RESPONSE,
        "eligible": False,
    }


async def test_a_census_flag_without_the_probe_failure_renders_the_advisory_only() -> None:
    """Flag-without-fail: the CT-dead-but-commandable class is NOT the wedge
    a standby cycle is proven to fix — the advisory, no cycle."""
    rig = Rig()
    # rhs flagged by the window, but it DELIVERS when probed.
    def delivering_rhs() -> None:
        rig.observations.latest["rhs"].battery_watts = 300.0

    delivering_rhs()
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] is None
    assert row.payload["eligibility"]["probe"] == hw.PROBE_PASS


async def test_an_inconclusive_probe_on_a_flagged_unit_is_not_eligible() -> None:
    """A skipped or inconclusive probe leaves the evidence incomplete, and an
    incomplete case never mints a mode write (§7.1)."""
    rig = Rig(echo=hw.ECHO_UNREADABLE)  # still + unreadable -> inconclusive_aborted
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] is None
    assert row.payload["eligibility"]["probe"] == hw.PROBE_INCONCLUSIVE_ABORTED


async def test_no_ladder_state_is_ever_a_trigger_i10() -> None:
    """I10 pinned at the R boundary: ``actuation_incoherent`` (or any ladder
    state) alone never composes a cycle — the rows are the only trigger, and
    the re-verify's own walk skips the inhibited unit besides."""
    rig = Rig(health_state="actuation_incoherent")
    rig.observations.latest["rhs"].battery_watts = 300.0  # probe PASSES
    await rig.run()
    assert rig.park.park_calls == []
    assert rig.outcome_row("rhs").payload["verdict"] is None


# --- §7.2: the composite in the auto posture ----------------------------------------


async def test_the_full_auto_cycle_recovers_and_ends_disarmed_and_normal() -> None:
    """reverify -> disarm -> park -> hold -> resume -> ONE re-arm -> the §6
    re-run (pass) -> disarm: verdict ``recovered``, resolved tier, the unit
    disarmed, and every row under the health principal."""
    rig = Rig()

    def rhs_delivers_after_the_cycle() -> None:
        if rig.park.resume_calls:
            rig.observations.latest["rhs"].battery_watts = 300.0

    await rig.run(each_tick=rhs_delivers_after_the_cycle)
    # The composite's exact reach: one disarm, one park, one resume, one arm,
    # one closing disarm — and nothing else.
    assert rig.disarm.calls == ["rhs", "rhs"]
    assert [call["unit_id"] for call in rig.park.park_calls] == ["rhs"]
    assert [call["unit_id"] for call in rig.park.resume_calls] == ["rhs"]
    assert rig.arm.calls == ["rhs"]
    park_call = rig.park.park_calls[0]
    assert park_call["principal_subject"] == hw.HEALTH_ADVISER_PRINCIPAL
    assert park_call["lease_s"] == HOLD_S + 60
    assert park_call["reason"] == (
        "nightly health-watch recovery (census flag + probe no-response)"
    )
    assert rig.park.resume_calls[0]["principal_subject"] == hw.HEALTH_ADVISER_PRINCIPAL
    # The verdict, its tier, and the honest figures.
    row = rig.outcome_row("rhs")
    assert row.principal == hw.HEALTH_ADVISER_PRINCIPAL
    assert row.payload["verdict"] == hw.RECOVERY_RECOVERED
    assert row.payload["tier"] == hw.TIER_RESOLVED
    assert row.payload["rung"] == hw.RUNG_VERIFIED
    assert row.payload["posture"] == "auto"
    assert row.payload["cycle"]["park"]["written_value"] == 1
    assert row.payload["cycle"]["resume"]["readback_word"] == 0
    assert row.payload["verification"]["verdict"] == hw.PROBE_PASS
    assert row.payload["verification"]["core_samples"] > 0
    assert row.payload["left_armed"] is False
    assert row.payload["bmu_cross_check"] == hw.BMU_CROSS_CHECK_NOTE
    # The event rides the bus at the resolved tier.
    events = [e for e in rig.bus.events if e["type"] == "health.recovery"]
    assert any(e["payload"]["unit_id"] == "rhs" and e["payload"]["tier"] == "resolved"
               for e in events)
    # The non-eligible units carry their honest null rows (I11).
    assert rig.outcome_row("lhs").payload["verdict"] is None
    assert rig.outcome_row("mid").payload["verdict"] is None
    # The projection: the verdict plus A11's morning figures.
    payload = rig.recovery_payload("rhs")
    assert payload["mode"] == "auto"
    assert payload["verdict"] == hw.RECOVERY_RECOVERED
    assert payload["attempts_total"] == 1
    assert payload["consecutive_fails"] == 0
    assert payload["trailing_30_nights"]["attempted"] == 1
    assert payload["trailing_30_nights"]["recovered"] == 1


async def test_the_wedge_that_survives_the_cycle_is_failed_no_effect() -> None:
    """Rung 4: a verified cycle whose verification re-run still reads
    still-and-echoed -> ``failed_no_effect``, ALERT, counted to the cap."""
    rig = Rig()  # rhs stays dead through the verification (the persistent leg)
    await rig.run()
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_FAILED_NO_EFFECT
    assert row.payload["tier"] == hw.TIER_ALERT
    assert row.payload["rung"] == hw.RUNG_VERIFICATION_FAILED
    assert row.payload["verification"]["verdict"] == hw.PROBE_FAIL_NO_RESPONSE
    assert row.payload["advisory"] == hw.RESTART_ADVISORY
    assert row.payload["attempts_total"] == 1
    assert row.payload["consecutive_fails"] == 1
    # The composite still ended disarmed (I9: authority-net-zero).
    assert row.payload["left_armed"] is False
    assert rig.recovery_payload("rhs")["consecutive_fails"] == 1


# --- §8: the ladder -----------------------------------------------------------------


async def test_a_clean_transport_refusal_reissues_once_then_fails_write() -> None:
    """Rung 2: ONE re-issue after the actor's own resync; the second refusal
    ends the night's attempt at ``failed_write`` — exactly two park calls."""
    rig = Rig()
    rig.park.park_refusals = [
        ParkingRefusal(PARK_WRITE_FAILED, "the transport refused or timed out"),
        ParkingRefusal(PARK_WRITE_FAILED, "the transport refused or timed out"),
    ]
    await rig.run()
    assert len(rig.park.park_calls) == 2  # the write, then rung 2's ONE re-issue
    assert rig.park.resume_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_FAILED_WRITE
    assert row.payload["rung"] == hw.RUNG_RESYNC_EXHAUSTED
    assert row.payload["consecutive_fails"] == 1


async def test_the_reissue_can_still_recover() -> None:
    """Rung 1+2 in order: the built-in retry, one resync re-issue, success."""
    rig = Rig()
    rig.park.park_refusals = [ParkingRefusal(PARK_WRITE_FAILED, "transport blip")]

    def rhs_delivers() -> None:
        if rig.park.resume_calls:
            rig.observations.latest["rhs"].battery_watts = 300.0

    await rig.run(each_tick=rhs_delivers)
    assert len(rig.park.park_calls) == 2
    assert rig.outcome_row("rhs").payload["verdict"] == hw.RECOVERY_RECOVERED


async def test_an_acked_but_unverified_write_retires_the_night_with_no_further_write() -> None:
    """Rung 3 / I3: after an ACKed-but-unverified park write, no resume, no
    verification, no second attempt — that night or on any later tick."""
    rig = Rig()
    rig.park.park_refusals = [
        ParkingRefusal(
            PARK_READBACK_UNVERIFIED,
            "the mode write was acknowledged but the readback did not confirm it",
        )
    ]
    await rig.run()
    assert len(rig.park.park_calls) == 1
    assert rig.park.resume_calls == []
    assert rig.arm.calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_WRITE_UNVERIFIED
    assert row.payload["rung"] == hw.RUNG_ACKED_UNVERIFIED
    assert "no_further_write_tonight" in row.reason_codes
    # I3 structurally: more ticks never mint another write.
    park_calls = len(rig.park.park_calls)
    for _ in range(20):
        await rig.controller.tick()
        rig.clock.advance(1.0)
    assert len(rig.park.park_calls) == park_calls
    assert rig.outcome_row("rhs").payload["verdict"] == hw.RECOVERY_WRITE_UNVERIFIED


async def test_a_refused_rearm_ends_recovered_unproven_and_does_not_count() -> None:
    """A2's refused-re-arm rung: a foreign objective during the park (the
    operator's takeover acknowledgement) or a latched stop both end the
    sequence at ``recovered_unproven`` — the unit disarmed, the cap
    uncounted."""
    rig = Rig()
    rig.arm.armed = False
    rig.arm.arm_reason = "external_writer"
    await rig.run()
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_RECOVERED_UNPROVEN
    assert row.payload["rung"] == hw.RUNG_REARM_REFUSED
    assert row.payload["reason"] == "external_writer"
    assert row.payload["tier"] == hw.TIER_ALERT
    assert row.payload["advisory"] == hw.RESTART_ADVISORY
    assert row.payload["attempts_total"] == 1
    assert row.payload["consecutive_fails"] == 0  # A2: proofs that went missing
    # The unit was never armed: the closing disarm is the composite's own.
    assert rig.disarm.calls == ["rhs"]


async def test_an_inconclusive_verification_rerun_is_recovered_unproven() -> None:
    """A2's inconclusive rung: the cycle plausibly worked, the proof is
    missing (here the quiet-load gate refuses the re-run's start), and NO
    re-run is issued (I1)."""
    rig = Rig()
    # The house starts drawing hard right after the resume: the verification
    # leg's own §6.1 gate refuses its start.
    def house_gets_busy() -> None:
        if rig.park.resume_calls:
            for observation in rig.observations.latest.values():
                observation.grid_power_w = -2500.0

    await rig.run(each_tick=house_gets_busy)
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_RECOVERED_UNPROVEN
    assert row.payload["rung"] == hw.RUNG_VERIFICATION_INCONCLUSIVE
    assert row.payload["consecutive_fails"] == 0


async def test_the_reverify_skips_name_their_reasons_and_alert_on_foreign_words() -> None:
    """§7.2 step 1: a standby word of 1 that is not ours is a SKIP + ALERT
    (``foreign_standby``); a word in 2-6 is a SKIP + ALERT (``vendor_mode``).
    The word lands AFTER the probe (the census would have excluded the unit
    earlier — the re-verify walk is the §7.2 step-1 fence, re-checked inside
    the standing locks)."""
    for word, reason in ((1, hw.SKIP_FOREIGN_STANDBY), (3, hw.SKIP_VENDOR_MODE)):
        rig = Rig()

        def the_word_lands(rig: Rig = rig, word: int = word) -> None:
            rhs_row = [
                e
                for e in rig.audit.appended
                if e.event_type == "health_probe_completed" and e.unit_id == "rhs"
            ]
            if rhs_row:
                rig.observations.latest["rhs"].debug_mode_w = word

        await rig.run(each_tick=the_word_lands)
        assert rig.park.park_calls == []
        row = rig.outcome_row("rhs")
        assert row.payload["verdict"] == f"skipped:{reason}"
        assert row.payload["tier"] == hw.TIER_ALERT


async def test_an_open_lease_skips_the_cycle() -> None:
    """Our own lease standing (parked, whatever the word) is another
    surface's exit — the cycle never contends with it."""
    rig = Rig()

    def the_lease_appears() -> None:
        rhs_row = [
            e
            for e in rig.audit.appended
            if e.event_type == "health_probe_completed" and e.unit_id == "rhs"
        ]
        if rhs_row:
            rig.observations.latest["rhs"].debug_mode_w = 1
            rig._parked.add("rhs")

    await rig.run(each_tick=the_lease_appears)
    assert rig.park.park_calls == []
    assert rig.outcome_row("rhs").payload["verdict"] == f"skipped:{hw.SKIP_OPEN_LEASE}"


async def test_a_disarm_refusal_skips_before_any_cycle_is_attempted() -> None:
    rig = Rig()
    rig.disarm.disarmed = False
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == f"skipped:{hw.SKIP_DISARM_REFUSED}"
    assert row.payload["consecutive_fails"] == 0


# --- §7.3/I1/I4: the cap and the never-thrash budget -------------------------------


async def test_the_cap_stands_the_unit_down_to_advisory_only() -> None:
    """Three consecutive counted failures (seeded rows) -> the R stage stands
    down to advisory-only for that unit: no cycle, the alert, the limit
    named on the projection."""
    audit = FakeAudit()
    for nights_ago in (3, 2, 1):
        night = (NIGHT - timedelta(days=nights_ago)).date()
        audit.seeded.append(outcome_row_for("rhs", night, hw.RECOVERY_FAILED_NO_EFFECT))
    rig = Rig(audit=audit)
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_ADVISORY_ONLY
    assert row.payload["tier"] == hw.TIER_ALERT
    assert row.payload["advisory"] == hw.RESTART_ADVISORY
    payload = rig.recovery_payload("rhs")
    assert payload["consecutive_fails"] == 3
    assert payload["consecutive_fail_limit"] == 3


async def test_recovered_unproven_nights_neither_count_nor_break_toward_the_cap() -> None:
    """A2's arithmetic from the streak side: a ``recovered_unproven`` night
    is not a counted failure, and it breaks the consecutive chain (it is not
    a failure), so fail -> unproven -> fail leaves a streak of one."""
    audit = FakeAudit()
    audit.seeded.append(outcome_row_for("rhs", (NIGHT - timedelta(days=3)).date(),
                                        hw.RECOVERY_FAILED_NO_EFFECT))
    audit.seeded.append(outcome_row_for("rhs", (NIGHT - timedelta(days=2)).date(),
                                        hw.RECOVERY_RECOVERED_UNPROVEN))
    audit.seeded.append(outcome_row_for("rhs", (NIGHT - timedelta(days=1)).date(),
                                        hw.RECOVERY_FAILED_NO_EFFECT))
    rig = Rig(audit=audit)
    await rig.run()
    # The streak is one: the cap does not stand the unit down.
    assert rig.park.park_calls, "a broken fail chain must still cycle"
    payload = rig.recovery_payload("rhs")
    assert payload["attempts_total"] == 4  # the three seeded plus tonight's
    assert payload["consecutive_fails"] == 2  # tonight + the night before the unproven


async def test_a_probe_pass_morning_resets_the_streak() -> None:
    """The operator's evidence-driven acknowledgement: a night whose rows
    show the unit healthy (not eligible, verdict null) breaks the fail
    chain."""
    audit = FakeAudit()
    audit.seeded.append(outcome_row_for("rhs", (NIGHT - timedelta(days=2)).date(),
                                        hw.RECOVERY_FAILED_NO_EFFECT))
    audit.seeded.append(outcome_row_for("rhs", (NIGHT - timedelta(days=1)).date(),
                                        hw.RECOVERY_FAILED_NO_EFFECT))
    # The healthy night: an outcome row with a NULL verdict (not eligible).
    audit.seeded.append(
        SimpleNamespace(
            event_type="health_recovery_outcome",
            unit_id="rhs",
            occurred_at=datetime.combine(
                (NIGHT - timedelta(days=1)).date(), time(23, 40), tzinfo=ZONE
            ).astimezone(UTC),
            payload={"night": (NIGHT - timedelta(days=1)).date().isoformat(), "verdict": None},
        )
    )
    rig = Rig(audit=audit)
    await rig.run()
    assert rig.park.park_calls, "a healthy night resets the cap's chain"


# --- A4/I8: the restart reconstruction ----------------------------------------------


async def test_health_rows_without_an_outcome_row_raise_the_interrupted_alert() -> None:
    """A4's R shape: a restart finding census+probe rows for a unit-night
    with NO ``health_recovery_outcome`` row raises the alert naming the
    state the unit was actually left in — including the crash-after-re-arm
    state (armed, normal, no lease) — and retires the night."""
    night = NIGHT.date()
    audit = FakeAudit()
    for unit in UNIT_IDS:
        audit.seeded.append(census_row_for(unit, night, hw.CENSUS_STUCK if unit == "rhs"
                                           else hw.CENSUS_NOMINAL))
        audit.seeded.append(probe_row_for(unit, night, hw.PROBE_FAIL_NO_RESPONSE
                                          if unit == "rhs" else hw.PROBE_PASS))
    rig = Rig(audit=audit)
    # The crash-after-re-arm shape: armed, normal, no lease, un-alerted.
    await rig.controller.tick()
    rig.clock.advance(1.0)
    payload = rig.controller.state_payload()
    assert payload["phase"] == "done"
    assert payload["reason"] == hw.REASON_INTERRUPTED
    program_rows = [
        e for e in rig.audit.appended if e.event_type == "health_program_recorded"
    ]
    assert program_rows and program_rows[-1].payload["reason"] == hw.REASON_INTERRUPTED
    states = program_rows[-1].payload["units"]
    assert states["rhs"]["lifecycle"] == "armed_idle"
    assert states["rhs"]["debug_mode_w"] == 0
    assert states["rhs"]["parked"] is False
    # And the retired night never cycles (I1/I8).
    assert rig.park.park_calls == []


async def test_a_completed_night_never_reruns_across_restart_i1() -> None:
    """One cycle per unit per civil night, derived from durable rows: a NEW
    controller over the SAME store, with the night's outcome rows complete,
    stands the night done — no re-run, no second attempt."""
    rig = Rig()
    await rig.run()
    park_calls = len(rig.park.park_calls)
    # The restart: a fresh controller over the same audit (the memory of the
    # night is the rows, never the process).
    restarted = Rig(audit=rig.audit)
    restarted.clock.wall = rig.clock.wall
    restarted.observations.latest = rig.observations.latest
    for _ in range(10):
        await restarted.controller.tick()
        restarted.clock.advance(1.0)
    assert restarted.controller.state_payload()["phase"] == "done"
    assert restarted.park.park_calls == []
    assert len(rig.park.park_calls) == park_calls


async def test_boot_never_starts_the_program_i8() -> None:
    """Before the window, nothing runs at all — R runs only from its own
    window evaluation."""
    rig = Rig()
    rig.clock.wall = NIGHT.replace(hour=12, minute=0)
    for _ in range(10):
        await rig.controller.tick()
        rig.clock.advance(1.0)
    assert rig.park.park_calls == []
    assert rig.controller.state_payload()["phase"] == "await_window"


# --- I7: the emergency stop ---------------------------------------------------------


async def test_a_latched_stop_mid_hold_ends_the_sequence_with_no_further_write() -> None:
    """I7: the e-stop is supreme — bounded by the standing rules, the
    program writes nothing further that night; the open lease follows the
    standing expiry rules."""
    rig = Rig()

    def stop_lands_mid_hold() -> None:
        if rig.park.park_calls:
            rig._stopped = frozenset({"rhs"})

    await rig.run(each_tick=stop_lands_mid_hold)
    assert rig.park.resume_calls == []
    assert rig.arm.calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == f"skipped:{hw.SKIP_LATCHED_STOP}"
    assert row.payload["tier"] == hw.TIER_ALERT
    assert "no_further_program_write" in row.reason_codes


# --- §7.2's advise posture -----------------------------------------------------------


async def test_the_advise_posture_renders_the_advisory_and_writes_nothing() -> None:
    """Advise is a real posture, not a stub: the ALERT-tier advisory naming
    the exact operator sequence, the record — and NO disarm, park, resume,
    or re-arm, ever (structurally: the ports are not even wired)."""
    rig = Rig(mode="advise")
    await rig.run()
    assert rig.park.park_calls == []
    assert rig.disarm.calls == []
    assert rig.arm.calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_ADVISED
    assert row.payload["tier"] == hw.TIER_ALERT
    assert row.payload["posture"] == "advise"
    assert row.payload["walkthrough"] == hw.ADVISE_WALKTHROUGH
    assert row.payload["advisory"] == hw.RESTART_ADVISORY
    assert row.payload["eligibility"]["eligible"] is True
    payload = rig.recovery_payload("rhs")
    assert payload["mode"] == "advise"
    assert payload["verdict"] == hw.RECOVERY_ADVISED
    # The not-eligible units still carry their honest null rows.
    assert rig.outcome_row("lhs").payload["verdict"] is None


async def test_a_missing_auto_receipt_degrades_that_unit_to_advise_loudly_a6() -> None:
    rig = Rig(receipts_missing=("rhs",))
    await rig.run()
    assert rig.park.park_calls == []
    row = rig.outcome_row("rhs")
    assert row.payload["verdict"] == hw.RECOVERY_ADVISED
    assert row.payload["posture"] == "advise"
    payload = rig.recovery_payload("rhs")
    assert payload["mode"] == "advise"
    assert payload["configured_mode"] == "auto"
    assert "receipt missing" in (payload["note"] or "")


# --- the structural pins -------------------------------------------------------------


async def test_a_staged_recovery_requires_its_composer_ports() -> None:
    """The composition guards: a staged recovery without its park port, and
    an auto posture without its lifecycle twins, are refused at
    construction — never silently incapable."""
    with pytest.raises(ValueError, match="park_control"):
        hw.HealthWatchController(
            settings=recovery_settings(),
            policy=POLICY,
            clock=FakeClock(),
            observations=FakeObservations(),
            intents=FakeIntents(),
            submit=FakeSubmit(),
            history=FakeHistory(),
            audit=FakeAudit(),
            bus=FakeBus(),
            actors={},
            health_states=_no_health_states,
            parked_units=lambda: frozenset(),
            latched_stop_units=lambda: frozenset(),
        )
    settings = recovery_settings()
    with pytest.raises(ValueError, match="disarm and arm ports"):
        hw.HealthWatchController(
            settings=settings,
            policy=POLICY,
            clock=FakeClock(),
            observations=FakeObservations(),
            intents=FakeIntents(),
            submit=FakeSubmit(),
            history=FakeHistory(),
            audit=FakeAudit(),
            bus=FakeBus(),
            actors={},
            health_states=_no_health_states,
            parked_units=lambda: frozenset(),
            latched_stop_units=lambda: frozenset(),
            park_control=FakeParkControl(),
        )


async def _no_health_states() -> dict[str, Any]:
    return {}


def test_the_recovery_tier_arithmetic() -> None:
    """§11: ``recovered`` resolves; every other outcome alerts except the
    notice-tier skips and the null verdict."""
    assert hw.recovery_tier(hw.RECOVERY_RECOVERED) == hw.TIER_RESOLVED
    assert hw.recovery_tier(None) == hw.TIER_NOTICE
    assert hw.recovery_tier(hw.RECOVERY_ADVISED) == hw.TIER_ALERT
    assert hw.recovery_tier(hw.RECOVERY_RECOVERED_UNPROVEN) == hw.TIER_ALERT
    assert hw.recovery_tier(hw.RECOVERY_FAILED_NO_EFFECT) == hw.TIER_ALERT
    assert hw.recovery_tier(hw.RECOVERY_WRITE_UNVERIFIED) == hw.TIER_ALERT
    assert hw.recovery_tier(hw.RECOVERY_FAILED_WRITE) == hw.TIER_ALERT
    assert hw.recovery_tier(f"skipped:{hw.SKIP_FOREIGN_STANDBY}") == hw.TIER_ALERT
    assert hw.recovery_tier(f"skipped:{hw.SKIP_VENDOR_MODE}") == hw.TIER_ALERT
    assert hw.recovery_tier(f"skipped:{hw.SKIP_DEADLINE_PASSED}") == hw.TIER_NOTICE
    assert hw.recovery_tier(f"skipped:{hw.SKIP_OPEN_LEASE}") == hw.TIER_NOTICE


def test_the_advisory_text_is_pinned_verbatim() -> None:
    """§11's load-bearing text: the 10-minute wait and the battery-first
    ordering ride the alert, the row, and the console identically."""
    assert "WAIT 10 MINUTES" in hw.RESTART_ADVISORY
    assert "battery ON FIRST, then AC, then DC" in hw.RESTART_ADVISORY
    assert hw.RESTART_ADVISORY.startswith("Remote recovery exhausted.")
