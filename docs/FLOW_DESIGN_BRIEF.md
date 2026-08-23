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
