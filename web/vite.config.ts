import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  // Local development proxy: the console is a same-origin client of the
  // guarded API (ADR-0002 / UI_CONTRACTS), so the dev server forwards API
  // traffic — including the WebSocket upgrade — to the local controller.
  server: {
    proxy: {
      "/api/v1": {
        target: "http://127.0.0.1:8080",
        changeOrigin: false,
        ws: true,
      },
      "/healthz": {
        target: "http://127.0.0.1:8080",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // The design loop's round backups (web/.design-shots/_backups/*/) hold
    // frozen copies of earlier rounds' *.test.* files — loop backups are not
    // suite members, and vitest otherwise collects them (stale-collection
    // failures on a plain run). Setting `exclude` REPLACES vitest's defaults,
    // so the defaults (vitest 3.2.4 defaultExclude) are mirrored verbatim
    // below with the backup vault appended.
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build,eslint,prettier}.config.*",
      "**/.design-shots/**",
    ],
  },
});
