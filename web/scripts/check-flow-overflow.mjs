#!/usr/bin/env node
// Usage: node scripts/check-flow-overflow.mjs [--state <id>]  — the flow view's
// OVERFLOW PROBE (dev tooling only — nothing here ships), the design loop's
// standing pixel-perfect gate (loop ledger rule 7).
//
/**
 * The Flow view's DOM OVERFLOW PROBE: the automated half of the round gate
 * ("NOTHING truncated, no misalignment, no overflow"). It boots the same
 * private Vite dev server + headless Chromium pair as scripts/screenshot.mjs,
 * mounts the REAL FlowView for EVERY flow state × EVERY harness viewport
 * (10 states × desktop-1440 / tablet-768 / narrow-390 — both lists read from
 * the page's own manifest, never hardcoded), and audits the settled DOM:
 *
 *   (a) internal scroll overflow — scrollWidth/scrollHeight exceeding
 *       clientWidth/clientHeight on figure/name/story/command/connection boxes;
 *   (b) escaping children — any descendant whose bounding rect leaves its
 *       .flow-node card by more than 1px (the pre-clip truth behind
 *       `overflow:hidden`, which would otherwise hide a truncation);
 *   (c) page-level horizontal overflow — documentElement scrollWidth beyond
 *       its clientWidth;
 *   (d) clipped text — for every figure word, figure number, SoC percent,
 *       SoC word, scope line and node name, a Range over the text must sit
 *       inside both its own span and the card's rect.
 *
 * BARE-MOUNT ASSUMPTION: this probe mounts each view STANDALONE in the
 * harness page (shots.html), NOT inside the app shell. Anything an ancestor
 * would contribute — `main.dimmed` opacity when disconnected, shell-level
 * scroll/border context, sibling chrome reflow — is invisible here by
 * design; the probe certifies the view's own layout at each viewport, and
 * the shell contract stays the app tests' job.
 *
 * Exit 0 with "no overflow in N captures" when clean; exit 1 with a printed
 * failure report (state / viewport / check / selector / text / rects) when any
 * capture leaks. Requires the same one-time Playwright Chromium install as the
 * screenshot harness.
 */
import { createServer as createHttpServer } from "node:http";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));
const PAGE_URL_PATH = "/scripts/shots/shots.html";
const FLOW_VIEW_ID = "flow";
const READY_TIMEOUT_MS = 30_000;

function parseArgs(argv) {
  const options = { state: undefined };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--state") options.state = argv[++index];
    else if (argv[index]?.startsWith("--state=")) options.state = argv[index].slice("--state=".length);
    else throw new Error(`Unknown argument: ${argv[index]}`);
  }
  return options;
}

/** Start the probe's own Vite dev server (middleware mode) on a free port. */
async function startDevServer() {
  const { createServer } = await import("vite");
  const vite = await createServer({
    root: WEB_ROOT,
    logLevel: "error",
    appType: "mpa",
    server: { middlewareMode: true, watch: null },
  });
  const http = createHttpServer(vite.middlewares);
  await new Promise((resolve, reject) => {
    http.once("error", reject);
    http.listen(0, "127.0.0.1", resolve);
  });
  const address = http.address();
  if (address === null || typeof address === "string") {
    throw new Error("The probe dev server did not report a port.");
  }
  return { vite, http, baseUrl: `http://127.0.0.1:${address.port}` };
}

/**
 * The in-page audit. Runs against the settled picture; returns an array of
 * issue records (empty = clean). Rects are reported at hundredth-px so the
 * report names the leak precisely enough to fix it.
 */
function auditFlowOverflow() {
  // Tolerances live INSIDE the function: page.evaluate serializes it alone,
  // so nothing it reads may sit in the Node module's scope.
  const EPSILON_PX = 0.5; // sub-pixel layout rounding slack
  const CARD_ESCAPE_PX = 1; // a child may kiss its card's edge, not cross it
  const issues = [];
  const px = (value) => Math.round(value * 100) / 100;
  const rectOf = (r) => ({ left: px(r.left), right: px(r.right), top: px(r.top), bottom: px(r.bottom) });

  // (a) internal scroll overflow on every text-bearing flow box.
  const BOX_SELECTORS = [
    ".flow-node",
    ".flow-node-figure",
    ".flow-fig-part",
    ".flow-node-name",
    ".flow-phase-name",
    ".flow-phase-note",
    ".flow-story",
    ".flow-commands li",
    ".flow-connection",
    ".flow-footnote",
  ];
  for (const selector of BOX_SELECTORS) {
    for (const el of document.querySelectorAll(selector)) {
      if (el.scrollWidth > el.clientWidth + EPSILON_PX || el.scrollHeight > el.clientHeight + EPSILON_PX) {
        issues.push({
          check: "scroll-size",
          selector,
          text: (el.textContent ?? "").trim().slice(0, 60),
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight,
        });
      }
    }
  }

  // (b) descendants escaping their .flow-node card (layout rects, pre-clip).
  for (const node of document.querySelectorAll(".flow-node")) {
    const card = node.getBoundingClientRect();
    for (const child of node.querySelectorAll("*")) {
      const r = child.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      if (
        r.left < card.left - CARD_ESCAPE_PX ||
        r.right > card.right + CARD_ESCAPE_PX ||
        r.top < card.top - CARD_ESCAPE_PX ||
        r.bottom > card.bottom + CARD_ESCAPE_PX
      ) {
        issues.push({
          check: "escapes-card",
          selector: String(child.className.baseVal ?? (child.className || child.tagName)),
          text: (child.textContent ?? "").trim().slice(0, 60),
          child: rectOf(r),
          card: rectOf(card),
        });
      }
    }
  }

  // (c) page-level horizontal overflow (the phone-killer).
  const root = document.documentElement;
  if (root.scrollWidth > root.clientWidth + EPSILON_PX) {
    issues.push({
      check: "page-x-overflow",
      selector: "html",
      scrollWidth: root.scrollWidth,
      clientWidth: root.clientWidth,
    });
  }

  // (d) clipped text: the Range's own client rects must sit inside the text's
  // span AND inside its card. Range rects are layout rects — they report where
  // the glyphs WANT to be even when an ancestor's overflow:hidden clips them,
  // which is exactly the truncation the eye would miss. Every datum string is
  // covered: figure words, the numbers themselves, both SoC lines, and the
  // fleet scope suffix ("— across the 2 of 3 phases reporting").
  for (const el of document.querySelectorAll(
    ".flow-fig-word, .flow-fig-num, .flow-soc-pct, .flow-soc-word, .flow-fig-scope, .flow-node-name",
  )) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const container = el.getBoundingClientRect();
    const cardRect = el.closest(".flow-node")?.getBoundingClientRect();
    for (const r of range.getClientRects()) {
      if (r.width === 0 && r.height === 0) continue;
      const out = [];
      if (
        r.left < container.left - CARD_ESCAPE_PX ||
        r.right > container.right + CARD_ESCAPE_PX ||
        r.top < container.top - CARD_ESCAPE_PX ||
        r.bottom > container.bottom + CARD_ESCAPE_PX
      ) {
        out.push("container");
      }
      if (
        cardRect !== undefined &&
        (r.left < cardRect.left - CARD_ESCAPE_PX || r.right > cardRect.right + CARD_ESCAPE_PX)
      ) {
        out.push("card");
      }
      if (out.length > 0) {
        issues.push({
          check: `clipped-text(${out.join("+")})`,
          selector: String(el.className),
          text: (el.textContent ?? "").trim(),
          textRect: rectOf(r),
          container: rectOf(container),
          ...(cardRect === undefined ? {} : { card: rectOf(cardRect) }),
        });
      }
    }
  }

  return issues;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const { vite, http, baseUrl } = await startDevServer();
  let browser = null;
  try {
    const { chromium } = await import("playwright");
    browser = await chromium.launch();

    // The manifest comes from the page itself — the registry stays the single
    // source of truth for states and viewports (same contract as screenshot.mjs).
    const manifestPage = await browser.newPage();
    await manifestPage.goto(`${baseUrl}${PAGE_URL_PATH}?manifest=1`, { waitUntil: "load" });
    await manifestPage.waitForSelector("html[data-shots-ready='true']", { state: "attached", timeout: READY_TIMEOUT_MS });
    const manifest = await manifestPage.evaluate(() => window.__shotsManifest());
    await manifestPage.close();

    const view = manifest.views.find((entry) => entry.id === FLOW_VIEW_ID);
    if (view === undefined) {
      throw new Error(`No such view in the harness manifest: ${FLOW_VIEW_ID}`);
    }
    const states = view.states.filter((state) => options.state === undefined || state.id === options.state);
    if (states.length === 0) {
      throw new Error(
        `No such state on ${FLOW_VIEW_ID}: ${options.state}. Known: ${view.states.map((s) => s.id).join(", ")}`,
      );
    }

    const failures = [];
    let captures = 0;
    const startedAt = Date.now();

    for (const viewport of manifest.viewports) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: viewport.deviceScaleFactor,
        reducedMotion: "reduce",
      });
      const page = await context.newPage();
      for (const state of states) {
        const label = `${FLOW_VIEW_ID}/${state.id}.${viewport.id}`;
        try {
          await page.goto(`${baseUrl}${PAGE_URL_PATH}?view=${FLOW_VIEW_ID}&state=${state.id}`, {
            waitUntil: "load",
          });
          await page.waitForSelector("html[data-shots-ready='true']", { state: "attached", timeout: READY_TIMEOUT_MS });
          const failed = await page.locator("html").getAttribute("data-shots-failed");
          if (failed !== null) {
            throw new Error(`the harness page refused: ${failed}`);
          }
          const mountedView = await page.locator("html").getAttribute("data-shots-view");
          const mountedState = await page.locator("html").getAttribute("data-shots-state");
          if (mountedView !== FLOW_VIEW_ID || mountedState !== state.id) {
            throw new Error(`mounted ${mountedView}/${mountedState}, wanted ${FLOW_VIEW_ID}/${state.id}`);
          }
          await page.evaluate(() => document.fonts.ready);
          const issues = await page.evaluate(auditFlowOverflow);
          captures += 1;
          for (const issue of issues) {
            failures.push({ label, ...issue });
          }
          process.stdout.write(`  ${issues.length === 0 ? "ok  " : "FAIL"}  ${label} — ${issues.length} issue(s)\n`);
        } catch (error) {
          failures.push({
            label,
            check: "capture",
            selector: "",
            text: error instanceof Error ? error.message : String(error),
          });
          process.stdout.write(`  FAIL  ${label} — ${failures[failures.length - 1].text}\n`);
        }
      }
      await context.close();
    }

    const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
    if (failures.length > 0) {
      process.stdout.write(`\n${failures.length} overflow issue(s) across ${captures} capture(s) in ${seconds}s:\n`);
      for (const failure of failures) {
        const { label, ...detail } = failure;
        process.stdout.write(
          `  - [${label}] ${detail.check} ${detail.selector}${detail.text ? ` "${detail.text}"` : ""}\n` +
            `      ${JSON.stringify(detail)}\n`,
        );
      }
      process.exitCode = 1;
    } else {
      process.stdout.write(`\nno overflow in ${captures} captures (${seconds}s)\n`);
    }
  } finally {
    if (browser !== null) {
      await browser.close();
    }
    await vite.close();
    await new Promise((resolve) => http.close(resolve));
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
