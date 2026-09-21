import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import { globalIgnores } from 'eslint/config';
import tseslint from 'typescript-eslint';

/**
 * Monetary values cross the wire as JSON strings, never as JSON numbers,
 * because IEEE-754 doubles cannot represent decimal amounts exactly. Coercing
 * one of those strings with `parseFloat`, `Number()`, `parseInt` or unary `+`
 * silently reintroduces the rounding errors the string representation exists
 * to prevent, and `parseInt` additionally truncates everything after the
 * decimal point.
 *
 * The ban is deny-by-default across all of `src/**`, not an allowlist of the
 * directories money happens to live in today. An allowlist makes every
 * directory not yet named safe by omission: a future `src/hooks/`,
 * `src/utils/` or `src/store/` would be free to `parseFloat` a balance and
 * nothing would fail, and the diagnosis would arrive downstream in a wrong
 * number rather than at review time in a lint error. CLAUDE.md rule 8 makes
 * the identical choice for endpoints - deny-by-default, in middleware, so
 * that forgetting to opt in is what fails - and this rule now matches it:
 * adding a directory is automatically covered, and only *removing* protection
 * from one is a visible line in this file.
 *
 * `ignores` below is the deliberate, narrow exemption: `src/api/**` and
 * `src/test/**` are where legitimate non-monetary parsing lives (HTTP status
 * codes, response plumbing, test fixtures), and test files everywhere are
 * exempt because a test proving the rule fires has to be able to write the
 * violation. `src/lib/money.ts` and `<Money>` are what everywhere else uses
 * instead.
 */
const noNumberCoercionMessage =
  'Monetary values arrive from the API as strings and must never be coerced into a JavaScript number: IEEE-754 doubles cannot represent them exactly. Use the decimal helpers instead of parseFloat, parseInt, Number() or unary +.';

export default tseslint.config(
  globalIgnores(['dist', 'coverage', 'src/api/generated']),
  {
    files: ['**/*.{ts,tsx,js}'],
    extends: [
      js.configs.recommended,
      // Type-aware linting: `projectService` resolves each file to the nearest
      // tsconfig, which is what makes rules like no-floating-promises work.
      tseslint.configs.strictTypeChecked,
      tseslint.configs.stylisticTypeChecked,
    ],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [reactHooks.configs.flat.recommended, jsxA11y.flatConfigs.recommended],
  },
  {
    // The ESLint config itself is plain JavaScript and is not part of any
    // tsconfig project, so type-aware rules cannot run against it.
    files: ['**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
  },
  {
    // See the comment above `noNumberCoercionMessage`.
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/api/**', 'src/test/**', '**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-globals': [
        'error',
        { name: 'parseFloat', message: noNumberCoercionMessage },
        { name: 'parseInt', message: noNumberCoercionMessage },
      ],
      'no-restricted-properties': [
        'error',
        { object: 'Number', property: 'parseFloat', message: noNumberCoercionMessage },
        { object: 'Number', property: 'parseInt', message: noNumberCoercionMessage },
      ],
      'no-restricted-syntax': [
        'error',
        // Targets the `Number(value)` call specifically, so that
        // `Number.isFinite` and friends stay available.
        {
          selector: 'CallExpression[callee.name="Number"]',
          message: noNumberCoercionMessage,
        },
        // Unary `+` is the same coercion spelled differently: `+value` calls
        // the same `ToNumber` abstract operation `Number(value)` does.
        {
          selector: 'UnaryExpression[operator="+"]',
          message: noNumberCoercionMessage,
        },
      ],
    },
  },
  // Must stay last: it switches off every rule that would fight Prettier.
  prettier,
);
