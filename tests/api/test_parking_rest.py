"""The pod-parking REST boundary contracts (DESIGN_POD_PARKING section 2).

T-PARK-COMMISSIONING's REST half plus the boundary mapping of every typed
refusal: the three operator-only routes (arm scope AND an interactive
principal, both directions), the Idempotency-Key discipline through the
shared ``mutation()`` wrapper, the typed 409 envelopes with EXACTLY the
pinned details shapes, the 422 lease/reason validation, and the fixed
not-isolation sentence on the operator-facing surface (the OpenAPI summary
is the documented home of the sentence -- checked here so it never drifts).

The service is the recording fake: these tests pin the BOUNDARY, not the
controller (tests/unit/test_parking_controller.py owns the doctrine).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from energypod.application.parking import ParkingRefusal

from .conftest import (
    FakeAuthenticator,
    FakeEventSource,
    RecordingEnergyService,
    load_contract_module,
)

API = "/api/v1"
PARK_PATH = f"{API}/units/pod-a/park"
RENEW_PATH = f"{API}/units/pod-a/park/renew"
RESUME_PATH = f"{API}/units/pod-a/resume"


def _client(
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> TestClient:
    module = load_contract_module("energypod.api.rest")
    app = module.create_api_app(
        service=service,
        authenticator=authenticator,
        event_source=FakeEventSource(),
        auth_required=True,
    )
    return TestClient(app)


def _mutation_headers(token: str, *, key: str = "park-key-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
        "X-Request-ID": "park-request-1",
    }


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    return body


PARK_BODY = {"confirmation": "PARK", "reason": "inverter work", "lease_s": 7200}


# --- the guarded surface ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (PARK_PATH, PARK_BODY),
        (RENEW_PATH, {"confirmation": "RENEW", "lease_s": 3600}),
        (RESUME_PATH, {"confirmation": "RESUME"}),
   ],
)
def test_the_parking_routes_require_the_arm_scope(
    path: str,
    body: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    """The viewer (observe only) never reaches the service."""
    with _client(service, authenticator) as client:
        _assert_error(
            client.post(path, json=body, headers=_mutation_headers("viewer-token")),
            403,
            "insufficient_scope",
        )
    assert service.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (PARK_PATH, PARK_BODY),
        (RENEW_PATH, {"confirmation": "RENEW", "lease_s": 3600}),
        (RESUME_PATH, {"confirmation": "RESUME"}),
    ],
)
def test_the_parking_routes_require_an_interactive_operator_both_directions(
    path: str,
    body: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    """Flag 10: park AND resume are interactive human acts -- an arm-scoped
    automation principal is refused before the service is touched."""
    with _client(service, authenticator) as client:
        _assert_error(
            client.post(
                path, json=body, headers=_mutation_headers("noninteractive-operator-token")
            ),
            403,
            "interactive_operator_required",
        )
    assert service.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (PARK_PATH, PARK_BODY),
        (RENEW_PATH, {"confirmation": "RENEW", "lease_s": 3600}),
        (RESUME_PATH, {"confirmation": "RESUME"}),
    ],
)
def test_the_parking_routes_are_mutations_with_idempotency_keys(
    path: str,
    body: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    with _client(service, authenticator) as client:
        headers = _mutation_headers("operator-token")
        del headers["Idempotency-Key"]
        _assert_error(
            client.post(path, json=body, headers=headers), 400, "idempotency_key_required"
        )
    assert service.calls == []


def test_park_happy_path_is_synchronous_and_shaped(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        response = client.post(
            PARK_PATH, json=PARK_BODY, headers=_mutation_headers("operator-token")
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "unit_id",
        "action",
        "prior_word",
        "written_value",
        "readback_word",
        "verified",
        "as_of",
        "lease",
        "prior_state",
    }
    assert body["written_value"] == 1 and body["verified"] is True
    assert set(body["lease"]) == {
        "parked_at",
        "expires_at",
        "max_total_s",
        "reason",
        "authorizer",
        "epoch",
    }
    # The facade received the guarded fields verbatim, correlation included.
    (name, kwargs), *_ = service.calls
    assert name == "park_unit"
    assert kwargs["confirmation"] == "PARK"
    assert kwargs["reason"] == "inverter work"
    assert kwargs["lease_s"] == 7200
    assert kwargs["idempotency_key"] == "park-key-1"
    assert kwargs["principal"].subject == "person:operator"


def test_an_idempotent_replay_returns_the_stored_answer_without_re_parking(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """The shared ``mutation()`` wrapper: the same key and body replay the
    ORIGINAL operation's answer (the stale-answer honesty), the service is
    reached exactly once, and a DIFFERENT body under the same key refuses."""
    with _client(service, authenticator) as client:
        first = client.post(
            PARK_PATH, json=PARK_BODY, headers=_mutation_headers("operator-token")
        )
        service.park_result["as_of"] = "2026-08-24T09:00:00+00:00"
        replay = client.post(
            PARK_PATH, json=PARK_BODY, headers=_mutation_headers("operator-token")
        )
        conflict = client.post(
            PARK_PATH,
            json={**PARK_BODY, "lease_s": 3600},
            headers=_mutation_headers("operator-token"),
        )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["as_of"] == first.json()["as_of"], "the replay describes the ORIGINAL act"
    _assert_error(conflict, 409, "idempotency_conflict")
    assert len(service.calls) == 1


# --- typed refusals map verbatim --------------------------------------------------


@pytest.mark.parametrize(
    ("code", "details"),
    [
        ("park_not_commissioned", {"cause": "block_absent"}),
        ("park_not_commissioned", {"cause": "mode_not_write_enabled"}),
        ("park_conflict_refused", {"units": [{"unit_id": "pod-a", "cause": "unit_armed"}]}),
        ("park_already_parked", {"lease": {"epoch": 1, "reason": "inverter work"}}),
        ("park_mode_out_of_scope", {"prior_word": 4, "vendor_name": "Circulation"}),
        ("park_write_failed", {"error_class": "TransportConnectionError"}),
        (
            "park_readback_unverified",
            {"prior_word": 0, "written_value": 1, "readback_word": 0, "retries": 1},
        ),
    ],
)
def test_every_park_refusal_is_the_pinned_409_envelope(
    code: str,
    details: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    service.park_refusal = ParkingRefusal(code, "the pinned refusal message", details)
    with _client(service, authenticator) as client:
        body = _assert_error(
            client.post(
                PARK_PATH, json=PARK_BODY, headers=_mutation_headers("operator-token")
            ),
            409,
            code,
        )
    assert body["details"] == details


@pytest.mark.parametrize(
    ("code", "details"),
    [
        (
            "park_lease_cap_reached",
            {
                "parked_at": "2026-08-24T02:00:00+00:00",
                "max_total_s": 14400,
                "requested_expires_at": "2026-08-25T02:00:00+00:00",
            },
        ),
        ("park_lease_absent", {"origin": "none", "closed_at": None}),
        (
            "resume_stop_latched",
            {
                "stop_ids": ["stop-1"],
                "acknowledgement_endpoint": (
                    "/api/v1/emergency-stop/{stop_id}/acknowledge"
                ),
            },
        ),
    ],
)
def test_renew_and_resume_refusals_map_verbatim(
    code: str,
    details: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    service.park_refusal = ParkingRefusal(code, "the pinned refusal message", details)
    path = RENEW_PATH if code != "resume_stop_latched" else RESUME_PATH
    body = (
        {"confirmation": "RENEW", "lease_s": 3600}
        if path == RENEW_PATH
        else {"confirmation": "RESUME"}
    )
    with _client(service, authenticator) as client:
        envelope = _assert_error(
            client.post(path, json=body, headers=_mutation_headers("operator-token")),
            409,
            code,
        )
    assert envelope["details"] == details


def test_a_facade_validation_error_maps_to_422_never_500(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    service.park_validation_error = ValueError("lease_s must be between 60 and 14400 seconds")
    with _client(service, authenticator) as client:
        body = _assert_error(
            client.post(
                PARK_PATH, json=PARK_BODY, headers=_mutation_headers("operator-token")
            ),
            422,
            "validation_error",
        )
    assert "lease_s" in body["message"]


# --- the dispatch refusal's provenance rides the intents 409 envelope ---------------


def _debug_refusal(units: list[str], details: Any = None) -> ValueError:
    """The facade's device-mode dispatch refusal, exactly as it is raised."""
    error = ValueError(f"device_debug_mode_active: {sorted(units)}")
    if details is not None:
        error.details = details  # type: ignore[attr-defined]
    return error


def test_the_intents_refusal_carries_the_parked_provenance(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """DESIGN section 3's wire promise: the existing ``device_debug_mode_active``
    refusal fires as it always has -- the message VERBATIM (byte-compat) --
    and the structured provenance the facade attaches gains the 409 envelope's
    ``details`` additively."""
    provenance = {
        "pod-a": {
            "parked_provenance": {
                "parked_at": "2026-08-24T02:00:00+00:00",
                "authorizer": "person:operator",
                "reason": "inverter work",
                "lease_expires_at": "2026-08-24T06:00:00+00:00",
            }
        }
    }
    service.intent_refusal = _debug_refusal(["pod-a"], provenance)
    with _client(service, authenticator) as client:
        body = _assert_error(
            client.post(
                f"{API}/intents",
                json={"unit_ids": ["pod-a"], "direction": "charge", "watts": 500, "ttl_s": 60},
                headers=_mutation_headers("operator-token"),
            ),
            409,
            "device_debug_mode_active",
        )
    assert body["message"] == "device_debug_mode_active: ['pod-a']"
    assert body["details"] == provenance


def test_the_intents_refusal_stays_shape_identical_without_provenance(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """The non-parked refusal (a debugging word the ledger does not name) is
    the same envelope with empty details -- additive means additive: no
    provenance, no new keys, the message still verbatim."""
    service.intent_refusal = _debug_refusal(["pod-b"])
    with _client(service, authenticator) as client:
        body = _assert_error(
            client.post(
                f"{API}/intents",
                json={"unit_ids": ["pod-b"], "direction": "charge", "watts": 500, "ttl_s": 60},
                headers=_mutation_headers("operator-token"),
            ),
            409,
            "device_debug_mode_active",
        )
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["message"] == "device_debug_mode_active: ['pod-b']"
    assert body["details"] == {}


def test_an_unknown_unit_maps_to_404(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    service.park_validation_error = LookupError("no unit with id 'pod-ghost'")
    with _client(service, authenticator) as client:
        _assert_error(
            client.post(
                f"{API}/units/pod-ghost/park",
                json=PARK_BODY,
                headers=_mutation_headers("operator-token"),
            ),
            404,
            "unit_not_found",
        )


# --- the strict request shapes ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"confirmation": "PARK"},  # reason required
        {"confirmation": "RESUME", "reason": "x"},  # wrong literal
        {"confirmation": "park", "reason": "inverter work"},  # case-sensitive literal
        {"confirmation": "PARK", "reason": " padded "},  # canonical
        {"confirmation": "PARK", "reason": "x" * 501},  # bound
        {"confirmation": "PARK", "reason": "inverter work", "lease_s": 30},  # min 60
        {"confirmation": "PARK", "reason": "inverter work", "lease_s": 1.5},  # strict int
        {"confirmation": "PARK", "reason": "inverter work", "extra": 1},  # extra forbidden
    ],
)
def test_the_park_body_is_strict(
    body: dict[str, Any],
    service: RecordingEnergyService,
    authenticator: FakeAuthenticator,
) -> None:
    with _client(service, authenticator) as client:
        _assert_error(
            client.post(PARK_PATH, json=body, headers=_mutation_headers("operator-token")),
            422,
            "validation_error",
        )
    assert service.calls == []


def test_the_resume_takeover_literal_is_the_only_accepted_value(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    with _client(service, authenticator) as client:
        _assert_error(
            client.post(
                RESUME_PATH,
                json={"confirmation": "RESUME", "takeover": "ACKNOWLEDGE"},
                headers=_mutation_headers("operator-token"),
            ),
            422,
            "validation_error",
        )
    assert service.calls == []


def test_the_openapi_summaries_carry_the_fixed_not_isolation_sentence(
    service: RecordingEnergyService, authenticator: FakeAuthenticator
) -> None:
    """DESIGN section 0: the fixed sentence appears verbatim in the park and
    resume route summaries -- the documented operator-facing home of the
    warning; no countdown may imply time-bounded safety."""
    sentence = "Parking is not electrical isolation"
    with _client(service, authenticator) as client:
        spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    park_key = "/api/v1/units/{unit_id}/park"
    resume_key = "/api/v1/units/{unit_id}/resume"
    assert park_key in paths and resume_key in paths
    park_text = " ".join(
        (
            str(paths[park_key]["post"].get("summary", ""))
            + str(paths[park_key]["post"].get("description", ""))
        ).split()
    )
    resume_text = " ".join(
        (
            str(paths[resume_key]["post"].get("summary", ""))
            + str(paths[resume_key]["post"].get("description", ""))
        ).split()
    )
    assert sentence in park_text
    assert "battery stays connected at full voltage" in park_text
    assert "lease countdown is policy, never safety" in park_text
    assert sentence in resume_text
