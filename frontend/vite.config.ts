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
      // Set to the value measured on #4, which is what the project rule asks
      // for: thresholds only ever ratchet upward, and never come down to make
      // a build green. Lowering one needs an explicit justification in the
      // pull request description.
      //
      // Every branch of the shipped frontend is exercised, including the ones
      // that only a misbehaving backend reaches - a problem document with no
      // `detail`, a 200 whose body is not JSON, a response with no reason
      // phrase. Those are the branches that never run in development and
      // always run on the Pi at 3am, so 100 here is a statement that they are
      // deliberate rather than an accident of what was easy to test.
      thresholds: {
        lines: 100,
        branches: 100,
        functions: 100,
        statements: 100,
      },
    },
  },
});
