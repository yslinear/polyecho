import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

/**
 * Vitest config for @polyecho/web.
 *
 * - jsdom environment so React Testing Library can render components.
 * - No DB / no network: lib/db and server actions are mocked per-test.
 * - `vitest run` (the `test` script) is headless and exits; safe for CI.
 *   The CI agent wires `pnpm --filter @polyecho/web test`.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Mirror tsconfig "@/*" -> "./*"
      "@": resolve(__dirname, "."),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next", "coverage"],
    restoreMocks: true,
    clearMocks: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      reportsDirectory: "./coverage",
      include: [
        "lib/**/*.{ts,tsx}",
        "components/**/*.{ts,tsx}",
        "app/**/*.{ts,tsx}",
      ],
      exclude: [
        "**/*.test.{ts,tsx}",
        "**/*.d.ts",
        "lib/db/index.ts", // thin postgres-js wrapper; needs a live DB
        "app/**/page.tsx", // server components hit the DB; islands tested instead
        "app/**/layout.tsx",
        "components/ui/sidebar.tsx", // large vendored shadcn primitive
        "components/ui/sheet.tsx",
        "components/ui/chart.tsx", // vendored recharts wrapper
        "components/ui/sonner.tsx",
        "components/nav-sidebar.tsx", // legacy, superseded by app-sidebar
        "components/data-chart.tsx",
      ],
      // Thresholds based on the suite actually written here. Kept modest so a
      // single new untested file does not turn CI red; raise as coverage grows.
      thresholds: {
        statements: 60,
        branches: 70,
        functions: 55,
        lines: 60,
      },
    },
  },
});
