"""The pvoutput.org reporter's composition contract (block-presence doctrine).

The three composition postures the brief pins, plus the durability proof the
runtime toggle exists for:

- an ABSENT ``pvoutput`` block composes NOTHING (no uploader handle, the
  guarded routes answer 409 ``pvoutput_not_commissioned`` through the
  facade's refusal);
- a PRESENT block composes the surface ALWAYS, with ``enabled: false``
  keeping participation suspended (the toggle starts it);
- unresolved credential REFERENCES (unset environment variables) compose the
  surface WITHOUT the wire client, with the note naming exactly which
  variables are unset -- never a boot failure (the Solcast pattern);
- the runtime toggle is a DURABLE fact: a recomposition over the same
  database (a controller restart) keeps the operator's last choice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from energypod.runtime.config import ControllerConfig

UNIT_IDS = ("mid", "rhs", "lhs")
UNIT_SLOTS = {"lhs": ["v7", "v8"], "rhs": ["v9", "v10"], "mid": ["v11", "v12"]}


class ManualClock:
    """The composition clock shape, frozen for these tests."""

    _origin = 1000.0

    def wall_now(self) -> datetime:
        return datetime(2026, 8, 25, 4, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._origin


def _payload(database: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 7,
        "mode": "observe_only",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 3,
        },
        "units": [
            {
                "unit_id": unit_id,
                "display_name": unit_id.capitalize(),
                "endpoint": {"host": f"192.168.1.{11 + index}", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": f"BEP-{unit_id.upper()}",
                "expected_cell_count": 59,
            }
            for index, unit_id in enumerate(UNIT_IDS)
        ],
        "timing": {
            "device_command_expiry_s": 9.0,
            "device_command_expiry_evidence": "commissioning://placeholder",
            "control_period_s": 0.40,
            "essential_read_timeout_s": 0.10,
            "kernel_timeout_s": 0.05,
            "audit_timeout_s": 0.05,
            "write_timeout_s": 0.10,
            "acknowledgement_timeout_s": 0.10,
            "maximum_jitter_s": 0.10,
            "renewal_margin_s": 0.50,
        },
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
    }


def _compose(payload: dict[str, Any]):
    from energypod.runtime.composition import build_runtime

    return build_runtime(
        ControllerConfig.model_validate(payload),
        simulate=False,
        clock=ManualClock(),
    )


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from unresolved references unless it sets fakes."""
    monkeypatch.delenv("PVOUTPUT_API_KEY", raising=False)
    monkeypatch.delenv("PVOUTPUT_SYSTEM_ID", raising=False)


class TestBlockPresence:
    def test_an_absent_block_composes_nothing(self, tmp_path: Path) -> None:
        runtime = _compose(_payload(tmp_path / "controller.sqlite3"))
        assert runtime.pvoutput is None

    def test_a_present_block_composes_the_surface_suspended(self, tmp_path: Path) -> None:
        payload = _payload(tmp_path / "controller.sqlite3")
        payload["pvoutput"] = {"enabled": False, "unit_slots": UNIT_SLOTS}
        runtime = _compose(payload)
        assert runtime.pvoutput is not None
        status = runtime.pvoutput.status_payload()
        assert status["enabled"] is False
        assert status["enabled_origin"] == "config"

    def test_unresolved_references_compose_the_note_never_a_boot_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _payload(tmp_path / "controller.sqlite3")
        payload["pvoutput"] = {"enabled": True, "unit_slots": UNIT_SLOTS}
        runtime = _compose(payload)  # must not raise (the Solcast pattern)
        status = runtime.pvoutput.status_payload()
        assert status["credentials_note"] is not None
        assert "PVOUTPUT_API_KEY" in status["credentials_note"]
        assert "PVOUTPUT_SYSTEM_ID" in status["credentials_note"]
        assert status["disabled_reason"] == "missing_credentials"

    def test_resolved_references_compose_the_wire_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fake fixture values only -- never a real credential.
        monkeypatch.setenv("PVOUTPUT_API_KEY", "composition-test-key")
        monkeypatch.setenv("PVOUTPUT_SYSTEM_ID", "12345")
        payload = _payload(tmp_path / "controller.sqlite3")
        payload["pvoutput"] = {"enabled": True, "unit_slots": UNIT_SLOTS}
        runtime = _compose(payload)
        status = runtime.pvoutput.status_payload()
        assert status["credentials_note"] is None
        assert status["disabled_reason"] is None
        assert status["enabled"] is True

    def test_the_fleet_cycle_step_holds_the_composed_uploader(self, tmp_path: Path) -> None:
        """The supervision pass is the historian's own envelope: the step must
        hold exactly the uploader handle the runtime exposes, so the bounded
        suppressed tick reaches the real cadence gate (a wiring typo would be
        silently swallowed by the suppression -- this pins it)."""
        from energypod.runtime import composition

        payload = _payload(tmp_path / "controller.sqlite3")
        payload["pvoutput"] = {"enabled": False, "unit_slots": UNIT_SLOTS}
        runtime = _compose(payload)
        assert runtime.pvoutput is not None
        supervision = composition._LAST_SUPERVISION
        assert supervision is not None
        assert supervision._pvoutput is runtime.pvoutput

    def test_the_status_snapshot_carries_the_pinned_layout(self, tmp_path: Path) -> None:
        payload = _payload(tmp_path / "controller.sqlite3")
        payload["pvoutput"] = {"enabled": False, "unit_slots": UNIT_SLOTS}
        runtime = _compose(payload)
        assert runtime.pvoutput.status_payload()["unit_slots"] == {
            "lhs": ["v7", "v8"],
            "mid": ["v11", "v12"],
            "rhs": ["v9", "v10"],
        }


class TestTheDurableToggleAcrossARestart:
    def test_the_operators_choice_survives_a_recomposition(self, tmp_path: Path) -> None:
        """The durability proof: config says ``enabled: false``; the operator
        enables at the toggle; a recomposition over the SAME database (a
        controller restart) boots with the operator's choice -- and a later
        disable survives the same way.  There is no in-memory-only state."""
        database = tmp_path / "durable.sqlite3"
        payload = _payload(database)
        payload["pvoutput"] = {"enabled": False, "unit_slots": UNIT_SLOTS}

        first = _compose(payload)
        assert first.pvoutput.enabled is False
        first.pvoutput.set_enabled(True)
        assert first.pvoutput.enabled_origin == "runtime"

        # A restart: the same configuration, the same durable store.
        second = _compose(payload)
        assert second.pvoutput.enabled is True
        assert second.pvoutput.enabled_origin == "runtime"

        second.pvoutput.set_enabled(False)
        third = _compose(payload)
        assert third.pvoutput.enabled is False
        assert third.pvoutput.enabled_origin == "runtime"

    def test_a_never_toggled_site_boots_from_the_config(self, tmp_path: Path) -> None:
        database = tmp_path / "fresh.sqlite3"
        payload = _payload(database)
        payload["pvoutput"] = {"enabled": False, "unit_slots": UNIT_SLOTS}
        _compose(payload)
        again = _compose(payload)
        assert again.pvoutput.enabled is False
        assert again.pvoutput.enabled_origin == "config"
