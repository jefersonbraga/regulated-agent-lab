import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { Theme } from "@astryxdesign/core/theme";
import { stoneTheme } from "@astryxdesign/theme-stone/built";
import App, { type ThemeMode } from "./App";
import { demoClient, parseDemo } from "./demo";
import "./index.css";

const MODE_KEY = "agent-console-mode";

/** The remembered choice, else the system colour scheme, else dark. */
function initialMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(MODE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Blocked storage: fall through to the system preference.
  }
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

// Dev-only fixture replay (src/demo.ts); null in production builds.
const demo = parseDemo(window.location.search);
const client = demo ? demoClient(demo) : undefined;

/** The header toggle switches mode and is remembered per browser. */
function Root() {
  const [mode, setMode] = useState<ThemeMode>(initialMode);
  function change(next: ThemeMode) {
    setMode(next);
    try {
      localStorage.setItem(MODE_KEY, next);
    } catch {
      // Private mode or blocked storage: the choice lasts for this page only.
    }
  }
  return (
    <Theme theme={stoneTheme} mode={mode}>
      <App
        themeMode={mode}
        onThemeModeChange={change}
        client={client}
        paceMs={demo?.paceMs}
        demoLabel={demo ? `demo: ${demo.scenario}` : undefined}
      />
    </Theme>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
