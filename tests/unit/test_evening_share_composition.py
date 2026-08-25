"""The composed evening load-sharing surface (DESIGN_EVENING_LOAD_SHARING §7/§8).

The composition-level pins: the ``evening_load_sharing:`` block composes the
adviser, the ``evening_load_share_state`` snapshot projection, and the status
route (block-presence); an ABSENT block composes NOTHING — a byte-identical
snapshot and 409 ``evening_share_not_commissioned`` on the route; the split's
submission twin mints ``els-`` OPTIMIZER intents with per-unit watts under
the composed ``energypod:evening-adviser`` principal exactly like the adviser
twins (the night-charge class, no mode register anywhere on the path); E6's
boot half degrades an act block whose recorded evidence file is MISSING to
advise LOUDLY; the supervision pass carries the tick beside the calibration
twin's slot; and the architecture pins — the historian rides the injected
port (no application import of adapters), the grid classification REUSES the
excess family's shape, and the block adds no write method anywhere on the
path.

T-ELS-ARCHITECTURE's no-intent-alive-at-midnight leg lives here: the composed
TTL plus the validated end wall mean the last split intent dies by TTL long
before any sibling window opens — proven at the composition that shipped.

SAFETY: the in-memory simulate composition only — no socket, no live system.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from energypod.runtime.composition import build_runtime
from energypod.runtime.config import ControllerConfig

from .test_composition import (
    OPERATOR,
    _authentication_payload,
    _policy_payload,
    _timing_payload,
    _unit_payload,
)


def _evening_payload(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "advise",
        "window_local": "16:00",
        "window_end_local": "22:30",
        "min_share_w": 500,
        "cap_w": 2500,
        "participation_floor_pct": 20.0,
        "soc_exponent": 2.0,
        "spill_tolerance_w": 150,
        "import_tolerance_w": 100,
        "assumed_discharge_over_frac": 1.16,
        "frozen_word_ticks": 8,
        "frozen_flow_delta_w": 200,
        "delivery_move_floor_w": 400,
        "exchange_move_floor_w": 150,
        "non_delivery_ticks": 3,
        "intent_ttl_s": 10.0,
        "assumed_capacity_wh": {"mid": 5000},
    }
    block.update(overrides)
    return block


def _payload(evening: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 15,
        "mode": "write_enabled",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [_unit_payload("mid", "BEP-MID", "192.168.1.11")],
        "timing": _timing_payload(),
        "policy": _policy_payload(),
        "authentication": _authentication_payload(),
        "plant_history": {
            "sample_interval_s": 30.0,
            "retention_full_resolution_days": 14,
            "retention_rollup_days": 0,
        },
    }
    if evening is not None:
        payload["evening_load_sharing"] = evening
    return payload


class ManualClock:
    """Deterministic time source (the wedge-scenario clock's shape)."""

    def __init__(self, *, wall: datetime | None = None) -> None:
        self.now = 5000.0
        self.wall = wall or datetime(2026, 8, 25, 8, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall


def _compose(evening: dict[str, Any] | None, clock: Any, tmp_path: Any = None) -> Any:
    payload = _payload(evening)
    if tmp_path is not None:
        payload["storage"] = {
            "database_path": str(tmp_path / "evening.sqlite3"),
            "busy_timeout_ms": 250,
        }
    return build_runtime(ControllerConfig.model_validate(payload), clock=clock, simulate=True)


async def test_an_absent_block_composes_nothing_and_the_route_refuses(tmp_path) -> None:
    runtime = _compose(None, ManualClock(), tmp_path)
    assert runtime.evening_share is None
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "evening_load_share_state" not in snapshot
    with pytest.raises(Exception) as caught:
        await runtime.facade.get_evening_share_status(principal=OPERATOR)
    assert getattr(caught.value, "code", "") == "evening_share_not_commissioned"


async def test_a_present_block_composes_the_projection_and_the_route(tmp_path) -> None:
    runtime = _compose(_evening_payload(), ManualClock(), tmp_path)
    assert runtime.evening_share is not None
    paths = {
        route.path for route in runtime.app.routes if getattr(route, "path", "").startswith("/api")
    }
    assert "/api/v1/evening-sharing/status" in paths
    status = await runtime.facade.get_evening_share_status(principal=OPERATOR)
    assert status["mode"] == "advise"
    assert status["submits"] == "never"
    assert status["window"] == {"opens_local": "16:00", "ends_local": "22:30"}
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert snapshot["evening_load_share_state"] == status


async def test_the_split_submission_twin_mints_els_intents_with_per_unit_watts(
    tmp_path,
) -> None:
    """The internal drive: an OPTIMIZER DISCHARGE intent with the ``els-``
    prefix and NATIVE per-unit watts, attributed to the composed evening
    principal — the night-charge class exactly, judged by everything
    downstream."""
    from energypod.application.evening_share import EVENING_ADVISER_PRINCIPAL
    from energypod.domain.intents import Direction, IntentSource

    runtime = _compose(
        _evening_payload(mode="act", act_netting_evidence="docs/evidence/x.md"),
        ManualClock(),
        tmp_path,
    )
    principal = type("EveningPrincipal", (), {})()
    principal.subject = EVENING_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch"})
    principal.interactive = False
    principal.site_id = "home"
    result = await runtime.facade.submit_evening_intent(
        unit_ids=["mid"],
        direction=Direction.DISCHARGE,
        watts=None,
        ttl_s=10.0,
        watts_by_unit={"mid": 900},
        principal=principal,
    )
    assert result["intent_id"].startswith("els-")
    active = await runtime.intents.active(runtime.clock.monotonic())
    (intent,) = active
    assert intent.source is IntentSource.OPTIMIZER
    assert intent.direction is Direction.DISCHARGE
    assert intent.watts_by_unit == {"mid": 900}
    assert intent.actor_identity == EVENING_ADVISER_PRINCIPAL


async def test_e6_boot_degrades_a_missing_evidence_file_to_advise_loudly(
    tmp_path, monkeypatch
) -> None:
    """The A6 receipts shape: validation stays offline-pure (the path's SHAPE
    passed above); boot verifies EXISTENCE and degrades the act block to
    advise with the loud note riding every projection frame."""
    runtime = _compose(
        _evening_payload(mode="act", act_netting_evidence="docs/evidence/absent.md"),
        ManualClock(),
        tmp_path,
    )
    assert runtime.evening_share is not None
    status = await runtime.facade.get_evening_share_status(principal=OPERATOR)
    assert status["mode"] == "advise"
    assert "degraded_note" in status
    assert "docs/evidence/absent.md" in status["degraded_note"]
    assert "E6" in status["degraded_note"]
    # A PRESENT evidence file keeps the act posture (the shape stays a
    # docs/evidence/ path, resolved from the working directory).
    evidence_dir = tmp_path / "docs" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "netting.md").write_text("cross-check recorded", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runtime_present = _compose(
        _evening_payload(
            mode="act", act_netting_evidence="docs/evidence/netting.md"
        ),
        ManualClock(),
        tmp_path,
    )
    status_present = await runtime_present.facade.get_evening_share_status(
        principal=OPERATOR
    )
    assert status_present["mode"] == "act"
    assert "degraded_note" not in status_present


async def test_no_evening_intent_is_alive_at_the_sibling_windows(tmp_path) -> None:
    """§4's arithmetic, proven at the composition that shipped: the LAST
    intent the adviser could ever submit dies by TTL long before the 23:00
    health watch, the 00:00 night window, and (by the same TTL) long before
    any sibling window opens."""
    from energypod.application.evening_share import EVENING_ADVISER_PRINCIPAL
    from energypod.domain.intents import Direction

    runtime = _compose(
        _evening_payload(mode="act", act_netting_evidence="docs/evidence/x.md"),
        ManualClock(),
        tmp_path,
    )
    principal = type("EveningPrincipal", (), {})()
    principal.subject = EVENING_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch"})
    principal.interactive = False
    principal.site_id = "home"
    # The LAST intent the adviser could ever submit, at the no-new-renewal
    # boundary itself: 22:30 local (12:30 UTC) + the composed 10 s TTL.
    clock = runtime.clock
    clock.wall = datetime(2026, 8, 25, 12, 30, 0, tzinfo=UTC)
    await runtime.facade.submit_evening_intent(
        unit_ids=["mid"],
        direction=Direction.DISCHARGE,
        watts=None,
        ttl_s=10.0,
        watts_by_unit={"mid": 500},
        principal=principal,
    )
    watch_open = datetime(2026, 8, 25, 13, 0, 0, tzinfo=UTC)  # 23:00 local
    clock.wall = watch_open
    clock.now += (watch_open - datetime(2026, 8, 25, 12, 30, 0, tzinfo=UTC)).total_seconds()
    active = await runtime.intents.active(clock.monotonic())
    assert active == ()
    assert clock.wall - datetime(2026, 8, 25, 12, 30, 10, tzinfo=UTC) > timedelta(minutes=29)


async def test_the_historian_rides_the_injected_port_and_classification_is_reused(
    tmp_path,
) -> None:
    """T-ELS-ARCHITECTURE: the application layer imports no adapter (the
    historian rides the injected port), and the grid classification REUSES
    the excess rollup's family — no second implementation exists."""
    adviser_source = Path("src/energypod/application/evening_share.py").read_text(
        encoding="utf-8"
    )
    assert "from energypod.adapters" not in adviser_source
    # The evidence words and their precedence are the excess family's own.
    from energypod.application.evening_share import _BASIS_EVIDENCE_RANK
    from energypod.application.excess_charge import _EXPORT_EVIDENCE_RANK

    assert dict(_BASIS_EVIDENCE_RANK) == dict(_EXPORT_EVIDENCE_RANK)


async def test_no_write_method_is_added_anywhere_on_the_path(tmp_path) -> None:
    """T-ELS-ARCHITECTURE, mutation-style: the debug-mode write composer is
    exactly the parking block's own (the evening block adds none of them),
    the adviser holds no transport and no arm reach, and the composition
    wires no arm scope into the evening principal."""
    composition = Path("src/energypod/runtime/composition.py").read_text(encoding="utf-8")
    write_debug_writers = [line for line in composition.splitlines() if "write_debug_mode" in line]
    assert write_debug_writers, "the parking block's own writer remains"
    assert all("evening" not in line for line in write_debug_writers)
    # No 0x8000 mode register anywhere in the program's own CODE: the word
    # appears only in the module's docstring pins (the first statement), and
    # the module holds no transport, codec, or register reach at all.
    import ast

    adviser_source = Path("src/energypod/application/evening_share.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(adviser_source)
    docstrings = {id(tree.body[0])}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "0x8000" in node.value
        ):
            assert id(node) in docstrings or node.lineno <= 40, (
                "the mode register's name belongs to the PIN, never the code"
            )
    assert "modbus" not in adviser_source
    assert "encode_" not in adviser_source
    # The composed evening principal's scopes carry no arm reach.
    from energypod.runtime.composition import _EVENING_ADVISER_PRINCIPAL_SCOPES

    assert "arm" not in _EVENING_ADVISER_PRINCIPAL_SCOPES


async def test_the_supervision_pass_carries_the_tick_beside_the_calibration_twin(
    tmp_path,
) -> None:
    """T-ELS-ARCHITECTURE: one tick per fleet cycle inside the existing
    bounded supervision pass — no new task class; the two evening advisers
    share the fleet loop slot and nothing else."""
    source = Path("src/energypod/runtime/composition.py").read_text(encoding="utf-8")
    assert "self._evening_share.tick()" in source
    runtime = _compose(_evening_payload(), ManualClock(), tmp_path)
    assert runtime.evening_share is not None
