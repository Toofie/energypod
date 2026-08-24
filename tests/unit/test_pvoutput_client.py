"""The pvoutput.org adapter contract (the retiring Docker writer's replacement).

Every wire fact asserted here carries its evidence in the adapter's own
header (adapters/pvoutput/client.py) and in the pinned sources:

- **Endpoint + headers**: the add-status specification's URL with
  ``X-Pvoutput-Apikey`` / ``X-Pvoutput-SystemId`` (the old container's exact
  header names, byd/scheduler.py) plus ``X-Rate-Limit: 1`` -- the spec's own
  switch that surfaces ``X-Rate-Limit-Remaining`` for observability.
- **d/t**: the specification's ``yyyymmdd`` / ``hh:mm`` civil formats,
  derived from the observation's UTC stamp converted to the SITE timezone
  (Australia/Brisbane, UTC+10) and floored onto the pinned 5-minute slot
  grid -- so the posted slot is always the one the data belongs to and never
  a future stamp.
- **Sign continuity (PROTOCOL_EVIDENCE 4b/4c)**: the battery power family
  (BMS 0x5008 / DCDC 0x2009 / system 0x0114, and the PCS 0x1007 grid P the
  operator remembers as "reg 4103") is live-proven NEGATIVE = CHARGE /
  POSITIVE = DISCHARGE -- the same convention pod-manager decodes -- so the
  per-unit v-slots post UNNEGATED (the old container's raw-signed posts stay
  continuous on the dashboard).
- **b1's spec-mandated flip**: PVOutput's native battery field is the
  OPPOSITE ("positive ... charging", example "-200 (Discharge), 200
  (Charge)" -- the add-status specification), so the fleet aggregate arrives
  in pod-manager's convention and NEGATES exactly at this boundary.
- **Typed refusals**: 401 / non-rate 403 (read-only key, donation mode --
  v7-v12 and b1/b2 are donation-tier parameters) are auth-class; the rate
  403 ("Exceeded number requests per hour") is rate-limited with the reset
  instant; 400 carries PVOutput's reason VERBATIM; 5xx and timeouts are
  unavailability.

All tests run against a scripted fake transport: no socket, no quota, no
secret (the key material below is test fixture text).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from energypod.adapters.pvoutput.client import (
    PVOUTPUT_ADD_STATUS_URL,
    HttpxPvOutputTransport,
    PvOutputAuthError,
    PvOutputHttpResponse,
    PvOutputRateLimited,
    PvOutputRejected,
    PvOutputStatusClient,
    PvOutputUnavailable,
    floor_to_slot,
)

API_KEY = "test-api-key-material"
SYSTEM_ID = "99999"


@dataclass
class FakeTransport:
    """The scripted wire: one queued answer (a response or a raise) per call."""

    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def post_form(
        self,
        url: str,
        *,
        data: dict[str, str],
        headers: dict[str, str],
    ) -> PvOutputHttpResponse:
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _ok(rate_remaining: str | None = "57") -> PvOutputHttpResponse:
    headers = {"Content-Type": "text/plain"}
    if rate_remaining is not None:
        headers["X-Rate-Limit-Remaining"] = rate_remaining
        headers["X-Rate-Limit-Limit"] = "60"
        headers["X-Rate-Limit-Reset"] = "1756104000"
    return PvOutputHttpResponse(200, "OK 200: Added Status", headers)


def _client(transport: FakeTransport, **overrides: Any) -> PvOutputStatusClient:
    fields: dict[str, Any] = {
        "transport": transport,
        "api_key": API_KEY,
        "system_id": SYSTEM_ID,
        "timezone_name": "Australia/Brisbane",
    }
    fields.update(overrides)
    return PvOutputStatusClient(**fields)


class TestRequestShape:
    async def test_the_endpoint_headers_and_rate_limit_switch(self) -> None:
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 7, 33, tzinfo=UTC),
            fields={"v7": 61},
        )
        call = transport.calls[0]
        assert call["url"] == PVOUTPUT_ADD_STATUS_URL == (
            "https://pvoutput.org/service/r2/addstatus.jsp"
        )
        assert call["headers"] == {
            "X-Pvoutput-Apikey": API_KEY,
            "X-Pvoutput-SystemId": SYSTEM_ID,
            "X-Rate-Limit": "1",
        }

    async def test_d_and_t_format_in_the_site_timezone_floored_to_five_minutes(self) -> None:
        """04:07:33 UTC is 14:07:33 in Brisbane: the slot stamps 14:05 on
        2026-08-25 -- the observation's own moment, never a future slot."""
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 7, 33, tzinfo=UTC), fields={"v8": -500}
        )
        data = transport.calls[0]["data"]
        assert data["d"] == "20260825"
        assert data["t"] == "14:05"

    async def test_the_site_day_owns_the_date_not_the_utc_day(self) -> None:
        """14:02 UTC is already the next civil day in Brisbane (00:02): the
        posted date is the SITE's date with the midnight slot."""
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 14, 2, 11, tzinfo=UTC), fields={"v9": 80}
        )
        data = transport.calls[0]["data"]
        assert data["d"] == "20260826"
        assert data["t"] == "00:00"

    async def test_the_slot_floor_is_pinned_at_five_minutes(self) -> None:
        local = datetime(2026, 8, 25, 23, 59, 59, tzinfo=ZoneInfo("Australia/Brisbane"))
        assert floor_to_slot(local).strftime("%H:%M") == "23:55"

    async def test_field_values_coerce_to_whole_numbers_like_the_old_container(self) -> None:
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
            fields={"v7": 61.4, "v8": -500.6},
        )
        data = transport.calls[0]["data"]
        assert data["v7"] == "61"
        assert data["v8"] == "-501"


class TestSignContinuity:
    async def test_the_per_unit_power_slots_post_unnegated(self) -> None:
        """The dashboard-continuity pin: ``battery_watts`` and the old
        container's raw-signed word (DCDC 0x2009, and the PCS 0x1007 grid P
        remembered as "reg 4103") share the live-proven negative = charge
        orientation, so a -500 W charge posts as -500 on v8/v10/v12."""
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
            fields={"v8": -500, "v10": 990, "v12": -2498},
        )
        data = transport.calls[0]["data"]
        assert (data["v8"], data["v10"], data["v12"]) == ("-500", "990", "-2498")

    async def test_b1_flips_onto_the_specifications_opposite_sign(self) -> None:
        """The native battery field is the ONE flipped quantity: the uploader
        sums per-pod ``battery_watts`` in pod-manager's convention (a -1500 W
        fleet charge) and the adapter negates it onto the specification's
        positive = charge ("-200 (Discharge), 200 (Charge)")."""
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
            fields={"v8": -500, "b1": -1500, "b2": 66.6},
        )
        data = transport.calls[0]["data"]
        assert data["b1"] == "1500"
        assert data["v8"] == "-500"
        assert data["b2"] == "67"

    async def test_a_discharging_fleet_posts_negative_b1(self) -> None:
        transport = FakeTransport(script=[_ok()])
        await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
            fields={"b1": 720},
        )
        assert transport.calls[0]["data"]["b1"] == "-720"

    async def test_the_reporter_vocabulary_never_touches_v1_to_v6(self) -> None:
        """v1-v6 (generation, consumption, temperature, voltage) belong to the
        solar inverter's integration on this site: a caller trying to write
        one through this boundary is refused, not silently forwarded."""
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="v1-v6"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
                fields={"v1": 1000},
            )
        assert transport.calls == []


class TestAcceptedAnswers:
    async def test_the_rate_limit_headers_surface_for_observability(self) -> None:
        transport = FakeTransport(script=[_ok("43")])
        result = await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
        )
        assert result.rate_remaining == 43
        assert result.rate_limit == 60
        assert result.rate_reset_unix == 1756104000

    async def test_answers_without_rate_headers_stay_honest_nulls(self) -> None:
        transport = FakeTransport(script=[_ok(rate_remaining=None)])
        result = await _client(transport).post_status(
            sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
        )
        assert result.rate_remaining is None


class TestTypedRefusals:
    async def test_unauthorized_is_the_auth_class(self) -> None:
        transport = FakeTransport(
            script=[PvOutputHttpResponse(401, "Unauthorized API Key", {})]
        )
        with pytest.raises(PvOutputAuthError, match="Unauthorized API Key"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_the_read_only_key_403_is_the_auth_class(self) -> None:
        transport = FakeTransport(script=[PvOutputHttpResponse(403, "Read only key", {})])
        with pytest.raises(PvOutputAuthError, match="Read only key"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_the_donation_mode_403_is_the_auth_class(self) -> None:
        """v7-v12 and b1/b2 are donation-tier parameters: a non-donating
        site's refusal is a standing credential fact, so it lands in the same
        loud, non-retryable class as a read-only key."""
        transport = FakeTransport(
            script=[PvOutputHttpResponse(403, "Donation Mode", {})]
        )
        with pytest.raises(PvOutputAuthError, match="Donation Mode"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_the_rate_403_carries_the_reset_instant(self) -> None:
        transport = FakeTransport(
            script=[
                PvOutputHttpResponse(
                    403,
                    "Exceeded number requests per hour",
                    {"X-Rate-Limit-Reset": "1756104000"},
                )
            ]
        )
        with pytest.raises(PvOutputRateLimited, match="Exceeded number requests") as raised:
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )
        assert raised.value.reset_at_unix == 1756104000

    async def test_a_400_surfaces_pvoutputs_reason_verbatim(self) -> None:
        transport = FakeTransport(
            script=[PvOutputHttpResponse(400, "Invalid Date 20261301", {})]
        )
        with pytest.raises(PvOutputRejected, match=r"^Invalid Date 20261301$"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    @pytest.mark.parametrize("status", [500, 503])
    async def test_server_failures_are_unavailability(self, status: int) -> None:
        transport = FakeTransport(
            script=[PvOutputHttpResponse(status, "server error", {})]
        )
        with pytest.raises(PvOutputUnavailable, match=str(status)):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_transport_timeouts_normalize_to_unavailability(self) -> None:
        transport = FakeTransport(script=[PvOutputUnavailable("pvoutput request timed out")])
        with pytest.raises(PvOutputUnavailable, match="timed out"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_unexpected_status_codes_are_unavailability(self) -> None:
        transport = FakeTransport(script=[PvOutputHttpResponse(405, "POST or GET only", {})])
        with pytest.raises(PvOutputUnavailable, match="405"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": 61}
            )

    async def test_the_failure_class_attribute_rides_every_error(self) -> None:
        """The structural word the application uploader classifies through --
        the layering pin: the uploader never imports this adapter module."""
        assert PvOutputAuthError.pvoutput_failure == "auth"
        assert PvOutputRateLimited.pvoutput_failure == "rate_limited"
        assert PvOutputRejected.pvoutput_failure == "rejected"
        assert PvOutputUnavailable.pvoutput_failure == "unavailable"


class TestConstruction:
    def test_credentials_and_timezone_are_validated(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="api_key"):
            _client(transport, api_key="   ")
        with pytest.raises(ValueError, match="system_id"):
            _client(transport, system_id="")
        with pytest.raises(ValueError, match="IANA"):
            _client(transport, timezone_name="Not/AZone")

    def test_the_production_transport_bounds_its_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            HttpxPvOutputTransport(timeout_s=0.0)

    async def test_a_naive_sample_stamp_is_refused(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="timezone-aware"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0), fields={"v7": 61}
            )

    async def test_non_numeric_field_values_are_refused(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(TypeError, match="numbers"):
            await _client(transport).post_status(
                sample_at=datetime(2026, 8, 25, 4, 0, tzinfo=UTC), fields={"v7": "61"}
            )
