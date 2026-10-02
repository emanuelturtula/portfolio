import { readFileSync } from 'node:fs';
import path from 'node:path';

import { ESLint, type Linter } from 'eslint';
import tseslint from 'typescript-eslint';
import { describe, expect, it } from 'vitest';

/**
 * Spec 027, acceptance criterion 16: "No amount is parsed as a number, summed or compared as
 * a string anywhere in the new code."
 *
 * The behaviour is tested where it shows - eighteen places carried from the form to the list
 * and back, the endpoint's order kept. This file is the other half: the shipped money lint
 * rule, run over the **real source** of every file the page added, so "the rule passes on the
 * new code" is something a test says and not something a reviewer remembers to check.
 *
 * `money.eslint.test.ts` proves the rule fires on fixtures. A rule that fires on fixtures and
 * is never pointed at the new files proves nothing about them, and one of the new files -
 * `src/api/adjustments.ts` - lives in a directory the rule exempts. So each source is linted
 * twice over: under its own path when the ban covers it, and under a path the ban is known to
 * cover when it does not.
 *
 * Type-aware parsing is switched off, for the reason `money.eslint.test.ts` gives: the rules
 * under test are syntactic.
 */

const FRONTEND_ROOT = process.cwd();

/** Every source file spec 027 added, and the two existing modules it added money code to. */
const NEW_SOURCES = [
  'src/api/adjustments.ts',
  'src/lib/adjustments.ts',
  'src/lib/money.ts',
  'src/pages/AdjustmentsPage.tsx',
  'src/pages/adjustments/AdjustmentForm.tsx',
  'src/pages/adjustments/AdjustmentList.tsx',
  'src/pages/adjustments/FailedRecomputeAlert.tsx',
  'src/pages/dashboard/HoldingsLists.tsx',
] as const;

/** The files that are only about adjustments: nothing in them has a reason to order or round. */
const PAGE_SOURCES = NEW_SOURCES.filter(
  (file) => file !== 'src/lib/money.ts' && file !== 'src/pages/dashboard/HoldingsLists.tsx',
);

const MONEY_RULES = new Set([
  'no-restricted-globals',
  'no-restricted-properties',
  'no-restricted-syntax',
]);

const untyped: Linter.Config = {
  files: ['**/*.{ts,tsx}'],
  ...tseslint.configs.disableTypeChecked,
  languageOptions: { parserOptions: { projectService: false } },
};

/** The shipped configuration, as `npm run lint` runs it, minus type information. */
const shipped = new ESLint({ cwd: FRONTEND_ROOT, overrideConfig: [untyped] });

/**
 * The shipped configuration with one more rule: no ordering and no rounding. An adjustment
 * list is shown in the endpoint's order, and an amount is never rounded outside
 * `lib/money.ts`; a `sort` or a `toFixed` in one of these files would be either a string
 * comparison of amounts or a float in disguise, and neither has another use here.
 */
const strict = new ESLint({
  cwd: FRONTEND_ROOT,
  overrideConfig: [
    untyped,
    {
      files: ['src/**/*.{ts,tsx}'],
      rules: {
        'no-restricted-syntax': [
          'error',
          {
            selector:
              'CallExpression[callee.property.name=/^(sort|toSorted|localeCompare|toFixed|toPrecision)$/]',
            message:
              'Adjustments are shown in the order served, and amounts are never rounded here.',
          },
          {
            selector: 'MemberExpression[object.name="Math"]',
            message: 'No arithmetic on a float in the adjustments code.',
          },
        ],
      },
    },
  ],
});

function sourceOf(file: string): string {
  return readFileSync(path.join(FRONTEND_ROOT, file), 'utf8');
}

async function moneyErrors(
  eslint: ESLint,
  code: string,
  filePath: string,
): Promise<Linter.LintMessage[]> {
  const [result] = await eslint.lintText(code, { filePath, warnIgnored: false });
  if (result === undefined) {
    throw new Error(`ESLint returned no result for ${filePath}.`);
  }
  const parseError = result.messages.find((message) => message.fatal === true);
  if (parseError !== undefined) {
    throw new Error(`ESLint could not parse ${filePath}: ${parseError.message}`);
  }
  return result.messages.filter(
    (message) => message.ruleId !== null && MONEY_RULES.has(message.ruleId),
  );
}

/** A path the ban covers, standing in for a file in a directory the ban exempts. */
function coveredPathFor(file: string): string {
  return file.startsWith('src/api/') ? file.replace('src/api/', 'src/lib/api-') : file;
}

describe('the money lint rule, over the source spec 027 added (criterion 16)', () => {
  it.each(NEW_SOURCES)('%s is read, and is not empty', (file) => {
    // The control on every test below: a wrong path would lint an empty string and pass.
    expect(sourceOf(file).length).toBeGreaterThan(200);
  });

  it.each(NEW_SOURCES)('fires on a coercion written into %s', async (file) => {
    // The second control: at the path the source is linted under, the ban is in force. A
    // source that passes below passes because it holds no coercion, not because nothing looks.
    const tampered = `${sourceOf(file)}\nexport const tampered = Number('1.10');\n`;

    const errors = await moneyErrors(shipped, tampered, coveredPathFor(file));

    expect(errors).toHaveLength(1);
    expect(errors[0]?.severity).toBe(2);
  });

  it.each(NEW_SOURCES)('finds no coercion of an amount in %s', async (file) => {
    expect(await moneyErrors(shipped, sourceOf(file), coveredPathFor(file))).toEqual([]);
  });

  it('exempts src/api/adjustments.ts under its own path, which is why it is linted under another', async () => {
    // If this stops holding, the exemption has been narrowed and the alias above is no longer
    // needed; that is a change to `eslint.config.js` worth seeing here.
    const tampered = `${sourceOf('src/api/adjustments.ts')}\nexport const tampered = Number('1.10');\n`;

    expect(await moneyErrors(shipped, tampered, 'src/api/adjustments.ts')).toEqual([]);
  });
});

describe('no ordering and no rounding in the adjustments code (criterion 16)', () => {
  it.each([
    ['a sort', 'export const ordered = ["10", "9"].sort();'],
    ['a string comparison', 'export const order = "10".localeCompare("9");'],
    ['a rounding', 'export const rounded = (0.1).toFixed(2);'],
    ['float arithmetic', 'export const total = Math.round(0.1 + 0.2);'],
  ])('the check fires on %s', async (_label, code) => {
    expect(await moneyErrors(strict, `${code}\n`, 'src/lib/adjustments.ts')).toHaveLength(1);
  });

  it.each(PAGE_SOURCES)('%s neither orders nor rounds anything', async (file) => {
    expect(await moneyErrors(strict, sourceOf(file), coveredPathFor(file))).toEqual([]);
  });
});
