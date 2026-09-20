import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { coverageConfigDefaults, defineConfig } from 'vitest/config';

// The backend listens on the loopback interface during local development.
// Every request the app makes is same-origin (`/api/...`); in development the
// dev server proxies that prefix, and in production the app is served from the
// same origin as the API, so no base URL ever has to be configured.
const BACKEND_ORIGIN = 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Mirrors the `@/*` path alias declared in tsconfig.app.json.
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        ...coverageConfigDefaults.exclude,
        // Composition root: it only wires providers together and cannot run
        // outside a real browser document.
        'src/main.tsx',
        // Checked-in output of `npm run gen:api`, not hand-written code.
        'src/api/generated/**',
        // Test scaffolding (MSW server, global setup), not production code.
        'src/test/**',
        '**/*.config.{ts,js}',
      ],
      // These thresholds are deliberately low: this is a walking skeleton with
      // a single page, so a high bar here would measure nothing. The rule for
      // this project is that they only ever ratchet upward - never lower a
      // threshold to make a build green.
      thresholds: {
        lines: 60,
        branches: 60,
        functions: 60,
        statements: 60,
      },
    },
  },
});
