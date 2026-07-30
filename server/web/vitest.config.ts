import { defineConfig } from "vitest/config";

/**
 * Test config, kept separate from `vite.config.ts` on purpose.
 *
 * Putting a `test` block in the build config would mean importing
 * `defineConfig` from `vitest/config` there, which makes the production build's
 * configuration depend on a test runner. Vitest prefers this file when it
 * exists, so the build config stays exactly what it was.
 *
 * No `@vitejs/plugin-react` here either: its job is fast refresh in the dev
 * server, and JSX is transformed by esbuild from the `jsx: "react-jsx"` already
 * in tsconfig. A plugin that does nothing in this context would only be one
 * more thing that has to keep working.
 */
export default defineConfig({
  test: {
    // Only the component tests need a DOM; the cost of giving it to the pure
    // ones is a few milliseconds, and the alternative is per-file environment
    // pragmas that get forgotten on the next file.
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    restoreMocks: true,
  },
});
