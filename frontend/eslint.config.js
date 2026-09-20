import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import { globalIgnores } from 'eslint/config';
import tseslint from 'typescript-eslint';

/**
 * Monetary values cross the wire as JSON strings, never as JSON numbers,
 * because IEEE-754 doubles cannot represent decimal amounts exactly. Coercing
 * one of those strings with `parseFloat`, `Number()` or `parseInt` silently
 * reintroduces the rounding errors the string representation exists to prevent,
 * and `parseInt` additionally truncates everything after the decimal point.
 *
 * The ban is scoped to the directories where money will actually be handled -
 * feature code (`src/features/**`) and shared domain helpers (`src/lib/**`) -
 * so that legitimate non-monetary parsing elsewhere (reading a pixel offset,
 * a page number from a query string) does not have to fight the rule. When the
 * money value object lands, it is what these files must use instead.
 */
const noNumberCoercionMessage =
  'Monetary values arrive from the API as strings and must never be coerced into a JavaScript number: IEEE-754 doubles cannot represent them exactly. Use the decimal helpers instead of parseFloat, parseInt or Number().';

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
    files: ['src/features/**/*.{ts,tsx}', 'src/lib/**/*.{ts,tsx}'],
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
      // Targets the `Number(value)` call specifically, so that `Number.isFinite`
      // and friends stay available.
      'no-restricted-syntax': [
        'error',
        {
          selector: 'CallExpression[callee.name="Number"]',
          message: noNumberCoercionMessage,
        },
      ],
    },
  },
  // Must stay last: it switches off every rule that would fight Prettier.
  prettier,
);
