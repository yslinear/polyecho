/**
 * Global Vitest setup for @polyecho/web.
 *
 * - Adds @testing-library/jest-dom matchers (toBeInTheDocument, etc.).
 * - Cleans up the DOM between tests.
 * - Polyfills jsdom gaps that Radix UI / Recharts touch
 *   (ResizeObserver, matchMedia, scrollIntoView, PointerEvent capture).
 *
 * No DB and no network are configured here on purpose: every test that would
 * touch `@/lib/db` or a server action mocks it explicitly.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

// Radix UI components observe size and media; jsdom lacks these.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  globalThis.ResizeObserver ?? (ResizeObserverStub as unknown as typeof ResizeObserver);

if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }) as unknown as MediaQueryList;
}

// Radix Select / DropdownMenu call these in jsdom where they are missing.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = vi.fn(() => false);
}
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = vi.fn();
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = vi.fn();
}
