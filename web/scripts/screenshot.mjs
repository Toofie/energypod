#!/usr/bin/env node
// Usage: npm run shots   — regenerate the console's full screenshot matrix into web/.design-shots/ (see --help for filters)
/**
 * The EnergyPod console's SCREENSHOT HARNESS (dev tooling only — nothing here
 * ships). It boots a private Vite dev server, mounts the REAL view components
 * in headless Chromium against seeded wire worlds (web/scripts/shots/), and
 * captures one PNG per defined state per viewport for human visual review:
 *
 *   web/.design-shots/<view>/<state>.<viewport>.png
 *
 * One-time setup (the browser lands in the OS user cache, never the repo):
 *   pnpm add -D playwright && pnpm exec playwright install chromium
 *
 * See `--help` for filters. The state matrix itself lives in
 * web/scripts/shots/registry.tsx + web/scripts/shots/states/<view>.ts.
 */
import { createHash } from "node:crypto";
import { createServer as createHttpServer } from "node:http";
import { mkdir, readFile, rm, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));
const PAGE_URL_PATH = "/scripts/shots/shots.html";
const DEFAULT_OUT_DIR = join(WEB_ROOT, ".design-shots");
const MIN_PNG_BYTES = 8 * 1024;
const READY_TIMEOUT_MS = 30_000;

const HELP = `EnergyPod console — screenshot harness (dev tooling)

Usage:
  npm run shots                        regenerate every view's full matrix
  npm run shots -- --view flow         one view's states
  npm run shots -- --state all-idle    one state, both viewports
  npm run shots -- --list              print the state matrix and exit
  npm run shots -- --out DIR           write elsewhere (default web/.design-shots)
  npm run shots -- --keep              don't wipe existing PNGs first

What it does: starts a private Vite dev server (never touching dist/ or the
build config), loads web/scripts/shots/shots.html in headless Chromium, and
mounts the REAL view components with seeded wire worlds built from the shared
fixtures (web/src/test/wire.ts) — one PNG per state x viewport, waited until
the view has consumed its snapshot, gone live, and painted twice.

Output: web/.design-shots/<view>/<state>.<viewport>.png (gitignored — never
committed). The run FAILS if any capture is blank-sized or two states of one
view produce identical pixels, so a silently-broken render cannot pass.

Requires the Playwright Chromium browser once per machine:
  pnpm exec playwright install chromium
`;

function parseArgs(argv) {
  const options = { view: undefined, state: undefined, out: undefined, keep: false, list: false, help: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--help" || arg === "-h") options.help = true;
    else if (arg === "--list") options.list = true;
    else if (arg === "--keep") options.keep = true;
    else if (arg === "--view") options.view = argv[++index];
    else if (arg === "--state") options.state = argv[++index];
    else if (arg === "--out") options.out = argv[++index];
    else if (arg.startsWith("--view=")) options.view = arg.slice("--view=".length);
    else if (arg.startsWith("--state=")) options.state = arg.slice("--state=".length);
    else if (arg.startsWith("--out=")) options.out = arg.slice("--out=".length);
    else {
      throw new Error(`Unknown argument: ${arg}\n\n${HELP}`);
    }
  }
  return options;
}

/** Start the harness's own Vite dev server (middleware mode) on a free port. */
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

/** One capture: load the state page, wait for its settled render, shoot it. */
async function captureState(page, baseUrl, view, state, viewport, path) {
  const errors = [];
  const onError = (error) => errors.push(String(error));
  page.on("pageerror", onError);
  try {
    await page.goto(`${baseUrl}${PAGE_URL_PATH}?view=${view.id}&state=${state.id}`, {
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
    if (mountedView !== view.id || mountedState !== state.id) {
      throw new Error(`mounted ${mountedView}/${mountedState}, wanted ${view.id}/${state.id}`);
    }
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path, fullPage: true });
  } finally {
    page.off("pageerror", onError);
  }
  if (errors.length > 0) {
    throw new Error(`the page threw while rendering: ${errors.join(" | ")}`);
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(HELP);
    return;
  }

  const { vite, http, baseUrl } = await startDevServer();
  let browser = null;
  try {
    const { chromium } = await import("playwright");
    browser = await chromium.launch();

    // The manifest comes from the page itself — the registry is the single
    // source of truth for views, states, and viewports.
    const manifestPage = await browser.newPage();
    await manifestPage.goto(`${baseUrl}${PAGE_URL_PATH}?manifest=1`, { waitUntil: "load" });
    await manifestPage.waitForSelector("html[data-shots-ready='true']", { state: "attached", timeout: READY_TIMEOUT_MS });
    const manifest = await manifestPage.evaluate(() => window.__shotsManifest());
    await manifestPage.close();

    const views = manifest.views.filter((view) => options.view === undefined || view.id === options.view);
    if (views.length === 0) {
      throw new Error(`No such view: ${options.view}. Known: ${manifest.views.map((v) => v.id).join(", ")}`);
    }
    if (options.list) {
      for (const view of views) {
        process.stdout.write(`${view.id} (${view.label})\n`);
        for (const state of view.states) {
          process.stdout.write(`  ${state.id} — ${state.caption}\n`);
        }
      }
      process.stdout.write(`viewports: ${manifest.viewports.map((v) => `${v.id} (${v.width}x${v.height} @${v.deviceScaleFactor}x)`).join(", ")}\n`);
      return;
    }

    const outRoot = options.out === undefined ? DEFAULT_OUT_DIR : resolve(process.cwd(), options.out);
    const failures = [];
    const results = [];
    const startedAt = Date.now();
    // A view's directory is wiped once per run (before its first capture), so
    // the second viewport's pass appends to — never erases — the first's.
    const wipedViews = new Set();

    for (const viewport of manifest.viewports) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: viewport.deviceScaleFactor,
        reducedMotion: "reduce",
      });
      const page = await context.newPage();
      for (const view of views) {
        const states = view.states.filter((state) => options.state === undefined || state.id === options.state);
        if (states.length === 0) {
          throw new Error(`No such state on ${view.id}: ${options.state}. Known: ${view.states.map((s) => s.id).join(", ")}`);
        }
        const viewDir = join(outRoot, view.id);
        if (!options.keep && !wipedViews.has(view.id)) {
          await rm(viewDir, { recursive: true, force: true });
          wipedViews.add(view.id);
        }
        await mkdir(viewDir, { recursive: true });
        for (const state of states) {
          const path = join(viewDir, `${state.id}.${viewport.id}.png`);
          const label = `${view.id}/${state.id}.${viewport.id}.png`;
          try {
            await captureState(page, baseUrl, view, state, viewport, path);
            const { size } = await stat(path);
            if (size < MIN_PNG_BYTES) {
              failures.push(`${label}: ${humanBytes(size)} is blank-sized (under ${MIN_PNG_BYTES} bytes)`);
            } else {
              results.push({ label, path, size });
              process.stdout.write(`  ok    ${label} — ${humanBytes(size)}\n`);
            }
          } catch (error) {
            failures.push(`${label}: ${error instanceof Error ? error.message : String(error)}`);
            process.stdout.write(`  FAIL  ${label} — ${failures[failures.length - 1]}\n`);
          }
        }
      }
      await context.close();
    }

    // No two states of one view+viewport may capture identical pixels — a
    // duplicated picture means a state rendered another state's world (or a
    // blank), and the design review would judge the wrong picture.
    const seen = new Map();
    for (const result of results) {
      const bytes = await readFile(result.path);
      const digest = createHash("sha256").update(bytes).digest("hex");
      const twin = seen.get(digest);
      if (twin !== undefined) {
        failures.push(`${result.label} is pixel-identical to ${twin}`);
      } else {
        seen.set(digest, result.label);
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
      process.stdout.write(`\n${results.length} PNG(s) into ${outRoot} in ${seconds}s — all distinct, none blank.\n`);
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
