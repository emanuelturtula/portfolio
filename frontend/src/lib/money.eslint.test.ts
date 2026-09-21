import { ESLint, type Linter } from 'eslint';
import tseslint from 'typescript-eslint';
import { describe, expect, it } from 'vitest';

/**
 * Proves the money coercion ban in the real `eslint.config.js` actually fires.
 *
 * An untested lint rule is a rule that silently stops firing: a typo in a
 * selector, a `files` glob that no longer matches the directory the code moved
 * to, and the guarantee is gone with nothing failing. This test runs the
 * shipped config through the ESLint Node API over fixture source.
 *
 * Two things make it honest rather than decorative:
 *
 * 1. It asserts the **absence** of an error for legitimate constructs
 *    (`Number.isFinite`, unary minus, binary plus) as well as the presence of
 *    one for each violation. A rule that fires on everything passes a
 *    presence-only test and makes the codebase unlintable.
 * 2. It lints each fixture under a `filePath` inside a directory the ban
 *    actually covers. A path outside the scope produces no error for the right
 *    reason and would make this whole file pass for the wrong one.
 *
 * Type-aware parsing is switched off for the fixtures only. The TypeScript
 * project service resolves a file against `tsconfig.app.json` and refuses a
 * path that is not on disk, and writing fixture files into `src/` would leave
 * lint violations in the tree if the run crashed. The three rules under test -
 * `no-restricted-globals`, `no-restricted-properties` and `no-restricted-syntax`
 * - are purely syntactic and do not consult type information, so nothing under
 * test is weakened by it.
 */

/** The rules the money ban is built from. Everything else is noise here. */
const MONEY_RULES = new Set([
  'no-restricted-globals',
  'no-restricted-properties',
  'no-restricted-syntax',
]);

// Vitest sets the working directory to the Vite project root, which is
// `frontend/` - the directory that holds `eslint.config.js`. `import.meta.url`
// is not a `file:` URL inside a transformed test module, so `process.cwd()` is
// the way to name it. ESLint throws when it finds no config file, so a wrong
// directory fails loudly rather than silently linting against nothing.
const FRONTEND_ROOT = process.cwd();

const eslint = new ESLint({
  cwd: FRONTEND_ROOT,
  overrideConfig: [
    {
      files: ['**/*.{ts,tsx}'],
      ...tseslint.configs.disableTypeChecked,
      languageOptions: { parserOptions: { projectService: false } },
    },
  ],
});

/** Lints one snippet as if it were the given file and keeps the money errors. */
async function moneyErrors(code: string, filePath: string): Promise<Linter.LintMessage[]> {
  const [result] = await eslint.lintText(`${code}\n`, { filePath, warnIgnored: false });

  if (result === undefined) {
    throw new Error(`ESLint returned no result for ${filePath}.`);
  }

  const parseError = result.messages.find((message) => message.fatal === true);
  if (parseError !== undefined) {
    throw new Error(`ESLint could not parse the fixture: ${parseError.message}`);
  }

  return result.messages.filter(
    (message) => message.ruleId !== null && MONEY_RULES.has(message.ruleId),
  );
}

/** A file in a directory the ban covers. */
const BANNED_FILE = 'src/lib/exchange-rate.ts';

describe('the money coercion lint rule', () => {
  it.each([
    ['parseFloat', 'export const amount = parseFloat("1.10");'],
    ['parseInt', 'export const amount = parseInt("110", 10);'],
    ['Number(x)', 'export const amount = Number("1.10");'],
    ['unary +', 'export const amount = +"1.10";'],
    ['Number.parseFloat', 'export const amount = Number.parseFloat("1.10");'],
    ['Number.parseInt', 'export const amount = Number.parseInt("110", 10);'],
  ])('rejects %s in a directory that handles money', async (_label, code) => {
    const errors = await moneyErrors(code, BANNED_FILE);

    expect(errors).toHaveLength(1);
    // An error, not a warning: a warning does not fail `npm run lint`.
    expect(errors[0]?.severity).toBe(2);
  });

  it.each([
    ['Number.isFinite', 'export const ok = Number.isFinite(1);'],
    ['Number.isInteger', 'export const ok = Number.isInteger(1);'],
    ['Number.MAX_SAFE_INTEGER', 'export const ok = Number.MAX_SAFE_INTEGER;'],
    ['String()', 'export const ok = String(1);'],
    ['unary minus', 'export const ok = -Number.EPSILON;'],
    ['a negative literal', 'export const ok = -1;'],
    ['binary plus', 'export const ok = "a" + "b";'],
    ['string concatenation assignment', 'export let ok = "a"; ok += "b";'],
  ])('leaves %s alone', async (_label, code) => {
    // The whole point of scoping the ban to a selector rather than banning the
    // `Number` identifier outright. If these fire, the rule is unusable and
    // someone will switch it off rather than fix their code.
    expect(await moneyErrors(code, BANNED_FILE)).toHaveLength(0);
  });

  it.each([
    'src/lib/exchange-rate.ts',
    'src/features/wallets/total.ts',
    'src/components/Balance.tsx',
    'src/pages/DashboardPage.tsx',
  ])('covers %s, where money is actually handled', async (filePath) => {
    expect(await moneyErrors('export const amount = Number("1.10");', filePath)).toHaveLength(1);
    expect(await moneyErrors('export const amount = +"1.10";', filePath)).toHaveLength(1);
  });

  it.each(['src/api/client.ts', 'src/test/server.ts'])(
    'leaves %s outside the ban, where non-monetary parsing belongs',
    async (filePath) => {
      // Reading a status code, a pixel offset or a page number is legitimate.
      // A ban everywhere is a ban people route around.
      expect(await moneyErrors('export const page = Number("2");', filePath)).toHaveLength(0);
    },
  );

  it('exempts test files, so a test may write the violation it proves', async () => {
    expect(
      await moneyErrors('export const amount = +"1.10";', 'src/lib/money.test.ts'),
    ).toHaveLength(0);
  });
});
