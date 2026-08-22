"""Contract tests for the run-mode credential store (API_CONTRACTS
"Operations surface" / "Security and privacy").

``energypod.runtime.credentials.FileCredentialStore`` is the real credential
store behind ``run`` mode: credentials are supplied at runtime through a JSON
file mapping bearer tokens to ``{subject, scopes, interactive, site_id}``, are
scoped to exactly the boundary vocabulary, rotate by replacing the file, and
never appear in logs, errors, or responses.  The suite is authored
contract-first (the module does not exist yet), so every production import is
lazy and a missing contract surfaces as an ordinary test failure, never as a
collection error.

Pinned decisions, each pinned exactly one way:

- Loading is eager and loud at the store boundary: a present-but-invalid file
  (malformed JSON, wrong shape, unknown scope) raises a clear error naming
  the file and never names a token.  Composition wraps that: ``build_runtime``
  gains an injectable ``credential_store`` used only when
  ``config.authentication`` references credentials, and any unusable file
  composes the fail-closed surface (every bearer refused with the structured
  401 envelope) instead of crashing.
- Rotation is per authentication: the store re-reads the file when its stat
  changes, so replacing the file swaps tokens without a restart, while an
  unusable replacement (or a deleted file) keeps the fail-closed default.
- A store injected without an ``authentication`` block is not used: the
  configuration, not the injection, is the authority, and the simulator
  development grant stays exactly "no credential store configured".

No test opens a socket, contacts hardware, or reads a real secret: every
bearer string below is a synthetic literal deliberately shaped so it could
never be mistaken for (or validated as) a canonical identifier.
"""

from __future__ import annotations

import hmac
import importlib
import inspect
import json
import os
import secrets
import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from energypod.runtime.config import ControllerConfig

SITE_ID = "home"
UNIT_ID = "mid"
_CREDENTIALS_NAME = "credentials.json"

# API_CONTRACTS "API and MCP": reads require `observe` (audit additionally
# `audit:read`), intent mutations `dispatch`, arming `arm` plus an interactive
# human principal, and stop acknowledgement `stop:acknowledge`.  This is the
# complete scope vocabulary a credential may carry -- nothing invented,
# nothing missing ("Maintenance is absent").
FULL_SCOPES = (
    "observe",
    "audit:read",
    "dispatch",
    "arm",
    "stop",
    "stop:acknowledge",
)


def _load(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        pytest.fail(f"credential contract dependency is not implemented: {module_name}: {error}")


def _store_class() -> Any:
    value = getattr(_load("energypod.runtime.credentials"), "FileCredentialStore", None)
    if value is None:
        pytest.fail(
            "energypod.runtime.credentials.FileCredentialStore is not implemented",
            pytrace=False,
        )
    return value


def _store(path: Path) -> Any:
    return _store_class()(path)


async def _settle(value: Any) -> Any:
    """Await a repository result only when the composed handle is asynchronous."""
    if inspect.isawaitable(value):
        return await value
    return value


def _bearer(label: str) -> str:
    """One synthetic credential; deliberately not a canonical identifier.

    Real bearer secrets are opaque strings, so the `=` sign keeps this value
    outside the boundary's identifier grammar and pins that the store never
    validates a *secret* as if it were an identity.
    """
    return f"cred-{label}={'a' * 24}"


def _entry(
    subject: str,
    scopes: tuple[str, ...] = FULL_SCOPES,
    *,
    interactive: bool = True,
    site_id: str = SITE_ID,
) -> dict[str, Any]:
    return {
        "subject": subject,
        "scopes": list(scopes),
        "interactive": interactive,
        "site_id": site_id,
    }


def _write_credentials(path: Path, entries: Mapping[str, Mapping[str, Any]], *, stamp: int) -> None:
    """Write one credential file and pin its mtime to a distinct, increasing stamp.

    Rotation is detected through the file stat, and filesystem timestamp
    granularity differs across hosts, so every rewrite carries an explicitly
    advanced mtime: the suite never passes or fails on a clock accident.
    """
    path.write_text(json.dumps(dict(entries), indent=2, sort_keys=True), encoding="utf-8")
    nanos = stamp * 1_000_000_000
    os.utime(path, ns=(nanos, nanos))


def _expect_rejected_at_load(
    path: Path, *, must_name: tuple[str, ...] = (), never_name: tuple[str, ...] = ()
) -> None:
    """The loud load error: it names the file and the problem, never a token."""
    with pytest.raises((ValueError, OSError)) as excinfo:
        _store(path)
    message = str(excinfo.value)
    assert path.name in message, f"the load error must name the credential file: {message!r}"
    for needle in must_name:
        assert needle in message, f"the load error must name {needle!r}: {message!r}"
    for secret in never_name:
        assert secret not in message, "a load error must never contain a credential"


def _forbid_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("credential authentication must not open sockets")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "socketpair", refused)


async def _asgi_request(
    app: Any,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Drive one request through the composed ASGI app with no test server."""
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    body = b"" if json_body is None else json.dumps(dict(json_body)).encode("utf-8")
    if json_body is not None:
        raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(body)).encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return status, json.loads(response_body)


def _bearer_headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


# --- configuration payloads (shared commissioning fixtures) ------------------


def _timing_payload() -> dict[str, Any]:
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "live-trial://direction-2026-08-22/rev-1",
        "control_period_s": 0.40,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _policy_payload() -> dict[str, Any]:
    return {
        "version": 3,
        "threshold_provenance": "commissioning-record-2026-08",
        "max_fleet_charge_w": 6000,
        "max_fleet_discharge_w": 6000,
        "max_unit_charge_w": 2500,
        "max_unit_discharge_w": 2500,
        "minimum_soc_pct": 10.0,
        "maximum_soc_pct": 95.0,
        "minimum_cell_v": 2.80,
        "maximum_cell_v": 3.65,
        "maximum_cell_imbalance_v": 0.050,
        "minimum_temperature_c": 0.0,
        "maximum_temperature_c": 45.0,
        "maximum_soc_difference_pct": 5.0,
        "maximum_soc_jump_pct": 10.0,
        "maximum_telemetry_age_s": 1.0,
        "maximum_cell_data_age_s": 5.0,
        "authorization_lifetime_s": 0.75,
        "ramp_limit_w_per_s": 1000,
        "stable_samples_to_rearm": 5,
        "reactive_power_limit_var": 0,
        "blocking_fault_codes": [
            "PCS_EE_CALIBRATION_OUT_OF_RANGE",
            "DCDC_EE_CALIBRATION_OUT_OF_RANGE",
        ],
        "debug_modes_enabled": False,
    }


def _authentication_payload() -> dict[str, Any]:
    return {
        "enabled": True,
        "operator_credential_ref": "secret://energypod/credential-store-contract",
        "trusted_proxy_cidrs": ["192.168.1.0/24"],
    }


def _config_payload(*, mode: str, authentication: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 7,
        "mode": mode,
        "site": {
            "site_id": SITE_ID,
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [
            {
                "unit_id": UNIT_ID,
                "display_name": "Mid",
                "endpoint": {"host": "192.168.1.11", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": "BEP-MID",
                "expected_cell_count": 59,
            }
        ],
        "timing": _timing_payload(),
    }
    if authentication:
        payload["authentication"] = _authentication_payload()
    if mode == "write_enabled":
        payload["policy"] = _policy_payload()
    return payload


def _validated(payload: Mapping[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(dict(payload))


def _compose(
    config: ControllerConfig,
    store: Any | None = None,
    *,
    simulate: bool = False,
    announce: Callable[[str], None] | None = None,
) -> Any:
    """Compose through the only composition point with an injected store."""
    factory = getattr(_load("energypod.runtime.composition"), "build_runtime", None)
    if not callable(factory):
        pytest.fail("energypod.runtime.composition.build_runtime is not implemented", pytrace=False)
    overrides: dict[str, Any] = {"simulate": simulate}
    if store is not None:
        overrides["credential_store"] = store
    if announce is not None:
        overrides["dev_credential_announce"] = announce
    try:
        return factory(config, **overrides)
    except TypeError as error:
        pytest.fail(
            "build_runtime must accept an injectable `credential_store` used when "
            f"config.authentication references credentials: {error}",
            pytrace=False,
        )


def _compose_with_credentials(
    entries: Mapping[str, Mapping[str, Any]],
    tmp_path: Path,
    *,
    simulate: bool = True,
    mode: str = "write_enabled",
    announce: Callable[[str], None] | None = None,
) -> tuple[Any, Any, Path]:
    """Write a credential file, load it, and compose a store-backed runtime."""
    path = tmp_path / _CREDENTIALS_NAME
    _write_credentials(path, entries, stamp=1)
    store = _store(path)
    runtime = _compose(
        _validated(_config_payload(mode=mode, authentication=True)),
        store,
        simulate=simulate,
        announce=announce,
    )
    return runtime, store, path


# --- the store surface --------------------------------------------------------


async def test_the_store_loads_the_mapped_tokens_to_principals(tmp_path: Path) -> None:
    """Pin 1: the JSON file maps tokens to {subject, scopes, interactive, site_id}."""
    path = tmp_path / _CREDENTIALS_NAME
    operator_bearer = _bearer("operator")
    automation_bearer = _bearer("automation")
    opaque_bearer = "cred-opaque=~+not/an@identifier.shape"
    _write_credentials(
        path,
        {
            operator_bearer: _entry("person:operator", FULL_SCOPES),
            automation_bearer: _entry(
                "automation:optimizer", ("observe", "dispatch"), interactive=False
            ),
            opaque_bearer: _entry("person:opaque", ("observe",)),
        },
        stamp=1,
    )
    store = _store(path)

    operator = await store.authenticate(operator_bearer)
    assert operator is not None
    assert operator.subject == "person:operator"
    assert operator.scopes == frozenset(FULL_SCOPES)
    assert isinstance(operator.scopes, frozenset)
    assert type(operator.interactive) is bool
    assert operator.interactive is True
    assert operator.site_id == SITE_ID
    # A credential is not single-use: the same offer keeps authenticating.
    assert await store.authenticate(operator_bearer) is not None

    automation = await store.authenticate(automation_bearer)
    assert automation is not None
    assert automation.subject == "automation:optimizer"
    assert automation.scopes == frozenset({"observe", "dispatch"})
    assert automation.interactive is False

    # A secret need not be a canonical identifier; only identities are identities.
    opaque = await store.authenticate(opaque_bearer)
    assert opaque is not None
    assert opaque.subject == "person:opaque"

    # Anything that is not exactly a mapped token is no credential at all.
    for refused in ("", "x" * 64, f"{operator_bearer}x", operator_bearer.upper(), "nön-ascii"):
        assert await store.authenticate(refused) is None, refused


async def test_token_comparison_routes_through_the_constant_time_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin 2: bearer comparison uses the standard library's constant-time digest.

    A dictionary lookup (or ``==``) short-circuits on the first differing
    byte; ``hmac.compare_digest`` is the only shipped comparison whose timing
    is independent of where the offered token disagrees.  The module is
    reloaded under surveillance so both the ``from hmac import ...`` and the
    ``hmac.compare_digest``/``secrets.compare_digest`` spellings are covered.
    """
    credentials = _load("energypod.runtime.credentials")
    path = tmp_path / _CREDENTIALS_NAME
    bearer = _bearer("timing")
    _write_credentials(path, {bearer: _entry("person:timing", ("observe",))}, stamp=1)
    witnessed: list[tuple[Any, ...]] = []
    real_digest = hmac.compare_digest

    def surveilled(*args: Any, **kwargs: Any) -> Any:
        witnessed.append(args)
        return real_digest(*args, **kwargs)

    try:
        with monkeypatch.context() as patched:
            patched.setattr(hmac, "compare_digest", surveilled)
            patched.setattr(secrets, "compare_digest", surveilled)
            reloaded = importlib.reload(credentials)
            store = reloaded.FileCredentialStore(path)
            before = len(witnessed)
            assert await store.authenticate("offered-but-unmapped") is None
            assert len(witnessed) > before, (
                "an unmapped offer must still be compared through the constant-time digest, "
                "not answered by a short-circuiting lookup"
            )
    finally:
        # Rebind the module to the real digest for every later test.
        importlib.reload(credentials)


@pytest.mark.parametrize("scope", FULL_SCOPES)
async def test_every_boundary_scope_is_accepted_and_returned_exactly(
    tmp_path: Path, scope: str
) -> None:
    """Pin 3: the vocabulary is exactly what the guarded boundary can check."""
    path = tmp_path / _CREDENTIALS_NAME
    bearer = _bearer(f"scope-{scope.replace(':', '-')}")
    _write_credentials(path, {bearer: _entry("person:scope-probe", (scope,))}, stamp=1)
    store = _store(path)
    principal = await store.authenticate(bearer)
    assert principal is not None
    assert principal.scopes == frozenset({scope})


@pytest.mark.parametrize(
    ("scope", "must_name_scope"),
    [
        ("maintenance", True),
        ("admin", True),
        ("OBSERVE", True),
        ("observe ", True),
        ("audit:read:extra", True),
        ("observe,dispatch", True),
        ("", False),
    ],
    ids=[
        "maintenance_absent",
        "admin",
        "wrong_case",
        "padded",
        "compound_impostor",
        "comma_joined",
        "empty",
    ],
)
async def test_unknown_scope_strings_fail_validation_at_load_naming_the_file(
    tmp_path: Path, scope: str, must_name_scope: bool
) -> None:
    """Pin 3: an invented scope is a commissioning error, never a silent grant."""
    path = tmp_path / _CREDENTIALS_NAME
    bearer = _bearer("scope-outlier")
    _write_credentials(path, {bearer: _entry("person:outlier", ("observe", scope))}, stamp=1)
    _expect_rejected_at_load(
        path, must_name=(("observe", scope) if must_name_scope else ()), never_name=(bearer,)
    )


def _tampered_files() -> list[tuple[str, Any, tuple[str, ...]]]:
    """One (case, JSON payload, credentials that must stay out of the error)."""
    shape_bearer = _bearer("shape")
    witness_bearer = _bearer("witness")
    sound = _entry("person:operator", ("observe",))
    witness = _entry("person:witness", ("observe",))
    hidden = (shape_bearer, witness_bearer)

    def case(label: str, payload: Any) -> tuple[str, Any, tuple[str, ...]]:
        return (label, payload, hidden)

    return [
        case(
            "unknown_entry_key",
            {shape_bearer: {**sound, "comment": "tampered"}, witness_bearer: witness},
        ),
        case(
            "missing_scopes",
            {shape_bearer: {k: v for k, v in sound.items() if k != "scopes"}},
        ),
        case(
            "missing_interactive",
            {shape_bearer: {k: v for k, v in sound.items() if k != "interactive"}},
        ),
        case(
            "missing_subject",
            {shape_bearer: {k: v for k, v in sound.items() if k != "subject"}},
        ),
        case(
            "missing_site_id",
            {shape_bearer: {k: v for k, v in sound.items() if k != "site_id"}},
        ),
        case("scopes_as_string", {shape_bearer: {**sound, "scopes": "observe"}}),
        case("scopes_as_object", {shape_bearer: {**sound, "scopes": {"observe": True}}}),
        case("interactive_as_string", {shape_bearer: {**sound, "interactive": "yes"}}),
        case("interactive_as_integer", {shape_bearer: {**sound, "interactive": 1}}),
        case("subject_not_a_string", {shape_bearer: {**sound, "subject": 7}}),
        case("empty_subject", {shape_bearer: {**sound, "subject": ""}}),
        case("empty_site_id", {shape_bearer: {**sound, "site_id": ""}}),
        case("entry_not_an_object", {shape_bearer: ["observe"]}),
        case("top_level_array", [sound]),
        case("top_level_string", "credentials"),
        case("empty_token_key", {"": sound, shape_bearer: witness}),
    ]


@pytest.mark.parametrize(
    ("case_id", "payload", "bearers_in_file"),
    _tampered_files(),
    ids=[item[0] for item in _tampered_files()],
)
async def test_tampered_or_malformed_files_fail_validation_naming_the_file(
    tmp_path: Path, case_id: str, payload: Any, bearers_in_file: tuple[str, ...]
) -> None:
    """Pin 1: a present-but-wrong file is rejected loudly, and never echoes a token."""
    path = tmp_path / _CREDENTIALS_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    _expect_rejected_at_load(path, never_name=bearers_in_file)


async def test_unparseable_json_fails_validation_naming_the_file(tmp_path: Path) -> None:
    """Pin 1: garbage content is a load error naming the file, not a runtime crash."""
    path = tmp_path / _CREDENTIALS_NAME
    path.write_text("{ this is not json", encoding="utf-8")
    _expect_rejected_at_load(path)

    path.write_text("", encoding="utf-8")
    _expect_rejected_at_load(path)


async def test_a_missing_credential_file_is_a_loud_load_error(tmp_path: Path) -> None:
    """Pin 1: an absent store is a commissioning error, not a silent empty store."""
    path = tmp_path / "absent-credentials.json"
    _expect_rejected_at_load(path)


async def test_the_store_never_renders_its_tokens(tmp_path: Path) -> None:
    """Pin 2: no rendering of the store leaks a credential."""
    path = tmp_path / _CREDENTIALS_NAME
    first_bearer = _bearer("render-first")
    second_bearer = _bearer("render-second")
    _write_credentials(
        path,
        {
            first_bearer: _entry("person:first", FULL_SCOPES),
            second_bearer: _entry("person:second", ("observe",)),
        },
        stamp=1,
    )
    store = _store(path)
    for rendered in (repr(store), str(store)):
        for bearer in (first_bearer, second_bearer):
            assert bearer not in rendered


# --- rotation: the pinned reload story ---------------------------------------


async def test_rotation_swaps_tokens_without_restart(tmp_path: Path) -> None:
    """Pin 4 (per-authentication stat+mtime re-read): replacing the file is the
    rotation mechanism -- new tokens appear, removed tokens stop working, and
    the same store instance serves both without any restart."""
    path = tmp_path / _CREDENTIALS_NAME
    departing_bearer = _bearer("departing")
    remaining_bearer = _bearer("remaining")
    arriving_bearer = _bearer("arriving")
    _write_credentials(
        path,
        {
            departing_bearer: _entry("person:departing", FULL_SCOPES),
            remaining_bearer: _entry("person:viewer", ("observe",)),
        },
        stamp=1,
    )
    store = _store(path)
    assert await store.authenticate(departing_bearer) is not None
    retained = await store.authenticate(remaining_bearer)
    assert retained is not None
    assert retained.subject == "person:viewer"

    # Rotate: the departing credential is dropped, the retained one is
    # rescoped, and a successor is added -- all in one file replacement.
    _write_credentials(
        path,
        {
            remaining_bearer: _entry("person:viewer", ("observe", "audit:read")),
            arriving_bearer: _entry("person:successor", FULL_SCOPES),
        },
        stamp=2,
    )

    assert await store.authenticate(departing_bearer) is None, (
        "a token removed from the file must stop authenticating"
    )
    rescoped = await store.authenticate(remaining_bearer)
    assert rescoped is not None
    assert rescoped.scopes == frozenset({"observe", "audit:read"})
    successor = await store.authenticate(arriving_bearer)
    assert successor is not None
    assert successor.subject == "person:successor"
    assert successor.scopes == frozenset(FULL_SCOPES)


async def test_an_unusable_file_after_rotation_fails_closed_without_raising(
    tmp_path: Path,
) -> None:
    """Pin 4: an unusable replacement keeps the fail-closed default.

    Re-read failures are never raised out of authentication and never fall
    back to stale credentials: every offer is refused until a valid file is
    back, and the same instance recovers the moment one is.
    """
    path = tmp_path / _CREDENTIALS_NAME
    bearer = _bearer("rotation-victim")
    _write_credentials(path, {bearer: _entry("person:operator", FULL_SCOPES)}, stamp=1)
    store = _store(path)
    assert await store.authenticate(bearer) is not None

    path.write_text("]{ not json at all", encoding="utf-8")
    nanos = 2 * 1_000_000_000
    os.utime(path, ns=(nanos, nanos))
    assert await store.authenticate(bearer) is None, "a malformed file must refuse everyone"

    path.unlink()
    assert await store.authenticate(bearer) is None, "a deleted file must refuse everyone"

    replacement_bearer = _bearer("rotation-successor")
    _write_credentials(
        path, {replacement_bearer: _entry("person:successor", ("observe",))}, stamp=3
    )
    recovered = await store.authenticate(replacement_bearer)
    assert recovered is not None
    assert await store.authenticate(bearer) is None


# --- composition wiring --------------------------------------------------------


async def test_build_runtime_authenticates_file_credentials_through_the_guarded_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin 5: composition uses the configured store; nothing else authenticates."""
    _forbid_sockets(monkeypatch)
    capsys.readouterr()
    operator_bearer = _bearer("operator")
    runtime, _store_handle, _path = _compose_with_credentials(
        {operator_bearer: _entry("person:operator", FULL_SCOPES)},
        tmp_path,
        simulate=True,
    )

    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(operator_bearer)
    )
    assert status == 200, body
    assert body["site_id"] == SITE_ID
    status, _audit = await _asgi_request(
        runtime.app, "GET", "/api/v1/audit", headers=_bearer_headers(operator_bearer)
    )
    assert status == 200

    # Anything that is not exactly the mapped token gets the structured 401
    # envelope -- never a leak of what was offered.
    for candidate in ("", "x" * 64, f"{operator_bearer}x", operator_bearer.upper(), "nön-ascii"):
        status, body = await _asgi_request(
            runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(candidate)
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"
        assert body["request_id"]
        # The offered credential must never leak into the response body.  The
        # empty offer has nothing to leak and is a substring of every body, so
        # only non-empty candidates can carry this pin.
        if candidate:
            assert candidate not in json.dumps(body)
    status, body = await _asgi_request(runtime.app, "GET", "/api/v1/snapshot")
    assert status == 401
    assert body["code"] == "authentication_required"

    # /healthz stays the one unauthenticated endpoint, liveness only.
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}

    captured = capsys.readouterr()
    assert operator_bearer not in captured.out, "a credential must never reach stdout"


async def test_file_credentials_enforce_the_boundary_scope_vocabulary_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin 3 through the real boundary: each scope grants exactly its surface."""
    _forbid_sockets(monkeypatch)
    viewer_bearer = _bearer("viewer")
    auditor_bearer = _bearer("auditor")
    automation_bearer = _bearer("automation")
    noninteractive_armer_bearer = _bearer("noninteractive-armer")
    operator_bearer = _bearer("operator")
    runtime, _store_handle, _path = _compose_with_credentials(
        {
            viewer_bearer: _entry("person:viewer", ("observe",)),
            auditor_bearer: _entry("person:auditor", ("observe", "audit:read")),
            automation_bearer: _entry(
                "automation:optimizer", ("observe", "dispatch"), interactive=False
            ),
            noninteractive_armer_bearer: _entry(
                "person:automation-armer", ("observe", "arm"), interactive=False
            ),
            operator_bearer: _entry("person:operator", FULL_SCOPES),
        },
        tmp_path,
        simulate=True,
    )
    app = runtime.app

    # observe alone reads the fleet and nothing else.
    status, body = await _asgi_request(
        app, "GET", "/api/v1/snapshot", headers=_bearer_headers(viewer_bearer)
    )
    assert status == 200, body
    status, body = await _asgi_request(
        app, "GET", "/api/v1/audit", headers=_bearer_headers(viewer_bearer)
    )
    assert status == 403
    assert body["code"] == "insufficient_scope"
    status, body = await _asgi_request(
        app,
        "POST",
        "/api/v1/intents",
        headers=_bearer_headers(viewer_bearer),
        json_body={"unit_ids": [UNIT_ID], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 403
    assert body["code"] == "insufficient_scope"

    # audit:read unlocks the audit view on top of observe.
    status, _body = await _asgi_request(
        app, "GET", "/api/v1/audit", headers=_bearer_headers(auditor_bearer)
    )
    assert status == 200

    # dispatch accepts an intent that carries the file's automation subject.
    status, body = await _asgi_request(
        app,
        "POST",
        "/api/v1/intents",
        headers={
            **_bearer_headers(automation_bearer),
            "Idempotency-Key": "file-credential-dispatch-1",
        },
        json_body={"unit_ids": [UNIT_ID], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 202, body
    active = await _settle(runtime.intents.active(runtime.clock.monotonic()))
    assert {intent.actor_identity for intent in active} == {"automation:optimizer"}

    # The arm scope without an interactive principal is still refused.
    status, body = await _asgi_request(
        app,
        "POST",
        "/api/v1/arm",
        headers={
            **_bearer_headers(noninteractive_armer_bearer),
            "Idempotency-Key": "file-credential-arm-refused-1",
        },
        json_body={"unit_ids": [UNIT_ID], "confirmation": "ARM"},
    )
    assert status == 403
    assert body["code"] == "interactive_operator_required"

    # The full interactive credential passes the arm+interactive gate and
    # fails on the unit lookup instead (404, not a 403).
    status, body = await _asgi_request(
        app,
        "POST",
        "/api/v1/units/pod-ghost/inhibit/acknowledge",
        headers={
            **_bearer_headers(operator_bearer),
            "Idempotency-Key": "file-credential-interactive-probe-1",
        },
        json_body={"confirmation": "ACKNOWLEDGE"},
    )
    assert status == 404, body
    assert body["code"] == "unit_not_found"

    # stop and stop:acknowledge: the full latch/acknowledge round trip.
    status, stopped = await _asgi_request(
        app,
        "POST",
        "/api/v1/emergency-stop",
        headers={
            **_bearer_headers(operator_bearer),
            "Idempotency-Key": "file-credential-stop-1",
        },
        json_body={"unit_ids": [UNIT_ID], "reason": "credential-store scope probe"},
    )
    assert status == 202, stopped
    status, acknowledged = await _asgi_request(
        app,
        "POST",
        f"/api/v1/emergency-stop/{stopped['stop_id']}/acknowledge",
        headers={
            **_bearer_headers(operator_bearer),
            "Idempotency-Key": "file-credential-stop-ack-1",
        },
        json_body={"confirmation": "ACKNOWLEDGE"},
    )
    assert status == 200, acknowledged


async def test_run_mode_uses_the_store_without_any_network_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin 5: the store serves run mode too, and needs no socket to do it."""
    _forbid_sockets(monkeypatch)
    operator_bearer = _bearer("run-operator")
    runtime, _store_handle, _path = _compose_with_credentials(
        {operator_bearer: _entry("person:operator", FULL_SCOPES)},
        tmp_path,
        simulate=False,
        mode="observe_only",
    )
    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(operator_bearer)
    )
    assert status == 200, body
    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers("not-a-mapped-credential")
    )
    assert status == 401
    assert body["code"] == "authentication_required"
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}


async def test_run_mode_without_a_store_reference_stays_fail_closed(
    tmp_path: Path,
) -> None:
    """Pin 5: `_UnresolvedCredentialAuthenticator` stays the default without a store."""
    composition = _load("energypod.runtime.composition")
    unresolved = getattr(composition, "_UnresolvedCredentialAuthenticator", None)
    assert isinstance(unresolved, type), (
        "the fail-closed authenticator must remain the composed default when no "
        "credential store is injected"
    )
    assert await unresolved().authenticate(_bearer("nobody")) is None

    # A credential file sitting next to the configuration composes nothing on
    # its own: only an injected store referenced by the configuration unlocks
    # authentication, and every bearer -- including the file's own -- is
    # refused with the structured envelope.
    path = tmp_path / _CREDENTIALS_NAME
    operator_bearer = _bearer("operator")
    _write_credentials(path, {operator_bearer: _entry("person:operator", FULL_SCOPES)}, stamp=1)
    runtime = _compose(_validated(_config_payload(mode="write_enabled", authentication=True)))
    for candidate in (operator_bearer, "anything", "x" * 64):
        status, body = await _asgi_request(
            runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(candidate)
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}


async def test_a_store_without_an_authentication_reference_is_not_used(
    tmp_path: Path,
) -> None:
    """Pin 5: the configuration, not the injection, is the authority.

    An injected store never turns authentication on for a configuration that
    does not reference one, and the simulator development grant stays exactly
    "no credential store configured" -- minted, announced once, and the only
    credential that deployment accepts.
    """
    path = tmp_path / _CREDENTIALS_NAME
    operator_bearer = _bearer("operator")
    _write_credentials(path, {operator_bearer: _entry("person:operator", FULL_SCOPES)}, stamp=1)
    store = _store(path)

    runtime = _compose(
        _validated(_config_payload(mode="observe_only", authentication=False)),
        store,
        simulate=False,
    )
    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(operator_bearer)
    )
    assert status == 401, "run mode without an authentication reference must refuse every bearer"
    assert body["code"] == "authentication_required"
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200

    announced: list[str] = []
    simulated = _compose(
        _validated(_config_payload(mode="observe_only", authentication=False)),
        store,
        simulate=True,
        announce=announced.append,
    )
    assert len(announced) == 1, "the simulator grant is unchanged by an unused injected store"
    status, _body = await _asgi_request(
        simulated.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(announced[0])
    )
    assert status == 200
    status, body = await _asgi_request(
        simulated.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(operator_bearer)
    )
    assert status == 401
    assert body["code"] == "authentication_required"


async def test_composition_over_a_corrupt_file_fails_closed_and_recovers_on_rotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pin 1: an unusable file at composition never crashes the build.

    The store loaded cleanly over a sound file, the file then went bad before
    composition: the runtime still composes, every bearer is refused with the
    structured 401 envelope, liveness keeps answering -- and once a valid file
    is back, the same composed process authenticates it without a restart.
    """
    capsys.readouterr()
    path = tmp_path / _CREDENTIALS_NAME
    operator_bearer = _bearer("operator")
    _write_credentials(path, {operator_bearer: _entry("person:operator", FULL_SCOPES)}, stamp=1)
    store = _store(path)

    path.write_text("}{ tampered beyond parsing", encoding="utf-8")
    nanos = 2 * 1_000_000_000
    os.utime(path, ns=(nanos, nanos))

    runtime = _compose(
        _validated(_config_payload(mode="write_enabled", authentication=True)),
        store,
        simulate=True,
    )
    for candidate in (operator_bearer, "anything"):
        status, body = await _asgi_request(
            runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(candidate)
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"
        assert body["request_id"]
        assert candidate not in json.dumps(body)
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}

    successor_bearer = _bearer("successor")
    _write_credentials(path, {successor_bearer: _entry("person:successor", FULL_SCOPES)}, stamp=3)
    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(successor_bearer)
    )
    assert status == 200, "the same composed process must adopt the rotated file"
    assert body["site_id"] == SITE_ID
    status, _body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(operator_bearer)
    )
    assert status == 401, "the credential dropped by the rotation must stop working"

    captured = capsys.readouterr()
    for bearer in (operator_bearer, successor_bearer):
        assert bearer not in captured.out


async def test_cross_site_file_principals_are_still_refused_by_the_facade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File credentials are scoped: a foreign site_id authenticates nowhere."""
    _forbid_sockets(monkeypatch)
    local_bearer = _bearer("local")
    stranger_bearer = _bearer("stranger")
    runtime, store, _path = _compose_with_credentials(
        {
            local_bearer: _entry("person:local", FULL_SCOPES),
            stranger_bearer: _entry("person:stranger", FULL_SCOPES, site_id="elsewhere"),
        },
        tmp_path,
        simulate=True,
    )
    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers=_bearer_headers(local_bearer)
    )
    assert status == 200, body

    stranger = await store.authenticate(stranger_bearer)
    assert stranger is not None
    assert stranger.site_id == "elsewhere"
    with pytest.raises(PermissionError):
        await _settle(runtime.facade.snapshot(principal=stranger))
