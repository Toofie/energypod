/**
 * The nightly battery health watch's shared wire model
 * (DESIGN_BATTERY_HEALTH_WATCH.md §10/§11/§13, CONTRACT v1.2): the
 * `health_watch_state` snapshot projection plus the plain-word maps the Home
 * Health Watch card renders. Stages C and P are live; Stage R (the recovery
 * stage) ships in its standing `advise` posture — writes nothing, ever — and
 * its verdict vocabulary, the A11 morning figures, and the §13 advisory
 * surfaces are pinned here. A site without the stage renders the contract's
 * own uncommissioned honesty (`{"mode": "uncommissioned"}`), never a
 * suggestion the site cannot execute.
 *
 * Wire truth pinned here (§10's exact shape):
 * - Projection: `{stages, window{opens_local, deadline_local}, phase, night,
 *   reason, as_of, units[{unit_id, census{verdict, nights, predicates,
 *   tier, note}, probe{verdict, probe_w, qualifying_samples, core_samples,
 *   echo, consecutive_fail_nights}, recovery{mode, verdict?, tier?,
 *   posture?, route?, rung?, reason?, left_armed?, attempts_total?,
 *   consecutive_fails?, consecutive_fail_limit?,
 *   trailing_30_nights{attempted, recovered, attempt_rate}?, note?}}]}`.
 * - `phase`: await_window | census | probe | recovery | record | done — the
 *   per-night phase machine; `reason` carries the program-level frame words
 *   (window_not_quiet / outside_window / interrupted).
 * - Census `verdict`: nominal | stuck_suspected | phase_idle_or_ct_silent |
 *   degraded_evidence | excluded:<class>. The stuck flag is NOTICE tier on
 *   first occurrence and promotes to ALERT after `flag_persistence_nights`
 *   consecutive nights; `nights` carries the streak and `predicates` rides
 *   it (the full vector, one tap away — since A16 the load predicate key is
 *   `load_unserved_in_phase`, the unit's OWN CT word). The A16 soft note
 *   `phase_idle_or_ct_silent` is informational at EVERY age — never a flag,
 *   never promoting — and carries `note: "ct_link_suspect"` when the unit's
 *   CT word has never moved in the trailing phase-live window.
 * - Probe `verdict`: pass | fail_no_response | fail_partial |
 *   fail_baseline_not_returned | inconclusive_echo_mismatch |
 *   inconclusive_baseline_confounded | inconclusive_preempted |
 *   inconclusive_aborted | skipped:<reason> — the skip reasons verbatim.
 *   `consecutive_fail_nights` carries the fail_no_response streak, tonight
 *   included (route B's repetition evidence).
 * - Recovery `verdict` (§7.2 step 7, §8's ladder): recovered |
 *   recovered_unproven | failed_write | write_unverified | failed_no_effect
 *   | advised | advisory_only | skipped:<reason> | null (not eligible).
 *   `route` names §7.1's eligibility basis on eligible nights: "a" (census
 *   flag AND probe no-response) or "b" (A16: the soft-note class AND
 *   `probe_fail_nights` consecutive no-response nights). `recovered` is the
 *   resolved tier; every other outcome is alert except the notice-tier
 *   skips. `recovered_unproven` does NOT count toward `consecutive_fails`
 *   (A2); `attempts_total` counts every attempted cycle, and
 *   `trailing_30_nights` carries A11's chronic-case figures.
 * - `echo`: the standing discriminator's words (echo_matches_write |
 *   objective_not_served | external_writer | echo_unreadable).
 *
 * Honesty rules (§13's own): uncommissioned stages render "not commissioned"
 * where their UI would be; skipped units render their skip reason verbatim;
 * a disarmed unit is the arm instruction it is (the program never arms
 * outside the ONE bounded verification re-arm inside an `auto` cycle); the
 * A16 soft note renders as the informational note it is (never a flag chip,
 * never alert styling); and the §6.3 export note names the brief feed-in
 * blip so the export meter is never a surprise. The not-isolation sentence
 * rides the card wherever a parked unit's state shows (§13's pinned styling
 * rule).
 */
import { isRecord } from "./fleet";

/** The bus vocabulary the watch publishes (§10). */
export const HEALTH_CENSUS_EVENT = "health.census" as const;
export const HEALTH_PROBE_EVENT = "health.probe" as const;
export const HEALTH_RECOVERY_EVENT = "health.recovery" as const;
export const HEALTH_PROGRAM_EVENT = "health.program" as const;

/** The per-night phase machine's ONE vocabulary (§4). */
export type HealthWatchPhase =
  | "await_window"
  | "census"
  | "probe"
  | "recovery"
  | "record"
  | "done";

export const HEALTH_WATCH_PHASES: readonly HealthWatchPhase[] = [
  "await_window",
  "census",
  "probe",
  "recovery",
  "record",
  "done",
];

/** §11's severity tiers (the operator's four distinguishable mornings). */
export type HealthTierWord = "notice" | "alert";
export type HealthTier = HealthTierWord | null;

/** One unit's census half: the verdict, the persistence streak, the vector,
 * and the A16 soft note's `ct_link_suspect` annotator (null everywhere
 * else). */
export interface HealthCensusState {
  verdict: string | null;
  nights: number;
  predicates: Record<string, boolean> | null;
  tier: HealthTier;
  note: string | null;
}

/** One unit's probe half: the verdict with its one-line figures (§13) and
 * the fail_no_response streak tonight included (route B's evidence). */
export interface HealthProbeState {
  verdict: string | null;
  probeW: number | null;
  qualifyingSamples: number | null;
  coreSamples: number | null;
  echo: string | null;
  consecutiveFailNights: number | null;
}

/** A11's trailing-30-night figures: the chronic case made visible. */
export interface HealthRecoveryTrailing {
  attempted: number;
  recovered: number;
  attemptRate: number;
}

/**
 * One unit's recovery half (§7/§8/§11). `mode` is the unit's EFFECTIVE
 * posture — `advise`, `auto`, or `uncommissioned` when the stage is absent;
 * a missing auto receipt degrades the unit to `advise` with the loud
 * `note` (A6). `verdict` is null before the night's recovery phase and on
 * not-eligible nights — never an invented outcome.
 */
export interface HealthRecoveryState {
  mode: string;
  verdict: string | null;
  tier: HealthTier | "resolved";
  posture: string | null;
  /** §7.1's eligibility route on eligible nights: "a" (the conjunction) or
   * "b" (A16's repetition on the soft-note class); null otherwise. */
  route: "a" | "b" | null;
  rung: string | null;
  reason: string | null;
  leftArmed: boolean;
  attemptsTotal: number;
  consecutiveFails: number;
  consecutiveFailLimit: number;
  trailing: HealthRecoveryTrailing | null;
  note: string | null;
}

export interface HealthWatchUnitState {
  unitId: string;
  census: HealthCensusState;
  probe: HealthProbeState;
  recovery: HealthRecoveryState;
}

export interface HealthWatchState {
  stages: string[];
  window: { opensLocal: string; deadlineLocal: string };
  phase: HealthWatchPhase;
  night: string | null;
  reason: string | null;
  asOf: string;
  units: HealthWatchUnitState[];
}

// --- parsing -------------------------------------------------------------------

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function optionalInt(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : null;
}

function intOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : fallback;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry !== "")
    : [];
}

function oneOf<T extends string | null>(
  value: unknown,
  vocabulary: readonly T[],
  fallback: T,
): T {
  return typeof value === "string" && (vocabulary as readonly (string | null)[]).includes(value)
    ? (value as T)
    : fallback;
}

function toPredicates(value: unknown): Record<string, boolean> | null {
  if (!isRecord(value)) {
    return null;
  }
  const predicates: Record<string, boolean> = {};
  for (const [key, flag] of Object.entries(value)) {
    if (typeof flag === "boolean") {
      predicates[key] = flag;
    }
  }
  return Object.keys(predicates).length > 0 ? predicates : null;
}

function toCensus(value: unknown): HealthCensusState {
  const record = isRecord(value) ? value : {};
  return {
    verdict: optionalText(record.verdict),
    nights: intOr(record.nights, 0),
    predicates: toPredicates(record.predicates),
    tier: oneOf<HealthTierWord | null>(record.tier, ["notice", "alert", null], null),
    note: optionalText(record.note),
  };
}

function toProbe(value: unknown): HealthProbeState {
  const record = isRecord(value) ? value : {};
  return {
    verdict: optionalText(record.verdict),
    probeW: optionalInt(record.probe_w),
    qualifyingSamples: optionalInt(record.qualifying_samples),
    coreSamples: optionalInt(record.core_samples),
    echo: optionalText(record.echo),
    consecutiveFailNights: optionalInt(record.consecutive_fail_nights),
  };
}

function toRecovery(value: unknown): HealthRecoveryState {
  const record = isRecord(value) ? value : {};
  const trailing = isRecord(record.trailing_30_nights)
    ? {
        attempted: intOr(record.trailing_30_nights.attempted, 0),
        recovered: intOr(record.trailing_30_nights.recovered, 0),
        attemptRate:
          typeof record.trailing_30_nights.attempt_rate === "number" &&
          Number.isFinite(record.trailing_30_nights.attempt_rate)
            ? record.trailing_30_nights.attempt_rate
            : 0,
      }
    : null;
  return {
    mode: text(record.mode, "uncommissioned"),
    verdict: optionalText(record.verdict),
    tier: oneOf<HealthTier | "resolved">(
      record.tier,
      ["notice", "alert", "resolved", null],
      null,
    ),
    posture: optionalText(record.posture),
    route: oneOf<"a" | "b" | null>(record.route, ["a", "b", null], null),
    rung: optionalText(record.rung),
    reason: optionalText(record.reason),
    leftArmed: record.left_armed === true,
    attemptsTotal: intOr(record.attempts_total, 0),
    consecutiveFails: intOr(record.consecutive_fails, 0),
    consecutiveFailLimit: intOr(record.consecutive_fail_limit, 0),
    trailing,
    note: optionalText(record.note),
  };
}

function toUnit(value: unknown): HealthWatchUnitState | null {
  if (!isRecord(value) || typeof value.unit_id !== "string" || value.unit_id === "") {
    return null;
  }
  return {
    unitId: value.unit_id,
    census: toCensus(value.census),
    probe: toProbe(value.probe),
    recovery: toRecovery(value.recovery),
  };
}

/**
 * Narrow one health-watch projection (a snapshot's `health_watch_state` or
 * the status route's body). Null when the value is not an object: a garbage
 * frame is never half-adopted. Absent fields inherit from `base` when one is
 * given, else take their honest defaults.
 */
export function toHealthWatchState(
  value: unknown,
  base: HealthWatchState | null = null,
): HealthWatchState | null {
  if (!isRecord(value)) {
    return null;
  }
  const units = Array.isArray(value.units)
    ? value.units.map(toUnit).filter((unit): unit is HealthWatchUnitState => unit !== null)
    : (base?.units ?? []);
  const window = isRecord(value.window) ? value.window : {};
  return {
    stages: stringList(value.stages),
    window: {
      opensLocal: text(window.opens_local, base?.window.opensLocal ?? ""),
      deadlineLocal: text(window.deadline_local, base?.window.deadlineLocal ?? ""),
    },
    phase: oneOf(value.phase, HEALTH_WATCH_PHASES, base?.phase ?? "await_window"),
    night: optionalText(value.night),
    reason: optionalText(value.reason),
    asOf: text(value.as_of, base?.asOf ?? ""),
    units,
  };
}

// --- plain-language maps (the codes are the wire, the sentences are the console) -

/**
 * The card's status sentence for the program frame: the phase in plain
 * words, with the program-level reason words rendered as their honest
 * one-line stories (a deferred window never fights; an interrupted night
 * names the alert).
 */
export function healthWatchStatusText(state: HealthWatchState): string {
  if (state.reason === "window_not_quiet") {
    return `The ${state.window.opensLocal} window was not quiet tonight — the health watch deferred rather than fight for it.`;
  }
  if (state.reason === "interrupted") {
    return "Tonight's health watch was interrupted — the alert names the state each battery was left in, and the program stands down for the night.";
  }
  if (state.reason === "outside_window") {
    return "The health-watch window passed before the program could run tonight — no act starts after the deadline.";
  }
  switch (state.phase) {
    case "await_window":
      return `The nightly health watch opens at ${state.window.opensLocal}.`;
    case "census":
      return "Tonight's health watch is reading the batteries (the census — no writes).";
    case "probe":
      return "Tonight's health watch is running its one-at-a-time actuation probes.";
    case "recovery":
      return "Tonight's health watch is running its one-at-a-time recovery cycles (the auto posture) or rendering its advisories (advise).";
    case "record":
      return "Tonight's health watch is recording its results.";
    default:
      return "Tonight's health watch is complete.";
  }
}

/** The stage list in plain words, "not commissioned" included honestly. */
export function healthWatchStagesText(state: HealthWatchState): string {
  const stage = (name: string): string =>
    state.stages.includes(name) ? name : `${name} not commissioned`;
  return [stage("census"), stage("probe"), stage("recovery")].join(" · ");
}

/**
 * One unit's census chip: nominal reads nominal, a stuck flag names its
 * nights and tier, the A16 soft note renders as the informational note it is
 * (its age and the ct_link_suspect annotator included — never a flag chip,
 * never alert styling), an excluded class renders its class, degraded
 * evidence says so (never a wrong verdict).
 */
export function censusChipText(unit: HealthWatchUnitState): string {
  const verdict = unit.census.verdict;
  if (verdict === null) {
    return "not yet read";
  }
  if (verdict === "stuck_suspected") {
    const nights = unit.census.nights > 1 ? ` ${unit.census.nights} nights` : "";
    return `flagged${nights}`;
  }
  if (verdict === "phase_idle_or_ct_silent") {
    // A16: the pod is EITHER on an idle phase OR its CT link is dead, and
    // the census cannot tell which — a note, at the notice tier, at every
    // age.
    const nights = unit.census.nights > 1 ? ` ${unit.census.nights} nights` : "";
    const note = unit.census.note === "ct_link_suspect" ? " · CT link suspect" : "";
    return `idle phase or CT silent${nights}${note}`;
  }
  if (verdict === "degraded_evidence") {
    return "evidence degraded";
  }
  if (verdict.startsWith("excluded:")) {
    return verdict.slice("excluded:".length).replace(/_/g, " ");
  }
  return verdict;
}

/**
 * §13's probe line with its one-line figures: "fail — 3/20 samples moved"
 * for the fail classes, the inconclusive words as their own honest stories,
 * the skip reasons verbatim, and a pass naming the delivery.
 */
export function probeVerdictText(unit: HealthWatchUnitState): string {
  const probe = unit.probe;
  const verdict = probe.verdict;
  if (verdict === null) {
    return "not probed yet";
  }
  if (verdict.startsWith("skipped:")) {
    return probeSkipText(verdict.slice("skipped:".length));
  }
  const figures =
    probe.coreSamples === null
      ? ""
      : ` — ${probe.qualifyingSamples ?? 0}/${probe.coreSamples} samples moved`;
  const streak =
    verdict === "fail_no_response" &&
    probe.consecutiveFailNights !== null &&
    probe.consecutiveFailNights > 1
      ? ` · ${probe.consecutiveFailNights} nights in a row`
      : "";
  switch (verdict) {
    case "pass":
      return `pass${figures === "" ? "" : ` — ${probe.qualifyingSamples ?? 0}/${probe.coreSamples} samples delivered`}`;
    case "fail_no_response":
      return `fail — no response${figures} · echo followed the write, the battery stayed still${streak}`;
    case "fail_partial":
      return `fail — partial${figures}`;
    case "fail_baseline_not_returned":
      return "fail — did not stop cleanly (never returned to its baseline)";
    case "inconclusive_echo_mismatch":
      return "inconclusive — the echo and the delivery disagree (advisory only, never stuck evidence)";
    case "inconclusive_baseline_confounded":
      return "inconclusive — house demand moved mid-probe, the baseline judgment cannot hold";
    case "inconclusive_preempted":
      return "inconclusive — a higher-priority request took the battery mid-probe";
    case "inconclusive_aborted":
      return "inconclusive — the evidence did not carry (no verdict, next night is another chance)";
    default:
      return verdict;
  }
}

/** A skip reason in plain words; unknown reasons render verbatim. */
export function probeSkipText(reason: string): string {
  switch (reason) {
    case "unit_disarmed":
      return "skipped — disarmed (the program never arms; arm before the window)";
    case "unit_parked":
      return "skipped — parked";
    case "vendor_mode":
      return "skipped — the vendor app holds the mode word";
    case "latched_stop":
      return "skipped — an emergency stop holds it";
    case "under_intent":
      return "skipped — another request has this battery";
    case "telemetry_stale":
      return "skipped — telemetry not fresh enough to judge";
    case "quiet_load_gate":
      return "skipped — the house was drawing too much for a quiet probe";
    case "quiet_evidence_stale":
      return "skipped — the quiet-load reading could not be judged";
    case "deadline_passed":
      return "skipped — the no-new-act deadline passed first";
    case "census_excluded":
      return "not probed — the census excluded it (another surface owns its state)";
    case "census_degraded_evidence":
      return "not probed — the census evidence was degraded";
    default:
      return `skipped — ${reason}`;
  }
}

/**
 * One unit's recovery verdict in plain words (§7.2 step 7, §8's ladder,
 * §11's tiers). The verdicts the operator can distinguish in the morning:
 * the recovered morning line, the recovered_unproven honest proof-missing
 * word, the three ladder failures, the advisory postures, and the skips.
 */
export function recoveryVerdictText(unit: HealthWatchUnitState): string {
  const recovery = unit.recovery;
  if (recovery.mode === "uncommissioned") {
    return "recovery not commissioned";
  }
  const verdict = recovery.verdict;
  if (verdict === null) {
    return "not eligible tonight";
  }
  if (verdict.startsWith("skipped:")) {
    return recoverySkipText(verdict.slice("skipped:".length));
  }
  switch (verdict) {
    case "recovered":
      return `recovered — the standby cycle ran and the verification probe PASSED (${recovery.attemptsTotal} attempt${recovery.attemptsTotal === 1 ? "" : "s"} total)`;
    case "recovered_unproven":
      return `cycle ran, proof missing — ${recovery.rung === "rearm_refused" ? "the re-arm was refused (a foreign write during the park or a latched stop)" : "the verification could not run"}; verify it yourself this morning (not counted toward the cap)`;
    case "failed_write":
      return "failed — the mode write refused twice after resync (counted toward the cap)";
    case "write_unverified":
      return "failed — the write was acknowledged but the readback never confirmed it; no further write tonight (counted toward the cap)";
    case "failed_no_effect":
      return "failed — the cycle ran and the battery still did not follow commands (counted toward the cap)";
    case "advised":
      return "advisory rendered — the operator walkthrough is on the card below (this posture writes nothing, ever)";
    case "advisory_only":
      return `advisory-only — ${recovery.consecutiveFails} consecutive failed cycles reached the cap of ${recovery.consecutiveFailLimit}; no more cycles until the pod is fixed and passes a probe`;
    default:
      return verdict;
  }
}

/**
 * §7.1's eligibility route in plain words, when tonight was eligible by the
 * route the operator cannot see otherwise. Route A is the standing
 * conjunction (named by the verdict line already); route B is A16's
 * repetition on the soft-note class — the census cannot see this pod's
 * phase, so the consecutive nightly probe failures stood in for the flag.
 */
export function recoveryRouteText(unit: HealthWatchUnitState): string | null {
  if (unit.recovery.route !== "b") {
    return null;
  }
  return "eligible by route B — the census cannot see this pod's phase (idle, or CT-silent), so two consecutive nightly probe failures stood in for the flag";
}

/** A recovery skip reason in plain words; unknown reasons render verbatim. */
export function recoverySkipText(reason: string): string {
  switch (reason) {    case "foreign_standby":
      return "skipped — the battery is in Standby and it is not ours (the takeover resume is the only exit)";
    case "vendor_mode":
      return "skipped — the vendor app holds the mode word (it must clear it)";
    case "open_lease":
      return "skipped — a parking lease already stands (resume it through the parking surface)";
    case "latched_stop":
      return "skipped — an emergency stop holds it; the program writes nothing further tonight";
    case "disarm_refused":
      return "skipped — the disarm refused, so the cycle never started";
    case "deadline_passed":
      return "skipped — the no-new-act deadline passed first";
    case "park_conflict":
      return "skipped — the park conflict guard refused (armed or under a request)";
    case "resume_refused":
      return "skipped — the resume refused after the park; the lease follows the standing rules";
    case "lease_bounds":
      return "skipped — the hold does not fit the parking block's lease bounds";
    default:
      return `skipped — ${reason}`;
  }
}

/**
 * §13's recovery advisory card line: in `advise`, the operator walkthrough
 * (disarm → park → resume → re-arm → verify); in `auto`, what the program
 * did and the morning re-arm the operator owes (§4 — the designed human
 * check on a recovered pod).
 */
export function recoveryWalkthroughText(unit: HealthWatchUnitState): string | null {
  const recovery = unit.recovery;
  if (recovery.mode === "uncommissioned" || recovery.verdict === null) {
    return null;
  }
  if (recovery.mode === "advise") {
    return "Operator walkthrough (the advise posture writes nothing, ever — these are your hands): disarm → park → resume → re-arm → verify — through the standing surfaces, with the parking dialog's own confirmations.";
  }
  const owes =
    recovery.verdict === "recovered"
      ? ` The battery was left DISARMED by design — re-arm it this morning (attempt ${recovery.attemptsTotal}).`
      : "";
  return `The program ran its cycle under the auto posture and left the battery disarmed.${owes}`;
}

/**
 * The morning-after line for a recovered unit (§13): the before/after
 * evidence with the honest done-what sentence, beside A11's figures.
 */
export function recoveryMorningText(unit: HealthWatchUnitState): string | null {
  const recovery = unit.recovery;
  if (recovery.verdict !== "recovered") {
    return null;
  }
  const trailing = recovery.trailing;
  const rate =
    trailing === null ? "" : ` · ${trailing.attempted} cycle night${trailing.attempted === 1 ? "" : "s"} in the last 30 (${Math.round(trailing.attemptRate * 100)}%)`;
  const before =
    recovery.route === "b"
      ? "a phase the census cannot see + repeated no-response probes"
      : "stuck signature + a no-response probe";
  return `Before: ${before}. After: one supervised standby cycle, then a passing 300 W verification probe.${rate}`;
}

/** One unit's whole row: the census chip, the probe line, and the recovery
 * word (the uncommissioned honesty included — a site without R never sees
 * "recovery" offered as a suggestion it cannot execute). */
export function healthWatchUnitRowText(unit: HealthWatchUnitState): string {
  const recovery =
    unit.recovery.mode === "uncommissioned"
      ? "recovery not commissioned"
      : `recovery ${unit.recovery.mode}${unit.recovery.note === null ? "" : ` — ${unit.recovery.note}`}: ${recoveryVerdictText(unit)}`;
  return `${unit.unitId} — census ${censusChipText(unit)} · probe ${probeVerdictText(unit)} · ${recovery}`;
}

/**
 * The card's alert story: the first alert-tier fact across the fleet, for
 * the card's own alert styling (§11's tiers). Null while nothing is at
 * alert tier.
 */
export function healthWatchAlertText(state: HealthWatchState): string | null {
  const stuck = state.units.filter((unit) => unit.census.tier === "alert");
  const failed = state.units.filter((unit) => (unit.probe.verdict ?? "").startsWith("fail"));
  const recoveryFailed = state.units.filter(
    (unit) => unit.recovery.tier === "alert" && unit.recovery.verdict !== null,
  );
  if (recoveryFailed.length > 0) {
    const names = recoveryFailed.map((unit) => unit.unitId).join(", ");
    const first = recoveryFailed[0]!;
    return `Recovery outcome on ${names}: ${recoveryVerdictText(first)} — the defined-restart advisory is below.`;
  }
  if (failed.length > 0) {
    const names = failed.map((unit) => unit.unitId).join(", ");
    const auto = failed.some((unit) => unit.recovery.mode === "auto");
    return auto
      ? `Actuation probe FAILED on ${names} — the recovery stage will take it from here if the census also flagged it.`
      : `Actuation probe FAILED on ${names} — recorded and alerted; the recovery advisory is below (this site's recovery stage is advise — it writes nothing).`;
  }
  if (stuck.length > 0) {
    const names = stuck.map((unit) => unit.unitId).join(", ");
    return `Stuck signature persisted on ${names} — flagged ${stuck[0]!.census.nights} consecutive nights.`;
  }
  return null;
}

/**
 * §6.3's export honesty note, VERBATIM from the wire model: the probe's
 * brief discharge can export up to its own magnitude on a quiet house —
 * named so the export meter is never a surprise.
 */
export const HEALTH_WATCH_EXPORT_NOTE =
  "on a quiet house the probe's brief discharge can export up to its own magnitude for under a minute — trivial at the feed-in rate";

/** Whether any unit's census shows the parked class (the card's pinned styling gate). */
export function anyUnitParked(state: HealthWatchState): boolean {
  return state.units.some((unit) => unit.census.verdict === "excluded:parked");
}

/**
 * The defined-restart advisory (§11's terminal text, pinned verbatim): it
 * rides the card beside every recovery failure-ladder end and every
 * alert-tier probe failure so the operator has the escalation ladder's end
 * without opening a log directory.
 */
export const HEALTH_WATCH_RESTART_ADVISORY =
  "Remote recovery exhausted. Defined-restart procedure, in this order: battery button OFF for 5 s; DC off; AC off; WAIT 10 MINUTES (the fuse re-engagement lockout — do not shorten it); battery ON FIRST, then AC, then DC. Then verify telemetry resumes, Debug Mode reads Normal Mode and SysControlMode reads Remote in the vendor MiniES app. Take the logs to the installer if the pod does not return.";

/**
 * The vendor-documented on-device cross-check (§11): force-state
 * transitions land in the BMU event log — pull it after any cycle to
 * corroborate (or contradict) the ledger.
 */
export const HEALTH_WATCH_BMU_CROSS_CHECK =
  "Force-state transitions are logged in the BMU event log — pull it after any cycle as the on-device cross-check on ours.";
