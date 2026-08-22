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
  },
});
