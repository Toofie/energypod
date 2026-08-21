"""Red-phase HTTP contracts for the guarded ``/api/v1`` boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import (
    FakeAuthenticator,
    FakeEventSource,
    RecordingEnergyService,
    load_contract_module,
)

API = "/api/v1"


def _client(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> TestClient:
    module = load_contract_module("energypod.api.rest")
    assert hasattr(module, "create_api_app"), "energypod.api.rest.create_api_app is required"
    app = module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
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


def test_auditor_read_is_bounded_and_reaches_the_service(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.get(f"{API}/audit?limit=25", headers=_auth("auditor-token"))
    assert response.status_code == 200
    assert response.json()["events"][0]["sequence"] == 7
    assert service.calls[-1][0] == "recent_audit"
    assert service.calls[-1][1]["limit"] == 25


def test_audit_read_scope_alone_is_insufficient_without_observe(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        audit = client.get(f"{API}/audit", headers=_auth("audit-only-token"))
        snapshot = client.get(f"{API}/snapshot", headers=_auth("audit-only-token"))
    _assert_error(audit, 403, "insufficient_scope")
    _assert_error(snapshot, 403, "insufficient_scope")
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
    assert all(path == "/openapi.json" or path.startswith(API) for path in paths)
