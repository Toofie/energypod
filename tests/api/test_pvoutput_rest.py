"""The pvoutput.org reporter's guarded REST contract.

The night-charging toggle's exact boundary shape with this feature's own
literals (API_CONTRACTS' guarded-toggle pattern):

- ``GET /api/v1/pvoutput/status`` (observe scope): the uploader's health
  snapshot, or the structured 409 ``pvoutput_not_commissioned`` on a
  deployment without the ``pvoutput`` config block (the block-presence
  refusal code).
- ``POST /api/v1/pvoutput`` (arm scope, Idempotency-Key required): the
  guarded activation toggle.  Enabling additionally demands an INTERACTIVE
  principal (it starts an external write); disabling is safety-positive and
  stays open to any arm-scoped principal.  The body carries
  ``{action, confirmation: "PVOUTPUT"}``; a missing/invalid key refuses with
  400 ``idempotency_key_required`` exactly like every other mutation; the
  service's typed refusals map onto their 409 envelopes verbatim.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from energypod.application.pvoutput_upload import PvOutputRefusal

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
    app = module.create_api_app(
        service=service, authenticator=authenticator, event_source=FakeEventSource()
    )
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mutation_headers(token: str, *, key: str = "idem-1") -> dict[str, str]:
    return {**_auth(token), "Idempotency-Key": key}


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    return body


class TestStatusRead:
    def test_the_health_snapshot_answers_for_the_observe_scope(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        service.pvoutput_status_view = {
            **service.pvoutput_status_view,
            "enabled": True,
            "enabled_origin": "runtime",
            "last_success_at": "2026-08-25T04:05:00+00:00",
            "last_post_age_s": 42.0,
            "last_posted_slot": "2026-08-25 14:05",
            "rate_remaining": 57,
            "slots_skipped_stale": 2,
        }
        with _client(service, authenticator) as client:
            response = client.get(f"{API}/pvoutput/status", headers=_auth("viewer-token"))
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["enabled_origin"] == "runtime"
        assert body["last_posted_slot"] == "2026-08-25 14:05"
        assert body["rate_remaining"] == 57
        assert body["slots_skipped_stale"] == 2

    def test_a_site_without_the_block_refuses_with_its_own_code(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        service.pvoutput_refusal = PvOutputRefusal(
            "pvoutput_not_commissioned",
            "the pvoutput feature is not composed on this site",
        )
        with _client(service, authenticator) as client:
            response = client.get(f"{API}/pvoutput/status", headers=_auth("viewer-token"))
        body = _assert_error(response, 409, "pvoutput_not_commissioned")
        assert "not composed" in body["message"]

    def test_authentication_is_required(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.get(f"{API}/pvoutput/status")
        _assert_error(response, 401, "authentication_required")


class TestToggleGuards:
    def test_the_toggle_requires_the_arm_scope(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "PVOUTPUT"},
                headers=_mutation_headers("viewer-token"),
            )
        _assert_error(response, 403, "insufficient_scope")

    def test_the_toggle_requires_an_idempotency_key(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "PVOUTPUT"},
                headers=_auth("operator-token"),
            )
        _assert_error(response, 400, "idempotency_key_required")
        assert not [True for name, _call in service.calls if name == "set_pvoutput"]

    def test_enabling_demands_an_interactive_principal(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "PVOUTPUT"},
                headers=_mutation_headers("noninteractive-operator-token"),
            )
        _assert_error(response, 403, "interactive_operator_required")

    def test_disabling_stays_open_to_any_arm_scoped_principal(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "disable", "confirmation": "PVOUTPUT"},
                headers=_mutation_headers("noninteractive-operator-token"),
            )
        assert response.status_code == 200
        assert response.json()["feature"] == "pvoutput"

    def test_the_typed_confirmation_is_validated(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "NIGHT"},
                headers=_mutation_headers("operator-token"),
            )
        _assert_error(response, 422, "validation_error")
        assert not [True for name, _call in service.calls if name == "set_pvoutput"]


class TestToggleFlow:
    def test_the_enable_200_carries_the_post_toggle_state_and_the_persisted_fact(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "PVOUTPUT"},
                headers=_mutation_headers("operator-token", key="idem-enable"),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["feature"] == "pvoutput"
        assert body["enabled"] is True
        # The night twin answers False (participation resets at boot); this
        # toggle is a durable fact and says so on the wire.
        assert body["persisted"] is True
        assert body["pvoutput_state"]["enabled_origin"] == "runtime"
        calls = [kwargs for name, kwargs in service.calls if name == "set_pvoutput"]
        assert calls == [
            {
                "action": "enable",
                "confirmation": "PVOUTPUT",
                "principal": calls[0]["principal"],
                "idempotency_key": "idem-enable",
                "request_id": calls[0]["request_id"],
            }
        ]

    def test_the_service_refusals_map_to_their_envelopes(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        service.pvoutput_refusal = PvOutputRefusal(
            "pvoutput_toggle_failed",
            "the durable pvoutput toggle could not be stored",
        )
        with _client(service, authenticator) as client:
            response = client.post(
                f"{API}/pvoutput",
                json={"action": "disable", "confirmation": "PVOUTPUT"},
                headers=_mutation_headers("operator-token"),
            )
        _assert_error(response, 409, "pvoutput_toggle_failed")

    def test_an_idempotent_replay_returns_the_stored_result(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        headers = _mutation_headers("operator-token", key="replay-once")
        with _client(service, authenticator) as client:
            body = {"action": "enable", "confirmation": "PVOUTPUT"}
            first = client.post(f"{API}/pvoutput", json=body, headers=headers)
            second = client.post(f"{API}/pvoutput", json=body, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert len([1 for name, _ in service.calls if name == "set_pvoutput"]) == 1

    def test_the_same_key_on_a_different_body_is_a_conflict(
        self, service: RecordingEnergyService, authenticator: FakeAuthenticator
    ) -> None:
        headers = _mutation_headers("operator-token", key="replay-conflict")
        with _client(service, authenticator) as client:
            first = client.post(
                f"{API}/pvoutput",
                json={"action": "enable", "confirmation": "PVOUTPUT"},
                headers=headers,
            )
            second = client.post(
                f"{API}/pvoutput",
                json={"action": "disable", "confirmation": "PVOUTPUT"},
                headers=headers,
            )
        assert first.status_code == 200
        _assert_error(second, 409, "idempotency_conflict")
