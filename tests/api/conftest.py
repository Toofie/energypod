"""Boundary fakes shared by REST, event-stream, and MCP contract tests."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    interactive: bool = False
    site_id: str = "home"


PRINCIPALS = {
    "viewer-token": Principal("person:viewer", frozenset({"observe"})),
    "auditor-token": Principal("person:auditor", frozenset({"observe", "audit:read"})),
    # Holds audit:read but NOT the baseline observe scope; must be refused audit.
    "audit-only-token": Principal("person:audit-only", frozenset({"audit:read"})),
    "operator-token": Principal(
        "person:operator",
        frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
        interactive=True,
    ),
    "second-operator-token": Principal(
        "person:second-operator",
        frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
        interactive=True,
    ),
    "service-token": Principal(
        "service:optimizer",
        frozenset({"observe", "dispatch"}),
        interactive=False,
    ),
    "noninteractive-operator-token": Principal(
        "service:operator-automation",
        frozenset({"observe", "dispatch", "arm"}),
        interactive=False,
    ),
}


class FakeAuthenticator:
    def __init__(self) -> None:
        self.presented_tokens: list[str] = []

    async def authenticate(self, bearer_token: str) -> Principal | None:
        self.presented_tokens.append(bearer_token)
        return PRINCIPALS.get(bearer_token)


def _cell_ladder(low_v: float, high_v: float, count: int) -> list[float]:
    """Deterministic cell-voltage ladder whose endpoints are exact literals."""
    steps = max(1, round((high_v - low_v) / 0.001))
    ladder = [low_v + step * 0.001 for step in range(steps)]
    ladder.append(high_v)
    return [ladder[index % len(ladder)] for index in range(count)]


# API_CONTRACTS "Application service facade": the snapshot carries a nullable
# per-unit telemetry summary; every field is null when the observation lacks
# that datum, never zero-filled.  These are the live-decoded reference values
# (MID 10% / 192.4 V / 60 cells 3.205-3.209 V / 23-28 C, both warnings, no
# faults) so the boundary round-trips exactly what the facade derives.
MID_TELEMETRY_SUMMARY: dict[str, Any] = {
    "soc_pct": 10.0,
    "bms_soc_pct": 10.0,
    "soh_pct": 99.0,
    "pack_voltage_v": 192.4,
    "pack_current_a": 12.5,
    "battery_watts": 2405.0,
    "dynamic_charge_limit_w": 2500.0,
    "dynamic_discharge_limit_w": 3000.0,
    "cell_count": 60,
    "cell_min_v": 3.205,
    "cell_max_v": 3.209,
    "cell_spread_mv": (3.209 - 3.205) * 1000.0,
    "temperature_min_c": 23.0,
    "temperature_max_c": 28.0,
    "active_faults": [],
    "active_warnings": ["DCDC_Warning0_1", "PCS_Warning0_1"],
}

MID_TEMPERATURES_C: list[float] = [23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 24.5, 25.5]

# The full single-unit projection served by GET /api/v1/units/{unit_id}: the
# summary scalars plus identity, sequences, capture times, the complete arrays,
# and the per-field quality map.
UNIT_DETAIL_PROJECTIONS: dict[str, dict[str, Any]] = {
    "MID": {
        "unit_id": "MID",
        "device_identity": "BEP0005KXX11B10500055",
        "protocol_profile": "iot-v1",
        "connection_epoch": 3,
        "lifecycle": "disarmed",
        "sequence": 41,
        "captured_at_mono": 99.5,
        "cell_sequence": 12,
        "cell_captured_at_mono": 98.0,
        "wall_timestamp": "2026-08-22T00:04:05+00:00",
        **MID_TELEMETRY_SUMMARY,
        "cell_voltages_v": _cell_ladder(3.205, 3.209, 60),
        "temperatures_c": list(MID_TEMPERATURES_C),
        "quality": {
            "battery_watts": "good",
            "bms_soc_pct": "good",
            "cell_voltages_v": "good",
            "dynamic_charge_limit_w": "good",
            "dynamic_discharge_limit_w": "good",
            "pack_current_a": "good",
            "pack_voltage_v": "good",
            "soh_pct": "good",
            "system_soc_pct": "good",
            "temperatures_c": "good",
        },
    },
    # A commissioned unit that has not published an observation yet: the
    # projection exists, and every telemetry datum is null, never zero.
    "pod-empty": {
        "unit_id": "pod-empty",
        "device_identity": None,
        "protocol_profile": None,
        "connection_epoch": None,
        "lifecycle": None,
        "sequence": None,
        "captured_at_mono": None,
        "cell_sequence": None,
        "cell_captured_at_mono": None,
        "wall_timestamp": None,
        **dict.fromkeys(MID_TELEMETRY_SUMMARY),
        "cell_voltages_v": None,
        "temperatures_c": None,
        "quality": None,
    },
}


@dataclass
class RecordingEnergyService:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_revision: int = 40
    # The schedule surface's scripted answers (DESIGN_SCHEDULES §5): a view
    # body, an optional refusal (ScheduleRefusal-shaped), and an optional
    # validation error raised to the boundary.
    schedule_view: dict[str, Any] = field(
        default_factory=lambda: {
            "plan": None,
            "policy": {
                "posture": "yield",
                "allowed_windows_local": [["06:00", "20:00"]],
                "intent_ttl_s": 10.0,
            },
            "acknowledged_night_windows": False,
            "next_action": None,
        }
    )
    schedule_result: dict[str, Any] = field(
        default_factory=lambda: {
            "version": 1,
            "plan": {
                "version": 1,
                "timezone": "Australia/Brisbane",
                "entries": [],
            },
            "diff": {"added": [], "removed": [], "changed": [], "timezone_changed": False},
            "acknowledged_night_windows": False,
            "next_action": None,
        }
    )
    schedule_refusal: Any = None
    schedule_error: Any = None
    # The plant-history surface's scripted answers (DESIGN_PLANT_HISTORY
    # section 3): a query body, an optional refusal, an optional validation
    # error raised to the boundary.
    history_view: dict[str, Any] = field(
        default_factory=lambda: {
            "from": "2026-08-25T06:00:00+00:00",
            "to": "2026-08-26T06:00:00+00:00",
            "resolution": "full",
            "points": 600,
            "fields": [
                "bms_soc_pct",
                "battery_watts",
                "grid_power_w",
                "temperature_min_c",
                "temperature_max_c",
            ],
            "units": {
                "mid": {
                    "first_sample_at": "2026-08-25T06:00:30+00:00",
                    "last_sample_at": "2026-08-26T05:59:30+00:00",
                    "sample_count": 2871,
                    "quality_worst": "good",
                    "gaps": [
                        {
                            "from": "2026-08-26T02:10:00+00:00",
                            "to": "2026-08-26T03:40:30+00:00",
                        }
                    ],
                    "series": {
                        "battery_watts": {
                            "window_min": -2503.0,
                            "window_min_at": "2026-08-26T00:41:00+00:00",
                            "window_max": 914.0,
                            "window_max_at": "2026-08-25T19:12:30+00:00",
                            "sample_count": 2871,
                            "points": [
                                {"t": "2026-08-25T06:00:30+00:00", "v": -521.0},
                                {"t": "2026-08-26T05:59:30+00:00", "v": -2498.0},
                            ],
                        }
                    },
                }
            },
            "fleet": {
                "series": {
                    "grid_power_w": {
                        "window_min": -2901.0,
                        "window_min_at": "2026-08-25T14:10:00+00:00",
                        "window_max": 1204.0,
                        "window_max_at": "2026-08-25T19:12:30+00:00",
                        "sample_count": 2871,
                        "points": [
                            {"t": "2026-08-25T06:00:30+00:00", "v": -1521.0},
                            {"t": "2026-08-26T05:59:30+00:00", "v": -2510.0},
                        ],
                    }
                },
                "gaps": [],
            },
        }
    )
    history_refusal: Any = None
    history_error: Any = None
    history_units: tuple[str, ...] = ("mid", "rhs")
    # The energy scorecard's scripted answers (API_CONTRACTS "Energy
    # scorecard"): the days body and an optional refusal.
    energy_days_view: dict[str, Any] = field(
        default_factory=lambda: {
            "days": [
                {
                    "date": "2026-08-25",
                    "timezone": "Australia/Brisbane",
                    "utc_offset_minutes": 600,
                    "kind": "complete",
                    "units": {},
                    "fleet": {
                        "grid_import_kwh": 8.3,
                        "grid_export_kwh": 12.8,
                        "battery_charged_kwh": 6.2,
                        "battery_discharged_kwh": 4.1,
                        "load_kwh": 14.7,
                        "charged_from_surplus_kwh": 3.1,
                        "coverage_pct": 99.4,
                    },
                    "sources": {
                        "grid": "integrated_ct",
                        "battery": "device_counter",
                        "load": "device_counter",
                        "surplus": "attributed_adviser",
                    },
                    "counter_cross_check": {
                        "grid_a_delta_kwh": 8.3,
                        "grid_b_delta_kwh": 12.8,
                        "consistent_with": "vendor_labels",
                        "discriminating": True,
                    },
                    "solar_production_measured": False,
                }
            ],
            "grid_counter_roles": "unpinned",
            "solar_production_measured": False,
            "tariff": None,
        }
    )
    energy_refusal: Any = None
    # The night-writer detector's scripted answer (API_CONTRACTS "Night-writer
    # detector"): the observed-objectives window body and an optional error.
    observed_objectives_view: dict[str, Any] = field(
        default_factory=lambda: {
            "as_of": "2026-08-26T22:30:00+00:00",
            "last": "24h",
            "window_s": 86400,
            "units": [
                {
                    "unit_id": "pod-a",
                    "first_seen_at": "2026-08-25T23:41:00+00:00",
                    "last_seen_at": "2026-08-26T22:29:31+00:00",
                    "sample_count": 641,
                    "charge_sample_count": 641,
                    "discharge_sample_count": 0,
                    "min_active_w": -2400,
                    "typical_active_w": -2400,
                    "max_active_w": -2400,
                    "classification_counts": {
                        "pod_autonomy_objective_observed": 0,
                        "expected_nightly_charge": 0,
                        "handback_grace": 0,
                        "foreign_objective_observed": 641,
                    },
                    "foreign_episode_count": 1,
                    "foreign_active": True,
                    "foreign_reason": "sustained_charge_without_pv_evidence",
                    "last_objective_observed": {
                        "observed_at": "2026-08-26T22:29:31+00:00",
                        "active_w": -2400,
                        "reactive_var": 0,
                        "classification": "foreign_objective_observed",
                        "reason": "sustained_charge_without_pv_evidence",
                        "lifecycle": "disarmed",
                        "claimed": False,
                        "run_mode_w": 1,
                        "ctrl_mode_w": 1,
                        "work_mode_w": 6,
                        "debug_mode_w": 0,
                        "grid_power_w": -1500.0,
                    },
                }
            ],
        }
    )
    observed_objectives_error: Any = None

    async def snapshot(self, *, principal: Principal) -> dict[str, Any]:
        self.calls.append(("snapshot", {"principal": principal}))
        return {
            "site_id": "home",
            "snapshot_sequence": 20,
            "captured_at": "2026-08-21T01:02:03Z",
            "units": [
                {
                    "unit_id": "pod-a",
                    "lifecycle": "disarmed",
                    "telemetry_age_s": 0.4,
                    "quality": "good",
                    "requested_power": {"direction": "idle", "watts": 0},
                    "authorized_power": {"direction": "idle", "watts": 0},
                    "measured_watts": 0,
                    "telemetry": dict(MID_TELEMETRY_SUMMARY),
                },
                {
                    "unit_id": "pod-b",
                    "lifecycle": "disarmed",
                    "telemetry_age_s": None,
                    "quality": "missing",
                    "requested_power": {"direction": "idle", "watts": 0},
                    "authorized_power": None,
                    "measured_watts": None,
                    "telemetry": None,
                },
            ],
        }

    async def unit_detail(self, *, principal: Principal, unit_id: str) -> dict[str, Any]:
        self.calls.append(("unit_detail", {"principal": principal, "unit_id": unit_id}))
        projection = UNIT_DETAIL_PROJECTIONS.get(unit_id)
        if projection is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        return projection

    async def health(self, *, principal: Principal) -> dict[str, Any]:
        self.calls.append(("health", {"principal": principal}))
        return {
            "liveness": {"ok": True},
            "service_readiness": {"ready": True, "reasons": []},
            "control_readiness": {"ready": False, "reasons": ["units_disarmed"]},
        }

    async def recent_audit(
        self, *, principal: Principal, limit: int, cursor: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("recent_audit", {"principal": principal, "limit": limit, "cursor": cursor})
        )
        return {
            "events": [{"sequence": 7, "type": "intent.accepted", "request_id": "req-old"}],
            "next_cursor": None,
        }

    async def submit_intent(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("submit_intent", kwargs))
        self.next_revision += 1
        return {
            "intent_id": "intent-server-1",
            "acceptance_revision": self.next_revision,
            "accepted_at_monotonic": 123.5,
            "status": "accepted",
            "requested": {
                "direction": kwargs["direction"],
                "watts": kwargs["watts"],
            },
            "authorized": None,
            "measured": None,
            "expires_in_s": kwargs["ttl_s"],
        }

    async def arm(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("arm", kwargs))
        return {"unit_ids": kwargs["unit_ids"], "status": "armed_idle"}

    async def disarm(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("disarm", kwargs))
        return {
            "units": [
                {"unit_id": unit_id, "status": "disarmed", "reason": "disarmed"}
                for unit_id in kwargs["unit_ids"]
            ]
        }

    async def emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("emergency_stop", kwargs))
        return {"stop_id": "stop-server-1", "status": "latched"}

    async def acknowledge_emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("acknowledge_emergency_stop", kwargs))
        return {"stop_id": kwargs["stop_id"], "status": "acknowledged"}

    async def acknowledge_inhibit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("acknowledge_inhibit", kwargs))
        if kwargs["unit_id"] == "pod-ghost":
            raise LookupError("no unit with id 'pod-ghost'")
        return {
            "unit_id": kwargs["unit_id"],
            "status": "acknowledged",
            "latch_cleared": True,
        }

    async def set_excess_charging(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("set_excess_charging", kwargs))
        enabled = kwargs["action"] == "enable"
        return {
            "feature": "excess_charging",
            "enabled": enabled,
            "enabled_origin": "runtime",
            "persisted": False,
            "acknowledged_economics": True,
            "adviser_state": {
                "enabled": enabled,
                "enabled_origin": "runtime",
                "acknowledged_economics": True,
                "active": False,
                "hysteresis_state": "inactive",
                "reason_codes": ["export_headroom_available"],
            },
        }

    async def set_night_charging(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("set_night_charging", kwargs))
        enabled = kwargs["action"] == "enable"
        return {
            "feature": "night_charging",
            "enabled": enabled,
            "enabled_origin": "runtime",
            "persisted": False,
            "acknowledged_partition": True,
            "night_charge_state": {
                "enabled": enabled,
                "enabled_origin": "runtime",
                "acknowledged_partition": True,
                "posture": "partition",
                "active": False,
                "phase": "idle",
                "reason_codes": ["window_open"] if enabled else ["disabled_by_runtime"],
            },
        }

    async def get_schedule(self, *, principal: Any) -> dict[str, Any]:
        self.calls.append(("get_schedule", {"principal": principal}))
        if self.schedule_refusal is not None:
            raise self.schedule_refusal
        return dict(self.schedule_view)

    async def get_energy_days(self, *, principal: Any, limit: int = 8) -> dict[str, Any]:
        self.calls.append(("get_energy_days", {"principal": principal, "limit": limit}))
        if self.energy_refusal is not None:
            raise self.energy_refusal
        return dict(self.energy_days_view)

    async def get_plant_history(
        self,
        *,
        principal: Any,
        range_from: str,
        range_to: str,
        unit_ids: list[str] | None = None,
        fields: list[str] | None = None,
        points: int = 600,
    ) -> dict[str, Any]:
        # The production facade validates before reading; the fake applies
        # the SAME parser so the boundary's 422 mapping is exercised against
        # the real rule set (one implementation, zero drift), and a refused
        # request never reaches the recorded call.
        from energypod.application.history import parse_plant_history_query

        parse_plant_history_query(
            range_from=range_from,
            range_to=range_to,
            unit_ids=unit_ids,
            fields=fields,
            points=points,
            configured_units=self.history_units,
        )
        self.calls.append(
            (
                "get_plant_history",
                {
                    "principal": principal,
                    "range_from": range_from,
                    "range_to": range_to,
                    "unit_ids": unit_ids,
                    "fields": fields,
                    "points": points,
                },
            )
        )
        if self.history_refusal is not None:
            raise self.history_refusal
        if self.history_error is not None:
            raise self.history_error
        return dict(self.history_view)

    async def get_observed_objectives(self, *, principal: Any, last: str = "24h") -> dict[str, Any]:
        self.calls.append(("get_observed_objectives", {"principal": principal, "last": last}))
        if self.observed_objectives_error is not None:
            raise self.observed_objectives_error
        # The production facade parses the window before reading; the fake
        # applies the same parser so the boundary's 422 mapping is exercised
        # against the real rule (one implementation, zero drift).
        from energypod.application.service import parse_objective_window_hours

        hours = parse_objective_window_hours(last)
        payload = dict(self.observed_objectives_view)
        payload["last"] = last
        payload["window_s"] = hours * 3600
        return payload

    async def replace_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("replace_schedule", kwargs))
        if self.schedule_refusal is not None:
            raise self.schedule_refusal
        if self.schedule_error is not None:
            raise self.schedule_error
        return dict(self.schedule_result)


class MutableMonotonicClock:
    """Injectable ticket clock: tests advance time deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now


class FakeEventSource:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.subscriptions: list[int | None] = []
        self.closed_subscriptions = 0

    async def subscribe(self, *, after_sequence: int | None) -> AsyncIterator[dict[str, Any]]:
        self.subscriptions.append(after_sequence)
        try:
            for event in self.events:
                yield event
        finally:
            self.closed_subscriptions += 1


def load_contract_module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"guarded boundary contract is not implemented: {name}: {exc}")


@pytest.fixture
def service() -> RecordingEnergyService:
    return RecordingEnergyService()


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()
