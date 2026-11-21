/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { apiOriginFor, securityHeaders } from "./security-headers";

// Node global, declared here so the config typechecks without @types/node.
declare const process: { env: Record<string, string | undefined> };
// An absolute VITE_API_URL adds its origin to connect-src; a relative one ("/api", the API behind
// the same reverse proxy as the UI, see deploy/) needs nothing beyond 'self'.
const apiOrigin = apiOriginFor(process.env.VITE_API_URL ?? "http://localhost:8000");

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  // The production build is checked under the same headers it is served with (see docker/).
  preview: { headers: securityHeaders(apiOrigin, false) },
  // Astryx colours use native light-dark(); keep CSS targets at its browser floor so the
  // minifier does not lower it into helper variables that break theming.
  build: {
    cssTarget: ["chrome123", "firefox120", "safari17.5"],
    // Astryx is already imported per component (no barrel); its pre-built components and
    // their runtime still make one ~800 kB chunk for this single-page demo. Splitting it
    // would add loading states for no gain, so the warning limit is raised instead.
    chunkSizeWarningLimit: 1300, // the date field of the trip planner adds ~190 kB to the same chunk
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
    // App-level tests drive whole runs through the demo client; under CPU load (a build or the
    // Python suite running alongside) the 5 s default timed some of them out.
    testTimeout: 20000,
  },
});
