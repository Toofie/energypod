"""Red-phase HTTP contracts for the guarded ``/api/v1`` boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from energypod.application.excess_charge import ExcessChargingRefusal
from energypod.application.service import DegradedReport

from .conftest import (
    MID_TELEMETRY_SUMMARY,
    UNIT_DETAIL_PROJECTIONS,
    FakeAuthenticator,
    FakeEventSource,
    MutableMonotonicClock,
    RecordingEnergyService,
    load_contract_module,
)

API = "/api/v1"


def _client(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
    **app_kwargs: Any,
) -> TestClient:
    module = load_contract_module("energypod.api.rest")
    assert hasattr(module, "create_api_app"), "energypod.api.rest.create_api_app is required"
    app = module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
        **app_kwargs,
    )
    return TestClient(app)


def _auth(token: str, *, request_id: str = "req-123") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _mutation_headers(token: str, *, key: str = "idem-123") -> dict[str, str]:
    return {**_auth(token), "Idempotency-Key": key}


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["details"], dict | list)
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]
    return body


@pytest.mark.parametrize("path", ["/snapshot", "/health", "/audit"])
def test_read_endpoints_require_authentication(
    path: str, service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        _assert_error(client.get(API + path), 401, "authentication_required")
    assert service.calls == []


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer unknown-token",
        "bearer operator-token",
        "Bearer operator-token extra",
    ],
)
def test_malformed_or_unknown_credentials_never_reach_the_service(
    authorization: str,
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/snapshot", headers=headers)
    _assert_error(response, 401, "authentication_required")
    assert "unknown-token" not in response.text
    assert service.calls == []


def test_bearer_boundary_does_not_treat_an_ambient_cookie_as_authentication(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.get(
            f"{API}/snapshot",
            cookies={"access_token": "operator-token", "session": "operator-token"},
        )
    _assert_error(response, 401, "authentication_required")
    assert service.calls == []


def test_viewer_can_read_snapshot_and_health_but_not_audit(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        snapshot = client.get(f"{API}/snapshot", headers=_auth("viewer-token"))
        health = client.get(f"{API}/health", headers=_auth("viewer-token"))
        audit = client.get(f"{API}/audit", headers=_auth("viewer-token"))

    assert snapshot.status_code == 200
    assert snapshot.json()["units"][0]["telemetry_age_s"] == 0.4
    assert snapshot.json()["units"][0]["quality"] == "good"
    assert health.status_code == 200
    assert set(health.json()) == {"liveness", "service_readiness", "control_readiness"}
    _assert_error(audit, 403, "insufficient_scope")


def test_snapshot_serves_the_nullable_telemetry_summary_block(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """API_CONTRACTS facade amendment: every snapshot unit carries a nullable
    telemetry summary; absent data is null, never zero-filled."""
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/snapshot", headers=_auth("viewer-token"))

    assert response.status_code == 200
    units = {unit["unit_id"]: unit for unit in response.json()["units"]}
    assert units["pod-a"]["telemetry"] == MID_TELEMETRY_SUMMARY
    without_observation = units["pod-b"]
    assert without_observation["telemetry"] is None
    assert without_observation["measured_watts"] is None


def test_unit_detail_requires_authentication_and_observe_scope(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        unauthenticated = client.get(f"{API}/units/MID")
        audit_only = client.get(f"{API}/units/MID", headers=_auth("audit-only-token"))
        viewer = client.get(f"{API}/units/MID", headers=_auth("viewer-token"))

    _assert_error(unauthenticated, 401, "authentication_required")
    _assert_error(audit_only, 403, "insufficient_scope")
    assert viewer.status_code == 200
    forwarded = [values for name, values in service.calls if name == "unit_detail"]
    assert len(forwarded) == 1
    assert forwarded[0]["unit_id"] == "MID"
    assert forwarded[0]["principal"].subject == "person:viewer"


def test_unit_detail_serves_the_full_latest_observation_projection(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/units/MID", headers=_auth("viewer-token"))

    assert response.status_code == 200
    body = response.json()
    assert body == UNIT_DETAIL_PROJECTIONS["MID"]
    # Live-decoded reference values survive the boundary unchanged.
    assert body["device_identity"] == "BEP0005KXX11B10500055"
    assert body["soc_pct"] == 10.0
    assert body["pack_voltage_v"] == 192.4
    assert body["cell_count"] == 60
    assert len(body["cell_voltages_v"]) == 60
    assert body["cell_min_v"] == 3.205
    assert body["cell_max_v"] == 3.209
    assert body["temperature_min_c"] == 23.0
    assert body["temperature_max_c"] == 28.0
    assert body["active_warnings"] == ["DCDC_Warning0_1", "PCS_Warning0_1"]
    assert body["active_faults"] == []
    assert set(body["quality"]) == {
        "system_soc_pct",
        "bms_soc_pct",
        "soh_pct",
        "battery_watts",
        "pack_voltage_v",
        "pack_current_a",
        "dynamic_charge_limit_w",
        "dynamic_discharge_limit_w",
        "cell_voltages_v",
        "temperatures_c",
    }


def test_unit_detail_passes_null_fields_through_unchanged(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """A commissioned unit without observations projects nulls, never zeros."""
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/units/pod-empty", headers=_auth("viewer-token"))

    assert response.status_code == 200
    body = response.json()
    assert body == UNIT_DETAIL_PROJECTIONS["pod-empty"]
    for field in (
        "soc_pct",
        "pack_voltage_v",
        "battery_watts",
        "cell_count",
        "cell_spread_mv",
        "temperature_min_c",
        "active_faults",
        "active_warnings",
        "cell_voltages_v",
        "temperatures_c",
        "quality",
        "sequence",
    ):
        assert body[field] is None, f"{field} must be null, never a fabricated zero"


def test_unit_detail_refuses_unknown_and_malformed_unit_ids(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        malformed = client.get(f"{API}/units/-pod", headers=_auth("viewer-token"))
        unknown = client.get(f"{API}/units/pod-ghost", headers=_auth("viewer-token"))

    _assert_error(malformed, 422, "validation_error")
    _assert_error(unknown, 404, "unit_not_found")
    # Only the well-formed but unknown probe reaches the service.
    forwarded = [values for name, values in service.calls if name == "unit_detail"]
    assert [values["unit_id"] for values in forwarded] == ["pod-ghost"]


def test_auditor_read_is_bounded_and_reaches_the_service(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/audit?limit=25", headers=_auth("auditor-token"))
    assert response.status_code == 200
    assert response.json()["events"][0]["sequence"] == 7
    assert service.calls[-1][0] == "recent_audit"
    assert service.calls[-1][1]["limit"] == 25


def test_audit_endpoint_serves_real_canonical_events(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """Regression: kernel-minted AuditEvents (frozen mapping fields) must
    serialize through the REST boundary, not 500 on the frozen mapping."""
    from datetime import UTC, datetime

    from energypod.domain import IntentSource, UnitLifecycle
    from energypod.domain.audit import AuditEvent

    event = AuditEvent(
        event_id="event-0001",
        occurred_at=datetime(2026, 8, 22, tzinfo=UTC),
        monotonic_offset_s=12.5,
        process_instance_id="process-1",
        event_type="control_decision",
        generation=3,
        cycle_id="cycle-1",
        principal="person:operator",
        source=IntentSource.MANUAL,
        correlation_id="intent:i-1:revision:2",
        intent_id="i-1",
        policy_version="policy-1",
        configuration_version=1,
        observation_sequences={"MID": 41, "RHS": 12},
        reason_codes=("safety_checks_passed",),
        requested_active_w=1500,
        authorized_active_w=1200,
        request_fingerprint="ab" * 32,
        response_fingerprint="cd" * 32,
        result="authorized",
        lifecycle=UnitLifecycle.ACTIVE,
    )

    async def real_audit(**_kwargs: Any) -> dict[str, Any]:
        return {"events": [event], "next_cursor": None}

    service.recent_audit = real_audit  # type: ignore[method-assign]
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/audit", headers=_auth("auditor-token"))
    assert response.status_code == 200, response.text
    body = response.json()
    served = body["events"][0]
    assert served["observation_sequences"] == {"MID": 41, "RHS": 12}
    assert served["event_type"] == "control_decision"
    assert served["result"] == "authorized"


def test_audit_read_scope_alone_is_insufficient_without_observe(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        audit = client.get(f"{API}/audit", headers=_auth("audit-only-token"))
        snapshot = client.get(f"{API}/snapshot", headers=_auth("audit-only-token"))
    _assert_error(audit, 403, "insufficient_scope")
    _assert_error(snapshot, 403, "insufficient_scope")
    assert service.calls == []


class _PagedAuditService(RecordingEnergyService):
    """Two bounded pages; the second resumes strictly older than the first."""

    async def recent_audit(
        self, *, principal: object, limit: int, cursor: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("recent_audit", {"principal": principal, "limit": limit, "cursor": cursor})
        )
        if cursor is None:
            return {
                "events": [
                    {"sequence": 50, "type": "intent.accepted", "request_id": "req-1"},
                    {"sequence": 42, "type": "decision.evaluated", "request_id": "req-2"},
                ],
                "next_cursor": 42,
            }
        return {
            "events": [{"sequence": 41, "type": "unit.armed", "request_id": "req-3"}],
            "next_cursor": None,
        }


def test_audit_pagination_continues_through_the_returned_cursor(
    authenticator: FakeAuthenticator,
) -> None:
    service = _PagedAuditService()
    with _client(service, authenticator) as client:
        first = client.get(f"{API}/audit?limit=2", headers=_auth("auditor-token"))
        assert first.status_code == 200
        page_one = first.json()
        assert page_one["next_cursor"] == 42
        second = client.get(
            f"{API}/audit?limit=2&after_sequence={page_one['next_cursor']}",
            headers=_auth("auditor-token"),
        )
        assert second.status_code == 200
        page_two = second.json()

    assert [event["sequence"] for event in page_two["events"]] == [41]
    assert page_two["next_cursor"] is None
    forwarded = [values for name, values in service.calls if name == "recent_audit"]
    assert [item["cursor"] for item in forwarded] == [None, 42]
    assert [item["limit"] for item in forwarded] == [2, 2]


def test_audit_forwards_after_sequence_and_defaults_to_absent(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        plain = client.get(f"{API}/audit", headers=_auth("auditor-token"))
        zero = client.get(f"{API}/audit?after_sequence=0", headers=_auth("auditor-token"))
        paged = client.get(
            f"{API}/audit?limit=1&after_sequence=118", headers=_auth("auditor-token")
        )
    assert plain.status_code == zero.status_code == paged.status_code == 200
    forwarded = [values for name, values in service.calls if name == "recent_audit"]
    assert forwarded[0]["cursor"] is None
    assert forwarded[1]["cursor"] == 0
    assert set(forwarded[2]) == {"principal", "limit", "cursor"}
    assert forwarded[2]["limit"] == 1
    assert forwarded[2]["cursor"] == 118


@pytest.mark.parametrize("cursor", ["-1", "abc", "1.5", "1e2", "true", ""])
def test_audit_rejects_an_invalid_cursor_before_the_service(
    cursor: str,
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    with _client(service, authenticator) as client:
        response = client.get(
            f"{API}/audit?after_sequence={cursor}", headers=_auth("auditor-token")
        )
    _assert_error(response, 422, "validation_error")
    assert service.calls == []


@pytest.mark.parametrize("token", ["viewer-token", "auditor-token"])
def test_read_only_principals_cannot_submit_intents(
    token: str, service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 1200, "ttl_s": 15}
    with _client(service, authenticator) as client:
        response = client.post(f"{API}/intents", json=payload, headers=_mutation_headers(token))
    _assert_error(response, 403, "insufficient_scope")
    assert all(name != "submit_intent" for name, _ in service.calls)


def test_intent_schema_uses_direction_and_strictly_positive_watts(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    valid = {"unit_ids": ["pod-a"], "direction": "discharge", "watts": 1200, "ttl_s": 15}
    with _client(service, authenticator) as client:
        accepted = client.post(
            f"{API}/intents", json=valid, headers=_mutation_headers("operator-token")
        )
        for invalid in (
            {**valid, "watts": 0},
            {**valid, "watts": -1},
            {**valid, "watts": True},
            {**valid, "watts": 1.0},
            {**valid, "watts": "1200"},
            {**valid, "ttl_s": True},
            {**valid, "ttl_s": "15"},
            {**valid, "unit_ids": "pod-a"},
            {**valid, "unit_ids": []},
            {**valid, "unit_ids": ["pod-a", "pod-a"]},
            {**valid, "unit_ids": [" pod-a"]},
            {**valid, "direction": "idle"},
            {**valid, "signed_watts": -1200},
            {**valid, "register_address": 0x0200},
            {**valid, "acceptance_revision": 999},
            {**valid, "accepted_at_monotonic": 0.0},
            {**valid, "unknown": "field"},
        ):
            response = client.post(
                f"{API}/intents",
                json=invalid,
                headers=_mutation_headers("operator-token", key=f"bad-{len(str(invalid))}"),
            )
            _assert_error(response, 422, "validation_error")

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["acceptance_revision"] == 41
    assert body["accepted_at_monotonic"] == 123.5
    assert body["requested"] == {"direction": "discharge", "watts": 1200}
    assert body["authorized"] is None
    assert body["measured"] is None
    call = next(values for name, values in service.calls if name == "submit_intent")
    assert call["principal"].subject == "person:operator"
    assert call["direction"] == "discharge"
    assert call["watts"] == 1200
    assert "acceptance_revision" not in call
    assert "accepted_at_monotonic" not in call


def test_intent_accepts_per_unit_watts_mutually_exclusive_with_scalar(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """The 2026-08-23 operator ruling: one request, one watt target per battery.

    ``watts_by_unit`` carries one positive integer per selected unit and the
    key set must equal ``unit_ids`` exactly.  On the wire it is mutually
    exclusive with scalar ``watts``: one or the other, never both, never
    neither.  The scalar form keeps working unchanged.
    """
    per_unit = {
        "unit_ids": ["pod-a", "pod-b"],
        "direction": "charge",
        "ttl_s": 15,
        "watts_by_unit": {"pod-a": 1200, "pod-b": 300},
    }
    scalar = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    with _client(service, authenticator) as client:
        accepted = client.post(
            f"{API}/intents", json=per_unit, headers=_mutation_headers("operator-token")
        )
        scalar_accepted = client.post(
            f"{API}/intents",
            json=scalar,
            headers=_mutation_headers("operator-token", key="scalar-still-works"),
        )
        without_map = {key: value for key, value in per_unit.items() if key != "watts_by_unit"}
        for index, invalid in enumerate(
            (
                {**per_unit, "watts": 1500},  # both forms on the wire
                without_map,  # neither form
                {**per_unit, "watts_by_unit": {"pod-a": 1200}},  # a selected unit has no target
                {  # a target names an unselected unit
                    **per_unit,
                    "watts_by_unit": {"pod-a": 1200, "pod-b": 300, "pod-c": 100},
                },
                {**per_unit, "watts_by_unit": {"pod-a": 0, "pod-b": 300}},
                {**per_unit, "watts_by_unit": {"pod-a": -1, "pod-b": 300}},
                {**per_unit, "watts_by_unit": {"pod-a": 1.5, "pod-b": 300}},
                {**per_unit, "watts_by_unit": {"pod-a": True, "pod-b": 300}},
                {**per_unit, "watts_by_unit": {" pod-a": 1200, "pod-b": 300}},
                {**per_unit, "watts_by_unit": {}},
                {**per_unit, "watts_by_unit": ["pod-a", "pod-b"]},
            )
        ):
            response = client.post(
                f"{API}/intents",
                json=invalid,
                headers=_mutation_headers("operator-token", key=f"per-unit-bad-{index}"),
            )
            _assert_error(response, 422, "validation_error")

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert scalar_accepted.status_code == 202
    forwarded = [values for name, values in service.calls if name == "submit_intent"]
    assert len(forwarded) == 2
    per_unit_call, scalar_call = forwarded
    assert per_unit_call["watts"] is None
    assert per_unit_call["watts_by_unit"] == {"pod-a": 1200, "pod-b": 300}
    assert per_unit_call["unit_ids"] == ["pod-a", "pod-b"]
    # The scalar form passes through exactly as before: no per-unit map.
    assert scalar_call["watts"] == 500
    assert scalar_call["watts_by_unit"] is None


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_numbers_are_rejected_before_the_service(
    nonfinite: str,
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    raw = f'{{"unit_ids":["pod-a"],"direction":"charge","watts":1200,"ttl_s":{nonfinite}}}'
    with _client(service, authenticator) as client:
        response = client.post(
            f"{API}/intents",
            content=raw,
            headers={
                **_mutation_headers("operator-token", key=f"nonfinite-{nonfinite}"),
                "Content-Type": "application/json",
            },
        )
    _assert_error(response, 422, "validation_error")
    assert all(name != "submit_intent" for name, _ in service.calls)


def test_mutations_require_idempotency_and_preserve_request_id(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    headers = _auth("operator-token", request_id="trace-client-7")
    with _client(service, authenticator) as client:
        missing_key = client.post(f"{API}/intents", json=payload, headers=headers)
        accepted = client.post(
            f"{API}/intents",
            json=payload,
            headers={**headers, "Idempotency-Key": "same-operation"},
        )
        replay = client.post(
            f"{API}/intents",
            json=payload,
            headers={**headers, "Idempotency-Key": "same-operation"},
        )

    _assert_error(missing_key, 400, "idempotency_key_required")
    assert accepted.status_code == replay.status_code == 202
    assert accepted.json() == replay.json()
    assert accepted.headers["X-Request-ID"] == "trace-client-7"
    submissions = [values for name, values in service.calls if name == "submit_intent"]
    assert len(submissions) == 1
    assert submissions[0]["idempotency_key"] == "same-operation"
    assert submissions[0]["request_id"] == "trace-client-7"


def test_reusing_idempotency_key_for_different_payload_conflicts(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    headers = _mutation_headers("operator-token", key="collision")
    first = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    second = {**first, "watts": 501}
    with _client(service, authenticator) as client:
        assert client.post(f"{API}/intents", json=first, headers=headers).status_code == 202
        conflict = client.post(f"{API}/intents", json=second, headers=headers)
    _assert_error(conflict, 409, "idempotency_conflict")
    assert len([name for name, _ in service.calls if name == "submit_intent"]) == 1


def test_idempotency_namespace_includes_authenticated_principal(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    with _client(service, authenticator) as client:
        operator = client.post(
            f"{API}/intents",
            json=payload,
            headers=_mutation_headers("operator-token", key="shared-key"),
        )
        second_operator = client.post(
            f"{API}/intents",
            json=payload,
            headers=_mutation_headers("second-operator-token", key="shared-key"),
        )
    assert operator.status_code == second_operator.status_code == 202
    submissions = [values for name, values in service.calls if name == "submit_intent"]
    assert [item["principal"].subject for item in submissions] == [
        "person:operator",
        "person:second-operator",
    ]


def test_concurrent_duplicate_idempotency_key_executes_service_once(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    app_module = load_contract_module("energypod.api.rest")
    app = app_module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
    )

    def submit() -> tuple[int, dict[str, Any]]:
        with TestClient(app) as client:
            response = client.post(
                f"{API}/intents",
                json=payload,
                headers=_mutation_headers("operator-token", key="concurrent-key"),
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))

    assert results[0] == results[1]
    assert results[0][0] == 202
    assert len([name for name, _ in service.calls if name == "submit_intent"]) == 1


def test_idempotency_replay_does_not_echo_the_second_request_id(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10}
    with _client(service, authenticator) as client:
        first = client.post(
            f"{API}/intents",
            json=payload,
            headers={
                **_mutation_headers("operator-token", key="replay-key"),
                "X-Request-ID": "first-request",
            },
        )
        replay = client.post(
            f"{API}/intents",
            json=payload,
            headers={
                **_mutation_headers("operator-token", key="replay-key"),
                "X-Request-ID": "second-request",
            },
        )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    # The transport correlation belongs to each HTTP exchange; the durable
    # operation correlation remains the first request ID inside the service call.
    assert replay.headers["X-Request-ID"] == "second-request"
    submission = next(values for name, values in service.calls if name == "submit_intent")
    assert submission["request_id"] == "first-request"


def test_arm_requires_interactive_operator_not_service_credential(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "confirmation": "ARM"}
    with _client(service, authenticator) as client:
        denied = client.post(
            f"{API}/arm",
            json=payload,
            headers=_mutation_headers("noninteractive-operator-token", key="arm-1"),
        )
        allowed = client.post(
            f"{API}/arm", json=payload, headers=_mutation_headers("operator-token", key="arm-2")
        )
    _assert_error(denied, 403, "interactive_operator_required")
    assert allowed.status_code == 200
    arm_call = next(values for name, values in service.calls if name == "arm")
    assert arm_call["principal"].interactive is True


def test_interactive_identity_does_not_substitute_for_required_scope(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "confirmation": "ARM"}
    with _client(service, authenticator) as client:
        denied = client.post(
            f"{API}/arm",
            json=payload,
            headers=_mutation_headers("viewer-token", key="arm-viewer"),
        )
    _assert_error(denied, 403, "insufficient_scope")
    assert all(name != "arm" for name, _ in service.calls)


def test_client_cannot_supply_identity_site_or_arming_lifecycle_metadata(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "confirmation": "ARM"}
    with _client(service, authenticator) as client:
        for injected in (
            {"principal": "person:admin"},
            {"subject": "person:admin"},
            {"site_id": "other-site"},
            {"interactive": True},
            {"lifecycle": "active"},
            {"generation": 999},
        ):
            response = client.post(
                f"{API}/arm",
                json={**payload, **injected},
                headers=_mutation_headers("operator-token", key=json.dumps(injected)),
            )
            _assert_error(response, 422, "validation_error")
    assert all(name != "arm" for name, _ in service.calls)


def test_disarm_requires_arm_scope_but_no_interactive_operator(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"]}
    with _client(service, authenticator) as client:
        denied = client.post(
            f"{API}/disarm",
            json=payload,
            headers=_mutation_headers("viewer-token", key="disarm-viewer"),
        )
        automation = client.post(
            f"{API}/disarm",
            json=payload,
            headers=_mutation_headers("noninteractive-operator-token", key="disarm-auto"),
        )
        human = client.post(
            f"{API}/disarm",
            json=payload,
            headers=_mutation_headers("operator-token", key="disarm-human"),
        )
    _assert_error(denied, 403, "insufficient_scope")
    # Disarming is safety-positive: the non-interactive automation credential
    # with the arm scope is accepted where arming would refuse it.
    assert automation.status_code == 200
    assert human.status_code == 200
    disarm_calls = [values for name, values in service.calls if name == "disarm"]
    assert [values["principal"].subject for values in disarm_calls] == [
        "service:operator-automation",
        "person:operator",
    ]
    assert disarm_calls[0]["principal"].interactive is False


def test_disarm_validates_units_like_arm_and_reports_per_unit_outcomes(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        accepted = client.post(
            f"{API}/disarm",
            json={"unit_ids": ["pod-a", "pod-b"]},
            headers=_mutation_headers("operator-token", key="disarm-multi"),
        )
        for invalid in (
            {"unit_ids": "pod-a"},
            {"unit_ids": []},
            {"unit_ids": ["pod-a", "pod-a"]},
            {"unit_ids": [" pod-a"]},
            {"unit_ids": ["-pod"]},
            {"unit_ids": ["pod-a"], "confirmation": "DISARM"},
            {"unit_ids": ["pod-a"], "unknown": "field"},
            {"unit_ids": ["pod-a"], "principal": "person:admin"},
        ):
            response = client.post(
                f"{API}/disarm",
                json=invalid,
                headers=_mutation_headers("operator-token", key=f"disarm-bad-{len(str(invalid))}"),
            )
            _assert_error(response, 422, "validation_error")

    assert accepted.status_code == 200
    assert accepted.json() == {
        "units": [
            {"unit_id": "pod-a", "status": "disarmed", "reason": "disarmed"},
            {"unit_id": "pod-b", "status": "disarmed", "reason": "disarmed"},
        ]
    }
    disarm_calls = [values for name, values in service.calls if name == "disarm"]
    assert len(disarm_calls) == 1
    assert disarm_calls[0]["unit_ids"] == ["pod-a", "pod-b"]
    assert disarm_calls[0]["idempotency_key"] == "disarm-multi"
    assert disarm_calls[0]["request_id"] == "req-123"


def test_disarm_is_idempotent_like_other_mutations(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        missing_key = client.post(
            f"{API}/disarm", json={"unit_ids": ["pod-a"]}, headers=_auth("operator-token")
        )
        first = client.post(
            f"{API}/disarm",
            json={"unit_ids": ["pod-a"]},
            headers=_mutation_headers("operator-token", key="disarm-replay"),
        )
        replay = client.post(
            f"{API}/disarm",
            json={"unit_ids": ["pod-a"]},
            headers=_mutation_headers("operator-token", key="disarm-replay"),
        )
        conflict = client.post(
            f"{API}/disarm",
            json={"unit_ids": ["pod-b"]},
            headers=_mutation_headers("operator-token", key="disarm-replay"),
        )

    _assert_error(missing_key, 400, "idempotency_key_required")
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    _assert_error(conflict, 409, "idempotency_conflict")
    assert len([name for name, _ in service.calls if name == "disarm"]) == 1


def test_emergency_stop_latches_and_acknowledges_exact_stop_id(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    headers = _mutation_headers("operator-token", key="stop-1")
    with _client(service, authenticator) as client:
        stopped = client.post(
            f"{API}/emergency-stop",
            json={"unit_ids": ["pod-a"], "reason": "operator requested"},
            headers=headers,
        )
        assert stopped.status_code == 202
        assert stopped.json() == {"stop_id": "stop-server-1", "status": "latched"}

        wrong = client.post(
            f"{API}/emergency-stop/stop-other/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="ack-wrong"),
        )
        exact = client.post(
            f"{API}/emergency-stop/stop-server-1/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="ack-exact"),
        )

    _assert_error(wrong, 409, "stop_id_mismatch")
    assert exact.status_code == 200
    ack = next(values for name, values in service.calls if name == "acknowledge_emergency_stop")
    assert ack["stop_id"] == "stop-server-1"
    assert ack["principal"].subject == "person:operator"


class _SequentialStopService(RecordingEnergyService):
    async def emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("emergency_stop", kwargs))
        stop_number = sum(1 for name, _ in self.calls if name == "emergency_stop")
        return {"stop_id": f"stop-{stop_number}", "status": "latched"}


class _DegradedStopService(RecordingEnergyService):
    """A stop whose safety work landed but whose completion is degraded.

    Impl-11: the facade raises the original store error with the uniform
    DegradedReport attached (stop id + degraded reason codes).
    """

    async def emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("emergency_stop", kwargs))
        error = OSError("intent store unavailable")
        error.stop_id = "stop-degraded-1"  # type: ignore[attr-defined]
        error.degraded_report = DegradedReport(  # type: ignore[attr-defined]
            stop_id="stop-degraded-1", degraded=("intent_store_unavailable",)
        )
        raise error


def test_a_degraded_stop_surfaces_its_stop_id_and_reason_codes(
    authenticator: FakeAuthenticator,
) -> None:
    """Impl-11: a stop that latched behind a degraded dependency answers 503
    ``emergency_stop_degraded`` carrying the stop id and the degraded codes in
    its details -- the stop stays acknowledgeable by that id, never a bare
    internal_error over landed safety work."""
    module = load_contract_module("energypod.api.rest")
    service = _DegradedStopService()
    app = module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
    )
    with TestClient(app) as client:
        degraded = client.post(
            f"{API}/emergency-stop",
            json={"unit_ids": ["pod-a"], "reason": "degraded store"},
            headers=_mutation_headers("operator-token", key="stop-degraded"),
        )
        details = _assert_error(degraded, 503, "emergency_stop_degraded")["details"]
        assert details["stop_id"] == "stop-degraded-1"
        assert details["degraded"] == ["intent_store_unavailable"]

        acknowledged = client.post(
            f"{API}/emergency-stop/stop-degraded-1/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="ack-degraded"),
        )

    assert acknowledged.status_code == 200
    ack = next(values for name, values in service.calls if name == "acknowledge_emergency_stop")
    assert ack["stop_id"] == "stop-degraded-1"


def test_latched_stops_stay_acknowledgeable_beyond_idempotency_capacity(
    authenticator: FakeAuthenticator,
) -> None:
    """Latched emergency stops are safety-critical state, not a bounded replay
    cache; accumulating more stops than the idempotency capacity must never
    strand an earlier stop so it can no longer be acknowledged."""
    module = load_contract_module("energypod.api.rest")
    service = _SequentialStopService()
    app = module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
        idempotency_capacity=2,
    )
    with TestClient(app) as client:
        stop_ids = []
        for index in range(1, 4):
            stopped = client.post(
                f"{API}/emergency-stop",
                json={"unit_ids": ["pod-a"], "reason": f"scenario {index}"},
                headers=_mutation_headers("operator-token", key=f"stop-key-{index}"),
            )
            assert stopped.status_code == 202
            stop_ids.append(stopped.json()["stop_id"])
        assert stop_ids == ["stop-1", "stop-2", "stop-3"]

        ack_first = client.post(
            f"{API}/emergency-stop/{stop_ids[0]}/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="ack-key-1"),
        )
    assert ack_first.status_code == 200, ack_first.json()


def test_inhibit_acknowledgement_requires_arm_scope_and_an_interactive_operator(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    path = f"{API}/units/pod-a/inhibit/acknowledge"
    with _client(service, authenticator) as client:
        acknowledged = client.post(
            path,
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="inhibit-1"),
        )
        viewer = client.post(
            path,
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("viewer-token", key="inhibit-2"),
        )
        automation = client.post(
            path,
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("noninteractive-operator-token", key="inhibit-3"),
        )
        unauthenticated = client.post(
            path,
            json={"confirmation": "ACKNOWLEDGE"},
            headers={"Idempotency-Key": "inhibit-4"},
        )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "acknowledged"
    call = next(values for name, values in service.calls if name == "acknowledge_inhibit")
    assert call["unit_id"] == "pod-a"
    assert call["principal"].subject == "person:operator"
    _assert_error(viewer, 403, "insufficient_scope")
    _assert_error(automation, 403, "interactive_operator_required")
    _assert_error(unauthenticated, 401, "authentication_required")
    assert [name for name, _ in service.calls].count("acknowledge_inhibit") == 1


def test_inhibit_acknowledgement_rejects_malformed_or_unknown_units(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        wrong_confirmation = client.post(
            f"{API}/units/pod-a/inhibit/acknowledge",
            json={"confirmation": "STOP"},
            headers=_mutation_headers("operator-token", key="inhibit-5"),
        )
        malformed_id = client.post(
            f"{API}/units/-pod/inhibit/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="inhibit-6"),
        )
        unknown_unit = client.post(
            f"{API}/units/pod-ghost/inhibit/acknowledge",
            json={"confirmation": "ACKNOWLEDGE"},
            headers=_mutation_headers("operator-token", key="inhibit-7"),
        )
    _assert_error(wrong_confirmation, 422, "validation_error")
    _assert_error(malformed_id, 422, "validation_error")
    _assert_error(unknown_unit, 404, "unit_not_found")
    # Only the unknown-unit probe reaches the service (and is refused there);
    # validation failures never reach it.
    assert [name for name, _ in service.calls].count("acknowledge_inhibit") == 1


def test_emergency_stop_requires_auth_but_not_an_arming_confirmation(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"unit_ids": ["pod-a"], "reason": "visible hazard"}
    with _client(service, authenticator) as client:
        unauthenticated = client.post(
            f"{API}/emergency-stop",
            json=payload,
            headers={"Idempotency-Key": "stop-no-auth"},
        )
        stopped = client.post(
            f"{API}/emergency-stop",
            json=payload,
            headers=_mutation_headers("operator-token", key="stop-fast"),
        )
    _assert_error(unauthenticated, 401, "authentication_required")
    assert stopped.status_code == 202
    stop_call = next(values for name, values in service.calls if name == "emergency_stop")
    assert "confirmation" not in stop_call


def test_public_route_and_openapi_surfaces_exclude_maintenance_and_debug(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    forbidden = ("debug", "maintenance", "register", "modbus", "clear-energy", "calibration")
    with _client(service, authenticator) as client:
        schema = client.get("/openapi.json").json()
        paths = tuple(schema["paths"])
        for candidate in (
            f"{API}/debug",
            f"{API}/maintenance",
            f"{API}/registers/512",
            f"{API}/clear-energy",
        ):
            assert (
                client.post(
                    candidate, json={}, headers=_mutation_headers("operator-token", key=candidate)
                ).status_code
                == 404
            )

    lowered = " ".join(paths).lower()
    assert all(word not in lowered for word in forbidden)
    # /healthz is the one contracted non-API public surface: unauthenticated
    # liveness for container orchestration (API_CONTRACTS "Operations
    # surface").  Everything else stays behind the versioned, guarded prefix.
    non_api = {path for path in paths if not path.startswith(API)}
    assert non_api <= {"/openapi.json", "/healthz"}
    assert "/healthz" in paths


def test_events_session_requires_bearer_and_observe_scope(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        unauthenticated = client.post(f"{API}/events/session")
        audit_only = client.post(f"{API}/events/session", headers=_auth("audit-only-token"))
        viewer = client.post(f"{API}/events/session", headers=_auth("viewer-token"))
    _assert_error(unauthenticated, 401, "authentication_required")
    _assert_error(audit_only, 403, "insufficient_scope")
    assert viewer.status_code == 200
    assert service.calls == []


def test_events_session_issues_opaque_short_lived_tickets(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    clock = MutableMonotonicClock(500.0)
    with _client(
        service, authenticator, event_ticket_ttl_s=5.0, event_ticket_clock=clock
    ) as client:
        first = client.post(f"{API}/events/session", headers=_auth("viewer-token")).json()
        second = client.post(f"{API}/events/session", headers=_auth("operator-token")).json()

    for body in (first, second):
        assert set(body) == {"ticket", "expires_in_s"}
        assert body["expires_in_s"] == 5.0
        ticket = body["ticket"]
        assert isinstance(ticket, str) and len(ticket) >= 32
        assert ticket.strip() == ticket and " " not in ticket
        # Opaque: the ticket never echoes the credential or the principal.
        assert "viewer-token" not in ticket
        assert "operator-token" not in ticket
        assert "person:" not in ticket
    assert first["ticket"] != second["ticket"]


def test_events_session_default_ttl_is_bounded_and_the_knob_is_validated(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    module = load_contract_module("energypod.api.rest")
    with _client(service, authenticator) as client:
        body = client.post(f"{API}/events/session", headers=_auth("viewer-token")).json()
    assert 0 < body["expires_in_s"] <= 30

    # The contracted ceiling itself is accepted; anything beyond it, or a
    # non-positive / non-finite / boolean value, is refused at construction.
    module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
        event_ticket_ttl_s=30.0,
    )
    for invalid in (30.5, 31.0, 0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="event_ticket_ttl_s"):
            module.create_api_app(
                service=service,
                authenticator=authenticator,
                event_source=FakeEventSource(),
                auth_required=True,
                event_ticket_ttl_s=invalid,
            )


def test_event_ticket_grants_nothing_but_the_event_stream(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    clock = MutableMonotonicClock()
    with _client(
        service, authenticator, event_ticket_ttl_s=15.0, event_ticket_clock=clock
    ) as client:
        ticket = client.post(f"{API}/events/session", headers=_auth("operator-token")).json()[
            "ticket"
        ]
        bearer = {"Authorization": f"Bearer {ticket}"}
        snapshot = client.get(f"{API}/snapshot", headers=bearer)
        disarm = client.post(
            f"{API}/disarm",
            json={"unit_ids": ["pod-a"]},
            headers={**bearer, "Idempotency-Key": "ticket-as-bearer"},
        )
        session = client.post(f"{API}/events/session", headers=bearer)

    # The ticket is not a bearer credential: it authorizes only the WebSocket
    # handshake, never a REST read, mutation, or further ticket issuance.
    _assert_error(snapshot, 401, "authentication_required")
    _assert_error(disarm, 401, "authentication_required")
    _assert_error(session, 401, "authentication_required")
    assert service.calls == []
    assert authenticator.presented_tokens == ["operator-token", ticket, ticket, ticket]


# --- Operations surface (Milestone C): GET /healthz -------------------------
#
# API_CONTRACTS: "/healthz is the only unauthenticated endpoint: liveness only
# (process up), never readiness, never data.  It exists for container
# orchestration; /api/v1/health remains the authenticated three-fact health
# view."


def test_healthz_answers_unauthenticated_liveness_only(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok"}, "liveness exposes exactly the ok fact, never readiness or data"
    assert body["ok"] is True
    # Nothing was consulted on the way to the answer: no credential was
    # validated and no service read happened.
    assert authenticator.presented_tokens == []
    assert service.calls == []


@pytest.mark.parametrize(
    "authorization",
    ["Bearer unknown-token", "Bearer ", "Basic dXNlcjpwYXNz", "not-a-scheme"],
)
def test_healthz_ignores_offered_credentials_instead_of_validating_them(
    authorization: str,
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    """A liveness probe never consults the credential store: an orchestrator
    that forwards (or mangles) an Authorization header still learns process-up
    without granting itself anything."""
    with _client(service, authenticator) as client:
        response = client.get("/healthz", headers={"Authorization": authorization})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert authenticator.presented_tokens == []
    assert service.calls == []


def test_healthz_is_a_get_only_deterministic_probe(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        first = client.get("/healthz")
        second = client.get("/healthz")
        post = client.post("/healthz")
        api_health = client.get(f"{API}/health")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True}
    assert post.status_code == 405
    # The authenticated three-fact health view stays a separate guarded route.
    _assert_error(api_health, 401, "authentication_required")
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/snapshot", None),
        ("GET", "/health", None),
        ("GET", "/audit", None),
        ("GET", "/units/MID", None),
        (
            "POST",
            "/intents",
            {"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 10},
        ),
        ("POST", "/arm", {"unit_ids": ["pod-a"], "confirmation": "ARM"}),
        ("POST", "/disarm", {"unit_ids": ["pod-a"]}),
        ("POST", "/emergency-stop", {"unit_ids": ["pod-a"], "reason": "probe"}),
        ("POST", "/emergency-stop/stop-1/acknowledge", {"confirmation": "ACKNOWLEDGE"}),
        ("POST", "/units/pod-a/inhibit/acknowledge", {"confirmation": "ACKNOWLEDGE"}),
        ("POST", "/events/session", None),
    ],
)
def test_every_route_except_healthz_still_requires_bearer_authentication(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    """Adding the one unauthenticated liveness endpoint weakens nothing else:
    every operational route refuses an unauthenticated request exactly as
    before, and none of them reach the service."""
    with _client(service, authenticator) as client:
        response = client.request(method, API + path, json=payload)
    _assert_error(response, 401, "authentication_required")
    assert service.calls == []


# --- the guarded excess-charging toggle (DESIGN_EXCESS_ACTIVATION §3) ------------
#
# POST /api/v1/excess-charging: the inhibit-acknowledgement
# guarded-confirmation pattern applied to a feature gate.  The boundary owns
# the 422 validation envelope (unknown action, missing/incorrect
# confirmation, an economics field that is not exactly "NET_BILLED"), the
# 403 scope/interactivity rules (P4: arm + interactive to enable, arm alone
# to disable), and the 409 mapping of the facade's three refusal shapes.


class _RefusingExcessService(RecordingEnergyService):
    """Raises one ExcessChargingRefusal shaped by the test."""

    def __init__(self, refusal: object) -> None:
        super().__init__()
        self.refusal = refusal

    async def set_excess_charging(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("set_excess_charging", kwargs))
        raise self.refusal  # type: ignore[misc]


def test_excess_charging_toggle_answers_the_200_contract_body(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"action": "enable", "confirmation": "EXCESS", "economics": "NET_BILLED"}
    with _client(service, authenticator) as client:
        missing_key = client.post(
            f"{API}/excess-charging", json=payload, headers=_auth("operator-token")
        )
        accepted = client.post(
            f"{API}/excess-charging",
            json=payload,
            headers=_mutation_headers("operator-token", key="excess-enable-1"),
        )
        replay = client.post(
            f"{API}/excess-charging",
            json=payload,
            headers=_mutation_headers("operator-token", key="excess-enable-1"),
        )
        disable = client.post(
            f"{API}/excess-charging",
            json={"action": "disable", "confirmation": "EXCESS"},
            headers=_mutation_headers("operator-token", key="excess-disable-1"),
        )

    _assert_error(missing_key, 400, "idempotency_key_required")
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["feature"] == "excess_charging"
    assert body["enabled"] is True
    assert body["enabled_origin"] == "runtime"
    assert body["persisted"] is False, "P1 rides every response"
    assert body["acknowledged_economics"] is True
    assert body["adviser_state"]["enabled"] is True
    assert replay.status_code == 200 and replay.json() == body
    assert disable.status_code == 200 and disable.json()["enabled"] is False
    calls = [values for name, values in service.calls if name == "set_excess_charging"]
    assert len(calls) == 2, "one call per distinct key; the replay never re-reaches the service"
    assert calls[0]["action"] == "enable"
    assert calls[0]["confirmation"] == "EXCESS"
    assert calls[0]["economics"] == "NET_BILLED"
    assert calls[0]["principal"].interactive is True
    assert calls[1]["economics"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "pause", "confirmation": "EXCESS"},
        {"action": "enable"},
        {"action": "enable", "confirmation": "ARM"},
        {"action": "enable", "confirmation": "EXCESS", "economics": "GROSS_BILLED"},
        {"action": "disable", "confirmation": "excess"},
    ],
)
def test_excess_charging_toggle_validates_its_literals(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
    payload: dict[str, object],
) -> None:
    with _client(service, authenticator) as client:
        response = client.post(
            f"{API}/excess-charging",
            json=payload,
            headers=_mutation_headers("operator-token", key="excess-invalid"),
        )

    _assert_error(response, 422, "validation_error")
    details = response.json()["details"]
    assert details["errors"], "field errors ride the validation envelope"
    assert not [values for name, values in service.calls if name == "set_excess_charging"]


def test_excess_charging_toggle_requires_the_arm_scope(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    payload = {"action": "disable", "confirmation": "EXCESS"}
    with _client(service, authenticator) as client:
        viewer = client.post(
            f"{API}/excess-charging",
            json=payload,
            headers=_mutation_headers("viewer-token", key="v1"),
        )
        dispatcher = client.post(
            f"{API}/excess-charging",
            json=payload,
            headers=_mutation_headers("service-token", key="s1"),
        )
    _assert_error(viewer, 403, "insufficient_scope")
    _assert_error(dispatcher, 403, "insufficient_scope")


def test_excess_charging_enable_demands_an_interactive_principal(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """P4: disable is safety-positive and stays open to arm-scoped automation;
    enabling grants participation and needs the human."""
    enable = {"action": "enable", "confirmation": "EXCESS", "economics": "NET_BILLED"}
    disable = {"action": "disable", "confirmation": "EXCESS"}
    with _client(service, authenticator) as client:
        denied = client.post(
            f"{API}/excess-charging",
            json=enable,
            headers=_mutation_headers("noninteractive-operator-token", key="ni1"),
        )
        allowed = client.post(
            f"{API}/excess-charging",
            json=disable,
            headers=_mutation_headers("noninteractive-operator-token", key="ni2"),
        )
    _assert_error(denied, 403, "interactive_operator_required")
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    ("refusal", "expected_details"),
    [
        (
            ExcessChargingRefusal(
                "excess_charging_not_commissioned",
                "the excess_charging feature is not composed on this site",
            ),
            {},
        ),
        (
            ExcessChargingRefusal(
                "economics_acknowledgement_required",
                "the one-time net-billing acknowledgement is required",
                {"acknowledgement": "NET_BILLED"},
            ),
            {"acknowledgement": "NET_BILLED"},
        ),
        (
            ExcessChargingRefusal(
                "excess_enable_refused",
                "enabling requires a quiet fleet",
                {
                    "reasons": ["unit_active_under_intent", "latched_stop_holds"],
                    "unit_ids": ["pod-a"],
                    "stop_ids": ["stop-1-1"],
                },
            ),
            {
                "reasons": ["unit_active_under_intent", "latched_stop_holds"],
                "unit_ids": ["pod-a"],
                "stop_ids": ["stop-1-1"],
            },
        ),
    ],
)
def test_excess_charging_refusals_map_to_their_structured_envelopes(
    authenticator: FakeAuthenticator, refusal: ExcessChargingRefusal, expected_details: dict
) -> None:
    service = _RefusingExcessService(refusal)
    with _client(service, authenticator) as client:
        response = client.post(
            f"{API}/excess-charging",
            json={"action": "enable", "confirmation": "EXCESS"},
            headers=_mutation_headers("operator-token", key="refuse-1"),
        )

    _assert_error(response, 409, refusal.code)
    assert response.json()["details"] == expected_details


# --- DESIGN_SCHEDULES §5 B4: the schedule REST surface --------------------------

from energypod.application.scheduling import (  # noqa: E402
    SchedulePublishValidationError,
    ScheduleRefusal,
)


def _wire_entry(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "entry_id": "day-charge",
        "days": ["mon"],
        "start_local": "09:00",
        "end_local": "17:00",
        "action": "charge",
        "watts": 1200,
        "unit_ids": ["pod-a"],
        "effective_from": "2026-01-01",
        "effective_until": "2026-12-31",
        "priority": 0,
        "enabled": True,
    }
    values.update(overrides)
    return values


def _put_body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "expected_version": None,
        "timezone": "Australia/Brisbane",
        "entries": [_wire_entry()],
    }
    values.update(overrides)
    return values


def test_schedule_get_requires_authentication_and_serves_the_view(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        anonymous = client.get(f"{API}/schedule")
        refused = client.get(f"{API}/schedule", headers=_auth("viewer-token"))

    _assert_error(anonymous, 401, "authentication_required")
    assert refused.status_code == 200
    assert refused.json() == service.schedule_view
    forwarded = [values for name, values in service.calls if name == "get_schedule"]
    assert forwarded[0]["principal"].subject == "person:viewer"


def test_schedule_get_maps_the_not_commissioned_refusal_verbatim(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    service.schedule_refusal = ScheduleRefusal(
        "schedule_not_commissioned", "the schedule feature is not composed on this site"
    )
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/schedule", headers=_auth("viewer-token"))

    _assert_error(response, 409, "schedule_not_commissioned")


def test_schedule_put_publishes_the_whole_plan_with_an_idempotency_key(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        missing_key = client.put(
            f"{API}/schedule", json=_put_body(), headers=_auth("operator-token")
        )
        accepted = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("operator-token", key="publish-1"),
        )
        replay = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("operator-token", key="publish-1"),
        )

    _assert_error(missing_key, 400, "idempotency_key_required")
    assert accepted.status_code == 200
    assert accepted.json() == service.schedule_result
    assert replay.status_code == 200
    assert replay.json() == accepted.json()
    forwarded = [values for name, values in service.calls if name == "replace_schedule"]
    assert len(forwarded) == 1, "a replay never re-reaches the service"
    call = forwarded[0]
    assert call["expected_version"] is None
    assert call["timezone"] == "Australia/Brisbane"
    assert call["entries"][0]["entry_id"] == "day-charge"
    assert call["night_posture"] is None
    assert call["principal"].subject == "person:operator"


def test_schedule_put_reusing_a_key_with_another_body_is_a_conflict(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        first = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("operator-token", key="publish-1"),
        )
        conflict = client.put(
            f"{API}/schedule",
            json=_put_body(timezone="Australia/Perth"),
            headers=_mutation_headers("operator-token", key="publish-1"),
        )

    assert first.status_code == 200
    _assert_error(conflict, 409, "idempotency_conflict")


def test_schedule_put_requires_dispatch_scope_and_an_interactive_principal(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        viewer = client.put(
            f"{API}/schedule", json=_put_body(), headers=_mutation_headers("viewer-token")
        )
        automation = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("noninteractive-operator-token"),
        )

    _assert_error(viewer, 403, "insufficient_scope")
    _assert_error(automation, 403, "interactive_operator_required")
    assert all(name != "replace_schedule" for name, _ in service.calls)


@pytest.mark.parametrize(
    ("refusal", "details"),
    [
        (
            ScheduleRefusal(
                "schedule_window_not_allowed",
                "entries fall outside the allowed windows (day-only posture)",
                {
                    "posture": "yield",
                    "allowed_windows_local": [["06:00", "20:00"]],
                    "offending": [
                        {"entry_id": "Night Charge", "start_local": "00:01", "end_local": "05:59"}
                    ],
                },
            ),
            {
                "posture": "yield",
                "allowed_windows_local": [["06:00", "20:00"]],
                "offending": [
                    {"entry_id": "Night Charge", "start_local": "00:01", "end_local": "05:59"}
                ],
            },
        ),
        (
            ScheduleRefusal(
                "night_posture_acknowledgement_required",
                "the first night schedule publish requires the one-time partition acknowledgement",
                {"acknowledgement": "PARTITION_ACKNOWLEDGED"},
            ),
            {"acknowledgement": "PARTITION_ACKNOWLEDGED"},
        ),
        (
            ScheduleRefusal(
                "schedule_version_conflict",
                "the plan changed elsewhere — reload and re-apply",
                {"current_version": 3},
            ),
            {"current_version": 3},
        ),
    ],
)
def test_schedule_put_maps_every_refusal_shape_verbatim(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
    refusal: ScheduleRefusal,
    details: dict[str, Any],
) -> None:
    service.schedule_refusal = refusal
    with _client(service, authenticator) as client:
        response = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("operator-token", key=f"refuse-{refusal.code}"),
        )

    _assert_error(response, 409, refusal.code)
    assert response.json()["details"] == details


def test_schedule_put_maps_facade_validation_errors_with_entry_names(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    service.schedule_error = SchedulePublishValidationError(
        [{"entry_id": "day-charge", "message": "idle entries require zero watts"}]
    )
    with _client(service, authenticator) as client:
        response = client.put(
            f"{API}/schedule",
            json=_put_body(),
            headers=_mutation_headers("operator-token"),
        )

    body = _assert_error(response, 422, "validation_error")
    assert body["details"]["errors"] == [
        {"entry_id": "day-charge", "message": "idle entries require zero watts"}
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        {"days": ["monday"]},
        {"days": []},
        {"action": "hold"},
        {"watts": -100},
        {"watts": True},
        {"priority": "high"},
        {"enabled": "yes"},
        {"unit_ids": []},
        {"effective_from": "2026-1-1"},
        {"night_posture": "SILENT"},
        {"expected_version": -1},
        {"unknown": "field"},
    ],
)
def test_schedule_put_rejects_malformed_wire_shapes_before_the_service(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
    mutation: dict[str, Any],
) -> None:
    body = _put_body()
    if mutation.keys() & {
        "days",
        "action",
        "watts",
        "priority",
        "enabled",
        "unit_ids",
        "effective_from",
    }:
        body["entries"] = [
            _wire_entry(
                **{
                    k: v
                    for k, v in mutation.items()
                    if k != "night_posture" and k != "expected_version" and k != "unknown"
                }
            )
        ]
        mutation = {
            k: v
            for k, v in mutation.items()
            if k
            not in {"days", "action", "watts", "priority", "enabled", "unit_ids", "effective_from"}
        }
    if mutation:
        body.update(mutation)
    with _client(service, authenticator) as client:
        response = client.put(
            f"{API}/schedule",
            json=body,
            headers=_mutation_headers("operator-token", key=f"bad-{abs(hash(str(mutation)))}"),
        )

    _assert_error(response, 422, "validation_error")
    assert all(name != "replace_schedule" for name, _ in service.calls)


def test_schedule_put_accepts_an_empty_entries_list_as_off(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.put(
            f"{API}/schedule",
            json=_put_body(entries=[]),
            headers=_mutation_headers("operator-token"),
        )

    assert response.status_code == 200
    forwarded = [values for name, values in service.calls if name == "replace_schedule"]
    assert forwarded[0]["entries"] == []


def test_schedule_put_accepts_the_optional_effective_dates_as_absent_or_null(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    # DESIGN_SCHEDULES §1: the effective date range is OPTIONAL. The editor
    # omits both fields when the operator left them empty (and sends null on a
    # cleared field) — the strict wire schema must pass that through to the
    # facade, which resolves the open bounds. This is the exact shape the
    # 2026-08-23 console publish was refused for.
    absent = {
        key: value
        for key, value in _wire_entry().items()
        if key not in ("effective_from", "effective_until")
    }
    with _client(service, authenticator) as client:
        first = client.put(
            f"{API}/schedule",
            json=_put_body(entries=[absent]),
            headers=_mutation_headers("operator-token", key="publish-open-1"),
        )
        nulled = client.put(
            f"{API}/schedule",
            json=_put_body(entries=[_wire_entry(effective_from=None)]),
            headers=_mutation_headers("operator-token", key="publish-open-2"),
        )

    assert first.status_code == 200
    assert nulled.status_code == 200
    forwarded = [values for name, values in service.calls if name == "replace_schedule"]
    assert forwarded[0]["entries"][0]["effective_from"] is None
    assert forwarded[0]["entries"][0]["effective_until"] is None
    assert forwarded[1]["entries"][0]["effective_from"] is None
    assert forwarded[1]["entries"][0]["effective_until"] == "2026-12-31"
