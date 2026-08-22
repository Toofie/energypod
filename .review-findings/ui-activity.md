# UI authoring findings: activity (C:\Users\vagrant\Downloads\EnergyPod\pod-manager\web\src\views\activity\ActivityView.test.tsx)

## Author notes
Gates: vitest run -> ActivityView suite 11/11 failed, all assertion errors (findBy/getBy against the placeholder stub); src/test/harness.test.tsx green; src/api/client.test.ts green (22). HomeView/BatteriesView/NowView are in their own red phase against their own stubs (per-test assertion failures, files collect fine) - untouched by me. tsc --noEmit exit 0. COORDINATION: I initially created web/src/api/client.ts as a stub; the client-suite owner has since replaced it with the real implementation (createApiClient + ApiClientError + isUnauthorizedError; AuditEvent has an index signature so my audit fixtures type-check). My suite only vi.mocks that module - no remaining edits from me in it. CONTRACT DECISIONS for the implementer: ActivityView receives the client (and optional connection) as props; page size pinned at 50; audit cursor semantics = response next_cursor (number|null) passed back as getAudit's afterSequence, null ends pagination; filters are client-side toggle chips (kind groups map audit type: observation->Observations, decision->Decisions, arm/disarm->Arming, stop->Stops, *_acknowledgement->Acknowledgements); reason-code plain mapping pinned (SITE_EXPORT_LIMIT -> "Site export limit"). All pinned display strings are listed in the test file header comment. Suite passes client via vi.mocked(createApiClient).mockReturnValue(client) with exact API names; keyboard-only flow uses chip.focus() + user.keyboard("{Enter}") asserting aria-pressed; refused path is a Load-more 403 whose envelope code+message+request_id must render verbatim.

## Review summary
Reviewed web/src/views/activity/ActivityView.test.tsx against docs/UI_CONTRACTS.md ("Activity" + "State and error contract" + "Test conventions"). Gates re-run: vitest run -> ActivityView 11/11 failed, all assertion errors (findBy heading/listitem/text against the placeholder stub, no collect/infra errors); src/test/harness.test.tsx green; src/api/client.test.ts green (22). tsc --noEmit exit 0. Red phase is clean and the author's report is accurate. Strengths confirmed: requested/allowed/actual are pinned as three distinct visible facts (no allowed-as-requested misrepresentation), the refused Load-more path requires envelope code+message+request_id verbatim while keeping loaded entries, cursor semantics are pinned behaviorally via getAudit(PAGE_SIZE, cursor) argument assertions, and filtered-empty is distinct from global empty. No P0 found. The 8 findings below are coverage holes and assertion weaknesses: the five-kind filter contract is almost untested (only Stops+MID pressed; zero acknowledgement/disarm fixtures), the hidden-raw-code assertion blocks a native <details> disclosure, aria-pressed is never asserted false (a hardcoded always-true attribute passes), the loading test's claimed skeleton-vs-spinner property is not actually asserted, stale age and disconnected age are not pinned to the entries the contract attaches them to, the client-module mock diverges from the real client's ApiClientError pin (missing exports would crash a conforming implementation), and the request_id verbatimness standard is inconsistent between the two error tests.


## [1] P1 - Kind/unit filter contract barely exercised: only Stops and MID chips; no acknowledgement or disarm fixture exists
Category: coverage-hole

UI_CONTRACTS.md pins "Filterable by unit and by kind (observations, decisions, arming, stops, acknowledgements)" and the suite header (lines 16-17) lists all five kind chips plus "All units". Grep confirms "Observations", "Decisions", "Arming", "Acknowledgements", and "All units" appear only in the header comment, never in a query. The single filter test presses only the "Stops" and "MID" chips. No audit fixture of type *_acknowledgement or disarm exists anywhere in the file, so the acknowledgements and disarm mappings can never be tested by this suite, and an implementation that renders only Stops/MID/RHS/LHS chips (dropping four contracted kind chips and the All-units reset) passes 11/11. The arm->Arming mapping is likewise never asserted even though armLHS fixtures are present.

Suggested fix: Assert all five kind chips and the four unit chips exist as buttons; add at least one stop_acknowledgement (or inhibit_acknowledgement) and one disarm fixture and verify each maps to its chip via keyboard toggle; exercise "All units" as a reset.

## [2] P1 - Pre-click queryByText(...).toBeNull() blocks a native <details> disclosure, pinning an implementation detail
Category: impl-detail-constraint

testing-library text queries match elements regardless of visibility (they only ignore script/style), so the raw code inside a closed <details><summary>Show technical detail</summary>...</details> is still found by queryByText("SITE_EXPORT_LIMIT"), failing this assertion. The idiomatic, no-JS-state disclosure pattern therefore cannot pass the suite; the implementer is forced to conditionally unmount the code. The contract only requires "raw codes on demand" — hidden-by-closed-details satisfies it — and the suite's own conventions say "behavior over implementation ... never on DOM internals". DOM-presence-vs-visibility is a DOM internal.

Suggested fix: Assert non-visibility rather than absence: capture the element after disclosure (or query with a fallback), e.g. expect the code element, when present before the click, to not be visible (`const code = entry.queryByText("SITE_EXPORT_LIMIT"); if (code) expect(code).not.toBeVisible();`), keeping the post-click toBeVisible assertion.

## [3] P2 - aria-pressed asserted only as "true" after activation; never "false" initially or after de-toggle
Category: weak-accessibility

Lines 248 and 258 check aria-pressed="true" only after Enter. The second MID toggle (lines 264-267) asserts list length but not aria-pressed="false", and no test asserts the initial unpressed state. A static aria-pressed="true" hardcoded on every chip (an always-pressed lie to assistive tech) passes both assertions, so the aria-pressed checks add nothing beyond the list-length behavior assertions that already exist — an accessibility assertion that checks nothing real.

Suggested fix: Assert aria-pressed="false" on stopsChip before the first activation, "true" after, and "false" on midChip after the second toggle.

## [4] P2 - Loading test's title claims "skeleton, not spinner-only" but no assertion distinguishes a skeleton from any labeled status region
Category: assertion-overstates

The contract pins "Loading: skeleton, never spinner-only" and the test name repeats it, but the body only asserts a role="status" named "Loading activity" plus zero list items — a bare <div role="status" aria-label="Loading activity"> spinner satisfies every assertion. The named property of the test is unverified, so an implementation regressing to spinner-only is not caught.

Suggested fix: Assert something skeleton-specific and role/structure-based, e.g. a bounded set of placeholder list items inside the status region (getAllByRole within it), or at minimum rename the test to match what is asserted.

## [5] P2 - Stale age assertion is page-global; never pinned to sit next to the stale entry
Category: coverage-hole

Contract: "Stale: age shown next to values past freshness". The test does screen.findByText("2 hours ago") anywhere on the page, then checks items[0].textContent only for "MID" — not for the age. A page-level "last updated 2 hours ago" banner with per-entry ages omitted passes, and the follow-up check never ties the age to the listitem.

Suggested fix: Use within(items[0]).getByText("2 hours ago") (and toBeVisible) so the age is asserted inside the stale entry, matching the comment already on the test.

## [6] P2 - Disconnected state omits the contracted "last data dimmed with age" fact
Category: coverage-hole

The state contract reads "Disconnected: banner + per-view notice, last data dimmed with age, retry ... manual for REST". The disconnected test asserts the notice, the retained entries, and the manual retry (getAudit twice), but never that an age is shown alongside the retained data while disconnected — the only age assertion in the suite is in the connected stale test. An implementation that drops age display when connection="disconnected" passes.

Suggested fix: Since the retained entries are 2 and 30 minutes old in this test, assert a relative-age string within one of the retained listitems while disconnected (fixture ages may need adjusting past the freshness bound).

## [7] P2 - Mock factory omits ApiClientError/isUnauthorizedError and rejections are plain objects, diverging from the real client's TypedError pin
Category: mock-fidelity

The real client (web/src/api/client.ts, the shared contract) rejects every failure as an ApiClientError carrying status, and documents isUnauthorizedError as the consumer's test. This suite rejects bare {code,message,details,request_id} objects (lines 298-304, 395-400), so an implementation branching on error.status sees undefined in tests while working in production; worse, the vi.mock factory returns only { createApiClient }, so a green-phase ActivityView importing ApiClientError or isUnauthorizedError throws Vitest's "no export defined on the mock" error for the whole file — a conforming implementation is blocked from using the client's documented API.

Suggested fix: Build the mock via importOriginal: vi.mock("../../api/client", async (orig) => ({ ...(await orig()), createApiClient: vi.fn() })), and reject with new ApiClientError({ status: 403, ...envelope }) so fixtures match what the view actually catches.

## [8] P2 - request_id rendering demanded only in the refused path; the general error fixture's request_id is never asserted
Category: inconsistent-envelope-pin

The general error test fixture carries request_id "req-9z8y" (line 303) but asserts only code and message; the refused test requires "req-1a2b3c" visible (line 414). The two error paths thus apply different verbatimness standards for the same envelope shape, and neither matches a documented rule (UI_CONTRACTS says "code and message" for errors, "code + message" for refusals). An implementation showing request_id only on refusals passes; one showing it on all API errors also passes — the suite does not decide, and the header comment (line 22) does not say request_id is required.

Suggested fix: Pick one standard and encode it: either assert req-9z8y in the general error test too (and state in the header that request_id renders in every error envelope) or drop the line-414 request_id assertion; document the choice in the header comment.
