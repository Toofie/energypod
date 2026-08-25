"""The nightly battery health watch — Stages C (census), P (probe), R (recovery).

DESIGN_BATTERY_HEALTH_WATCH (CONTRACT v1.1) §4/§5/§6/§7.  A ``HealthWatchController``
composed exactly when the ``battery_health_watch:`` block is PRESENT, ticking
once per fleet cycle inside the existing bounded supervision pass (no new task
class), carrying a small per-night phase machine::

    await_window -> census -> probe (lhs -> mid -> rhs, strictly sequential,
    one unit at a time) -> recovery (eligible units, strictly sequential) ->
    record -> done

Its durable facts are AUDIT ROWS, so a restart reconstructs conservatively: a
night whose rows show an attempt never re-runs, an interrupted census, probe,
or recovery composite raises the alert naming the state the unit was actually
left in (A4 — including the crash-after-re-arm state: armed, normal,
lease-less), and "an attempt" — for every once-per-night budget in the
contract — is ANY health-watch audit row for that unit that civil night.

What this module is, pinned:

- **Stage C (§5) is zero writes.**  It issues NO reads of its own and no
  writes, ever: every word it judges (mode words, CT words, SoC, battery
  watts) comes from the standing observation stream or the telemetry
  historian through injected ports (the load-baseline pattern — no
  application import of adapters).
- **Stage P (§6) is ordinary dispatch traffic, not a new write path.**  The
  probe submits short-TTL ``OPTIMIZER`` discharge intents under the composed
  automation principal ``energypod:health-adviser`` through the internal
  facade twin — judged by the arbiter, allocator, SafetyKernel and actor
  exactly like every adviser's intents.  A guard refusal is a SKIP with that
  guard's reason, never a failure of the battery.
- **Stage R (§7) composes; it never forks.**  The recovery cycle's ONLY mode
  writes ride the injected ``ParkController`` (the existing ``{0, 1}``-bound
  primitive — this module holds no transport and no arm/park/resume reach of
  its own), the disarm and the ONE bounded verification re-arm ride the
  facade's internal twins under the health principal, and in the ``advise``
  posture NONE of it runs: no disarm, no park, no re-arm, ever, on any night.
  Eligibility is the §7.1 conjunction — census ``stuck_suspected`` AND probe
  ``fail_no_response`` — and NO ladder state (``actuation_incoherent``
  included, I10) is ever a trigger.  One cycle per unit per civil night;
  never a second attempt after any terminal outcome (I1); no write after an
  ACKed-but-unverified one (I3); the unit ends disarmed-and-Normal or the
  program alerts that it could not (I9).
- **Once per civil night, crash-safe and crash-honest at every arrow** (§4).

The export honesty note (§6.3): on a quiet house the probe's 300 W discharge
briefly EXPORTS up to ~300 W for ~50 s — named on the projection so nobody is
surprised by the export meter.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from energypod.domain.audit import AuditEvent
from energypod.domain.intents import Direction
from energypod.domain.observations import UnitLifecycle

from .parking import (
    PARK_ALREADY_PARKED,
    PARK_CONFLICT_REFUSED,
    PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED,
    PARK_MODE_OUT_OF_SCOPE,
    PARK_READBACK_UNVERIFIED,
    PARK_WRITE_FAILED,
    RESUME_STOP_LATCHED,
    ParkingRefusal,
)

# The composed automation principal (composition supplies the real principal
# for the facade twin; audit rows under this subject are the health
# adviser's own).
HEALTH_ADVISER_PRINCIPAL: Final[str] = "energypod:health-adviser"
_HEALTH_POLICY_VERSION: Final[str] = "health-watch-1"

# Objective-echo classifications (the standing discriminator's vocabulary —
# the probe reuses it, §6.2 step 5).
ECHO_MATCHES_WRITE: Final[str] = "echo_matches_write"
ECHO_EXTERNAL_WRITER: Final[str] = "external_writer"
ECHO_OBJECTIVE_NOT_SERVED: Final[str] = "objective_not_served"
ECHO_UNREADABLE: Final[str] = "echo_unreadable"

# The verdict vocabulary (§5/§6.3).  Census: nominal | stuck_suspected |
# degraded_evidence | excluded:<class>.  Probe: pass | fail_no_response |
# fail_partial | fail_baseline_not_returned | inconclusive_echo_mismatch |
# inconclusive_baseline_confounded | inconclusive_preempted |
# inconclusive_aborted | skipped:<reason>.
CENSUS_NOMINAL: Final[str] = "nominal"
CENSUS_STUCK: Final[str] = "stuck_suspected"
CENSUS_DEGRADED: Final[str] = "degraded_evidence"

PROBE_PASS: Final[str] = "pass"  # noqa: S105 -- a verdict word, not a secret
PROBE_FAIL_NO_RESPONSE: Final[str] = "fail_no_response"
PROBE_FAIL_PARTIAL: Final[str] = "fail_partial"
PROBE_FAIL_BASELINE: Final[str] = "fail_baseline_not_returned"
PROBE_INCONCLUSIVE_ECHO: Final[str] = "inconclusive_echo_mismatch"
PROBE_INCONCLUSIVE_CONFOUNDED: Final[str] = "inconclusive_baseline_confounded"
PROBE_INCONCLUSIVE_PREEMPTED: Final[str] = "inconclusive_preempted"
PROBE_INCONCLUSIVE_ABORTED: Final[str] = "inconclusive_aborted"

# §11's severity tiers (the operator's four distinguishable mornings).
TIER_NOTICE: Final[str] = "notice"
TIER_ALERT: Final[str] = "alert"
# §10/§11: ``health.recovery`` is alert tier on every outcome except
# ``recovered``, which is the resolved tier.
TIER_RESOLVED: Final[str] = "resolved"

# Stage R's verdict vocabulary (§7.2 step 7, §8's ladder):
#   recovered            -- cycle verified AND the verification probe passed
#   recovered_unproven   -- A2: the verification re-run was inconclusive, or
#                           the re-arm was refused; NOT counted to the cap
#   failed_write         -- rung 2: transport resync + ONE re-issue exhausted
#   write_unverified     -- rung 3: ACKed-but-unverified, no further write
#   failed_no_effect     -- rung 4: verified cycle, verification probe FAILED
#   advised              -- the advise posture's ALERT-tier advisory (§7.2)
#   advisory_only        -- the cap's stand-down (§7.3): no cycle until the
#                           operator acknowledgement resets it
RECOVERY_RECOVERED: Final[str] = "recovered"
RECOVERY_RECOVERED_UNPROVEN: Final[str] = "recovered_unproven"
RECOVERY_FAILED_WRITE: Final[str] = "failed_write"
RECOVERY_WRITE_UNVERIFIED: Final[str] = "write_unverified"
RECOVERY_FAILED_NO_EFFECT: Final[str] = "failed_no_effect"
RECOVERY_ADVISED: Final[str] = "advised"
RECOVERY_ADVISORY_ONLY: Final[str] = "advisory_only"

# §8 rung 6 / A2 from the positive side: the counted failures are exactly the
# outcomes where a cycle was ATTEMPTED and the unit was NOT recovered —
# ``recovered_unproven`` proofs that went missing do NOT count.
_RECOVERY_COUNTED_FAILS: Final[frozenset[str]] = frozenset(
    {RECOVERY_FAILED_WRITE, RECOVERY_WRITE_UNVERIFIED, RECOVERY_FAILED_NO_EFFECT}
)
# A11's ``attempts_total``: every outcome where a cycle actually ran,
# recovered and recovered_unproven included (the cap bounds failures, not
# successes — the trailing rate makes the chronic case visible instead).
_RECOVERY_ATTEMPTED: Final[frozenset[str]] = frozenset(
    {*_RECOVERY_COUNTED_FAILS, RECOVERY_RECOVERED, RECOVERY_RECOVERED_UNPROVEN}
)
# The trailing window A11 pins on the morning facts.
_RECOVERY_TRAILING_NIGHTS: Final[int] = 30

# The ladder rung that ended each outcome (§10: the health_recovery_outcome
# row carries it when any did).
RUNG_RESYNC_EXHAUSTED: Final[str] = "transport_resync_exhausted"
RUNG_ACKED_UNVERIFIED: Final[str] = "acked_unverified"
RUNG_VERIFICATION_FAILED: Final[str] = "verification_failed"
RUNG_VERIFICATION_INCONCLUSIVE: Final[str] = "verification_inconclusive"
RUNG_REARM_REFUSED: Final[str] = "rearm_refused"
RUNG_VERIFIED: Final[str] = "verified"

# §7.2's step-1 re-verify skip words, and the R-specific reasons.
SKIP_OPEN_LEASE: Final[str] = "open_lease"
SKIP_FOREIGN_STANDBY: Final[str] = "foreign_standby"
SKIP_DISARM_REFUSED: Final[str] = "disarm_refused"
SKIP_REARM_REFUSED: Final[str] = "rearm_refused"
SKIP_LEASE_BOUNDS: Final[str] = "lease_bounds"
SKIP_PARK_CONFLICT: Final[str] = "park_conflict"
SKIP_RESUME_REFUSED: Final[str] = "resume_refused"
REASON_DEADLINE_PASSED_VERIFICATION: Final[str] = "deadline_passed_before_verification"
REASON_VERIFICATION_INCONCLUSIVE: Final[str] = "verification_inconclusive"

# §11's defined-restart advisory, pinned VERBATIM (the EFT Systems BYD
# service guideline V1.5 procedure; it extends, and where R has run
# supersedes the wording of, the standing PHYSICAL_RESTART_HINT).  The
# 10-minute wait and the battery-first ordering are load-bearing text: they
# ride the alert row, the event, and the console card identically.
RESTART_ADVISORY: Final[str] = (
    "Remote recovery exhausted. Defined-restart procedure, in this order: "
    "battery button OFF for 5 s; DC off; AC off; WAIT 10 MINUTES (the "
    "fuse re-engagement lockout — do not shorten it); battery ON FIRST, "
    "then AC, then DC. Then verify telemetry resumes, Debug Mode reads "
    "Normal Mode and SysControlMode reads Remote in the vendor MiniES app. "
    "Take the logs to the installer if the pod does not return."
)
# The vendor-documented on-device cross-check named beside every cycle
# (§11): force-state transitions land in the BMU event log.
BMU_CROSS_CHECK_NOTE: Final[str] = (
    "force-state transitions are logged in the BMU event log — pull it "
    "afterward as the on-device cross-check on every cycle"
)
# §7.2's advise posture: the exact operator sequence the alert names.
ADVISE_WALKTHROUGH: Final[str] = "disarm → park → resume → re-arm → verify"

# The audit scan bound for the durable-row derivations (once-per-night,
# persistence streaks, the interrupted-program reconstruction): the parking
# boot-adoption precedent's bounded recent window.  A night's program writes
# at most two rows per unit plus a handful of program rows; anything pushed
# beyond this window is honestly NOT FOUND — the streak undercounts and the
# night re-runs conservatively, never a fabricated answer either way.
_ROW_SCAN_LIMIT: Final[int] = 1000

# The evidence-window coverage floor (§5's degraded_evidence verdict): fewer
# usable samples than this share of the historian's expected count means the
# window cannot honestly judge an N-hours predicate — the night renders no
# verdict rather than a wrong one (excluded samples, never interpolated).
_EVIDENCE_COVERAGE_FLOOR: Final[float] = 0.5

# §6.3's export honesty note, pinned verbatim on every probe row.
EXPORT_HONESTY_NOTE: Final[str] = (
    "on a quiet house the probe's brief discharge can export up to its own "
    "magnitude for under a minute — trivial at the feed-in rate, and named so "
    "the export meter is never a surprise"
)

Phase = str  # await_window | census | probe | recovery | record | done

# The skip-if vocabulary (§4/§6.1): every skip is a recorded verdict with its
# reason — never silence, never a failure.
SKIP_UNIT_PARKED: Final[str] = "unit_parked"
SKIP_VENDOR_MODE: Final[str] = "vendor_mode"
SKIP_LATCHED_STOP: Final[str] = "latched_stop"
SKIP_UNREACHABLE: Final[str] = "unreachable"
SKIP_NOT_RESPONDING: Final[str] = "not_responding"
SKIP_FOREIGN_WRITER: Final[str] = "foreign_writer"
SKIP_INHIBITED: Final[str] = "inhibited"
SKIP_TELEMETRY_STALE: Final[str] = "telemetry_stale"
SKIP_UNDER_INTENT: Final[str] = "under_intent"
SKIP_UNIT_DISARMED: Final[str] = "unit_disarmed"
SKIP_QUIET_LOAD_GATE: Final[str] = "quiet_load_gate"
SKIP_QUIET_EVIDENCE_STALE: Final[str] = "quiet_evidence_stale"
SKIP_DEADLINE_PASSED: Final[str] = "deadline_passed"
SKIP_CENSUS_EXCLUDED: Final[str] = "census_excluded"
SKIP_CENSUS_DEGRADED: Final[str] = "census_degraded_evidence"
# §2's doctrine: a probe a guard refuses is a SKIP with that guard's reason,
# never a failure of the battery.  The facade twin's refusal (a unit not
# dispatchable, a policy bound) lands here verbatim as the skip's reason.
SKIP_DISPATCH_REFUSED: Final[str] = "dispatch_refused"

REASON_WINDOW_NOT_QUIET: Final[str] = "window_not_quiet"
REASON_OUTSIDE_WINDOW: Final[str] = "outside_window"
REASON_INTERRUPTED: Final[str] = "interrupted"

# The preemption classes (§4/A12): the probe rides ``optimizer``, which
# OUTRANKS ``schedule`` — only MANUAL/AGENT claims and the emergency stop
# preempt it, and the preemption test uses exactly those claimant classes.
_PREEMPTING_SOURCES: Final[frozenset[str]] = frozenset({"manual", "agent", "emergency_stop"})
_OWN_INTENT_PREFIX: Final[str] = "health-"


class HealthWatchRefusal(Exception):
    """The status surface refused a read (the block-presence doctrine).

    Mirrors the schedule/history refusal types: the REST boundary maps it to
    409 with the pinned code, so the shape stays one per feature.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if not code or code != code.strip():
            raise ValueError("refusal code must be non-empty and normalized")
        self.code = code
        self.message = message


HEALTH_WATCH_NOT_COMMISSIONED: Final[str] = "health_watch_not_commissioned"


# --- settings -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StuckSettings:
    """§5's stuck-signature thresholds (the ``stuck:`` block)."""

    evidence_window_h: int = 6
    soc_floor_pct: float = 95.0
    soc_hold_frac: float = 0.9
    min_soc_hours: int = 6
    still_w: int = 50
    still_frac: float = 0.9
    grid_import_w: int = 500
    sibling_flow_w: int = 500
    flow_frac: float = 0.5
    load_floor_w: int = 30
    sibling_load_w: int = 100
    load_frac: float = 0.8
    flag_persistence_nights: int = 2


@dataclass(frozen=True, slots=True)
class ProbeSettings:
    """§6's probe knobs (the ``probe:`` block)."""

    probe_w: int = 300
    settle_s: int = 20
    sustain_s: int = 30
    pass_fraction: float = 0.5
    pass_sample_frac: float = 0.8
    return_band_w: int = 150
    baseline_return_s: int = 30
    quiet_load_w: int = 1000
    load_move_w: int = 300
    inter_unit_gap_s: int = 30


@dataclass(frozen=True, slots=True)
class RecoverySettings:
    """§7/§9's recovery keys (the ``recovery:`` block)."""

    mode: str = "advise"  # advise | auto
    hold_s: int = 90
    consecutive_fail_limit: int = 3


@dataclass(frozen=True, slots=True)
class HealthWatchSettings:
    """Every behavioural key of the ``battery_health_watch:`` block."""

    timezone: str
    window_local: time
    deadline_local: time
    stages: tuple[str, ...]
    stuck: StuckSettings
    probe: ProbeSettings
    recovery_mode: str
    unit_ids: tuple[str, ...]
    recovery: RecoverySettings = RecoverySettings()
    # The historian's sampling cadence: the census's degraded-evidence
    # coverage floor and the expected-sample count derive from it.
    sample_interval_s: float = 30.0
    # The probe intent's TTL (derived at composition: a few fleet cycles, so
    # a lapsed process's intent dies by TTL, never by watchdog starvation).
    intent_ttl_s: float = 10.0


# --- ports ----------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class _ObservationsPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentsPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class _SubmitPort(Protocol):
    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> Any: ...


class _HistoryPort(Protocol):
    """The census's historian read port (the load-baseline pattern)."""

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]: ...


class _AuditPort(Protocol):
    async def append(self, event: Any) -> None: ...

    async def recent(
        self, *, limit: int, after_sequence: int | None = None
    ) -> tuple[Any, ...]: ...


class _BusPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class _ActorEchoPort(Protocol):
    """The one actor surface the probe reads: the objective-echo read-back."""

    async def read_objective_echo(self) -> tuple[str, tuple[int | None, int | None]]: ...


class _ParkControlPort(Protocol):
    """The Stage R composer's ONE mode-write reach: the ParkController itself.

    §12: the amendment adds an INTERNAL composer, it opens no API — R drives
    the existing ``park``/``resume`` mutations under the health principal, so
    every parking guard (conflict, vendor word, lease epochs, durable-first
    rows, the ``{0, 1}`` transport bound) judges the recovery cycle exactly
    as it judges an operator's act.  No other method of the controller is
    reachable from here, and this module holds no transport of its own.
    """

    async def park(
        self,
        unit_id: str,
        *,
        reason: str,
        principal_subject: str,
        request_id: str,
        lease_s: int | None = None,
    ) -> dict[str, Any]: ...

    async def resume(
        self, unit_id: str, *, principal_subject: str, request_id: str
    ) -> dict[str, Any]: ...


class _LifecyclePort(Protocol):
    """One facade internal twin: the disarm (stop-direction, §7.2 step 2) or
    the ONE bounded verification re-arm (§7.4, panel ruling 1).  Composition
    wires these; they are never routed on REST or MCP."""

    async def __call__(self, *, unit_id: str) -> Mapping[str, Any]: ...


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_text(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _finite(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


def _word(raw: Any) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return int(raw)
    return None


# --- the pure verdict arithmetic (exported for the named tests) ------------------


@dataclass(frozen=True, slots=True)
class CensusFigures:
    """One unit's evidence-window figures (§5's predicate inputs).

    ``sample_interval_s`` is the historian's own cadence: the S1
    continuous-hours comparison carries ONE interval of tolerance against
    it, because a horizon of exactly ``min_soc_hours`` measured over a
    window of exactly that length can never span more than the window
    (samples are instants inside it) — equality (the 6 = 6 defaults) must
    be reachable, and §9 pins that it is intended.
    """

    samples: int = 0
    sample_interval_s: float = 30.0
    soc_full_frac: float = 0.0
    soc_full_longest_run_s: float = 0.0
    still_frac: float = 0.0
    house_needed_frac: float = 0.0
    no_ct_view_frac: float = 0.0
    modes_normal: bool = True


def stuck_predicates(figures: CensusFigures, settings: StuckSettings) -> dict[str, bool]:
    """§5's five predicates over one unit's evidence-window figures.

    ALL must hold for ``stuck_suspected``; the row records the full vector so
    a wrong threshold is discoverable, not hidden.
    """
    horizon_s = settings.min_soc_hours * 3600.0
    return {
        "full": figures.soc_full_frac >= settings.soc_hold_frac
        and figures.soc_full_longest_run_s + figures.sample_interval_s >= horizon_s,
        "still": figures.still_frac >= settings.still_frac,
        "house_needed": figures.house_needed_frac >= settings.flow_frac,
        "no_ct_view": figures.no_ct_view_frac >= settings.load_frac,
        "modes_normal": figures.modes_normal,
    }


def census_verdict(predicates: Mapping[str, bool], *, degraded: bool) -> str:
    """nominal | stuck_suspected | degraded_evidence (excluded is decided upstream)."""
    if degraded:
        return CENSUS_DEGRADED
    if all(predicates.values()):
        return CENSUS_STUCK
    return CENSUS_NOMINAL


MeasuredClass = str  # "met" | "partial" | "still"


def measured_class(
    core_samples: Sequence[float],
    *,
    probe_w: int,
    pass_fraction: float,
    pass_sample_frac: float,
    still_w: int,
) -> tuple[MeasuredClass, int]:
    """§6.3's measured dimension over the core samples.

    Returns ``(measured_class, qualifying_samples)``.  ``met``: the signed
    measured watts in the probe (discharge-positive) direction reach
    ``pass_fraction x probe_w`` on >= ``pass_sample_frac`` of core samples —
    a share, never the mean, so a single-sample spike can never pass a probe
    and a genuinely serving pod holds essentially always.  ``still``: the
    MEAN absolute measured watts stay inside the still band (sustained
    stillness, robust to one metering blip).  Everything else is ``partial``
    — movement in the direction, below the delivery band.
    """
    if not core_samples:
        return ("still", 0)
    threshold = float(pass_fraction) * float(probe_w)
    qualifying = sum(1 for watts in core_samples if watts >= threshold)
    if qualifying >= math.ceil(float(pass_sample_frac) * len(core_samples)):
        return ("met", qualifying)
    mean_abs = sum(abs(watts) for watts in core_samples) / len(core_samples)
    if mean_abs < float(still_w):
        return ("still", qualifying)
    return ("partial", qualifying)


def probe_verdict(
    *,
    measured: MeasuredClass,
    echo: str,
    baseline_returned: bool,
    demand_move_confounded: bool,
) -> str:
    """§6.3's verdict matrix, EXACTLY — measured x echo x baseline-return.

    The demand-move downgrade (A8) applies only where the matrix's baseline
    column was load-bearing (the ``met``/``still`` rows under a matching
    echo): a mid-leg demand move makes the baseline judgment unjudgeable, and
    a pod whose autonomy resumed into a demand spike is not a broken pod.
    """
    if measured in {"met", "still"} and echo == ECHO_MATCHES_WRITE:
        if demand_move_confounded:
            return PROBE_INCONCLUSIVE_CONFOUNDED
        if not baseline_returned:
            return PROBE_FAIL_BASELINE
        return PROBE_PASS if measured == "met" else PROBE_FAIL_NO_RESPONSE
    if measured == "met":
        # Delivery met with any non-matching echo (not_served / external /
        # unreadable): evidence of interference, never stuck evidence.
        return PROBE_INCONCLUSIVE_ECHO
    if measured == "still":
        if echo in {ECHO_OBJECTIVE_NOT_SERVED, ECHO_EXTERNAL_WRITER}:
            # §6.3 row 6, advisory only: under the pinned ACK-then-ignore
            # device model this is the write-never-served class — Stage R is
            # inapplicable to it whatever the census says.
            return PROBE_INCONCLUSIVE_ECHO
        return PROBE_INCONCLUSIVE_ABORTED
    # partial: movement in direction, below the band.  "any readable" echo —
    # an unreadable echo leaves no evidence to hang the verdict on.
    if echo == ECHO_UNREADABLE:
        return PROBE_INCONCLUSIVE_ABORTED
    return PROBE_FAIL_PARTIAL


def probe_tier(verdict: str) -> str:
    """§11's probe tier: alert on the fail classes, quiet otherwise."""
    if verdict.startswith("fail"):
        return TIER_ALERT
    return TIER_NOTICE


# The skip words §7.2 pins at ALERT tier (foreign standby, vendor mode) plus
# the exits a landed cycle owes the operator's morning (a latched stop, a
# refused resume); every other skip is the notice tier.
_RECOVERY_ALERT_SKIPS: Final[frozenset[str]] = frozenset(
    {SKIP_FOREIGN_STANDBY, SKIP_VENDOR_MODE, SKIP_LATCHED_STOP, SKIP_RESUME_REFUSED}
)


def recovery_tier(verdict: str | None) -> str:
    """§11's recovery tier: ``recovered`` is resolved; every outcome is alert
    except the notice-tier skips and the null (not-eligible) verdict."""
    if verdict == RECOVERY_RECOVERED:
        return TIER_RESOLVED
    if verdict is None:
        return TIER_NOTICE
    if verdict.startswith("skipped:"):
        reason = verdict[len("skipped:") :]
        return TIER_ALERT if reason in _RECOVERY_ALERT_SKIPS else TIER_NOTICE
    return TIER_ALERT


def census_tier(stuck_nights: int, persistence_nights: int) -> str:
    """§5/§11's census tier: notice on first occurrence, alert on persistence."""
    return TIER_ALERT if stuck_nights >= persistence_nights else TIER_NOTICE


# --- the per-unit night records ---------------------------------------------------


@dataclass(slots=True)
class _CensusOutcome:
    verdict: str
    tier: str = TIER_NOTICE
    nights: int = 0
    predicates: dict[str, bool] | None = None
    excluded_class: str | None = None
    soc_flagged: bool = False
    samples: int = 0


@dataclass(slots=True)
class _ProbeOutcome:
    verdict: str | None = None
    probe_w: int = 300
    core_samples: int = 0
    qualifying_samples: int = 0
    echo: str | None = None
    baseline_w: float | None = None
    returned_to_baseline: bool | None = None
    final_w: float | None = None
    demand_move_w: float | None = None


@dataclass(slots=True)
class _ProbeLeg:
    """One in-flight probe leg (§6.2's seven steps, one tick at a time).

    ``verification`` marks the §7.2 step-6 re-run: identical steps and
    figures, but its verdict routes to the recovery outcome (never a second
    ``health_probe_completed`` row — that row is per unit per night) and its
    skip/preemption classes end at ``recovered_unproven`` (A2).
    """

    unit_id: str
    step: str = "baseline"  # baseline|settle|sustain|echo|cancel|return
    started_mono: float = 0.0
    step_started_mono: float = 0.0
    baseline_samples: list[float] = field(default_factory=list)
    baseline_w: float | None = None
    start_import_w: float | None = None
    core_samples: list[float] = field(default_factory=list)
    echo: str | None = None
    cancel_import_w: float | None = None
    final_w: float | None = None
    returned: bool = False
    intent_id: str | None = None
    verification: bool = False

    def reset_steps(self, step: str, now_mono: float) -> None:
        self.step = step
        self.step_started_mono = now_mono


@dataclass(slots=True)
class _RecoveryFacts:
    """A11's morning figures, derived from the durable outcome rows."""

    attempts_total: int = 0
    consecutive_fails: int = 0
    trailing_attempted: int = 0
    trailing_recovered: int = 0


@dataclass(slots=True)
class _RecoveryOutcome:
    verdict: str | None
    tier: str = TIER_NOTICE
    posture: str = "advise"
    reason: str | None = None
    rung: str | None = None
    codes: tuple[str, ...] = ()
    cycle: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    left_armed: bool = False
    attempts_total: int = 0
    consecutive_fails: int = 0
    note: str | None = None


@dataclass(slots=True)
class _RecoveryLeg:
    """One in-flight §7.2 composite (the auto posture), one tick at a time.

    The step order is the contract's own and never loops backward — I1's
    structural shape: there is no step that can revisit a mode write, and
    once ``write_attempted`` is set the leg can only END (skip, ladder rung,
    or verdict), never restart.  ``reissued`` is rung 2's ONE re-issue.
    """

    unit_id: str
    step: str = "reverify"  # reverify|disarm|park|hold|resume|rearm|verify|disarm_after
    step_started_mono: float = 0.0
    reissued: bool = False
    write_attempted: bool = False
    park_result: dict[str, Any] | None = None
    resume_result: dict[str, Any] | None = None
    rearm_reason: str | None = None
    verify: _ProbeLeg | None = None
    verify_outcome: _ProbeOutcome | None = None
    verify_reason_codes: tuple[str, ...] = ()
    final_verdict: str | None = None
    final_reason: str | None = None
    final_rung: str | None = None
    final_tier: str = TIER_ALERT

    def advance(self, step: str, now_mono: float) -> None:
        self.step = step
        self.step_started_mono = now_mono


# --- the controller ---------------------------------------------------------------


class HealthWatchController:
    """The nightly program frame plus Stages C, P, and R (§4/§5/§6/§7).

    Ticks once per fleet cycle, fully suppressed by the supervision pass: an
    observability failure is a missed night's evidence, never a delay to
    control.  Holds no transport of its own — the probe's intent submission
    is the one dispatch act, and Stage R's mode writes ride the injected
    ``ParkController`` (the disarm/re-arm ride the facade's internal twins).
    In the ``advise`` posture the recovery stage renders its advisory and
    records: no disarm, no park, no re-arm, ever, on any night, for any unit.
    """

    def __init__(
        self,
        *,
        settings: HealthWatchSettings,
        policy: Any,
        clock: Clock,
        observations: _ObservationsPort,
        intents: _IntentsPort,
        submit: _SubmitPort,
        history: _HistoryPort,
        audit: _AuditPort,
        bus: _BusPort,
        actors: Mapping[str, _ActorEchoPort],
        health_states: Callable[[], Awaitable[Mapping[str, Any]]],
        parked_units: Callable[[], frozenset[str]],
        latched_stop_units: Callable[[], frozenset[str]],
        park_control: _ParkControlPort | None = None,
        disarm: _LifecyclePort | None = None,
        arm: _LifecyclePort | None = None,
        recovery_receipts_missing: tuple[str, ...] = (),
        process_instance_id: str = "",
    ) -> None:
        units = tuple(settings.unit_ids)
        if not units:
            raise ValueError("unit_ids must not be empty")
        if "recovery" in settings.stages and park_control is None:
            # §9 composes the parking primitive with the stage; a staged
            # recovery without its composer is a composition bug, refused
            # loudly here rather than silently incapable.
            raise ValueError("a staged recovery requires its park_control port")
        if (
            "recovery" in settings.stages
            and settings.recovery_mode == "auto"
            and (disarm is None or arm is None)
        ):
            # §7.2's composite needs both facade twins; the ``advise``
            # posture contains none of the sequence, so its ports may stay
            # unwired (structurally: the sequence is the auto path's own
            # code, never entered).
            raise ValueError("the auto recovery posture requires its disarm and arm ports")
        self._settings = settings
        self._policy = policy
        self._clock = clock
        self._observations = observations
        self._intents = intents
        self._submit = submit
        self._history = history
        self._audit = audit
        self._bus = bus
        self._actors = dict(actors)
        self._health_states = health_states
        self._parked_units = parked_units
        self._latched_stop_units = latched_stop_units
        self._park_control = park_control
        self._disarm_port = disarm
        self._arm_port = arm
        self._receipts_missing = tuple(recovery_receipts_missing)
        self._process_instance_id = process_instance_id
        self._zone = ZoneInfo(settings.timezone)
        # Per-night state (reset on civil-night rollover; rows are the truth).
        self._night: date | None = None
        self._phase: Phase = "await_window"
        self._program_reason: str | None = None
        self._census: dict[str, _CensusOutcome] = {}
        self._probe: dict[str, _ProbeOutcome] = {}
        self._probe_order: tuple[str, ...] = ()
        self._probe_index = 0
        self._leg: _ProbeLeg | None = None
        self._gap_until_mono: float | None = None
        self._rows_checked = False
        self._recovery: dict[str, _RecoveryOutcome] = {}
        self._recovery_order: tuple[str, ...] = ()
        self._recovery_index = 0
        self._recovery_leg: _RecoveryLeg | None = None
        self._recovery_facts: dict[str, _RecoveryFacts] = {}

    # --- the fleet-loop tick ---------------------------------------------------

    async def tick(self) -> None:
        """Advance the per-night phase machine by one bounded step."""
        try:
            await self._tick()
        except Exception as error:
            # Survived but never invisible (the historian's own envelope).
            print(f"SUPERVISED HEALTH-WATCH TICK FAILURE: {error!r}", flush=True)

    async def _tick(self) -> None:
        wall = self._clock.wall_now()
        local = wall.astimezone(self._zone)
        self._roll_night(local)
        if self._phase == "await_window":
            await self._evaluate_window_open(local, wall)
            return
        if self._phase == "census":
            await self._run_census(wall)
            return
        if self._phase == "probe":
            await self._advance_probe(wall)
            return
        if self._phase == "recovery":
            await self._advance_recovery(wall)
            return
        # record/done: the rows already landed as each verdict resolved, and
        # the program ends done.
        self._phase = "done"

    def _roll_night(self, local: datetime) -> None:
        """Reset the per-night state on civil-night rollover."""
        night = local.date()
        if self._night == night:
            return
        self._night = night
        self._phase = "await_window"
        self._program_reason = None
        self._census = {}
        self._probe = {}
        self._probe_order = ()
        self._probe_index = 0
        self._leg = None
        self._gap_until_mono = None
        self._rows_checked = False
        self._recovery = {}
        self._recovery_order = ()
        self._recovery_index = 0
        self._recovery_leg = None
        self._recovery_facts = {}

    def _second_of_day(self, value: time) -> int:
        return value.hour * 3600 + value.minute * 60 + value.second

    def _in_program_window(self, local: datetime) -> bool:
        return self._second_of_day(local.timetz().replace(tzinfo=None)) >= self._second_of_day(
            self._settings.window_local
        )

    def _past_deadline(self, local: datetime) -> bool:
        return self._second_of_day(local.timetz().replace(tzinfo=None)) >= self._second_of_day(
            self._settings.deadline_local
        )

    async def _evaluate_window_open(self, local: datetime, wall: datetime) -> None:
        """await_window: the once-per-night gate (quiet-window + rows check)."""
        if not self._in_program_window(local):
            return
        # In the window's evening.  The durable-rows check runs once per
        # night: a night with ANY health-watch row is retired (I8/A4) — one
        # crash anywhere retires the night's program unambiguously.
        if not self._rows_checked:
            self._rows_checked = True
            attempts, complete = await self._night_program_state(self._night or local.date())
            if attempts:
                if not complete:
                    await self._record_interrupted(wall, sorted(attempts))
                else:
                    # A completed program earlier tonight (this process or a
                    # predecessor): the night stands done, no re-run, no
                    # false "interrupted" alert.
                    self._phase = "done"
                return
        if self._past_deadline(local):
            # Window opened while the controller was down or deferred; no ACT
            # starts after the deadline (A1) — the night stands down.
            self._phase = "done"
            self._program_reason = REASON_OUTSIDE_WINDOW
            return
        # §4's quiet-window verification: active_schedule empty for the slot,
        # no live intent claims, no adviser participation — else defer to the
        # next night with reason window_not_quiet (never fights, never wedges
        # itself in).
        now_mono = float(self._clock.monotonic())
        busy = await self._any_live_claims(now_mono)
        if busy:
            self._phase = "done"
            self._program_reason = REASON_WINDOW_NOT_QUIET
            await self._program_row(
                wall,
                REASON_WINDOW_NOT_QUIET,
                reason_codes=("window_not_quiet",),
                tier=TIER_NOTICE,
                payload={"claims": sorted(busy)},
            )
            return
        self._phase = "census"

    # --- Stage C: the census (§5) ------------------------------------------------

    async def _run_census(self, wall: datetime) -> None:
        """One evaluation per unit over the trailing evidence window."""
        settings = self._settings
        window_s = settings.stuck.evidence_window_h * 3600.0
        from_at = wall.astimezone(UTC) - timedelta(seconds=window_s)
        to_at = wall.astimezone(UTC)
        rows: tuple[Any, ...] = ()
        with contextlib.suppress(Exception):
            rows = tuple(self._history.samples(settings.unit_ids, from_at, to_at))
        latest = await self._observations.all_latest()
        health = await self._health_states()
        parked = self._parked_view()
        stopped = self._latched_view()
        socs = self._fleet_socs(latest)
        streaks = await self._stuck_streaks(self._night or wall.date())
        if "recovery" in settings.stages:
            # A11's morning figures derive from the durable outcome rows;
            # computed here so the projection carries them from the window's
            # first rendering, refreshed as tonight's outcomes land.
            self._recovery_facts = await self._derive_recovery_facts(self._night or wall.date())
        for unit_id in settings.unit_ids:
            outcome = self._census_unit(
                unit_id,
                rows=rows,
                latest=latest.get(unit_id),
                health_view=health.get(unit_id),
                parked=unit_id in parked,
                stopped=unit_id in stopped,
                fleet_socs=socs,
                streak=streaks.get(unit_id, 0),
                now_mono=float(self._clock.monotonic()),
            )
            self._census[unit_id] = outcome
            await self._record_census_row(wall, unit_id, outcome)
        if "probe" in settings.stages:
            # §6.1: fixed sorted order, strictly one at a time.
            self._probe_order = tuple(
                unit_id
                for unit_id in sorted(settings.unit_ids)
                if self._census[unit_id].verdict in {CENSUS_NOMINAL, CENSUS_STUCK}
            )
            self._probe_index = 0
            self._phase = "probe"
        elif "recovery" in settings.stages:
            # Unreachable under the strict-prefix rule (recovery stages probe)
            # but honest if the vocabulary ever widens: the recovery phase
            # runs its own eligibility gate, which will find no probe rows.
            self._phase = "recovery"
        else:
            self._phase = "done"

    def _census_unit(
        self,
        unit_id: str,
        *,
        rows: Sequence[Any],
        latest: Any,
        health_view: Any,
        parked: bool,
        stopped: bool,
        fleet_socs: Mapping[str, float],
        streak: int,
        now_mono: float,
    ) -> _CensusOutcome:
        stuck = self._settings.stuck
        excluded = self._census_excluded_class(
            latest,
            health_view,
            parked=parked,
            stopped=stopped,
            now_mono=now_mono,
            max_age_s=float(self._policy.max_telemetry_age_s),
        )
        if excluded is not None:
            return _CensusOutcome(verdict=f"excluded:{excluded}", excluded_class=excluded)
        figures, degraded = self._evidence_figures(unit_id, rows)
        predicates = stuck_predicates(figures, stuck)
        verdict = census_verdict(predicates, degraded=degraded)
        soc = _finite(getattr(latest, "authoritative_soc_pct", None))
        flagged = False
        if soc is not None:
            peers = [value for other, value in fleet_socs.items() if other != unit_id]
            if peers and max(abs(soc - peer) for peer in peers) > float(
                self._policy.max_soc_disagreement_pct
            ):
                flagged = True
        nights = streak + 1 if verdict == CENSUS_STUCK else 0
        return _CensusOutcome(
            verdict=verdict,
            tier=census_tier(nights, stuck.flag_persistence_nights),
            nights=nights,
            predicates=None if verdict != CENSUS_STUCK else dict(predicates),
            soc_flagged=flagged,
            samples=figures.samples,
        )

    @staticmethod
    def _census_excluded_class(
        latest: Any,
        health_view: Any,
        *,
        parked: bool,
        stopped: bool,
        now_mono: float,
        max_age_s: float,
    ) -> str | None:
        """§5's excluded:<class> set — named, owned elsewhere, never re-classified.

        The health ladder's precedence (unreachable > not_responding >
        foreign_writer > inhibited) decides between ladder states; the lease
        ledger and the debug word decide parked; telemetry freshness is the
        readiness check's own bound.
        """
        state = getattr(health_view, "state", None)
        state_word = _enum_text(state) if state is not None else ""
        if state_word == "unreachable":
            return "unreachable"
        if state_word == "not_responding":
            return "not_responding"
        if stopped:
            return "latched_stop"
        if parked:
            return "parked"
        word = _word(getattr(latest, "debug_mode_w", None))
        if word is not None and word in {2, 3, 4, 5, 6}:
            return "vendor_mode"
        if word == 1:
            return "parked"
        if state_word == "foreign_writer":
            return "foreign_writer"
        if state_word in {"inhibited", "actuation_incoherent"}:
            return "inhibited"
        captured = _finite(getattr(latest, "captured_at_mono", None))
        if latest is None or captured is None or (now_mono - captured > max_age_s):
            return "telemetry_stale"
        return None

    def _evidence_figures(self, unit_id: str, rows: Sequence[Any]) -> tuple[CensusFigures, bool]:
        """§5's predicate figures over the historian rows (S1-S5).

        The fleet predicates (S3/S4) join per timestamp: a sample where the
        unit or every sibling lacks a row is EXCLUDED from that predicate's
        own denominator — never interpolated, never zero-filled.
        """
        stuck = self._settings.stuck
        fleet_ids = frozenset(self._settings.unit_ids)
        by_timestamp: dict[Any, dict[str, Any]] = {}
        unit_rows: list[Any] = []
        for row in rows:
            holder = str(getattr(row, "unit_id", ""))
            if holder == unit_id:
                unit_rows.append(row)
            by_timestamp.setdefault(getattr(row, "sampled_at", None), {})[holder] = row
        unit_rows.sort(key=lambda row: str(getattr(row, "sampled_at", "")))
        expected = max(
            1.0, stuck.evidence_window_h * 3600.0 / max(1.0, self._settings.sample_interval_s)
        )
        usable = 0
        soc_full = 0
        still = 0
        modes_normal = True
        longest_run_s = 0.0
        run_start: Any = None
        house_needed = 0
        flow_denom = 0
        no_ct_view = 0
        ct_denom = 0
        for row in unit_rows:
            soc = _finite(getattr(row, "bms_soc_pct", None))
            if soc is None:
                soc = _finite(getattr(row, "system_soc_pct", None))
            watts = _finite(getattr(row, "battery_watts", None))
            word = _word(getattr(row, "debug_mode_w", None))
            sampled_at = getattr(row, "sampled_at", None)
            if soc is None or watts is None or word is None:
                continue  # an excluded sample, never an interpolated one
            usable += 1
            if word != 0:
                modes_normal = False
            if soc >= stuck.soc_floor_pct:
                soc_full += 1
                if run_start is None:
                    run_start = sampled_at
                if sampled_at is not None:
                    # A sample COVERS its interval (the historian's own
                    # integration discipline), so the run's span counts its
                    # last interval too; the comparison then carries one
                    # interval of tolerance for the window-edge sample (the
                    # equality pin lives with ``CensusFigures``).
                    interval = self._settings.sample_interval_s
                    span = (sampled_at - run_start).total_seconds() + interval
                    longest_run_s = max(longest_run_s, span)
            else:
                run_start = None
            if abs(watts) < stuck.still_w:
                still += 1
            # S3/S4: the fleet join at this sample's own timestamp.
            fleet = by_timestamp.get(sampled_at, {}) if sampled_at is not None else {}
            siblings = [fleet[other] for other in fleet if other != unit_id and other in fleet_ids]
            imports = [
                max(0.0, -grid)
                for grid in (_finite(getattr(holder, "grid_power_w", None))
                             for holder in fleet.values())
                if grid is not None
            ]
            if len(fleet) == len(fleet_ids) and len(imports) == len(fleet):
                mean_import = sum(imports) / len(imports)
                sibling_flow = any(
                    abs(_finite(getattr(other, "battery_watts", None)) or 0.0)
                    > stuck.sibling_flow_w
                    for other in siblings
                )
                flow_denom += 1
                if mean_import > stuck.grid_import_w or sibling_flow:
                    house_needed += 1
            unit_load = _finite(getattr(row, "load_power_w", None))
            if unit_load is not None and siblings:
                sibling_load = any(
                    (_finite(getattr(other, "load_power_w", None)) or 0.0) > stuck.sibling_load_w
                    for other in siblings
                )
                ct_denom += 1
                if sibling_load and unit_load < stuck.load_floor_w:
                    no_ct_view += 1
        degraded = usable < _EVIDENCE_COVERAGE_FLOOR * expected
        figures = CensusFigures(
            samples=usable,
            sample_interval_s=float(self._settings.sample_interval_s),
            soc_full_frac=soc_full / usable if usable else 0.0,
            soc_full_longest_run_s=longest_run_s,
            still_frac=still / usable if usable else 0.0,
            house_needed_frac=house_needed / flow_denom if flow_denom else 0.0,
            no_ct_view_frac=no_ct_view / ct_denom if ct_denom else 0.0,
            modes_normal=modes_normal,
        )
        return figures, degraded

    @staticmethod
    def _fleet_socs(latest: Mapping[str, Any]) -> dict[str, float]:
        socs: dict[str, float] = {}
        for unit_id, observation in latest.items():
            soc = _finite(getattr(observation, "authoritative_soc_pct", None))
            if soc is not None:
                socs[str(unit_id)] = soc
        return socs

    # --- Stage P: the probe (§6) --------------------------------------------------

    async def _advance_probe(self, wall: datetime) -> None:
        settings = self._settings
        now_mono = float(self._clock.monotonic())
        local = wall.astimezone(self._zone)
        # Units the census excluded or degraded render their honest non-probe
        # verdicts (I11: every skip is a recorded verdict, never silence).
        for unit_id in sorted(settings.unit_ids):
            if unit_id in self._probe:
                continue
            census = self._census.get(unit_id)
            if census is None:
                continue
            if census.verdict not in {CENSUS_NOMINAL, CENSUS_STUCK}:
                reason = (
                    SKIP_CENSUS_DEGRADED
                    if census.verdict == CENSUS_DEGRADED
                    else SKIP_CENSUS_EXCLUDED
                )
                outcome = _ProbeOutcome(verdict=f"skipped:{reason}")
                self._probe[unit_id] = outcome
                await self._record_probe_row(wall, unit_id, outcome, (reason,))
        if self._gap_until_mono is not None and now_mono < self._gap_until_mono:
            return
        self._gap_until_mono = None
        if self._leg is None:
            if self._probe_index >= len(self._probe_order):
                # The probes are done; the recovery stage (when commissioned)
                # takes the night from here over the SAME rows.
                self._phase = "recovery" if "recovery" in settings.stages else "done"
                return
            unit_id = self._probe_order[self._probe_index]
            if self._past_deadline(local):
                # I2: no ACT starts outside [window, deadline] — the night's
                # remaining probes are skipped, never started late.
                outcome = _ProbeOutcome(verdict=f"skipped:{SKIP_DEADLINE_PASSED}")
                self._probe[unit_id] = outcome
                await self._record_probe_row(wall, unit_id, outcome, (SKIP_DEADLINE_PASSED,))
                self._probe_index += 1
                return
            await self._start_leg(wall, unit_id, now_mono)
            return
        await self._advance_leg(wall, now_mono)

    async def _start_leg(self, wall: datetime, unit_id: str, now_mono: float) -> None:
        """§6.1's gate walk then §6.2 step 1 (the pre-probe baseline opens)."""
        latest = await self._observations.all_latest()
        active = await self._intents.active(now_mono)
        health = await self._health_states()
        parked = self._parked_view()
        stopped = self._latched_view()
        quiet, import_w = self._quiet_gate(latest)
        reason = self._probe_skip_reason(
            unit_id,
            latest.get(unit_id),
            active=active,
            health_view=health.get(unit_id),
            parked=unit_id in parked,
            stopped=unit_id in stopped,
            quiet_ok=quiet is True,
            quiet_stale=quiet is None,
            now_mono=now_mono,
        )
        if reason is not None:
            outcome = _ProbeOutcome(verdict=f"skipped:{reason}")
            self._probe[unit_id] = outcome
            await self._record_probe_row(wall, unit_id, outcome, (reason,))
            self._probe_index += 1
            self._gap_until_mono = now_mono + self._settings.probe.inter_unit_gap_s
            return
        leg = _ProbeLeg(unit_id=unit_id)
        leg.started_mono = now_mono
        leg.step_started_mono = now_mono
        leg.start_import_w = import_w
        self._leg = leg

    def _probe_skip_reason(
        self,
        unit_id: str,
        latest: Any,
        *,
        active: Sequence[Any],
        health_view: Any,
        parked: bool,
        stopped: bool,
        quiet_ok: bool,
        quiet_stale: bool,
        now_mono: float,
    ) -> str | None:
        """§4/§6.1's skip-if set, one honest reason per unit.

        The pinned order: the states another surface owns first (parked,
        vendor mode, latched stop, ladder faults), then telemetry, then
        single-writer claims, then arm (the program NEVER arms — a disarmed
        unit is an honest skip, counted on the projection), then the
        quiet-load gate (A8) with its fail-closed stale-evidence skip.
        """
        if parked:
            return SKIP_UNIT_PARKED
        word = _word(getattr(latest, "debug_mode_w", None))
        if word is not None and word in {2, 3, 4, 5, 6}:
            return SKIP_VENDOR_MODE
        if stopped:
            return SKIP_LATCHED_STOP
        state = getattr(health_view, "state", None)
        state_word = _enum_text(state) if state is not None else ""
        if state_word == "unreachable":
            return SKIP_UNREACHABLE
        if state_word == "not_responding":
            return SKIP_NOT_RESPONDING
        if state_word == "foreign_writer":
            return SKIP_FOREIGN_WRITER
        if state_word in {"inhibited", "actuation_incoherent"}:
            return SKIP_INHIBITED
        captured = _finite(getattr(latest, "captured_at_mono", None))
        if latest is None or captured is None or (
            now_mono - captured > float(self._policy.max_telemetry_age_s)
        ):
            return SKIP_TELEMETRY_STALE
        claimed = self._claiming_units(active, exclude_prefix=_OWN_INTENT_PREFIX)
        if unit_id in claimed:
            return SKIP_UNDER_INTENT
        lifecycle = _enum_text(getattr(latest, "lifecycle", None))
        if lifecycle not in {"armed_idle", "active"}:
            return SKIP_UNIT_DISARMED
        if quiet_stale:
            return SKIP_QUIET_EVIDENCE_STALE
        if not quiet_ok:
            return SKIP_QUIET_LOAD_GATE
        return None

    def _quiet_gate(self, latest: Mapping[str, Any]) -> tuple[bool | None, float | None]:
        """A8's quiet-load gate on the fleet-mean grid IMPORT magnitude.

        The control-grade PCS word (``grid_power_w``, 0x1000+17 — negative =
        import), NEVER the load-CT mean (a dead CT biases it low, and it is
        per-phase load, not house draw).  Any unit's word missing or beyond
        telemetry age makes the gate UNJUDGEABLE (None): the probes skip with
        ``quiet_evidence_stale`` — never run on bad evidence.
        """
        now_mono = float(self._clock.monotonic())
        imports: list[float] = []
        for unit_id in self._settings.unit_ids:
            observation = latest.get(unit_id)
            grid = _finite(getattr(observation, "grid_power_w", None)) if observation else None
            captured = (
                _finite(getattr(observation, "captured_at_mono", None)) if observation else None
            )
            if grid is None or captured is None or (
                now_mono - captured > float(self._policy.max_telemetry_age_s)
            ):
                return (None, None)
            imports.append(max(0.0, -grid))
        mean_import = sum(imports) / len(imports)
        return (mean_import < float(self._settings.probe.quiet_load_w), mean_import)

    def _claiming_units(
        self, active: Sequence[Any], *, exclude_prefix: str | None = None
    ) -> frozenset[str]:
        """Units named by live intents; own-prefixed intents excluded."""
        claimed: set[str] = set()
        for intent in active:
            intent_id = str(getattr(intent, "id", "") or "")
            if exclude_prefix is not None and intent_id.startswith(exclude_prefix):
                continue
            for unit_id in getattr(intent, "selected_unit_ids", ()) or ():
                claimed.add(str(unit_id))
        return frozenset(claimed)

    def _preempted_by(self, active: Sequence[Any], unit_id: str) -> str | None:
        """A12's preemption test: MANUAL / AGENT / emergency-stop claims only.

        A SCHEDULE claim does NOT preempt (``optimizer`` outranks it in the
        standing order; the quiet-window check already deferred a night with
        live claims).
        """
        for intent in active:
            source = _enum_text(getattr(intent, "source", None))
            if source not in _PREEMPTING_SOURCES:
                continue
            if unit_id in {str(unit) for unit in getattr(intent, "selected_unit_ids", ()) or ()}:
                return source
        return None

    async def _advance_leg(self, wall: datetime, now_mono: float) -> None:
        leg = self._leg
        assert leg is not None
        probe = self._settings.probe
        latest = await self._observations.all_latest()
        observation = latest.get(leg.unit_id)
        measured = _finite(getattr(observation, "battery_watts", None))
        # §6.2 step 7: a MANUAL or AGENT claim arriving mid-probe preempts it
        # instantly, as does the emergency stop — never a fail.  A schedule
        # claim does not (A12).
        active = await self._intents.active(now_mono)
        preemption = self._preempted_by(active, leg.unit_id)
        if leg.step in {"baseline", "settle", "sustain", "return"} and preemption is not None:
            await self._cancel_intent(leg)
            await self._finish_leg(
                wall,
                leg,
                _ProbeOutcome(verdict=PROBE_INCONCLUSIVE_PREEMPTED, probe_w=probe.probe_w),
                reason_codes=(f"preempted_by_{preemption}",),
            )
            return
        elapsed = now_mono - leg.step_started_mono
        if leg.step == "baseline":
            if measured is not None:
                leg.baseline_samples.append(measured)
            if elapsed >= probe.settle_s:
                if not leg.baseline_samples:
                    await self._finish_leg(
                        wall,
                        leg,
                        _ProbeOutcome(verdict=PROBE_INCONCLUSIVE_ABORTED, probe_w=probe.probe_w),
                        reason_codes=("no_baseline_evidence",),
                    )
                    return
                leg.baseline_w = sum(leg.baseline_samples) / len(leg.baseline_samples)
                if not await self._renew_intent(leg):
                    await self._refused_leg(wall, leg)
                    return
                leg.reset_steps("settle", now_mono)
            return
        if leg.step == "settle":
            if not await self._renew_intent(leg):
                await self._refused_leg(wall, leg)
                return
            if elapsed >= probe.settle_s:
                leg.reset_steps("sustain", now_mono)
            return
        if leg.step == "sustain":
            if not await self._renew_intent(leg):
                await self._refused_leg(wall, leg)
                return
            if measured is not None:
                leg.core_samples.append(measured)
            if elapsed >= probe.sustain_s:
                leg.reset_steps("echo", now_mono)
            return
        if leg.step == "echo":
            # §6.2 step 5: ONE bounded objective-echo read at probe end (the
            # existing discriminator; one read per probe, the episode budget).
            handle = self._actors.get(leg.unit_id)
            classification = ECHO_UNREADABLE
            if handle is not None:
                with contextlib.suppress(Exception):
                    classification = (await handle.read_objective_echo())[0]
            leg.echo = classification
            # §6.2 step 6: CANCEL — the deliberate end through the intent
            # path (never mere non-renewal), then the quiet gate re-check.
            await self._cancel_intent(leg)
            quiet, import_w = self._quiet_gate(latest)
            if quiet is None:
                await self._finish_leg(
                    wall,
                    leg,
                    _ProbeOutcome(verdict=PROBE_INCONCLUSIVE_ABORTED, probe_w=probe.probe_w),
                    reason_codes=(SKIP_QUIET_EVIDENCE_STALE,),
                )
                return
            leg.cancel_import_w = import_w
            leg.reset_steps("return", now_mono)
            return
        if leg.step == "return":
            band = float(probe.return_band_w)
            if measured is not None:
                leg.final_w = measured
                if leg.baseline_w is not None and abs(measured - leg.baseline_w) <= band:
                    leg.returned = True
            if leg.returned or elapsed >= probe.baseline_return_s:
                await self._finish_leg(wall, leg, self._leg_outcome(leg), reason_codes=())
            return

    def _leg_outcome(self, leg: _ProbeLeg) -> _ProbeOutcome:
        """§6.3's matrix over the leg's own figures."""
        probe = self._settings.probe
        measured, qualifying = measured_class(
            leg.core_samples,
            probe_w=probe.probe_w,
            pass_fraction=probe.pass_fraction,
            pass_sample_frac=probe.pass_sample_frac,
            still_w=self._settings.stuck.still_w,
        )
        demand_move: float | None = None
        if leg.start_import_w is not None and leg.cancel_import_w is not None:
            demand_move = abs(leg.cancel_import_w - leg.start_import_w)
        verdict = probe_verdict(
            measured=measured,
            echo=leg.echo or ECHO_UNREADABLE,
            baseline_returned=leg.returned,
            demand_move_confounded=(
                demand_move is not None and demand_move > float(probe.load_move_w)
            ),
        )
        return _ProbeOutcome(
            verdict=verdict,
            probe_w=probe.probe_w,
            core_samples=len(leg.core_samples),
            qualifying_samples=qualifying,
            echo=leg.echo,
            baseline_w=leg.baseline_w,
            returned_to_baseline=leg.returned,
            final_w=leg.final_w,
            demand_move_w=demand_move,
        )

    async def _renew_intent(self, leg: _ProbeLeg) -> bool:
        """Renew the probe's one intent remove-then-submit (§6.2 step 2).

        Returns False when the submission itself was refused — §2's doctrine:
        every standing guard (SoC floor, cell bounds, imbalance veto,
        telemetry staleness, foreign writer, park) judges the probe through
        the ordinary dispatch path, and a guard that refuses it makes a SKIP,
        never a failure of the battery.
        """
        await self._cancel_intent(leg)
        try:
            result = await self._submit(
                unit_ids=[leg.unit_id],
                direction=Direction.DISCHARGE,
                watts=int(self._settings.probe.probe_w),
                ttl_s=float(self._settings.intent_ttl_s),
            )
        except Exception:
            leg.intent_id = None
            return False
        submitted = result.get("intent_id") if isinstance(result, Mapping) else None
        leg.intent_id = submitted if isinstance(submitted, str) else None
        return leg.intent_id is not None

    async def _refused_leg(self, wall: datetime, leg: _ProbeLeg) -> None:
        """A guard refused the probe's own dispatch: an honest skip verdict."""
        await self._finish_leg(
            wall,
            leg,
            _ProbeOutcome(
                verdict=f"skipped:{SKIP_DISPATCH_REFUSED}", probe_w=self._settings.probe.probe_w
            ),
            reason_codes=(SKIP_DISPATCH_REFUSED,),
        )

    async def _cancel_intent(self, leg: _ProbeLeg) -> None:
        """The deliberate end (§6.2 step 6): withdraw through the intent path.

        Removing the probe's own intent hands the kernel zero authority at
        once, so the actor's standing heartbeat delivers the bounded zero —
        the probe ends deliberately, never by TTL drift.
        """
        held = leg.intent_id
        leg.intent_id = None
        if held is None:
            return
        with contextlib.suppress(Exception):
            await self._intents.remove(held)

    async def _finish_leg(
        self,
        wall: datetime,
        leg: _ProbeLeg,
        outcome: _ProbeOutcome,
        *,
        reason_codes: tuple[str, ...],
    ) -> None:
        """Record the verdict row/event, then the inter-unit gap (§6.1)."""
        if leg.verification:
            # §7.2 step 6: the re-run's verdict is the recovery outcome's own
            # verification half — never a second ``health_probe_completed``
            # row (that row is per unit per night), never a probe-order step.
            await self._complete_verification(wall, leg, outcome, reason_codes=reason_codes)
            return
        self._probe[leg.unit_id] = outcome
        codes: tuple[str, ...] = (
            reason_codes if reason_codes else (outcome.verdict or "recorded",)
        )
        await self._record_probe_row(wall, leg.unit_id, outcome, codes)
        self._probe_index += 1
        self._leg = None
        gap_s = self._settings.probe.inter_unit_gap_s
        self._gap_until_mono = float(self._clock.monotonic()) + gap_s

    async def _complete_verification(
        self,
        wall: datetime,
        leg: _ProbeLeg,
        outcome: _ProbeOutcome,
        *,
        reason_codes: tuple[str, ...],
    ) -> None:
        """Route one finished verification re-run to its recovery verdict.

        §8 rungs 4/5 (A2): a FAILING re-run is ``failed_no_effect`` (counted
        to the cap); an INCONCLUSIVE re-run — preemption, guard refusal,
        telemetry loss, a skipped leg — is ``recovered_unproven`` (NOT
        counted: the cycle plausibly worked, the proof is missing).  The
        composite then runs its final disarm step whatever the verdict: the
        sequence is authority-net-zero (I9).
        """
        recovery = self._recovery_leg
        if recovery is None or recovery.unit_id != leg.unit_id:
            return  # pragma: no cover - a verification leg without its composite
        self._leg = None  # the verification leg is finished; the composite continues
        recovery.verify_outcome = outcome
        if outcome.verdict == PROBE_PASS:
            recovery.final_verdict = RECOVERY_RECOVERED
            recovery.final_rung = RUNG_VERIFIED
            recovery.final_tier = TIER_RESOLVED
        elif (outcome.verdict or "").startswith("fail"):
            recovery.final_verdict = RECOVERY_FAILED_NO_EFFECT
            recovery.final_rung = RUNG_VERIFICATION_FAILED
            recovery.final_tier = TIER_ALERT
        else:
            recovery.final_verdict = RECOVERY_RECOVERED_UNPROVEN
            recovery.final_rung = RUNG_VERIFICATION_INCONCLUSIVE
            recovery.final_tier = TIER_ALERT
            recovery.final_reason = outcome.verdict or REASON_VERIFICATION_INCONCLUSIVE
        recovery.verify_reason_codes = reason_codes
        recovery.advance("disarm_after", float(self._clock.monotonic()))

    # --- Stage R: recovery (§7) ----------------------------------------------------

    def _recovery_posture(self, unit_id: str) -> str:
        """The unit's EFFECTIVE posture: advise, or auto minus A6's degrade."""
        if (
            self._settings.recovery_mode == "auto"
            and unit_id not in self._receipts_missing
        ):
            return "auto"
        return "advise"

    def _recovery_eligible(self, unit_id: str) -> bool:
        """§7.1's conjunction, deliberately conservative.

        Census ``stuck_suspected`` AND that unit's probe ``fail_no_response``
        — the vendor's own two-condition rule made structural.  A probe
        failure without a census flag, a census flag without the probe
        failure, a skipped or inconclusive probe: all NOT eligible (the
        evidence is incomplete, and an incomplete case never mints a mode
        write).  Per I10, no ladder state is consulted at all.
        """
        census = self._census.get(unit_id)
        probe = self._probe.get(unit_id)
        return (
            census is not None
            and census.verdict == CENSUS_STUCK
            and probe is not None
            and probe.verdict == PROBE_FAIL_NO_RESPONSE
        )

    async def _advance_recovery(self, wall: datetime) -> None:
        """The recovery phase: record every unit's honest outcome, cycle the
        eligible ones strictly sequentially (§7.2)."""
        settings = self._settings
        now_mono = float(self._clock.monotonic())
        for unit_id in sorted(settings.unit_ids):
            if unit_id in self._recovery:
                continue
            census = self._census.get(unit_id)
            probe = self._probe.get(unit_id)
            if census is None or probe is None:
                continue
            eligible = self._recovery_eligible(unit_id)
            posture = self._recovery_posture(unit_id)
            if not eligible:
                # I11: never silence — the basis rides the row, verdict null
                # (the §10 projection's own "not eligible" shape).
                await self._record_recovery_outcome(
                    wall,
                    unit_id,
                    _RecoveryOutcome(verdict=None, posture=posture),
                )
            elif posture == "advise":
                # §7.2's advise posture: a REAL posture, not a stub — the
                # ALERT-tier advisory naming the exact operator sequence, and
                # what the operator does through the standing surfaces is the
                # night's recovery evidence.  Steps 2-6 do not run.
                await self._record_recovery_outcome(
                    wall,
                    unit_id,
                    _RecoveryOutcome(
                        verdict=RECOVERY_ADVISED,
                        tier=TIER_ALERT,
                        posture=posture,
                        reason=ADVISE_WALKTHROUGH,
                    ),
                )
            elif (
                self._recovery_facts.get(unit_id, _RecoveryFacts()).consecutive_fails
                >= settings.recovery.consecutive_fail_limit
            ):
                # §7.3: the cap's stand-down — advisory-only for this unit
                # until the operator acknowledgement resets it (the streak
                # resets when the unit's own evidence clears: a night whose
                # outcome row is not a counted failure, the probe-pass morning
                # included).
                await self._record_recovery_outcome(
                    wall,
                    unit_id,
                    _RecoveryOutcome(
                        verdict=RECOVERY_ADVISORY_ONLY,
                        tier=TIER_ALERT,
                        posture=posture,
                        reason="consecutive_fail_limit reached — advisory-only until acknowledged",
                    ),
                )
            else:
                # I1's structural shape: a unit joins the night's cycle order
                # exactly once — while its composite is in flight (and after
                # it ends) the guard above plus this membership check keep a
                # second leg from ever being queued.
                if unit_id not in self._recovery_order:
                    self._recovery_order += (unit_id,)
        if self._recovery_leg is None:
            if self._recovery_index >= len(self._recovery_order):
                self._phase = "done"
                return
            unit_id = self._recovery_order[self._recovery_index]
            leg = _RecoveryLeg(unit_id=unit_id)
            leg.step_started_mono = now_mono
            self._recovery_leg = leg
            return
        local = wall.astimezone(self._zone)
        await self._advance_recovery_leg(wall, self._recovery_leg, now_mono, local)

    async def _advance_recovery_leg(
        self, wall: datetime, leg: _RecoveryLeg, now_mono: float, local: datetime
    ) -> None:
        """One bounded step of the §7.2 composite (the auto posture)."""
        unit_id = leg.unit_id
        # I7: the emergency stop is supreme over the program — a latched stop
        # naming the unit ends the sequence with NO further write, whatever
        # the step; an in-flight lease follows the standing expiry rules.
        if unit_id in self._latched_view():
            await self._finish_recovery(
                wall,
                leg,
                verdict=f"skipped:{SKIP_LATCHED_STOP}",
                tier=TIER_ALERT,
                reason_codes=("emergency_stop", "no_further_program_write"),
                rung=None,
            )
            return
        if leg.step == "reverify":
            reason = await self._recovery_skip_reason(unit_id, now_mono)
            if reason is not None:
                alert = reason in {SKIP_FOREIGN_STANDBY, SKIP_VENDOR_MODE}
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=f"skipped:{reason}",
                    tier=TIER_ALERT if alert else TIER_NOTICE,
                    reason_codes=(reason,),
                )
                return
            if self._past_deadline(local):
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=f"skipped:{SKIP_DEADLINE_PASSED}",
                    tier=TIER_NOTICE,
                    reason_codes=(SKIP_DEADLINE_PASSED,),
                )
                return
            leg.advance("disarm", now_mono)
            return
        if leg.step == "disarm":
            # §7.2 step 2: the standing facade disarm under the health
            # principal — a stop-direction act (the emergency-stop family,
            # fail-safe by construction) so PARK'S CONFLICT GUARD NEVER NEEDS
            # WEAKENING.
            outcome = await self._disarm_unit(unit_id=unit_id)
            if outcome.get("status") != "disarmed":
                # No cycle was attempted (the park never ran) — a guard-class
                # skip, never a battery failure and never a counted outcome.
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=f"skipped:{SKIP_DISARM_REFUSED}",
                    tier=TIER_NOTICE,
                    reason_codes=(SKIP_DISARM_REFUSED, str(outcome.get("reason", ""))),
                )
                return
            leg.advance("park", now_mono)
            return
        if leg.step == "park":
            leg.write_attempted = True
            control = self._park_control
            assert control is not None  # the construction guard is the call site's
            try:
                result = await control.park(
                    unit_id,
                    reason=(
                        "nightly health-watch recovery (census flag + probe no-response)"
                    ),
                    principal_subject=HEALTH_ADVISER_PRINCIPAL,
                    request_id=f"health-recovery:{unit_id}:{self._night}",
                    lease_s=self._settings.recovery.hold_s + 60,
                )
            except ParkingRefusal as refusal:
                handled = await self._recovery_park_refusal(wall, leg, refusal)
                if handled:
                    return
                leg.advance("park", now_mono)  # rung 2's ONE re-issue
                return
            except ValueError:
                # lease_s outside the parking block's commissioned bounds.
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=f"skipped:{SKIP_LEASE_BOUNDS}",
                    tier=TIER_NOTICE,
                    reason_codes=(SKIP_LEASE_BOUNDS,),
                )
                return
            leg.park_result = self._cycle_excerpt(result)
            leg.advance("hold", now_mono)
            return
        if leg.step == "hold":
            # §7.2 step 4: the hold is bounded policy, NEVER safety — comms,
            # telemetry and SoC stay alive (live-proven), and if comms die the
            # STANDING rules own it (the lease expires alarm-only, no write).
            if now_mono - leg.step_started_mono >= float(self._settings.recovery.hold_s):
                leg.advance("resume", now_mono)
            return
        if leg.step == "resume":
            control = self._park_control
            assert control is not None  # the construction guard is the call site's
            try:
                result = await control.resume(
                    unit_id,
                    principal_subject=HEALTH_ADVISER_PRINCIPAL,
                    request_id=f"health-recovery:{unit_id}:{self._night}",
                )
            except ParkingRefusal as refusal:
                handled = await self._recovery_resume_refusal(wall, leg, refusal)
                if handled:
                    return
                leg.advance("resume", now_mono)  # rung 2's ONE re-issue
                return
            leg.resume_result = self._cycle_excerpt(result)
            leg.advance("rearm", now_mono)
            return
        if leg.step == "rearm":
            # §7.2 step 6 / §7.4: the ONE bounded verification re-arm — the
            # only automation arm authority this controller ever composes
            # (panel ruling 1).  The verification act must START before the
            # deadline (A1); a refused re-arm (a foreign objective during the
            # park makes the next arm the operator's takeover acknowledgement,
            # a latched stop names the unit) is A2's honest terminal.
            if self._past_deadline(local):
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=RECOVERY_RECOVERED_UNPROVEN,
                    tier=TIER_ALERT,
                    reason_codes=(REASON_DEADLINE_PASSED_VERIFICATION,),
                    rung=RUNG_VERIFICATION_INCONCLUSIVE,
                    reason=REASON_DEADLINE_PASSED_VERIFICATION,
                )
                return
            outcome = await self._arm_unit(unit_id=unit_id)
            if outcome.get("status") != "armed":
                leg.rearm_reason = str(outcome.get("reason", "refused"))
                await self._finish_recovery(
                    wall,
                    leg,
                    verdict=RECOVERY_RECOVERED_UNPROVEN,
                    tier=TIER_ALERT,
                    reason_codes=(SKIP_REARM_REFUSED, leg.rearm_reason),
                    rung=RUNG_REARM_REFUSED,
                    reason=leg.rearm_reason,
                )
                return
            leg.advance("verify", now_mono)
            return
        if leg.step == "verify":
            await self._advance_verify(wall, leg, now_mono)
            return
        if leg.step == "disarm_after":
            # The composite's last step whatever the verdict: authority-net-
            # zero (I9) — the unit ends disarmed-and-Normal, or the program
            # alerts that it could not (a refusal never re-attempts).
            outcome = await self._disarm_unit(unit_id=unit_id)
            left_armed = outcome.get("status") != "disarmed"
            await self._finish_recovery(
                wall,
                leg,
                verdict=leg.final_verdict or RECOVERY_RECOVERED_UNPROVEN,
                tier=leg.final_tier,
                reason_codes=tuple(leg.verify_reason_codes or ()),
                rung=leg.final_rung,
                reason=leg.final_reason,
                left_armed=left_armed,
            )
            return

    async def _advance_verify(self, wall: datetime, leg: _RecoveryLeg, now_mono: float) -> None:
        """§7.2 step 6: the §6 leg re-run, unchanged, on the same unit.

        The re-run rides the SAME leg machinery (``_advance_leg`` and its
        seven steps, its renew/cancel discipline, its preemption classes) with
        the ``verification`` flag set: identical judgment, a different
        destination for the verdict (``_finish_leg`` routes it to the
        recovery outcome instead of a second ``health_probe_completed`` row).
        """
        if leg.verify is None:
            # The §6.1 gate walk for the re-run: a guard refusal is an
            # inconclusive VERIFICATION (A2 — the cycle plausibly worked, the
            # proof is missing), never a failure of the battery.
            reason = await self._probe_gate_skip(leg.unit_id, now_mono)
            if reason is not None:
                leg.final_verdict = RECOVERY_RECOVERED_UNPROVEN
                leg.final_rung = RUNG_VERIFICATION_INCONCLUSIVE
                leg.final_tier = TIER_ALERT
                leg.final_reason = reason
                leg.verify_reason_codes = (reason,)
                leg.advance("disarm_after", now_mono)
                return
            verify = _ProbeLeg(unit_id=leg.unit_id, verification=True)
            verify.started_mono = now_mono
            verify.step_started_mono = now_mono
            latest = await self._observations.all_latest()
            quiet, import_w = self._quiet_gate(latest)
            verify.start_import_w = import_w if quiet else None
            leg.verify = verify
            # The probe machinery advances ``self._leg``; the recovery leg
            # holds its own reference and the phase machine never touches
            # ``self._probe_order`` from here (a verification leg finishes
            # into the recovery outcome, not the probe sequence).
            self._leg = verify
            return
        await self._advance_leg(wall, now_mono)

    async def _recovery_park_refusal(
        self, wall: datetime, leg: _RecoveryLeg, refusal: ParkingRefusal
    ) -> bool:
        """Map one refused PARK onto §8's ladder; True when the leg ended."""
        if refusal.code == PARK_WRITE_FAILED:
            if not leg.reissued:
                # Rung 2: on a clean transport refusal the actor has already
                # reconnected (its own automatic resync); ONE re-issue of the
                # write.  A second refusal ends the night's attempt below.
                leg.reissued = True
                return False
            await self._finish_recovery(
                wall,
                leg,
                verdict=RECOVERY_FAILED_WRITE,
                tier=TIER_ALERT,
                reason_codes=("write_failed", "transport_resync_reissued_once"),
                rung=RUNG_RESYNC_EXHAUSTED,
                reason=refusal.message,
            )
            return True
        if refusal.code == PARK_READBACK_UNVERIFIED:
            # Rung 3: ACKed-but-unverified — no re-issue, no second attempt
            # (I3): the word state is unknown and the program writes nothing
            # further that night.
            await self._finish_recovery(
                wall,
                leg,
                verdict=RECOVERY_WRITE_UNVERIFIED,
                tier=TIER_ALERT,
                reason_codes=("readback_unverified", "no_further_write_tonight"),
                rung=RUNG_ACKED_UNVERIFIED,
                reason=refusal.message,
            )
            return True
        skip = {
            PARK_CONFLICT_REFUSED: SKIP_PARK_CONFLICT,
            PARK_ALREADY_PARKED: SKIP_OPEN_LEASE,
            PARK_MODE_OUT_OF_SCOPE: SKIP_VENDOR_MODE,
            PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED: SKIP_FOREIGN_STANDBY,
        }.get(refusal.code, "park_refused")
        await self._finish_recovery(
            wall,
            leg,
            verdict=f"skipped:{skip}",
            tier=TIER_ALERT if skip in {SKIP_VENDOR_MODE, SKIP_FOREIGN_STANDBY} else TIER_NOTICE,
            reason_codes=(skip,),
        )
        return True

    async def _recovery_resume_refusal(
        self, wall: datetime, leg: _RecoveryLeg, refusal: ParkingRefusal
    ) -> bool:
        """Map one refused RESUME onto §8's ladder; True when the leg ended."""
        if refusal.code == PARK_WRITE_FAILED:
            if not leg.reissued:
                leg.reissued = True
                return False
            await self._finish_recovery(
                wall,
                leg,
                verdict=RECOVERY_FAILED_WRITE,
                tier=TIER_ALERT,
                reason_codes=("write_failed", "transport_resync_reissued_once"),
                rung=RUNG_RESYNC_EXHAUSTED,
                reason=refusal.message,
            )
            return True
        if refusal.code == PARK_READBACK_UNVERIFIED:
            # Rung 3 on the exit write: parking itself holds the lease in the
            # terminal write_unverified posture (parked stays true, ours stays
            # ours); the program writes nothing further that night.
            await self._finish_recovery(
                wall,
                leg,
                verdict=RECOVERY_WRITE_UNVERIFIED,
                tier=TIER_ALERT,
                reason_codes=("readback_unverified", "no_further_write_tonight"),
                rung=RUNG_ACKED_UNVERIFIED,
                reason=refusal.message,
            )
            return True
        if refusal.code == RESUME_STOP_LATCHED:
            # A latched stop landed mid-cycle: no further write (I7); the
            # open lease follows the standing expiry rules and the operator's
            # resume-with-acknowledgement is the named exit.
            await self._finish_recovery(
                wall,
                leg,
                verdict=f"skipped:{SKIP_LATCHED_STOP}",
                tier=TIER_ALERT,
                reason_codes=("emergency_stop", "lease_follows_standing_rules"),
            )
            return True
        skip = {
            PARK_MODE_OUT_OF_SCOPE: SKIP_VENDOR_MODE,
            PARK_FOREIGN_WORD_ACKNOWLEDGEMENT_REQUIRED: SKIP_FOREIGN_STANDBY,
        }.get(refusal.code, SKIP_RESUME_REFUSED)
        # A cycle already ran (the park write landed): every exit here is the
        # operator's morning, so every one carries the alert tier.
        await self._finish_recovery(
            wall,
            leg,
            verdict=f"skipped:{skip}",
            tier=TIER_ALERT,
            reason_codes=(skip,),
        )
        return True

    async def _recovery_skip_reason(self, unit_id: str, now_mono: float) -> str | None:
        """§7.2 step 1's re-verify walk — every guard re-checked inside the
        standing locks, one honest reason per unit."""
        latest = await self._observations.all_latest()
        observation = latest.get(unit_id)
        health = await self._health_states()
        parked = self._parked_view()
        active = await self._intents.active(now_mono)
        word = _word(getattr(observation, "debug_mode_w", None))
        if parked or word == 1:
            if word == 1 and unit_id not in parked:
                # A standby word of 1 that is not ours: the operator's
                # takeover resume is the only exit, forever (§7.2 step 1).
                return SKIP_FOREIGN_STANDBY
            return SKIP_OPEN_LEASE
        if word is not None and word in {2, 3, 4, 5, 6}:
            return SKIP_VENDOR_MODE
        state = getattr(health.get(unit_id), "state", None)
        state_word = _enum_text(state) if state is not None else ""
        if state_word == "unreachable":
            return SKIP_UNREACHABLE
        if state_word == "not_responding":
            return SKIP_NOT_RESPONDING
        if state_word == "foreign_writer":
            return SKIP_FOREIGN_WRITER
        if state_word in {"inhibited", "actuation_incoherent"}:
            return SKIP_INHIBITED
        captured = _finite(getattr(observation, "captured_at_mono", None))
        if observation is None or captured is None or (
            now_mono - captured > float(self._policy.max_telemetry_age_s)
        ):
            return SKIP_TELEMETRY_STALE
        if unit_id in self._claiming_units(active, exclude_prefix=_OWN_INTENT_PREFIX):
            return SKIP_UNDER_INTENT
        return None

    async def _probe_gate_skip(self, unit_id: str, now_mono: float) -> str | None:
        """The §6.1 gate walk (the verification re-run's own start gate)."""
        latest = await self._observations.all_latest()
        active = await self._intents.active(now_mono)
        health = await self._health_states()
        parked = self._parked_view()
        stopped = self._latched_view()
        quiet, _ = self._quiet_gate(latest)
        return self._probe_skip_reason(
            unit_id,
            latest.get(unit_id),
            active=active,
            health_view=health.get(unit_id),
            parked=unit_id in parked,
            stopped=unit_id in stopped,
            quiet_ok=quiet is True,
            quiet_stale=quiet is None,
            now_mono=now_mono,
        )

    def _cycle_excerpt(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """The row's cycle evidence: the write/readback words and times (the
        Be-Connect pattern — the operator acts without opening a log)."""
        raw_lease = result.get("lease")
        lease: Mapping[str, Any] = raw_lease if isinstance(raw_lease, Mapping) else {}
        return {
            "prior_word": result.get("prior_word"),
            "written_value": result.get("written_value"),
            "readback_word": result.get("readback_word"),
            "verified": result.get("verified"),
            "origin": result.get("origin"),
            "lease_epoch": lease.get("epoch"),
            "lease_s": lease.get("max_total_s"),
            "checklist": result.get("checklist"),
        }

    async def _disarm_unit(self, *, unit_id: str) -> Mapping[str, Any]:
        port = self._disarm_port
        assert port is not None  # the construction guard is the call site's
        with contextlib.suppress(Exception):
            return await port(unit_id=unit_id)
        return {"status": "refused", "reason": "disarm_port_unavailable"}

    async def _arm_unit(self, *, unit_id: str) -> Mapping[str, Any]:
        port = self._arm_port
        assert port is not None  # the construction guard is the call site's
        with contextlib.suppress(Exception):
            return await port(unit_id=unit_id)
        return {"status": "refused", "reason": "arm_port_unavailable"}

    async def _finish_recovery(
        self,
        wall: datetime,
        leg: _RecoveryLeg,
        *,
        verdict: str,
        tier: str,
        reason_codes: tuple[str, ...],
        rung: str | None = None,
        reason: str | None = None,
        left_armed: bool = False,
    ) -> None:
        """Record the §10 outcome row/event and retire the unit's night (I1:
        this is the ONLY exit — no step of the composite ever loops back)."""
        posture = self._recovery_posture(leg.unit_id)
        facts = self._recovery_facts.get(leg.unit_id, _RecoveryFacts())
        counted = verdict in _RECOVERY_COUNTED_FAILS
        outcome = _RecoveryOutcome(
            verdict=verdict,
            tier=tier,
            posture=posture,
            reason=reason,
            rung=rung,
            codes=tuple(reason_codes) or (verdict or "not_eligible",),
            cycle={
                "hold_s": self._settings.recovery.hold_s,
                "reissued": leg.reissued,
                "park": leg.park_result,
                "resume": leg.resume_result,
            },
            verification=self._verification_payload(leg.verify_outcome),
            left_armed=left_armed,
            attempts_total=facts.attempts_total + (1 if verdict in _RECOVERY_ATTEMPTED else 0),
            consecutive_fails=facts.consecutive_fails + (1 if counted else 0),
        )
        await self._record_recovery_outcome(wall, leg.unit_id, outcome)
        self._recovery_index += 1
        self._recovery_leg = None

    @staticmethod
    def _verification_payload(outcome: _ProbeOutcome | None) -> dict[str, Any] | None:
        if outcome is None:
            return None
        return {
            "verdict": outcome.verdict,
            "probe_w": outcome.probe_w,
            "core_samples": outcome.core_samples,
            "qualifying_samples": outcome.qualifying_samples,
            "echo": outcome.echo,
            "returned_to_baseline": outcome.returned_to_baseline,
            "final_w": outcome.final_w,
        }

    async def _record_recovery_outcome(
        self, wall: datetime, unit_id: str, outcome: _RecoveryOutcome
    ) -> None:
        """§10's ``health_recovery_outcome`` row + §11's ``health.recovery``
        event (alert tier on every outcome except ``recovered``, which is the
        resolved tier)."""
        self._recovery[unit_id] = outcome
        night = (self._night or wall.date()).isoformat()
        census = self._census.get(unit_id)
        probe = self._probe.get(unit_id)
        payload: dict[str, Any] = {
            "night": night,
            "verdict": outcome.verdict,
            "tier": outcome.tier,
            "posture": outcome.posture,
            "eligibility": {
                "census": None if census is None else census.verdict,
                "probe": None if probe is None else probe.verdict,
                "eligible": self._recovery_eligible(unit_id),
            },
            "reason": outcome.reason,
            "rung": outcome.rung,
            "cycle": outcome.cycle,
            "verification": outcome.verification,
            "left_armed": outcome.left_armed,
            "attempts_total": outcome.attempts_total,
            "consecutive_fails": outcome.consecutive_fails,
            "consecutive_fail_limit": self._settings.recovery.consecutive_fail_limit,
            "hold_s": self._settings.recovery.hold_s,
            "bmu_cross_check": BMU_CROSS_CHECK_NOTE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        if outcome.tier == TIER_ALERT:
            # §11: the advisory rides the alert row verbatim, and the advise
            # posture names the exact operator sequence.
            payload["advisory"] = RESTART_ADVISORY
            if outcome.verdict == RECOVERY_ADVISED:
                payload["walkthrough"] = ADVISE_WALKTHROUGH
        await self._append_row(
            self._health_row(
                event_type="health_recovery_outcome",
                unit_id=unit_id,
                reason_codes=outcome.codes,
                result="recorded",
                payload=payload,
            )
        )
        await self._publish("health.recovery", payload | {"unit_id": unit_id})
        # A11's figures refresh as the night's outcomes land.
        with contextlib.suppress(Exception):
            self._recovery_facts = await self._derive_recovery_facts(self._night or wall.date())

    async def _derive_recovery_facts(self, night: date) -> dict[str, _RecoveryFacts]:
        """A11's morning figures per unit, from the durable outcome rows.

        ``attempts_total`` counts every attempted cycle (bounded by the audit
        window — an evicted window undercounts, the honest direction).  The
        consecutive-fail streak walks the outcome rows newest-first and stops
        at the first night whose outcome is NOT a counted failure — so a
        probe-pass morning (verdict null: not eligible) breaks the streak,
        which is the operator's evidence-driven reset of §7.3's stand-down.
        Two verdict classes are TRANSPARENT (they neither extend nor reset):
        nights with no row at all (a deferred or interrupted night), and the
        ``advised``/``advisory_only`` rows — the cap's own stand-down and the
        advise posture's advisory are not cycles and must never erode the
        streak they guard, or the cap could never bind across them.
        """
        rows = await self._recent_health_rows()
        outcomes: dict[str, dict[date, str]] = {}
        for row in rows:
            if getattr(row, "event_type", "") != "health_recovery_outcome":
                continue
            payload = getattr(row, "payload", None)
            if not isinstance(payload, Mapping):
                continue
            row_night = payload.get("night")
            unit_id = getattr(row, "unit_id", None)
            verdict = payload.get("verdict")
            if not isinstance(row_night, str) or not isinstance(unit_id, str):
                continue
            if isinstance(verdict, str) and verdict in {RECOVERY_ADVISED, RECOVERY_ADVISORY_ONLY}:
                continue  # transparent: not a cycle, never a reset
            if not isinstance(verdict, str):
                verdict = ""
            try:
                night_date = date.fromisoformat(row_night)
            except ValueError:
                continue
            # A null-verdict row participates as "" — a night the program ran
            # and the unit was NOT eligible (healthy): it breaks the streak.
            outcomes.setdefault(unit_id, {})[night_date] = verdict
        trailing_from = night - timedelta(days=_RECOVERY_TRAILING_NIGHTS - 1)
        facts: dict[str, _RecoveryFacts] = {}
        for unit_id, by_night in outcomes.items():
            streak = 0
            for night_date in sorted(by_night, reverse=True):
                if by_night[night_date] in _RECOVERY_COUNTED_FAILS:
                    streak += 1
                else:
                    break
            window = [by_night[d] for d in sorted(by_night) if d >= trailing_from]
            facts[unit_id] = _RecoveryFacts(
                attempts_total=sum(1 for v in by_night.values() if v in _RECOVERY_ATTEMPTED),
                consecutive_fails=streak,
                trailing_attempted=sum(1 for v in window if v in _RECOVERY_ATTEMPTED),
                trailing_recovered=sum(1 for v in window if v == RECOVERY_RECOVERED),
            )
        return facts

    # --- durable-row derivations (§4/A4/I8) --------------------------------------

    async def _night_program_state(self, night: date) -> tuple[set[str], bool]:
        """The night's attempt units and whether its program completed (A4).

        An attempt is ANY health-watch audit row for a unit that civil
        night; a program-level row (a deferred or interrupted night) retires
        the whole night.  The night is COMPLETE when every unit carries a row
        for the LAST commissioned stage — the interrupted-composite
        reconstruction fires only on a genuinely incomplete program.
        """
        rows = await self._recent_health_rows()
        units: set[str] = set()
        censored: set[str] = set()
        last_stage_rows: set[str] = set()
        last_stage = next(
            stage
            for stage in ("recovery", "probe", "census")
            if stage in self._settings.stages
        )
        last_event = {
            "recovery": "health_recovery_outcome",
            "probe": "health_probe_completed",
            "census": "health_census_recorded",
        }[last_stage]
        for row in rows:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, Mapping):
                continue
            if payload.get("night") != night.isoformat():
                continue
            event_type = str(getattr(row, "event_type", ""))
            unit_id = getattr(row, "unit_id", None)
            if event_type == "health_program_recorded":
                # A program-level row (deferred or interrupted) retires the
                # whole night's program.
                censored.update(self._settings.unit_ids)
                continue
            if isinstance(unit_id, str) and unit_id:
                units.add(unit_id)
                if event_type == last_event:
                    last_stage_rows.add(unit_id)
        attempt_units = units | censored
        complete = bool(attempt_units) and set(self._settings.unit_ids) <= last_stage_rows
        return attempt_units, complete

    async def _stuck_streaks(self, night: date) -> dict[str, int]:
        """Consecutive prior stuck nights per unit, from durable rows (§5).

        Tonight's own rows are excluded (the streak counts the nights BEFORE
        this one); an evicted window undercounts — the honest, conservative
        direction (notice where alert cannot be proven).
        """
        rows = await self._recent_health_rows()
        stuck_nights: dict[str, set[date]] = {}
        census_nights: set[date] = set()
        for row in rows:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, Mapping):
                continue
            row_night = payload.get("night")
            if not isinstance(row_night, str):
                continue
            try:
                night_date = date.fromisoformat(row_night)
            except ValueError:
                continue
            if night_date >= night:
                continue
            if row.event_type == "health_census_recorded":
                census_nights.add(night_date)
                if payload.get("verdict") == CENSUS_STUCK:
                    unit_id = getattr(row, "unit_id", None)
                    if isinstance(unit_id, str):
                        stuck_nights.setdefault(unit_id, set()).add(night_date)
        streaks: dict[str, int] = {}
        for unit_id, nights_stuck in stuck_nights.items():
            streak = 0
            cursor = night - timedelta(days=1)
            while cursor in nights_stuck and cursor in census_nights:
                streak += 1
                cursor -= timedelta(days=1)
            streaks[unit_id] = streak
        return streaks

    async def _recent_health_rows(self) -> tuple[Any, ...]:
        with contextlib.suppress(Exception):
            rows = await self._audit.recent(limit=_ROW_SCAN_LIMIT)
            return tuple(
                row for row in rows if str(getattr(row, "event_type", "")).startswith("health_")
            )
        return ()

    async def _record_interrupted(self, wall: datetime, units: Sequence[str]) -> None:
        """A4's shape: the interrupted program alert, naming unit states.

        The night is retired (rows exist); the alert names the state each
        interrupted unit was actually left in — the honest telemetry state
        (arm, mode word, parked), which for an interrupted Stage R composite
        is exactly the crash-after-re-arm shape A4 names: armed, normal, no
        lease, silently night-charging without this alert.
        """
        latest = await self._observations.all_latest()
        parked = self._parked_view()
        states: dict[str, Any] = {}
        for unit_id in units:
            observation = latest.get(unit_id)
            states[unit_id] = {
                "lifecycle": _enum_text(getattr(observation, "lifecycle", None)),
                "debug_mode_w": _word(getattr(observation, "debug_mode_w", None)),
                "parked": unit_id in parked,
                "battery_watts": _finite(getattr(observation, "battery_watts", None)),
            }
        self._phase = "done"
        self._program_reason = REASON_INTERRUPTED
        await self._program_row(
            wall,
            REASON_INTERRUPTED,
            reason_codes=("interrupted_program",),
            tier=TIER_ALERT,
            payload={"units": states},
        )

    # --- audit rows + events (§10) -------------------------------------------------

    def _health_row(
        self,
        *,
        event_type: str,
        unit_id: str | None,
        reason_codes: tuple[str, ...],
        result: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now().astimezone(UTC)
        return AuditEvent(
            event_id=f"health-{event_type}-{uuid.uuid4().hex}",
            occurred_at=wall,
            monotonic_offset_s=now_mono,
            process_instance_id=self._process_instance_id or HEALTH_ADVISER_PRINCIPAL,
            event_type=event_type,
            unit_id=unit_id,
            principal=HEALTH_ADVISER_PRINCIPAL,
            correlation_id=f"health:{event_type}",
            policy_version=_HEALTH_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"event_type": event_type, **payload}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=UnitLifecycle.DISARMED,
            payload=payload,
        )

    async def _append_row(self, event: AuditEvent) -> None:
        with contextlib.suppress(Exception):
            await self._audit.append(event)

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._bus.publish({"type": event_type, "payload": dict(payload)})

    async def _record_census_row(
        self, wall: datetime, unit_id: str, outcome: _CensusOutcome
    ) -> None:
        """§10's ``health_census_recorded`` row + §11's ``health.census`` event."""
        night = (self._night or wall.date()).isoformat()
        payload = {
            "night": night,
            "verdict": outcome.verdict,
            "tier": outcome.tier,
            "nights": outcome.nights,
            "predicates": outcome.predicates,
            "excluded_class": outcome.excluded_class,
            "soc_flagged": outcome.soc_flagged,
            "samples": outcome.samples,
            "evidence_window_h": self._settings.stuck.evidence_window_h,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._health_row(
                event_type="health_census_recorded",
                unit_id=unit_id,
                reason_codes=(outcome.verdict,),
                result="recorded",
                payload=payload,
            )
        )
        await self._publish("health.census", payload | {"unit_id": unit_id})

    async def _record_probe_row(
        self,
        wall: datetime,
        unit_id: str,
        outcome: _ProbeOutcome,
        reason_codes: tuple[str, ...],
    ) -> None:
        """§10's ``health_probe_completed`` row + §11's ``health.probe`` event."""
        night = (self._night or wall.date()).isoformat()
        payload = {
            "night": night,
            "verdict": outcome.verdict,
            "tier": probe_tier(outcome.verdict or ""),
            "probe_w": outcome.probe_w,
            "core_samples": outcome.core_samples,
            "qualifying_samples": outcome.qualifying_samples,
            "echo": outcome.echo,
            "baseline_w": outcome.baseline_w,
            "returned_to_baseline": outcome.returned_to_baseline,
            "final_w": outcome.final_w,
            "demand_move_w": outcome.demand_move_w,
            "export_note": EXPORT_HONESTY_NOTE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._health_row(
                event_type="health_probe_completed",
                unit_id=unit_id,
                reason_codes=tuple(reason_codes) or (outcome.verdict or "recorded",),
                result="recorded",
                payload=payload,
            )
        )
        await self._publish("health.probe", payload | {"unit_id": unit_id})

    async def _program_row(
        self,
        wall: datetime,
        reason: str,
        *,
        reason_codes: tuple[str, ...],
        tier: str,
        payload: dict[str, Any],
    ) -> None:
        """The program-frame row (window_not_quiet / interrupted) + event."""
        night = (self._night or wall.date()).isoformat()
        body = {"night": night, "reason": reason, "tier": tier, **payload}
        await self._append_row(
            self._health_row(
                event_type="health_program_recorded",
                unit_id=None,
                reason_codes=reason_codes,
                result=reason,
                payload=body,
            )
        )
        await self._publish("health.program", body)

    # --- views -------------------------------------------------------------------

    def _parked_view(self) -> frozenset[str]:
        with contextlib.suppress(Exception):
            return frozenset(self._parked_units())
        return frozenset()

    def _latched_view(self) -> frozenset[str]:
        with contextlib.suppress(Exception):
            return frozenset(self._latched_stop_units())
        return frozenset()

    async def _any_live_claims(self, now_mono: float) -> frozenset[str]:
        """§4's quiet-window check: the units any live intent claims."""
        with contextlib.suppress(Exception):
            active = await self._intents.active(now_mono)
            return self._claiming_units(active)
        return frozenset()  # pragma: no cover - a failed read defers nothing

    def state_payload(self) -> dict[str, Any]:
        """§10's ``health_watch_state`` snapshot projection.

        Present exactly when the block composes; uncommissioned stages render
        ``{"mode": "uncommissioned"}`` — never absence-that-looks-like-health
        — and skipped units render their skip reason verbatim.
        """
        wall = self._clock.wall_now()
        units: list[dict[str, Any]] = []
        for unit_id in self._settings.unit_ids:
            units.append(
                {
                    "unit_id": unit_id,
                    "census": self._census_payload(self._census.get(unit_id)),
                    "probe": self._probe_payload(self._probe.get(unit_id)),
                    "recovery": self._recovery_payload(unit_id),
                }
            )
        return {
            "stages": list(self._settings.stages),
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "deadline_local": self._settings.deadline_local.strftime("%H:%M"),
            },
            "phase": self._phase,
            "night": None if self._night is None else self._night.isoformat(),
            "reason": self._program_reason,
            "as_of": wall.astimezone(UTC).isoformat(),
            "units": units,
        }

    @staticmethod
    def _census_payload(census: _CensusOutcome | None) -> dict[str, Any]:
        if census is None:
            return {"verdict": None, "nights": 0, "predicates": None, "tier": None}
        return {
            "verdict": census.verdict,
            "nights": census.nights,
            "predicates": census.predicates,
            "tier": census.tier,
        }

    @staticmethod
    def _probe_payload(probe: _ProbeOutcome | None) -> dict[str, Any]:
        if probe is None:
            return {
                "verdict": None,
                "probe_w": None,
                "qualifying_samples": None,
                "core_samples": None,
                "echo": None,
            }
        return {
            "verdict": probe.verdict,
            "probe_w": probe.probe_w,
            "qualifying_samples": probe.qualifying_samples,
            "core_samples": probe.core_samples,
            "echo": probe.echo,
        }

    def _recovery_payload(self, unit_id: str) -> dict[str, Any]:
        if "recovery" not in self._settings.stages:
            # The uncommissioned honesty (§10): a site without R never sees
            # "recovery" offered as if it could run.
            return {"mode": "uncommissioned"}
        effective_mode = self._recovery_posture(unit_id)
        facts = self._recovery_facts.get(unit_id, _RecoveryFacts())
        outcome = self._recovery.get(unit_id)
        payload: dict[str, Any] = {
            "mode": effective_mode,
            "verdict": None if outcome is None else outcome.verdict,
            "tier": None if outcome is None else outcome.tier,
            "posture": None if outcome is None else outcome.posture,
            "rung": None if outcome is None else outcome.rung,
            "reason": None if outcome is None else outcome.reason,
            "left_armed": bool(outcome is not None and outcome.left_armed),
            "attempts_total": facts.attempts_total,
            "consecutive_fails": facts.consecutive_fails,
            "consecutive_fail_limit": self._settings.recovery.consecutive_fail_limit,
            # A11: the trailing-30-night figures so the chronic case (a unit
            # that NEEDS the cycle most nights) is visible in the daily read.
            "trailing_30_nights": {
                "attempted": facts.trailing_attempted,
                "recovered": facts.trailing_recovered,
                "attempt_rate": round(facts.trailing_attempted / _RECOVERY_TRAILING_NIGHTS, 4),
            },
        }
        if self._settings.recovery_mode == "auto" and unit_id in self._receipts_missing:
            # A6's boot degradation, loud: the receipt file is gone, so the
            # unit runs advise until the operator restores it.
            payload["configured_mode"] = "auto"
            payload["note"] = "auto receipt missing at boot — degraded to advise"
        return payload


__all__ = [
    "ADVISE_WALKTHROUGH",
    "BMU_CROSS_CHECK_NOTE",
    "CENSUS_DEGRADED",
    "CENSUS_NOMINAL",
    "CENSUS_STUCK",
    "ECHO_EXTERNAL_WRITER",
    "ECHO_MATCHES_WRITE",
    "ECHO_OBJECTIVE_NOT_SERVED",
    "ECHO_UNREADABLE",
    "EXPORT_HONESTY_NOTE",
    "HEALTH_ADVISER_PRINCIPAL",
    "HEALTH_WATCH_NOT_COMMISSIONED",
    "PROBE_FAIL_BASELINE",
    "PROBE_FAIL_NO_RESPONSE",
    "PROBE_FAIL_PARTIAL",
    "PROBE_INCONCLUSIVE_ABORTED",
    "PROBE_INCONCLUSIVE_CONFOUNDED",
    "PROBE_INCONCLUSIVE_ECHO",
    "PROBE_INCONCLUSIVE_PREEMPTED",
    "PROBE_PASS",
    "RECOVERY_ADVISED",
    "RECOVERY_ADVISORY_ONLY",
    "RECOVERY_FAILED_NO_EFFECT",
    "RECOVERY_FAILED_WRITE",
    "RECOVERY_RECOVERED",
    "RECOVERY_RECOVERED_UNPROVEN",
    "RECOVERY_WRITE_UNVERIFIED",
    "RESTART_ADVISORY",
    "RUNG_ACKED_UNVERIFIED",
    "RUNG_REARM_REFUSED",
    "RUNG_RESYNC_EXHAUSTED",
    "RUNG_VERIFICATION_FAILED",
    "RUNG_VERIFICATION_INCONCLUSIVE",
    "RUNG_VERIFIED",
    "SKIP_FOREIGN_STANDBY",
    "TIER_ALERT",
    "TIER_NOTICE",
    "TIER_RESOLVED",
    "HealthWatchController",
    "HealthWatchRefusal",
    "HealthWatchSettings",
    "ProbeSettings",
    "RecoverySettings",
    "StuckSettings",
    "census_tier",
    "census_verdict",
    "measured_class",
    "probe_tier",
    "probe_verdict",
    "recovery_tier",
    "stuck_predicates",
]
