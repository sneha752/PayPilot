import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Vitest runs without injected globals, so Testing Library cannot register
// its own afterEach cleanup — do it explicitly or the DOM accumulates
// across tests.
afterEach(() => {
  cleanup();
});

// jsdom does not implement scrollIntoView (used by the chat auto-scroll and
// the confirmation card); stub it so components can mount in tests.
Element.prototype.scrollIntoView = vi.fn();
