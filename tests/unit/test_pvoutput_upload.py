"""The pvoutput.org uploader control contract (application layer).

Everything asserted here is the uploader's own doctrine
(application/pvoutput_upload.py), against scripted fakes only -- a fake
observations port, a fake pvoutput client, a fake durable toggle store, and
an injectable manual clock.  No socket, no secret, no adapter import (the
layering pin: the fake client carries the same structural
``pvoutput_failure`` word the real adapter's errors do).

The pinned vocabulary:

- **Cadence**: one POST per 5-minute slot, attempted only after the slot's
  ~30 s grace; never a second successful post per slot; a restart posts the
  CURRENT slot only -- no backfill, ever.
- **Omission**: per-field quality gating (a non-good SoC or power word is
  OMITTED, never zero-filled); an all-stale fleet skips the slot and the
  skip counts once when the slot closes still stale; the native b1/b2 pair
  omits together whenever b1 cannot be computed fresh (the specification
  makes b1 mandatory when any battery field is sent).
- **Failures**: auth-class disables loudly (and the operator's re-enable is
  the one honest retry); the rate 403 backs off to PVOutput's reset instant;
  a 400 surfaces the reason verbatim and spends the slot's budget; a
  transient failure retries at most ``retry_max`` times per slot, spaced --
  never bursts.
- **The durable toggle**: the store write happens BEFORE the in-memory flip
  (a failing write refuses; there is never an in-memory-only choice), and a
  stored row overrides the config's ``enabled`` at boot.
- **Suppression**: the tick never raises into the fleet loop, whatever the
  injected ports do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energypod.adapters.persistence.memory import InMemoryPvOutputStateRepository
from energypod.adapters.pvoutput.client import (
    PvOutputAuthError,
    PvOutputRateLimited,
    PvOutputRejected,
    PvOutputUnavailable,
)
from energypod.application.pvoutput_upload import PvOutputRefusal, PvOutputUploader

UNIT_IDS = ("lhs", "mid", "rhs")
UNIT_SLOTS = {"lhs": ("v7", "v8"), "rhs": ("v9", "v10"), "mid": ("v11", "v12")}


class ManualClock:
    """Wall and monotonic time, advanced together by the test."""

    def __init__(self, wall: datetime, mono: float = 1000.0) -> None:
        self.wall = wall
        self.mono = mono

    def advance(self, seconds: float) -> None:
        self.wall = self.wall + timedelta(seconds=seconds)
        self.mono += seconds

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@dataclass
class Obs:
    """The observation projection the uploader reads (the gated fields)."""

    unit_id: str
    captured_at_mono: float
    wall_timestamp: datetime
    bms_soc_pct: float | None
    battery_watts: float | None
    quality: dict[str, str] = field(default_factory=dict)


class FakeObservations:
    def __init__(self) -> None:
        self.latest: dict[str, Obs] = {}
        self.fail: bool = False

    async def all_latest(self) -> dict[str, Obs]:
        if self.fail:
            raise RuntimeError("observation store unreadable")
        return dict(self.latest)


@dataclass
class FakeResult:
    rate_remaining: int | None = 57


class FakeClient:
    """The scripted pvoutput client: records every call, raises on script."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[BaseException] = []
        self.broken: bool = False

    async def post_status(self, *, sample_at: datetime, fields: dict[str, float]) -> FakeResult:
        self.calls.append({"sample_at": sample_at, "fields": dict(fields)})
        if self.broken:
            raise RuntimeError("the uploader exploded")
        if self.script:
            raise self.script.pop(0)
        return FakeResult()


class ExplodingStore:
    """A durable toggle store that refuses every write (the refusal path)."""

    def __init__(self) -> None:
        self.writes = 0

    def state(self) -> tuple[bool, str] | None:
        return None

    def store(self, *, enabled: bool, updated_at: str) -> None:
        self.writes += 1
        raise RuntimeError("database is busy")


def _observation(
    unit_id: str,
    clock: ManualClock,
    *,
    age_s: float = 5.0,
    soc: float | None = 61.0,
    power: float | None = -500.0,
    soc_quality: str = "good",
    power_quality: str = "good",
) -> Obs:
    return Obs(
        unit_id=unit_id,
        captured_at_mono=clock.monotonic() - age_s,
        wall_timestamp=clock.wall_now() - timedelta(seconds=age_s),
        bms_soc_pct=soc,
        battery_watts=power,
        quality={"bms_soc_pct": soc_quality, "battery_watts": power_quality},
    )


def _seed(observations: FakeObservations, clock: ManualClock, **overrides: Any) -> None:
    defaults: dict[str, Any] = {}
    for unit_id, power in (("lhs", -500.0), ("mid", -1000.0), ("rhs", 990.0)):
        defaults[unit_id] = _observation(unit_id, clock, soc=60.0 + 2.0 * len(unit_id), power=power)
    defaults.update(overrides)
    observations.latest = dict(defaults)


#: Lets the helper compose the uploader with an EXPLICITLY absent client
#: (the missing-credentials posture) instead of its default fake.
_NO_CLIENT = object()


def _uploader(
    *,
    clock: ManualClock,
    observations: FakeObservations,
    client: Any = None,
    store: Any = None,
    enabled: bool = True,
    retry_max: int = 1,
    native: bool = True,
    max_age_s: float = 120.0,
    interval_s: float = 300.0,
    credentials_note: str | None = None,
) -> PvOutputUploader:
    return PvOutputUploader(
        unit_ids=UNIT_IDS,
        unit_slots=dict(UNIT_SLOTS),
        timezone_name="UTC",
        interval_s=interval_s,
        max_sample_age_s=max_age_s,
        retry_max=retry_max,
        native_battery_fields=native,
        config_enabled=enabled,
        client=FakeClient() if client is None else (None if client is _NO_CLIENT else client),
        clock=clock,
        observations=observations,
        store=store if store is not None else InMemoryPvOutputStateRepository(),
        credentials_note=credentials_note,
    )


def _at_minute(hour: int, minute: int, second: int = 35) -> ManualClock:
    """A clock inside the slot starting at :minute, past the 30 s grace."""
    return ManualClock(datetime(2026, 8, 25, hour, minute, second, tzinfo=UTC))


class TestCadenceGate:
    async def test_no_post_inside_the_slots_grace(self) -> None:
        clock = _at_minute(4, 0, second=10)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert client.calls == []

    async def test_the_slot_posts_once_after_the_grace(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert len(client.calls) == 1
        status = uploader.status_payload()
        assert status["last_success_at"] is not None
        assert status["last_posted_slot"] == "2026-08-25 04:00"

    async def test_no_double_post_inside_one_slot(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        clock.advance(60.0)
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1

    async def test_the_next_boundary_fires_a_new_post(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        clock.advance(300.0)  # 04:05:35 -- the next slot, past its grace
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2
        assert uploader.status_payload()["last_posted_slot"] == "2026-08-25 04:05"

    async def test_a_restart_posts_the_current_slot_only_never_backfill(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert len(client.calls) == 1
        # A controller restart at 04:12: the fresh uploader holds no slot
        # memory, posts the 04:10 slot when its grace passes, and never
        # reaches back for the 04:05 gap.
        clock.advance(12 * 60.0 - 35.0 + 35.0)  # 04:12:35
        restarted_client = FakeClient()
        _seed(observations, clock)
        restarted = _uploader(clock=clock, observations=observations, client=restarted_client)
        await restarted.tick()
        assert len(restarted_client.calls) == 1
        assert restarted.status_payload()["last_posted_slot"] == "2026-08-25 04:10"

    async def test_a_coarser_interval_posts_every_other_slot(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(
            clock=clock, observations=observations, client=client, interval_s=600.0
        )
        await uploader.tick()
        clock.advance(300.0)  # 04:05:35 -- inside the 600 s spacing: no post
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1
        clock.advance(300.0)  # 04:10:35 -- the second eligible slot
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2


class TestFieldMappingAndOmission:
    async def test_the_pinned_per_unit_slots_and_fleet_aggregates(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        fields = client.calls[0]["fields"]
        # The old container's exact layout: lhs v7/v8, rhs v9/v10, mid
        # v11/v12, SoC then power, power UNNEGATED (negative = charge).
        assert fields["v7"] == 60.0 + 2 * len("lhs")
        assert fields["v8"] == -500.0
        assert fields["v9"] == 60.0 + 2 * len("rhs")
        assert fields["v10"] == 990.0
        assert fields["v11"] == 60.0 + 2 * len("mid")
        assert fields["v12"] == -1000.0
        # b1 is the fleet sum in POD-MANAGER's convention (the adapter flips
        # it onto the specification's); b2 is the mean SoC over identical
        # pod capacities.
        assert fields["b1"] == -500.0 + 990.0 - 1000.0
        expected_soc = (
            60.0 + 2 * len("lhs") + 60.0 + 2 * len("rhs") + 60.0 + 2 * len("mid")
        ) / 3.0
        assert fields["b2"] == pytest.approx(expected_soc)

    async def test_a_bad_field_is_omitted_never_zero_filled(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(
            observations,
            clock,
            lhs=_observation("lhs", clock, power_quality="bad"),
        )
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        fields = client.calls[0]["fields"]
        assert "v8" not in fields  # lhs power: quality bad -> omitted
        assert "v7" in fields  # lhs SoC still good -> posted
        assert "v10" in fields and "v12" in fields

    async def test_a_stale_pod_contributes_no_fields(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(
            observations,
            clock,
            rhs=_observation("rhs", clock, age_s=400.0),  # beyond max_sample_age_s
        )
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        fields = client.calls[0]["fields"]
        assert "v9" not in fields and "v10" not in fields
        assert "v7" in fields

    async def test_an_all_stale_fleet_skips_the_slot_and_the_gap_counts_once(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        observations.latest = {
            unit_id: _observation(unit_id, clock, age_s=400.0) for unit_id in UNIT_IDS
        }
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert client.calls == []
        assert uploader.status_payload()["slots_skipped_stale"] == 0  # judged at close
        clock.advance(300.0)  # the next slot's first tick closes 04:00
        await uploader.tick()
        assert uploader.status_payload()["slots_skipped_stale"] == 1
        # The stale slot itself is never revisited.
        assert client.calls == []

    async def test_b2_omits_together_with_b1_when_power_is_not_computable(self) -> None:
        """The specification makes b1 mandatory when any battery field is
        sent: no fresh-good power word anywhere means no b1, and therefore
        no b2 -- even with healthy SoC words."""
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(
            observations,
            clock,
            lhs=_observation("lhs", clock, power_quality="bad"),
            mid=_observation("mid", clock, power_quality="stale"),
            rhs=_observation("rhs", clock, power_quality="missing"),
        )
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        fields = client.calls[0]["fields"]
        assert "b1" not in fields
        assert "b2" not in fields
        assert "v7" in fields  # the per-unit SoC slots still post

    async def test_native_fields_off_omits_b1_and_b2(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client, native=False)
        await uploader.tick()
        fields = client.calls[0]["fields"]
        assert "b1" not in fields and "b2" not in fields
        assert "v7" in fields

    async def test_a_fresh_fleet_with_no_good_fields_skips_the_post_loudly(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(
            observations,
            clock,
            lhs=_observation("lhs", clock, power_quality="bad", soc_quality="bad"),
            mid=_observation("mid", clock, power_quality="bad", soc_quality="bad"),
            rhs=_observation("rhs", clock, power_quality="bad", soc_quality="bad"),
        )
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert client.calls == []
        assert "quality-gated" in (uploader.status_payload()["last_error"] or "")

    async def test_the_post_stamp_is_the_freshest_observation_never_the_future(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock, lhs=_observation("lhs", clock, age_s=90.0))
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        stamp = client.calls[0]["sample_at"]
        assert stamp <= clock.wall_now()
        assert stamp.tzinfo is not None


class TestFailureSemantics:
    async def test_an_auth_refusal_disables_the_uploader_loudly(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.script = [PvOutputAuthError("Read only key")]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        status = uploader.status_payload()
        assert status["disabled_reason"] == "auth_failed"
        assert status["last_error"] == "Read only key"
        assert status["consecutive_failures"] == 1
        # Disabled means disabled: no further wire attempts at any later slot.
        clock.advance(300.0)
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1

    async def test_the_operators_re_enable_clears_the_auth_latch(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.script = [PvOutputAuthError("Unauthorized API Key")]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert uploader.status_payload()["disabled_reason"] == "auth_failed"
        uploader.set_enabled(True)
        assert uploader.status_payload()["disabled_reason"] is None
        clock.advance(300.0)  # the next slot retries the (fixed) credential
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2

    async def test_the_rate_limit_backs_off_to_pvoutputs_reset_instant(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        reset_unix = int(clock.wall_now().timestamp()) + 120
        client.script = [
            PvOutputRateLimited(
                "Exceeded number requests per hour", reset_at_unix=reset_unix
            )
        ]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert uploader.status_payload()["last_error"].startswith("Exceeded")
        clock.advance(60.0)  # still inside the backoff window
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1
        clock.advance(70.0)  # past the reset instant: posting resumes
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2

    async def test_a_400_surfaces_the_reason_verbatim_and_spends_the_slot(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.script = [PvOutputRejected("Moon Powered")]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert uploader.status_payload()["last_error"] == "Moon Powered"
        clock.advance(120.0)  # past the retry spacing: the refused payload never retries
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1

    async def test_a_transient_failure_retries_once_per_slot_never_bursts(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.script = [
            PvOutputUnavailable("pvoutput is unhealthy (HTTP 503)"),
            PvOutputUnavailable("pvoutput is unhealthy (HTTP 503)"),
        ]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        assert len(client.calls) == 1
        clock.advance(30.0)  # inside the 60 s retry spacing: no burst
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 1
        clock.advance(35.0)  # 04:02:20 -- the one spaced retry
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2
        assert uploader.status_payload()["consecutive_failures"] == 2
        clock.advance(90.0)  # the slot's budget is spent: nothing more
        _seed(observations, clock)
        await uploader.tick()
        assert len(client.calls) == 2

    async def test_a_successful_retry_resets_the_failure_streak_and_records_the_rate(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.script = [PvOutputUnavailable("timeout")]
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()
        clock.advance(65.0)
        _seed(observations, clock)
        await uploader.tick()  # the retry succeeds
        status = uploader.status_payload()
        assert status["consecutive_failures"] == 0
        assert status["rate_remaining"] == 57
        assert status["last_error"] is None


class TestSuppression:
    async def test_an_exploding_client_never_raises_into_the_fleet_cycle(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        client.broken = True
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()  # must not raise
        assert uploader.status_payload()["consecutive_failures"] == 1

    async def test_an_unreadable_observation_store_never_raises_into_the_cycle(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        observations.fail = True
        client = FakeClient()
        uploader = _uploader(clock=clock, observations=observations, client=client)
        await uploader.tick()  # must not raise
        assert client.calls == []

    async def test_a_disabled_uploader_touches_no_port(self) -> None:
        clock = _at_minute(4, 0, second=35)
        observations = FakeObservations()
        client = FakeClient()
        _seed(observations, clock)
        uploader = _uploader(clock=clock, observations=observations, client=client, enabled=False)
        await uploader.tick()
        assert client.calls == []
        assert uploader.status_payload()["enabled"] is False


class TestTheDurableToggle:
    async def test_the_toggle_writes_the_store_before_the_flip(self) -> None:
        clock = _at_minute(4, 0, second=35)
        store = InMemoryPvOutputStateRepository()
        uploader = _uploader(
            clock=clock, observations=FakeObservations(), store=store, enabled=False
        )
        uploader.set_enabled(True)
        assert store.state() is not None
        assert store.state()[0] is True
        status = uploader.status_payload()
        assert status["enabled"] is True
        assert status["enabled_origin"] == "runtime"

    async def test_a_failing_store_write_refuses_and_flips_nothing(self) -> None:
        clock = _at_minute(4, 0, second=35)
        store = ExplodingStore()
        uploader = _uploader(
            clock=clock, observations=FakeObservations(), store=store, enabled=False
        )
        with pytest.raises(PvOutputRefusal) as raised:
            uploader.set_enabled(True)
        assert raised.value.code == "pvoutput_toggle_failed"
        assert uploader.enabled is False
        assert uploader.enabled_origin == "config"
        assert store.writes == 1

    async def test_boot_composes_from_the_durable_row_over_the_config(self) -> None:
        store = InMemoryPvOutputStateRepository()
        store.store(enabled=True, updated_at="2026-08-24T09:00:00+00:00")
        uploader = _uploader(
            clock=_at_minute(4, 0),
            observations=FakeObservations(),
            store=store,
            enabled=False,  # the config says off; the operator said on
        )
        assert uploader.enabled is True
        assert uploader.enabled_origin == "runtime"

    async def test_a_storeless_composition_refuses_the_toggle(self) -> None:
        uploader = PvOutputUploader(
            unit_ids=UNIT_IDS,
            unit_slots=dict(UNIT_SLOTS),
            timezone_name="UTC",
            interval_s=300.0,
            max_sample_age_s=120.0,
            retry_max=1,
            native_battery_fields=True,
            config_enabled=True,
            client=FakeClient(),
            clock=_at_minute(4, 0),
            observations=FakeObservations(),
            store=None,
        )
        with pytest.raises(PvOutputRefusal, match="toggle store"):
            uploader.set_enabled(False)

    async def test_a_missing_client_composes_as_missing_credentials(self) -> None:
        uploader = _uploader(
            clock=_at_minute(4, 0),
            observations=FakeObservations(),
            client=_NO_CLIENT,
            credentials_note="the credential environment variables are not set",
        )
        status = uploader.status_payload()
        assert status["disabled_reason"] == "missing_credentials"
        assert status["credentials_note"] is not None
