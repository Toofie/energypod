# Operator console UI contract (Milestone B)

Normative behavior contract for the React operator console in `web/`. Tests
authored against this document are the acceptance contract for the console;
implementation follows them. The information architecture and tone come from
`PRODUCT_ROADMAP.md` ("Non-technical UI information architecture"); this
document pins the testable behavior for the Phase-2 surface that exists today.

## Scope

Built now: **Home**, **Batteries** (fleet + per-unit detail), **Now** (arming,
dispatch, stop, acknowledgements), **Activity** (audit timeline), and the app
shell (navigation, connection status, authentication). Navigation entries for
Energy Flow, Insights, Schedule, and Plan history render explicit
"not available yet" states — an honest unavailable state is contracted, never
a hidden or dead link.

Not in this milestone: the separate browser-session/OIDC boundary (contracted
separately in API_CONTRACTS.md), Settings editing, plans/schedules UI,
tariff/provider views.

## Data sources

- REST `/api/v1`: `snapshot`, `health`, `audit` (with `after_sequence` cursor),
  `intents` (dispatch), `arm`, `disarm`, `emergency-stop` (+ acknowledgement),
  `units/{id}/inhibit/acknowledge`, `events/session` (browser WS ticket).
- Every mutation carries an `Idempotency-Key` header: the client generates one
  per call unless the caller supplies a key to reuse for retries of the same
  operator action.
- WebSocket `/api/v1/events`: authoritative snapshot envelope first, then
  ordered events; `resync_required` (any reason) triggers a snapshot refetch
  and reconnect; connection loss shows the disconnected state and retries with
  the last sequence as the cursor when reconnecting. Browsers authenticate the
  handshake with a single-use ticket via `Sec-WebSocket-Protocol:
  energypod-events, <ticket>` obtained from `events/session` (API_CONTRACTS);
  the token never appears in a URL.
- Wire casing: the service serializes enums as lowercase StrEnum values
  (`"disarmed"`, `"armed_idle"`, `"active"`, `"inhibited"`, `"charge"`,
  `"discharge"`, `"idle"`, `"good"` ...). All fixtures and comparisons use the
  lowercase wire values.
- The console is a client of the guarded API only. It never imports anything
  from the controller, never derives safety decisions, and never talks to a
  gateway.

## Authentication model (console side)

- Token entry screen: a single field, paste-only token, submit labeled
  "Unlock". Token lives in memory only (never localStorage/sessionStorage,
  never URL).
- Wrong or expired token surfaces the API's error envelope message with a
  retry; no partial data renders.
- Sign-out clears the token and all cached data; the app returns to the entry
  screen. A 401 on any call does the same.
- The UI displays the caller's effective permissions only through what the API
  accepts or rejects: it does not guess scopes, but it MUST surface rejection
  envelopes verbatim (code + message) where an action was refused.

## Global shell behavior

- A status banner is always visible with a lifecycle badge for the fleet:
  observe-only, disarmed, armed, active, limited, inhibited — text label plus
  icon plus color (never color alone). When units disagree, the banner shows
  the most conservative state and the unit list disambiguates.
- Connection indicator distinguishes: page loaded, API reachable (health ok),
  event stream live, and control readiness — four separate facts, never
  collapsed into one "online" light.
- Stop is reachable from every screen that can initiate or display active
  control (one action, then a confirmation dialog with the affected units).
- Keyboard: every control is reachable and operable by keyboard; focus is
  visible; moving between views moves focus sensibly; dialogs trap and restore
  focus.
- Live updates: status changes announce via an ARIA live region (polite);
  emergency events (stop latched, inhibit latched, connection lost during
  active control) announce assertively.
- Reduced motion is honored (`prefers-reduced-motion`: no animated flows).
- Every data-driven view implements distinct empty, loading, disconnected,
  stale, partial, and error states. Stale = data older than its configured
  freshness bound, shown with the age next to the value.

## Home

Answers, in order: safe and connected? what is powering the home now? how full
are the batteries? what happens next? anything limiting operation?

- Fleet reserve (combined charge level) with per-unit breakdown.
- Current power figures per unit: requested vs allowed vs actual, visually and
  textually distinct; a limited action is never presented as fully delivered.
- Next planned action if any (from active intents), with expiry countdown.
- Limiting factors list (from health reasons and snapshot quality), in plain
  language, each expandable to its raw reason code.
- The banner badge is present and correct for every fleet state.

## Batteries

- Fleet view: one card per unit (MID, RHS, LHS — named, never positional
  assumptions). Card: charge level, direction+power, availability, temperature
  range, cell spread, data age, warnings; inhibited/latched units show their
  latch state and an acknowledge control when permitted.
- Unit detail tabs:
  - **Summary**: condition, power, limits, communications, recent trend.
  - **Cells**: voltage distribution (min/max/spread readable as text, not
    color-only), temperatures, data completeness.
  - **Events**: active and historical warnings/faults with plain-English
    text plus the raw code on demand.
  - **Details**: advanced measurements and data-quality map, expert-oriented.
- A unit with missing/partial telemetry renders exactly which fields are
  missing and their age; it never renders fabricated or zero-filled values
  presented as measurements.

## Now (control)

- Current request card: what was requested, what the safety system allowed,
  what is actually happening, remaining time — four separate facts.
- Arm: a deliberate flow — readiness checklist (unit qualified, not latched,
  policy visible), explicit confirmation ("ARM"), per-unit outcomes with
  refusal reasons surfaced verbatim. Arm is refused interactively when the
  API refuses.
- Disarm: always offered for armed units, with confirmation.
- Dispatch: charge and discharge are separate actions, each with a preview
  (direction, watts, units, limit applied, expiry) before an explicit confirm.
  Inputs are constrained (positive watts, bounded TTL); API validation errors
  render inline with the field.
- Emergency stop: available whenever control is possible; confirmation names
  the affected units; after stopping, the stop id is shown with an
  acknowledgement control requiring exact-id confirmation.
- Inhibit acknowledgement: shown for latched units, requires explicit confirm,
  explains that acknowledgement clears the latch but re-arming is still a
  separate deliberate step.

## Activity

- Unified, newest-first timeline from `audit` with cursor pagination (Load
  more). Every control entry shows: who/what requested it, what was decided,
  what happened, and why — mapped from the audit record's principal, decision
  status, result, and reason codes (plain language first, raw codes on demand).
- Filterable by unit and by kind (observations, decisions, arming, stops,
  acknowledgements). Filters are URL-independent and keyboard-operable.

## State and error contract (all views)

- Loading: skeleton, never spinner-only, no layout shift when data lands.
- Empty: explains what will appear here and what to do first.
- Disconnected: banner + per-view notice, last data dimmed with age, retry
  behavior automatic for the socket and manual for REST.
- Stale: age shown next to values past freshness, values dimmed, not hidden.
- Partial: missing fields named explicitly.
- Error: the API's error envelope rendered (code and message), with a retry
  action; never a bare stack trace or silent failure.

## Test conventions (web/)

- vitest + @testing-library/react + user-event; behavior over implementation:
  assert on roles, labels, and visible text — never on class names or DOM
  internals.
- Every view suite must drive at least: a healthy render, each contracted
  state above, one keyboard-only interaction flow, and one refused-action path
  (API error envelope surfaced verbatim).
- The API/WS layer is mocked at the fetch/WebSocket boundary with the exact
  envelope shapes from API_CONTRACTS.md; tests never construct server state.
- Accessibility assertions are explicit (roles, names, live-region
  announcements, focus order); no color-only or icon-only assertions.
