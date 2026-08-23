/**
 * The screenshot harness's page entry (dev tooling; served only by the
 * harness's own Vite dev server at /scripts/shots/shots.html).
 *
 * It mounts the REAL view components — the same modules the console ships —
 * inside a minimal shell that reproduces the composed app's DOM shape
 * (`div.shell > main > h1 + view`) and its real global styles, with the
 * seeded stand-in client (client.ts) answering from the state's frozen wire
 * world. No router, no token gate, no network.
 *
 * READINESS CONTRACT (what Playwright waits for): this entry flips
 * `<html data-shots-ready="true">` only after the view has READ the seeded
 * snapshot, CONSUMED the first stream frame (its connection reads "live"),
 * the fonts have settled, and two animation frames have painted — so every
 * capture is the settled picture, never a loading flash.
 */
import { createRoot } from "react-dom/client";
import { seededClient } from "./client";
import { SHOT_VIEWS, SHOT_VIEWPORTS } from "./registry";
import "../../src/styles.css";

/** The manifest the orchestrator reads (view ids, state ids, viewports). */
export interface ShotsManifest {
  views: { id: string; label: string; states: { id: string; caption: string }[] }[];
  viewports: { id: string; width: number; height: number; deviceScaleFactor: number }[];
}

declare global {
  interface Window {
    __shotsManifest?: () => ShotsManifest;
  }
}

const params = new URLSearchParams(window.location.search);
const root = document.getElementById("root");

function markReady(): void {
  document.documentElement.dataset.shotsReady = "true";
}

function markFailed(message: string): void {
  document.documentElement.dataset.shotsReady = "true";
  document.documentElement.dataset.shotsFailed = message;
}

window.__shotsManifest = () => ({
  views: SHOT_VIEWS.map((view) => ({
    id: view.id,
    label: view.label,
    states: view.states.map((state) => ({ id: state.id, caption: state.caption })),
  })),
  viewports: SHOT_VIEWPORTS.map(({ id, width, height, deviceScaleFactor }) => ({
    id,
    width,
    height,
    deviceScaleFactor,
  })),
});

const viewId = params.get("view");
const stateId = params.get("state");

if (viewId === null || stateId === null) {
  // Manifest mode: nothing mounts; the page answers and stands ready.
  markReady();
} else if (root === null) {
  markFailed("no #root element");
} else {
  const view = SHOT_VIEWS.find((entry) => entry.id === viewId);
  const state = view?.states.find((entry) => entry.id === stateId);
  if (view === undefined || state === undefined) {
    markFailed(`unknown view/state: ${viewId}/${stateId}`);
  } else {
    document.documentElement.dataset.shotsView = view.id;
    document.documentElement.dataset.shotsState = state.id;
    const client = seededClient(
      state.world(),
      state.connection === undefined ? {} : { connection: state.connection },
    );
    createRoot(root).render(
      <div className="shell">
        <main>
          <h1>{view.label}</h1>
          <view.Component client={client} />
        </main>
      </div>,
    );
    void (async () => {
      try {
        await client.ready;
        await document.fonts.ready;
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        markReady();
      } catch (error) {
        markFailed(error instanceof Error ? error.message : String(error));
      }
    })();
  }
}
