import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Children, isValidElement, type ReactNode } from 'react';
import { Route } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';
import { threeAdjustments, threeFirstTrades } from '@/test/adjustmentFixtures';
import {
  accountFailed,
  accountSucceeded,
  authFailedExchange,
  erroredExchange,
  finishedRun,
  syncTriggered,
  truncatedExchange,
} from '@/test/exchangeFixtures';
import { fakeAccounting } from '@/test/fakeAccounting';
import { fakeAdjustments } from '@/test/fakeAdjustments';
import { fakeExchanges, type FakeExchangesOptions } from '@/test/fakeExchanges';
import { fakePortfolio } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { renderApp } from '@/test/render';
import { fakeSession, server, TEST_USERNAME } from '@/test/server';

/**
 * Criterion 6 of #16: no credential input exists anywhere in the UI.
 *
 * CLAUDE.md rule 3: exchange keys are read from environment variables on the
 * host into `SecretStr`, never persisted, never returned. A field that
 * accepted one would invite the owner to type a secret into a page, and
 * whatever it did next - a request, a log line, the browser's autofill store -
 * would be a leak this repository exists to prevent. So the test walks every
 * route and inspects every control on it.
 */

/** Named, labelled, `id`-ed or `name`-d like this: the control is suspect. */
const CREDENTIAL_LIKE = /key|secret|passphrase|token|credential/i;

/** Every control a person can type or choose a value in. */
const CONTROL_SELECTOR =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"])';

/** Every `path` in `App`'s route table, read from the element tree `App` returns. */
function routePaths(node: ReactNode): string[] {
  const paths: string[] = [];

  Children.forEach(node, (child) => {
    if (!isValidElement<{ path?: unknown; children?: ReactNode }>(child)) {
      return;
    }
    if (child.type === Route && typeof child.props.path === 'string') {
      paths.push(child.props.path);
    }
    paths.push(...routePaths(child.props.children));
  });

  return paths;
}

/** Everything a control is called by: its name, its labels, its id and its name attribute. */
function namesOf(control: Element): string[] {
  const names: (string | null)[] = [
    control.id,
    control.getAttribute('name'),
    control.getAttribute('aria-label'),
    control.getAttribute('placeholder'),
    control.getAttribute('title'),
    control.getAttribute('autocomplete'),
  ];

  for (const id of (control.getAttribute('aria-labelledby') ?? '').split(/\s+/)) {
    if (id !== '') {
      names.push(document.getElementById(id)?.textContent ?? null);
    }
  }
  if (
    control instanceof HTMLInputElement ||
    control instanceof HTMLTextAreaElement ||
    control instanceof HTMLSelectElement
  ) {
    for (const label of Array.from(control.labels ?? [])) {
      names.push(label.textContent);
    }
  }
  const wrapping = control.closest('label');
  if (wrapping !== null) {
    names.push(wrapping.textContent);
  }

  return names.filter((name): name is string => name !== null && name !== '');
}

function expectNoCredentialControl(): number {
  const controls = Array.from(document.body.querySelectorAll(CONTROL_SELECTOR));

  for (const control of controls) {
    for (const name of namesOf(control)) {
      expect(name, `a ${control.tagName.toLowerCase()} is called "${name}"`).not.toMatch(
        CREDENTIAL_LIKE,
      );
    }
  }

  return controls.length;
}

/**
 * The transaction filters of spec 024: a checkbox per venue and two day pickers, each named
 * by its label. The only controls the exchanges page has, and each one's type and name is
 * pinned, so a field added anywhere on the page, or a filter that changes into something a
 * value could be typed into, fails here.
 */
const TRANSACTION_FILTERS: readonly string[] = [
  'checkbox BingX',
  'checkbox Bitget',
  'date From',
  'date To',
];

function expectOnlyTheTransactionFilters(main: HTMLElement): void {
  const filters = within(main).getByRole('group', { name: 'Transaction filters' });
  const controls = Array.from(main.querySelectorAll(CONTROL_SELECTOR));

  for (const control of controls) {
    expect(filters.contains(control), `a ${control.tagName} outside the filters`).toBe(true);
  }
  expect(
    controls.map((control) =>
      control instanceof HTMLInputElement
        ? `${control.type} ${Array.from(control.labels ?? [])
            .map((label) => label.textContent.trim())
            .join(' ')}`
        : control.tagName.toLowerCase(),
    ),
  ).toEqual(TRANSACTION_FILTERS);
  expectNoCredentialControl();
}

function passwordInputs(): Element[] {
  return Array.from(document.body.querySelectorAll('input')).filter(
    (input) => input.type === 'password',
  );
}

/**
 * The richest exchanges page the backend can serve: a refused key with its
 * remediation, an outage, a truncation banner, and a run log with an error.
 * Every sentence that names a credential variable is on screen.
 */
const EXCHANGES_SCENARIO: FakeExchangesOptions = {
  exchanges: [truncatedExchange({ exchange_key: 'bingx' }), authFailedExchange('auth')],
  runs: [
    finishedRun({
      trigger: 'scheduled',
      accounts: [
        accountSucceeded('bingx', { fills_seen: 3, fills_inserted: 3 }),
        accountFailed('bitget', 'auth'),
      ],
    }),
  ],
  onSync: () =>
    syncTriggered(
      finishedRun({
        run_id: 9,
        trigger: 'manual',
        accounts: [accountSucceeded('bingx'), accountFailed('bitget', 'auth')],
      }),
    ),
};

interface RouteCase {
  /** The entry in `App`'s table. */
  readonly route: string;
  /** Where to go to reach it. */
  readonly visit: string;
  readonly signedIn: boolean;
  /** Resolves once the page has rendered what it renders with data. */
  readonly ready: () => Promise<unknown>;
  readonly passwordInputs: number;
}

const ROUTE_CASES: readonly RouteCase[] = [
  {
    route: '/login',
    visit: '/login',
    signedIn: false,
    ready: () => screen.findByRole('button', { name: /sign in/i }),
    passwordInputs: 1,
  },
  {
    route: '/',
    visit: '/',
    signedIn: true,
    ready: () => screen.findByRole('region', { name: 'Total value' }),
    passwordInputs: 0,
  },
  {
    route: '/wallets',
    visit: '/wallets',
    signedIn: true,
    ready: async () => {
      await screen.findByRole('form', { name: 'Add a wallet' });
      return within(await screen.findByRole('region', { name: 'Your wallets' })).findByRole('list');
    },
    passwordInputs: 0,
  },
  {
    route: '/exchanges',
    visit: '/exchanges',
    signedIn: true,
    ready: () => screen.findByRole('listitem', { name: 'Bitget' }),
    passwordInputs: 0,
  },
  {
    route: '/adjustments',
    visit: '/adjustments?asset=BTC',
    signedIn: true,
    ready: async () => {
      await screen.findByRole('form', { name: 'Record an adjustment' });
      // The suggestion's button and the rows' controls are on screen too.
      await screen.findByRole('button', { name: /^Use / });
      return within(await screen.findByRole('region', { name: 'Recorded adjustments' })).findByRole(
        'table',
      );
    },
    passwordInputs: 0,
  },
  {
    route: '/health',
    visit: '/health',
    signedIn: true,
    ready: () => screen.findByText(/backend health/i),
    passwordInputs: 0,
  },
  {
    route: '*',
    visit: '/nowhere-at-all',
    signedIn: true,
    ready: () => screen.findByRole('heading', { name: /not found/i }),
    passwordInputs: 0,
  },
];

function serve(signedIn: boolean, exchanges: FakeExchangesOptions = EXCHANGES_SCENARIO): void {
  const session = fakeSession(signedIn ? { initialUser: TEST_USERNAME } : {});
  const scenario = healthyPortfolio();
  server.use(
    ...session.handlers,
    ...fakePortfolio({ ...scenario, session }).handlers,
    ...fakeExchanges({ ...exchanges, session }).handlers,
    ...fakeAccounting({ session }).handlers,
    ...fakeAdjustments({
      adjustments: threeAdjustments(),
      firstTrades: threeFirstTrades(),
      session,
    }).handlers,
  );
}

describe('credentials', () => {
  it("visits every route in App's table", () => {
    // Read from the element tree, so a route added without a case here fails
    // this test rather than going unwalked.
    const declared = routePaths(App()).sort();

    expect(declared).toEqual(ROUTE_CASES.map((entry) => entry.route).sort());
  });

  it.each(ROUTE_CASES)(
    'has no credential-like control on $route, and a password input only on /login',
    async ({ visit, signedIn, ready, passwordInputs: expected }) => {
      serve(signedIn);

      renderApp([visit]);
      await ready();

      expectNoCredentialControl();
      expect(passwordInputs()).toHaveLength(expected);
    },
  );

  it('the exchanges page has no form control but its buttons and the transaction filters', async () => {
    const user = userEvent.setup();
    serve(true);

    renderApp(['/exchanges']);
    const bitget = await screen.findByRole('listitem', { name: 'Bitget' });

    // The page that names every credential variable, and asks the owner to
    // act on a refused key, offers nowhere to type one.
    expect(bitget).toHaveTextContent('PORTFOLIO_BITGET_API_SECRET');
    const main = screen.getByRole('main');
    expectOnlyTheTransactionFilters(main);
    expect(main.querySelector('form')).toBeNull();
    expect(within(main).queryAllByRole('textbox')).toHaveLength(0);

    // Nor after a sync has run and summarised itself.
    await user.click(screen.getByRole('button', { name: 'Sync now' }));
    await screen.findByText(/^The sync /);

    expectOnlyTheTransactionFilters(main);
    expect(main.querySelector('form')).toBeNull();
    expect(passwordInputs()).toHaveLength(0);
  });

  it('the adjustments page has the five fields of an adjustment and no other control', async () => {
    // Spec 027. The one page besides the wallets with a form on it, so each control's type
    // and name is pinned: a field added to it - a key to import from a venue, say - fails here.
    const user = userEvent.setup();
    serve(true);

    renderApp(['/adjustments']);
    const form = await screen.findByRole('form', { name: 'Record an adjustment' });
    const region = await screen.findByRole('region', { name: 'Recorded adjustments' });

    const controls = (): Element[] =>
      Array.from(screen.getByRole('main').querySelectorAll(CONTROL_SELECTOR));
    const described = (): string[] =>
      controls().map((control) => {
        const labels =
          control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement
            ? Array.from(control.labels ?? [])
                .map((label) => label.textContent.trim())
                .join(' ')
            : '';
        const kind =
          control instanceof HTMLInputElement ? control.type : control.tagName.toLowerCase();
        return `${kind} ${labels}`;
      });
    const FIELDS = [
      'text Asset',
      'text Quantity',
      'text Unit cost (USD)',
      'datetime-local Acquired on',
      'textarea Note',
    ];

    expect(described()).toEqual(FIELDS);
    for (const control of controls()) {
      expect(form.contains(control), `a ${control.tagName} outside the form`).toBe(true);
    }
    expectNoCredentialControl();

    // Nor in edit mode, which is the same form filled in.
    const [edit] = within(region).getAllByRole('button', { name: /^Edit / });
    if (edit === undefined) {
      throw new Error('No row offers Edit.');
    }
    await user.click(edit);
    await screen.findByRole('form', { name: 'Edit adjustment' });

    expect(described()).toEqual(FIELDS);
    expectNoCredentialControl();
    expect(passwordInputs()).toHaveLength(0);
  });

  it('the exchanges page has no form control when nothing is configured', async () => {
    serve(true, { exchanges: [], runs: [] });

    renderApp(['/exchanges']);
    await screen.findByRole('heading', { name: 'No exchange connected' });

    const main = screen.getByRole('main');
    expect(main.querySelectorAll(CONTROL_SELECTOR)).toHaveLength(0);
    expect(main.querySelector('form')).toBeNull();
  });

  it('an error venue with a remediation-free entry offers no other control either', async () => {
    serve(true, { exchanges: [erroredExchange('unavailable')], runs: [] });

    renderApp(['/exchanges']);
    await screen.findByRole('listitem', { name: 'Bitget' });

    expectOnlyTheTransactionFilters(screen.getByRole('main'));
  });

  it('the check itself fires on a control named like a key', () => {
    // The positive control: a check that never matches passes on a page full
    // of key fields.
    document.body.innerHTML = `
      <label for="venue-api-key">API key</label><input id="venue-api-key" />
    `;
    try {
      expect(() => expectNoCredentialControl()).toThrow();
    } finally {
      document.body.innerHTML = '';
    }

    for (const markup of [
      '<input name="api_secret" />',
      '<input aria-label="Passphrase" />',
      '<textarea placeholder="Paste your token"></textarea>',
      '<select id="credential-kind"></select>',
      '<div contenteditable="true" aria-labelledby="l"></div><span id="l">Secret</span>',
      '<label>Bitget key <input /></label>',
    ]) {
      document.body.innerHTML = markup;
      try {
        expect(() => expectNoCredentialControl(), markup).toThrow();
      } finally {
        document.body.innerHTML = '';
      }
    }
  });
});
