/**
 * The screenshot harness's smoke test (the harness itself is dev tooling that
 * lives outside the suite — web/scripts/screenshot.mjs — because jsdom cannot
 * rasterize). This pins the two facts the design-polish loop depends on: the
 * script exists where `npm run shots` expects it, and it answers --help
 * without needing a browser. The full run (`npm run shots`) is executed by
 * humans and the design loop, not by CI.
 */
import { execFile } from "node:child_process";
import { execPath } from "node:process";
import { resolve } from "node:path";
import { promisify } from "node:util";
import { describe, expect, it } from "vitest";

const run = promisify(execFile);
// The suite runs with the web package as its working directory (`npm test`).
const scriptPath = resolve(process.cwd(), "scripts", "screenshot.mjs");

describe("the screenshot harness", () => {
  it("exists and answers --help without booting a browser", async () => {
    const { stdout } = await run(execPath, [scriptPath, "--help"]);
    expect(stdout).toContain("Usage:");
    expect(stdout).toContain("npm run shots");
  }, 15_000);
});
