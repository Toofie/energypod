#!/usr/bin/env node
// Usage: node scripts/flow-motion-frames.mjs
//
// The FLOW VIEW'S MOTION HARNESS (dev tooling only — nothing here ships).
// Round 2 of the design loop replaced the retired thickness-ribbon grammar
// with LIVING PARTICLE STREAMS on a Canvas2D light engine (glow.ts) — light
// that only exists in motion, so a single still frame cannot prove it works.
// This script captures THREE frames per state, 400 ms apart, with reduced
// motion explicitly NOT preferred (the opposite of screenshot.mjs, which
// pins "reduce"), and fails unless every frame is distinct:
//
//   web/.design-shots/_loop/motion/<state>.<n>.png   (n = 1..3)
//
// Two identical frames mean the engine is frozen (ticker dead, spec never
// pushed, full-stop engaged when it should not be) — exactly the failure a
// still-shot harness would miss.
//
// Requires the Playwright Chromium browser once per machine:
//   pnpm exec playwright install chromium
import { createHash } from "node:crypto";
import { createServer as createHttpServer } from "node:http";
import { mkdir, readFile, rm, stat } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));
const PAGE_URL_PATH = "/scripts/shots/shots.html";
const OUT_DIR = join(WEB_ROOT, ".design-shots", "_loop", "motion");
const MIN_PNG_BYTES = 8 * 1024;
const READY_TIMEOUT_MS = 30_000;
/** Frames per state, and the gap between captures — enough time for the
 *  slowest lawful stream (30 units/s over a ~62-unit conduit, share-scaled)
 *  to visibly rearrange the picture several times over. */
const FRAMES_PER_STATE = 3;
const FRAME_GAP_MS = 400;
/** Settle after readiness before frame 1: let the engine's first spawns,
 *  fades, and terminal blooms leave their startup transient behind. */
const SETTLE_MS = 600;

const VIEW_ID = "flow";
const STATE_IDS = ["fleet-charging", "mixed-directions", "night-pacing"];
const VIEWPORT_ID = "desktop-1440";

/**
 * Start the harness's own Vite dev server (middleware mode) on a free port —
 * the same private-server pattern as scripts/screenshot.mjs, never touching
 * dist/ or the operator's :5173 instance.
 */
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
    throw new Error("The harness dev server did not report a port.");
  }
  return { vite, http, baseUrl: `http://127.0.0.1:${address.port}` };
}

function humanBytes(size) {
  return size >= 1024 * 1024
    ? `${(size / (1024 * 1024)).toFixed(2)} MiB`
    : `${(size / 1024).toFixed(1)} KiB`;
}

function sha256(path) {
  return readFile(path).then((bytes) => createHash("sha256").update(bytes).digest("hex"));
}

/**
 * Capture one state's motion sequence: load, wait for the settled render
 * (readiness contract + fonts), settle past the engine's startup transient,
 * then shoot FRAMES_PER_STATE full-page frames FRAME_GAP_MS apart while the
 * streams keep moving underneath the camera.
 */
async function captureMotion(page, baseUrl, stateId) {
  const errors = [];
  const onError = (error) => errors.push(String(error));
  page.on("pageerror", onError);
  const paths = [];
  try {
    await page.goto(`${baseUrl}${PAGE_URL_PATH}?view=${VIEW_ID}&state=${stateId}`, {
      waitUntil: "load",
    });
    await page.waitForSelector("html[data-shots-ready='true']", { state: "attached", timeout: READY_TIMEOUT_MS });
    const documentElement = page.locator("html");
    const failed = await documentElement.getAttribute("data-shots-failed");
    if (failed !== null) {
      throw new Error(`the harness page refused: ${failed}`);
    }
    const mountedView = await documentElement.getAttribute("data-shots-view");
    const mountedState = await documentElement.getAttribute("data-shots-state");
    if (mountedView !== VIEW_ID || mountedState !== stateId) {
      throw new Error(`mounted ${mountedView}/${mountedState}, wanted ${VIEW_ID}/${stateId}`);
    }
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(SETTLE_MS);
    for (let frame = 1; frame <= FRAMES_PER_STATE; frame += 1) {
      const path = join(OUT_DIR, `${stateId}.${frame}.png`);
      await page.screenshot({ path, fullPage: true });
      paths.push(path);
      if (frame < FRAMES_PER_STATE) {
        await page.waitForTimeout(FRAME_GAP_MS);
      }
    }
  } finally {
    page.off("pageerror", onError);
  }
  if (errors.length > 0) {
    throw new Error(`the page threw while rendering: ${errors.join(" | ")}`);
  }
  return paths;
}

async function main() {
  // A clean slate per run: last round's motion evidence must never masquerade
  // as this round's.
  await rm(OUT_DIR, { recursive: true, force: true });
  await mkdir(OUT_DIR, { recursive: true });

  const { vite, http, baseUrl } = await startDevServer();
  let browser = null;
  const results = [];
  const failures = [];
  const startedAt = Date.now();
  try {
    const { chromium } = await import("playwright");
    browser = await chromium.launch();

    // Viewport + states come from the registry via the page itself, same as
    // screenshot.mjs — this script owns no copies of that truth.
    const manifestPage = await browser.newPage();
    await manifestPage.goto(`${baseUrl}${PAGE_URL_PATH}?manifest=1`, { waitUntil: "load" });
    await manifestPage.waitForSelector("html[data-shots-ready='true']", { state: "attached", timeout: READY_TIMEOUT_MS });
    const manifest = await manifestPage.evaluate(() => window.__shotsManifest());
    await manifestPage.close();

    const viewport = manifest.viewports.find((candidate) => candidate.id === VIEWPORT_ID);
    if (viewport === undefined) {
      throw new Error(`No such viewport: ${VIEWPORT_ID}. Known: ${manifest.viewports.map((v) => v.id).join(", ")}`);
    }
    const flowView = manifest.views.find((candidate) => candidate.id === VIEW_ID);
    if (flowView === undefined) {
      throw new Error(`No such view: ${VIEW_ID}. Known: ${manifest.views.map((v) => v.id).join(", ")}`);
    }
    const unknownStates = STATE_IDS.filter((id) => !flowView.states.some((state) => state.id === id));
    if (unknownStates.length > 0) {
      throw new Error(
        `No such state(s) on ${VIEW_ID}: ${unknownStates.join(", ")}. Known: ${flowView.states.map((s) => s.id).join(", ")}`,
      );
    }

    // THE ONE DELIBERATE DIFFERENCE FROM screenshot.mjs: reduced motion is
    // explicitly NOT preferred, so the engine runs at full life — this
    // harness exists to watch it move.
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: viewport.deviceScaleFactor,
      reducedMotion: "no-preference",
    });
    const page = await context.newPage();
    for (const stateId of STATE_IDS) {
      const label = `${VIEW_ID}/${stateId}`;
      try {
        const paths = await captureMotion(page, baseUrl, stateId);
        for (const path of paths) {
          const { size } = await stat(path);
          if (size < MIN_PNG_BYTES) {
            throw new Error(`${path} is blank-sized (${humanBytes(size)})`);
          }
        }
        results.push(...paths);
        process.stdout.write(`  ok    ${label} — ${FRAMES_PER_STATE} frames\n`);
      } catch (error) {
        failures.push(`${label}: ${error instanceof Error ? error.message : String(error)}`);
        process.stdout.write(`  FAIL  ${label} — ${failures[failures.length - 1]}\n`);
      }
    }
    await context.close();

    // DISTINCTNESS, twice over: within a state (a frozen engine would shoot
    // triplets) and across the whole run (9 genuinely different pictures).
    const seen = new Map();
    for (const path of results) {
      const digest = await sha256(path);
      const twin = seen.get(digest);
      if (twin !== undefined) {
        failures.push(`${path} is pixel-identical to ${twin}`);
      } else {
        seen.set(digest, path);
      }
    }

    const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
    if (failures.length > 0) {
      process.stdout.write(`\n${failures.length} failure(s) after ${seconds}s:\n`);
      for (const failure of failures) {
        process.stdout.write(`  - ${failure}\n`);
      }
      process.exitCode = 1;
    } else {
      process.stdout.write(
        `\n${results.length} motion frame(s) into ${OUT_DIR} in ${seconds}s — all distinct, none blank.\n`,
      );
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
