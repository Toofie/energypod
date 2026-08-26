# Flow view design brief — the one opinionated design

Status: accepted design for the Flow view's polish round. The base view (commit
`36c27e0`) already owns the data model, the sign-to-words discipline, the
per-phase + fleet columns, and the story line; this brief pins every remaining
visual decision so the polish round executes without further choices. Sources
are numbered `[n]` and listed in full at the end; every external claim cites
one. Tokens reference `web/src/styles.css`; behavior references
`web/src/views/flow/flow.ts` (sign conventions, wording, arrow scale) and
`web/src/lib/format.ts` (the two-decimal display discipline).

**One correction up front: this console is light-mode-first.** `styles.css`
pins `color-scheme: light` with white cards (`--card: #ffffff`) on a pale
surface (`--surface: #f7f8fa`). The brief was commissioned as "dark-mode-first
utilities aesthetic"; the repo disagrees, and the brief matches the repo. That
is also the research-backed call: NN/g's review of contrast-polarity studies
finds light mode outperforms dark for glanceable reading and visual acuity
"most of the time," with dark mode as an accessibility option rather than a
default [15]. Dark mode is therefore ruled out of this round entirely, not
flipped on automatically (§ Visual language, end).

**The survey in five lines (what the best consumer apps agree on).** Lead
with the whole-home picture: Tesla's app opens energy with "how much energy
is stored from solar, used by your home, or exported to the grid" [1]. An
explicit flow diagram is table stakes: Growatt's ShinePhone markets "energy
flow diagrams [that] clearly show generation, consumption, storage, and power
movement," with "clear visibility of battery level … load power and grid
interaction" [2]. Live beats layered: Enphase's own listings sell period
reports while a third-party app wins users purely by showing live status "in
less than a second" [3]. Australians buy automation narrated in plain words:
Amber leads with live prices and SmartShift's plain-language buy/sell/hold
decisions [4]; Solar Analytics sells performance assurance and plan
optimization, not gauges [5]. And the flow graphic changes with the sun only
where solar is measured — mySolarEdge ships explicit "power flow day and
night" views [6]. None of them makes the homeowner read a sign.

---

## (a) THE LAYOUT

**Structure (top to bottom of the view, unchanged in kind, re-ordered in one
place):**

1. **Heading row** — `h2` "Energy flow" (1.2 rem) left, the connection line
   ("Live — this picture refreshes every few seconds." / "lost" / "connecting")
   right. Exists; keep.
2. **THE STORY SENTENCE — one full-width line, directly under the heading,
   above the diagram.** This is where the story lives, permanently: the
   advisers' own sentence while a window runs, the measured composition
   otherwise, "Carrying out a power request — …" prefixed while a live
   command exists (`flow.ts: flowStory / nightStory / excessStory` — already
   built, keep verbatim). Card on `--card`, 4 px left border in `--active`,
   1.05 rem text. The story is the diagram's caption, not its legend.
3. **The diagram — four columns in one responsive grid
   (`repeat(auto-fit, minmax(15rem, 1fr))`, gap 0.75 rem):**
   - **Column 1: "Whole site" (the fleet rollup) — MOVED FIRST.** Reading
     order and DOM order both. Rationale: the glance path is story → whole
     site → phase detail, which is also the reading order a screen reader
     speaks; every best-in-class consumer app leads the whole-home picture
     before its parts (Tesla's app leads with "how much energy is stored from
     solar, used by your home, or exported to the grid" as the headline
     tri-flow [1]; mySolarEdge's power-flow graphic is the app's landing
     display [6]). On narrow screens (one column) fleet-first is the only
     sane order. The fleet column keeps its distinct treatment: 2 px
     `--active` border, tinted card `#f4f8ff`.
   - **Columns 2–4: one per phase, in the snapshot's unit order (`lhs`, `mid`,
     `rhs` — named, never "Phase 1").** Each column: phase name (0.8 rem,
     uppercase, letterspaced, `--ink-soft`), optional lifecycle note, the bus
     (below), then the three node cards.
4. **Command overlay** ("Commanded vs delivering") while any phase carries a
   live commanded figure; then the **solar-honesty footnote**; both exist,
   keep.

**The bus inside each column (the only geometry that changes):**

- One horizontal rail — **the phase conductor** — 3 px, `--line`, rounded
  caps, spanning the column at the top of the SVG (y = 12 in a `0 0 240 96`
  viewBox). The rail is the phase itself: everything above/below it is either
  feeding it or drawing from it.
- **Three stub slots drop from the rail to the node cards: GRID, BATTERY,
  HOME, left to right, x = 40 / 120 / 200** — pinned to the thirds of the
  viewBox so each stub lands on the center of the node card beneath it
  (the current 48/120/192 misses the card centers; 40/200 fix it).
- **Node cards dock immediately under the SVG** (no gap beyond 0.25 rem) so
  the arrowhead visually reaches its node. Each card: name (0.72 rem
  uppercase), the worded figure (0.95 rem, weight 600, `font-variant-numeric:
  tabular-nums`), optional extra line (SoC "48.5% charged", 0.8 rem).
- The bus stays `aria-hidden` — it is decorative duplication; the worded
  figures and each column's `aria-label` carry every fact as text (already
  built, keep).

**Why a bus, not a Sankey:** with three nodes all meeting one conductor there
is no second stage to flow *through* — a Sankey needs a directed acyclic graph
of stages with node rectangles scaled by value [8][9]; our edges are
bidirectional (import/export, charge/discharge on the same edge) and the
phases are parallel, not sequential. Sankey is the right form when "a total
quantity is traced from origins through intermediate steps to destinations"
[7] — that is not this shape. A bus/rail diagram is the honest form here, and
it is also the electrical idiom (a single-line diagram) the phases actually
are. d3-sankey is explicitly rejected in § Implementation.

**Fleet-vs-per-phase semantics (pinned):** the fleet column is the *summed*
picture — its Grid and Battery slots may legally show **both directions at
once** when phases pull apart (see split stubs, below); its Home figure names
its scope when a phase is missing ("— across the 2 of 3 phases reporting",
already built). The one **global width scale** (largest measured flow on
screen, `arrowScaleW`) is shared by all four columns so thickness compares
across columns; do not normalize per column — a per-column scale is a small
lie.

---

## (b) THE VISUAL LANGUAGE

**Color: color names the NODE FAMILY, never the direction.** Direction is
carried by the arrowhead geometry plus the word beside it — the existing
"never a raw sign" discipline (`flow.ts`), extended to "never color-alone
direction." This is the colorblind-safe construction: the Okabe-Ito
recommendation is redundant coding — "use not only different colors but also a
combination of different shapes, positions, line types" [14] — and here
direction has *three* redundant channels (word, head, thickness).

| Element | Token | Hex | Role |
|---|---|---|---|
| Grid stub / Grid node name | `--active` | `#1d5fa0` | the external network |
| Battery stub / Battery node name | `--armed` | `#8a6d1a` | stored energy |
| Home stub / Home node name | `--ink-soft` | `#56637a` | the neutral terminus (the house) |
| Rail | `--line` | `#d9dee7` | the phase conductor, recessive |
| Arrowheads (all) | `--ink` | `#1f2733` | one neutral ink; the head names direction, the stroke names the family |
| Idle ring | node-family color | — | hollow, 7 px ring |
| Story border / fleet border | `--active` | `#1d5fa0` | the view's accent |

Validated, not eyeballed (the dataviz six-checks validator, this console's
white-card surface): grid `#1d5fa0` + battery `#8a6d1a` as the categorical
pair **pass every gate** — worst adjacent CVD ΔE 21.5 (protan; target ≥8),
normal-vision ΔE 23.4, lightness band and chroma floor clean. Home's
`--ink-soft` is deliberately NOT a third categorical hue: it sits below the
chroma floor (reads gray) *by design* — the house is the neutral sink, and its
identity is carried by its fixed position, its label, and its worded figure,
never by hue alone. Text contrast on the white card, computed: `--active`
6.55:1, `--armed` 4.90:1, `--ink-soft` 6.07:1 — all ≥ 4.5:1 (AA body text).

**Ribbon/arrow treatment (proportional widths):**

- Active stub: a straight vertical line, stroke width linear in watts —
  floor **2 px**, max **10 px** (`ARROW_MIN_PX`/`ARROW_MAX_PX`, `flow.ts` —
  keep). Linear, not square-root: the Sankey lineage's whole point is that
  "width [is] proportional to the importance of the flow" [7]; a linear map
  keeps the thickness ratio honest (2× the watts = 2× the ink).
- Arrowhead: neutral `--ink`, constant *shape*, two sizes — small head
  (8×7 marker) under 4 px stroke, large head (12×10) at 4 px and above
  (`markerUnits="userSpaceOnUse"`, `orient="auto-start-reverse"`, one def per
  size). A constant single size makes a 2 px trickle wear a cartoon head;
  two steps solve it without per-pixel math.
- **Direction language (words + color + arrow — never sign alone):**
  - Grid: **Importing** = head up *into* the rail (the grid feeds the phase);
    **Exporting** = head down *out of* the rail (surplus leaves the site).
  - Battery: **Discharging** = head up into the rail; **Charging** = head
    down toward the battery node.
  - Home: **Using** = head down toward the Home card, always (the house only
    draws; a measured 0 W renders the idle ring and the word "Idle", never a
    fake load).
- **Idle** (measured 0 W): NO stub line. A **7 px hollow ring** (2 px stroke,
  node-family color) centered in the slot just below the rail. A dashed line
  reads as "a weak flow"; a ring reads "connected, nothing moving."
- **Unknown** ("not available"): a **1.5 px dotted** vertical stub
  (`stroke-dasharray: 1 7`) in `--ink-soft` — presence without a claim. Never
  zero-filled (`flow.ts` pins this; keep).
- **Split stubs (the fleet's both-directions case) — the one honesty upgrade
  that matters:** when the phases pull apart, the fleet column's Grid (or
  Battery) slot renders **two half-slot stubs side by side** (offset ±14 px
  from the slot center): the import side head-up with its own width, the
  export side head-down with its own width. The current single both-headed
  max-width stub encodes two opposite flows in one thickness — exactly the
  sign-confusion pitfall; two stubs make the split legible at a glance and the
  worded figure below already carries the exact numbers ("Importing 412 W ·
  Exporting 300 W").
- **Disconnected phase:** the whole column dims to 0.6 opacity, its stubs go
  dotted-unknown, the lifecycle note renders ("No contact — these figures are
  the last known"), and the last-known worded figures stay visible — dimmed,
  never gone (the shell's standing rule).

**Day/night theming: not warranted.** mySolarEdge ships distinct day and
night power-flow graphics [6] because a solar node appears and disappears
with the sun; this site has no solar node at all (the pinned honesty rule —
the footnote says so once), and the night window is already narrated by the
story sentence ("Night charging is running/holding…"). The honest night signal
is the flows themselves. No sky gradients, no moon icons.

**Dark mode: out of scope, and measurably not a flip.** Re-running the
validator against a dark surface fails the current tokens outright
(`--active` 2.65:1, `--ink-soft` 2.87:1 — below the 3:1 mark floor), which is
the expected result of the dark-mode literature: dark themes need their own
re-stepped colors (desaturated, lightened accents), not inverted ones [16],
and NN/g recommends defaulting light with dark as an opt-in [15]. If a dark
theme is ever built, re-step the flow tokens for the dark surface, validate,
and ship them as custom-property overrides in one block [16] — until then,
nothing here should react to `prefers-color-scheme`.

---

## (c) MOTION

Contract first: `UI_CONTRACTS.md` pins "Reduced motion is honored
(`prefers-reduced-motion`: no animated flows)," and `styles.css` already
globally kills animation/transition under that preference (`animation: none
!important`), so every CSS-only effect below inherits its fallback for free.
WCAG 2.3.3 (AAA) frames the rule: non-essential motion must be
avoidable/dismissible [12]; MDN's implementation guidance adds that the
fallback should replace movement with non-motion cues such as opacity changes,
not frozen movement [11]. Duration budgets follow NN/g: ~100 ms feels
immediate, 100–400 ms is the productive range, ≥500 ms reads as drag, and
easing is mandatory (linear looks unnatural) [10].

**Exactly four things move:**

1. **The march (the aliveness cue).** Every *active* stub carries a texture
   overlay: a second `<line>` on the same coordinates, same color, stroke
   width = solid width × 0.55, `stroke-dasharray: 8 12`, animated by
   `@keyframes flow-march { to { stroke-dashoffset: -40 } }` (−40 = two dash
   periods; the `stroke-dashoffset` slide is the standard technique [13]).
   **The invariant that makes this trivial: each stub's own draw direction
   runs source → target** (already true in `FlowBus` — into-bus stubs are
   drawn bottom→top, to-node top→bottom), so a single negative-offset keyframe
   marches every stub in its arrowhead's direction with zero per-aim variants.
   Speed maps to magnitude:
   `animation-duration: calc(3200ms − 1600ms × share)` where
   `share = |watts| / scaleMax` — the largest flow on screen cycles in 1.6 s,
   the smallest in 3.2 s. Calm at rest, quick at full power, never frantic
   (ambient loop motion belongs at the slow end of the budget [10]).
   Reduced motion: the overlay is hidden (`opacity: 0`) under the media
   query — **never frozen mid-march** (static dashes read as a broken line).
2. **Thickness transitions.** `stroke-width` transitions over **400 ms** with
   `cubic-bezier(0.2, 0, 0, 1)` (ease-out family — entering emphasis [10]) on
   every stub, so each ~2.5 s snapshot breathes the new magnitude instead of
   snapping. Killed globally by the reduced-motion rule (instant snap — the
   information is the value, not the tween).
3. **The number tick.** The watt figure inside each worded line tweens from
   its previous value to the new one over **300 ms**, ease-out, `requestAni
   mationFrame`-driven, only when the direction word is unchanged (a state
   change — Importing→Idle, Charging→Discharging — swaps instantly; rolling a
   counter across a semantic boundary is a lie). `tabular-nums` keeps the
   line width stable while it counts. Reduced motion: snap (the existing
   `usePrefersReducedMotion` hook gates the JS tween).
4. **The commanded-not-moving pulse.** While the command overlay says a phase
   is commanded but its measured watts are still 0 ("not moving yet"), that
   phase's battery idle ring pulses `opacity 1 ↔ 0.5`, **1.6 s cycle** — the
   one honest way to say "we asked; nothing has happened yet" without drawing
   a fake flow. Reduced motion: static ring; the overlay text carries the
   whole message.

**What never moves:** node cards, the rail, stub endpoints and slot
positions, labels, the story sentence (it re-renders as text), and the
column grid. Endpoint/geometry changes (an aim flip, a fleet stub splitting)
render as a fresh stub with a **150 ms opacity fade-in** — SVG geometry
attributes are not reliably CSS-transitionable across browsers, and the CSS
`d`/path-morphing route is explicitly not Baseline [17]; do not reach for it.
FLIP [18] is noted for completeness and rejected here: nothing reflows, so
there is no First/Last/Invert/Play to do — it becomes relevant only if node
reordering is ever animated.

**Stale/connection-lost:** the march stops (overlay hidden — a moving
diagram claiming liveness while disconnected is dishonest), the diagram dims
to 0.75 (already built), the connection line explains, the figures stay.

---

## (d) THE STATE MATRIX

Ten states; the builder renders each for screenshot review (fake/simulator
snapshots already exercise most of these paths). One line each — the visual
description of the *whole view*, story first:

1. **All idle (deep night, between windows).** Every slot an idle ring; story
   "Nothing is flowing — the grid connection, the batteries and the house are
   all idle."; nothing marches.
2. **Fleet night charge (the known 00:00–06:00 writer).** Three thick gold
   battery stubs marching downward (2,500 W each ≈ max width), three blue
   grid stubs marching up into the rails, Home modest; fleet column mirrors
   the sum; story "All the batteries are charging 7,500 W in total…".
3. **Daytime autonomy (self-charge, no command).** Gold battery stubs
   charging at mid width, grid slot exporting on some phases (head-down
   blue); story is the measured composition; the solar footnote earns its
   place under the diagram.
4. **Exporting while charging (the excess-solar adviser active).** On the
   surplus phases: gold battery stub head-down *and* blue grid stub
   head-down, both marching — power visibly leaving while the battery fills;
   story "Solar-surplus charging is active — …".
5. **Mixed battery directions (concurrent per-unit commands).** mid's gold
   stub thick head-down (charging 2,000 W), rhs's thinner head-up
   (discharging 1,000 W), lhs idle ring; the fleet Battery slot splits into
   two half-stubs; story "rhs is discharging 1,000 W while mid charges 2,000 W
   in total" per `measuredStory` (discharging clause first).
6. **Split grid (phases pulling different ways).** lhs blue head-up
   (importing) beside rhs blue head-down (exporting); the fleet Grid slot
   splits into two half-stubs with distinct widths; story "The phases are
   pulling different ways…".
7. **One pod down.** One phase column at 0.6 opacity with dotted unknown
   stubs and the note "No contact — these figures are the last known", the
   last-known figures still legible; the fleet Home figure names its scope
   ("— across the 2 of 3 phases reporting"); the other columns march on.
8. **Night hold (demand hold, the EV-spike case).** Batteries idle rings,
   Home stub thick head-down, grid stub head-up carrying the house; story
   "Night charging is holding — house demand is …, so the batteries neither
   drain nor cycle while the grid meets the house."
9. **Commanded but not yet moving.** Command overlay rows render ("mid —
   commanded 2,000 W charge, not moving yet"), the commanded phase's battery
   idle ring pulses, no gold flow is drawn until watts are measured; story
   carries the "Carrying out a power request —" prefix.
10. **Connection lost.** March stops everywhere, diagram dimmed to 0.75,
    connection line "Connection lost — showing the last known picture;
    reconnecting automatically.", figures and story frozen at last-known.

(Emergency-stop latched is deliberately absent: it is a shell-level banner
state, not a flow state — the flows it produces are simply idle.)

---

## (e) CRAFT DETAILS

**Typography hierarchy** (all in the existing `"Segoe UI", system-ui…` stack;
no new faces — large standalone numbers keep proportional figures, only
aligned figures go tabular):

| Role | Size / weight | Notes |
|---|---|---|
| View heading | 1.2 rem / 700 | exists |
| Story sentence | 1.05 rem / 400, line-height 1.45 | the view's voice; one sentence, never two |
| Connection line | 0.9 rem / 400, `--ink-soft`; live=`--yes`, lost=`--no` | exists |
| Phase / "Whole site" name | 0.8 rem / 700, uppercase, 0.08 em tracking | trimmed from 1 rem — the column is about the flows, not its title |
| Node name | 0.72 rem / 700, uppercase | node-family color |
| Node figure | 0.95 rem / 600, `tabular-nums` | the worded line |
| Node extra (SoC) | 0.8 rem / 400, `--ink-soft` | |
| Lifecycle note | 0.8 rem / 400, `--warn` | exists |
| Footnote / overlay rows | 0.85–0.95 rem | exist |

**Number formatting — the two-decimal discipline, verbatim.** Every figure
routes through `formatWatts` / `formatPercent` (`web/src/lib/format.ts`):
at most two decimals, integers as integers ("1,500 W" never "1,500.00 W"),
trailing zeros trimmed, thousands grouped, no scientific notation, and no
figure ever renders a raw sign — the direction word carries it. The diagram
invents no formatting of its own.

**Hover / touch affordances.** The node cards are the hit surfaces (and are
already >44 px). Hovering or keyboard-focusing a node card raises its stub to
full opacity (default 0.92) and tints the card's border with the node-family
color — the one linkage cue between card and stroke. No tooltips: every
number is already on screen as text (the anti-tooltip pattern this view is
built on). No gestures, no drag, no zoom.

**Accessibility.** The bus is `aria-hidden` decorative duplication; each
column carries its full state as an `aria-label` (exists — keep the
"Phase {id} — grid: …; battery: … (SoC); house: …" composition); identity is
never color-alone (position + label + word, per the redundant-coding rule
[14]); every ink passes 4.5:1 on the white card (§ b); focus-visible outlines
are global (3 px `--focus`); reduced motion is total (§ c), satisfying the
UI contract and WCAG 2.3.3 [12]. The story sentence is plain text, not a live
region — it changes every ~2.5 s and would chatter; the shell's existing
announcement regions handle status changes.

**Performance at the 2.5 s cadence.** Four small SVGs of ≤8 lines re-rendering
every ~2.5 s is trivial for React 19; all continuous motion is CSS
(`stroke-dashoffset`, `stroke-width`, `opacity`); the only JS animation is the
300 ms number tick on change. No `will-change`, no per-frame JS, no listeners
on the SVG. uPlot stays where it is (history charts) — see below.

**Libraries — the recommendation, without waffle: none.**

- **d3-sankey: rejected.** It lays out a directed *acyclic* graph whose link
  widths scale into node rectangles over ≥2 stages [9]; this view is one bus
  with three bidirectional edges per phase. Adopting it would mean fighting
  its layout (nodeSort/linkSort/iterations) to reproduce a diagram that is
  four fixed rails, plus a d3-array/d3-path/d3-shape dependency chain, for
  zero expressive gain.
- **uPlot: not here (and that is its own recommendation).** It is a
  time-series canvas lib (~50 KB, lines/areas/bars/OHLC) whose explicit
  non-features include animations and any chart type outside time series
  [19] — the exact opposite of an animated SVG flow diagram. It remains the
  right tool for the history charts it was chosen for.
- **Hand-rolled SVG: stays.** The current approach — declarative React SVG,
  CSS-only motion, geometry rebuilt on state change with a 150 ms fade — is
  the correct one for this shape; morphing `d` attributes is off the table
  (the CSS `d` property is not Baseline [17]) and FLIP solves a problem this
  view does not have [18].

---

## (f) SOURCES

Consumer energy dashboards:

1. Tesla app — App Store listing (energy monitoring description: "monitor how
   energy is stored from solar, used by your home, or exported to the grid"):
   https://itunes.apple.com/search?term=tesla&entity=software&country=us&limit=2
2. Growatt ShinePhone / ShinePhone 2.0 — App Store listings ("Energy flow
   diagrams clearly show generation, consumption, storage, and power
   movement"; "clear visibility of battery level, device operation, load
   power and grid interaction"; the third-party Growatt Companion widget as
   evidence of live flow demand):
   https://itunes.apple.com/search?term=growatt&entity=software&country=au&limit=5
3. Enphase ecosystem — App Store search (Enphase's own listings emphasize
   period reporting; the third-party "Solar Live for Enphase" — "shows the
   status in less than a second, and refreshes every second", local-network
   live status, no battery support — is the demand signal for sub-second
   live flow):
   https://itunes.apple.com/search?term=enphase&entity=software&country=au&limit=10
4. Amber (AU) — live wholesale price and feed-in rate in-app, SmartShift
   battery automation: https://amber.com.au/
5. Solar Analytics (AU) — monitoring + Plan Optimiser positioning:
   https://www.solaranalytics.com.au/
6. SolarEdge mySolarEdge — the "power flow day and night" homeowner graphic:
   https://marketing.solaredge.com/rose/mysolaredge-for-homeowners-en
   (product page: https://www.solaredge.com/aus/products/software-tools/mysolaredge)

Flow-diagram and Sankey craft:

7. Data-to-Viz — Sankey diagrams (use cases, "width proportional to the
   importance of the flow", clutter/crossing caveats):
   https://www.data-to-viz.com/graph/sankey.html
8. Schmidt, M. (2008), "The Sankey Diagram in Energy and Material Flow
   Management: Part I", *Journal of Industrial Ecology* (history, strengths —
   intuitive readability, conservation transparency — and weaknesses):
   https://onlinelibrary.wiley.com/doi/10.1111/j.1530-9290.2008.00004.x
9. d3-sankey (API and DAG/acyclicity requirements):
   https://github.com/d3/d3-sankey

Motion and micro-interaction:

10. NN/g, animation duration (100–400 ms budget, easing, too-slow is the
    common failure, Pratt et al. 2010 attention research):
    https://www.nngroup.com/articles/animation-duration/
11. MDN, `prefers-reduced-motion` (vestibular triggers; opacity-based
    fallbacks): https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion
12. WCAG 2.3.3 Animation from Interactions (AAA; motion avoidable unless
    essential): https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions.html
13. CSS-Tricks, SVG line animation (`stroke-dasharray`/`stroke-dashoffset`
    mechanics, `pathLength`, cross-browser notes):
    https://css-tricks.com/svg-line-animation-works/

Color, dark mode, and implementation:

14. Okabe & Ito, Color Universal Design (the colorblind-safe palette and the
    redundant-coding recommendation: "different shapes, positions, line types
    and coloring patterns"):
    https://jfly.uni-koeln.de/color/
15. NN/g, dark mode vs light mode (light mode outperforms for most users;
    dark as an option): https://www.nngroup.com/articles/dark-mode/
16. CSS-Tricks, dark mode guide (custom-property theming; dark needs
    re-stepped, desaturated accents — not inversion; 4.5:1 verification):
    https://css-tricks.com/a-complete-guide-to-dark-mode-on-the-web/
17. MDN, CSS `d` property (path interpolation exists but is not Baseline —
    the reason not to morph paths): https://developer.mozilla.org/en-US/docs/Web/CSS/d
18. Paul Lewis, FLIP (First/Last/Invert/Play; transform/opacity
    compositor-friendly technique — evaluated and not needed here):
    https://aerotwist.com/blog/flip-your-animations/
19. uPlot (canvas, time-series scope, explicit non-features incl. animations —
    why it stays out of the flow view):
    https://github.com/leeoniya/uPlot

Palette validation was run locally with the dataviz skill's
`scripts/validate_palette.js` (grid+battery pair on `#ffffff`: all checks
pass, worst CVD ΔE 21.5; full trio on dark `#1a1a19`: fails, the dark-mode
evidence).

## Palette validation evidence

Round 1.5 (2026-08-26): the validator is now VENDORED in-repo at
`web/scripts/design-tools/validate-palette.mjs` (verbatim copy of the dataviz
skill's `validate_palette.js`; provenance header inside; one-line CLI-dispatch
deviation documented there so the renamed copy cannot silently no-op). The
bay's family trio re-validated against the tile surface it lives on:

```
$ node web/scripts/design-tools/validate-palette.mjs "#4595ec,#c78202,#476aa4" --mode dark --surface "#11161e"
Palette (dark, surface #11161e, categorical): 3 slots
  [PASS] Lightness band         all 3 inside L 0.48–0.67
  [PASS] Chroma floor           all 3 >= 0.1
  [PASS] CVD separation         worst adjacent #476aa4↔#c78202 ΔE 23.4 (protan) · tritan 24.7
  [PASS] Normal-vision floor    worst adjacent #476aa4↔#c78202 ΔE 27.8 (normal)
  [PASS] Contrast vs surface    all 3 >= 3:1

  → ALL CHECKS PASS  (CVD in the 6–8 floor band is legal ONLY with secondary encoding: direct labels, gaps, or texture)
  scope: categorical palettes only. For a lone status/text color check WCAG
  text contrast; for a sequential ramp, lightness monotonicity.

(exit code 0)
```

Round-1.5 contrast computations (WCAG ratios, computed not eyeballed):

Focus rings (blended ring color vs BOTH surfaces it lands on — required ≥3:1):
    grid    rgba(125,180,255,.62) → 4.01 vs tile #11161e · 4.08 vs bay #0b1017   (was .45 → 2.70 / 2.70 FAIL)
    battery rgba(255,194,70,.60)  → 4.74 vs tile          · 4.83 vs bay           (was .45 → 3.20 / 3.20)
    home    rgba(160,185,220,.62) → 4.21 vs tile          · 4.28 vs bay           (was .40 → 2.81 / 2.81 FAIL)

Downed zone + connection lost (single ×0.75 dim after the chrome-not-words
repair; worst surface per token; required ≥4.5:1):
    figure #f2f6fb            9.24      node name home #c2ccdc   6.44
    fig-word/scope/soc-word  5.35 (α .66→.76; was 4.37 FAIL)
    fig-sep                  4.74 (α .40→.70; was 2.44 FAIL)
    node name grid #6cb2ff    4.87      fleet name #6cb2ff       4.94
    soc-pct/battery name      6.44      phase-note #ffd489       6.96
    phase-name               4.78 (#93a1b5→#9aa8bd; was 4.45 FAIL)

## The living-stream grammar (round 2 of the design loop)

Round 2 retired the thickness-ribbon grammar outright — the operator's words:
"I don't like the lines representing the bars of like thickness… every element
is up for grabs." Magnitude encoded as stroke width is gone: no rim/track/march
trio, no width-scaled anything. What replaced it is a two-layer bus per column:
the structural SVG keeps rail, collars, idle rings (+ commanded pulse), unknown
dotted stubs, and one neutral arrowhead docked on each active slot's curved
conduit; underneath it, a `<canvas class="flow-bus-glow">` runs a small Canvas2D
light engine (`web/src/views/flow/glow.ts`) that paints LIVING PARTICLE STREAMS
along those same curves. Magnitude now survives four ways: the worded figures
(primary, unchanged), the streams' density/speed/glow, terminal bloom size, and
two discrete head sizes — never continuous thickness again.

Geometry (`web/src/views/flow/flowGeometry.ts`) is pure and shared by both
layers, so the SVG path and the particles' track are literally the same object:

- `conduitPath(slotX, aim, splitSide?)` builds each stub as a gentle cubic
  S-curve from a rail junction `(junctionX, 16)` down to its landing
  `(landX, 78)`. Outer slots bias their junction outward (grid −10, home +10)
  so single conduits fan slightly; battery rides plumb unless it splits.
  Split twins offset junctions ∓10 and landings ∓14 — analytic x-gap 20 units,
  unit-tested to keep ≥4 units of clearance sampled 129×129 over both curves.
- Paths are stored ALONG THE FLOW: `to-node` runs junction→landing,
  `into-bus` is reversed. `pointAt`/`tangentAt` (de Casteljau + derivative,
  clamped, NaN-hostile) let the engine place particles and let SVG's
  `markerEnd` orient heads along the tangent — direction stays tri-redundant:
  word, head, and now the streams' own travel.
- The retired-era constants survive verbatim (RAIL_Y 12, STUB bounds 14/78,
  RING_Y 21, SLOT_X {40,120,200}, SPLIT_OFFSET 14): one pinned source, three
  readers (SVG, engine, tests).

Engine laws (`glow.ts`, ~zero-dep, one module-level rAF ticker for ≤4
canvases):

- Spawn rate `2 + 10·share` particles/s; speed `30 + 80·share` units/s ±10%
  jitter; radius `0.8 + 1.2·share`; sprite alpha `0.35 + 0.45·share`. Share is
  `|watts| / scaleMax` smoothed with τ = 400 ms so snapshot flips glide.
- Light is pre-rendered radial-gradient SPRITES per family tone blitted with
  `globalCompositeOperation: "lighter"` (no per-frame gradient allocation);
  motion-blur streaks connect each particle to its previous position; lateral
  sine wander ≤1.2 units keeps streams organic without ever leaving their
  conduit's corridor.
- Terminals breathe: blooms grow 6→14 units ∝ share, ±7% at a 2400 ms period,
  phases staggered per slot so columns never pulse in lockstep.
- The rail itself energizes between through-flow: when a column both feeds and
  drains the bus, a faint particle chain lights the segment between the outer
  active junctions, intensity = min(intoSum, outSum).
- FULL-STOP matrix (no frames, ticker self-evicts): document hidden, connection
  ≠ 'live', zero active stubs, or reduced-motion preference. dt clamped at
  50 ms so a backgrounded tab never spawns a catch-up burst on return.
- Reduced motion never starts the ticker; instead ONE static frame paints
  sprite chains along every path, final-size terminal pools, and ~5
  deterministic particles — the picture without the motion.
- jsdom safety: one module-level probe checks that a canvas can actually yield
  a 2d context; under vitest the renderer becomes a permanent no-op, so tests
  assert structure while the browser asserts light.

Decisions worth remembering:

1. NO NEW TINTS. Sprites, streaks, blooms, and conduit strokes reuse the
   round-1 validated families exactly (cores #bfdcff/#ffdf9e/#dfe6ee, bodies
   #4595ec/#c78202/#476aa4, deep rims #1d5fa0/#a16207/#31517f, neutrals
   #e8eef6/#85c2ff) — the validator run above already covers them, and the
   part-0 contrast work stayed untouched by treaty.
2. Arrowheads stay NEUTRAL near-white (#e8eef6) for every family: the stream
   names the family, the head names the direction — direction is therefore
   never color. Two discrete sizes (small 8×7, large 12×10) replace continuous
   width; a stub wears the large head when its retired ribbon would have
   reached LARGE_HEAD_AT_PX (share ≥ 0.25 on the old 2→10 px map), keeping the
   constant load-bearing instead of deleting it.
3. Stacking contract: the canvas is absolutely positioned inside
   `.flow-bus-wrap`; the SVG gets `position: relative` so later DOM order wins
   and structure paints above glow. Canvas first in DOM, both aria-hidden —
   every fact remains duplicated as text.
4. Honest liveness: connection ≠ 'live' suppresses even the reduced-motion
   static frame (the canvas clears) — a disconnected column shows dotted
   presence lines, never a glowing stream it cannot vouch for.
5. ~~Accepted imperfection: terminal blooms may clip faintly past the viewBox
   edges (top/bottom).~~ SUPERSEDED in round 3a: the bus box grew vertical
   headroom (`0 -8 240 108`) and every bloom blit is now edge-capped to fit
   (`terminalBlitDiameter`) — see the round-3a appendix below. The clip is
   gone by construction, not by growth.
6. Proof harness: `web/scripts/flow-motion-frames.mjs` loads the seeded states
   with `reducedMotion: "no-preference"` and captures three full-page frames
   400 ms apart per state, failing on any pixel-identical pair — a frozen
   engine can pass a still-shot harness but not this one. First run: 9/9
   distinct frames across fleet-charging / mixed-directions / night-pacing.

## Round 3a — the DRAMA pass ("alive but shy" → rivers)

The round-2 verdict was that the streams were ALIVE BUT SHY: "the difference
between premium and wow is drama." Round 3a cranks the engine's dials inside
the standing ceilings — no new hues, no strobing, reduced-motion frame still
legible — and closes the six round-2 reviewer minors. All engine dials live in
`glow.ts` and are pinned as pure functions in `glow.test.ts`.

**The dial sheet (old → new), and why each holds honesty:**

| Dial | Round 2 | Round 3a | Note |
|---|---|---|---|
| spawn rate | 2 + 10·share /s | **3 + 14·share /s** | full share is a RIVER (~17/s) |
| speed | 30 + 80·share u/s | unchanged | speed already read well |
| bead radius | 0.8 + 1.2·share | **1.3 + 1.9·share** | beads read at a glance |
| bead alpha | 0.35 + 0.45·share | **0.55 + 0.45·share** | exactly 1 at full share |
| streak alpha | 0.18 + 0.30·share | **×1.3 → 0.234..0.624** | trails clearly visible |
| streak length | 1 frame of travel | **×1.5** (half a segment extrapolated back) | longer light-paint |
| terminal radius | 6 → 14 | **6 → 16** | with the edge cap below |
| terminal alpha | 0.22 + 0.30·share | **×1.25 → 0.275..0.65** | prescribed envelope |
| rail energization | 0.03→0.12 | **0.14 → 0.60** ∝ min(Σin, Σout) | the through-flow reads |
| conduit strokes | deep rims, w 2.4 | **body tints @ 0.55 alpha, w 2.8** | light needs a visible road |

- **The hot filament (dial 5) is gradient stops, not hues.** Above share ≈ 0.5
  each tone crossfades (weights sum to 1) to a second sprite built from the
  SAME two validated tints whose near-white core holds to 88% of the radius —
  a white-hot wire inside the colored glow. `hotBlend` is the pure law.
- **The conduit road (dial 6)** steps `.flow-conduit` up from the deep rim
  tones to the validated BODY tints (#4595ec/#c78202/#476aa4) at 0.55 alpha,
  width 2.8. Print regains the solid deep rims (translucent 55% blue washes
  out on white paper — the print block re-asserts them). No second rim path:
  the engine's additive glow already wraps the road in the same family tint.
- **Bloom edge-cap (was round-2 decision 5).** The bus box grew to
  `0 -8 240 108` (`BUS_VIEWBOX` — 24 units above the junctions, 30 below the
  landings), and `terminalBlitDiameter(radius, anchorY, unitsH)` caps every
  blit at `2·min(anchorY−top, top+unitsH−anchorY)` measured against the REAL
  box edges, so light grows to fill the headroom and a clipped additive glow
  (a hard chopped line) is impossible by construction. Junction blooms cap at
  48, landings at 44 — both unit-pinned.
- **The landing fix (reviewer minor e) — exact derivation, not a nudge.** The
  node grid's `column-gap: 2%` resolves against the dock's width — the width
  the 240-unit viewBox maps to — so the gap is a CONSTANT 4.8 units
  (`NODES_GAP_UNITS`; the CSS comment and the constant pin each other). With
  c = (240−2g)/3, card centers are c/2, 1.5c+g, 2.5c+2g → the outer cards sit
  exactly g/3 = 1.6 units outside their third-points. `cardCenterX(slotX)`
  applies that correction to conduit LANDINGS only (rail collars stay pinned
  to the thirds where the hardware lives), so arrowheads and arriving
  particles meet true card centers by construction. Chosen over nudging
  SLOT_X because the thirds are load-bearing for junction hardware and the
  derivation stays exact if the gap percentage ever changes.
- **Prior-runtime matching (minor b).** Runtimes match by stable key
  `stubKey` = tone+aim (tone IS the slot; one stub per aim per slot), never
  array index — a split→single topology change keeps the surviving stream's
  pool and τ-glide instead of snapping to fresh.
- **Reduced-motion discipline (minor a).** The static frame paints only for a
  live world that is actually watching (`connection === "live" && running &&
  !document.hidden` — the live visibility probe, not a stale flag); a hidden
  tab gets no repaints of any kind and waits for visibilitychange.
- **Allocation claim made TRUE (minor c).** The ticker iterates the shared
  renderer Set directly (ECMA-262 deletion-safe iteration; self-eviction
  only), particle placement writes through `pointAtInto(path, t, scratch)`
  into one reused point per renderer, and terminal blooms draw through one
  shared `blitBloom` helper invoked explicitly per path end — no `[...spread]`
  copies, no per-frame array literals or result objects anywhere in the frame
  path.
- **Unit seams (minor f).** `glow.test.ts` (14 tests, jsdom-safe — no canvas)
  pins every law adversarially: monotonicity across the WHOLE hostile domain
  (negative, >1, NaN), the exact dial endpoints, the α≤1 ceiling (a
  globalAlpha above 1 throws), the edge-cap exact-fit property swept across
  the box, NaN/impossible geometry reading as "draw nothing", the hot-blend
  window, and the key's blindness to share and path endpoints.

**Performance guard, computed:** worst lawful population is spawn 17/s ÷
speed 110 u/s over a ~63-unit conduit ≈ 10 alive per stream; even a fleet
split's five streams hold ~50 per canvas (×4 canvases ≈ 200 live beads, one
streak + ≤2 sprite blits each). `POOL_CAP` stays 72 PER STUB — it never binds
in lawful operation (a dt-clamped stall requests <1 spawn/frame and the
saturation branch drops backlog instead of bursting), so the ≤320 global
raise was not needed. dt clamp stays 50 ms; DPR cap 2.

**Ceilings honored / taste call:** no hue changes (sprite gradient stops
only), no flicker (all envelopes are smooth laws; breathing ±7% on 2400 ms),
terminal envelope ≤ the prescribed 0.65, and the reduced-motion static frame
keeps its legibility (chain stamps ≤ 0.32 alpha, pools at final intensity,
five deterministic capsules). Frame inspection (fleet-charging,
mixed-directions) found no neon-vomit regime to dial back: additive light
stays inside the bay, structure (rail, collars, heads) still reads over it,
and stream brightness still tracks watts — the 60–255 W home streams stay
quiet beside the 2,500 W rivers, which IS the honesty.

**Round-3a gates (2026-08-26):** `tsc -b` clean · `vitest run src/views/flow`
100/100 (82 round-2 + 14 glow + 4 cardCenterX) · overflow probe 30/30
captures clean · motion frames 9/9 distinct, none blank.

## The node instruments (round 3b)

Round 3a made the CONDUITS alive; round 3b makes the NODES physical and
reclaims the page. Every node tile now carries one physical instrument —
each an aria-hidden duplicate of text that already exists, each built from
previously validated family hexes, none ever the sole carrier of a fact.

**A — THE MOLTEN CELL (battery).** The thin `.flow-soc-meter` sliver is
DELETED; in its reserved slot stands a vertical glass vessel (~38×72 desktop,
34×64 at ≥81 rem, 30×54 phone) whose fill level IS the SoC figure: the level
rides in as `--cell-level` set inline from the REAL reading, with exactly ONE
transition (`height`, 450 ms) so the surface glides to a new datum and stops
— it animates nowhere else. The validated gold ramp stood upright (core
#ffdf9e → name #ffc246 → body #c78202 → deep rim #a16207, top→bottom) so
light pools at the surface; a meniscus line (#ffdf9e, glowing), faint tenth
ticks, a diagonal specular strip, an end-glow ∝ the reading
(`--cell-glow = 0.18 + 0.5·SoC/100`), and a top-lip light catch finish the
glass. THE HONESTY LAWS: while CHARGING only — and only while the phase is in
contact — a glint sweeps ALONG the meniscus line (horizontal translate,
never vertical: motion may shimmer IN PLACE, never fake a level change); a
DISCHARGE SHIMMER IS FORBIDDEN. Unknown SoC renders an EMPTY vessel plus the
worded "not available" — never a zero fill (the unknown variant has no fill
layer at all). A disconnected phase keeps its LAST-KNOWN fill, dimmed by the
down-zone chrome drain, and loses the glint. "40% charged" stays as real
text above the vessel; the worded figure is the primary carrier.

**B — THE FEED PORT (grid).** A machined socket (36 px, 30 px phone): dark
radial hardware, a recessed inner ring with faint radial tooling, ringed in
the grid body blue #4595ec. When power moves, a small tick (grid core
#bfdcff) rotates to the heading — up = import, down = export — on one 300 ms
ease. Split (the fleet's legal both-ways) and idle STAND THE TICK DOWN
entirely; absent telemetry dims the whole socket to opacity 0.4 (presence
without a claim). The port never carries direction alone: words + arrowheads
+ stream travel own it.

**C — THE HEARTH (home).** A warm radial ambience behind the figure — one
hue whisper (battery core #ffdf9e / body #c78202 stops at 0.17/0.09 alpha),
opacity `calc(--hearth-i × 0.9)` where `--hearth-i` is set inline from load
watts against the view's shared scale (`hearthLevel`: exactly 0 when idle or
unknown — a dark hearth is the honest hearth). It breathes on a 4600 ms
scale cycle; reduced motion stills it at its computed intensity. Cozy, not
christmassy.

**Reserved-height discipline.** EVERY card carries exactly one instrument
slot (`.flow-node-extra`, min-height pinned), empty-and-reserved where there
is nothing honest to show (the fleet battery has no summed SoC; homes'
slots stay reserved) — rows land at the same y across all columns, and the
reserved space now hosts the port/cell instead of dead air.

**D — LAYOUT RECLAIM.** The bay reclaims its page. Geometry moved in
flowGeometry.ts (tests moved with it): the bus box grew `0 -8 240 108` →
`0 -8 240 128` and the landings dropped STUB_BOTTOM 78 → 96 — every conduit
~29% longer (drop 62 → 80 units), landing blooms keeping their ≥24-unit
bottom headroom (cap diameter 48), x geometry untouched. The stage gained
desktop min-heights (30 rem from 45 rem, 33 rem from 70 rem — the bay fills
the viewport band at 1440 even when only one phase reports; the 390 phone
stays content-height), zones breathe (padding-block 1 rem), the fleet zone
is the HERO band (dock max-width 30 rem at the 45–81 rem bands; frame
strengthened — brighter border step, two-stop wash, faint family-blue
ambient shadow; figures already larger at 1.12 rem/800), `.flow-view` gap
widened one step (1.25 rem), and the footnote got air under the taller bay.
NO filler content anywhere — only spacing that lets the composition sit.

**E — RAIL SMOOTHING: CHOICE MADE.** Round 3a's energized rail chained the
rail sprite every 9 units and read as a dotted bead-chain. Replaced with ONE
CONTINUOUS filament: a single stretched-sprite halo (soft elliptical falloff,
hottest mid-span, carried a sprite-radius past each end so energy enters and
leaves without a hard step) under a single hairline core stroke in the
arrowhead white — two draw calls, zero allocations, painted additively like
everything else. The smooth linear-GRADIENT-stroke alternative was evaluated
and REJECTED: the τ-smoothed intensity changes every frame, so a cached
CanvasGradient would go stale and a per-frame rebuild would break the
engine's zero-allocation frame law. The static (reduced-motion) frame draws
the same filament, so stillness stays truthful to what motion looks like.

**F — HONESTY/A11Y AUDIT (all green).** Every new visual is aria-hidden;
every figure stays text; direction is tri-redundant (word, arrowhead, stream)
and never color-alone. The down-zone drain dims ONLY chrome and explicitly
covers all three instruments (`.flow-soc-cell`, `.flow-port`,
`.flow-node-hearth` → opacity .45, saturate .6) while names, figures, SoC
wording, and notes stay full-opacity word-perfect. Print: the cell prints as
a light trough with a solid dark-gold fill (the LEVEL survives as ink);
meniscus/ticks/gloss/glow/hearth/port-recess are screen material and drop;
the port carries no unique fact and hides. Forced colors: hearth/port step
aside; the cell reduces to Canvas trough with a CanvasText level.
Reduced motion: styles.css kills animation/transition globally (!important);
each instrument's base style IS its premium static frame (shimmer layer
opacity 0 when not running, fill snaps via transition:none, hearth holds its
intensity).

**THE PALETTE AUDIT (found + fixed this round's gate run).** The first-draft
cell poured an UNVALIDATED Tailwind amber ramp (#fbbf24/#f59e0b/#d97706/
#b45309, glow rgba(245,158,11,.5)) — caught by this round's mandatory
validator run and replaced with the round-1 battery quartet. Evidence:
categorical runs recorded for BOTH sets against #11161e (amber set: FAIL —
two stops above the dark L band, adjacent ΔE 8.0 below the normal-vision
floor; quartet: bright stops above the L band too — EXPECTED, see scope).
The decisive lens is the validator's own scope note — a vessel fill is ONE
sequential mark, not four categorical slots — so the binding checks are
per-stop WCAG vs every backdrop the fill can touch and lightness
monotonicity, both computed: worst stop #a16207 = 3.44:1 on the trough's
lightest point #211c12 (body 5.34, name 10.53, core 13.15; ≥3.69:1 vs stage
#11161e), strictly monotonic top→bottom (14.27/11.43/5.80/3.73 vs
trough-top). The trough's near-black shades are container chrome with the
same standing as the round-1 tile gradients. Zero new MARK tints remain.

**G — TESTS (enumerated).** FlowView.test.tsx grew 22 → 31 pins: every card
carries exactly one instrument slot; both grid cards dock a feed port whose
data-flow matches the flow (import/export) and whose tick is aria-hidden;
the molten cell meters the real reading (--cell-level verbatim, charging
class on −W charge); the unknown-SoC vessel is EMPTY (no fill layer exists)
beside the worded "not available"; ports rotate import→up/export→down and
STAND DOWN on the fleet's split; hearths breathe >0 ≤1 for measured load and
stay DARK at measured zero (rhs idles dark, mid + fleet lit); the meniscus
shimmer class appears ONLY while charging — never discharging, never down;
a downed cell keeps its last-known fill (45%) without the glint and its port
dims to unknown; the reserved-empty census (fleet battery + homes).
flowGeometry.test.ts re-pinned the grown box (BUS_VIEWBOX `0 -8 240 128`,
STUB_BOTTOM 96, headroom sweep over the new edges); glow.test.ts pins the
rail laws and the round-3b box height. flow.ts/flow.test.ts untouched (the
competing session owns them).

**Round-3b gates (2026-08-26):** `tsc -b` clean · `vitest run src/views/flow`
105/105 (31 FlowView + 38 flow + 22 geometry + 14 glow) · overflow probe
30/30 captures clean (all 10 states × 3 viewports) · motion frames 9/9
distinct, none blank · palette validator runs recorded (see audit above).
