import "@testing-library/jest-dom/vitest";
import { act, cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeAll, beforeEach, vi } from "vitest";
import { Button } from "@astryxdesign/core/Button";

// jsdom gaps used by Astryx components (same polyfills as the Astryx test setup).
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: /\(\s*hover\s*:\s*hover\s*\)/.test(query),
      media: query,
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}
if (!HTMLElement.prototype.showPopover) {
  HTMLElement.prototype.showPopover = function () {};
  HTMLElement.prototype.hidePopover = function () {};
  HTMLElement.prototype.togglePopover = function () {
    return false;
  };
}
if (typeof globalThis.IntersectionObserver === "undefined") {
  globalThis.IntersectionObserver = class {
    readonly root = null;
    readonly rootMargin = "";
    readonly thresholds = [];
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  } as unknown as typeof IntersectionObserver;
}

// React state updates outside act() mean a test stopped before the component settled.
// Fail the test instead of only printing a warning to CI output.
// Astryx Button lazy-loads its Tooltip module after its first render and sets state when the
// import resolves. Warm that module cache once, inside act, so no test sees the late update.
beforeAll(async () => {
  await act(async () => {
    render(createElement(Button, { label: "warm-up" }));
    await import("@astryxdesign/core/Tooltip");
  });
  cleanup();
});

let actWarnings: string[] = [];
const originalError = console.error;

beforeEach(() => {
  actWarnings = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    const text = args.map(String).join(" ");
    if (text.includes("not wrapped in act(")) {
      actWarnings.push(text.split("\n")[0]);
      return;
    }
    originalError(...args);
  });
});

afterEach(async () => {
  // Let pending microtasks (e.g. a promise resolved at the end of a test) flush first.
  await new Promise((r) => setTimeout(r, 0));
  vi.mocked(console.error).mockRestore();
  if (actWarnings.length > 0) {
    throw new Error(`React act() warning: ${actWarnings[0]}`);
  }
});
